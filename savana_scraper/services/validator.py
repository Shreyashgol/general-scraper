"""Validation service.

Extraction produces *candidate* data; validation decides whether a candidate
is trustworthy enough to export. Rules live here (not scattered through the
extractor) so the policy is easy to read and change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from savana_scraper.core.logging import get_logger
from savana_scraper.models import Product

log = get_logger(__name__)


@dataclass
class ValidationOutcome:
    """Result of validating one product."""

    product: Product
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class Validator:
    """Applies business rules to an extracted :class:`Product`."""

    def validate(self, product: Product) -> ValidationOutcome:
        outcome = ValidationOutcome(product=product)

        # --- Hard requirements (Green Flag: "Extract all required fields") ----
        if not product.name.strip():
            outcome.errors.append("name is blank")
        if not str(product.image_url):
            outcome.errors.append("image_url is missing")
        if not str(product.product_url):
            outcome.errors.append("product_url is missing")

        # At least one price must be known for the record to be useful.
        if product.mrp is None and product.asp is None:
            outcome.errors.append("no price found (both mrp and asp missing)")

        # --- Soft sanity checks ----------------------------------------------
        if product.mrp is not None and product.asp is not None and product.asp > product.mrp:
            # Selling above list price is suspicious but not fatal — the two may
            # have been swapped by an ambiguous layout.
            outcome.warnings.append(
                f"asp ({product.asp}) > mrp ({product.mrp}); prices may be swapped"
            )
        if product.mrp == 0 or product.asp == 0:
            outcome.warnings.append("a price is zero")

        if not outcome.ok:
            log.debug("Validation failed for %s: %s", product.product_url, outcome.errors)
        return outcome
