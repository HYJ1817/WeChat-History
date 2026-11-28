"""Task records, scene templates, and file delivery for WeChat Memory.

A task is one export run: an immutable manifest describing what was selected, a
mutable state file describing where it was delivered, and the produced files
under ``files/``.  Scenes are local prompt templates only -- this module never
calls a model, the network, or the clipboard.

Privacy boundaries:

* the jobs root is supplied by the caller and must be a private directory, never
  the source tree or the upstream application's data directory;
* the upstream ``JobStore.log`` is disabled here because its lines can contain
  conversation names and destinations;
* callers never receive exception text, paths, or message content through an
  error message: every ``TaskError`` message is a fixed Chinese sentence;
* previews are omitted from history summaries and from ``get_job`` unless the
  caller explicitly asks for them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .safe_delete import ProtectedTree, SafeDeleteError

_VENDOR_DIR = Path(__file__).resolve().parents[1] / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

from wechat_context_exporter.delivery import (  # noqa: E402
    DeliveryError,
    DeliveryRequest,
    DeliveryResult,
    FolderDelivery,
    ObsidianDelivery,
    copy_artifacts,
)
from wechat_context_exporter.jobs import (  # noqa: E402
    JobError,
    JobManifest,
    JobRecord,
    JobStatus,
    JobStore,
    MessageRef,
    collect_artifacts,
    format_utc,
    message_preview,
    new_job_id,
    utc_now,
)
from wechat_context_exporter.jobs.models import (  # noqa: E402
    JOB_SCHEMA_VERSION,
    JobState,
    as_utc,
    parse_utc,
)
from wechat_context_exporter.scenes import (  # noqa: E402
    BUILTIN_BY_ID,
    Scene,
    SceneError,
    SceneStore,
    render_prompt,
)

__all__ = ["TaskError", "TaskService"]

_PROMPT_FILENAME = "prompt.txt"
_FORMATS = ("markdown", "json", "pdf")
_DEFAULT_OBSIDIAN_FOLDER = "微信记忆"
_DEFAULT_OBSIDIAN_ATTACHMENTS = "_附件"
_MAX_LIST_LIMIT = 500
_MAX_ERROR_LENGTH = 200
_MAX_PRUNE_METADATA_BYTES = 4 * 1024 * 1024
_PRUNE_SNAPSHOT_VERSION = 1
_PRUNE_BUCKETS = ("Ready", "Failed")
# Do not use FINISHED_STATUSES: FAILED is recoverable, not disposable.
_PRUNABLE_STATUSES = frozenset({JobStatus.DELIVERED, JobStatus.COPIED, JobStatus.EXPIRED})

_JOB_ID_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
_SCENE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")

# Fixed, caller-safe messages.  Nothing below interpolates a path, a job id, a
# conversation name, a prompt, or exception text.
_INVALID_ARGUMENT_MESSAGE = "参数无效。"
_JOBS_ROOT_MESSAGE = "任务目录必须是独立的私有目录。"
_SCENES_FILE_MESSAGE = "场景文件不可用。"
_SCENE_INVALID_MESSAGE = "场景定义无效。"
_SCENE_ID_MESSAGE = "场景标识无效。"
_SCENE_NOT_FOUND_MESSAGE = "找不到指定的场景。"
_TARGET_INVALID_MESSAGE = "投递目标无效。"
_TARGET_NOT_SUPPORTED_MESSAGE = "不支持该投递目标。"
_TARGET_PATH_MESSAGE = "目标路径无效或不存在。"
_EXPORT_FAILED_MESSAGE = "导出失败，任务已中止。"
_STORE_UNAVAILABLE_MESSAGE = "任务目录不可用。"
_JOB_NOT_FOUND_MESSAGE = "找不到指定的任务。"
_JOB_NOT_READY_MESSAGE = "任务当前状态不可投递。"
_JOB_PATH_UNSAFE_MESSAGE = "任务路径不安全。"
_ARTIFACT_NOT_FOUND_MESSAGE = "找不到指定的任务文件。"
_DELIVERY_FAILED_MESSAGE = "投递失败，任务已保留，可稍后重试。"
_NOTE_EXISTS_MESSAGE = "目标笔记已存在，请选择其他 vault 或先移动旧笔记。"
_DELIVERY_STATUS_MESSAGE = "投递状态无效。"
_RECOVERED_MESSAGE = "任务在完成前被中断。"
_GENERIC_JOB_ERROR_MESSAGE = "任务执行失败。"
_SCENES_WARNING = "部分自定义场景无法读取，已忽略。"
_JOBS_WARNING = "部分任务无法读取，已跳过。"


class TaskError(Exception):
    """An expected failure with a stable code and a safe Chinese message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _QuietJobStore(JobStore):
    """JobStore without the upstream text log.

    ``begin``/``commit``/``abort`` call ``log`` internally and the upstream log
    line includes the conversation name.  A no-op override keeps the privacy
    promise while leaving every other behaviour untouched.
    """

    def log(self, job_id: str, message: str) -> None:  # noqa: D401 - intentional no-op
        return None


class _NoteExistsError(DeliveryError):
    """Internal marker for the no-overwrite rule."""


class _SafeObsidianDelivery(ObsidianDelivery):
    """Obsidian delivery that never replaces an existing note.

    With ``dedupe`` the colliding note gets ``-<job_id>`` appended to its stem;
    without it the delivery fails and the old note is left untouched.
    """

    def __init__(
        self,
        vault: Path | str,
        *,
        folder: str,
        attachments: str,
        dedupe: bool = False,
    ) -> None:
        super().__init__(vault, folder=folder, attachments=attachments)
        self._dedupe = dedupe

    def destination_for(self, request: DeliveryRequest) -> Path:
        note_path = super().destination_for(request)
        if self._dedupe and note_path.exists():
            return note_path.with_name(f"{note_path.stem}-{request.job.job_id}{note_path.suffix}")
        return note_path

    def deliver(self, request: DeliveryRequest) -> Any:
        note_path = self.destination_for(request)
        if note_path.exists():
            raise _NoteExistsError(_NOTE_EXISTS_MESSAGE)
        attachment_dir = self.vault / self.folder / self.attachments / request.job.job_id
        try:
            note_path.parent.mkdir(parents=True, exist_ok=True)
            content = self._render(request, note_path, attachment_dir)
            copy_artifacts(request.job, attachment_dir)
            # Exclusive creation closes the check-then-write race: another
            # instance may create the note between exists() and write_text().
            with note_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        except FileExistsError:
            raise _NoteExistsError(_NOTE_EXISTS_MESSAGE) from None
        except OSError as exc:
            raise DeliveryError(f"cannot write note to {note_path}: {exc}") from exc
        return DeliveryResult(
            target=self.name,
            status="delivered",
            destination=str(note_path),
            detail=f"附件在 {attachment_dir.name}/",
        )


