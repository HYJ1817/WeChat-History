"""Tests for the structured Markdown/JSON/PDF exporter.

Run from the project root:
  .venv\\Scripts\\python.exe -I -X utf8 -m unittest discover -s tests/PythonWorker.Tests -v
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
VENDOR_DIR = WORKER_DIR / "vendor"
sys.path.insert(0, str(VENDOR_DIR))
sys.path.insert(0, str(WORKER_DIR))

from wechat_context_exporter.models import (  # noqa: E402
    Conversation,
    ConversationKind,
    Message,
    MessageType,
)
from wechat_context_exporter.rendering.fonts import FontBook  # noqa: E402
from wechat_memory import exporters  # noqa: E402
from wechat_memory.appmsg_parser import parse_appmsg  # noqa: E402
from wechat_memory.exporters import ExportError, export_conversation  # noqa: E402
from wechat_memory.offline_import import load_json_file  # noqa: E402

UTC8 = timezone(timedelta(hours=8))
UTC = timezone.utc


def _fonts_available() -> bool:
    try:
        FontBook.discover()
        return True
    except RuntimeError:
        return False


FONT_AVAILABLE = _fonts_available()


def make_conversation(
    name: str = "测试会话",
    conversation_id: str = "conv-1",
    kind: ConversationKind = ConversationKind.DIRECT,
) -> Conversation:
    return Conversation(id=conversation_id, name=name, kind=kind)


def make_message(
    message_id: str,
    kind: MessageType,
    content: str,
    *,
    sender: str = "张三",
    timestamp: datetime | None = None,
    conversation_id: str = "conv-1",
    **extra: object,
) -> Message:
    return Message(
        id=message_id,
        conversation_id=conversation_id,
        sender=sender,
        timestamp=timestamp or datetime(2026, 11, 24, 9, 0, tzinfo=UTC8),
        type=kind,
        content=content,
        **extra,  # type: ignore[arg-type]
    )


def make_png(path: Path, size: tuple[int, int] = (24, 18)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (20, 120, 200)).save(path)
    return path


def unescape_markdown(text: str) -> str:
    """Undo the exporter's CommonMark backslash escaping to compare visible text."""
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~<])", r"\1", text)


def make_appmsg_messages() -> list[Message]:
    """Project synthetic cards at the same FILE-model seam as the live source."""
    def field(name: str, text: str) -> str:
        return f"<{name}>{escape(text)}</{name}>"

    def app(kind: int, title: str, extra: str = "") -> str:
        return f"<appmsg><type>{kind}</type>{field('title', title)}{extra}</appmsg>"

    quoted = "前" * 90 + "保留引文尾部 / 引用路径"
    item = (
        '<dataitem datatype="1"><dataitemsource><sourcename>内部发送者</sourcename>'
        '<sourcetime>2026-11-28 09:01</sourcetime></dataitemsource>'
        + field("datadesc", "重复条目\n![x](https://a.invalid/x)\n<script>literal</script>")
        + "</dataitem>"
    )
    cards = {
        "link": app(5, "**链接标题**", field("des", "第一行\n\n# 不是标题")
                    + field("url", "https://a.invalid/path?a=1&b=2")),
        "file-card": app(6, "附件标题", "<appattach>"
                         + field("filename", r"C:\synthetic\报告`[1].pdf")
                         + "<totallen>42</totallen></appattach>"),
        "one-line-file-card": app(6, "只有文件名.pdf"),
        "one-line-link": app(5, "只有链接标题"),
        "quote": app(57, "当前回复", "<refermsg><type>1</type><svrid>123456789</svrid>"
                     "<displayname>被引发送者</displayname>" + field("content", quoted)
                     + "</refermsg>"),
        "forward": app(19, "合并转发", "<recorditem><recordinfo><title>记录标题</title>"
                       '<datalist count="2">' + item * 2
                       + "</datalist></recordinfo></recorditem>"),
        "unsupported": app(999, "未知卡片 / 保留标题"),
        "untitled-unsupported": app(999, ""),
        "invalid": "<appmsg>",
        "unsupported-root": "<unrelated/>",
        "rejected": '<!DOCTYPE appmsg SYSTEM "file:///do-not-open"><appmsg/>',
    }
    messages = []
    for message_id, xml in cards.items():
        projection = parse_appmsg(xml)
        messages.append(make_message(
            message_id, MessageType.FILE, projection.canonical_text,
            reply_to=projection.reply_to, is_outgoing=message_id == "quote",
        ))
    return messages


class ExportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="wechat-export-test-")
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name)

    def assert_export_error(self, code: str, callable_, message: str | None = None):
        with self.assertRaises(ExportError) as raised:
            callable_()
        error = raised.exception
        self.assertEqual(error.code, code)
        self.assertTrue(error.message)
        if message is not None:
            self.assertEqual(error.message, message)
        self.assertNotIn(str(self.folder), error.message)
        return error


class MarkdownTests(ExportTestCase):
    def test_text_export_has_title_sender_content_and_utf8(self):
        conversation = make_conversation("测试会话 · 产品讨论", kind=ConversationKind.GROUP)
        messages = [
            make_message(
                "m1",
                MessageType.TEXT,
                "你好，世界！\n第二行原样保留 <b>与符号 &",
                timestamp=datetime(2026, 11, 24, 1, 30, tzinfo=UTC),
            ),
            make_message(
                "m2",
                MessageType.SYSTEM,
                "系统提示：仅供测试",
                sender="系统",
                timestamp=datetime(2026, 11, 25, 3, 4, tzinfo=UTC8),
            ),
        ]
        output = self.folder / "聊天 记录.md"
        result = export_conversation(conversation, messages, output, "markdown")

        text = output.read_text(encoding="utf-8")
        self.assertIn("# 测试会话 · 产品讨论", text)
        self.assertIn("Conversation ID: `conv-1`", text)
        self.assertIn("## 2026-11-24", text)
        self.assertIn("## 2026-11-25", text)
        self.assertIn("**张三** · 09:30", text)
        self.assertIn(
            "你好，世界！\n第二行原样保留 <b>与符号 &", unescape_markdown(text)
        )
        # Raw HTML must not survive as active markup; the visible text remains.
        self.assertNotIn("<b>", text)
        self.assertIn("\\<b\\>", text)
        self.assertIn("_系统提示：仅供测试_", text)

        self.assertEqual(result.path, output)
        self.assertEqual(result.message_count, 2)
        self.assertEqual(result.bytes, output.stat().st_size)
        self.assertEqual(result.format, "markdown")
        self.assertEqual(result.page_count, 0)
        self.assertEqual(result.chat_page_count, 0)
        self.assertEqual(result.image_page_count, 0)
        self.assertIsNone(result.pages_dir)
        self.assertIsNone(result.assets_dir)

    def test_empty_markdown_is_valid_and_has_notice(self):
        output = self.folder / "empty.md"
        result = export_conversation(make_conversation(), [], output, "markdown")
        text = output.read_text(encoding="utf-8")
        self.assertIn("# 测试会话", text)
        self.assertIn("Conversation ID: `conv-1`", text)
        self.assertIn("所选范围内没有消息", text)
        self.assertEqual(result.message_count, 0)
        self.assertGreater(result.bytes, 0)


