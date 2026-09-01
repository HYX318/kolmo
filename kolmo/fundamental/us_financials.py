"""Point-in-time-safe standardization of canonical SEC Company Facts."""

from __future__ import annotations

import csv
import gzip
import io
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from kolmo.data_products import (
    US_STANDARDIZED_BASE_METRICS,
    US_STANDARDIZED_DERIVED_METRICS,
    US_STANDARDIZED_FINANCIAL_COLUMNS,
    US_STANDARDIZED_PROVENANCE_COLUMNS,
)
from kolmo.fundamental.sec_edgar import read_gzip_csv
from kolmo.fundamental.us_concepts import (
    CONCEPT_BY_NAME,
    FLOW_METRICS,
    INSTANT_METRICS,
    NOT_APPLICABLE,
    TAG_INDEX,
    TTM_ADDITIVE_METRICS,
)


STANDARDIZED_SOURCE = "sec.edgar.standardized"
PERIOD_TYPES = ("quarterly", "annual", "ttm")
CORE_METRICS = ("revenue", "net_income", "operating_cash_flow", "capital_expenditure")


@dataclass(frozen=True)
class MetricValue:
    value: Decimal
    taxonomy: str
    tag: str
    unit: str
    row_ids: tuple[str, ...]
    calculation: str = "direct"
    component_periods: tuple[str, ...] = ()
    component_accessions: tuple[str, ...] = ()


@dataclass
class FactContext:
    accession: str
    available_date: str
    accepted_at: str
    form: str
    end_date: str
    period_class: str
    fiscal_year: str
    fiscal_period: str
    start_date: str = ""
    metrics: dict[str, MetricValue] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)


@dataclass
class StandardizedRow:
    values: dict[str, str]
    provenance: dict[str, MetricValue]
    flags: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class BuildStats:
    input_facts_rows: int
    excluded_future_period_facts: int
    unmapped_tags: int
    conflicting_concepts: int
    ambiguous_period_facts: int
    unsupported_custom_taxonomy: bool


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("financial value must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"invalid SEC numeric value: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"non-finite SEC numeric value: {value!r}")
    return parsed


def classify_period(row: Mapping[str, str]) -> str:
    start_text = str(row.get("start_date", ""))
    if not start_text:
        return "instant"
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(str(row["end_date"]))
    days = (end - start).days + 1
    fiscal_period = str(row.get("fiscal_period", "")).upper()
    form = str(row.get("form", "")).replace("/A", "")
    if 300 <= days <= 390 and (fiscal_period == "FY" or form in {"10-K", "20-F", "40-F"}):
        return "annual"
    if 60 <= days <= 120:
        return "quarter"
    if 150 <= days <= 220:
        return "h1_ytd"
    if 230 <= days <= 300:
        return "nine_month_ytd"
    if 300 <= days <= 390:
        return "annual"
    return "ambiguous"


def _preferred(rows: Sequence[Mapping[str, str]], metric: str) -> tuple[MetricValue, bool]:
    ranked = []
    values: set[Decimal] = set()
    for row in rows:
        spec, priority = TAG_INDEX[(row["taxonomy"], row["tag"], row["unit"])]
        if spec.name != metric:
            continue
        value = _decimal(row["value"])
        values.add(abs(value) if spec.expenditure else value)
        ranked.append((priority, row["start_date"], row["row_id"], row, value))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    _, _, _, chosen, value = ranked[0]
    spec = CONCEPT_BY_NAME[metric]
    calculation = "normalize_positive_outflow" if spec.expenditure and value < 0 else "direct"
    if spec.expenditure:
        value = abs(value)
    same_choice = [
        item[3]["row_id"] for item in ranked
        if item[0] == ranked[0][0] and (abs(item[4]) if spec.expenditure else item[4]) == value
    ]
    return MetricValue(
        value, chosen["taxonomy"], chosen["tag"], chosen["unit"], tuple(sorted(same_choice)),
        calculation, (), (chosen["accession_number"],),
    ), len(values) > 1


