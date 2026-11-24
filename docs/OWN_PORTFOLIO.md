# Build your own portfolio

Replace the sample portfolio with your own bonds and, optionally, your own trade prices.
Everything is plain CSV in `data/`, so you can edit it in Excel or any text editor.

## 1. Positions — `data/C_portfolio.csv`

One row per position:

```csv
pos_id,name,isin,coupon,issue_date,maturity_date,coupon_dates,direction,size_type,size_inr,opened_on
P1,7.18% GS 2033,IN0020230085,7.18,2023-08-14,2033-08-14,14-Aug/14-Feb,long,face,500000000,2025-01-10
P2,7.10% GS 2034,IN0020240019,7.10,2024-04-08,2034-04-08,8-Apr/8-Oct,short,mv,250000000,2025-03-02
```

| Column | Meaning |
|---|---|
| `pos_id` | any unique label |
| `name`, `isin` | bond name and ISIN (the ISIN links the position to your prices) |
| `coupon` | annual coupon in %, e.g. `7.18` |
| `issue_date`, `maturity_date` | `YYYY-MM-DD` |
| `coupon_dates` | the two semi-annual coupon days, e.g. `14-Aug/14-Feb` (also accepts `14 August / 14 February`, `Aug-14/Feb-14`) |
| `direction` | `long` or `short` |
| `size_type` | `face` = `size_inr` is face value; `mv` = `size_inr` is market value (converted to face at today's price) |
| `size_inr` | amount in ₹ |
| `opened_on` | trade date, `YYYY-MM-DD` (informational) |

You can also add, edit and delete positions in the web app's **Portfolio** tab, or through the API
(`PUT /portfolio`, `POST /portfolio/positions`).

## 2. Your trade prices — `data/F_market_prices.csv`

Add a clean price for each bond on each date you want checked:

```csv
date,isin,clean_price,source
2026-11-20,IN0020230085,101.2350,my-desk
2026-11-20,IN0020240019,100.8800,my-desk
```

On every run the engine compares its model price with your price for that date and reports the
difference in paise per position (Risk tab → valuation and *Curve comparison*;
`curve_comparison_<date>.csv`). `source` is a free-text label.

**Using FBIL instead:** upload FBIL's daily G-Sec price/yield workbook in the **Yield builder** tab
(or `python -m gsec_engine.cli import-fbil <file>`). It fills `F_market_prices.csv` for every G-Sec
and fits the engine's own curve to those prices.

## 3. Run

```bash
python -m gsec_engine.cli run --date 2026-11-20               # value on the CCIL NS curve
python -m gsec_engine.cli run --date 2026-11-20 --curve own   # value on the curve fitted to FBIL prices
```

or start the web app and pick the date in the header. Reports go to `outputs/<date>/`.

## Keep your data separate (optional)

To leave the sample files untouched, copy `data/` to a folder of your own, edit the copies, and point
the web app/API at it:

```bash
GSEC_DATA_DIR=/path/to/my-data GSEC_OUTPUT_DIR=/path/to/my-outputs python -m uvicorn gsec_engine.api:app
```

(Windows PowerShell: `$env:GSEC_DATA_DIR="C:\my-data"` before starting.) Keep the curve files
(`A_key_rates.csv`, `B_ns_params.csv`, `D_calibration.json`, `D_hist_scenarios.csv`) in the copy — the
valuation and VaR need them. The CLI always reads `data/`.

## Checks

- Valuation date must have a curve: CCIL NS parameters on or before that date (File B), or an FBIL
  upload for `--curve own`.
- A wrong coupon schedule shows up as a flag in the run log and as a large model-vs-market gap.
