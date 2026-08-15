"""A-share board classification from exchange and security code."""

from __future__ import annotations


def board_for_symbol(symbol: str) -> str:
    """Classify an A-share symbol into a coarse board bucket.

    This is a code-prefix classification, not an industry or index membership
    source. It is suitable for broad universe filters such as ChiNext or STAR.
    """
    text = symbol.strip().upper()
    if "." not in text:
        return ""
    code, exchange = text.split(".", 1)
    if len(code) != 6 or not code.isdigit():
        return ""

    if exchange == "SZ":
        if code.startswith(("300", "301")):
            return "chi_next"
        if code.startswith("002"):
            return "sme"
        if code.startswith("200"):
            return "b_share"
        if code.startswith(("000", "001", "003")):
            return "main"
        return "other"

    if exchange == "SH":
        if code.startswith("688"):
            return "star"
        if code.startswith("900"):
            return "b_share"
        if code.startswith(("600", "601", "603", "605")):
            return "main"
        return "other"

    if exchange == "BJ":
        return "beijing"

    return ""
