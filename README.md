# kolmo

China market data ingestion, normalization, profile generation, and maintenance
tools.

`kolmo` is the standalone data layer for the research stack. It prepares
upstream market data for downstream systems such as `cn_daily_lab`, but it does
not contain strategy logic, portfolio accounting, order-book replay, broker
integration, or live-trading code.

Architecture entry point:

```text
docs/architecture.md
```

## Scope

Current:

```text
BaoStock / AKShare / company profile files
  -> normalized daily bar/profile tables
  -> company-style daily cross-section files
```

Planned:

- coverage and data-quality reports
- OHLCV and turnover visualizations
- market breadth dashboards
- symbol-level inspection pages
- exchange OHLC validation data products
- point-in-time financial statement validation

## Project Layout

```text
kolmo/
  pyproject.toml
  requirements.txt
  configs/
    ashare_daily_profile.json
  kolmo/
    ashare/
      build_cn_profile_daily.py
      update_cn_profile_daily.py
      fetch_baostock_daily.py
      fetch_akshare_daily.py
      partition_daily_bars_by_date.py
      normalize_daily_profile.py
    viz/
      README.md
  scripts/
    build_cn_profile_daily.sh
    update_cn_profile_daily.sh
  data/
    raw/
    clean/
```

Local project `data/raw/` and `data/clean/` are ignored by git, but the normal
data root is outside the repo.

## Data Root

All default raw and clean paths are derived from one canonical data root:

```bash
export KOLMO_DATA_ROOT=/Users/galoishuang/dat/all
```

If `KOLMO_DATA_ROOT` is not set, Kolmo uses:

```text
/Users/galoishuang/dat/all
```

Recommended layout:

```text
$KOLMO_DATA_ROOT/
  profile/
    daily/
      sz/YYYY/MM/YYYYMMDD.csv
      sh/YYYY/MM/YYYYMMDD.csv
  raw/
    ashare/
      baostock/
      akshare/
  work/
    ashare/
  validation/
    ashare/
      profile_ohlc_check/
  fundamental/
    ashare/
      financial_statement/
        quarterly/
```

Daily profile rows include a coarse `board` classification for A-share universe
filters:

```text
main       Shanghai/Shenzhen main board
sme        Shenzhen 002 legacy SME board
chi_next   Shenzhen 300/301 ChiNext
star       Shanghai 688 STAR Market
b_share    B-share code ranges
beijing    Beijing Stock Exchange, when present
other      recognized exchange, unknown prefix
```

Existing profile files can be upgraded in place:

```bash
python3 -m kolmo.ashare.backfill_profile_board
```

When moving to an external drive, only update the environment variable:

```bash
export KOLMO_DATA_ROOT=/Volumes/KOLMO_DATA/kolmo
```

## Install

```bash
python3 -m pip install -r requirements.txt
```

Optional editable install:

```bash
python3 -m pip install -e .
```

## Full Build

Fetch real China A-share daily data from 2017 to today and store company-style
daily profile files:

```bash
python3 -m kolmo.ashare.build_cn_profile_daily
```

Default output:

```text
$KOLMO_DATA_ROOT/profile/daily/sz/YYYY/MM/YYYYMMDD.csv
$KOLMO_DATA_ROOT/profile/daily/sh/YYYY/MM/YYYYMMDD.csv
```

Each `YYYYMMDD.csv` is a daily cross-section. For example,
`$KOLMO_DATA_ROOT/profile/daily/sz/2017/01/20170103.csv` contains all fetched
SZSE stocks that traded on 2017-01-03.

Build one exchange only:

```bash
python3 -m kolmo.ashare.build_cn_profile_daily --exchange sz
python3 -m kolmo.ashare.build_cn_profile_daily --exchange sh
```

Shell wrappers are also available:

```bash
scripts/build_cn_profile_daily.sh
scripts/build_cn_profile_daily.sh --exchange sz
```

## Incremental Update

After the first full build:

```bash
python3 -m kolmo.ashare.update_cn_profile_daily
```

Check whether the latest profile files are complete enough for research:

```bash
python3 -m kolmo.ashare.profile_health_check \
  --days 20 \
  --json-output $KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.json \
  --csv-output $KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.csv
```

The health check validates:

- latest available profile date
- both `sz` and `sh` daily files exist
- required profile columns
- row count versus recent exchange median
- duplicate symbols
- non-positive OHLC values
- bound BaoStock fetch-run completion and failure evidence, where provenance is available

By default this updates both outputs: date-partitioned profile files and the
canonical per-symbol gzip raw caches. Select one output when needed:

