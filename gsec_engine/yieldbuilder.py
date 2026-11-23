"""Yield builder: our own zero curve fitted to FBIL G-Sec prices, with portfolio checks.

FBIL publishes a clean price and a semi-annual YTM for every outstanding G-Sec each day
(sheet "G-Sec"), plus its par yield curve (sheet "Par Yield"). We:

1. read the file and rebuild each bond's cash flows (coupon schedule inferred from the maturity
   date; bonds in the portfolio use their File C definition),
2. check that the engine turns FBIL's price into FBIL's yield (tests coupon dates, accrued and
   the yield convention bond by bond),
3. fit a Svensson zero curve (continuously compounded, like the NS curve) by minimising
   duration-weighted price errors, i.e. yield errors in bp. Traded bonds weigh more,
4. compare our curve, a 4-parameter NS fit and CCIL's NS curve on the same bonds, and
5. value the portfolio bonds on each curve against FBIL's prices.

Files: raw uploads in data/raw/fbil/, parsed prices in data/fbil/, fitted parameters in
data/G_fitted_curves.csv; FBIL clean prices are also appended to File F (market prices).
"""
from __future__ import annotations

import calendar as _cal
import io
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import least_squares

from .bonds import Bond, Instrument, instrument_from_bond, ytm_from_dirty
from .config import EngineConfig
from .conventions import add_months, settlement_date, to_date
from .curves import BumpInterpolator, ParShift, SvenssonCurve, key_labels
from .store import Store, bond_from_row

BUCKETS = [(0, 3, "<3Y"), (3, 7, "3-7Y"), (7, 12, "7-12Y"), (12, 20, "12-20Y"), (20, 99, "20Y+")]
G_COLS = ["date", "model", "b0", "b1", "b2", "b3", "tau1", "tau2", "n_bonds", "rmse_bp",
          "rmse_traded_bp", "source", "fitted_at"]
YTM_CHECK_BP = 0.5          # engine yield from FBIL price must match FBIL yield within this


def ns_as_svensson(b0, b1, b2, tau, label=""):
    return SvenssonCurve(b0, b1, b2, 0.0, tau, max(tau, 1.0), label)


# ---------------------------------------------------------------- FBIL file
def _as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
            try:
                return datetime.strptime(v.strip(), fmt).date()
            except ValueError:
                pass
    return None


def read_fbil(src) -> dict:
    """Parse FBIL's daily G-Sec price/yield workbook. `src` is bytes or a path.

    FBIL's download is an .xlsx workbook even when it is named .xls, so the content is
    checked, not the extension."""
    data = src if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise ValueError("this is an old binary .xls file: open it in Excel and save it as .xlsx")
    if data[:2] != b"PK":
        raise ValueError("not an Excel workbook (expected FBIL's G-Sec price/yield file)")
    import openpyxl                       # pip install openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = next((s for s in wb.sheetnames if s.strip().lower() in ("g-sec", "gsec")), None)
    if sheet is None:
        raise ValueError(f"no 'G-Sec' sheet (sheets: {', '.join(wb.sheetnames)})")
    rows = [list(r) for r in wb[sheet].iter_rows(values_only=True)]

    fdate = next((d for r in rows[:8] for d in map(_as_date, r) if d), None)
    if fdate is None:
        raise ValueError("could not find the price date in the first rows of the G-Sec sheet")
    hdr = next((i for i, r in enumerate(rows) if r and str(r[0] or "").strip().upper() == "ISIN"), None)
    if hdr is None:
        raise ValueError("no 'ISIN' header row in the G-Sec sheet")

    out = []
    for r in rows[hdr + 1:]:
        r = r + [None] * (8 - len(r))
        isin = str(r[0] or "").strip().upper()
        if not isin.startswith("IN"):
            continue
        mat = _as_date(r[2])
        try:
            coupon, price, ytm = float(r[1]), float(r[3]), float(r[4])
        except (TypeError, ValueError):
            raise ValueError(f"{isin}: coupon, price and YTM must be numbers")
        if mat is None:
            raise ValueError(f"{isin}: maturity date '{r[2]}' not understood")
        rem = [str(x).strip() if x not in (None, "") else "" for x in r[5:8]]
        out.append({"isin": isin, "coupon": round(coupon, 6), "maturity": mat, "price": price,
                    "ytm": ytm, "remark1": rem[0], "remark2": rem[1], "remark3": rem[2]})
    if not out:
        raise ValueError("no bond rows found under the ISIN header")
    bonds = pd.DataFrame(out)

    par = pd.DataFrame(columns=["tenor", "par_sa", "par_annual"])
    psheet = next((s for s in wb.sheetnames if "par" in s.lower()), None)
    if psheet:
        prow = [list(r) for r in wb[psheet].iter_rows(values_only=True)]
        recs = []
        for r in prow:
            r = r + [None] * (3 - len(r))
            if isinstance(r[0], (int, float)) and isinstance(r[1], (int, float)):
                recs.append({"tenor": float(r[0]), "par_sa": float(r[1]),
                             "par_annual": float(r[2]) if isinstance(r[2], (int, float)) else np.nan})
        if recs:
            par = pd.DataFrame(recs)
    return {"date": fdate, "bonds": bonds, "par": par}


