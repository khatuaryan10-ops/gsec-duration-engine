"""API tests on a temporary copy of the data folder (the real stores are never written)."""
import dataclasses
import shutil

import pytest
from fastapi.testclient import TestClient

from gsec_engine.api import create_app
from gsec_engine.config import ROOT, EngineConfig

VD = "2026-11-19"
TEST_PORTFOLIO = """pos_id,name,isin,coupon,issue_date,maturity_date,coupon_dates,direction,size_type,size_inr,opened_on
P1,7.06% GS 2041,PLACEHOLDER-A,7.06,2026-04-10,2041-04-10,10-Apr/10-Oct,long,face,740000000000,
P2,6.57% GS 2033,PLACEHOLDER-B,6.57,2026-06-05,2033-06-05,05-Jun/05-Dec,long,face,460000000000,
"""


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("api")
    data = tmp / "data"
    shutil.copytree(ROOT / "data", data, ignore=shutil.ignore_patterns("raw"))
    (data / "C_portfolio.csv").write_text(TEST_PORTFOLIO)     # fixed, independent of the live file
    cfg = dataclasses.replace(EngineConfig(), data_dir=data, output_dir=tmp / "outputs")
    with TestClient(create_app(cfg)) as c:
        yield c


def test_health_and_config(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["positions"] == 2 and h["latest_ns"] == VD
    assert client.get("/config").json()["interpolation"] == "linear"


def test_calendar(client):
    r = client.get("/calendar", params={"date": "2026-11-23"}).json()
    assert r["is_business_day"] and r["settlement_date"] == "2026-11-25"   # 24-Nov holiday


def test_ns_latest_and_curve(client):
    r = client.get("/ns/latest", params={"date": VD}).json()
    assert r["b0"] == pytest.approx(8.1832) and not r["stale"]
    c = client.get("/curve", params={"date": VD, "tenors": "2,10"}).json()
    assert c["points"][0]["zero_pct"] == pytest.approx(r["key_rates_pct"]["2Y"])
    assert client.get("/ns/latest", params={"date": "2020-01-01"}).status_code == 404
    assert client.get("/curve", params={"date": VD, "tenors": "x"}).status_code == 422


def test_ns_validate_and_add(client):
    body = {"date": "2026-11-20", "b0": 8.19, "b1": -2.43, "b2": 0.0, "tau": 3.52, "source": "test"}
    v = client.post("/ns/validate", json=body).json()
    assert v["ok"] and not v["review"]
    bad = client.post("/ns/validate", json={**body, "tau": 50}).json()
    assert not bad["ok"]
    # a large jump needs review (409), then is accepted with accept_review
    jump = {**body, "b0": 8.6}
    r = client.post("/ns", json=jump)
    assert r.status_code == 409 and r.json()["review"]
    assert client.post("/ns", json={**jump, "accept_review": True}).status_code == 201
    assert client.post("/ns", json=body).status_code == 422           # already stored
    assert client.get("/ns/latest", params={"date": "2026-11-20"}).json()["b0"] == 8.6


def test_run(client):
    r = client.post("/run", json={"date": VD, "write_reports": True})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["totals"]["net_mv_inr"] / 1e7 == pytest.approx(117310.68, abs=0.05)
    assert j["risk"]["portfolio_duration"] == pytest.approx(7.5023, abs=1e-4)
    assert len(j["valuation"]) == 2 and len(j["scenarios"]) > 20
    assert "pnl" not in j["historical"]
    files = client.get(f"/reports/{VD}").json()["files"]
    assert any(f.startswith("valuation_") for f in files)
    assert client.get(f"/reports/{VD}/{files[0]}").status_code == 200
    assert client.get(f"/reports/{VD}/../../data/C_portfolio.csv").status_code == 404


def test_custom_scenario_matches_ladder(client):
    s = client.post("/scenarios/custom", json={"date": VD, "moves_bp": {"10Y": 50}}).json()
    L = client.get("/ladder", params={"date": VD, "max_bp": 50, "step": 10}).json()
    row = [g for g in L["grid_crore"] if g["key_rate"] == "10Y" and g["shift_bp"] == 50][0]
    assert s["full_reprice"] / 1e7 == pytest.approx(row["full_reprice_cr"])
    assert sum(s["full_reprice_by_position_inr"].values()) == pytest.approx(s["full_reprice"])
    assert sum(L["portfolio_krd"].values()) == pytest.approx(L["portfolio_duration"], abs=1e-5)


def test_portfolio_crud(client):
    pos = {"pos_id": "P3", "name": "7.10% GS 2034", "coupon": 7.10, "issue_date": "2026-01-15",
           "maturity_date": "2034-01-15", "coupon_dates": "15-Jan/15-Jul", "direction": "short",
           "size_inr": 1e10}
    assert client.post("/portfolio/positions", json=pos).status_code == 201
    assert client.post("/portfolio/positions", json=pos).status_code == 409
    bad = {**pos, "pos_id": "P4", "maturity_date": "2025-01-15"}
    assert client.post("/portfolio/positions", json=bad).status_code == 422
    assert client.post("/portfolio/positions", json={**pos, "direction": "flat"}).status_code == 422
    ids = [p["pos_id"] for p in client.get("/portfolio").json()["positions"]]
    assert ids == ["P1", "P2", "P3"]
    r = client.post("/run", json={"date": VD, "write_reports": False}).json()
    assert r["totals"]["short_mv_inr"] < 0
    assert client.delete("/portfolio/positions/P3").status_code == 200
    assert client.delete("/portfolio/positions/P3").status_code == 404


def test_calibration(client):
    c = client.get("/calibration").json()
    assert c["n_changes"] > 500 and len(c["sigma_bp"]) == 4


def test_ui_served(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/"
    page = client.get("/ui/")
    assert page.status_code == 200 and "app.js" in page.text
    assert client.get("/ui/vendor/react.production.min.js").status_code == 200


def test_par_basis(client):
    j = client.post("/run", json={"date": VD, "write_reports": False}).json()
    assert len(j["krd_par"]) == len(j["krd"])
    assert "portfolio_duration_par" in j["risk"] and "key_par_yields_today_pct" in j["run_log"]
    assert j["kr01_portfolio"][0]["portfolio_KRD_par"] is not None
    spot = client.get("/ladder", params={"date": VD, "max_bp": 20, "step": 10}).json()
    par = client.get("/ladder", params={"date": VD, "max_bp": 20, "step": 10, "space": "par"}).json()
    assert par["space"] == "par" and par["portfolio_krd"] != spot["portfolio_krd"]
    s = client.post("/scenarios/custom", json={"date": VD, "space": "par", "moves_bp": {"10Y": 10}}).json()
    assert s["space"] == "par" and s["full_reprice"] < 0
    assert client.get("/ladder", params={"date": VD, "space": "fwd"}).status_code == 422


def test_coupon_dates_messages(client):
    pos = {"pos_id": "P9", "name": "8.30% GS 2040", "coupon": 8.30, "issue_date": "2010-07-02",
           "maturity_date": "2040-07-02", "coupon_dates": "2 July/2-Jan", "direction": "long", "size_inr": 1e9}
    assert client.post("/portfolio/positions", json=pos).status_code == 201
    row = [p for p in client.get("/portfolio").json()["positions"] if p["pos_id"] == "P9"][0]
    assert row["coupon_dates"] == "2-Jul/2-Jan"
    r = client.post("/portfolio/positions", json={**pos, "pos_id": "P10", "coupon_dates": "2 July"})
    assert r.status_code == 422 and "two day-month pairs" in r.json()["detail"]
    client.delete("/portfolio/positions/P9")
