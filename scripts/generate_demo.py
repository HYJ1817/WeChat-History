#!/usr/bin/env python3
"""Regenerate demo/conversations.json.

Every name, message and timestamp below is fictional. The file follows the
version-1 JSON shape used by wechat-ai-memory (MIT). Timestamps are explicit
UTC+08:00 so no machine time zone can change filtering results.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "demo" / "conversations.json"
TZ = timezone(timedelta(hours=8))


def stamp(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ).isoformat()


def message(mid, sender, when, kind, content):
    return {"id": mid, "sender": sender, "timestamp": when, "type": kind,
            "content": content, "is_outgoing": False}


async_discussion = [
    message("m001", "林晓", stamp(2026, 11, 23, 9, 12), "text",
            "早上好 ☀️ 今天的议程：\n1. 原生客户端迁移\n2. 导出流程验收\n3. 风险清单"),
    message("m002", "Chen Yu", stamp(2026, 11, 23, 9, 18), "text",
            "Morning! I reviewed the WinUI prototype. The layout is clear, but we still need real export verification."),
    message("m003", "林晓", stamp(2026, 11, 23, 18, 40), "image",
            "【图片占位】白板上的迁移里程碑草图；演示数据不包含图片文件，未做媒体解析。"),
    message("m004", "阿宁", stamp(2026, 11, 23, 19, 5), "file",
            "【文件占位】demo-migration-plan.pdf；演示数据不包含实际文件。"),
    message("m005", "Chen Yu", stamp(2026, 11, 24, 0, 0), "text",
            "边界测试：这条消息正好在 24 日 00:00（UTC+08:00）。"),
    message("m006", "林晓", stamp(2026, 11, 24, 9, 30), "text",
            "日期筛选要以“包含所选日期”为准：\n开始日 00:00 包含，结束日次日 00:00 不包含。\n界面和导出必须一致 ✅"),
    message("m007", "阿宁", stamp(2026, 11, 24, 12, 5), "voice",
            "【语音占位】约 12 秒；演示数据不包含音频，也未进行转写。"),
    message("m008", "Chen Yu", stamp(2026, 11, 24, 23, 59, 59), "text",
            "End-of-day check: inclusive end date works ✅"),
    message("m009", "系统", stamp(2026, 11, 25, 0, 0), "system",
            "你已加入群聊「演示 · 产品讨论」。"),
    message("m010", "林晓", stamp(2026, 11, 25, 8, 15), "text",
            "新的一天。中文、English 和 Emoji 😀 都应该在导出里原样保留。"),
    message("m011", "阿宁", stamp(2026, 11, 25, 10, 42), "text",
            "多行消息示例：\n- 第一行\n- 第二行\n- 第三行"),
    message("m012", "Chen Yu", stamp(2026, 11, 25, 16, 20), "text",
            "No real WeChat data, keys, or media are used anywhere in this demo."),
]

standup = []
senders = ["小雨", "Leo", "演示助手"]
start = datetime(2026, 11, 20, 9, 0, tzinfo=TZ)
for index in range(1, 121):
    when = (start + timedelta(minutes=10 * (index - 1))).isoformat()
    content = f"第 {index} 条演示消息：站会进度同步。"
    if index % 17 == 0:
        content = f"第 {index} 条：English note & emoji 🚀"
    if index % 29 == 0:
        content = f"第 {index} 条多行消息：\n第一行\n第二行"
    standup.append(message(f"s{index:03d}", senders[(index - 1) % 3], when, "text", content))

payload = {
    "version": 1,
    "notice": "完全虚构的演示数据，与任何真实用户、聊天或数据库无关。",
    "conversations": [
        {"id": "demo-chat-001", "name": "演示 · 产品讨论（虚构）", "kind": "group",
         "messages": async_discussion},
        {"id": "demo-chat-002", "name": "演示 · 迭代站会（虚构）", "kind": "group",
         "messages": standup},
        {"id": "demo-chat-empty", "name": "演示 · 空会话（虚构）", "kind": "direct",
         "messages": []},
    ],
}

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"wrote {OUTPUT} ({len(async_discussion)}+{len(standup)} messages)")
