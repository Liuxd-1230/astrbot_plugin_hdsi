"""Async SQLite storage for HDSI.

- aiosqlite, WAL journal
- one global write chain (WriteQueue) so SQLite has exactly one writer,
  matching the Koishi original's databaseWriteQueue
- bounded transient-error retries on reads and writes
- row <-> pydantic model mapping for the 11 domain tables
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import aiosqlite

from ..concurrency import WriteQueue, is_transient_db_error
from ..types import (
    InterludeArc,
    InterludeParticipant,
    InterludeScene,
    InterludeStory,
    NarrativeFact,
    NarrativeIntent,
    NarrativeMemory,
    OverlaySnapshot,
    ScriptEntry,
    StatePatchProposal,
    StoryState,
    StorySetting,
    ParticipantState,
    WebObservation,
    iso,
    normalize_participant_state,
    normalize_story_state,
    parse_date,
)
from .migrations import DDL, SCHEMA_VERSION

logger = logging.getLogger("hdsi.database")

MODEL_BY_TABLE = {
    "interlude_story": InterludeStory,
    "interlude_participant": InterludeParticipant,
    "interlude_script_entry": ScriptEntry,
    "interlude_memory": NarrativeMemory,
    "interlude_intent": NarrativeIntent,
    "interlude_scene": InterludeScene,
    "interlude_arc": InterludeArc,
    "interlude_fact": NarrativeFact,
    "interlude_state_patch": StatePatchProposal,
    "interlude_overlay_snapshot": OverlaySnapshot,
    "interlude_web_observation": WebObservation,
}

DATE_FIELDS: dict[str, tuple[str, ...]] = {
    "interlude_story": ("cursor_at", "created_at", "updated_at"),
    "interlude_participant": ("created_at", "updated_at"),
    "interlude_script_entry": ("occurred_at", "created_at"),
    "interlude_memory": ("created_at", "updated_at"),
    "interlude_intent": ("not_before", "created_at", "updated_at"),
    "interlude_scene": ("started_at", "ended_at", "created_at", "updated_at"),
    "interlude_arc": ("created_at", "updated_at"),
    "interlude_fact": ("last_seen_at", "created_at", "updated_at"),
    "interlude_state_patch": ("created_at", "applied_at"),
    "interlude_overlay_snapshot": ("period_start", "period_end", "created_at", "updated_at"),
    "interlude_web_observation": ("accessed_at", "created_at"),
}

JSON_FIELDS: dict[str, tuple[str, ...]] = {
    "interlude_story": ("setting", "state"),
    "interlude_participant": ("state",),
    "interlude_script_entry": ("metadata",),
    "interlude_intent": ("payload",),
    "interlude_fact": ("embedding", "source_entry_ids"),
    "interlude_state_patch": ("source_entry_ids",),
    "interlude_overlay_snapshot": ("major_events", "source_patch_ids"),
}

# TS-style column name -> python field name mapping per table.
COLUMN_ALIASES: dict[str, dict[str, str]] = {
    "*": {
        "storyId": "story_id",
        "participantId": "participant_id",
        "occurredAt": "occurred_at",
        "createdAt": "created_at",
        "updatedAt": "updated_at",
        "notBefore": "not_before",
        "sourceEntryId": "source_entry_id",
        "startedAt": "started_at",
        "endedAt": "ended_at",
        "entryCount": "entry_count",
        "lastEntryId": "last_entry_id",
        "sceneCount": "scene_count",
        "proposedValue": "proposed_value",
        "appliedAt": "applied_at",
        "sourceEntryIds": "source_entry_ids",
        "sourcePatchIds": "source_patch_ids",
        "periodStart": "period_start",
        "periodEnd": "period_end",
        "majorEvents": "major_events",
        "intentId": "intent_id",
        "accessedAt": "accessed_at",
        "platformId": "platform_id",
        "selfId": "self_id",
        "sessionKey": "session_key",
        "messageType": "message_type",
        "personId": "person_id",
        "displayName": "display_name",
        "cursorAt": "cursor_at",
    }
}


def _alias(table: str, column: str) -> str:
    return COLUMN_ALIASES.get(table, COLUMN_ALIASES["*"]).get(column, column)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._writes = WriteQueue(max_retries=7)
        self._closed = False
        # Test hook: when > 0, the next write submissions raise a transient
        # database error instead of executing (consumed one per attempt).
        self.fail_transient_writes = 0
        self.fail_all_writes = False
        # Test hook: when > 0 the next commit attempt raises a transient
        # error AFTER the statement executed (consumed one per attempt).
        self.fail_next_commit = 0

    # ------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        async with self._lock:
            if self._conn is not None:
                return
            conn = await aiosqlite.connect(self.path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=NORMAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.executescript(DDL)
            await conn.execute(
                "INSERT INTO hdsi_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
            await conn.commit()
            self._conn = conn

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                try:
                    await self._conn.close()
                finally:
                    self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None or self._closed:
            raise RuntimeError("Database is not connected")
        return self._conn

    # ------------------------------------------------------------ helpers

    def _retryable(self, error: BaseException) -> bool:
        return is_transient_db_error(error)

    async def _read(self, task: Callable[[], Any]) -> Any:
        delays = (0.05, 0.125, 0.25)
        attempt = 0
        while True:
            try:
                return await task()
            except Exception as error:
                if attempt >= len(delays) or not self._retryable(error):
                    raise
                await asyncio.sleep(delays[attempt])
                attempt += 1

    # ------------------------------------------------------------ raw SQL

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Serialized write with retry. Any failure rolls the statement back
        so a later unrelated commit cannot resurrect partial state."""

        async def task() -> None:
            if self.fail_all_writes:
                raise RuntimeError("disk I/O error (injected)")
            if self.fail_transient_writes > 0:
                self.fail_transient_writes -= 1
                raise RuntimeError("database is locked (injected transient)")
            conn = self.conn
            await conn.execute("BEGIN")
            try:
                await conn.execute(sql, params)
                await conn.commit()
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001 - rollback best-effort
                    pass
                raise

        await self._writes.submit(task, retryable=self._retryable)

    async def execute_many(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """Serialized all-or-nothing batch: explicit BEGIN + ROLLBACK so a
        failure can never leave half-applied statements to be committed by a
        later unrelated write (P0-2)."""

        async def task() -> None:
            if self.fail_all_writes:
                raise RuntimeError("disk I/O error (injected)")
            if self.fail_transient_writes > 0:
                self.fail_transient_writes -= 1
                raise RuntimeError("database is locked (injected transient)")
            conn = self.conn
            await conn.execute("BEGIN")
            try:
                for sql, params in statements:
                    await conn.execute(sql, params)
                await conn.commit()
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001 - rollback best-effort
                    pass
                raise

        await self._writes.submit(task, retryable=self._retryable)

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async def task() -> list[dict[str, Any]]:
            cursor = await self.conn.execute(sql, params)
            try:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
            finally:
                await cursor.close()

        return await self._read(task)

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[dict[str, Any]]:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------ generic CRUD

    async def get(
        self,
        table: str,
        query: dict[str, Any],
        limit: int | None = None,
        order_by: str | None = None,
        descending: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in query.items():
            if isinstance(value, (list, tuple, set)):
                values = list(value)
                if not values:
                    clauses.append("1=0")
                    continue
                marks = ",".join("?" for _ in values)
                clauses.append(f"{key} IN ({marks})")
                params.extend(values)
            elif isinstance(value, dict):
                for op, operand in value.items():
                    op_normalized = {"$gte": ">=", "$lte": "<=", "$gt": ">", "$lt": "<", "$ne": "!="}.get(op)
                    if op_normalized is None:
                        continue
                    clauses.append(f"{key} {op_normalized} ?")
                    params.append(_to_db_value(op_normalized, operand))
            else:
                clauses.append(f"{key} = ?")
                params.append(_to_db_value("=", value))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = ""
        if order_by:
            order = f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
        limit_sql = f" LIMIT {int(limit)}" if limit else ""
        rows = await self.fetch_all(f"SELECT * FROM {table}{where}{order}{limit_sql}", params)
        return [self.row_to_dict(table, row) for row in rows]

    async def insert(self, table: str, data: dict[str, Any]) -> dict[str, Any]:
        payload = _prepare_row(table, data)
        columns = ",".join(payload.keys())
        marks = ",".join("?" for _ in payload)
        await self.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({marks})",
            list(payload.values()),
        )
        return data

    async def insert_returning_id(self, table: str, data: dict[str, Any]) -> int:
        """Insert one row and return its generated rowid.

        The INSERT and the rowid read happen inside the SAME write-queue task,
        so concurrent stories can never observe each other's generated ids
        (P0-3). Callers that need the persisted row should use
        ``get`` afterwards with the returned id.
        """
        payload = _prepare_row(table, data)
        columns = ",".join(payload.keys())
        marks = ",".join("?" for _ in payload)

        async def task() -> int:
            if self.fail_all_writes:
                raise RuntimeError("disk I/O error (injected)")
            if self.fail_transient_writes > 0:
                self.fail_transient_writes -= 1
                raise RuntimeError("database is locked (injected transient)")
            conn = self.conn
            await conn.execute("BEGIN")
            try:
                cursor = await conn.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                    list(payload.values()),
                )
                rowid = int(cursor.lastrowid or 0)
                if self.fail_next_commit > 0:
                    self.fail_next_commit -= 1
                    raise RuntimeError("database is locked (injected commit failure)")
                await conn.commit()
                return rowid
            except BaseException:
                # P0-B: a failed COMMIT must roll the INSERT back, otherwise
                # the WriteQueue retry inserts a second identical row.
                try:
                    await conn.rollback()
                except Exception:  # noqa: BLE001 - rollback best-effort
                    pass
                raise

        return await self._writes.submit(task, retryable=self._retryable)

    async def update(self, table: str, query: dict[str, Any], data: dict[str, Any]) -> int:
        rows = await self.get(table, query)
        if not rows:
            return 0
        prepared = _prepare_row(table, data)
        sets = ",".join(f"{key} = ?" for key in prepared)
        statements: list[tuple[str, tuple[Any, ...]]] = []
        for row in rows:
            pk_columns = ("id",) if "id" in row else tuple(row.keys())
            where_sql = " AND ".join(f"{col} = ?" for col in pk_columns)
            params = [_to_db_value("=", value) for value in prepared.values()]
            params += [_to_db_value("=", row[col]) for col in pk_columns]
            statements.append((f"UPDATE {table} SET {sets} WHERE {where_sql}", params))
        await self.execute_many(statements)
        return len(statements)

    async def remove(self, table: str, query: dict[str, Any]) -> int:
        rows = await self.get(table, query)
        if not rows:
            return 0
        statements: list[tuple[str, tuple[Any, ...]]] = []
        for row in rows:
            pk = row.get("id")
            statements.append(("DELETE FROM " + table + " WHERE id = ?", (pk,)))
        await self.execute_many(statements)
        return len(statements)

    # ------------------------------------------------------------ mapping

    def row_to_dict(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in row.items():
            out[_alias(table, key)] = value
        for field in DATE_FIELDS.get(table, ()):  # type: ignore[arg-type]
            if out.get(field) is not None:
                parsed = parse_date(out[field])
                if parsed is not None:
                    out[field] = parsed
        for field in JSON_FIELDS.get(table, ()):  # type: ignore[arg-type]
            raw = out.get(field)
            if isinstance(raw, (str, bytes)) and raw:
                try:
                    out[field] = json.loads(raw)
                except (ValueError, TypeError):
                    out[field] = None if field == "embedding" else {}
            elif raw is None and field == "embedding":
                out[field] = []
        if table == "interlude_fact":
            out["unresolved"] = bool(out.get("unresolved"))
        if table == "interlude_story":
            out["status"] = out.get("status") or "active"
            out["setting"] = StorySetting.model_validate(out.get("setting") or {})
            out["state"] = normalize_story_state(out.get("state") or {})
        if table == "interlude_participant":
            out["state"] = normalize_participant_state(out.get("state") or {})
        if table == "interlude_intent":
            out["status"] = out.get("status") or "pending"
        return out


def _to_db_value(op: str, value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, bool):
        return 1 if value else 0
    if op != "=" and isinstance(value, (list, tuple)):
        raise ValueError("range operands must be scalars")
    return value


def _prepare_row(table: str, data: dict[str, Any]) -> dict[str, Any]:
    """Convert python values into SQLite-storable text for one row."""
    json_fields = JSON_FIELDS.get(table, ())
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in json_fields:
            if value is None:
                out[key] = None if key == "embedding" else "{}" if False else None
                if key != "embedding":
                    out[key] = "[]"
            elif isinstance(value, (dict, list)):
                out[key] = json.dumps(value, ensure_ascii=False)
            else:
                out[key] = value
        else:
            out[key] = _to_db_value("=", value)
    return out
