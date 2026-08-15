"""Publish immutable, content-addressed snapshots of A-share daily profiles."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, TextIO, Tuple

from kolmo.paths import data_root as configured_data_root


PRODUCT_ID = "profile_daily"
MANIFEST_SCHEMA_VERSION = "1.0.0"
PROFILE_SCHEMA_VERSION = "1.0.0"
EXCHANGES = ("sz", "sh")
PROFILE_COLUMNS = (
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
)

_DATE_PATTERN = re.compile(r"[0-9]{8}")
_SNAPSHOT_PATTERN = re.compile(r"[0-9a-f]{64}")
_HASH_PATTERN = _SNAPSHOT_PATTERN
_SYMBOL_PATTERNS = {
    "sz": re.compile(r"[0-9]{6}\.SZ"),
    "sh": re.compile(r"[0-9]{6}\.SH"),
}


class ProfileSnapshotError(RuntimeError):
    """Raised when a profile snapshot cannot be safely published or read."""


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ProfileSnapshotError(f"cannot hash object {path}: {exc}") from exc
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, payload: bytes, staging_dir: Path) -> None:
    staging_dir.mkdir(parents=True, exist_ok=True)
    temporary = staging_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(temporary), str(path))
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ProfileSnapshotCatalog:
    """Catalog immutable profile manifests backed by hard-linked content objects."""

    def __init__(self, data_root: Optional[Path] = None) -> None:
        root = configured_data_root() if data_root is None else Path(data_root)
        self.data_root = root.expanduser().resolve()
        self.profile_root = self.data_root / "profile" / "daily"
        self.catalog_root = self.data_root / "catalog"
        self.snapshot_root = self.catalog_root / "snapshots" / PRODUCT_ID
        self.object_root = self.catalog_root / "objects" / PRODUCT_ID
        self.staging_root = self.catalog_root / ".staging" / PRODUCT_ID
        self.current_path = self.snapshot_root / "CURRENT"
        self.lock_path = self.catalog_root / ".profile_daily_publish.lock"

    def profile_path(self, exchange: str, trade_date: str) -> Path:
        normalized_exchange = self._normalize_exchange(exchange)
        normalized_date = self._normalize_date(trade_date)
        return (
            self.profile_root
            / normalized_exchange
            / normalized_date[:4]
            / normalized_date[4:6]
            / f"{normalized_date}.csv"
        )

    def discover_dates(self) -> Tuple[str, ...]:
        """Return dates for which both required exchange partitions exist."""
        dates_by_exchange = []
        for exchange in EXCHANGES:
            dates = set()
            for path in (self.profile_root / exchange).glob("*/*/*.csv"):
                try:
                    trade_date = self._normalize_date(path.stem)
                except ProfileSnapshotError:
                    continue
                if path == self.profile_path(exchange, trade_date):
                    dates.add(trade_date)
            dates_by_exchange.append(dates)
        common_dates = set.intersection(*dates_by_exchange) if dates_by_exchange else set()
        return tuple(sorted(common_dates))

    def latest_dates(self, count: int) -> Tuple[str, ...]:
        if isinstance(count, bool) or count <= 0:
            raise ProfileSnapshotError("latest-days must be a positive integer")
        dates = self.discover_dates()
        if not dates:
            raise ProfileSnapshotError(f"no profile partitions found under {self.profile_root}")
        if len(dates) < count:
            raise ProfileSnapshotError(
                f"requested {count} latest dates but only {len(dates)} complete dates exist"
            )
        return dates[-count:]

    def publish(
        self,
        dates: Optional[Iterable[str]] = None,
        *,
        latest_days: Optional[int] = None,
        promote: bool = True,
    ) -> Dict[str, object]:
        if dates is not None and latest_days is not None:
            raise ProfileSnapshotError("dates and latest_days are mutually exclusive")
        if dates is None:
            selected_dates = self.latest_dates(20 if latest_days is None else latest_days)
        else:
            selected_dates = tuple(sorted({self._normalize_date(value) for value in dates}))
            if not selected_dates:
                raise ProfileSnapshotError("at least one profile date is required")
        all_dates = self.discover_dates()
        selected = set(selected_dates)
        if any(value not in selected for value in all_dates if selected_dates[0] <= value <= selected_dates[-1]):
            raise ProfileSnapshotError(
                "selected dates are not contiguous in the local complete-date calendar"
            )

        with self._publish_lock():
            return self._publish_locked(selected_dates, promote=promote)

    def resolve_current(self) -> str:
        try:
            content = self.current_path.read_text(encoding="ascii")
        except FileNotFoundError as exc:
            raise ProfileSnapshotError(f"CURRENT does not exist: {self.current_path}") from exc
        except OSError as exc:
            raise ProfileSnapshotError(f"cannot read CURRENT {self.current_path}: {exc}") from exc
        lines = content.splitlines()
        if len(lines) != 1 or content != f"{lines[0]}\n":
            raise ProfileSnapshotError(f"invalid CURRENT file: {self.current_path}")
        snapshot_id = self._normalize_snapshot_id(lines[0])
        self.verify_snapshot(snapshot_id)
        return snapshot_id

    def load_manifest(self, snapshot_id: str) -> Dict[str, object]:
        normalized_id = self._normalize_snapshot_id(snapshot_id)
        path = self._manifest_path(normalized_id)
        try:
            with path.open("r", encoding="utf-8") as source:
                manifest = json.load(source)
        except FileNotFoundError as exc:
            raise ProfileSnapshotError(f"unknown profile snapshot: {normalized_id}") from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileSnapshotError(f"cannot load snapshot manifest {path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ProfileSnapshotError(f"snapshot manifest must be a JSON object: {path}")
        self._validate_manifest(manifest, normalized_id)
        return manifest

    def verify_snapshot(self, snapshot_id: str) -> Dict[str, object]:
        manifest = self.load_manifest(snapshot_id)
        partitions = manifest["partitions"]
        assert isinstance(partitions, list)
        verified = []
        observed_adjustments: Set[str] = set()
        for partition in partitions:
            assert isinstance(partition, dict)
            object_path = self._object_path_from_partition(partition)
            expected_hash = str(partition["sha256"])
            actual_hash = _sha256_file(object_path)
            if actual_hash != expected_hash:
                raise ProfileSnapshotError(
                    f"profile object hash mismatch: {object_path}; "
                    f"expected={expected_hash} actual={actual_hash}"
                )
            rows, adjustments = self._validate_csv(
                object_path,
                str(partition["exchange"]),
                str(partition["date"]),
            )
            observed_adjustments.update(adjustments)
            if rows != partition["rows"]:
                raise ProfileSnapshotError(
                    f"profile object row count mismatch: {object_path}; "
                    f"expected={partition['rows']} actual={rows}"
                )
            verified.append(dict(partition))
        if sorted(observed_adjustments) != manifest["adjustments"]:
            raise ProfileSnapshotError(
                "profile object adjustments do not match manifest: "
                f"expected={manifest['adjustments']} actual={sorted(observed_adjustments)}"
            )
        return {
            "snapshot_id": manifest["snapshot_id"],
            "as_of": manifest["as_of"],
            "partitions": verified,
        }

    def partition_paths(self, snapshot_id: str, *, verify: bool = True) -> Tuple[Path, ...]:
        manifest = self.load_manifest(snapshot_id)
        if verify:
            self.verify_snapshot(snapshot_id)
        partitions = manifest["partitions"]
        assert isinstance(partitions, list)
        return tuple(self._object_path_from_partition(item) for item in partitions)

    def _publish_locked(
        self, selected_dates: Sequence[str], *, promote: bool
    ) -> Dict[str, object]:
        staging_dir = self.staging_root / uuid.uuid4().hex
        partitions: List[Dict[str, object]] = []
        quality_results: List[Dict[str, object]] = []
        fetch_evidence: Dict[Tuple[str, str], Optional[Tuple[Path, str]]] = {}
        adjustments: Set[str] = set()
        try:
            staging_dir.mkdir(parents=True, exist_ok=False)
            for trade_date in selected_dates:
                for exchange in EXCHANGES:
                    source_path = self.profile_path(exchange, trade_date)
                    if not source_path.is_file():
                        raise ProfileSnapshotError(
                            f"required {exchange.upper()} profile partition is missing: {source_path}"
                        )
                    evidence_identity = self._validate_partition_fetch_evidence(
                        source_path, exchange, trade_date
                    )
                    fetch_evidence[(trade_date, exchange)] = evidence_identity
                    if evidence_identity is None:
                        quality_results.append(
                            {
                                "level": "RESEARCH_ONLY",
                                "code": "fetch_failure_evidence_missing",
                                "message": (
                                    "profile provenance is absent; fetch completion and no-failure "
                                    "status are unverified"
                                ),
                                "date": trade_date,
                                "exchange": exchange,
                                "path": self._relative_path(self._provenance_path(source_path)),
                            }
                        )
                    else:
                        quality_results.append(
                            {
                                "level": "PASS",
                                "code": "no_fetch_failures",
                                "message": "profile partition is bound to a completed zero-failure fetch run",
                                "date": trade_date,
                                "exchange": exchange,
                                "path": self._relative_path(evidence_identity[0]),
                                "rows": 0,
                            }
                        )
                    temporary_object = staging_dir / f"{trade_date}-{exchange}-{uuid.uuid4().hex}.csv"
                    try:
                        os.link(str(source_path), str(temporary_object))
                    except OSError as exc:
                        raise ProfileSnapshotError(
                            "cannot hard-link profile source into object staging; "
                            f"source and {self.catalog_root} must share a filesystem: {source_path}: {exc}"
                        ) from exc
                    before = temporary_object.stat()
                    rows, partition_adjustments = self._validate_csv(
                        temporary_object, exchange, trade_date
                    )
                    if partition_adjustments != {"qfq"}:
                        raise ProfileSnapshotError(
                            "profile snapshot requires uniform qfq adjustment; "
                            f"{source_path} has {sorted(partition_adjustments)}"
                        )
                    adjustments.update(partition_adjustments)
                    digest = _sha256_file(temporary_object)
                    after = temporary_object.stat()
                    if self._file_identity(before) != self._file_identity(after):
                        raise ProfileSnapshotError(
                            f"profile partition changed during publication: {source_path}"
                        )
                    object_path = self._publish_object(temporary_object, digest)
                    partitions.append(
                        {
                            "date": trade_date,
                            "exchange": exchange,
                            "source_path": self._relative_path(source_path),
                            "object_path": self._relative_path(object_path),
                            "sha256": digest,
                            "rows": rows,
                        }
                    )

            for trade_date in selected_dates:
                for exchange in EXCHANGES:
                    evidence_identity = fetch_evidence[(trade_date, exchange)]
                    if evidence_identity is None:
                        continue
                    evidence_path, evidence_hash = evidence_identity
                    if _sha256_file(evidence_path) != evidence_hash:
                        raise ProfileSnapshotError(
                            f"fetch evidence changed during publication: {evidence_path}"
                        )
            quality_results.append(
                {
                    "level": "PASS",
                    "code": "required_partitions_valid",
                    "message": (
                        f"validated {len(partitions)} SZ/SH profile partitions "
                        f"for {len(selected_dates)} dates"
                    ),
                }
            )
            quality_results.append(
                {
                    "level": "RESEARCH_ONLY",
                    "code": "exchange_calendar_unverified",
                    "message": (
                        "dates are contiguous in the local SZ/SH partition calendar, "
                        "but no independent exchange-calendar snapshot is bound"
                    ),
                }
            )
            if adjustments != {"qfq"}:
                raise ProfileSnapshotError(
                    f"profile snapshot requires adjustment ['qfq']; got {sorted(adjustments)}"
                )
            quality_results.append(
                {
                    "level": "RESEARCH_ONLY",
                    "code": "adjusted_price_research_only",
                    "message": (
                        "qfq prices are suitable for research signals but not trusted cash, "
                        "share, corporate-action, or price-limit accounting"
                    ),
                }
            )
            identity = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "product_id": PRODUCT_ID,
                "product_schema_version": PROFILE_SCHEMA_VERSION,
                "as_of": selected_dates[-1],
                "adjustments": sorted(adjustments),
                "research_only": True,
                "partitions": partitions,
                "quality_results": quality_results,
            }
            snapshot_id = hashlib.sha256(_canonical_json(identity)).hexdigest()
            manifest = dict(identity)
            manifest["snapshot_id"] = snapshot_id
            manifest["created_at"] = datetime.now(timezone.utc).isoformat()
            manifest["manifest_sha256"] = self._manifest_hash(manifest)

            manifest_path = self._manifest_path(snapshot_id)
            if manifest_path.exists():
                existing = self.load_manifest(snapshot_id)
                self.verify_snapshot(snapshot_id)
                if self._manifest_identity(existing) != identity:
                    raise ProfileSnapshotError(
                        f"existing snapshot identity mismatch: {snapshot_id}"
                    )
                manifest = existing
            else:
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                _write_atomic(
                    manifest_path,
                    json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True).encode("utf-8")
                    + b"\n",
                    staging_dir,
                )
                self.verify_snapshot(snapshot_id)
            if promote:
                self._update_current(snapshot_id, staging_dir)
            return manifest
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

    def _publish_object(self, temporary_object: Path, digest: str) -> Path:
        self.object_root.mkdir(parents=True, exist_ok=True)
        object_path = self.object_root / f"{digest}.csv"
        if object_path.exists():
            existing_hash = _sha256_file(object_path)
            if existing_hash != digest:
                raise ProfileSnapshotError(
                    f"existing profile object is corrupt: {object_path}; "
                    f"expected={digest} actual={existing_hash}"
                )
            object_path.chmod(0o444)
            temporary_object.unlink()
            return object_path
        os.replace(str(temporary_object), str(object_path))
        object_path.chmod(0o444)
        _fsync_directory(self.object_root)
        actual_hash = _sha256_file(object_path)
        if actual_hash != digest:
            try:
                object_path.unlink()
            except OSError:
                pass
            raise ProfileSnapshotError(
                f"profile object changed during publication: {object_path}; "
                f"expected={digest} actual={actual_hash}"
            )
        return object_path

    def _update_current(self, snapshot_id: str, staging_dir: Path) -> None:
        try:
            if self.current_path.read_text(encoding="ascii") == f"{snapshot_id}\n":
                return
        except FileNotFoundError:
            pass
        _write_atomic(self.current_path, f"{snapshot_id}\n".encode("ascii"), staging_dir)

    def _validate_csv(
        self, path: Path, exchange: str, trade_date: str
    ) -> Tuple[int, Set[str]]:
        expected_exchange = exchange.upper()
        seen = set()
        adjustments: Set[str] = set()
        try:
            with path.open("r", encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                if tuple(reader.fieldnames or ()) != PROFILE_COLUMNS:
                    raise ProfileSnapshotError(
                        f"profile schema mismatch: {path}; "
                        f"expected={list(PROFILE_COLUMNS)} actual={reader.fieldnames or []}"
                    )
                rows = 0
                for line_number, row in enumerate(reader, start=2):
                    rows += 1
                    if row["date"] != trade_date:
                        raise ProfileSnapshotError(
                            f"profile date mismatch at {path}:{line_number}: {row['date']!r}"
                        )
                    if row["exchange"] != expected_exchange:
                        raise ProfileSnapshotError(
                            f"profile exchange mismatch at {path}:{line_number}: "
                            f"{row['exchange']!r}"
                        )
                    symbol = row["symbol"]
                    if _SYMBOL_PATTERNS[exchange].fullmatch(symbol) is None:
                        raise ProfileSnapshotError(
                            f"profile symbol/exchange mismatch at {path}:{line_number}: {symbol!r}"
                        )
                    if symbol in seen:
                        raise ProfileSnapshotError(
                            f"duplicate profile symbol at {path}:{line_number}: {symbol}"
                        )
                    seen.add(symbol)
                    prices = {}
                    for field in ("open", "high", "low", "close", "preclose"):
                        try:
                            value = float(row[field])
                        except (TypeError, ValueError) as exc:
                            raise ProfileSnapshotError(
                                f"profile {field} must be numeric at {path}:{line_number}"
                            ) from exc
                        if not math.isfinite(value):
                            raise ProfileSnapshotError(
                                f"profile {field} must be finite at {path}:{line_number}"
                            )
                        prices[field] = value
                    if prices["preclose"] <= 0.0:
                        raise ProfileSnapshotError(
                            f"profile preclose must be positive at {path}:{line_number}"
                        )
                    if (
                        prices["high"] < max(prices["open"], prices["close"])
                        or prices["low"] > min(prices["open"], prices["close"])
                        or prices["low"] > prices["high"]
                    ):
                        raise ProfileSnapshotError(
                            f"profile OHLC relationship is invalid at {path}:{line_number}"
                        )
                    try:
                        status_number = float(row["trade_status"])
                    except (TypeError, ValueError) as exc:
                        raise ProfileSnapshotError(
                            f"profile trade_status must be numeric at {path}:{line_number}"
                        ) from exc
                    if (
                        not math.isfinite(status_number)
                        or not status_number.is_integer()
                        or int(status_number) not in (0, 1)
                    ):
                        raise ProfileSnapshotError(
                            f"profile trade_status must be 0 or 1 at {path}:{line_number}"
                        )
                    if int(status_number) == 1 and min(
                        prices["open"], prices["high"], prices["low"], prices["close"]
                    ) <= 0.0:
                        raise ProfileSnapshotError(
                            f"tradable profile OHLC must be positive at {path}:{line_number}"
                        )
                    for field in ("volume", "amount", "turnover_rate", "pct_change"):
                        raw_value = row[field]
                        if int(status_number) == 0 and not (raw_value or "").strip():
                            continue
                        try:
                            activity = float(raw_value)
                        except (TypeError, ValueError) as exc:
                            raise ProfileSnapshotError(
                                f"profile {field} must be numeric at {path}:{line_number}"
                            ) from exc
                        if not math.isfinite(activity):
                            raise ProfileSnapshotError(
                                f"profile {field} must be finite at {path}:{line_number}"
                            )
                        if field != "pct_change" and activity < 0.0:
                            raise ProfileSnapshotError(
                                f"profile {field} must be non-negative at {path}:{line_number}"
                            )
                    raw_adjustment = row["adjust"]
                    adjustment = raw_adjustment.strip().lower() if raw_adjustment else ""
                    if not adjustment:
                        raise ProfileSnapshotError(
                            f"profile adjust must not be empty at {path}:{line_number}"
                        )
                    adjustments.add(adjustment)
                if rows == 0:
                    raise ProfileSnapshotError(f"profile partition is empty: {path}")
                return rows, adjustments
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            raise ProfileSnapshotError(f"cannot validate profile partition {path}: {exc}") from exc

    @staticmethod
    def _provenance_path(profile_path: Path) -> Path:
        return profile_path.with_name(f"{profile_path.name}.provenance.json")

    def _validate_partition_fetch_evidence(
        self, source_path: Path, exchange: str, trade_date: str
    ) -> Optional[Tuple[Path, str]]:
        provenance_path = self._provenance_path(source_path)
        if not provenance_path.exists():
            return None
        if not provenance_path.is_file():
            raise ProfileSnapshotError(f"profile provenance is not a file: {provenance_path}")
        try:
            with provenance_path.open("r", encoding="utf-8") as source:
                provenance = json.load(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileSnapshotError(
                f"cannot read profile provenance {provenance_path}: {exc}"
            ) from exc
        required = {
            "schema_version",
            "product_id",
            "date",
            "exchange",
            "profile_path",
            "profile_sha256",
            "fetch_evidence_path",
            "fetch_evidence_sha256",
        }
        if not isinstance(provenance, dict) or required - set(provenance):
            raise ProfileSnapshotError(f"invalid profile provenance schema: {provenance_path}")
        if provenance["product_id"] != "profile_daily_partition_provenance":
            raise ProfileSnapshotError(f"invalid profile provenance product: {provenance_path}")
        if provenance["date"] != trade_date or provenance["exchange"] != exchange:
            raise ProfileSnapshotError(f"profile provenance does not match partition: {provenance_path}")
        if provenance["profile_path"] != str(source_path.resolve()):
            raise ProfileSnapshotError(f"profile provenance source path mismatch: {provenance_path}")
        if provenance["profile_sha256"] != _sha256_file(source_path):
            raise ProfileSnapshotError(f"profile provenance hash mismatch: {provenance_path}")

        raw_evidence_path = Path(str(provenance["fetch_evidence_path"]))
        evidence_path = (
            raw_evidence_path.resolve()
            if raw_evidence_path.is_absolute()
            else (self.data_root / raw_evidence_path).resolve()
        )
        try:
            evidence_path.relative_to(self.data_root)
        except ValueError as exc:
            raise ProfileSnapshotError(
                f"fetch evidence escapes data root: {evidence_path}"
            ) from exc
        if not evidence_path.is_file():
            raise ProfileSnapshotError(f"fetch evidence does not exist: {evidence_path}")
        evidence_hash = _sha256_file(evidence_path)
        if provenance["fetch_evidence_sha256"] != evidence_hash:
            raise ProfileSnapshotError(f"fetch evidence hash mismatch: {provenance_path}")
        try:
            with evidence_path.open("r", encoding="utf-8") as source:
                evidence = json.load(source)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProfileSnapshotError(f"cannot read fetch evidence {evidence_path}: {exc}") from exc
        try:
            symbols = evidence["symbols"]
            failed = symbols["failed"]
            start_date = self._normalize_date(str(evidence["start_date"]))
            end_date = self._normalize_date(str(evidence["end_date"]))
        except (KeyError, TypeError, ProfileSnapshotError) as exc:
            raise ProfileSnapshotError(f"invalid fetch evidence schema: {evidence_path}") from exc
        if evidence.get("product_id") != "baostock_daily_fetch":
            raise ProfileSnapshotError(f"invalid fetch evidence product: {evidence_path}")
        if evidence.get("exchange") != exchange or not start_date <= trade_date <= end_date:
            raise ProfileSnapshotError(f"fetch evidence does not cover partition: {evidence_path}")
        if evidence.get("status") != "completed" or not isinstance(failed, int) or failed != 0:
            raise ProfileSnapshotError(
                f"fetch evidence is not a completed zero-failure run: {evidence_path}"
            )
        return evidence_path, evidence_hash

    def _validate_manifest(self, manifest: Mapping[str, object], snapshot_id: str) -> None:
        expected_keys = {
            "schema_version",
            "product_id",
            "product_schema_version",
            "snapshot_id",
            "as_of",
            "adjustments",
            "research_only",
            "partitions",
            "quality_results",
            "created_at",
            "manifest_sha256",
        }
        if set(manifest) != expected_keys:
            raise ProfileSnapshotError(
                f"invalid manifest fields for {snapshot_id}: {sorted(set(manifest) ^ expected_keys)}"
            )
        if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
            raise ProfileSnapshotError(
                f"unsupported manifest schema version: {manifest['schema_version']!r}"
            )
        if manifest["product_id"] != PRODUCT_ID:
            raise ProfileSnapshotError(f"unexpected manifest product: {manifest['product_id']!r}")
        if manifest["product_schema_version"] != PROFILE_SCHEMA_VERSION:
            raise ProfileSnapshotError(
                f"unsupported profile schema version: {manifest['product_schema_version']!r}"
            )
        if manifest["snapshot_id"] != snapshot_id:
            raise ProfileSnapshotError(
                f"manifest snapshot id mismatch: expected={snapshot_id} "
                f"actual={manifest['snapshot_id']!r}"
            )
        self._normalize_date(str(manifest["as_of"]))
        if manifest["adjustments"] != ["qfq"]:
            raise ProfileSnapshotError(
                f"published profile snapshot must use adjustments ['qfq']: {manifest['adjustments']!r}"
            )
        if manifest["research_only"] is not True:
            raise ProfileSnapshotError("qfq profile snapshot must set research_only=true")
        partitions = manifest["partitions"]
        if not isinstance(partitions, list) or not partitions:
            raise ProfileSnapshotError("manifest partitions must be a non-empty list")
        expected_partition_keys = {
            "date", "exchange", "source_path", "object_path", "sha256", "rows"
        }
        seen = set()
        for partition in partitions:
            if not isinstance(partition, dict) or set(partition) != expected_partition_keys:
                raise ProfileSnapshotError("manifest contains an invalid partition record")
            trade_date = self._normalize_date(str(partition["date"]))
            exchange = self._normalize_exchange(str(partition["exchange"]))
            key = (trade_date, exchange)
            if key in seen:
                raise ProfileSnapshotError(f"duplicate manifest partition: {key}")
            seen.add(key)
            digest = str(partition["sha256"])
            if _HASH_PATTERN.fullmatch(digest) is None:
                raise ProfileSnapshotError(f"invalid partition hash: {digest!r}")
            if isinstance(partition["rows"], bool) or not isinstance(partition["rows"], int):
                raise ProfileSnapshotError("partition rows must be a positive integer")
            if partition["rows"] <= 0:
                raise ProfileSnapshotError("partition rows must be a positive integer")
            self._object_path_from_partition(partition)
            expected_source = self._relative_path(self.profile_path(exchange, trade_date))
            if partition["source_path"] != expected_source:
                raise ProfileSnapshotError(
                    f"invalid source_path for {key}: {partition['source_path']!r}"
                )
        dates = sorted({item[0] for item in seen})
        if str(manifest["as_of"]) != dates[-1]:
            raise ProfileSnapshotError("manifest as_of does not match its latest partition")
        for trade_date in dates:
            covered = {exchange for date, exchange in seen if date == trade_date}
            if covered != set(EXCHANGES):
                raise ProfileSnapshotError(
                    f"manifest date {trade_date} lacks required exchanges: {sorted(set(EXCHANGES) - covered)}"
                )
        quality_results = manifest["quality_results"]
        if not isinstance(quality_results, list) or not quality_results:
            raise ProfileSnapshotError("manifest quality_results must be a non-empty list")
        allowed_levels = {"PASS", "RESEARCH_ONLY"}
        if any(
            not isinstance(item, dict) or item.get("level") not in allowed_levels
            for item in quality_results
        ):
            raise ProfileSnapshotError(
                "published manifest quality results may contain only PASS or RESEARCH_ONLY"
            )
        research_results = [
            item
            for item in quality_results
            if item.get("level") == "RESEARCH_ONLY"
            and item.get("code") == "adjusted_price_research_only"
        ]
        if len(research_results) != 1:
            raise ProfileSnapshotError(
                "qfq profile snapshot requires one adjusted_price_research_only quality result"
            )
        identity = self._manifest_identity(manifest)
        actual_id = hashlib.sha256(_canonical_json(identity)).hexdigest()
        if actual_id != snapshot_id:
            raise ProfileSnapshotError(
                f"manifest content hash mismatch: expected={snapshot_id} actual={actual_id}"
            )
        if not isinstance(manifest["created_at"], str) or not manifest["created_at"]:
            raise ProfileSnapshotError("manifest created_at must be a non-empty string")
        try:
            created_at = datetime.fromisoformat(manifest["created_at"])
        except ValueError as exc:
            raise ProfileSnapshotError("manifest created_at must be ISO-8601") from exc
        if created_at.tzinfo is None:
            raise ProfileSnapshotError("manifest created_at must include a timezone")
        manifest_hash = manifest["manifest_sha256"]
        if not isinstance(manifest_hash, str) or _HASH_PATTERN.fullmatch(manifest_hash) is None:
            raise ProfileSnapshotError("manifest_sha256 must be a SHA-256 hex digest")
        actual_manifest_hash = self._manifest_hash(manifest)
        if actual_manifest_hash != manifest_hash:
            raise ProfileSnapshotError(
                f"manifest hash mismatch: expected={manifest_hash} actual={actual_manifest_hash}"
            )

    def _manifest_identity(self, manifest: Mapping[str, object]) -> Dict[str, object]:
        return {
            "schema_version": manifest["schema_version"],
            "product_id": manifest["product_id"],
            "product_schema_version": manifest["product_schema_version"],
            "as_of": manifest["as_of"],
            "adjustments": manifest["adjustments"],
            "research_only": manifest["research_only"],
            "partitions": manifest["partitions"],
            "quality_results": manifest["quality_results"],
        }

    @staticmethod
    def _manifest_hash(manifest: Mapping[str, object]) -> str:
        protected = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        return hashlib.sha256(_canonical_json(protected)).hexdigest()

    def _object_path_from_partition(self, partition: Mapping[str, object]) -> Path:
        digest = str(partition.get("sha256", ""))
        expected_relative = Path("catalog") / "objects" / PRODUCT_ID / f"{digest}.csv"
        if partition.get("object_path") != expected_relative.as_posix():
            raise ProfileSnapshotError(
                f"invalid object_path for partition hash {digest}: {partition.get('object_path')!r}"
            )
        object_path = (self.data_root / expected_relative).resolve()
        if object_path.parent != self.object_root.resolve():
            raise ProfileSnapshotError(f"profile object escapes catalog root: {object_path}")
        if not object_path.is_file():
            raise ProfileSnapshotError(f"profile object does not exist: {object_path}")
        return object_path

    def _relative_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.data_root).as_posix()
        except ValueError as exc:
            raise ProfileSnapshotError(f"catalog path is outside data root: {path}") from exc

    def _manifest_path(self, snapshot_id: str) -> Path:
        return self.snapshot_root / snapshot_id / "manifest.json"

    @staticmethod
    def _file_identity(stat_result: os.stat_result) -> Tuple[int, int, int, int]:
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_size,
            stat_result.st_mtime_ns,
        )

    @staticmethod
    def _normalize_date(value: str) -> str:
        if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
            raise ProfileSnapshotError(f"profile date must be YYYYMMDD: {value!r}")
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError as exc:
            raise ProfileSnapshotError(f"invalid profile date: {value}") from exc
        return value

    @staticmethod
    def _normalize_exchange(value: str) -> str:
        if value not in EXCHANGES:
            raise ProfileSnapshotError(f"unsupported profile exchange: {value!r}")
        return value

    @staticmethod
    def _normalize_snapshot_id(value: str) -> str:
        if not isinstance(value, str) or _SNAPSHOT_PATTERN.fullmatch(value) is None:
            raise ProfileSnapshotError(f"invalid profile snapshot id: {value!r}")
        return value

    @contextmanager
    def _publish_lock(self) -> Iterator[None]:
        self.catalog_root.mkdir(parents=True, exist_ok=True)
        try:
            with self.lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise ProfileSnapshotError(f"cannot acquire profile publish lock: {exc}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        default=None,
        help="Kolmo data root. Defaults to KOLMO_DATA_ROOT.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    publish = subparsers.add_parser("publish", help="Publish a validated profile snapshot.")
    selection = publish.add_mutually_exclusive_group()
    selection.add_argument(
        "--latest-days",
        type=int,
        help="Publish the latest N dates (default: 20).",
    )
    selection.add_argument(
        "--dates",
        nargs="+",
        help="Explicit YYYYMMDD dates to publish.",
    )
    publish.add_argument(
        "--no-promote",
        action="store_true",
        help="Publish objects and manifest without changing CURRENT.",
    )

    verify = subparsers.add_parser("verify", help="Verify a snapshot manifest and all objects.")
    verify.add_argument("snapshot_id", nargs="?", help="Defaults to CURRENT.")
    subparsers.add_parser("current", help="Print the current snapshot id.")
    return parser


def main(argv: Optional[Sequence[str]] = None, stdout: TextIO = sys.stdout) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    catalog = ProfileSnapshotCatalog(Path(args.data_root) if args.data_root else None)
    try:
        if args.command == "publish":
            manifest = catalog.publish(
                dates=args.dates,
                latest_days=args.latest_days,
                promote=not args.no_promote,
            )
            result = {"snapshot_id": manifest["snapshot_id"], "as_of": manifest["as_of"]}
            print(json.dumps(result, sort_keys=True), file=stdout)
        elif args.command == "verify":
            if args.snapshot_id:
                snapshot_id = args.snapshot_id
            else:
                snapshot_id = catalog.resolve_current()
                print(
                    json.dumps(
                        {
                            "snapshot_id": snapshot_id,
                            "as_of": catalog.load_manifest(snapshot_id)["as_of"],
                            "verified": True,
                        },
                        sort_keys=True,
                    ),
                    file=stdout,
                )
                return 0
            print(json.dumps(catalog.verify_snapshot(snapshot_id), sort_keys=True), file=stdout)
        else:
            print(catalog.resolve_current(), file=stdout)
        return 0
    except ProfileSnapshotError as exc:
        print(f"profile snapshot error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
