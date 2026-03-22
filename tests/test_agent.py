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


class FakeHealthyExecutor(FakeExecutor):
    def describe_state(self) -> dict[str, object]:
        return {
            "run": {"id": "demo", "backend": "pty", "log_dir": "/tmp/demo"},
            "workflow": self.workflow.to_dict(),
            "steps": [],
            "sessions": [],
            "background_tasks": [],
        }

    def list_steps(self, *, status: str | None = None, only_ready: bool = False) -> list[dict[str, object]]:
        del status, only_ready
        return []


class FakeClient:
    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        stream: bool = False,
        on_delta=None,
        **_: object,
    ):
        del model, messages, tools, stream, on_delta
        return SimpleNamespace(message={"content": "Everything passed", "tool_calls": []})


class FakeStreamingClient(FakeClient):
    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
        stream: bool = False,
        on_delta=None,
        **_: object,
    ):
        del model, messages, tools
        if stream and on_delta is not None:
            on_delta({"type": "content_start"})
            on_delta({"type": "content_delta", "text": "I will inspect the state first. "})
            on_delta({"type": "content_delta", "text": "Then I will run the next ready step."})
            on_delta({"type": "content_end"})
        return SimpleNamespace(
            message={
                "content": "I will inspect the state first. Then I will run the next ready step.",
                "tool_calls": [],
            }
        )


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

    def test_agent_emits_operator_note_when_model_returns_content(self) -> None:
        settings = Settings(
            base_url="http://example.com/v1",
            api_key="",
            model="test-model",
            planner_model="test-model",
            agent_model="test-model",
        )
        events: list[dict[str, object]] = []
        agent = ModelTestAgent(
            settings=settings,
            workflow=WorkflowSpec.from_dict({"name": "demo", "objective": "demo", "sessions": {}, "steps": []}),
            executor=FakeHealthyExecutor(),
            client=FakeClient(),
            progress_callback=events.append,
        )

        agent.run()
        note_events = [item for item in events if item.get("event") == "agent_note"]
        self.assertEqual(len(note_events), 1)
        self.assertEqual(note_events[0]["content"], "Everything passed")

    def test_agent_emits_stream_events_when_client_streams_content(self) -> None:
        settings = Settings(
            base_url="http://example.com/v1",
            api_key="",
            model="test-model",
            planner_model="test-model",
            agent_model="test-model",
        )
        events: list[dict[str, object]] = []
        agent = ModelTestAgent(
            settings=settings,
            workflow=WorkflowSpec.from_dict({"name": "demo", "objective": "demo", "sessions": {}, "steps": []}),
            executor=FakeHealthyExecutor(),
            client=FakeStreamingClient(),
            progress_callback=events.append,
        )

        agent.run()
        event_names = [str(item.get("event")) for item in events]
        self.assertIn("agent_stream_started", event_names)
        self.assertIn("agent_stream_delta", event_names)
        self.assertIn("agent_stream_finished", event_names)
        self.assertNotIn("agent_note", event_names)


if __name__ == "__main__":
    unittest.main()
