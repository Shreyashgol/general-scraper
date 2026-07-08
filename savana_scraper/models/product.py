"""Product domain models.

These are the strongly-typed heart of the domain layer. All extraction and
validation converges on :class:`Product`.

Field glossary (as defined by the PRD):
    * name        — Product Name
    * category    — Broad group ("Bags"); may be absent on sites that expose none
    * subcategory — Narrower group within it ("Backpacks"); likewise optional
    * image_url   — Image URL
    * mrp         — Maximum Retail Price (list / struck-through price)
    * asp         — Average Selling Price (the actual price paid)
    * product_url — Product Link

Category and subcategory are deliberately *not* required. Plenty of storefronts
publish no taxonomy at all, and a missing label is honest where an inferred one
("Dress", because the title says so) would be a guess wearing a fact's clothes.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

# CSV column order — the single source of truth for the exporter and tests.
CSV_FIELDS: tuple[str, ...] = (
    "name",
    "category",
    "subcategory",
    "image_url",
    "mrp",
    "asp",
    "product_url",
)


def product_key(url: str | HttpUrl) -> str:
    """The de-duplication / resume identity of a product URL.

    Host + path, with query, fragment and any trailing slash discarded. Defined
    once here so discovery, export, and resume can never disagree on identity.
    """
    parsed = urlparse(str(url))
    return f"{parsed.hostname}{(parsed.path or '').rstrip('/')}"


class ProductRef(BaseModel):
    """A lightweight reference discovered on a listing page.

    Discovery yields these; extraction turns them into full :class:`Product`
    objects. Keeping them separate keeps the two phases decoupled.
    """

    model_config = ConfigDict(frozen=True)

    product_url: HttpUrl

    def key(self) -> str:
        """Stable identity used for de-duplication (URL without query/fragment)."""
        return product_key(self.product_url)


class Product(BaseModel):
    """A fully-extracted product record."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1)
    image_url: HttpUrl
    product_url: HttpUrl
    mrp: Decimal | None = Field(default=None, ge=0)
    asp: Decimal | None = Field(default=None, ge=0)
    category: str | None = None
    subcategory: str | None = None

    @field_validator("name")
    @classmethod
    def _non_blank_name(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must not be blank")
        return v

    def key(self) -> str:
        """De-duplication identity, consistent with :meth:`ProductRef.key`."""
        return product_key(self.product_url)

    def to_row(self) -> dict[str, str]:
        """Flatten to a CSV-ready string row following :data:`CSV_FIELDS`."""
        return {
            "name": self.name,
            "category": self.category or "",
            "subcategory": self.subcategory or "",
            "image_url": str(self.image_url),
            "mrp": "" if self.mrp is None else f"{self.mrp:.2f}",
            "asp": "" if self.asp is None else f"{self.asp:.2f}",
            "product_url": str(self.product_url),
        }