def classify(r) -> tuple[str, str, float]:
    """(kind, tag, fit weight) from FBIL's remark columns: 'FRB' = floating rate bond,
    'Input Point' = used in FBIL's own curve, 'T' = traded that day."""
    r1, r3 = str(r.get("remark1", "")).strip().lower(), str(r.get("remark3", "")).strip().lower()
    if r1 == "frb":
        return "FRB", "floating rate", 0.0
    if r3 == "t":
        return "fixed", "traded", 3.0
    if r1 == "input point":
        return "fixed", "input (proxy)", 2.0
    return "fixed", "valued", 1.0


def infer_bond(isin: str, coupon: float, maturity: date, settle: date) -> Bond:
    """Regular semi-annual bond: coupons on the maturity day-month and six months away.
    The issue date is set to a coupon date two years back (only paid coupons before settlement
    depend on it). New issues with a long or short first coupon still unpaid are caught by the
    price -> yield check."""
    other = add_months(maturity, 6)
    y = min(settle.year - 2, maturity.year - 1)
    issue = date(y, maturity.month, min(maturity.day, _cal.monthrange(y, maturity.month)[1]))
    return Bond(f"{coupon:.2f}% GS {maturity.year}", coupon, issue, maturity,
                ((maturity.day, maturity.month), (maturity.day, other.month)), isin)


# ---------------------------------------------------------------- pricing helpers
def _yield_price(inst: Instrument, y_pct: float) -> float:
    return float(np.sum(inst.amounts / (1.0 + y_pct / 200.0) ** inst.ytm_exponents))


def mod_duration(inst: Instrument, y_pct: float) -> float:
    h = 0.01                                              # 1bp
    p0 = _yield_price(inst, y_pct)
    return (_yield_price(inst, y_pct - h) - _yield_price(inst, y_pct + h)) / (2 * h / 100.0) / p0


def model_dirty(curve, inst: Instrument) -> float:
    return float(np.sum(inst.amounts * np.exp(-curve.zero(inst.times) / 100.0 * inst.times)))


@dataclass
class Universe:
    """FBIL bonds with their cash flows, ready to fit."""
    table: pd.DataFrame            # one row per FBIL bond, with 'used' and 'reason'
    insts: dict                    # isin -> Instrument (bonds that could be built)
    settle: date


