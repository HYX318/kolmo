# kolmo

China and US market data ingestion, normalization, profile generation, and
maintenance tools.

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

Current US support:

- Tiingo end-of-day stock and ETF ingestion
- atomic full and overlapping incremental updates
- offline schema/OHLC/corporate-action validation
- provider-neutral drawdown statistics
- SEC EDGAR filing history and XBRL Company Facts long tables
- point-in-time filing, availability, and retrieval timestamps

Current local terminal support:

- separated React frontend and Python market-data API
- unified local search across A-shares, US stocks, and US ETFs
- daily, five-session, weekly, and monthly OHLCV charts
- cached per-symbol gzip reads with automatic file-change invalidation

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
    US/
      sec/
        companyfacts/{CIK}/
        submissions/{CIK}/
  work/
    ashare/
    US/drawdown/latest.csv
  validation/
    ashare/
      profile_ohlc_check/
  fundamental/
    ashare/
      financial_statement/
        quarterly/
    us/
      sec/
        company_facts/{SYMBOL}.csv.gz
        filings/{SYMBOL}.csv.gz
        _meta/fetch_runs/{RUN_ID}.json
  reference/
    ashare/
      trading_calendar/
      security_master/
        observed/
  US/
    STK/1d/{SYMBOL}.csv.gz
    ETF/1d/{SYMBOL}.csv.gz
    _meta/fetch_runs/{RUN_ID}.json
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

From a fresh standalone clone, one command creates the Python virtual
environment, installs Kolmo, compiles the C++ raw scanner, builds the React
frontend, and exposes the `kolmo` command through `~/.local/bin`:

```bash
./install.sh
```

If `~/.local/bin` is not already in `PATH`, follow the single `export PATH=...`
line printed by the installer. Local configuration lives in the ignored `.env`
file copied from `.env.example`; no parent repository or parent configuration
file is used.

Start the local terminal after installation:

```bash
kolmo web
```

## US Daily Stocks and ETFs

Edit `configs/us_value_universe.csv` to maintain the tracked universe. Each
enabled row needs `symbol` and `asset_type` (`STK` or `ETF`); `provider_symbol`
handles provider aliases such as `BRK.B` -> `BRK-B`. The included rows are a
small starter universe, not an investment recommendation or a complete market
universe.

Keep the Tiingo token in Kolmo's ignored `.env` file and run the incremental
fetch:

```text
TIINGO_API_TOKEN="your_token_here"
```

```bash
python3 -m kolmo.us_market.fetch_daily
```

The first run requests all available history from `1962-01-01`. Later runs
replace a 10-calendar-day overlap and preserve older rows. If a new or corrected
cash dividend/split is detected, that symbol is automatically refreshed from
the configured start date because provider-adjusted history may have changed.
A symbol's file naturally begins at the first date Tiingo has for that listing;
the request start does not fabricate pre-listing rows. The default end date is
the local current date, and the provider returns only completed market sessions.
A manual full rebuild is also available:

```bash
python3 -m kolmo.us_market.fetch_daily --refresh
```

Fetch selected assets without editing the universe:

```bash
python3 -m kolmo.us_market.fetch_daily \
  --symbol AAPL:STK \
  --symbol SPY:ETF
```

Canonical outputs are atomic gzip CSV files:

```text
$KOLMO_DATA_ROOT/US/STK/1d/AAPL.csv.gz
$KOLMO_DATA_ROOT/US/ETF/1d/SPY.csv.gz
```

Every fetch writes run evidence under `US/_meta/fetch_runs/`. One symbol failure
does not overwrite its previous valid file and does not prevent other symbols
from completing; the command exits nonzero when any requested symbol fails.

Validate cached files without network access, then build neutral drawdown
statistics:

```bash
python3 -m kolmo.us_market.validate_daily \
  --json-output "$KOLMO_DATA_ROOT/validation/US/daily_latest.json"
python3 -m kolmo.us_market.drawdown
```

