"""Savana category taxonomy — numeric ids to human names.

savana.com's product-detail SSR payload carries a real four-level taxonomy
(``level1CatId`` … ``level4CatId``), but exposes it *only as integers*. Nothing
on the site resolves them: the ``CATEGORY`` goods-flow is a legal ``flowType``
yet answers ``QUERY Category info error`` for every id, and no other endpoint
returns a name. So the mapping has to live here.

We take levels 2 and 3, which is where the useful distinction sits:

    level1  2    — the whole storefront (womenswear). Constant; carries nothing.
    level2  11   — Bags          → **category**
    level3  57   — Backpacks     → **subcategory**
    level4  341  — finer than any shopper's mental model.

The names below were not invented. Each was read off the site's own labels: a
sample of products from every category tile on the homepage was pulled, and each
id was named by the tile it appeared under (``level2 750`` only ever under the
"Denim" tile) or by the noun its product names share (``level3 57`` is
"Kitty School Backpack", "Solid School Backpack", …). ``level2 12`` spans the
Dresses, TOPS, Bottoms, T-Shirts and Co-ords tiles, so it is "Clothing".

An id we have never seen is *not* guessed at. It exports as ``cat:<id>`` and the
run reports it, so a new savana category shows up as an obvious gap rather than
as a plausible wrong label. Override or extend the map without touching code by
pointing ``SAVANA_TAXONOMY_PATH`` at a JSON file::

    {"level2": {"999": "Footwear"}, "level3": {"1000": "Sneakers"}}
"""

from __future__ import annotations

import json
from pathlib import Path

from savana_scraper.core.logging import get_logger

log = get_logger(__name__)

#: Rendered for an id absent from the map, e.g. ``cat:912``.
UNKNOWN_PREFIX = "cat:"

#: ``level2CatId`` → category. Named after the homepage tile each id appears under.
LEVEL2_NAMES: dict[int, str] = {
    9: "Accessories",
    10: "Jewelry",
    11: "Bags",
    12: "Clothing",
    14: "Beauty",
    83: "Loungewear",
    729: "Lingerie",
    750: "Denim",
}

#: ``level3CatId`` → subcategory. Named after the noun shared by its products.
LEVEL3_NAMES: dict[int, str] = {
    43: "Eyewear",
    51: "Phone Cases",
    54: "Tote Bags",
    57: "Backpacks",
    61: "Necklaces",
    62: "Earrings",
    63: "Bracelets",
    64: "Rings",
    67: "Nose Rings",
    68: "Tops",
    69: "Co-ord Sets",
    70: "T-Shirts",
    74: "Skirts",
    76: "Pants",
    79: "Dresses",
    80: "Blouses",
    101: "Hair Accessories",
    102: "Artificial Nails",
    103: "Makeup Bags",
    730: "Bras",
    734: "Briefs",
    751: "Jeans",
    903: "Lounge Sets",
    1086: "Lounge Bottoms",
    1141: "Intimate Accessories",
}


class SavanaTaxonomy:
    """Resolves savana's numeric category ids, remembering the ones it could not.

    Stateful on purpose: unknown ids accumulate across a run so the pipeline can
    report them once at the end instead of logging per product.
    """

    def __init__(
        self,
        level2: dict[int, str] | None = None,
        level3: dict[int, str] | None = None,
    ) -> None:
        self._level2 = LEVEL2_NAMES if level2 is None else level2
        self._level3 = LEVEL3_NAMES if level3 is None else level3
        self._unknown: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path | None = None) -> SavanaTaxonomy:
        """Built-in map, with ``path``'s JSON merged over it when given."""
        if path is None:
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("Could not read taxonomy override %s: %s — using defaults", path, e)
            return cls()

        return cls(
            level2={**LEVEL2_NAMES, **_int_keys(raw.get("level2"))},
            level3={**LEVEL3_NAMES, **_int_keys(raw.get("level3"))},
        )

    # ------------------------------------------------------------------ #
    def category(self, cat_id: object) -> str | None:
        return self._label(cat_id, self._level2, "level2")

    def subcategory(self, cat_id: object) -> str | None:
        return self._label(cat_id, self._level3, "level3")

    def _label(self, cat_id: object, names: dict[int, str], level: str) -> str | None:
        if not isinstance(cat_id, int) or isinstance(cat_id, bool):
            return None
        name = names.get(cat_id)
        if name is not None:
            return name
        self._unknown.add((level, cat_id))
        return f"{UNKNOWN_PREFIX}{cat_id}"

    # ------------------------------------------------------------------ #
    @property
    def warnings(self) -> list[str]:
        """One warning per unmapped id, for the run report."""
        return [
            f"Unmapped savana {level} category id {cat_id}; "
            f"exported as {UNKNOWN_PREFIX}{cat_id}. Add it to the taxonomy map."
            for level, cat_id in sorted(self._unknown)
        ]


def _int_keys(mapping: object) -> dict[int, str]:
    """Coerce a JSON object's string keys to ints, dropping anything unusable."""
    if not isinstance(mapping, dict):
        return {}
    out: dict[int, str] = {}
    for key, value in mapping.items():
        try:
            out[int(key)] = str(value)
        except (TypeError, ValueError):
            log.warning("Ignoring non-integer taxonomy key %r", key)
    return out
