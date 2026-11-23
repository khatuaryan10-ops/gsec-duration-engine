"""Stage 0: choose the discounting time basis (ACT/365 vs 30/360) against market prices (File F)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .bonds import dirty_price, instrument_from_bond
from .config import EngineConfig
from .conventions import settlement_date
from .store import Store, bond_from_row

BUCKETS = [(0, 5, "<5Y"), (5, 10, "5-10Y"), (10, 20, "10-20Y"), (20, 99, "20Y+")]


def time_basis_test(store: Store, cfg: EngineConfig) -> dict:
    F, E = store.load_F(), store.load_E()
    if F.empty or E.empty:
        raise LookupError("Stage 0 needs File F (market prices) and File E (bond master, by ISIN)")
    cal = store.calendar()
    rows = []
    for r in F.itertuples(index=False):
        d = r.date.date()
        match = E[E["isin"] == r.isin]
        if match.empty:
            continue
        bond = bond_from_row(match.iloc[0])
        curve, ns = store.latest_ns(d)
        if ns.date.date() != d:
            continue                          # need that day's own curve
        settle = settlement_date(d, cal, cfg.settlement_lag)
        rec = {"date": d, "isin": r.isin, "name": bond.name, "market_clean": r.clean_price}
        for basis in ("ACT/365", "30/360"):
            inst = instrument_from_bond(bond, settle, cal, basis)
            clean = dirty_price(inst, curve) - inst.accrued
            rec[f"model_{basis}"] = clean
            rec[f"err_paise_{basis}"] = (clean - r.clean_price) * 100.0
        rec["residual_years"] = inst.maturity_years
        rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        raise LookupError("No File F rows matched a bond in File E and a same-day NS row")
    df["bucket"] = pd.cut(df["residual_years"], [b[0] for b in BUCKETS] + [99],
                          labels=[b[2] for b in BUCKETS], right=False)

    def stats(g):
        out = {}
        for basis in ("ACT/365", "30/360"):
            e = g[f"err_paise_{basis}"]
            out[f"{basis} mean"] = e.mean()
            out[f"{basis} MAE"] = e.abs().mean()
            out[f"{basis} max|err|"] = e.abs().max()
        out["n"] = len(g)
        return pd.Series(out)

    overall = stats(df)
    by_bucket = df.groupby("bucket", observed=True).apply(stats, include_groups=False)
    mae_a, mae_t = overall["ACT/365 MAE"], overall["30/360 MAE"]
    if abs(mae_a - mae_t) < 1.0:
        choice, reason = "ACT/365", "difference under 1 paise on average: choice immaterial"
    else:
        choice = "ACT/365" if mae_a < mae_t else "30/360"
        reason = "lower mean absolute error"
    return {"detail": df, "overall": overall, "by_bucket": by_bucket,
            "choice": choice, "reason": reason,
            "dates": sorted({str(x) for x in df["date"]}), "bonds": sorted(set(df["isin"]))}
