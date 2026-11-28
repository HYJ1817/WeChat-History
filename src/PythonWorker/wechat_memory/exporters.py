"""Structured Markdown/JSON/PDF export for a single conversation.

This module is a thin, UI-agnostic layer over the vendored
``wechat_context_exporter`` slice. It writes only the requested output (plus
optional asset/page files), never prints to stdout/stderr, and reports all
expected failures as :class:`ExportError` with a stable machine code and a
fixed Chinese message. Exception details stay in the chained cause, never in
the user-facing message.

The caller is responsible for protocol framing and for mapping
:class:`ExportError` to its own response format.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

# Allow direct imports when only src/PythonWorker is on sys.path (the packaged
# worker does the same in worker.py).
_WORKER_DIR = Path(__file__).resolve().parents[1]
_VENDOR_DIR = _WORKER_DIR / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

from PIL import Image  # noqa: E402

from wechat_context_exporter.models import Conversation, Message, MessageType  # noqa: E402
from wechat_context_exporter.pdf_exporter import PdfExporter  # noqa: E402
from wechat_context_exporter.rendering import (  # noqa: E402
    ChatRenderer,
    ImagePageRenderer,
    PageConfig,
)
from wechat_context_exporter.rendering.fonts import FontBook  # noqa: E402

_UTC8 = timezone(timedelta(hours=8))
_FORMAT_SUFFIXES = {"markdown": ".md", "json": ".json", "pdf": ".pdf"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# Files this exporter owns in its ancillary directories. Cleanup on overwrite
# only touches these names, so unrelated user files survive. Asset names match
# ``_AssetStore.add``; page names match ``_save_pdf_page``.
_ASSET_NAME_RE = re.compile(
    r"\d{4,}_[A-Za-z0-9._-]+\.(?:png|jpg|jpeg|gif|webp|bmp)", re.IGNORECASE
)
_PAGE_NAME_RE = re.compile(r"page_\d{4,}_(?:chat|image)\.png", re.IGNORECASE)

# Chat text is data, never Markdown/HTML. Escaping CommonMark punctuation keeps
# the text visible while disabling links, images, HTML, emphasis and block
# syntax. Only syntax this module emits itself (headings, the local image link,
# the fixed placeholders) stays active.
_MARKDOWN_ESCAPE = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~<])")

# Hard caps for the simple PDF path. Each rendered page is a 2480x3508 RGB
# canvas (~26 MiB of pixels); the exporter lays out first and draws one page at
# a time, but an unbounded history would still write an enormous number of
# page PNGs and PDF images. 20,000 messages is a cheap pre-render guard; 500
# pages is checked after layout and before any page is drawn. Both produce a
# fixed PAGE_LIMIT_EXCEEDED error instead of risking the process.
MAX_PDF_PAGES = 500
MAX_PDF_MESSAGES = 20_000

# Fixed, UI-safe messages. They must never contain chat content, sender names,
# file paths, keys, or exception text.
_MSG_INVALID = "导出参数无效。"
_MSG_OUTPUT_EXISTS = "输出文件已存在。请确认覆盖或选择其他位置。"
_MSG_NOT_WRITABLE = "输出位置不可写。"
_MSG_EXPORT_FAILED = "导出文件失败。"
_MSG_FONT_UNAVAILABLE = "系统中没有可用于 PDF 的中文字体。"
_MSG_PAGE_LIMIT = "所选范围的消息过多，无法生成 PDF。请缩小范围后重试。"

_IMAGE_MISSING = "图片不可读"
_VOICE_MISSING = "语音消息"

__all__ = ["ExportError", "ExportResult", "export_conversation"]


class ExportError(Exception):
    """An expected export failure with a stable, UI-safe code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class ExportResult:
    path: Path
    message_count: int
    bytes: int
    format: str
    page_count: int = 0
    chat_page_count: int = 0
    image_page_count: int = 0
    pages_dir: Path | None = None
    assets_dir: Path | None = None


