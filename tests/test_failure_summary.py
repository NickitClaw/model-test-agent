from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _bootstrap import SRC  # noqa: F401
from model_test_agent.runtime.failure_summary import FailureSummaryBuilder


class FailureSummaryBuilderTests(unittest.TestCase):
    def test_collect_prefers_step_output_excerpt(self) -> None:
        builder = FailureSummaryBuilder()
        rows = builder.collect(
            steps=[
                {
                    "id": "probe_health",
                    "title": "Probe health endpoint",
                    "session": "client",
                    "status": "failed",
                    "result": {
                        "summary": "Probe timed out",
                        "output": "attempt 1\ncurl: (7) Failed to connect to 127.0.0.1 port 18081\nattempt 2",
                    },
                }
            ],
            sessions=[],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_kind"], "step_output")
        self.assertIn("Failed to connect", rows[0]["excerpt"])

    def test_collect_falls_back_to_stderr_log(self) -> None:
        builder = FailureSummaryBuilder()
        with tempfile.TemporaryDirectory() as tmpdir:
            stderr_path = Path(tmpdir) / "stderr.log"
            stderr_path.write_text(
                "booting\nTraceback (most recent call last):\nValueError: bad launch args\n"
            )
            rows = builder.collect(
                steps=[
                    {
                        "id": "launch_server",
                        "title": "Launch server",
                        "session": "server",
                        "status": "failed",
                        "result": {
                            "summary": "Command exited with 1",
                            "output": "",
                        },
                    }
                ],
                sessions=[
                    {
                        "name": "server",
                        "stderr_log_path": str(stderr_path),
                        "stdout_log_path": str(Path(tmpdir) / "stdout.log"),
                        "combined_log_path": str(Path(tmpdir) / "session.log"),
                    }
                ],
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_kind"], "stderr")
        self.assertEqual(rows[0]["source_path"], str(stderr_path))
        self.assertIn("Traceback", rows[0]["excerpt"])


if __name__ == "__main__":
    unittest.main()
