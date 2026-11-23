"""Stage A: historical calibration of the four key rates (File A -> File D)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import EngineConfig
from .store import KEY_COLS


def daily_changes(a: pd.DataFrame, cfg: EngineConfig) -> pd.DataFrame:
    """bp changes between consecutive valid days at most cfg.max_gap_days calendar days apart."""
    a = a[a["date"] >= pd.Timestamp(cfg.window_start)].sort_values("date").reset_index(drop=True)
    a = a[(a[KEY_COLS] > 0).all(axis=1)]
    gap = a["date"].diff().dt.days
    dy = a[KEY_COLS].diff() * 100.0
    dy.insert(0, "date", a["date"])
    dy["gap_days"] = gap
    return dy[(gap > 0) & (gap <= cfg.max_gap_days)].dropna().reset_index(drop=True)


def calibrate(a: pd.DataFrame, cfg: EngineConfig) -> tuple[dict, pd.DataFrame]:
    dy = daily_changes(a, cfg)
    if len(dy) < 30:
        raise ValueError(f"Only {len(dy)} usable daily changes in the window; need at least 30")
    X = dy[KEY_COLS]
    sd = X.std()
    mad = (X - X.median()).abs().median() * 1.4826
    cov = X.cov()
    corr = X.corr()

    eigval, eigvec = np.linalg.eigh(cov.values)
    order = np.argsort(eigval)[::-1]
    eigval, eigvec = eigval[order], eigvec[:, order]
    # sign conventions: PC1 positive on average; PC2 steepener (30Y > 2Y); PC3 positive belly
    if eigvec[:, 0].sum() < 0:
        eigvec[:, 0] *= -1
    if eigvec[3, 1] - eigvec[0, 1] < 0:
        eigvec[:, 1] *= -1
    if eigvec[1:3, 2].mean() - eigvec[[0, 3], 2].mean() < 0:
        eigvec[:, 2] *= -1

    names = ["PC1_level", "PC2_slope", "PC3_curvature", "PC4"]
    calib = {
        "window_start": str(dy["date"].iloc[0].date()),
        "window_end": str(dy["date"].iloc[-1].date()),
        "config_window_start": cfg.window_start,
        "n_changes": int(len(dy)),
        "keys": KEY_COLS,
        "sigma_bp": sd.round(6).to_dict(),
        "robust_sigma_bp": mad.round(6).to_dict(),
        "cov_bp2": cov.round(8).values.tolist(),
        "corr": corr.round(6).values.tolist(),
        "pc_names": names,
        "loadings": eigvec.round(8).tolist(),            # rows = keys, cols = PCs
        "eigenvalues_bp2": eigval.round(8).tolist(),
        "pc_sigma_bp": np.sqrt(np.clip(eigval, 0, None)).round(6).tolist(),
        "explained": (eigval / eigval.sum()).round(6).tolist(),
    }
    scen = dy[["date"] + KEY_COLS].copy()
    return calib, scen