def build_contexts(
    facts: Sequence[Mapping[str, str]], filing_accessions: set[str]
) -> tuple[list[FactContext], BuildStats, str]:
    grouped: dict[tuple[str, str, str, str, str], list[Mapping[str, str]]] = defaultdict(list)
    future = 0
    ambiguous = 0
    unmapped_tags: set[tuple[str, str, str]] = set()
    custom_taxonomy = False
    built_at = ""
    for row in facts:
        built_at = max(built_at, str(row.get("retrieved_at", "")))
        key = (str(row["taxonomy"]), str(row["tag"]), str(row["unit"]))
        if key not in TAG_INDEX:
            unmapped_tags.add(key)
            if key[0] not in {"us-gaap", "dei", "ifrs-full"}:
                custom_taxonomy = True
            continue
        if row["end_date"] > row["available_date"]:
            future += 1
            continue
        period_class = classify_period(row)
        if period_class == "ambiguous":
            ambiguous += 1
            continue
        grouped[(
            row["accession_number"], row["available_date"], row["end_date"],
            period_class, row["start_date"],
        )].append(row)

    contexts: list[FactContext] = []
    conflicts = 0
    for (accession, available, end, period_class, context_start), rows in sorted(grouped.items()):
        fiscal_year = Counter(row["fiscal_year"] for row in rows if row["fiscal_year"]).most_common(1)
        fiscal_period = Counter(row["fiscal_period"] for row in rows if row["fiscal_period"]).most_common(1)
        accepted = max((row["accepted_at"] for row in rows if row["accepted_at"]), default="")
        form = Counter(row["form"] for row in rows).most_common(1)[0][0]
        starts = [row["start_date"] for row in rows if row["start_date"]]
        context = FactContext(
            accession, available, accepted, form, end, period_class,
            fiscal_year[0][0] if fiscal_year else "",
            fiscal_period[0][0] if fiscal_period else "",
            context_start or (min(starts) if starts else ""),
        )
        by_metric: dict[str, list[Mapping[str, str]]] = defaultdict(list)
        for row in rows:
            by_metric[TAG_INDEX[(row["taxonomy"], row["tag"], row["unit"])][0].name].append(row)
        for metric, candidates in by_metric.items():
            selected, conflict = _preferred(candidates, metric)
            context.metrics[metric] = selected
            if conflict:
                context.flags.add("conflicting_candidate_tags")
                conflicts += 1
        if accession not in filing_accessions:
            context.flags.add("unlinked_accession")
        contexts.append(context)
    return contexts, BuildStats(
        len(facts), future, len(unmapped_tags), conflicts, ambiguous, custom_taxonomy
    ), built_at


def _empty_values() -> dict[str, str]:
    return {metric: "" for metric in (*US_STANDARDIZED_BASE_METRICS, *US_STANDARDIZED_DERIVED_METRICS)}


def _context_row(context: FactContext, period_type: str, business_model: str, built_at: str) -> StandardizedRow:
    values = _empty_values()
    values.update({
        "symbol": "", "cik": "", "report_period": context.end_date,
        "period_start": context.start_date, "period_end": context.end_date,
        "period_type": period_type, "fiscal_year": context.fiscal_year,
        "fiscal_period": context.fiscal_period, "available_date": context.available_date,
        "accepted_at": context.accepted_at, "accession_number": context.accession,
        "form": context.form, "business_model": business_model,
        "applicability_flags": "", "quality_status": "", "quality_flags": "",
        "completeness_score": "", "source": STANDARDIZED_SOURCE, "built_at": built_at,
    })
    row = StandardizedRow(values, dict(context.metrics), set(context.flags))
    for metric, metric_value in context.metrics.items():
        values[metric] = decimal_text(metric_value.value)
    return row


def _metric_from_parts(
    value: Decimal, parts: Sequence[tuple[str, MetricValue]], calculation: str
) -> MetricValue:
    row_ids = tuple(sorted({row_id for _, part in parts for row_id in part.row_ids}))
    periods = tuple(period for period, _ in parts)
    accessions = tuple(sorted({accession for accession in (
        item for _, part in parts for item in part.component_accessions
    ) if accession}))
    first = parts[0][1]
    return MetricValue(
        value, first.taxonomy, first.tag, first.unit, row_ids, calculation,
        periods, accessions,
    )


def _latest_context(
    contexts: Sequence[FactContext], period_class: str, fiscal_year: str,
    before_end: str, as_of: str, metric: str = "",
) -> FactContext | None:
    candidates = [
        context for context in contexts
        if context.period_class == period_class
        and (not fiscal_year or context.fiscal_year == fiscal_year)
        and context.end_date < before_end
        and context.available_date <= as_of
        and (not metric or metric in context.metrics)
    ]
    return max(candidates, key=lambda item: (item.end_date, item.available_date, item.accepted_at, item.accession), default=None)


