from __future__ import annotations

import io
import unittest

from _bootstrap import SRC  # noqa: F401
from model_test_agent.models import WorkflowSpec
from model_test_agent.progress import ConsoleProgressReporter, summarize_workflow


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class ProgressReporterTests(unittest.TestCase):
    def test_reporter_describes_workflow_and_steps_in_natural_language(self) -> None:
        workflow = WorkflowSpec.from_dict(
            {
                "name": "demo-workflow",
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
                        "command": "python3 app.py --startup-delay 30",
                        "background": True,
                        "ready_pattern": "SERVER READY",
                    },
                    {
                        "id": "probe_health",
                        "kind": "probe",
                        "title": "Probe health",
                        "session": "workspace_client",
                        "command": "curl --fail --silent http://127.0.0.1:8080/healthz",
                    },
                    {
                        "id": "cleanup",
                        "kind": "command",
                        "title": "Cleanup server",
                        "session": "workspace_client",
                        "command": "pkill -f app.py",
                    },
                ],
            }
        )
        stream = io.StringIO()
        reporter = ConsoleProgressReporter(stream=stream)

        reporter.emit({"event": "workflow_planned", **summarize_workflow(workflow)})
        reporter.emit({"event": "workflow_execution_started", **summarize_workflow(workflow)})
        reporter.emit(
            {
                "event": "step_started",
                "step_id": "launch_server",
                "title": "Launch server",
                "kind": "command",
                "session_name": "workspace",
                "index": 1,
                "total": 3,
            }
        )
        reporter.emit(
            {
                "event": "step_finished",
                "step_id": "launch_server",
                "title": "Launch server",
                "kind": "command",
                "session_name": "workspace",
                "status": "background",
                "summary": "Background command dispatched in session workspace",
                "index": 1,
                "total": 3,
            }
        )

        output = stream.getvalue()
        self.assertIn("I organized the document into a workflow named demo-workflow", output)
        self.assertIn("1) bring up the service side of the workflow.", output)
        self.assertIn("2) wait for readiness and synchronize dependent steps.", output)
        self.assertIn("3) clean up long-running sessions.", output)
        self.assertIn("The workflow demo-workflow is ready.", output)
        self.assertIn("I am starting step 1/3: Launch server.", output)
        self.assertIn("Session: workspace.", output)
        self.assertIn("Step 1/3 is now running in the background: Launch server.", output)

    def test_reporter_formats_agent_updates_as_operator_narration(self) -> None:
        stream = io.StringIO()
        reporter = ConsoleProgressReporter(stream=stream)

        reporter.emit({"event": "agent_note", "content": "I will inspect the current state and then start the server."})
        reporter.emit({"event": "agent_tool_call", "tool_name": "run_step", "detail": "step_id=launch_server"})
        reporter.emit(
            {
                "event": "background_notifications",
                "notifications": [{"summary": "Pattern 'SERVER READY' matched in session server"}],
            }
        )

        output = stream.getvalue()
        self.assertIn("I will inspect the current state and then start the server.", output)
        self.assertIn("I am now executing the next declared workflow step: step_id=launch_server", output)
        self.assertIn("Background update: Pattern 'SERVER READY' matched in session server", output)

    def test_reporter_streams_agent_text_incrementally(self) -> None:
        stream = io.StringIO()
        reporter = ConsoleProgressReporter(stream=stream)

        reporter.emit({"event": "agent_stream_started"})
        reporter.emit({"event": "agent_stream_delta", "text": "I will inspect the state first. "})
        reporter.emit({"event": "agent_stream_delta", "text": "Then I will run the next step."})
        reporter.emit({"event": "agent_stream_finished"})

        output = stream.getvalue()
        self.assertIn("[assistant ", output)
        self.assertIn("I will inspect the state first. Then I will run the next step.", output)

    def test_reporter_formats_document_analysis_and_planner_stream(self) -> None:
        stream = io.StringIO()
        reporter = ConsoleProgressReporter(stream=stream)

        reporter.emit(
            {
                "event": "document_analysis",
                "command_count": 2,
                "heading_count": 2,
                "phases": [
                    "bring up the service or model server",
                    "wait for readiness and verify the service is responding",
                ],
            }
        )
        reporter.emit({"event": "planner_stream_started"})
        reporter.emit({"event": "planner_stream_delta", "text": "I can already see a launch phase and a health check. "})
        reporter.emit({"event": "planner_stream_delta", "text": "Next I will convert them into steps."})
        reporter.emit({"event": "planner_stream_finished"})

        output = stream.getvalue()
        self.assertIn("From the document structure I can already see about 2 command-like lines", output)
        self.assertIn("1) bring up the service or model server.", output)
        self.assertIn("2) wait for readiness and verify the service is responding.", output)
        self.assertIn("I can already see a launch phase and a health check. Next I will convert them into steps.", output)

    def test_reporter_refreshes_live_status_in_place_on_tty(self) -> None:
        stream = TtyStringIO()
        reporter = ConsoleProgressReporter(stream=stream, dynamic_refresh=True)

        reporter._write_status(
            "I have finished 1 of 4 steps so far. Right now I am focused on Wait for health endpoint has been active for about 10s."
        )
        reporter._write_status(
            "I have finished 1 of 4 steps so far. Right now I am focused on Wait for health endpoint has been active for about 12s."
        )
        reporter.emit(
            {
                "event": "step_finished",
                "step_id": "check_health",
                "title": "Wait for health endpoint",
                "kind": "probe",
                "session_name": "workspace_client",
                "status": "completed",
                "summary": "Probe succeeded after 26 attempts in session server_client.",
                "index": 2,
                "total": 4,
            }
        )

        output = stream.getvalue()
        self.assertIn("\r[progress] I have finished 1 of 4 steps so far.", output)
        self.assertIn("about 12s.", output)
        self.assertEqual(output.count("\n"), 1)
        self.assertIn("Step 2/4 finished successfully: Wait for health endpoint.", output)


if __name__ == "__main__":
    unittest.main()