def build_universe(fb: pd.DataFrame, settle: date, cal, cfg: EngineConfig, known: dict) -> Universe:
    rows, insts = [], {}
    for r in fb.to_dict("records"):
        kind, tag, w = classify(r)
        rec = {**r, "kind": kind, "tag": tag, "weight": w, "used": False, "reason": "",
               "bond_source": "", "residual_years": (r["maturity"] - settle).days / 365.0,
               "accrued": np.nan, "dirty": np.nan, "mod_duration": np.nan, "ytm_check_bp": np.nan}
        if kind == "FRB":
            rec["reason"] = "floating rate bond: needs its own model"
            rows.append(rec)
            continue
        if rec["residual_years"] < cfg.extrapolation_min_years:
            rec["reason"] = f"matures within {cfg.extrapolation_min_years}Y"
            rows.append(rec)
            continue
        bond = known.get(r["isin"])
        rec["bond_source"] = "portfolio" if bond else "inferred"
        bond = bond or infer_bond(r["isin"], r["coupon"], r["maturity"], settle)
        try:
            inst = instrument_from_bond(bond, settle, cal, cfg.time_basis, cfg.ex_interest_days,
                                        999.0, 0.0)
        except ValueError as e:
            rec["reason"] = str(e)
            rows.append(rec)
            continue
        dirty = r["price"] + inst.accrued
        rec.update(accrued=inst.accrued, dirty=dirty, mod_duration=mod_duration(inst, r["ytm"]),
                   ytm_check_bp=(ytm_from_dirty(inst, dirty) - r["ytm"]) * 100)
        insts[r["isin"]] = inst
        if abs(rec["ytm_check_bp"]) > YTM_CHECK_BP:
            rec["reason"] = (f"engine yield from FBIL price differs by {rec['ytm_check_bp']:+.2f}bp: "
                             "coupon schedule (e.g. an unpaid odd first coupon) not reproduced")
        else:
            rec["used"] = True
        rows.append(rec)
    return Universe(pd.DataFrame(rows), insts, settle)


# ---------------------------------------------------------------- fitting
SV_LO = [0.0, -20.0, -40.0, -40.0, 0.05, 0.5]
SV_HI = [20.0, 20.0, 40.0, 40.0, 15.0, 60.0]
NS_LO = [0.0, -20.0, -40.0, 0.05]
NS_HI = [20.0, 20.0, 40.0, 30.0]


def _stack(u: Universe, isins):
    insts = [u.insts[i] for i in isins]
    T = np.concatenate([x.times for x in insts])
    A = np.concatenate([x.amounts for x in insts])
    starts = np.cumsum([0] + [len(x.times) for x in insts[:-1]])
    return T, A, starts


def fit_curve(u: Universe, model: str = "svensson"):
    used = u.table[u.table["used"]]
    if len(used) < (6 if model == "svensson" else 4) + 2:
        raise ValueError(f"only {len(used)} usable bonds: too few to fit a {model} curve")
    T, A, starts = _stack(u, used["isin"])
    P = used["dirty"].to_numpy(float)
    D = used["mod_duration"].to_numpy(float)
    sw = np.sqrt(used["weight"].to_numpy(float))
    make = (lambda p: SvenssonCurve(*p)) if model == "svensson" else (lambda p: ns_as_svensson(*p))

    def resid(p):
        z = make(p).zero(T)
        m = np.add.reduceat(A * np.exp(-z / 100.0 * T), starts)
        return sw * (m - P) / (D * P) * 1e4                 # ~ yield error, bp

    long_y = float(used.loc[used["residual_years"].idxmax(), "ytm"])
    short_y = float(used.loc[used["residual_years"].idxmin(), "ytm"])
    b0, b1 = long_y, short_y - long_y
    if model == "svensson":
        lo, hi = SV_LO, SV_HI
        x0s = [[b0, b1, b2, b3, t1, t2] for t1 in (0.7, 2.0, 4.0) for t2 in (6.0, 15.0)
               for b2, b3 in ((-1.0, 1.0), (1.0, -1.0))]
    else:
        lo, hi = NS_LO, NS_HI
        x0s = [[b0, b1, b2, t] for t in (0.7, 2.0, 4.0, 8.0) for b2 in (-1.0, 1.0)]
    best = None
    for x0 in x0s:
        x0 = np.clip(x0, np.array(lo) + 1e-6, np.array(hi) - 1e-6)
        r = least_squares(resid, x0, bounds=(lo, hi), x_scale="jac", max_nfev=4000)
        if best is None or r.cost < best.cost:
            best = r
    p = list(best.x)
    if model == "svensson" and p[4] > p[5]:                  # same curve, keep tau1 < tau2
        p = [p[0], p[1], p[3], p[2], p[5], p[4]]
    at_bound = [n for n, v, l, h in zip(SvenssonCurve.NAMES if model == "svensson" else
                                         ("b0", "b1", "b2", "tau"), p, lo, hi)
                if min(abs(v - l), abs(v - h)) < 1e-4 * max(1.0, abs(h - l))]
    curve = make(p)
    curve.label = f"own {model}"
    return curve, at_bound