def _instant_context(contexts: Sequence[FactContext], accession: str, end: str, as_of: str) -> FactContext | None:
    same_accession = [
        item for item in contexts
        if item.period_class == "instant" and item.accession == accession and item.end_date == end
    ]
    if same_accession:
        return max(same_accession, key=lambda item: (item.available_date, item.accession))
    candidates = [
        item for item in contexts
        if item.period_class == "instant" and item.end_date == end and item.available_date <= as_of
    ]
    return max(candidates, key=lambda item: (item.available_date, item.accepted_at, item.accession), default=None)


def _merge_instant(row: StandardizedRow, context: FactContext | None) -> None:
    if not context:
        return
    for metric in INSTANT_METRICS:
        if metric in context.metrics:
            row.provenance[metric] = context.metrics[metric]
            row.values[metric] = decimal_text(context.metrics[metric].value)
    row.flags.update(context.flags)


def build_annual_rows(
    contexts: Sequence[FactContext], symbol: str, cik: str, business_model: str, built_at: str
) -> list[StandardizedRow]:
    rows: list[StandardizedRow] = []
    for context in contexts:
        if context.period_class != "annual":
            continue
        if context.form.replace("/A", "") not in {"10-K", "20-F", "40-F"} and context.fiscal_period != "FY":
            continue
        row = _context_row(context, "annual", business_model, built_at)
        row.values["symbol"], row.values["cik"] = symbol, cik
        row.values["fiscal_period"] = "FY"
        _merge_instant(row, _instant_context(contexts, context.accession, context.end_date, context.available_date))
        rows.append(row)
    rows = _dedupe_rows(rows)
    by_period: dict[str, list[StandardizedRow]] = defaultdict(list)
    for row in rows:
        by_period[row.values["report_period"]].append(row)
    for versions in by_period.values():
        earliest = min(versions, key=lambda item: (
            item.values["available_date"], item.values["accepted_at"], item.values["accession_number"]
        ))
        fiscal_year = earliest.values["fiscal_year"]
        for row in versions:
            row.values["fiscal_year"] = fiscal_year
            row.values["fiscal_period"] = "FY"
    return rows


def _quarter_label(contexts: Sequence[FactContext]) -> str:
    labels = [item.fiscal_period.upper() for item in contexts if item.fiscal_period]
    if "Q1" in labels:
        return "Q1"
    if "Q2" in labels or any(item.period_class == "h1_ytd" for item in contexts):
        return "Q2"
    if "Q3" in labels or any(item.period_class == "nine_month_ytd" for item in contexts):
        return "Q3"
    if "FY" in labels:
        return "Q4"
    return ""


