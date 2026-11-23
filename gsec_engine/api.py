"""REST API over the engine (FastAPI).

  uvicorn gsec_engine.api:app --reload            # http://127.0.0.1:8000/docs
  GSEC_DATA_DIR / GSEC_OUTPUT_DIR override the data and output folders.

Errors: 404 = missing data (no curve, no calibration, unknown position), 422 = invalid input,
409 = NS row needs review (resend with accept_review=true after checking it).
"""
from __future__ import annotations

import calendar
import dataclasses
import json
import os
import re
import shutil
import threading
from datetime import date as Date, datetime
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import plots, yieldbuilder
from .config import ROOT, EngineConfig
from .conventions import settlement_date, to_date
from .curves import BumpInterpolator, ParShift, key_labels
from .engine import RunResult, run, run_calibration
from .portfolio import positions_from_file
from .risk import PortfolioPricer, evaluate_scenario, krd_kr01
from .stage0 import time_basis_test
from .bonds import parse_coupon_dates
from .store import C_COLS, NeedsReview, Store, bond_from_row

Interp = Literal["linear", "cubic", "pchip"]
Basis = Literal["ACT/365", "30/360"]
Space = Literal["spot", "par"]
CurveSrc = Literal["ccil", "own"]


# ---------------------------------------------------------------- helpers
def records(df: pd.DataFrame | None, index: str | None = None) -> list[dict]:
    """DataFrame -> JSON-safe records (NaN -> null, numpy -> python, dates -> ISO)."""
    if df is None or df.empty:
        return []
    if index:
        df = df.rename_axis(index).reset_index()
    out = json.loads(df.to_json(orient="records", date_format="iso", default_handler=str))
    for r in out:                                   # midnight timestamps -> plain dates
        for k, v in r.items():
            if isinstance(v, str) and v.endswith("T00:00:00.000"):
                r[k] = v[:10]
    return out


def jsonable(x):
    return json.loads(json.dumps(x, default=lambda o: o.item() if isinstance(o, np.generic)
                                 else o.tolist() if isinstance(o, np.ndarray) else str(o)))


def ns_row(row: pd.Series) -> dict:
    return {"date": str(row["date"].date()), "b0": float(row.b0), "b1": float(row.b1),
            "b2": float(row.b2), "tau": float(row.tau), "source": row.source,
            "entered_at": row.entered_at, "flags": row["flags"], "notes": row.notes}


# ---------------------------------------------------------------- request models
class Options(BaseModel):
    interpolation: Interp | None = None
    time_basis: Basis | None = None
    curve: CurveSrc | None = Field(None, description="valuation curve: 'ccil' (CCIL NS, default) or "
                                                     "'own' (Svensson fitted to FBIL prices)")


class NSInput(BaseModel):
    date: Date
    b0: float
    b1: float
    b2: float
    tau: float
    source: str = "manual"
    notes: str = ""
    correction: bool = False
    accept_review: bool = False


class PositionIn(BaseModel):
    pos_id: str = Field(min_length=1)
    name: str
    isin: str = ""
    coupon: float
    issue_date: Date
    maturity_date: Date
    coupon_dates: str = Field(description="two day-month pairs, e.g. '10-Apr/10-Oct'")
    direction: Literal["long", "short"]
    size_type: Literal["face", "mv"] = "face"
    size_inr: float = Field(gt=0)
    opened_on: Date | None = None

    @field_validator("pos_id")
    @classmethod
    def _pid(cls, v):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", v):
            raise ValueError("pos_id may contain letters, digits, '_', '.', '-' only")
        return v

    def row(self) -> dict:
        d = self.model_dump()
        for k in ("issue_date", "maturity_date", "opened_on"):
            d[k] = str(d[k]) if d[k] else ""
        try:                                   # store one canonical form, e.g. '2-Jul/2-Jan'
            d["coupon_dates"] = "/".join(f"{dd}-{calendar.month_abbr[mm]}"
                                         for dd, mm in parse_coupon_dates(d["coupon_dates"]))
        except ValueError:
            pass                               # reported with the position id by check_positions
        return d


class RunRequest(Options):
    date: Date = Field(default_factory=Date.today)
    write_reports: bool = True
    include_pnl: bool = False


