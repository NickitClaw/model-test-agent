from __future__ import annotations

import json
import os
import shutil
import socket
import time
import unittest
from pathlib import Path

from _bootstrap import SRC  # noqa: F401
from model_test_agent.config import Settings
from model_test_agent.models import WorkflowSpec
from model_test_agent.runtime.executor import WorkflowExecutor
from model_test_agent.workflow_enricher import WorkflowEnricher


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@unittest.skipIf(os.name == "nt", "Slow server system test requires a POSIX platform")
@unittest.skipUnless(shutil.which("curl"), "curl is required for the slow server system test")
class SlowServerSystemTests(unittest.TestCase):
    def test_runbook_emphasizes_waiting_for_readiness(self) -> None:
        runbook = (
            Path(__file__).resolve().parents[1] / "examples" / "slow_server_runbook.md"
        ).read_text()
        self.assertIn("--startup-delay 30", runbook)
        self.assertIn("must spend at least **30 seconds**", runbook)
        self.assertIn("Do **not** run the `curl` command early.", runbook)
        self.assertIn("Only continue after the server terminal prints:", runbook)
        self.assertIn("SERVER READY host=127.0.0.1 port=18081 delay=30.0", runbook)

    def test_enriched_minimal_workflow_waits_for_real_30_second_startup(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        port = find_free_port()
        startup_delay = 30.0
        raw_workflow = WorkflowSpec.from_dict(
            {
                "name": "slow-server-system-test-minimal",
                "objective": "Validate inference of omitted wait and cleanup around a real 30-second startup",
                "sessions": {
                    "server": {"transport": "local", "shell": "/bin/bash", "workdir": str(project_root)},
                    "client": {"transport": "local", "shell": "/bin/bash", "workdir": str(project_root)},
                },
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch slow-start server",
                        "session": "server",
                        "command": (
                            "python3 examples/slow_start_http_server.py "
                            f"--host 127.0.0.1 --port {port} --startup-delay {startup_delay}"
                        ),
                    },
                    {
                        "id": "curl_healthz",
                        "kind": "command",
                        "title": "Call healthz",
                        "session": "client",
                        "command": f"curl --fail --silent http://127.0.0.1:{port}/healthz",
                        "success_patterns": ['"status"\\s*:\\s*"ok"', f'"startup_delay_s"\\s*:\\s*{startup_delay:.1f}'],
                        "fail_patterns": ["Connection refused", "Failed to connect", "curl:"],
                        "timeout_s": 15,
                    },
                ],
            }
        )
        workflow = WorkflowEnricher().enrich(raw_workflow)
        step_ids = {step.id for step in workflow.steps}
        self.assertIn("launch_server_wait_ready", step_ids)
        self.assertIn("launch_server_stop", step_ids)
        settings = Settings(
            base_url="http://example.com/v1",
            api_key="",
            model="test-model",
            planner_model="test-model",
            agent_model="test-model",
            session_backend="pty",
            poll_interval_s=0.1,
            default_timeout_s=50,
        )
        executor = WorkflowExecutor(workflow, settings)
        started = time.monotonic()
        try:
            launch = executor.run_step("launch_server")
            self.assertIn(launch["status"], {"background", "completed"}, msg=launch)

            waited = executor.run_step("launch_server_wait_ready")
            ready_elapsed = time.monotonic() - started
            self.assertEqual(waited["status"], "completed", msg=waited)
            self.assertGreaterEqual(
                ready_elapsed,
                startup_delay,
                msg=f"Server became ready too early: waited {ready_elapsed:.2f}s",
            )

            curl = executor.run_step("curl_healthz")
            self.assertEqual(curl["status"], "completed", msg=curl)
            payload = json.loads(curl["output"])
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["startup_delay_s"], startup_delay)

            stopped = executor.run_step("launch_server_stop")
            self.assertEqual(stopped["status"], "completed", msg=stopped)
        finally:
            executor.close_all()


if __name__ == "__main__":
    unittest.main()