def build_quarterly_rows(
    contexts: Sequence[FactContext], annual_rows: Sequence[StandardizedRow],
    symbol: str, cik: str, business_model: str, built_at: str,
) -> list[StandardizedRow]:
    events: dict[tuple[str, str, str], list[FactContext]] = defaultdict(list)
    for context in contexts:
        if context.period_class in {"quarter", "h1_ytd", "nine_month_ytd", "instant"}:
            events[(context.accession, context.available_date, context.end_date)].append(context)
    rows: list[StandardizedRow] = []
    for (_, _, _), event_contexts in sorted(events.items(), key=lambda item: item[0][1:]):
        flow_contexts = [item for item in event_contexts if item.period_class != "instant"]
        if not flow_contexts:
            continue
        label = _quarter_label(event_contexts)
        if label not in {"Q1", "Q2", "Q3"}:
            continue
        direct = max(
            (item for item in flow_contexts if item.period_class == "quarter"),
            key=lambda item: (len(item.metrics), item.start_date), default=None,
        )
        cumulative_class = "h1_ytd" if label == "Q2" else "nine_month_ytd" if label == "Q3" else "quarter"
        cumulative = max(
            (item for item in flow_contexts if item.period_class == cumulative_class),
            key=lambda item: (len(item.metrics), item.start_date), default=direct,
        )
        anchor = direct or cumulative or event_contexts[0]
        row = _context_row(anchor, "quarterly", business_model, built_at)
        row.values["symbol"], row.values["cik"], row.values["fiscal_period"] = symbol, cik, label
        row.provenance = {}
        for metric in (*FLOW_METRICS, *INSTANT_METRICS):
            row.values[metric] = ""
        for metric in FLOW_METRICS:
            if direct and metric in direct.metrics:
                row.provenance[metric] = direct.metrics[metric]
                row.values[metric] = decimal_text(direct.metrics[metric].value)
                continue
            if cumulative and metric in cumulative.metrics and CONCEPT_BY_NAME[metric].derive_quarter:
                if label == "Q1":
                    row.provenance[metric] = cumulative.metrics[metric]
                    row.values[metric] = decimal_text(cumulative.metrics[metric].value)
                else:
                    previous_class = "quarter" if label == "Q2" else "h1_ytd"
                    previous = _latest_context(
                        contexts, previous_class, cumulative.fiscal_year,
                        cumulative.end_date, cumulative.available_date, metric,
                    )
                    if previous and metric in previous.metrics and previous.metrics[metric].unit == cumulative.metrics[metric].unit:
                        value = cumulative.metrics[metric].value - previous.metrics[metric].value
                        part = _metric_from_parts(
                            value,
                            ((cumulative.end_date, cumulative.metrics[metric]), (previous.end_date, previous.metrics[metric])),
                            "ytd_difference",
                        )
                        row.provenance[metric] = part
                        row.values[metric] = decimal_text(value)
                        row.flags.add("quarter_derived_from_ytd")
                        if not direct:
                            row.values["period_start"] = (
                                date.fromisoformat(previous.end_date) + timedelta(days=1)
                            ).isoformat()
        if cumulative and label in {"Q2", "Q3"} and any(
            metric in cumulative.metrics
            and CONCEPT_BY_NAME[metric].derive_quarter
            and not row.values[metric]
            for metric in FLOW_METRICS
        ):
            row.flags.add("quarter_derivation_incomplete")
        _merge_instant(row, _instant_context(contexts, anchor.accession, anchor.end_date, anchor.available_date))
        row.flags.update(*(item.flags for item in event_contexts))
        if not any(row.values[metric] for metric in (*FLOW_METRICS, *INSTANT_METRICS)):
            continue
        rows.append(row)

    # Comparative facts carry the filing's fiscal focus, not necessarily their own.
    # Freeze identities before Q4 derivation so later comparison disclosures cannot
    # relabel an already observed quarter.
    normalize_quarter_identities(rows, annual_rows)

    # Q4 appears only when the annual filing is public. Direct quarter facts win;
    # otherwise each additive flow is FY minus the latest Q1/Q2/Q3 versions known then.
    for annual in annual_rows:
        as_of = annual.values["available_date"]
        fiscal_year = annual.values["fiscal_year"]
        report_end = annual.values["report_period"]
        direct_context = next((
            item for item in contexts
            if item.period_class == "quarter" and item.accession == annual.values["accession_number"]
            and item.end_date == report_end
        ), None)
        anchor = FactContext(
            annual.values["accession_number"], as_of, annual.values["accepted_at"], annual.values["form"],
            report_end, "quarter", fiscal_year, "Q4", "",
            dict(direct_context.metrics) if direct_context else {}, set(annual.flags),
        )
        q4 = _context_row(anchor, "quarterly", business_model, built_at)
        q4.values["symbol"], q4.values["cik"], q4.values["fiscal_period"] = symbol, cik, "Q4"
        q4.provenance = dict(anchor.metrics)
        for metric, value in anchor.metrics.items():
            q4.values[metric] = decimal_text(value.value)
        prior_period_by_label: dict[str, str] = {}
        for label in ("Q1", "Q2", "Q3"):
            candidates = [
                item for item in rows
                if item.values["fiscal_year"] == fiscal_year
                and item.values["fiscal_period"] == label
                and item.values["report_period"] < report_end
                and item.values["available_date"] <= as_of
            ]
            if candidates:
                prior_period_by_label[label] = max(item.values["report_period"] for item in candidates)
        for metric in FLOW_METRICS:
            if q4.values[metric] or not CONCEPT_BY_NAME[metric].derive_quarter:
                continue
            if not annual.values[metric] or len(prior_period_by_label) != 3:
                continue
            metric_quarters: dict[str, StandardizedRow] = {}
            for label, period in prior_period_by_label.items():
                versions = [
                    item for item in rows
                    if item.values["report_period"] == period
                    and item.values["available_date"] <= as_of
                    and item.values[metric]
                ]
                if versions:
                    metric_quarters[label] = max(versions, key=lambda item: (
                        item.values["available_date"], item.values["accepted_at"],
                        item.values["accession_number"],
                    ))
            if len(metric_quarters) != 3:
                continue
            annual_value = Decimal(annual.values[metric])
            quarter_values = [Decimal(metric_quarters[label].values[metric]) for label in ("Q1", "Q2", "Q3")]
            value = annual_value - sum(quarter_values, Decimal(0))
            parts = [(report_end, annual.provenance[metric])] + [
                (metric_quarters[label].values["report_period"], metric_quarters[label].provenance[metric])
                for label in ("Q1", "Q2", "Q3")
            ]
            q4.provenance[metric] = _metric_from_parts(value, parts, "fy_less_q1_q2_q3")
            q4.values[metric] = decimal_text(value)
            q4.flags.add("q4_derived_from_fy")
        if prior_period_by_label.get("Q3"):
            q4.values["period_start"] = (
                date.fromisoformat(prior_period_by_label["Q3"]) + timedelta(days=1)
            ).isoformat()
        _merge_instant(q4, _instant_context(contexts, anchor.accession, report_end, as_of))
        if any(q4.values[metric] for metric in (*FLOW_METRICS, *INSTANT_METRICS)):
            if any(annual.values[metric] and not q4.values[metric] for metric in CORE_METRICS):
                q4.flags.add("quarter_derivation_incomplete")
            rows.append(q4)
    rows = _dedupe_rows(rows)
    normalize_quarter_identities(rows, annual_rows)
    return rows