The drawdown product uses split-only adjusted closes for price drawdown and
Tiingo adjusted closes for a separate total-return drawdown. It contains no
buy/sell threshold; strategy interpretation belongs to downstream research.

## US SEC EDGAR Fundamentals

拉取 universe 中全部符合条件的美国上市公司（自动读取项目 `.env`）：

```bash
./scripts/fetch_sec_edgar.sh
```

也可以只拉指定股票：

```bash
./scripts/fetch_sec_edgar.sh --symbol AAPL --symbol MSFT
```

Kolmo uses only the SEC's official public Company Submissions and XBRL Company
Facts endpoints for this product. No API key is required, but the SEC requires
automated clients to identify themselves. Set a real application/contact value;
Kolmo deliberately has no fabricated default and fails before making a request
when the value is absent:

```bash
export SEC_USER_AGENT="Kolmo research-data your-real-address@example.com"
```

The default universe is `configs/us_value_universe.csv`. SEC ingestion processes
only rows with `enabled=1`, `asset_type=STK`, and a non-empty `cik`. ETFs and
rows without CIKs are recorded as skipped, not failed. CIKs are normalized to
ten digits. Fetch the whole eligible universe or selected companies:

```bash
python3 -m kolmo.fundamental.fetch_sec_edgar

python3 -m kolmo.fundamental.fetch_sec_edgar \
  --symbol AAPL \
  --symbol MSFT

python3 -m kolmo.fundamental.fetch_sec_edgar \
  --cik 320193 \
  --requests-per-second 5 \
  --workers 2
```

`--symbol` and `--cik` selectors must resolve to eligible rows in the selected
universe, preserving the stable CIK-to-symbol identity needed by canonical
per-symbol files. `--universe`, `--output-root`, `--user-agent`, `--timeout`,
`--workers`, `--requests-per-second`, and `--refresh` are also supported. The
global limiter defaults to five requests per second and rejects values above the
SEC limit of ten. HTTP 429, 5xx, timeout, and temporary network failures receive
bounded exponential-backoff retries.

Raw responses are stored separately from canonical tables:

```text
$KOLMO_DATA_ROOT/raw/US/sec/companyfacts/{CIK}/{RETRIEVED_AT}-main-{HASH}.json.gz
$KOLMO_DATA_ROOT/raw/US/sec/submissions/{CIK}/{RETRIEVED_AT}-{PART}-{HASH}.json.gz
```

The submissions directory includes both the current response and every history
file referenced by it. Content hashes prevent an unchanged response from
creating another snapshot. Canonical deterministic-gzip outputs and run evidence
are stored under:

```text
$KOLMO_DATA_ROOT/fundamental/us/sec/company_facts/{SYMBOL}.csv.gz
$KOLMO_DATA_ROOT/fundamental/us/sec/filings/{SYMBOL}.csv.gz
$KOLMO_DATA_ROOT/fundamental/us/sec/_meta/fetch_runs/{RUN_ID}.json
```

The filing contract retains `accession_number`, form, filing/report dates,
SEC acceptance time, primary document, XBRL flags, source, and Kolmo retrieval
time. Supported forms are 10-K, 10-Q, 8-K, 20-F, 40-F, 6-K, and their `/A`
amendments. Amendments remain separate accession records. Current and historical
submissions are merged, deduplicated by accession, and stably sorted.

Company Facts remains a long table. Each row retains `taxonomy`, `tag`, `unit`,
context dates, exact numeric text, form, fiscal year/period, frame, accession,
and these three distinct time meanings:

- `end_date`: the reporting context, not an information-availability date;
- `filed_date` / `accepted_at`: when the SEC received and exposed the filing;
- `retrieved_at`: when Kolmo observed the row.

`available_date` is the date portion of `accepted_at`, falling back to
`filed_date` only when acceptance metadata is unavailable. Downstream as-of
queries and backtests must filter on `available_date` or `accepted_at`; they must
never treat `end_date` as the date on which the market knew a fact. A stable
`row_id` prevents repeat ingestion from duplicating a fact while retaining every
accession-specific amendment and restatement version.

