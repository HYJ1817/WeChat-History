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
import sys
import tempfile
import traceback
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_PAGE_SIZE = 100
MAX_EXPORT_MESSAGES = 200_000
DEFAULT_PAGE_SIZE = 50
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


class Context:
    """Per-worker state plus the current request's progress channel."""

    def __init__(self, demo_path: str, jobs_dir: str | None = None,
                 scenes_file: str | None = None, model_root: str | None = None) -> None:
        self.demo_path = demo_path
        self.jobs_dir = jobs_dir
        self.scenes_file = scenes_file
        self.model_root = model_root
        self.wechat = None  # WeChat4Source | None
        self.progress: Callable[[int, int, str], None] | None = None
        self.request_id: str = ""

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
        kind = item.kind if isinstance(item, DemoMessage) else item.type.value
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
    if source not in {"demo", "wechat4"}:
        raise WorkerError("SOURCE_NOT_FOUND", "当前版本不支持此数据源。")
    return source


def _checked_source(payload: dict[str, Any], context: Context) -> str:
    if _source_kind(payload) != "demo":
        raise WorkerError("SOURCE_NOT_FOUND", "此命令只适用于演示数据源。")
    return context.demo_path


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
    return {
        "id": item.id,
        "sender": item.sender,
        "timestamp": item.timestamp.isoformat(timespec="seconds"),
        "content": item.content,
        "kind": item.kind,
    }


def _real_message_json(item: Message) -> dict[str, Any]:
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

def handle_hello(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": [
            "demo", "wechat4", "accounts", "message_search", "message_pagination",
            "markdown_export", "json_export", "pdf_export", "png_pages",
            "image_recovery", "voice_transcribe", "tasks", "scenes", "delivery",
            "progress_events",
        ],
        "timezone": TIMEZONE_LABEL,
        "pageSizeLimit": MAX_PAGE_SIZE,
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

    if context.wechat is not None:
        try:
            context.wechat.close()
        except Exception:
            pass
        context.wechat = None
    token = payload.get("accountToken")
    if token is not None and not isinstance(token, str):
        raise WorkerError("INVALID_ARGUMENT", "accountToken 必须是字符串。")
    source = WeChat4Source()
    try:
        result = source.connect(
            token,
            enable_images=_bool_arg(payload, "enableImages", False),
            enable_voice=_bool_arg(payload, "enableVoice", False),
            progress=context.emit_progress,
            cancelled=lambda: False,
        )
    except SourceFailure as error:
        source.close()
        raise WorkerError(error.code, error.message, error.retryable)
    context.wechat = source
    return {
        "connected": True,
        "source": "wechat4",
        **result,
        "capabilities": source.capabilities,
    }


def handle_disconnect(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    if context.wechat is not None:
        context.wechat.close()
        context.wechat = None
    return {"connected": False}


def handle_list_conversations(payload: dict[str, Any], context: Context) -> dict[str, Any]:
    source_name = _source_kind(payload)
    query = payload.get("query")
    if query is not None and not isinstance(query, str):
        raise WorkerError("INVALID_ARGUMENT", "query 必须是字符串。")
    if source_name == "demo":
        conversations = load_demo(context.demo_path)
        needle = (query or "").casefold()
        return {
            "items": [
                {"id": item.id, "title": item.title, "messageCount": len(item.messages),
                 "kind": item.kind, "countKnown": True}
                for item in conversations
                if not needle or needle in item.title.casefold()
            ]
        }
    source = _require_wechat(context)
    items = source.list_conversations(query)
    return {
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
    conversation, page = _real_messages(
        context, payload.get("conversationId", ""), start, end, query, offset, limit, order
    )
    return {
        "items": [_real_message_json(item) for item in page.items],
        "total": page.total,
        "nextOffset": page.next_offset,
    }


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


def _to_export_message(conversation: DemoConversation, item: DemoMessage) -> Message:
    kind = MessageType(item.kind)
    # Demo media contents are placeholders unless they point at a readable
    # local file. Only then may an image stay an image: this lets deterministic
    # tests (and a user-edited demo file) exercise real media export without
    # ever inventing links or transcripts for placeholder text.
    if kind is MessageType.IMAGE:
        try:
            export_kind = kind if Path(item.content).is_file() else MessageType.SYSTEM
        except (OSError, ValueError):
            export_kind = MessageType.SYSTEM
    else:
        export_kind = (
            kind
            if kind in (MessageType.TEXT, MessageType.SYSTEM)
            else MessageType.SYSTEM
        )
    return Message(
        id=item.id,
        conversation_id=conversation.id,
        sender=item.sender,
        timestamp=item.timestamp,
        type=export_kind,
        content=item.content,
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
            result.append(Message(
                id=message.id, conversation_id=message.conversation_id,
                sender=message.sender, timestamp=message.timestamp,
                type=MessageType.SYSTEM, content=f"[文件] {message.content}",
                is_outgoing=message.is_outgoing,
            ))
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
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix=".wechat-memory-", suffix=".md.tmp", dir=str(output.parent))
        os.close(descriptor)
    except OSError:
        raise WorkerError("OUTPUT_NOT_WRITABLE", "输出位置不可写。") from None
    try:
        if messages:
            export_markdown(conversation, _sanitize_for_text(messages), temp_name)
        else:
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
            materialize_media=_bool_arg(payload, "materializeMedia", True),
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
                    materialize_media=_bool_arg(payload, "materializeMedia", True),
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
            source_kind="wechat4" if _source_kind(payload) == "wechat4" else "demo",
            source_label="微信 4.x 只读" if _source_kind(payload) == "wechat4" else "虚构演示数据",
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
    service, TaskError = _task_service(context)
    try:
        return service.prune_jobs(_int_arg(payload, "days", 30, 1, 3650))
    except TaskError as error:
        raise _task_error(error) from None


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
    transcribed = sum(1 for item in result if item.type is MessageType.VOICE and item.transcript)
    return {"total": len(result), "transcribed": transcribed}


HANDLERS = {
    "hello": handle_hello,
    "list_accounts": handle_list_accounts,
    "connect": handle_connect,
    "disconnect": handle_disconnect,
    "list_conversations": handle_list_conversations,
    "get_messages": handle_get_messages,
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