def export_conversation(
    conversation: Conversation,
    messages: Sequence[Message],
    output_path: str | Path,
    fmt: str,
    *,
    overwrite: bool = False,
    pages_dir: str | Path | None = None,
    materialize_media: bool = True,
) -> ExportResult:
    """Export ``messages`` for ``conversation`` to ``output_path``.

    ``fmt`` is one of ``"markdown"``, ``"json"``, ``"pdf"`` and must match the
    output suffix (``.md``, ``.json``, ``.pdf``). The parent directory must
    already exist. Existing targets are only replaced when ``overwrite`` is
    true; writes go through a same-directory temporary file and ``os.replace``.

    Media handling: if ``materialize_media`` is true, readable image files are
    copied into ``<stem>_assets/`` for Markdown/JSON. Images that are missing,
    unreadable, or disabled become explicit placeholders. File and voice
    messages never become links; a voice transcript is used as text when it
    exists (unless media materialisation is disabled). Canonical card/forward
    content is already plain text: it is not parsed again or opened as a path.
    JSON v1 retains its content/reply_to, not a structured card or forward tree.
    """
    try:
        return _export(
            conversation,
            messages,
            output_path,
            fmt,
            overwrite=overwrite,
            pages_dir=pages_dir,
            materialize_media=materialize_media,
        )
    except ExportError:
        raise
    except Exception as exc:  # noqa: BLE001 - callers must get a structured error
        raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc


# --------------------------------------------------------------------------- #
# Argument validation and atomic output helpers
# --------------------------------------------------------------------------- #

def _as_path(value: object) -> Path | None:
    try:
        return Path(value).expanduser()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _validated_target(output_path: str | Path, fmt_key: str) -> Path:
    output = _as_path(output_path)
    if (
        output is None
        or not output.is_absolute()
        or "\0" in str(output)
    ):
        raise ExportError("INVALID_ARGUMENT", _MSG_INVALID)
    if output.suffix.lower() != _FORMAT_SUFFIXES[fmt_key]:
        raise ExportError("INVALID_ARGUMENT", _MSG_INVALID)
    if not output.parent.is_dir():
        raise ExportError("INVALID_ARGUMENT", _MSG_INVALID)
    return output


def _validated_pages_dir(pages_dir: str | Path | None, fmt_key: str) -> Path | None:
    # pages_dir only applies to PDF today; other formats ignore it.
    if fmt_key != "pdf" or pages_dir is None:
        return None
    target = _as_path(pages_dir)
    if target is None or not target.is_absolute() or "\0" in str(target):
        raise ExportError("INVALID_ARGUMENT", _MSG_INVALID)
    return target


def _temp_file_in(directory: Path, suffix: str) -> tuple[int, Path]:
    descriptor, name = tempfile.mkstemp(
        prefix=".wechat-memory-", suffix=suffix, dir=str(directory)
    )
    return descriptor, Path(name)


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _stage_text(output: Path, text: str) -> Path:
    """Write ``text`` to a same-directory temporary file, without replacing ``output``."""
    try:
        descriptor, temporary = _temp_file_in(output.parent, output.suffix + ".tmp")
    except OSError as exc:
        raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _discard(temporary)
        raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc
    return temporary


def _write_text_atomic(output: Path, text: str) -> None:
    temporary = _stage_text(output, text)
    try:
        os.replace(temporary, output)
    except OSError as exc:
        raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc
    finally:
        _discard(temporary)


def _escape_markdown(value: str) -> str:
    return _MARKDOWN_ESCAPE.sub(r"\\\1", value.replace("\x00", "�"))


def _markdown_inline(value: str) -> str:
    """Escape a value that must stay on one Markdown line (titles, senders)."""
    text = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return _escape_markdown(text)


def _markdown_code(value: str) -> str:
    """Render ``value`` as an inline code span even when it contains backticks."""
    text = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    runs = re.findall(r"`+", text)
    fence = "`" * (max((len(run) for run in runs), default=0) + 1)
    if text.startswith(("`", " ")) or text.endswith(("`", " ")):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def _markdown_destination(value: str) -> str:
    """Keep a generated relative asset path inside an angle-bracket destination."""
    return (
        value.replace("%", "%25")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
    )


