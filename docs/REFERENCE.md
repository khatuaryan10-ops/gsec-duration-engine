# G-Sec Portfolio Duration Engine

Implements *G-Sec Duration Engine — Workflow Spec v1.0* (20-Nov-2026): valuation with accrued
interest on today's Nelson-Siegel curve, KRD/KR01, convexity, PCA exposures, scenarios
(duration-only, duration + convexity, full reprice) and historical replay VaR/ES.

## Layout

| Module | Spec stage |
|---|---|
| `gsec_engine/conventions.py` | 30/360, ACT/365, business-day calendar, T+1 settlement (D) |
| `gsec_engine/bonds.py` | coupon schedule, accrued, cash flows, NS pricing, YTM (C, E, F) |
| `gsec_engine/curves.py` | NS curve, test-mode key-rate curve, linear / cubic / PCHIP bump spreading |
| `gsec_engine/store.py` | Files A–F, Stage B validation, import of the NS parameter file |
| `gsec_engine/calibration.py` | Stage A: σ, robust σ, correlation, PCA, historical scenarios |
| `gsec_engine/portfolio.py` | Stage C positions, Stage G signed values |
| `gsec_engine/risk.py` | Stage H: KRD/KR01, convexity, PC exposures, scenarios, replay |
| `gsec_engine/stage0.py` | Stage 0 time-basis test |
| `gsec_engine/engine.py` | daily run orchestration, reports, run log (I) |
| `gsec_engine/yieldbuilder.py` | own Svensson curve fitted to FBIL prices; portfolio check against FBIL |
| `gsec_engine/cli.py` | command line |
| `tests/test_spec.py` | T1–T6 + edge cases (`python -m pytest`) |

## Commands

```
python -m gsec_engine.cli import-ns <NS parameter file .csv/.xlsx>   # bulk: File B + File A
python -m gsec_engine.cli add-ns --date 2026-11-20 --b0 .. --b1 .. --b2 .. --tau .. --source CCIL
python -m gsec_engine.cli calibrate                                  # Stage A -> File D
python -m gsec_engine.cli stage0                                     # needs File F + File E
python -m gsec_engine.cli run --date 2026-11-20 [--interp cubic] [--basis 30/360]
python -m gsec_engine.cli import-fbil <FBIL G-Sec price/yield workbook>     # own curve + portfolio check
```

`.xlsx` import needs `pip install openpyxl`. Outputs go to `outputs/YYYY-MM-DD/`.

## Web front end

```
uvicorn gsec_engine.api:app          # then open http://127.0.0.1:8000/  (redirects to /ui/)
```

React 18 + htm with no build step. React, ReactDOM and htm are vendored in `frontend/vendor/`,
so it works offline and doesn't need Node. Files: `frontend/index.html`, `app.js` (pages),
`charts.js` (SVG charts), `styles.css` (light/dark tokens; `?theme=light|dark` forces a theme).

| Tab | What it shows / does |
|---|---|
| Risk | headline tiles (MV, duration, convexity, KR01, VaR/ES), KRD and KR01 charts, valuation, scenarios, historical P&L histogram + worst days, PCA exposures; "Save reports" writes the CSV/JSON set |
| Key-rate ladder | ±N bp bump for each key rate and parallel: full reprice, duration-only, duration+convexity and their errors; KRD and KRC bars; grid |
| Scenario builder | sliders/presets for 2/5/10/30Y moves, live P&L by method and by position |
| Curve | NS zero curve (with compare date), Stage B entry with validate / review / save, bulk import, recent rows |
| Portfolio | add, edit, delete positions (File C) |
| Calibration | σ, robust σ, correlation, PC loadings; recalibrate |
| Yield builder | upload FBIL's daily G-Sec file; own Svensson (and NS) curve vs CCIL NS and FBIL par; portfolio check (static data, accrued, price→yield, valuation on each curve vs FBIL price, ₹ impact); fit errors by bond and maturity bucket |

Valuation date, bump interpolation and time basis in the header apply to every tab.

## REST API

```
pip install fastapi uvicorn python-multipart httpx
uvicorn gsec_engine.api:app --reload          # interactive docs: http://127.0.0.1:8000/docs
```

`GSEC_DATA_DIR` / `GSEC_OUTPUT_DIR` override the folders; CORS allows localhost origins
(`GSEC_CORS_REGEX` to change). Errors: 404 missing data, 422 invalid input, 409 NS row needs review.

