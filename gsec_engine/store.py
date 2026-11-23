"""Data stores A-F, Stage B NS input/validation, and import of the NS parameter file."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .bonds import Bond, parse_coupon_dates
from .config import EngineConfig
from .conventions import BusinessCalendar, to_date
from .curves import KEYS, NSCurve, SvenssonCurve

KEY_COLS = ["r2", "r5", "r10", "r30"]
B_COLS = ["date", "b0", "b1", "b2", "tau", "source", "entered_at", "flags", "notes"]
C_COLS = ["pos_id", "name", "isin", "coupon", "issue_date", "maturity_date", "coupon_dates",
          "direction", "size_type", "size_inr", "opened_on"]


class NeedsReview(Exception):
    """Raised when a valid NS row moves a key rate more than the review threshold."""


@dataclass
class ValidationResult:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    review: list = field(default_factory=list)
    key_rates: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


class Store:
    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        d = Path(cfg.data_dir)
        d.mkdir(parents=True, exist_ok=True)
        self.path_A = d / "A_key_rates.csv"
        self.path_B = d / "B_ns_params.csv"
        self.path_C = d / "C_portfolio.csv"
        self.path_D = d / "D_calibration.json"
        self.path_D_scen = d / "D_hist_scenarios.csv"
        self.path_E = d / "E_bond_master.csv"
        self.path_F = d / "F_market_prices.csv"
        self.path_G = d / "G_fitted_curves.csv"
        self.path_hol = d / "holidays.csv"
        self.path_import_log = d / "import_log.csv"

    # ---------- calendar ----------
    def calendar(self) -> BusinessCalendar:
        return BusinessCalendar.from_csv(self.path_hol)

    # ---------- File A ----------
    def load_A(self) -> pd.DataFrame:
        if not self.path_A.exists():
            df = pd.DataFrame(columns=["date"] + KEY_COLS + ["source"])
            df["date"] = pd.to_datetime(df["date"])
            return df
        df = pd.read_csv(self.path_A, parse_dates=["date"])
        return df.sort_values("date").reset_index(drop=True)

    def save_A(self, df: pd.DataFrame):
        df = df.sort_values("date", kind="stable").drop_duplicates("date", keep="last")
        df.to_csv(self.path_A, index=False, date_format="%Y-%m-%d", float_format="%.6f")

    # ---------- File B ----------
    def load_B(self) -> pd.DataFrame:
        if not self.path_B.exists():
            df = pd.DataFrame(columns=B_COLS)
            df["date"] = pd.to_datetime(df["date"])
            return df
        return pd.read_csv(self.path_B, parse_dates=["date"], keep_default_na=False)

    def latest_ns(self, on_or_before: date) -> tuple[NSCurve, pd.Series]:
        b = self.load_B()
        b = b[b["date"].dt.date <= on_or_before]
        if b.empty:
            raise LookupError(f"No NS parameters stored on or before {on_or_before}")
        row = b.sort_values(["date", "entered_at"]).iloc[-1]       # latest correction wins
        curve = NSCurve(float(row.b0), float(row.b1), float(row.b2), float(row.tau),
                        label=f"NS {row.date:%d-%m-%Y} ({row.source})")
        return curve, row

    # ---------- curve choice: CCIL NS (File B) or own Svensson fitted to FBIL (File G) ----------
    def latest_own(self, on_or_before: date, model: str = "svensson") -> tuple[SvenssonCurve, pd.Series]:
        if not self.path_G.exists():
            raise LookupError("No own curve (File G): upload an FBIL price file in the Yield builder")
        g = pd.read_csv(self.path_G, parse_dates=["date"], keep_default_na=False)
        g = g[(g["model"] == model) & (g["date"].dt.date <= on_or_before)]
        if g.empty:
            raise LookupError(f"No own {model} curve on or before {on_or_before}: upload that day's FBIL file "
                              "in the Yield builder")
        row = g.sort_values(["date", "fitted_at"]).iloc[-1]
        curve = SvenssonCurve(*(float(row[k]) for k in SvenssonCurve.NAMES),
                              label=f"own Svensson {row.date:%d-%m-%Y} (FBIL)")
        return curve, row

    def curve_for(self, on_or_before: date, source: str = "ccil"):
        """(curve, info) for the valuation curve chosen by `source`: 'ccil' (CCIL NS, File B) or
        'own' (Svensson fitted to FBIL prices, File G). info: date, label, params, flags."""
        if source == "ccil":
            curve, r = self.latest_ns(on_or_before)
            return curve, {"source": "ccil", "label": "CCIL NS", "date": r["date"].date(),
                           "origin": str(r.source), "model": "Nelson-Siegel",
                           "params": {"b0": float(r.b0), "b1": float(r.b1), "b2": float(r.b2), "tau": float(r.tau)},
                           "flags": str(r["flags"]).strip(), "entered_at": str(r.entered_at)}
        if source == "own":
            curve, r = self.latest_own(on_or_before)
            return curve, {"source": "own", "label": "Own Svensson (FBIL)", "date": r["date"].date(),
                           "origin": str(r.source), "model": "Svensson",
                           "params": {k: float(r[k]) for k in SvenssonCurve.NAMES},
                           "flags": "", "entered_at": str(r.fitted_at),
                           "fit": {"n_bonds": int(float(r.n_bonds)), "rmse_bp": float(r.rmse_bp),
                                   "rmse_traded_bp": float(r.rmse_traded_bp) if str(r.rmse_traded_bp) else None}}
        raise ValueError(f"curve source must be 'ccil' or 'own', not {source!r}")

    def latest_curve_date(self, source: str) -> str | None:
        try:
            return str(self.curve_for(date.max, source)[1]["date"])
        except LookupError:
            return None

    # ---------- Stage B validation ----------
    def validate_ns(self, d: date, b0, b1, b2, tau, *, correction: bool = False,
                    prev_rates: np.ndarray | None = None, prev_date: date | None = None,
                    cal: BusinessCalendar | None = None,
                    existing_dates: set | None = None) -> ValidationResult:
        cfg, res = self.cfg, ValidationResult()
        cal = cal or self.calendar()
        if all(float(x) == 0.0 for x in (b0, b1, b2, tau)):
            res.errors.append("all-zero row")
            return res
        if not cfg.tau_min < tau < cfg.tau_max:
            res.errors.append(f"tau={tau} outside ({cfg.tau_min}, {cfg.tau_max}): degenerate fit")
        if abs(b2) >= cfg.beta2_bound:
            res.errors.append(f"|beta2|={abs(b2)} at/above optimiser bound {cfg.beta2_bound}")
        if not cal.is_business_day(d):
            res.errors.append(f"{d} is not a business day")
        if existing_dates is None:
            existing_dates = set(self.load_B()["date"].dt.date)
        if d in existing_dates and not correction:
            res.errors.append(f"{d} already stored (use a correction row with a note)")
        if res.errors:
            return res

        curve = NSCurve(b0, b1, b2, tau)
        rates = curve.zero(KEYS)
        res.key_rates = dict(zip(KEY_COLS, rates))
        if cfg.repo_rate is not None and abs(b0 + b1 - cfg.repo_rate) * 100 > cfg.short_rate_warn_bp:
            res.warnings.append(f"short rate b0+b1={b0 + b1:.2f}% vs repo {cfg.repo_rate:.2f}%")
        if abs(curve.zero(40.0) - b0) * 100 > cfg.long_rate_warn_bp:
            res.warnings.append(f"40Y rate {curve.zero(40.0):.2f}% far from b0 {b0:.2f}%")
        if prev_rates is None:
            a = self.load_A()
            a = a[a["date"].dt.date < d]
            prev_rates = a[KEY_COLS].iloc[-1].values if len(a) else None
            prev_date = a["date"].iloc[-1].date() if len(a) else None
        if prev_rates is not None and prev_date is not None and (d - prev_date).days > 7:
            res.warnings.append(f"previous stored day {prev_date} is over 7 days earlier: "
                                "jump check skipped")
            prev_rates = None
        if prev_rates is not None:
            moves = (rates - np.asarray(prev_rates, float)) * 100
            for k, mv in zip(KEYS, moves):
                if abs(mv) > cfg.jump_flag_bp:
                    res.review.append(f"{k:g}Y moved {mv:+.1f}bp vs previous stored day")
        return res

    def add_ns_row(self, d, b0, b1, b2, tau, source="manual", *, notes="", correction=False,
                   accept_review=False) -> ValidationResult:
        """Stage B: validate, then append to File B and the key rates to File A."""
        d = to_date(d)
        res = self.validate_ns(d, b0, b1, b2, tau, correction=correction)
        if not res.ok:
            raise ValueError("NS row rejected: " + "; ".join(res.errors))
        if res.review and not accept_review:
            raise NeedsReview("; ".join(res.review))
        if correction and not notes:
            raise ValueError("correction rows need a note")
        flags = res.warnings + [f"REVIEWED: {r}" for r in res.review]
        row = pd.DataFrame([{"date": pd.Timestamp(d), "b0": b0, "b1": b1, "b2": b2, "tau": tau,
                             "source": source, "entered_at": datetime.now().isoformat(timespec="seconds"),
                             "flags": " | ".join(flags), "notes": notes}])
        b = pd.concat([self.load_B(), row], ignore_index=True) if self.path_B.exists() else row
        b.to_csv(self.path_B, index=False, date_format="%Y-%m-%d")
        a = self.load_A()
        new = pd.DataFrame([{"date": pd.Timestamp(d), **res.key_rates, "source": f"NS:{source}"}])
        self.save_A(pd.concat([a, new], ignore_index=True))
        return res

    # ---------- import of the source NS file ----------
    def import_ns_file(self, path: str | Path, source: str | None = None) -> pd.DataFrame:
        """Read the source file (layout: 2 title rows, then 'Date, b0, b1, b2, tau, spot rates...').
        Every row is validated; valid rows go to File B, and rows from the calibration window
        that pass validation and are not flagged go to File A. Returns the import log."""
        path = Path(path)
        source = source or f"import:{path.name}"
        if path.suffix.lower() in (".xlsx", ".xls"):
            raw = pd.read_excel(path, header=None)        # needs openpyxl
        else:
            import csv
            with open(path, newline="", encoding="latin-1") as fh:
                rows = [r + [""] * (5 - len(r)) for r in csv.reader(fh)]   # tolerate ragged rows
            raw = pd.DataFrame([r[:5] for r in rows], dtype=str)
        hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip().str.lower() == "date"]
        start = int(hdr[0]) + 1 if len(hdr) else 0
        body = raw.iloc[start:, :5].copy()
        body.columns = ["date", "b0", "b1", "b2", "tau"]
        body["date"] = pd.to_datetime(body["date"].astype(str).str.strip(), format="%d-%m-%Y",
                                      errors="coerce")
        body = body.dropna(subset=["date"])
        for c in ["b0", "b1", "b2", "tau"]:
            body[c] = pd.to_numeric(body[c], errors="coerce")
        body = body.dropna().sort_values("date")

        cal = self.calendar()
        existing_B = self.load_B()
        existing_dates = set(existing_B["date"].dt.date) if len(existing_B) else set()
        win = pd.Timestamp(self.cfg.window_start)
        prev, prev_d, log, b_rows, a_rows = None, None, [], [], []
        now = datetime.now().isoformat(timespec="seconds")
        for r in body.itertuples(index=False):
            d = r.date.date()
            res = self.validate_ns(d, r.b0, r.b1, r.b2, r.tau, prev_rates=prev, prev_date=prev_d,
                                   cal=cal, existing_dates=existing_dates)
            status = "rejected" if not res.ok else ("flagged" if res.review else "ok")
            log.append({"date": d, "status": status,
                        "detail": "; ".join(res.errors + res.review + res.warnings)})
            if not res.ok:
                continue
            b_rows.append({"date": r.date, "b0": r.b0, "b1": r.b1, "b2": r.b2, "tau": r.tau,
                           "source": source, "entered_at": now,
                           "flags": " | ".join(res.warnings + [f"REVIEW: {x}" for x in res.review]),
                           "notes": "bulk import"})
            existing_dates.add(d)
            if status == "ok":
                prev, prev_d = np.array(list(res.key_rates.values())), d
                if r.date >= win:
                    a_rows.append({"date": r.date, **res.key_rates, "source": f"NS:{source}"})
        if b_rows:
            new_b = pd.DataFrame(b_rows)
            b = pd.concat([existing_B, new_b], ignore_index=True) if len(existing_B) else new_b
            b.sort_values(["date", "entered_at"]).to_csv(self.path_B, index=False,
                                                          date_format="%Y-%m-%d")
        if a_rows:
            self.save_A(pd.concat([self.load_A(), pd.DataFrame(a_rows)], ignore_index=True))
        logdf = pd.DataFrame(log)
        logdf.to_csv(self.path_import_log, index=False)
        return logdf

    def seed_A_from_spot_csv(self, path: str | Path) -> int:
        """Fallback: seed File A from a spot-rate table (columns Date, 2, 5, 10, 30) when the
        NS parameter file is not available. Imported NS rows later replace these dates."""
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["Date"], format="%d-%m-%Y")
        df = df[df["date"] >= pd.Timestamp(self.cfg.window_start)]
        out = pd.DataFrame({"date": df["date"], "r2": df["2"], "r5": df["5"], "r10": df["10"],
                            "r30": df["30"], "source": "seed:spot-csv(2dp)"})
        out = out[(out[KEY_COLS] > 0).all(axis=1)]
        existing = self.load_A()
        keep = existing[~existing["source"].astype(str).str.startswith("seed")]
        merged = pd.concat([out, keep], ignore_index=True)        # NS-derived rows win
        self.save_A(merged)
        return len(out)

    # ---------- Files C, E, F ----------
    def load_C(self) -> pd.DataFrame:
        return pd.read_csv(self.path_C, dtype={"isin": str}, keep_default_na=False) \
            if self.path_C.exists() else pd.DataFrame(columns=C_COLS)

    def save_C(self, df: pd.DataFrame):
        df[C_COLS].to_csv(self.path_C, index=False)

    def load_E(self) -> pd.DataFrame:
        return pd.read_csv(self.path_E, dtype={"isin": str}, keep_default_na=False) \
            if self.path_E.exists() else pd.DataFrame()

    def load_F(self) -> pd.DataFrame:
        if not self.path_F.exists():
            return pd.DataFrame(columns=["date", "isin", "clean_price", "source"])
        return pd.read_csv(self.path_F, parse_dates=["date"], dtype={"isin": str})

    # ---------- File D ----------
    def save_D(self, calib: dict, scen: pd.DataFrame):
        self.path_D.write_text(json.dumps(calib, indent=2, default=str))
        scen.to_csv(self.path_D_scen, index=False, date_format="%Y-%m-%d")

    def load_D(self) -> tuple[dict, pd.DataFrame]:
        if not self.path_D.exists():
            raise LookupError("No calibration (File D). Run the 'calibrate' step first.")
        return (json.loads(self.path_D.read_text()),
                pd.read_csv(self.path_D_scen, parse_dates=["date"]))


def bond_from_row(row) -> Bond:
    return Bond(name=str(row["name"]), coupon=float(row["coupon"]),
                issue=to_date(row["issue_date"]), maturity=to_date(row["maturity_date"]),
                coupon_dates=parse_coupon_dates(row["coupon_dates"]), isin=str(row.get("isin", "")))
