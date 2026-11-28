"""Synthetic stdio peer for Native B2 paging, lifecycle and full-range tests."""
import json
import sys
import time
from pathlib import Path

scenario, root_arg = sys.argv[1:3]
root = Path(root_arg)
assert root.is_dir()
source_session = "source-demo-1"
page_number = 0
import_number = 0
failed_once = False
sessions = {}
total = 2351
stamp = "2026-11-25T12:00:00.123456+08:00"
conversations = [
    {"id": "conv-1", "title": "合成会话一", "kind": "direct", "messageCount": total},
    {"id": "conv-2", "title": "合成会话二", "kind": "group", "messageCount": total},
]


def respond(req, result=None, error=None):
    envelope = {"protocolVersion": 1, "requestId": req["requestId"], "type": "response", "ok": error is None}
    if error is None:
        envelope["result"] = result or {}
    else:
        envelope["error"] = {"code": error, "message": "合成测试拒绝：" + error, "retryable": True}
    print(json.dumps(envelope, ensure_ascii=False), flush=True)


def message(i):
    return {"id": "m-" + str(i), "sender": "合成成员", "timestamp": stamp, "kind": "text", "content": "row-" + str(i)}


def pause(req):
    print(json.dumps({"protocolVersion": 1, "requestId": req["requestId"], "type": "progress",
                      "current": 0, "total": 1, "label": "fixture_paused"}), flush=True)
    deadline = time.monotonic() + 15
    while not (root / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.01)


