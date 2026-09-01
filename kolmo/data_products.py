"""Shared data-product contracts for Kolmo outputs."""

from __future__ import annotations

from dataclasses import dataclass


PROFILE_OHLC_CHECK_COLUMNS = [
    "date",
    "symbol",
    "exchange",
    "source",
    "profile_open",
    "source_open",
    "profile_high",
    "source_high",
    "profile_low",
    "source_low",
    "profile_close",
    "source_close",
    "open_diff",
    "high_diff",
    "low_diff",
    "close_diff",
    "status",
]

FINANCIAL_STATEMENT_COLUMNS = [
    "symbol",
    "report_period",
    "announce_date",
    "statement_type",
    "source_code",
    "source_year",
    "source_quarter",
    "source_stat_date",
    "source_pub_date",
    "revenue",
    "net_profit",
    "operating_cash_flow",
    "operating_cash_flow_ratio",
    "operating_cash_flow_ratio_raw_percent",
    "total_assets",
    "total_liabilities",
    "equity",
    "roe",
    "roe_raw_percent",
    "gross_margin",
    "gross_margin_raw_percent",
    "debt_to_assets",
    "debt_to_assets_raw_percent",
]

FINANCIAL_PRIMARY_KEY_COLUMNS = [
    "source_code",
    "source_year",
    "source_quarter",
    "source_stat_date",
    "source_pub_date",
]

SEC_FILING_COLUMNS = [
    "symbol",
    "cik",
    "accession_number",
    "form",
    "filing_date",
    "report_date",
    "accepted_at",
    "primary_document",
    "is_xbrl",
    "is_inline_xbrl",
    "source",
    "retrieved_at",
]

SEC_COMPANY_FACT_COLUMNS = [
    "row_id",
    "symbol",
    "cik",
    "taxonomy",
    "tag",
    "label",
    "description",
    "unit",
    "start_date",
    "end_date",
    "value",
    "filed_date",
    "accepted_at",
    "available_date",
    "form",
    "fiscal_year",
    "fiscal_period",
    "frame",
    "accession_number",
    "source",
    "retrieved_at",
]

US_STANDARDIZED_BASE_METRICS = [
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "interest_expense",
    "income_tax_expense",
    "diluted_eps",
    "cash_and_equivalents",
    "short_term_investments",
    "total_assets",
    "current_assets",
    "total_liabilities",
    "current_liabilities",
    "short_term_debt",
    "long_term_debt",
    "total_debt",
    "stockholders_equity",
    "shares_outstanding",
    "operating_cash_flow",
    "capital_expenditure",
    "free_cash_flow",
    "depreciation_and_amortization",
    "stock_based_compensation",
    "dividends_paid",
    "share_repurchases",
]

US_STANDARDIZED_DERIVED_METRICS = [
    "revenue_yoy",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "operating_cash_flow_margin",
    "free_cash_flow_margin",
    "debt_to_assets",
    "debt_to_equity",
    "net_debt",
    "current_ratio",
    "shares_yoy",
]

US_STANDARDIZED_FINANCIAL_COLUMNS = [
    "symbol",
    "cik",
    "report_period",
    "period_start",
    "period_end",
    "period_type",
    "fiscal_year",
    "fiscal_period",
    "available_date",
    "accepted_at",
    "accession_number",
    "form",
    "business_model",
    "applicability_flags",
    *US_STANDARDIZED_BASE_METRICS,
    *US_STANDARDIZED_DERIVED_METRICS,
    "quality_status",
    "quality_flags",
    "completeness_score",
    "source",
    "built_at",
]

US_STANDARDIZED_PROVENANCE_COLUMNS = [
    "symbol",
    "report_period",
    "period_type",
    "available_date",
    "accession_number",
    "metric",
    "source_taxonomy",
    "source_tag",
    "source_unit",
    "source_row_ids",
    "calculation",
    "component_periods",
    "component_accessions",
]

MARKET_FLOW_DAILY_COLUMNS = [
    "date",
    "symbol",
    "source",
    "northbound_net_amount",
    "margin_balance",
    "short_balance",
    "etf_flow_amount",
    "free_float_shares",
    "unlock_shares",
]

TRADING_CALENDAR_COLUMNS = [
    "date",
    "market",
    "is_trading_day",
    "source",
    "observed_at",
]

SECURITY_MASTER_COLUMNS = [
    "observed_at",
    "symbol",
    "exchange",
    "name",
    "board",
    "listing_date",
    "delisting_date",
    "status_as_of",
    "st_status",
    "base_price_limit_pct",
    "price_limit_rule",
    "price_limit_status",
    "source",
]

INDUSTRY_CLASSIFICATION_COLUMNS = [
    "classification_date",
    "provider_update_date",
    "retrieved_at",
    "symbol",
    "exchange",
    "name",
    "industry",
    "classification",
    "source",
]


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    missing: list[str]
    extra: list[str]


def normalize_date(value: str) -> str:
    parsed = value.replace("-", "")
    if len(parsed) != 8 or not parsed.isdigit():
        raise ValueError("date must be YYYYMMDD or YYYY-MM-DD")
    return parsed


def is_a_share_symbol(symbol: str) -> bool:
    text = symbol.strip().upper()
    if "." not in text:
        return False
    code, exchange = text.split(".", 1)
    if exchange == "SZ":
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    if exchange == "SH":
        return code.startswith(("600", "601", "603", "605", "688"))
    return False


def validate_columns(fieldnames: list[str] | None, required: list[str]) -> ValidationResult:
    actual = set(fieldnames or [])
    expected = set(required)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return ValidationResult(ok=not missing, missing=missing, extra=extra)