class CustomScenario(Options):
    date: Date = Field(default_factory=Date.today)
    space: Space = Field("spot", description="'spot': moves are zero-rate moves; 'par': par-yield "
                                             "moves with the zero curve re-bootstrapped")
    moves_bp: dict[str, float] = Field(
        description="key-rate moves in bp by key label (see GET /config key_labels); missing keys "
                    "are 0, e.g. {'2Y': 25, '10Y': -10}")


# ---------------------------------------------------------------- app factory
def create_app(cfg: EngineConfig | None = None) -> FastAPI:
    base = cfg or EngineConfig()
    if os.getenv("GSEC_DATA_DIR"):
        base = dataclasses.replace(base, data_dir=Path(os.environ["GSEC_DATA_DIR"]))
    if os.getenv("GSEC_OUTPUT_DIR"):
        base = dataclasses.replace(base, output_dir=Path(os.environ["GSEC_OUTPUT_DIR"]))
    lock = threading.RLock()        # file stores are CSV/JSON: serialise writes
    KEYS = np.asarray(base.key_tenors, float)
    KEY_LABELS = key_labels(KEYS)

    app = FastAPI(title="G-Sec Duration Engine API", version="1.0",
                  description="Valuation, KRD/KR01, convexity, PCA exposures, scenarios and "
                              "historical VaR for a G-Sec portfolio on the Nelson-Siegel curve.")
    app.add_middleware(CORSMiddleware,
                       allow_origin_regex=os.getenv("GSEC_CORS_REGEX", r"https?://(localhost|127\.0\.0\.1)(:\d+)?"),
                       allow_methods=["*"], allow_headers=["*"])

    def conf(o: Options | None = None, interpolation=None, time_basis=None, curve=None) -> EngineConfig:
        c = dataclasses.replace(base)
        interpolation = interpolation or (o and o.interpolation)
        time_basis = time_basis or (o and o.time_basis)
        curve = curve or (o and o.curve)
        if curve:
            c.curve_source = curve
        if interpolation:
            c.interpolation = interpolation
        if time_basis:
            c.time_basis = time_basis
        return c

    def store(c: EngineConfig | None = None) -> Store:
        return Store(c or base)

    @app.exception_handler(LookupError)
    async def _missing(_: Request, e: LookupError):
        if isinstance(e, (KeyError, IndexError)):           # programming errors, not missing data
            return JSONResponse(status_code=500, content={"detail": f"internal error: {e!r}"})
        return JSONResponse(status_code=404, content={"detail": str(e).strip("'\"")})

    @app.exception_handler(NeedsReview)
    async def _review(_: Request, e: NeedsReview):
        return JSONResponse(status_code=409, content={
            "detail": "NS row needs review; resend with accept_review=true after checking it",
            "review": str(e).split("; ")})

    @app.exception_handler(ValueError)
    async def _invalid(_: Request, e: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(e)})

    # ------------------------------------------------------------ meta
    @app.get("/health", tags=["meta"])
    def health():
        s = store()
        b = s.load_B()
        return {"status": "ok", "data_dir": str(base.data_dir),
                "ns_rows": len(b), "latest_ns": str(b["date"].max().date()) if len(b) else None,
                "latest_curve": {"ccil": s.latest_curve_date("ccil"), "own": s.latest_curve_date("own")},
                "positions": len(s.load_C()), "calibrated": s.path_D.exists()}

    @app.get("/config", tags=["meta"])
    def config():
        return {**jsonable(base.to_dict()), "key_labels": KEY_LABELS}

    @app.get("/calendar", tags=["meta"])
    def calendar(on: Date = Query(alias="date")):
        cal = store().calendar()
        return {"date": str(on), "is_business_day": cal.is_business_day(on),
                "settlement_date": str(settlement_date(on, cal, base.settlement_lag)),
                "holidays_loaded_for_year": cal.has_year(on.year)}

    # ------------------------------------------------------------ Stage B: NS parameters
    @app.get("/ns", tags=["curve"])
    def list_ns(start: Date | None = None, end: Date | None = None, limit: int = Query(30, ge=1, le=5000)):
        b = store().load_B()
        if start:
            b = b[b["date"].dt.date >= start]
        if end:
            b = b[b["date"].dt.date <= end]
        b = b.sort_values(["date", "entered_at"]).tail(limit)
        return {"count": len(b), "rows": [ns_row(r) for _, r in b.iterrows()]}

    @app.get("/ns/latest", tags=["curve"])
    def latest_ns(on: Date = Query(default_factory=Date.today, alias="date")):
        curve, row = store().latest_ns(on)
        return {**ns_row(row), "stale": row["date"].date() != on,
                "key_rates_pct": dict(zip(KEY_LABELS, curve.zero(KEYS).round(6).tolist()))}

    @app.post("/ns/validate", tags=["curve"])
    def validate_ns(x: NSInput):
        """Dry run of Stage B: nothing is stored."""
        r = store().validate_ns(x.date, x.b0, x.b1, x.b2, x.tau, correction=x.correction)
        return {"ok": r.ok, "errors": r.errors, "warnings": r.warnings, "review": r.review,
                "key_rates_pct": {l: float(r.key_rates[k]) for l, k in zip(KEY_LABELS, store().key_cols)}
                if r.key_rates else {}}

    @app.post("/ns", status_code=201, tags=["curve"])
    def add_ns(x: NSInput):
        with lock:
            r = store().add_ns_row(x.date, x.b0, x.b1, x.b2, x.tau, x.source, notes=x.notes,
                                   correction=x.correction, accept_review=x.accept_review)
        return {"stored": str(x.date), "warnings": r.warnings, "reviewed": r.review,
                "key_rates_pct": {l: float(r.key_rates[k]) for l, k in zip(KEY_LABELS, store().key_cols)},
                "note": "calibration (File D) is not refreshed automatically; POST /calibration"}

    @app.post("/ns/import", tags=["curve"])
    def import_ns(file: UploadFile = File(...)):
        """Bulk import of the source NS parameter file (.csv or .xlsx)."""
        name = Path(file.filename or "upload.csv").name
        if Path(name).suffix.lower() not in (".csv", ".xlsx"):
            raise ValueError("upload a .csv or .xlsx file")
        dest = Path(base.data_dir) / "raw" / "uploads"
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / f"{datetime.now():%Y%m%d_%H%M%S}_{name}"
        with path.open("wb") as fh:
            shutil.copyfileobj(file.file, fh)
        with lock:
            log = store().import_ns_file(path)
        return {"saved_as": str(path), "status_counts": log["status"].value_counts().to_dict(),
                "not_stored": records(log[log["status"] != "stored"].head(200))}

    @app.get("/curve", tags=["curve"])
    def zero_curve(on: Date = Query(default_factory=Date.today, alias="date"),
                   tenors: str = "0.25,0.5,1,2,3,5,7,10,15,20,30,40"):
        """NS zero rates (%, continuous) and implied semi-annual par yields (%) on the latest curve
        on/before the date. Par yields are interpolated from the 0.5Y bootstrap grid (flat below 0.5Y)."""
        try:
            t = np.array([float(x) for x in tenors.split(",") if x.strip()])
        except ValueError:
            raise ValueError("tenors must be comma-separated numbers (years)")
        if not len(t) or (t <= 0).any() or len(t) > 200:
            raise ValueError("give 1-200 positive tenors")
        curve, row = store().latest_ns(on)
        par = ParShift(curve, BumpInterpolator("linear"), max_t=max(40.0, float(t.max()))).par_yields(t)
        return {"curve_date": str(row["date"].date()), "stale": row["date"].date() != on,
                "points": [{"tenor": float(a), "zero_pct": float(z), "par_pct": float(q)}
                           for a, z, q in zip(t, curve.zero(t), par)]}

    # ------------------------------------------------------------ File C: portfolio
    def check_positions(rows: list[dict]) -> list[str]:
        ids = [r["pos_id"] for r in rows]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"duplicate pos_id: {dup}")
        warnings = []
        for r in rows:
            try:
                bond = bond_from_row(r)
            except Exception as e:
                raise ValueError(f"{r['pos_id']}: {e}")
            if bond.issue >= bond.maturity:
                raise ValueError(f"{r['pos_id']}: issue date not before maturity")
            warnings += bond.validate()
        return warnings

    def save_rows(rows: list[dict]):
        store().save_C(pd.DataFrame(rows, columns=C_COLS))

    def load_rows() -> list[dict]:
        df = store().load_C()
        return [{k: (v.item() if isinstance(v, np.generic) else v) for k, v in r.items()}
                for r in df.to_dict("records")]

    @app.get("/portfolio", tags=["portfolio"])
    def get_portfolio():
        return {"positions": load_rows()}

    @app.put("/portfolio", tags=["portfolio"])
    def replace_portfolio(positions: list[PositionIn]):
        rows = [p.row() for p in positions]
        with lock:
            w = check_positions(rows)
            save_rows(rows)
        return {"positions": len(rows), "warnings": w}

    @app.post("/portfolio/positions", status_code=201, tags=["portfolio"])
    def add_position(p: PositionIn):
        with lock:
            rows = load_rows()
            if any(r["pos_id"] == p.pos_id for r in rows):
                raise HTTPException(409, f"pos_id {p.pos_id} exists; use PUT to replace it")
            rows.append(p.row())
            w = check_positions(rows)
            save_rows(rows)
        return {"added": p.pos_id, "warnings": w}

    @app.put("/portfolio/positions/{pos_id}", tags=["portfolio"])
    def put_position(pos_id: str, p: PositionIn):
        if p.pos_id != pos_id:
            raise ValueError("pos_id in the body must match the URL")
        with lock:
            rows = load_rows()
            idx = [i for i, r in enumerate(rows) if r["pos_id"] == pos_id]
            if idx:
                rows[idx[0]] = p.row()
            else:
                rows.append(p.row())
            w = check_positions(rows)
            save_rows(rows)
        return {"saved": pos_id, "created": not idx, "warnings": w}

    @app.delete("/portfolio/positions/{pos_id}", tags=["portfolio"])
    def delete_position(pos_id: str):
        with lock:
            rows = load_rows()
            keep = [r for r in rows if r["pos_id"] != pos_id]
            if len(keep) == len(rows):
                raise LookupError(f"no position {pos_id}")
            save_rows(keep)
        return {"deleted": pos_id, "remaining": len(keep)}

    # ------------------------------------------------------------ Stage A: calibration
    @app.get("/calibration", tags=["calibration"])
    def get_calibration():
        calib, scen = store().load_D()
        return {**jsonable(calib), "historical_days": len(scen)}

    @app.post("/calibration", tags=["calibration"])
    def recalibrate():
        with lock:
            calib = run_calibration(store(), base)
        return jsonable(calib)

    # ------------------------------------------------------------ Stages D-I: daily run
    def result_json(r: RunResult, include_pnl: bool) -> dict:
        out = {
            "valuation_date": str(r.valuation_date), "settlement_date": str(r.settlement),
            "flags": r.flags, "totals": jsonable(r.totals), "risk": jsonable(r.risk_summary),
            "valuation": records(r.valuation), "net_by_isin": records(r.net_isin),
            "krd": records(r.krd, "pos_id"), "kr01": records(r.kr01, "pos_id"),
            "kr01_portfolio": records(r.kr01_summary), "convexity": records(r.convexity, "pos_id"),
            "krd_par": records(r.krd_par, "pos_id"), "kr01_par": records(r.kr01_par, "pos_id"),
            "convexity_par": records(r.convexity_par, "pos_id"),
            "pc_exposures": records(r.pc), "scenarios": records(r.scenarios),
            "historical": {"stats": jsonable(r.hist["stats"]), "worst5": records(r.hist["worst5"])},
            "price_check": records(r.price_check), "run_log": jsonable(r.run_log),
            "curve_comparison": {**jsonable({k: v for k, v in r.curve_compare.items() if k != "rows"}),
                                 "rows": records(r.curve_compare.get("rows"))},
            "output_dir": str(r.output_dir) if r.output_dir else None,
            "key_tenors": KEYS.tolist(), "key_labels": KEY_LABELS,
        }
        if include_pnl:
            out["historical"]["pnl"] = records(r.hist["pnl"])
        return out

    @app.post("/run", tags=["risk"])
    def daily_run(req: RunRequest):
        c = conf(req)
        with lock:
            r = run(req.date, c, store(c), write=req.write_reports)
        return result_json(r, req.include_pnl)

    @app.post("/scenarios/custom", tags=["risk"])
    def custom_scenario(req: CustomScenario):
        """Any key-rate move vector: duration-only, duration + convexity and full reprice."""
        c = conf(req)
        s = store(c)
        cal = s.calendar()
        settle = settlement_date(req.date, cal, c.settlement_lag)
        curve, ns = s.curve_for(req.date, c.curve_source)
        positions = positions_from_file(s.load_C(), settle, cal, curve, c)
        if not positions:
            raise LookupError("Portfolio (File C) is empty")
        bad = sorted(set(req.moves_bp) - set(KEY_LABELS))
        if bad:
            raise ValueError(f"unknown key rate(s) {bad}; use {KEY_LABELS}")
        pp = PortfolioPricer(positions, curve, BumpInterpolator(c.interpolation, keys=KEYS), space=req.space)
        sens = krd_kr01(pp, c.kr_bump_bp)
        v = np.array([req.moves_bp.get(l, 0.0) for l in KEY_LABELS])
        res = evaluate_scenario(pp, sens["net_kr01"], sens["net_mv"], v, c.convexity_bump_bp)
        by_pos = pp.values(v) - pp.values()
        return {"curve_date": str(ns["date"]), "stale_curve": ns["date"] != req.date,
                "curve": ns["source"], "curve_label": ns["label"], "space": req.space,
                "moves_bp": dict(zip(KEY_LABELS, v.tolist())), "net_mv_inr": sens["net_mv"],
                **res, "convexity_gap_inr": res["full_reprice"] - res["duration_only"],
                "full_reprice_by_position_inr": dict(zip([p.pos_id for p in positions], by_pos.tolist()))}

    @app.get("/ladder", tags=["risk"])
    def keyrate_ladder(on: Date = Query(default_factory=Date.today, alias="date"),
                       max_bp: float = Query(50, gt=0, le=500), step: float = Query(5, gt=0),
                       interpolation: Interp | None = None, time_basis: Basis | None = None,
                       space: Space = "spot", curve: CurveSrc | None = None):
        """KRD, key-rate convexity and portfolio dMV for each key rate bumped alone over +/-max_bp.
        space='par' bumps par yields and re-bootstraps the zero curve."""
        if max_bp / step > 200:
            raise ValueError("grid too fine: max_bp/step must be at most 200")
        c = conf(interpolation=interpolation, time_basis=time_basis, curve=curve)
        L = plots.ladder(on, c, store(c), max_bp, step, space)
        sens, krc = L["sens"], L["krc"]
        return {"valuation_date": str(L["vd"]), "settlement_date": str(L["settle"]),
                "curve_date": str(L["ns"]["date"]), "curve": L["ns"]["source"], "curve_label": L["ns"]["label"],
                "interpolation": c.interpolation,
                "space": space,
                "net_mv_inr": sens["net_mv"], "portfolio_duration": sens["portfolio_duration"],
                "krd": records(sens["krd"], "pos_id"),
                "portfolio_krd": dict(zip(KEY_LABELS, sens["portfolio_krd"].tolist())),
                "net_kr01_inr": dict(zip(KEY_LABELS, sens["net_kr01"].tolist())),
                "krc": records(krc["table"], "pos_id"),
                "portfolio_krc": dict(zip(KEY_LABELS, krc["portfolio"].tolist())),
                "portfolio_parallel_convexity": krc["portfolio_parallel"],
                "grid_crore": records(L["grid"])}

    @app.post("/stage0", tags=["risk"])
    def stage0():
        r = time_basis_test(store(), base)
        return {"choice": r["choice"], "reason": r["reason"],
                "overall": records(r["overall"], "basis"), "by_bucket": records(r["by_bucket"])}

    # ------------------------------------------------------------ yield builder (own curve from FBIL)
    yb_cache: dict = {}

    def yb_result(on: Date | None) -> dict:
        s = store()
        dates = yieldbuilder.available_dates(base)
        if on is not None:
            dates = [x for x in dates if x <= str(on)]
        if not dates:
            raise LookupError("no FBIL price file stored" + (f" on or before {on}" if on else "")
                              + "; upload FBIL's G-Sec price/yield file")
        deps = [yieldbuilder.fbil_dir(base) / f"fbil_gsec_{dates[-1]}.csv", s.path_B, s.path_C,
                s.path_E, s.path_hol]
        key = (dates[-1], base.time_basis, tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in deps))
        if key not in yb_cache:
            yb_cache.clear()
            yb_cache[key] = yieldbuilder.build(s, to_date(dates[-1]))
        return yb_cache[key]

    def yb_json(r: dict, on: Date | None) -> dict:
        u = r["universe"].copy()
        u["maturity"] = u["maturity"].astype(str)
        cur = {k: (c.params if hasattr(c, "params") else None) for k, c in r["curves"].items()}
        return {"fbil_date": str(r["fbil_date"]), "settlement_date": str(r["settlement"]),
                "requested_date": str(on) if on else None,
                "stale": bool(on and str(r["fbil_date"]) != str(on)),
                "source": r["source"], "time_basis": r["time_basis"],
                "params": {"svensson": cur["svensson"], "ns": cur["ns"], "ccil": r["ccil_row"]},
                "stats": jsonable(r["stats"]), "by_bucket": jsonable(r["by_bucket"]),
                "key_rates": jsonable(r["key_rates"]), "points": records(r["points"]),
                "fbil_par": records(r["fbil_par"]), "bonds": records(u),
                "portfolio": records(r["portfolio"]), "findings": r["findings"], "flags": r["flags"],
                "dates": yieldbuilder.available_dates(base)}

    @app.get("/yieldbuilder/dates", tags=["yield builder"])
    def yb_dates():
        return {"dates": yieldbuilder.available_dates(base)}

    @app.get("/yieldbuilder", tags=["yield builder"])
    def yb_get(on: Date | None = Query(None, alias="date")):
        """Own Svensson (and NS) zero curve fitted to the FBIL prices of the date (or the latest
        file before it), compared with CCIL's NS curve, plus the portfolio check against FBIL."""
        with lock:
            r = yb_result(on)
        return yb_json(r, on)

    @app.post("/yieldbuilder/import", tags=["yield builder"])
    def yb_import(file: UploadFile = File(...)):
        """Upload FBIL's daily G-Sec price/yield workbook (.xlsx, also when named .xls). Stores the
        prices (data/fbil, File F), fits the curve and saves its parameters (File G)."""
        data = file.file.read()
        if len(data) > 20_000_000:
            raise ValueError("file too large for an FBIL price file")
        with lock:
            r = yieldbuilder.import_fbil(store(), data, file.filename or "fbil.xlsx")
            yb_cache.clear()
        out = yb_json(r, r["fbil_date"])
        out["imported"] = r["imported"]
        return out

    # ------------------------------------------------------------ reports & charts
    def report_dir(on: Date) -> Path:
        d = Path(base.output_dir) / f"{on:%Y-%m-%d}"
        if not d.is_dir():
            raise LookupError(f"no reports for {on}; POST /run first")
        return d

    @app.post("/charts", tags=["reports"])
    def charts(on: Date = Query(default_factory=Date.today, alias="date"),
               max_bp: float = Query(50, gt=0, le=500), step: float = Query(5, gt=0),
               interpolation: Interp | None = None, space: Space = "spot", curve: CurveSrc | None = None):
        if max_bp / step > 200:
            raise ValueError("grid too fine: max_bp/step must be at most 200")
        c = conf(interpolation=interpolation, curve=curve)
        with lock:
            out = plots.make_charts(on, c, max_bp, step, store(c), space)
        return {"files": sorted(f"charts/{p.name}" for p in out.iterdir())}

    @app.get("/reports/{on}", tags=["reports"])
    def list_reports(on: Date):
        d = report_dir(on)
        return {"files": sorted(str(p.relative_to(d)).replace("\\", "/")
                                for p in d.rglob("*") if p.is_file())}

    @app.get("/reports/{on}/{name:path}", tags=["reports"])
    def get_report(on: Date, name: str):
        d = report_dir(on).resolve()
        p = (d / name).resolve()
        if d not in p.parents or not p.is_file():
            raise LookupError(f"no report {name} for {on}")
        return FileResponse(p)

    # ------------------------------------------------------------ web front end
    ui = Path(os.getenv("GSEC_UI_DIR", ROOT / "frontend"))
    if ui.is_dir():
        app.mount("/ui", StaticFiles(directory=ui, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def home():
            return RedirectResponse("/ui/")

    return app


app = create_app()