```bash
# Only rebuild profile/daily/{exchange}/YYYY/MM/YYYYMMDD.csv.
python3 -m kolmo.ashare.update_cn_profile_daily --target profile

# Only merge the latest existing per-symbol raw cache window; this does not
# fetch again after a profile update.
python3 -m kolmo.ashare.update_cn_profile_daily --target raw --exchange sz --workers 4

# Fetch a fresh window, then merge only per-symbol raw caches.
python3 -m kolmo.ashare.update_cn_profile_daily --target raw --refresh-raw --exchange sz --workers 4

# Explicitly update both outputs (the default).
python3 -m kolmo.ashare.update_cn_profile_daily --target all --workers 4
```

The raw-cache merge uses independent workers per symbol. Start with four
workers on a laptop; fetching from BaoStock remains serialized to avoid
rate-limit and session-safety issues.

The updater finds the latest local file under
`$KOLMO_DATA_ROOT/profile/daily/{exchange}/YYYY/MM/`, starts from that date
minus 10 calendar days, fetches through today, and overwrites the affected daily
profile files. The lookback window handles late upstream corrections and cases
where today's data is not published yet.

The scheduled updater runs the same health check after every 17:00 job and
writes:

```text
$KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.json
$KOLMO_DATA_ROOT/logs/kolmo/profile_health_latest.csv
```

If either the update command or the health check fails, the LaunchAgent exits
non-zero. This is intentional: a file with the latest date is not enough if one
exchange is partially populated.

### Publish an immutable profile snapshot

After the update and health check succeed, publish the latest common SZ/SH date
set for downstream research:

```bash
python3 -m kolmo.catalog.profile_snapshot publish --latest-days 20
```

The command validates the exact profile schema, non-empty CSVs, unique symbols,
row date/exchange/symbol consistency, and finite market prices. `preclose` must
always be positive; tradable rows must have positive OHLC, while suspended rows
may retain zero OHLC under the ProfileStore contract. Newly rebuilt partitions
also carry `<date>.csv.provenance.json`, which binds the partition hash to a
run-scoped BaoStock fetch evidence file. Publication requires the bound run to
be complete with zero failed symbols. Legacy partitions without provenance are
explicitly marked `RESEARCH_ONLY`; old `failures_*_daily_*` files remain audit
records and are not interpreted as date-level proof. `CURRENT` is updated only
after every selected partition passes its blocking checks.

Published metadata and immutable content objects are stored at:

```text
$KOLMO_DATA_ROOT/catalog/snapshots/profile_daily/<snapshot_id>/manifest.json
$KOLMO_DATA_ROOT/catalog/snapshots/profile_daily/CURRENT
$KOLMO_DATA_ROOT/catalog/objects/profile_daily/<sha256>.csv
```

The snapshot ID is derived from canonical data-identity content; a separate
`manifest_sha256` also protects release metadata such as `created_at`. A partition record
contains both its mutable `source_path` for lineage and immutable `object_path`
for reads and verification. CSV data is not copied: publication hard-links each
source partition into staging and atomically moves that link into the
content-addressed object directory. Consequently the data root and catalog must
be on the same filesystem.

The current profile product contract accepts only a uniform `qfq` adjustment.
The manifest records `adjustments: ["qfq"]`, `research_only: true`, and a
`RESEARCH_ONLY/adjusted_price_research_only` quality result. Mixed adjustments,
raw prices, and `hfq` inputs are blocked until separately versioned product
contracts are published. A published manifest may contain `PASS` and
`RESEARCH_ONLY` results, but never `BLOCK` or `FAIL`.

Profile writers must replace completed source files atomically with
`os.replace`; they must never truncate or rewrite an already published source
inode in place. Atomic source replacement gives the source a new inode while
old hard-linked snapshot objects remain unchanged and verifiable.

Published object inodes are made read-only. Because each object and its source
partition are hard links to the same inode, normal writers cannot truncate the
source in place and must continue using temporary files plus `os.replace`.
This prevents accidental mutation, not malicious tampering by the same system
user; signed manifests or read-only object storage remain future controls.

Use the catalog from scheduled shell jobs:

```bash
# Print the immutable ID selected by CURRENT.
python3 -m kolmo.catalog.profile_snapshot current

# Verify CURRENT, including each object hash, row count, and CSV contract.
python3 -m kolmo.catalog.profile_snapshot verify

# Verify an explicitly pinned snapshot.
python3 -m kolmo.catalog.profile_snapshot verify <snapshot_id>

# Publish an explicit date set when repairing or reproducing a release.
python3 -m kolmo.catalog.profile_snapshot publish \
  --dates 20260720 20260721 20260722
```