@dataclass(frozen=True)
class _TargetSpec:
    kind: str
    folder_root: Path | None = None
    organize_by_date: bool = True
    vault: Path | None = None
    folder: str = _DEFAULT_OBSIDIAN_FOLDER
    attachments: str = _DEFAULT_OBSIDIAN_ATTACHMENTS
    dedupe: bool = False


def _absolute_path(value: Any) -> Path:
    """Validate an absolute, local path without exposing it in an error."""
    if not isinstance(value, (str, Path)):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    if isinstance(value, str) and not value.strip():
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, OSError, RuntimeError):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE) from None
    if not path.is_absolute():
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE) from None


def _is_within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
        return resolved == base or resolved.is_relative_to(base)
    except (OSError, RuntimeError, ValueError):
        return False


def _upstream_default_root() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    try:
        return (Path(local) / "WeChatAIMemory").resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _safe_error_text(value: Any) -> str | None:
    """Return a short error string only when it cannot carry a path or a line break.

    Jobs written by this module already contain fixed sentences; the extra check
    keeps a hand-edited or upstream-written state file from echoing a path into
    the UI through a history summary.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    if any(token in value for token in ("/", "\\", "\n", "\r", "\x00", ":")):
        return _GENERIC_JOB_ERROR_MESSAGE
    return value[:_MAX_ERROR_LENGTH]


def _scene_payload(scene: Scene) -> dict[str, Any]:
    payload = scene.to_dict()
    payload["builtin"] = bool(scene.builtin)
    return payload


def _validate_scene_id(scene_id: Any) -> str:
    if not isinstance(scene_id, str) or not _SCENE_ID_PATTERN.match(scene_id):
        raise TaskError("SCENE_INVALID", _SCENE_ID_MESSAGE)
    return scene_id


def _validate_job_id(job_id: Any) -> str:
    if not isinstance(job_id, str) or not _JOB_ID_PATTERN.match(job_id):
        raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
    return job_id


def _conversation_identity(conversation: Any) -> tuple[str, str]:
    if isinstance(conversation, Mapping):
        conversation_id = conversation.get("id")
        conversation_name = conversation.get("name")
    else:
        conversation_id = getattr(conversation, "id", None)
        conversation_name = getattr(conversation, "name", None)
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    if not isinstance(conversation_name, str) or not conversation_name.strip():
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    return conversation_id, conversation_name


def _normalize_formats(formats: Any) -> list[str]:
    if isinstance(formats, (str, bytes)) or not isinstance(formats, (list, tuple)):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    normalized: list[str] = []
    seen: set[str] = set()
    for item in formats:
        if not isinstance(item, str) or item not in _FORMATS:
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    if not normalized:
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    return normalized


def _normalize_messages(messages: Any) -> list[Any]:
    if isinstance(messages, (str, bytes)) or not isinstance(messages, Iterable):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    try:
        return list(messages)
    except TypeError:
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE) from None


def _preview_text(message: Any) -> str:
    """Placeholder-aware preview so an image/file path never becomes a preview."""
    message_type = getattr(message, "type", None)
    kind = getattr(message_type, "value", message_type)
    content = getattr(message, "content", "")
    if not isinstance(content, str):
        content = ""
    if kind == "image":
        return "[图片]"
    if kind == "file":
        name = Path(content).name if content else ""
        return f"[文件] {name}".strip()
    if kind == "voice":
        transcript = getattr(message, "transcript", "") or ""
        return f"[语音] {transcript}".strip()
    return content


def _message_ref(message: Any) -> MessageRef:
    message_id = getattr(message, "id", None)
    sender = getattr(message, "sender", None)
    timestamp = getattr(message, "timestamp", None)
    if not isinstance(message_id, str) or not message_id.strip():
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    if not isinstance(sender, str):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    if not isinstance(timestamp, datetime):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    try:
        return MessageRef(
            id=message_id,
            timestamp=timestamp,
            sender=sender,
            preview=message_preview(_preview_text(message)),
        )
    except (JobError, ValueError):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE) from None


def _normalize_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    return as_utc(value)


def _normalize_query(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    return value


def _normalize_notes(notes: Any) -> list[str]:
    if notes is None:
        return []
    if isinstance(notes, str) or not isinstance(notes, (list, tuple)):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    result: list[str] = []
    for note in notes:
        if not isinstance(note, str):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        result.append(note)
    return result


def _coerce_scene(scene: Any) -> Scene | None:
    if scene is None:
        return None
    if isinstance(scene, Scene):
        return scene
    if isinstance(scene, Mapping):
        try:
            return Scene.from_dict(dict(scene))
        except (SceneError, TypeError, ValueError):
            raise TaskError("SCENE_INVALID", _SCENE_INVALID_MESSAGE) from None
    raise TaskError("SCENE_INVALID", _SCENE_INVALID_MESSAGE)


def _safe_subfolder(value: Any, default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    text = value.strip()
    if not text or text in {".", ".."}:
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    if any(character in text for character in ("/", "\\", ":", "<", ">", '"', "|", "?", "*")):
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    if any(ord(character) < 32 for character in text):
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    if text != Path(text).name or Path(text).is_absolute():
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    if text.rstrip(" .") != text:
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    return text


def _prepare_target(target: Any, *, allowed: frozenset[str]) -> _TargetSpec:
    if not isinstance(target, dict):
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    kind = target.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
    if kind not in allowed:
        raise TaskError("TARGET_NOT_SUPPORTED", _TARGET_NOT_SUPPORTED_MESSAGE)

    if kind in {"none", "clipboard", "agent"}:
        return _TargetSpec(kind=kind)

    if kind == "folder":
        if "root" not in target:
            raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
        root = _absolute_path(target.get("root"))
        if root.exists():
            if not root.is_dir():
                raise TaskError("INVALID_ARGUMENT", _TARGET_PATH_MESSAGE)
        elif not root.parent.is_dir():
            raise TaskError("INVALID_ARGUMENT", _TARGET_PATH_MESSAGE)
        organize_by_date = target.get(
            "organizeByDate", target.get("organize_by_date", True)
        )
        if not isinstance(organize_by_date, bool):
            raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
        return _TargetSpec(
            kind="folder",
            folder_root=root,
            organize_by_date=organize_by_date,
        )

    if kind == "obsidian":
        vault_raw = target.get("vault")
        if not isinstance(vault_raw, (str, Path)):
            raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
        vault = _absolute_path(vault_raw)
        if not vault.is_dir():
            raise TaskError("INVALID_ARGUMENT", _TARGET_PATH_MESSAGE)
        folder = _safe_subfolder(
            target.get("folder", _DEFAULT_OBSIDIAN_FOLDER),
            _DEFAULT_OBSIDIAN_FOLDER,
        )
        attachments = _safe_subfolder(
            target.get("attachments", _DEFAULT_OBSIDIAN_ATTACHMENTS),
            _DEFAULT_OBSIDIAN_ATTACHMENTS,
        )
        dedupe = target.get("dedupe", False)
        if not isinstance(dedupe, bool):
            raise TaskError("INVALID_ARGUMENT", _TARGET_INVALID_MESSAGE)
        return _TargetSpec(
            kind="obsidian",
            vault=vault,
            folder=folder,
            attachments=attachments,
            dedupe=dedupe,
        )

    raise TaskError("TARGET_NOT_SUPPORTED", _TARGET_NOT_SUPPORTED_MESSAGE)


def _check_exported_paths(raw_paths: Any, files_dir: Path) -> None:
    """Every returned path must be an existing file inside ``files_dir``."""
    if isinstance(raw_paths, (str, bytes)) or raw_paths is None:
        raise ValueError("invalid exported path list")
    try:
        returned = list(raw_paths)
    except TypeError:
        raise ValueError("invalid exported path list") from None
    try:
        base = files_dir.resolve()
    except (OSError, RuntimeError, ValueError):
        raise ValueError("unreadable files directory") from None
    for item in returned:
        if not isinstance(item, (str, Path)):
            raise ValueError("invalid exported path")
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = files_dir / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise ValueError("missing exported path") from None
        if not resolved.is_file() or not resolved.is_relative_to(base):
            raise ValueError("exported path outside the job")


def _artifact_payload(record: JobRecord) -> list[dict[str, Any]]:
    return [
        {
            "name": artifact.name,
            "kind": artifact.kind,
            "size": artifact.size,
            "sha256": artifact.sha256,
        }
        for artifact in record.manifest.artifacts
    ]


def _job_summary(record: JobRecord, *, include_previews: bool = False) -> dict[str, Any]:
    manifest = record.manifest
    state = record.state
    summary: dict[str, Any] = {
        "jobId": manifest.job_id,
        "status": state.status.value,
        "createdAt": format_utc(manifest.created_at),
        "updatedAt": format_utc(state.updated_at),
        "conversationId": manifest.conversation_id,
        "conversationName": manifest.conversation_name,
        "messageCount": manifest.message_count,
        "sceneId": manifest.scene_id,
        "sceneName": manifest.scene_name,
        "target": state.target,
        "destination": state.destination,
        "detail": state.detail,
        "error": _safe_error_text(state.error),
        "attempts": state.attempts,
        "start": format_utc(manifest.start) if manifest.start else None,
        "end": format_utc(manifest.end) if manifest.end else None,
        "artifacts": _artifact_payload(record),
        "directory": str(record.directory),
    }
    if include_previews:
        first = manifest.first_message.to_dict() if manifest.first_message else None
        last = manifest.last_message.to_dict() if manifest.last_message else None
        summary["firstMessage"] = first
        summary["lastMessage"] = last
        summary["reference"] = {"first": first, "last": last}
    return summary


class _PruneProtected(Exception):
    """A fixed reason code; never includes filenames or exception text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _prune_cutoff(days: int) -> datetime:
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
    try:
        # Use exactly the precision sent in the snapshot, including at execution.
        return parse_utc(format_utc(utc_now() - timedelta(days=days)))
    except (OverflowError, ValueError):
        raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE) from None