def bond_errors(u: Universe, curve) -> pd.Series:
    """Model yield minus FBIL yield (bp) for every bond that has cash flows."""
    out = {}
    for isin, inst in u.insts.items():
        row = u.table.loc[u.table["isin"] == isin].iloc[0]
        out[isin] = (ytm_from_dirty(inst, model_dirty(curve, inst)) - row["ytm"]) * 100
    return pd.Series(out)


def _stats(err: pd.Series, table: pd.DataFrame) -> dict:
    t = table.set_index("isin")
    used = t.index[t["used"]]
    e = err.reindex(used).dropna()
    tr = err.reindex(t.index[t["used"] & (t["tag"] == "traded")]).dropna()
    rms = lambda s: float(np.sqrt(np.mean(s ** 2))) if len(s) else None
    return {"n": int(len(e)), "rmse_bp": rms(e), "mae_bp": float(e.abs().mean()) if len(e) else None,
            "max_abs_bp": float(e.abs().max()) if len(e) else None, "mean_bp": float(e.mean()) if len(e) else None,
            "n_traded": int(len(tr)), "rmse_traded_bp": rms(tr)}


# ---------------------------------------------------------------- storage
def fbil_dir(cfg: EngineConfig) -> Path:
    return Path(cfg.data_dir) / "fbil"


def g_path(cfg: EngineConfig) -> Path:
    return Path(cfg.data_dir) / "G_fitted_curves.csv"


def available_dates(cfg: EngineConfig) -> list[str]:
    d = fbil_dir(cfg)
    return sorted(p.stem.replace("fbil_gsec_", "") for p in d.glob("fbil_gsec_*.csv")) if d.is_dir() else []


def save_parsed(cfg: EngineConfig, parsed: dict, source: str):
    d = fbil_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    tag = f"{parsed['date']:%Y-%m-%d}"
    b = parsed["bonds"].copy()
    b["source"] = source
    b.to_csv(d / f"fbil_gsec_{tag}.csv", index=False, date_format="%Y-%m-%d")
    parsed["par"].to_csv(d / f"fbil_par_{tag}.csv", index=False)


def load_parsed(cfg: EngineConfig, on: date | None = None) -> dict:
    dates = available_dates(cfg)
    if on is not None:
        dates = [x for x in dates if x <= str(on)]
    if not dates:
        raise LookupError("no FBIL price file stored" + (f" on or before {on}" if on else "")
                          + "; upload FBIL's G-Sec price/yield file first")
    tag = dates[-1]
    d = fbil_dir(cfg)
    b = pd.read_csv(d / f"fbil_gsec_{tag}.csv", dtype={"isin": str}, keep_default_na=False)
    b["maturity"] = pd.to_datetime(b["maturity"]).dt.date
    for c in ("coupon", "price", "ytm"):
        b[c] = b[c].astype(float)
    pp = d / f"fbil_par_{tag}.csv"
    par = pd.read_csv(pp) if pp.exists() else pd.DataFrame(columns=["tenor", "par_sa", "par_annual"])
    return {"date": to_date(tag), "bonds": b, "par": par,
            "source": str(b["source"].iloc[0]) if "source" in b and len(b) else ""}


def append_market_prices(store: Store, parsed: dict, source: str = "FBIL") -> int:
    """FBIL clean prices of fixed-coupon bonds -> File F (replaces that date's FBIL rows)."""
    b = parsed["bonds"]
    fixed = b[b["remark1"].astype(str).str.upper() != "FRB"]
    new = pd.DataFrame({"date": pd.Timestamp(parsed["date"]), "isin": fixed["isin"],
                        "clean_price": fixed["price"], "source": source})
    F = store.load_F()
    if len(F):
        F = F[~((F["date"].dt.date == parsed["date"]) & (F["source"].astype(str) == source))]
        new = pd.concat([F, new], ignore_index=True)
    new.to_csv(store.path_F, index=False, date_format="%Y-%m-%d")
    return len(fixed)


