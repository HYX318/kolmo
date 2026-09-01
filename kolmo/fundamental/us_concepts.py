"""Explicit SEC tag mappings for the US standardized-financial product."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConceptSpec:
    name: str
    statement: str
    instant: bool
    candidates: tuple[tuple[str, str, str], ...]
    ttm_additive: bool = False
    derive_quarter: bool = True
    expenditure: bool = False


def _usd(name: str, statement: str, tags: tuple[str, ...], **kwargs) -> ConceptSpec:
    return ConceptSpec(
        name, statement, False,
        tuple(("us-gaap", tag, "USD") for tag in tags),
        **kwargs,
    )


def _instant_usd(name: str, tags: tuple[str, ...]) -> ConceptSpec:
    return ConceptSpec(
        name, "balance", True,
        tuple(("us-gaap", tag, "USD") for tag in tags),
        derive_quarter=False,
    )


CONCEPT_SPECS = (
    _usd("revenue", "income", (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenuesNetOfInterestExpense",
        "RegulatedAndUnregulatedOperatingRevenue",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
    ), ttm_additive=True),
    _usd("cost_of_revenue", "income", (
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
        "CostOfGoodsSold",
    ), ttm_additive=True),
    _usd("gross_profit", "income", ("GrossProfit",), ttm_additive=True),
    _usd("operating_income", "income", (
        "OperatingIncomeLoss",
    ), ttm_additive=True),
    _usd("net_income", "income", (
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ), ttm_additive=True),
    _usd("interest_expense", "income", (
        "InterestExpenseNonOperating",
        "InterestAndDebtExpense",
        "InterestExpense",
    ), ttm_additive=True),
    _usd("income_tax_expense", "income", (
        "IncomeTaxExpenseBenefit",
        "CurrentIncomeTaxExpenseBenefit",
    ), ttm_additive=True),
    ConceptSpec(
        "diluted_eps", "income", False,
        (
            ("us-gaap", "EarningsPerShareDiluted", "USD/shares"),
            ("us-gaap", "IncomeLossFromContinuingOperationsPerDilutedShare", "USD/shares"),
        ),
        derive_quarter=False,
    ),
    _instant_usd("cash_and_equivalents", (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks",
    )),
    _instant_usd("short_term_investments", (
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
    )),
    _instant_usd("total_assets", ("Assets",)),
    _instant_usd("current_assets", ("AssetsCurrent",)),
    _instant_usd("total_liabilities", ("Liabilities",)),
    _instant_usd("current_liabilities", ("LiabilitiesCurrent",)),
    _instant_usd("short_term_debt", (
        "ShortTermBorrowings",
        "ShortTermDebtCurrent",
        "LongTermDebtCurrent",
        "LongTermDebtAndFinanceLeaseObligationsCurrent",
    )),
    _instant_usd("long_term_debt", (
        "LongTermDebtNoncurrent",
        "LongTermDebtAndFinanceLeaseObligationsNoncurrent",
        "LongTermDebt",
    )),
    _instant_usd("total_debt", (
        "LongTermDebtAndFinanceLeaseObligations",
        "LongTermDebtAndCapitalLeaseObligations",
    )),
    _instant_usd("stockholders_equity", (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "PartnersCapital",
    )),
    ConceptSpec(
        "shares_outstanding", "balance", True,
        (
            ("dei", "EntityCommonStockSharesOutstanding", "shares"),
            ("us-gaap", "CommonStockSharesOutstanding", "shares"),
        ),
        derive_quarter=False,
    ),
    _usd("operating_cash_flow", "cash_flow", (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ), ttm_additive=True),
    _usd("capital_expenditure", "cash_flow", (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "PaymentsForAdditionsToPropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ), ttm_additive=True, expenditure=True),
    _usd("depreciation_and_amortization", "cash_flow", (
        "DepreciationDepletionAndAmortization",
        "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
        "Depreciation",
    ), ttm_additive=True),
    _usd("stock_based_compensation", "cash_flow", (
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
    ), ttm_additive=True),
    _usd("dividends_paid", "cash_flow", (
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
        "PaymentsOfOrdinaryDividends",
    ), ttm_additive=True, expenditure=True),
    _usd("share_repurchases", "cash_flow", (
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ), ttm_additive=True, expenditure=True),
)

CONCEPT_BY_NAME = {spec.name: spec for spec in CONCEPT_SPECS}
TAG_INDEX = {
    candidate: (spec, priority)
    for spec in CONCEPT_SPECS
    for priority, candidate in enumerate(spec.candidates)
}

FLOW_METRICS = tuple(spec.name for spec in CONCEPT_SPECS if not spec.instant)
INSTANT_METRICS = tuple(spec.name for spec in CONCEPT_SPECS if spec.instant)
TTM_ADDITIVE_METRICS = tuple(spec.name for spec in CONCEPT_SPECS if spec.ttm_additive)

QUALITY_FLAGS = {
    "missing_revenue",
    "missing_net_income",
    "conflicting_candidate_tags",
    "unsupported_custom_taxonomy",
    "period_classification_ambiguous",
    "quarter_derived_from_ytd",
    "q4_derived_from_fy",
    "quarter_derivation_incomplete",
    "ttm_incomplete",
    "future_period_fact_excluded",
    "unlinked_accession",
    "negative_equity",
    "not_applicable_for_business_model",
}

NOT_APPLICABLE = {
    "bank": {
        "cost_of_revenue", "gross_profit", "current_assets", "current_liabilities",
        "operating_cash_flow", "capital_expenditure", "free_cash_flow",
        "gross_margin", "operating_cash_flow_margin", "free_cash_flow_margin", "current_ratio",
    },
    "insurance": {
        "cost_of_revenue", "gross_profit", "current_assets", "current_liabilities",
        "operating_cash_flow", "capital_expenditure", "free_cash_flow",
        "gross_margin", "operating_cash_flow_margin", "free_cash_flow_margin", "current_ratio",
    },
    "reit": {"capital_expenditure", "free_cash_flow", "free_cash_flow_margin"},
    "utility": set(),
    "non_financial": set(),
}


def classify_business_model(sector: str, industry: str) -> str:
    sector_text = sector.strip().lower()
    industry_text = industry.strip().lower()
    if "bank" in industry_text:
        return "bank"
    if "insurance" in industry_text or "financial" in industry_text or "holdings" in industry_text:
        return "insurance"
    if "reit" in industry_text or sector_text == "real estate":
        return "reit"
    if sector_text == "utilities" or "utilit" in industry_text:
        return "utility"
    return "non_financial"
