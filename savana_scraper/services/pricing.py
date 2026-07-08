"""Price parsing helpers — shared by every extraction strategy.

Kept in one place so the "never duplicate logic" rule holds: currency symbols,
thousands separators and stray text are stripped in exactly one function.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Matches the first monetary number in a string: 1,299.00 / 1299 / 1.299,00 ...
_NUMBER_RE = re.compile(r"\d[\d.,\s]*\d|\d")


def parse_price(raw: str | int | float | None) -> Decimal | None:
    """Best-effort parse of a price string into a :class:`Decimal`.

    Returns ``None`` when nothing price-like can be found, rather than raising —
    callers decide whether a missing price is fatal.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None

    match = _NUMBER_RE.search(raw)
    if not match:
        return None
    token = match.group(0).replace(" ", "")

    # Normalise separators. If both '.' and ',' appear, the last one is the
    # decimal separator; the other is a thousands separator.
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            token = token.replace(".", "").replace(",", ".")
        else:
            token = token.replace(",", "")
    elif "," in token:
        # A lone comma: decimal separator only if it looks like ",dd".
        if re.search(r",\d{1,2}$", token):
            token = token.replace(",", ".")
        else:
            token = token.replace(",", "")

    try:
        value = Decimal(token)
    except InvalidOperation:
        return None
    return value if value >= 0 else None