Validate canonical files without contacting the SEC:

```bash
python3 -m kolmo.fundamental.validate_sec_edgar
python3 -m kolmo.fundamental.validate_sec_edgar --symbol AAPL
```

XBRL tags and custom taxonomies differ across issuers. The canonical SEC layer
therefore does not guess which tag is the correct revenue or net-income measure
or build a wide table. XBRL history is generally more complete only after
SEC XBRL requirements took effect. This product provides auditable data, not an
investment conclusion. Downstream strategy projects—not Kolmo—are responsible
for financial growth/quality rules, `sector_leader`, `sector_outlook`,
`thesis_intact`, valuation, and buy/sell signals.

### Standardized US financial metrics

The next offline layer converts the SEC long table into explicitly mapped,
point-in-time versions of quarterly, annual, and TTM metrics:

```text
SEC raw JSON snapshots
  -> fundamental/us/sec/company_facts + filings       (canonical SEC semantics)
  -> fundamental/us/standardized/{quarterly,annual,ttm}/{SYMBOL}.csv.gz
  -> fundamental/us/standardized/provenance/{SYMBOL}.csv.gz
  -> fundamental/us/standardized/_meta/build_runs/{RUN_ID}.json
```

Build and validate all eligible stocks without network access:

```bash
python3 -m kolmo.fundamental.build_us_financials
python3 -m kolmo.fundamental.validate_us_financials
```

Build one company or use the installed commands:

```bash
python3 -m kolmo.fundamental.build_us_financials --symbol AAPL
kolmo-build-us-financials --symbol AAPL
kolmo-validate-us-financials --symbol AAPL
```

`kolmo.fundamental.us_concepts` is the only mapping registry. Each standard
concept has ordered exact `(taxonomy, tag, unit)` candidates; labels and
descriptions are never fuzzy-matched, and issuer custom taxonomies are not
mapped automatically. If candidates disagree in the same complete XBRL
context, the higher-priority candidate is selected and
`conflicting_candidate_tags` is emitted. Every populated field has a provenance
row containing source tags, units, SEC fact row IDs, calculation type, periods,
and accessions.

Duration classification uses start/end dates together with fiscal metadata,
form, and accession. Because SEC `fy`/`fp` often describe the current filing
rather than a historical comparison fact, standardized fiscal identities are
anchored to annual endpoints and the period's earliest disclosure; a later
comparison cannot relabel an earlier quarter. A directly disclosed quarter wins over a cumulative fact.
When compatible inputs are available at the current as-of date, Q2 and Q3 may
be derived from YTD differences and Q4 from FY less Q1/Q2/Q3. Q4 never exists
before the 10-K is public. TTM requires exactly four consecutive, non-overlapping
quarter versions. Each TTM metric independently selects the latest non-empty
version of each report period, so a later comparison row containing only one
metric cannot erase other previously disclosed metrics. Balance-sheet fields
use the latest non-empty quarter-end version and are never summed. Diluted EPS
is not mechanically summed or differenced.

Point-in-time versions are keyed by symbol, report period, period type,
availability date, and accession. A later amendment or comparison creates a new
version from its own availability date and never changes an earlier observation.
Read the latest state known on a historical date with:

```python
from kolmo.fundamental.us_financials import latest_metrics_as_of

row = latest_metrics_as_of(
    symbol="AAPL",
    as_of_date="2023-08-10",
    period_type="ttm",
    max_age_days=180,
)
```

The reader returns `None` when no version was public, when the latest report
period is older than `max_age_days`, and never returns a row whose
`available_date` is later than the requested date. For diagnostics,
`include_stale=True` returns stale data with `age_days` and `is_stale`; use
`max_age_days=None` only when the caller deliberately accepts any age.

The offline validator checks structural integrity, stable fiscal identities,
four-quarter continuity, TTM provenance and arithmetic, use of the latest
non-empty metric version, empty TTM products, stale latest periods, and missing
applicable core metrics. Availability warnings do not turn industry-inapplicable
fields into errors.

