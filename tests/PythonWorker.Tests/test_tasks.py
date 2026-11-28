"""Behaviour tests for scenes, job history, and file delivery.

Run from the project root:
  .venv\\Scripts\\python.exe -X utf8 tests/PythonWorker.Tests/test_tasks.py -v

All data is synthetic and every writable path comes from tempfile.TemporaryDirectory.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
VENDOR_DIR = WORKER_DIR / "vendor"
for _path in (WORKER_DIR, VENDOR_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from wechat_context_exporter.jobs import (  # noqa: E402
    JobManifest,
    JobStore,
    new_job_id,
    utc_now,
)
from wechat_context_exporter.models import (  # noqa: E402
    Conversation,
    ConversationKind,
    Message,
    MessageType,
)
from wechat_memory.tasks import TaskError, TaskService  # noqa: E402

EXPORT_FAILED_TEXT = "导出失败，任务已中止。"
RECOVERED_TEXT = "任务在完成前被中断。"
NOTE_EXISTS_TEXT = "目标笔记已存在，请选择其他 vault 或先移动旧笔记。"


class TaskTestCase(unittest.TestCase):
    """Shared temporary service, fictional conversation, and fake exporter."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="wechat-memory-tasks-")
        self.addCleanup(self._temp.cleanup)
        self.temp_dir = Path(self._temp.name)
        self.jobs_root = self.temp_dir / "jobs"
        self.scenes_file = self.temp_dir / "scenes.json"
        self.service = TaskService(
            jobs_root=self.jobs_root,
            scenes_file=self.scenes_file,
        )
        self.conversation = Conversation(
            id="conv-fictional-001",
            name="虚构项目讨论",
            kind=ConversationKind.GROUP,
        )
        self.messages = [
            Message(
                id="m001",
                conversation_id=self.conversation.id,
                sender="小林",
                timestamp=datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc),
                type=MessageType.TEXT,
                content="第一段合成内容",
            ),
            Message(
                id="m002",
                conversation_id=self.conversation.id,
                sender="阿禾",
                timestamp=datetime(2026, 11, 24, 18, 30, tzinfo=timezone.utc),
                type=MessageType.TEXT,
                content="第二段合成内容，用于测试首尾引用。",
            ),
        ]

    def write_export(self, files_dir: Path, formats: list[str]) -> list[Path]:
        """Deterministic fake export: writes only the requested top-level formats."""
        paths: list[Path] = []
        if "markdown" in formats:
            path = files_dir / "chat.md"
            path.write_text("# 虚构会话\n\n这是合成内容。\n", encoding="utf-8")
            paths.append(path)
        if "json" in formats:
            path = files_dir / "chat.json"
            path.write_text(
                json.dumps({"messages": ["synthetic"]}, ensure_ascii=False),
                encoding="utf-8",
            )
            paths.append(path)
        if "pdf" in formats:
            path = files_dir / "chat.pdf"
            path.write_bytes(b"%PDF-1.4\nfictional\n")
            paths.append(path)
        return paths

    def export(self, **overrides):
        parameters = {
            "conversation": self.conversation,
            "messages": self.messages,
            "formats": ["markdown", "json"],
            "target": {"kind": "none"},
            "scene": None,
            "start": None,
            "end": None,
            "query": None,
            "source_kind": "demo",
            "source_label": "演示数据（虚构）",
            "wechat_version": "4.0-fictional",
            "export_fn": self.write_export,
            "prompt": None,
            "notes": None,
        }
        parameters.update(overrides)
        return self.service.export_job(**parameters)

    @staticmethod
    def plain(payload) -> str:
        return json.dumps(payload, ensure_ascii=False)


