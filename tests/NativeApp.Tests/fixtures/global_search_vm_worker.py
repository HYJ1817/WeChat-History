"""Synthetic, isolated stdio peer for the production MainViewModel tests."""
import json
import sys
import time
from pathlib import Path

scenario, root_arg = sys.argv[1:3]
root = Path(root_arg)
assert root.is_dir()
source = "demo"
source_session = "source-demo-1"
search_number = 0
import_number = 0
connect_number = 0
context_requests = 0
stamp = "2026-11-25T12:00:00+08:00"
conversations = [
    {"id": "conv-2", "title": "原会话", "kind": "direct", "messageCount": 2},
    {"id": "conv-1", "title": "合成群聊", "kind": "group", "messageCount": 60},
]


def respond(req, result=None, error=None):
    envelope = {"protocolVersion": 1, "requestId": req["requestId"], "type": "response", "ok": error is None}
    if error is None:
        envelope["result"] = result or {}
    else:
        envelope["error"] = {"code": error, "message": "合成测试拒绝：" + error, "retryable": False}
    print(json.dumps(envelope, ensure_ascii=False), flush=True)


def message(message_id, content="合成消息"):
    return {"id": message_id, "sender": "合成成员", "timestamp": stamp, "kind": "text", "content": content}


for line in sys.stdin:
    req = json.loads(line)
    command, payload = req["command"], req.get("payload", {})
    with (root / "requests.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"command": command, "payload": payload}, ensure_ascii=False) + "\n")
    if command == "hello":
        caps = [] if scenario == "legacy" else ["global_message_search", "message_context"]
        if scenario == "search-only":
            caps = ["global_message_search"]
        respond(req, {"protocolVersion": 1, "timezone": "UTC+08:00", "capabilities": caps,
                      "sourceSessionId": source_session if scenario != "no-token" else None})
    elif command == "list_accounts":
        respond(req, {"items": [{"token": "account-1", "label": "合成账户", "lastUsed": "", "recent": True}]})
    elif command == "connect":
        connect_number += 1
        if scenario == "connect-fail" or scenario == "reconnect-fail" and connect_number > 1:
            respond(req, error="WECHAT_KEY_UNAVAILABLE")
            continue
        source, source_session = "wechat4", "source-wechat4-1"
        respond(req, {"connected": True, "source": source, "sourceSessionId": source_session,
                      "accountLabel": "合成账户", "conversationCount": 2, "messageTableCount": 2,
                      "imageKeyAvailable": False, "voiceAvailable": False})
    elif command == "import_json":
        if Path(payload["path"]).name == "reject.json":
            respond(req, error="IMPORT_INVALID_JSON")
            continue
        import_number += 1
        source, source_session = "offline", "source-offline-" + str(import_number)
        respond(req, {"source": source, "schema": "wechat-memory-json-v1", "sourceSessionId": source_session,
                      "conversationCount": 2, "messageCount": 62})
    elif command == "list_conversations":
        if scenario == "hidden-list-fail" and context_requests and not payload.get("query"):
            respond(req, error="SEARCH_SOURCE_UNREADABLE")
            continue
        visible = [conversations[0]] if payload.get("query") else conversations
        respond(req, {"items": visible, "sourceSessionId": source_session if scenario != "no-token" else None})
    elif command == "get_messages":
        if scenario == "bad-browse" and context_requests:
            respond(req, {"items": None, "total": 2, "nextOffset": None})
            continue
        total = 60 if payload["conversationId"] == "conv-1" else 2
        offset = payload.get("offset", 0)
        end = min(total, offset + payload["limit"])
        respond(req, {"items": [message("ordinary-" + str(i), "普通会话正文") for i in range(offset, end)],
                      "total": total, "nextOffset": end if end < total else None})
    elif command == "search_messages":
        if scenario == "exit-search":
            sys.exit(17)
        if scenario == "pause-search" and payload.get("query") == "paused":
            print(json.dumps({"protocolVersion": 1, "requestId": req["requestId"], "type": "progress",
                              "current": 0, "total": 1, "label": "fixture_paused"}), flush=True)
            deadline = time.monotonic() + 10
            while not (root / "release").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
        if "searchId" in payload and scenario in {"expired-page", "changed-page", "source-session-changed"}:
            respond(req, error={"expired-page": "SEARCH_EXPIRED", "changed-page": "SEARCH_SOURCE_CHANGED",
                                "source-session-changed": "SOURCE_SESSION_CHANGED"}[scenario])
            continue
        if payload.get("query") == "soft-error":
            respond(req, error="SEARCH_BUDGET_EXCEEDED")
            continue
        if "searchId" not in payload:
            search_number += 1
        search_id = payload.get("searchId", "search-" + str(search_number))
        offset = payload.get("offset", 0)
        empty = payload.get("query") == "none"
        coverage = {"complete": False, "scannedMessages": 62, "partialMessages": 3, "truncatedMessages": 1,
                    "warnings": ["部分复杂消息未完整展开；搜索仅覆盖规范可读正文。"]}
        hit = {"hitId": "hit-" + str(offset + 1), "conversationId": "conv-1", "conversationTitle": "合成群聊",
               "conversationKind": "group", "messageId": "msg-" + str(offset + 1), "sender": "合成成员",
               "timestamp": stamp, "kind": "text", "snippet": "合成命中内容", "matchedFields": ["content"]}
        respond(req, {"searchId": search_id,
                      "sourceSessionId": "wrong-source" if payload.get("query") == "bad-first" else source_session,
                      "items": [] if empty else [hit], "total": 0 if empty else 2, "resultLimit": 1000,
                      "truncated": False, "nextOffset": 1 if not empty and offset == 0 else None, "coverage": coverage})
    elif command == "get_message_context":
        context_requests += 1
        if scenario == "expired-context":
            respond(req, error="CONTEXT_TARGET_CHANGED")
            continue
        target = message("msg-" + payload["hitId"].split("-")[-1], "合成命中内容")
        target["attachmentPath"] = "C:/must-not-be-opened.png"
        if scenario == "bad-focus":
            target["id"] = "wrong-message"
        respond(req, {"conversation": conversations[1], "items": [message("before"), target, message("after")],
                      "focusIndex": 1, "hasEarlier": True, "hasLater": True})
    elif command == "disconnect":
        source_session = "source-disconnected"
        respond(req, {"connected": False, "sourceSessionId": source_session})
    elif command in {"list_scenes", "list_jobs"}:
        respond(req, {"items": []})
    elif command.startswith("export_"):
        output = Path(payload["outputPath"])
        assert output.parent == root
        output.write_text("Synthetic complete export: 60 messages", encoding="utf-8")
        respond(req, {"path": str(output), "messageCount": 60, "bytes": output.stat().st_size, "format": "markdown"})
    elif command == "voice_status":
        respond(req, error="CAPABILITY_UNAVAILABLE")
    elif command == "shutdown":
        respond(req)
        break
    else:
        respond(req)