| Method & path | Purpose |
|---|---|
| `GET /health`, `GET /config`, `GET /calendar?date=` | status, defaults, business day + settlement |
| `GET /ns?start=&end=&limit=` | stored NS rows (File B) |
| `GET /ns/latest?date=` | curve used for a date, its key rates, stale flag |
| `POST /ns/validate` | Stage B dry run (errors / warnings / review) |
| `POST /ns` | store a row; 409 if it needs review → resend with `accept_review: true` |
| `POST /ns/import` | upload the NS parameter file (.csv/.xlsx) |
| `GET /curve?date=&tenors=` | NS zero rates (%, continuous) |
| `GET/PUT /portfolio`, `POST /portfolio/positions`, `PUT/DELETE /portfolio/positions/{id}` | File C |
| `GET/POST /calibration` | read / rebuild File D |
| `POST /run` | full daily run (`date`, `interpolation`, `time_basis`, `write_reports`, `include_pnl`) |
| `POST /scenarios/custom` | any key-rate move vector: duration, duration+convexity, full reprice |
| `GET /ladder?date=&max_bp=&step=` | KRD, KRC and the ±N bp key-rate bump grid |
| `POST /stage0` | time-basis test (needs Files E, F) |
| `POST /charts`, `GET /reports/{date}`, `GET /reports/{date}/{file}` | chart PNGs and report files |
| `POST /yieldbuilder/import` | upload FBIL's G-Sec price/yield workbook (.xlsx, also when named .xls): stores prices, fits and saves the curve |
| `GET /yieldbuilder?date=`, `GET /yieldbuilder/dates` | own curve for the latest FBIL file on/before the date, comparison and portfolio check |

Writes go through one lock (the stores are CSV/JSON), so run a single worker process.

## Python API (for the front end)

```python
from gsec_engine import EngineConfig, Store, run
cfg = EngineConfig(interpolation="linear", time_basis="ACT/365")
store = Store(cfg)
store.add_ns_row("2026-11-20", b0, b1, b2, tau, source="CCIL")   # raises ValueError / NeedsReview
result = run("2026-11-20", cfg, store)   # RunResult: valuation, krd, kr01, convexity, pc,
                                        # scenarios, hist (pnl, stats, worst5), run_log, flags
```

## Data status (as delivered)

- **File B / File A**: imported from `data/raw/NSS_PARAMETERS_2024_2026.csv` (02-Apr-2024 to
  18-Nov-2026, parameter columns of the source file; checked against the file's published spot
  rates to within 0.6bp), plus the 19-Nov-2026 row entered through Stage B. 21-Jan-2026
  (all-zero) is rejected. File A rates are exact NS values at 2/5/10/30Y.
- **File C**: test portfolio, 7.06% GS 2041 and 6.57% GS 2033 with 2026 issue dates. **ISINs and
  coupon dates are test values**, not the real securities.
- **holidays.csv**: 2026 only, from the supplied bank-holiday list, excluding dates on which the
  NS file shows the market was open. 2024–2025 and 2027+ are not loaded.
- **File F**: empty, needed for Stage 0 and the pricing check.

## KRD bases: spot and par

Every run reports KRD, KR01 and convexity on **two bases**:

- **Spot**: the zero (spot) rate at each key tenor is bumped, and the bump is spread across
  maturities with the chosen interpolation.
- **Par**: semi-annual **par yields** are derived from the NS discount factors on a 0.5Y grid
  out to 40Y. The par yield at each key tenor is bumped (spread across the par grid the same
  way), the discount factors are re-bootstrapped, and the resulting change in zero rates is added
  to the NS zero curve. Unbumped prices are unchanged, and the round trip is exact.
  A par bond maturing on a key tenor has par-basis KRD only at that key, so par KRDs map
  directly to hedges with benchmark bonds.

Each basis sums to its own parallel duration. The two totals differ slightly, because a parallel
par shift and a parallel spot shift are different curve moves. Reports: `krd_*.csv` / `krd_par_*.csv`,
`kr01_by_position[_par]_*.csv`, `convexity[_par]_*.csv`; `kr01_portfolio_*.csv` has both.
Scenarios, Basel shocks and historical replay stay on the spot basis.
API: `space=spot|par` on `/ladder`, `/charts` and `/scenarios/custom`; `/curve` also returns par yields.

