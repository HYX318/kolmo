# Visualization

Planned visualization scope:

- daily profile coverage and missing-date heatmaps
- per-symbol OHLCV inspection
- market breadth and turnover dashboards
- data quality reports for failed or suspicious rows

## Local market terminal

The terminal is now a separated React frontend and Python read-only API. It
indexes local A-share and US per-symbol caches, then loads only the selected
instrument into the browser.

The standalone installer builds the frontend and installs the CLI:

```bash
./install.sh
```

Start the complete localhost-only application and open the default browser:

```bash
kolmo web
```

Use `kolmo web --no-browser` when only the local server should start.

Open `http://127.0.0.1:8765`. The UI supports:

- A-share and US symbol/company search
- daily, five-session, weekly, and monthly OHLCV aggregation
- one-, three-, five-year and complete-history ranges
- raw/adjusted US prices and BaoStock qfq A-share prices
- candlestick, volume, crosshair, zoom, and pan interactions
- SEC filing-history browsing for US stocks with local canonical data
- raw XBRL fact filtering by taxonomy, text, unit, and filing form
- exact taxonomy/tag/unit history charts with amendments retained

The SEC view is a data inspection surface, not a financial-statement mapper.
It displays report-period and availability dates separately and does not decide
which company-specific XBRL tag represents revenue, earnings, or another
strategy concept.

The API caches parsed gzip files by path, modification time, and size. Updated
files are picked up automatically without restarting the service. JSON payloads
are gzip-compressed when supported by the browser. The service binds only to
`127.0.0.1` by default and makes no outbound market-data requests. Stop it with
`Ctrl-C`.
