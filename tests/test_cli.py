from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _bootstrap import SRC  # noqa: F401
from model_test_agent.cli import (
    _collect_runs,
    _derive_run_status,
    _format_run_summary,
    _format_runs_table,
    _format_single_run,
    _load_workflow,
    _resolve_run_summary,
)
from model_test_agent.workflow_schema import get_workflow_json_schema


class CliFormattingTests(unittest.TestCase):
    def test_format_run_summary_includes_logs_and_failures(self) -> None:
        summary = _format_run_summary(
            status="failed",
            summary="curl health check failed",
            iterations=4,
            state={
                "run": {
                    "id": "abcd1234",
                    "backend": "pty",
                    "log_dir": "/tmp/mta-run",
                    "failure_excerpts": [
                        {
                            "step_id": "curl_healthz",
                            "source_kind": "stderr",
                            "source_path": "/tmp/mta-run/server/stderr.log",
                            "excerpt": "curl: (7) Failed to connect to 127.0.0.1 port 18081",
                        }
                    ],
                },
                "workflow": {
                    "name": "slow-server-check",
                },
                "steps": [
                    {
                        "id": "launch_server",
                        "title": "Launch server",
                        "status": "completed",
                    },
                    {
                        "id": "curl_healthz",
                        "title": "Check health",
                        "status": "failed",
                        "result": {"summary": "Command exited with 7"},
                    },
                ],
                "sessions": [
                    {
                        "name": "server",
                        "transport": "local",
                        "backend": "pty",
                        "combined_log_path": "/tmp/mta-run/server/session.log",
                        "stdout_log_path": "/tmp/mta-run/server/stdout.log",
                        "stderr_log_path": "/tmp/mta-run/server/stderr.log",
                    }
                ],
                "background_tasks": [],
            },
        )

        self.assertIn("Run Summary", summary)
        self.assertIn("workflow: slow-server-check", summary)
        self.assertIn("status: failed", summary)
        self.assertIn("agent iterations: 4", summary)
        self.assertIn("log dir: /tmp/mta-run", summary)
        self.assertIn("failed steps:", summary)
        self.assertIn("- curl_healthz: Command exited with 7", summary)
        self.assertIn("failure excerpts:", summary)
        self.assertIn("- curl_healthz [stderr] /tmp/mta-run/server/stderr.log", summary)
        self.assertIn("curl: (7) Failed to connect", summary)
        self.assertIn("session logs:", summary)
        self.assertIn("session=/tmp/mta-run/server/session.log", summary)

    def test_format_run_summary_shows_log_alias_when_it_differs(self) -> None:
        summary = _format_run_summary(
            status="completed",
            summary="ok",
            state={
                "run": {"id": "abcd1234", "backend": "pty", "log_dir": "/tmp/mta-run"},
                "workflow": {"name": "demo"},
                "steps": [{"id": "launch_server", "title": "Launch server", "status": "completed"}],
                "sessions": [
                    {
                        "name": "workspace",
                        "log_name": "server",
                        "transport": "local",
                        "backend": "pty",
                        "combined_log_path": "/tmp/mta-run/server/session.log",
                        "stdout_log_path": "/tmp/mta-run/server/stdout.log",
                        "stderr_log_path": "/tmp/mta-run/server/stderr.log",
                    }
                ],
                "background_tasks": [],
            },
        )

        self.assertIn("- workspace [logs: server] (local/pty):", summary)

    def test_derive_run_status_reports_failed_when_any_step_failed(self) -> None:
        status = _derive_run_status(
            {
                "steps": [
                    {"id": "launch_server", "status": "completed"},
                    {"id": "cleanup", "status": "failed"},
                ]
            }
        )
        self.assertEqual(status, "failed")

    def test_schema_contains_expected_top_level_keys(self) -> None:
        schema = get_workflow_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("sessions", schema["properties"])
        self.assertIn("steps", schema["properties"])
        self.assertIn("metadata", schema["properties"])

    def test_load_workflow_validates_before_normalization(self) -> None:
        payload = {
            "name": "demo",
            "objective": "demo",
            "sessions": {"server": {"transport": "local"}},
            "steps": [
                {
                    "id": "launch_server",
                    "kind": "command",
                    "title": "Launch server",
                    "session": "missing",
                    "command": "python3 app.py",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "workflow.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(Exception, "unknown session"):
                _load_workflow(path, normalize=False, enrich=False)

    def test_collect_runs_and_show_run(self) -> None:
        payload = {
            "ts": 123.0,
            "run_id": "abcd1234",
            "workflow": "demo",
            "status": "completed",
            "summary": "ok",
            "iterations": 2,
            "state": {
                "run": {
                    "id": "abcd1234",
                    "backend": "pty",
                    "log_dir": "/tmp/mta-run",
                    "event_log_path": "/tmp/mta-run/events.jsonl",
                    "summary_path": "/tmp/mta-run/summary.json",
                },
                "workflow": {"name": "demo"},
                "steps": [{"id": "launch_server", "title": "Launch server", "status": "completed"}],
                "sessions": [],
                "background_tasks": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            run_dir = root / "20260322-foo-abcd1234"
            run_dir.mkdir()
            (run_dir / "summary.json").write_text(json.dumps(payload))
            rows = _collect_runs(root, limit=5)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["run_id"], "abcd1234")

            resolved = _resolve_run_summary(root=root, selector="abcd1234")
            self.assertEqual(resolved["workflow"], "demo")

            table = _format_runs_table(rows, root=root)
            self.assertIn("abcd1234 | completed | demo", table)
            single = _format_single_run(resolved)
            self.assertIn("Run Summary", single)
            self.assertIn("status: completed", single)


if __name__ == "__main__":
    unittest.main()