def normalize_quarter_identities(
    rows: Sequence[StandardizedRow], annual_rows: Sequence[StandardizedRow]
) -> None:
    """Assign a stable fiscal identity to each economic report period.

    SEC ``fy``/``fp`` describe the filing focus and are frequently copied onto
    prior-year comparison facts.  Annual endpoints are authoritative anchors;
    otherwise the earliest disclosure of a period is retained.
    """
    by_period: dict[str, list[StandardizedRow]] = defaultdict(list)
    for row in rows:
        by_period[row.values["report_period"]].append(row)
    if not by_period:
        return

    identities: dict[str, tuple[str, str]] = {}
    for period, versions in by_period.items():
        earliest = min(
            versions,
            key=lambda item: (
                item.values["available_date"], item.values["accepted_at"],
                item.values["accession_number"],
            ),
        )
        identities[period] = (earliest.values["fiscal_year"], earliest.values["fiscal_period"])

    annual_anchors: dict[str, str] = {}
    for annual in annual_rows:
        period = annual.values["report_period"]
        fy = annual.values["fiscal_year"]
        if period in by_period and fy and period not in annual_anchors:
            annual_anchors[period] = fy
            identities[period] = (fy, "Q4")

    periods = sorted(by_period)
    anchor_periods = sorted(annual_anchors)
    previous_anchor = ""
    for anchor in anchor_periods:
        candidates = [period for period in periods if previous_anchor < period <= anchor]
        # A regular or 52/53-week fiscal year has four report endpoints.  When
        # history is incomplete, anchor only the suffix rather than inventing data.
        suffix = candidates[-4:]
        if len(suffix) == 4 and 250 <= (
            date.fromisoformat(suffix[-1]) - date.fromisoformat(suffix[0])
        ).days <= 430:
            fy = annual_anchors[anchor]
            for label, period in zip(("Q1", "Q2", "Q3", "Q4"), suffix):
                identities[period] = (fy, label)
        previous_anchor = anchor

    # Extend the last reliable annual anchor into the current incomplete year.
    if anchor_periods:
        last = anchor_periods[-1]
        fy_text = annual_anchors[last]
        try:
            next_fy = str(int(fy_text) + 1)
        except ValueError:
            next_fy = fy_text
        following = [period for period in periods if period > last][:3]
        prior = date.fromisoformat(last)
        for index, period in enumerate(following, start=1):
            current = date.fromisoformat(period)
            if not 45 <= (current - prior).days <= 150:
                break
            identities[period] = (next_fy, f"Q{index}")
            prior = current

    for period, versions in by_period.items():
        fiscal_year, fiscal_period = identities[period]
        for row in versions:
            row.values["fiscal_year"] = fiscal_year
            row.values["fiscal_period"] = fiscal_period


def _row_key(row: StandardizedRow) -> tuple[str, str, str, str, str]:
    values = row.values
    return (
        values["symbol"], values["report_period"], values["period_type"],
        values["available_date"], values["accession_number"],
    )


def _dedupe_rows(rows: Iterable[StandardizedRow]) -> list[StandardizedRow]:
    by_key: dict[tuple[str, str, str, str, str], StandardizedRow] = {}
    for row in rows:
        key = _row_key(row)
        previous = by_key.get(key)
        if previous:
            for metric, value in row.provenance.items():
                if metric not in previous.provenance:
                    previous.provenance[metric] = value
                    previous.values[metric] = row.values[metric]
            previous.flags.update(row.flags)
        else:
            by_key[key] = row
    return sorted(by_key.values(), key=_row_key)


