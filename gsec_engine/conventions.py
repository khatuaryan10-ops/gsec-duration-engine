"""Day counts, business-day calendar and settlement (spec sections 2 and Stage D)."""
from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta
from pathlib import Path

import pandas as pd


def days_30_360(d1: date, d2: date) -> int:
    """30/360 days as in the spec: D1=31 -> 30; D2=31 and D1>=30 -> 30. No February rule
    (open item: exact FIMMDA variant)."""
    D1, D2 = d1.day, d2.day
    if D1 == 31:
        D1 = 30
    if D2 == 31 and D1 >= 30:
        D2 = 30
    return 360 * (d2.year - d1.year) + 30 * (d2.month - d1.month) + (D2 - D1)


def year_fraction(d1: date, d2: date, basis: str) -> float:
    if basis == "ACT/365":
        return (d2 - d1).days / 365.0
    if basis == "30/360":
        return days_30_360(d1, d2) / 360.0
    raise ValueError(f"Unknown time basis {basis!r}")


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, _cal.monthrange(y, m)[1]))


class BusinessCalendar:
    """Weekends plus a holiday list (data/holidays.csv, column 'date', YYYY-MM-DD)."""

    def __init__(self, holidays=()):
        self.holidays = {pd.Timestamp(h).date() for h in holidays}

    @classmethod
    def from_csv(cls, path: Path) -> "BusinessCalendar":
        if not Path(path).exists():
            return cls()
        df = pd.read_csv(path, comment="#")
        return cls(df["date"].dropna().tolist() if "date" in df else [])

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def following(self, d: date) -> date:
        while not self.is_business_day(d):
            d += timedelta(days=1)
        return d

    def add_business_days(self, d: date, n: int) -> date:
        step = 1 if n >= 0 else -1
        for _ in range(abs(n)):
            d += timedelta(days=step)
            while not self.is_business_day(d):
                d += timedelta(days=step)
        return d

    def has_year(self, year: int) -> bool:
        return any(h.year == year for h in self.holidays)


def settlement_date(valuation: date, cal: BusinessCalendar, lag: int = 1) -> date:
    return cal.add_business_days(valuation, lag)


def to_date(x) -> date:
    if isinstance(x, date) and not isinstance(x, pd.Timestamp):
        return x
    return pd.Timestamp(x).date()
