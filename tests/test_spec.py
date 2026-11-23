"""Spec section 6 test cases T1-T6, plus edge cases from section 5."""
from datetime import date

import numpy as np
import pytest

from gsec_engine.bonds import Bond, dirty_price, instrument_from_bond, test_mode_instrument
from gsec_engine.config import EngineConfig
from gsec_engine.conventions import BusinessCalendar, days_30_360, settlement_date
from gsec_engine.curves import BumpInterpolator, KeyRateCurve, NSCurve
from gsec_engine.portfolio import make_position
from gsec_engine.risk import PortfolioPricer, convexity, evaluate_scenario, krd_kr01
from gsec_engine.store import NeedsReview, Store

KR = [6.30, 6.87, 7.36, 7.88]
CURVE = KeyRateCurve(KR)
LIN = BumpInterpolator("linear")


def pricer(*positions, interp=LIN):
    return PortfolioPricer(list(positions), CURVE, interp)


def T2():
    return make_position("T2", test_mode_instrument("7Y 7%", 7.00, 7), "long", 100.0)


def T3():
    return make_position("T3", test_mode_instrument("4Y 6.5%", 6.50, 4), "long", 100.0)


# ---------------- T1 ----------------
def test_T1_accrued_interest():
    cal = BusinessCalendar()
    settle = settlement_date(date(2026, 11, 20), cal)
    assert settle == date(2026, 11, 23)
    assert days_30_360(date(2026, 10, 15), settle) == 38
    bond = Bond("6.9% GOI 2065", 6.9, date(2025, 4, 15), date(2065, 4, 15), ((15, 4), (15, 10)))
    inst = instrument_from_bond(bond, settle, cal)
    assert inst.prev_coupon == date(2026, 10, 15)
    assert inst.accrued == pytest.approx(0.7283, abs=5e-5)


# ---------------- T2 / T3 ----------------
@pytest.mark.parametrize("pos, price, krd, total", [
    (T2, 99.3621, [0.3852, 3.3845, 1.8632, 0.0], 5.6329),
    (T3, 99.0887, [1.3706, 2.2113, 0.0, 0.0], 3.5820),
])
def test_T2_T3_price_and_krd(pos, price, krd, total):
    p = pos()
    assert dirty_price(p.instrument, CURVE) == pytest.approx(price, abs=5e-5)
    s = krd_kr01(pricer(p))
    got = s["krd"].iloc[0]
    np.testing.assert_allclose(got[["KRD_2Y", "KRD_5Y", "KRD_10Y", "KRD_30Y"]].values, krd, atol=5e-5)
    assert got["sum_KRD"] == pytest.approx(total, abs=5e-5)
    assert got["sum_KRD"] == pytest.approx(got["parallel_duration"], abs=1e-6)


# ---------------- T4 ----------------
def test_T4_portfolio_kr01():
    long_ = make_position("L", test_mode_instrument("T2", 7.00, 7), "long", 100e7)
    short = make_position("S", test_mode_instrument("T3", 6.50, 4), "short", 50e7)
    s = krd_kr01(pricer(long_, short))
    np.testing.assert_allclose(s["net_kr01"] / 1e5, [-0.296, 2.267, 1.851, 0.0], atol=5e-4)
    assert s["net_kr01_total"] / 1e5 == pytest.approx(3.822, abs=5e-4)
    assert s["gross_kr01_positions"] / 1e5 == pytest.approx(7.372, abs=5e-4)
    assert s["kr01"].loc["S", "total_KR01"] < 0                     # short has negative KR01


# ---------------- T5 ----------------
def test_T5_parallel_100bp():
    pp = pricer(T2())
    s = krd_kr01(pp)
    r = evaluate_scenario(pp, s["net_kr01"], s["net_mv"], np.full(4, 100.0), 10.0)
    V0 = s["net_mv"]
    assert r["full_reprice"] / V0 * 100 == pytest.approx(-5.456, abs=5e-4)
    assert r["duration_only"] / V0 * 100 == pytest.approx(-5.633, abs=5e-4)
    assert r["duration_convexity"] / V0 * 100 == pytest.approx(-5.452, abs=5e-4)


# ---------------- T6 ----------------
@pytest.fixture
def store(tmp_path):
    return Store(EngineConfig(data_dir=tmp_path))


def test_T6_rejects_all_zero(store):
    with pytest.raises(ValueError, match="all-zero"):
        store.add_ns_row(date(2026, 1, 21), 0, 0, 0, 0)


def test_T6_rejects_degenerate_tau(store):
    with pytest.raises(ValueError, match="tau"):
        store.add_ns_row(date(2014, 3, 3), 9.3259, 1.53, -8.0089, 0.005)


def test_T6_flags_jump(store):
    store.add_ns_row(date(2026, 11, 18), 8.1627, -2.4437, -0.0001, 3.4817)
    with pytest.raises(NeedsReview):
        store.add_ns_row(date(2026, 11, 19), 8.5627, -2.4437, -0.0001, 3.4817)   # +40bp
    store.add_ns_row(date(2026, 11, 19), 8.5627, -2.4437, -0.0001, 3.4817, accept_review=True)
    assert len(store.load_B()) == 2 and "REVIEWED" in store.load_B()["flags"].iloc[-1]


def test_rejects_weekend_and_duplicate(store):
    with pytest.raises(ValueError, match="business day"):
        store.add_ns_row(date(2026, 11, 21), 8.16, -2.44, 0.0, 3.48)
    store.add_ns_row(date(2026, 11, 18), 8.1627, -2.4437, -0.0001, 3.4817)
    with pytest.raises(ValueError, match="already stored"):
        store.add_ns_row(date(2026, 11, 18), 8.1627, -2.4437, -0.0001, 3.4817)