def _copy_file_atomic(source: Path, target: Path) -> None:
    descriptor, temporary = _temp_file_in(target.parent, target.suffix + ".tmp")
    try:
        with os.fdopen(descriptor, "wb") as destination, source.open("rb") as origin:
            shutil.copyfileobj(origin, destination, length=1024 * 1024)
        os.replace(temporary, target)
    finally:
        _discard(temporary)


def _normalise_timestamp(value: datetime) -> datetime:
    """Render/export in UTC+08:00; naive values are assumed to already use it."""
    try:
        aware = value.tzinfo is not None and value.utcoffset() is not None
    except (OverflowError, ValueError):
        aware = False
    return value.astimezone(_UTC8) if aware else value.replace(tzinfo=_UTC8)


def _relative_posix(path: Path, start: Path) -> str:
    try:
        return Path(os.path.relpath(path, start)).as_posix()
    except ValueError:
        # Different drives: keep an absolute POSIX path; the caller chose it.
        return path.as_posix()


# --------------------------------------------------------------------------- #
# Media helpers
# --------------------------------------------------------------------------- #

def _safe_fragment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(". ")[:48]
    return cleaned or "image"


def _is_copyable_image(path: Path) -> bool:
    try:
        return path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES
    except (OSError, ValueError):
        return False


