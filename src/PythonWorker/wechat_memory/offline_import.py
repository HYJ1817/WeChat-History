"""Validated, memory-only reader for the native app's offline JSON source.

The reader accepts the v1 JSON emitted by this application and the compatible
v1 demo shape. It deliberately returns a complete in-memory snapshot: after a
successful import, querying the source never follows paths found in message
content and never re-reads the user's file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC8 = timezone(timedelta(hours=8))
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_CONVERSATIONS = 10_000
MAX_MESSAGES = 200_000
MAX_ID_LENGTH = 512
MAX_TEXT_LENGTH = 4 * 1024 * 1024
_CONVERSATION_KINDS = {"direct", "group"}
_MESSAGE_KINDS = {"text", "image", "file", "voice", "system"}


class OfflineImportError(Exception):
    """A fixed, user-explainable validation/read failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class JsonMessage:
    id: str
    sender: str
    timestamp: datetime
    kind: str
    content: str
    is_outgoing: bool = False
    reply_to: str | None = None
    duration_ms: int | None = None
    transcript: str | None = None


@dataclass(frozen=True, slots=True)
class JsonConversation:
    id: str
    title: str
    kind: str
    messages: tuple[JsonMessage, ...]


@dataclass(frozen=True, slots=True)
class ImportedJson:
    conversations: tuple[JsonConversation, ...]
    schema: str
    message_count: int


