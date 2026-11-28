#!/usr/bin/env python3
"""Standalone JSON Lines worker for the native WeChat Memory app.

stdout carries protocol JSON exclusively; diagnostics go to stderr. Sources:
  * demo    - fictional JSON data, no WeChat access (default)
  * wechat4 - local WeChat 4.x databases, read-only, account/key kept in memory

The worker is a dedicated child process. It never prints chat content, paths,
account ids or keys to stdout/stderr. All plaintext database copies live under
the app's private LOCALAPPDATA temporary root and are removed on close.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
import time as _time
import traceback
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_PAGE_SIZE = 100
MAX_EXPORT_MESSAGES = 200_000
DEFAULT_PAGE_SIZE = 50
_MAX_PRUNE_PREVIEWS = 3
_PRUNE_PREVIEW_TTL_SECONDS = 600
_MAX_PRUNE_REASON_BUCKETS = 12
DEMO_TIMEZONE = timezone(timedelta(hours=8))
TIMEZONE_LABEL = "UTC+08:00"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONVERSATION_KINDS = {"direct", "group"}
_MESSAGE_KINDS = {"text", "image", "file", "voice", "system"}

_SCRIPT_DIR = Path(__file__).resolve().parent
_VENDOR_DIR = _SCRIPT_DIR / "vendor"
for _path in (_SCRIPT_DIR, _VENDOR_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from wechat_context_exporter.models import (  # noqa: E402
    Conversation,
    ConversationKind,
    Message,
    MessageType,
)
from wechat_context_exporter.text_exporters import export_json, export_markdown  # noqa: E402
from wechat_memory.global_search import (  # noqa: E402
    KEYWORD_SESSION_PREFIX, MAX_QUERY_BYTES, SOFT_DEADLINE_SECONDS,
    KeywordPageCache, KeywordSearchCriteria, SearchCache, SearchDocument, SearchError,
    bounded_context, content_fingerprint, datetime_microseconds, scan_documents,
    scan_keyword_documents, search_page,
)
from wechat_memory.memory_paging import ImmutableMessagePageProvider  # noqa: E402
from wechat_memory.offline_import import (  # noqa: E402
    ImportedJson,
    JsonMessage,
    OfflineImportError,
    OfflineJsonPageProvider,
    load_json_file,
)


class WorkerError(Exception):
    """An expected, user-explainable failure with a stable protocol code."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class DemoMessage:
    id: str
    sender: str
    timestamp: datetime  # always aware, normalised to UTC+08:00
    kind: str
    content: str


@dataclass(frozen=True)
class DemoConversation:
    id: str
    title: str
    kind: str
    messages: tuple[DemoMessage, ...]


@dataclass(frozen=True, slots=True)
class _MemoryLocator:
    conversation_id: str
    index: int


@dataclass(frozen=True, slots=True)
class _KeywordPageState:
    source: str
    criteria: KeywordSearchCriteria
    generations: Any


@dataclass(frozen=True)
class _PrunePreviewToken:
    days: int
    snapshot: dict[str, Any]
    expires_at: float


class Context:
    """Per-worker state plus the current request's progress channel."""

    def __init__(self, demo_path: str, jobs_dir: str | None = None,
                 scenes_file: str | None = None, model_root: str | None = None) -> None:
        self.demo_path = demo_path
        self.jobs_dir = jobs_dir
        self.scenes_file = scenes_file
        self.model_root = model_root
        self.wechat = None  # WeChat4Source | None
        # A successful import is a complete immutable snapshot.  The selected
        # file is not consulted again, so later edits/removal cannot create a
        # half-updated active source and message content cannot become a path.
        self.offline: ImportedJson | None = None
        self.offline_path: str | None = None
        self.offline_page_provider: OfflineJsonPageProvider | None = None
        self.active_source = "demo"
        self.source_session_id = secrets.token_urlsafe(24)
        self.demo_search_snapshot: tuple[DemoConversation, ...] | None = None
        self.demo_page_provider: ImmutableMessagePageProvider[DemoMessage] | None = None
        self.search_cache = SearchCache()
        self.keyword_page_cache = KeywordPageCache()
        self.keyword_page_states: dict[str, _KeywordPageState] = {}
        self.progress: Callable[[int, int, str], None] | None = None
        self.request_id: str = ""
        # Confirmation snapshots are deliberately process-local, bounded, and
        # short-lived. They never cross the JSON-lines boundary or reach logs.
        self.prune_previews: OrderedDict[str, _PrunePreviewToken] = OrderedDict()
        self.expired_prune_tokens: OrderedDict[str, None] = OrderedDict()

    def emit_progress(self, current: int, total: int, label: str) -> None:
        if self.progress is not None:
            self.progress(current, total, label)


# --------------------------------------------------------------------------- #
# Demo data loading
# --------------------------------------------------------------------------- #

