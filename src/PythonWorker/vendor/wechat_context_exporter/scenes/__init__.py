# Vendored from wechat-ai-memory (MIT), commit 14d94db8b1affa12a2d7d88f0f830c1e0b8d752a.
# Copyright (c) 2026 WeChat AI Memory contributors. See LICENSES/wechat-ai-memory-LICENSE.txt.
"""Scene recipes: what to export, how to phrase it, who receives it."""

from .catalog import BUILTIN_BY_ID, BUILTIN_SCENES
from .models import SCENE_TARGETS, Scene, SceneError
from .prompt import CITATION_RULE, DEFAULT_TASK, render_prompt
from .store import SceneStore, default_scene_path

__all__ = [
    "BUILTIN_BY_ID",
    "BUILTIN_SCENES",
    "CITATION_RULE",
    "DEFAULT_TASK",
    "SCENE_TARGETS",
    "Scene",
    "SceneError",
    "SceneStore",
    "default_scene_path",
    "render_prompt",
]
