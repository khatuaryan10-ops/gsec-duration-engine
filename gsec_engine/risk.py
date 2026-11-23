"""Stage H: KRD/KR01, convexity, PC exposures, scenarios and historical replay.

Bumps are added on top of today's curve; a key-rate move vector (bp at 2/5/10/30Y) is spread
across maturities by the chosen interpolation, and every cash flow is fully repriced.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import EngineConfig
from .curves import KEYS, SPACES, BumpInterpolator, ParShift, ShiftedCurve
from .portfolio import Position

BP = 1e-4
KEY_LABELS = [f"{k:g}Y" for k in KEYS]


class PortfolioPricer:
    """Vectorised repricing of all positions' cash flows."""

    def __init__(self, positions: list[Position], base_curve, interp: BumpInterpolator,
                 space: str = "spot"):
        """space='spot': key-rate moves are added to zero (spot) rates.
        space='par': key-rate moves are semi-annual par-yield moves; zero rates are re-bootstrapped."""
        if space not in SPACES:
            raise ValueError(f"space must be one of {SPACES}")
        self.positions, self.base, self.interp, self.space = positions, base_curve, interp, space
        self.par = ParShift(base_curve, interp) if space == "par" else None
        self.t = np.concatenate([p.instrument.times for p in positions])
        self.cf = np.concatenate([p.instrument.amounts for p in positions])
        self.idx = np.concatenate([np.full(len(p.instrument.times), i)
                                   for i, p in enumerate(positions)])
        self.face = np.array([p.signed_face for p in positions])
        self.z0 = base_curve.zero(self.t)

    def shift_bp(self, v_bp) -> np.ndarray:
        """Zero-rate change (bp) at every cash-flow time for key-rate moves v (bp)."""
        if self.space == "par":
            return self.par.shift_bp(self.t, v_bp)
        return self.interp.spread(self.t, v_bp)

    def prices(self, v_bp=None) -> np.ndarray:
        """Dirty price per Rs100 of each position under the key-rate shift v (bp)."""
        z = self.z0 if v_bp is None or not np.any(v_bp) else self.z0 + self.shift_bp(v_bp) / 100.0
        pv = self.cf * np.exp(-z / 100.0 * self.t)
        return np.bincount(self.idx, pv, minlength=len(self.positions))

    def values(self, v_bp=None) -> np.ndarray:
        return self.prices(v_bp) * self.face / 100.0

    def total(self, v_bp=None) -> float:
        return float(self.values(v_bp).sum())


def krd_kr01(pp: PortfolioPricer, bump_bp: float = 1.0) -> dict:
    n = len(KEYS)
    P0 = pp.prices()
    MV0 = P0 * pp.face / 100.0
    krd = np.zeros((len(P0), n))
    for k in range(n):
        v = np.zeros(n)
        v[k] = bump_bp
        Pd, Pu = pp.prices(-v), pp.prices(v)
        krd[:, k] = (Pd - Pu) / (2.0 * P0 * bump_bp * BP)
    par = np.full(n, bump_bp)
    dur_par = (pp.prices(-par) - pp.prices(par)) / (2.0 * P0 * bump_bp * BP)
    kr01 = krd * MV0[:, None] * BP                       # Rs lost if that key rate rises 1bp

    names = [p.pos_id for p in pp.positions]
    krd_df = pd.DataFrame(krd, index=names, columns=[f"KRD_{k}" for k in KEY_LABELS])
    krd_df["sum_KRD"] = krd.sum(axis=1)
    krd_df["parallel_duration"] = dur_par
    krd_df["check_diff"] = krd_df["sum_KRD"] - dur_par
    kr01_df = pd.DataFrame(kr01, index=names, columns=[f"KR01_{k}" for k in KEY_LABELS])
    kr01_df["total_KR01"] = kr01.sum(axis=1)

    net = kr01.sum(axis=0)
    V0 = MV0.sum()
    return {
        "krd": krd_df, "kr01": kr01_df, "net_kr01": net,
        "net_kr01_total": float(net.sum()),
        "gross_kr01_positions": float(np.abs(kr01.sum(axis=1)).sum()),
        "gross_kr01_per_key": np.abs(kr01).sum(axis=0),
        "portfolio_krd": net / (V0 * BP) if V0 else np.full(n, np.nan),
        "portfolio_duration": float(net.sum() / (V0 * BP)) if V0 else float("nan"),
        "parallel_check_max_abs": float(np.abs(krd_df["check_diff"]).max()),
        "net_mv": float(V0),
    }


def convexity(pp: PortfolioPricer, bump_bp: float = 10.0) -> dict:
    n = len(KEYS)
    v = np.full(n, bump_bp)
    dy = bump_bp * BP
    P0, Pu, Pd = pp.prices(), pp.prices(v), pp.prices(-v)
    C = (Pu + Pd - 2.0 * P0) / (P0 * dy ** 2)
    V0, Vu, Vd = pp.values(), pp.values(v), pp.values(-v)
    dollar_C = (Vu + Vd - 2.0 * V0) / dy ** 2                 # Rs per unit y^2, signed
    tot = dollar_C.sum()
    df = pd.DataFrame({"convexity": C, "dollar_convexity_inr": dollar_C,
                       "share_of_total": dollar_C / tot if tot else np.nan},
                      index=[p.pos_id for p in pp.positions])
    return {"table": df, "portfolio_convexity": float(tot / V0.sum()) if V0.sum() else float("nan"),
            "dollar_convexity_inr": float(tot)}


