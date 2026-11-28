"""Native-only synthetic card protocol; backend card serialization is independent."""
import json
import sys
from pathlib import Path

scenario, root_arg = sys.argv[1:3]
root = Path(root_arg)
assert root.is_dir()
stamp = "2026-11-25T12:00:00+08:00"
conversations = [
    {"id": "conv-1", "title": "合成会话", "messageCount": 4, "kind": "direct"},
    {"id": "conv-2", "title": "其他合成会话", "messageCount": 4, "kind": "direct"},
]


def cards():
    items = [
        {"id": "duplicate-id", "sender": "当前成员", "timestamp": stamp, "kind": "text",
         "content": "当前回复\n引用 原成员: 原始摘要", "displayKind": "quote", "replyTo": "reference-42",
         "quoteSummary": "原始摘要 <script>plain text</script>", "quoteSender": "原成员", "parseStatus": "ok"},
        {"id": "duplicate-id", "sender": "当前成员", "timestamp": stamp, "kind": "file",
         "content": "[聊天记录] 合成转发\n1. 内部成员: 子条目\n2. 嵌套记录\n[内容已截断]",
         "displayKind": "forward", "forwardItems": [
             {"sender": "内部成员", "timestamp": "合成时间", "content": "子条目 https://example.invalid/plain",
              "displayKind": "text", "forwardItems": []},
             {"sender": "内部成员", "content": "嵌套记录", "displayKind": "forward", "forwardItems": [
                 {"sender": None, "timestamp": "1730000000", "content": "[图片] C:/must-not-open.png", "displayKind": "media"},
             ]},
         ], "parseStatus": "partial", "truncated": True, "omittedCount": None},
        {"id": "relation-only", "sender": "当前成员", "timestamp": stamp, "kind": "text",
         "content": "仅保留关系的正文", "replyTo": "reference-42"},
        {"id": "old-message", "sender": "当前成员", "timestamp": stamp, "kind": "text", "content": "旧格式纯正文"},
    ]
    if scenario in {"absent", "null"}:
        for item in items:
            for key in ("displayKind", "replyTo", "quoteSummary", "quoteSender", "forwardItems", "parseStatus", "truncated", "omittedCount"):
                item.pop(key, None)
                if scenario == "null":
                    item[key] = None
    return items


def respond(req, result=None):
    print(json.dumps({"protocolVersion": 1, "requestId": req["requestId"], "type": "response", "ok": True,
                      "result": result or {}}, ensure_ascii=False), flush=True)


for line in sys.stdin:
    req = json.loads(line)
    command, payload = req["command"], req.get("payload", {})
    with (root / "requests.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"command": command, "payload": payload}, ensure_ascii=False) + "\n")
    if command == "hello":
        caps = ["global_message_search", "message_context"]
        if scenario != "legacy":
            caps.append("message_cursor_pagination")
        respond(req, {"protocolVersion": 1, "timezone": "UTC+08:00", "capabilities": caps, "sourceSessionId": "source-1"})
    elif command == "list_conversations":
        respond(req, {"items": conversations, "sourceSessionId": "source-1"})
    elif command == "get_messages_page":
        items = cards()
        if scenario == "invalid-refresh" and payload.get("startDate"):
            items[0]["omittedCount"] = -1
        respond(req, {"items": items, "total": 4, "previousCursor": None, "nextCursor": None,
                      "pageSessionId": "page-1", "sortVersion": "precise-v1"})
    elif command == "get_messages":
        respond(req, {"items": cards(), "total": 4, "nextOffset": None})
    elif command == "search_messages":
        hits = []
        for index, item in enumerate(cards()[:2]):
            hit = {key: value for key, value in item.items() if key not in {"content", "id"}}
            hit.update({"hitId": "hit-" + str(index), "conversationId": "conv-1", "conversationTitle": "合成会话",
                        "conversationKind": "direct", "messageId": item["id"], "snippet": "子条目命中摘要",
                        "matchedFields": ["content"]})
            hits.append(hit)
        respond(req, {"searchId": "search-1", "sourceSessionId": "source-1", "items": hits, "total": 2,
                      "resultLimit": 1000, "truncated": False, "nextOffset": None, "coverage": {"complete": True}})
    elif command == "get_message_context":
        items = cards()
        items[0]["attachmentPath"] = "C:/must-not-open.png"
        respond(req, {"conversation": conversations[0], "items": items, "focusIndex": int(payload["hitId"].split("-")[1]),
                      "hasEarlier": False, "hasLater": False})
    elif command == "export_markdown":
        output = Path(payload["outputPath"])
        assert output.parent == root
        output.write_text("Synthetic full-range canonical export", encoding="utf-8")
        respond(req, {"path": str(output), "messageCount": 4, "bytes": output.stat().st_size, "format": "markdown"})
    elif command in {"list_scenes", "list_jobs", "list_accounts"}:
        respond(req, {"items": []})
    elif command == "shutdown":
        respond(req)
        break
    else:
        respond(req)
