#!/usr/bin/env python3
"""Fetch real A-share daily bars from BaoStock."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import signal
import socket
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar

from kolmo.ashare.board_rules import board_for_symbol
from kolmo.paths import data_path


FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "tradestatus",
    "pctChg",
    # 日频估值由 BaoStock 按当时可得的最近报告期（TTM/MRQ）计算；
    # 保留原始字段，研究层再决定如何处理负 PE 或缺失估值。
    "peTTM",
    "pbMRQ",
    "psTTM",
    "pcfNcfTTM",
    "isST",
]

NORMALIZED_COLUMNS = [
    "date",
    "symbol",
    "exchange",
    "board",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "volume_unit",
    "amount",
    "turnover_rate",
    "pct_change",
    "pe_ttm",
    "pb_mrq",
    "ps_ttm",
    "pcf_ncf_ttm",
    "trade_status",
    "is_st",
    "adjust",
    "source",
]

T = TypeVar("T")
_deadline_was_triggered = False


class BaoStockDeadlineError(TimeoutError):
    """Raised when BaoStock fails to complete one operation before its deadline."""


class TerminationRequested(BaseException):
    """Raised when the supervisor asks this fetch process to terminate."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"termination requested by signal {signum}")
        self.signum = signum


@dataclass(frozen=True)
class StockInfo:
    code: str
    bs_code: str
    symbol: str
    name: str
    listing_date: str
    delisting_date: str
    status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch real A-share daily bars from BaoStock.")
    parser.add_argument(
        "--exchange",
        default="sz",
        choices=["sz", "sh"],
        help="Exchange to fetch: sz=SZSE, sh=SSE.",
    )
    parser.add_argument("--start-date", default="2017-01-01", help="Inclusive YYYY-MM-DD/YYYMMDD.")
    parser.add_argument(
        "--end-date",
        default=date.today().strftime("%Y-%m-%d"),
        help="Inclusive YYYY-MM-DD/YYYMMDD.",
    )
    parser.add_argument(
        "--adjust",
        default="qfq",
        choices=["qfq", "hfq", "raw"],
        help="qfq=front adjusted, hfq=back adjusted, raw=unadjusted.",
    )
    parser.add_argument(
        "--raw-dir",
        default="",
        help="Directory for per-symbol raw CSV cache.",
    )
    parser.add_argument(
        "--clean-output",
        default="",
        help="Combined normalized CSV path. Default is under KOLMO_DATA_ROOT/work/ashare/.",
    )
    parser.add_argument(
        "--universe-output",
        default="",
        help="Universe CSV path. Default is under KOLMO_DATA_ROOT/raw/ashare/baostock/{exchange}/.",
    )
    parser.add_argument(
        "--universe-input",
        default="",
        help="Use this cached universe CSV instead of querying BaoStock.",
    )
    parser.add_argument(
        "--universe-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Fall back to the latest recent local universe if the live query fails.",
    )
    parser.add_argument(
        "--max-universe-age-days",
        type=float,
        default=7.0,
        help="Maximum age of an automatic cached-universe fallback. Default: 7 days.",
    )
    parser.add_argument(
        "--failures-output",
        default="",
        help="Failure manifest path. Default is a run-scoped file beside the universe CSV.",
    )
    parser.add_argument(
        "--evidence-output",
        default="",
        help="Completed fetch-evidence JSON path. Default is a run-scoped file beside the universe CSV.",
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Optional immutable identifier for this fetch run. Defaults to a UUID.",
    )
    parser.add_argument(
        "--include-delisted",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include stocks delisted after the start date.",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse existing per-symbol raw CSV files.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional symbol limit for testing.")
    parser.add_argument(
        "--symbol",
        action="append",
        help="Optional exact symbol filter, e.g. --symbol 000858.SZ. Can be repeated.",
    )
    parser.add_argument("--sleep", type=float, default=0.05, help="Seconds to sleep between symbols.")
    parser.add_argument(
        "--max-rows-per-symbol",
        type=int,
        default=10_000,
        help="Abort one malformed BaoStock result after this many rows and continue. Default: 10000.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=15.0,
        help="Hard deadline for one BaoStock login/query operation. Default: 15 seconds.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=1,
        help="Reconnect and retry one failed BaoStock operation this many times. Default: 1.",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=1.0,
        help="Initial exponential retry delay. Default: 1 second.",
    )
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=3,
        help="Abort the exchange after this many consecutive symbol failures. Default: 3.",
    )
    parser.add_argument(
        "--heartbeat-symbols",
        type=int,
        default=50,
        help="Persist running evidence after this many processed symbols. Default: 50.",
    )
    parser.add_argument("--no-combine", action="store_true", help="Only write raw per-symbol files.")
    parser.add_argument(
        "--compress-raw",
        choices=["none", "gzip"],
        default="gzip",
        help="Compress per-symbol raw cache files. Default: gzip.",
    )
    return parser.parse_args()


