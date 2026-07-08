# kolmo

China market data ingestion, profile generation, and visualization tools.

`kolmo` is a standalone data project. It prepares upstream market data for
research systems, but it does not contain strategy logic, order-book replay, or
backtest engines.

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

The updater finds the latest local file under
`$KOLMO_DATA_ROOT/profile/daily/{exchange}/YYYY/MM/`, starts from that date
minus 10 calendar days, fetches through today, and overwrites the affected daily
profile files. The lookback window handles late upstream corrections and cases
where today's data is not published yet.

Use a wider repair window:

```bash
python3 -m kolmo.ashare.update_cn_profile_daily --lookback-days 30
```

Update one exchange only:

```bash
python3 -m kolmo.ashare.update_cn_profile_daily --exchange sz
python3 -m kolmo.ashare.update_cn_profile_daily --exchange sh
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