Cash-flow expenditure concepts such as capex, dividends, and repurchases are
normalized to positive outflows; therefore free cash flow is
`operating_cash_flow - capital_expenditure`. Ratios use decimal form (`0.15`
means 15%), remain empty for missing or zero denominators, and do not coerce
missing facts to zero. `completeness_score` measures only field presence and is
not an investment-quality score.

Business-model metadata comes from the universe. Banks, insurance/financial
groups, REITs, utilities, and ordinary non-financial companies are distinguished.
Fields declared inapplicable are empty and listed in `applicability_flags`.
Bank revenue can use the exact standard `RevenuesNetOfInterestExpense` tag and
utility revenue can use exact regulated/unregulated operating-revenue tags, but
this version intentionally does not invent bank-specific cash-flow metrics,
insurer underwriting metrics, REIT FFO/AFFO, or treat real-estate acquisitions
as ordinary maintenance capex. Company-specific tags, incomplete early
XBRL history, fiscal-calendar changes, short transition periods, and unresolved
candidate conflicts remain explicit limitations rather than guessed values.

> 标准化财务指标是数据产品，不代表投资判断。下游策略项目负责定义阈值、行业领先性、行业前景和投资逻辑是否成立。

## Local Market Terminal

The market terminal directly reads canonical Kolmo files and never downloads
market data while serving the UI. A normal installation builds it automatically:

```bash
./install.sh
```

Start the standalone application and open it in the default browser:

```bash
kolmo web
```

For a server-only session, such as one started from a terminal multiplexer:

```bash
kolmo web --no-browser
```

Open `http://127.0.0.1:8765`. Symbol search covers local A-shares and US assets;
the API performs daily, five-session, weekly, and monthly aggregation before
returning only the selected series. Parsed source files are cached and keyed by
file modification time and size, so a scheduled data update is visible on the
next request without restarting the terminal.

The `SEC 基本面` workspace is available for US stocks with local SEC canonical
data. It shows coverage, filing history, raw XBRL fact filters, and an exact
taxonomy/tag/unit history chart. API and table values remain canonical decimal
strings. Amendments and restatements remain separate, and the UI distinguishes
`end_date`, `accepted_at`/`available_date`, and `retrieved_at`. The chart is an
inspection aid only; it does not infer which company-specific tag represents a
normalized revenue, profit, or other strategy concept.

### Scheduled US Update on macOS

The checked-in job wrapper fetches prices, validates every configured asset,
and rebuilds drawdown statistics in one locked run. Install its LaunchAgent only
when automatic updates are wanted:

```bash
scripts/install_us_update_launchagent.sh install
scripts/install_us_update_launchagent.sh print
```

It runs daily at 10:00 system-local time, safely after the US close and Tiingo
correction window when the machine uses Asia/Shanghai. Weekend and US-holiday
runs simply repeat the overlap window and create no market rows. Logs are stored
at `$KOLMO_DATA_ROOT/logs/kolmo/update_us_daily.log`.

Remove the schedule with:

```bash
scripts/install_us_update_launchagent.sh uninstall
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

Adjustment controls the default date-partitioned profile destination. `qfq`
writes to `$KOLMO_DATA_ROOT/profile/daily/{exchange}/`, while `raw` writes to
`$KOLMO_DATA_ROOT/profile/daily_raw/{exchange}/`; the two price series never
overwrite each other. The installed 18:30 scheduler updates and health-checks
both products on every A-share trading day.

The raw-cache merge uses independent workers per symbol. Start with four
workers on a laptop; fetching from BaoStock remains serialized to avoid
rate-limit and session-safety issues.

The updater finds the latest local file under
`$KOLMO_DATA_ROOT/profile/daily/{exchange}/YYYY/MM/`, starts from that date
minus 10 calendar days, fetches through today, and overwrites the affected daily
profile files. The lookback window handles late upstream corrections and cases
where today's data is not published yet.

The scheduled updater runs the same health check after every 18:30 job and
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

On macOS, install a LaunchAgent to run the updater every day at 18:30 local
time:

```bash
scripts/install_update_launchagent.sh install
```

The scheduled wrapper checks the BaoStock A-share trading calendar first. If
today is not an exchange trading day, it logs a skip and exits successfully. On
trading days it runs:

```bash
python3 -m kolmo.scheduler.update_cn_profile_if_trading_day \
  --calendar baostock -- --target all --workers 4
