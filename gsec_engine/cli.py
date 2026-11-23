"""Command line entry point.

  python -m gsec_engine.cli import-ns <NS file .csv/.xlsx>
  python -m gsec_engine.cli add-ns --date 2026-11-20 --b0 8.16 --b1 -2.44 --b2 0 --tau 3.48 [--source CCIL]
  python -m gsec_engine.cli calibrate
  python -m gsec_engine.cli stage0
  python -m gsec_engine.cli run [--date 2026-11-20] [--interp linear|cubic|pchip] [--basis ACT/365|30/360] [--curve ccil|own]
  python -m gsec_engine.cli import-fbil <FBIL G-Sec price/yield workbook>   # own curve + portfolio check
"""
from __future__ import annotations

import argparse
from datetime import date

import pandas as pd

from .config import EngineConfig
from .engine import run, run_calibration
from .stage0 import time_basis_test
from .store import NeedsReview, Store
from . import yieldbuilder

CR = 1e7


def _cfg(a) -> EngineConfig:
    cfg = EngineConfig()
    if getattr(a, "interp", None):
        cfg.interpolation = a.interp
    if getattr(a, "basis", None):
        cfg.time_basis = a.basis
    if getattr(a, "curve", None):
        cfg.curve_source = a.curve
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(prog="gsec_engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("import-ns"); s.add_argument("path")
    s = sub.add_parser("add-ns")
    for k in ("date", "b0", "b1", "b2", "tau"):
        s.add_argument(f"--{k}", required=True)
    s.add_argument("--source", default="manual"); s.add_argument("--note", default="")
    s.add_argument("--correction", action="store_true"); s.add_argument("--accept-review", action="store_true")
    sub.add_parser("calibrate")
    sub.add_parser("stage0")
    s = sub.add_parser("run"); s.add_argument("--date", default=str(date.today()))
    s.add_argument("--interp", choices=["linear", "cubic", "pchip"]); s.add_argument("--basis")
    s.add_argument("--curve", choices=["ccil", "own"], help="valuation curve: CCIL NS or own Svensson (FBIL)")
    s = sub.add_parser("import-fbil"); s.add_argument("path")
    a = p.parse_args(argv)
    cfg = _cfg(a)
    store = Store(cfg)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)

    if a.cmd == "import-ns":
        log = store.import_ns_file(a.path)
        print(log["status"].value_counts().to_string())
        print(f"Import log: {store.path_import_log}")
    elif a.cmd == "add-ns":
        try:
            r = store.add_ns_row(a.date, float(a.b0), float(a.b1), float(a.b2), float(a.tau), a.source,
                                 notes=a.note, correction=a.correction, accept_review=a.accept_review)
        except NeedsReview as e:
            print(f"REVIEW NEEDED: {e}\nRe-run with --accept-review after checking the inputs.")
            return 2
        print("Stored. Key rates:", {k: round(v, 4) for k, v in r.key_rates.items()})
        for w in r.warnings:
            print("WARNING:", w)
    elif a.cmd == "calibrate":
        c = run_calibration(store, cfg)
        print(f"Window {c['window_start']} -> {c['window_end']}, {c['n_changes']} daily changes")
        print("sigma (bp):", c["sigma_bp"])
        print("explained:", [round(x, 4) for x in c["explained"]])
    elif a.cmd == "stage0":
        r = time_basis_test(store, cfg)
        print(r["overall"].round(2).to_string()); print(r["by_bucket"].round(2).to_string())
        print(f"Choice: {r['choice']} ({r['reason']})")
    elif a.cmd == "import-fbil":
        from pathlib import Path
        r = yieldbuilder.import_fbil(store, Path(a.path).read_bytes(), Path(a.path).name)
        print(f"FBIL {r['imported']['date']}: {r['imported']['bonds']} bonds, settlement {r['settlement']}, "
              f"{r['imported']['market_prices_added']} prices added to File F")
        for k in ("svensson", "ns", "ccil"):
            if k in r["stats"]:
                st = r["stats"][k]
                print(f"  {k:9s} RMSE {st['rmse_bp']:.2f}bp (traded {st['rmse_traded_bp']:.2f}bp) on {st['n']} bonds")
        print("  Svensson:", {k: round(v, 4) for k, v in r["curves"]["svensson"].params.items()})
        for f in r["findings"]:
            print(f"  [{f['severity']}] {f['text']}")
        for f in r["flags"]:
            print("  FLAG:", f)
    elif a.cmd == "run":
        r = run(a.date, cfg, store)
        print_summary(r)
    return 0


