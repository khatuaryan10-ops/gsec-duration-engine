"""Bond definitions, coupon schedule, accrued interest, pricing and YTM (Stages C, E, F)."""
from __future__ import annotations

import calendar as _cal
import re
from dataclasses import dataclass, field
from datetime import date

import numpy as np
from scipy.optimize import brentq

from .conventions import BusinessCalendar, add_months, days_30_360, year_fraction

MONTHS = {m.lower(): i for i, m in enumerate(_cal.month_abbr) if m}


_PAIR = re.compile(r"^\s*(\d{1,2})\s*[-\s.]?\s*([A-Za-z]{3,9})\.?\s*$|^\s*([A-Za-z]{3,9})\.?\s*[-\s.]?\s*(\d{1,2})\s*$")


def parse_coupon_dates(text: str) -> tuple:
    """Two day-month pairs separated by '/', ',' or ';'.

    Accepts '15-Apr/15-Oct', '15 April / 15 October', '15Apr,15Oct', 'Apr-15/Oct-15'
    -> ((15, 4), (15, 10)). Raises ValueError with a readable message otherwise."""
    parts = [p for p in re.split(r"[/,;&]|\band\b", str(text)) if p.strip()]
    if len(parts) != 2:
        raise ValueError(f"coupon dates '{text}': give two day-month pairs, e.g. 10-Apr/10-Oct")
    pairs = []
    for part in parts:
        m = _PAIR.match(part)
        if not m:
            raise ValueError(f"coupon date '{part.strip()}' not understood; use day-month, e.g. 2-Jul")
        d, mon = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        key = mon[:3].lower()
        if key not in MONTHS or (len(mon) > 3 and not _cal.month_name[MONTHS[key]].lower().startswith(mon.lower())):
            raise ValueError(f"coupon date '{part.strip()}': unknown month '{mon}'")
        day, month = int(d), MONTHS[key]
        if not 1 <= day <= _cal.monthrange(2024, month)[1]:
            raise ValueError(f"coupon date '{part.strip()}': day {day} does not exist in {_cal.month_abbr[month]}")
        pairs.append((day, month))
    return tuple(pairs)


@dataclass
class Bond:
    name: str
    coupon: float                 # % p.a.
    issue: date
    maturity: date
    coupon_dates: tuple           # ((day, month), (day, month))
    isin: str = ""

    def validate(self) -> list[str]:
        """Flags (never silently corrected)."""
        flags = []
        if not 0 <= self.coupon <= 15:
            flags.append(f"{self.name}: coupon {self.coupon}% outside 0-15%")
        if len(self.coupon_dates) != 2:
            flags.append(f"{self.name}: expected two coupon day-month pairs")
        mend = _cal.monthrange(self.maturity.year, self.maturity.month)[1]
        if not any(m == self.maturity.month and min(d, mend) == self.maturity.day
                   for d, m in self.coupon_dates):
            flags.append(f"{self.name}: coupon dates {self.coupon_dates} do not match maturity "
                         f"{self.maturity:%d-%b}")
        months = sorted(m for _, m in self.coupon_dates)
        if len(months) == 2 and months[1] - months[0] != 6:
            flags.append(f"{self.name}: coupon months are not 6 months apart")
        if self.issue >= self.maturity:
            flags.append(f"{self.name}: issue date not before maturity")
        return flags

    def schedule(self) -> list[date]:
        out = []
        for y in range(self.issue.year, self.maturity.year + 1):
            for d, m in self.coupon_dates:
                dt = date(y, m, min(d, _cal.monthrange(y, m)[1]))
                if self.issue < dt <= self.maturity:
                    out.append(dt)
        if not out or out[-1] != self.maturity:
            out.append(self.maturity)
        return sorted(set(out))