```

Every scheduled run also writes an observed security-master snapshot for that
date. These snapshots accumulate the point-in-time history consumed by research
systems; a historical backtest must never select a snapshot observed after its
decision date.

The wrapper reads Kolmo's own `.env` and uses its `.venv` interpreter. Logs are
written to:

```text
$KOLMO_DATA_ROOT/logs/kolmo/update_cn_profile_daily.log
```

Check the installed job:

```bash
scripts/install_update_launchagent.sh print
```

Remove it:

```bash
scripts/install_update_launchagent.sh uninstall
```

Manual dry checks:

```bash
# Holiday-aware check through BaoStock, then run update only if the date trades.
python3 -m kolmo.scheduler.update_cn_profile_if_trading_day --date 20260729 -- --target profile

# Local weekday-only fallback for script testing; this does not know China holidays.
python3 -m kolmo.scheduler.update_cn_profile_if_trading_day --calendar weekday --date 20260729 -- --target profile
```

## Reference Products

Build a stored calendar observation for a selected date range:

```bash
python3 -m kolmo.reference.trading_calendar \
  --start-date 20170101 \
  --end-date 20261231
```

Build an observed security-master snapshot:

```bash
python3 -m kolmo.reference.security_master --as-of-date 20260816
```

The security master captures BaoStock listing/delisting information, board and
base board-limit rules. It deliberately marks ST status, IPO exception periods
and corporate-action price-limit effects as unresolved. Downstream backtests may
use it for conservative universe filtering, but must not treat it as final
execution-limit evidence.

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

### BaoStock 5-minute bars

Five-minute bars use a separate, resumable store from daily bars. Per-symbol
provider caches are written under:

```text
$KOLMO_DATA_ROOT/raw/ashare/baostock/{sz,sh}/minute/5m/{raw,qfq,hfq}/{SYMBOL}.csv.gz
```

Start with a small smoke test, then remove `--limit` for a slow full-market
backfill. Requests are split into bounded date chunks and completed symbol
caches are reused on subsequent runs:

```bash
python3 -m kolmo.ashare.fetch_baostock_5min \
  --exchange sh --start-date 20170101 --end-date 20260904 \
  --adjust raw --limit 10

python3 -m kolmo.ashare.fetch_baostock_5min \
  --exchange sh --start-date 20170101 --end-date 20260904 \
  --adjust raw --sleep 2
```

`--sleep` applies after every date-chunk request, not merely between symbols.
Use at least two seconds for a long free-provider backfill. Error `10001011`
means the current IP is blacklisted; stop all BaoStock jobs and contact the
provider instead of retrying.

After a fetch, build compressed daily cross-sections. Each output contains all
five-minute bars for one exchange and trading day:

```bash
python3 -m kolmo.ashare.partition_baostock_5min \
  --exchange sh --adjust raw \
  --start-date 20170101 --end-date 20260904
```

The partitioner uses `$KOLMO_DATA_ROOT/work/ashare/minute_5m/` for temporary
uncompressed buckets. Override it with `--work-dir` when that filesystem lacks
enough free space.

The date partitions are written to:

```text
$KOLMO_DATA_ROOT/profile/minute/5m/{raw,qfq,hfq}/{sz,sh}/YYYY/MM/YYYYMMDD.csv.gz
```

The 5-minute backfill is intentionally not part of the 18:30 daily scheduler;
run it independently until the historical archive is complete.

### Vendor Parquet minute archives

Annual ZIP files, or a year's monthly ZIP shards, can be queried as one logical
dataset without extracting the whole archive:

```bash
python3 -m kolmo.ashare.vendor_minute \
  --year 2010 --frequency 5 --symbol 600519.SH