for line in sys.stdin:
    req = json.loads(line)
    command, payload = req["command"], req.get("payload", {})
    with (root / "requests.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"command": command, "payload": payload}, ensure_ascii=False) + "\n")
    if command == "hello":
        caps = ["global_message_search", "message_context"]
        if scenario != "legacy":
            caps.append("message_cursor_pagination")
        if scenario.startswith("keyword"):
            caps.append("message_keyword_pagination")
        hello = {"protocolVersion": 1, "timezone": "UTC+08:00", "capabilities": caps,
                 "sourceSessionId": source_session if scenario != "no-token" else None}
        if scenario == "source-scoped":
            hello["sourceCapabilities"] = {"demo": [], "offline": [], "wechat4": ["message_cursor_pagination"]}
        elif scenario.startswith("keyword"):
            hello["sourceCapabilities"] = {"demo": ["message_cursor_pagination", "message_keyword_pagination"],
                                             "offline": [], "wechat4": []}
        respond(req, hello)
    elif command == "list_accounts":
        respond(req, {"items": [{"token": "synthetic-account", "label": "合成账户", "lastUsed": "", "recent": True}]})
    elif command == "connect":
        if scenario == "connect-fail":
            respond(req, error="WECHAT_KEY_UNAVAILABLE")
            continue
        source_session = "source-wechat4-1"
        sessions.clear()
        respond(req, {"connected": True, "source": "wechat4", "sourceSessionId": source_session,
                      "accountLabel": "合成账户", "conversationCount": 2, "messageTableCount": 2,
                      "imageKeyAvailable": False, "voiceAvailable": True})
    elif command == "import_json":
        if Path(payload["path"]).name == "reject.json":
            respond(req, error="IMPORT_INVALID_JSON")
            continue
        import_number += 1
        source_session = "source-offline-" + str(import_number)
        sessions.clear()
        respond(req, {"source": "offline", "schema": "wechat-memory-json-v1", "sourceSessionId": source_session,
                      "conversationCount": 2, "messageCount": total * 2})
    elif command == "list_conversations":
        respond(req, {"items": conversations, "sourceSessionId": source_session if scenario != "no-token" else None})
    elif command == "get_messages_page":
        continuation = "pageSessionId" in payload
        expected = {"source", "sourceSessionId", "pageSessionId", "cursor", "limit"} if continuation else {
            "source", "sourceSessionId", "conversationId", "startDate", "endDate", "query", "order", "limit"}
        if set(payload) != expected or payload["limit"] != 50:
            respond(req, error="INVALID_ARGUMENT")
            continue
        if payload["sourceSessionId"] != source_session:
            respond(req, error="SOURCE_SESSION_CHANGED")
            continue
        if scenario.startswith("keyword") and (
                payload.get("query") is not None or
                (continuation and str(payload.get("pageSessionId", "")).startswith("keyword-"))):
            keyword_total, candidate_count = 123, 400
            projection = "canonical-appmsg-v1"
            if continuation:
                session = payload["pageSessionId"]
                if not session.startswith("keyword-"):
                    respond(req, error="PAGE_EXPIRED")
                    continue
                parts = payload["cursor"].split(":")
                if len(parts) != 3 or parts[1] != session:
                    respond(req, error="PAGE_EXPIRED")
                    continue
                if scenario == "keyword-expired":
                    respond(req, error="PAGE_EXPIRED")
                    continue
                if scenario == "keyword-budget":
                    respond(req, error="SEARCH_BUDGET_EXCEEDED")
                    continue
                boundary = int(parts[2])
                start = max(0, boundary - 50) if parts[0] == "p" else boundary
            else:
                session = "keyword-" + str(page_number + 1)
                sessions[session] = payload["conversationId"]
                start = 0
            end = min(keyword_total, start + 50)
            result = {"pageSessionId": session, "sourceSessionId": source_session, "mode": "keyword",
                      "sortVersion": "precise-v1", "projectionVersion": projection, "order": payload.get("order", "desc"),
                      "total": keyword_total, "candidateCount": candidate_count,
                      "previousCursor": f"p:{session}:{start}" if start else None,
                      "nextCursor": f"n:{session}:{end}" if end < keyword_total else None,
                      "items": [{**message(i), "content": "needle row-" + str(i)} for i in range(start, end)]}
            if scenario == "keyword-response":
                result["projectionVersion"] = "changed-projection"
            respond(req, result)
            continue
        if scenario in {"schema", "unverifiable", "unsupported-worker"}:
            respond(req, error={"schema": "PAGE_UNSUPPORTED_SCHEMA", "unverifiable": "PAGE_SOURCE_UNVERIFIABLE",
                                "unsupported-worker": "CAPABILITY_UNAVAILABLE"}[scenario])
            continue
        if continuation:
            if scenario == "budget-controlled" and (root / "reject-page").exists() and not failed_once:
                failed_once = True
                respond(req, error="PAGE_BUDGET_EXCEEDED")
                continue
            if scenario == "exit-next":
                sys.exit(17)
            if scenario in {"expired-next", "changed-next", "source-session-changed", "schema-next"} and not failed_once:
                failed_once = True
                respond(req, error={"expired-next": "PAGE_EXPIRED", "changed-next": "PAGE_SOURCE_CHANGED",
                                    "source-session-changed": "SOURCE_SESSION_CHANGED", "schema-next": "PAGE_UNSUPPORTED_SCHEMA"}[scenario])
                continue
            if scenario == "pause-next":
                pause(req)
            session = payload["pageSessionId"]
            if session not in sessions:
                respond(req, error="PAGE_EXPIRED")
                continue
            parts = payload["cursor"].split(":")
            if len(parts) != 3 or parts[1] != session:
                respond(req, error="PAGE_EXPIRED")
                continue
            boundary = int(parts[2])
            start = max(0, boundary - 50) if parts[0] == "p" else boundary
        else:
            if payload["query"] is not None or payload["order"] != "desc":
                respond(req, error="INVALID_ARGUMENT")
                continue
            page_number += 1
            if scenario == "pause-first" and page_number > 1:
                pause(req)
            session = "page-" + str(page_number)
            sessions[session] = payload["conversationId"]
            start = 0
        end = min(total, start + 50)
        result = {"pageSessionId": session, "sortVersion": "precise-v1", "total": total,
                  "previousCursor": f"p:{session}:{start}" if start else None,
                  "nextCursor": f"n:{session}:{end}" if end < total else None,
                  "items": [message(i) for i in range(start, end)]}
        if scenario == "wrong-page" and continuation:
            result["pageSessionId"] = "different-page-session"
        if scenario == "bad-first" and page_number > 1:
            result["previousCursor"] = "unexpected"
        respond(req, result)
    elif command == "get_messages":
        offset = payload["offset"]
        end = min(total, offset + payload["limit"])
        respond(req, {"items": [message(i) for i in range(offset, end)], "total": total,
                      "nextOffset": end if end < total else None})
    elif command == "search_messages":
        hit = {"hitId": "hit-1", "conversationId": "conv-2", "conversationTitle": conversations[1]["title"],
               "conversationKind": "group", "messageId": "m-5", "sender": "合成成员", "timestamp": stamp,
               "kind": "text", "snippet": "needle", "matchedFields": ["content"]}
        respond(req, {"searchId": "search-1", "sourceSessionId": source_session, "items": [hit], "total": 1,
                      "resultLimit": 1000, "truncated": False, "nextOffset": None, "coverage": {"complete": True}})
    elif command == "get_message_context":
        respond(req, {"conversation": conversations[1], "items": [message(4), message(5), message(6)],
                      "focusIndex": 1, "hasEarlier": True, "hasLater": True})
    elif command in {"list_scenes", "list_jobs"}:
        respond(req, {"items": []})
    elif command == "export_job":
        respond(req, {"job": {"jobId": "synthetic-job", "status": "ready", "messageCount": total,
                              "directory": str(root)}})
    elif command.startswith("export_"):
        output = Path(payload["outputPath"])
        assert output.parent == root
        output.write_text("Synthetic complete range: " + str(total), encoding="utf-8")
        respond(req, {"path": str(output), "messageCount": total, "bytes": output.stat().st_size, "format": "markdown"})
    elif command == "transcribe_messages":
        respond(req, {"total": total, "transcribed": total, "cached": 0, "failed": 0})
    elif command == "voice_status":
        respond(req, error="CAPABILITY_UNAVAILABLE")
    elif command == "shutdown":
        respond(req)
        break
    elif command == "disconnect":
        sessions.clear()
        source_session = "source-disconnected"
        respond(req, {"connected": False})
    else:
        respond(req)