@dataclass
class Instrument:
    """Remaining cash flows (per Rs100 face) seen from settlement."""
    name: str
    times: np.ndarray             # years from settlement (pricing time basis)
    amounts: np.ndarray           # per Rs100 face
    ytm_exponents: np.ndarray     # half-year periods for the semi-annual YTM
    accrued: float                # per Rs100 face
    maturity_years: float
    isin: str = ""
    coupon: float = 0.0
    pay_dates: list = field(default_factory=list)
    prev_coupon: date | None = None
    next_coupon: date | None = None
    days_accrued: int = 0
    flags: list = field(default_factory=list)


def instrument_from_bond(bond: Bond, settlement: date, cal: BusinessCalendar,
                         basis: str = "ACT/365", ex_interest_days: int | None = None,
                         ext_max: float = 40.0, ext_min: float = 0.25) -> Instrument:
    flags = list(bond.validate())
    if not bond.issue < settlement < bond.maturity:
        raise ValueError(f"{bond.name}: need issue < settlement ({settlement}) < maturity")

    sched = bond.schedule()
    first = sched[0]
    regular_start = add_months(first, -6)
    first_amount = bond.coupon / 2.0
    if regular_start != bond.issue:
        first_amount = bond.coupon * days_30_360(bond.issue, first) / 360.0
        flags.append(f"{bond.name}: irregular first coupon period "
                     f"({bond.issue:%d-%b-%Y} to {first:%d-%b-%Y}), amount {first_amount:.4f}")

    paid = [d for d in sched if d <= settlement]
    prev = paid[-1] if paid else bond.issue
    remaining = [d for d in sched if d > settlement]
    nxt = remaining[0]

    amounts = np.array([first_amount if d == first else bond.coupon / 2.0 for d in remaining])
    amounts[-1] += 100.0

    days = days_30_360(prev, settlement)
    accrued = bond.coupon * days / 360.0

    if ex_interest_days is not None and (nxt - settlement).days <= ex_interest_days:
        flags.append(f"{bond.name}: settlement in ex-interest period; next coupon excluded, "
                     "negative accrued (confirm RBI record-date rule)")
        accrued = -bond.coupon * days_30_360(settlement, nxt) / 360.0
        if len(remaining) > 1:
            amounts = amounts[1:]
            remaining = remaining[1:]

    adj = [cal.following(d) for d in remaining]
    times = np.array([year_fraction(settlement, d, basis) for d in adj])
    w = days_30_360(settlement, remaining[0]) / 180.0
    exps = w + np.arange(len(remaining))

    mat_years = year_fraction(settlement, bond.maturity, "ACT/365")
    if mat_years > ext_max or mat_years < ext_min:
        flags.append(f"{bond.name}: residual maturity {mat_years:.2f}Y outside "
                     f"{ext_min}-{ext_max}Y, NS curve is extrapolating")

    return Instrument(bond.name, times, amounts, exps, accrued, mat_years, bond.isin, bond.coupon,
                      adj, prev, nxt, days, flags)


def test_mode_instrument(name: str, coupon: float, years: float) -> Instrument:
    """Spec T2-T5 test mode: valuation on a coupon date, cash flows at 0.5, 1.0, ... years."""
    n = int(round(years * 2))
    times = np.arange(1, n + 1) / 2.0
    amounts = np.full(n, coupon / 2.0)
    amounts[-1] += 100.0
    return Instrument(name, times, amounts, times * 2.0, 0.0, float(years), coupon=coupon)


test_mode_instrument.__test__ = False      # not a pytest test


def dirty_price(inst: Instrument, curve) -> float:
    return float(np.sum(inst.amounts * np.exp(-curve.zero(inst.times) / 100.0 * inst.times)))


def ytm_from_dirty(inst: Instrument, dirty: float) -> float:
    """Semi-annual yield (%), discounting at (1 + y/2)^-(periods), periods on 30/360."""
    def f(y):
        return np.sum(inst.amounts / (1.0 + y / 200.0) ** inst.ytm_exponents) - dirty
    return brentq(f, -50.0, 100.0)
