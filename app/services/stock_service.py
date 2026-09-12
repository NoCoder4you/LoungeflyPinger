"""Persistence operations for current and historical product state."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from app.database import Database
from app.models import Availability, Product


@dataclass(frozen=True, slots=True)
class ProductState:
    availability: Availability
    price: Decimal | None
    currency: str
    preorder: bool
    previous_price: Decimal | None
    lowest_price: Decimal | None
    highest_price: Decimal | None
    estimated_ship_date: date | None
    estimated_arrival_date: date | None
    estimated_arrival_text: str | None
    estimated_dispatch_date: date | None


class StockService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def record(self, product_id: int, product: Product) -> None:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        now = datetime.now(UTC).isoformat()
        old = await self.current(product_id)
        previous_price = old.price if old else None
        comparable = old is not None and old.currency == product.currency
        observed = product.price
        low = min(filter(lambda value: value is not None,
                         (old.lowest_price if comparable else None,
                          old.price if comparable else None, observed)), default=None)
        high = max(filter(lambda value: value is not None,
                          (old.highest_price if comparable else None,
                           old.price if comparable else None, observed)), default=None)
        await connection.execute(
            """INSERT INTO product_state_history
               (product_id, availability, price, currency, preorder, checked_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (product_id, product.availability.value, str(observed) if observed is not None else None,
             product.currency, product.preorder, now),
        )
        await connection.execute(
            """INSERT INTO product_states
               (product_id, availability, price, currency, preorder, checked_at,
                previous_price, lowest_price, highest_price, estimated_ship_date,
                estimated_arrival_date, estimated_arrival_text, estimated_dispatch_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id) DO UPDATE SET availability=excluded.availability,
                 price=excluded.price, currency=excluded.currency, preorder=excluded.preorder,
                 checked_at=excluded.checked_at, previous_price=excluded.previous_price,
                 lowest_price=excluded.lowest_price, highest_price=excluded.highest_price,
                 estimated_ship_date=COALESCE(
                     excluded.estimated_ship_date, product_states.estimated_ship_date
                 ), estimated_arrival_date=COALESCE(
                     excluded.estimated_arrival_date, product_states.estimated_arrival_date
                 ), estimated_arrival_text=COALESCE(
                     excluded.estimated_arrival_text, product_states.estimated_arrival_text
                 ), estimated_dispatch_date=COALESCE(
                     excluded.estimated_dispatch_date, product_states.estimated_dispatch_date
                 )""",
            (product_id, product.availability.value, str(product.price) if product.price is not None else None,
             product.currency, product.preorder, now,
             str(previous_price) if previous_price is not None else None,
             str(low) if low is not None else None, str(high) if high is not None else None,
             product.estimated_ship_date.isoformat() if product.estimated_ship_date else None,
             product.estimated_arrival_date.isoformat() if product.estimated_arrival_date else None,
             product.estimated_arrival_text,
             product.estimated_dispatch_date.isoformat() if product.estimated_dispatch_date else None),
        )

    async def current(self, product_id: int) -> ProductState | None:
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        row = await (await connection.execute(
            """SELECT availability, price, currency, preorder, previous_price,
                      lowest_price, highest_price, estimated_ship_date,
                      estimated_arrival_date, estimated_dispatch_date
                      , estimated_arrival_text
                 FROM product_states WHERE product_id=?""",
            (product_id,),
        )).fetchone()
        if row is None:
            return None
        money = lambda value: Decimal(value) if value is not None else None
        return ProductState(Availability(row[0]), money(row[1]), row[2], bool(row[3]),
                            money(row[4]), money(row[5]), money(row[6]),
                            date.fromisoformat(row[7]) if row[7] else None,
                            date.fromisoformat(row[8]) if row[8] else None,
                            row[10], date.fromisoformat(row[9]) if row[9] else None)

    async def current_availability(self, product_id: int) -> Availability | None:
        """Return the persisted state, or ``None`` for a never-synchronized product."""
        connection = self.database.connection
        if connection is None:
            raise RuntimeError("database is not connected")
        state = await self.current(product_id)
        return state.availability if state else None
