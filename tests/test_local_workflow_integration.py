from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from _bootstrap import SRC  # noqa: F401
from model_test_agent.config import Settings
from model_test_agent.models import WorkflowSpec
from model_test_agent.runtime.executor import WorkflowExecutor


@unittest.skipIf(os.name == "nt", "Local PTY integration test requires a POSIX platform")
class LocalWorkflowIntegrationTests(unittest.TestCase):
    def test_local_mock_workflow(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmpdir:
            notes_path = Path(tmpdir) / "notes.txt"
            workflow = WorkflowSpec.from_dict(
                {
                    "name": "local-mock-benchmark-test",
                    "objective": "Validate PTY workflow execution end to end",
                    "sessions": {
                        "server": {"transport": "local", "shell": "/bin/bash", "workdir": str(project_root)},
                        "client": {"transport": "local", "shell": "/bin/bash", "workdir": str(project_root)},
                    },
                    "steps": [
                        {
                            "id": "launch_server",
                            "kind": "command",
                            "title": "Launch mock server",
                            "session": "server",
                            "command": "python3 examples/mock_server.py",
                            "background": True,
                            "ready_pattern": "SERVER READY",
                            "timeout_s": 10,
                        },
                        {
                            "id": "wait_for_server",
                            "kind": "barrier",
                            "title": "Wait for server",
                            "wait_for": ["launch_server"],
                            "timeout_s": 10,
                        },
                        {
                            "id": "run_client",
                            "kind": "command",
                            "title": "Run mock client",
                            "session": "client",
                            "depends_on": ["wait_for_server"],
                            "command": "python3 examples/mock_client.py",
                            "success_patterns": ["Throughput:", "Latency:"],
                            "timeout_s": 10,
                        },
                        {
                            "id": "open_notes",
                            "kind": "command",
                            "title": "Open note writer",
                            "session": "client",
                            "depends_on": ["run_client"],
                            "command": f"cat > {notes_path}",
                            "background": True,
                        },
                        {
                            "id": "write_notes",
                            "kind": "send_keys",
                            "title": "Write notes",
                            "session": "client",
                            "depends_on": ["wait_writer_ready"],
                            "keys": ["integration test ok\n", "Throughput verified.\n", "C-d"],
                        },
                        {
                            "id": "wait_writer_ready",
                            "kind": "sleep",
                            "title": "Allow writer to attach",
                            "depends_on": ["open_notes"],
                            "seconds": 0.2,
                        },
                        {
                            "id": "wait_shell_return",
                            "kind": "sleep",
                            "title": "Wait for shell to return",
                            "depends_on": ["write_notes"],
                            "seconds": 0.2,
                        },
                        {
                            "id": "verify_notes",
                            "kind": "command",
                            "title": "Verify note contents",
                            "session": "client",
                            "depends_on": ["wait_shell_return"],
                            "command": f"grep -n 'Throughput verified.' {notes_path}",
                            "success_patterns": ["2:Throughput verified."],
                            "timeout_s": 10,
                        },
                        {
                            "id": "stop_server",
                            "kind": "send_keys",
                            "title": "Stop server",
                            "session": "server",
                            "depends_on": ["verify_notes"],
                            "keys": ["C-c"],
                        },
                    ],
                }
            )
            settings = Settings(
                base_url="http://example.com/v1",
                api_key="",
                model="test-model",
                planner_model="test-model",
                agent_model="test-model",
                session_backend="pty",
                poll_interval_s=0.05,
                default_timeout_s=10,
            )
            executor = WorkflowExecutor(workflow, settings)
            try:
                for step_id in [
                    "launch_server",
                    "wait_for_server",
                    "run_client",
                    "open_notes",
                    "wait_writer_ready",
                    "write_notes",
                    "wait_shell_return",
                    "verify_notes",
                    "stop_server",
                ]:
                    result = executor.run_step(step_id)
                    self.assertIn(result["status"], {"background", "completed"}, msg=f"{step_id}: {result}")
                time.sleep(0.2)
                self.assertTrue(notes_path.exists())
                content = notes_path.read_text()
                self.assertIn("integration test ok", content)
                self.assertIn("Throughput verified.", content)

                state = executor.describe_state()
                self.assertTrue(Path(state["run"]["log_dir"]).is_dir())
                sessions = {item["name"]: item for item in state["sessions"]}
                server_log = Path(sessions["server"]["combined_log_path"])
                client_stdout_log = Path(sessions["client"]["stdout_log_path"])
                client_stderr_log = Path(sessions["client"]["stderr_log_path"])

                self.assertTrue(server_log.exists())
                self.assertTrue(client_stdout_log.exists())
                self.assertTrue(client_stderr_log.exists())
                self.assertIn("SERVER READY", server_log.read_text())
                self.assertIn("Throughput: 12.5 req/s", client_stdout_log.read_text())
            finally:
                executor.close_all()


if __name__ == "__main__":
    unittest.main()