历史研究窗口使用 `--no-promote`，只生成固定 snapshot ID，不改变定时任务维护的
`CURRENT`：

```bash
python3 -m kolmo.catalog.profile_snapshot publish --no-promote \
  --dates 20230103 20230104 20230105
```

The intended scheduled sequence is `update -> health check -> snapshot
publish -> snapshot verify`. Failed publication leaves the previous `CURRENT`
unchanged. Existing manifests are immutable; publishing the same content is
idempotent, while corrected source partitions produce a new snapshot ID and
retain the old snapshot. An interrupted fetch leaves a `running` run-evidence
record and does not atomically replace its failure manifest, so it cannot be
mistaken for a zero-failure update.

`scripts/update_cn_profile_daily_scheduled.sh` 已接入该顺序：交易日更新和最近 20 日
健康检查均通过后，自动发布最近 80 个完整交易日，满足 MA60 特征预热。任一步失败都会
返回非零状态，且不会
切换 `CURRENT`。

调度锁记录 PID 和启动时间。发现仍存活的同一任务时返回 `75`，发现陈旧锁时自动清理并
重试；日志使用 JSON Lines 记录 started/skipped/finished/failed 事件。

Use a wider repair window:

```bash
python3 -m kolmo.ashare.update_cn_profile_daily --lookback-days 30
```

## Optional Data Products

### Exchange OHLC Check

When exchange/reference OHLC files are available under:

```text
$KOLMO_DATA_ROOT/raw/ashare/exchange_ohlc/daily/{sz,sh}/YYYY/MM/YYYYMMDD.csv
```

run:

```bash
python3 -m kolmo.validation.profile_ohlc_check \
  --start-date 2024-01-02 \
  --end-date 2024-01-31 \
  --output $KOLMO_DATA_ROOT/validation/ashare/profile_ohlc_check/202401.csv
```

The checker compares profile open/high/low/close against the reference file and
writes row-level mismatches. The fetcher for the exchange source can be added
later without changing this validation contract.

### Financial Statements

Normalized point-in-time financial statement files should live under:

```text
$KOLMO_DATA_ROOT/fundamental/ashare/financial_statement/quarterly/{symbol}.csv
```

Every row must include `announce_date`. Strategies may use a report only after
that announcement date, not merely because the `report_period` has ended.
BaoStock exposes some cash-flow and balance-sheet fields as ratios, so Kolmo
keeps `operating_cash_flow_ratio` beside `operating_cash_flow`. BaoStock
percentage fields are stored both as `*_raw_percent` and as decimal normalized
fields; conversion is always `raw / 100`, independent of value magnitude.

Incremental fetches merge with existing history by
`source_code/source_year/source_quarter/source_stat_date/source_pub_date` and
atomically replace the symbol file. Profit, balance, cash-flow, and optional
operation rows must share `code/statDate/pubDate`; mismatched report versions
are written to the failure manifest instead of being combined.

Validate the current files:

```bash
python3 -m kolmo.fundamental.validate_financials
```

Fetch a small financial sample from BaoStock:

```bash
python3 -m kolmo.fundamental.fetch_baostock_financials \
  --symbol 000001.SZ \
  --start-year 2023 \
  --end-year 2024
```

The fetcher is resumable by default. Existing files that already contain the
requested periods are skipped:

```bash
python3 -m kolmo.fundamental.fetch_baostock_financials \
  --exchange sz \
  --start-year 2023 \
  --end-year 2026 \
  --resume
```

Fetch one year/quarter incrementally:

```bash
python3 -m kolmo.fundamental.fetch_baostock_financials \
  --exchange all \
  --start-year 2026 \
  --end-year 2026 \
  --quarter 1
```

Fetch a custom batch:

```bash
python3 -m kolmo.fundamental.fetch_baostock_financials \
  --symbols-file /tmp/symbols.txt \
  --start-year 2023 \
  --end-year 2026 \
  --failures-output /tmp/financial_failures.csv
```

Fetch the full A-share quarterly financial set only after the smaller runs look
healthy:

```bash
python3 -m kolmo.fundamental.fetch_baostock_financials \
  --exchange all \
  --start-year 2017 \
  --end-year 2026
```

If a run is interrupted, rerun the same command. Completed symbols are skipped.
Interrupted empty or incomplete files are not considered complete and will be
refetched.

Update one exchange only:

```bash
python3 -m kolmo.ashare.update_cn_profile_daily --exchange sz
python3 -m kolmo.ashare.update_cn_profile_daily --exchange sh
```

## Scheduled Update

On macOS, install a LaunchAgent to run the updater every day at 17:00 local
time:

```bash
kolmo/scripts/install_update_launchagent.sh install
```