def _string(value: Any, label: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise OfflineImportError("SOURCE_INVALID", f"{label}必须是字符串。")
    if len(value) > max_length:
        raise OfflineImportError("SOURCE_TOO_LARGE", f"{label}超过长度上限。")
    if "\x00" in value:
        raise OfflineImportError("SOURCE_INVALID", f"{label}包含无效字符。")
    # JSON escapes can produce lone UTF-16 surrogates even when the file's
    # bytes were valid UTF-8.  Validate every retained string before publishing
    # the snapshot; neither the JSON Lines encoder nor exporters can emit them.
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise OfflineImportError("SOURCE_INVALID", f"{label}包含无效的 Unicode 字符。") from None
    return value


def _required_text(raw: dict[str, Any], key: str, label: str, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    value = _string(raw.get(key), f"{label}的 {key} ", max_length=max_length)
    if not value.strip():
        raise OfflineImportError("SOURCE_INVALID", f"{label}缺少有效的 {key}。")
    return value


def _identifier(raw: dict[str, Any], key: str, label: str) -> str:
    value = _required_text(raw, key, label, max_length=MAX_ID_LENGTH)
    if any(char in value for char in "\r\n"):
        raise OfflineImportError("SOURCE_INVALID", f"{label}的 {key} 包含无效换行。")
    return value


def _parse_timestamp(value: Any, *, allow_naive: bool) -> datetime:
    value = _string(value, "消息 timestamp ")
    if not value.strip():
        raise OfflineImportError("SOURCE_INVALID", "消息 timestamp 必须是非空字符串。")
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise OfflineImportError("SOURCE_INVALID", "消息 timestamp 不是有效的 ISO 8601 时间。") from None
    try:
        aware = timestamp.tzinfo is not None and timestamp.utcoffset() is not None
    except (OverflowError, ValueError):
        aware = False
    if not aware:
        if not allow_naive:
            raise OfflineImportError("SOURCE_INVALID", "消息 timestamp 必须包含时区偏移。")
        timestamp = timestamp.replace(tzinfo=UTC8)
    try:
        # DateTimeOffset requires both the wall clock and its UTC instant to
        # fit years 1..9999. A naive year-1 midnight assigned UTC+08:00 has an
        # unrepresentable UTC instant even though normalising to UTC8 is a no-op.
        timestamp.astimezone(timezone.utc)
        return timestamp.astimezone(UTC8)
    except (OverflowError, ValueError):
        raise OfflineImportError("SOURCE_INVALID", "消息 timestamp 的 UTC 时间或显示时间超出 1 到 9999 年范围。") from None


def _optional_bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise OfflineImportError("SOURCE_INVALID", f"消息 {key} 必须是布尔值。")
    return value


def _optional_text(raw: dict[str, Any], key: str, label: str, *, max_length: int = MAX_TEXT_LENGTH) -> str | None:
    if key not in raw or raw[key] is None:
        return None
    return _string(raw[key], f"{label}的 {key} ", max_length=max_length)


def _optional_duration(raw: dict[str, Any]) -> int | None:
    if "duration_ms" not in raw or raw["duration_ms"] is None:
        return None
    value = raw["duration_ms"]
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 24 * 60 * 60 * 1000:
        raise OfflineImportError("SOURCE_INVALID", "消息 duration_ms 必须是合理的非负整数。")
    return value


def _parse_message(raw: Any, conversation_id: str, *, allow_naive: bool, seen_ids: set[str]) -> JsonMessage:
    if not isinstance(raw, dict):
        raise OfflineImportError("SOURCE_INVALID", "消息条目必须是 JSON 对象。")
    message_id = _identifier(raw, "id", "消息")
    if message_id in seen_ids:
        raise OfflineImportError("SOURCE_INVALID", "同一会话内的消息 id 不能重复。")
    seen_ids.add(message_id)
    kind = raw.get("type")
    if not isinstance(kind, str) or kind not in _MESSAGE_KINDS:
        raise OfflineImportError("SOURCE_INVALID", "消息 type 无效。")
    content = _string(raw.get("content"), "消息 content ")
    if kind != "system" and not content.strip():
        raise OfflineImportError("SOURCE_INVALID", "非系统消息的 content 不能为空。")
    sender = _required_text(raw, "sender", "消息")
    timestamp = _parse_timestamp(raw.get("timestamp"), allow_naive=allow_naive)
    reply_to = _optional_text(raw, "reply_to", "消息", max_length=MAX_ID_LENGTH)
    if reply_to is not None and not reply_to.strip():
        raise OfflineImportError("SOURCE_INVALID", "消息 reply_to 不能为空。")
    transcript = _optional_text(raw, "transcript", "消息")
    return JsonMessage(
        id=message_id,
        sender=sender,
        timestamp=timestamp,
        kind=kind,
        content=content,
        is_outgoing=_optional_bool(raw, "is_outgoing", False),
        reply_to=reply_to,
        duration_ms=_optional_duration(raw),
        transcript=transcript,
    )


def _parse_conversation(raw: Any, seen_ids: set[str], *, allow_naive: bool, total: list[int]) -> JsonConversation:
    if not isinstance(raw, dict):
        raise OfflineImportError("SOURCE_INVALID", "会话条目必须是 JSON 对象。")
    conversation_id = _identifier(raw, "id", "会话")
    if conversation_id in seen_ids:
        raise OfflineImportError("SOURCE_INVALID", "会话 id 不能重复。")
    seen_ids.add(conversation_id)
    title = _required_text(raw, "name", "会话")
    kind = raw.get("kind", "direct")
    if not isinstance(kind, str) or kind not in _CONVERSATION_KINDS:
        raise OfflineImportError("SOURCE_INVALID", "会话 kind 只能是 direct 或 group。")
    raw_messages = raw.get("messages")
    if not isinstance(raw_messages, list):
        raise OfflineImportError("SOURCE_INVALID", "会话 messages 必须是数组。")
    messages: list[JsonMessage] = []
    message_ids: set[str] = set()
    for raw_message in raw_messages:
        total[0] += 1
        if total[0] > MAX_MESSAGES:
            raise OfflineImportError("SOURCE_TOO_LARGE", "消息总数超过 200000 条上限。")
        messages.append(_parse_message(raw_message, conversation_id, allow_naive=allow_naive, seen_ids=message_ids))
    messages.sort(key=lambda item: item.timestamp)
    return JsonConversation(conversation_id, title, kind, tuple(messages))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # Never echo a chat-controlled property name in the error.
            raise OfflineImportError("SOURCE_INVALID", "JSON 对象中不能包含重复字段。")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    # Python's default decoder accepts NaN and Infinity, which are not JSON.
    raise OfflineImportError("SOURCE_INVALID", "JSON 文件不能包含非有限数值。")


def load_json_file(path: str | Path, *, allow_naive_timestamps: bool = True) -> ImportedJson:
    """Read and fully validate one user-selected JSON file.

    ``allow_naive_timestamps`` is kept explicit so the existing demo loader can
    retain its strict behaviour while offline import can read the historical
    dependency example, whose v1 timestamps had no offset and mean UTC+08:00.
    """
    file = Path(path)
    if not file.is_absolute() or file.suffix.lower() != ".json":
        raise OfflineImportError("INVALID_ARGUMENT", "离线导入只支持绝对路径的 .json 文件。")
    try:
        size = file.stat().st_size
    except FileNotFoundError:
        raise OfflineImportError("SOURCE_NOT_FOUND", "找不到所选 JSON 文件。") from None
    except OSError:
        raise OfflineImportError("SOURCE_NOT_FOUND", "无法读取所选 JSON 文件。") from None
    if size > MAX_JSON_BYTES:
        raise OfflineImportError("SOURCE_TOO_LARGE", "JSON 文件超过 64 MiB 大小上限。")
    try:
        # The file may grow between stat and read; never allocate past the cap.
        with file.open("rb") as handle:
            raw_bytes = handle.read(MAX_JSON_BYTES + 1)
    except FileNotFoundError:
        raise OfflineImportError("SOURCE_NOT_FOUND", "找不到所选 JSON 文件。") from None
    except OSError:
        raise OfflineImportError("SOURCE_NOT_FOUND", "无法读取所选 JSON 文件。") from None
    if len(raw_bytes) > MAX_JSON_BYTES:
        raise OfflineImportError("SOURCE_TOO_LARGE", "JSON 文件超过 64 MiB 大小上限。")
    try:
        raw_text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise OfflineImportError("SOURCE_INVALID", "JSON 文件不是合法的 UTF-8。") from None
    try:
        payload = json.loads(raw_text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except RecursionError:
        raise OfflineImportError("SOURCE_INVALID", "JSON 嵌套层级过深。") from None
    except ValueError:
        # Includes JSONDecodeError and Python's bounded integer conversion.
        raise OfflineImportError("SOURCE_INVALID", "JSON 文件无效或数值超出可处理范围。") from None
    if not isinstance(payload, dict):
        raise OfflineImportError("SOURCE_INVALID", "JSON 顶层结构必须是对象。")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise OfflineImportError("SOURCE_INVALID", "JSON version 必须是整数 1。")
    if version != 1:
        raise OfflineImportError("SOURCE_UNSUPPORTED", "不支持此 JSON 版本；当前仅支持 version 1。")
    entries = payload.get("conversations")
    if not isinstance(entries, list):
        raise OfflineImportError("SOURCE_INVALID", "JSON 缺少 conversations 数组。")
    if len(entries) > MAX_CONVERSATIONS:
        raise OfflineImportError("SOURCE_TOO_LARGE", "会话总数超过 10000 个上限。")
    seen_ids: set[str] = set()
    total = [0]
    conversations = tuple(
        _parse_conversation(entry, seen_ids, allow_naive=allow_naive_timestamps, total=total)
        for entry in entries
    )
    schema = "wechat-memory-json-v1" if "notice" not in payload else "demo-json-v1"
    return ImportedJson(conversations, schema, total[0])
