"""SEC EDGAR contracts, normalization, filesystem, and HTTP helpers."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from kolmo.data_products import SEC_COMPANY_FACT_COLUMNS, SEC_FILING_COLUMNS
from kolmo.us_market.daily import normalize_symbol


SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SEC_SUBMISSIONS_BASE_URL = "https://data.sec.gov/submissions/"
SEC_FILING_SOURCE = "sec.edgar.submissions"
SEC_FACT_SOURCE = "sec.edgar.companyfacts"
SUPPORTED_FORMS = {
    "10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A",
    "20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A",
}
CIK_PATTERN = re.compile(r"^\d{1,10}$")
ACCESSION_PATTERN = re.compile(r"^\d{10}-\d{2}-\d{6}$")
HISTORY_FILE_PATTERN = re.compile(r"^CIK\d{10}-submissions-\d{3}\.json$")
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off", ""}


class SecEdgarError(RuntimeError):
    """SEC data could not be safely fetched or normalized."""


class SecHttpError(SecEdgarError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SecUniverseEntry:
    symbol: str
    cik: str


@dataclass(frozen=True)
class UniverseSkip:
    symbol: str
    cik: str
    reason: str


@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    body: bytes


def normalize_cik(value: object) -> str:
    text = str(value).strip()
    if not CIK_PATTERN.fullmatch(text):
        raise ValueError(f"invalid CIK: {value!r}")
    return text.zfill(10)


def _enabled(value: object) -> bool:
    text = str(value if value is not None else "1").strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"invalid enabled value: {value!r}")


def load_sec_universe(path: Path) -> tuple[list[SecUniverseEntry], list[UniverseSkip]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"symbol", "asset_type", "cik"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"universe is missing columns: {', '.join(missing)}")
        entries: list[SecUniverseEntry] = []
        skipped: list[UniverseSkip] = []
        seen_symbols: set[str] = set()
        for line_number, row in enumerate(reader, start=2):
            try:
                symbol = normalize_symbol(row.get("symbol", ""))
                enabled = _enabled(row.get("enabled", "1"))
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            raw_cik = str(row.get("cik", "")).strip()
            asset_type = str(row.get("asset_type", "")).strip().upper()
            cik = ""
            reason = ""
            if not enabled:
                reason = "disabled"
            elif asset_type != "STK":
                reason = "asset_type_not_stock"
            elif not raw_cik:
                reason = "missing_cik"
            else:
                try:
                    cik = normalize_cik(raw_cik)
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_number}: {exc}") from exc
            if reason:
                skipped.append(UniverseSkip(symbol, cik, reason))
                continue
            if symbol in seen_symbols:
                raise ValueError(f"{path}:{line_number}: duplicate SEC symbol: {symbol}")
            seen_symbols.add(symbol)
            entries.append(SecUniverseEntry(symbol, cik))
    return entries, skipped


def select_sec_universe(
    entries: Sequence[SecUniverseEntry], symbols: Sequence[str], ciks: Sequence[str]
) -> list[SecUniverseEntry]:
    if not symbols and not ciks:
        return list(entries)
    by_symbol = {entry.symbol: entry for entry in entries}
    by_cik: dict[str, list[SecUniverseEntry]] = {}
    for entry in entries:
        by_cik.setdefault(entry.cik, []).append(entry)
    selected: dict[str, SecUniverseEntry] = {}
    for raw_symbol in symbols:
        symbol = normalize_symbol(raw_symbol)
        if symbol not in by_symbol:
            raise ValueError(f"symbol is not an enabled stock with a CIK in the universe: {symbol}")
        selected[by_symbol[symbol].symbol] = by_symbol[symbol]
    for raw_cik in ciks:
        cik = normalize_cik(raw_cik)
        if cik not in by_cik:
            raise ValueError(f"CIK is not an enabled stock in the universe: {cik}")
        for entry in by_cik[cik]:
            selected[entry.symbol] = entry
    return sorted(selected.values(), key=lambda item: item.symbol)


def parse_json_bytes(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"), parse_float=Decimal, parse_int=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecEdgarError(f"invalid SEC JSON response: {exc}") from exc


def _iso_date(value: object, field: str, context: str, *, allow_empty: bool = True) -> str:
    text = str(value or "").strip()[:10]
    if not text and allow_empty:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise SecEdgarError(f"{context}: invalid {field}: {value!r}") from exc


def normalize_accepted_at(value: object, context: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{14}", text):
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S")
        return parsed.replace(tzinfo=ZoneInfo("America/New_York")).isoformat()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SecEdgarError(f"{context}: invalid accepted_at: {value!r}") from exc
    if parsed.tzinfo is None:
        raise SecEdgarError(f"{context}: accepted_at lacks timezone: {value!r}")
    return parsed.isoformat()


def _flag(value: object) -> str:
    if value in (1, True, "1", "true", "True"):
        return "1"
    if value in (0, False, "0", "false", "False", "", None):
        return "0"
    raise SecEdgarError(f"invalid SEC boolean flag: {value!r}")


def _columnar_rows(payload: Mapping[str, object], context: str) -> Iterable[dict[str, object]]:
    lengths = {len(value) for value in payload.values() if isinstance(value, list)}
    if not lengths:
        return []
    if len(lengths) != 1:
        raise SecEdgarError(f"{context}: submissions columns have inconsistent lengths")
    count = lengths.pop()
    return [
        {key: value[index] if isinstance(value, list) and index < len(value) else "" for key, value in payload.items()}
        for index in range(count)
    ]


def normalize_filings(
    symbol: str,
    cik: str,
    submissions: Mapping[str, object],
    historical: Sequence[Mapping[str, object]],
    retrieved_at: str,
) -> list[dict[str, str]]:
    recent = submissions.get("filings", {})
    recent_table = recent.get("recent", {}) if isinstance(recent, dict) else {}
    tables: list[Mapping[str, object]] = list(historical)
    if isinstance(recent_table, dict):
        tables.append(recent_table)
    by_accession: dict[str, dict[str, str]] = {}
    for table_index, table in enumerate(tables):
        for item in _columnar_rows(table, f"{symbol} submissions table {table_index}"):
            form = str(item.get("form", "")).strip().upper()
            if form not in SUPPORTED_FORMS:
                continue
            accession = str(item.get("accessionNumber", "")).strip()
            context = f"{symbol} accession {accession or '<missing>'}"
            if not accession:
                raise SecEdgarError(f"{context}: missing accession number")
            row = {
                "symbol": symbol,
                "cik": cik,
                "accession_number": accession,
                "form": form,
                "filing_date": _iso_date(item.get("filingDate"), "filing_date", context, allow_empty=False),
                "report_date": _iso_date(item.get("reportDate"), "report_date", context),
                "accepted_at": normalize_accepted_at(item.get("acceptanceDateTime"), context),
                "primary_document": str(item.get("primaryDocument", "") or "").strip(),
                "is_xbrl": _flag(item.get("isXBRL")),
                "is_inline_xbrl": _flag(item.get("isInlineXBRL")),
                "source": SEC_FILING_SOURCE,
                "retrieved_at": retrieved_at,
            }
            by_accession[accession] = row
    return sorted(by_accession.values(), key=filing_sort_key)


def filing_sort_key(row: Mapping[str, str]) -> tuple[str, str, str]:
    return (row.get("accepted_at", "") or row.get("filing_date", ""), row.get("filing_date", ""), row.get("accession_number", ""))


def _number_string(value: object, context: str) -> str:
    if value is None or isinstance(value, bool):
        raise SecEdgarError(f"{context}: invalid numeric value: {value!r}")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise SecEdgarError(f"{context}: invalid numeric value: {value!r}") from exc
    if not parsed.is_finite():
        raise SecEdgarError(f"{context}: non-finite numeric value")
    return format(parsed, "f")


FACT_ID_FIELDS = (
    "symbol", "cik", "taxonomy", "tag", "unit", "start_date", "end_date", "value",
    "filed_date", "form", "fiscal_year", "fiscal_period", "frame", "accession_number",
)


def fact_row_id(row: Mapping[str, str]) -> str:
    identity = "\x1f".join(str(row.get(field, "")) for field in FACT_ID_FIELDS)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def normalize_company_facts(
    symbol: str,
    cik: str,
    payload: Mapping[str, object],
    accepted_by_accession: Mapping[str, str],
    retrieved_at: str,
) -> list[dict[str, str]]:
    facts = payload.get("facts", {})
    if not isinstance(facts, dict):
        raise SecEdgarError(f"{symbol}: companyfacts facts must be an object")
    output: dict[str, dict[str, str]] = {}
    for taxonomy in sorted(facts):
        tags = facts[taxonomy]
        if not isinstance(tags, dict):
            raise SecEdgarError(f"{symbol}: taxonomy {taxonomy} must be an object")
        for tag in sorted(tags):
            definition = tags[tag]
            if not isinstance(definition, dict):
                raise SecEdgarError(f"{symbol} {taxonomy}/{tag}: invalid definition")
            units = definition.get("units", {})
            if not isinstance(units, dict):
                raise SecEdgarError(f"{symbol} {taxonomy}/{tag}: units must be an object")
            for unit in sorted(units):
                records = units[unit]
                if not isinstance(records, list):
                    raise SecEdgarError(f"{symbol} {taxonomy}/{tag}/{unit}: facts must be an array")
                for index, item in enumerate(records):
                    if not isinstance(item, dict):
                        raise SecEdgarError(f"{symbol} {taxonomy}/{tag}/{unit}[{index}]: invalid fact")
                    form = str(item.get("form", "")).strip().upper()
                    if form not in SUPPORTED_FORMS:
                        continue
                    accession = str(item.get("accn", "")).strip()
                    context = f"{symbol} {taxonomy}/{tag}/{unit} accession {accession or '<missing>'}"
                    if not accession:
                        raise SecEdgarError(f"{context}: missing accession number")
                    filed_date = _iso_date(item.get("filed"), "filed_date", context, allow_empty=False)
                    accepted_at = accepted_by_accession.get(accession, "")
                    row = {
                        "row_id": "",
                        "symbol": symbol,
                        "cik": cik,
                        "taxonomy": str(taxonomy),
                        "tag": str(tag),
                        "label": str(definition.get("label", "") or ""),
                        "description": str(definition.get("description", "") or ""),
                        "unit": str(unit),
                        "start_date": _iso_date(item.get("start"), "start_date", context),
                        "end_date": _iso_date(item.get("end"), "end_date", context, allow_empty=False),
                        "value": _number_string(item.get("val"), context),
                        "filed_date": filed_date,
                        "accepted_at": accepted_at,
                        "available_date": accepted_at[:10] if accepted_at else filed_date,
                        "form": form,
                        "fiscal_year": str(item.get("fy", "") or ""),
                        "fiscal_period": str(item.get("fp", "") or ""),
                        "frame": str(item.get("frame", "") or ""),
                        "accession_number": accession,
                        "source": SEC_FACT_SOURCE,
                        "retrieved_at": retrieved_at,
                    }
                    row["row_id"] = fact_row_id(row)
                    output[row["row_id"]] = row
    return sorted(output.values(), key=fact_sort_key)


def fact_sort_key(row: Mapping[str, str]) -> tuple[str, ...]:
    return (
        row.get("available_date", ""), row.get("accepted_at", ""), row.get("filed_date", ""),
        row.get("accession_number", ""), row.get("taxonomy", ""), row.get("tag", ""),
        row.get("unit", ""), row.get("start_date", ""), row.get("end_date", ""),
        row.get("frame", ""), row.get("value", ""), row.get("row_id", ""),
    )


def merge_filings(existing: Iterable[Mapping[str, str]], incoming: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    for row in existing:
        by_key[str(row["accession_number"])] = {column: str(row.get(column, "")) for column in SEC_FILING_COLUMNS}
    for row in incoming:
        normalized = {column: str(row.get(column, "")) for column in SEC_FILING_COLUMNS}
        previous = by_key.get(normalized["accession_number"])
        if previous:
            normalized["retrieved_at"] = previous["retrieved_at"]
        by_key[normalized["accession_number"]] = normalized
    return sorted(by_key.values(), key=filing_sort_key)


def merge_facts(existing: Iterable[Mapping[str, str]], incoming: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    by_key = {
        str(row["row_id"]): {column: str(row.get(column, "")) for column in SEC_COMPANY_FACT_COLUMNS}
        for row in existing
    }
    for row in incoming:
        normalized = {column: str(row.get(column, "")) for column in SEC_COMPANY_FACT_COLUMNS}
        previous = by_key.get(normalized["row_id"])
        if previous:
            normalized["retrieved_at"] = previous["retrieved_at"]
        by_key[normalized["row_id"]] = normalized
    return sorted(by_key.values(), key=fact_sort_key)


def filings_path(output_root: Path, symbol: str) -> Path:
    return output_root / "filings" / f"{symbol}.csv.gz"


def facts_path(output_root: Path, symbol: str) -> Path:
    return output_root / "company_facts" / f"{symbol}.csv.gz"


def read_gzip_csv(path: Path, columns: Sequence[str]) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != list(columns):
            raise ValueError(f"unexpected schema in {path}: {reader.fieldnames}")
        return list(reader)


def _prepare_gzip_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)
            raw.flush()
            os.fsync(raw.fileno())
        return temporary_path
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def write_symbol_atomic(
    output_root: Path, symbol: str, filings: Sequence[Mapping[str, str]], facts: Sequence[Mapping[str, str]]
) -> tuple[Path, Path]:
    filing_output = filings_path(output_root, symbol)
    fact_output = facts_path(output_root, symbol)
    filing_temp = _prepare_gzip_csv(filing_output, SEC_FILING_COLUMNS, filings)
    try:
        fact_temp = _prepare_gzip_csv(fact_output, SEC_COMPANY_FACT_COLUMNS, facts)
    except Exception:
        filing_temp.unlink(missing_ok=True)
        raise
    try:
        os.replace(filing_temp, filing_output)
        os.replace(fact_temp, fact_output)
    finally:
        filing_temp.unlink(missing_ok=True)
        fact_temp.unlink(missing_ok=True)
    return filing_output, fact_output


def _snapshot_prefix(retrieved_at: str) -> str:
    parsed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("retrieved_at must include a timezone")
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def write_raw_snapshot_if_changed(
    raw_root: Path, kind: str, cik: str, retrieved_at: str, body: bytes, label: str = "main"
) -> tuple[Path, bool]:
    directory = raw_root / kind / cik
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    digest = hashlib.sha256(body).hexdigest()
    matching = sorted(directory.glob(f"*-{safe_label}-{digest}.json.gz"))
    for candidate in reversed(matching):
        try:
            with gzip.open(candidate, "rb") as source:
                if source.read() == body:
                    return candidate, False
        except OSError:
            continue
    path = directory / f"{_snapshot_prefix(retrieved_at)}-{safe_label}-{digest}.json.gz"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                compressed.write(body)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path, True


class GlobalRateLimiter:
    def __init__(self, requests_per_second: float, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep):
        if requests_per_second <= 0 or requests_per_second > 10:
            raise ValueError("requests-per-second must be greater than 0 and at most 10")
        self.interval = 1.0 / requests_per_second
        self.clock = clock
        self.sleeper = sleeper
        self.lock = threading.Lock()
        self.next_request_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = self.clock()
            delay = max(0.0, self.next_request_at - now)
            if delay:
                self.sleeper(delay)
                now = self.clock()
            self.next_request_at = max(now, self.next_request_at) + self.interval


class SecHttpClient:
    def __init__(
        self,
        user_agent: str,
        requests_per_second: float = 5.0,
        timeout: float = 30.0,
        retries: int = 3,
        opener: Callable[..., object] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        limiter: GlobalRateLimiter | None = None,
    ):
        if not user_agent.strip():
            raise ValueError("missing SEC User-Agent; set SEC_USER_AGENT or pass --user-agent")
        if timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        if retries < 0 or retries > 8:
            raise ValueError("retries must be between 0 and 8")
        self.user_agent = user_agent.strip()
        self.timeout = timeout
        self.retries = retries
        self.opener = opener
        self.sleeper = sleeper
        self.limiter = limiter or GlobalRateLimiter(requests_per_second)

    def get(self, url: str) -> HttpResponse:
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip", "Accept": "application/json"})
        last_status = 0
        for attempt in range(self.retries + 1):
            self.limiter.wait()
            try:
                with self.opener(request, timeout=self.timeout) as response:  # type: ignore[attr-defined]
                    status = int(getattr(response, "status", 200))
                    body = response.read()
                    if str(getattr(response, "headers", {}).get("Content-Encoding", "")).lower() == "gzip":
                        body = gzip.decompress(body)
                    return HttpResponse(url, status, body)
            except urllib.error.HTTPError as exc:
                last_status = exc.code
                if not (exc.code == 429 or 500 <= exc.code < 600) or attempt == self.retries:
                    raise SecHttpError(f"SEC HTTP {exc.code}: {url}", exc.code) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise SecHttpError(f"SEC request failed: {url}: {exc}", last_status) from exc
            self.sleeper(min(2**attempt, 8))
        raise AssertionError("unreachable")


def historical_submission_files(submissions: Mapping[str, object]) -> list[str]:
    filings = submissions.get("filings", {})
    files = filings.get("files", []) if isinstance(filings, dict) else []
    if not isinstance(files, list):
        raise SecEdgarError("submissions filings.files must be an array")
    names: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            raise SecEdgarError("submissions historical file record must be an object")
        name = str(item.get("name", "")).strip()
        if not HISTORY_FILE_PATTERN.fullmatch(name):
            raise SecEdgarError(f"invalid historical submissions filename: {name!r}")
        names.append(name)
    return sorted(set(names))