def _consecutive_quarters(rows: Sequence[StandardizedRow]) -> bool:
    if len(rows) != 4:
        return False
    ends = [date.fromisoformat(row.values["report_period"]) for row in rows]
    gaps = [(right - left).days for left, right in zip(ends, ends[1:])]
    if any(gap < 45 or gap > 150 for gap in gaps) or not 250 <= (ends[-1] - ends[0]).days <= 430:
        return False
    labels = [row.values["fiscal_period"] for row in rows]
    order = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 3}
    if all(label in order for label in labels):
        return all(order[right] == (order[left] + 1) % 4 for left, right in zip(labels, labels[1:]))
    return True


def build_ttm_rows(
    quarterly: Sequence[StandardizedRow], symbol: str, cik: str, business_model: str, built_at: str
) -> list[StandardizedRow]:
    events = sorted({(row.values["available_date"], row.values["accepted_at"], row.values["accession_number"]) for row in quarterly})
    output: list[StandardizedRow] = []
    for available, accepted, accession in events:
        visible = [row for row in quarterly if row.values["available_date"] <= available]
        by_period: dict[str, list[StandardizedRow]] = defaultdict(list)
        for item in visible:
            by_period[item.values["report_period"]].append(item)
        periods = sorted(by_period)
        if len(periods) < 4:
            continue
        component_periods = periods[-4:]
        components = [min(
            by_period[period],
            key=lambda item: (
                item.values["available_date"], item.values["accepted_at"],
                item.values["accession_number"],
            ),
        ) for period in component_periods]
        if not _consecutive_quarters(components):
            continue
        if not any(item.values["accession_number"] == accession and item.values["report_period"] in component_periods for item in visible if item.values["available_date"] == available):
            continue
        latest_versions = by_period[component_periods[-1]]
        latest = max(latest_versions, key=lambda item: (
            item.values["available_date"], item.values["accepted_at"], item.values["accession_number"]
        ))
        context = FactContext(
            accession, available, accepted, latest.values["form"], latest.values["report_period"],
            "ttm", latest.values["fiscal_year"], latest.values["fiscal_period"],
            components[0].values["period_start"],
        )
        row = _context_row(context, "ttm", business_model, built_at)
        row.values["symbol"], row.values["cik"] = symbol, cik
        row.values["period_start"] = components[0].values["period_start"]
        for metric in TTM_ADDITIVE_METRICS:
            metric_components: list[StandardizedRow] = []
            for period in component_periods:
                candidates = [item for item in by_period[period] if item.values[metric]]
                if not candidates:
                    break
                metric_components.append(max(candidates, key=lambda item: (
                    item.values["available_date"], item.values["accepted_at"],
                    item.values["accession_number"],
                )))
            if len(metric_components) == 4:
                value = sum((Decimal(item.values[metric]) for item in metric_components), Decimal(0))
                parts = [(item.values["report_period"], item.provenance[metric]) for item in metric_components]
                metric_value = _metric_from_parts(value, parts, "sum_four_quarters")
                metric_value = MetricValue(
                    metric_value.value, metric_value.taxonomy, metric_value.tag, metric_value.unit,
                    metric_value.row_ids, metric_value.calculation, metric_value.component_periods,
                    tuple(item.values["accession_number"] for item in metric_components),
                )
                row.provenance[metric] = metric_value
                row.values[metric] = decimal_text(value)
        for metric in INSTANT_METRICS:
            candidates = [item for item in latest_versions if item.values[metric]]
            if candidates:
                metric_row = max(candidates, key=lambda item: (
                    item.values["available_date"], item.values["accepted_at"],
                    item.values["accession_number"],
                ))
                row.values[metric] = metric_row.values[metric]
                row.provenance[metric] = metric_row.provenance[metric]
        if not all(row.values[metric] for metric in ("revenue", "net_income")):
            row.flags.add("ttm_incomplete")
        output.append(row)
    return _dedupe_rows(output)


def _derived_metric(row: StandardizedRow, name: str, value: Decimal, inputs: Sequence[str], calculation: str) -> None:
    parts = [(row.values["report_period"], row.provenance[item]) for item in inputs]
    row.provenance[name] = _metric_from_parts(value, parts, calculation)
    row.values[name] = decimal_text(value)


def _safe_ratio(row: StandardizedRow, name: str, numerator: str, denominator: str) -> None:
    if not row.values[numerator] or not row.values[denominator]:
        return
    divisor = Decimal(row.values[denominator])
    if divisor == 0:
        return
    with localcontext() as context:
        context.prec = 28
        value = Decimal(row.values[numerator]) / divisor
    _derived_metric(row, name, value, (numerator, denominator), f"{numerator}/{denominator}")


