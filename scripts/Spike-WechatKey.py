#!/usr/bin/env python3
"""One-off, read-only validation of the upstream WeChat 4.x key capture.

This script deliberately prints no key material, pointer values, account ids,
paths, conversation names or message content. It writes only timings and
counts to artifacts/wechat-key-spike.json, for the migration report.

Running it restarts WeChat once (authorised for this validation). The key stays
in this process and is discarded when the process exits.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "reference" / "wechat-ai-memory" / "src"))

from wechat_context_exporter.sources.wechat4_crypto import DecryptedDatabaseCache  # noqa: E402
from wechat_context_exporter.sources.wechat4_discovery import discover_wechat4_accounts  # noqa: E402
from wechat_context_exporter.sources.wechat4_key_capture import capture_account_key  # noqa: E402


def _click_login_for(seconds: float) -> None:
    """Click the large 'enter WeChat' button while the login window is up.

    WeChat 4.2 ignores UI Automation Invoke on that button; a real mouse click
    works. This thread only runs for the validation and never reads text.
    """
    from pywinauto import Desktop

    deadline = time.monotonic() + seconds
    clicked = 0
    while time.monotonic() < deadline:
        try:
            output = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process Weixin -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
                capture_output=True, text=True, check=False,
            )
            for pid_text in output.stdout.split():
                if not pid_text.isdigit():
                    continue
                for window in Desktop(backend="uia").windows(process=int(pid_text), visible_only=True):
                    if "LoginWindow" not in window.class_name():
                        continue
                    buttons = [
                        button for button in window.descendants(control_type="Button")
                        if button.is_enabled() and button.rectangle().width() >= 140
                    ]
                    if not buttons:
                        continue
                    max(buttons, key=lambda button: button.rectangle().width()).click_input()
                    clicked += 1
        except Exception:
            pass
        time.sleep(1.5)
    print(f"login clicks: {clicked}", flush=True)


def main() -> int:
    result: dict[str, object] = {
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": "start",
    }
    raw_key = None
    cache = None
    started = time.monotonic()
    try:
        accounts = discover_wechat4_accounts()
        result["accountCount"] = len(accounts)
        if not accounts:
            raise RuntimeError("no accounts")
        account = accounts[0]
        result["messageDbCount"] = len(list((account.db_dir / "message").glob("message_[0-9]*.db")))
        result["stage"] = "capturing"
        clicker = threading.Thread(target=_click_login_for, args=(100.0,), daemon=True)
        clicker.start()
        raw_key = capture_account_key(
            account.account_dir,
            timeout=150.0,
            progress=lambda current, total, label: print(
                f"stage {current}/{total}", flush=True
            ),
        )
        result["captured"] = True
        result["keyBytes"] = len(raw_key)
        result["stage"] = "decrypting"
        cache = DecryptedDatabaseCache(account.db_dir, raw_key=raw_key)
        session_db = cache.get("session/session.db")
        import sqlite3

        with sqlite3.connect(f"file:{session_db}?mode=ro", uri=True) as connection:
            result["sessionRows"] = connection.execute(
                "SELECT COUNT(*) FROM SessionTable"
            ).fetchone()[0]
        message_files = sorted((account.db_dir / "message").glob("message_[0-9]*.db"))
        tables = 0
        for path in message_files:
            db = cache.get(f"message/{path.name}")
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
                tables += connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchone()[0]
        result["messageTables"] = tables
        result["stage"] = "done"
        result["ok"] = True
    except BaseException as error:  # noqa: BLE001 - report class only, never text
        result["stage"] = "failed"
        result["ok"] = False
        result["errorClass"] = type(error).__name__
        result["errorModule"] = type(error).__module__
        traceback.print_exc(file=sys.stderr)
    finally:
        raw_key = None
        try:
            if cache is not None:
                cache.close()
        except Exception:
            pass
        result["elapsedSeconds"] = round(time.monotonic() - started, 1)
        out = ROOT / "artifacts" / "wechat-key-spike.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
