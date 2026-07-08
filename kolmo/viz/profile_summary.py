#!/usr/bin/env python3
"""Generate a lightweight HTML summary for daily profile files."""

from __future__ import annotations

import argparse
import csv
import html
from dataclasses import dataclass
from pathlib import Path

from kolmo.paths import data_path


@dataclass(frozen=True)
class DailyStat:
    date: str
    rows: int
    traded: int
    amount: float
    st_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a lightweight market summary report from profile/daily files."
    )
    parser.add_argument("--exchange", default="sz", choices=["sz", "sh"])
    parser.add_argument("--start-date", default="", help="Inclusive YYYYMMDD filter.")
    parser.add_argument("--end-date", default="", help="Inclusive YYYYMMDD filter.")
    parser.add_argument(
        "--input-dir",
        default="",
        help="Profile daily input dir. Defaults to KOLMO_DATA_ROOT/profile/daily/{exchange}.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output HTML path. Defaults to reports/profile_summary_{exchange}.html.",
    )
    return parser.parse_args()


def iter_profile_files(input_dir: Path, start_date: str, end_date: str):
    for path in sorted(input_dir.glob("*/*/*.csv")):
        date = path.stem
        if len(date) != 8 or not date.isdigit():
            continue
        if start_date and date < start_date:
            continue
        if end_date and date > end_date:
            continue
        yield date, path


def parse_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def summarize_file(date: str, path: Path) -> DailyStat:
    rows = 0
    traded = 0
    amount = 0.0
    st_count = 0
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            rows += 1
            if row.get("trade_status", "") == "1":
                traded += 1
            amount += parse_float(row.get("amount", "0"))
            if row.get("is_st", "") == "1":
                st_count += 1
    return DailyStat(date=date, rows=rows, traded=traded, amount=amount, st_count=st_count)


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
    height = 220
    pad = 28
    points = scale_points(values, width, height, pad)
    if not points:
        return "<p>No data.</p>"
    lo = min(values)
    hi = max(values)
    return f"""
<section>
  <h2>{html.escape(title)}</h2>
  <svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
    <rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff"/>
    <line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#d0d7de"/>
    <line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" stroke="#d0d7de"/>
    <polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/>
    <text x="{pad}" y="18" font-size="12" fill="#57606a">max {hi:,.2f}</text>
    <text x="{pad}" y="{height-6}" font-size="12" fill="#57606a">min {lo:,.2f}</text>
  </svg>
</section>
"""


def render_html(stats: list[DailyStat], exchange: str) -> str:
    total_rows = sum(item.rows for item in stats)
    total_amount = sum(item.amount for item in stats)
    latest = stats[-1].date if stats else ""
    rows_values = [float(item.rows) for item in stats]
    traded_values = [float(item.traded) for item in stats]
    amount_values = [item.amount / 1e8 for item in stats]
    tail = stats[-10:]

    rows = "\n".join(
        "<tr>"
        f"<td>{item.date}</td>"
        f"<td>{item.rows:,}</td>"
        f"<td>{item.traded:,}</td>"
        f"<td>{item.amount / 1e8:,.2f}</td>"
        f"<td>{item.st_count:,}</td>"
        "</tr>"
        for item in tail
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Kolmo Profile Summary - {exchange.upper()}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #24292f; }}
    h1 {{ margin-bottom: 8px; }}
    .meta {{ color: #57606a; margin-bottom: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; margin: 20px 0; }}
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
  <h1>Profile Summary - {exchange.upper()}</h1>
  <div class="meta">Daily profile cross-section report generated from local CSV files.</div>
  <div class="grid">
    <div class="metric">Trading days<b>{len(stats):,}</b></div>
    <div class="metric">Latest date<b>{html.escape(latest)}</b></div>
    <div class="metric">Rows scanned<b>{total_rows:,}</b></div>
    <div class="metric">Amount total, 100M<b>{total_amount / 1e8:,.2f}</b></div>
  </div>
  {line_chart("Rows per day", rows_values, "#0969da")}
  {line_chart("Traded symbols per day", traded_values, "#1f883d")}
  {line_chart("Amount per day, 100M", amount_values, "#bf8700")}
  <section>
    <h2>Latest 10 Days</h2>
    <table>
      <thead><tr><th>Date</th><th>Rows</th><th>Traded</th><th>Amount, 100M</th><th>ST Count</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </section>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir or data_path("profile", "daily", args.exchange))
    output = Path(args.output or Path("reports") / f"profile_summary_{args.exchange}.html")

    stats = [
        summarize_file(date, path)
        for date, path in iter_profile_files(input_dir, args.start_date, args.end_date)
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(stats, args.exchange), encoding="utf-8")
    print(f"days={len(stats)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
