from __future__ import annotations

import unittest

from _bootstrap import SRC  # noqa: F401
from model_test_agent.workflow_validation import WorkflowValidationError, build_and_validate_workflow


class WorkflowValidationTests(unittest.TestCase):
    def test_validation_rejects_simple_dependency_cycle(self) -> None:
        payload = {
            "name": "cycle-demo",
            "objective": "demo",
            "sessions": {"shell": {"transport": "local"}},
            "steps": [
                {
                    "id": "step_a",
                    "kind": "command",
                    "title": "Step A",
                    "session": "shell",
                    "depends_on": ["step_b"],
                    "command": "echo a",
                },
                {
                    "id": "step_b",
                    "kind": "command",
                    "title": "Step B",
                    "session": "shell",
                    "depends_on": ["step_a"],
                    "command": "echo b",
                },
            ],
        }

        with self.assertRaisesRegex(WorkflowValidationError, "dependency cycle: step_a -> step_b -> step_a"):
            build_and_validate_workflow(payload)

    def test_validation_rejects_barrier_cycle(self) -> None:
        payload = {
            "name": "barrier-cycle",
            "objective": "demo",
            "sessions": {"shell": {"transport": "local"}},
            "steps": [
                {
                    "id": "launch_server",
                    "kind": "command",
                    "title": "Launch server",
                    "session": "shell",
                    "depends_on": ["wait_for_server"],
                    "command": "python3 app.py",
                },
                {
                    "id": "wait_for_server",
                    "kind": "barrier",
                    "title": "Wait for server",
                    "wait_for": ["launch_server"],
                },
            ],
        }

        with self.assertRaisesRegex(
            WorkflowValidationError,
            "dependency cycle: launch_server -> wait_for_server -> launch_server",
        ):
            build_and_validate_workflow(payload)


if __name__ == "__main__":
    unittest.main()