def print_summary(r):
    f = lambda x: f"{x / CR:,.2f}"
    print(f"\nValuation {r.valuation_date}  settlement {r.settlement}  "
          f"curve {r.run_log['curve']['label']} {r.run_log['curve']['date']}  basis {r.run_log['time_basis']}  "
          f"interp {r.run_log['interpolation']}  calib {r.run_log['calibration_window']}")
    v = r.valuation[["pos_id", "direction", "signed_face_inr", "clean_price", "accrued_per100",
                     "dirty_price", "ytm_semiannual_pct", "signed_mv_inr"]].copy()
    v["signed_face_inr"] = v["signed_face_inr"].map(f); v["signed_mv_inr"] = v["signed_mv_inr"].map(f)
    print("\nValuation (Rs cr)\n", v.round(4).to_string(index=False))
    t = r.totals
    print(f"Net MV {f(t['net_mv_inr'])} cr | gross {f(t['gross_mv_inr'])} | long {f(t['long_mv_inr'])}"
          f" | short {f(t['short_mv_inr'])} | YTM {t['value_weighted_ytm_pct']:.4f}%")
    print("\nKRD, spot-rate bumps\n", r.krd.round(4).to_string())
    print("\nKRD, par-yield bumps (zero curve re-bootstrapped)\n", r.krd_par.round(4).to_string())
    k = r.kr01_summary.copy()
    k["net_KR01_lakh"] = k["net_KR01_inr"] / 1e5; k["gross_KR01_lakh"] = k["gross_KR01_inr"] / 1e5
    k["net_KR01_par_lakh"] = k["net_KR01_par_inr"] / 1e5
    print("\nPortfolio KR01 (Rs lakh per bp), spot vs par basis\n",
          k[["key_rate", "net_KR01_lakh", "gross_KR01_lakh", "portfolio_KRD",
             "net_KR01_par_lakh", "portfolio_KRD_par"]].round(4).to_string(index=False))
    rs = r.risk_summary
    print(f"Duration {rs['portfolio_duration']:.4f} | net KR01 {rs['net_KR01_total_inr'] / 1e5:,.2f} lakh/bp"
          f" | gross {rs['gross_KR01_inr'] / 1e5:,.2f} | convexity {rs['portfolio_convexity']:.2f}")
    print(f"Par basis: duration {rs['portfolio_duration_par']:.4f} | net KR01 "
          f"{rs['net_KR01_total_par_inr'] / 1e5:,.2f} lakh/bp | convexity {rs['portfolio_convexity_par']:.2f}")
    print("\nConvexity\n", r.convexity.round(4).to_string())
    pc = r.pc[["pc", "sigma_bp", "explained", "loss_per_1sigma_inr"]].copy()
    pc["loss_per_1sigma_cr"] = pc.pop("loss_per_1sigma_inr") / CR
    print("\nPC exposures (Rs cr lost per +1σ)\n", pc.round(4).to_string(index=False))
    sc = r.scenarios[["scenario", "duration_only", "duration_convexity", "full_reprice"]].copy()
    for c in ["duration_only", "duration_convexity", "full_reprice"]:
        sc[c] = sc[c] / CR
    print("\nScenarios (Rs cr; full reprice is official)\n", sc.round(2).to_string(index=False))
    h = r.hist["stats"]
    print(f"\nHistorical replay {h['window_start']} -> {h['window_end']} ({h['n_days']} days): "
          f"VaR95 {h['VaR_95_inr'] / CR:,.2f} cr, ES95 {h['ES_95_inr'] / CR:,.2f}, "
          f"VaR99 {h['VaR_99_inr'] / CR:,.2f}, ES99 {h['ES_99_inr'] / CR:,.2f}")
    w = r.hist["worst5"][["date", "pnl_inr"]].copy(); w["pnl_cr"] = w.pop("pnl_inr") / CR
    w["pnl_cr"] = w["pnl_cr"].round(2)
    w["date"] = pd.to_datetime(w["date"]).dt.date
    print("Worst 5 days\n", w.to_string(index=False))
    cc = r.curve_compare
    if cc.get("available"):
        c = cc["rows"][["pos_id", "ccil_clean", "own_clean", "diff_paise", "diff_bp"]].copy()
        print(f"\nCurve comparison (in use: {cc['active']}), own - CCIL\n", c.round(4).to_string(index=False))
        print(f"MV on CCIL {cc['totals']['ccil_mv_inr'] / CR:,.2f} cr | own {cc['totals']['own_mv_inr'] / CR:,.2f} cr"
              f" | diff {cc['totals']['diff_mv_inr'] / CR:+,.2f} cr")
    if r.flags:
        print("\nFLAGS"); [print(" -", x) for x in r.flags]
    print(f"\nReports: {r.output_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
