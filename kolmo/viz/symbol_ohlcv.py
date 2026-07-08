#!/usr/bin/env python3
"""Generate a lightweight symbol OHLCV HTML report from daily profile files."""

from __future__ import annotations

import argparse
import csv
import html
from dataclasses import dataclass
from pathlib import Path

from kolmo.paths import data_path


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a symbol OHLCV report from profile/daily files."
    )
    parser.add_argument("--symbol", required=True, help="Symbol, e.g. 000001.SZ.")
    parser.add_argument("--start-date", default="", help="Inclusive YYYYMMDD filter.")
    parser.add_argument("--end-date", default="", help="Inclusive YYYYMMDD filter.")
    parser.add_argument(
        "--input-root",
        default="",
        help="Profile daily root. Defaults to KOLMO_DATA_ROOT/profile/daily.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output HTML path. Defaults to reports/symbol_{symbol}.html.",
    )
    return parser.parse_args()


def parse_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def exchange_from_symbol(symbol: str) -> str:
    upper = symbol.upper()
    if upper.endswith(".SZ"):
        return "sz"
    if upper.endswith(".SH"):
        return "sh"
    raise ValueError("symbol must end with .SZ or .SH")


def iter_files(root: Path, exchange: str, start_date: str, end_date: str):
    for path in sorted((root / exchange).glob("*/*/*.csv")):
        date = path.stem
        if len(date) != 8 or not date.isdigit():
            continue
        if start_date and date < start_date:
            continue
        if end_date and date > end_date:
            continue
        yield date, path


def load_symbol_bars(root: Path, symbol: str, start_date: str, end_date: str) -> list[Bar]:
    exchange = exchange_from_symbol(symbol)
    target = symbol.upper()
    bars: list[Bar] = []
    for date, path in iter_files(root, exchange, start_date, end_date):
        with path.open("r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                if row.get("symbol", "").upper() != target:
                    continue
                bars.append(
                    Bar(
                        date=date,
                        open=parse_float(row.get("open", "")),
                        high=parse_float(row.get("high", "")),
                        low=parse_float(row.get("low", "")),
                        close=parse_float(row.get("close", "")),
                        volume=parse_float(row.get("volume", "")),
                        amount=parse_float(row.get("amount", "")),
                    )
                )
                break
    return bars


def scale_points(values: list[float], width: int, height: int, pad: int) -> str:
    if not values:
        return ""
    lo = min(values)
    hi = max(values)
    span = hi - lo if hi > lo else 1.0
    step = (width - 2 * pad) / max(1, len(values) - 1)
    points = []
    for idx, value in enumerate(values):
        x = pad + idx * step
        y = height - pad - ((value - lo) / span) * (height - 2 * pad)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def line_chart(title: str, values: list[float], color: str) -> str:
    width = 960
    height = 240
    pad = 30
    points = scale_points(values, width, height, pad)
    if not points:
        return "<p>No data.</p>"
    return f"""
<section>
  <h2>{html.escape(title)}</h2>
  <svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
    <rect width="{width}" height="{height}" fill="#ffffff"/>
    <line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#d0d7de"/>
    <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" stroke="#d0d7de"/>
    <polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/>
    <text x="{pad}" y="18" font-size="12" fill="#57606a">max {max(values):,.2f}</text>
    <text x="{pad}" y="{height-6}" font-size="12" fill="#57606a">min {min(values):,.2f}</text>
  </svg>
</section>
"""


def render_html(symbol: str, bars: list[Bar]) -> str:
    closes = [bar.close for bar in bars]
    amounts = [bar.amount / 1e8 for bar in bars]
    latest = bars[-1] if bars else None
    rows = "\n".join(
        "<tr>"
        f"<td>{bar.date}</td>"
        f"<td>{bar.open:,.4f}</td>"
        f"<td>{bar.high:,.4f}</td>"
        f"<td>{bar.low:,.4f}</td>"
        f"<td>{bar.close:,.4f}</td>"
        f"<td>{bar.volume:,.0f}</td>"
        f"<td>{bar.amount / 1e8:,.2f}</td>"
        "</tr>"
        for bar in bars[-20:]
    )
    latest_text = latest.date if latest else ""
    close_text = f"{latest.close:,.4f}" if latest else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Kolmo Symbol OHLCV - {html.escape(symbol)}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #24292f; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(150px, 1fr)); gap: 12px; margin: 20px 0; }}
    .metric {{ border: 1px solid #d0d7de; border-radius: 6px; padding: 12px; }}
    .metric b {{ display: block; font-size: 20px; margin-top: 4px; }}
    section {{ margin-top: 28px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
    th, td {{ border-bottom: 1px solid #d0d7de; padding: 8px; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #f6f8fa; }}
  </style>
</head>
<body>
  <h1>{html.escape(symbol.upper())}</h1>
  <div class="grid">
    <div class="metric">Bars<b>{len(bars):,}</b></div>
    <div class="metric">Latest date<b>{html.escape(latest_text)}</b></div>
    <div class="metric">Latest close<b>{html.escape(close_text)}</b></div>
  </div>
  {line_chart("Close", closes, "#0969da")}
  {line_chart("Amount, 100M", amounts, "#bf8700")}
  <section>
    <h2>Latest 20 Bars</h2>
    <table>
      <thead><tr><th>Date</th><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>Volume</th><th>Amount, 100M</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </section>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    symbol = args.symbol.upper()
    root = Path(args.input_root or data_path("profile", "daily"))
    output = Path(args.output or Path("reports") / f"symbol_{symbol.replace('.', '_')}.html")
    bars = load_symbol_bars(root, symbol, args.start_date, args.end_date)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(symbol, bars), encoding="utf-8")
    print(f"bars={len(bars)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
