import unittest

from _bootstrap import SRC  # noqa: F401
from model_test_agent.models import StepKind, WorkflowSpec


class WorkflowModelTests(unittest.TestCase):
    def test_workflow_roundtrip(self) -> None:
        payload = {
            "name": "demo",
            "objective": "run demo workflow",
            "sessions": {
                "server": {
                    "transport": "local",
                    "workdir": "/tmp/demo",
                }
            },
            "steps": [
                {
                    "id": "launch",
                    "kind": "command",
                    "title": "Launch server",
                    "session": "server",
                    "command": "python -m http.server",
                    "background": True,
                    "ready_pattern": "Serving HTTP",
                },
                {
                    "id": "wait",
                    "kind": "barrier",
                    "title": "Wait for launch",
                    "wait_for": ["launch"],
                },
            ],
        }
        workflow = WorkflowSpec.from_dict(payload)
        self.assertEqual(workflow.name, "demo")
        self.assertIs(workflow.steps[0].kind, StepKind.COMMAND)
        self.assertEqual(workflow.to_dict()["steps"][0]["ready_pattern"], "Serving HTTP")


if __name__ == "__main__":
    unittest.main()
