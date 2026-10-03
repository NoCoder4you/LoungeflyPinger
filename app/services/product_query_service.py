"""Bounded, typed catalogue reads and cross-retailer intelligence."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.database import Database
from app.models import Availability

DEFAULT_PAGE_SIZE = 12
MAX_PAGE_SIZE = 25


class RemovedFilter(StrEnum):
    EXCLUDE = "EXCLUDE"
    INCLUDE = "INCLUDE"
    ONLY = "ONLY"


class ProductMatchConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    POSSIBLE = "POSSIBLE"


@dataclass(frozen=True, slots=True)
class ProductQuery:
    text: str | None = None
    retailer: str | None = None
    retailer_key: str | None = None
    product_type: str | None = None
    franchise: str | None = None
    character: str | None = None
    availability: Availability | None = None
    available_only: bool = False
    include_preorders: bool = False
    preorder: bool | None = None
    exclusive: bool | None = None
    exclusive_retailer: str | None = None
    exclusive_region: str | None = None
    exclusive_type: str | None = None
    new_release: bool | None = None
    sale: bool | None = None
    clearance: bool | None = None
    last_chance: bool | None = None
    limited_edition: bool | None = None
    limited_release: bool | None = None
    disney_parks: bool | None = None
    event_exclusive: bool | None = None
    vaulted: bool | None = None
    sales_only: bool = False
    release_only: bool = False
    minimum_price: Decimal | None = None
    maximum_price: Decimal | None = None
    currency: str | None = None
    first_seen_since: datetime | None = None
    last_seen_since: datetime | None = None
    removed: RemovedFilter = RemovedFilter.EXCLUDE
    order: str = "search"


@dataclass(frozen=True, slots=True)
class ProductRecord:
    id: int
    retailer: str
    retailer_product_id: str
    name: str
    url: str
    image_url: str | None
    sku: str | None
    barcode: str | None
    loungefly_product_code: str | None
    franchise: str | None
    character: str | None
    characters: tuple[str, ...]
    product_type: str
    availability: str | None
    price: Decimal | None
    currency: str | None
    preorder: bool
    exclusive: bool
    first_seen: str
    last_seen: str
    removed_at: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Page:
    items: tuple[Any, ...]
    page: int
    page_size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True, slots=True)
class ProductOffer:
    product: ProductRecord
    confidence: ProductMatchConfidence
    cheapest_same_currency: bool = False


class ProductQueryService:
    """Read-only application service; every collection read has a hard SQL bound."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @property
    def connection(self):
        if self.database.connection is None:
            raise RuntimeError("database is not connected")
        return self.database.connection

    @staticmethod
    def _like(value: str) -> str:
        return f"%{value.strip()}%"

    def _where(self, query: ProductQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if query.removed == RemovedFilter.EXCLUDE:
            clauses.append("p.removed_at IS NULL")
        elif query.removed == RemovedFilter.ONLY:
            clauses.append("p.removed_at IS NOT NULL")
        if query.text and query.text.strip():
            pattern = self._like(query.text)
            fields = ("p.name", "p.franchise", "p.character", "p.characters", "p.sku",
                      "p.barcode", "p.loungefly_product_code", "p.property", "p.license")
            clauses.append("(" + " OR ".join(f"{field} LIKE ? COLLATE NOCASE" for field in fields) + ")")
            args.extend([pattern] * len(fields))
        for column, value in (("p.retailer", query.retailer or query.retailer_key),
                              ("p.product_type", query.product_type), ("p.franchise", query.franchise),
                              ("p.exclusive_retailer", query.exclusive_retailer),
                              ("p.exclusive_region", query.exclusive_region),
                              ("p.exclusive_type", query.exclusive_type)):
            if value:
                clauses.append(f"{column} LIKE ? COLLATE NOCASE")
                args.append(self._like(value))
        if query.character:
            clauses.append("(p.character LIKE ? COLLATE NOCASE OR p.characters LIKE ? COLLATE NOCASE)")
            args.extend([self._like(query.character)] * 2)
        if query.available_only:
            values = [Availability.IN_STOCK.value, Availability.LOW_STOCK.value]
            if query.include_preorders:
                values.append(Availability.PREORDER.value)
            clauses.append(f"s.availability IN ({','.join('?' for _ in values)})")
            args.extend(values)
        elif query.availability is not None:
            clauses.append("s.availability=?")
            args.append(Availability(query.availability).value)
        if query.sales_only:
            clauses.append("(p.sale=1 OR p.clearance=1 OR p.last_chance=1)")
        if query.release_only:
            clauses.append("(s.release_precision IN ('EXACT_DATETIME','DATE_ONLY','MONTH_ONLY','COMING_SOON') OR s.availability='COMING_SOON')")
        for column, value in (("s.preorder", query.preorder), ("p.exclusive", query.exclusive),
                              ("p.new_release", query.new_release), ("p.sale", query.sale),
                              ("p.clearance", query.clearance), ("p.last_chance", query.last_chance),
                              ("p.limited_edition", query.limited_edition),
                              ("p.limited_release", query.limited_release),
                              ("p.disney_parks", query.disney_parks),
                              ("p.event_exclusive", query.event_exclusive), ("p.vaulted", query.vaulted)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(int(value))
        if query.minimum_price is not None:
            clauses.append("CAST(s.price AS REAL)>=?"); args.append(float(query.minimum_price))
        if query.maximum_price is not None:
            clauses.append("CAST(s.price AS REAL)<=?"); args.append(float(query.maximum_price))
        if query.currency:
            clauses.append("s.currency=? COLLATE NOCASE"); args.append(query.currency)
        if query.first_seen_since:
            clauses.append("p.first_seen>=?"); args.append(query.first_seen_since.astimezone(UTC).isoformat())
        if query.last_seen_since:
            clauses.append("p.last_seen>=?"); args.append(query.last_seen_since.astimezone(UTC).isoformat())
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    async def search(self, query: ProductQuery = ProductQuery(), *, page: int = 1,
                     page_size: int = DEFAULT_PAGE_SIZE) -> Page:
        if page < 1:
            raise ValueError("page must be at least one")
        size = min(MAX_PAGE_SIZE, max(1, int(page_size)))
        where, args = self._where(query)
        joins = " FROM products p LEFT JOIN product_states s ON s.product_id=p.id"
        total = int((await (await self.connection.execute(
            "SELECT COUNT(*)" + joins + where, args)).fetchone())[0])
        orders = {"recent": "p.first_seen DESC,p.id DESC",
                  "available": "p.retailer COLLATE NOCASE,p.name COLLATE NOCASE,p.id",
                  "release": "CASE s.release_precision WHEN 'EXACT_DATETIME' THEN 0 WHEN 'DATE_ONLY' THEN 1 WHEN 'MONTH_ONLY' THEN 2 WHEN 'COMING_SOON' THEN 3 ELSE 4 END,COALESCE(s.release_datetime,s.release_date,printf('%04d-%02d',s.release_year,s.release_month)),p.id",
                  "search": "p.name COLLATE NOCASE,p.id"}
        order = orders.get(query.order, orders["search"])
        columns = """p.*,s.availability,s.price,s.currency,s.preorder,s.checked_at,
            s.previous_price,s.lowest_price,s.highest_price,s.release_date,s.release_time,
            s.release_timezone,s.release_datetime,s.release_precision,s.release_text,s.release_source,
            s.release_month,s.release_year,s.estimated_ship_date,s.estimated_arrival_date,
            s.estimated_arrival_text,s.estimated_dispatch_date"""
        cursor = await self.connection.execute(
            f"SELECT {columns}{joins}{where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, size, (page - 1) * size])
        names = [item[0] for item in cursor.description or ()]
        rows = await cursor.fetchall()
        return Page(tuple(self._record(dict(zip(names, row))) for row in rows), page, size, total)

    @staticmethod
    def _record(row: dict[str, Any]) -> ProductRecord:
        parsed = lambda value: tuple(json.loads(value or "[]"))
        reserved = {"id", "retailer", "retailer_product_id", "name", "url", "image_url", "sku",
                    "barcode", "loungefly_product_code", "franchise", "character", "characters",
                    "product_type", "availability", "price", "currency", "preorder", "exclusive",
                    "first_seen", "last_seen", "removed_at"}
        return ProductRecord(int(row["id"]), row["retailer"], row["retailer_product_id"], row["name"],
            row["url"], row["image_url"], row["sku"], row["barcode"], row["loungefly_product_code"],
            row["franchise"], row["character"], parsed(row["characters"]), row["product_type"],
            row.get("availability"), Decimal(row["price"]) if row.get("price") is not None else None,
            row.get("currency"), bool(row.get("preorder")), bool(row["exclusive"]), row["first_seen"],
            row["last_seen"], row["removed_at"], {k: v for k, v in row.items() if k not in reserved})

    async def get(self, product_id: int) -> ProductRecord | None:
        page = await self._by_ids([product_id])
        return page[0] if page else None

    async def _by_ids(self, ids: list[int]) -> tuple[ProductRecord, ...]:
        if not ids:
            return ()
        cursor = await self.connection.execute(
            f"""SELECT p.*,s.availability,s.price,s.currency,s.preorder,s.checked_at,
            s.previous_price,s.lowest_price,s.highest_price,s.release_date,s.release_time,
            s.release_timezone,s.release_datetime,s.release_precision,s.release_text,s.release_source,
            s.release_month,s.release_year,s.estimated_ship_date,s.estimated_arrival_date,
            s.estimated_arrival_text,s.estimated_dispatch_date
            FROM products p LEFT JOIN product_states s ON s.product_id=p.id
            WHERE p.id IN ({','.join('?' for _ in ids)}) ORDER BY p.id""",
            ids)
        names = [item[0] for item in cursor.description or ()]
        return tuple(self._record(dict(zip(names, row))) for row in await cursor.fetchall())

    async def available(self, query: ProductQuery = ProductQuery(), **pagination: Any) -> Page:
        values = {name: getattr(query, name) for name in query.__dataclass_fields__}
        values.update(available_only=True, order="available", removed=RemovedFilter.EXCLUDE)
        return await self.search(ProductQuery(**values), **pagination)

    async def recent(self, period: timedelta = timedelta(hours=24), **pagination: Any) -> Page:
        if period <= timedelta(0) or period > timedelta(days=30):
            raise ValueError("period must be between one second and 30 days")
        return await self.search(ProductQuery(first_seen_since=datetime.now(UTC) - period,
                                              order="recent"), **pagination)

    async def preorders(self, query: ProductQuery = ProductQuery(), **pagination: Any) -> Page:
        values = {name: getattr(query, name) for name in query.__dataclass_fields__}
        values.update(availability=Availability.PREORDER, removed=RemovedFilter.EXCLUDE)
        return await self.search(ProductQuery(**values), **pagination)

    async def sales(self, query: ProductQuery = ProductQuery(), **pagination: Any) -> Page:
        values = {name: getattr(query, name) for name in query.__dataclass_fields__}
        values.update(sales_only=True, removed=RemovedFilter.EXCLUDE)
        return await self.search(ProductQuery(**values), **pagination)

    async def exclusives(self, query: ProductQuery = ProductQuery(), **pagination: Any) -> Page:
        values = {name: getattr(query, name) for name in query.__dataclass_fields__}
        values.update(exclusive=True, removed=RemovedFilter.EXCLUDE)
        return await self.search(ProductQuery(**values), **pagination)

    async def releases(self, query: ProductQuery = ProductQuery(), **pagination: Any) -> Page:
        values = {name: getattr(query, name) for name in query.__dataclass_fields__}
        values.update(release_only=True, order="release", removed=RemovedFilter.EXCLUDE)
        return await self.search(ProductQuery(**values), **pagination)

    async def history(self, product_id: int, *, page: int = 1, page_size: int = 20) -> Page:
        size = min(MAX_PAGE_SIZE, max(1, page_size)); offset = (max(1, page) - 1) * size
        total = int((await (await self.connection.execute(
            "SELECT (SELECT COUNT(*) FROM product_state_history WHERE product_id=?)+(SELECT COUNT(*) FROM release_history WHERE product_id=?)+(SELECT COUNT(*) FROM product_events WHERE product_id=?)",
            (product_id,) * 3)).fetchone())[0])
        rows = await (await self.connection.execute("""
            SELECT id,checked_at AS timestamp,'STATE' AS kind,availability,price,currency,NULL AS detail
              FROM product_state_history WHERE product_id=?
            UNION ALL
            SELECT id,detected_at,'RELEASE',release_precision,NULL,NULL,COALESCE(release_datetime,release_date,release_text)
              FROM release_history WHERE product_id=?
            UNION ALL
            SELECT id,detected_at,event_type,availability,price,currency,new_summary
              FROM product_events WHERE product_id=?
            ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?""", (product_id, product_id, product_id, size, offset))).fetchall()
        keys = ("id", "timestamp", "kind", "availability", "price", "currency", "detail")
        return Page(tuple(dict(zip(keys, row)) for row in rows), max(1, page), size, total)

    async def offers(self, product_id: int) -> tuple[ProductOffer, ...]:
        source = await self.get(product_id)
        if source is None:
            return ()
        norm = lambda text: re.sub(r"[^a-z0-9]", "", (text or "").casefold())
        clauses, args = ["p.id<>?"], [product_id]
        signals: list[tuple[str, str]] = []
        for column, value in (("barcode", source.barcode), ("loungefly_product_code", source.loungefly_product_code), ("sku", source.sku)):
            if value:
                clauses.append(f"p.{column}=? COLLATE NOCASE"); args.append(value); signals.append((column, value))
        clauses.append("p.canonical_key=(SELECT canonical_key FROM products WHERE id=?)"); args.append(product_id)
        cursor = await self.connection.execute(
            "SELECT p.id FROM products p WHERE " + " OR ".join(clauses) + " ORDER BY p.id LIMIT 100", args)
        records = await self._by_ids([product_id, *[int(row[0]) for row in await cursor.fetchall()]])
        offers: list[ProductOffer] = []
        for record in records:
            if record.id == source.id:
                confidence = ProductMatchConfidence.HIGH
            elif (source.barcode and norm(source.barcode) == norm(record.barcode)) or (
                    source.loungefly_product_code and norm(source.loungefly_product_code) == norm(record.loungefly_product_code)):
                confidence = ProductMatchConfidence.HIGH
            elif (source.sku and norm(source.sku) == norm(record.sku) and
                  source.product_type.casefold() == record.product_type.casefold() and
                  norm(source.name) == norm(record.name)):
                confidence = ProductMatchConfidence.MEDIUM
            elif (norm(source.name) == norm(record.name) and source.franchise and record.franchise and
                  norm(source.franchise) == norm(record.franchise) and
                  source.product_type.casefold() == record.product_type.casefold()):
                confidence = ProductMatchConfidence.POSSIBLE
            else:
                continue
            offers.append(ProductOffer(record, confidence))
        available = {Availability.IN_STOCK.value, Availability.LOW_STOCK.value, Availability.PREORDER.value}
        cheapest: dict[str, Decimal] = {}
        for offer in offers:
            item = offer.product
            if item.availability in available and item.currency and item.price is not None:
                cheapest[item.currency] = min(cheapest.get(item.currency, item.price), item.price)
        return tuple(ProductOffer(o.product, o.confidence,
            bool(o.product.currency and o.product.price is not None and o.product.availability in available and
                 o.product.price == cheapest.get(o.product.currency))) for o in offers)

    async def distinct(self, field: str, prefix: str = "", *, limit: int = 25) -> tuple[str, ...]:
        allowed = {"retailer", "franchise", "character", "product_type"}
        if field not in allowed:
            raise ValueError("unsupported autocomplete field")
        size = min(25, max(1, limit))
        rows = await (await self.connection.execute(
            f"SELECT DISTINCT {field} FROM products WHERE removed_at IS NULL AND {field} IS NOT NULL AND {field} LIKE ? COLLATE NOCASE ORDER BY {field} COLLATE NOCASE LIMIT ?",
            (prefix + "%", size))).fetchall()
        return tuple(str(row[0]) for row in rows)

    async def events(self, *, event_type: str | None = None, retailer: str | None = None,
                     product_id: int | None = None, page: int = 1, page_size: int = 20) -> Page:
        clauses, args = [], []
        for sql, value in (("e.event_type=?", event_type), ("p.retailer=? COLLATE NOCASE", retailer),
                           ("e.product_id=?", product_id)):
            if value is not None: clauses.append(sql); args.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        size = min(MAX_PAGE_SIZE, max(1, page_size)); page = max(1, page)
        join = " FROM product_events e JOIN products p ON p.id=e.product_id"
        total = int((await (await self.connection.execute("SELECT COUNT(*)" + join + where, args)).fetchone())[0])
        rows = await (await self.connection.execute(
            "SELECT e.id,e.product_id,e.event_type,e.detected_at,e.previous_summary,e.new_summary,e.price,e.currency,e.availability,p.name,p.retailer" + join + where +
            " ORDER BY e.detected_at DESC,e.id DESC LIMIT ? OFFSET ?", [*args, size, (page-1)*size])).fetchall()
        keys = ("id","product_id","event_type","detected_at","previous_summary","new_summary","price","currency","availability","name","retailer")
        return Page(tuple(dict(zip(keys, row)) for row in rows), page, size, total)