class JsonTests(ExportTestCase):
    def test_json_structure_fields_and_timezone(self):
        conversation = make_conversation("测试群", kind=ConversationKind.GROUP)
        messages = [
            make_message(
                "m1",
                MessageType.TEXT,
                "第一行\n第二行",
                timestamp=datetime(2026, 11, 24, 1, 30, tzinfo=UTC),
            ),
            make_message(
                "m2",
                MessageType.SYSTEM,
                "系统消息",
                sender="系统",
                timestamp=datetime(2026, 11, 25, 20, 0, tzinfo=UTC),
            ),
            make_message(
                "m3",
                MessageType.TEXT,
                "回复内容",
                is_outgoing=True,
                reply_to="m1",
                timestamp=datetime(2026, 11, 26, 7, 8),  # naive -> UTC+08
            ),
        ]
        output = self.folder / "导出.json"
        result = export_conversation(conversation, messages, output, "json")

        raw = output.read_text(encoding="utf-8")
        self.assertIn("测试群", raw)  # ensure_ascii=False
        payload = json.loads(raw)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["conversations"]), 1)
        exported = payload["conversations"][0]
        self.assertEqual(exported["id"], "conv-1")
        self.assertEqual(exported["name"], "测试群")
        self.assertEqual(exported["kind"], "group")

        first, second, third = exported["messages"]
        self.assertEqual(first["id"], "m1")
        self.assertEqual(first["sender"], "张三")
        self.assertEqual(first["timestamp"], "2026-11-24T09:30:00+08:00")
        self.assertEqual(first["type"], "text")
        self.assertEqual(first["content"], "第一行\n第二行")
        self.assertIs(first["is_outgoing"], False)
        self.assertNotIn("audio_path", first)
        self.assertNotIn("transcript", first)

        self.assertEqual(second["type"], "system")
        self.assertEqual(second["content"], "系统消息")
        self.assertEqual(second["timestamp"], "2026-11-26T04:00:00+08:00")

        self.assertEqual(third["timestamp"], "2026-11-26T07:08:00+08:00")
        self.assertEqual(third["reply_to"], "m1")
        self.assertIs(third["is_outgoing"], True)

        self.assertEqual(result.message_count, 3)
        self.assertEqual(result.format, "json")
        self.assertEqual(result.bytes, output.stat().st_size)
        self.assertIsNone(result.assets_dir)

    def test_empty_json_is_valid(self):
        output = self.folder / "empty.json"
        result = export_conversation(make_conversation(), [], output, "json")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["conversations"][0]["messages"], [])
        self.assertEqual(result.message_count, 0)


