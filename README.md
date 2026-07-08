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

`data/raw/` and `data/clean/` are ignored by git.

## Data Root

All default raw and clean paths are derived from one canonical data root:

```bash
export KOLMO_DATA_ROOT=/Users/galoishuang/MarketData/kolmo
```

If `KOLMO_DATA_ROOT` is not set, Kolmo uses the local project directory:

```text
data/
```

Recommended layout:

```text
$KOLMO_DATA_ROOT/
  raw/
    ashare/
      baostock/
      akshare/
  clean/
    ashare/
      profile_daily/
        sz/YYYY/MM/YYYYMMDD.csv
        sh/YYYY/MM/YYYYMMDD.csv
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
$KOLMO_DATA_ROOT/clean/ashare/profile_daily/sz/YYYY/MM/YYYYMMDD.csv
$KOLMO_DATA_ROOT/clean/ashare/profile_daily/sh/YYYY/MM/YYYYMMDD.csv
```

Each `YYYYMMDD.csv` is a daily cross-section. For example,
`$KOLMO_DATA_ROOT/clean/ashare/profile_daily/sz/2017/01/20170103.csv` contains all fetched
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
`$KOLMO_DATA_ROOT/clean/ashare/profile_daily/{exchange}/YYYY/MM/`, starts from that date
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
