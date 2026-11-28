"""Synthetic A-G regressions for the transactional offline JSON source.

Run with the project's Python -I. No account discovery, live source, user job
store, network, or media path named inside imported JSON is used.
"""
from __future__ import annotations

import builtins
import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.request
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
sys.path.insert(0, str(WORKER_DIR / "vendor"))
sys.path.insert(0, str(WORKER_DIR))

import worker  # noqa: E402
from wechat_context_exporter.models import Conversation, Message, MessageType  # noqa: E402
from wechat_context_exporter.rendering import chat, image_page  # noqa: E402
from wechat_context_exporter.rendering.fonts import single_line_metadata  # noqa: E402
from wechat_memory import exporters, offline_import  # noqa: E402


def message(**overrides) -> dict:
    return {
        "id": "m1", "sender": "合成发送者",
        "timestamp": "2026-11-28T09:00:00+08:00",
        "type": "text", "content": "保留旧源 😀", **overrides,
    }


def document(messages: list[dict] | None = None, **conversation) -> dict:
    return {"version": 1, "conversations": [{
        "id": "offline", "name": "合成会话", "kind": "direct",
        "messages": [message()] if messages is None else messages, **conversation,
    }]}


def request(request_id: str, command: str, **payload) -> dict:
    return {"protocolVersion": 1, "requestId": request_id, "type": "request",
            "command": command, "payload": payload}


@contextmanager
def forbid_referenced_io():
    """Catch even swallowed attempts to inspect synthetic external paths."""
    attempts = []

    def guarded(original):
        def call(path, *args, **kwargs):
            if "offline-forbidden" in str(path).casefold():
                attempts.append(str(path))
                raise AssertionError("imported media must remain inert text")
            return original(path, *args, **kwargs)
        return call

    def no_network(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("offline import/export must not use the network")

    with ExitStack() as stack:
        for owner, name in ((Path, "open"), (Path, "stat"),
                            (builtins, "open"), (io, "open")):
            stack.enter_context(mock.patch.object(
                owner, name, autospec=True, side_effect=guarded(getattr(owner, name))))
        for owner, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                            (socket.socket, "connect"), (socket.socket, "connect_ex"),
                            (urllib.request, "urlopen")):
            stack.enter_context(mock.patch.object(owner, name, side_effect=no_network))
        yield attempts
    if attempts:
        raise AssertionError(f"unexpected imported-path/network access: {attempts!r}")


class OfflineRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory(prefix="offline-regressions-")
        self.addCleanup(holder.cleanup)
        self.folder = Path(holder.name)
        self.private = self.folder / "private-pdf-temp"
        self.private.mkdir()
        patcher = mock.patch.object(exporters, "_worker_temp_root", return_value=self.private)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.context = worker.Context(
            str(ROOT / "demo" / "conversations.json"),
            jobs_dir=str(self.folder / "jobs"), scenes_file=str(self.folder / "scenes.json"),
        )
        self.counter = 0
        self.old_path = self.write_document(document(), "old.json")
        worker.handle_import_json({"path": str(self.old_path)}, self.context)

    def write_document(self, payload: object, name: str = "candidate.json") -> Path:
        # ASCII escaping intentionally exercises JSON UTF-16 escapes, including
        # invalid lone surrogates and valid emoji surrogate pairs.
        return self.write_raw(json.dumps(payload, ensure_ascii=True), name)

    def write_raw(self, text: str, name: str = "candidate.json") -> Path:
        path = self.folder / name
        path.write_text(text, encoding="utf-8")
        return path

    def output(self, suffix: str) -> Path:
        self.counter += 1
        return self.folder / f"out-{self.counter}{suffix}"

    def payload(self, **overrides) -> dict:
        return {"source": "offline", "conversationId": "offline", **overrides}

    def run_protocol(self, requests: list[dict]) -> dict[str, dict]:
        incoming = io.BytesIO("".join(json.dumps(item) + "\n" for item in requests).encode("utf-8"))
        outgoing = io.BytesIO()
        self.assertEqual(worker.serve(
            incoming, outgoing, self.context.demo_path,
            jobs_dir=self.context.jobs_dir, scenes_file=self.context.scenes_file), 0)
        responses = [json.loads(line) for line in outgoing.getvalue().decode("utf-8").splitlines()]
        return {item["requestId"]: item for item in responses if item["type"] == "response"}

    def assert_rejected_without_source_change(self, path: Path, code: str = "SOURCE_INVALID") -> None:
        old = self.context.offline
        old_path = self.context.offline_path
        with self.assertRaises(worker.WorkerError) as raised:
            worker.handle_import_json({"path": str(path)}, self.context)
        self.assertEqual(raised.exception.code, code)
        self.assertIs(self.context.offline, old)
        self.assertEqual(self.context.offline_path, old_path)
        queried = worker.handle_get_messages(self.payload(query="保留"), self.context)
        self.assertEqual([item["id"] for item in queried["items"]], ["m1"])
        # Exercise the real strict UTF-8 protocol encoder, not just Python dicts.
        worker._write(io.BytesIO(), worker._success("old-source", queried))
        for command, suffix in ((worker.handle_export_json, ".json"),
                                (worker.handle_export_markdown, ".md")):
            output = self.output(suffix)
            result = command(self.payload(query="保留", outputPath=str(output), materializeMedia=False),
                             self.context)
            self.assertEqual(result["messageCount"], 1)
            self.assertIn("保留旧源 😀", output.read_text(encoding="utf-8"))

    def test_query_matches_offline_sender_content_and_kind(self) -> None:
        path = self.write_document(document([
            message(id="sender", sender="Straße", content="other"),
            message(id="content", content="NEEDLE"),
            message(id="kind", type="voice", content="other", transcript="已有转写"),
        ]))
        worker.handle_import_json({"path": str(path)}, self.context)
        for query, expected in (("strasse", ["sender"]), ("needle", ["content"]),
                                ("VOICE", ["kind"]), ("missing", [])):
            with self.subTest(query=query):
                result = worker.handle_get_messages(self.payload(query=query), self.context)
                self.assertEqual([item["id"] for item in result["items"]], expected)

    def test_query_commands_and_job_preserve_voice_without_external_io(self) -> None:
        local = self.folder / "offline-forbidden"
        transcript = "已有语音转写 😀"
        included = [
            message(id="text", sender="needle", content="普通正文"),
            message(id="voice", sender="needle", type="voice", content="语音占位",
                    transcript=transcript, duration_ms=4200,
                    audio_path=str(local / "audio.silk")),
            message(id="image", sender="needle", type="image", content=str(local / "image.png")),
            message(id="remote-image", sender="needle", type="image",
                    content="https://offline-forbidden.invalid/image.png"),
            message(id="file", sender="needle", type="file", content=str(local / "report.pdf")),
        ]
        path = self.write_document(document(included + [message(id="excluded", content="not selected")]))
        outputs = {fmt: self.output(suffix) for fmt, suffix in
                   (("markdown", ".md"), ("json", ".json"), ("pdf", ".pdf"))}
        commands = [request("import", "import_json", path=str(path)),
                    request("get", "get_messages", **self.payload(query="NEEDLE"))]
        commands.extend(request(fmt, f"export_{fmt}", **self.payload(
            query="NEEDLE", materializeMedia=False, outputPath=str(output)))
            for fmt, output in outputs.items())
        commands.append(request("job", "export_job", **self.payload(
            query="NEEDLE", materializeMedia=False, formats=list(outputs), target={"kind": "none"})))
        render_inputs = []
        original_layout = exporters.ChatRenderer.layout

        def record_layout(renderer, messages):
            items = list(messages)
            render_inputs.extend(items)
            return original_layout(renderer, items)

        with forbid_referenced_io(), mock.patch.object(
            exporters.ChatRenderer, "layout", autospec=True, side_effect=record_layout
        ):
            responses = self.run_protocol(commands)
        self.assertEqual(set(responses), {"import", "get", "markdown", "json", "pdf", "job"})
        for command, response in responses.items():
            self.assertTrue(response["ok"], (command, response))
        self.assertEqual(responses["get"]["result"]["total"], len(included))
        for fmt in outputs:
            self.assertEqual(responses[fmt]["result"]["messageCount"], len(included))
            self.assertIsNone(responses[fmt]["result"]["assetsDir"])
        self.assertEqual(responses["pdf"]["result"]["imagePageCount"], 0)
        files = Path(responses["job"]["result"]["job"]["directory"]) / "files"
        for output in (outputs["json"], files / "chat.json"):
            exported = json.loads(output.read_text(encoding="utf-8"))["conversations"][0]["messages"]
            voice = next(item for item in exported if item["id"] == "voice")
            self.assertEqual((voice["type"], voice["duration_ms"], voice["transcript"]),
                             ("voice", 4200, transcript))
            self.assertEqual(voice["content"], transcript)
            self.assertNotIn("audio_path", voice)
            for image in (item for item in exported if item["id"] in {"image", "remote-image"}):
                self.assertEqual((image["type"], image["content"]), ("system", "[图片]"))
            restored = offline_import.load_json_file(output)
            restored_voice = next(item for item in restored.conversations[0].messages if item.id == "voice")
            self.assertEqual((restored_voice.kind, restored_voice.duration_ms, restored_voice.transcript),
                             ("voice", 4200, transcript))
        for output in (outputs["markdown"], files / "chat.md"):
            text = output.read_text(encoding="utf-8")
            self.assertIn(transcript, text)
            self.assertNotIn("audio.silk", text)
            self.assertNotIn("image.png", text)
        for output in (outputs["pdf"], files / "chat.pdf"):
            self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        self.assertEqual([item.content for item in render_inputs if item.id == "voice"],
                         [transcript, transcript])
        self.assertTrue(all(item.audio_path is None for item in render_inputs))
        self.assertEqual(list(self.private.iterdir()), [])

    def test_offline_ignored_media_fields_and_snapshot_never_open_paths(self) -> None:
        references = [str(self.folder / "offline-forbidden" / "local.png"),
                      r"\\offline-forbidden.invalid\share\image.png",
                      "file:///offline-forbidden/image.png",
                      "https://offline-forbidden.invalid/image.png"]
        path = self.write_document(document([
            message(id=f"image-{index}", type="image", content=reference,
                    audio_path=reference, attachment_path=reference, file_path=reference)
            for index, reference in enumerate(references)
        ]))
        with forbid_referenced_io():
            worker.handle_import_json({"path": str(path)}, self.context)
            path.unlink()
            queried = worker.handle_get_messages(self.payload(query="image"), self.context)
            self.assertEqual([item["content"] for item in queried["items"]], ["[图片]"] * 4)
            for enabled in (False, True):
                output = self.output(".json")
                worker.handle_export_json(self.payload(outputPath=str(output), materializeMedia=enabled),
                                          self.context)
                exported = json.loads(output.read_text(encoding="utf-8"))["conversations"][0]["messages"]
                self.assertEqual([item["content"] for item in exported], ["[图片]"] * 4)
                self.assertTrue(all("audio_path" not in item for item in exported))

    def test_non_offline_media_disabled_keeps_existing_transcript_privacy(self) -> None:
        conversation = Conversation("synthetic-live", "合成模型，不连接微信")
        voice = Message("voice", conversation.id, "合成发送者", datetime.now(timezone.utc),
                        MessageType.VOICE, "占位", transcript="不得导出的转写", duration_ms=4200)
        # Only the source-model seam is synthetic; both real direct/job writers
        # run, proving the offline-only condition does not change live/demo flags.
        for source in ("demo", "wechat4"):
            with self.subTest(source=source), mock.patch.object(
                worker, "_models_for_export", return_value=(conversation, [voice])
            ), forbid_referenced_io():
                output = self.output(".json")
                worker.handle_export_json({"source": source, "outputPath": str(output),
                                           "materializeMedia": False}, self.context)
                result = worker.handle_export_job({
                    "source": source, "formats": ["json", "markdown"],
                    "materializeMedia": False, "target": {"kind": "none"},
                }, self.context)
                files = Path(result["job"]["directory"]) / "files"
                for exported in (output, files / "chat.json", files / "chat.md"):
                    text = exported.read_text(encoding="utf-8")
                    self.assertNotIn(voice.transcript, text)
                    self.assertIn("语音消息", text)

    def test_lone_surrogates_in_every_retained_string_are_rejected(self) -> None:
        fields = (("conversation", "id"), ("conversation", "name"), ("conversation", "kind"),
                  ("message", "id"), ("message", "sender"), ("message", "content"),
                  ("message", "timestamp"), ("message", "type"),
                  ("message", "reply_to"), ("message", "transcript"))
        for entity, key in fields:
            for surrogate in ("\ud800", "\udfff"):
                with self.subTest(entity=entity, key=key, codepoint=ord(surrogate)):
                    payload = document()
                    conversation = payload["conversations"][0]
                    target = conversation if entity == "conversation" else conversation["messages"][0]
                    target[key] = "invalid" + surrogate
                    self.assert_rejected_without_source_change(self.write_document(payload))

    def test_valid_emoji_surrogate_pairs_survive_query_and_json_round_trip(self) -> None:
        payload = document([message(id="m😀", sender="合成😀", content="正文😀", reply_to="previous😀",
                                    type="voice", transcript="转写😀", duration_ms=0)],
                           id="c😀", name="标题😀")
        path = self.write_document(payload)
        worker.handle_import_json({"path": str(path)}, self.context)
        queried = worker.handle_get_messages(self.payload(conversationId="c😀", query="😀"), self.context)
        worker._write(io.BytesIO(), worker._success("emoji", queried))
        self.assertEqual(queried["items"][0]["sender"], "合成😀")
        output = self.output(".json")
        worker.handle_export_json(self.payload(conversationId="c😀", query="😀", outputPath=str(output),
                                              materializeMedia=False), self.context)
        restored = offline_import.load_json_file(output).conversations[0]
        self.assertEqual((restored.id, restored.title), ("c😀", "标题😀"))
        self.assertEqual((restored.messages[0].id, restored.messages[0].reply_to,
                          restored.messages[0].duration_ms, restored.messages[0].transcript),
                         ("m😀", "previous😀", 0, "转写😀"))

    def test_late_invalid_conversation_does_not_publish_partial_candidate(self) -> None:
        payload = document()
        payload["conversations"].append(document(id="second", name="invalid\ud800")["conversations"][0])
        self.assert_rejected_without_source_change(self.write_document(payload))

    def test_duplicate_keys_at_every_object_depth_are_rejected(self) -> None:
        raw = json.dumps(document(), separators=(",", ":"), ensure_ascii=False)
        cases = [
            raw.replace('"version":1', '"version":2,"version":1'),
            raw[:-1] + ',"conversations":[]}',
            raw.replace('"name":"合成会话"', '"name":"first","name":"合成会话"'),
            raw.replace('"id":"m1"', '"id":"first","id":"m1"'),
            raw[:-1] + ',"ignored":[{"nested":{"key":1,"key":2}}]}',
            raw.replace('"version":1', '"ver\\u0073ion":1,"version":1'),
        ]
        for index, candidate in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected_without_source_change(self.write_raw(candidate))

    def test_unrepresentable_utc_or_utc8_timestamps_are_rejected(self) -> None:
        cases = ("0001-01-01T00:00:00", "0001-01-01T00:00:00+08:00",
                 "0001-01-01T01:00:00+14:00", "9999-12-31T23:59:59Z",
                 "9999-12-31T23:59:59-01:00")
        for timestamp in cases:
            with self.subTest(timestamp=timestamp):
                self.assert_rejected_without_source_change(
                    self.write_document(document([message(timestamp=timestamp)])))

    def test_representable_historical_and_offset_boundaries_remain_usable(self) -> None:
        cases = ("0001-01-01T08:00:00", "0001-01-01T00:00:00Z",
                 "0001-01-01T14:00:00+14:00", "1900-01-01T00:00:00-05:00",
                 "2026-11-28T09:00:00+08:00", "9999-12-31T15:59:59.999999Z",
                 "9999-12-31T23:59:59.999999+08:00")
        for timestamp in cases:
            with self.subTest(timestamp=timestamp):
                path = self.write_document(document([message(timestamp=timestamp)]))
                worker.handle_import_json({"path": str(path)}, self.context)
                stored = self.context.offline.conversations[0].messages[0].timestamp
                self.assertGreaterEqual(stored.astimezone(timezone.utc).year, 1)
                self.assertEqual(stored.utcoffset().total_seconds(), 8 * 3600)
                queried = worker.handle_get_messages(self.payload(query="保留"), self.context)
                worker._write(io.BytesIO(), worker._success("historical", queried))
                output = self.output(".json")
                worker.handle_export_json(self.payload(outputPath=str(output)), self.context)
                self.assertEqual(offline_import.load_json_file(output).conversations[0].messages[0].timestamp,
                                 stored)

    def test_multiline_metadata_is_only_flattened_at_pdf_drawing_boundaries(self) -> None:
        title, sender, reply = "标题\r\n次行\n末行", "发送者\n次行\r末行", "m0\r\nreply"
        body = "第一行正文\n第二行正文"
        path = self.write_document(document([message(sender=sender, content=body, reply_to=reply)], name=title))
        worker.handle_import_json({"path": str(path)}, self.context)
        before = self.output(".md")
        worker.handle_export_markdown(self.payload(outputPath=str(before)), self.context)
        output = self.output(".pdf")
        with mock.patch.object(chat, "draw_text_with_fallback", wraps=chat.draw_text_with_fallback) as draw:
            worker.handle_export_pdf(self.payload(outputPath=str(output), materializeMedia=False), self.context)
        drawn = [call.args[2] for call in draw.call_args_list]
        self.assertIn(single_line_metadata(title), drawn)
        self.assertIn(single_line_metadata(sender), drawn)
        self.assertIn("Reply to " + single_line_metadata(reply), drawn)
        self.assertTrue(all("\n" not in text and "\r" not in text for text in drawn))
        self.assertTrue(output.read_bytes().startswith(b"%PDF"))
        self.assertEqual(self.context.offline.conversations[0].title, title)
        self.assertEqual(self.context.offline.conversations[0].messages[0].sender, sender)
        after = self.output(".md")
        worker.handle_export_markdown(self.payload(outputPath=str(after)), self.context)
        self.assertEqual(before.read_bytes(), after.read_bytes())
        self.assertIn(body, after.read_text(encoding="utf-8"))
        exported = self.output(".json")
        worker.handle_export_json(self.payload(outputPath=str(exported)), self.context)
        restored = json.loads(exported.read_text(encoding="utf-8"))["conversations"][0]
        self.assertEqual((restored["name"], restored["messages"][0]["sender"],
                          restored["messages"][0]["reply_to"]), (title, sender, reply))

    def test_image_page_metadata_uses_the_same_single_line_boundary(self) -> None:
        from PIL import Image

        source = self.folder / "generated.png"
        Image.new("RGB", (8, 8), (20, 40, 60)).save(source)
        conversation = Conversation("synthetic", "图片页\n次行")
        item = Message("image", conversation.id, "发送者\r\n次行", datetime.now(timezone.utc),
                       MessageType.IMAGE, str(source))
        renderer = image_page.ImagePageRenderer()
        with mock.patch.object(image_page, "draw_text_with_fallback",
                               wraps=image_page.draw_text_with_fallback) as draw:
            rendered = renderer.render(conversation, item, 1, 1)
            rendered.close()
        drawn = [call.args[2] for call in draw.call_args_list]
        self.assertIn("图片页 次行", drawn)
        self.assertIn("发送者 次行", drawn)
        self.assertTrue(all("\n" not in text and "\r" not in text for text in drawn))
        self.assertEqual((conversation.name, item.sender), ("图片页\n次行", "发送者\r\n次行"))

    def test_non_string_types_are_structured_source_errors(self) -> None:
        for value in ([], {}, 1, True, None):
            for key in ("type", "kind"):
                with self.subTest(key=key, value=value):
                    payload = document()
                    target = payload["conversations"][0]
                    if key == "type":
                        target = target["messages"][0]
                    target[key] = value
                    self.assert_rejected_without_source_change(self.write_document(payload))

    def test_deep_nesting_and_overlong_integer_are_structured_source_errors(self) -> None:
        cases = ('{"version":1,"conversations":[],"ignored":' + "[" * 1200 + "0" + "]" * 1200 + "}",
                 '{"version":' + "9" * 5000 + ',"conversations":[]}')
        for index, raw in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected_without_source_change(self.write_raw(raw))

    def test_protocol_errors_keep_old_source_queryable_and_exportable(self) -> None:
        valid_raw = json.dumps(document(), ensure_ascii=False)
        bad_raw = [
            valid_raw.replace("保留旧源", "invalid\\ud800"),
            valid_raw[:-1] + ',"conversations":[]}',
            json.dumps(document([message(timestamp="0001-01-01T00:00:00")])),
            json.dumps(document([message(type=[])])),
            '{"version":1,"conversations":[],"extra":' + "[" * 1200 + "0" + "]" * 1200 + "}",
            '{"version":' + "9" * 5000 + ',"conversations":[]}',
        ]
        commands = [request("old", "import_json", path=str(self.old_path))]
        for index, raw in enumerate(bad_raw):
            candidate = self.write_raw(raw, f"bad-{index}.json")
            commands.extend([
                request(f"bad-{index}", "import_json", path=str(candidate)),
                request(f"get-{index}", "get_messages", **self.payload(query="保留")),
                request(f"export-{index}", "export_json", **self.payload(
                    query="保留", outputPath=str(self.output(".json")), materializeMedia=False)),
            ])
        responses = self.run_protocol(commands)
        self.assertTrue(responses["old"]["ok"])
        for index in range(len(bad_raw)):
            self.assertEqual(responses[f"bad-{index}"]["error"]["code"], "SOURCE_INVALID")
            queried = responses[f"get-{index}"]
            self.assertTrue(queried["ok"], queried)
            self.assertEqual(queried["result"]["items"][0]["content"], "保留旧源 😀")
            exported = responses[f"export-{index}"]
            self.assertTrue(exported["ok"], exported)
            self.assertEqual(exported["result"]["messageCount"], 1)

    def test_import_limits_and_bounded_read_enforce_actual_bytes(self) -> None:
        self.assertEqual((offline_import.MAX_JSON_BYTES, offline_import.MAX_CONVERSATIONS,
                          offline_import.MAX_MESSAGES), (64 * 1024 * 1024, 10_000, 200_000))
        path = self.folder / "bounded.json"
        # A small cap exercises the exact-size boundary and a stat/read growth
        # race without allocating a 64 MiB fixture.
        minimal = json.dumps({"version": 1, "conversations": []}).encode("utf-8")
        with mock.patch.object(offline_import, "MAX_JSON_BYTES", 64):
            path.write_bytes(minimal.ljust(64, b" "))
            self.assertEqual(offline_import.load_json_file(path).message_count, 0)
            stream = io.BytesIO(minimal.ljust(65, b" "))
            read = mock.Mock(wraps=stream.read)
            stream.read = read
            with mock.patch.object(Path, "stat", return_value=SimpleNamespace(st_size=64)), \
                    mock.patch.object(Path, "open", return_value=stream):
                with self.assertRaises(offline_import.OfflineImportError) as raised:
                    offline_import.load_json_file(path)
            self.assertEqual(raised.exception.code, "SOURCE_TOO_LARGE")
            read.assert_called_once_with(65)
            path.write_bytes(minimal.ljust(65, b" "))
            self.assert_rejected_without_source_change(path, "SOURCE_TOO_LARGE")

    def test_conversation_and_total_message_limits_fail_transactionally(self) -> None:
        with mock.patch.object(offline_import, "MAX_CONVERSATIONS", 1):
            payload = document()
            payload["conversations"].append(document(id="second")["conversations"][0])
            self.assert_rejected_without_source_change(self.write_document(payload), "SOURCE_TOO_LARGE")
        with mock.patch.object(offline_import, "MAX_MESSAGES", 1):
            payload = document()
            payload["conversations"].append(document(id="second")["conversations"][0])
            self.assert_rejected_without_source_change(self.write_document(payload), "SOURCE_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