class AppMessageExportTests(ExportTestCase):
    def test_markdown_preserves_canonical_lines_as_escaped_text_with_media_on_or_off(self):
        messages = make_appmsg_messages()
        messages.append(make_message(
            "multiline-file", MessageType.FILE,
            "普通 FILE 正文\r\n第二行\r第三行\n\n[链接](javascript:never)\n<https://a.invalid/>",
        ))
        for media in (False, True):
            with self.subTest(materialize_media=media):
                output = self.folder / f"cards-{media}.md"
                result = export_conversation(
                    make_conversation(), messages, output, "markdown", materialize_media=media,
                )
                raw = output.read_text(encoding="utf-8")
                visible = unescape_markdown(raw).replace("  \n", "\n")
                for message in messages:
                    expected = message.content.replace("\r\n", "\n").replace("\r", "\n")
                    self.assertIn(expected, visible, message.id)
                # The parser's label is not wrapped in another "file" label;
                # each newline is a visible Markdown hard break, not a soft one.
                self.assertIn("第一行  \n  \n\\# 不是标题", raw)
                self.assertNotIn("_[文件]", raw)
                self.assertNotRegex(raw, r"(?<!\\)<(?:script|https)")
                self.assertNotIn("](https", raw)
                self.assertNotIn("](javascript", raw)
                self.assertNotIn("\n# 不是标题", raw)
                self.assertEqual(visible.count("重复条目"), 2)
                self.assertEqual(result.message_count, len(messages))
                self.assertIsNone(result.assets_dir)

    def test_json_v1_round_trips_canonical_content_and_reply_without_a_card_tree(self):
        messages = make_appmsg_messages()
        output = self.folder / "cards.json"
        export_conversation(make_conversation(), messages, output, "json")
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        exported = payload["conversations"][0]["messages"]
        for original, entry in zip(messages, exported):
            with self.subTest(message_id=original.id):
                self.assertEqual(entry["type"], "text")
                self.assertEqual(entry["content"], original.content)
                self.assertEqual(entry.get("reply_to"), original.reply_to)
                expected_keys = {"id", "sender", "timestamp", "type", "content", "is_outgoing"}
                if original.reply_to:
                    expected_keys.add("reply_to")
                self.assertEqual(set(entry), expected_keys)
        restored = load_json_file(output).conversations[0]
        rebuilt = [make_message(
            item.id, MessageType(item.kind), item.content, sender=item.sender,
            timestamp=item.timestamp, reply_to=item.reply_to, is_outgoing=item.is_outgoing,
        ) for item in restored.messages]
        self.assertEqual(
            [(item.content, item.reply_to) for item in rebuilt],
            [(item.content, item.reply_to) for item in messages],
        )
        second = self.folder / "cards-again.json"
        export_conversation(make_conversation(), rebuilt, second, "json", materialize_media=False)
        self.assertEqual(json.loads(second.read_text(encoding="utf-8")), payload)

    def test_pdf_sanitizer_bypasses_file_basename_and_keeps_literal_canonical_text(self):
        messages = make_appmsg_messages()
        for media in (False, True):
            sanitized = exporters._sanitize_for_pdf(messages, media)
            for original, rendered in zip(messages, sanitized):
                with self.subTest(message_id=original.id, materialize_media=media):
                    self.assertEqual(rendered.type, MessageType.TEXT)
                    self.assertEqual(rendered.content, original.content)
                    self.assertEqual(rendered.reply_to, original.reply_to)
                    # In particular, a URL or slash in the canonical body must
                    # not reach the vendor FILE -> Path(content).name branch.
                    with mock.patch(
                        "wechat_context_exporter.rendering.chat.Path",
                        side_effect=AssertionError("canonical content is not a path"),
                    ):
                        self.assertEqual(
                            exporters.ChatRenderer._display_content(rendered), original.content,
                        )
                    self.assertIsNone(rendered.audio_path)

    def test_ordinary_file_keeps_legacy_label_and_never_materializes_an_attachment(self):
        source = self.folder / "report`[1].pdf"
        source.write_bytes(b"synthetic attachment; do not copy")
        contents = [str(source), source.name, "[链接].pdf", "[消息XML格式无效，未展开].pdf"]
        messages = [make_message(f"ordinary-{i}", MessageType.FILE, content)
                    for i, content in enumerate(contents)]
        expected = [f"[文件] {content}" for content in contents]
        output = self.folder / "ordinary.md"
        markdown = export_conversation(make_conversation(), messages, output, "markdown")
        raw = output.read_text(encoding="utf-8")
        for content in expected:
            self.assertIn(f"_{content}_", unescape_markdown(raw))
        self.assertNotIn("](", raw)
        self.assertIsNone(markdown.assets_dir)
        output = self.folder / "ordinary.json"
        result = export_conversation(make_conversation(), messages, output, "json")
        exported = json.loads(output.read_text(encoding="utf-8"))["conversations"][0]["messages"]
        self.assertEqual([(item["type"], item["content"]) for item in exported],
                         [("system", content) for content in expected])
        self.assertIsNone(result.assets_dir)
        for media in (False, True):
            sanitized = exporters._sanitize_for_pdf(messages, media)
            self.assertEqual([(item.type, item.content) for item in sanitized],
                             [(MessageType.SYSTEM, content) for content in expected])
            self.assertEqual([exporters.ChatRenderer._display_content(item) for item in sanitized], expected)
        self.assertEqual(source.read_bytes(), b"synthetic attachment; do not copy")

    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_pdf_draws_all_canonical_text_and_long_file_continuations_without_path_parsing(self):
        messages = make_appmsg_messages()
        messages.append(make_message(
            "continuation", MessageType.FILE,
            "\n".join(f"第{i:02d}行 / 保留正文" for i in range(45)),
        ))
        drawn: dict[str, list[str]] = {}
        original_draw = exporters.ChatRenderer._draw_message

        def record_draw(renderer, draw, page, block, y):
            drawn.setdefault(block.message.id, []).extend(block.lines)
            return original_draw(renderer, draw, page, block, y)

        output = self.folder / "cards.pdf"
        with mock.patch(
            "wechat_context_exporter.rendering.chat.Path",
            side_effect=AssertionError("body must never be interpreted as a path"),
        ), mock.patch.object(
            exporters.ChatRenderer, "_draw_message", autospec=True, side_effect=record_draw,
        ), mock.patch.object(exporters, "_worker_temp_root", return_value=self.folder):
            result = export_conversation(
                make_conversation(), messages, output, "pdf", materialize_media=False,
            )
        self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        self.assertEqual(result.message_count, len(messages))
        self.assertEqual(result.image_page_count, 0)
        self.assertGreater(result.chat_page_count, 1)
        for message in messages:
            # Wrapping can split a long source line, but may not discard any
            # non-whitespace text, path prefixes, literal HTML or link syntax.
            self.assertEqual(
                "".join("".join(drawn[message.id]).split()),
                "".join(message.content.split()), message.id,
            )
        self.assertIn("", drawn["link"])
        self.assertIn("# 不是标题", drawn["link"])
        self.assertEqual(drawn["forward"].count("重复条目"), 2)
        self.assertEqual(drawn["continuation"], messages[-1].content.split("\n"))


