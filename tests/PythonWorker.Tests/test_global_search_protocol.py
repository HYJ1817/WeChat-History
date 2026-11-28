"""Real JSON-lines pipe tests with fictional demo/offline/live synthetic data."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from synthetic_messages import ROOT
from wechat_memory.wechat4_source import SourceFailure, WeChat4Source
import worker


class GlobalSearchPipeTests(unittest.TestCase):
    def test_three_source_search_next_page_exact_context_and_switch_invalidation(self):
        with tempfile.TemporaryDirectory(prefix="search-protocol-") as folder:
            root = Path(folder)
            offline = root / "offline.json"
            offline.write_text(json.dumps({"version": 1, "conversations": [{
                "id": "offline", "name": "离线合成会话", "kind": "direct", "messages": [{
                    "id": "voice", "sender": "合成发送者", "type": "voice",
                    "timestamp": "2026-11-24T23:59:59+08:00", "content": "语音占位",
                    "transcript": "已有 needle 转写", "reply_to": "older",
                }],
            }]}), encoding="utf-8")
            # Only account connection is injected. The actual worker Context,
            # dispatch, serializers, search/context handlers and framing run in
            # a separate interpreter through stdin/stdout. No real account or
            # key capture is involved, and every generated DB is under root.
            bootstrap = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import worker
from synthetic_messages import SyntheticSourceFactory
import wechat_memory.wechat4_source as live
factory = SyntheticSourceFactory(Path(sys.argv[3]))
factory.add_shard("protocol_live.db", {"live": [
    {"create_time": 1795478400 + i, "content": "needle live " + str(i), "server_id": 7}
    for i in range(61)
]})
source = factory.source()
source.connect = lambda *args, **kwargs: {"accountLabel": "合成账户", "conversationCount": 1,
    "messageTableCount": 1, "imageKeyAvailable": False, "voiceAvailable": False}
live.WeChat4Source = lambda: source
raise SystemExit(worker.serve(sys.stdin.buffer, sys.stdout.buffer, sys.argv[4]))
'''
            process = subprocess.Popen(
                [sys.executable, "-I", "-X", "utf8", "-u", "-c", bootstrap,
                 str(ROOT / "src/PythonWorker"), str(ROOT / "tests/PythonWorker.Tests"), str(root),
                 str(ROOT / "demo/conversations.json")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT),
            )
            watchdog = threading.Timer(60, process.kill)
            watchdog.start()
            progress = []
            counter = 0
            def call(command, payload=None, *, ok=True):
                nonlocal counter
                counter += 1
                frame = {"protocolVersion": 1, "requestId": str(counter), "type": "request",
                         "command": command, "payload": payload or {}}
                process.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
                process.stdin.flush()
                while True:
                    line = process.stdout.readline()
                    self.assertTrue(line, "worker exited without a response")
                    self.assertLessEqual(len(line), 1024 * 1024)
                    response = json.loads(line)
                    self.assertEqual(response["requestId"], str(counter))
                    if response["type"] == "progress":
                        progress.append(response)
                        continue
                    self.assertEqual(response["ok"], ok, response)
                    return response["result"] if ok else response["error"]
            try:
                hello = call("hello")
                self.assertIn("global_message_search", hello["capabilities"])
                self.assertIn("message_context", hello["capabilities"])
                self.assertIn("message_cursor_pagination", hello["capabilities"])
                sid = call("list_conversations", {"source": "demo", "query": "hidden"})["sourceSessionId"]
                demo = call("search_messages", {"source": "demo", "sourceSessionId": sid,
                    "query": "text", "order": "desc", "limit": 50})
                self.assertGreater(demo["total"], 50)
                more = call("search_messages", {"source": "demo", "sourceSessionId": sid,
                    "searchId": demo["searchId"], "offset": demo["nextOffset"], "limit": 50})
                self.assertTrue(more["items"])
                target = demo["items"][0]
                context = call("get_message_context", {"source": "demo", "sourceSessionId": sid,
                    "searchId": demo["searchId"], "hitId": target["hitId"], "before": 20, "after": 20})
                self.assertEqual(context["items"][context["focusIndex"]]["id"], target["messageId"])
                bad = root / "bad.json"
                bad.write_text("{invalid", encoding="utf-8")
                self.assertEqual(call("import_json", {"path": str(bad)}, ok=False)["code"], "SOURCE_INVALID")
                call("search_messages", {"source": "demo", "sourceSessionId": sid,
                    "searchId": demo["searchId"], "offset": 0})
                imported = call("import_json", {"path": str(offline)})
                offline_sid = imported["sourceSessionId"]
                self.assertNotEqual(offline_sid, sid)
                self.assertEqual(call("search_messages", {"source": "demo", "sourceSessionId": sid,
                    "searchId": demo["searchId"]}, ok=False)["code"], "SOURCE_SESSION_CHANGED")
                offline.unlink()  # the active source is the memory snapshot
                imported_result = call("search_messages", {"source": "offline", "sourceSessionId": offline_sid,
                    "query": "needle"})
                self.assertEqual(imported_result["total"], 1)
                offline_context = call("get_message_context", {"source": "offline", "sourceSessionId": offline_sid,
                    "searchId": imported_result["searchId"], "hitId": imported_result["items"][0]["hitId"]})
                self.assertEqual(offline_context["items"][0]["replyTo"], "older")
                connected = call("connect", {"source": "wechat4", "enableImages": False, "enableVoice": False})
                live_sid = connected["sourceSessionId"]
                live_result = call("search_messages", {"source": "wechat4", "sourceSessionId": live_sid,
                    "query": "needle", "order": "desc"})
                self.assertEqual(live_result["total"], 61)
                live_more = call("search_messages", {"source": "wechat4", "sourceSessionId": live_sid,
                    "searchId": live_result["searchId"], "offset": 50})
                self.assertEqual(len(live_more["items"]), 11)
                focus_hit = live_more["items"][0]
                live_context = call("get_message_context", {"source": "wechat4", "sourceSessionId": live_sid,
                    "searchId": live_result["searchId"], "hitId": focus_hit["hitId"], "before": 2, "after": 2})
                self.assertEqual(live_context["items"][live_context["focusIndex"]]["content"], focus_hit["snippet"])
                self.assertEqual(live_context["focusIndex"], 2)
                call("disconnect")
                self.assertEqual(call("search_messages", {"source": "wechat4", "sourceSessionId": live_sid,
                    "searchId": live_result["searchId"]}, ok=False)["code"], "SOURCE_SESSION_CHANGED")
                call("shutdown")
                process.stdin.close()
                self.assertEqual(process.wait(timeout=15), 0)
                stderr = process.stderr.read().decode("utf-8", errors="replace")
                self.assertEqual(stderr, "")
                # Fixed progress labels do not disclose keywords, row contents,
                # account tokens or any fixture path.
                self.assertNotIn("needle", json.dumps(progress))
                self.assertNotIn(str(root), json.dumps(progress))
                self.assertGreater(len(progress), 0)
            finally:
                watchdog.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()


class ReconnectLifecycleTests(unittest.TestCase):
    def test_failed_reconnect_preserves_old_source_and_search_success_replaces_it(self):
        context = worker.Context(str(ROOT / "demo/conversations.json"))
        old_source = Mock()
        context.wechat = old_source
        worker._activate_source(context, "wechat4", force=True)
        sid = context.source_session_id
        failure = Mock()
        failure.connect.side_effect = SourceFailure("WECHAT_CONNECT_FAILED", "synthetic failure")
        with patch("wechat_memory.wechat4_source.WeChat4Source", return_value=failure):
            with self.assertRaises(worker.WorkerError):
                worker.handle_connect({"source": "wechat4"}, context)
        self.assertIs(context.wechat, old_source)
        self.assertEqual(context.source_session_id, sid)
        old_source.close.assert_not_called()
        failure.close.assert_called_once()
        candidate = Mock()
        candidate.connect.return_value = {"conversationCount": 1}
        candidate.capabilities = {"images": False, "voice": False}
        with patch("wechat_memory.wechat4_source.WeChat4Source", return_value=candidate):
            connected = worker.handle_connect({"source": "wechat4"}, context)
        self.assertIs(context.wechat, candidate)
        self.assertNotEqual(connected["sourceSessionId"], sid)
        old_source.close.assert_called_once()

    def test_invalid_connect_options_are_validated_before_old_source_is_destroyed(self):
        context = worker.Context("unused")
        old_source = Mock()
        context.wechat = old_source
        sid = context.source_session_id
        with self.assertRaises(worker.WorkerError):
            worker.handle_connect({"source": "wechat4", "enableVoice": "not a boolean"}, context)
        old_source.close.assert_not_called()
        self.assertIs(context.wechat, old_source)
        self.assertEqual(context.source_session_id, sid)


if __name__ == "__main__":
    unittest.main()
