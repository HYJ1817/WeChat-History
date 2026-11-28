#!/usr/bin/env python3
"""Reproducible synthetic-only message-query benchmarks; no account discovery.

Run with the project venv, -I and -B. Each case runs in a fresh subprocess;
fixtures live only in a new TemporaryDirectory and are always removed. There
is deliberately no --database, --account, --source or existing-fixture option.

Measures production legacy/B1/B2 source methods, reader/parser, A helpers, and
B3 keyword snapshot/cache helpers against generated SQLite shards. Global search
uses a SYNTHETIC HARNESS document adapter, not the worker's A command. B2/B3 call
production source/service implementations; stdio/UI, decryption and real media
are NOT benchmarked. resolve() calls use no-I/O stubs. SQL yielded rows are NOT
SQLite scan counts; progress callbacks approximate VM instructions, not rows.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from html import escape
import importlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
import tracemalloc
from types import SimpleNamespace
from typing import Any

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
FORMAT = "wechat-history-synthetic-benchmark-v1"
DENSE = "BENCH_COMMON"
SPARSE = "Bench_命中_Straße🙂"
QUERIES = {"sparse": SPARSE.casefold(), "dense": DENSE, "none": "BENCH_NO_MATCH_8d70c1"}
CASES = (
    "legacy_first", "legacy_deep", "b1_first", "b1_deep",
    "b2_first", "b2_deep", "b2_reverse",
    "keyword_legacy_first", "keyword_legacy_deep", "b3_keyword_first",
    "b3_keyword_continuation", "global_sparse", "global_dense", "global_none",
    "global_cached_page", "parser_sample",
)
SOURCE_FILES = (
    "scripts/Benchmark-MessageQueries.py", "src/PythonWorker/worker.py",
    "src/PythonWorker/wechat_memory/wechat4_source.py",
    "src/PythonWorker/wechat_memory/message_reader.py",
    "src/PythonWorker/wechat_memory/global_search.py",
    "src/PythonWorker/wechat_memory/page_sessions.py",
    "src/PythonWorker/wechat_memory/appmsg_parser.py",
    "src/PythonWorker/vendor/wechat_context_exporter/sources/wechat4_crypto.py",
)


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_hashes() -> dict[str, str | None]:
    return {name: file_hash(ROOT / name) if (ROOT / name).is_file() else None for name in SOURCE_FILES}


def production() -> tuple[Any, Any, Any, Any]:
    # Explicit repository code only; -I prevents cwd/PYTHONPATH import injection.
    worker = ROOT / "src" / "PythonWorker"
    sys.path[:0] = [str(worker), str(worker / "vendor")]
    return tuple(importlib.import_module(name) for name in (
        "wechat_memory.wechat4_source", "wechat_memory.message_reader",
        "wechat_memory.global_search", "wechat_context_exporter.models",
    ))


def conversation_id(index: int) -> str:
    return f"bench-{index:04d}" + ("@chatroom" if index % 2 else "")


def table_name(index: int) -> str:
    return "Msg_" + hashlib.md5(conversation_id(index).encode("utf-8")).hexdigest()


def synthetic_message(index: int, config: dict[str, Any]) -> tuple[int, str, bool, bool]:
    """Only deterministic generated literals; never read user content."""
    bucket = index % 100
    card = bucket < config["card_percent"]
    media = config["card_percent"] <= bucket < config["card_percent"] + config["media_percent"]
    sparse = (index + config["seed"]) % config["sparse_every"] == 0
    body = f"{DENSE} synthetic message {index}" + (f" {SPARSE}" if sparse else "")
    if config["long_every"] and index % config["long_every"] == 0:
        body += " x" * (config["long_chars"] // 2)
    if card:
        subtype = (5, 6, 57, 19)[(index // config["conversations"]) % 4]
        if subtype == 5:
            fields = f"<title>{DENSE} link</title><des>{escape(body)}</des><url>https://example.invalid/bench</url>"
        elif subtype == 6:
            fields = f"<title>{escape(body)}.txt</title><appattach><totallen>1234</totallen></appattach>"
        elif subtype == 57:
            fields = (f"<title>{DENSE} reply</title><refermsg><type>1</type><svrid>{index + 1}</svrid>"
                      f"<displayname>synthetic peer</displayname><content>{'q' * 96}{escape(body)}</content></refermsg>")
        else:
            record = ("<recordinfo><title>synthetic record</title><datalist count=\"2\">"
                      "<dataitem datatype=\"1\"><dataitemsource><sourcename>inner peer</sourcename>"
                      "<sourcetime>2026-01-01 00:00:00</sourcetime></dataitemsource>"
                      f"<datadesc>{escape(body)}</datadesc></dataitem>"
                      "<dataitem datatype=\"2\"><datatitle>synthetic image</datatitle></dataitem>"
                      "</datalist></recordinfo>")
            fields = f"<title>{DENSE} forward</title><recorditem>{escape(record)}</recorditem>"
        body = f"<msg><appmsg><type>{subtype}</type>{fields}</appmsg></msg>"
        raw_type = (subtype << 32) | 49
    elif media:
        raw_type = 3 if (index // config["conversations"]) % 2 else 34
        body = "[synthetic image]" if raw_type == 3 else '<msg><voicemsg voicelength="1234"/></msg>'
    else:
        raw_type = 1
    return raw_type, body, sparse and not media, not media


def generate(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    zstd = importlib.import_module("zstandard") if config["compress_every"] else None
    compressor = zstd.ZstdCompressor(level=1) if zstd else None
    start = time.perf_counter_ns()
    logical = hashlib.sha256()
    connections: list[sqlite3.Connection] = []
    sparse_total = dense_total = stored_bytes = raw_bytes = 0
    # Timestamps strictly increase with generated index (conversations <=64,
    # ordinal step 137ms). Keep only the last two pages for a bounded, independent
    # newest-first identity oracle; no parser/search code builds this oracle.
    newest = {name: deque(maxlen=2 * config["page_size"]) for name in QUERIES}
    keyword_newest = {
        name: [deque(maxlen=2 * config["page_size"]) for _ in range(config["conversations"])]
        for name in ("sparse", "dense")
    }
    matches_by_conversation = {name: [0] * config["conversations"] for name in ("sparse", "dense")}
    counts = [0] * config["conversations"]
    paths = [root / f"shard_{s:03d}.sqlite" for s in range(config["shards"])]
    try:
        for path in paths:
            connection = sqlite3.connect(path)
            connections.append(connection)
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("CREATE TABLE benchmark_meta (marker TEXT NOT NULL)")
            connection.execute("INSERT INTO benchmark_meta VALUES (?)", (FORMAT,))
            connection.execute("CREATE TABLE Name2Id (user_name TEXT NOT NULL)")
            connection.executemany("INSERT INTO Name2Id VALUES (?)", [("bench_self",), ("bench_peer",)])
            for c in range(config["conversations"]):
                table = table_name(c)
                connection.execute(f'CREATE TABLE "{table}" (local_id INTEGER, create_time INTEGER, '
                                   'local_type INTEGER, real_sender_id INTEGER, message_content BLOB, '
                                   'packed_info_data BLOB, server_id INTEGER, WCDB_CT_message_content INTEGER)')
        batches: dict[tuple[int, int], list[tuple[Any, ...]]] = {}
        pending = 0
        for index in range(config["rows"]):
            c = index % config["conversations"]
            ordinal = index // config["conversations"]
            shard = ordinal % config["shards"]
            local_id = ordinal // config["shards"] + 1
            raw_type, text, sparse, dense = synthetic_message(index, config)
            sparse_total += sparse
            dense_total += dense
            counts[c] += 1
            timestamp = 1_767_225_600_000 + ordinal * 137 + c
            identity = [conversation_id(c), str(local_id), timestamp * 1000]
            if sparse:
                newest["sparse"].append(identity)
                keyword_newest["sparse"][c].append(identity)
                matches_by_conversation["sparse"][c] += 1
            if dense:
                newest["dense"].append(identity)
                keyword_newest["dense"][c].append(identity)
                matches_by_conversation["dense"][c] += 1
            if c % 2:
                text = "bench_peer:\n" + text
            data = text.encode("utf-8")
            raw_bytes += len(data)
            logical.update(encoded([index, raw_type, text]))
            compressed = bool(compressor and index % config["compress_every"] == 0)
            value = compressor.compress(data) if compressed else text
            stored_bytes += len(value) if isinstance(value, bytes) else len(data)
            # Repeated local/server IDs across shards/conversations are intentional.
            row = (local_id, timestamp, raw_type, 1 if index % 7 == 0 else 2,
                   value, b"", local_id, 4 if compressed else 0)
            batches.setdefault((shard, c), []).append(row)
            pending += 1
            if pending >= 1024:
                # Bound generation memory globally, not 256 rows per table.
                for (batch_shard, batch_c), batch in batches.items():
                    if batch:
                        connections[batch_shard].executemany(
                            f'INSERT INTO "{table_name(batch_c)}" VALUES (?,?,?,?,?,?,?,?)', batch)
                        batch.clear()
                pending = 0
        for (shard, c), batch in batches.items():
            if batch:
                connections[shard].executemany(f'INSERT INTO "{table_name(c)}" VALUES (?,?,?,?,?,?,?,?)', batch)
        for connection in connections:
            for c in range(config["conversations"]):
                if config["index"] == "time":
                    table = table_name(c)
                    # Both precise raw-time and legacy truncated-second order.
                    connection.execute(f'CREATE INDEX "idx_raw_{c}" ON "{table}" (create_time,local_id)')
                    connection.execute(f'CREATE INDEX "idx_legacy_{c}" ON "{table}" '
                                       '(CAST(create_time/1000 AS INTEGER),local_id,create_time)')
            connection.commit()
    finally:
        for connection in connections:
            connection.close()
    return {
        "generation_ms": (time.perf_counter_ns() - start) / 1e6,
        "logical_sha256": logical.hexdigest(), "rows": config["rows"],
        "conversation_rows": counts, "expected_matches": {"sparse": sparse_total, "dense": dense_total, "none": 0},
        "expected_identity_sha256": {name: {
            "first": hashlib.sha256(encoded(list(reversed(keys))[:config["page_size"]])).hexdigest(),
            "second": hashlib.sha256(encoded(list(reversed(keys))[config["page_size"]:2 * config["page_size"]])).hexdigest(),
        } for name, keys in newest.items()},
        "expected_matches_by_conversation": matches_by_conversation,
        "expected_keyword_identity_sha256": {
            name: {str(c): {
                "first": hashlib.sha256(encoded(list(reversed(keys))[:config["page_size"]])).hexdigest(),
                "second": hashlib.sha256(encoded(list(reversed(keys))[config["page_size"]:2 * config["page_size"]])).hexdigest(),
            } for c, keys in enumerate(conversations)}
            for name, conversations in keyword_newest.items()
        },
        "raw_content_bytes": raw_bytes, "stored_content_bytes": stored_bytes,
        "database_bytes": sum(path.stat().st_size for path in paths),
        "files": {path.name: file_hash(path) for path in paths},
    }


class Metrics:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.times: dict[str, int] = {}
        self.statements: dict[str, tuple[str, tuple[Any, ...]]] = {}

    def inc(self, key: str, value: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + value

    def timed(self, key: str, function: Any, *args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter_ns()
        try:
            return function(*args, **kwargs)
        finally:
            self.times[key] = self.times.get(key, 0) + time.perf_counter_ns() - start

    def snapshot(self) -> dict[str, Any]:
        keys = ("python_rows_yielded", "metadata_rows_yielded", "body_rows_yielded",
                "keyset_rows_yielded", "keyword_documents_yielded", "transform_calls",
                "image_resolve_calls", "voice_resolve_calls", "transcript_load_calls", "parser_calls",
                "decode_calls", "database_acquisitions", "sql_statements", "sql_count_calls",
                "cursor_count_calls", "schema_inspections", "snapshot_validations",
                "sql_progress_callbacks", "retained_search_hits")
        counts = {key: self.counts.get(key, 0) for key in keys}
        counts["media_resolve_calls"] = counts["image_resolve_calls"] + counts["voice_resolve_calls"]
        counts["approx_sql_vm_instructions"] = counts["sql_progress_callbacks"] * 1000
        return {"counts": counts, "timing_ms_non_additive": {key: value / 1e6 for key, value in self.times.items()}}


def instrument(source_module: Any, reader_module: Any, metrics: Metrics) -> None:
    cls = reader_module.SQLiteShardReader
    original_execute, original_iter = cls.execute, cls.iter_rows
    original_keyset, original_progress = cls.iter_keyset_rows, cls._progress

    def execute(self: Any, sql: str, parameters: Any = ()) -> Any:
        metrics.inc("sql_statements")
        # Store at most one example per SELECT shape, for EXPLAIN after timing.
        if sql.startswith("SELECT") and len(metrics.statements) < 32:
            metrics.statements.setdefault(sql, (sql, tuple(parameters)))
        return metrics.timed("sql_execute", original_execute, self, sql, parameters)

    def iterate(self: Any, *args: Any, **kwargs: Any) -> Any:
        iterator = original_iter(self, *args, **kwargs)
        try:
            while True:
                try:
                    row = metrics.timed("sql_iteration", next, iterator)
                except StopIteration:
                    return
                metrics.inc("python_rows_yielded")
                metrics.inc("metadata_rows_yielded" if kwargs.get("metadata_only") else "body_rows_yielded")
                yield row
        finally:
            iterator.close()

    def iterate_keyset(self: Any, *args: Any, **kwargs: Any) -> Any:
        iterator = original_keyset(self, *args, **kwargs)
        try:
            while True:
                try:
                    row = metrics.timed("keyset_iteration", next, iterator)
                except StopIteration:
                    return
                metrics.inc("keyset_rows_yielded")
                metrics.inc("metadata_rows_yielded")
                yield row
        finally:
            iterator.close()

    def progress(self: Any) -> int:
        metrics.inc("sql_progress_callbacks")
        return original_progress(self)

    cls.execute, cls.iter_rows, cls.iter_keyset_rows, cls._progress = execute, iterate, iterate_keyset, progress
    for name, counter, timing in (("count_rows", "sql_count_calls", "sql_count"),
                                   ("count_cursor_rows", "cursor_count_calls", "cursor_count"),
                                   ("inspect_table", "schema_inspections", "sql_schema")):
        original = getattr(cls, name)

        def wrapper(self: Any, *args: Any, _original: Any = original, _counter: str = counter,
                    _timing: str = timing, **kwargs: Any) -> Any:
            metrics.inc(_counter)
            return metrics.timed(_timing, _original, self, *args, **kwargs)
        setattr(cls, name, wrapper)
    original_transform = source_module.WeChat4Source._row_to_message

    def transform(self: Any, *args: Any, **kwargs: Any) -> Any:
        metrics.inc("transform_calls")
        return metrics.timed("transform", original_transform, self, *args, **kwargs)

    source_module.WeChat4Source._row_to_message = transform
    for name, counter, timing in (("parse_appmsg", "parser_calls", "parser"),
                                   ("_decode_content", "decode_calls", "decode")):
        original = getattr(source_module, name)

        def wrapper(*args: Any, _original: Any = original, _counter: str = counter,
                    _timing: str = timing, **kwargs: Any) -> Any:
            metrics.inc(_counter)
            return metrics.timed(_timing, _original, *args, **kwargs)
        setattr(source_module, name, wrapper)


class SyntheticDatabases:
    def __init__(self, root: Path, shards: int, metrics: Metrics) -> None:
        self.paths = {f"message\\message_{s:03d}.db": root / f"shard_{s:03d}.sqlite" for s in range(shards)}
        self.metrics = metrics

    def get(self, name: str) -> Path:
        self.metrics.inc("database_acquisitions")
        return self.paths[name]  # Unknown paths fail closed; no directory discovery.

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int]:
        info = path.stat()
        return info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0)

    def get_snapshot(self, name: str) -> Any:
        path = self.get(name)
        return SimpleNamespace(path=path, generation=1, signature=self._signature(path))

    def is_snapshot_current(self, name: str, snapshot: Any) -> bool:
        self.metrics.inc("snapshot_validations")
        try:
            path = self.paths[name]
            return (snapshot.path == path and snapshot.generation == 1
                    and snapshot.signature == self._signature(path))
        except (KeyError, OSError, AttributeError):
            return False


class NoIOMedia:
    def __init__(self, counter: str, metrics: Metrics) -> None:
        self.counter, self.metrics = counter, metrics

    def resolve(self, *_args: Any) -> None:
        self.metrics.inc(self.counter)
        return None

    def load(self, *_args: Any) -> None:
        self.metrics.inc("transcript_load_calls")
        return None


def synthetic_source(root: Path, config: dict[str, Any], modules: Any, metrics: Metrics) -> Any:
    source_module, _reader, _search, models = modules
    # Bypass __init__: even PrivateTempRoot/product-data initialization is avoided.
    source = source_module.WeChat4Source.__new__(source_module.WeChat4Source)
    source._connected = True
    source._page_generation = 1
    source._media_revision = 0
    source._cursor_sessions = source_module.PageSessionManager()
    source._own_wxid = "bench_self"
    source._contacts = {"bench_peer": "synthetic peer", "bench_self": "synthetic self"}
    source._databases = SyntheticDatabases(root, config["shards"], metrics)
    source._images = NoIOMedia("image_resolve_calls", metrics)
    source._voices = NoIOMedia("voice_resolve_calls", metrics)
    source._transcripts = NoIOMedia("transcript_load_calls", metrics)
    source.image_key_available = False
    source.voice_available = False
    source._conversations = [models.Conversation(conversation_id(c), f"synthetic conversation {c}",
                             models.ConversationKind.GROUP if c % 2 else models.ConversationKind.DIRECT)
                             for c in range(config["conversations"])]
    source._locations = {conversation_id(c): [(relative, table_name(c)) for relative in source._databases.paths]
                         for c in range(config["conversations"])}
    return source


def message_wire(message: Any) -> dict[str, Any]:
    # Harness serialization, NOT a claim that worker optional fields are wired up.
    return {"id": message.id, "sender": message.sender, "timestamp": message.timestamp.isoformat(),
            "content": message.content, "kind": message.type.value, "isOutgoing": message.is_outgoing,
            "replyTo": message.reply_to, "transcript": message.transcript}


def documents(source: Any, config: dict[str, Any], modules: Any, deadline: float) -> Any:
    """Harness adapter: production reader + converter; no live media or cache I/O."""
    _source_module, reader_module, search, _models = modules
    with ExitStack() as stack:
        for shard, relative in enumerate(source._databases.paths):
            reader = stack.enter_context(reader_module.SQLiteShardReader(source._databases.get(relative), deadline=deadline))
            own = reader.own_sender_id(source._own_wxid)
            for c, conversation in enumerate(source._conversations):
                table = table_name(c)
                schema = reader.inspect_table(table)
                for row in reader.iter_rows(table, schema, None, None, precise=True):
                    converted = source._row_to_message(conversation, row, own, relative, enrich_media=False, precise=True)
                    message = message_wire(converted.message)
                    locator = (shard, table, int(row["physical_rowid"]))
                    yield search.SearchDocument(
                        {"id": conversation.id, "title": conversation.name, "kind": conversation.kind.value},
                        message, (reader_module.epoch_microseconds(row["create_time"]), conversation.id,
                                  f"shard-{shard:03d}", table, int(row["physical_rowid"])),
                        locator, search.content_fingerprint(*locator, row["create_time"], row["message_content"]),
                        converted.parse_status, converted.content_truncated,
                    )


def prepare_keyword_session(source: Any, config: dict[str, Any], modules: Any, metrics: Metrics) -> Any:
    """Production B3 scan/cache adapter over the synthetic WeChat4 source."""
    source_module, _reader, search, _models = modules
    query = QUERIES["sparse"]
    conversation = conversation_id(0)
    generations: dict[str, Any] = {}
    criteria = search.KeywordSearchCriteria(
        query, None, None, "desc", conversation_id=conversation,
        media_selection=source.keyword_media_selection(),
    )
    deadline = time.monotonic() + config["timeout"]
    documents = source.iter_keyword_documents(
        conversation, None, None, deadline=deadline, generations=generations,
    )

    def counted_documents() -> Any:
        for document in documents:
            metrics.inc("keyword_documents_yielded")
            yield document

    entries, total, candidates = search.scan_keyword_documents(
        counted_documents(), query, "desc", deadline=deadline, max_candidates=source_module.MAX_MESSAGES,
    )
    cache = search.KeywordPageCache()
    session = cache.create(
        "wechat4", "synthetic-source-v1", entries, total, candidates, generations, criteria,
    )
    return cache, session, generations, criteria


def keyword_cursor_page(source: Any, cache: Any, session: Any, generations: Any,
                        criteria: Any, cursor: str | None, limit: int) -> dict[str, Any]:
    source.validate_keyword_generations(generations)
    return cache.cursor_page(
        session.page_session_id, "wechat4", "synthetic-source-v1", generations, criteria,
        cursor=cursor, limit=limit,
    )


def rss_peak_bytes() -> int | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            size_t = ctypes.c_size_t

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (name, size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                              "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                              "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            current = ctypes.windll.kernel32.GetCurrentProcess
            current.restype = ctypes.c_void_p
            query = ctypes.windll.psapi.GetProcessMemoryInfo
            query.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), wintypes.DWORD]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if query(current(), ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
        except (AttributeError, OSError):
            return None
    else:
        try:
            import resource
            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return int(value if sys.platform == "darwin" else value * 1024)
        except (ImportError, AttributeError, OSError):
            return None
    return None


def parser_samples(source_module: Any, config: dict[str, Any]) -> dict[str, Any]:
    fixed = {**config, "card_percent": 100, "media_percent": 0, "long_every": 0, "conversations": 1}
    samples = [synthetic_message(i, fixed)[:2] for i in range(4)]
    samples.extend([
        (49, '<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///BENCH_NEVER_OPEN">]><msg>&x;</msg>'),
        (49, "<msg><broken>"),
        (49, "<x>" * 33 + "deep" + "</x>" * 33),
    ])
    items = []
    for raw_type, text in samples:
        projection = source_module.parse_appmsg(text, raw_type)
        items.append({"status": projection.parse_status, "truncated": projection.truncated,
                      "canonicalBytes": len(projection.canonical_text.encode("utf-8")),
                      "kind": projection.display_kind})
    return {"items": items, "total": len(items), "nextOffset": None}


def explain(root: Path, statements: Any) -> list[dict[str, Any]]:
    # Collected after measurement. All generated shards have identical schema.
    with sqlite3.connect((root / "shard_000.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
        results = []
        for sql, parameters in statements:
            try:
                plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)]
            except sqlite3.Error:
                plan = ["EXPLAIN unavailable"]
            results.append({"sql": sql, "plan": plan})
        return results


def measure(function: Any, metrics: Metrics, trace: bool, temperature: str, *,
            prepare: Any = None, identity_conversation: str | None = None) -> tuple[dict[str, Any], Any]:
    metrics.counts.clear()
    metrics.times.clear()
    if trace:
        tracemalloc.start()
    prepared = None
    setup_ms = 0.0
    setup_work = None
    payload = None
    identity_hash_override = None
    cache_bytes: int | None = 0
    wire = b""
    status: dict[str, Any]
    try:
        if prepare is not None:
            setup_start = time.perf_counter_ns()
            prepared = prepare()
            setup_ms = (time.perf_counter_ns() - setup_start) / 1e6
            setup_work = metrics.snapshot()
            metrics.counts.clear()
            metrics.times.clear()
        start, cpu = time.perf_counter_ns(), time.process_time_ns()
        try:
            measured = function(prepared) if prepare is not None else function()
            if len(measured) == 3:
                payload, cache_bytes, identity_hash_override = measured
            else:
                payload, cache_bytes = measured
            wire = encoded(payload)
            status = {"status": "ok"}
        except Exception as error:
            status = {"status": "error", "error_code": getattr(error, "code", type(error).__name__)}
        elapsed, cpu_elapsed = time.perf_counter_ns() - start, time.process_time_ns() - cpu
    except Exception as error:
        elapsed = cpu_elapsed = 0
        status = {"status": "error", "error_code": getattr(error, "code", type(error).__name__)}
    peak = tracemalloc.get_traced_memory()[1] if trace else None
    if trace:
        tracemalloc.stop()
    report = {**status, "temperature": temperature, "latency_ms": elapsed / 1e6,
              "setup_latency_ms": setup_ms, "cpu_ms": cpu_elapsed / 1e6,
              "tracemalloc_peak_bytes": peak, "process_lifetime_peak_rss_bytes": rss_peak_bytes(),
              "response_bytes": len(wire), "cache_accounted_bytes": cache_bytes,
              "setup_work": setup_work, **metrics.snapshot()}
    if payload is not None:
        items = payload.get("items", [])
        stable_items = [{key: value for key, value in item.items() if key != "hitId"} for item in items]
        report.update({"returned_items": len(items), "total": payload.get("total"),
                       "candidate_count": payload.get("candidateCount"),
                       "next_offset": payload.get("nextOffset"), "truncated": payload.get("truncated", False),
                       "coverage": payload.get("coverage"),
                       "canonical_output_bytes": sum(len(item.get("content", "").encode("utf-8")) for item in items),
                       "snippet_output_bytes": sum(len(item.get("snippet", "").encode("utf-8")) for item in items),
                       "output_sha256": hashlib.sha256(encoded(stable_items)).hexdigest()})
        if ((not items and ("searchId" in payload or identity_conversation is not None))
                or (items and ("conversationId" in items[0] or identity_conversation is not None))):
            identities = []
            for item in items:
                delta = datetime.fromisoformat(item["timestamp"]).astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
                micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
                identities.append([item.get("conversationId", identity_conversation),
                                   item.get("messageId", item.get("id")), micros])
            report["identity_sha256"] = hashlib.sha256(encoded(identities)).hexdigest()
        if items and "canonicalBytes" in items[0]:
            report["canonical_output_bytes"] = sum(item["canonicalBytes"] for item in items)
            report["parser_statuses"] = [item["status"] for item in items]
    if identity_hash_override is not None:
        report["identity_sha256"] = identity_hash_override
    return report, payload


def b2_page_offset(config: dict[str, Any], *, reverse: bool = False) -> int:
    rows_in_conversation = (config["rows"] + config["conversations"] - 1) // config["conversations"]
    maximum_start = max(0, rows_in_conversation - config["page_size"])
    target = min(config["deep_offset"], maximum_start) // config["page_size"] * config["page_size"]
    if reverse and maximum_start >= config["page_size"]:
        target = max(config["page_size"], target)
    return target


def page_object_identity_sha256(page: Any, conversation: str) -> str:
    identities = []
    for item in page.items:
        message = getattr(item, "message", item)
        delta = message.timestamp.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        identities.append([conversation, message.id, micros])
    return hashlib.sha256(encoded(identities)).hexdigest()


def cursor_page_at(source: Any, source_session_id: str, config: dict[str, Any], offset: int) -> Any:
    page = source.get_messages_page(
        conversation_id(0), source_session_id=source_session_id,
        start_epoch=None, end_epoch_exclusive=None, order="desc", limit=config["page_size"],
    )
    if page.total != (config["rows"] + config["conversations"] - 1) // config["conversations"]:
        raise RuntimeError("cursor first-page total differs from the synthetic row oracle")
    current_offset = 0
    while current_offset < offset:
        if page_object_identity_sha256(page, conversation_id(0)) != expected_cursor_identity_sha256(config, current_offset):
            raise RuntimeError("cursor traversal page differs from the synthetic identity oracle")
        if page.next_cursor is None:
            raise RuntimeError("cursor ended before the synthetic target offset")
        prior_count = len(page.items)
        page = source.get_messages_cursor_page(
            source_session_id, page.page_session_id, page.next_cursor, limit=config["page_size"],
        )
        current_offset += prior_count
    if current_offset != offset:
        raise RuntimeError("cursor page size changed before the synthetic target offset")
    if page_object_identity_sha256(page, conversation_id(0)) != expected_cursor_identity_sha256(config, current_offset):
        raise RuntimeError("cursor target page differs from the synthetic identity oracle")
    return page


def expected_cursor_identity_sha256(config: dict[str, Any], offset: int, *, order: str = "desc") -> str:
    rows = (config["rows"] + config["conversations"] - 1) // config["conversations"]
    size = min(config["page_size"], max(0, rows - offset))
    if order == "desc":
        ordinals = range(rows - 1 - offset, rows - 1 - offset - size, -1)
    else:
        ordinals = range(offset, offset + size)
    identities = []
    for ordinal in ordinals:
        timestamp_ms = 1_767_225_600_000 + ordinal * 137
        local_id = ordinal // config["shards"] + 1
        identities.append([conversation_id(0), str(local_id), timestamp_ms * 1000])
    return hashlib.sha256(encoded(identities)).hexdigest()


def child(spec: dict[str, Any]) -> dict[str, Any]:
    config, root, case = spec["config"], Path(spec["root"]), spec["case"]
    # Refuse anything without the marker created by this harness.
    for shard in range(config["shards"]):
        with sqlite3.connect((root / f"shard_{shard:03d}.sqlite").as_uri() + "?mode=ro", uri=True) as connection:
            if connection.execute("SELECT marker FROM benchmark_meta").fetchone() != (FORMAT,):
                raise ValueError("Not a synthetic benchmark fixture")
    modules = production()
    source_module, _reader_module, search, _models = modules
    metrics = Metrics()
    instrument(source_module, modules[1], metrics)
    source = synthetic_source(root, config, modules, metrics)
    row_count = (config["rows"] + config["conversations"] - 1) // config["conversations"]
    limited_cases = (case.startswith(("legacy_", "b1_", "keyword_legacy", "b3_keyword_")))
    if limited_cases and row_count > source_module.MAX_MESSAGES:
        return {"case": case, "status": "skipped", "reason": "production conversation candidate limit", "runs": []}
    offset = config["deep_offset"] if case.endswith("deep") else 0
    cache = search.SearchCache()
    worker_module = importlib.import_module("worker")
    setup = None
    prepare = None
    identity_conversation = None

    def scan(pattern: str) -> tuple[dict[str, Any], int]:
        deadline = time.monotonic() + config["timeout"]
        hits, total, coverage = search.scan_documents(documents(source, config, modules, deadline),
                                                       QUERIES[pattern], "desc", deadline=deadline)
        metrics.inc("retained_search_hits", len(hits))
        session = cache.create("synthetic-harness", "synthetic-source-v1", hits, total, coverage, "immutable-fixture-v1")
        return search.search_page(session, 0, config["page_size"]), session.cache_bytes

    if case == "global_cached_page":
        setup, initial = measure(lambda: scan("dense"), metrics, config["tracemalloc"], "setup_scan_not_a_continuation")
        if initial is None or setup["status"] != "ok":
            return {"case": case, "status": "error", "setup": setup, "runs": []}
        search_id = initial["searchId"]

        def operation() -> tuple[dict[str, Any], int]:
            session = cache.get(search_id, "synthetic-harness", "synthetic-source-v1")
            return search.search_page(session, config["page_size"], config["page_size"]), session.cache_bytes
        path = "production SearchCache.get/search_page (A only; setup scan reported separately)"
    elif case.startswith("global_"):
        def operation() -> tuple[dict[str, Any], int]:
            cache.clear()
            return scan(case.removeprefix("global_"))
        path = "production scan_documents/SearchCache with harness-only synthetic iterator"
    elif case == "parser_sample":
        def operation() -> tuple[dict[str, Any], int]:
            return parser_samples(source_module, config), 0
        path = "production parse_appmsg; four supported shapes plus DTD/malformed/depth probes"
    elif case == "b2_first":
        def operation() -> tuple[dict[str, Any], int | None]:
            page = source.get_messages_page(
                conversation_id(0), source_session_id="synthetic-source-v1",
                start_epoch=None, end_epoch_exclusive=None, order="desc", limit=config["page_size"],
            )
            return (worker_module._cursor_page_result(page, config["page_size"], first_page=True), None,
                    page_object_identity_sha256(page, conversation_id(0)))
        identity_conversation = conversation_id(0)
        path = "production WeChat4Source.get_messages_page + worker cursor response validator"
    elif case == "b2_deep":
        target = b2_page_offset(config)
        prepare = lambda: cursor_page_at(source, "synthetic-source-v1", config, max(0, target - config["page_size"])) if target else None

        def operation(prior_page: Any) -> tuple[dict[str, Any], int | None]:
            if prior_page is None:
                page = source.get_messages_page(
                    conversation_id(0), source_session_id="synthetic-source-v1",
                    start_epoch=None, end_epoch_exclusive=None, order="desc", limit=config["page_size"],
                )
                first = True
            else:
                if prior_page.next_cursor is None:
                    raise RuntimeError("deep cursor setup produced no next boundary")
                page = source.get_messages_cursor_page(
                    "synthetic-source-v1", prior_page.page_session_id, prior_page.next_cursor,
                    limit=config["page_size"],
                )
                first = False
            return (worker_module._cursor_page_result(
                page, config["page_size"], page.page_session_id, first_page=first,
            ), None, page_object_identity_sha256(page, conversation_id(0)))
        identity_conversation = conversation_id(0)
        offset = target
        path = "production B2 keyset continuation at computed deep page; setup traversal reported separately"
    elif case == "b2_reverse":
        target = b2_page_offset(config, reverse=True)
        prepare = lambda: cursor_page_at(source, "synthetic-source-v1", config, target)

        def operation(current_page: Any) -> tuple[dict[str, Any], int | None]:
            if current_page.previous_cursor is None:
                raise RuntimeError("reverse cursor setup produced no previous boundary")
            page = source.get_messages_cursor_page(
                "synthetic-source-v1", current_page.page_session_id, current_page.previous_cursor,
                limit=config["page_size"],
            )
            return (worker_module._cursor_page_result(
                page, config["page_size"], current_page.page_session_id,
            ), None, page_object_identity_sha256(page, conversation_id(0)))
        identity_conversation = conversation_id(0)
        offset = max(0, target - config["page_size"])
        path = "production B2 before-cursor continuation after deep traversal; setup reported separately"
    elif case == "b3_keyword_first":
        def operation() -> tuple[dict[str, Any], int]:
            keyword_cache, session, generations, criteria = prepare_keyword_session(source, config, modules, metrics)
            page = keyword_cursor_page(source, keyword_cache, session, generations, criteria, None, config["page_size"])
            return page, session.cache_bytes
        identity_conversation = conversation_id(0)
        path = "production WeChat4Source.iter_keyword_documents + scan_keyword_documents + KeywordPageCache"
    elif case == "b3_keyword_continuation":
        holder: dict[str, Any] = {}

        def initial_keyword_scan() -> tuple[dict[str, Any], int]:
            keyword_cache, session, generations, criteria = prepare_keyword_session(source, config, modules, metrics)
            page = keyword_cursor_page(source, keyword_cache, session, generations, criteria, None, config["page_size"])
            holder.update(cache=keyword_cache, session=session, generations=generations,
                          criteria=criteria, initial=page)
            return page, session.cache_bytes

        setup, _initial = measure(initial_keyword_scan, metrics, config["tracemalloc"], "B3_full_first_scan_separately_measured")
        if setup["status"] != "ok":
            return {"case": case, "status": "error", "setup": setup, "runs": []}

        def operation() -> tuple[dict[str, Any], int]:
            initial = holder["initial"]
            if not initial["nextCursor"]:
                raise RuntimeError("B3 setup scan has no continuation cursor")
            session = holder["session"]
            page = keyword_cursor_page(
                source, holder["cache"], session, holder["generations"], holder["criteria"],
                initial["nextCursor"], config["page_size"],
            )
            return page, session.cache_bytes
        identity_conversation = conversation_id(0)
        offset = config["page_size"]
        path = "production KeywordPageCache.cursor_page continuation; complete B3 first scan reported separately"
    else:
        def operation() -> tuple[dict[str, Any], int]:
            function = source.get_messages if case.startswith("b1_") else source._get_messages_legacy
            page = function(conversation_id(0), start_epoch=None, end_epoch_exclusive=None,
                            query=QUERIES["dense"] if case.startswith("keyword_legacy") else None,
                            offset=offset, limit=config["page_size"], order="desc")
            return {"items": [message_wire(item) for item in page.items], "total": page.total,
                    "nextOffset": page.next_offset}, 0
        path = "production WeChat4Source." + ("get_messages (inspect counts to detect fallback)" if case.startswith("b1_") else "_get_messages_legacy")

    runs = [measure(
        operation, metrics, config["tracemalloc"],
        "first_call_fresh_process_os_cache_unspecified" if index == 0 and setup is None else "repeat_os_warm_likely_new_sql_connections",
        prepare=prepare, identity_conversation=identity_conversation,
    )[0] for index in range(config["repeats"])]
    return {"case": case, "status": "ok" if all(run["status"] == "ok" for run in runs) else "error",
            "path": path, "offset": offset, "setup": setup, "runs": runs,
            "query_plans": explain(root, metrics.statements.values())}


def validate(results: list[dict[str, Any]], dataset: dict[str, Any], config: dict[str, Any], search_limits: dict[str, int]) -> list[str]:
    """No timing thresholds: assert observed work/contracts, not machine speed."""
    by_case = {result["case"]: result for result in results}
    failures = []
    for result in results:
        if result["status"] == "error":
            failures.append(f"{result['case']}: execution failed")
    for suffix in ("first", "deep"):
        reference = by_case.get("legacy_" + suffix, {}).get("runs", [])
        candidate = by_case.get("b1_" + suffix, {}).get("runs", [])
        for old, new in zip(reference, candidate):
            if old["status"] != "ok" or new["status"] != "ok":
                continue
            if (old["total"], old["next_offset"], old["output_sha256"]) != (new["total"], new["next_offset"], new["output_sha256"]):
                failures.append(f"b1_{suffix}: not equivalent to same-contract legacy output")
            counts = new["counts"]
            if counts["transform_calls"] != new["returned_items"] or counts["media_resolve_calls"] > new["returned_items"]:
                failures.append(f"b1_{suffix}: off-page conversion/media work or fallback on supported fixture")
    for case in ("b2_first", "b2_deep", "b2_reverse"):
        result = by_case.get(case, {})
        expected_offset = 0 if case == "b2_first" else (
            max(0, b2_page_offset(config, reverse=True) - config["page_size"])
            if case == "b2_reverse" else b2_page_offset(config)
        )
        for run in result.get("runs", []):
            if run["status"] != "ok":
                continue
            expected_total = (config["rows"] + config["conversations"] - 1) // config["conversations"]
            expected_items = min(config["page_size"], max(0, expected_total - expected_offset))
            if run["total"] != expected_total or run["returned_items"] != expected_items:
                failures.append(f"{case}: exact total or page cardinality differs from oracle")
            if run.get("identity_sha256") != expected_cursor_identity_sha256(config, expected_offset):
                failures.append(f"{case}: cursor page order/identity has a duplicate, gap, or ordering error")
            counts = run["counts"]
            if counts["keyset_rows_yielded"] > config["shards"] * (config["page_size"] + 1):
                failures.append(f"{case}: Python received more than the per-shard bounded keyset window")
            if counts["transform_calls"] != expected_items or counts["body_rows_yielded"] != expected_items:
                failures.append(f"{case}: row projection work is not bounded to the returned page")
            if counts["media_resolve_calls"] > expected_items:
                failures.append(f"{case}: media resolver attempts exceed the returned page")
            if run["response_bytes"] > 1024 * 1024:
                failures.append(f"{case}: serialized response exceeds the benchmark 1 MiB target")

    for pattern in ("sparse", "dense", "none"):
        for run in by_case.get("global_" + pattern, {}).get("runs", []):
            if run["status"] != "ok":
                continue
            if run["total"] != dataset["expected_matches"][pattern]:
                failures.append(f"global_{pattern}: total differs from generator oracle")
            if run["identity_sha256"] != dataset["expected_identity_sha256"][pattern]["first"]:
                failures.append(f"global_{pattern}: newest-first Top-K selection differs from generator oracle")
            if run["coverage"]["scannedMessages"] != config["rows"]:
                failures.append(f"global_{pattern}: incomplete scan")
            if run["counts"]["media_resolve_calls"] or run["counts"]["transcript_load_calls"]:
                failures.append(f"global_{pattern}: media/transcript I/O stub was invoked")
            if run["response_bytes"] > search_limits["max_response_bytes"] or run["cache_accounted_bytes"] > search_limits["cache_bytes"]:
                failures.append(f"global_{pattern}: response/cache budget exceeded")
            if run["truncated"] != (run["total"] > search_limits["result_limit"]):
                failures.append(f"global_{pattern}: wrong Top-K truncation flag")
            if run["counts"]["retained_search_hits"] != min(run["total"], search_limits["result_limit"]):
                failures.append(f"global_{pattern}: retained hits do not obey the Top-K limit")
    for run in by_case.get("global_cached_page", {}).get("runs", []):
        if run["status"] == "ok" and run["identity_sha256"] != dataset["expected_identity_sha256"]["dense"]["second"]:
            failures.append("global_cached_page: second-page identity/order differs from generator oracle")
        if run["status"] == "ok" and any(run["counts"][key] for key in (
            "python_rows_yielded", "transform_calls", "media_resolve_calls", "parser_calls", "decode_calls", "sql_statements")):
            failures.append("global_cached_page: continuation repeated scan/projection work")
    expected_keyword_total = dataset["expected_matches_by_conversation"]["sparse"][0]
    expected_keyword_candidates = dataset["conversation_rows"][0]
    for case, page_key in (("b3_keyword_first", "first"), ("b3_keyword_continuation", "second")):
        result = by_case.get(case, {})
        for run in result.get("runs", []):
            if run["status"] != "ok":
                continue
            expected_hash = dataset["expected_keyword_identity_sha256"]["sparse"]["0"][page_key]
            if run["total"] != expected_keyword_total or run["candidate_count"] != expected_keyword_candidates:
                failures.append(f"{case}: keyword total or full candidate count differs from generator oracle")
            if run["identity_sha256"] != expected_hash:
                failures.append(f"{case}: keyword snapshot page order has a duplicate, gap, or mismatch")
            if run["cache_accounted_bytes"] > search_limits["cache_bytes"]:
                failures.append(f"{case}: production keyword cache accounting exceeds its shared budget")
            if run["response_bytes"] > search_limits["max_response_bytes"]:
                failures.append(f"{case}: keyword response exceeds the production response budget")
    for run in by_case.get("b3_keyword_first", {}).get("runs", []):
        if run["status"] != "ok":
            continue
        counts = run["counts"]
        if counts["keyword_documents_yielded"] != expected_keyword_candidates:
            failures.append("b3_keyword_first: first scan did not process every synthetic candidate")
        if counts["transform_calls"] != expected_keyword_candidates or counts["body_rows_yielded"] != expected_keyword_candidates:
            failures.append("b3_keyword_first: first scan transform/body work differs from candidate count")
    for run in by_case.get("b3_keyword_continuation", {}).get("runs", []):
        if run["status"] != "ok":
            continue
        if run["cache_accounted_bytes"] != by_case["b3_keyword_continuation"].get("setup", {}).get("cache_accounted_bytes"):
            failures.append("b3_keyword_continuation: cache accounting changed after setup")
        if any(run["counts"][key] for key in (
            "keyset_rows_yielded", "keyword_documents_yielded", "body_rows_yielded",
            "transform_calls", "media_resolve_calls", "transcript_load_calls", "parser_calls",
            "decode_calls", "database_acquisitions", "sql_statements",
        )):
            failures.append("b3_keyword_continuation: continuation repeated scan/projection/media/parser/SQL work")
    for run in by_case.get("parser_sample", {}).get("runs", []):
        if run["status"] == "ok" and run.get("parser_statuses") != ["ok"] * 4 + ["rejected", "invalid", "limit_exceeded"]:
            failures.append("parser_sample: supported/safety probe statuses differ")
    return failures


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--conversations", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261128)
    parser.add_argument("--index", choices=("time", "none"), default="time")
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--deep-offset", type=int, default=500)
    parser.add_argument("--sparse-every", type=int, default=97)
    parser.add_argument("--long-every", type=int, default=23, help="0 disables long messages")
    parser.add_argument("--long-chars", type=int, default=2048)
    parser.add_argument("--compress-every", type=int, default=7, help="0 disables zstd content")
    parser.add_argument("--card-percent", type=int, default=12)
    parser.add_argument("--media-percent", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=20.0, help="harness search deadline; does not change production B1 deadline")
    parser.add_argument("--no-tracemalloc", action="store_true", help="lower instrumentation overhead; peak Python allocation becomes null")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--output", type=Path, help="optional NEW JSON file; never overwritten; fixtures are not retained")
    parser.add_argument("--self-test", action="store_true", help="fixed 1,600-row safety/equivalence smoke configuration, all cases")
    args = parser.parse_args()
    if args.self_test:
        for name, value in {"rows": 1600, "shards": 4, "conversations": 4, "page_size": 25,
                            "deep_offset": 90, "sparse_every": 7, "repeats": 1, "index": "time",
                            "card_percent": 12, "media_percent": 20, "long_every": 11,
                            "compress_every": 7, "long_chars": 2048, "seed": 20261128, "timeout": 20.0}.items():
            setattr(args, name, value)
        args.cases = list(CASES)
    ranges = {"rows": (1, 20_000_000), "shards": (1, 64), "conversations": (1, 64),
              "page_size": (1, 100), "deep_offset": (0, 1_000_000_000), "sparse_every": (1, 10_000_000),
              "long_every": (0, 10_000_000), "long_chars": (128, 32768), "compress_every": (0, 10_000_000),
              "card_percent": (0, 100), "media_percent": (0, 100), "repeats": (1, 20), "timeout": (0.01, 600)}
    for name, (low, high) in ranges.items():
        if not low <= getattr(args, name) <= high:
            parser.error(f"--{name.replace('_', '-')} must be in [{low}, {high}]")
    if args.card_percent + args.media_percent > 100:
        parser.error("card-percent + media-percent must not exceed 100")
    if args.output and (args.output.exists() or not args.output.resolve().parent.is_dir()):
        parser.error("--output must not exist and its parent directory must exist")
    return args


def main() -> int:
    if sys.argv[1:] == ["--_child"]:
        try:
            result = child(json.load(sys.stdin))
        except Exception as error:
            result = {"status": "error", "error_code": getattr(error, "code", type(error).__name__), "runs": []}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] != "error" else 1
    args = arguments()
    config = {key: value for key, value in vars(args).items() if key not in {"output", "self_test", "cases", "no_tracemalloc"}}
    config["tracemalloc"] = not args.no_tracemalloc
    before = source_hashes()
    _source, _reader, search, _models = production()
    limits = {"result_limit": search.RESULT_LIMIT, "max_response_bytes": search.MAX_RESPONSE_BYTES,
              "cache_bytes": search.QUERY_CACHE_BYTES}
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="wechat-history-synthetic-bench-") as directory:
        root = Path(directory)
        dataset = generate(root, config)
        for case in args.cases:
            spec = {"case": case, "config": config, "root": str(root)}
            try:
                completed = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(Path(__file__).resolve()), "--_child"],
                                           input=json.dumps(spec, ensure_ascii=False), text=True, encoding="utf-8",
                                           capture_output=True, timeout=max(120, (config["timeout"] * config["repeats"] + 60) * 3),
                                           check=False)
                result = json.loads(completed.stdout)
                result["case"] = case
                if completed.returncode and result.get("status") != "error":
                    result["status"] = "error"
                # No traceback/body/path logging; structured errors suffice.
                result["child_exit_code"] = completed.returncode
                result["unexpected_stderr"] = bool(completed.stderr)
            except subprocess.TimeoutExpired:
                result = {"case": case, "status": "error", "error_code": "HARNESS_PROCESS_TIMEOUT", "runs": []}
            except (json.JSONDecodeError, OSError):
                result = {"case": case, "status": "error", "error_code": "HARNESS_CHILD_FAILED", "runs": []}
            results.append(result)
        unchanged = all(file_hash(root / name) == digest for name, digest in dataset["files"].items())
    after = source_hashes()
    failures = validate(results, dataset, config, limits)
    if not unchanged:
        failures.append("generated SQLite fixtures changed during read-only measurements")
    if before != after:
        failures.append("production/harness source changed concurrently; rerun before comparing performance")
    report = {
        "format": FORMAT, "synthetic_only": True, "config": config, "dataset": dataset,
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
                        "zstandard": importlib.import_module("zstandard").__version__,
                        "machine": platform.machine(), "processor": platform.processor(), "cpu_count": os.cpu_count()},
        "source_sha256_before": before, "source_sha256_after": after,
        "source_files_changed_during_run": [name for name in before if before[name] != after[name]],
        "fixtures_unchanged": unchanged, "fixtures_retained": False, "limits_from_helpers": limits,
        "results": results, "checks": {"passed": not failures, "failures": failures},
        "not_measured": {"decrypt_copy_ms": None, "real_media_recovery_ms": None,
                         "b2_cursor_pagination": "measured via production WeChat4Source cursor methods on synthetic snapshots",
                         "b3_keyword_session": "measured via production source iterator, scan_keyword_documents and KeywordPageCache",
                         "worker_stdio_native_ui": "not exercised", "source_generation_ttl_invalidation": "not exercised",
                         "markdown_json_pdf_export": "not exercised", "physical_sql_rows_scanned": None,
                         "b2_cursor_session_retained_bytes": None, "isolated_sql_sort_ms": None},
        "measurement_notes": [
            "Fresh process is not OS-cold disk cache; repeat calls may benefit from OS/import/schema caches.",
            "Query SQL connections are reopened; this is not an assertion of retained SQLite page caches.",
            "Latency excludes fixture generation/import; B2 deep/reverse traversal and B3 first scan are reported as setup separately.",
            "Tracemalloc peak includes setup plus measured operation when a setup is reported; process RSS is a lifetime high-water.",
            "SQL/schema/iteration/decode/parser/transform timings overlap: never add them together.",
            "SQL yielded rows and LIMIT are not engine scanned rows; progress counts approximate 1000-op VM callbacks.",
            "sql_statements counts reader.execute calls, not direct connection calls such as BEGIN; sort cost is not isolated.",
            "B2 reports bounded Python keyset rows and VM progress callbacks, not SQLite physical rows visited.",
            "B2 uses the production live-source cursor and response validator; B3 uses the production live-source iterator, matcher and memory cache. Worker JSON-lines dispatch is not invoked.",
            "B2 cursor metadata has no production byte ledger; B3 cache bytes use its shared conservative accounting, not a proof of bounded process RSS.",
            "Media stubs count resolver attempts without opening resources; no real media, transcript cache, audio, image, network, or decrypt I/O occurs.",
            "Global document iteration and message serialization are harness adapters, not worker command verification.",
            "Unchanged fixture SHA-256 checks cover generated SQLite files, not real account DB/WAL safety.",
        ],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text + "\n")
        print(json.dumps({"report": str(args.output.resolve()), "passed": not failures}, ensure_ascii=False))
    else:
        print(text)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
