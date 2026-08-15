# Visualization

Planned visualization scope:

- daily profile coverage and missing-date heatmaps
- per-symbol OHLCV inspection
- market breadth and turnover dashboards
- data quality reports for failed or suspicious rows

## Local market terminal

Run a localhost-only interactive terminal that reads the canonical external data
root on demand:

```bash
python3 -m kolmo.viz.market_terminal
```

Open `http://127.0.0.1:8765` in a browser. It has two views:

- **个股 K 线**: reads one symbol's full BaoStock qfq raw cache, with OHLC,
  volume, amount, and any available PE/PB fields.
- **全市场**: reads one `profile/daily/{exchange}/YYYY/MM/YYYYMMDD.csv` file
  at a time, so switching dates does not load the entire A-share history into
  the browser.

The server binds only to `127.0.0.1` by default and makes no outbound network
requests. Stop it with `Ctrl-C`.
