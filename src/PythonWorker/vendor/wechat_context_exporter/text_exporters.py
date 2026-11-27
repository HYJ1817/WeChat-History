# Vendored from wechat-ai-memory (MIT), commit 14d94db8b1affa12a2d7d88f0f830c1e0b8d752a.
# Copyright (c) 2026 WeChat AI Memory contributors. See LICENSES/wechat-ai-memory-LICENSE.txt.
# Local hardening: chat text, senders and titles are escaped before being placed
# in Markdown, so they stay literal text instead of active Markdown/HTML or
# remote image embeds. export_json is unchanged data output.
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from .models import Conversation, Message, MessageType

# CommonMark backslash escapes render the original character; escaping all
# punctuation that can start an active construct keeps message text inert.
_MARKDOWN_ESCAPE = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~<])")
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _escape_markdown(text: str) -> str:
    return _MARKDOWN_ESCAPE.sub(r"\\\1", text.replace("\x00", "�"))


def _escape_inline(text: str) -> str:
    return _escape_markdown(text.replace("\r\n", " ").replace("\r", " ").replace("\n", " "))


def _code_span(value: str) -> str:
    text = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    runs = re.findall(r"`+", text)
    fence = "`" * (max((len(run) for run in runs), default=0) + 1)
    if text.startswith(("`", " ")) or text.endswith(("`", " ")):
        text = f" {text} "
    return f"{fence}{text}{fence}"


def _local_image_target(content: str) -> str | None:
    """Return a quoted local path, or None for remote/UNC/URI image sources."""
    if content.startswith(("//", "\\\\")):
        return None
    if _URI_SCHEME.match(content) and not _WINDOWS_DRIVE.match(content):
        return None
    try:
        return quote(Path(content).as_posix(), safe="/:._-~")
    except (OSError, TypeError, ValueError):
        return None


def _image_line(content: str) -> str:
    target = _local_image_target(content)
    if target is None:
        # Preserve the source text without turning it into an active embed.
        return f"_[Image attachment] {_escape_markdown(content)}_"
    return f"![Image attachment]({target})"


def export_markdown(conversation: Conversation, messages: Iterable[Message], output_path: str | Path) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {_escape_inline(conversation.name)}",
        "",
        f"Conversation ID: {_code_span(conversation.id)}",
        "",
    ]
    current_date = None
    for message in messages:
        if current_date != message.timestamp.date():
            current_date = message.timestamp.date()
            lines.extend([f"## {current_date:%Y-%m-%d}", ""])
        heading = f"**{_escape_inline(message.sender)}** · {message.timestamp:%H:%M}"
        lines.extend([heading, ""])
        if message.type is MessageType.IMAGE:
            lines.append(_image_line(message.content))
        elif message.type is MessageType.FILE:
            lines.append(f"File attachment: {_code_span(message.content)}")
        elif message.type is MessageType.VOICE:
            duration = f" ({max(1, round(message.duration_ms / 1000))}s)" if message.duration_ms else ""
            lines.append(f"Voice transcript{duration}: {_escape_markdown(message.content)}")
        elif message.type is MessageType.SYSTEM:
            lines.append(f"_{_escape_markdown(message.content)}_")
        else:
            lines.append(_escape_markdown(message.content))
        lines.append("")
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def export_json(conversation: Conversation, messages: Iterable[Message], output_path: str | Path) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "conversations": [
            {
                "id": conversation.id,
                "name": conversation.name,
                "kind": conversation.kind.value,
                "messages": [
                    {
                        "id": message.id,
                        "sender": message.sender,
                        "timestamp": message.timestamp.isoformat(),
                        "type": message.type.value,
                        "content": message.content,
                        "is_outgoing": message.is_outgoing,
                        **({"reply_to": message.reply_to} if message.reply_to else {}),
                        **({"audio_path": str(message.audio_path)} if message.audio_path else {}),
                        **({"duration_ms": message.duration_ms} if message.duration_ms is not None else {}),
                        **({"transcript": message.transcript} if message.transcript else {}),
                    }
                    for message in messages
                ],
            }
        ],
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