def _required_text(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkerError("SOURCE_INVALID", f"{label}缺少有效的 {key}。")
    return value


def _parse_conversation(entry: Any, seen_ids: set[str]) -> DemoConversation:
    if not isinstance(entry, dict):
        raise WorkerError("SOURCE_INVALID", "会话条目必须是 JSON 对象。")
    conversation_id = _required_text(entry, "id", "会话")
    if conversation_id in seen_ids:
        raise WorkerError("SOURCE_INVALID", "会话 id 不能重复。")
    seen_ids.add(conversation_id)
    title = _required_text(entry, "name", "会话")
    kind = entry.get("kind", "direct")
    if kind not in _CONVERSATION_KINDS:
        raise WorkerError("SOURCE_INVALID", "会话 kind 只能是 direct 或 group。")
    raw_messages = entry.get("messages")
    if not isinstance(raw_messages, list):
        raise WorkerError("SOURCE_INVALID", "会话 messages 必须是数组。")

    messages: list[DemoMessage] = []
    message_ids: set[str] = set()
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise WorkerError("SOURCE_INVALID", "消息条目必须是 JSON 对象。")
        message_id = _required_text(raw, "id", "消息")
        if message_id in message_ids:
            raise WorkerError("SOURCE_INVALID", "同一会话内的消息 id 不能重复。")
        message_ids.add(message_id)
        kind_name = raw.get("type")
        if kind_name not in _MESSAGE_KINDS:
            raise WorkerError("SOURCE_INVALID", "消息 type 无效。")
        content = raw.get("content")
        if not isinstance(content, str):
            raise WorkerError("SOURCE_INVALID", "消息 content 必须是字符串。")
        if kind_name != "system" and not content.strip():
            raise WorkerError("SOURCE_INVALID", "非系统消息的 content 不能为空。")
        timestamp_text = _required_text(raw, "timestamp", "消息")
        try:
            timestamp = datetime.fromisoformat(timestamp_text)
        except ValueError:
            raise WorkerError("SOURCE_INVALID", "消息 timestamp 不是 ISO 8601。") from None
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise WorkerError("SOURCE_INVALID", "消息 timestamp 必须包含时区偏移。")
        messages.append(DemoMessage(
            id=message_id,
            sender=_required_text(raw, "sender", "消息"),
            timestamp=timestamp.astimezone(DEMO_TIMEZONE),
            kind=kind_name,
            content=content,
        ))
    # Python's sort is stable: equal timestamps keep the file's original order.
    messages.sort(key=lambda item: item.timestamp)
    return DemoConversation(conversation_id, title, kind, tuple(messages))


def load_demo(path: str | Path) -> list[DemoConversation]:
    file = Path(path)
    try:
        raw_text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise WorkerError("SOURCE_NOT_FOUND", "找不到演示数据文件。") from None
    except OSError:
        raise WorkerError("SOURCE_NOT_FOUND", "无法读取演示数据文件。") from None
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        raise WorkerError("SOURCE_INVALID", "演示数据不是有效的 JSON。") from None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise WorkerError("SOURCE_INVALID", "演示数据版本必须为 1。")
    entries = payload.get("conversations")
    if not isinstance(entries, list):
        raise WorkerError("SOURCE_INVALID", "演示数据缺少 conversations 数组。")
    seen: set[str] = set()
    return [_parse_conversation(entry, seen) for entry in entries]


# --------------------------------------------------------------------------- #
# Shared filtering: the UI preview and Markdown export use this one function.
# --------------------------------------------------------------------------- #

def inclusive_bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    """[start 00:00+08, day-after-end 00:00+08): both selected dates included."""
    if start is not None and end is not None and start > end:
        raise WorkerError("INVALID_ARGUMENT", "开始日期不能晚于结束日期。")
    if end is not None and end.year >= 9999:
        raise WorkerError("INVALID_ARGUMENT", "结束日期必须早于 9999 年。")
    lower = datetime.combine(start, time.min, tzinfo=DEMO_TIMEZONE) if start else None
    upper = datetime.combine(end + timedelta(days=1), time.min, tzinfo=DEMO_TIMEZONE) if end else None
    return lower, upper


def filter_messages(conversation: DemoConversation, start: date | None, end: date | None) -> list[DemoMessage]:
    lower, upper = inclusive_bounds(start, end)
    return [
        item for item in conversation.messages
        if (lower is None or item.timestamp >= lower) and (upper is None or item.timestamp < upper)
    ]


def _filter_query(items: list[Any], query: str | None) -> list[Any]:
    if not query:
        return items
    needle = query.casefold()
    result = []
    for item in items:
        # Live models use an enum; demo and offline JSON models both expose
        # the already-validated string kind instead of a Message.type member.
        kind = item.type.value if isinstance(item, Message) else item.kind
        if (needle in item.sender.casefold() or needle in item.content.casefold()
                or needle in kind.casefold()):
            result.append(item)
    return result


# --------------------------------------------------------------------------- #
# Payload helpers
# --------------------------------------------------------------------------- #

def _date_arg(payload: dict[str, Any], key: str) -> date | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise WorkerError("INVALID_ARGUMENT", f"{key} 必须为 YYYY-MM-DD 格式。")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise WorkerError("INVALID_ARGUMENT", f"{key} 不是有效日期。") from None


def _int_arg(payload: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise WorkerError("INVALID_ARGUMENT", f"{key} 必须是 {low} 到 {high} 之间的整数。")
    return value


def _bool_arg(payload: dict[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise WorkerError("INVALID_ARGUMENT", f"{key} 必须是布尔值。")
    return value


def _source_kind(payload: dict[str, Any]) -> str:
    source = payload.get("source", "demo")
    if not isinstance(source, str) or source not in {"demo", "wechat4", "offline"}:
        raise WorkerError("SOURCE_NOT_FOUND", "当前版本不支持此数据源。")
    return source


def _clear_keyword_pages(context: Context) -> None:
    context.keyword_page_cache.clear()
    context.keyword_page_states.clear()


def _activate_source(context: Context, source: str, *, force: bool = False) -> str:
    if context.active_source != source or force:
        if context.active_source == "offline" and context.offline_page_provider is not None:
            context.offline_page_provider.invalidate()
        if context.demo_page_provider is not None:
            context.demo_page_provider.close()
            context.demo_page_provider = None
        context.search_cache.clear()
        _clear_keyword_pages(context)
        context.source_session_id = secrets.token_urlsafe(24)
        context.active_source = source
        context.demo_search_snapshot = None
    return context.source_session_id


def _require_search_source(payload: dict[str, Any], context: Context) -> str:
    source = _source_kind(payload)
    token = payload.get("sourceSessionId")
    if (not isinstance(token, str) or not token or len(token) > 128
            or token != context.source_session_id or source != context.active_source):
        raise WorkerError("SOURCE_SESSION_CHANGED", "数据源已切换或会话已失效，请刷新后重新搜索。")
    if source == "wechat4":
        _require_wechat(context)
    elif source == "offline":
        _offline_conversations(context)
    return source


def _checked_source(payload: dict[str, Any], context: Context) -> str:
    if _source_kind(payload) != "demo":
        raise WorkerError("SOURCE_NOT_FOUND", "此命令只适用于演示数据源。")
    return context.demo_path


def _offline_conversations(context: Context):
    if context.offline is None:
        raise WorkerError("SOURCE_NOT_FOUND", "尚未导入离线 JSON。")
    return context.offline.conversations


def _find_demo_conversation(conversations: list[DemoConversation], payload: dict[str, Any]) -> DemoConversation:
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise WorkerError("INVALID_ARGUMENT", "conversationId 必须是非空字符串。")
    for conversation in conversations:
        if conversation.id == conversation_id:
            return conversation
    raise WorkerError("CONVERSATION_NOT_FOUND", "找不到指定的会话。")
    return conversation


def _demo_message_json(item: DemoMessage) -> dict[str, Any]:
    result = {
        "id": item.id,
        "sender": item.sender,
        "timestamp": item.timestamp.isoformat(timespec="seconds"),
        "content": item.content,
        "kind": item.kind,
    }
    if getattr(item, "is_outgoing", False):
        result["isOutgoing"] = True
    return result


def _offline_message_json(item: Any) -> dict[str, Any]:
    """Expose an imported snapshot without exposing or opening media paths."""
    result = _demo_message_json(item)
    if item.kind == "image":
        result["content"] = "[图片]"
    if item.duration_ms is not None:
        result["durationMs"] = item.duration_ms
    if item.transcript:
        result["transcript"] = item.transcript
    if item.reply_to:
        result["replyTo"] = item.reply_to
    return result


def _app_message_projection_json(projection: Any) -> dict[str, Any]:
    """Use the live source's bounded serializer without extending vendor models."""
    try:
        from wechat_memory.wechat4_source import app_message_projection_json
        return app_message_projection_json(projection)
    except (ImportError, ValueError):
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息投影结构无效。") from None


def _real_message_json(item: Message, projection: Any = None) -> dict[str, Any]:
    content = item.content
    attachment: str | None = None
    if item.type is MessageType.IMAGE:
        attachment = item.content
        content = "[图片]"
    result = {
        "id": item.id,
        "sender": item.sender,
        "timestamp": item.timestamp.astimezone(DEMO_TIMEZONE).isoformat(timespec="seconds"),
        "content": content,
        "kind": item.type.value,
        "isOutgoing": item.is_outgoing,
    }
    if attachment:
        result["attachmentPath"] = attachment
    if item.duration_ms is not None:
        result["durationMs"] = item.duration_ms
    if item.transcript:
        result["transcript"] = item.transcript
    if item.reply_to:
        result["replyTo"] = item.reply_to
    if projection is not None:
        result.update(_app_message_projection_json(projection))
    return result


# --------------------------------------------------------------------------- #
# Real source helpers
# --------------------------------------------------------------------------- #

def _require_wechat(context: Context):
    if context.wechat is None:
        raise WorkerError("WECHAT_NOT_CONNECTED", "尚未连接微信。请先选择账户并连接。")
    return context.wechat


def _real_conversation(context: Context, conversation_id: str) -> tuple[Conversation, Any]:
    source = _require_wechat(context)
    for item in source.list_conversations():
        if item.id == conversation_id:
            return item, source
    raise WorkerError("CONVERSATION_NOT_FOUND", "找不到指定的会话。")


def _real_messages(
    context: Context,
    conversation_id: str,
    start: date | None,
    end: date | None,
    query: str | None,
    offset: int = 0,
    limit: int = DEFAULT_PAGE_SIZE,
    order: str = "asc",
):
    conversation, source = _real_conversation(context, conversation_id)
    lower, upper = inclusive_bounds(start, end)
    page = source.get_messages(
        conversation_id,
        start_epoch=int(lower.timestamp()) if lower else None,
        end_epoch_exclusive=int(upper.timestamp()) if upper else None,
        query=query,
        offset=offset,
        limit=limit,
        order=order,
    )
    return conversation, page


def _real_export_models(context: Context, payload: dict[str, Any]) -> tuple[Conversation, list[Message]]:
    conversation, page = _real_messages(
        context, payload.get("conversationId", ""),
        _date_arg(payload, "startDate"), _date_arg(payload, "endDate"),
        payload.get("query"),
        offset=0,
        limit=MAX_EXPORT_MESSAGES,
    )
    return conversation, list(page.items)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def _cursor_pagination_supported(source: str) -> bool:
    """Whether a source mode implements both halves of the cursor protocol."""
    if source == "offline":
        source_type = OfflineJsonPageProvider
    elif source == "demo":
        source_type = ImmutableMessagePageProvider
    elif source == "wechat4":
        try:
            from wechat_memory.wechat4_source import WeChat4Source
        except ImportError:
            return False
        source_type = WeChat4Source
    else:
        return False
    return all(callable(getattr(source_type, name, None)) for name in (
        "get_messages_page", "get_messages_cursor_page",
    ))


def _keyword_pagination_supported(source: str) -> bool:
    if source in {"demo", "offline"}:
        # Validated in-memory models already support the legacy preview
        # predicate and display projection. No media references are opened.
        return True
    if source == "wechat4":
        try:
            from wechat_memory.wechat4_source import WeChat4Source
        except ImportError:
            return False
        return all(callable(getattr(WeChat4Source, name, None)) for name in (
            "iter_keyword_documents", "validate_keyword_generations", "keyword_media_selection",
        ))
    return False


def _source_capabilities() -> dict[str, list[str]]:
    result = {}
    for source in ("demo", "wechat4", "offline"):
        capabilities = []
        if _cursor_pagination_supported(source):
            capabilities.append("message_cursor_pagination")
        if _keyword_pagination_supported(source):
            capabilities.append("message_keyword_pagination")
        result[source] = capabilities
    return result


def handle_hello(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source_capabilities = _source_capabilities()
    capabilities = [
        "demo", "wechat4", "offline_json", "accounts", "message_search", "message_pagination",
        "markdown_export", "json_export", "pdf_export", "png_pages",
        "image_recovery", "voice_transcribe", "tasks", "scenes", "delivery",
        "progress_events", "global_message_search", "message_context",
    ]
    for capability in ("message_cursor_pagination", "message_keyword_pagination"):
        if any(capability in items for items in source_capabilities.values()):
            capabilities.append(capability)
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": capabilities,
        "sourceCapabilities": source_capabilities,
        "timezone": TIMEZONE_LABEL,
        "pageSizeLimit": MAX_PAGE_SIZE,
        "sourceSessionId": context.source_session_id,
        "source": context.active_source,
    }


def handle_list_accounts(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    from wechat_memory.wechat4_source import WeChat4Source

    return {
        "items": [
            {
                "token": item.token,
                "label": item.label,
                "lastUsed": item.last_used,
                "recent": item.recent,
            }
            for item in WeChat4Source.discover_accounts()
        ]
    }


def handle_connect(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source_name = _source_kind(payload)
    if source_name != "wechat4":
        raise WorkerError("INVALID_ARGUMENT", "只有微信数据源需要连接。")
    from wechat_memory.wechat4_source import SourceFailure, WeChat4Source

    token = payload.get("accountToken")
    if token is not None and not isinstance(token, str):
        raise WorkerError("INVALID_ARGUMENT", "accountToken 必须是字符串。")
    enable_images = _bool_arg(payload, "enableImages", False)
    enable_voice = _bool_arg(payload, "enableVoice", False)
    source = WeChat4Source()
    try:
        result = source.connect(
            token,
            enable_images=enable_images,
            enable_voice=enable_voice,
            progress=context.emit_progress,
            cancelled=lambda: False,
        )
    except SourceFailure as error:
        source.close()
        raise WorkerError(error.code, error.message, error.retryable)
    except Exception:
        source.close()
        raise WorkerError("WECHAT_CONNECT_FAILED", "连接微信失败，原数据源未切换。", True) from None
    previous = context.wechat
    if previous is not None:
        previous.close()
    context.wechat = source
    session_id = _activate_source(context, "wechat4", force=True)
    return {
        "connected": True,
        "source": "wechat4",
        "sourceSessionId": session_id,
        **result,
        "capabilities": source.capabilities,
    }


def handle_disconnect(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    try:
        if context.wechat is not None:
            context.wechat.close()
    finally:
        context.wechat = None
        _activate_source(context, "demo", force=True)
    return {"connected": False, "sourceSessionId": context.source_session_id}


def handle_import_json(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    raw_path = payload.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise WorkerError("INVALID_ARGUMENT", "path 必须是非空字符串。")
    # Parse and validate everything before changing either the active source or
    # the live WeChat source.  A failed candidate therefore leaves old data
    # and exportability untouched.
    try:
        candidate = load_json_file(raw_path)
    except OfflineImportError as error:
        raise WorkerError(error.code, error.message) from None

    if context.wechat is not None:
        try:
            context.wechat.close()
        except Exception:
            raise WorkerError("SOURCE_SWITCH_FAILED", "无法切换到离线数据源，原数据仍保持不变。") from None
    context.wechat = None
    context.offline = candidate
    context.offline_path = raw_path
    if context.offline_page_provider is None:
        context.offline_page_provider = OfflineJsonPageProvider(candidate)
    else:
        context.offline_page_provider.replace_snapshot(candidate)
    session_id = _activate_source(context, "offline", force=True)
    return {
        "source": "offline",
        "sourceSessionId": session_id,
        "schema": candidate.schema,
        "conversationCount": len(candidate.conversations),
        "messageCount": candidate.message_count,
    }


def handle_list_conversations(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source_name = _source_kind(payload)
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        raise WorkerError("INVALID_ARGUMENT", "query 必须是字符串。")
    if source_name == "demo":
        conversations = load_demo(context.demo_path)
        session_id = _activate_source(context, "demo")
        if context.demo_search_snapshot is None:
            context.demo_search_snapshot = tuple(conversations)
        needle = (query or "").casefold()
        return {
            "sourceSessionId": session_id,
            "items": [
                {"id": item.id, "title": item.title, "messageCount": len(item.messages),
                 "kind": item.kind, "countKnown": True}
                for item in conversations
                if not needle or needle in item.title.casefold()
            ]
        }
    if source_name == "offline":
        conversations = _offline_conversations(context)
        session_id = _activate_source(context, "offline")
        needle = (query or "").casefold()
        return {
            "sourceSessionId": session_id,
            "items": [
                {"id": item.id, "title": item.title, "messageCount": len(item.messages),
                 "kind": item.kind, "countKnown": True}
                for item in conversations
                if not needle or needle in item.title.casefold()
            ]
        }
    source = _require_wechat(context)
    items = source.list_conversations(query)
    session_id = _activate_source(context, "wechat4")
    return {
        "sourceSessionId": session_id,
        "items": [
            {"id": item.id, "title": item.name, "messageCount": 0,
             "kind": item.kind.value, "countKnown": False}
            for item in items
        ]
    }


def handle_get_messages(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source_name = _source_kind(payload)
    start = _date_arg(payload, "startDate")
    end = _date_arg(payload, "endDate")
    offset = _int_arg(payload, "offset", 0, 0, 1_000_000_000)
    limit = _int_arg(payload, "limit", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        raise WorkerError("INVALID_ARGUMENT", "query 必须是字符串。")
    order = payload.get("order", "asc")
    if order not in ("asc", "desc"):
        raise WorkerError("INVALID_ARGUMENT", "order 必须是 asc 或 desc。")
    if source_name == "demo":
        conversations = load_demo(context.demo_path)
        conversation = _find_demo_conversation(conversations, payload)
        selected = _filter_query(filter_messages(conversation, start, end), query)
        if order == "desc":
            selected = list(reversed(selected))
        page = selected[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "items": [_demo_message_json(item) for item in page],
            "total": len(selected),
            "nextOffset": next_offset if next_offset < len(selected) else None,
        }
    if source_name == "offline":
        conversation = _find_demo_conversation(_offline_conversations(context), payload)
        selected = _filter_query(filter_messages(conversation, start, end), query)
        if order == "desc":
            selected = list(reversed(selected))
        page = selected[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "items": [_offline_message_json(item) for item in page],
            "total": len(selected),
            "nextOffset": next_offset if next_offset < len(selected) else None,
        }
    conversation, page = _real_messages(
        context, payload.get("conversationId", ""), start, end, query, offset, limit, order
    )
    projections = getattr(page, "projections", ())
    return {
        "items": [
            _real_message_json(item, projections[index] if index < len(projections) else None)
            for index, item in enumerate(page.items)
        ],
        "total": page.total,
        "nextOffset": page.next_offset,
    }


# --------------------------------------------------------------------------- #
# B2 cursors / B3 completed keyword snapshots: preview-only message pages
# --------------------------------------------------------------------------- #

_CURSOR_REQUEST_KEYS = {
    "source", "sourceSessionId", "conversationId", "startDate", "endDate", "query", "order",
    "limit", "pageSessionId", "cursor",
}


def _cursor_token(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise WorkerError("INVALID_ARGUMENT", f"{key} 必须是有效的分页标识。")
    return value


def _require_cursor_source(payload: dict[str, Any], context: Context):
    if "source" not in payload:
        raise WorkerError("INVALID_ARGUMENT", "source 必须明确指定。")
    source_name = _source_kind(payload)
    source_session_id = _cursor_token(payload, "sourceSessionId")
    if source_name != context.active_source or source_session_id != context.source_session_id:
        raise WorkerError("SOURCE_SESSION_CHANGED", "数据源已切换或会话已失效，请刷新后重试。")
    if not _cursor_pagination_supported(source_name):
        raise WorkerError("CAPABILITY_UNAVAILABLE", "当前数据源不支持稳定游标分页。")
    if source_name == "wechat4":
        source = _require_wechat(context)
    elif source_name == "offline":
        source = context.offline_page_provider
        if source is None:
            raise WorkerError("CAPABILITY_UNAVAILABLE", "当前数据源不支持稳定游标分页。")
    elif source_name == "demo":
        if context.demo_page_provider is None:
            context.demo_page_provider = ImmutableMessagePageProvider(
                _memory_search_conversations(context, "demo"),
            )
        source = context.demo_page_provider
    else:
        raise WorkerError("CAPABILITY_UNAVAILABLE", "当前数据源不支持稳定游标分页。")
    if not all(callable(getattr(source, name, None)) for name in (
        "get_messages_page", "get_messages_cursor_page",
    )):
        raise WorkerError("CAPABILITY_UNAVAILABLE", "当前数据源不支持稳定游标分页。")
    return source_name, source


def _invoke_cursor_method(method: Callable[..., Any], *args: Any, **kwargs: Any):
    try:
        return method(*args, **kwargs)
    except WorkerError:
        raise
    except Exception as error:  # noqa: BLE001 - source failures have fixed public errors
        code = getattr(error, "code", None)
        message = getattr(error, "message", None)
        if isinstance(code, str) and isinstance(message, str):
            raise WorkerError(code, message, bool(getattr(error, "retryable", False))) from None
        raise WorkerError("PAGE_READ_FAILED", "读取消息页失败，请重试。", True) from None


def _page_response_token(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise WorkerError("PAGE_RESPONSE_INVALID", f"消息分页服务返回了无效的 {label}。")
    return value


def _cursor_page_result(
    page: Any,
    limit: int,
    expected_session_id: str | None = None,
    *,
    first_page: bool = False,
) -> dict[str, Any]:
    items = getattr(page, "items", None)
    total = getattr(page, "total", None)
    if not isinstance(items, (tuple, list)) or len(items) > limit:
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了超出限制的页面。")
    if type(total) is not int or not 0 <= total <= (1 << 63) - 1 or len(items) > total:
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了无效的总数。")
    page_session_id = _page_response_token(getattr(page, "page_session_id", None), "pageSessionId")
    if expected_session_id is not None and page_session_id != expected_session_id:
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了不匹配的会话标识。")
    previous_cursor = _page_response_token(
        getattr(page, "previous_cursor", None), "previousCursor", optional=True,
    )
    next_cursor = _page_response_token(getattr(page, "next_cursor", None), "nextCursor", optional=True)
    if getattr(page, "sort_version", None) != "precise-v1":
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了不支持的排序版本。")
    if total and not items:
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了空白页面。")
    if previous_cursor is not None and previous_cursor == next_cursor:
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了重复的游标。")
    if not items and (previous_cursor is not None or next_cursor is not None):
        raise WorkerError("PAGE_RESPONSE_INVALID", "空消息页不能包含游标。")
    if first_page and (
        previous_cursor is not None or (next_cursor is not None) != (total > len(items))
    ):
        raise WorkerError("PAGE_RESPONSE_INVALID", "首屏游标边界与消息总数不一致。")

    serialized_items: list[dict[str, Any]] = []
    for item in items:
        projection = None
        candidate = item
        if not isinstance(candidate, (Message, JsonMessage, DemoMessage)):
            candidate = getattr(item, "message", None)
            projection = getattr(item, "projection", None)
        common_valid = (
            isinstance(candidate, (Message, JsonMessage, DemoMessage))
            and isinstance(candidate.id, str) and bool(candidate.id)
            and isinstance(candidate.sender, str) and isinstance(candidate.content, str)
            and isinstance(candidate.timestamp, datetime) and candidate.timestamp.tzinfo is not None
            and candidate.timestamp.utcoffset() is not None
        )
        if isinstance(candidate, (Message, JsonMessage)):
            common_valid = common_valid and (
                isinstance(candidate.is_outgoing, bool)
                and (candidate.duration_ms is None or (
                    type(candidate.duration_ms) is int and 0 <= candidate.duration_ms <= 2_147_483_647
                ))
                and (candidate.transcript is None or isinstance(candidate.transcript, str))
                and (candidate.reply_to is None or isinstance(candidate.reply_to, str))
            )
        if isinstance(candidate, Message):
            valid = common_valid and isinstance(candidate.type, MessageType)
            serialized = _real_message_json(candidate, projection) if valid else None
        elif isinstance(candidate, JsonMessage):
            valid = common_valid and candidate.kind in _MESSAGE_KINDS and projection is None
            serialized = _offline_message_json(candidate) if valid else None
        elif isinstance(candidate, DemoMessage):
            valid = common_valid and candidate.kind in _MESSAGE_KINDS and projection is None
            serialized = _demo_message_json(candidate) if valid else None
        else:
            valid = False
            serialized = None
        if not valid or serialized is None:
            raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了无效的消息结构。")
        serialized_items.append(serialized)

    result = {
        "items": serialized_items,
        "total": total,
        "pageSessionId": page_session_id,
        "previousCursor": previous_cursor,
        "nextCursor": next_cursor,
        "sortVersion": "precise-v1",
    }
    try:
        size = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError):
        raise WorkerError("PAGE_RESPONSE_INVALID", "消息分页服务返回了无法编码的页面。") from None
    # Reserve enough space for the JSON-lines envelope, including the maximum
    # accepted requestId, so the complete response stays below the line limit.
    if size > MAX_REQUEST_BYTES - 2048:
        raise WorkerError("PAGE_RESPONSE_TOO_LARGE", "消息页超过协议大小上限，请缩小每页条数。")
    return result


def handle_get_messages_page(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    unknown = set(payload) - _CURSOR_REQUEST_KEYS
    if unknown:
        raise WorkerError("INVALID_ARGUMENT", "分页请求包含不支持的字段。")
    continuation = "pageSessionId" in payload or "cursor" in payload
    allowed = (
        {"source", "sourceSessionId", "pageSessionId", "cursor", "limit"}
        if continuation else
        {"source", "sourceSessionId", "conversationId", "startDate", "endDate", "query", "order", "limit"}
    )
    if set(payload) - allowed:
        page_id = payload.get("pageSessionId")
        if isinstance(page_id, str) and page_id.startswith(KEYWORD_SESSION_PREFIX):
            raise WorkerError("PAGE_CRITERIA_CHANGED", "关键词续页不能更改会话或筛选条件，请重新查询。", True)
        raise WorkerError("INVALID_ARGUMENT", "续页请求不能更改会话或筛选条件。")
    limit = _int_arg(payload, "limit", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    if continuation:
        page_session_id = _cursor_token(payload, "pageSessionId")
        cursor = _cursor_token(payload, "cursor")
        if page_session_id.startswith(KEYWORD_SESSION_PREFIX):
            source_name = _require_keyword_source(payload, context)
            return _continue_keyword_page(context, source_name, page_session_id, cursor, limit)
    elif payload.get("query") is not None:
        source_name = _require_keyword_source(payload, context)
        return _first_keyword_page(payload, context, source_name, limit)

    _source_name, source = _require_cursor_source(payload, context)
    if continuation:
        page = _invoke_cursor_method(
            source.get_messages_cursor_page,
            context.source_session_id,
            page_session_id,
            cursor,
            limit=limit,
        )
        return _cursor_page_result(page, limit, page_session_id)

    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id.strip() or len(conversation_id) > 1024:
        raise WorkerError("INVALID_ARGUMENT", "conversationId 必须是有效的会话标识。")
    start = _date_arg(payload, "startDate")
    end = _date_arg(payload, "endDate")
    lower, upper = inclusive_bounds(start, end)
    order = payload.get("order", "desc")
    if not isinstance(order, str) or order not in {"asc", "desc"}:
        raise WorkerError("INVALID_ARGUMENT", "order 必须是 asc 或 desc。")
    page = _invoke_cursor_method(
        source.get_messages_page,
        conversation_id,
        source_session_id=context.source_session_id,
        start_epoch=int(lower.timestamp()) if lower else None,
        end_epoch_exclusive=int(upper.timestamp()) if upper else None,
        order=order,
        limit=limit,
    )
    return _cursor_page_result(page, limit, first_page=True)


def _require_keyword_source(payload: dict[str, Any], context: Context) -> str:
    if "source" not in payload:
        raise WorkerError("INVALID_ARGUMENT", "source 必须明确指定。")
    source_name = _source_kind(payload)
    source_session_id = _cursor_token(payload, "sourceSessionId")
    if source_name != context.active_source or source_session_id != context.source_session_id:
        raise WorkerError("SOURCE_SESSION_CHANGED", "数据源已切换或会话已失效，请刷新后重试。")
    if not _keyword_pagination_supported(source_name):
        raise WorkerError("CAPABILITY_UNAVAILABLE", "当前数据源不支持关键词快照分页。")
    if source_name == "wechat4":
        _require_wechat(context)
    elif source_name == "offline":
        _offline_conversations(context)
    return source_name


def _keyword_media_selection(context: Context, source: str) -> dict[str, Any]:
    if source == "wechat4":
        return _require_wechat(context).keyword_media_selection()
    return {"recoverImages": False, "recoverVoice": False, "projection": "legacy-memory-preview"}


def _memory_keyword_generations(context: Context, source: str) -> dict[str, Any]:
    snapshot = context.offline if source == "offline" else context.demo_search_snapshot
    return {
        "sourceSessionId": context.source_session_id, "snapshot": id(snapshot),
        "revision": context.offline_page_provider.generation
        if source == "offline" and context.offline_page_provider is not None else 0,
    }


def _memory_keyword_documents(conversation: Any, source: str, lower: datetime | None,
                              upper: datetime | None, deadline: float):
    for index, item in enumerate(conversation.messages):
        if _time.monotonic() >= deadline:
            raise SearchError("SEARCH_BUDGET_EXCEEDED", "关键词检索超过时间预算，请缩小日期范围。", True)
        if (lower is not None and item.timestamp < lower) or (upper is not None and item.timestamp >= upper):
            continue
        # Preserve _filter_query's predicate over the imported content, but
        # cache the existing wire display (images remain safe placeholders).
        # Hidden transcript metadata is NOT a separate legacy match field.
        message = _offline_message_json(item) if source == "offline" else _demo_message_json(item)
        message["timestamp"] = item.timestamp.isoformat(timespec="microseconds")
        yield SearchDocument(
            {"id": conversation.id, "title": conversation.title, "kind": conversation.kind},
            message, (datetime_microseconds(item.timestamp), conversation.id, "memory", "messages", index),
            _MemoryLocator(conversation.id, index),
            content_fingerprint(item.id, item.sender, item.timestamp.isoformat(), item.kind, item.content,
                                getattr(item, "transcript", None), getattr(item, "reply_to", None)),
            keyword_content=item.content,
        )


def _prune_keyword_states(context: Context) -> None:
    context.keyword_page_cache.purge()
    for page_id in list(context.keyword_page_states):
        if page_id not in context.keyword_page_cache.sessions:
            del context.keyword_page_states[page_id]


def _first_keyword_page(payload: dict[str, Any], context: Context, source: str, limit: int) -> dict[str, Any]:
    query = payload.get("query")
    if not isinstance(query, str):
        raise WorkerError("INVALID_ARGUMENT", "query 必须是字符串或 null。")
    try:
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise WorkerError("INVALID_ARGUMENT", "搜索关键词超过长度上限。")
    except UnicodeEncodeError:
        raise WorkerError("INVALID_ARGUMENT", "搜索关键词包含无效字符。") from None
    # Empty/whitespace strings intentionally keep legacy literal semantics;
    # only JSON null (or an omitted query) selects ordinary B2 pagination.
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id.strip() or len(conversation_id) > 1024:
        raise WorkerError("INVALID_ARGUMENT", "conversationId 必须是有效的会话标识。")
    start, end = _date_arg(payload, "startDate"), _date_arg(payload, "endDate")
    lower, upper = inclusive_bounds(start, end)
    order = payload.get("order", "desc")
    if not isinstance(order, str) or order not in {"asc", "desc"}:
        raise WorkerError("INVALID_ARGUMENT", "order 必须是 asc 或 desc。")
    criteria = KeywordSearchCriteria(
        query, start.isoformat() if start else None, end.isoformat() if end else None, order,
        conversation_id=conversation_id, media_selection=_keyword_media_selection(context, source),
    )
    deadline = _time.monotonic() + SOFT_DEADLINE_SECONDS
    generations: Any = {}
    if source == "wechat4":
        documents = _require_wechat(context).iter_keyword_documents(
            conversation_id, int(lower.timestamp()) if lower else None, int(upper.timestamp()) if upper else None,
            deadline=deadline, generations=generations,
        )
    else:
        conversation = _find_demo_conversation(_memory_search_conversations(context, source), payload)
        generations = _memory_keyword_generations(context, source)
        documents = _memory_keyword_documents(conversation, source, lower, upper, deadline)
    _prune_keyword_states(context)
    context.emit_progress(0, 1, "正在完整匹配当前会话；首次查询可能需要较长时间")
    entries, total, candidates = scan_keyword_documents(
        documents, query, order, deadline=deadline, max_candidates=MAX_EXPORT_MESSAGES,
    )
    session = context.keyword_page_cache.create(
        source, context.source_session_id, entries, total, candidates, generations, criteria,
    )
    context.keyword_page_states[session.page_session_id] = _KeywordPageState(source, criteria, generations)
    _prune_keyword_states(context)
    try:
        result = _continue_keyword_page(context, source, session.page_session_id, None, limit)
    except Exception:
        context.keyword_page_cache.discard(session.page_session_id)
        context.keyword_page_states.pop(session.page_session_id, None)
        raise
    context.emit_progress(1, 1, "关键词匹配已完成，后续翻页使用内存快照")
    return result


def _continue_keyword_page(context: Context, source: str, page_session_id: str,
                           cursor: str | None, limit: int) -> dict[str, Any]:
    _prune_keyword_states(context)
    state = context.keyword_page_states.get(page_session_id)
    if state is None:
        raise WorkerError("PAGE_EXPIRED", "关键词分页已过期或被释放，请重新查询。", True)
    try:
        if source == "wechat4":
            _require_wechat(context).validate_keyword_generations(state.generations)
            generations = state.generations
        else:
            generations = _memory_keyword_generations(context, source)
        criteria = replace(state.criteria, media_selection=_keyword_media_selection(context, source))
        return context.keyword_page_cache.cursor_page(
            page_session_id, source, context.source_session_id, generations, criteria, cursor=cursor, limit=limit,
        )
    except Exception:
        # A changed/unreadable source or changed criteria must not leave a
        # usable old session behind; the next attempt is explicitly expired.
        context.keyword_page_cache.discard(page_session_id)
        context.keyword_page_states.pop(page_session_id, None)
        raise


# --------------------------------------------------------------------------- #
# Global search v1: fixed in-memory results and bounded verified context
# --------------------------------------------------------------------------- #


def _memory_search_conversations(context: Context, source: str):
    if source == "offline":
        return _offline_conversations(context)
    if context.demo_search_snapshot is None:
        context.demo_search_snapshot = tuple(load_demo(context.demo_path))
    return context.demo_search_snapshot


def _memory_search_document(conversation: Any, item: Any, index: int) -> SearchDocument:
    message = _demo_message_json(item)
    message["timestamp"] = item.timestamp.isoformat(timespec="microseconds")
    if item.kind == "image":
        message["content"] = "[图片]"
    elif item.kind in {"voice", "file"} and re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/|file:|https?://)", item.content):
        # JSON media references are metadata, not searchable local paths. This
        # is string handling only: never stat/open a referenced file or URL.
        message["content"] = "[语音消息]" if item.kind == "voice" else "[文件] " + re.split(r"[\\/]", item.content)[-1]
    transcript = getattr(item, "transcript", None)
    if transcript:
        message["transcript"] = transcript
    reply = getattr(item, "reply_to", None)
    if reply:
        message["replyTo"] = reply
    duration = getattr(item, "duration_ms", None)
    if duration is not None:
        message["durationMs"] = duration
    fingerprint = content_fingerprint(item.id, item.sender, item.timestamp.isoformat(), item.kind,
                                      item.content, transcript, reply)
    return SearchDocument(
        {"id": conversation.id, "title": conversation.title, "kind": conversation.kind,
         "messageCount": len(conversation.messages), "countKnown": True},
        message, (datetime_microseconds(item.timestamp), conversation.id, "memory", "messages", index),
        _MemoryLocator(conversation.id, index), fingerprint,
    )


def _memory_search_documents(context: Context, source: str, lower: datetime | None, upper: datetime | None,
                             deadline: float):
    for conversation in _memory_search_conversations(context, source):
        for index, item in enumerate(conversation.messages):
            if _time.monotonic() >= deadline:
                raise SearchError("SEARCH_BUDGET_EXCEEDED", "搜索超过时间预算，请缩小日期范围。", True)
            if (lower is None or item.timestamp >= lower) and (upper is None or item.timestamp < upper):
                yield _memory_search_document(conversation, item, index)


def _search_token(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise WorkerError("INVALID_ARGUMENT", f"{key} 必须是有效的服务端标识。")
    return value


def _search_session(payload: dict[str, Any], context: Context, source: str):
    session = context.search_cache.get(_search_token(payload, "searchId"), source, context.source_session_id)
    if source == "wechat4":
        try:
            _require_wechat(context).validate_search_generations(session.generations)
        except Exception:
            context.search_cache.discard(session.search_id)
            raise
    return session


def _attach_live_search_projections(hits: tuple[Any, ...]) -> None:
    """Move bounded C3 metadata from hit locators into the cached hit summaries."""
    for hit in hits:
        slot = getattr(hit.locator, "projection_slot", None)
        if slot is None:
            continue
        projection = getattr(slot, "projection", None)
        if projection is not None:
            if not isinstance(projection, dict):
                raise WorkerError("PAGE_RESPONSE_INVALID", "消息投影结构无效。")
            hit.item.update(projection)
            # The hit item is included in SearchCache's byte accounting. Release
            # the temporary duplicate held only to bridge scan_documents' summary.
            slot.projection = None


def handle_search_messages(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source = _require_search_source(payload, context)
    offset = _int_arg(payload, "offset", 0, 0, 1_000_000_000)
    limit = _int_arg(payload, "limit", DEFAULT_PAGE_SIZE, 1, MAX_PAGE_SIZE)
    common = {"source", "sourceSessionId", "searchId", "offset", "limit"}
    if "searchId" in payload:
        if set(payload) - common:
            raise WorkerError("INVALID_ARGUMENT", "搜索续页不能混入新的关键词、日期或排序条件。")
        return search_page(_search_session(payload, context, source), offset, limit)
    if set(payload) - (common | {"query", "startDate", "endDate", "order"}):
        raise WorkerError("INVALID_ARGUMENT", "全局搜索包含不受支持的条件。")
    if offset != 0:
        raise WorkerError("INVALID_ARGUMENT", "首次搜索的 offset 必须为 0。")
    query = payload.get("query")
    if not isinstance(query, str) or not query.strip():
        raise WorkerError("INVALID_ARGUMENT", "请输入非空的搜索关键词。")
    try:
        if len(query.encode("utf-8")) > MAX_QUERY_BYTES:
            raise WorkerError("INVALID_ARGUMENT", "搜索关键词超过长度上限。")
    except UnicodeEncodeError:
        raise WorkerError("INVALID_ARGUMENT", "搜索关键词包含无效字符。") from None
    order = payload.get("order", "desc")
    if not isinstance(order, str) or order not in {"asc", "desc"}:
        raise WorkerError("INVALID_ARGUMENT", "order 必须是 asc 或 desc。")
    lower, upper = inclusive_bounds(_date_arg(payload, "startDate"), _date_arg(payload, "endDate"))
    deadline = _time.monotonic() + SOFT_DEADLINE_SECONDS
    generations: Any = {}
    if source == "wechat4":
        documents = _require_wechat(context).iter_search_documents(
            int(lower.timestamp()) if lower else None, int(upper.timestamp()) if upper else None,
            deadline=deadline, generations=generations,
        )
    else:
        generations = ("memory", context.source_session_id)
        documents = _memory_search_documents(context, source, lower, upper, deadline)
    context.emit_progress(0, 1, "正在搜索当前数据源的全部可读会话")
    hits, total, coverage = scan_documents(documents, query, order, deadline=deadline)
    if source == "wechat4":
        reservation = getattr(hits, "reservation", None)
        try:
            _attach_live_search_projections(hits)
        except Exception:
            if reservation is not None:
                reservation.release()
                hits.reservation = None
            raise
    session = context.search_cache.create(
        source, context.source_session_id, hits, total, coverage, generations,
        criteria_fingerprint=content_fingerprint(query, payload.get("startDate"), payload.get("endDate"), order),
    )
    try:
        result = search_page(session, 0, limit)
    except Exception:
        context.search_cache.discard(session.search_id)
        raise
    context.emit_progress(1, 1, "搜索已完成")
    return result


def handle_get_message_context(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source = _require_search_source(payload, context)
    if set(payload) - {"source", "sourceSessionId", "searchId", "hitId", "before", "after"}:
        raise WorkerError("INVALID_ARGUMENT", "上下文请求不能携带新的搜索条件或物理位置。")
    before = _int_arg(payload, "before", 20, 0, 20)
    after = _int_arg(payload, "after", 20, 0, 20)
    session = _search_session(payload, context, source)
    hit_id = _search_token(payload, "hitId")
    hit = next((candidate for candidate in session.hits if candidate.item["hitId"] == hit_id), None)
    if hit is None:
        raise WorkerError("CONTEXT_TARGET_UNKNOWN", "命中不属于当前搜索结果，请重新搜索。")
    if source == "wechat4":
        return _require_wechat(context).get_search_context(hit, session.generations, before, after)
    locator = hit.locator
    if not isinstance(locator, _MemoryLocator):
        raise WorkerError("CONTEXT_TARGET_CHANGED", "搜索定位已失效，请重新搜索。")
    conversation = next((item for item in _memory_search_conversations(context, source)
                         if item.id == locator.conversation_id), None)
    if conversation is None or not 0 <= locator.index < len(conversation.messages):
        raise WorkerError("CONTEXT_TARGET_CHANGED", "原会话或消息已不可读取，请重新搜索。")
    target = _memory_search_document(conversation, conversation.messages[locator.index], locator.index)
    if target.fingerprint != hit.fingerprint or target.sort_key != hit.sort_key:
        raise WorkerError("CONTEXT_TARGET_CHANGED", "原消息内容或身份已变化，请重新搜索。")
    start = max(0, locator.index - before)
    end = min(len(conversation.messages), locator.index + after + 1)
    leading = [_memory_search_document(conversation, conversation.messages[index], index).message
               for index in range(start, locator.index)]
    trailing = [_memory_search_document(conversation, conversation.messages[index], index).message
                for index in range(locator.index + 1, end)]
    return bounded_context(target.conversation, leading, target.message, trailing,
                           start > 0, end < len(conversation.messages))


def _output_path(payload: dict[str, Any], suffix: str) -> Path:
    raw = payload.get("outputPath")
    if not isinstance(raw, str) or not raw.strip():
        raise WorkerError("INVALID_ARGUMENT", "outputPath 必须是非空字符串。")
    path = Path(raw)
    if not path.is_absolute():
        raise WorkerError("INVALID_ARGUMENT", "outputPath 必须是绝对路径。")
    if path.suffix.lower() != suffix:
        raise WorkerError("INVALID_ARGUMENT", f"outputPath 的扩展名必须是 {suffix}。")
    if not path.parent.is_dir():
        raise WorkerError("OUTPUT_NOT_WRITABLE", "输出目录不存在或不可写。")
    return path


def _to_export_message(
    conversation: DemoConversation, item: DemoMessage, *, safe_media: bool = False
) -> Message:
    kind = MessageType(item.kind)
    content = item.content
    # Offline JSON is untrusted metadata.  Never test/open a path from its
    # content; media remains an explicit placeholder during every export.
    if kind is MessageType.IMAGE:
        if safe_media:
            export_kind = MessageType.SYSTEM
            content = "[图片]"
        else:
            try:
                export_kind = kind if Path(item.content).is_file() else MessageType.SYSTEM
            except (OSError, ValueError):
                export_kind = MessageType.SYSTEM
    elif kind is MessageType.FILE:
        export_kind = MessageType.SYSTEM
        content = f"[文件] {item.content}"
    elif kind is MessageType.VOICE:
        export_kind = kind if safe_media else MessageType.SYSTEM
    else:
        export_kind = kind if kind in (MessageType.TEXT, MessageType.SYSTEM) else MessageType.SYSTEM
    return Message(
        id=item.id,
        conversation_id=conversation.id,
        sender=item.sender,
        timestamp=item.timestamp,
        type=export_kind,
        content=content,
        is_outgoing=getattr(item, "is_outgoing", False),
        reply_to=getattr(item, "reply_to", None),
        duration_ms=getattr(item, "duration_ms", None),
        transcript=getattr(item, "transcript", None),
    )


def _sanitize_for_text(messages: list[Message]) -> list[Message]:
    result: list[Message] = []
    for message in messages:
        if message.type is MessageType.IMAGE:
            result.append(Message(
                id=message.id, conversation_id=message.conversation_id,
                sender=message.sender, timestamp=message.timestamp,
                type=MessageType.SYSTEM, content="[图片]", is_outgoing=message.is_outgoing,
            ))
        elif message.type is MessageType.FILE:
            # The C3 projection keeps FILE messages as canonical plain text so
            # the structured exporter can distinguish app-message bodies from
            # ordinary one-line attachment names.  The latter still render
            # with the legacy [文件] label; multiline cards stay literal text.
            result.append(message)
        elif message.type is MessageType.VOICE:
            transcript = message.transcript or ""
            content = f"语音转写：{transcript}" if transcript else "[语音消息]"
            result.append(Message(
                id=message.id, conversation_id=message.conversation_id,
                sender=message.sender, timestamp=message.timestamp,
                type=MessageType.SYSTEM, content=content,
                is_outgoing=message.is_outgoing,
            ))
        else:
            result.append(message)
    return result


def _empty_markdown(conversation: Conversation, temp_name: str) -> None:
    """Write the empty-range notice through the escaping exporter.

    The title and conversation id are chat-controlled values; building this
    file with an f-string would make the empty-range export the one path that
    bypasses Markdown hardening.
    """
    export_markdown(conversation, [], temp_name)
    with Path(temp_name).open("a", encoding="utf-8") as handle:
        handle.write("\n（所选日期范围内没有消息。）\n")


def _write_atomic(conversation: DemoConversation, selected: list[DemoMessage], output: Path, overwrite: bool) -> None:
    if output.exists() and not overwrite:
        raise WorkerError("OUTPUT_EXISTS", "输出文件已存在。请确认覆盖或选择其他位置。")
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".wechat-memory-", suffix=".md.tmp", dir=str(output.parent))
        os.close(descriptor)
    except OSError:
        raise WorkerError("OUTPUT_NOT_WRITABLE", "输出位置不可写。") from None
    try:
        export_conversation = Conversation(
            id=conversation.id,
            name=conversation.title,
            kind=ConversationKind(conversation.kind),
        )
        if selected:
            export_markdown(
                export_conversation,
                [_to_export_message(conversation, item) for item in selected],
                temp_name,
            )
        else:
            _empty_markdown(export_conversation, temp_name)
        os.replace(temp_name, output)
    except OSError:
        raise WorkerError("EXPORT_FAILED", "写入 Markdown 文件失败。") from None
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def _write_models_atomic(conversation: Conversation, messages: list[Message],
                         output: Path, overwrite: bool) -> None:
    if output.exists() and not overwrite:
        raise WorkerError("OUTPUT_EXISTS", "输出文件已存在。请确认覆盖或选择其他位置。")
    if output.is_dir() and overwrite:
        # The legacy outer replace reported a write failure for a directory;
        # keep that response contract while using the canonical writer below.
        raise WorkerError("EXPORT_FAILED", "写入 Markdown 文件失败。")

    if messages:
        # Live Markdown is the C3 integration point.  Keep the legacy media
        # projection, but let the canonical exporter render FILE content so
        # app-message cards/forwards cannot reach the vendor basename branch.
        try:
            from wechat_memory.exporters import ExportError, export_conversation
        except ImportError:
            raise WorkerError("CAPABILITY_UNAVAILABLE", "当前软件包缺少导出组件。") from None
        try:
            export_conversation(
                conversation,
                _sanitize_for_text(messages),
                output,
                "markdown",
                overwrite=overwrite,
                materialize_media=False,
            )
        except ExportError as error:
            raise WorkerError(error.code, error.message) from None
        return

    # Preserve the established empty-range Markdown contract, including its
    # legacy notice and same-directory outer replace.
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".wechat-memory-", suffix=".md.tmp", dir=str(output.parent))
        os.close(descriptor)
    except OSError:
        raise WorkerError("OUTPUT_NOT_WRITABLE", "输出位置不可写。") from None
    try:
        _empty_markdown(conversation, temp_name)
        os.replace(temp_name, output)
    except OSError:
        raise WorkerError("EXPORT_FAILED", "写入 Markdown 文件失败。") from None
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def handle_export_markdown(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source_name = _source_kind(payload)
    start = _date_arg(payload, "startDate")
    end = _date_arg(payload, "endDate")
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        raise WorkerError("INVALID_ARGUMENT", "query 必须是字符串。")
    output = _output_path(payload, ".md")
    overwrite = _bool_arg(payload, "overwrite", False)
    if source_name == "demo":
        conversations = load_demo(context.demo_path)
        conversation = _find_demo_conversation(conversations, payload)
        selected = _filter_query(filter_messages(conversation, start, end), query)
        _write_atomic(conversation, selected, output, overwrite)
        return {"path": str(output), "messageCount": len(selected), "bytes": output.stat().st_size}
    if source_name == "offline":
        return _export_with_module(payload, context, "markdown")
    conversation, messages = _real_export_models(context, payload)
    _write_models_atomic(conversation, messages, output, overwrite)
    return {"path": str(output), "messageCount": len(messages), "bytes": output.stat().st_size}


def _models_for_export(payload: dict[str, Any], context: Context) -> tuple[Conversation, list[Message]]:
    source_name = _source_kind(payload)
    start = _date_arg(payload, "startDate")
    end = _date_arg(payload, "endDate")
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        raise WorkerError("INVALID_ARGUMENT", "query 必须是字符串。")
    if source_name == "demo":
        conversations = load_demo(context.demo_path)
        conversation = _find_demo_conversation(conversations, payload)
        selected = _filter_query(filter_messages(conversation, start, end), query)
        model = Conversation(conversation.id, conversation.title, ConversationKind(conversation.kind))
        return model, [_to_export_message(conversation, item) for item in selected]
    if source_name == "offline":
        conversation = _find_demo_conversation(_offline_conversations(context), payload)
        selected = _filter_query(filter_messages(conversation, start, end), query)
        model = Conversation(conversation.id, conversation.title, ConversationKind(conversation.kind))
        return model, [_to_export_message(conversation, item, safe_media=True) for item in selected]
    return _real_export_models(context, payload)


def _export_with_module(payload: dict[str, Any], context: Context, fmt: str) -> dict[str, Any]:
    try:
        from wechat_memory.exporters import ExportError, export_conversation
    except ImportError:
        raise WorkerError("CAPABILITY_UNAVAILABLE", "当前软件包缺少导出组件。") from None
    conversation, messages = _models_for_export(payload, context)
    suffix = {"markdown": ".md", "json": ".json", "pdf": ".pdf"}[fmt]
    output = _output_path(payload, suffix)
    overwrite = _bool_arg(payload, "overwrite", False)
    pages_dir = payload.get("pagesDir")
    if pages_dir is not None:
        if not isinstance(pages_dir, str) or not pages_dir.strip():
            raise WorkerError("INVALID_ARGUMENT", "pagesDir 必须是非空字符串。")
        if not Path(pages_dir).is_absolute():
            raise WorkerError("INVALID_ARGUMENT", "pagesDir 必须是绝对路径。")
    try:
        result = export_conversation(
            conversation, messages, output, fmt, overwrite=overwrite,
            pages_dir=pages_dir,
            # Offline models already replaced image paths with placeholders;
            # their existing voice transcripts are text, not recovered media.
            materialize_media=(
                _bool_arg(payload, "materializeMedia", True) or _source_kind(payload) == "offline"
            ),
        )
    except ExportError as error:
        raise WorkerError(error.code, error.message) from None
    return {
        "path": str(result.path),
        "messageCount": result.message_count,
        "bytes": result.bytes,
        "format": result.format,
        "pageCount": result.page_count,
        "chatPageCount": result.chat_page_count,
        "imagePageCount": result.image_page_count,
        "pagesDir": str(result.pages_dir) if result.pages_dir else None,
        "assetsDir": str(result.assets_dir) if result.assets_dir else None,
    }


def handle_export_json(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    return _export_with_module(payload, context, "json")


def handle_export_pdf(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    return _export_with_module(payload, context, "pdf")


# --------------------------------------------------------------------------- #
# Tasks / scenes / delivery (implemented in wechat_memory.tasks)
# --------------------------------------------------------------------------- #

def _task_service(context: Context):
    try:
        from wechat_memory.tasks import TaskError, TaskService
    except ImportError:
        raise WorkerError("CAPABILITY_UNAVAILABLE", "当前软件包缺少任务组件。") from None
    jobs_dir = Path(context.jobs_dir) if context.jobs_dir else (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / "WechatMemoryNative" / "jobs"
    )
    scenes_file = Path(context.scenes_file) if context.scenes_file else (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / "WechatMemoryNative" / "scenes.json"
    )
    try:
        return TaskService(jobs_root=jobs_dir, scenes_file=scenes_file), TaskError
    except Exception as error:
        raise WorkerError("JOB_STORE_UNAVAILABLE", "任务目录不可用。") from None


def _task_error(error: Exception) -> WorkerError:
    return WorkerError(
        getattr(error, "code", "TASK_FAILED"),
        getattr(error, "message", "任务执行失败。"),
        bool(getattr(error, "retryable", False)),
    )


def handle_list_scenes(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    try:
        return service.list_scenes()
    except TaskError as error:
        raise _task_error(error) from None


def handle_get_scene(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    try:
        return {"scene": service.get_scene(payload.get("sceneId", ""))}
    except TaskError as error:
        raise _task_error(error) from None


def handle_save_scene(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    scene = payload.get("scene")
    if not isinstance(scene, dict):
        raise WorkerError("INVALID_ARGUMENT", "scene 必须是对象。")
    try:
        return {"scene": service.save_scene(scene)}
    except TaskError as error:
        raise _task_error(error) from None


def handle_delete_scene(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    try:
        return service.delete_scene(payload.get("sceneId", ""))
    except TaskError as error:
        raise _task_error(error) from None


def _task_export_writer(
    context: Context,
    payload: dict[str, Any],
    conversation: Conversation,
    messages: list[Message],
):
    """Write every format from one captured snapshot.

    ``handle_export_job`` already read the selected range.  Re-querying the
    source per format (as the old writer did) could mix a manifest with files
    from a different moment when WeChat received messages mid-export.
    """
    pages_dir = payload.get("pagesDir")
    if pages_dir is not None:
        if not isinstance(pages_dir, str) or not pages_dir.strip():
            raise WorkerError("INVALID_ARGUMENT", "pagesDir 必须是非空字符串。")
        if not Path(pages_dir).is_absolute():
            raise WorkerError("INVALID_ARGUMENT", "pagesDir 必须是绝对路径。")

    def writer(files_dir: Path, formats: list[str]) -> list[Path]:
        try:
            from wechat_memory.exporters import ExportError, export_conversation
        except ImportError:
            raise WorkerError("CAPABILITY_UNAVAILABLE", "当前软件包缺少导出组件。") from None
        written: list[Path] = []
        for fmt in formats:
            suffix = {"markdown": ".md", "json": ".json", "pdf": ".pdf"}.get(fmt)
            if suffix is None:
                raise WorkerError("INVALID_ARGUMENT", "不支持的导出格式。")
            output = files_dir / f"chat{suffix}"
            try:
                result = export_conversation(
                    conversation,
                    messages,
                    output,
                    fmt,
                    overwrite=False,
                    pages_dir=pages_dir,
                    # Match direct offline exports: safe models contain no
                    # recoverable paths, but keep their existing transcripts.
                    materialize_media=(
                        _bool_arg(payload, "materializeMedia", True) or _source_kind(payload) == "offline"
                    ),
                )
            except ExportError as error:
                raise WorkerError(error.code, error.message) from None
            written.append(Path(result.path))
        return written
    return writer


def handle_export_job(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    formats = payload.get("formats")
    if not isinstance(formats, list) or not formats:
        raise WorkerError("INVALID_ARGUMENT", "formats 必须是非空数组。")
    conversation, messages = _models_for_export(payload, context)
    scene = None
    scene_id = payload.get("sceneId")
    if scene_id:
        try:
            scene = service.get_scene(scene_id)
        except TaskError as error:
            raise _task_error(error) from None
    target = payload.get("target", {"kind": "none"})
    if not isinstance(target, dict):
        raise WorkerError("INVALID_ARGUMENT", "target 必须是对象。")
    from datetime import datetime as _dt
    start = _date_arg(payload, "startDate")
    end = _date_arg(payload, "endDate")
    start_dt = datetime.combine(start, time.min, tzinfo=DEMO_TIMEZONE) if start else None
    # The manifest stores an inclusive range.  Using the next midnight here
    # made a single-day filter read as "day ~ next day" in every window label.
    end_dt = datetime.combine(end, time.max, tzinfo=DEMO_TIMEZONE) if end else None
    try:
        return service.export_job(
            conversation=conversation,
            messages=messages,
            formats=[str(item) for item in formats],
            target=target,
            scene=scene,
            start=start_dt,
            end=end_dt,
            query=payload.get("query"),
            source_kind=_source_kind(payload),
            source_label=(
                "微信 4.x 只读" if _source_kind(payload) == "wechat4" else
                "离线 JSON · 只读" if _source_kind(payload) == "offline" else
                "虚构演示数据"
            ),
            wechat_version="4.x" if _source_kind(payload) == "wechat4" else None,
            export_fn=_task_export_writer(context, payload, conversation, messages),
            prompt=payload.get("prompt") if isinstance(payload.get("prompt"), str) else None,
            notes=None,
        )
    except TaskError as error:
        raise _task_error(error) from None


def handle_list_jobs(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    try:
        return service.list_jobs(limit=_int_arg(payload, "limit", 50, 1, 500),
                                 include_previews=False)
    except TaskError as error:
        raise _task_error(error) from None


def handle_get_job(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    try:
        return service.get_job(payload.get("jobId", ""),
                               include_prompt=_bool_arg(payload, "includePrompt", False),
                               include_previews=_bool_arg(payload, "includePreviews", False))
    except TaskError as error:
        raise _task_error(error) from None


def handle_deliver_job(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    target = payload.get("target")
    if not isinstance(target, dict):
        raise WorkerError("INVALID_ARGUMENT", "target 必须是对象。")
    try:
        return service.deliver_job(payload.get("jobId", ""), target)
    except TaskError as error:
        raise _task_error(error) from None


def handle_record_delivery(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    target = payload.get("target")
    if not isinstance(target, dict):
        raise WorkerError("INVALID_ARGUMENT", "target 必须是对象。")
    try:
        return service.record_delivery(
            payload.get("jobId", ""), str(payload.get("status", "")), target,
            destination=str(payload.get("destination") or ""),
            detail=str(payload.get("detail") or ""),
            error=str(payload.get("error") or ""),
        )
    except TaskError as error:
        raise _task_error(error) from None


def handle_recover_jobs(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    try:
        return service.recover_jobs()
    except TaskError as error:
        raise _task_error(error) from None


def handle_prune_jobs(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    # Legacy command retained for old clients. The native UI must use the
    # preview_prune_jobs / confirm_prune_jobs token path below.
    service, TaskError = _task_service(context)
    try:
        return service.prune_jobs(_int_arg(payload, "days", 30, 1, 3650))
    except TaskError as error:
        raise _task_error(error) from None


def _required_prune_days(payload: dict[str, Any]) -> int:
    if "days" not in payload:
        raise WorkerError("INVALID_ARGUMENT", "清理保留天数必须明确提供。")
    return _int_arg(payload, "days", 0, 1, 3650)


def _prune_reason_counts(items: list[Any]) -> dict[str, int]:
    """Summarize fixed backend reason codes without returning an unbounded list."""
    counts: dict[str, int] = {}
    overflow_key = "other"
    for item in items:
        reason = item.get("reason") if isinstance(item, dict) else None
        if not isinstance(reason, str) or not reason or len(reason) > 64:
            reason = "unknown"
        if reason in counts:
            counts[reason] += 1
        elif len(counts) < _MAX_PRUNE_REASON_BUCKETS - 1 and reason != overflow_key:
            counts[reason] = 1
        else:
            counts[overflow_key] = counts.get(overflow_key, 0) + 1
    return counts


def _remember_expired_prune_token(context: Context, token: str) -> None:
    context.expired_prune_tokens[token] = None
    context.expired_prune_tokens.move_to_end(token)
    while len(context.expired_prune_tokens) > 8:
        context.expired_prune_tokens.popitem(last=False)


def _purge_expired_prune_tokens(context: Context, now: float) -> None:
    for token, entry in list(context.prune_previews.items()):
        if entry.expires_at <= now:
            del context.prune_previews[token]
            _remember_expired_prune_token(context, token)


def _cache_prune_snapshot(context: Context, days: int, snapshot: dict[str, Any]) -> str:
    now = _time.monotonic()
    _purge_expired_prune_tokens(context, now)
    while len(context.prune_previews) >= _MAX_PRUNE_PREVIEWS:
        context.prune_previews.popitem(last=False)
    token = secrets.token_urlsafe(32)
    while token in context.prune_previews or token in context.expired_prune_tokens:
        token = secrets.token_urlsafe(32)
    context.prune_previews[token] = _PrunePreviewToken(
        days=days,
        snapshot=snapshot,
        expires_at=now + _PRUNE_PREVIEW_TTL_SECONDS,
    )
    return token


def _take_prune_snapshot(context: Context, days: int, payload: dict[str, Any]) -> dict[str, Any]:
    token = payload.get("previewToken")
    if not isinstance(token, str) or not token.strip() or len(token) > 128:
        raise WorkerError("PRUNE_PREVIEW_REQUIRED", "请先预览清理范围，再确认执行。")
    now = _time.monotonic()
    _purge_expired_prune_tokens(context, now)
    entry = context.prune_previews.get(token)
    if entry is None:
        if token in context.expired_prune_tokens:
            context.expired_prune_tokens.pop(token, None)
            raise WorkerError("PRUNE_PREVIEW_EXPIRED", "清理预览已过期，请重新预览。")
        raise WorkerError("PRUNE_PREVIEW_UNKNOWN", "清理预览不存在，请重新预览。")
    if entry.expires_at <= now:
        del context.prune_previews[token]
        _remember_expired_prune_token(context, token)
        context.expired_prune_tokens.pop(token, None)
        raise WorkerError("PRUNE_PREVIEW_EXPIRED", "清理预览已过期，请重新预览。")
    if entry.days != days:
        raise WorkerError("PRUNE_PREVIEW_MISMATCH", "预览与保留天数不一致，请重新预览。")
    # Consume before any deletion starts. A retry after an uncertain pipe loss
    # must obtain a new preview rather than replaying the same authorization.
    del context.prune_previews[token]
    return entry.snapshot


def handle_preview_prune_jobs(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    days = _required_prune_days(payload)
    service, TaskError = _task_service(context)
    try:
        preview = service.preview_prune_jobs(days)
    except TaskError as error:
        raise _task_error(error) from None

    if (
        not isinstance(preview, dict)
        or preview.get("dryRun") is not True
        or type(preview.get("days")) is not int
        or preview["days"] != days
        or not isinstance(preview.get("cutoff"), str)
        or not isinstance(preview.get("candidates"), list)
        or not isinstance(preview.get("skipped"), list)
        or type(preview.get("candidateCount")) is not int
        or preview["candidateCount"] != len(preview["candidates"])
        or type(preview.get("knownArtifactBytes")) is not int
        or preview["knownArtifactBytes"] < 0
        or type(preview.get("unknownSizeCount")) is not int
        or preview["unknownSizeCount"] != len(preview["skipped"])
    ):
        raise WorkerError("PROTOCOL_ERROR", "清理预览结果格式不正确。")

    snapshot = preview.get("snapshot")
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("version")) is not int
        or snapshot["version"] != 1
        or type(snapshot.get("days")) is not int
        or snapshot["days"] != days
        or snapshot.get("cutoff") != preview["cutoff"]
        or not isinstance(snapshot.get("jobs"), list)
        or len(snapshot["jobs"]) != preview["candidateCount"]
    ):
        raise WorkerError("PROTOCOL_ERROR", "清理预览快照格式不正确。")

    token = _cache_prune_snapshot(context, days, snapshot)
    return {
        "dryRun": True,
        "days": days,
        "cutoff": preview["cutoff"],
        "candidateCount": preview["candidateCount"],
        "knownArtifactBytes": preview["knownArtifactBytes"],
        "unknownSizeCount": preview["unknownSizeCount"],
        "skippedReasonCounts": _prune_reason_counts(preview["skipped"]),
        "previewToken": token,
    }


def handle_confirm_prune_jobs(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    days = _required_prune_days(payload)
    snapshot = _take_prune_snapshot(context, days, payload)
    service, TaskError = _task_service(context)
    try:
        result = service.prune_jobs(days, snapshot=snapshot)
    except TaskError as error:
        raise _task_error(error) from None

    if (
        not isinstance(result, dict)
        or result.get("dryRun") is not False
        or type(result.get("days")) is not int
        or result["days"] != days
        or not isinstance(result.get("cutoff"), str)
        or not isinstance(result.get("removed"), list)
        or not isinstance(result.get("skipped"), list)
    ):
        raise WorkerError("PROTOCOL_ERROR", "清理执行结果格式不正确。")
    return {
        "dryRun": False,
        "days": days,
        "cutoff": result["cutoff"],
        "removedCount": len(result["removed"]),
        "skippedCount": len(result["skipped"]),
        "skippedReasonCounts": _prune_reason_counts(result["skipped"]),
    }


def handle_get_job_artifact_path(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    service, TaskError = _task_service(context)
    try:
        path = service.get_job_artifact_path(payload.get("jobId", ""), payload.get("name", ""))
        return {"path": str(path)}
    except TaskError as error:
        raise _task_error(error) from None


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #

def _model_root(context: Context) -> Path:
    if context.model_root:
        return Path(context.model_root)
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        / "WechatMemoryNative" / "models"
    )


def handle_voice_status(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    root = _model_root(context)
    installed = False
    try:
        installed = root.is_dir() and any(root.iterdir())
    except OSError:
        installed = False
    component = True
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        component = False
    return {
        "model": "small",
        "installed": installed,
        "componentAvailable": component,
        "requiresDownload": not installed,
        "connectedVoice": bool(context.wechat is not None and context.wechat.voice_available),
    }


def handle_prepare_voice_model(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    if not _bool_arg(payload, "consent", False):
        raise WorkerError("CONSENT_REQUIRED", "下载语音模型需要明确同意。")
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise WorkerError("CAPABILITY_UNAVAILABLE", "当前软件包缺少本地语音转写组件。") from None
    root = _model_root(context)
    try:
        root.mkdir(parents=True, exist_ok=True)
        context.emit_progress(0, 1, "正在准备本地语音模型")
        WhisperModel("small", device="cpu", compute_type="int8", download_root=str(root))
        context.emit_progress(1, 1, "语音模型已就绪")
    except Exception:
        raise WorkerError("MODEL_DOWNLOAD_FAILED", "语音模型下载或加载失败。请检查网络和磁盘空间后重试。", True) from None
    return {"ready": True, "model": "small"}


def handle_transcribe_messages(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    conversation, messages = _real_export_models(context, payload)
    source = _require_wechat(context)
    if not source.voice_available:
        raise WorkerError("VOICE_UNAVAILABLE", "语音读取未启用。请重新连接并开启语音。")
    try:
        result = source.transcribe(
            messages, progress=context.emit_progress,
            cancelled=lambda: False, model_root=_model_root(context),
        )
    except Exception as error:
        raise WorkerError(
            getattr(error, "code", "TRANSCRIBE_FAILED"),
            getattr(error, "message", "语音转写失败。"),
            bool(getattr(error, "retryable", False)),
        ) from None
    finally:
        # Regeneration may have written a partial transcript cache even if it
        # failed. Never keep a previous keyword match/display snapshot alive.
        _clear_keyword_pages(context)
    transcribed = sum(1 for item in result if item.type is MessageType.VOICE and item.transcript)
    return {"total": len(result), "transcribed": transcribed}


HANDLERS = {
    "hello": handle_hello,
    "list_accounts": handle_list_accounts,
    "connect": handle_connect,
    "disconnect": handle_disconnect,
    "import_json": handle_import_json,
    "list_conversations": handle_list_conversations,
    "get_messages": handle_get_messages,
    "get_messages_page": handle_get_messages_page,
    "search_messages": handle_search_messages,
    "get_message_context": handle_get_message_context,
    "export_markdown": handle_export_markdown,
    "export_json": handle_export_json,
    "export_pdf": handle_export_pdf,
    "list_scenes": handle_list_scenes,
    "get_scene": handle_get_scene,
    "save_scene": handle_save_scene,
    "delete_scene": handle_delete_scene,
    "export_job": handle_export_job,
    "list_jobs": handle_list_jobs,
    "get_job": handle_get_job,
    "deliver_job": handle_deliver_job,
    "record_delivery": handle_record_delivery,
    "recover_jobs": handle_recover_jobs,
    "prune_jobs": handle_prune_jobs,
    "preview_prune_jobs": handle_preview_prune_jobs,
    "confirm_prune_jobs": handle_confirm_prune_jobs,
    "get_job_artifact_path": handle_get_job_artifact_path,
    "voice_status": handle_voice_status,
    "prepare_voice_model": handle_prepare_voice_model,
    "transcribe_messages": handle_transcribe_messages,
    "shutdown": lambda payload, context: {"shutdown": True},
}


# --------------------------------------------------------------------------- #
# JSON Lines loop
# --------------------------------------------------------------------------- #

def _write(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
    stream.flush()


def _success(request_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id,
            "type": "response", "ok": True, "result": result}


def _failure(request_id: str | None, code: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id,
            "type": "response", "ok": False,
            "error": {"code": code, "message": message, "retryable": retryable}}


def _progress(request_id: str, current: int, total: int, label: str) -> dict[str, Any]:
    return {"protocolVersion": PROTOCOL_VERSION, "requestId": request_id,
            "type": "progress", "current": current, "total": total, "label": label}


def serve(stream_in: BinaryIO, stream_out: BinaryIO, demo_path: str,
          jobs_dir: str | None = None, scenes_file: str | None = None,
          model_root: str | None = None) -> int:
    seen_requests: set[str] = set()
    context = Context(demo_path, jobs_dir, scenes_file, model_root)
    try:
        first_request = True
        while True:
            line = stream_in.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                break
            if first_request:
                first_request = False
                if line.startswith(b"\xef\xbb\xbf"):
                    line = line[3:]
            if len(line) > MAX_REQUEST_BYTES and not line.endswith(b"\n"):
                while True:
                    extra = stream_in.readline(MAX_REQUEST_BYTES + 1)
                    if not extra or extra.endswith(b"\n"):
                        break
                _write(stream_out, _failure(None, "INVALID_REQUEST", "请求超过 1 MiB 上限。"))
                continue

            request_id: str | None = None
            try:
                try:
                    payload = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise WorkerError("INVALID_JSON", "请求不是有效的 UTF-8 JSON。") from None
                if not isinstance(payload, dict):
                    raise WorkerError("INVALID_REQUEST", "请求必须是 JSON 对象。")
                candidate = payload.get("requestId")
                if isinstance(candidate, str) and candidate.strip():
                    request_id = candidate
                if payload.get("protocolVersion") != PROTOCOL_VERSION:
                    raise WorkerError("PROTOCOL_MISMATCH", "protocolVersion 必须为 1。")
                if payload.get("type") != "request":
                    raise WorkerError("INVALID_REQUEST", "type 必须为 request。")
                if request_id is None:
                    raise WorkerError("INVALID_REQUEST", "requestId 必须是非空字符串。")
                if request_id in seen_requests:
                    raise WorkerError("DUPLICATE_REQUEST", "requestId 不能重复。")
                seen_requests.add(request_id)
                command = payload.get("command")
                if not isinstance(command, str) or command not in HANDLERS:
                    raise WorkerError("UNKNOWN_COMMAND", "不支持的命令。")
                if command in {"search_messages", "get_message_context", "get_messages_page"} and len(request_id.encode("utf-8")) > 256:
                    raise WorkerError("INVALID_ARGUMENT", "请求标识超过长度上限。")
                body = payload.get("payload", {})
                if not isinstance(body, dict):
                    raise WorkerError("INVALID_ARGUMENT", "payload 必须是 JSON 对象。")
                context.request_id = request_id
                context.progress = lambda current, total, label: _write(
                    stream_out, _progress(request_id or "", current, total, label)
                )
                try:
                    result = HANDLERS[command](body, context)
                finally:
                    context.progress = None
                _write(stream_out, _success(request_id, result))
                if command == "shutdown":
                    return 0
            except WorkerError as error:
                _write(stream_out, _failure(request_id, error.code, error.message, error.retryable))
            except Exception as error:  # noqa: BLE001 - never leak internals or chat content to stdout
                # Modules use structured errors with fixed messages. Anything
                # else is an internal bug; only its class goes to the response.
                code = getattr(error, "code", None)
                message = getattr(error, "message", None)
                retryable = bool(getattr(error, "retryable", False))
                if isinstance(code, str) and isinstance(message, str):
                    _write(stream_out, _failure(request_id, code, message, retryable))
                else:
                    traceback.print_exc(file=sys.stderr)
                    _write(stream_out, _failure(request_id, "INTERNAL_ERROR", "工作进程内部错误。"))
    finally:
        context.search_cache.clear()
        _clear_keyword_pages(context)
        context.demo_search_snapshot = None
        if context.demo_page_provider is not None:
            context.demo_page_provider.close()
            context.demo_page_provider = None
        if context.offline_page_provider is not None:
            context.offline_page_provider.close()
        if context.wechat is not None:
            try:
                context.wechat.close()
            except Exception:
                pass
    return 0


def _prepare_private_temp() -> None:
    """Claim this worker's private root and remove abandoned sibling roots.

    The UI deletes the root after every normal close or timeout.  This sweep
    covers crashes and forced kills, and the per-directory leases ensure a live
    second instance is never deleted just because it is old.
    """
    try:
        from wechat_memory.private_temp import (
            cleanup_abandoned_roots,
            cleanup_legacy_pdf_temp,
            get_worker_temp_root,
        )

        root = get_worker_temp_root()
        cleanup_abandoned_roots(current=root)
        cleanup_legacy_pdf_temp()
    except Exception:
        # Temporary storage problems are reported later by the operation that
        # needs the space; they must not prevent the protocol from starting.
        pass


def main(argv: list[str] | None = None) -> int:
    default_demo = _SCRIPT_DIR.parents[1] / "demo" / "conversations.json"
    parser = argparse.ArgumentParser(description="WeChat Memory worker (JSON Lines over stdio).")
    parser.add_argument("--demo", default=str(default_demo), help="Path to the fictional demo JSON file.")
    parser.add_argument("--jobs-dir", default=None, help="Private jobs root (optional).")
    parser.add_argument("--scenes-file", default=None, help="Private scenes JSON file (optional).")
    parser.add_argument("--model-root", default=None, help="Local speech model directory (optional).")
    args = parser.parse_args(argv)
    protocol_out = sys.stdout.buffer
    # From here on, any accidental print() from this process or a library can
    # only reach stderr; stdout stays a pure protocol channel.
    sys.stdout = sys.stderr
    _prepare_private_temp()
    return serve(sys.stdin.buffer, protocol_out, args.demo, args.jobs_dir,
                 args.scenes_file, args.model_root)


if __name__ == "__main__":
    raise SystemExit(main())