```

Run structural, session-grid, cross-frequency, and minute-to-daily checks with:

```bash
python3 -m kolmo.validation.minute_mdcheck \
  --year 2010 --symbols 000001.SZ 600000.SH 600519.SH \
  --skip-daily-reference --output /tmp/mdcheck-2010.json
```

After building the date-partitioned Parquet history, independently recheck all
produced years in parallel:

```bash
caffeinate -i ./scripts/check_vendor_minute_daily.sh \
  --start-year 2010 --end-year 2026 --workers 4 \
  --output /tmp/minute-daily-mdcheck.json
```

This scans the produced Parquet values and metadata, validates daily symbol
session grids, and recomputes every 5/15/30/60-minute bar from 1-minute data.
See [`docs/minute_daily_mdcheck.md`](docs/minute_daily_mdcheck.md).

Before row-level checks, inventory package/month completeness. Add `--deep`
for release-gate ZIP CRC and SHA-256 verification:

```bash
python3 -m kolmo.validation.minute_inventory \
  --start-year 2010 --end-year 2026 --through-month 9 \
  --output /tmp/minute-inventory.json
```

Build a date-partitioned staging product from authoritative vendor 1-minute
bars (higher frequencies are derived from 1-minute bars). This command does not
publish or overwrite the formal profile:

```bash
./scripts/build_vendor_minute_profile.sh \
  --year 2026 --start-date 2026-08-01 --end-date 2026-08-31 \
  --workers 4
```

The default output is a unique build directory under
`$KOLMO_DATA_ROOT/staging/minute/`. See the minute product design document for
the validation and publication gates. The wrapper deliberately uses Kolmo's
`.venv` interpreter; do not run this job with Apple's Xcode Python.

Validate every Parquet file, manifest totals, and all derived frequencies:

```bash
./scripts/validate_vendor_minute_profile.sh \
  --build-root "$KOLMO_DATA_ROOT/staging/minute/<build_id>"
```

Validation is streaming by daily partition and does not load a full month or
year into memory. A successful report still does not publish the build.

After the monthly pilot passes, build and validate the complete history one
year at a time. The runner is resumable and skips years with a matching clean
validation report:

```bash
./scripts/build_vendor_minute_history.sh \
  --start-year 2010 --end-year 2026 --end-date 2026-09-04 \
  --workers 4
```

Annual outputs are staged under
`$KOLMO_DATA_ROOT/staging/minute/history-v1/year=YYYY/`. A failure stops before
the next year and never publishes partial history.
The wrapper loads `KOLMO_DATA_ROOT` from the project `.env`; normally no
`--output-root` argument is needed.
Both build stages use process workers: symbols are decoded into temporary
daily fragments in parallel, then independent date/exchange partitions are
merged and derived in parallel. Four workers is the laptop-safe default; raise
it only after observing memory and storage throughput on a complete year.
The canonical session is exchange-aware: Beijing Stock Exchange rows from
15:01 through 15:30 are retained as a separate post-close segment and are
never merged into the regular 15:00 bar.

See [`docs/minute_mdcheck.md`](docs/minute_mdcheck.md) for the data contract,
test levels, tolerances, and acceptance gates. Keep unaccepted deliveries under
`raw/vendor_candidate`; the checker does not rewrite source archives.

The proposed canonical schema, daily Parquet layout, production state machine,
release gates, and pilot plan are documented in
[`docs/minute_data_product_design.md`](docs/minute_data_product_design.md).

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

For A-shares, the chart automatically exposes `1m`, `5m`, `15m`, `30m`, and
`60m` when complete vendor ZIP archives are present under
`$KOLMO_DATA_ROOT/raw/vendor_candidate/minute_201001_202609/分钟线数据/`.
The backend reads only the requested symbol/year Parquet member, caches decoded
bars in memory, and skips partial `.qkdownloading` files. Minute prices are raw;
the UI displays volume, amount, zero-volume warnings, and MA5/10/20 overlays.

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
