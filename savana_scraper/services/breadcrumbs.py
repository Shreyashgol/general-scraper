"""Breadcrumb reading — where category/subcategory come from on an unknown site.

Field extraction generalises because storefronts publish ``schema.org`` data, and
their taxonomy is no exception: it arrives as a ``BreadcrumbList``, as a
``Product.category`` path ("Women > Bags > Backpacks"), or as a breadcrumb nav in
the DOM. Read in that order, most trustworthy first.

Turning a trail into two columns needs one decision, and it is the same one the
savana adapter makes with ``level1``…``level4``: **drop the constant root, then
take the next two.**

    Home  >  Women  >  Bags  >  Backpacks  >  Kitty School Backpack
    ~~~~     ~~~~~     ~~~~     ~~~~~~~~~     ~~~~~~~~~~~~~~~~~~~~~
    drop     category  subcat    ignored      dropped (it is the product)

"Home" carries nothing, exactly as savana's ``level1`` (the whole womenswear
storefront) carries nothing. The trailing crumb is the product itself, not a
category, so a trail of ``Home > Dresses > Slit Bodycon Dress`` yields
``("Dresses", None)`` — a real category and an honestly empty subcategory, rather
than a subcategory that is just the product name again.

The alternative reading — take the *two most specific* crumbs — is rejected on
purpose: on ``Home > Women > Bags`` it would answer ``("Women", "Bags")`` on one
site and ``("Bags", "Backpacks")`` on another, so the column would mean a
different depth per row.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from bs4 import BeautifulSoup, Tag

# Crumbs that name the site's root rather than a category.
_ROOT_CRUMBS = frozenset({"home", "homepage", "index", "main", "start", "shop", "all"})

# Separators used by ``Product.category`` paths, widest first.
_PATH_SEPARATORS = (">", "›", "|", "/")

# DOM containers that hold a breadcrumb trail, most explicit first.
_DOM_SELECTORS = (
    "[itemtype$='BreadcrumbList']",
    "nav[aria-label*='breadcrumb' i]",
    "[class*='breadcrumb' i]",
    "[id*='breadcrumb' i]",
)


def crumbs_from_json_ld(soup: BeautifulSoup) -> list[str]:
    """Names from a ``schema.org/BreadcrumbList``, in ``position`` order."""
    for blob in _json_ld_blocks(soup):
        node = _find_node(blob, "breadcrumblist")
        if node is None:
            continue
        items = node.get("itemListElement")
        if not isinstance(items, list):
            continue

        ordered: list[tuple[int, str]] = []
        for index, element in enumerate(items):
            if not isinstance(element, dict):
                continue
            name = _name_of(element)
            if name:
                position = element.get("position")
                ordered.append((position if isinstance(position, int) else index, name))
        if ordered:
            return [name for _, name in sorted(ordered, key=lambda pair: pair[0])]
    return []


def crumbs_from_schema_category(soup: BeautifulSoup) -> list[str]:
    """Crumbs from a ``schema.org/Product``'s ``category`` ("Women > Bags")."""
    for blob in _json_ld_blocks(soup):
        node = _find_node(blob, "product")
        if node is None:
            continue
        crumbs = crumbs_from_category_value(node.get("category"))
        if crumbs:
            return crumbs
    return []


def crumbs_from_category_value(value: object) -> list[str]:
    """Normalise a ``category`` value — path string, list, or nested node."""
    if isinstance(value, dict):
        value = _name_of(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return _split_path(value)
    return []


def crumbs_from_dom(soup: BeautifulSoup) -> list[str]:
    """Crumbs from a breadcrumb nav, read as its ordered link/item text."""
    for selector in _DOM_SELECTORS:
        for container in soup.select(selector):
            crumbs = _container_crumbs(container)
            # One crumb is a link that merely happens to sit in a matching
            # element; a trail has at least two.
            if len(crumbs) >= 2:
                return crumbs
    return []


def category_pair(
    crumbs: Iterable[str], product_name: str | None = None
) -> tuple[str | None, str | None]:
    """Reduce a breadcrumb trail to ``(category, subcategory)``.

    Drops root crumbs, then the product's own name if it terminates the trail,
    then takes the first two of what remains. See the module docstring.
    """
    cleaned = [c.strip() for c in crumbs if c and c.strip()]

    # Root crumbs only count as roots at the front: a "Shop" nested three deep
    # is a real category.
    while cleaned and cleaned[0].strip().lower().strip(" >/|") in _ROOT_CRUMBS:
        cleaned.pop(0)

    if product_name and cleaned and _same_text(cleaned[-1], product_name):
        cleaned.pop()

    category = cleaned[0] if cleaned else None
    subcategory = cleaned[1] if len(cleaned) > 1 else None
    return category, subcategory


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _container_crumbs(container: Tag) -> list[str]:
    """Ordered crumb text inside one breadcrumb container."""
    items = container.select("[itemprop='name'], li, a, span")
    crumbs: list[str] = []
    for item in items:
        text = item.get("content") if item.get("itemprop") == "name" else None
        text = str(text) if text else item.get_text(" ", strip=True)
        text = " ".join(text.split())
        # A <li> wrapping an <a> would otherwise contribute the same text twice.
        if text and text not in crumbs and not _is_separator(text):
            crumbs.append(text)
    return crumbs


def _is_separator(text: str) -> bool:
    return text.strip("  ") in {">", "/", "|", "›", "»", "-", "—"}


def _same_text(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _split_path(path: str) -> list[str]:
    for separator in _PATH_SEPARATORS:
        if separator in path:
            return [part.strip() for part in path.split(separator) if part.strip()]
    return [path.strip()]


def _name_of(element: dict[str, Any]) -> str | None:
    """The display name of a breadcrumb element or a nested ``item``."""
    name = element.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    item = element.get("item")
    if isinstance(item, dict):
        inner = item.get("name")
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
    return None


def _json_ld_blocks(soup: BeautifulSoup) -> Iterable[Any]:
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = tag.string or tag.get_text()
        if not text:
            continue
        try:
            yield json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue


def _find_node(node: Any, wanted_type: str) -> dict[str, Any] | None:
    """Depth-first search for a node whose ``@type`` matches ``wanted_type``."""
    if isinstance(node, dict):
        type_ = node.get("@type")
        types = type_ if isinstance(type_, list) else [type_]
        if any(isinstance(t, str) and t.lower() == wanted_type for t in types):
            return node
        for value in node.values():
            found = _find_node(value, wanted_type)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_node(item, wanted_type)
            if found is not None:
                return found
    return None