class ImageTests(ExportTestCase):
    def test_image_is_copied_and_referenced_from_markdown_and_json(self):
        source = make_png(self.folder / "source 截图.PNG")
        conversation = make_conversation()
        image_message = make_message("../截图", MessageType.IMAGE, str(source))

        markdown_path = self.folder / "带图.md"
        markdown_result = export_conversation(conversation, [image_message], markdown_path, "markdown")
        markdown = markdown_path.read_text(encoding="utf-8")
        match = re.search(r"!\[图片\]\(<([^>]+)>\)", markdown)
        self.assertIsNotNone(match)
        relative = match.group(1)
        self.assertFalse(Path(relative).is_absolute())
        copied = (markdown_path.parent / relative).resolve()
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.read_bytes(), source.read_bytes())
        self.assertEqual(copied.parent, self.folder / "带图_assets")
        self.assertEqual(markdown_result.assets_dir, self.folder / "带图_assets")
        # A malicious-looking id must not escape the assets directory.
        self.assertEqual(copied.parent, markdown_result.assets_dir.resolve())
        self.assertFalse((self.folder / "evil").exists())

        json_path = self.folder / "带图.json"
        json_result = export_conversation(conversation, [image_message], json_path, "json")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        exported = payload["conversations"][0]["messages"][0]
        self.assertEqual(exported["type"], "image")
        self.assertFalse(Path(exported["content"]).is_absolute())
        self.assertTrue((json_path.parent / exported["content"]).is_file())
        self.assertNotIn("audio_path", exported)
        self.assertEqual(json_result.assets_dir, self.folder / "带图_assets")

    def test_missing_image_becomes_placeholder(self):
        conversation = make_conversation()
        message = make_message("m-missing", MessageType.IMAGE, str(self.folder / "not-here.png"))

        markdown_path = self.folder / "缺失.md"
        result = export_conversation(conversation, [message], markdown_path, "markdown")
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("_[图片不可读]_", markdown)
        self.assertNotIn("![图片]", markdown)
        self.assertIsNone(result.assets_dir)
        self.assertFalse((self.folder / "缺失_assets").exists())

        json_path = self.folder / "缺失.json"
        result = export_conversation(conversation, [message], json_path, "json")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        exported = payload["conversations"][0]["messages"][0]
        self.assertEqual(exported["type"], "system")
        self.assertEqual(exported["content"], "图片不可读")
        self.assertIsNone(result.assets_dir)

    def test_materialize_media_false_uses_placeholders(self):
        source = make_png(self.folder / "real.png")
        conversation = make_conversation()
        messages = [
            make_message("img", MessageType.IMAGE, str(source)),
            make_message(
                "voice",
                MessageType.VOICE,
                "语音占位",
                transcript="这段转写不应出现",
                duration_ms=1000,
            ),
        ]
        markdown_path = self.folder / "关闭媒体.md"
        result = export_conversation(
            conversation,
            messages,
            markdown_path,
            "markdown",
            materialize_media=False,
        )
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("_[图片不可读]_", markdown)
        self.assertIn("_[语音消息]_", markdown)
        self.assertNotIn("![图片]", markdown)
        self.assertNotIn("这段转写不应出现", markdown)
        self.assertIsNone(result.assets_dir)
        self.assertFalse((self.folder / "关闭媒体_assets").exists())

        json_path = self.folder / "关闭媒体.json"
        export_conversation(
            conversation,
            messages,
            json_path,
            "json",
            materialize_media=False,
        )
        exported = json.loads(json_path.read_text(encoding="utf-8"))["conversations"][0]["messages"]
        self.assertEqual([item["type"] for item in exported], ["system", "system"])
        self.assertEqual([item["content"] for item in exported], ["图片不可读", "语音消息"])


