# Vendored from wechat-ai-memory (MIT), commit 14d94db8b1affa12a2d7d88f0f830c1e0b8d752a.
# Copyright (c) 2026 WeChat AI Memory contributors. See LICENSES/wechat-ai-memory-LICENSE.txt.
"""Write the archive into a plain folder, one folder per conversation per day."""

from __future__ import annotations

from pathlib import Path

from .base import (
    DeliveryError,
    DeliveryRequest,
    DeliveryResult,
    copy_artifacts,
    safe_filename,
)

PROMPT_FILENAME = "给 Agent 的提示词.txt"


class FolderDelivery:
    """The simplest target: a directory the user already browses.

    Naming follows ``2026-11-15 生态项目组 (20261115T230755-a1b2c3)/`` so
    exports of the same group line up chronologically, while each job keeps
    its own directory.  Date and conversation alone are not unique: two
    exports of the same conversation on one day would otherwise leave the
    first job's files next to the second job's, and same-name formats would be
    overwritten silently.
    """

    name = "folder"

    def __init__(self, root: Path | str, *, organize_by_date: bool = True) -> None:
        self.root = Path(root).expanduser().resolve()
        self.organize_by_date = organize_by_date

    def destination_for(self, request: DeliveryRequest) -> Path:
        conversation = safe_filename(request.conversation)
        job_id = safe_filename(request.job.job_id, fallback="job")
        name = f"{conversation} ({job_id})"
        if not self.organize_by_date:
            return self.root / name
        stamp = request.job.manifest.created_at.strftime("%Y-%m-%d")
        return self.root / f"{stamp} {name}"

    def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        destination = self.destination_for(request)
        try:
            copied = copy_artifacts(request.job, destination)
            if request.prompt:
                (destination / PROMPT_FILENAME).write_text(request.prompt, encoding="utf-8")
        except OSError as exc:
            raise DeliveryError(f"cannot write to {destination}: {exc}") from exc

        detail = f"{len(copied)} 个文件"
        if request.prompt:
            detail += f" + {PROMPT_FILENAME}"
        return DeliveryResult(
            target=self.name,
            status="delivered",
            destination=str(destination),
            detail=detail,
        )
