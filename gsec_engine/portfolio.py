"""Stage C (positions) and Stage G (position and portfolio values)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .bonds import Bond, Instrument, dirty_price, instrument_from_bond, ytm_from_dirty
from .config import EngineConfig
from .conventions import BusinessCalendar
from .store import bond_from_row


@dataclass
class Position:
    pos_id: str
    instrument: Instrument
    signed_face: float                 # Rs; long +, short -
    direction: str
    bond: Bond | None = None
    opened_on: str = ""
    flags: list = field(default_factory=list)

    @property
    def name(self):
        return self.instrument.name


def make_position(pos_id, inst: Instrument, direction: str, face: float, bond=None,
                  opened_on="") -> Position:
    direction = direction.strip().lower()
    if direction not in ("long", "short"):
        raise ValueError(f"{pos_id}: direction must be long or short")
    sign = 1.0 if direction == "long" else -1.0
    return Position(str(pos_id), inst, sign * abs(float(face)), direction, bond, opened_on)


def positions_from_file(rows: pd.DataFrame, settlement: date, cal: BusinessCalendar, curve,
                        cfg: EngineConfig) -> list[Position]:
    """Build positions from File C rows. size_type 'mv' converts to face using today's dirty price."""
    out = []
    for _, r in rows.iterrows():
        bond = bond_from_row(r)
        inst = instrument_from_bond(bond, settlement, cal, cfg.time_basis, cfg.ex_interest_days,
                                    cfg.extrapolation_max_years, cfg.extrapolation_min_years)
        size_type = str(r.get("size_type", "face")).strip().lower()
        size = float(r["size_inr"])
        flags = []
        if size_type == "mv":
            face = size / dirty_price(inst, curve) * 100.0
            flags.append(f"{r['pos_id']}: face {face:,.0f} derived from MV {size:,.0f}")
        elif size_type == "face":
            face = size
        else:
            raise ValueError(f"{r['pos_id']}: size_type must be 'face' or 'mv'")
        p = make_position(r["pos_id"], inst, r["direction"], face, bond, str(r.get("opened_on", "")))
        p.flags = inst.flags + flags
        out.append(p)
    return out


def valuation_table(positions: list[Position], curve) -> tuple[pd.DataFrame, dict]:
    rows = []
    for p in positions:
        inst = p.instrument
        dirty = dirty_price(inst, curve)
        clean = dirty - inst.accrued
        mv = dirty * p.signed_face / 100.0
        rows.append({
            "pos_id": p.pos_id, "name": p.name, "isin": inst.isin, "direction": p.direction,
            "signed_face_inr": p.signed_face, "coupon_pct": inst.coupon,
            "prev_coupon": inst.prev_coupon, "next_coupon": inst.next_coupon,
            "days_accrued_30_360": inst.days_accrued, "residual_years": round(inst.maturity_years, 4),
            "clean_price": clean, "accrued_per100": inst.accrued, "dirty_price": dirty,
            "ytm_semiannual_pct": ytm_from_dirty(inst, dirty),
            "signed_accrued_inr": inst.accrued * p.signed_face / 100.0,
            "signed_mv_inr": mv,
        })
    df = pd.DataFrame(rows)
    mv = df["signed_mv_inr"]
    w = mv.abs() / mv.abs().sum()
    totals = {
        "net_mv_inr": float(mv.sum()),
        "gross_mv_inr": float(mv.abs().sum()),
        "long_mv_inr": float(mv[mv > 0].sum()),
        "short_mv_inr": float(mv[mv < 0].sum()),
        "net_accrued_inr": float(df["signed_accrued_inr"].sum()),
        "value_weighted_ytm_pct": float((w * df["ytm_semiannual_pct"]).sum()),
        "ytm_weighting": "|MV|-weighted",
    }
    return df, totals


def net_by_isin(val: pd.DataFrame) -> pd.DataFrame:
    """Duplicate ISINs are allowed but netted in reports."""
    key = val["isin"].where(val["isin"].astype(str) != "", val["name"])
    return (val.assign(key=key).groupby("key")
            .agg(name=("name", "first"), positions=("pos_id", "count"),
                 net_face_inr=("signed_face_inr", "sum"), net_mv_inr=("signed_mv_inr", "sum"))
            .reset_index())