def save_fit(cfg: EngineConfig, on: date, model: str, curve: SvenssonCurve, stats: dict, source: str):
    p = g_path(cfg)
    g = pd.read_csv(p, dtype=str, keep_default_na=False) if p.exists() else pd.DataFrame(columns=G_COLS)
    g = g[~((g["date"] == str(on)) & (g["model"] == model))]
    row = {"date": str(on), "model": model, **{k: f"{v:.6f}" for k, v in curve.params.items()},
           "n_bonds": stats["n"], "rmse_bp": f"{stats['rmse_bp']:.4f}",
           "rmse_traded_bp": "" if stats["rmse_traded_bp"] is None else f"{stats['rmse_traded_bp']:.4f}",
           "source": source, "fitted_at": datetime.now().isoformat(timespec="seconds")}
    g = pd.concat([g, pd.DataFrame([row])], ignore_index=True).sort_values(["date", "model"])
    g[G_COLS].to_csv(p, index=False)


def import_fbil(store: Store, data: bytes, filename: str) -> dict:
    """Save the upload, parse it, store prices (data/fbil + File F), fit and save the curve."""
    parsed = read_fbil(data)
    raw = Path(store.cfg.data_dir) / "raw" / "fbil"
    raw.mkdir(parents=True, exist_ok=True)
    name = Path(filename or "fbil.xlsx").name
    (raw / f"{datetime.now():%Y%m%d_%H%M%S}_{name}").write_bytes(data)
    save_parsed(store.cfg, parsed, name)
    n_f = append_market_prices(store, parsed)
    res = build(store, parsed["date"])
    save_fit(store.cfg, parsed["date"], "svensson", res["curves"]["svensson"], res["stats"]["svensson"], name)
    save_fit(store.cfg, parsed["date"], "ns", res["curves"]["ns"], res["stats"]["ns"], name)
    res["imported"] = {"file": name, "date": str(parsed["date"]), "bonds": len(parsed["bonds"]),
                       "market_prices_added": n_f}
    return res


# ---------------------------------------------------------------- the build
def _known_bonds(store: Store) -> dict:
    known = {}
    for loader in (store.load_E, store.load_C):
        df = loader()
        for r in df.to_dict("records") if len(df) else []:
            isin = str(r.get("isin", "")).strip().upper()
            if isin.startswith("IN"):
                try:
                    known[isin] = bond_from_row(r)
                except Exception:
                    pass
    return known


