from decimal import Decimal

import pytest

from app.models import AlertType, Availability, Product


def test_normalized_enums() -> None:
    assert Availability.IN_STOCK.value == "IN_STOCK"
    assert len(Availability) == 9
    assert AlertType.MONITOR_RECOVERED.value == "MONITOR_RECOVERED"
    assert len(AlertType) == 17


def test_product_normalizes_values() -> None:
    product = Product(" Retailer ", " sku-1 ", " Bag ", "https://example.com/bag", price="79.99", currency="usd")
    assert product.retailer == "Retailer"
    assert product.price == Decimal("79.99")
    assert product.currency == "USD"


@pytest.mark.parametrize("url", ["", "javascript:alert(1)", "example.com/item"])
def test_product_rejects_invalid_url(url: str) -> None:
    with pytest.raises(ValueError, match="valid HTTP"):
        Product("Retailer", "one", "Bag", url)


def test_product_rejects_negative_price() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        Product("Retailer", "one", "Bag", "https://example.com/one", price=Decimal("-1"))