class ExportJobTests(TaskTestCase):
    def test_export_creates_ready_job_with_hashes_and_references(self) -> None:
        start = datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 11, 24, 23, 59, 59, tzinfo=timezone.utc)
        result = self.export(start=start, end=end, query="合成关键词")

        job = result["job"]
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["messageCount"], 2)
        self.assertEqual(result["delivery"]["status"], "none")
        self.assertEqual(result["prompt"], "")

        job_id = job["jobId"]
        job_dir = (self.jobs_root / "Ready" / job_id).resolve()
        self.assertEqual(Path(job["directory"]).resolve(), job_dir)
        self.assertEqual(self.service.job_directory(job_id).resolve(), job_dir)

        manifest = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["job_id"], job_id)
        self.assertEqual(manifest["conversation"]["id"], self.conversation.id)
        self.assertEqual(manifest["conversation"]["name"], self.conversation.name)
        self.assertEqual(manifest["message_count"], 2)
        self.assertEqual(manifest["window"]["start"], "2026-11-24T00:00:00Z")
        self.assertEqual(manifest["window"]["end"], "2026-11-24T23:59:59Z")
        self.assertEqual(manifest["window"]["query"], "合成关键词")
        self.assertEqual(manifest["reference"]["first"]["id"], "m001")
        self.assertEqual(manifest["reference"]["last"]["id"], "m002")
        self.assertEqual(manifest["reference"]["first"]["preview"], "第一段合成内容")
        self.assertIsNone(manifest["scene"]["id"])

        state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["target"], "none")
        self.assertFalse((job_dir / "prompt.txt").exists())

        files_dir = job_dir / "files"
        self.assertTrue((files_dir / "chat.md").is_file())
        self.assertTrue((files_dir / "chat.json").is_file())
        artifacts = {item["name"]: item for item in job["artifacts"]}
        self.assertEqual(set(artifacts), {"chat.md", "chat.json"})
        for name in ("chat.md", "chat.json"):
            data = (files_dir / name).read_bytes()
            self.assertEqual(artifacts[name]["size"], len(data))
            self.assertEqual(
                artifacts[name]["sha256"],
                hashlib.sha256(data).hexdigest(),
            )

        # The upstream store's text log is disabled by this module.
        self.assertFalse((self.jobs_root / "Logs").exists())


