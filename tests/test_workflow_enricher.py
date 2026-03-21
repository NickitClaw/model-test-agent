from __future__ import annotations

import unittest

from _bootstrap import SRC  # noqa: F401
from model_test_agent.models import CommandStep, ProbeStep, SendKeysStep, WorkflowSpec
from model_test_agent.workflow_enricher import WorkflowEnricher


class WorkflowEnricherTests(unittest.TestCase):
    def test_enricher_adds_probe_and_cleanup_for_minimal_server_workflow(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "minimal",
                "objective": "minimal server doc",
                "sessions": {
                    "server": {"transport": "local"},
                    "client": {"transport": "local"},
                },
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch server",
                        "session": "server",
                        "command": "python3 examples/slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30",
                    },
                    {
                        "id": "curl_healthz",
                        "kind": "command",
                        "title": "Call service",
                        "session": "client",
                        "command": "curl --fail --silent http://127.0.0.1:18081/healthz",
                    },
                ],
            }
        )
        enriched = WorkflowEnricher().enrich(workflow)
        launch = next(step for step in enriched.steps if step.id == "launch_server")
        self.assertIsInstance(launch, CommandStep)
        self.assertTrue(launch.background)

        probe = next(step for step in enriched.steps if isinstance(step, ProbeStep))
        self.assertEqual(probe.session, "client")
        self.assertIn("curl --fail --silent http://127.0.0.1:18081/healthz", probe.command)
        self.assertIn("launch_server", probe.depends_on)

        curl_step = next(step for step in enriched.steps if step.id == "curl_healthz")
        self.assertIn(probe.id, curl_step.depends_on)

        cleanup = next(step for step in enriched.steps if isinstance(step, SendKeysStep) and "C-c" in step.keys)
        self.assertEqual(cleanup.session, "server")
        self.assertIn("curl_healthz", cleanup.depends_on)

    def test_enricher_replaces_ambiguous_ready_pattern_with_probe(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "ambiguous-ready-pattern",
                "objective": "minimal server doc",
                "sessions": {
                    "server": {"transport": "local"},
                    "client": {"transport": "local"},
                },
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch server",
                        "session": "server",
                        "command": "python3 examples/slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30",
                        "background": True,
                        "ready_pattern": "listening",
                    },
                    {
                        "id": "curl_healthz",
                        "kind": "command",
                        "title": "Call service",
                        "session": "client",
                        "command": "curl --fail --silent http://127.0.0.1:18081/healthz",
                    },
                ],
            }
        )

        enriched = WorkflowEnricher().enrich(workflow)
        launch = next(step for step in enriched.steps if step.id == "launch_server")
        self.assertIsInstance(launch, CommandStep)
        self.assertIsNone(launch.ready_pattern)

        probe = next(step for step in enriched.steps if isinstance(step, ProbeStep))
        self.assertIn("launch_server", probe.depends_on)
        curl_step = next(step for step in enriched.steps if step.id == "curl_healthz")
        self.assertIn(probe.id, curl_step.depends_on)

    def test_enricher_clears_ambiguous_ready_pattern_when_probe_already_exists(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "preplanned-probe",
                "objective": "minimal server doc",
                "sessions": {"server": {"transport": "local"}},
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch server",
                        "session": "server",
                        "command": "python3 examples/slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30",
                        "background": True,
                        "ready_pattern": "listening",
                    },
                    {
                        "id": "wait_for_health",
                        "kind": "probe",
                        "title": "Wait for health endpoint",
                        "session": "server",
                        "depends_on": ["launch_server"],
                        "command": "curl --fail --silent http://127.0.0.1:18081/healthz",
                    },
                ],
            }
        )

        enriched = WorkflowEnricher().enrich(workflow)
        launch = next(step for step in enriched.steps if step.id == "launch_server")
        self.assertIsNone(launch.ready_pattern)

    def test_enricher_moves_same_session_probe_and_client_off_server_session(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "same-session-client",
                "objective": "minimal server doc",
                "sessions": {"server": {"transport": "local"}},
                "steps": [
                    {
                        "id": "launch_server",
                        "kind": "command",
                        "title": "Launch server",
                        "session": "server",
                        "command": "python3 examples/slow_start_http_server.py --host 127.0.0.1 --port 18081 --startup-delay 30",
                        "background": True,
                        "ready_pattern": "listening",
                    },
                    {
                        "id": "wait_for_health",
                        "kind": "probe",
                        "title": "Wait for health endpoint",
                        "session": "server",
                        "depends_on": ["launch_server"],
                        "command": "curl --fail --silent http://127.0.0.1:18081/healthz",
                    },
                    {
                        "id": "run_client",
                        "kind": "command",
                        "title": "Call service",
                        "session": "server",
                        "depends_on": ["wait_for_health"],
                        "command": "curl --fail --silent http://127.0.0.1:18081/healthz",
                    },
                    {
                        "id": "cleanup",
                        "kind": "command",
                        "title": "Stop server",
                        "session": "server",
                        "depends_on": ["run_client"],
                        "command": "pkill -f slow_start_http_server.py",
                    },
                ],
            }
        )

        enriched = WorkflowEnricher().enrich(workflow)
        self.assertIn("server_client", enriched.sessions)
        probe = next(step for step in enriched.steps if step.id == "wait_for_health")
        curl = next(step for step in enriched.steps if step.id == "run_client")
        cleanup = next(step for step in enriched.steps if step.id == "cleanup")
        self.assertEqual(probe.session, "server_client")
        self.assertEqual(curl.session, "server_client")
        self.assertEqual(cleanup.session, "server_client")


if __name__ == "__main__":
    unittest.main()
