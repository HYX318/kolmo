# Kolmo Architecture

Kolmo is the market-data preparation layer. It owns ingestion, normalization,
data contracts, and local maintenance jobs. It must not contain strategy logic,
portfolio accounting, or trading rules.

US end-of-day products follow the same boundary. Kolmo may calculate neutral
price statistics such as drawdown, but threshold-based entry/exit decisions and
fundamental investment judgments belong to downstream research systems.

## Boundaries

```text
upstream providers
  -> raw caches + completed fetch-run evidence
  -> normalized per-symbol files
  -> profile/daily/{exchange}/YYYY/MM/YYYYMMDD.csv + provenance sidecar
  -> catalog/objects/profile_daily/{sha256}.csv
  -> catalog/snapshots/profile_daily/{snapshot_id}/manifest.json
  -> downstream research systems
```

Allowed:

- provider adapters such as BaoStock, AKShare, and Tiingo
- raw-cache maintenance
- daily profile generation and repair
- schema enrichment such as `board`
- data-quality and visualization reports
- scheduled updates
- versioned reference products such as calendars and security-master observations
- read-only local APIs and visual inspection tools over canonical products

Not allowed:

- alpha signals
- portfolio construction
- strategy backtests
- broker, OMS, or live-trading code

## Local Market Terminal

The terminal preserves a strict frontend/backend boundary:

```text
web/frontend (React + lightweight-charts)
  -> GET /api/v1/instruments
  -> GET /api/v1/bars
kolmo.web (read-only Python API)
  -> US/{STK,ETF}/1d/*.csv.gz
  -> raw/ashare/baostock/{sz,sh}/daily/qfq/*.csv.gz
```

The backend owns symbol resolution, source-schema normalization, adjusted/raw
price selection, calendar aggregation, range limiting, and JSON compression.
The frontend owns interaction and rendering only. Source files are cached by
path, modification time, and size; an updated canonical file creates a new
cache key and requires no service restart.

## Data Contracts

Daily A-share profile files are the primary downstream contract:

```text
$KOLMO_DATA_ROOT/profile/daily/{sz,sh}/YYYY/MM/YYYYMMDD.csv
```

Required columns:

```text
date
symbol
exchange
board
open
high
low
close
preclose
volume
volume_unit
amount
turnover_rate
pct_change
pe_ttm
pb_mrq
ps_ttm
pcf_ncf_ttm
trade_status
is_st
adjust
source
```

Index daily files are stored separately:

```text
$KOLMO_DATA_ROOT/raw/ashare/baostock/index/daily/{SYMBOL}.csv
```

Required index columns:

```text
date
symbol
open
high
low
close
preclose
volume
amount
pct_change
source
```

US stock and ETF daily bars are canonical per-symbol gzip files:

```text
$KOLMO_DATA_ROOT/US/STK/1d/{SYMBOL}.csv.gz
$KOLMO_DATA_ROOT/US/ETF/1d/{SYMBOL}.csv.gz
```

They retain raw and provider-adjusted OHLCV, cash dividends, split factors, and
source identity. Fetch-run evidence is stored under
`$KOLMO_DATA_ROOT/US/_meta/fetch_runs/`. Generic latest drawdown statistics are
derived into `$KOLMO_DATA_ROOT/work/US/drawdown/latest.csv`; source bars are
never modified by the statistics job.

Reference products are versioned separately from daily profiles:

```text
$KOLMO_DATA_ROOT/reference/ashare/trading_calendar/YYYY.csv
$KOLMO_DATA_ROOT/reference/ashare/security_master/observed/YYYY/MM/YYYYMMDD.csv
$KOLMO_DATA_ROOT/reference/ashare/industry/as_of/YYYY/MM/YYYYMMDD.csv
```

The current calendar is a BaoStock observation. The security master is an
observed BaoStock basic-information snapshot with listing/delisting dates and a
base board limit rule. It explicitly does not claim date-effective ST status,
IPO exception windows, corporate-action adjustments, or a production price-limit
decision. Those fields remain required before live-trading or production-grade
execution claims.

## Board Classification

`board` is a market-board classification from code prefixes. It is not an
industry taxonomy and not index membership.

```text
main
sme
chi_next
star
b_share
beijing
other
```

Strategy systems may use this for broad universe filters, but industry or
constituent-aware strategies need explicit membership data added separately.
BaoStock industry classifications are stored as a separate date-effective
product, including both the requested classification date and retrieval time;
they are never inferred from board or security-name prefixes.

## Operations

Kolmo is installed and operated from its own repository root. `./install.sh`
creates the local runtime and frontend build; `kolmo web` serves the read-only
terminal. Runtime configuration is read only from Kolmo's ignored `.env` file.

Incremental update:

```bash
python3 -m kolmo.ashare.update_cn_profile_daily --target all --workers 4
```

Scheduled macOS update:

```bash
scripts/install_update_launchagent.sh install
```

Backfill board field for existing data:

```bash
python3 -m kolmo.ashare.backfill_profile_board
```

Maintain and validate US daily bars:

```bash
python3 -m kolmo.us_market.fetch_daily
python3 -m kolmo.us_market.validate_daily
python3 -m kolmo.us_market.drawdown
```

## Quality Gates

Before downstream research trusts new data, Kolmo should be able to report:

- latest date by exchange
- rows per exchange and board
- required-column presence
- duplicate symbols per date
- missing/zero OHLC values
- ST and suspended counts
- provider failures

`profile_health_check` and `profile_snapshot` implement these baseline gates.
Provider cross-checks against exchange-published OHLC, expected-universe row
counts, and richer anomaly history remain future work.

Each newly written profile partition is bound to a BaoStock fetch-run evidence
file. The evidence records its requested date range, completion status, failed
symbol count, and hash of its run-scoped failure manifest. Snapshot publication
accepts a partition as `PASS` only when that binding points to a completed
zero-failure run and both hashes still match. A missing provenance sidecar on
legacy data is `RESEARCH_ONLY`, not a reason to infer success or failure from
an old date-named failure CSV. Interrupted fetches leave `status=running` and
cannot become publishable evidence.

Snapshot manifests currently mark the absence of independent exchange-calendar
evidence and legacy partitions without fetch provenance as `RESEARCH_ONLY`.
A future calendar and date-effective security-master product must be published
and hashed before downstream systems can claim complete sessions or complete
historical universes.
