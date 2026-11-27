"""Security, overwrite-cleanup and PDF caps for the export path.

Synthetic conversations and images only. Run from the project root:

  .venv\\Scripts\\python.exe -I -X utf8 -m unittest discover -s tests/PythonWorker.Tests -v
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

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
from wechat_context_exporter.text_exporters import export_markdown as vendor_export_markdown  # noqa: E402
from wechat_context_exporter.pdf_exporter import PdfExporter  # noqa: E402
from wechat_memory import exporters  # noqa: E402
from wechat_memory.exporters import ExportError, export_conversation  # noqa: E402

UTC8 = timezone(timedelta(hours=8))


def _fonts_available() -> bool:
    try:
        FontBook.discover()
        return True
    except RuntimeError:
        return False


FONT_AVAILABLE = _fonts_available()


def make_conversation(name: str = "测试会话") -> Conversation:
    return Conversation(id="conv-1", name=name, kind=ConversationKind.GROUP)


def make_message(
    message_id: str,
    kind: MessageType,
    content: str,
    *,
    sender: str = "张三",
    timestamp: datetime | None = None,
    **extra: object,
) -> Message:
    return Message(
        id=message_id,
        conversation_id="conv-1",
        sender=sender,
        timestamp=timestamp or datetime(2026, 11, 24, 9, 0, tzinfo=UTC8),
        type=kind,
        content=content,
        **extra,  # type: ignore[arg-type]
    )


def make_png(path: Path, color: tuple[int, int, int] = (20, 120, 200)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (24, 18), color).save(path)
    return path


def unescape_markdown(text: str) -> str:
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~<])", r"\1", text)


def fake_private_temp(root: Path) -> types.ModuleType:
    module = types.ModuleType("wechat_memory.private_temp")
    module.get_worker_temp_root = lambda: root  # type: ignore[attr-defined]
    return module


class ExportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="wechat-export-security-")
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name)

    def assert_export_error(self, code: str, callable_):
        with self.assertRaises(ExportError) as raised:
            callable_()
        self.assertEqual(raised.exception.code, code)
        self.assertTrue(raised.exception.message)
        self.assertNotIn(str(self.folder), raised.exception.message)
        return raised.exception


class MarkdownInjectionTests(ExportTestCase):
    def test_title_sender_and_body_stay_literal_text(self):
        image_source = make_png(self.folder / "local 图片.png")
        conversation = make_conversation("正常\n# 注入标题 <img src='https://evil.test/a.png'>")
        malicious_sender = '**evil** <img src="https://evil.test/sender.png">'
        malicious_body = (
            "![远程图片](https://evil.test/pixel.png)\n"
            "<script>alert('x')</script>\n"
            "[链接](javascript:alert(1))\n"
            "<https://evil.test/auto>"
        )
        messages = [
            make_message("m1", MessageType.TEXT, malicious_body, sender=malicious_sender),
            make_message("m2", MessageType.SYSTEM, "<img src=https://evil.test/sys>", sender="系统"),
            make_message("m3", MessageType.IMAGE, str(image_source)),
        ]
        output = self.folder / "安全.md"
        result = export_conversation(conversation, messages, output, "markdown")
        raw = output.read_text(encoding="utf-8")

        # No active HTML, image syntax, link syntax or injected heading.
        # An escaped ``\<tag`` is literal text and safe.
        self.assertNotRegex(raw, r"(?<!\\)<(?:img|script|https)")
        self.assertNotIn("](https", raw)
        self.assertNotIn("](javascript", raw)
        self.assertNotIn("\n# 注入", raw)
        self.assertEqual(raw.count("![图片]"), 1)  # only the generated local link
        self.assertIn("\\# 注入标题", raw)
        self.assertIn("\\<script\\>", raw)

        # The original text is still present for a reader (modulo escaping).
        visible = unescape_markdown(raw)
        self.assertIn("<img src=\"https://evil.test/sender.png\">", visible)
        self.assertIn("evil", visible)
        self.assertIn(malicious_body, visible)
        self.assertIn("<img src=https://evil.test/sys>", visible)
        self.assertIn("正常", raw)

        # The generated local image link still works.
        match = re.search(r"!\[图片\]\(<([^>]+)>\)", raw)
        self.assertIsNotNone(match)
        copied = (output.parent / match.group(1)).resolve()
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.parent, result.assets_dir)

    def test_json_keeps_raw_text_as_data(self):
        body = "![x](https://evil.test/a.png) <script>alert(1)</script>"
        output = self.folder / "raw.json"
        export_conversation(
            make_conversation(),
            [make_message("m1", MessageType.TEXT, body)],
            output,
            "json",
        )
        import json

        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["conversations"][0]["messages"][0]["content"], body)


class VendorMarkdownInjectionTests(ExportTestCase):
    def test_vendor_markdown_escapes_chat_text_and_rejects_remote_images(self):
        local_image = make_png(self.folder / "local image.png")
        conversation = make_conversation("标题\n# 注入")
        messages = [
            make_message(
                "m1",
                MessageType.TEXT,
                "![远程](https://evil.test/x.png)\n<script>alert(1)</script>",
                sender="**[evil](https://evil.test)**",
            ),
            make_message(
                "m2",
                MessageType.IMAGE,
                "https://evil.test/remote.png",
                sender="[/\\]",
            ),
            make_message(
                "m3",
                MessageType.FILE,
                "bad`name\nhttps://evil.test/file",
                sender="文件",
            ),
            make_message(
                "m4",
                MessageType.VOICE,
                "<script>transcript</script>",
                sender="语音",
                duration_ms=1500,
            ),
            make_message(
                "m5",
                MessageType.IMAGE,
                str(local_image),
                sender="图片",
            ),
        ]
        output = self.folder / "vendor.md"
        vendor_export_markdown(conversation, messages, output)
        raw = output.read_text(encoding="utf-8")

        self.assertNotRegex(raw, r"(?<!\\)<(?:script|https)")
        self.assertNotIn("](https", raw)
        self.assertNotIn("\n# 注入", raw)
        self.assertNotIn("![Image attachment](https", raw)
        self.assertIn("!\\[远程\\]", raw)
        self.assertIn("\\<script\\>transcript", raw)

        # The one local image source remains an active, quoted local link.
        links = re.findall(r"!\[Image attachment\]\(([^)]+)\)", raw)
        self.assertEqual(len(links), 1)
        self.assertIn("local%20image.png", links[0])
        self.assertTrue((self.folder / "local image.png").is_file())

        visible = unescape_markdown(raw)
        self.assertIn("![远程](https://evil.test/x.png)", visible)
        self.assertIn("https://evil.test/remote.png", visible.replace("\\", ""))
        self.assertIn("标题", visible)


class OverwriteCleanupTests(ExportTestCase):
    def test_overwrite_removes_stale_assets_but_keeps_unrelated_files(self):
        sources = [
            make_png(self.folder / f"img{i}.png", (i * 40, 20, 20))
            for i in range(1, 4)
        ]
        messages = [
            make_message(f"a{i}", MessageType.IMAGE, str(source))
            for i, source in enumerate(sources, start=1)
        ]
        output = self.folder / "记录.md"
        first = export_conversation(make_conversation(), messages, output, "markdown")
        assets = first.assets_dir
        self.assertIsNotNone(assets)
        self.assertEqual(
            sorted(path.name for path in assets.glob("*.png")),
            ["0001_a1.png", "0002_a2.png", "0003_a3.png"],
        )
        (assets / "notes.txt").write_text("用户文件", encoding="utf-8")
        (assets / "custom.png").write_bytes(b"not our naming scheme")

        second = export_conversation(
            make_conversation(),
            [messages[0]],
            output,
            "markdown",
            overwrite=True,
        )
        self.assertEqual(second.assets_dir, assets)
        self.assertEqual(
            sorted(path.name for path in assets.glob("0*.png")),
            ["0001_a1.png"],
        )
        self.assertTrue((assets / "notes.txt").is_file())
        self.assertTrue((assets / "custom.png").is_file())
        text = output.read_text(encoding="utf-8")
        self.assertIn("0001_a1.png", text)
        self.assertNotIn("0002_a2.png", text)

    def test_media_disabled_overwrite_removes_stale_generated_assets(self):
        source = make_png(self.folder / "source.png")
        output = self.folder / "无媒体.md"
        first = export_conversation(
            make_conversation(),
            [make_message("m1", MessageType.IMAGE, str(source))],
            output,
            "markdown",
        )
        assets = first.assets_dir
        self.assertIsNotNone(assets)
        (assets / "keep.txt").write_text("保留", encoding="utf-8")

        second = export_conversation(
            make_conversation(),
            [make_message("m1", MessageType.IMAGE, str(source))],
            output,
            "markdown",
            overwrite=True,
            materialize_media=False,
        )
        self.assertIsNone(second.assets_dir)
        self.assertEqual(list(assets.glob("0*.png")), [])
        self.assertTrue((assets / "keep.txt").is_file())
        self.assertIn("_[图片不可读]_", output.read_text(encoding="utf-8"))

    def test_failed_overwrite_preserves_old_output_and_assets(self):
        first_source = make_png(self.folder / "blue.png", (20, 40, 200))
        output = self.folder / "保留.md"
        first = export_conversation(
            make_conversation(),
            [make_message("m1", MessageType.IMAGE, str(first_source))],
            output,
            "markdown",
        )
        old_output = output.read_bytes()
        old_assets = {path.name: path.read_bytes() for path in first.assets_dir.iterdir()}
        self.assertEqual(len(old_assets), 1)

        new_source = make_png(self.folder / "red.png", (220, 20, 20))
        real_replace = os.replace

        def failing_replace(source, target):
            if Path(target) == output:
                raise OSError("simulated output failure")
            return real_replace(source, target)

        with mock.patch.object(exporters.os, "replace", side_effect=failing_replace):
            error = self.assert_export_error(
                "EXPORT_FAILED",
                lambda: export_conversation(
                    make_conversation(),
                    [make_message("m1", MessageType.IMAGE, str(new_source))],
                    output,
                    "markdown",
                    overwrite=True,
                ),
            )
        self.assertEqual(error.message, "导出文件失败。")
        self.assertEqual(output.read_bytes(), old_output)
        self.assertEqual(
            {path.name: path.read_bytes() for path in first.assets_dir.iterdir()},
            old_assets,
        )
        self.assertEqual(
            [path.name for path in self.folder.iterdir() if path.name.startswith(".wechat-memory-")],
            [],
        )


class PdfStreamingTests(ExportTestCase):
    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_page_limit_is_checked_before_any_page_is_drawn(self):
        image = make_png(self.folder / "photo.png")
        output = self.folder / "limit.pdf"
        pages_dir = self.folder / "pages"
        pages_dir.mkdir()
        messages = [
            make_message("m1", MessageType.TEXT, "正文"),
            make_message("m2", MessageType.IMAGE, str(image)),
        ]
        with mock.patch.object(exporters, "MAX_PDF_PAGES", 1), mock.patch.object(
            exporters.ChatRenderer,
            "_draw_page",
            side_effect=AssertionError("page must not be drawn"),
        ) as draw:
            self.assert_export_error(
                "PAGE_LIMIT_EXCEEDED",
                lambda: export_conversation(
                    make_conversation(), messages, output, "pdf", pages_dir=pages_dir
                ),
            )
        draw.assert_not_called()
        self.assertFalse(output.exists())
        self.assertEqual(list(pages_dir.glob("page_*.png")), [])

    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_pdf_temp_pages_use_private_root_and_are_removed(self):
        private_root = self.folder / "private-temp"
        private_root.mkdir()
        fake_module = fake_private_temp(private_root)
        output = self.folder / "private.pdf"
        real_mkdtemp = tempfile.mkdtemp

        with mock.patch.dict(
            sys.modules, {"wechat_memory.private_temp": fake_module}
        ), mock.patch.object(
            exporters.tempfile, "mkdtemp", side_effect=real_mkdtemp
        ) as mkdtemp:
            result = export_conversation(
                make_conversation(),
                [make_message("m1", MessageType.TEXT, "私有临时根")],
                output,
                "pdf",
            )
        dirs = [
            Path(call.kwargs["dir"]).resolve()
            for call in mkdtemp.call_args_list
            if call.kwargs.get("dir")
        ]
        self.assertIn(private_root.resolve(), dirs)
        self.assertEqual(list(private_root.iterdir()), [])
        self.assertGreaterEqual(result.page_count, 1)

        failing = self.folder / "failing.pdf"
        with mock.patch.dict(
            sys.modules, {"wechat_memory.private_temp": fake_module}
        ), mock.patch.object(
            PdfExporter, "export", side_effect=OSError("disk full")
        ):
            self.assert_export_error(
                "EXPORT_FAILED",
                lambda: export_conversation(
                    make_conversation(),
                    [make_message("m1", MessageType.TEXT, "失败清理")],
                    failing,
                    "pdf",
                ),
            )
        self.assertEqual(list(private_root.iterdir()), [])
        self.assertFalse(failing.exists())

    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_failed_pdf_overwrite_restores_old_pages_and_output(self):
        pages_dir = self.folder / "pages"
        pages_dir.mkdir()
        output = self.folder / "old.pdf"
        export_conversation(
            make_conversation(),
            [make_message("m1", MessageType.TEXT, "旧页面")],
            output,
            "pdf",
            pages_dir=pages_dir,
        )
        old_pdf = output.read_bytes()
        old_page = (pages_dir / "page_0001_chat.png").read_bytes()
        (pages_dir / "keep.txt").write_text("保留", encoding="utf-8")

        real_replace = os.replace

        def failing_replace(source, target):
            if Path(target) == output:
                raise OSError("simulated output failure")
            return real_replace(source, target)

        with mock.patch.object(exporters.os, "replace", side_effect=failing_replace):
            self.assert_export_error(
                "EXPORT_FAILED",
                lambda: export_conversation(
                    make_conversation(),
                    [make_message("m1", MessageType.TEXT, "新页面内容不同")],
                    output,
                    "pdf",
                    overwrite=True,
                    pages_dir=pages_dir,
                ),
            )
        self.assertEqual(output.read_bytes(), old_pdf)
        self.assertEqual((pages_dir / "page_0001_chat.png").read_bytes(), old_page)
        self.assertTrue((pages_dir / "keep.txt").is_file())

    @unittest.skipUnless(FONT_AVAILABLE, "系统中没有可用于 PDF 的中文字体")
    def test_successful_pdf_overwrite_removes_stale_page_pngs(self):
        pages_dir = self.folder / "pages"
        pages_dir.mkdir()
        output = self.folder / "pages.pdf"
        export_conversation(
            make_conversation(),
            [make_message("m1", MessageType.TEXT, "第一版")],
            output,
            "pdf",
            pages_dir=pages_dir,
        )
        stale = pages_dir / "page_0099_chat.png"
        stale.write_bytes((pages_dir / "page_0001_chat.png").read_bytes())
        (pages_dir / "keep.txt").write_text("保留", encoding="utf-8")

        export_conversation(
            make_conversation(),
            [make_message("m1", MessageType.TEXT, "第二版")],
            output,
            "pdf",
            overwrite=True,
            pages_dir=pages_dir,
        )
        names = sorted(path.name for path in pages_dir.glob("page_*.png"))
        self.assertEqual(names, ["page_0001_chat.png"])
        self.assertTrue((pages_dir / "keep.txt").is_file())


class PrivateTempModuleTests(unittest.TestCase):
    def test_worker_temp_root_uses_private_temp_module(self):
        with tempfile.TemporaryDirectory(prefix="wechat-private-root-") as holder:
            root = Path(holder) / "worker-temp"
            fake_module = fake_private_temp(root)
            with mock.patch.dict(
                sys.modules, {"wechat_memory.private_temp": fake_module}
            ):
                resolved = exporters._worker_temp_root()
            self.assertEqual(resolved, root)
            self.assertTrue(root.is_dir())


if __name__ == "__main__":
    unittest.main()