def calculate_common_metrics(rows: Sequence[StandardizedRow], business_model: str) -> None:
    not_applicable = NOT_APPLICABLE[business_model]
    for row in rows:
        for metric in sorted(not_applicable):
            row.values[metric] = ""
            row.provenance.pop(metric, None)
        if not_applicable:
            row.flags.add("not_applicable_for_business_model")
            row.values["applicability_flags"] = ";".join(f"not_applicable:{metric}" for metric in sorted(not_applicable))
        if not row.values["total_debt"] and row.values["short_term_debt"] and row.values["long_term_debt"]:
            value = Decimal(row.values["short_term_debt"]) + Decimal(row.values["long_term_debt"])
            _derived_metric(row, "total_debt", value, ("short_term_debt", "long_term_debt"), "short_term_debt+long_term_debt")
        if "free_cash_flow" not in not_applicable and row.values["operating_cash_flow"] and row.values["capital_expenditure"]:
            value = Decimal(row.values["operating_cash_flow"]) - Decimal(row.values["capital_expenditure"])
            _derived_metric(row, "free_cash_flow", value, ("operating_cash_flow", "capital_expenditure"), "operating_cash_flow-capital_expenditure")
        for name, numerator, denominator in (
            ("gross_margin", "gross_profit", "revenue"),
            ("operating_margin", "operating_income", "revenue"),
            ("net_margin", "net_income", "revenue"),
            ("operating_cash_flow_margin", "operating_cash_flow", "revenue"),
            ("free_cash_flow_margin", "free_cash_flow", "revenue"),
            ("debt_to_assets", "total_debt", "total_assets"),
            ("debt_to_equity", "total_debt", "stockholders_equity"),
            ("current_ratio", "current_assets", "current_liabilities"),
        ):
            if name not in not_applicable:
                _safe_ratio(row, name, numerator, denominator)
        if row.values["total_debt"] and row.values["cash_and_equivalents"]:
            value = Decimal(row.values["total_debt"]) - Decimal(row.values["cash_and_equivalents"])
            inputs = ["total_debt", "cash_and_equivalents"]
            if row.values["short_term_investments"]:
                value -= Decimal(row.values["short_term_investments"])
                inputs.append("short_term_investments")
            _derived_metric(row, "net_debt", value, inputs, "total_debt-cash-short_term_investments")

    for row in rows:
        current_end = date.fromisoformat(row.values["report_period"])
        for metric, output in (("revenue", "revenue_yoy"), ("shares_outstanding", "shares_yoy")):
            if not row.values[metric]:
                continue
            candidates = []
            for prior in rows:
                if prior is row or prior.values["period_type"] != row.values["period_type"] or not prior.values[metric]:
                    continue
                if prior.values["available_date"] > row.values["available_date"]:
                    continue
                day_gap = (current_end - date.fromisoformat(prior.values["report_period"])).days
                if 330 <= day_gap <= 400 and (
                    row.values["period_type"] != "quarterly"
                    or prior.values["fiscal_period"] == row.values["fiscal_period"]
                ):
                    candidates.append((abs(day_gap - 365), prior.values["available_date"], prior))
            if not candidates:
                continue
            prior = min(candidates, key=lambda item: (item[0], -int(item[1].replace("-", ""))))[2]
            denominator = Decimal(prior.values[metric])
            if denominator == 0:
                continue
            with localcontext() as context:
                context.prec = 28
                value = Decimal(row.values[metric]) / denominator - Decimal(1)
            parts = ((row.values["report_period"], row.provenance[metric]), (prior.values["report_period"], prior.provenance[metric]))
            row.provenance[output] = _metric_from_parts(value, parts, "year_over_year")
            row.values[output] = decimal_text(value)


def finalize_quality(rows: Sequence[StandardizedRow], stats: BuildStats, business_model: str) -> None:
    applicable = [metric for metric in US_STANDARDIZED_BASE_METRICS if metric not in NOT_APPLICABLE[business_model]]
    for row in rows:
        if not row.values["revenue"] and "revenue" in applicable:
            row.flags.add("missing_revenue")
        if not row.values["net_income"] and "net_income" in applicable:
            row.flags.add("missing_net_income")
        if row.values["stockholders_equity"] and Decimal(row.values["stockholders_equity"]) < 0:
            row.flags.add("negative_equity")
        present = sum(bool(row.values[metric]) for metric in applicable)
        row.values["completeness_score"] = decimal_text(Decimal(present) / Decimal(len(applicable))) if applicable else ""
        row.values["quality_flags"] = ";".join(sorted(row.flags))
        row.values["quality_status"] = "warning" if row.flags else "observed"