def build(store: Store, on: date | None = None) -> dict:
    cfg = store.cfg
    parsed = load_parsed(cfg, on)
    fd = parsed["date"]
    cal = store.calendar()
    settle = settlement_date(fd, cal, cfg.settlement_lag)
    u = build_universe(parsed["bonds"], settle, cal, cfg, _known_bonds(store))

    curves, bounds = {}, {}
    curves["svensson"], bounds["svensson"] = fit_curve(u, "svensson")
    curves["ns"], bounds["ns"] = fit_curve(u, "ns")
    flags = []
    try:
        ccil, ns_row = store.latest_ns(fd)
        curves["ccil"] = ccil
        ccil_date = ns_row["date"].date()
        if ccil_date != fd:
            flags.append(f"CCIL NS curve for {fd} not stored; comparing with {ccil_date}")
    except LookupError:
        ns_row, ccil_date = None, None
        flags.append(f"no CCIL NS curve on or before {fd}: CCIL comparison skipped")

    tab = u.table.copy()
    errs = {k: bond_errors(u, c) for k, c in curves.items()}
    for k, e in errs.items():
        tab[f"err_{k}_bp"] = tab["isin"].map(e)
    stats = {k: _stats(e, tab) for k, e in errs.items()}

    bk = pd.cut(tab["residual_years"], [b[0] for b in BUCKETS] + [99], labels=[b[2] for b in BUCKETS], right=False)
    by_bucket = []
    for lab in [b[2] for b in BUCKETS]:
        sel = tab[(bk == lab) & tab["used"]]
        row = {"bucket": lab, "n": int(len(sel))}
        for k in curves:
            e = sel[f"err_{k}_bp"].dropna()
            row[f"{k}_mean_bp"] = float(e.mean()) if len(e) else None
            row[f"{k}_rmse_bp"] = float(np.sqrt(np.mean(e ** 2))) if len(e) else None
        by_bucket.append(row)

    for k, b in bounds.items():
        if b:
            flags.append(f"own {k} fit: parameter(s) {', '.join(b)} at the optimiser bound")
    n_mis = int((tab["ytm_check_bp"].abs() > YTM_CHECK_BP).sum())
    if n_mis:
        flags.append(f"{n_mis} bond(s) excluded: engine could not reproduce FBIL's yield from its price")

    # curve points
    t = np.round(np.arange(0.25, 50.0001, 0.25), 4)
    pts = {"tenor": t}
    for k, c in curves.items():
        pts[f"zero_{k}"] = c.zero(t)
        pts[f"par_{k}"] = ParShift(c, BumpInterpolator("linear"), max_t=50.0).par_yields(t)
    points = pd.DataFrame(pts)
    par = parsed["par"]
    key = []
    for lab, k in zip(key_labels(cfg.key_tenors), cfg.key_tenors):
        row = {"key_rate": lab}
        for name, c in curves.items():
            row[f"zero_{name}"] = float(c.zero(k))
            row[f"par_{name}"] = float(points.loc[np.isclose(points["tenor"], k), f"par_{name}"].iloc[0])
        m = par[np.isclose(par["tenor"].astype(float), k)] if len(par) else par
        row["par_fbil"] = float(m["par_sa"].iloc[0]) if len(m) else None
        key.append(row)

    pc, findings = portfolio_check(store, parsed, u, curves, settle, cal)
    return {"fbil_date": fd, "settlement": settle, "source": parsed.get("source", ""),
            "universe": tab, "curves": curves, "stats": stats, "by_bucket": by_bucket,
            "points": points, "fbil_par": par, "key_rates": key, "portfolio": pc,
            "findings": findings, "flags": flags, "time_basis": cfg.time_basis,
            "ccil_row": None if ns_row is None else {
                "date": str(ccil_date), "b0": float(ns_row.b0), "b1": float(ns_row.b1),
                "b2": float(ns_row.b2), "tau": float(ns_row.tau), "source": ns_row.source}}


