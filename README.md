# Micro-Sector Dashboard

Equal-weight micro-sector indices for NSE stocks, compared with the NIFTY 500, published as a
static site. A GitHub Actions job refreshes the data after every trading day.

**Pilot scope:** Pharma & Healthcare, Capital Goods and Chemicals (23 micro sectors, 177 stocks).

## Run locally

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pipeline.run_all                # fetch → validate → build → export
cd site && python3 -m http.server 8000              # open http://localhost:8000
```

Individual steps: `python -m pipeline.fetch_prices [--full]`, `pipeline.validate`,
`pipeline.build_indices`, `pipeline.export_json`.

## Layout

| Path | What it is |
|---|---|
| `taxonomy/micro_sectors.csv` | symbol, name, broad sector, micro sector. A stock may appear in several micro sectors. |
| `taxonomy/corporate_actions.csv` | Manual price fixes (unadjusted splits, demergers, bad prints) |
| `taxonomy/reviewed_moves.csv` | >20% daily moves checked and confirmed genuine |
| `pipeline/` | Fetch, validate, build and export scripts |
| `data/prices.parquet` | 6 years of adjusted daily closes and volumes (committed; the only state the pipeline keeps) |
| `data/validation_report.json` | Latest data-quality report |
| `site/` | The static dashboard; `site/data/` is generated |

## Data sources

1. **yfinance** (`SYMBOL.NS`, `^CRSLDX` for NIFTY 500). This is the primary source. Prices are adjusted for splits, bonuses and dividends.
   Yahoo rewrites adjusted history when a stock goes ex-date, so each update compares the overlapping
   days and fully re-downloads any symbol whose history changed.
2. **NSE bhavcopy** cross-checks the latest closes and is the fallback if Yahoo misses a session.
   NSE sometimes blocks non-Indian IPs such as GitHub's runners. When that happens the check is skipped with a warning.
3. **Broker API** is the last-resort fallback. It is a stub in `pipeline/sources.py:fetch_broker_closes` and needs your
   credentials and SDK code before it does anything.

## Index methodology

- Equal weight, rebalanced at the close of the first trading day of Jan/Apr/Jul/Oct. Weights drift between rebalances.
- To be eligible, a stock needs a 20-day median traded value of at least ₹1 crore, measured up to the prior session. New listings
  join at the next rebalance.
- A micro sector needs 3 or more eligible stocks to get an index. The index starts at 100 on the first rebalance where that holds.
- A stock with no price on a day is left out of that day's average.
- The dashboard rebases every chart to 100 at the start of the selected timeframe.

## Daily review

`validate.py` flags single-day moves over 20%, symbols with missing or stale data, and bhavcopy mismatches.
It fails the run (so nothing is published) only if the benchmark is missing or more than 25% of
symbols have no price for the latest date. For each flagged move:

- **Genuine** (circuit, results, news): add it to `taxonomy/reviewed_moves.csv`.
- **Data error**: add a row to `taxonomy/corporate_actions.csv`. Use `adjust` with a factor for splits and demergers,
  `drop_before` for relistings, or `drop_day` for bad prints.

## Publishing on GitHub

1. Create a repository and push this directory to `main`.
2. In **Settings → Pages**, set *Source* to **GitHub Actions**.
3. The workflow `.github/workflows/nightly.yml` runs Mon–Fri at 19:30 IST. You can also run it from
   the Actions tab or trigger it by pushing changes to `site/`, `taxonomy/` or `pipeline/`. On NSE holidays nothing changes, so
   nothing is committed or deployed.

## Adding sectors

Add rows to `taxonomy/micro_sectors.csv` (use the NSE symbol, and check it exists on Yahoo as `SYMBOL.NS`)
and run the pipeline. New symbols are backfilled automatically. Review any new big-move flags before publishing.
