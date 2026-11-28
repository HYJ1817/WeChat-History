"""Thin, bounded, read-only primitives for discovered WeChat message tables.

The source, never a protocol client, chooses the paths and table names. LIMIT
bounds Python candidates, not SQLite work: missing indexes can still require a
scan/sort. All statements share a shard read transaction and a soft deadline.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from wechat_context_exporter.sources.base import sqlite_read_only_uri

FETCH_BATCH_SIZE = 128
MAX_RAW_MESSAGE_BYTES = 2 * 1024 * 1024
SQL_PROGRESS_OPCODES = 1_000
MIN_CURSOR_TIMESTAMP_US = -62_135_596_800_000_000
MAX_CURSOR_TIMESTAMP_US = 253_402_271_999_999_999


class MessageReadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class TableSchema:
    columns: frozenset[str]
    rowid_column: str | None
    timestamp_unit: str
    mixed_timestamp_units: bool
    legacy_scale: int
    legacy_order_safe: bool
    integer_values: bool
    precise_order_safe: bool = False

    @property
    def legacy_fast_path_supported(self) -> bool:
        return (
            self.rowid_column is not None and self.legacy_order_safe
            and not self.mixed_timestamp_units and self.integer_values
            and self.timestamp_unit in {"seconds", "milliseconds", "empty"}
        )

    @property
    def precise_cursor_supported(self) -> bool:
        return (
            self.rowid_column is not None and self.integer_values
            and not self.mixed_timestamp_units and self.precise_order_safe
            and self.timestamp_unit in {"seconds", "milliseconds", "microseconds", "empty"}
        )

    @property
    def precise_cursor_scale(self) -> int:
        return {
            "seconds": 1_000_000,
            "milliseconds": 1_000,
            "microseconds": 1,
            "empty": 1_000_000,
        }.get(self.timestamp_unit, 1)


class SQLiteShardReader:
    def __init__(self, db_path: Path, *, deadline: float | None = None) -> None:
        self.deadline = deadline
        self._timed_out = False
        self._closed = True
        connection = None
        try:
            connection = sqlite3.connect(sqlite_read_only_uri(db_path), uri=True, timeout=1.0)
            self.connection = connection
            connection.row_factory = sqlite3.Row
            connection.set_progress_handler(self._progress, SQL_PROGRESS_OPCODES)
            connection.execute("BEGIN")
            self._closed = False
        except sqlite3.Error:
            if connection is not None:
                connection.close()
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表结构不受支持。") from None

    def _progress(self) -> int:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self._timed_out = True
            return 1
        return 0

    def check_deadline(self) -> None:
        if self._timed_out or (self.deadline is not None and time.monotonic() >= self.deadline):
            raise MessageReadError("SEARCH_BUDGET_EXCEEDED", "读取消息超过时间预算，请缩小日期范围后重试。")

    @staticmethod
    def quote_identifier(identifier: str) -> str:
        if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表结构不受支持。")
        return '"' + identifier.replace('"', '""') + '"'

    def execute(self, sql: str, parameters: tuple | list = ()) -> sqlite3.Cursor:
        self.check_deadline()
        try:
            return self.connection.execute(sql, parameters)
        except sqlite3.Error:
            self.check_deadline()
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表结构不受支持。") from None

    def inspect_table(
        self, table: str, *, check_legacy_order: bool = True, verify_precise_order: bool = False
    ) -> TableSchema:
        quoted = self.quote_identifier(table)
        if self.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表结构不受支持。")
        columns = frozenset(str(row[1]) for row in self.execute(f"PRAGMA table_info({quoted})"))
        required = {"local_id", "create_time", "local_type", "real_sender_id", "message_content"}
        if not required.issubset(columns):
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表缺少必要字段，可能是版本不兼容。")
        if self.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'").fetchone() is None:
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表缺少联系人映射。")
        name_columns = {str(row[1]).casefold() for row in self.execute('PRAGMA table_info("Name2Id")')}
        # A declared rowid column can change affinity or fan out the JOIN
        # (e.g. text '2' and '02' both matching integer sender 2). Only the
        # unshadowed intrinsic integer rowid is proven unique for this join.
        join_unique = "rowid" not in name_columns
        rowid_column = next((name for name in ("_rowid_", "rowid", "oid") if name not in {c.casefold() for c in columns}), None)
        if not join_unique:
            rowid_column = None  # a fan-out JOIN has no unique message-row locator
        if rowid_column is not None:
            try:
                self.connection.execute(f"SELECT m.{self.quote_identifier(rowid_column)} FROM {quoted} m LIMIT 0")
            except sqlite3.Error:
                rowid_column = None  # WITHOUT ROWID or all rowid aliases shadowed
        precise = self.precise_time_expression()
        precise_check = (
            f", SUM(CASE WHEN typeof({precise})<>'integer' OR {precise}<? OR {precise}>? "
            "THEN 1 ELSE 0 END)"
            if verify_precise_order else ", 0"
        )
        precise_parameters = (MIN_CURSOR_TIMESTAMP_US, MAX_CURSOR_TIMESTAMP_US) if verify_precise_order else ()
        row = self.execute(
            f"SELECT MAX(create_time), "
            f"SUM(CASE WHEN create_time<>0 AND ABS(create_time)<=10000000000 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN ABS(create_time)>10000000000 AND ABS(create_time)<=100000000000000 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN ABS(create_time)>100000000000000 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN typeof(create_time)<>'integer' OR typeof(local_id)<>'integer' OR create_time<0 THEN 1 ELSE 0 END)"
            f"{precise_check} FROM {quoted} m",
            precise_parameters,
        ).fetchone()
        units = tuple(int(value or 0) for value in row[1:4])
        mixed = sum(bool(count) for count in units) > 1
        unit = "mixed" if mixed else next((label for label, count in zip(("seconds", "milliseconds", "microseconds"), units) if count), "empty")
        # The reference orders raw timestamps/local_id before a stable Python
        # (truncated-second, local_id) sort. Exact raw ties have unspecified SQL
        # order, so B1 falls back instead of inventing a tie-breaker for them.
        # Cursor pagination has its own verified physical-row identity and does
        # not pay for this legacy-only grouping scan.
        tied = None
        if check_legacy_order:
            tied = self.execute(
                f"SELECT 1 FROM {quoted} GROUP BY create_time, local_id HAVING COUNT(*)>1 LIMIT 1"
            ).fetchone()
        precise_order_safe = verify_precise_order and not bool(row[5])
        try:
            maximum_timestamp = int(row[0] or 0)
        except (TypeError, ValueError, OverflowError):
            maximum_timestamp = 0
        scale = 1000 if maximum_timestamp > 10_000_000_000 else 1
        return TableSchema(
            columns, rowid_column, unit, mixed, scale,
            check_legacy_order and tied is None, not bool(row[4]), precise_order_safe,
        )

    def own_sender_id(self, wxid: str | None) -> int | None:
        row = self.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (wxid,)).fetchone()
        return int(row[0]) if row else None

    def count_rows(self, table: str, schema: TableSchema, start: int | None, end: int | None, *, precise: bool = False) -> int:
        where, parameters = self._where(schema, start, end, precise=precise)
        row = self.execute(
            f"SELECT COUNT(*) FROM {self.quote_identifier(table)} m "
            f"LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id{where}", parameters,
        ).fetchone()
        return int(row[0] or 0)

    def count_cursor_rows(self, table: str, schema: TableSchema, start: int | None, end: int | None) -> int:
        """Count exact precise-v1 candidates without a sender-map join."""
        if not schema.precise_cursor_supported:
            raise MessageReadError("PAGE_UNSUPPORTED_SCHEMA", "此消息表无法安全使用稳定游标分页。")
        where, parameters = self._cursor_date_where(schema, start, end)
        row = self.execute(
            f"SELECT COUNT(*) FROM {self.quote_identifier(table)} m{where}", parameters,
        ).fetchone()
        return int(row[0] or 0)

    def iter_keyset_rows(
        self,
        table: str,
        schema: TableSchema,
        start: int | None,
        end: int | None,
        *,
        boundary_time_us: int | None,
        boundary_rowid: int | None,
        location_relation: int,
        key_relation: str,
        reverse: bool,
        limit: int,
    ) -> Iterator[sqlite3.Row]:
        """Yield a bounded, metadata-only slice after/before a precise key.

        ``location_relation`` compares this table's stable source/table key to
        the boundary's source/table key (-1, 0, +1). Timestamp units are schema
        gated, so raw integer time retains exact ordering and can use its index.
        """
        if not schema.precise_cursor_supported or schema.rowid_column is None:
            raise MessageReadError("PAGE_UNSUPPORTED_SCHEMA", "此消息表无法安全使用稳定游标分页。")
        if key_relation not in {"gt", "lt"}:
            raise MessageReadError("PAGE_UNSUPPORTED_SCHEMA", "分页游标无效。")
        if (boundary_time_us is None) != (boundary_rowid is None):
            raise MessageReadError("PAGE_UNSUPPORTED_SCHEMA", "分页游标无效。")
        where, parameters = self._cursor_date_where(schema, start, end)
        if boundary_time_us is not None:
            predicate, boundary_parameters = self._keyset_predicate(
                schema, boundary_time_us, int(boundary_rowid), location_relation, key_relation,
            )
            where += (" AND " if where else " WHERE ") + predicate
            parameters.extend(boundary_parameters)
        direction = "DESC" if reverse else "ASC"
        quoted_table = self.quote_identifier(table)
        rowid = self.quote_identifier(schema.rowid_column)
        timestamp = "m.create_time"
        server_id = "m.server_id" if "server_id" in schema.columns else "0"
        cursor = self.execute(
            f"SELECT m.local_id, m.create_time, m.local_type, m.real_sender_id, "
            f"{server_id} AS server_id, m.{rowid} AS physical_rowid "
            f"FROM {quoted_table} m{where} "
            f"ORDER BY {timestamp} {direction}, m.{rowid} {direction} LIMIT ?",
            (*parameters, max(0, int(limit))),
        )
        try:
            while True:
                self.check_deadline()
                rows = cursor.fetchmany(FETCH_BATCH_SIZE)
                if not rows:
                    break
                for row in rows:
                    self.check_deadline()
                    yield row
        except sqlite3.Error:
            self.check_deadline()
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表结构不受支持。") from None
        finally:
            cursor.close()

    @classmethod
    def _cursor_date_where(
        cls, schema: TableSchema, start: int | None, end: int | None
    ) -> tuple[str, list[int]]:
        """Translate whole-second date bounds exactly for a uniform-unit table."""
        scale = schema.precise_cursor_scale
        clauses, parameters = [], []
        if start is not None:
            clauses.append("m.create_time>=?")
            parameters.append(cls._ceil_div(int(start) * 1_000_000, scale))
        if end is not None:
            clauses.append("m.create_time<?")
            parameters.append(cls._ceil_div(int(end) * 1_000_000, scale))
        return ((" WHERE " + " AND ".join(clauses)) if clauses else "", parameters)

    @staticmethod
    def _ceil_div(numerator: int, denominator: int) -> int:
        return -((-numerator) // denominator)

    @classmethod
    def _keyset_predicate(
        cls,
        schema: TableSchema,
        boundary_time_us: int,
        boundary_rowid: int,
        location_relation: int,
        key_relation: str,
    ) -> tuple[str, list[int]]:
        scale = schema.precise_cursor_scale
        floor_time = boundary_time_us // scale
        ceil_time = cls._ceil_div(boundary_time_us, scale)
        divisible = boundary_time_us % scale == 0
        rowid = f"m.{cls.quote_identifier(schema.rowid_column or '')}"
        raw_time = "m.create_time"
        if key_relation == "gt":
            if location_relation < 0:
                return f"{raw_time}>?", [floor_time]
            if location_relation > 0:
                return f"{raw_time}>=?", [ceil_time]
            if divisible:
                return f"({raw_time}>? OR ({raw_time}=? AND {rowid}>?))", [floor_time, floor_time, boundary_rowid]
            return f"{raw_time}>?", [floor_time]
        if location_relation < 0:
            return f"{raw_time}<=?", [floor_time]
        if location_relation > 0:
            return f"{raw_time}<?", [ceil_time]
        if divisible:
            return f"({raw_time}<? OR ({raw_time}=? AND {rowid}<?))", [ceil_time, floor_time, boundary_rowid]
        return f"{raw_time}<?", [ceil_time]

    def iter_rows(
        self, table: str, schema: TableSchema, start: int | None, end: int | None,
        *, precise: bool = False, legacy_order: str | None = None,
        limit: int | None = None, metadata_only: bool = False,
        physical_rowid: int | None = None,
    ) -> Iterator[sqlite3.Row]:
        selected = ["m.local_id", "m.create_time", "m.local_type", "m.real_sender_id"]
        if not metadata_only:
            # Check SQL byte lengths before promoting BLOBs to Python. Oversize
            # rows retain an error flag and are never silently omitted.
            for column, alias in (("message_content", "message_content"), ("packed_info_data", "packed_info_data")):
                if column in schema.columns:
                    expression = f"m.{self.quote_identifier(column)}"
                    selected.extend((
                        f"CASE WHEN length(CAST({expression} AS BLOB))<= {MAX_RAW_MESSAGE_BYTES} THEN {expression} ELSE NULL END AS {alias}",
                        f"length(CAST({expression} AS BLOB))>{MAX_RAW_MESSAGE_BYTES} AS {alias}_oversize",
                    ))
                else:
                    selected.extend((f"NULL AS {alias}", f"0 AS {alias}_oversize"))
            selected.append("n.user_name AS sender_wxid")
            selected.append('m."WCDB_CT_message_content" AS content_compression' if "WCDB_CT_message_content" in schema.columns else "0 AS content_compression")
        selected.append("m.server_id" if "server_id" in schema.columns else "0 AS server_id")
        if schema.rowid_column is not None:
            selected.append(f"m.{self.quote_identifier(schema.rowid_column)} AS physical_rowid")
        where, parameters = self._where(schema, start, end, precise=precise)
        if physical_rowid is not None:
            if schema.rowid_column is None:
                raise MessageReadError("SEARCH_UNSUPPORTED_SCHEMA", "此消息表没有可验证的唯一行身份，无法精确定位。")
            where += (" AND " if where else " WHERE ") + f"m.{self.quote_identifier(schema.rowid_column)}=?"
            parameters.append(physical_rowid)
        order_sql = ""
        if legacy_order is not None:
            if legacy_order not in {"asc", "desc"} or not schema.legacy_fast_path_supported:
                raise MessageReadError("WECHAT_DB_UNSUPPORTED", "此消息表无法安全使用兼容分页优化。")
            direction = "ASC" if legacy_order == "asc" else "DESC"
            stamp = "m.create_time" if schema.legacy_scale == 1 else "CAST(m.create_time/1000 AS INTEGER)"
            order_sql = f" ORDER BY {stamp} {direction}, m.local_id {direction}, m.create_time {direction}"
        elif not precise:
            order_sql = " ORDER BY m.create_time ASC, m.local_id ASC"
        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            parameters.append(max(0, int(limit)))
        cursor = self.execute(
            f"SELECT {', '.join(selected)} FROM {self.quote_identifier(table)} m "
            f"LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id{where}{order_sql}{limit_sql}", parameters,
        )
        try:
            while True:
                self.check_deadline()
                # Large content rows get a smaller fixed batch; metadata-only
                # scans can safely use the normal batch without retaining BLOBs.
                rows = cursor.fetchmany(FETCH_BATCH_SIZE if metadata_only else 8)
                if not rows:
                    break
                for row in rows:
                    self.check_deadline()
                    if not metadata_only and (row["message_content_oversize"] or row["packed_info_data_oversize"]):
                        raise MessageReadError("MESSAGE_TOO_LARGE", "单条消息内容超过安全读取上限。")
                    yield row
        except sqlite3.Error:
            self.check_deadline()
            raise MessageReadError("WECHAT_DB_UNSUPPORTED", "微信消息表结构不受支持。") from None
        finally:
            cursor.close()

    @staticmethod
    def precise_time_expression() -> str:
        stamp = "m.create_time"
        return (f"CASE WHEN ABS({stamp})>100000000000000 THEN {stamp} "
                f"WHEN ABS({stamp})>10000000000 THEN {stamp}*1000 ELSE {stamp}*1000000 END")

    @classmethod
    def _where(cls, schema: TableSchema, start: int | None, end: int | None, *, precise: bool) -> tuple[str, list[int]]:
        expression = cls.precise_time_expression() if precise else "m.create_time"
        scale = 1_000_000 if precise else schema.legacy_scale
        clauses, parameters = [], []
        if start is not None:
            clauses.append(f"{expression}>=?")
            parameters.append(start * scale)
        if end is not None:
            clauses.append(f"{expression}<?")
            parameters.append(end * scale)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", parameters

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.connection.set_progress_handler(None, 0)
            self.connection.close()

    def __enter__(self) -> "SQLiteShardReader":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def epoch_microseconds(timestamp: int) -> int:
    value = int(timestamp)
    magnitude = abs(value)
    if magnitude > 100_000_000_000_000:
        return value
    return value * (1_000 if magnitude > 10_000_000_000 else 1_000_000)


def legacy_timestamp_seconds(timestamp: int) -> int:
    value = int(timestamp)
    return value // 1_000 if value > 10_000_000_000 else value