The scheduled wrapper checks the BaoStock A-share trading calendar first. If
today is not an exchange trading day, it logs a skip and exits successfully. On
trading days it runs:

```bash
python3 -m kolmo.scheduler.update_cn_profile_if_trading_day \
  --calendar baostock -- --target all --workers 4
```

The wrapper reads the repository-level `.quant-lab.env` file when present, so
`KOLMO_DATA_ROOT` can stay in one place. Logs are written to:

```text
$KOLMO_DATA_ROOT/logs/kolmo/update_cn_profile_daily.log
```

Check the installed job:

```bash
kolmo/scripts/install_update_launchagent.sh print
```

Remove it:

```bash
kolmo/scripts/install_update_launchagent.sh uninstall
```

Manual dry checks:

```bash
# Holiday-aware check through BaoStock, then run update only if the date trades.
python3 -m kolmo.scheduler.update_cn_profile_if_trading_day --date 20260729 -- --target profile

# Local weekday-only fallback for script testing; this does not know China holidays.
python3 -m kolmo.scheduler.update_cn_profile_if_trading_day --calendar weekday --date 20260729 -- --target profile
```

## Data Sources

Primary source:

```bash
python3 -m kolmo.ashare.fetch_baostock_daily \
  --exchange sz \
  --start-date 20170101 \
  --end-date 20260708 \
  --adjust qfq
```

BaoStock raw daily caches also retain historical valuation fields:

```text
peTTM, pbMRQ, psTTM, pcfNcfTTM
```

Existing OHLCV-only caches are detected as stale and refreshed automatically
when fetched again with `--resume`. To refresh one stock without rebuilding
profile files:

```bash
python3 -m kolmo.ashare.fetch_baostock_daily \
  --exchange sz --symbol 000858.SZ \
  --start-date 20170101 --end-date 20260722 \
  --adjust qfq --no-combine
```

AKShare support is currently kept as a secondary source:

```bash
python3 -m kolmo.ashare.fetch_akshare_daily \
  --start-date 20170101 \
  --end-date 20260708 \
  --adjust qfq
```

## Company Profile Input

If you already have company-style daily txt/csv files, normalize them with:

```bash
python3 -m kolmo.ashare.normalize_daily_profile \
  --input-dir data/raw/ashare/profile_daily \
  --output data/clean/ashare/sz_daily_bars.csv \
  --config configs/ashare_daily_profile.json \
  --exchange SZ \
  --start-date 20170101 \
  --end-date 20260708
```

## Design Rules

- Keep source-specific parsing in `kolmo.ashare`.
- Keep generated market data under `data/`.
- Keep visualization/report code under `kolmo.viz`.
- Do not add strategy, backtest, or order-book logic here.

## Visualization

For the interactive local terminal (K-line plus daily market cross-section):

```bash
python3 -m kolmo.viz.market_terminal
```

Then open `http://127.0.0.1:8765`. It loads exactly one symbol history or one
daily cross-section on demand from `$KOLMO_DATA_ROOT`; it never copies raw data
into the repository.

Generate a lightweight market coverage and turnover report from local daily
profile files:

```bash
python3 -m kolmo.viz.profile_summary --exchange sz
```

Default output:

```text
reports/profile_summary_sz.html
```

Generate a symbol OHLCV report without loading the full dataset into memory:

```bash
python3 -m kolmo.viz.symbol_ohlcv --symbol 000001.SZ
```

Default output:

```text
reports/symbol_000001_SZ.html
```

The visualization tools stream daily CSV files and emit static HTML with inline
SVG charts, so they avoid browser dashboards and large in-memory DataFrames.
Reports are generated under the project directory by default; market data stays
under `KOLMO_DATA_ROOT`.

## Raw Cache Compression

BaoStock raw cache files are useful for replaying the profile build without
pulling the full history again, but they are not on the hot research path.
Compress them with gzip:

```bash
python3 -m kolmo.ashare.compress_raw_cache --source baostock --exchange sz
```

Future BaoStock downloads write gzip raw cache by default:

```text
$KOLMO_DATA_ROOT/raw/ashare/baostock/sz/daily/qfq/000001.SZ.csv.gz
```

The profile builder can resume from either `.csv.gz` or legacy `.csv` cache
files.

## C++ Tools

High-throughput read-only tools live under `tools/`. Build the raw cache scanner:

```bash
cmake -S tools/raw_scan -B build/raw_scan
cmake --build build/raw_scan
```

Scan raw BaoStock cache:

```bash
./build/raw_scan/kolmo_raw_scan \
  /Users/galoishuang/dat/all/raw/ashare/baostock/sz/daily/qfq
```