## Implementation choices

- Ex-interest handling is implemented but **off** (`ex_interest_days=None`) until the RBI
  record-date rule is confirmed.
- Irregular first coupon: amount = coupon × 30/360 days(issue → first coupon) / 360 (flagged).
- YTM is semi-annual, with periods counted on 30/360 from settlement.
- Value-weighted portfolio YTM uses |MV| weights.
- Basel IRRBB shocks use INR sizes 400/500/300bp (configurable), evaluated at the key tenors
  and spread with the chosen interpolation, so all scenarios use the same bump machinery.
- Duration + convexity uses a second difference **along the scenario direction**, scaled so the
  largest key move is 10bp.
- The >25bp jump check is skipped (with a warning) if the previous stored day is over 7 days earlier.
- The sum-of-KRDs check tolerance is 1e-5 for linear/cubic (central-difference error), 1e-2 for PCHIP.

## Yield builder (own curve from FBIL prices)

FBIL publishes a clean price and semi-annual YTM for every G-Sec each day. The yield builder:

1. rebuilds each bond's cash flows (coupons on the maturity day-month and 6 months away; portfolio
   bonds use File C) and checks that the engine turns FBIL's price into FBIL's yield (T+1 settlement).
   Bonds failing by more than 0.5bp are excluded and listed;
2. fits a **Svensson** zero curve (continuously compounded, same convention as the NS curve) by least
   squares on duration-scaled price errors (≈ yield errors in bp). Weights: traded 3, FBIL input point 2,
   other 1. FRBs and bonds under 3 months are excluded. A 4-parameter NS fit is shown for comparison;
3. compares both with CCIL's NS curve for the same date on the same bonds, and values every position
   on each curve against FBIL's price.

Stores: `data/raw/fbil/` (uploads), `data/fbil/fbil_gsec_<date>.csv` and `fbil_par_<date>.csv` (parsed),
`data/G_fitted_curves.csv` (fitted parameters, one row per date and model). FBIL clean prices are also
written to File F, so the Risk tab's model-vs-market price check works for FBIL dates.
**Valuation curve switch.** The header's *Valuation curve* selector (API: `curve=ccil|own` on `/run`,
`/ladder`, `/charts`, `/scenarios/custom`; CLI: `run --curve own`) chooses the curve for valuation, KRD/KR01,
convexity, scenarios and historical replay: `ccil` = CCIL NS (File B, default), `own` = Svensson fitted to FBIL
(File G, latest on/before the valuation date). Both are kept: every run also prices the portfolio on the other
curve (Risk tab "Curve comparison", report `curve_comparison_<date>.csv`), next to the File F market price.
Historical replay and PCA still use the key-rate history from CCIL NS (File A) for the daily *changes*.

First result (FBIL 16-Nov-2026, 115 bonds): own Svensson RMSE 6.3bp (traded 3.9bp); own NS 9.7bp;
CCIL NS 24.7bp, with yields 41bp too low under 3Y and 10bp too high at 12-20Y. The portfolio's static
data, accrued and yield maths match FBIL to 0.01bp; on CCIL's curve the three bonds are ₹2.03 cr below
FBIL prices.

## Key rates

Key tenors are a setting (`EngineConfig.key_tenors`), default **2, 3, 5, 7, 10, 15, 30, 40Y**: one key per
liquid on-the-run benchmark (6.20% 2029, 6.53% 2031, 6.57% 2033, 6.94% 2036, 7.06% 2041, 7.63% 2056,
7.71% 2066), plus 2Y for the short end. 20Y and 50Y are left out (no traded 20Y benchmark; bumps are flat
beyond 40Y). KRD/KR01, convexity, scenarios, Basel shocks, PCA and historical replay all follow the setting;
the front end reads it from `GET /config` (`key_labels`).

Changing the tenors rebuilds File A automatically from the stored NS parameters (same dates) and the next
run recalibrates File D. The spec v1.0 tests (T1-T6) keep the original 2/5/10/30Y set (`SPEC_KEYS`).

The history is a 4-parameter NS curve, so the 8 key-rate changes have at most ~4 independent directions:
PC1-PC3 explain 99.5% and PC5-PC8 are ~0. Scenarios use PC1-PC3 only. More independent detail comes as
own Svensson fits accumulate.
