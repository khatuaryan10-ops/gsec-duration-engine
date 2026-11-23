"""Charts: key-rate durations, key-rate convexities, and portfolio value change for key-rate
bumps over a +/-50bp grid (full reprice vs duration-only vs duration+convexity).

  python -m gsec_engine.plots [--date 2026-11-19] [--max-bp 50] [--step 5]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .config import EngineConfig
from .conventions import settlement_date, to_date
from .curves import KEYS, BumpInterpolator
from .portfolio import positions_from_file
from .risk import BP, KEY_LABELS, PortfolioPricer, krd_kr01
from .store import Store

CR = 1e7
# reference palette (light), fixed categorical order
S1, S2, S3, S4, S5 = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"
SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "text.color": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID,
    "grid.linewidth": 0.6, "axes.spines.top": False, "axes.spines.right": False,
    "axes.titleweight": "bold", "axes.titlesize": 11, "axes.titlelocation": "left",
    "font.size": 9, "legend.frameon": False, "lines.linewidth": 2,
})


def key_rate_convexity(pp: PortfolioPricer, bump_bp: float = 10.0) -> dict:
    """KRC_k = (P(+h e_k) + P(-h e_k) - 2P0) / (P0 h^2), per position and for the portfolio."""
    n = len(KEYS)
    P0, V0 = pp.prices(), pp.values()
    h = bump_bp * BP
    krc = np.zeros((len(P0), n))
    dollar = np.zeros((len(P0), n))
    for k in range(n):
        v = np.zeros(n)
        v[k] = bump_bp
        krc[:, k] = (pp.prices(v) + pp.prices(-v) - 2 * P0) / (P0 * h ** 2)
        dollar[:, k] = (pp.values(v) + pp.values(-v) - 2 * V0) / h ** 2
    par = np.full(n, bump_bp)
    c_par = (pp.prices(par) + pp.prices(-par) - 2 * P0) / (P0 * h ** 2)
    names = [p.pos_id for p in pp.positions]
    df = pd.DataFrame(krc, index=names, columns=[f"KRC_{l}" for l in KEY_LABELS])
    df["sum_KRC"] = krc.sum(axis=1)
    df["parallel_convexity"] = c_par
    port = dollar.sum(axis=0) / V0.sum()
    return {"table": df, "dollar": dollar, "portfolio": port,
            "portfolio_parallel": float((pp.total(par) + pp.total(-par) - 2 * pp.total()) / (pp.total() * h ** 2))}


def bump_grid(pp: PortfolioPricer, net_kr01, dollar_krc, max_bp=50, step=5) -> pd.DataFrame:
    """Portfolio dMV for each key rate bumped alone (and all together) across the grid."""
    V0 = pp.total()
    shifts = np.arange(-max_bp, max_bp + step / 2, step)
    rows = []
    n = len(KEYS)
    dollar_k = dollar_krc.sum(axis=0)
    for k, lab in enumerate(KEY_LABELS + ["Parallel"]):
        for s in shifts:
            v = np.full(n, s) if lab == "Parallel" else np.eye(n)[k] * s
            full = pp.total(v) - V0
            if lab == "Parallel":
                dur = -net_kr01.sum() * s
                C = (pp.total(np.full(n, 10.0)) + pp.total(np.full(n, -10.0)) - 2 * V0) / (10 * BP) ** 2
            else:
                dur = -net_kr01[k] * s
                C = dollar_k[k]
            rows.append({"key_rate": lab, "shift_bp": s, "full_reprice_cr": full / CR,
                         "duration_only_cr": dur / CR,
                         "duration_convexity_cr": (dur + 0.5 * C * (s * BP) ** 2) / CR})
    return pd.DataFrame(rows)


def _bar_labels(ax, bars, fmt="{:.2f}"):
    for b in bars:
        h = b.get_height()
        if abs(h) < 1e-9:
            continue
        ax.annotate(fmt.format(h), (b.get_x() + b.get_width() / 2, h),
                    xytext=(0, 3 if h >= 0 else -11), textcoords="offset points",
                    ha="center", fontsize=7.5, color=INK2)


def grouped_bars(ax, table: pd.DataFrame, cols, series, colors, title, ylabel):
    x = np.arange(len(cols))
    w = 0.8 / len(series)
    for i, (name, vals) in enumerate(series):
        bars = ax.bar(x + (i - (len(series) - 1) / 2) * w, vals, w * 0.92, label=name,
                      color=colors[i], edgecolor=SURFACE, linewidth=1)
        _bar_labels(ax, bars)
    ax.set_xticks(x, cols)
    ax.set_title(title)
    ax.set_ylabel(ylabel)
    ax.axhline(0, color=AXIS, linewidth=0.8)
    ax.grid(axis="x", visible=False)
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi * 1.22)
    ax.legend(loc="upper left", ncols=len(series))


def ladder(valuation_date, cfg: EngineConfig | None = None, store: Store | None = None,
           max_bp=50, step=5, space: str = "spot") -> dict:
    """KRD, KRC and the key-rate bump grid for one valuation date (shared by charts and the API)."""
    cfg = cfg or EngineConfig()
    store = store or Store(cfg)
    vd = to_date(valuation_date)
    cal = store.calendar()
    settle = settlement_date(vd, cal, cfg.settlement_lag)
    curve, ns = store.curve_for(vd, cfg.curve_source)
    positions = positions_from_file(store.load_C(), settle, cal, curve, cfg)
    pp = PortfolioPricer(positions, curve, BumpInterpolator(cfg.interpolation), space=space)
    sens = krd_kr01(pp, cfg.kr_bump_bp)
    krc = key_rate_convexity(pp, cfg.convexity_bump_bp)
    grid = bump_grid(pp, sens["net_kr01"], krc["dollar"], max_bp, step)
    return {"vd": vd, "settle": settle, "ns": ns, "positions": positions, "sens": sens,
            "krc": krc, "grid": grid, "space": space}


def make_charts(valuation_date, cfg: EngineConfig | None = None, max_bp=50, step=5,
                store: Store | None = None, space: str = "spot") -> Path:
    cfg = cfg or EngineConfig()
    L = ladder(valuation_date, cfg, store, max_bp, step, space)
    vd, settle, ns, positions = L["vd"], L["settle"], L["ns"], L["positions"]
    sens, krc, grid = L["sens"], L["krc"], L["grid"]

    out = Path(cfg.output_dir) / f"{vd:%Y-%m-%d}" / "charts"
    out.mkdir(parents=True, exist_ok=True)
    tag = f"{vd:%Y%m%d}" + ("_par" if space == "par" else "")
    basis = "par-yield bumps, re-bootstrapped" if space == "par" else "spot-rate bumps"
    names = [f"{p.pos_id} {p.name}" for p in positions]
    sub = (f"Valuation {vd:%d-%b-%Y}, settlement {settle:%d-%b-%Y}, {ns['label']} curve {ns['date']:%d-%m-%Y}, "
           f"{cfg.interpolation} bump interpolation, {basis}")

    # 1-2: KRD and KRC
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    krd = sens["krd"][[f"KRD_{l}" for l in KEY_LABELS]].values
    series = [(n, krd[i]) for i, n in enumerate(names)] + [("Portfolio", sens["portfolio_krd"])]
    grouped_bars(axes[0], None, KEY_LABELS, series, [S1, S2, S3],
                 f"Key-rate duration (sum: portfolio {sens['portfolio_duration']:.2f})", "KRD (years)")
    krcv = krc["table"][[f"KRC_{l}" for l in KEY_LABELS]].values
    series = [(n, krcv[i]) for i, n in enumerate(names)] + [("Portfolio", krc["portfolio"])]
    grouped_bars(axes[1], None, KEY_LABELS, series, [S1, S2, S3],
                 f"Key-rate convexity (sum {krc['portfolio'].sum():.1f}; parallel convexity "
                 f"{krc['portfolio_parallel']:.1f})", "KRC")
    fig.suptitle(sub, x=0.01, ha="left", fontsize=8.5, color=INK2)
    fig.tight_layout()
    fig.savefig(out / f"krd_krc_{tag}.png", dpi=160)
    plt.close(fig)

    # 3: dMV for each key rate bumped alone, plus parallel, one axis
    fig, ax = plt.subplots(figsize=(10, 5.6))
    colors = dict(zip(KEY_LABELS + ["Parallel"], [S1, S2, S3, S4, S5]))
    for lab, g in grid.groupby("key_rate", sort=False):
        ax.plot(g.shift_bp, g.full_reprice_cr, color=colors[lab], label=lab,
                linestyle="--" if lab == "Parallel" else "-",
                marker="o", markersize=4, markeredgecolor=SURFACE, markeredgewidth=1)
        end = g.iloc[-1]
        ax.annotate(f"{lab}  {end.full_reprice_cr:,.0f}", (end.shift_bp, end.full_reprice_cr),
                    xytext=(6, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
    ax.axhline(0, color=AXIS, linewidth=0.8)
    ax.axvline(0, color=AXIS, linewidth=0.8)
    ax.set_xlim(-max_bp - 3, max_bp + 22)
    ax.set_xticks(np.arange(-max_bp, max_bp + 1, 10))
    ax.set_xlabel("Bump at the key rate (bp); other key rates unchanged")
    ax.set_ylabel("Change in portfolio value (Rs crore), full reprice")
    ax.set_title(f"Portfolio value change for key-rate bumps of ±{max_bp}bp "
                 f"(net MV Rs {sens['net_mv'] / CR:,.0f} cr)")
    ax.legend(loc="upper right", ncols=5)
    fig.text(0.01, 0.005, sub, fontsize=8, color=INK2)
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(out / f"keyrate_bump_dmv_{tag}.png", dpi=160)
    plt.close(fig)

    # 4: small multiples, error of each approximation vs full reprice
    fig, axes = plt.subplots(1, 5, figsize=(16, 4.2), sharex=True)
    for ax, (lab, g) in zip(axes, grid.groupby("key_rate", sort=False)):
        ax.plot(g.shift_bp, g.duration_only_cr - g.full_reprice_cr, color=S1, label="Duration only")
        ax.plot(g.shift_bp, g.duration_convexity_cr - g.full_reprice_cr, color=S2,
                label="Duration + convexity")
        ax.axhline(0, color=AXIS, linewidth=0.8)
        ax.set_title(lab)
        ax.set_xlabel("bp")
    axes[0].set_ylabel("Approximation − full reprice (Rs crore)")
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="upper right", ncols=2)
    fig.suptitle("Approximation error vs full reprice (each panel has its own y-scale)",
                 x=0.01, ha="left", fontsize=11, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out / f"approx_error_{tag}.png", dpi=160)
    plt.close(fig)

    # tables (the table view for the charts)
    krc["table"].to_csv(out / f"krc_{tag}.csv")
    grid.to_csv(out / f"keyrate_bump_grid_{tag}.csv", index=False)
    wide = grid.pivot(index="shift_bp", columns="key_rate", values="full_reprice_cr")[KEY_LABELS + ["Parallel"]]
    wide.to_csv(out / f"keyrate_bump_grid_wide_{tag}.csv")
    return out


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--date", default="2026-11-19")
    p.add_argument("--max-bp", type=float, default=50)
    p.add_argument("--step", type=float, default=5)
    p.add_argument("--space", choices=["spot", "par"], default="spot")
    a = p.parse_args(argv)
    out = make_charts(a.date, max_bp=a.max_bp, step=a.step, space=a.space)
    tag = "_par" if a.space == "par" else ""
    print(f"Charts and tables: {out}")
    wide = pd.read_csv(out / f"keyrate_bump_grid_wide_{a.date.replace('-', '')}{tag}.csv", index_col=0)
    print((wide.loc[wide.index % 10 == 0]).round(1).to_string())
    print(pd.read_csv(out / f"krc_{a.date.replace('-', '')}{tag}.csv", index_col=0).round(2).to_string())


if __name__ == "__main__":
    main()
