from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import ConfigurationError
from app.models import Product, Priority
from app.watchlist import WatchRule, Watchlist, classify_product, load_watchlist


@pytest.fixture
def stitch() -> Product:
    return Product(
        retailer="Disney Store UK", retailer_product_id="PID-42",
        name="Disney’s STITCH: Floral Mini-Backpack!", url="https://shop.example/item/42/",
        price=Decimal("89.99"), currency="GBP", product_type="Mini Backpack",
        exclusive=True, sku="LF-ST-42",
    )


def test_capitalization_punctuation_and_deterministic_classification(stitch: Product) -> None:
    rule = WatchRule(
        "Stitch Backpacks", Priority.HIGH, keywords=("stitch", "floral"),
        franchises=("DISNEY",), characters=("stitch",), product_types=("mini_backpack",),
    )
    normalized = classify_product(stitch)
    assert normalized.product_type == "mini_backpack"
    assert normalized.franchise == "Disney"
    assert normalized.character == "Stitch"
    assert rule.matches(stitch)


def test_retailer_specific_naming_differences_use_keywords_not_exact_name(stitch: Product) -> None:
    rule = WatchRule("Flexible", keywords=("stitch", "mini backpack"))
    assert rule.matches(stitch)
    assert rule.matches(replace(stitch, name="Loungefly Disney Stitch Floral Mini Backpack Bag"))
    assert not WatchRule("Exact", product_name="Stitch Floral Mini Backpack").matches(stitch)


def test_excluded_keywords_override_required_keywords(stitch: Product) -> None:
    rule = WatchRule("No wallets", keywords=("stitch",), excluded_keywords=("wallet",))
    assert rule.matches(stitch)
    assert not rule.matches(replace(stitch, name="Stitch floral wallet mini backpack set"))


def test_maximum_price_requires_a_known_price_at_or_below_limit(stitch: Product) -> None:
    rule = WatchRule("Under 90", max_price=Decimal("90"))
    assert rule.matches(stitch)
    assert not rule.matches(replace(stitch, price=Decimal("90.01")))
    assert not rule.matches(replace(stitch, price=None))


def test_exact_url_ignores_fragment_host_case_and_trailing_slash(stitch: Product) -> None:
    assert WatchRule("URL", exact_url="https://SHOP.example/item/42#details").matches(stitch)
    assert not WatchRule("URL", exact_url="https://shop.example/item/43").matches(stitch)


def test_ids_sku_flags_and_retailer_are_conjunctive(stitch: Product) -> None:
    rule = WatchRule(
        "Specific", retailer_product_id="pid 42", sku="lf-st-42",
        retailers=("disney-store uk",), exclusive=True, preorder=False,
    )
    assert rule.matches(stitch)
    assert not rule.matches(replace(stitch, preorder=True))


def test_multiple_rules_return_unique_matches_with_highest_priority_metadata(stitch: Product) -> None:
    watchlist = Watchlist((
        WatchRule("All minis", Priority.LOW, product_types=("mini backpack",)),
        WatchRule("Stitch", Priority.HIGH, keywords=("stitch",)),
        WatchRule("STITCH", Priority.HIGH, keywords=("stitch",)),
        WatchRule("Wallets", keywords=("wallet",)),
    ))
    matches = watchlist.match(stitch)
    assert [(match.name, match.priority) for match in matches] == [
        ("All minis", Priority.LOW), ("Stitch", Priority.HIGH),
    ]
    assert len({(match.name, match.priority) for match in matches}) == len(matches)


def test_yaml_loads_all_supported_options(tmp_path: Path, stitch: Product) -> None:
    path = tmp_path / "watchlist.yaml"
    path.write_text("""products:
  - name: Specific Stitch
    priority: high
    exact_url: https://shop.example/item/42
    retailer_product_id: PID-42
    sku: LF-ST-42
    product_name: "Disney’s STITCH: Floral Mini-Backpack!"
    required_keywords: [stitch, floral]
    excluded_keywords: [wallet]
    franchise: Disney
    character: Stitch
    retailers: [Disney Store UK]
    product_types: [mini_backpack]
    exclusivity: true
    preorder: false
    max_price: 90
""", encoding="utf-8")
    watchlist = load_watchlist(path)
    assert watchlist.match(stitch)[0].name == "Specific Stitch"


@pytest.mark.parametrize("yaml_text, message", [
    ("products: [{name: Bad, priority: urgent}]", "invalid priority"),
    ("products: [{name: Bad, max_price: -1}]", "invalid max_price"),
    ("products: [{name: Bad, preorder: 'yes'}]", "must be boolean"),
    ("products: [{name: Bad, mystery: true}]", "unknown watchlist options"),
])
def test_invalid_watch_rules_are_rejected(tmp_path: Path, yaml_text: str, message: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_watchlist(path)
