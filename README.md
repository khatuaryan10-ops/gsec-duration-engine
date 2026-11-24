# G-Sec Duration Engine

Interest-rate risk engine for Indian Government Securities (G-Secs). It values a bond portfolio on the
CCIL Nelson-Siegel curve (or its own Svensson curve fitted to FBIL prices) and reports duration, key-rate
risk, convexity, scenario P&L and historical VaR/ES — from the command line, a REST API or a web app.

## Features

- **Valuation** — clean/dirty prices with accrued interest, 30/360 coupons, T+1 settlement, business-day calendar.
- **Key-rate risk** — KRD, KR01 and convexity on **spot** and **par** bases; key tenors configurable
  (default 2/3/5/7/10/15/30/40Y), with linear, cubic or PCHIP bump spreading.
- **Scenarios** — duration-only, duration + convexity and full reprice; Basel IRRBB shocks; custom curve moves.
- **Historical risk** — PCA of key-rate changes, historical-replay VaR and Expected Shortfall.
- **Yield builder** — fits a Svensson curve to FBIL's daily G-Sec prices, compares it with CCIL NS,
  and checks portfolio valuations against FBIL prices. Either curve can drive the risk run.
- **Web app** — React front end (no build step, works offline) with risk, key-rate ladder, scenario builder,
  curve, portfolio, calibration and yield-builder tabs.

## Quick start

```bash
pip install numpy scipy pandas matplotlib openpyxl fastapi uvicorn pydantic python-multipart httpx pytest
```

Run the web app (Windows: double-click `start.bat`):

```bash
python -m uvicorn gsec_engine.api:app --port 8000
```

Then open http://127.0.0.1:8000/ — interactive API docs are at `/docs`.

Or use the CLI:

```bash
python -m gsec_engine.cli import-ns data/raw/NSS_PARAMETERS_2024_2026.csv   # load NS curve history
python -m gsec_engine.cli calibrate                                          # σ, correlation, PCA
python -m gsec_engine.cli run --date 2026-11-20 [--curve own] [--interp cubic]
python -m gsec_engine.cli import-fbil <FBIL G-Sec price/yield workbook>
```

Reports (CSV/JSON/PNG) are written to `outputs/YYYY-MM-DD/`.

## Use your own portfolio

Swap in your own bonds and trade prices — two CSV files, no code changes:
**[Build your own portfolio →](docs/OWN_PORTFOLIO.md)**

## Project layout

```
gsec_engine/   core library: conventions, bonds, curves, risk, calibration, yield builder, API, CLI
frontend/      web app (React 18 + htm, vendored)
data/          input files: NS parameters, key rates, portfolio, calibration, FBIL prices
tests/         spec tests T1–T6, API and yield-builder tests
docs/          full reference: methodology, REST endpoints, data files, implementation choices
```

## Tests

```bash
python -m pytest
```

## Data notes

- Curve history: CCIL NS/NSS parameters, Apr 2024 – Nov 2026. Market prices: FBIL daily G-Sec file.
- The sample portfolio is illustrative only — replace it with your own (see above).
- The holiday calendar covers 2026 only.

## Documentation

See [docs/REFERENCE.md](docs/REFERENCE.md) for the methodology (spot vs par KRD, yield-builder fit,
valuation-curve switch), every REST endpoint, the Python API, and implementation choices.
