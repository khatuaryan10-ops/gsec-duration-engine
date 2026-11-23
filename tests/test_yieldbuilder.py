"""Yield builder: FBIL parsing, curve fit and portfolio check, on a synthetic FBIL workbook
priced off a known Svensson curve (no dependency on a real FBIL download)."""
import dataclasses
import io
import shutil
from datetime import date

import numpy as np
import pytest

openpyxl = pytest.importorskip("openpyxl")

from gsec_engine import yieldbuilder as yb
from gsec_engine.bonds import instrument_from_bond, ytm_from_dirty
from gsec_engine.config import ROOT, EngineConfig
from gsec_engine.conventions import BusinessCalendar, settlement_date
from gsec_engine.store import Store

FD = date(2026, 11, 16)
TRUE = yb.SvenssonCurve(8.0, -2.2, -1.5, 1.2, 1.1, 9.0)
MATS = [date(2027 + k // 2, 3 + 6 * (k % 2), 15) for k in range(0, 58, 2)] + [date(2066, 5, 18)]
P3 = dict(isin="IN0020260025", coupon=6.94, maturity=date(2036, 5, 11))   # in the live portfolio


def synthetic_book(cal):
    settle = settlement_date(FD, cal, 1)
    rows = []
    for i, m in enumerate(MATS + [P3["maturity"]]):
        isin = P3["isin"] if m == P3["maturity"] else f"IN00TEST{i:04d}"
        cpn = P3["coupon"] if isin == P3["isin"] else 6.5 + 0.05 * i
        inst = instrument_from_bond(yb.infer_bond(isin, cpn, m, settle), settle, cal, "ACT/365")
        dirty = yb.model_dirty(TRUE, inst)
        clean = round(dirty - inst.accrued, 4)
        rows.append((isin, cpn, m, clean, round(ytm_from_dirty(inst, clean + inst.accrued), 4),
                     "Input Point" if i % 3 == 0 else None, None, "T" if i % 4 == 0 else None))
    rows.append(("IN00TESTFRB1", 6.45, date(2028, 10, 4), 100.46, 6.66, "FRB", "GOI FRB 2028", "T"))
    return rows


def workbook_bytes(rows, fd=FD) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "G-Sec"
    ws.append([None, "Financial Benchmarks India Private Ltd "])
    ws.append([])
    ws.append(["FBIL GSec Prices/Yields", None, fd.strftime("%d-%b-%Y")])
    ws.append([])
    ws.append(["ISIN", "Coupon", "Maturity(dd-mmm-yyyy)", "Price(Rs)", "YTM% p.a. (Semi-Annual)",
               "Remark 1", "Remark 2", None])
    for r in rows:
        ws.append(list(r))
    ps = wb.create_sheet("Par Yield")
    ps.append(["Tenor (Year)", "YTM% p.a.(Semi-Annual)", "YTM % p.a.(Annualized)"])
    for t in (0.5, 1, 2, 5, 10, 30):
        ps.append([t, 7.0, 7.12])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def tmpstore(tmp_path):
    data = tmp_path / "data"
    shutil.copytree(ROOT / "data", data, ignore=shutil.ignore_patterns("raw", "fbil", "G_fitted_curves.csv"))
    (data / "F_market_prices.csv").write_text("date,isin,clean_price,source\n")
    cfg = dataclasses.replace(EngineConfig(), data_dir=data, output_dir=tmp_path / "out")
    return Store(cfg)


def test_read_fbil_layout_and_xls_name():
    cal = BusinessCalendar()
    p = yb.read_fbil(workbook_bytes(synthetic_book(cal)))
    assert p["date"] == FD
    assert len(p["bonds"]) == len(MATS) + 2
    assert p["bonds"].iloc[-1]["remark1"] == "FRB"
    assert list(p["par"]["tenor"]) == [0.5, 1, 2, 5, 10, 30]


def test_read_fbil_rejects_bad_files():
    with pytest.raises(ValueError, match="old binary .xls"):
        yb.read_fbil(b"\xd0\xcf\x11\xe0" + b"\0" * 100)
    with pytest.raises(ValueError, match="not an Excel workbook"):
        yb.read_fbil(b"date,isin\n")


def test_svensson_reduces_to_ns():
    from gsec_engine.curves import NSCurve
    t = np.linspace(0.1, 40, 50)
    assert np.allclose(yb.ns_as_svensson(8.2, -2.4, 0.3, 3.4).zero(t), NSCurve(8.2, -2.4, 0.3, 3.4).zero(t))


def test_fit_recovers_known_curve(tmpstore):
    cal = tmpstore.calendar()
    res = yb.import_fbil(tmpstore, workbook_bytes(synthetic_book(cal)), "16112026.xls")
    u = res["universe"]
    assert not u.loc[u["kind"] == "FRB", "used"].any()
    assert (u.loc[u["used"], "ytm_check_bp"].abs() < yb.YTM_CHECK_BP).all()
    st = res["stats"]["svensson"]
    assert st["rmse_bp"] < 0.1 and st["max_abs_bp"] < 0.3          # prices rounded to 4dp only
    sv = res["curves"]["svensson"]
    for k in (2.0, 5.0, 10.0, 30.0):
        assert abs(sv.zero(k) - TRUE.zero(k)) * 100 < 0.5
    # stored: parsed prices, File F rows, fitted parameters
    assert yb.available_dates(tmpstore.cfg) == ["2026-11-16"]
    F = tmpstore.load_F()
    assert len(F) == len(MATS) + 1 and set(F["source"]) == {"FBIL"}
    g = yb.g_path(tmpstore.cfg).read_text()
    assert "2026-11-16,ns," in g and "2026-11-16,svensson," in g
    # re-import replaces, does not duplicate
    yb.import_fbil(tmpstore, workbook_bytes(synthetic_book(cal)), "again.xlsx")
    assert len(tmpstore.load_F()) == len(F)
    assert yb.g_path(tmpstore.cfg).read_text().count("2026-11-16,svensson,") == 1


def test_portfolio_check_findings(tmpstore):
    cal = tmpstore.calendar()
    res = yb.import_fbil(tmpstore, workbook_bytes(synthetic_book(cal)), "f.xlsx")
    pc = res["portfolio"].set_index("pos_id")
    assert pc.loc["P3", "found"] and abs(pc.loc["P3", "ytm_check_bp"]) < 0.01
    assert abs(pc.loc["P3", "svensson_diff_bp"]) < 0.5               # synthetic prices lie on the curve
    texts = " ".join(f["text"] for f in res["findings"])
    assert "P1" in texts and "not in the FBIL file" in texts           # P1/P2 are not in the synthetic file


def test_portfolio_check_spots_wrong_isin(tmpstore):
    c = tmpstore.load_C()
    c.loc[c["pos_id"] == "P3", "isin"] = "IN0099999999"
    tmpstore.save_C(c)
    res = yb.import_fbil(tmpstore, workbook_bytes(synthetic_book(tmpstore.calendar())), "f.xlsx")
    bad = [f for f in res["findings"] if f["pos_id"] == "P3"]
    assert bad and bad[0]["severity"] == "error" and "IN0020260025" in bad[0]["text"]


def test_api_upload_and_get(tmpstore):
    from fastapi.testclient import TestClient
    from gsec_engine.api import create_app
    blob = workbook_bytes(synthetic_book(tmpstore.calendar()))
    with TestClient(create_app(tmpstore.cfg)) as c:
        assert c.get("/yieldbuilder").status_code == 404
        r = c.post("/yieldbuilder/import", files={"file": ("16112026.xls", blob, "application/vnd.ms-excel")})
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["imported"]["date"] == "2026-11-16" and j["fbil_date"] == "2026-11-16"
        g = c.get("/yieldbuilder", params={"date": "2026-11-23"}).json()
        assert g["stale"] and g["fbil_date"] == "2026-11-16"
        assert {"svensson", "ns"} <= set(g["params"]) and len(g["points"]) == 200
        assert c.get("/yieldbuilder/dates").json()["dates"] == ["2026-11-16"]
        assert c.post("/yieldbuilder/import", files={"file": ("x.csv", b"a,b\n", "text/csv")}).status_code == 422


# ---------------------------------------------------------------- valuation curve switch
def test_curve_for_both_sources(tmpstore):
    with pytest.raises(LookupError, match="Yield builder"):
        tmpstore.curve_for(FD, "own")
    yb.import_fbil(tmpstore, workbook_bytes(synthetic_book(tmpstore.calendar())), "f.xlsx")
    c_own, i_own = tmpstore.curve_for(date(2026, 11, 23), "own")
    assert i_own["date"] == FD and i_own["model"] == "Svensson"
    assert abs(c_own.zero(10.0) - TRUE.zero(10.0)) * 100 < 0.5
    _, i_ccil = tmpstore.curve_for(date(2026, 11, 23), "ccil")
    assert i_ccil["model"] == "Nelson-Siegel" and set(i_ccil["params"]) == {"b0", "b1", "b2", "tau"}
    with pytest.raises(ValueError):
        tmpstore.curve_for(FD, "bloomberg")


def test_run_on_own_curve_keeps_ccil_comparison(tmpstore):
    from gsec_engine.engine import run
    yb.import_fbil(tmpstore, workbook_bytes(synthetic_book(tmpstore.calendar())), "f.xlsx")
    own = run(FD, dataclasses.replace(tmpstore.cfg, curve_source="own"), tmpstore, write=False)
    ccil = run(FD, tmpstore.cfg, tmpstore, write=False)
    assert own.run_log["curve"]["source"] == "own" and ccil.run_log["curve"]["source"] == "ccil"
    assert not any("STALE" in f for f in own.flags)                 # own curve dated FD
    co, cc = own.curve_compare, ccil.curve_compare
    assert co["available"] and cc["available"] and co["active"] == "own" and cc["active"] == "ccil"
    # same two curves either way; the active one equals the run's own valuation
    assert np.allclose(co["rows"]["own_clean"], own.valuation["clean_price"])
    assert np.allclose(cc["rows"]["ccil_clean"], ccil.valuation["clean_price"])
    assert np.allclose(co["rows"]["ccil_clean"], cc["rows"]["ccil_clean"])
    p3 = co["rows"].set_index("pos_id").loc["P3"]
    assert abs(p3["own_vs_market_paise"]) < 1                       # synthetic FBIL price lies on the curve
    assert abs(own.totals["net_mv_inr"] - co["totals"]["own_mv_inr"]) < 1


def test_api_curve_param(tmpstore):
    from fastapi.testclient import TestClient
    from gsec_engine.api import create_app
    with TestClient(create_app(tmpstore.cfg)) as c:
        r = c.post("/run", json={"date": "2026-11-16", "curve": "own", "write_reports": False})
        assert r.status_code == 404 and "Yield builder" in r.json()["detail"]
        assert c.get("/health").json()["latest_curve"]["own"] is None
        c.post("/yieldbuilder/import", files={"file": ("f.xlsx", workbook_bytes(synthetic_book(tmpstore.calendar())))})
        assert c.get("/health").json()["latest_curve"]["own"] == "2026-11-16"
        j = c.post("/run", json={"date": "2026-11-16", "curve": "own", "write_reports": False}).json()
        assert j["run_log"]["curve"]["source"] == "own" and j["curve_comparison"]["available"]
        assert len(j["curve_comparison"]["rows"]) == 3
        lad = c.get("/ladder", params={"date": "2026-11-16", "curve": "own", "max_bp": 10, "step": 5}).json()
        assert lad["curve"] == "own" and lad["curve_date"] == "2026-11-16"
        sc = c.post("/scenarios/custom", json={"date": "2026-11-23", "curve": "own", "moves_bp": {"10Y": 10}}).json()
        assert sc["curve"] == "own" and sc["stale_curve"] and sc["full_reprice"] < 0
        assert c.post("/run", json={"date": "2026-11-16", "curve": "x"}).status_code == 422
