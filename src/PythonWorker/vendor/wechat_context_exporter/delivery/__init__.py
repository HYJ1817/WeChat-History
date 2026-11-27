# Vendored from wechat-ai-memory (MIT), commit 14d94db8b1affa12a2d7d88f0f830c1e0b8d752a.
# Copyright (c) 2026 WeChat AI Memory contributors. See LICENSES/wechat-ai-memory-LICENSE.txt.
"""Delivery targets: where a finished job actually goes."""

from .agent import AgentDelivery
from .base import (
    DeliveryError,
    DeliveryRequest,
    DeliveryResult,
    DeliveryTarget,
    copy_artifacts,
    ordered_artifacts,
    paths_as_text,
    safe_filename,
)
from .clipboard import ClipboardDelivery, fallback_text
from .folder import FolderDelivery
from .obsidian import ObsidianDelivery

TARGETS = ("none", "folder", "obsidian", "clipboard", "agent")

__all__ = [
    "AgentDelivery",
    "ClipboardDelivery",
    "DeliveryError",
    "DeliveryRequest",
    "DeliveryResult",
    "DeliveryTarget",
    "FolderDelivery",
    "ObsidianDelivery",
    "TARGETS",
    "copy_artifacts",
    "fallback_text",
    "ordered_artifacts",
    "paths_as_text",
    "safe_filename",
]
