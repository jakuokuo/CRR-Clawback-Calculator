# CRR Clawback Calculator

A Dash app that calculates how much of CAISO's post-market CRR **offset revenue** (clawbacks) belongs to each cleared CRR. It does the same thing as the `CRR Summary` sheet in `clawbacks_allocated.xlsx`.

## How to use it

1. **CRR Results:** upload one or more auction results files (for example `TEAX CRR Results.csv`). The app uses these to find the Source → Sink path, portfolio and TOU for each CRR ID. Only rows with a CRR ID (awarded bids) are used. If you hold annual CRRs, upload the annual results file as well. Annual CRR IDs that aren't in any results file still get their clawbacks calculated, but they show as *(not in CRR results)*.
2. **Revenue Adjustments Details:** upload one or more CAISO CMRI *Post-Market CRR Revenue Adjustments Details* reports (`.ods`, `.xlsx`, `.xls` or `.csv`). If the same report or overlapping date ranges get uploaded twice, the duplicate rows are dropped.
3. On the page:
   - KPIs, charts, the **CRR Summary** table with a Total row, and a reconciliation check
   - a **CRR ID lookup** that matches the lookup box at the top of the workbook
   - tables of binding constraints and row-level allocations
   - **Excel** (Summary, Constraints and Allocation Detail sheets) and **CSV** downloads

The filters (portfolio, CRR ID, product type, dates) only change what's shown. Offsets are always allocated across **all** uploaded CRRs first, so filtering never changes anyone's MW share.

## How the allocation works

| Step | Formula |
|---|---|
| Negative-only offset | `Neg Offset = MIN(Offset Revenue, 0)`: offsets can only lose money |
| Constraint-hour | group of rows with the same `Start Date × Product Type × Transmission Constraint ID × Constraint Case` |
| Constraint-hour MW | `Σ CRR Award MW` of every CRR in the group |
| MW share | `CRR Award MW ÷ Constraint-Hour MW` |
| Allocated offset | `Neg Offset × MW share` |
| Net revenue | `Notional + Allocated Offset + CRR Clawback + Circular Scheduling` |
| Clawback % | `|Allocated Offset| ÷ Notional Revenue` |
| Naive offset pull | `Σ Neg Offset`, for reference only. It double counts constraints that several CRRs share. |

**Check:** the allocated total should equal the sum of negative offsets counted once per constraint-hour, and the app shows this under the summary table.

## Repo layout

```
main_app.py        Dash app (layout + callbacks). Entrypoint: main_app:app
clawback_calc.py   Parsing + allocation math (no Dash code)
tests/             pytest unit tests for the math
requirements.txt
manifest.json      Posit Connect deployment manifest
```

## Run locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (source .venv/bin/activate on Mac/Linux)
pip install -r requirements.txt
python main_app.py              # http://127.0.0.1:8050
python -m pytest tests          # optional (pip install pytest)
```

## Deploy to Posit Connect (RConnect)

**Git-backed (preferred):** push this repo to GitHub, then in Connect go to **Publish → Import from Git**, pick the repo and branch, and Connect reads `manifest.json`.

**Push from the CLI:**

```bash
pip install rsconnect-python
rsconnect deploy dash --entrypoint main_app:app \
    --server https://connect.teainc.org --api-key <YOUR_API_KEY> \
    --title "CRR Clawback Calculator" .
```

When you change dependencies or add files, regenerate the manifest:

```bash
rsconnect write-manifest dash --entrypoint main_app:app --overwrite .
```

Don't commit API keys. Pass them on the command line or store them with `rsconnect add`.
