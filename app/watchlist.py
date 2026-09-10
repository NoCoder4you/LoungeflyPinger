"""Validated watchlist rules and deterministic product classification/matching."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from app.config import ConfigurationError
from app.models import Product, Priority, WatchMatch


def normalize_text(value: str) -> str:
    """Make punctuation, whitespace, and capitalization irrelevant to textual rules."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).split())


def normalize_url(value: str) -> str:
    parts = urlsplit(value.strip())
    host = parts.hostname.casefold() if parts.hostname else ""
    if parts.port:
        host += f":{parts.port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), host, path, parts.query, ""))


PRODUCT_TYPE_ALIASES = {
    "mini backpack": "mini_backpack", "mini bag": "mini_backpack", "backpack mini": "mini_backpack",
    "crossbody bag": "crossbody", "cross body": "crossbody", "wallet": "wallet", "purse": "wallet",
}
KNOWN_FRANCHISES = ("Disney", "Marvel", "Star Wars", "Pokemon", "Harry Potter", "Sanrio")
KNOWN_CHARACTERS = (
    "Stitch", "Mickey Mouse", "Minnie Mouse", "Winnie the Pooh", "Tinker Bell", "Hello Kitty",
    "Spider-Man", "Darth Vader",
)


def canonical_product_type(value: str) -> str:
    normalized = normalize_text(value)
    return PRODUCT_TYPE_ALIASES.get(normalized, normalized.replace(" ", "_"))


def classify_product(product: Product) -> Product:
    """Fill/canonicalize common taxonomy fields without pretending to be NLP."""
    searchable = normalize_text(product.name)
    product_type = canonical_product_type(product.product_type)
    if product_type not in set(PRODUCT_TYPE_ALIASES.values()):
        for alias, canonical in PRODUCT_TYPE_ALIASES.items():
            if alias in searchable:
                product_type = canonical
                break
    franchise = product.franchise
    if not franchise:
        franchise = next((item for item in KNOWN_FRANCHISES if normalize_text(item) in searchable), None)
    character = product.character
    if not character:
        character = next((item for item in KNOWN_CHARACTERS if normalize_text(item) in searchable), None)
    return replace(product, product_type=product_type, franchise=franchise, character=character)


@dataclass(frozen=True, slots=True)
class WatchRule:
    name: str
    priority: Priority = Priority.NORMAL
    exact_url: str | None = None
    retailer_product_id: str | None = None
    sku: str | None = None
    product_name: str | None = None
    keywords: tuple[str, ...] = ()
    excluded_keywords: tuple[str, ...] = ()
    franchises: tuple[str, ...] = ()
    characters: tuple[str, ...] = ()
    retailers: tuple[str, ...] = ()
    product_types: tuple[str, ...] = ()
    exclusive: bool | None = None
    preorder: bool | None = None
    max_price: Decimal | None = None

    def matches(self, raw_product: Product) -> bool:
        product = classify_product(raw_product)
        text = normalize_text(product.name)
        if self.exact_url and normalize_url(product.url) != normalize_url(self.exact_url): return False
        if self.retailer_product_id and normalize_text(product.retailer_product_id) != normalize_text(self.retailer_product_id): return False
        if self.sku and (not product.sku or normalize_text(product.sku) != normalize_text(self.sku)): return False
        if self.product_name and text != normalize_text(self.product_name): return False
        if any(normalize_text(term) not in text for term in self.keywords): return False
        if any(normalize_text(term) in text for term in self.excluded_keywords): return False
        if self.franchises and normalize_text(product.franchise or "") not in map(normalize_text, self.franchises): return False
        if self.characters and normalize_text(product.character or "") not in map(normalize_text, self.characters): return False
        if self.retailers and normalize_text(product.retailer) not in map(normalize_text, self.retailers): return False
        if self.product_types and product.product_type not in map(canonical_product_type, self.product_types): return False
        if self.exclusive is not None and product.exclusive != self.exclusive: return False
        if self.preorder is not None and product.preorder != self.preorder: return False
        if self.max_price is not None and (product.price is None or product.price > self.max_price): return False
        return True


@dataclass(frozen=True, slots=True)
class Watchlist:
    rules: tuple[WatchRule, ...] = ()

    def match(self, product: Product) -> tuple[WatchMatch, ...]:
        matches: list[WatchMatch] = []
        seen: set[tuple[str, Priority]] = set()
        for rule in self.rules:
            match = WatchMatch(rule.name, rule.priority)
            identity = (normalize_text(match.name), match.priority)
            if rule.matches(product) and identity not in seen:
                matches.append(match)
                seen.add(identity)
        return tuple(matches)


def _strings(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if isinstance(value, str): value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigurationError(f"watchlist {key} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def load_watchlist(path: str | Path = "config/watchlist.yaml") -> Watchlist:
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Unable to load watchlist: {config_path}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("products", []), list):
        raise ConfigurationError("watchlist root must contain a products list")
    rules: list[WatchRule] = []
    for index, item in enumerate(raw.get("products", []), 1):
        if not isinstance(item, dict): raise ConfigurationError(f"watchlist product {index} must be a mapping")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip(): raise ConfigurationError(f"watchlist product {index} requires a name")
        unknown = set(item) - {"name", "priority", "exact_url", "url", "retailer_product_id", "sku", "product_name", "keywords", "required_keywords", "excluded_keywords", "franchise", "franchises", "character", "characters", "retailers", "product_types", "exclusivity", "exclusive", "preorder", "max_price"}
        if unknown: raise ConfigurationError(f"unknown watchlist options for {name}: {', '.join(sorted(unknown))}")
        def optional_string(key: str) -> str | None:
            value = item.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()): raise ConfigurationError(f"watchlist {key} must be a non-empty string")
            return value.strip() if value else None
        try: priority = Priority(str(item.get("priority", "normal")).casefold())
        except ValueError as exc: raise ConfigurationError(f"invalid priority for watch {name}") from exc
        price = item.get("max_price")
        try: max_price = Decimal(str(price)) if price is not None else None
        except InvalidOperation as exc: raise ConfigurationError(f"invalid max_price for watch {name}") from exc
        if max_price is not None and (not max_price.is_finite() or max_price < 0): raise ConfigurationError(f"invalid max_price for watch {name}")
        for flag in ("exclusivity", "exclusive", "preorder"):
            if flag in item and not isinstance(item[flag], bool): raise ConfigurationError(f"watchlist {flag} must be boolean")
        def choices(singular: str, plural: str) -> tuple[str, ...]:
            values = list(_strings(item, plural))
            if singular in item:
                value = item[singular]
                if not isinstance(value, str) or not value.strip(): raise ConfigurationError(f"watchlist {singular} must be a non-empty string")
                values.append(value.strip())
            return tuple(values)
        rules.append(WatchRule(name.strip(), priority, optional_string("exact_url") or optional_string("url"), optional_string("retailer_product_id"), optional_string("sku"), optional_string("product_name"), _strings(item, "keywords") + _strings(item, "required_keywords"), _strings(item, "excluded_keywords"), choices("franchise", "franchises"), choices("character", "characters"), _strings(item, "retailers"), _strings(item, "product_types"), item.get("exclusivity", item.get("exclusive")), item.get("preorder"), max_price))
    return Watchlist(tuple(rules))
