from __future__ import annotations

import unittest
from types import SimpleNamespace

from _bootstrap import SRC  # noqa: F401
from model_test_agent.agent import ModelTestAgent
from model_test_agent.config import Settings
from model_test_agent.models import WorkflowSpec


class FakeExecutor:
    def __init__(self) -> None:
        self.workflow = WorkflowSpec.from_dict(
            {
                "name": "demo",
                "objective": "demo",
                "sessions": {},
                "steps": [],
            }
        )

    def drain_notifications(self) -> list[dict[str, object]]:
        return []

    def all_steps_finished(self) -> bool:
        return True

    def describe_state(self) -> dict[str, object]:
        return {
            "run": {"id": "demo", "backend": "pty", "log_dir": "/tmp/demo"},
            "workflow": self.workflow.to_dict(),
            "steps": [
                {
                    "id": "launch_server",
                    "status": "failed",
                    "title": "Launch server",
                    "result": {"summary": "Timed out waiting for readiness"},
                }
            ],
            "sessions": [],
            "background_tasks": [],
        }

    def list_steps(self, *, status: str | None = None, only_ready: bool = False) -> list[dict[str, object]]:
        steps = self.describe_state()["steps"]
        if status is not None:
            steps = [item for item in steps if item["status"] == status]
        if only_ready:
            return []
        return steps

    def capture_session(self, *, session_name: str, lines: int | None = None) -> dict[str, object]:
        del session_name, lines
        return {"output": ""}


class FakeClient:
    def chat(self, *, model: str, messages: list[dict[str, object]], tools: list[dict[str, object]]):
        del model, messages, tools
        return SimpleNamespace(message={"content": "Everything passed", "tool_calls": []})


class AgentTests(unittest.TestCase):
    def test_agent_does_not_report_completed_when_workflow_has_failed_steps(self) -> None:
        settings = Settings(
            base_url="http://example.com/v1",
            api_key="",
            model="test-model",
            planner_model="test-model",
            agent_model="test-model",
        )
        agent = ModelTestAgent(
            settings=settings,
            workflow=WorkflowSpec.from_dict({"name": "demo", "objective": "demo", "sessions": {}, "steps": []}),
            executor=FakeExecutor(),
            client=FakeClient(),
        )

        report = agent.run()
        self.assertEqual(report.status, "failed")
        self.assertIn("launch_server", report.summary)


if __name__ == "__main__":
    unittest.main()