class _AncillaryCommit:
    """Installs staged files into an export-owned directory with rollback.

    Old files matching the caller's ownership pattern are moved to a hidden
    backup inside the target directory first. Unrelated files are never
    touched. If anything fails, installed files are removed and the backup is
    restored, so the previous export stays consistent.
    """

    def __init__(self, directory: Path, backup: Path | None, created: bool) -> None:
        self.directory = directory
        self.backup = backup
        self.created = created
        self.installed: list[Path] = []
        self._finished = False

    def rollback(self) -> None:
        if self._finished:
            return
        self._finished = True
        for target in reversed(self.installed):
            _discard(target)
        if self.backup is not None:
            # A failed restore must not delete the only remaining copy of an
            # old exported file; leave the hidden backup for manual recovery.
            restored = True
            try:
                for item in list(self.backup.iterdir()):
                    if item.is_file():
                        try:
                            os.replace(item, self.directory / item.name)
                        except OSError:
                            restored = False
            except OSError:
                restored = False
            if restored:
                shutil.rmtree(self.backup, ignore_errors=True)
        self._remove_if_empty()

    def finalize(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self.backup is not None:
            shutil.rmtree(self.backup, ignore_errors=True)
        self._remove_if_empty()

    def _remove_if_empty(self) -> None:
        try:
            self.directory.rmdir()
        except OSError:
            pass


def _commit_ancillary(
    directory: Path,
    staged: Sequence[Path],
    pattern: re.Pattern[str],
) -> _AncillaryCommit:
    """Commit ``staged`` files into ``directory``, replacing only owned names."""
    staged_files = list(staged)
    if not directory.exists() and not staged_files:
        return _AncillaryCommit(directory, None, False)
    created = not directory.exists()
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise OSError("ancillary target is not a directory")
    try:
        backup = Path(
            tempfile.mkdtemp(prefix=".wechat-memory-backup-", dir=str(directory))
        )
    except OSError:
        if created:
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    handle = _AncillaryCommit(directory, backup, created)
    try:
        for child in list(directory.iterdir()):
            if child == backup or not child.is_file():
                continue
            if pattern.fullmatch(child.name):
                os.replace(child, backup / child.name)
        for source in staged_files:
            target = directory / source.name
            if target.exists():
                os.replace(target, backup / target.name)
            _copy_file_atomic(source, target)
            handle.installed.append(target)
        return handle
    except Exception:  # noqa: BLE001 - every failure must restore the old export
        handle.rollback()
        raise


class _AssetStore:
    """Copies readable images into ``<stem>_assets`` with traversal-safe names.

    Files are written to a same-directory staging folder and installed only
    after the output text is ready, so a failed export never damages the
    previous export's assets.
    """

    def __init__(self, output: Path) -> None:
        self.directory = output.parent / f"{output.stem}_assets"
        self._staging: Path | None = None
        self._files: list[Path] = []
        self._next_index = 0
        self._stored = 0
        self._commit: _AncillaryCommit | None = None

    def _staging_directory(self) -> Path:
        if self._staging is None:
            self._staging = Path(
                tempfile.mkdtemp(
                    prefix=".wechat-memory-assets-", dir=str(self.directory.parent)
                )
            )
        return self._staging

    def add(self, source: Path, message_id: str) -> Path | None:
        if not _is_copyable_image(source):
            return None
        self._next_index += 1
        name = f"{self._next_index:04d}_{_safe_fragment(message_id)}{source.suffix.lower()}"
        target = self.directory / name
        try:
            staged = self._staging_directory() / name
            _copy_file_atomic(source, staged)
        except (OSError, ValueError):
            # Treat an unreadable/copy-failing source as "image unavailable";
            # the export itself can still succeed with a placeholder.
            self._next_index -= 1
            return None
        self._files.append(staged)
        self._stored += 1
        return target

    def commit(self) -> None:
        if self._commit is None:
            self._commit = _commit_ancillary(
                self.directory, self._files, _ASSET_NAME_RE
            )

    def finalize(self) -> None:
        if self._commit is not None:
            self._commit.finalize()
            self._commit = None
        self._discard_staging()

    def abort(self) -> None:
        if self._commit is not None:
            self._commit.rollback()
            self._commit = None
        self._discard_staging()

    def _discard_staging(self) -> None:
        if self._staging is not None:
            shutil.rmtree(self._staging, ignore_errors=True)
            self._staging = None

    @property
    def used_directory(self) -> Path | None:
        return self.directory if self._stored else None


def _image_source(message: Message) -> Path | None:
    try:
        return Path(message.content)
    except (TypeError, ValueError):
        return None


def _materialized_image(
    message: Message,
    assets: _AssetStore | None,
    output: Path,
) -> str | None:
    if assets is None:
        return None
    source = _image_source(message)
    if source is None:
        return None
    target = assets.add(source, message.id)
    if target is None:
        return None
    return _relative_posix(target, output.parent)


def _image_is_renderable(content: str) -> bool:
    """Pillow validates images for the PDF path; invalid files become notes."""
    try:
        path = Path(content)
    except (TypeError, ValueError):
        return False
    try:
        if not path.is_file():
            return False
        with Image.open(path) as opened:
            opened.verify()
        return True
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return False


def _system_message(source: Message, timestamp: datetime, content: str) -> Message:
    return replace(
        source,
        timestamp=timestamp,
        type=MessageType.SYSTEM,
        content=content,
        audio_path=None,
        duration_ms=None,
        transcript=None,
    )


def _voice_or_placeholder(
    source: Message,
    materialize_media: bool,
) -> tuple[MessageType, str, int | None, str | None]:
    """Return ``(type, content, duration_ms, transcript)`` for a voice message."""
    if materialize_media and source.transcript:
        return MessageType.VOICE, source.transcript, source.duration_ms, source.transcript
    return MessageType.SYSTEM, _VOICE_MISSING, None, None


# --------------------------------------------------------------------------- #
# Shared FILE text convention
# --------------------------------------------------------------------------- #

# FILE also carries appmsg_parser's canonical text, without a new vendor model
# or a card tree. Multiline bodies and the parser's single-line labels are text,
# not attachment paths. This is only an output convention: never reparse XML or
# probe a purported filename to decide how to display it.
_APPMSG_TEXT_PREFIXES = ("[链接] ", "[文件] ", "[聊天记录] ", "[不支持的卡片类型: ")
_APPMSG_TEXT_NOTICES = frozenset({
    "[卡片类型冲突，未展开]",
    "[消息XML包含禁止的DTD/实体声明，未展开]",
    "[消息XML超过安全预算，未展开]",
    "[消息XML格式无效，未展开]",
    "[不支持的消息XML结构，未展开]",
})


def _file_is_text(message: Message) -> bool:
    return (
        "\n" in message.content
        or "\r" in message.content
        or message.content.startswith(_APPMSG_TEXT_PREFIXES)
        or message.content in _APPMSG_TEXT_NOTICES
    )


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #

def _markdown_file_content(message: Message) -> str:
    if _file_is_text(message):
        # Hard breaks retain the canonical line boundaries in rendered Markdown
        # too. Escape *before* adding our own syntax; URLs/HTML stay literal.
        text = message.content.replace("\r\n", "\n").replace("\r", "\n")
        return _escape_markdown(text).replace("\n", "  \n")
    # Normal one-line FILE names/paths keep the historical attachment display.
    return f"_[文件] {_markdown_inline(message.content)}_"


def _markdown_text(
    conversation: Conversation,
    messages: Sequence[Message],
    assets: _AssetStore | None,
    output: Path,
    materialize_media: bool,
) -> str:
    lines = [
        f"# {_markdown_inline(conversation.name)}",
        "",
        f"Conversation ID: {_markdown_code(conversation.id)}",
        "",
    ]
    current_date = None
    for message in messages:
        timestamp = _normalise_timestamp(message.timestamp)
        if current_date != timestamp.date():
            current_date = timestamp.date()
            lines.extend([f"## {current_date:%Y-%m-%d}", ""])
        lines.extend(
            [f"**{_markdown_inline(message.sender)}** · {timestamp:%H:%M}", ""]
        )
        if message.type is MessageType.IMAGE:
            relative = (
                _materialized_image(message, assets, output) if materialize_media else None
            )
            if relative is None:
                lines.append(f"_[{_IMAGE_MISSING}]_")
            else:
                # Only this generated link is deliberately active Markdown.
                lines.append(
                    f"![图片](<{_markdown_destination(relative)}>)"
                )
        elif message.type is MessageType.FILE:
            lines.append(_markdown_file_content(message))
        elif message.type is MessageType.VOICE:
            if materialize_media and message.transcript:
                lines.append(_escape_markdown(message.transcript))
            else:
                lines.append(f"_[{_VOICE_MISSING}]_")
        elif message.type is MessageType.SYSTEM:
            lines.append(f"_{_escape_markdown(message.content)}_")
        else:
            lines.append(_escape_markdown(message.content))
        lines.append("")
    if not messages:
        lines.extend(["_所选范围内没有消息_", ""])
    return "\n".join(lines)


def _export_markdown(
    conversation: Conversation,
    messages: Sequence[Message],
    output: Path,
    materialize_media: bool,
) -> ExportResult:
    assets = _AssetStore(output)
    temporary: Path | None = None
    try:
        text = _markdown_text(
            conversation, messages, assets, output, materialize_media
        )
        temporary = _stage_text(output, text)
        try:
            assets.commit()
        except OSError as exc:
            raise ExportError("OUTPUT_NOT_WRITABLE", _MSG_NOT_WRITABLE) from exc
        try:
            os.replace(temporary, output)
        except OSError as exc:
            raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc
        temporary = None
        assets.finalize()
    except Exception:  # noqa: BLE001 - roll back staged assets, then re-raise
        assets.abort()
        raise
    finally:
        if temporary is not None:
            _discard(temporary)
    return _make_result(
        output,
        len(messages),
        "markdown",
        assets_dir=assets.used_directory,
    )


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #

def _json_message(
    message: Message,
    assets: _AssetStore | None,
    output: Path,
    materialize_media: bool,
) -> dict[str, object]:
    timestamp = _normalise_timestamp(message.timestamp)
    kind = message.type
    content = message.content
    duration_ms: int | None = None
    transcript: str | None = None

    if kind is MessageType.IMAGE:
        relative = (
            _materialized_image(message, assets, output) if materialize_media else None
        )
        if relative is None:
            kind = MessageType.SYSTEM
            content = _IMAGE_MISSING
        else:
            kind = MessageType.IMAGE
            content = relative
    elif kind is MessageType.FILE:
        # v1 archives preserve canonical content/reply_to as plain text, not a
        # new display tree. Ordinary attachments retain their safe placeholder.
        if _file_is_text(message):
            kind = MessageType.TEXT
        else:
            kind = MessageType.SYSTEM
            content = f"[文件] {message.content}"
    elif kind is MessageType.VOICE:
        kind, content, duration_ms, transcript = _voice_or_placeholder(
            message, materialize_media
        )
    elif kind is MessageType.SYSTEM:
        kind = MessageType.SYSTEM
    else:
        kind = MessageType.TEXT

    entry: dict[str, object] = {
        "id": message.id,
        "sender": message.sender,
        "timestamp": timestamp.isoformat(),
        "type": kind.value,
        "content": content,
        "is_outgoing": message.is_outgoing,
    }
    if message.reply_to:
        entry["reply_to"] = message.reply_to
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    if transcript:
        entry["transcript"] = transcript
    return entry


def _json_text(
    conversation: Conversation,
    messages: Sequence[Message],
    assets: _AssetStore | None,
    output: Path,
    materialize_media: bool,
) -> str:
    payload = {
        "version": 1,
        "conversations": [
            {
                "id": conversation.id,
                "name": conversation.name,
                "kind": conversation.kind.value,
                "messages": [
                    _json_message(message, assets, output, materialize_media)
                    for message in messages
                ],
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _export_json(
    conversation: Conversation,
    messages: Sequence[Message],
    output: Path,
    materialize_media: bool,
) -> ExportResult:
    assets = _AssetStore(output)
    temporary: Path | None = None
    try:
        text = _json_text(conversation, messages, assets, output, materialize_media)
        temporary = _stage_text(output, text)
        try:
            assets.commit()
        except OSError as exc:
            raise ExportError("OUTPUT_NOT_WRITABLE", _MSG_NOT_WRITABLE) from exc
        try:
            os.replace(temporary, output)
        except OSError as exc:
            raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc
        temporary = None
        assets.finalize()
    except Exception:  # noqa: BLE001 - roll back staged assets, then re-raise
        assets.abort()
        raise
    finally:
        if temporary is not None:
            _discard(temporary)
    return _make_result(
        output,
        len(messages),
        "json",
        assets_dir=assets.used_directory,
    )


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

def _sanitize_for_pdf(
    messages: Sequence[Message], materialize_media: bool
) -> list[Message]:
    sanitized: list[Message] = []
    for message in messages:
        timestamp = _normalise_timestamp(message.timestamp)
        if message.type is MessageType.IMAGE:
            if materialize_media and _image_is_renderable(message.content):
                sanitized.append(
                    replace(message, timestamp=timestamp, audio_path=None, transcript=None)
                )
            else:
                sanitized.append(_system_message(message, timestamp, _IMAGE_MISSING))
        elif message.type is MessageType.FILE:
            if _file_is_text(message):
                # Vendor FILE rendering takes Path(content).name. Canonical
                # cards/forwards are literal body text, so use a text bubble
                # (also retaining the sender and optional reply relationship).
                # PDF draws text into pixels; Markdown/HTML escapes do not
                # belong here and would change the visible message.
                sanitized.append(replace(
                    message, timestamp=timestamp, type=MessageType.TEXT,
                    audio_path=None, duration_ms=None, transcript=None,
                ))
            else:
                sanitized.append(
                    _system_message(message, timestamp, f"[文件] {message.content}")
                )
        elif message.type is MessageType.VOICE:
            if materialize_media and message.transcript:
                # The PDF renderer draws voice bubbles with a fixed label
                # around ``content``; a plain text bubble preserves the
                # transcript without implying that the audio file exists.
                sanitized.append(
                    replace(
                        message,
                        timestamp=timestamp,
                        type=MessageType.TEXT,
                        content=message.transcript,
                        audio_path=None,
                        duration_ms=None,
                        transcript=None,
                    )
                )
            else:
                sanitized.append(
                    _system_message(message, timestamp, _VOICE_MISSING)
                )
        else:
            sanitized.append(replace(message, timestamp=timestamp, audio_path=None))
    return sanitized


def _empty_pdf_message(conversation: Conversation) -> Message:
    return Message(
        id="__empty__",
        conversation_id=conversation.id,
        sender="系统",
        timestamp=datetime.now(tz=_UTC8),
        type=MessageType.SYSTEM,
        content="所选范围内没有消息",
    )


def _worker_temp_root() -> Path:
    """Return the worker-private temporary root.

    The worker supplies ``wechat_memory.private_temp``; the fallback keeps the
    same application-private base for standalone/library use instead of falling
    back to the shared ``%TEMP%``.
    """
    try:
        import importlib

        private_temp = importlib.import_module("wechat_memory.private_temp")
    except ImportError:
        local = Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        )
        root = local / "WechatMemoryNative" / "worker-temp"
    else:
        root = Path(private_temp.get_worker_temp_root())
    if not root.is_absolute():
        raise ValueError("worker temp root must be absolute")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _create_pdf_temp_dir() -> Path:
    return Path(
        tempfile.mkdtemp(prefix="wechat-memory-pdf-", dir=str(_worker_temp_root()))
    )


def _save_pdf_page(
    directory: Path, page_image: Image.Image, suffix: str, number: int
) -> Path:
    page_path = directory / f"page_{number:04d}_{suffix}.png"
    page_image.save(page_path, format="PNG")
    return page_path


def _export_pdf(
    conversation: Conversation,
    messages: Sequence[Message],
    output: Path,
    pages_target: Path | None,
    materialize_media: bool,
) -> ExportResult:
    if len(messages) > MAX_PDF_MESSAGES:
        raise ExportError("PAGE_LIMIT_EXCEEDED", _MSG_PAGE_LIMIT)

    pages_created = False
    if pages_target is not None:
        try:
            if pages_target.exists():
                if not pages_target.is_dir():
                    raise ExportError("OUTPUT_NOT_WRITABLE", _MSG_NOT_WRITABLE)
            else:
                pages_target.mkdir(parents=True, exist_ok=True)
                pages_created = True
        except OSError as exc:
            raise ExportError("OUTPUT_NOT_WRITABLE", _MSG_NOT_WRITABLE) from exc

    pages_commit: _AncillaryCommit | None = None
    temporary_pdf: Path | None = None
    temporary_dir: Path | None = None
    try:
        try:
            fonts = FontBook.discover()
        except RuntimeError as exc:
            raise ExportError("FONT_UNAVAILABLE", _MSG_FONT_UNAVAILABLE) from exc

        config = PageConfig()
        renderer = ChatRenderer(config=config, fonts=fonts)
        render_messages: Sequence[Message] = _sanitize_for_pdf(
            messages, materialize_media
        )
        if not render_messages:
            render_messages = [_empty_pdf_message(conversation)]

        # Layout first, then enforce the cap. No full-resolution page exists
        # yet, so an oversized history costs no page-sized RGB buffers.
        planned_pages = renderer.layout(render_messages)

        # Upstream renders one high-resolution attachment page per image,
        # placed directly after the chat page that shows its thumbnail. Only
        # images that survived ``_sanitize_for_pdf`` (i.e. are readable) count
        # towards the cap.
        image_messages = [
            message
            for message in render_messages
            if message.type is MessageType.IMAGE and message.image_path is not None
        ]
        image_positions = {
            message.id: index + 1 for index, message in enumerate(image_messages)
        }
        expected_pages = len(planned_pages) + (
            len(image_messages) if materialize_media else 0
        )
        if expected_pages > MAX_PDF_PAGES:
            raise ExportError("PAGE_LIMIT_EXCEEDED", _MSG_PAGE_LIMIT)

        try:
            temporary_dir = _create_pdf_temp_dir()
        except (OSError, ValueError) as exc:
            raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc

        page_paths: list[Path] = []
        image_count = 0
        image_renderer = (
            ImagePageRenderer(config=config, fonts=fonts)
            if materialize_media and image_messages
            else None
        )
        message_by_id = {message.id: message for message in render_messages}
        total_pages = len(planned_pages)

        # Draw, persist and release one page at a time. The PDF is assembled
        # from the temporary PNGs after this loop.
        for page_number, page_blocks in enumerate(planned_pages, start=1):
            rendered = renderer.render_page(
                conversation, page_blocks, page_number, total_pages
            )
            image = rendered.image
            try:
                if image.size != config.pixel_size:
                    resized = image.resize(
                        config.pixel_size, Image.Resampling.LANCZOS
                    )
                    image.close()
                    image = resized
                if image.mode != "RGB":
                    converted = image.convert("RGB")
                    image.close()
                    image = converted
                page_paths.append(
                    _save_pdf_page(
                        temporary_dir, image, "chat", len(page_paths) + 1
                    )
                )
            finally:
                image.close()

            if image_renderer is None:
                continue
            for message_id in rendered.image_message_ids:
                message = message_by_id.get(message_id)
                if message is None or message.type is not MessageType.IMAGE:
                    continue
                attachment = image_renderer.render(
                    conversation,
                    message,
                    image_positions[message_id],
                    len(image_messages),
                )
                try:
                    page_paths.append(
                        _save_pdf_page(
                            temporary_dir,
                            attachment,
                            "image",
                            len(page_paths) + 1,
                        )
                    )
                finally:
                    attachment.close()
                image_count += 1

        descriptor, pdf_name = _temp_file_in(output.parent, ".pdf.tmp")
        os.close(descriptor)
        temporary_pdf = Path(pdf_name)
        try:
            PdfExporter().export(page_paths, temporary_pdf, conversation.name)
        except OSError as exc:
            raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc

        if pages_target is not None:
            try:
                pages_commit = _commit_ancillary(
                    pages_target, page_paths, _PAGE_NAME_RE
                )
            except OSError as exc:
                raise ExportError("OUTPUT_NOT_WRITABLE", _MSG_NOT_WRITABLE) from exc

        try:
            os.replace(temporary_pdf, output)
        except OSError as exc:
            raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc
        temporary_pdf = None
        if pages_commit is not None:
            pages_commit.finalize()
            pages_commit = None

        return _make_result(
            output,
            len(messages),
            "pdf",
            page_count=len(page_paths),
            chat_page_count=total_pages,
            image_page_count=image_count,
            pages_dir=pages_target,
        )
    finally:
        if pages_commit is not None:
            pages_commit.rollback()
        if temporary_pdf is not None:
            _discard(temporary_pdf)
        if temporary_dir is not None:
            shutil.rmtree(temporary_dir, ignore_errors=True)
        if pages_created and pages_target is not None:
            try:
                pages_target.rmdir()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

def _make_result(
    output: Path,
    message_count: int,
    fmt_key: str,
    *,
    page_count: int = 0,
    chat_page_count: int = 0,
    image_page_count: int = 0,
    pages_dir: Path | None = None,
    assets_dir: Path | None = None,
) -> ExportResult:
    try:
        size = output.stat().st_size
    except OSError as exc:
        raise ExportError("EXPORT_FAILED", _MSG_EXPORT_FAILED) from exc
    return ExportResult(
        path=output,
        message_count=message_count,
        bytes=size,
        format=fmt_key,
        page_count=page_count,
        chat_page_count=chat_page_count,
        image_page_count=image_page_count,
        pages_dir=pages_dir,
        assets_dir=assets_dir,
    )


def _export(
    conversation: Conversation,
    messages: Sequence[Message],
    output_path: str | Path,
    fmt: str,
    *,
    overwrite: bool,
    pages_dir: str | Path | None,
    materialize_media: bool,
) -> ExportResult:
    if not isinstance(fmt, str):
        raise ExportError("INVALID_ARGUMENT", _MSG_INVALID)
    fmt_key = fmt.strip().lower()
    if fmt_key not in _FORMAT_SUFFIXES:
        raise ExportError("INVALID_ARGUMENT", _MSG_INVALID)

    output = _validated_target(output_path, fmt_key)
    pages_target = _validated_pages_dir(pages_dir, fmt_key)

    if output.exists() and not overwrite:
        raise ExportError("OUTPUT_EXISTS", _MSG_OUTPUT_EXISTS)
    if output.is_dir():
        raise ExportError("INVALID_ARGUMENT", _MSG_INVALID)

    items = list(messages)
    if fmt_key == "markdown":
        return _export_markdown(conversation, items, output, bool(materialize_media))
    if fmt_key == "json":
        return _export_json(conversation, items, output, bool(materialize_media))
    return _export_pdf(
        conversation,
        items,
        output,
        pages_target,
        bool(materialize_media),
    )
