from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from _bootstrap import SRC  # noqa: F401
from model_test_agent.config import Settings
from model_test_agent.models import WorkflowSpec
from model_test_agent.runtime.background import BackgroundTaskManager
from model_test_agent.runtime.executor import WorkflowExecutor
from model_test_agent.runtime.tmux import TmuxCommandResult, TmuxClient, WaitResult


class FakeTmux(TmuxClient):
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.commands: list[tuple[str, str]] = []
        self.wait_calls: list[tuple[str, str]] = []
        self.combined_logs: dict[str, str] = {}

    def session_exists(self, session_name: str) -> bool:
        return session_name in self.sessions

    def create_session(self, session_name: str, shell: str = "/bin/bash") -> None:
        self.sessions[session_name] = shell

    def kill_session(self, session_name: str) -> None:
        self.sessions.pop(session_name, None)

    def send_literal(self, session_name: str, text: str, enter: bool = True) -> None:
        self.commands.append((session_name, text))

    def send_keys(self, session_name: str, keys: list[str], press_enter: bool = False) -> None:
        self.commands.append((session_name, " ".join(keys)))

    def capture_pane(self, session_name: str, lines: int = 300) -> str:
        return "Application startup complete\nThroughput: 10 req/s"

    def attach_combined_log(self, session_name: str, log_path: str) -> None:
        self.combined_logs[session_name] = log_path

    def wait_for_pattern(
        self,
        session_name: str,
        pattern: str,
        *,
        timeout_s: int,
        fail_patterns: list[str] | None = None,
        lines: int = 300,
    ) -> WaitResult:
        self.wait_calls.append((session_name, pattern))
        time.sleep(0.02)
        return WaitResult(status="matched", output=self.capture_pane(session_name, lines), matched_pattern=pattern)

    def run_command(
        self,
        session_name: str,
        command: str,
        *,
        timeout_s: int,
        lines: int = 300,
    ) -> TmuxCommandResult:
        self.commands.append((session_name, command))
        return TmuxCommandResult(exit_code=0, output="Throughput: 10 req/s")

    @staticmethod
    def build_export_commands(env: dict[str, str]) -> list[str]:
        return [f"export {key}={value}" for key, value in env.items()]


class FailingSetupTmux(FakeTmux):
    def run_command(
        self,
        session_name: str,
        command: str,
        *,
        timeout_s: int,
        lines: int = 300,
    ) -> TmuxCommandResult:
        if command.startswith("cd "):
            self.commands.append((session_name, command))
            return TmuxCommandResult(exit_code=1, output="bash: cd: /missing: No such file or directory")
        return super().run_command(session_name, command, timeout_s=timeout_s, lines=lines)