def _prune_stat(path: Path, protection: ProtectedTree | None = None) -> os.stat_result:
    """Preview checks paths; final execution checks retained, protected handles."""
    if protection is not None:
        return protection.stat(path)
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
            or path.resolve(strict=True) != path
        ):
            raise _PruneProtected("unsafe_path")
        return info
    except (OSError, RuntimeError, ValueError):
        raise _PruneProtected("unreadable") from None


def _prune_stat_key(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _prune_file_digest(
    path: Path, before: os.stat_result, protection: ProtectedTree | None = None,
) -> str:
    """Hash contents; execution retains deny-write handles until deletion ends."""
    digest = hashlib.sha256()
    key = _prune_stat_key(before)
    with protection.open_file(path) if protection is not None else path.open("rb") as handle:
        # A link/path replacement between lstat and open must not cause reads
        # from the replacement. Check the opened handle before reading bytes.
        if _prune_stat_key(os.fstat(handle.fileno())) != key:
            raise _PruneProtected("changed")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        if _prune_stat_key(os.fstat(handle.fileno())) != key:
            raise _PruneProtected("changed")
    return digest.hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _validate_prune_metadata(manifest: dict, state: dict, job_id: str) -> None:
    """Validate v1 JSON types *before* the vendored reader can coerce/default.

    Viewing old records is deliberately tolerant; permanent deletion is not.
    Optional v1 blocks/fields may be absent, but a supplied value must obey its
    contract. Unknown extra fields remain forward-compatible within v1.
    """
    def require(condition: bool) -> None:
        if not condition:
            raise _PruneProtected("unreadable_metadata")

    def text(value: Any, *, nonempty: bool = False) -> None:
        require(type(value) is str and (not nonempty or bool(value.strip())))

    def count(value: Any) -> None:
        require(type(value) is int and value >= 0)

    def optional_text(payload: dict, name: str, *, nullable: bool = True) -> None:
        if name in payload and (payload[name] is not None or not nullable):
            text(payload[name])

    def timestamp(value: Any) -> None:
        text(value, nonempty=True)
        parse_utc(value)  # Includes UTC normalization; the read boundary catches overflow.

    def block(payload: dict, name: str, *, required: bool = False) -> dict:
        value = payload[name] if required else payload.get(name, {})
        require(type(value) is dict)
        return value

    text(manifest.get("job_id"), nonempty=True)
    text(state.get("job_id"), nonempty=True)
    require(manifest["job_id"] == job_id and state["job_id"] == job_id)
    timestamp(manifest.get("created_at"))
    timestamp(state.get("updated_at"))
    text(state.get("status"), nonempty=True)
    count(manifest.get("message_count"))
    count(state.get("attempts"))
    source = block(manifest, "source", required=True)
    text(source.get("kind"), nonempty=True)
    optional_text(source, "label", nullable=False)
    optional_text(source, "wechat_version")
    conversation = block(manifest, "conversation", required=True)
    for name in ("id", "name"):
        text(conversation.get(name), nonempty=True)
    window = block(manifest, "window")
    for name in ("start", "end"):
        if window.get(name) is not None:
            timestamp(window[name])
    optional_text(window, "query")
    scene = block(manifest, "scene")
    for name in ("id", "name"):
        optional_text(scene, name)
    reference = block(manifest, "reference")
    for name in ("first", "last"):
        value = reference.get(name)
        if value is not None:
            require(type(value) is dict)
            text(value.get("id"), nonempty=True)
            timestamp(value.get("timestamp"))
            optional_text(value, "sender", nullable=False)
            optional_text(value, "preview", nullable=False)
    artifacts = manifest.get("artifacts")
    require(type(artifacts) is list)
    for artifact in artifacts:
        require(type(artifact) is dict)
        for name in ("name", "sha256"):
            text(artifact.get(name), nonempty=True)
        optional_text(artifact, "kind", nullable=False)
        count(artifact.get("size"))
    notes = manifest.get("notes", [])
    require(type(notes) is list)
    for note in notes:
        text(note)
    for name in ("target", "destination", "error"):
        optional_text(state, name)
    optional_text(state, "detail", nullable=False)


def _prune_metadata(
    directory: Path, protection: ProtectedTree | None = None,
) -> tuple[JobRecord, bytes, bytes]:
    """Strict reading for deletion, without JobStore's tolerant/default state."""
    raw: list[bytes] = []
    payloads: list[dict[str, Any]] = []
    try:
        for name in ("manifest.json", "state.json"):
            path = directory / name
            before = _prune_stat(path, protection)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_PRUNE_METADATA_BYTES:
                raise _PruneProtected("unreadable_metadata")
            with protection.open_file(path) if protection is not None else path.open("rb") as handle:
                if _prune_stat_key(os.fstat(handle.fileno())) != _prune_stat_key(before):
                    raise _PruneProtected("changed")
                data = handle.read(_MAX_PRUNE_METADATA_BYTES + 1)
                if _prune_stat_key(os.fstat(handle.fileno())) != _prune_stat_key(before):
                    raise _PruneProtected("changed")
            if len(data) > _MAX_PRUNE_METADATA_BYTES:
                raise _PruneProtected("unreadable_metadata")
            if _prune_stat_key(before) != _prune_stat_key(_prune_stat(path, protection)):
                raise _PruneProtected("changed")
            payload = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, dict):
                raise _PruneProtected("unreadable_metadata")
            raw.append(data)
            payloads.append(payload)
        manifest, state = payloads
        # The vendored manifest reader accepts future versions and a missing
        # version; that is appropriate for viewing, never for deletion.
        version = manifest.get("schema_version")
        state_version = state.get("schema_version", JOB_SCHEMA_VERSION)
        if (
            type(version) is not int or version != JOB_SCHEMA_VERSION
            or type(state_version) is not int or state_version != JOB_SCHEMA_VERSION
        ):
            raise _PruneProtected("unsupported_schema")
        _validate_prune_metadata(manifest, state, directory.name)
        record = JobRecord(
            manifest=JobManifest.from_dict(manifest),
            state=JobState.from_dict(state),
            directory=directory,
        )
        return record, raw[0], raw[1]
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise _PruneProtected("unreadable_metadata") from None


