from __future__ import annotations

import unittest
from types import SimpleNamespace

from _bootstrap import SRC  # noqa: F401
from model_test_agent.config import Settings
from model_test_agent.models import SleepStep, StepKind, StepResult, StepStatus
from model_test_agent.runtime.step_handlers import StepExecutionContext
from model_test_agent.runtime.step_runner import StepRunner


class DummySessionManager:
    @staticmethod
    def ensure_session(name: str):
        raise AssertionError(f"ensure_session should not be used in this test: {name}")


class CustomSleepHandler:
    kind = StepKind.SLEEP

    def execute(self, step, ctx: StepExecutionContext) -> StepResult:
        del ctx
        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            summary="custom sleep handler",
        )


class StepRunnerTests(unittest.TestCase):
    def test_custom_handler_can_override_builtin_step_execution(self) -> None:
        settings = Settings(
            base_url="http://example.com/v1",
            api_key="",
            model="test-model",
            planner_model="test-model",
            agent_model="test-model",
        )
        runner = StepRunner(
            settings=settings,
            backend=SimpleNamespace(),
            background=SimpleNamespace(),
            session_manager=DummySessionManager(),
            step_statuses={},
            handlers=[CustomSleepHandler()],
        )

        result = runner.execute_step(
            SleepStep(
                id="sleep_a",
                kind=StepKind.SLEEP,
                title="Sleep A",
                seconds=2.0,
            )
        )

        self.assertEqual(result.summary, "custom sleep handler")


if __name__ == "__main__":
    unittest.main()