def standardize_symbol(
    symbol: str,
    cik: str,
    facts: Sequence[Mapping[str, str]],
    filings: Sequence[Mapping[str, str]],
    business_model: str,
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]], BuildStats]:
    contexts, stats, built_at = build_contexts(facts, {row["accession_number"] for row in filings})
    annual = build_annual_rows(contexts, symbol, cik, business_model, built_at)
    quarterly = build_quarterly_rows(contexts, annual, symbol, cik, business_model, built_at)
    ttm = build_ttm_rows(quarterly, symbol, cik, business_model, built_at)
    all_rows = [*quarterly, *annual, *ttm]
    calculate_common_metrics(all_rows, business_model)
    finalize_quality(all_rows, stats, business_model)
    products = {
        "quarterly": [row.values for row in sorted(quarterly, key=_row_key)],
        "annual": [row.values for row in sorted(annual, key=_row_key)],
        "ttm": [row.values for row in sorted(ttm, key=_row_key)],
    }
    provenance: list[dict[str, str]] = []
    for row in sorted(all_rows, key=_row_key):
        for metric, source in sorted(row.provenance.items()):
            provenance.append({
                "symbol": symbol,
                "report_period": row.values["report_period"],
                "period_type": row.values["period_type"],
                "available_date": row.values["available_date"],
                "accession_number": row.values["accession_number"],
                "metric": metric,
                "source_taxonomy": source.taxonomy,
                "source_tag": source.tag,
                "source_unit": source.unit,
                "source_row_ids": ";".join(source.row_ids),
                "calculation": source.calculation,
                "component_periods": ";".join(source.component_periods),
                "component_accessions": ";".join(source.component_accessions),
            })
    return products, provenance, stats


def standardized_path(output_root: Path, period_type: str, symbol: str) -> Path:
    if period_type not in PERIOD_TYPES:
        raise ValueError(f"invalid period_type: {period_type}")
    return output_root / period_type / f"{symbol}.csv.gz"


def provenance_path(output_root: Path, symbol: str) -> Path:
    return output_root / "provenance" / f"{symbol}.csv.gz"


def _prepare_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)
            raw.flush()
            os.fsync(raw.fileno())
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def write_standardized_atomic(
    output_root: Path, symbol: str, products: Mapping[str, Sequence[Mapping[str, str]]],
    provenance: Sequence[Mapping[str, str]],
) -> dict[str, Path]:
    targets = {period_type: standardized_path(output_root, period_type, symbol) for period_type in PERIOD_TYPES}
    targets["provenance"] = provenance_path(output_root, symbol)
    prepared: dict[str, Path] = {}
    try:
        for period_type in PERIOD_TYPES:
            prepared[period_type] = _prepare_csv(targets[period_type], US_STANDARDIZED_FINANCIAL_COLUMNS, list(products[period_type]))
        prepared["provenance"] = _prepare_csv(targets["provenance"], US_STANDARDIZED_PROVENANCE_COLUMNS, list(provenance))
        for name in (*PERIOD_TYPES, "provenance"):
            os.replace(prepared[name], targets[name])
    finally:
        for temporary in prepared.values():
            temporary.unlink(missing_ok=True)
    return targets


def read_standardized(path: Path) -> list[dict[str, str]]:
    return read_gzip_csv(path, US_STANDARDIZED_FINANCIAL_COLUMNS)


def latest_metrics_as_of(
    symbol: str,
    as_of_date: str,
    period_type: str = "ttm",
    output_root: Path | None = None,
    max_age_days: int | None = 180,
    include_stale: bool = False,
) -> dict[str, str] | None:
    as_of = date.fromisoformat(as_of_date)
    if max_age_days is not None and max_age_days < 0:
        raise ValueError("max_age_days must be non-negative or None")
    if output_root is None:
        from kolmo.paths import data_path
        output_root = data_path("fundamental", "us", "standardized")
    rows = read_standardized(standardized_path(output_root, period_type, symbol.strip().upper()))
    visible = [row for row in rows if row["available_date"] <= as_of_date]
    if not visible:
        return None
    latest_period = max(row["report_period"] for row in visible)
    versions = [row for row in visible if row["report_period"] == latest_period]
    result = dict(max(versions, key=lambda row: (row["available_date"], row["accepted_at"], row["accession_number"])))
    age_days = (as_of - date.fromisoformat(result["report_period"])).days
    is_stale = max_age_days is not None and age_days > max_age_days
    result["age_days"] = str(age_days)
    result["is_stale"] = "1" if is_stale else "0"
    if is_stale and not include_stale:
        return None
    return result