def _prune_tree(
    directory: Path, cutoff: datetime, protection: ProtectedTree | None = None,
) -> tuple[str, int]:
    """Check the *whole* structure/recency cheaply before hashing any artifact.

    Execution acquires and retains every directory/file handle in this first
    pass, so no file already hashed can be rewritten while hashing later files.
    The preview is read-only/best-effort, never a substitute for this protected
    execution check. Empty dirs, attachments, prompt and metadata all matter.
    """
    pending = [directory]
    observed: list[tuple[Path, os.stat_result]] = []
    digest = hashlib.sha256()
    artifact_bytes = 0
    # Avoid datetime.timestamp(), which can raise OSError for year-0001 snapshot
    # cutoffs on Windows. Compute signed epoch nanoseconds using pure arithmetic.
    delta = cutoff - datetime(1970, 1, 1, tzinfo=cutoff.tzinfo)
    cutoff_ns = ((delta.days * 86_400 + delta.seconds) * 1_000_000_000
                 + delta.microseconds * 1_000)
    try:
        while pending:
            path = pending.pop()
            info = _prune_stat(path, protection)
            if info.st_mtime_ns > cutoff_ns:
                raise _PruneProtected("recent_write")
            observed.append((path, info))
            if stat.S_ISDIR(info.st_mode):
                if protection is not None:
                    names = protection.names(path)
                else:
                    with os.scandir(path) as entries:
                        names = sorted(entry.name for entry in entries)
                pending.extend(path / name for name in reversed(names))
        for path, info in observed:
            relative = path.relative_to(directory)
            digest.update(json.dumps(
                [relative.as_posix(), _prune_stat_key(info)], ensure_ascii=True,
            ).encode("ascii"))
            if stat.S_ISREG(info.st_mode):
                digest.update(_prune_file_digest(path, info, protection).encode("ascii"))
                if relative.parts and Path(relative.parts[0]) == Path("files"):
                    artifact_bytes += info.st_size
        for path, info in observed:
            if _prune_stat_key(_prune_stat(path, protection)) != _prune_stat_key(info):
                raise _PruneProtected("changed")
            if protection is not None and stat.S_ISDIR(info.st_mode):
                protection.names(path)  # A new child is never silently adopted.
    except (OSError, RuntimeError, ValueError):
        raise _PruneProtected("unreadable") from None
    return digest.hexdigest(), artifact_bytes