def require_dependencies():
    try:
        import baostock as bs  # type: ignore
    except ModuleNotFoundError as exc:
        print(
            "missing Python dependency: baostock\n"
            "Install with: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return bs


def _deadline_expired(_signum, _frame) -> None:
    global _deadline_was_triggered
    _deadline_was_triggered = True
    raise BaoStockDeadlineError("BaoStock operation exceeded its hard deadline")


def _termination_requested(signum, _frame) -> None:
    raise TerminationRequested(signum)


@contextmanager
def hard_deadline(seconds: float) -> Iterator[None]:
    """Interrupt BaoStock's EOF busy loop as well as ordinary blocking socket reads."""
    global _deadline_was_triggered
    if seconds <= 0:
        raise ValueError("timeout seconds must be positive")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_triggered = _deadline_was_triggered
    _deadline_was_triggered = False
    signal.signal(signal.SIGALRM, _deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
        if _deadline_was_triggered:
            raise BaoStockDeadlineError("BaoStock operation exceeded its hard deadline")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        _deadline_was_triggered = previous_triggered


def close_baostock_socket() -> None:
    """Close BaoStock's process-global connection without another network call."""
    try:
        import baostock.common.context as context  # type: ignore
    except ModuleNotFoundError:
        return
    connection = getattr(context, "default_socket", None)
    if connection is not None:
        try:
            connection.close()
        finally:
            setattr(context, "default_socket", None)


def login_baostock(bs, timeout_seconds: float) -> None:
    close_baostock_socket()
    with hard_deadline(timeout_seconds):
        login = bs.login()
    if login.error_code != "0":
        close_baostock_socket()
        raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")


def login_baostock_with_retry(
    bs,
    timeout_seconds: float,
    retries: int,
    retry_backoff_seconds: float,
) -> None:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            login_baostock(bs, timeout_seconds)
            return
        except Exception as exc:
            last_error = exc
            close_baostock_socket()
            # BaoStock documents 10001011 as an IP blacklist response. Retrying
            # cannot recover it and only sends more traffic from the blocked IP.
            if "10001011" in str(exc):
                break
            if attempt < retries and retry_backoff_seconds:
                time.sleep(retry_backoff_seconds * (2**attempt))
    assert last_error is not None
    raise RuntimeError(f"initial BaoStock login failed after {attempt + 1} attempts: {last_error}") from last_error


def run_with_reconnect(
    bs,
    operation: Callable[[], T],
    *,
    context: str,
    timeout_seconds: float,
    retries: int,
    retry_backoff_seconds: float,
) -> T:
    """Run one operation with a hard deadline and a fresh session after failures."""
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            if attempt:
                login_baostock(bs, timeout_seconds)
            with hard_deadline(timeout_seconds):
                return operation()
        except Exception as exc:
            last_error = exc
            close_baostock_socket()
            if "10001011" in str(exc):
                break
            if attempt < retries and retry_backoff_seconds:
                time.sleep(retry_backoff_seconds * (2**attempt))
    assert last_error is not None
    raise RuntimeError(f"{context} failed after {attempt + 1} attempts: {last_error}") from last_error


def normalize_date(value: str) -> str:
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10]


def compact_date(value: str) -> str:
    return normalize_date(value).replace("-", "")


def today_yyyymmdd() -> str:
    return date.today().strftime("%Y%m%d")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=True, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def failure_temp_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    return Path(temporary_name)


def adjustflag(adjust: str) -> str:
    if adjust == "hfq":
        return "1"
    if adjust == "qfq":
        return "2"
    return "3"


def adjustment_dir_name(adjust: str) -> str:
    return adjust


def raw_cache_path(raw_dir: Path, adjust: str, symbol: str, compress_raw: str) -> Path:
    suffix = ".csv.gz" if compress_raw == "gzip" else ".csv"
    return raw_dir / adjustment_dir_name(adjust) / f"{symbol}{suffix}"


def existing_raw_cache_path(raw_dir: Path, adjust: str, symbol: str) -> Path | None:
    base = raw_dir / adjustment_dir_name(adjust)
    gzip_path = base / f"{symbol}.csv.gz"
    if gzip_path.exists() and gzip_path.stat().st_size > 0:
        return gzip_path
    csv_path = base / f"{symbol}.csv"
    if csv_path.exists() and csv_path.stat().st_size > 0:
        return csv_path
    return None


def raw_cache_has_required_fields(path: Path) -> bool:
    """Old OHLCV caches must be refreshed once to gain valuation columns."""
    try:
        with open_text(path, "rt") as file:
            fields = next(csv.reader(file), [])
        return set(FIELDS).issubset(fields)
    except (OSError, UnicodeError):
        return False


def raw_cache_covers_requested_end(path: Path, stock: StockInfo, end_date: str) -> bool:
    """Reject a resumable cache that stopped before its expected final date."""
    expected = normalize_date(end_date)
    if stock.delisting_date:
        expected = min(expected, normalize_date(stock.delisting_date))
    latest = ""
    try:
        with open_text(path, "rt") as file:
            for row in csv.DictReader(file):
                row_date = normalize_date(row.get("date", ""))
                if row_date > latest:
                    latest = row_date
    except (OSError, UnicodeError, csv.Error):
        return False
    return bool(latest and latest >= expected)


def open_text(path: Path, mode: str):
    if path.suffix == ".gz":
        return gzip.open(path, mode, encoding="utf-8", newline="")
    return path.open(mode, encoding="utf-8", newline="")


def rows_from_result(result, max_rows: int = 0) -> list[list[str]]:
    rows: list[list[str]] = []
    while result.next():
        if max_rows > 0 and len(rows) >= max_rows:
            raise RuntimeError(
                f"BaoStock result exceeded {max_rows} rows; aborting this symbol to avoid an infinite iterator"
            )
        rows.append(result.get_row_data())
    return rows


def query_or_raise(result, context: str):
    if result.error_code != "0":
        raise RuntimeError(f"{context}: {result.error_code} {result.error_msg}")
    return result


def exchange_prefix(exchange: str) -> str:
    return f"{exchange}."


def exchange_suffix(exchange: str) -> str:
    return exchange.upper()


def load_universe(
    bs,
    start_date: str,
    end_date: str,
    include_delisted: bool,
    exchange: str,
) -> list[StockInfo]:
    result = query_or_raise(bs.query_stock_basic(), "query_stock_basic")
    fields = result.fields
    stocks: list[StockInfo] = []
    prefix = exchange_prefix(exchange)
    suffix = exchange_suffix(exchange)

    for values in rows_from_result(result):
        row = dict(zip(fields, values))
        bs_code = row.get("code", "")
        if not bs_code.startswith(prefix):
            continue
        if row.get("type", "") != "1":
            continue

        listing_date = row.get("ipoDate", "")
        delisting_date = row.get("outDate", "")
        if listing_date and listing_date > end_date:
            continue
        if delisting_date and delisting_date < start_date and include_delisted:
            continue
        if delisting_date and not include_delisted:
            continue

        code = bs_code.split(".", 1)[1]
        stocks.append(
            StockInfo(
                code=code,
                bs_code=bs_code,
                symbol=f"{code}.{suffix}",
                name=row.get("code_name", ""),
                listing_date=listing_date,
                delisting_date=delisting_date,
                status="delisted" if delisting_date else "active",
            )
        )

    if not stocks:
        raise RuntimeError(f"query_stock_basic returned an empty {exchange} stock universe")
    return sorted(stocks, key=lambda stock: stock.code)


def write_universe(path: Path, stocks: Iterable[StockInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "code",
                "bs_code",
                "symbol",
                "name",
                "listing_date",
                "delisting_date",
                "status",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for stock in stocks:
            writer.writerow(stock.__dict__)


def read_universe(path: Path, exchange: str) -> list[StockInfo]:
    stocks: list[StockInfo] = []
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        required = set(StockInfo.__dataclass_fields__)
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"cached universe has invalid columns: {path}")
        for row in reader:
            stock = StockInfo(**{field: row.get(field, "") for field in required})
            if stock.bs_code.startswith(f"{exchange}.") and stock.symbol.endswith(f".{exchange.upper()}"):
                stocks.append(stock)
    symbols = [stock.symbol for stock in stocks]
    if not stocks or len(symbols) != len(set(symbols)):
        raise ValueError(f"cached universe is empty or has duplicate symbols: {path}")
    return sorted(stocks, key=lambda stock: stock.code)


def latest_cached_universe(exchange: str, max_age_days: float) -> Path:
    root = data_path("raw", "ashare", "baostock", exchange)
    candidates = [path for path in root.glob(f"universe_{exchange}_*.csv") if path.is_file()]
    if not candidates:
        raise FileNotFoundError(f"no cached {exchange} universe found under {root}")
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    age_seconds = max(0.0, time.time() - latest.stat().st_mtime)
    if age_seconds > max_age_days * 86400:
        raise RuntimeError(
            f"latest cached {exchange} universe is {age_seconds / 86400:.1f} days old; "
            f"limit={max_age_days:g}: {latest}"
        )
    return latest


def fetch_symbol(bs, stock: StockInfo, args: argparse.Namespace, raw_dir: Path) -> tuple[Path, str]:
    if args.resume:
        existing_path = existing_raw_cache_path(raw_dir, args.adjust, stock.symbol)
        if (
            existing_path is not None
            and raw_cache_has_required_fields(existing_path)
            and raw_cache_covers_requested_end(existing_path, stock, args.end_date)
        ):
            return existing_path, "cached"
    path = raw_cache_path(raw_dir, args.adjust, stock.symbol, args.compress_raw)

    result = query_or_raise(
        bs.query_history_k_data_plus(
            stock.bs_code,
            ",".join(FIELDS),
            start_date=normalize_date(args.start_date),
            end_date=normalize_date(args.end_date),
            frequency="d",
            adjustflag=adjustflag(args.adjust),
        ),
        f"query_history_k_data_plus {stock.bs_code}",
    )

    rows = rows_from_result(result, args.max_rows_per_symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp{path.suffix}")
    try:
        with open_text(temporary_path, "wt") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(result.fields)
            writer.writerows(rows)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return path, "fetched"


def append_normalized(
    output: Path,
    raw_path: Path,
    stock: StockInfo,
    adjust: str,
    exchange: str,
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output.exists() or output.stat().st_size == 0
    rows_written = 0

    with open_text(raw_path, "rt") as input_file:
        reader = csv.DictReader(input_file)
        with output.open("a", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(
                output_file, fieldnames=NORMALIZED_COLUMNS, lineterminator="\n"
            )
            if write_header:
                writer.writeheader()

            for row in reader:
                if not row.get("date"):
                    continue
                writer.writerow(
                    {
                        "date": compact_date(row.get("date", "")),
                        "symbol": stock.symbol,
                        "exchange": exchange_suffix(exchange),
                        "board": board_for_symbol(stock.symbol),
                        "open": row.get("open", ""),
                        "high": row.get("high", ""),
                        "low": row.get("low", ""),
                        "close": row.get("close", ""),
                        "preclose": row.get("preclose", ""),
                        "volume": row.get("volume", ""),
                        "volume_unit": "share",
                        "amount": row.get("amount", ""),
                        "turnover_rate": row.get("turn", ""),
                        "pct_change": row.get("pctChg", ""),
                        "pe_ttm": row.get("peTTM", ""),
                        "pb_mrq": row.get("pbMRQ", ""),
                        "ps_ttm": row.get("psTTM", ""),
                        "pcf_ncf_ttm": row.get("pcfNcfTTM", ""),
                        "trade_status": row.get("tradestatus", ""),
                        "is_st": row.get("isST", ""),
                        "adjust": adjust,
                        "source": "baostock.query_history_k_data_plus",
                    }
                )
                rows_written += 1

    return rows_written


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    if args.retries < 0:
        raise ValueError("--retries must not be negative")
    if args.retry_backoff_seconds < 0:
        raise ValueError("--retry-backoff-seconds must not be negative")
    if args.max_consecutive_failures < 1:
        raise ValueError("--max-consecutive-failures must be at least 1")
    if args.heartbeat_symbols < 1:
        raise ValueError("--heartbeat-symbols must be at least 1")
    if args.max_universe_age_days <= 0:
        raise ValueError("--max-universe-age-days must be positive")
    bs = require_dependencies()

    run_id = args.run_id.strip() or uuid.uuid4().hex
    start_date = normalize_date(args.start_date)
    end_date = normalize_date(args.end_date)
    raw_dir = Path(
        args.raw_dir or data_path("raw", "ashare", "baostock", args.exchange, "daily")
    )
    evidence_output = Path(
        args.evidence_output
        or data_path(
            "raw", "ashare", "baostock", args.exchange, "runs", f"fetch_{args.exchange}_{run_id}.json"
        )
    )
    failures_output = Path(
        args.failures_output
        or data_path(
            "raw", "ashare", "baostock", args.exchange, "runs", f"failures_{args.exchange}_{run_id}.csv"
        )
    )
    started_at = utc_now()
    evidence = {
        "schema_version": "1.0.0",
        "product_id": "baostock_daily_fetch",
        "run_id": run_id,
        "exchange": args.exchange,
        "start_date": compact_date(start_date),
        "end_date": compact_date(end_date),
        "adjust": args.adjust,
        "status": "running",
        "started_at": started_at,
        "completed_at": None,
        "failure_manifest_path": str(failures_output.resolve()),
        "failure_manifest_sha256": None,
        "symbols": {"total": 0, "fetched": 0, "cached": 0, "failed": 0},
        "rows": 0,
    }
    write_atomic_json(evidence_output, evidence)

    previous_socket_timeout = socket.getdefaulttimeout()
    previous_term_handler = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _termination_requested)
    socket.setdefaulttimeout(args.timeout_seconds)
    try:
        login_baostock_with_retry(
            bs,
            args.timeout_seconds,
            args.retries,
            args.retry_backoff_seconds,
        )
    except TerminationRequested as exc:
        close_baostock_socket()
        evidence["status"] = "aborted"
        evidence["completed_at"] = utc_now()
        evidence["error"] = str(exc)
        write_atomic_json(evidence_output, evidence)
        socket.setdefaulttimeout(previous_socket_timeout)
        signal.signal(signal.SIGTERM, previous_term_handler)
        return 128 + exc.signum
    except Exception as exc:
        close_baostock_socket()
        evidence["status"] = "login_failed"
        evidence["completed_at"] = utc_now()
        evidence["error"] = str(exc)
        write_atomic_json(evidence_output, evidence)
        print(str(exc), file=sys.stderr)
        socket.setdefaulttimeout(previous_socket_timeout)
        signal.signal(signal.SIGTERM, previous_term_handler)
        return 2

    try:
        clean_output = Path(
            args.clean_output
            or data_path(
                "work",
                "ashare",
                f"{args.exchange}_daily_bars_{compact_date(start_date)}_{compact_date(end_date)}_{args.adjust}_baostock.csv",
            )
        )
        universe_output = Path(
            args.universe_output
            or data_path(
                "raw",
                "ashare",
                "baostock",
                args.exchange,
                f"universe_{args.exchange}_{run_id}.csv",
            )
        )

        universe_source = "live"
        universe_source_path = ""
        if args.universe_input:
            cached_path = Path(args.universe_input)
            stocks = read_universe(cached_path, args.exchange)
            universe_source = "explicit_cache"
            universe_source_path = str(cached_path.resolve())
        else:
            try:
                stocks = run_with_reconnect(
                    bs,
                    lambda: load_universe(
                        bs,
                        start_date=start_date,
                        end_date=end_date,
                        include_delisted=args.include_delisted,
                        exchange=args.exchange,
                    ),
                    context="load universe",
                    timeout_seconds=args.timeout_seconds,
                    retries=args.retries,
                    retry_backoff_seconds=args.retry_backoff_seconds,
                )
            except Exception as live_error:
                if not args.universe_fallback:
                    raise
                cached_path = latest_cached_universe(args.exchange, args.max_universe_age_days)
                stocks = read_universe(cached_path, args.exchange)
                universe_source = "automatic_cache_fallback"
                universe_source_path = str(cached_path.resolve())
                print(
                    f"warning: live universe failed ({live_error}); using cached universe={cached_path}",
                    file=sys.stderr,
                    flush=True,
                )
        symbols = {symbol.upper() for symbol in args.symbol or []}
        if symbols:
            stocks = [stock for stock in stocks if stock.symbol in symbols]
            missing = symbols - {stock.symbol for stock in stocks}
            if missing:
                raise ValueError(f"symbols are not in the {args.exchange} universe: {sorted(missing)}")
        if args.limit > 0:
            stocks = stocks[: args.limit]
        write_universe(universe_output, stocks)
        evidence["symbols"]["total"] = len(stocks)
        evidence["universe"] = {
            "source": universe_source,
            "source_path": universe_source_path,
            "output_path": str(universe_output.resolve()),
            "output_sha256": sha256_file(universe_output),
        }
        evidence["last_progress_at"] = utc_now()
        write_atomic_json(evidence_output, evidence)

        if clean_output.exists() and not args.no_combine:
            clean_output.unlink()

        fetched = 0
        cached = 0
        failed = 0
        consecutive_failures = 0
        total_rows = 0
        temporary_failures = failure_temp_path(failures_output)

        try:
            with temporary_failures.open("w", encoding="utf-8", newline="") as failure_file:
                failure_writer = csv.DictWriter(
                    failure_file,
                    fieldnames=["symbol", "status", "error"],
                    lineterminator="\n",
                )
                failure_writer.writeheader()

                for index, stock in enumerate(stocks, start=1):
                    try:
                        raw_path, source_state = run_with_reconnect(
                            bs,
                            lambda: fetch_symbol(bs, stock, args, raw_dir),
                            context=f"fetch {stock.symbol}",
                            timeout_seconds=args.timeout_seconds,
                            retries=args.retries,
                            retry_backoff_seconds=args.retry_backoff_seconds,
                        )
                        fetched += 1 if source_state == "fetched" else 0
                        cached += 1 if source_state == "cached" else 0
                        consecutive_failures = 0
                        rows = 0
                        if not args.no_combine:
                            rows = append_normalized(
                                clean_output, raw_path, stock, args.adjust, args.exchange
                            )
                            total_rows += rows

                        print(
                            f"[{index}/{len(stocks)}] {stock.symbol} {source_state} rows={rows}",
                            flush=True,
                        )
                    except Exception as exc:
                        failed += 1
                        consecutive_failures += 1
                        failure_writer.writerow(
                            {"symbol": stock.symbol, "status": stock.status, "error": repr(exc)}
                        )
                        print(f"[{index}/{len(stocks)}] {stock.symbol} failed: {exc}", file=sys.stderr)

                        if consecutive_failures >= args.max_consecutive_failures:
                            remaining = stocks[index:]
                            for skipped in remaining:
                                failure_writer.writerow(
                                    {
                                        "symbol": skipped.symbol,
                                        "status": skipped.status,
                                        "error": "circuit_open_after_consecutive_failures",
                                    }
                                )
                            failed += len(remaining)
                            print(
                                f"aborting exchange after {consecutive_failures} consecutive failures; "
                                f"remaining={len(remaining)}",
                                file=sys.stderr,
                                flush=True,
                            )
                            break

                    if index % args.heartbeat_symbols == 0:
                        evidence["symbols"].update(
                            {"fetched": fetched, "cached": cached, "failed": failed}
                        )
                        evidence["rows"] = total_rows
                        evidence["last_symbol"] = stock.symbol
                        evidence["last_progress_at"] = utc_now()
                        write_atomic_json(evidence_output, evidence)

                    if args.sleep > 0:
                        time.sleep(args.sleep)
                failure_file.flush()
                os.fsync(failure_file.fileno())
            os.replace(temporary_failures, failures_output)
        finally:
            temporary_failures.unlink(missing_ok=True)

        completed_empty = failed == 0 and not args.no_combine and total_rows == 0
        evidence.update(
            {
                "status": (
                    "completed_empty"
                    if completed_empty
                    else "completed" if failed == 0 else "completed_with_failures"
                ),
                "completed_at": utc_now(),
                "failure_manifest_sha256": sha256_file(failures_output),
                "symbols": {
                    "total": len(stocks),
                    "fetched": fetched,
                    "cached": cached,
                    "failed": failed,
                },
                "rows": total_rows,
            }
        )
        if completed_empty:
            evidence["error"] = "BaoStock returned zero rows for the requested exchange window"
        write_atomic_json(evidence_output, evidence)

        print(
            "done "
            f"symbols={len(stocks)} fetched={fetched} cached={cached} failed={failed} "
            f"rows={total_rows} universe={universe_output} failures={failures_output} "
            f"evidence={evidence_output} "
            f"clean={'' if args.no_combine else clean_output}"
        )
        return 0 if failed == 0 and not completed_empty else 1
    except TerminationRequested as exc:
        evidence["status"] = "aborted"
        evidence["completed_at"] = utc_now()
        evidence["error"] = str(exc)
        write_atomic_json(evidence_output, evidence)
        print(str(exc), file=sys.stderr)
        return 128 + exc.signum
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["completed_at"] = utc_now()
        evidence["error"] = repr(exc)
        write_atomic_json(evidence_output, evidence)
        print(f"BaoStock fetch aborted: {exc}", file=sys.stderr)
        return 1
    finally:
        close_baostock_socket()
        socket.setdefaulttimeout(previous_socket_timeout)
        signal.signal(signal.SIGTERM, previous_term_handler)


if __name__ == "__main__":
    raise SystemExit(main())
