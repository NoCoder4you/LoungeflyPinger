from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio

from app.database import Database
from app.models import Availability, Product
from app.services.product_query_service import (
    MAX_PAGE_SIZE, ProductMatchConfidence, ProductQuery, ProductQueryService, RemovedFilter,
)
from app.services.product_service import ProductService
from app.services.stock_service import StockService


@pytest_asyncio.fixture
async def catalogue(tmp_path):
    db = Database(tmp_path / "catalogue.db")
    await db.connect(); await db.initialize()
    products, stock = ProductService(db), StockService(db)

    async def add(code, name, retailer="Shop A", availability=Availability.IN_STOCK,
                  price="50", **kwargs):
        product = Product(retailer=retailer, retailer_product_id=code, name=name,
            url=f"https://example.com/{code}", availability=availability,
            price=Decimal(price), currency=kwargs.pop("currency", "USD"), **kwargs)
        product_id = await products.upsert(product); await stock.record(product_id, product)
        await db.connection.commit()
        return product_id

    yield db, ProductQueryService(db), add
    await db.close()


@pytest.mark.asyncio
async def test_composable_search_and_removed_filter(catalogue):
    db, service, add = catalogue
    wanted = await add("stitch", "Stitch Floral Mini Backpack", franchise="Disney",
                       character="Stitch", product_type="Mini Backpack", exclusive=True,
                       preorder=True)
    await add("mickey", "Mickey Wallet", franchise="Disney", character="Mickey",
              product_type="Wallet", price="20")
    removed = await add("old", "Stitch Retired Backpack", franchise="Disney", character="Stitch")
    await db.connection.execute("UPDATE products SET removed_at=? WHERE id=?",
                                (datetime.now(UTC).isoformat(), removed)); await db.connection.commit()

    page = await service.search(ProductQuery(text="floral", retailer="shop", franchise="dis",
        character="tit", product_type="mini", availability=Availability.IN_STOCK,
        minimum_price=Decimal("40"), maximum_price=Decimal("60"), exclusive=True,
        preorder=True))
    assert [item.id for item in page.items] == [wanted]
    assert service.connection is db.connection
    assert not (await service.search(ProductQuery(text="retired"))).items
    assert [p.id for p in (await service.search(ProductQuery(
        text="retired", removed=RemovedFilter.INCLUDE))).items] == [removed]


@pytest.mark.asyncio
async def test_available_semantics_and_stable_bounded_pages(catalogue):
    _, service, add = catalogue
    for number, state in enumerate((Availability.IN_STOCK, Availability.LOW_STOCK,
                                    Availability.PREORDER, Availability.OUT_OF_STOCK,
                                    Availability.COMING_SOON, Availability.ERROR)):
        await add(str(number), f"Item {number:02}", availability=state, preorder=state == Availability.PREORDER)
    ordinary = await service.available(page_size=1000)
    assert ordinary.page_size == MAX_PAGE_SIZE
    assert [p.availability for p in ordinary.items] == ["IN_STOCK", "LOW_STOCK"]
    with_preorders = await service.available(ProductQuery(include_preorders=True))
    assert [p.availability for p in with_preorders.items] == ["IN_STOCK", "LOW_STOCK", "PREORDER"]


@pytest.mark.asyncio
async def test_recent_uses_first_seen_newest_first(catalogue):
    db, service, add = catalogue
    old = await add("old", "Old")
    new = await add("new", "New")
    await db.connection.execute("UPDATE products SET first_seen=? WHERE id=?",
                                ((datetime.now(UTC)-timedelta(days=2)).isoformat(), old))
    await db.connection.commit()
    assert [p.id for p in (await service.recent(timedelta(hours=24))).items] == [new]


@pytest.mark.asyncio
async def test_history_deduplicates_identical_observations(catalogue):
    db, service, add = catalogue
    product_id = await add("history", "History")
    row = await service.get(product_id)
    product = Product(retailer=row.retailer, retailer_product_id=row.retailer_product_id,
        name=row.name, url=row.url, availability=Availability.IN_STOCK,
        price=Decimal("50"), currency="USD")
    await StockService(db).record(product_id, product); await db.connection.commit()
    count = await (await db.connection.execute(
        "SELECT COUNT(*) FROM product_state_history WHERE product_id=?", (product_id,))).fetchone()
    assert count[0] == 1


@pytest.mark.asyncio
async def test_offer_confidence_and_currency_scoped_cheapest(catalogue):
    _, service, add = catalogue
    source = await add("one", "Stitch Backpack", barcode="12345678", price="60")
    cheap = await add("two", "Stitch Backpack Other", retailer="Shop B", barcode="12345678", price="50")
    pounds = await add("three", "Stitch Backpack UK", retailer="Shop C", barcode="12345678",
                       price="40", currency="GBP")
    matches = {offer.product.id: offer for offer in await service.offers(source)}
    assert matches[cheap].confidence == ProductMatchConfidence.HIGH
    assert matches[cheap].cheapest_same_currency
    assert matches[pounds].cheapest_same_currency  # cheapest GBP, never compared with USD


@pytest.mark.asyncio
async def test_sales_preorders_exclusives_and_empty(catalogue):
    _, service, add = catalogue
    sale = await add("sale", "Sale", sale=True)
    preorder = await add("pre", "Preorder", availability=Availability.PREORDER, preorder=True)
    exclusive = await add("ex", "Exclusive", exclusive=True)
    assert [p.id for p in (await service.sales()).items] == [sale]
    assert [p.id for p in (await service.preorders()).items] == [preorder]
    assert [p.id for p in (await service.exclusives()).items] == [exclusive]
    assert not (await service.search(ProductQuery(text="does-not-exist"))).items


@pytest.mark.asyncio
async def test_large_catalogue_query_is_database_bounded(catalogue):
    db, service, _ = catalogue
    await db.connection.execute("INSERT INTO retailers(name) VALUES('Bulk')")
    now = datetime.now(UTC).isoformat()
    await db.connection.executemany("""INSERT INTO products
        (retailer,retailer_product_id,name,url,product_type,first_seen,last_seen)
        VALUES('Bulk',?,?,?,'Mini Backpack',?,?)""",
        [(str(i), f"Synthetic {i:04}", f"https://example.com/bulk/{i}", now, now)
         for i in range(1_000)])
    await db.connection.commit()
    page = await service.search(ProductQuery(text="Synthetic"), page_size=10)
    assert page.total == 1_000
    assert len(page.items) == 10
    assert [item.name for item in page.items] == [f"Synthetic {i:04}" for i in range(10)]