@dataclass(frozen=True)
class TaskService:
    """Task history, scene storage, and file delivery rooted at private paths."""

    jobs_root: Path
    scenes_file: Path

    def __post_init__(self) -> None:
        jobs_root = _absolute_path(self.jobs_root)
        scenes_file = _absolute_path(self.scenes_file)

        forbidden: list[Path] = [Path(__file__).resolve().parents[3]]
        upstream = _upstream_default_root()
        if upstream is not None:
            forbidden.append(upstream)
        if any(_is_within(jobs_root, root) for root in forbidden):
            raise TaskError("INVALID_ARGUMENT", _JOBS_ROOT_MESSAGE)
        if jobs_root.exists() and not jobs_root.is_dir():
            raise TaskError("INVALID_ARGUMENT", _JOBS_ROOT_MESSAGE)
        if scenes_file == jobs_root:
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        if scenes_file.exists() and not scenes_file.is_file():
            raise TaskError("INVALID_ARGUMENT", _SCENES_FILE_MESSAGE)

        object.__setattr__(self, "jobs_root", jobs_root)
        object.__setattr__(self, "scenes_file", scenes_file)

    # -- scenes ---------------------------------------------------------

    def list_scenes(self) -> dict[str, Any]:
        store = SceneStore(self.scenes_file)
        try:
            scenes = store.list()
        except Exception:
            raise TaskError("SCENE_INVALID", _SCENES_FILE_MESSAGE) from None
        warnings = [_SCENES_WARNING] if store.last_warnings else []
        return {"items": [_scene_payload(scene) for scene in scenes], "warnings": warnings}

    def get_scene(self, scene_id: str) -> dict[str, Any]:
        _validate_scene_id(scene_id)
        store = SceneStore(self.scenes_file)
        try:
            scene = store.get(scene_id)
        except SceneError:
            raise TaskError("SCENE_NOT_FOUND", _SCENE_NOT_FOUND_MESSAGE) from None
        except Exception:
            raise TaskError("SCENE_INVALID", _SCENES_FILE_MESSAGE) from None
        return _scene_payload(scene)

    def save_scene(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise TaskError("SCENE_INVALID", _SCENE_INVALID_MESSAGE)
        try:
            scene = Scene.from_dict(payload)
        except (SceneError, TypeError, ValueError):
            raise TaskError("SCENE_INVALID", _SCENE_INVALID_MESSAGE) from None
        store = SceneStore(self.scenes_file)
        try:
            saved = store.save(scene)
        except (OSError, ValueError):
            raise TaskError("SCENE_INVALID", _SCENES_FILE_MESSAGE) from None
        # SceneStore never persists the builtin marker: an override of a builtin id
        # is a user scene and is reported as such.
        return _scene_payload(saved)

    def delete_scene(self, scene_id: str) -> dict:
        _validate_scene_id(scene_id)
        store = SceneStore(self.scenes_file)
        try:
            deleted = store.delete(scene_id)
        except (OSError, ValueError):
            raise TaskError("SCENE_INVALID", _SCENES_FILE_MESSAGE) from None
        # A builtin itself can never be deleted.  Deleting a user override of a
        # builtin id removes only the override and restores the builtin.
        return {"deleted": bool(deleted)}

    # -- jobs -----------------------------------------------------------

    def export_job(
        self,
        *,
        conversation: Any,
        messages: Any,
        formats: list[str],
        target: dict,
        scene: dict | None,
        start: datetime | None,
        end: datetime | None,
        query: str | None,
        source_kind: str,
        source_label: str,
        wechat_version: str | None,
        export_fn: Callable[[Path, list[str]], list[Path]],
        prompt: str | None = None,
        notes: list[str] | None = None,
    ) -> dict:
        conversation_id, conversation_name = _conversation_identity(conversation)
        message_list = _normalize_messages(messages)
        format_list = _normalize_formats(formats)
        target_spec = _prepare_target(
            target,
            allowed=frozenset({"none", "folder", "obsidian", "clipboard", "agent"}),
        )
        scene_obj = _coerce_scene(scene)
        start_dt = _normalize_datetime(start)
        end_dt = _normalize_datetime(end)
        if start_dt is not None and end_dt is not None and start_dt > end_dt:
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        query_text = _normalize_query(query)
        if not isinstance(source_kind, str) or not source_kind.strip():
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        if not isinstance(source_label, str):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        if wechat_version is not None and not isinstance(wechat_version, str):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        note_list = _normalize_notes(notes)
        if prompt is not None and not isinstance(prompt, str):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        if not callable(export_fn):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)

        first_ref = _message_ref(message_list[0]) if message_list else None
        last_ref = _message_ref(message_list[-1]) if message_list else None
        try:
            manifest = JobManifest(
                job_id=new_job_id(),
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                created_at=utc_now(),
                source_kind=source_kind,
                source_label=source_label,
                wechat_version=wechat_version,
                message_count=len(message_list),
                start=start_dt,
                end=end_dt,
                query=query_text,
                scene_id=scene_obj.id if scene_obj else None,
                scene_name=scene_obj.name if scene_obj else None,
                first_message=first_ref,
                last_message=last_ref,
                notes=tuple(note_list),
                schema_version=JOB_SCHEMA_VERSION,
            )
        except (JobError, TypeError, ValueError):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE) from None

        store = self._job_store()
        try:
            handle = store.begin(manifest)
        except Exception:
            raise TaskError("JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE) from None

        try:
            files_dir = handle.files_dir
            returned_paths = export_fn(files_dir, list(format_list))
            _check_exported_paths(returned_paths, files_dir)
            artifacts = collect_artifacts(files_dir)
            if not artifacts:
                raise ValueError("the export produced no artifacts")
            record = handle.commit(artifacts, note_list)
        except Exception:
            try:
                handle.abort(_EXPORT_FAILED_MESSAGE)
            except Exception:
                pass
            raise TaskError("EXPORT_FAILED", _EXPORT_FAILED_MESSAGE) from None

        prompt_text = self._compose_prompt(record, scene_obj, prompt)
        if prompt_text:
            try:
                (record.directory / _PROMPT_FILENAME).write_text(prompt_text, encoding="utf-8")
            except OSError:
                # A missing prompt.txt only affects redelivery; it must not turn a
                # successfully committed archive into a failed export.
                pass

        if target_spec.kind in {"none", "clipboard", "agent"}:
            try:
                record = store.set_state(
                    record,
                    JobStatus.READY,
                    target=target_spec.kind,
                    detail="",
                    error=None,
                )
            except Exception:
                raise TaskError(
                    "JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE
                ) from None
            delivery = {
                "status": "none",
                "target": target_spec.kind,
                "kind": target_spec.kind,
                "destination": None,
                "detail": "",
                "error": None,
            }
        else:
            record, delivery = self._deliver_record(record, target_spec, prompt_text)

        return {
            "job": _job_summary(record),
            "delivery": delivery,
            "prompt": prompt_text,
        }

    def list_jobs(self, limit: int = 50, include_previews: bool = False) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        store = self._job_store()
        try:
            records = store.list_jobs(limit=limit)
        except Exception:
            raise TaskError("JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE) from None

        items: list[dict[str, Any]] = []
        for record in records:
            if not self._owns_record(record, record.job_id):
                continue
            try:
                items.append(_job_summary(record))
            except Exception:
                continue
        # The history list deliberately never carries message text.  Previews are
        # only available through get_job(include_previews=True), which is an
        # explicit user action.  include_previews is accepted for API symmetry.
        warnings = [_JOBS_WARNING] if store.last_warnings else []
        return {"items": items, "warnings": warnings}

    def get_job(
        self,
        job_id: str,
        include_prompt: bool = False,
        include_previews: bool = False,
    ) -> dict:
        record = self._load_record(job_id)
        summary = _job_summary(record, include_previews=include_previews)
        manifest_payload = record.manifest.to_dict()
        if not include_previews:
            # The manifest's reference block contains message previews; remove it
            # unless the caller explicitly asked for them.
            manifest_payload.pop("reference", None)
        state_payload = record.state.to_dict()
        state_payload["error"] = _safe_error_text(record.state.error)

        detail: dict[str, Any] = dict(summary)
        detail["job"] = summary
        detail["manifest"] = manifest_payload
        detail["state"] = state_payload
        detail["artifacts"] = summary["artifacts"]
        if include_prompt:
            detail["prompt"] = self._read_prompt(record)
        return detail

    def deliver_job(self, job_id: str, target: dict) -> dict:
        record = self._load_record(job_id)
        if record.state.status not in {JobStatus.READY, JobStatus.FAILED}:
            raise TaskError("JOB_NOT_READY", _JOB_NOT_READY_MESSAGE)
        target_spec = _prepare_target(
            target,
            allowed=frozenset({"folder", "obsidian"}),
        )
        prompt_text = self._read_prompt(record)
        if prompt_text is None:
            prompt_text = self._render_prompt_from_manifest(record)
        updated, delivery = self._deliver_record(record, target_spec, prompt_text)
        summary = _job_summary(updated)
        payload: dict[str, Any] = dict(summary)
        payload["job"] = summary
        payload["delivery"] = delivery
        return payload

    def record_delivery(
        self,
        job_id: str,
        status: str,
        target: dict,
        destination: str = "",
        detail: str = "",
        error: str = "",
    ) -> dict:
        if status not in {"delivered", "copied", "failed"}:
            raise TaskError("INVALID_ARGUMENT", _DELIVERY_STATUS_MESSAGE)
        for value in (destination, detail, error):
            if not isinstance(value, str):
                raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        target_spec = _prepare_target(
            target,
            allowed=frozenset({"clipboard", "agent"}),
        )

        record = self._load_record(job_id)
        job_status = {
            "delivered": JobStatus.DELIVERED,
            "copied": JobStatus.COPIED,
            "failed": JobStatus.FAILED,
        }[status]
        state_error = error if status == "failed" else None
        store = self._job_store()
        try:
            updated = store.set_state(
                record,
                job_status,
                target=target_spec.kind,
                destination=destination or None,
                detail=detail,
                error=state_error or None,
            )
        except Exception:
            raise TaskError("JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE) from None

        summary = _job_summary(updated)
        delivery = {
            "status": status,
            "target": target_spec.kind,
            "kind": target_spec.kind,
            "destination": destination or None,
            "detail": detail,
            "error": state_error or None,
        }
        payload: dict[str, Any] = dict(summary)
        payload["job"] = summary
        payload["delivery"] = delivery
        return payload

    def recover_jobs(self) -> dict:
        store = self._job_store()
        try:
            recovered = store.recover_stale()
        except Exception:
            raise TaskError("JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE) from None

        safe_ids: list[str] = []
        for job_id in recovered:
            if not isinstance(job_id, str) or not _JOB_ID_PATTERN.match(job_id):
                continue
            safe_ids.append(job_id)
            try:
                record = store.load(job_id)
                if record.state.status is JobStatus.FAILED:
                    store.set_state(
                        record,
                        JobStatus.FAILED,
                        detail="",
                        error=_RECOVERED_MESSAGE,
                        count_attempt=False,
                    )
            except (JobError, OSError):
                # The staging entry may have been renamed on a Failed collision;
                # recovery itself still succeeded, so do not abort the whole pass.
                continue
        return {"recovered": safe_ids}

    def preview_prune_jobs(self, days: int) -> dict:
        """Read-only maintenance preview, including a JSON-roundtrippable snapshot.

        knownArtifactBytes counts logical file lengths under candidates' files/
        only (including attachments), not external delivery copies or reclaimed
        disk allocation. unknownSizeCount counts skipped/unmeasured jobs, not
        additional candidates; their sizes are null, never zero. Nothing creates
        or rewrites even a missing root.
        """
        cutoff = _prune_cutoff(days)
        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        jobs: list[dict[str, str]] = []
        for job_id, paths in self._prune_entries().items():
            try:
                if len(paths) != 1:
                    raise _PruneProtected("duplicate_id")
                candidate, fingerprint = self._prune_candidate(paths[0], cutoff)
            except _PruneProtected as exc:
                skipped.append({"jobId": job_id, "reason": exc.reason, "artifactBytes": None})
                continue
            candidates.append(candidate)
            jobs.append({"jobId": job_id, "fingerprint": fingerprint})
        return {
            "dryRun": True,
            "days": days,
            "cutoff": format_utc(cutoff),
            "candidateCount": len(candidates),
            "knownArtifactBytes": sum(item["artifactBytes"] for item in candidates),
            "unknownSizeCount": len(skipped),
            "candidates": candidates,
            "skipped": skipped,
            "snapshot": {
                "version": _PRUNE_SNAPSHOT_VERSION,
                "days": days,
                "cutoff": format_utc(cutoff),
                "jobs": jobs,
            },
        }

    def prune_jobs(
        self,
        days: int,
        *,
        dry_run: bool = False,
        snapshot: dict | None = None,
    ) -> dict:
        """Explicit manual deletion; never called automatically by this service.

        UI callers should confirm preview_prune_jobs(), then pass its snapshot
        here. snapshot.jobs may be narrowed to the explicitly selected IDs; it
        never authorizes deleting newly discovered jobs. Every selected job is
        re-read and must still match its metadata/tree fingerprint and cutoff.

        The legacy prune_jobs(days) call remains supported: it takes a fresh
        preview and executes only that snapshot, using the same protections.
        Like the original prune, deletion is permanent, not a recycle-bin API;
        there is no fallback from failed recycling and no ignore_errors path.
        """
        if not isinstance(dry_run, bool):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
        if dry_run:
            if snapshot is not None:
                raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE)
            return self.preview_prune_jobs(days)
        if snapshot is None:
            snapshot = self.preview_prune_jobs(days)["snapshot"]
        cutoff, selected = self._prune_selection(snapshot, days, _prune_cutoff(days))
        entries = self._prune_entries()
        removed: list[str] = []
        skipped: list[dict[str, str]] = []
        for job_id, fingerprint in selected:
            paths = entries.get(job_id, [])
            try:
                if not paths:
                    raise _PruneProtected("not_found")
                if len(paths) != 1:
                    raise _PruneProtected("duplicate_id")
                # Acquire before final revalidation, and retain all ancestors /
                # descendants through deletion. Never pass a mutable path to a
                # recursive path-based deleter, even after resolve/lstat/hash.
                with ProtectedTree(paths[0]) as protection:
                    _, actual = self._prune_candidate(paths[0], cutoff, protection)
                    if actual != fingerprint:
                        raise _PruneProtected("changed")
                    protection.delete()
            except (_PruneProtected, SafeDeleteError) as exc:
                skipped.append({"jobId": job_id, "reason": exc.reason})
                continue
            except OSError:
                skipped.append({"jobId": job_id, "reason": "delete_failed"})
                continue
            removed.append(job_id)
        return {
            "dryRun": False,
            "days": days,
            "cutoff": format_utc(cutoff),
            "removed": removed,
            "skipped": skipped,
        }

    def get_job_artifact_path(self, job_id: str, name: str) -> Path:
        record = self._load_record(job_id)
        if not isinstance(name, str) or not name or name in {".", ".."}:
            raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
        if any(token in name for token in ("/", "\\", "\x00", ":")):
            raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
        if any(character in name for character in ('<', '>', '"', "|", "?", "*")):
            raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
        try:
            if Path(name).name != name or Path(name).is_absolute():
                raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
            files_dir = record.files_dir
            resolved_job = record.directory.resolve()
            resolved_base = files_dir.resolve()
            if not resolved_base.is_relative_to(resolved_job):
                raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
            candidate = files_dir / name
            resolved = candidate.resolve(strict=True)
        except TaskError:
            raise
        except (OSError, RuntimeError, ValueError):
            raise TaskError("JOB_NOT_FOUND", _ARTIFACT_NOT_FOUND_MESSAGE) from None
        if not resolved.is_relative_to(resolved_base) or not resolved.is_file():
            raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
        return candidate

    def job_directory(self, job_id: str) -> Path:
        record = self._load_record(job_id)
        return record.directory

    # -- internals ------------------------------------------------------

    @staticmethod
    def _prune_selection(
        snapshot: dict, days: int, current_cutoff: datetime,
    ) -> tuple[datetime, list[tuple[str, str]]]:
        try:
            if (
                not isinstance(snapshot, dict)
                or type(snapshot.get("version")) is not int
                or snapshot["version"] != _PRUNE_SNAPSHOT_VERSION
                or type(snapshot.get("days")) is not int
                or snapshot["days"] != days
                or not isinstance(snapshot.get("cutoff"), str)
                or not isinstance(snapshot.get("jobs"), list)
            ):
                raise ValueError
            cutoff = parse_utc(snapshot["cutoff"])
            if format_utc(cutoff) != snapshot["cutoff"] or cutoff > current_cutoff:
                raise ValueError
            selected: list[tuple[str, str]] = []
            seen: set[str] = set()
            for item in snapshot["jobs"]:
                if not isinstance(item, dict):
                    raise ValueError
                job_id, fingerprint = item.get("jobId"), item.get("fingerprint")
                if (
                    not isinstance(job_id, str) or not _JOB_ID_PATTERN.fullmatch(job_id)
                    or job_id in seen
                    or not isinstance(fingerprint, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                ):
                    raise ValueError
                seen.add(job_id)
                selected.append((job_id, fingerprint))
            return cutoff, selected
        except (ValueError, TypeError, OverflowError):
            raise TaskError("INVALID_ARGUMENT", _INVALID_ARGUMENT_MESSAGE) from None

    def _prune_entries(self) -> dict[str, list[Path]]:
        """Enumerate only owned buckets; never instantiate a writable store."""
        result: dict[str, list[Path]] = {}
        try:
            try:
                self.jobs_root.lstat()
            except FileNotFoundError:
                return result
            if not stat.S_ISDIR(_prune_stat(self.jobs_root).st_mode):
                raise _PruneProtected("unsafe_path")
            # Staging is never deletable, but a duplicate ID there protects the
            # corresponding Ready/Failed entry as well.
            for bucket in (*_PRUNE_BUCKETS, ".staging"):
                base = self.jobs_root / bucket
                try:
                    base.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(_prune_stat(base).st_mode):
                    raise _PruneProtected("unsafe_path")
                with os.scandir(base) as entries:
                    names = sorted(entry.name for entry in entries)
                for name in names:
                    if _JOB_ID_PATTERN.fullmatch(name):
                        result.setdefault(name, []).append(base / name)
            return result
        except _PruneProtected:
            raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE) from None
        except (OSError, RuntimeError, ValueError):
            raise TaskError("JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE) from None

    def _prune_candidate(
        self, directory: Path, cutoff: datetime, protection: ProtectedTree | None = None,
    ) -> tuple[dict, str]:
        if directory.parent.name not in _PRUNE_BUCKETS:
            raise _PruneProtected("unfinished")
        root_info = _prune_stat(self.jobs_root, protection)
        base_info = _prune_stat(directory.parent, protection)
        if not stat.S_ISDIR(_prune_stat(directory, protection).st_mode):
            raise _PruneProtected("unsafe_path")
        record, manifest_bytes, state_bytes = _prune_metadata(directory, protection)
        if not self._owns_record(record, directory.name):
            raise _PruneProtected("unsafe_path")
        if record.state.status not in _PRUNABLE_STATUSES:
            raise _PruneProtected("unfinished")
        if record.state.updated_at > cutoff or record.manifest.created_at > cutoff:
            raise _PruneProtected("not_old")
        tree_fingerprint, artifact_bytes = _prune_tree(directory, cutoff, protection)
        _, manifest_after, state_after = _prune_metadata(directory, protection)
        if manifest_after != manifest_bytes or state_after != state_bytes:
            raise _PruneProtected("changed")
        # Bucket/root mtime changes from unrelated jobs must not block cleanup,
        # but replacing either directory (or inserting a link) must block it.
        for path, before in ((self.jobs_root, root_info), (directory.parent, base_info)):
            after = _prune_stat(path, protection)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise _PruneProtected("changed")
        digest = hashlib.sha256()
        for value in (
            directory.relative_to(self.jobs_root).as_posix().encode("utf-8"),
            manifest_bytes, state_bytes, tree_fingerprint.encode("ascii"),
        ):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return {
            "jobId": record.job_id,
            "status": record.state.status.value,
            "updatedAt": format_utc(record.state.updated_at),
            "artifactBytes": artifact_bytes,
        }, digest.hexdigest()

    def _job_store(self) -> _QuietJobStore:
        try:
            return _QuietJobStore(self.jobs_root)
        except Exception:
            raise TaskError(
                "JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE
            ) from None

    def _owns_record(self, record: JobRecord, job_id: str) -> bool:
        try:
            if record.manifest.job_id != job_id or record.directory.name != job_id:
                return False
            directory = record.directory.resolve()
            root = self.jobs_root.resolve()
            if directory == root or not directory.is_relative_to(root):
                return False
            bases = {
                (root / "Ready").resolve(),
                (root / "Failed").resolve(),
                (root / ".staging").resolve(),
            }
            return directory.parent in bases
        except (OSError, RuntimeError, ValueError):
            return False

    def _load_record(self, job_id: str) -> JobRecord:
        _validate_job_id(job_id)
        store = self._job_store()
        try:
            record = store.load(job_id)
        except (JobError, OSError):
            raise TaskError("JOB_NOT_FOUND", _JOB_NOT_FOUND_MESSAGE) from None
        if not self._owns_record(record, job_id):
            raise TaskError("JOB_PATH_UNSAFE", _JOB_PATH_UNSAFE_MESSAGE)
        return record

    def _compose_prompt(
        self,
        record: JobRecord,
        scene: Scene | None,
        explicit_prompt: str | None,
    ) -> str:
        if explicit_prompt is not None:
            return explicit_prompt
        if scene is None:
            return ""
        files = [
            record.files_dir / artifact.name
            for artifact in record.manifest.artifacts
            if (record.files_dir / artifact.name).is_file()
        ]
        try:
            return render_prompt(scene, record.manifest, files)
        except Exception:
            return ""

    def _render_prompt_from_manifest(self, record: JobRecord) -> str:
        scene = self._scene_for_manifest(record)
        if scene is None:
            return ""
        files = [
            record.files_dir / artifact.name
            for artifact in record.manifest.artifacts
            if (record.files_dir / artifact.name).is_file()
        ]
        try:
            return render_prompt(scene, record.manifest, files)
        except Exception:
            return ""

    def _scene_for_manifest(self, record: JobRecord) -> Scene | None:
        scene_id = record.manifest.scene_id
        if not scene_id:
            return None
        try:
            return SceneStore(self.scenes_file).get(scene_id)
        except (SceneError, OSError, ValueError):
            return None

    def _read_prompt(self, record: JobRecord) -> str | None:
        path = record.directory / _PROMPT_FILENAME
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(record.directory.resolve()):
                return None
            if not resolved.is_file():
                return None
            return resolved.read_text(encoding="utf-8")
        except (OSError, RuntimeError, UnicodeDecodeError, ValueError):
            return None

    def _deliver_record(
        self,
        record: JobRecord,
        target_spec: _TargetSpec,
        prompt_text: str,
    ) -> tuple[JobRecord, dict[str, Any]]:
        store = self._job_store()
        request = DeliveryRequest(
            job=record,
            scene=self._scene_for_manifest(record),
            prompt=prompt_text or None,
        )
        try:
            if target_spec.kind == "folder":
                if target_spec.folder_root is None:
                    raise TaskError("TARGET_NOT_SUPPORTED", _TARGET_NOT_SUPPORTED_MESSAGE)
                delivery_target: Any = FolderDelivery(
                    target_spec.folder_root,
                    organize_by_date=target_spec.organize_by_date,
                )
                result = delivery_target.deliver(request)
            elif target_spec.kind == "obsidian":
                if target_spec.vault is None:
                    raise TaskError("TARGET_NOT_SUPPORTED", _TARGET_NOT_SUPPORTED_MESSAGE)
                delivery_target = _SafeObsidianDelivery(
                    target_spec.vault,
                    folder=target_spec.folder,
                    attachments=target_spec.attachments,
                    dedupe=target_spec.dedupe,
                )
                result = delivery_target.deliver(request)
            else:
                raise TaskError("TARGET_NOT_SUPPORTED", _TARGET_NOT_SUPPORTED_MESSAGE)
        except _NoteExistsError:
            return self._fail_delivery(record, target_spec, _NOTE_EXISTS_MESSAGE)
        except DeliveryError:
            # The upstream exception text can contain destination paths or OS
            # messages.  It is intentionally replaced with a fixed sentence.
            return self._fail_delivery(record, target_spec, _DELIVERY_FAILED_MESSAGE)
        except TaskError:
            raise
        except Exception:
            return self._fail_delivery(record, target_spec, _DELIVERY_FAILED_MESSAGE)

        job_status = (
            JobStatus.DELIVERED if result.status == "delivered" else JobStatus.COPIED
        )
        try:
            updated = store.set_state(
                record,
                job_status,
                target=result.target,
                destination=result.destination,
                detail=result.detail,
                error=None,
            )
        except Exception:
            raise TaskError("JOB_STORE_UNAVAILABLE", _STORE_UNAVAILABLE_MESSAGE) from None

        delivery = {
            "status": result.status,
            "target": result.target,
            "kind": result.target,
            "destination": result.destination,
            "detail": result.detail,
            "error": None,
        }
        return updated, delivery

    def _fail_delivery(
        self,
        record: JobRecord,
        target_spec: _TargetSpec,
        message: str,
    ) -> tuple[JobRecord, dict[str, Any]]:
        store = self._job_store()
        try:
            updated = store.set_state(
                record,
                JobStatus.FAILED,
                target=target_spec.kind,
                detail=message,
                error=message,
            )
        except Exception:
            # The archive is still intact under Ready/Failed; report the fixed
            # delivery failure even if state could not be rewritten.
            updated = record
        delivery = {
            "status": "failed",
            "target": target_spec.kind,
            "kind": target_spec.kind,
            "destination": None,
            "detail": message,
            "error": message,
            "code": "DELIVERY_FAILED",
        }
        return updated, delivery
