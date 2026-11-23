"""Engine configuration (spec section 2). Every run records the values used in its log."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class EngineConfig:
    # conventions
    time_basis: str = "ACT/365"            # or "30/360"; settled by the Stage 0 test
    interpolation: str = "linear"          # "linear" | "cubic" | "pchip"
    curve_source: str = "ccil"             # "ccil": CCIL NS (File B) | "own": Svensson fitted to FBIL (File G)
    key_tenors: tuple = (2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 30.0, 40.0)   # one key per liquid benchmark
    settlement_lag: int = 1                # T+1 business day
    kr_bump_bp: float = 1.0
    convexity_bump_bp: float = 10.0

    # calibration (Stage A)
    window_start: str = "2024-04-01"
    max_gap_days: int = 4

    # NS validation (Stage B)
    tau_min: float = 0.3
    tau_max: float = 20.0
    beta2_bound: float = 20.0
    jump_flag_bp: float = 25.0
    repo_rate: float | None = None         # % ; enables the short-rate sanity warning
    short_rate_warn_bp: float = 100.0
    long_rate_warn_bp: float = 75.0

    # pricing
    price_check_paise: float = 25.0
    ex_interest_days: int | None = None    # open item: RBI record-date rule; None = disabled
    extrapolation_max_years: float = 40.0
    extrapolation_min_years: float = 0.25

    # scenarios
    parallel_bp: tuple = (-100.0, -50.0, -25.0, 25.0, 50.0, 100.0)
    basel_inr_bp: dict = field(default_factory=lambda: {"parallel": 400.0, "short": 500.0, "long": 300.0})
    var_levels: tuple = (0.95, 0.99)

    # paths
    data_dir: Path = ROOT / "data"
    output_dir: Path = ROOT / "outputs"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["data_dir"], d["output_dir"] = str(self.data_dir), str(self.output_dir)
        return d