# ---------------- extra checks ----------------
def test_ns_curve_matches_published_spot():
    c = NSCurve(8.1627, -2.4437, -0.0001, 3.4817)       # 18-11-2026 row of the source file
    np.testing.assert_allclose(c.zero([2, 5, 10, 30]), [6.30, 6.87, 7.36, 7.88], atol=0.006)


@pytest.mark.parametrize("method, tol", [("linear", 1e-6), ("cubic", 1e-6), ("pchip", 2e-2)])
def test_sum_krd_equals_parallel(method, tol):
    s = krd_kr01(pricer(T2(), interp=BumpInterpolator(method)))
    assert abs(s["krd"]["check_diff"].iloc[0]) < tol


def test_settlement_on_coupon_date_excludes_coupon():
    cal = BusinessCalendar()
    bond = Bond("x", 7.0, date(2025, 4, 15), date(2035, 4, 15), ((15, 4), (15, 10)))
    inst = instrument_from_bond(bond, date(2026, 10, 15), cal)
    assert inst.accrued == 0 and inst.pay_dates[0] == date(2027, 4, 15)


def test_maturing_within_days_flags_and_prices_near_par():
    cal = BusinessCalendar()
    bond = Bond("x", 7.0, date(2016, 11, 25), date(2026, 11, 25), ((25, 5), (25, 11)))
    inst = instrument_from_bond(bond, date(2026, 11, 23), cal)
    assert len(inst.amounts) == 1 and any("extrapolating" in f for f in inst.flags)
    assert dirty_price(inst, NSCurve(8.1627, -2.4437, 0, 3.4817)) == pytest.approx(103.5, abs=0.05)


def test_irregular_first_coupon_accrues_from_issue():
    cal = BusinessCalendar()
    bond = Bond("x", 7.0, date(2026, 6, 1), date(2036, 10, 15), ((15, 4), (15, 10)))
    inst = instrument_from_bond(bond, date(2026, 8, 3), cal)
    assert inst.prev_coupon == date(2026, 6, 1)
    assert inst.accrued == pytest.approx(7.0 * days_30_360(date(2026, 6, 1), date(2026, 8, 3)) / 360)
    assert any("irregular" in f for f in inst.flags)


def test_coupon_date_mismatch_is_flagged_not_corrected():
    bond = Bond("x", 7.0, date(2025, 4, 15), date(2035, 4, 15), ((10, 4), (10, 10)))
    assert any("do not match maturity" in f for f in bond.validate())


def test_short_position_signs():
    p = make_position("S", test_mode_instrument("T2", 7.0, 7), "short", 10e7)
    pp = pricer(p)
    s, c = krd_kr01(pp), convexity(pp)
    assert s["net_mv"] < 0 and s["net_kr01_total"] < 0
    assert c["dollar_convexity_inr"] < 0                         # short is short convexity


# ---------------- par-yield KRD basis ----------------
NS = NSCurve(8.1832, -2.4242, 0.0, 3.5185)


def test_par_bootstrap_round_trip():
    from gsec_engine.curves import ParShift
    ps = ParShift(NS, LIN)
    assert np.allclose(ps.bootstrap(ps.par0), ps.D0, atol=1e-13)
    assert np.allclose(ps.shift_bp([1.0, 7.3, 25.0], [0, 0, 0, 0]), 0.0)
    # 6M point: +1bp semi-annual par = 1/(1 + c/2) bp continuously compounded
    assert ps.shift_bp([0.5], [1, 1, 1, 1])[0] == pytest.approx(1 / (1 + ps.par0[0] / 2), abs=1e-4)


def test_par_bond_loads_only_its_own_key():
    """A par bond maturing on a key tenor has par-basis KRD only at that key."""
    from types import SimpleNamespace
    from gsec_engine.curves import ParShift
    ps = ParShift(NS, LIN)
    n = 20                                                 # 10Y on the semi-annual grid
    c = ps.par0[n - 1] * 100
    inst = SimpleNamespace(times=ps.grid[:n], amounts=np.r_[np.full(n - 1, c / 2), 100 + c / 2])
    pos = SimpleNamespace(pos_id="PAR10", instrument=inst, signed_face=100.0)
    par = krd_kr01(PortfolioPricer([pos], NS, LIN, space="par"))["krd"]
    spot = krd_kr01(PortfolioPricer([pos], NS, LIN, space="spot"))["krd"]
    assert par[["KRD_2Y", "KRD_5Y", "KRD_30Y"]].abs().values.max() < 1e-6
    assert par["KRD_10Y"].iloc[0] > 6.5
    assert spot["KRD_5Y"].iloc[0] > 0.5                     # spot basis spreads coupon risk


def test_par_krds_sum_to_par_parallel_duration():
    s = krd_kr01(PortfolioPricer([T2(), T3()], NS, LIN, space="par"))
    assert s["parallel_check_max_abs"] < 1e-5
    with pytest.raises(ValueError):
        PortfolioPricer([T2()], NS, LIN, space="forward")


def test_coupon_date_formats():
    from gsec_engine.bonds import parse_coupon_dates
    for txt in ["2-Jul/2-Jan", "2 July/2-Jan", "02 Jul / 02 Jan", "2Jul,2Jan", "Jul-2/Jan-2", "2 july and 2 january"]:
        assert parse_coupon_dates(txt) == ((2, 7), (2, 1)), txt
    assert parse_coupon_dates("11-May/11-November") == ((11, 5), (11, 11))
    for bad in ["2-Jul", "2-Jul/2-Jan/2-Mar", "31-Jun/31-Dec", "2-Jux/2-Jan", "2-Julyy/2-Jan"]:
        with pytest.raises(ValueError):
            parse_coupon_dates(bad)
