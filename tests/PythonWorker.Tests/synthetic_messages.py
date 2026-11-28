"""Synthetic shard factory shared by reader/search/paging tests.

Only creates databases under the caller's TemporaryDirectory; never discovers
accounts, captures keys, reads user media, or connects to a real WeChat process.
The passthrough cache models versioned snapshots for these immutable fixtures.
"""
from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT / "src/PythonWorker", ROOT / "src/PythonWorker/vendor"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from wechat_context_exporter.models import Conversation, ConversationKind
from wechat_context_exporter.sources.wechat4_crypto import DatabaseSnapshot, _database_signature
from wechat_memory.wechat4_source import WeChat4Source


class SyntheticDatabaseCache:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths
        self.get_calls: Counter[str] = Counter()
        self.snapshots: dict[str, DatabaseSnapshot] = {}
        self.next_generation = 0

    def get(self, relative: str) -> Path:
        relative = relative.replace("/", "\\")
        self.get_calls[relative] += 1
        path = self.paths[relative]
        path.stat()
        return path

    def get_snapshot(self, relative: str) -> DatabaseSnapshot:
        path = self.get(relative)
        signature = _database_signature(path)
        cached = self.snapshots.get(relative)
        if cached is None or cached.signature != signature:
            self.next_generation += 1
            cached = DatabaseSnapshot(path, self.next_generation, signature)
            self.snapshots[relative] = cached
        return cached

    def is_snapshot_current(self, relative: str, snapshot: DatabaseSnapshot) -> bool:
        try:
            return _database_signature(self.paths[relative]) == snapshot.signature and self.snapshots.get(relative) == snapshot
        except OSError:
            return False

    def close(self) -> None:
        self.snapshots.clear()


def table_name(conversation_id: str) -> str:
    return "Msg_" + hashlib.md5(conversation_id.encode("utf-8")).hexdigest()


class SyntheticSourceFactory:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.paths: dict[str, Path] = {}
        self.locations: dict[str, list[tuple[str, str]]] = {}
        self.conversations: dict[str, Conversation] = {}

    def add_shard(self, name: str, conversations: dict[str, list[dict]], *,
                  without_rowid: bool = False, indexed: bool = False) -> Path:
        path = self.root / name
        relative = "message\\" + name
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
            connection.executemany("INSERT INTO Name2Id VALUES (?)", [("wxid_self",), ("wxid_friend",), ("wxid_member",)])
            for conversation_id, rows in conversations.items():
                table = table_name(conversation_id)
                pk = " PRIMARY KEY" if without_rowid else ""
                suffix = " WITHOUT ROWID" if without_rowid else ""
                connection.execute(
                    f'CREATE TABLE "{table}" (local_id INTEGER{pk}, create_time INTEGER, '
                    'local_type INTEGER, real_sender_id INTEGER, message_content BLOB, server_id INTEGER, '
                    f'WCDB_CT_message_content INTEGER, packed_info_data BLOB){suffix}'
                )
                connection.executemany(
                    f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    [(row.get("local_id", index + 1), row["create_time"], row.get("local_type", 1),
                      row.get("real_sender_id", 2), row.get("content", f"合成 {name}/{index}"),
                      row.get("server_id", 0), row.get("compression", 0), row.get("packed_info_data"))
                     for index, row in enumerate(rows)],
                )
                if indexed:
                    connection.execute(f'CREATE INDEX "idx_{table}" ON "{table}" (create_time, local_id)')
                self.locations.setdefault(conversation_id, []).append((relative, table))
                self.conversations.setdefault(conversation_id, Conversation(
                    conversation_id, "虚构会话 " + conversation_id,
                    ConversationKind.GROUP if "@chatroom" in conversation_id else ConversationKind.DIRECT,
                ))
            connection.commit()
        finally:
            connection.close()
        self.paths[relative] = path
        return path

    def source(self) -> WeChat4Source:
        source = WeChat4Source()
        source._connected = True
        source._own_wxid = "wxid_self"
        source._contacts = {"wxid_member": "群成员 Straße", "wxid_friend": "虚构朋友"}
        source._locations = {key: list(value) for key, value in self.locations.items()}
        source._conversations = list(self.conversations.values())
        source._databases = SyntheticDatabaseCache(self.paths)
        return source