# ---------------------------------------------------------------- portfolio check
def portfolio_check(store: Store, parsed: dict, u: Universe, curves: dict, settle: date, cal):
    cfg = store.cfg
    fb = parsed["bonds"].set_index("isin")
    C = store.load_C()
    rows, findings = [], []
    for r in C.to_dict("records"):
        pid, isin = r["pos_id"], str(r["isin"]).strip().upper()
        bond = bond_from_row(r)
        sign = 1.0 if str(r["direction"]).strip().lower() == "long" else -1.0
        rec = {"pos_id": pid, "name": r["name"], "isin": isin, "found": isin in fb.index}
        if not rec["found"]:
            alt = parsed["bonds"][(np.isclose(parsed["bonds"]["coupon"], bond.coupon))
                                  & (parsed["bonds"]["maturity"] == bond.maturity)]
            if len(alt):
                findings.append({"pos_id": pid, "severity": "error",
                                 "text": f"{pid} {r['name']}: ISIN {isin or '(blank)'} is not in the FBIL file, "
                                         f"but FBIL lists {bond.coupon}% maturing {bond.maturity:%d-%b-%Y} as "
                                         f"{alt['isin'].iloc[0]}. Correct the ISIN in the portfolio."})
            else:
                findings.append({"pos_id": pid, "severity": "error",
                                 "text": f"{pid} {r['name']}: not in the FBIL file (ISIN {isin or 'blank'}); "
                                         "no market price to check against."})
            rows.append(rec)
            continue
        f = fb.loc[isin]
        rec.update(fbil_coupon=float(f["coupon"]), fbil_maturity=str(f["maturity"]),
                   fbil_price=float(f["price"]), fbil_ytm=float(f["ytm"]), fbil_tag=classify(f.to_dict())[1])
        static = []
        if not np.isclose(f["coupon"], bond.coupon, atol=1e-6):
            static.append(f"coupon {bond.coupon}% vs FBIL {f['coupon']}%")
        if f["maturity"] != bond.maturity:
            static.append(f"maturity {bond.maturity} vs FBIL {f['maturity']}")
        rec["static_ok"] = not static
        for s in static:
            findings.append({"pos_id": pid, "severity": "error", "text": f"{pid} {r['name']}: {s}. Fix File C."})

        inst = instrument_from_bond(bond, settle, cal, cfg.time_basis, cfg.ex_interest_days, 999.0, 0.0)
        face = sign * float(r["size_inr"]) if str(r.get("size_type", "face")).lower() == "face" else None
        dirty_mkt = f["price"] + inst.accrued
        y_eng = ytm_from_dirty(inst, dirty_mkt)
        rec.update(accrued=inst.accrued, days_accrued=inst.days_accrued, prev_coupon=str(inst.prev_coupon),
                   next_coupon=str(inst.next_coupon), engine_ytm_from_fbil_price=y_eng,
                   ytm_check_bp=(y_eng - f["ytm"]) * 100)
        if abs(rec["ytm_check_bp"]) > YTM_CHECK_BP:
            findings.append({"pos_id": pid, "severity": "error",
                             "text": f"{pid} {r['name']}: FBIL price {f['price']:.4f} gives {y_eng:.4f}% in the engine but "
                                     f"FBIL says {f['ytm']:.4f}% ({rec['ytm_check_bp']:+.2f}bp). Check issue date, coupon "
                                     f"dates and accrued ({inst.accrued:.4f}, {inst.days_accrued} days from {inst.prev_coupon})."})
        if face is None:
            face = sign * float(r["size_inr"]) / dirty_mkt * 100.0
        rec["signed_face_inr"] = face
        for k, c in curves.items():
            md = model_dirty(c, inst)
            clean = md - inst.accrued
            rec[f"{k}_clean"] = clean
            rec[f"{k}_ytm"] = ytm_from_dirty(inst, md)
            rec[f"{k}_diff_paise"] = (clean - f["price"]) * 100
            rec[f"{k}_diff_bp"] = (rec[f"{k}_ytm"] - f["ytm"]) * 100
            rec[f"{k}_mv_diff_inr"] = (clean - f["price"]) / 100.0 * face
        rows.append(rec)

    df = pd.DataFrame(rows)
    if len(df) and "ccil_diff_bp" in df:
        for x in df[df["found"]].to_dict("records"):
            d, p, mv = x["ccil_diff_bp"], x["ccil_diff_paise"], x["ccil_mv_diff_inr"]
            if abs(d) >= 2:
                findings.append({"pos_id": x["pos_id"], "severity": "warn",
                                 "text": f"{x['pos_id']} {x['name']}: CCIL NS curve prices it at {x['ccil_clean']:.4f} vs FBIL "
                                         f"{x['fbil_price']:.4f} ({p:+.1f} paise; yield {d:+.1f}bp "
                                         f"{'too high' if d > 0 else 'too low'}) -> "
                                         f"₹{abs(mv) / 1e7:.2f} cr {'under' if mv < 0 else 'over'}valued."})
        tot = float(df.loc[df["found"], "ccil_mv_diff_inr"].sum())
        tot_sv = float(df.loc[df["found"], "svensson_mv_diff_inr"].sum())
        findings.append({"pos_id": "", "severity": "info",
                         "text": f"Portfolio on the CCIL NS curve vs FBIL prices: ₹{tot / 1e7:+.2f} cr. "
                                 f"On our own Svensson curve: ₹{tot_sv / 1e7:+.2f} cr."})
    if len(df) and df["found"].all() and (df["ytm_check_bp"].abs() <= YTM_CHECK_BP).all():
        findings.insert(0, {"pos_id": "", "severity": "ok",
                            "text": "Static data (ISIN, coupon, maturity), accrued interest and the yield "
                                    "calculation match FBIL for every position (price -> yield within "
                                    f"{YTM_CHECK_BP}bp at T+1 settlement {settle})."})
    return df, findings