class PdfTests(ExportTestCase):
    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_pdf_has_chinese_pages_and_persists_page_pngs(self):
        conversation = make_conversation("聊天记录")
        messages = [
            make_message(
                "m1",
                MessageType.TEXT,
                "这是一段用于 PDF 测试的中文消息。\n包含标点：，。！？",
                timestamp=datetime(2026, 11, 24, 9, 30, tzinfo=UTC8),
            ),
            make_message(
                "m2",
                MessageType.SYSTEM,
                "系统中文提示",
                sender="系统",
                timestamp=datetime(2026, 11, 24, 9, 31, tzinfo=UTC8),
            ),
        ]
        pages_dir = self.folder / "页面"
        pages_dir.mkdir()
        keep = pages_dir / "keep.txt"
        keep.write_text("用户文件", encoding="utf-8")

        output = self.folder / "聊天记录.pdf"
        result = export_conversation(
            conversation,
            messages,
            output,
            "pdf",
            pages_dir=pages_dir,
        )

        data = output.read_bytes()
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertGreater(len(data), 1024)
        self.assertIsNotNone(re.search(rb"/Type\s*/Page(?![s])", data))
        self.assertGreaterEqual(result.page_count, 1)
        self.assertEqual(result.page_count, result.chat_page_count)
        self.assertEqual(result.image_page_count, 0)
        self.assertEqual(result.message_count, 2)
        self.assertEqual(result.format, "pdf")
        self.assertEqual(result.bytes, len(data))
        self.assertEqual(result.pages_dir, pages_dir)

        page_files = sorted(pages_dir.glob("page_*_chat.png"))
        self.assertEqual(len(page_files), result.page_count)
        self.assertEqual(page_files[0].name, "page_0001_chat.png")
        with Image.open(page_files[0]) as rendered:
            self.assertEqual(rendered.size, (2480, 3508))
        self.assertEqual(keep.read_text(encoding="utf-8"), "用户文件")

    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_pdf_renders_independent_image_pages_when_media_enabled(self):
        conversation = make_conversation("聊天记录")
        image = make_png(self.folder / "照片.png")
        messages = [
            make_message("m1", MessageType.TEXT, "图片见下。"),
            make_message("m2", MessageType.IMAGE, str(image)),
            make_message("m3", MessageType.TEXT, "图片之后。"),
        ]
        pages_dir = self.folder / "页面"
        pages_dir.mkdir()
        output = self.folder / "聊天记录.pdf"
        result = export_conversation(
            conversation,
            messages,
            output,
            "pdf",
            pages_dir=pages_dir,
            materialize_media=True,
        )

        self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        self.assertEqual(result.message_count, 3)
        self.assertEqual(result.chat_page_count, 1)
        self.assertEqual(result.image_page_count, 1)
        self.assertEqual(result.page_count, 2)
        self.assertEqual(
            sorted(path.name for path in pages_dir.glob("page_*.png")),
            ["page_0001_chat.png", "page_0002_image.png"],
        )
        with Image.open(pages_dir / "page_0002_image.png") as rendered:
            self.assertEqual(rendered.size, (2480, 3508))

        without_media = self.folder / "无图片.pdf"
        result = export_conversation(
            conversation,
            messages,
            without_media,
            "pdf",
            materialize_media=False,
        )
        self.assertEqual(result.image_page_count, 0)
        self.assertEqual(result.page_count, result.chat_page_count)

    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_empty_pdf_has_one_page(self):
        output = self.folder / "empty.pdf"
        result = export_conversation(make_conversation(), [], output, "pdf")
        self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        self.assertEqual(result.message_count, 0)
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.chat_page_count, 1)
        self.assertEqual(result.image_page_count, 0)

    def test_pdf_sanitizes_media_without_fake_files(self):
        valid = make_png(self.folder / "good.png")
        messages = [
            make_message("good", MessageType.IMAGE, str(valid)),
            make_message("bad", MessageType.IMAGE, str(self.folder / "bad.png")),
            make_message("file", MessageType.FILE, str(self.folder / "report.pdf")),
            make_message(
                "voice",
                MessageType.VOICE,
                "语音占位",
                transcript="语音转写文本",
                duration_ms=2000,
                audio_path=Path(self.folder / "voice.silk"),
            ),
        ]
        sanitized = exporters._sanitize_for_pdf(messages, True)
        self.assertEqual(
            [message.type for message in sanitized],
            [MessageType.IMAGE, MessageType.SYSTEM, MessageType.SYSTEM, MessageType.TEXT],
        )
        self.assertEqual(sanitized[1].content, "图片不可读")
        self.assertTrue(sanitized[2].content.startswith("[文件] "))
        self.assertEqual(sanitized[3].content, "语音转写文本")
        self.assertIsNone(sanitized[3].audio_path)
        self.assertTrue(all(message.audio_path is None for message in sanitized))

        disabled = exporters._sanitize_for_pdf(messages, False)
        self.assertEqual(disabled[0].type, MessageType.SYSTEM)
        self.assertEqual(disabled[0].content, "图片不可读")
        self.assertEqual(disabled[3].type, MessageType.SYSTEM)
        self.assertEqual(disabled[3].content, "语音消息")

    def test_font_unavailable_is_a_fixed_chinese_error(self):
        output = self.folder / "font.pdf"
        with mock.patch.object(
            exporters.FontBook, "discover", side_effect=RuntimeError("no font here")
        ):
            error = self.assert_export_error(
                "FONT_UNAVAILABLE",
                lambda: export_conversation(
                    make_conversation(),
                    [make_message("m1", MessageType.TEXT, "内容")],
                    output,
                    "pdf",
                ),
                "系统中没有可用于 PDF 的中文字体。",
            )
        self.assertNotIn("no font here", error.message)
        self.assertNotIn(str(output), error.message)
        self.assertFalse(output.exists())

    def test_failed_pdf_keeps_existing_target_untouched(self):
        output = self.folder / "existing.pdf"
        sentinel = b"%PDF-old-content"
        output.write_bytes(sentinel)
        with mock.patch.object(
            exporters.FontBook, "discover", side_effect=RuntimeError("missing")
        ):
            error = self.assert_export_error(
                "FONT_UNAVAILABLE",
                lambda: export_conversation(
                    make_conversation(),
                    [make_message("m1", MessageType.TEXT, "内容")],
                    output,
                    "pdf",
                    overwrite=True,
                ),
            )
        self.assertTrue(error.message)
        self.assertEqual(output.read_bytes(), sentinel)

    def test_message_limit_is_a_fixed_error_without_rendering(self):
        output = self.folder / "limit.pdf"
        messages = [
            make_message("m1", MessageType.TEXT, "一"),
            make_message("m2", MessageType.TEXT, "二"),
        ]
        with mock.patch.object(exporters, "MAX_PDF_MESSAGES", 1):
            error = self.assert_export_error(
                "PAGE_LIMIT_EXCEEDED",
                lambda: export_conversation(make_conversation(), messages, output, "pdf"),
            )
        self.assertIn("所选范围的消息过多", error.message)
        self.assertNotIn(str(output), error.message)
        self.assertFalse(output.exists())

    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_page_limit_is_a_fixed_error(self):
        output = self.folder / "page-limit.pdf"
        with mock.patch.object(exporters, "MAX_PDF_PAGES", 0):
            error = self.assert_export_error(
                "PAGE_LIMIT_EXCEEDED",
                lambda: export_conversation(
                    make_conversation(),
                    [make_message("m1", MessageType.TEXT, "内容")],
                    output,
                    "pdf",
                ),
            )
        self.assertIn("所选范围的消息过多", error.message)
        self.assertFalse(output.exists())


