from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _bootstrap import SRC  # noqa: F401
from model_test_agent.config import Settings
from model_test_agent.document_loader import DocumentContent
from model_test_agent.models import WorkflowSpec
from model_test_agent.planner import WorkflowPlanner
from model_test_agent.workflow_normalizer import WorkflowNormalizer


class FakePlannerClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def complete_json(self, *, model: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
        del model, system_prompt, user_prompt
        return self.payload


class FakeStreamingPlannerClient(FakePlannerClient):
    def chat(self, *, model: str, messages, stream: bool = False, on_delta=None, **kwargs):
        del model, messages, kwargs
        if stream and on_delta is not None:
            on_delta({"type": "content_start"})
            on_delta({"type": "content_delta", "text": "I will first identify the service launch. "})
            on_delta({"type": "content_delta", "text": "Then I will map waits and cleanup."})
            on_delta({"type": "content_end"})
        return None


class PlannerTests(unittest.TestCase):
    def test_planner_defaults_placeholder_local_workdir_to_invocation_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            document_path = Path(tmpdir) / "runbook.md"
            document_path.write_text("# Runbook\n\n```bash\npython3 server.py\n```")
            planner = WorkflowPlanner(
                Settings(
                    base_url="http://example.com/v1",
                    api_key="",
                    model="test-model",
                    planner_model="test-model",
                    agent_model="test-model",
                ),
                client=FakePlannerClient(
                    {
                        "name": "demo",
                        "objective": "demo",
                        "sessions": {
                            "server": {
                                "transport": "local",
                                "shell": "/bin/bash",
                                "workdir": "/workspace",
                            }
                        },
                        "steps": [
                            {
                                "id": "launch_server",
                                "kind": "command",
                                "title": "Launch server",
                                "session": "server",
                                "command": "python3 server.py",
                            }
                        ],
                    }
                ),
            )

            workflow = planner.plan(
                DocumentContent(
                    path=document_path,
                    media_type="text/markdown",
                    text=document_path.read_text(),
                )
            )
            self.assertEqual(workflow.sessions["server"].workdir, str(Path.cwd().resolve()))

    def test_normalizer_moves_workdir_to_repo_root_when_command_path_is_repo_relative(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        workflow = WorkflowSpec.from_dict(
            {
                "name": "demo",
                "objective": "demo",
                "sessions": {
                    "server": {
                        "transport": "local",
                        "shell": "/bin/bash",
                        "workdir": str(project_root / "examples"),
                    }
                },
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch server",
                        "session": "server",
                        "command": "python3 examples/slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30",
                    }
                ],
            }
        )

        normalized = WorkflowNormalizer().normalize(
            workflow,
            project_root / "server_workflow.json",
            invocation_cwd=project_root,
        )
        self.assertEqual(normalized.sessions["server"].workdir, str(project_root))

    def test_normalizer_uses_document_dir_when_relative_command_exists_there(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            doc_dir = base / "docs"
            doc_dir.mkdir()
            (doc_dir / "server.py").write_text("print('ok')\n")
            workflow = WorkflowSpec.from_dict(
                {
                    "name": "demo",
                    "objective": "demo",
                    "sessions": {"server": {"transport": "local", "shell": "/bin/bash", "workdir": "/workspace"}},
                    "steps": [
                        {
                            "id": "launch_server",
                            "kind": "command",
                            "title": "Launch server",
                            "session": "server",
                            "command": "python3 server.py",
                        }
                    ],
                }
            )

            normalized = WorkflowNormalizer().normalize(
                workflow,
                doc_dir / "runbook.md",
                invocation_cwd=base,
            )
            self.assertEqual(normalized.sessions["server"].workdir, str(doc_dir.resolve()))

    def test_planner_emits_analysis_and_streaming_progress_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            document_path = Path(tmpdir) / "runbook.md"
            document_path.write_text(
                "# Runbook\n\n"
                "## Start Server\n"
                "```bash\npython3 app.py --startup-delay 30\n```\n\n"
                "## Check Health\n"
                "```bash\ncurl --fail --silent http://127.0.0.1:8080/healthz\n```\n"
            )
            events: list[dict[str, object]] = []
            planner = WorkflowPlanner(
                Settings(
                    base_url="http://example.com/v1",
                    api_key="",
                    model="test-model",
                    planner_model="test-model",
                    agent_model="test-model",
                ),
                client=FakeStreamingPlannerClient(
                    {
                        "name": "demo",
                        "objective": "demo",
                        "sessions": {"server": {"transport": "local", "workdir": "/workspace"}},
                        "steps": [
                            {
                                "id": "launch_server",
                                "kind": "command",
                                "title": "Launch server",
                                "session": "server",
                                "command": "python3 app.py --startup-delay 30",
                            }
                        ],
                    }
                ),
                progress_callback=events.append,
            )

            planner.plan(
                DocumentContent(
                    path=document_path,
                    media_type="text/markdown",
                    text=document_path.read_text(),
                )
            )

            names = [str(item.get("event")) for item in events]
            self.assertIn("document_analysis", names)
            self.assertIn("planning_model_call", names)
            self.assertIn("planner_stream_started", names)
            self.assertIn("planner_stream_delta", names)
            self.assertIn("planner_stream_finished", names)


if __name__ == "__main__":
    unittest.main()
