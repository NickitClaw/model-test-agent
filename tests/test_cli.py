from __future__ import annotations

import unittest

from _bootstrap import SRC  # noqa: F401
from model_test_agent.cli import _derive_run_status, _format_run_summary


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


if __name__ == "__main__":
    unittest.main()