class ErrorTests(ExportTestCase):
    def test_invalid_arguments_have_fixed_codes_and_no_paths(self):
        conversation = make_conversation()
        message = make_message("m1", MessageType.TEXT, "内容")
        cases = [
            ("relative.md", "markdown", {}),
            (str(self.folder / "wrong.txt"), "markdown", {}),
            (str(self.folder / "missing-dir" / "x.md"), "markdown", {}),
            (str(self.folder / "x.html"), "html", {}),
            (
                str(self.folder / "x.pdf"),
                "pdf",
                {"pages_dir": "relative-pages"},
            ),
        ]
        for output, fmt, extra in cases:
            with self.subTest(output=output, fmt=fmt):
                error = self.assert_export_error(
                    "INVALID_ARGUMENT",
                    lambda output=output, fmt=fmt, extra=extra: export_conversation(
                        conversation, [message], output, fmt, **extra
                    ),
                )
                self.assertEqual(error.message, "导出参数无效。")
                self.assertTrue(re.search(r"[一-鿿]", error.message))

    def test_existing_output_requires_overwrite_and_preserves_file(self):
        conversation = make_conversation()
        message = make_message("m1", MessageType.TEXT, "新内容")
        output = self.folder / "existing.md"
        output.write_text("旧内容", encoding="utf-8")

        error = self.assert_export_error(
            "OUTPUT_EXISTS",
            lambda: export_conversation(conversation, [message], output, "markdown"),
            "输出文件已存在。请确认覆盖或选择其他位置。",
        )
        self.assertNotIn(str(output), error.message)
        self.assertEqual(output.read_text(encoding="utf-8"), "旧内容")

        result = export_conversation(conversation, [message], output, "markdown", overwrite=True)
        self.assertIn("新内容", output.read_text(encoding="utf-8"))
        self.assertEqual(result.message_count, 1)


