"""Cached, read-only access to canonical SEC EDGAR data products."""

from __future__ import annotations

import csv
import gzip
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

from kolmo.web.market_data import US_SYMBOL_RE


@lru_cache(maxsize=64)
def _read_csv_cached(path_text: str, modified_ns: int, size: int) -> tuple[dict[str, str], ...]:
    del modified_ns, size
    with gzip.open(path_text, "rt", encoding="utf-8", newline="") as source:
        return tuple(dict(row) for row in csv.DictReader(source))


def _read_csv(path: Path) -> tuple[dict[str, str], ...]:
    stat = path.stat()
    return _read_csv_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _counts(values) -> list[dict[str, object]]:
    counter = Counter(value for value in values if value)
    return [
        {"value": value, "count": count}
        for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


class FundamentalStore:
    """Serve SEC facts and filings without changing their data semantics."""

    def __init__(self, root: Path):
        self.root = root / "fundamental" / "us" / "sec"

    def _symbol(self, symbol: str) -> str:
        normalized = symbol.strip().upper()
        if not US_SYMBOL_RE.fullmatch(normalized):
            raise ValueError("invalid US symbol")
        return normalized

    def _path(self, kind: str, symbol: str) -> Path:
        path = self.root / kind / f"{self._symbol(symbol)}.csv.gz"
        if not path.is_file():
            raise FileNotFoundError(f"SEC {kind} data not found for {symbol.strip().upper()}")
        return path

    def _facts(self, symbol: str) -> tuple[dict[str, str], ...]:
        return _read_csv(self._path("company_facts", symbol))

    def _filings(self, symbol: str) -> tuple[dict[str, str], ...]:
        return _read_csv(self._path("filings", symbol))

    def available(self, symbol: str) -> bool:
        normalized = self._symbol(symbol)
        return (
            (self.root / "company_facts" / f"{normalized}.csv.gz").is_file()
            and (self.root / "filings" / f"{normalized}.csv.gz").is_file()
        )

    def summary(self, symbol: str) -> dict[str, object]:
        started = time.perf_counter()
        normalized = self._symbol(symbol)
        facts = self._facts(normalized)
        filings = self._filings(normalized)
        if not facts or not filings:
            raise FileNotFoundError(f"SEC canonical data is empty for {normalized}")
        concepts = Counter(
            (row["taxonomy"], row["tag"], row["unit"])
            for row in facts
        )
        popular = [
            {"taxonomy": key[0], "tag": key[1], "unit": key[2], "count": count}
            for key, count in sorted(
                concepts.items(), key=lambda item: (-item[1], item[0])
            )[:12]
        ]
        end_dates = [row["end_date"] for row in facts if row["end_date"]]
        available_dates = [row["available_date"] for row in facts if row["available_date"]]
        retrieved = [row["retrieved_at"] for row in facts if row["retrieved_at"]]
        return {
            "symbol": normalized,
            "cik": facts[0]["cik"],
            "facts_rows": len(facts),
            "filings_rows": len(filings),
            "concepts": len(concepts),
            "period_start": min(end_dates) if end_dates else "",
            "period_end": max(end_dates) if end_dates else "",
            "available_start": min(available_dates) if available_dates else "",
            "available_end": max(available_dates) if available_dates else "",
            "retrieved_at": max(retrieved) if retrieved else "",
            "taxonomies": _counts(row["taxonomy"] for row in facts),
            "units": _counts(row["unit"] for row in facts),
            "fact_forms": _counts(row["form"] for row in facts),
            "filing_forms": _counts(row["form"] for row in filings),
            "popular_concepts": popular,
            "load_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    def facts(
        self,
        symbol: str,
        query: str = "",
        taxonomy: str = "",
        unit: str = "",
        form: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, object]:
        normalized = self._symbol(symbol)
        needle = query.strip().lower()
        rows = [
            row for row in self._facts(normalized)
            if (not taxonomy or row["taxonomy"] == taxonomy)
            and (not unit or row["unit"] == unit)
            and (not form or row["form"] == form)
            and (
                not needle
                or any(
                    needle in row[field].lower()
                    for field in ("taxonomy", "tag", "label", "description", "accession_number")
                )
            )
        ]
        rows.sort(
            key=lambda row: (
                row["available_date"], row["end_date"], row["taxonomy"], row["tag"],
                row["unit"], row["accession_number"], row["row_id"],
            ),
            reverse=True,
        )
        return {
            "symbol": normalized,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "facts": rows[offset:offset + limit],
        }

    def filings(
        self, symbol: str, form: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, object]:
        normalized = self._symbol(symbol)
        rows = [
            row for row in self._filings(normalized)
            if not form or row["form"] == form
        ]
        rows.sort(
            key=lambda row: (row["accepted_at"], row["filing_date"], row["accession_number"]),
            reverse=True,
        )
        return {
            "symbol": normalized,
            "total": len(rows),
            "offset": offset,
            "limit": limit,
            "filings": rows[offset:offset + limit],
        }

    def series(self, symbol: str, taxonomy: str, tag: str, unit: str) -> dict[str, object]:
        normalized = self._symbol(symbol)
        if not taxonomy or not tag or not unit:
            raise ValueError("taxonomy, tag, and unit are required")
        rows = [
            row for row in self._facts(normalized)
            if row["taxonomy"] == taxonomy and row["tag"] == tag and row["unit"] == unit
        ]
        rows.sort(
            key=lambda row: (
                row["end_date"], row["available_date"], row["accession_number"], row["row_id"]
            )
        )
        if not rows:
            raise FileNotFoundError(f"SEC fact series not found for {taxonomy}:{tag} [{unit}]")
        return {
            "symbol": normalized,
            "taxonomy": taxonomy,
            "tag": tag,
            "unit": unit,
            "label": rows[-1]["label"],
            "description": rows[-1]["description"],
            "rows": rows,
        }
