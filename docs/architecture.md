# Kolmo Architecture

Kolmo is the market-data preparation layer. It owns ingestion, normalization,
data contracts, and local maintenance jobs. It must not contain strategy logic,
portfolio accounting, or trading rules.

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

- provider adapters such as BaoStock and AKShare
- raw-cache maintenance
- daily profile generation and repair
- schema enrichment such as `board`
- data-quality and visualization reports
- scheduled updates

Not allowed:

- alpha signals
- portfolio construction
- strategy backtests
- broker, OMS, or live-trading code

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

## Operations

Incremental update:

```bash
python3 -m kolmo.ashare.update_cn_profile_daily --target all --workers 4
```

Scheduled macOS update:

```bash
kolmo/scripts/install_update_launchagent.sh install
```

Backfill board field for existing data:

```bash
python3 -m kolmo.ashare.backfill_profile_board
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