def pc_exposures(net_kr01: np.ndarray, calib: dict) -> pd.DataFrame:
    L = np.array(calib["loadings"])
    s = np.array(calib["pc_sigma_bp"])
    rows = []
    for j, name in enumerate(calib["pc_names"][:3]):
        per_bp = float(net_kr01 @ L[:, j])
        rows.append({"pc": name, "sigma_bp": s[j], "explained": calib["explained"][j],
                     "loadings": dict(zip(KEY_LABELS, L[:, j].round(4))),
                     "loss_per_unit_loading_inr": per_bp,
                     "loss_per_1sigma_inr": per_bp * s[j]})
    return pd.DataFrame(rows)


def basel_vectors(cfg: EngineConfig) -> dict:
    """BCBS IRRBB shapes evaluated at the key tenors (x = 4)."""
    p, s, l = (cfg.basel_inr_bp[k] for k in ("parallel", "short", "long"))
    S = s * np.exp(-KEYS / 4.0)
    Lg = l * (1.0 - np.exp(-KEYS / 4.0))
    return {
        "Basel parallel up": np.full(len(KEYS), p),
        "Basel parallel down": np.full(len(KEYS), -p),
        "Basel steepener": -0.65 * np.abs(S) + 0.9 * np.abs(Lg),
        "Basel flattener": 0.8 * np.abs(S) - 0.6 * np.abs(Lg),
        "Basel short up": S,
        "Basel short down": -S,
    }


def build_scenarios(calib: dict, cfg: EngineConfig) -> list[tuple[str, str, np.ndarray]]:
    out = []
    sig = np.array([calib["sigma_bp"][k] for k in calib["keys"]])
    for k, lab in enumerate(KEY_LABELS):
        for m in (-2, -1, 1, 2):
            v = np.zeros(len(KEYS))
            v[k] = m * sig[k]
            out.append((f"{lab} {m:+d}σ", "key rate", v))
    L = np.array(calib["loadings"])
    for j, name in enumerate(calib["pc_names"][:3]):
        for m in (-2, -1, 1, 2):
            out.append((f"{name} {m:+d}σ", "PCA", m * calib["pc_sigma_bp"][j] * L[:, j]))
    for b in cfg.parallel_bp:
        out.append((f"Parallel {b:+g}bp", "parallel", np.full(len(KEYS), float(b))))
    for name, v in basel_vectors(cfg).items():
        out.append((name, "Basel IRRBB", v))
    return out


def evaluate_scenario(pp: PortfolioPricer, net_kr01: np.ndarray, V0: float, v: np.ndarray,
                      conv_bp: float = 10.0) -> dict:
    """duration-only, duration + convexity along the scenario direction, and full reprice."""
    v = np.asarray(v, float)
    dur = float(-net_kr01 @ v)
    m = np.max(np.abs(v))
    if m == 0:
        return {"duration_only": 0.0, "duration_convexity": 0.0, "full_reprice": 0.0}
    s = conv_bp / m
    H = (pp.total(s * v) + pp.total(-s * v) - 2.0 * V0) / s ** 2
    full = pp.total(v) - V0
    return {"duration_only": dur, "duration_convexity": dur + 0.5 * H, "full_reprice": full}


def scenario_table(pp: PortfolioPricer, sens: dict, calib: dict, cfg: EngineConfig) -> pd.DataFrame:
    V0 = sens["net_mv"]
    rows = []
    for name, group, v in build_scenarios(calib, cfg):
        r = evaluate_scenario(pp, sens["net_kr01"], V0, v, cfg.convexity_bump_bp)
        rows.append({"scenario": name, "group": group,
                     **{f"move_{l}_bp": round(x, 3) for l, x in zip(KEY_LABELS, v)},
                     **r, "full_reprice_pct_of_net_mv": r["full_reprice"] / V0 * 100 if V0 else np.nan,
                     "convexity_gap_inr": r["full_reprice"] - r["duration_only"],
                     "residual_after_convexity_inr": r["full_reprice"] - r["duration_convexity"]})
    return pd.DataFrame(rows)


def historical_replay(pp: PortfolioPricer, scen: pd.DataFrame, cfg: EngineConfig) -> dict:
    V0 = pp.total()
    cols = ["r2", "r5", "r10", "r30"]
    pnl = np.array([pp.total(row) - V0 for row in scen[cols].values])
    df = pd.DataFrame({"date": scen["date"].values, **{c: scen[c].values for c in cols},
                       "pnl_inr": pnl})
    loss = -pnl
    stats = {}
    for q in cfg.var_levels:
        var = float(np.quantile(loss, q))
        es = float(loss[loss >= var].mean())
        stats[f"VaR_{int(q * 100)}_inr"] = var
        stats[f"ES_{int(q * 100)}_inr"] = es
    stats.update({"n_days": int(len(pnl)), "mean_pnl_inr": float(pnl.mean()),
                  "std_pnl_inr": float(pnl.std(ddof=1)),
                  "window_start": str(pd.Timestamp(scen["date"].min()).date()),
                  "window_end": str(pd.Timestamp(scen["date"].max()).date())})
    worst = df.nsmallest(5, "pnl_inr").reset_index(drop=True)
    return {"pnl": df, "stats": stats, "worst5": worst}