class ExecutorTests(unittest.TestCase):
    def test_background_command_and_barrier(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "demo",
                "objective": "demo",
                "sessions": {"server": {"transport": "local"}},
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch server",
                        "session": "server",
                        "command": "python -m server",
                        "background": True,
                        "ready_pattern": "Application startup complete",
                        "timeout_s": 10,
                    },
                    {
                        "id": "wait_server",
                        "kind": "barrier",
                        "title": "Wait for server",
                        "wait_for": ["launch_server"],
                        "timeout_s": 10,
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
        )
        tmux = FakeTmux()
        executor = WorkflowExecutor(
            workflow,
            settings,
            backend=tmux,
            background=BackgroundTaskManager(tmux),
        )
        launch = executor.run_step("launch_server")
        self.assertEqual(launch["status"], "background")
        time.sleep(0.05)
        barrier = executor.run_step("wait_server")
        self.assertEqual(barrier["status"], "completed")

    def test_session_logs_are_created_and_commands_are_wrapped(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "log-demo",
                "objective": "demo",
                "sessions": {"runner": {"transport": "local"}},
                "steps": [
                    {
                        "id": "run_check",
                        "kind": "command",
                        "title": "Run check",
                        "session": "runner",
                        "command": "printf 'hello\\n'; printf 'oops\\n' >&2",
                    }
                ],
            }
        )
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                base_url="http://example.com/v1",
                api_key="",
                model="test-model",
                planner_model="test-model",
                agent_model="test-model",
                session_backend="pty",
                log_root=tmpdir,
            )
            tmux = FakeTmux()
            executor = WorkflowExecutor(workflow, settings, backend=tmux)
            result = executor.run_step("run_check")
            self.assertEqual(result["status"], "completed")

            state = executor.describe_state()
            self.assertTrue(Path(state["run"]["log_dir"]).is_dir())
            self.assertTrue(Path(state["run"]["event_log_path"]).exists())
            session = state["sessions"][0]
            self.assertTrue(Path(session["combined_log_path"]).exists())
            self.assertTrue(Path(session["stdout_log_path"]).exists())
            self.assertTrue(Path(session["stderr_log_path"]).exists())
            self.assertEqual(tmux.combined_logs[session["backend_session_name"]], session["combined_log_path"])

            wrapped_command = tmux.commands[-1][1]
            self.assertIn("tee -a", wrapped_command)
            self.assertIn(session["stdout_log_path"], wrapped_command)
            self.assertIn(session["stderr_log_path"], wrapped_command)

            executor.write_summary_artifact(status="completed", summary="ok", state=state)
            self.assertTrue(Path(state["run"]["summary_path"]).exists())

    def test_session_log_directories_use_clear_role_names(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "clear-log-names",
                "objective": "demo",
                "sessions": {
                    "workspace": {"transport": "local"},
                    "workspace_client": {"transport": "local"},
                },
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch server",
                        "session": "workspace",
                        "command": "python3 slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30",
                        "background": True,
                        "ready_pattern": "SERVER READY",
                    },
                    {
                        "id": "probe_health",
                        "kind": "probe",
                        "title": "Probe health endpoint",
                        "session": "workspace_client",
                        "command": "curl --fail --silent http://127.0.0.1:18081/healthz",
                    },
                ],
            }
        )
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                base_url="http://example.com/v1",
                api_key="",
                model="test-model",
                planner_model="test-model",
                agent_model="test-model",
                session_backend="pty",
                log_root=tmpdir,
            )
            executor = WorkflowExecutor(workflow, settings, backend=FakeTmux())
            executor.run_step("launch_server")
            executor.run_step("probe_health")

            sessions = {item["name"]: item for item in executor.describe_state()["sessions"]}
            self.assertEqual(sessions["workspace"]["log_name"], "server")
            self.assertEqual(sessions["workspace_client"]["log_name"], "client")
            self.assertEqual(Path(sessions["workspace"]["combined_log_path"]).parent.name, "server")
            self.assertEqual(Path(sessions["workspace_client"]["combined_log_path"]).parent.name, "client")

    def test_clean_command_output_removes_logger_noise(self) -> None:
        cleaned = WorkflowExecutor._clean_command_output(
            "[1] 64986\n[2] 64987\n{\"status\":\"ok\"}\n"
            "[1]-  Done                    tee -a /tmp/stdout.log < \"$__mta_stdout_fifo\"\n"
            "[2]+  Done                    tee -a /tmp/stderr.log < \"$__mta_stderr_fifo\" 1>&2\n"
        )
        self.assertEqual(cleaned, "{\"status\":\"ok\"}")

    def test_logged_command_preserves_trailing_background_ampersand(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "ampersand-demo",
                "objective": "demo",
                "sessions": {"runner": {"transport": "local"}},
                "steps": [],
            }
        )
        settings = Settings(
            base_url="http://example.com/v1",
            api_key="",
            model="test-model",
            planner_model="test-model",
            agent_model="test-model",
            session_backend="pty",
        )
        executor = WorkflowExecutor(workflow, settings, backend=FakeTmux())
        wrapped = executor._command_with_session_logging(
            "python3 server.py &",
            type("State", (), {"stdout_log_path": "/tmp/stdout.log", "stderr_log_path": "/tmp/stderr.log"})(),
        )
        self.assertIn("python3 server.py &", wrapped)
        self.assertNotIn("&;", wrapped)

    def test_session_setup_fails_fast_on_bad_workdir(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "bad-workdir",
                "objective": "demo",
                "sessions": {"runner": {"transport": "local", "workdir": "/missing"}},
                "steps": [
                    {
                        "id": "run_check",
                        "kind": "command",
                        "title": "Run check",
                        "session": "runner",
                        "command": "printf 'hello\\n'",
                    }
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
        )
        executor = WorkflowExecutor(workflow, settings, backend=FailingSetupTmux())
        result = executor.run_step("run_check")
        self.assertEqual(result["status"], "failed")
        self.assertIn("changing directory to /missing", result["summary"])


if __name__ == "__main__":
    unittest.main()