class NonTextSafetyTests(ExportTestCase):
    def test_file_and_voice_never_become_fake_links(self):
        file_path = self.folder / "report.pdf"
        file_path.write_bytes(b"not really a pdf")
        audio_path = self.folder / "voice.silk"
        audio_path.write_bytes(b"SILK")
        conversation = make_conversation()
        messages = [
            make_message("file", MessageType.FILE, str(file_path), sender="李四"),
            make_message(
                "voice-1",
                MessageType.VOICE,
                "语音内容占位",
                audio_path=audio_path,
                duration_ms=4200,
            ),
            make_message(
                "voice-2",
                MessageType.VOICE,
                "另一条语音占位",
                audio_path=audio_path,
                duration_ms=5000,
                transcript="这是语音转写。",
            ),
        ]

        markdown_path = self.folder / "媒体.md"
        export_conversation(conversation, messages, markdown_path, "markdown")
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertNotIn("![", markdown)
        self.assertNotIn("](", markdown)
        self.assertNotIn("file://", markdown)
        self.assertIn("_[文件] ", markdown)
        self.assertIn(str(file_path), unescape_markdown(markdown))
        self.assertIn("_[语音消息]_", markdown)
        self.assertIn("这是语音转写。", markdown)
        self.assertNotIn(str(audio_path), markdown)
        self.assertNotIn("语音内容占位", markdown)

        json_path = self.folder / "媒体.json"
        export_conversation(conversation, messages, json_path, "json")
        raw = json_path.read_text(encoding="utf-8")
        self.assertNotIn("audio_path", raw)
        self.assertNotIn(str(audio_path), raw)
        exported = json.loads(raw)["conversations"][0]["messages"]
        self.assertEqual(exported[0]["type"], "system")
        self.assertTrue(exported[0]["content"].startswith("[文件] "))
        self.assertEqual(exported[1]["type"], "system")
        self.assertEqual(exported[1]["content"], "语音消息")
        self.assertEqual(exported[2]["type"], "voice")
        self.assertEqual(exported[2]["content"], "这是语音转写。")
        self.assertEqual(exported[2]["transcript"], "这是语音转写。")
        self.assertEqual(exported[2]["duration_ms"], 5000)

        sanitized = exporters._sanitize_for_pdf(messages, True)
        self.assertTrue(all(message.audio_path is None for message in sanitized))
        self.assertEqual(sanitized[1].type, MessageType.SYSTEM)
        self.assertEqual(sanitized[2].type, MessageType.TEXT)
        self.assertEqual(sanitized[2].content, "这是语音转写。")


if __name__ == "__main__":
    unittest.main()
