"""Durable watch-rule repository with an atomic in-memory read snapshot."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yaml

from app.database import Database
from app.watchlist import WatchRule, Watchlist, rule_from_mapping, rule_to_mapping

BOOTSTRAP_KEY = "watchlist_yaml_bootstrap_v1"


@dataclass(frozen=True, slots=True)
class StoredWatchRule:
    id: int
    rule: WatchRule
    enabled: bool
    created_at: str
    updated_at: str
    discord_user_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "enabled": self.enabled, **rule_to_mapping(self.rule),
                "created_at": self.created_at, "updated_at": self.updated_at,
                "discord_user_id": self.discord_user_id}


class WatchlistManager:
    """Serialize mutations while reads use a lock-free immutable snapshot."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._items: tuple[StoredWatchRule, ...] = ()
        self._snapshot = Watchlist()
        self._mutation_lock = asyncio.Lock()

    @property
    def current(self) -> Watchlist:
        return self._snapshot

    @property
    def items(self) -> tuple[StoredWatchRule, ...]:
        return self._items

    async def initialize(self, initial: Watchlist) -> None:
        connection = self._connection()
        async with self.database.write_lock:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                marker = await (await connection.execute(
                    "SELECT value FROM application_metadata WHERE key=?", (BOOTSTRAP_KEY,)
                )).fetchone()
                if marker is None:
                    now = datetime.now(UTC).isoformat()
                    for rule in initial.rules:
                        payload = json.dumps(rule_to_mapping(rule), separators=(",", ":"))
                        await connection.execute(
                            "INSERT INTO watch_rules(name,priority,rule_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                            (rule.name, rule.priority.value, payload, now, now),
                        )
                    await connection.execute(
                        "INSERT INTO application_metadata(key,value) VALUES(?,?)", (BOOTSTRAP_KEY, now)
                    )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        await self._refresh()

    async def _refresh(self) -> None:
        rows = await (await self._connection().execute(
            "SELECT id,enabled,rule_json,created_at,updated_at,discord_user_id FROM watch_rules ORDER BY id"
        )).fetchall()
        items = tuple(StoredWatchRule(int(row[0]), rule_from_mapping(json.loads(row[2])),
                                     bool(row[1]), row[3], row[4], row[5]) for row in rows)
        self._items = items
        self._snapshot = Watchlist(tuple(item.rule for item in items if item.enabled))

    def get(self, rule_id: int) -> StoredWatchRule | None:
        return next((item for item in self._items if item.id == rule_id), None)

    async def add(self, value: dict[str, Any], user_id: str | None = None) -> StoredWatchRule:
        rule = rule_from_mapping(value)
        now = datetime.now(UTC).isoformat()
        async with self._mutation_lock, self.database.write_lock:
            cursor = await self._connection().execute(
                "INSERT INTO watch_rules(name,priority,rule_json,created_at,updated_at,discord_user_id) VALUES(?,?,?,?,?,?)",
                (rule.name, rule.priority.value, json.dumps(rule_to_mapping(rule)), now, now, user_id),
            )
            await self._connection().commit()
            rule_id = cursor.lastrowid
            await self._refresh()
        assert rule_id is not None
        return self.get(rule_id)  # type: ignore[return-value]

    async def edit(self, rule_id: int, changes: dict[str, Any], user_id: str | None = None) -> StoredWatchRule:
        old = self._require(rule_id)
        value = rule_to_mapping(old.rule)
        value.update(changes)
        rule = rule_from_mapping(value)
        now = datetime.now(UTC).isoformat()
        async with self._mutation_lock, self.database.write_lock:
            await self._connection().execute(
                "UPDATE watch_rules SET name=?,priority=?,rule_json=?,updated_at=?,discord_user_id=? WHERE id=?",
                (rule.name, rule.priority.value, json.dumps(rule_to_mapping(rule)), now, user_id, rule_id),
            )
            await self._connection().commit()
            await self._refresh()
        return self._require(rule_id)

    async def set_enabled(self, rule_id: int, enabled: bool, user_id: str | None = None) -> StoredWatchRule:
        self._require(rule_id)
        async with self._mutation_lock, self.database.write_lock:
            await self._connection().execute(
                "UPDATE watch_rules SET enabled=?,updated_at=?,discord_user_id=? WHERE id=?",
                (enabled, datetime.now(UTC).isoformat(), user_id, rule_id),
            )
            await self._connection().commit()
            await self._refresh()
        return self._require(rule_id)

    async def delete(self, rule_id: int) -> StoredWatchRule:
        old = self._require(rule_id)
        async with self._mutation_lock, self.database.write_lock:
            await self._connection().execute("DELETE FROM watch_rules WHERE id=?", (rule_id,))
            await self._connection().commit()
            await self._refresh()
        return old

    def export_yaml(self) -> str:
        return yaml.safe_dump({"products": [rule_to_mapping(item.rule) for item in self._items]},
                              sort_keys=False, allow_unicode=True)

    def _require(self, rule_id: int) -> StoredWatchRule:
        item = self.get(rule_id)
        if item is None:
            raise KeyError(f"watch rule {rule_id} does not exist")
        return item

    def _connection(self):
        if self.database.connection is None:
            raise RuntimeError("database is not connected")
        return self.database.connection
