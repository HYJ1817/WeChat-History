"""Protocol tests for bounded, single-use task-history cleanup previews.

All task records are synthetic in-memory fixtures; no real jobs root is read or deleted.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "src" / "PythonWorker"
if str(WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(WORKER_DIR))

import worker  # noqa: E402


class FakeTaskError(Exception):
    code = "TASK_FAILED"
    message = "合成任务错误。"


class FakeTaskService:
    def __init__(self, job_count: int = 1) -> None:
        self.jobs = [
            {"jobId": f"synthetic-{index:06d}", "fingerprint": "a" * 64}
            for index in range(job_count)
        ]
        self.prune_calls: list[tuple[int, dict]] = []
        self.prune_result_override: dict | None = None

    def preview_prune_jobs(self, days: int) -> dict:
        candidates = [
            {"jobId": item["jobId"], "status": "copied", "updatedAt": "2020-01-01T00:00:00Z", "artifactBytes": 17}
            for item in self.jobs
        ]
        return {
            "dryRun": True,
            "days": days,
            "cutoff": "2020-02-01T00:00:00Z",
            "candidateCount": len(candidates),
            "knownArtifactBytes": 17 * len(candidates),
            "unknownSizeCount": 2,
            "candidates": candidates,
            "skipped": [
                {"jobId": "protected-1", "reason": "in_use", "artifactBytes": None},
                {"jobId": "protected-2", "reason": "safe_delete_unavailable", "artifactBytes": None},
            ],
            "snapshot": {"version": 1, "days": days, "cutoff": "2020-02-01T00:00:00Z", "jobs": self.jobs},
        }

    def prune_jobs(self, days: int, *, snapshot=None) -> dict:
        self.prune_calls.append((days, snapshot))
        if self.prune_result_override is not None:
            return self.prune_result_override
        ids = [item["jobId"] for item in snapshot["jobs"]]
        return {
            "dryRun": False,
            "days": days,
            "cutoff": snapshot["cutoff"],
            "removed": ids,
            "skipped": [{"jobId": "synthetic-skip", "reason": "protection_failed"}],
        }


def request(request_id: str, command: str, payload: dict | None = None) -> dict:
    return {
        "protocolVersion": 1,
        "requestId": request_id,
        "type": "request",
        "command": command,
        "payload": payload or {},
    }


class TaskProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="task-protocol-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.service = FakeTaskService()
        self.context = worker.Context("synthetic-demo.json", str(Path(self.temp.name) / "jobs"))
        self.service_patch = patch.object(worker, "_task_service", return_value=(self.service, FakeTaskError))
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)

    def preview(self, days: int = 30) -> dict:
        return worker.handle_preview_prune_jobs({"days": days}, self.context)

    def confirm(self, days: int = 30, token: str | None = None) -> dict:
        payload = {"days": days}
        if token is not None:
            payload["previewToken"] = token
        return worker.handle_confirm_prune_jobs(payload, self.context)

    def test_confirm_requires_a_token_and_rejects_unknown_tokens(self) -> None:
        with self.assertRaises(worker.WorkerError) as missing:
            self.confirm()
        self.assertEqual(missing.exception.code, "PRUNE_PREVIEW_REQUIRED")

        with self.assertRaises(worker.WorkerError) as unknown:
            self.confirm(token="not-a-issued-preview-token")
        self.assertEqual(unknown.exception.code, "PRUNE_PREVIEW_UNKNOWN")
        self.assertEqual(self.service.prune_calls, [])

    def test_preview_days_are_exact_integers_and_required(self) -> None:
        for payload in ({}, {"days": True}, {"days": 30.0}, {"days": "30"}):
            with self.assertRaises(worker.WorkerError) as raised:
                worker.handle_preview_prune_jobs(payload, self.context)
            self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")

    def test_token_is_bound_to_days_and_remains_usable_after_mismatch(self) -> None:
        preview = self.preview(30)
        with self.assertRaises(worker.WorkerError) as raised:
            self.confirm(90, preview["previewToken"])
        self.assertEqual(raised.exception.code, "PRUNE_PREVIEW_MISMATCH")
        self.assertEqual(self.service.prune_calls, [])

        result = self.confirm(30, preview["previewToken"])
        self.assertFalse(result["dryRun"])
        self.assertEqual(result["removedCount"], 1)

    def test_worker_restart_discards_confirmation_tokens(self) -> None:
        preview = self.preview()
        restarted_context = worker.Context("synthetic-demo.json", str(Path(self.temp.name) / "jobs"))
        with self.assertRaises(worker.WorkerError) as raised:
            worker.handle_confirm_prune_jobs(
                {"days": 30, "previewToken": preview["previewToken"]}, restarted_context
            )
        self.assertEqual(raised.exception.code, "PRUNE_PREVIEW_UNKNOWN")
        self.assertEqual(self.service.prune_calls, [])

    def test_expired_token_is_rejected_without_calling_service(self) -> None:
        with patch.object(worker._time, "monotonic", return_value=100.0):
            preview = self.preview()
        with patch.object(worker._time, "monotonic", return_value=701.0):
            with self.assertRaises(worker.WorkerError) as raised:
                self.confirm(token=preview["previewToken"])
        self.assertEqual(raised.exception.code, "PRUNE_PREVIEW_EXPIRED")
        self.assertEqual(self.service.prune_calls, [])

    def test_token_is_consumed_before_execution_and_cannot_be_replayed(self) -> None:
        preview = self.preview()
        first = self.confirm(token=preview["previewToken"])
        self.assertEqual(first["removedCount"], 1)
        self.assertEqual(first["skippedReasonCounts"], {"protection_failed": 1})
        self.assertEqual(len(self.service.prune_calls), 1)
        with self.assertRaises(worker.WorkerError) as replay:
            self.confirm(token=preview["previewToken"])
        self.assertEqual(replay.exception.code, "PRUNE_PREVIEW_UNKNOWN")
        self.assertEqual(len(self.service.prune_calls), 1)

    def test_preview_without_confirm_has_no_delete_side_effect(self) -> None:
        self.preview()
        self.assertEqual(self.service.prune_calls, [])

    def test_partial_delete_failure_is_preserved_in_bounded_protocol_summary(self) -> None:
        # The task service can fail after deleting a file but before removing the
        # whole job. Thus removed is empty even though deletion may have started.
        self.service.prune_result_override = {
            "dryRun": False,
            "days": 30,
            "cutoff": "2020-02-01T00:00:00Z",
            "removed": [],
            "skipped": [{"jobId": "synthetic-partial", "reason": "delete_failed"}],
        }
        output = io.BytesIO()

        class AdaptiveInput:
            calls = 0

            def readline(self, limit: int = -1) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    wire = json.dumps(request("preview-partial", "preview_prune_jobs", {"days": 30})).encode() + b"\n"
                elif self.calls == 2:
                    preview_response = json.loads(output.getvalue().decode("utf-8").splitlines()[-1])
                    token = preview_response["result"]["previewToken"]
                    wire = json.dumps(request("confirm-partial", "confirm_prune_jobs", {
                        "days": 30, "previewToken": token,
                    })).encode() + b"\n"
                else:
                    wire = json.dumps(request("shutdown-partial", "shutdown")).encode() + b"\n"
                if limit > 0 and len(wire) > limit:
                    raise AssertionError("synthetic request exceeded worker limit")
                return wire

        exit_code = worker.serve(
            AdaptiveInput(), output, "synthetic-demo.json", str(Path(self.temp.name) / "jobs")
        )
        responses = [json.loads(line) for line in output.getvalue().decode("utf-8").splitlines()]
        self.assertEqual(exit_code, 0)
        self.assertEqual([item["requestId"] for item in responses], [
            "preview-partial", "confirm-partial", "shutdown-partial",
        ])
        shared_protocol_result = json.loads(
            (ROOT / "tests" / "NativeApp.Tests" / "fixtures" / "partial_prune_result.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(responses[1]["result"], shared_protocol_result)
        self.assertEqual(len(self.service.prune_calls), 1)

    def test_protocol_uses_compact_token_for_snapshot_larger_than_request_limit(self) -> None:
        self.service = FakeTaskService(job_count=24_000)
        self.service_patch.stop()
        large_service_patch = patch.object(worker, "_task_service", return_value=(self.service, FakeTaskError))
        large_service_patch.start()
        self.addCleanup(large_service_patch.stop)

        output = io.BytesIO()

        class AdaptiveInput:
            calls = 0
            confirm_wire = b""

            def readline(self, limit: int = -1) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    wire = json.dumps(request("preview", "preview_prune_jobs", {"days": 30})).encode() + b"\n"
                elif self.calls == 2:
                    preview_response = json.loads(output.getvalue().decode("utf-8").splitlines()[-1])
                    token = preview_response["result"]["previewToken"]
                    wire = json.dumps(request("confirm", "confirm_prune_jobs", {"days": 30, "previewToken": token})).encode() + b"\n"
                    self.confirm_wire = wire
                else:
                    wire = json.dumps(request("shutdown", "shutdown")).encode() + b"\n"
                if limit > 0 and len(wire) > limit:
                    raise AssertionError("synthetic request exceeded worker limit")
                return wire

        input_stream = AdaptiveInput()
        self.assertEqual(worker.serve(input_stream, output, "synthetic-demo.json", str(Path(self.temp.name) / "jobs")), 0)
        responses = [json.loads(line) for line in output.getvalue().decode("utf-8").splitlines()]
        self.assertEqual([item["requestId"] for item in responses], ["preview", "confirm", "shutdown"])
        preview_result = responses[0]["result"]
        confirm_result = responses[1]["result"]

        snapshot = self.service.prune_calls[0][1]
        serialized_snapshot = json.dumps(snapshot, separators=(",", ":")).encode("utf-8")
        self.assertGreater(len(serialized_snapshot), worker.MAX_REQUEST_BYTES)
        self.assertLess(len(input_stream.confirm_wire), 512)
        self.assertNotIn(b"snapshot", input_stream.confirm_wire)
        self.assertNotIn("snapshot", preview_result)
        self.assertLess(len(json.dumps(preview_result).encode("utf-8")), 4096)
        self.assertEqual(confirm_result["removedCount"], 24_000)
        self.assertEqual(confirm_result["skippedReasonCounts"], {"protection_failed": 1})
        self.assertEqual(len(snapshot["jobs"]), 24_000)
        self.assertEqual(snapshot["jobs"], self.service.jobs)


if __name__ == "__main__":
    unittest.main()
