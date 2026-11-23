"""Daily run: Stages D-I. Values the portfolio, computes risk and scenarios, writes reports."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .calibration import calibrate
from .config import EngineConfig
from .conventions import settlement_date, to_date
from .curves import KEYS, BumpInterpolator, ParShift
from .bonds import dirty_price, ytm_from_dirty
from .portfolio import net_by_isin, positions_from_file, valuation_table
from .risk import (KEY_LABELS, PortfolioPricer, convexity, historical_replay, krd_kr01,
                   pc_exposures, scenario_table)
from .store import Store


@dataclass
class RunResult:
    valuation_date: date
    settlement: date
    valuation: pd.DataFrame
    totals: dict
    net_isin: pd.DataFrame
    krd: pd.DataFrame
    kr01: pd.DataFrame
    kr01_summary: pd.DataFrame
    convexity: pd.DataFrame
    risk_summary: dict
    krd_par: pd.DataFrame
    kr01_par: pd.DataFrame
    convexity_par: pd.DataFrame
    pc: pd.DataFrame
    scenarios: pd.DataFrame
    hist: dict
    price_check: pd.DataFrame
    run_log: dict
    flags: list = field(default_factory=list)
    curve_compare: dict = field(default_factory=dict)
    output_dir: Path | None = None


def run_calibration(store: Store, cfg: EngineConfig) -> dict:
    calib, scen = calibrate(store.load_A(), cfg)
    calib["calibrated_at"] = datetime.now().isoformat(timespec="seconds")
    store.save_D(calib, scen)
    return calib


def run(valuation_date, cfg: EngineConfig | None = None, store: Store | None = None,
        write: bool = True) -> RunResult:
    cfg = cfg or EngineConfig()
    store = store or Store(cfg)
    vd = to_date(valuation_date)
    cal = store.calendar()
    flags = []

    if not cal.is_business_day(vd):
        flags.append(f"valuation date {vd} is not a business day")
    if not cal.has_year(vd.year):
        flags.append(f"holiday calendar has no {vd.year} holidays loaded (weekends only)")
    settle = settlement_date(vd, cal, cfg.settlement_lag)

    curve, cinfo = store.curve_for(vd, cfg.curve_source)
    if cinfo["date"] != vd:
        how = ("Enter today's parameters (Stage B)" if cfg.curve_source == "ccil"
               else "Upload today's FBIL file in the Yield builder")
        flags.append(f"STALE CURVE: no {cinfo['label']} curve for {vd}; using {cinfo['date']:%d-%m-%Y}. "
                     f"{how} for an official run.")
    if cinfo["flags"]:
        flags.append(f"NS row flags: {cinfo['flags']}")

    rows = store.load_C()
    if rows.empty:
        raise LookupError("Portfolio (File C) is empty")
    positions = positions_from_file(rows, settle, cal, curve, cfg)
    for p in positions:
        flags.extend(p.flags)

    val, totals = valuation_table(positions, curve)
    netisin = net_by_isin(val)
    compare = curve_comparison(positions, curve, cinfo, store, vd)

    # pricing check against File F
    F = store.load_F()
    pc_rows = []
    if not F.empty:
        today = F[F["date"].dt.date == vd]
        for _, r in val.iterrows():
            m = today[today["isin"] == r["isin"]]
            if len(m):
                diff = (r["clean_price"] - float(m.iloc[0]["clean_price"])) * 100
                pc_rows.append({"pos_id": r["pos_id"], "isin": r["isin"],
                                "model_clean": r["clean_price"],
                                "market_clean": float(m.iloc[0]["clean_price"]),
                                "diff_paise": diff, "flag": abs(diff) > cfg.price_check_paise})
                if abs(diff) > cfg.price_check_paise:
                    flags.append(f"{r['pos_id']}: model vs market {diff:+.1f} paise "
                                 f"(limit {cfg.price_check_paise})")
    price_check = pd.DataFrame(pc_rows)

    interp = BumpInterpolator(cfg.interpolation)
    pp = PortfolioPricer([p for p in positions], curve, interp)
    sens = krd_kr01(pp, cfg.kr_bump_bp)
    conv = convexity(pp, cfg.convexity_bump_bp)
    tol = 1e-5 if cfg.interpolation in ("linear", "cubic") else 1e-2
    if sens["parallel_check_max_abs"] > tol:
        flags.append(f"sum of KRDs differs from parallel duration by "
                     f"{sens['parallel_check_max_abs']:.2e} ({cfg.interpolation})")

    # par-yield basis: bump semi-annual par yields at the key tenors and re-bootstrap
    pp_par = PortfolioPricer(positions, curve, interp, space="par")
    sens_par = krd_kr01(pp_par, cfg.kr_bump_bp)
    conv_par = convexity(pp_par, cfg.convexity_bump_bp)
    if sens_par["parallel_check_max_abs"] > max(tol, 1e-4):
        flags.append(f"sum of par KRDs differs from par-parallel duration by "
                     f"{sens_par['parallel_check_max_abs']:.2e} ({cfg.interpolation})")

    kr01_summary = pd.DataFrame({
        "key_rate": KEY_LABELS,
        "net_KR01_inr": sens["net_kr01"],
        "gross_KR01_inr": sens["gross_kr01_per_key"],
        "portfolio_KRD": sens["portfolio_krd"],
        "net_KR01_par_inr": sens_par["net_kr01"],
        "gross_KR01_par_inr": sens_par["gross_kr01_per_key"],
        "portfolio_KRD_par": sens_par["portfolio_krd"],
    })

    try:
        calib, hist_scen = store.load_D()
    except LookupError:
        calib = run_calibration(store, cfg)
        calib, hist_scen = store.load_D()
        flags.append("calibration (File D) was missing and was rebuilt for this run")
    pcs = pc_exposures(sens["net_kr01"], calib)
    scen = scenario_table(pp, sens, calib, cfg)
    hist = historical_replay(pp, hist_scen, cfg)

    V0 = totals["net_mv_inr"]
    if V0 and totals["gross_mv_inr"] and abs(V0) < 0.05 * totals["gross_mv_inr"]:
        flags.append("net MV is under 5% of gross MV: portfolio duration/convexity ratios are unstable;"
                     " use KR01 and Rs convexity")
    risk_summary = {
        "net_mv_inr": V0,
        "portfolio_duration": sens["portfolio_duration"],
        "net_KR01_total_inr": sens["net_kr01_total"],
        "gross_KR01_inr": sens["gross_kr01_positions"],
        "portfolio_convexity": conv["portfolio_convexity"],
        "dollar_convexity_inr": conv["dollar_convexity_inr"],
        "parallel_check_max_abs": sens["parallel_check_max_abs"],
        "portfolio_duration_par": sens_par["portfolio_duration"],
        "net_KR01_total_par_inr": sens_par["net_kr01_total"],
        "portfolio_convexity_par": conv_par["portfolio_convexity"],
        "dollar_convexity_par_inr": conv_par["dollar_convexity_inr"],
        "parallel_check_par_max_abs": sens_par["parallel_check_max_abs"],
        **{k: v for k, v in hist["stats"].items()},
    }

    run_log = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "valuation_date": str(vd), "settlement_date": str(settle),
        "curve": {**cinfo, "date": str(cinfo["date"])},
        "key_rates_today_pct": dict(zip(KEY_LABELS, curve.zero(KEYS).round(4).tolist())),
        "key_par_yields_today_pct": dict(zip(KEY_LABELS, pp_par.par.par_yields(KEYS).round(4).tolist())),
        "krd_bases": {"spot": "zero (spot) rates bumped at the key tenors",
                      "par": "semi-annual par yields bumped at the key tenors, zero curve re-bootstrapped"},
        "time_basis": cfg.time_basis, "interpolation": cfg.interpolation,
        "calibration_window": [calib["window_start"], calib["window_end"]],
        "calibration_n_changes": calib["n_changes"],
        "positions": len(positions), "flags": flags, "config": cfg.to_dict(),
    }
    res = RunResult(vd, settle, val, totals, netisin, sens["krd"], sens["kr01"], kr01_summary,
                    conv["table"], risk_summary, sens_par["krd"], sens_par["kr01"], conv_par["table"],
                    pcs, scen, hist, price_check, run_log, flags, compare)
    if write:
        res.output_dir = write_reports(res, cfg)
    return res


def curve_comparison(positions, curve, cinfo: dict, store: Store, vd: date) -> dict:
    """Every position priced on both curves (the one in use and the other source), next to the
    market clean price of the valuation date when File F has one (e.g. FBIL)."""
    other = "own" if cinfo["source"] == "ccil" else "ccil"
    try:
        ocurve, oinfo = store.curve_for(vd, other)
    except LookupError as e:
        return {"available": False, "reason": str(e), "active": cinfo["source"], "other": other}
    F = store.load_F()
    mkt = F[F["date"].dt.date == vd].drop_duplicates("isin", keep="last").set_index("isin") if len(F) else F
    cur = {cinfo["source"]: (curve, cinfo), other: (ocurve, oinfo)}
    rows = []
    for p in positions:
        inst = p.instrument
        r = {"pos_id": p.pos_id, "name": p.name, "isin": inst.isin, "signed_face_inr": p.signed_face}
        for k, (c, _) in cur.items():
            dirty = dirty_price(inst, c)
            r[f"{k}_clean"] = dirty - inst.accrued
            r[f"{k}_ytm"] = ytm_from_dirty(inst, dirty)
            r[f"{k}_mv_inr"] = dirty * p.signed_face / 100.0
        r["diff_paise"] = (r["own_clean"] - r["ccil_clean"]) * 100        # own - CCIL
        r["diff_bp"] = (r["own_ytm"] - r["ccil_ytm"]) * 100
        r["diff_mv_inr"] = r["own_mv_inr"] - r["ccil_mv_inr"]
        if len(mkt) and inst.isin in mkt.index:
            m = float(mkt.loc[inst.isin, "clean_price"])
            r["market_clean"], r["market_source"] = m, str(mkt.loc[inst.isin, "source"])
            for k in cur:
                r[f"{k}_vs_market_paise"] = (r[f"{k}_clean"] - m) * 100
        rows.append(r)
    df = pd.DataFrame(rows)
    return {"available": True, "active": cinfo["source"], "other": other,
            "curves": {k: {**i, "date": str(i["date"])} for k, (_, i) in cur.items()},
            "rows": df,
            "totals": {"ccil_mv_inr": float(df["ccil_mv_inr"].sum()), "own_mv_inr": float(df["own_mv_inr"].sum()),
                       "diff_mv_inr": float(df["diff_mv_inr"].sum())}}


def write_reports(res: RunResult, cfg: EngineConfig) -> Path:
    out = Path(cfg.output_dir) / f"{res.valuation_date:%Y-%m-%d}"
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{res.valuation_date:%Y%m%d}"
    res.valuation.to_csv(out / f"valuation_{tag}.csv", index=False)
    res.net_isin.to_csv(out / f"valuation_net_by_isin_{tag}.csv", index=False)
    res.krd.to_csv(out / f"krd_{tag}.csv")
    res.kr01.to_csv(out / f"kr01_by_position_{tag}.csv")
    res.kr01_summary.to_csv(out / f"kr01_portfolio_{tag}.csv", index=False)
    res.convexity.to_csv(out / f"convexity_{tag}.csv")
    res.krd_par.to_csv(out / f"krd_par_{tag}.csv")
    res.kr01_par.to_csv(out / f"kr01_by_position_par_{tag}.csv")
    res.convexity_par.to_csv(out / f"convexity_par_{tag}.csv")
    res.pc.to_csv(out / f"pc_exposures_{tag}.csv", index=False)
    res.scenarios.to_csv(out / f"scenarios_{tag}.csv", index=False)
    res.hist["pnl"].to_csv(out / f"historical_pnl_{tag}.csv", index=False)
    res.hist["worst5"].to_csv(out / f"historical_worst5_{tag}.csv", index=False)
    if res.curve_compare.get("available"):
        res.curve_compare["rows"].to_csv(out / f"curve_comparison_{tag}.csv", index=False)
    if not res.price_check.empty:
        res.price_check.to_csv(out / f"price_check_{tag}.csv", index=False)
    (out / f"risk_summary_{tag}.json").write_text(
        json.dumps({"totals": res.totals, "risk": res.risk_summary}, indent=2, default=float))
    (out / f"run_log_{tag}.json").write_text(json.dumps(res.run_log, indent=2, default=str))
    return out
