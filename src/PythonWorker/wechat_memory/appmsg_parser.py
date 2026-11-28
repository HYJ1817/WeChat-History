"""Bounded, side-effect-free projections of a small set of app-message shapes.

``parse_appmsg(xml, raw_type=None, *, limits=None)`` returns an immutable local
projection. ``canonical_text`` is plain text for Message.content/search/export;
all other strings are also *text*, never HTML, executable URLs or local paths to
open. This module imports neither worker/vendor models nor any I/O facilities.
It accepts already-decompressed UTF-8 bytes or Unicode, never a file or stream.
The caller must separately bound BLOB reads and decompression *before* calling.

Only direct ``msg/appmsg`` (or appmsg-root) fields are interpreted. Supported
synthetic shapes use appmsg/type 5=link, 6=file, 57=quote, 19=record; the raw-type
high word is only a consistency hint, never a replacement type mapping. Records
use recorditem -> recordinfo/datalist/dataitem, inline or escaped XML. Nested
records use dataitem datatype=17 and a direct recorditem/recordxml. This is not a
claim of compatibility with every WeChat version, and is not a lossless archive.

Limits apply before/during XML construction, not after building an unsafe tree:
UTF-8 bytes per document, depth per document, and nodes across all embedded XML.
Only designated embedded fields receive at most ONE extra HTML-unescape pass
(after XML's own entity decoding). DTDs/entities are rejected by Expat callbacks.
Entry and nesting limits are shared across the whole projection. Canonical text
and the combined retained display-field strings each have an output-byte budget
(the result is thus bounded by approximately twice max_output_bytes plus fixed
metadata). Quote summaries additionally have their own UTF-8 byte limit.

parse_status: ok / partial / unsupported / invalid / rejected / limit_exceeded.
Hard XML budgets use limit_exceeded, even inside an otherwise readable card;
entry/nesting/output clipping uses partial and truncated=True. ``issues`` contains
fixed machine-readable reasons, never source XML. omitted_count counts omitted
forward entries, NOT characters: it is exact for inspected record containers,
including nested containers, or None when opaque/skipped structures prevent an
exact count. Declared datalist counts are never trusted to invent this number.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from html import unescape
import re
from xml.etree.ElementTree import Element, TreeBuilder
from xml.parsers import expat


APPMSG_PARSER_VERSION = "appmsg-v1"
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_XML_DEPTH = 32
MAX_XML_NODES = 10_000
MAX_NESTING = 2
MAX_FORWARD_ENTRIES = 200
MAX_OUTPUT_BYTES = 64 * 1024
MAX_QUOTE_BYTES = 2 * 1024
_TRUNCATED = "\n[内容已截断]"
# Reserve enough space for the truncation notice without ever exceeding a cap.
_NOTICE_BYTES = len(_TRUNCATED.encode("utf-8"))
_TYPE_KINDS = {5: "link", 6: "file", 19: "forward", 57: "quote"}
_QUOTE_MEDIA = {3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置"}
_RECORD_MEDIA = {2: "图片", 3: "语音", 4: "视频", 6: "位置", 7: "表情"}
_STATUS_ORDER = {"ok": 0, "partial": 1, "unsupported": 2,
                 "invalid": 3, "limit_exceeded": 4, "rejected": 5}
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class AppMessageLimits:
    """Defaults are hard ceilings; callers/tests may request smaller budgets."""

    max_xml_bytes: int = MAX_XML_BYTES
    max_depth: int = MAX_XML_DEPTH
    max_nodes: int = MAX_XML_NODES
    max_nesting: int = MAX_NESTING
    max_entries: int = MAX_FORWARD_ENTRIES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    max_quote_bytes: int = MAX_QUOTE_BYTES

    def __post_init__(self) -> None:
        ceilings = (MAX_XML_BYTES, MAX_XML_DEPTH, MAX_XML_NODES, MAX_NESTING,
                    MAX_FORWARD_ENTRIES, MAX_OUTPUT_BYTES, MAX_QUOTE_BYTES)
        minimums = (1, 1, 1, 0, 0, 256, 32)
        for field, ceiling, minimum in zip(fields(self), ceilings, minimums):
            value = getattr(self, field.name)
            if type(value) is not int or not minimum <= value <= ceiling:
                raise ValueError(f"{field.name} must be an integer in [{minimum}, {ceiling}]")


@dataclass(frozen=True, slots=True)
class ForwardItem:
    sender: str | None
    timestamp: str | None
    content: str
    display_kind: str
    forward_items: tuple[ForwardItem, ...] = ()


@dataclass(frozen=True, slots=True)
class AppMessageProjection:
    canonical_text: str
    display_kind: str
    title: str = ""
    description: str = ""
    url: str | None = None
    file_name: str | None = None
    file_size: int | None = None
    reply_to: str | None = None
    quote_summary: str | None = None
    quote_sender: str | None = None
    forward_items: tuple[ForwardItem, ...] = ()
    parse_status: str = "ok"
    truncated: bool = False
    omitted_count: int | None = 0
    issues: tuple[str, ...] = ()


class _XMLFailure(Exception):
    def __init__(self, reason: str, status: str) -> None:
        self.reason = reason
        self.status = status


class _State:
    def __init__(self, limits: AppMessageLimits) -> None:
        self.limits = limits
        self.nodes = 0
        self.entries = 0
        self.retained_bytes = 0
        self.status = "ok"
        self.truncated = False
        self.omitted: int | None = 0
        self.issues: list[str] = []

    def issue(self, reason: str, status: str = "partial", *, truncate: bool = False) -> None:
        if reason not in self.issues:
            self.issues.append(reason)
        if _STATUS_ORDER[status] > _STATUS_ORDER[self.status]:
            self.status = status
        self.truncated |= truncate

    def text(self, value: str, *, cap: int | None = None, atomic: bool = False) -> str:
        """Charge all retained display strings to one shared byte allowance."""
        value = _CONTROLS.sub("�", value).strip()
        available = max(0, self.limits.max_output_bytes - self.retained_bytes)
        if cap is not None:
            available = min(available, cap)
        text, clipped = _clip(value, available)
        if clipped and atomic:
            text = ""  # Identifiers are retained whole or not at all.
        self.retained_bytes += len(text.encode("utf-8"))
        if clipped:
            self.issue("quote_limit" if cap is not None and available == cap else
                       "output_limit", truncate=True)
        return text

    def omit(self, entries: list[Element]) -> None:
        if self.omitted is not None:
            # A skipped record may hide an unknown number of descendants. Never
            # use a declared count or claim this is the full omitted total.
            if any(item.attrib.get("datatype") == "17" or
                   any(_local(child.tag) in {"recorditem", "recordxml"}
                       for child in item) for item in entries):
                self.omitted = None
            else:
                self.omitted += len(entries)


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(text)
    # UTF-8 uses >=1 byte per code point. Bound the temporary encoding as well.
    prefix = text[:limit]
    encoded = prefix.encode("utf-8", errors="replace")
    if len(text) <= limit and len(encoded) <= limit:
        return encoded.decode("utf-8"), False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(parent: Element, name: str) -> list[Element]:
    return [child for child in parent if _local(child.tag) == name]


def _child(parent: Element, name: str, state: _State) -> Element | None:
    children = _children(parent, name)
    if len(children) > 1:
        state.issue("ambiguous_" + name)
        return None
    return children[0] if children else None


def _value(parent: Element, name: str, state: _State) -> str:
    child = _child(parent, name, state)
    if child is None:
        return ""
    if len(child):
        state.issue("structured_" + name)
        return ""  # Never steal a descendant's similarly named text field.
    return (child.text or "").strip()


def _integer(text: str, *, positive: bool = False) -> int | None:
    if not text or len(text) > 20 or not text.isascii() or not text.isdecimal():
        return None
    number = int(text)
    if number > 2**64 - 1 or (positive and number == 0):
        return None
    return number


def _xml_bytes(content: str | bytes, limit: int) -> bytes:
    if len(content) > limit:
        raise _XMLFailure("xml_byte_limit", "limit_exceeded")
    if isinstance(content, bytes):
        return content
    pieces: list[bytes] = []
    total = 0
    try:
        for offset in range(0, len(content), 4096):
            piece = content[offset:offset + 4096].encode("utf-8", errors="strict")
            total += len(piece)
            if total > limit:
                raise _XMLFailure("xml_byte_limit", "limit_exceeded")
            pieces.append(piece)
    except UnicodeEncodeError:
        raise _XMLFailure("invalid_unicode", "invalid") from None
    return b"".join(pieces)


def _parse_xml(content: str | bytes, state: _State) -> Element:
    data = _xml_bytes(content, state.limits.max_xml_bytes)
    # This is deliberately a UTF-8-only contract, not an encoding detector.
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _XMLFailure("invalid_utf8", "invalid") from None
    builder = TreeBuilder()
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator="}")
    depth = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > state.limits.max_depth:
            raise _XMLFailure("xml_depth_limit", "limit_exceeded")
        state.nodes += 1
        if state.nodes > state.limits.max_nodes:
            raise _XMLFailure("xml_node_limit", "limit_exceeded")
        builder.start(name, attrs)

    def forbidden(*_args: object) -> None:
        raise _XMLFailure("forbidden_xml_declaration", "rejected")

    parser.StartElementHandler = start

    def end(name: str) -> None:
        nonlocal depth
        builder.end(name)
        depth -= 1

    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    parser.UnparsedEntityDeclHandler = forbidden
    parser.ProcessingInstructionHandler = lambda *_args: state.issue("processing_instruction_ignored")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        # Small feeds also bound how far the native parser can run ahead of a
        # limit callback. The tree is never built by an unbounded fromstring.
        for offset in range(0, len(data), 4096):
            parser.Parse(data[offset:offset + 4096], False)
        parser.Parse(b"", True)
        return builder.close()
    except expat.ExpatError:
        raise _XMLFailure("malformed_xml", "invalid") from None


def _xml_failure(failure: _XMLFailure, state: _State, *, embedded: bool = False) -> None:
    status = failure.status
    if embedded and status == "invalid":
        status = "partial"
    state.issue(failure.reason, status, truncate=failure.status == "limit_exceeded")
    state.omitted = None


def _embedded(holder: Element, state: _State, nesting: int) -> Element | None:
    if nesting > state.limits.max_nesting:
        state.issue("nesting_limit", truncate=True)
        state.omitted = None
        return None
    if len(holder):
        if len(holder) == 1 and not (holder.text or "").strip() and not (holder[0].tail or "").strip():
            return holder[0]
        state.issue("ambiguous_embedded_xml")
        state.omitted = None
        return None
    text = (holder.text or "").strip()
    if not text:
        state.issue("missing_embedded_xml")
        state.omitted = None
        return None
    if text.startswith(("&lt;", "&#60;", "&#x3c;", "&#x3C;")):
        text = unescape(text)  # ONE pass, only at an explicit embedded-XML site.
    try:
        return _parse_xml(text, state)
    except _XMLFailure as failure:
        _xml_failure(failure, state, embedded=True)
        return None


def _appmsg(root: Element, state: _State) -> Element | None:
    if _local(root.tag) == "appmsg":
        return root
    if _local(root.tag) == "msg":
        return _child(root, "appmsg", state)
    return None


def _join(*parts: str | None) -> str:
    return "\n".join(part for part in parts if part)


def _file_fields(parent: Element, title: str, state: _State, *, record: bool = False) -> tuple[str, int | None]:
    attach = parent if record else _child(parent, "appattach", state)
    if attach is None:
        return title, None
    name = _value(attach, "datatitle" if record else "filename", state) or title
    size_text = _value(attach, "datasize" if record else "totallen", state)
    size = _integer(size_text)
    if size_text and size is None:
        state.issue("invalid_file_size")
    # 'title' has already been charged; only newly extracted filename costs more.
    return (title if name == title else state.text(name)), size


def _quote(app: Element, title: str, state: _State, nesting: int) -> dict:
    refer = _child(app, "refermsg", state)
    if refer is None:
        state.issue("missing_reference")
        return {"canonical_text": _join(title or "[引用消息]", "[引用内容缺失]"),
                "display_kind": "quote", "title": title}
    sender = state.text(_value(refer, "displayname", state)) or None
    reference_id = _value(refer, "svrid", state)
    reply_to = (state.text(reference_id, atomic=True) or None) if _integer(reference_id, positive=True) is not None else None
    if reply_to is None:
        state.issue("missing_or_invalid_reference_id")
    subtype = _integer(_value(refer, "type", state))
    if subtype == 1:
        summary = _value(refer, "content", state)
    elif subtype == 49:
        holder = _child(refer, "content", state)
        root = _embedded(holder, state, nesting + 1) if holder is not None else None
        nested = _appmsg(root, state) if root is not None else None
        if nested is not None:
            summary = _card(nested, state, nesting + 1)["canonical_text"]
        else:
            summary = "[引用卡片未展开]"
            state.issue("unsupported_quote_card")
    elif subtype in _QUOTE_MEDIA:
        summary = f"[{_QUOTE_MEDIA[subtype]}]"
    else:
        summary = "[不支持的引用类型]"
        state.issue("unsupported_quote_type")
    if not summary:
        summary = "[引用内容缺失]"
        state.issue("missing_quote_content")
    summary = state.text(summary, cap=state.limits.max_quote_bytes)
    canonical = _join(title or "[引用消息]", f"引用 {sender or '未知发送者'}: {summary}")
    return {"canonical_text": canonical, "display_kind": "quote", "title": title,
            "reply_to": reply_to, "quote_sender": sender, "quote_summary": summary}


def _record_container(root: Element, state: _State) -> Element | None:
    name = _local(root.tag)
    if name in {"recordinfo", "recorditem", "recordxml"}:
        return _child(root, "datalist", state)
    if name == "datalist":
        return root
    return None


def _item(item: Element, state: _State, nesting: int) -> ForwardItem:
    source = _child(item, "dataitemsource", state)
    sender = state.text(_value(source, "sourcename", state)) if source is not None else ""
    timestamp = state.text(_value(source, "sourcetime", state)) if source is not None else ""
    datatype = _integer(item.attrib.get("datatype", ""))
    title = state.text(_value(item, "datatitle", state))
    description = _value(item, "datadesc", state)
    children: tuple[ForwardItem, ...] = ()
    if datatype == 1:
        kind, text = "text", state.text(description)
    elif datatype in _RECORD_MEDIA:
        kind, text = "media", f"[{_RECORD_MEDIA[datatype]}]"
        if description:
            text = _join(text, state.text(description))
    elif datatype == 5:
        kind = "link"
        url = state.text(_value(item, "dataurl", state))
        text = _join("[链接] " + title, state.text(description), f"URL: {url}" if url else None)
    elif datatype == 8:
        kind = "file"
        name, size = _file_fields(item, title, state, record=True)
        text = _join("[文件] " + name, f"大小: {size} 字节" if size is not None else None)
    elif datatype == 17:
        kind = "forward"
        holders = _children(item, "recorditem") + _children(item, "recordxml")
        root = _embedded(holders[0], state, nesting + 1) if len(holders) == 1 else None
        if root is not None:
            text, children = _record(root, title, state, nesting + 1)
        else:
            text = _join("[聊天记录] " + title, "[嵌套记录未展开]")
            state.issue("unsupported_nested_record")
            state.omitted = None
    else:
        kind = "unknown"
        text = _join("[不支持的转发条目]", title)
        state.issue("unsupported_forward_type")
        state.omitted = None
    if not text:
        text = "[空消息]"
        state.issue("missing_forward_content")
    # content is composed from already-budgeted fields; its separate final cap
    # prevents synthetic formatting/labels from bypassing the display budget.
    text, clipped = _clip(text, state.limits.max_output_bytes)
    if clipped:
        state.issue("output_limit", truncate=True)
    return ForwardItem(sender or None, timestamp or None, text, kind, children)


def _record(root: Element, title: str, state: _State, nesting: int) -> tuple[str, tuple[ForwardItem, ...]]:
    datalist = _record_container(root, state)
    if datalist is None:
        state.issue("unsupported_record_structure")
        state.omitted = None
        return _join("[聊天记录] " + title, "[记录内容未展开]"), ()
    record_title = state.text(_value(root, "title", state)) if root is not datalist else ""
    source = state.text(_value(root, "desc", state)) if root is not datalist else ""
    entries = _children(datalist, "dataitem")
    if len(entries) != len(datalist):
        state.issue("unsupported_record_child")
        state.omitted = None
    declared = datalist.attrib.get("count")
    if declared is not None and _integer(declared) != len(entries):
        state.issue("record_count_mismatch")
    lines = [_join("[聊天记录] " + title, record_title if record_title != title else None, source)]
    result: list[ForwardItem] = []
    for index, entry in enumerate(entries):
        if state.entries >= state.limits.max_entries:
            state.issue("entry_limit", truncate=True)
            state.omit(entries[index:])
            lines.append(f"[本层仅展开前{len(result)}条，省略{len(entries) - index}条]")
            break
        if state.retained_bytes >= state.limits.max_output_bytes:
            state.issue("output_limit", truncate=True)
            state.omit(entries[index:])
            lines.append(f"[本层仅展开前{len(result)}条，内容已截断]")
            break
        state.entries += 1
        projected = _item(entry, state, nesting)
        result.append(projected)
        label = " · ".join(value for value in (projected.sender, projected.timestamp) if value)
        lines.append(_join(f"{index + 1}. {label}".rstrip(), projected.content))
    return _join(*lines), tuple(result)


def _card(app: Element, state: _State, nesting: int, raw_type: int | None = None) -> dict:
    subtype = _integer(_value(app, "type", state))
    title = state.text(_value(app, "title", state))
    if raw_type is not None:
        if type(raw_type) is not int or not 0 <= raw_type <= 2**64 - 1:
            state.issue("invalid_raw_type_hint")
        elif raw_type >> 32 and subtype != raw_type >> 32:
            state.issue("type_conflict")
            state.omitted = None
            return {"canonical_text": _join("[卡片类型冲突，未展开]", title),
                    "display_kind": "unknown", "title": title}
    kind = _TYPE_KINDS.get(subtype, "unknown")
    if kind == "link":
        description = state.text(_value(app, "des", state))
        url = state.text(_value(app, "url", state)) or None
        if not (title or description or url):
            state.issue("missing_link_content")
        return {"canonical_text": _join("[链接] " + title, description, f"URL: {url}" if url else None),
                "display_kind": kind, "title": title, "description": description, "url": url}
    if kind == "file":
        name, size = _file_fields(app, title, state)
        if not name:
            state.issue("missing_file_name")
        return {"canonical_text": _join("[文件] " + (name or "未命名附件"),
                                       title if title != name else None,
                                       f"大小: {size} 字节" if size is not None else None),
                "display_kind": kind, "title": title, "file_name": name or None, "file_size": size}
    if kind == "quote":
        return _quote(app, title, state, nesting)
    if kind == "forward":
        holder = _child(app, "recorditem", state)
        root = _embedded(holder, state, nesting + 1) if holder is not None else None
        if root is None:
            state.issue("missing_or_invalid_record")
            state.omitted = None
            text, items = _join("[聊天记录] " + title, "[记录内容未展开]"), ()
        else:
            text, items = _record(root, title, state, nesting + 1)
        return {"canonical_text": text, "display_kind": kind, "title": title, "forward_items": items}
    state.issue("unsupported_appmsg_type", "unsupported" if nesting == 0 else "partial")
    state.omitted = None
    label = str(subtype) if subtype is not None else "未知"
    return {"canonical_text": _join(f"[不支持的卡片类型: {label}]", title),
            "display_kind": "unknown", "title": title}


def _cap_display(card: dict, state: _State) -> None:
    """Include composed/duplicated nested item text in the display-byte cap."""
    remaining = state.limits.max_output_bytes

    def take(value: str | None, *, atomic: bool = False) -> str | None:
        nonlocal remaining
        if value is None:
            return None
        text, clipped = _clip(value, remaining)
        if clipped:
            state.issue("output_limit", truncate=True)
            if atomic:
                text = ""  # Never publish a shortened, now-false reference ID.
        remaining -= len(text.encode("utf-8"))
        return text or None

    for name in ("reply_to", "title", "description", "url", "file_name", "quote_sender", "quote_summary"):
        if name in card:
            card[name] = take(card[name], atomic=name == "reply_to")
            if name in {"title", "description"} and card[name] is None:
                card[name] = ""

    def count(items: tuple[ForwardItem, ...]) -> int:
        return sum(1 + count(item.forward_items) for item in items)

    def items_within_budget(items: tuple[ForwardItem, ...]) -> tuple[ForwardItem, ...]:
        projected: list[ForwardItem] = []
        for index, item in enumerate(items):
            if remaining <= 0:
                state.issue("output_limit", truncate=True)
                if state.omitted is not None:
                    state.omitted += count(items[index:])
                break
            sender = take(item.sender)
            timestamp = take(item.timestamp)
            content = take(item.content) or ""
            nested = items_within_budget(item.forward_items)
            projected.append(ForwardItem(sender, timestamp, content, item.display_kind, nested))
        return tuple(projected)

    if "forward_items" in card:
        card["forward_items"] = items_within_budget(card["forward_items"])


def parse_appmsg(content: str | bytes, raw_type: int | None = None, *,
                 limits: AppMessageLimits | None = None) -> AppMessageProjection:
    """Parse one already-decoded/decompressed message without performing I/O.

    Content failures return visible bounded fallbacks, not exceptions or raw XML.
    Invalid API arguments (non-str/bytes input or invalid limits) raise TypeError
    or ValueError. XML hard failures remain explicit in parse_status and issues so
    callers cannot mistake an incomplete search projection for complete coverage.
    """
    if not isinstance(content, (str, bytes)):
        raise TypeError("content must be str or UTF-8 bytes")
    if limits is not None and not isinstance(limits, AppMessageLimits):
        raise TypeError("limits must be AppMessageLimits")
    state = _State(limits or AppMessageLimits())
    try:
        root = _parse_xml(content, state)
    except _XMLFailure as failure:
        _xml_failure(failure, state)
        text = {"rejected": "[消息XML包含禁止的DTD/实体声明，未展开]",
                "limit_exceeded": "[消息XML超过安全预算，未展开]"}.get(
                    failure.status, "[消息XML格式无效，未展开]")
        # Non-XML text can be useful as a fallback. Never echo malformed XML,
        # external references, or oversize input into output/logs.
        if (failure.status == "invalid" and isinstance(content, str)
                and not content.lstrip("﻿ \t\r\n").startswith("<")):
            text = _join(text, state.text(content))
        card = {"canonical_text": text, "display_kind": "unknown"}
    else:
        app = _appmsg(root, state)
        if app is None:
            state.issue("unsupported_root", "unsupported")
            state.omitted = None
            card = {"canonical_text": "[不支持的消息XML结构，未展开]", "display_kind": "unknown"}
        else:
            card = _card(app, state, 0, raw_type)
    _cap_display(card, state)
    # Reserve a visible notice only when clipping is actually necessary. This
    # final guard also includes all labels, separators and nested record output.
    text = card["canonical_text"]
    text, clipped = _clip(text, state.limits.max_output_bytes)
    if clipped or state.truncated:
        state.issue("output_limit" if clipped else "content_truncated", truncate=True)
        text, _ = _clip(text, state.limits.max_output_bytes - _NOTICE_BYTES)
        text += _TRUNCATED
    card["canonical_text"] = text
    return AppMessageProjection(**card, parse_status=state.status, truncated=state.truncated,
                                omitted_count=state.omitted, issues=tuple(state.issues))