class DeliveryTests(TaskTestCase):
    def test_folder_delivery_copies_artifacts_and_prompt(self) -> None:
        output_root = self.temp_dir / "delivery"
        output_root.mkdir()
        prompt = "请把归档整理成一份纪要。"
        result = self.export(
            target={
                "kind": "folder",
                "root": str(output_root),
                "organizeByDate": False,
            },
            prompt=prompt,
        )

        self.assertEqual(result["job"]["status"], "delivered")
        self.assertEqual(result["delivery"]["status"], "delivered")
        destination = Path(result["delivery"]["destination"])
        self.assertTrue(destination.is_dir())
        self.assertEqual(destination.parent, output_root.resolve())
        self.assertIn("虚构项目讨论", destination.name)
        self.assertIn(result["job"]["jobId"], destination.name)

        job_dir = Path(result["job"]["directory"])
        for name in ("chat.md", "chat.json"):
            self.assertTrue((destination / name).is_file())
            self.assertEqual(
                (destination / name).read_bytes(),
                (job_dir / "files" / name).read_bytes(),
            )
        prompt_file = destination / "给 Agent 的提示词.txt"
        self.assertEqual(prompt_file.read_text(encoding="utf-8"), prompt)
        self.assertTrue((job_dir / "prompt.txt").is_file())

        detail = self.service.get_job(result["job"]["jobId"])
        self.assertEqual(detail["status"], "delivered")
        self.assertEqual(detail["target"], "folder")
        self.assertEqual(Path(detail["destination"]), destination)

    def test_same_conversation_same_day_exports_get_separate_folders(self) -> None:
        output_root = self.temp_dir / "delivery-isolated"
        output_root.mkdir()
        target = {"kind": "folder", "root": str(output_root), "organizeByDate": False}
        first = self.export(target=target, formats=["markdown"])
        second = self.export(target=target, formats=["json"])

        first_dir = Path(first["delivery"]["destination"])
        second_dir = Path(second["delivery"]["destination"])
        self.assertNotEqual(first_dir, second_dir)
        self.assertIn(first["job"]["jobId"], first_dir.name)
        self.assertIn(second["job"]["jobId"], second_dir.name)
        self.assertEqual({path.name for path in first_dir.iterdir()}, {"chat.md"})
        self.assertEqual({path.name for path in second_dir.iterdir()}, {"chat.json"})

    def test_delivery_fails_when_a_recorded_artifact_is_missing(self) -> None:
        output_root = self.temp_dir / "delivery-missing"
        output_root.mkdir()
        exported = self.export()
        job_id = exported["job"]["jobId"]
        missing = Path(exported["job"]["directory"]) / "files" / "chat.json"
        missing.unlink()

        delivered = self.service.deliver_job(
            job_id,
            {"kind": "folder", "root": str(output_root), "organizeByDate": False},
        )
        self.assertEqual(delivered["job"]["status"], "failed")
        self.assertEqual(delivered["delivery"]["status"], "failed")
        self.assertTrue(delivered["delivery"]["error"])
        self.assertEqual(list(output_root.iterdir()), [])

    def test_obsidian_delivery_writes_note_and_never_overwrites(self) -> None:
        vault = self.temp_dir / "vault"
        vault.mkdir()
        target = {
            "kind": "obsidian",
            "vault": str(vault),
            "folder": "微信记忆",
            "attachments": "_附件",
            "dedupe": False,
        }
        prompt = "请生成一份决策记录。"
        result = self.export(
            target=target,
            prompt=prompt,
            scene=self.service.get_scene("group-digest"),
        )

        self.assertEqual(result["job"]["status"], "delivered")
        note = Path(result["delivery"]["destination"])
        self.assertTrue(note.is_file())
        self.assertTrue(note.is_relative_to(vault.resolve()))
        text = note.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        self.assertIn("source: wechat", text)
        self.assertIn("job_id:", text)
        self.assertIn("# 虚构项目讨论 ·", text)
        self.assertIn(prompt, text)
        self.assertIn("群聊总结", text)  # scene frontmatter

        job_id = result["job"]["jobId"]
        attachments = vault / "微信记忆" / "_附件" / job_id
        self.assertTrue((attachments / "chat.md").is_file())
        self.assertTrue((attachments / "chat.json").is_file())

        sentinel = "旧笔记内容必须保留"
        note.write_text(sentinel, encoding="utf-8")

        second = self.export(target=target, prompt="第二个任务")
        self.assertEqual(second["job"]["status"], "failed")
        self.assertEqual(second["delivery"]["status"], "failed")
        self.assertEqual(second["delivery"]["error"], NOTE_EXISTS_TEXT)
        self.assertEqual(note.read_text(encoding="utf-8"), sentinel)
        self.assertIn("目标笔记已存在", second["delivery"]["detail"])

        deduped_target = dict(target)
        deduped_target["dedupe"] = True
        third = self.export(target=deduped_target, prompt="第三个任务")
        self.assertEqual(third["job"]["status"], "delivered")
        third_note = Path(third["delivery"]["destination"])
        self.assertNotEqual(third_note, note)
        self.assertIn(third["job"]["jobId"], third_note.stem)
        self.assertTrue(third_note.is_file())
        self.assertEqual(note.read_text(encoding="utf-8"), sentinel)

    def test_deliver_job_redelivers_ready_job_and_rejects_finished_job(self) -> None:
        prompt = "重试投递时仍使用原提示词。"
        exported = self.export(prompt=prompt)
        job_id = exported["job"]["jobId"]
        self.assertEqual(exported["job"]["status"], "ready")

        output_root = self.temp_dir / "retry-delivery"
        output_root.mkdir()
        delivered = self.service.deliver_job(
            job_id,
            {
                "kind": "folder",
                "root": str(output_root),
                "organizeByDate": False,
            },
        )
        self.assertEqual(delivered["job"]["status"], "delivered")
        self.assertEqual(delivered["delivery"]["status"], "delivered")
        destination = Path(delivered["delivery"]["destination"])
        self.assertEqual(
            (destination / "给 Agent 的提示词.txt").read_text(encoding="utf-8"),
            prompt,
        )

        with self.assertRaises(TaskError) as not_ready:
            self.service.deliver_job(
                job_id,
                {"kind": "folder", "root": str(output_root)},
            )
        self.assertEqual(not_ready.exception.code, "JOB_NOT_READY")

    def test_client_target_is_not_executed_and_stays_ready(self) -> None:
        result = self.export(target={"kind": "clipboard"})
        self.assertEqual(result["job"]["status"], "ready")
        self.assertEqual(result["job"]["target"], "clipboard")
        self.assertEqual(result["delivery"]["status"], "none")
        self.assertEqual(result["delivery"]["target"], "clipboard")

        recorded = self.service.record_delivery(
            result["job"]["jobId"],
            "failed",
            {"kind": "clipboard"},
            destination="",
            detail="客户端复制失败",
            error="客户端复制失败",
        )
        self.assertEqual(recorded["job"]["status"], "failed")
        self.assertEqual(recorded["job"]["error"], "客户端复制失败")
        self.assertEqual(recorded["delivery"]["status"], "failed")

    def test_export_failure_aborts_with_fixed_safe_error(self) -> None:
        secret = "绝不能出现的异常内容 C:\\private\\secret.txt"

        def failing_export(files_dir: Path, formats: list[str]) -> list[Path]:
            partial = files_dir / "chat.md"
            partial.write_text("partial synthetic output\n", encoding="utf-8")
            raise RuntimeError(secret)

        with self.assertRaises(TaskError) as raised:
            self.export(export_fn=failing_export, prompt="不应保存")
        self.assertEqual(raised.exception.code, "EXPORT_FAILED")
        self.assertEqual(raised.exception.message, EXPORT_FAILED_TEXT)
        self.assertNotIn("private", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

        items = self.service.list_jobs()["items"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["error"], EXPORT_FAILED_TEXT)
        self.assertNotIn("private", self.plain(item))
        failed_dir = Path(item["directory"])
        self.assertEqual(failed_dir.parent.name, "Failed")
        state_text = (failed_dir / "state.json").read_text(encoding="utf-8")
        self.assertNotIn("private", state_text)
        self.assertNotIn("secret", state_text)
        self.assertEqual(json.loads(state_text)["error"], EXPORT_FAILED_TEXT)


class SceneTests(TaskTestCase):
    def test_scenes_builtin_custom_save_delete_and_invalid_target(self) -> None:
        listed = self.service.list_scenes()
        self.assertEqual(listed["warnings"], [])
        self.assertEqual(
            [item["id"] for item in listed["items"]],
            ["group-digest", "decision-review", "commitments", "change-trace"],
        )
        self.assertTrue(all(item["builtin"] for item in listed["items"]))
        self.assertTrue(all(item["prompt"] for item in listed["items"]))
        self.assertNotIn("path", self.plain(listed))

        scene_id = "my-fictional-scene"
        saved = self.service.save_scene(
            {
                "id": scene_id,
                "name": "我的虚构场景",
                "description": "仅用于测试。",
                "target": "folder",
                "prompt": "整理这段合成对话。",
                "days": 7,
            }
        )
        self.assertFalse(saved["builtin"])
        self.assertEqual(saved["name"], "我的虚构场景")
        self.assertEqual(self.service.get_scene(scene_id)["prompt"], "整理这段合成对话。")
        self.assertEqual(len(self.service.list_scenes()["items"]), 5)

        self.assertEqual(self.service.delete_scene(scene_id), {"deleted": True})
        with self.assertRaises(TaskError) as missing:
            self.service.get_scene(scene_id)
        self.assertEqual(missing.exception.code, "SCENE_NOT_FOUND")

        self.assertEqual(self.service.delete_scene("group-digest"), {"deleted": False})
        self.assertTrue(self.service.get_scene("group-digest")["builtin"])

        with self.assertRaises(TaskError) as invalid:
            self.service.save_scene(
                {"id": "bad-target", "name": "坏目标", "target": "ftp"}
            )
        self.assertEqual(invalid.exception.code, "SCENE_INVALID")

        # Invalid delivery targets are rejected before a job is created.
        for bad_target in (
            {"kind": "ftp"},
            {"kind": "folder"},
            {"kind": "folder", "root": "relative/path"},
            {"kind": "obsidian", "vault": str(self.temp_dir / "missing-vault")},
        ):
            with self.assertRaises(TaskError):
                self.export(target=bad_target)
        self.assertEqual(self.service.list_jobs()["items"], [])


class HistoryAndPathTests(TaskTestCase):
    def test_list_jobs_hides_previews_while_get_job_can_include_them(self) -> None:
        secret = "历史列表不得包含的预览内容"
        messages = [
            Message(
                id="m101",
                conversation_id=self.conversation.id,
                sender="小林",
                timestamp=datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc),
                type=MessageType.TEXT,
                content=secret,
            ),
            Message(
                id="m102",
                conversation_id=self.conversation.id,
                sender="阿禾",
                timestamp=datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc),
                type=MessageType.TEXT,
                content=f"{secret}（结尾）",
            ),
        ]
        result = self.export(messages=messages, prompt=None)
        job_id = result["job"]["jobId"]

        items = self.service.list_jobs()["items"]
        self.assertEqual(len(items), 1)
        self.assertNotIn(secret, self.plain(items))
        self.assertNotIn("firstMessage", items[0])
        self.assertNotIn("lastMessage", items[0])
        self.assertNotIn("reference", items[0])
        self.assertNotIn(
            secret,
            self.plain(self.service.list_jobs(include_previews=True)["items"]),
        )

        plain_detail = self.service.get_job(job_id)
        self.assertNotIn(secret, self.plain(plain_detail))
        self.assertNotIn("reference", plain_detail["manifest"])
        self.assertNotIn("prompt", plain_detail)

        preview_detail = self.service.get_job(job_id, include_previews=True)
        self.assertIn(secret, self.plain(preview_detail))
        self.assertEqual(preview_detail["firstMessage"]["id"], "m101")
        self.assertEqual(preview_detail["job"]["lastMessage"]["id"], "m102")
        self.assertEqual(preview_detail["manifest"]["reference"]["first"]["id"], "m101")

    def test_artifact_path_traversal_and_record_delivery(self) -> None:
        result = self.export()
        job_id = result["job"]["jobId"]
        artifact_name = result["job"]["artifacts"][0]["name"]
        artifact_path = self.service.get_job_artifact_path(job_id, artifact_name)
        self.assertTrue(artifact_path.is_file())
        self.assertEqual(artifact_path.name, artifact_name)

        for unsafe in (
            "../manifest.json",
            "..\\manifest.json",
            "sub/child.md",
            str(self.jobs_root / "Ready" / job_id / "manifest.json"),
        ):
            with self.assertRaises(TaskError) as raised:
                self.service.get_job_artifact_path(job_id, unsafe)
            self.assertEqual(raised.exception.code, "JOB_PATH_UNSAFE")

        with self.assertRaises(TaskError):
            self.service.get_job_artifact_path("../bad-id", "chat.md")
        with self.assertRaises(TaskError):
            self.service.job_directory("../bad-id")

        recorded = self.service.record_delivery(
            job_id,
            "copied",
            {"kind": "clipboard"},
            destination="剪贴板",
            detail="已复制，等待粘贴",
        )
        self.assertEqual(recorded["job"]["status"], "copied")
        self.assertEqual(recorded["status"], "copied")
        self.assertEqual(recorded["delivery"]["destination"], "剪贴板")

        detail = self.service.get_job(job_id)
        self.assertEqual(detail["status"], "copied")
        self.assertEqual(detail["state"]["status"], "copied")
        self.assertEqual(detail["target"], "clipboard")
        self.assertEqual(detail["destination"], "剪贴板")

        with self.assertRaises(TaskError) as bad_status:
            self.service.record_delivery(job_id, "unknown", {"kind": "clipboard"})
        self.assertEqual(bad_status.exception.code, "INVALID_ARGUMENT")

        with self.assertRaises(TaskError) as bad_target:
            self.service.record_delivery(
                job_id,
                "delivered",
                {"kind": "folder", "root": str(self.temp_dir)},
            )
        self.assertEqual(bad_target.exception.code, "TARGET_NOT_SUPPORTED")


