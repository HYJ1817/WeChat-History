# Vendored from wechat-ai-memory (MIT), commit 14d94db8b1affa12a2d7d88f0f830c1e0b8d752a.
# Copyright (c) 2026 WeChat AI Memory contributors. See LICENSES/wechat-ai-memory-LICENSE.txt.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageConfig:
    width: int = 1240
    height: int = 1754
    margin_x: int = 72
    header_height: int = 122
    footer_height: int = 58
    dpi: int = 300

    @property
    def scale(self) -> float:
        return self.dpi / 150

    @property
    def pixel_width(self) -> int:
        return round(self.width * self.scale)

    @property
    def pixel_height(self) -> int:
        return round(self.height * self.scale)

    @property
    def pixel_size(self) -> tuple[int, int]:
        return self.pixel_width, self.pixel_height

    def pixels(self, value: int | float) -> int:
        return round(value * self.scale)

    @property
    def content_top(self) -> int:
        return self.header_height

    @property
    def content_bottom(self) -> int:
        return self.height - self.footer_height

    @property
    def content_height(self) -> int:
        return self.content_bottom - self.content_top


@dataclass(frozen=True, slots=True)
class RenderTheme:
    page: str = "#F7F8FA"
    surface: str = "#FFFFFF"
    ink: str = "#17202A"
    muted: str = "#68737D"
    divider: str = "#DCE1E5"
    incoming: str = "#FFFFFF"
    outgoing: str = "#DFF4E5"
    accent: str = "#167B55"
    image_border: str = "#C9D0D5"
