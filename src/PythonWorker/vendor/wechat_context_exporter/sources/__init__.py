# Vendored from wechat-ai-memory (MIT), commit 14d94db8b1affa12a2d7d88f0f830c1e0b8d752a.
# Copyright (c) 2026 WeChat AI Memory contributors. See LICENSES/wechat-ai-memory-LICENSE.txt.
"""Minimal source package slice used by the native worker."""

from .base import ChatSource, SourceError
from .wechat4_discovery import WeChat4Account, discover_wechat4_accounts, select_wechat4_account

__all__ = [
    "ChatSource",
    "SourceError",
    "WeChat4Account",
    "discover_wechat4_accounts",
    "select_wechat4_account",
]