class RecoveryTests(TaskTestCase):
    def test_recover_jobs_moves_stale_staging_to_failed(self) -> None:
        manifest = JobManifest(
            job_id=new_job_id(),
            conversation_id="conv-fictional-restore",
            conversation_name="虚构恢复任务",
            created_at=utc_now(),
            source_kind="demo",
            message_count=0,
        )
        store = JobStore(self.jobs_root)
        handle = store.begin(manifest)
        (handle.files_dir / "chat.md").write_text("partial\n", encoding="utf-8")
        staging_dir = handle.directory
        self.assertTrue(staging_dir.is_dir())

        recovered = self.service.recover_jobs()
        self.assertIn(manifest.job_id, recovered["recovered"])
        self.assertFalse(staging_dir.exists())
        failed_dir = self.jobs_root / "Failed" / manifest.job_id
        self.assertTrue(failed_dir.is_dir())

        items = self.service.list_jobs()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["jobId"], manifest.job_id)
        self.assertEqual(items[0]["status"], "failed")
        self.assertEqual(items[0]["error"], RECOVERED_TEXT)
        self.assertNotIn("interrupted", self.plain(items))
        state = json.loads((failed_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], RECOVERED_TEXT)

    def test_prune_jobs_requires_positive_days_and_removes_old_finished_job(self) -> None:
        for bad_days in (0, -1, True, 1.5):
            with self.assertRaises(TaskError) as raised:
                self.service.prune_jobs(bad_days)
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

        result = self.export()
        job_id = result["job"]["jobId"]
        self.service.record_delivery(
            job_id,
            "copied",
            {"kind": "clipboard"},
            destination="剪贴板",
        )
        state_path = Path(result["job"]["directory"]) / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["updated_at"] = "2020-01-01T00:00:00Z"
        state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # Cleanup checks the entire synthetic task, not just the completion
        # timestamp: a newly created manifest/prompt/artifact must stay protected.
        job_dir = state_path.parent
        manifest_path = job_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = "2020-01-01T00:00:00Z"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        for base, directories, files in os.walk(job_dir, followlinks=False):
            for name in directories + files:
                os.utime(Path(base) / name, (old, old))
        os.utime(job_dir, (old, old))

        removed = self.service.prune_jobs(1)["removed"]
        self.assertIn(job_id, removed)
        self.assertFalse((self.jobs_root / "Ready" / job_id).exists())


class GuardTests(TaskTestCase):
    def test_jobs_root_must_be_private_and_absolute(self) -> None:
        with self.assertRaises(TaskError) as project:
            TaskService(jobs_root=ROOT, scenes_file=self.scenes_file)
        self.assertEqual(project.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(TaskError) as relative:
            TaskService(jobs_root=Path("relative-jobs"), scenes_file=self.scenes_file)
        self.assertEqual(relative.exception.code, "INVALID_ARGUMENT")

    def test_invalid_arguments_do_not_create_jobs(self) -> None:
        for message_overrides, export_overrides in (
            ({"formats": []}, {}),
            ({"formats": ["docx"]}, {}),
            ({"start": datetime(2026, 11, 25), "end": datetime(2026, 11, 24)}, {}),
            ({}, {"source_kind": ""}),
        ):
            overrides = {"messages": self.messages}
            overrides.update(message_overrides)
            overrides.update(export_overrides)
            with self.assertRaises(TaskError) as raised:
                self.export(**overrides)
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(self.service.list_jobs()["items"], [])


if __name__ == "__main__":
    unittest.main()
