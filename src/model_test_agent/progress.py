from __future__ import annotations

import sys
import threading
import time
from typing import Any, TextIO

from .models import ProbeStep, SendKeysStep, StepKind, WaitStep, WorkflowSpec, WorkflowStep


def summarize_workflow(workflow: WorkflowSpec) -> dict[str, Any]:
    phases: list[str] = []
    for step in workflow.steps:
        phase = _phase_for_step(step)
        if not phases or phases[-1] != phase:
            phases.append(phase)
    if not phases:
        phases = ["run the declared command sequence"]
    return {
        "workflow_name": workflow.name,
        "objective": workflow.objective,
        "session_count": len(workflow.sessions),
        "step_count": len(workflow.steps),
        "phases": phases[:5],
    }


def _phase_for_step(step: WorkflowStep) -> str:
    title = getattr(step, "title", "").lower()
    step_id = getattr(step, "id", "").lower()
    kind = getattr(step, "kind", None)
    session = getattr(step, "session", "") or ""
    session = session.lower()
    if isinstance(step, SendKeysStep):
        joined = " ".join(step.keys).lower()
        if "c-c" in joined or "ctrl-c" in joined:
            return "clean up long-running sessions"
        return "drive an interactive terminal or editor"
    if isinstance(step, (ProbeStep, WaitStep)) or kind in {StepKind.BARRIER, StepKind.SLEEP}:
        return "wait for readiness and synchronize dependent steps"
    command = getattr(step, "command", "").lower()
    if any(token in f"{title} {step_id} {command}" for token in ("pkill", "killall", " kill ", "cleanup", "stop")):
        return "clean up long-running sessions"
    if any(
        token in f"{title} {step_id} {command} {session}"
        for token in (
            "launch",
            "start",
            "server",
            "serve",
            "uvicorn",
            "gunicorn",
            "http.server",
            "startup-delay",
        )
    ):
        return "bring up the service side of the workflow"
    if any(token in f"{title} {step_id} {command}" for token in ("curl ", "wget ", "benchmark", "client", "healthz")):
        return "run client-side checks and validations"
    return "run the main command sequence"


class ConsoleProgressReporter:
    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        poll_interval_s: float = 2.0,
        dynamic_refresh: bool | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        self.poll_interval_s = poll_interval_s
        self.dynamic_refresh = self._resolve_dynamic_refresh(dynamic_refresh, self.stream)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: Any = None
        self._running_since: dict[str, float] = {}
        self._last_snapshot: str = ""
        self._last_snapshot_ts = 0.0
        self._agent_stream_open = False
        self._status_line_open = False
        self._status_line_width = 0

    def bind_executor(self, executor: Any) -> None:
        self._executor = executor
        if self._thread is None:
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        with self._lock:
            self._clear_status_line_locked()

    def emit(self, event: dict[str, Any]) -> None:
        if self._handle_stream_event(event):
            return
        if self._handle_live_status_event(event):
            return
        message = self._format_event(event)
        if message:
            self._write(message)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            if self._executor is None:
                continue
            if self._agent_stream_open:
                continue
            try:
                steps = self._executor.list_steps()
            except Exception:
                continue
            if steps and all(item["status"] in {"completed", "failed", "skipped"} for item in steps):
                continue
            summary = self._format_snapshot(steps)
            now = time.time()
            if summary != self._last_snapshot or now - self._last_snapshot_ts >= 10.0:
                self._last_snapshot = summary
                self._last_snapshot_ts = now
                self._write_status(summary)

    def _format_snapshot(self, steps: list[dict[str, Any]]) -> str:
        total = len(steps)
        completed = sum(1 for item in steps if item["status"] in {"completed", "skipped"})
        failed = sum(1 for item in steps if item["status"] == "failed")
        running = [item for item in steps if item["status"] == "running"]
        background = [item for item in steps if item["status"] == "background"]
        now = time.time()
        active_ids = {item["id"] for item in running + background}
        self._running_since = {key: value for key, value in self._running_since.items() if key in active_ids}
        active_text: list[str] = []
        for item in running + background:
            start = self._running_since.setdefault(item["id"], now)
            elapsed = int(now - start)
            label = str(item.get("title") or item["id"])
            if item["status"] == "background":
                active_text.append(f"{label} has been running in the background for about {elapsed}s")
            else:
                active_text.append(f"{label} has been active for about {elapsed}s")
        summary = f"I have finished {completed} of {total} steps so far."
        if active_text:
            summary += " Right now I am focused on " + "; ".join(active_text[:2]) + "."
        if failed:
            summary += f" There {'is' if failed == 1 else 'are'} {failed} failed step(s), so I am watching for a blocking condition."
        return summary

    def _format_event(self, event: dict[str, Any]) -> str | None:
        name = event.get("event")
        if name == "narration":
            return str(event.get("message", "")).strip() or None
        if name == "planning_started":
            return (
                f"I will first read {event['path']} and extract the commands, sessions, waits, "
                "and cleanup actions that matter for execution."
            )
        if name == "document_loaded":
            return (
                f"The source document is loaded ({event['media_type']}, {event['line_count']} lines, "
                f"{event['char_count']} characters). Next I will turn it into an executable workflow."
            )
        if name == "document_analysis":
            phases = [str(item) for item in event.get("phases", []) if str(item).strip()]
            phase_text = " ".join(f"{index}) {item}." for index, item in enumerate(phases, start=1))
            command_count = int(event.get("command_count", 0))
            heading_count = int(event.get("heading_count", 0))
            base = (
                f"From the document structure I can already see about {command_count} command-like lines "
                f"and {heading_count} heading(s)."
            )
            if phase_text:
                base += f" The likely phases are: {phase_text}"
            base += " Next I will map those phases into sessions, dependencies, waits, and cleanup actions."
            return base
        if name == "planning_model_call":
            return (
                f"I have enough context from the document. I am now using {event['model']} to convert it into "
                "structured sessions and steps, and I will fill in any omitted waits or cleanup actions."
            )
        if name == "workflow_planned":
            return self._format_workflow_plan(event)
        if name == "workflow_execution_started":
            return (
                f"The workflow {event['workflow_name']} is ready. I will execute it across "
                f"{event['session_count']} session(s) and keep the steps synchronized as output arrives."
            )
        if name == "step_started":
            return self._format_step_started(event)
        if name == "step_finished":
            return self._format_step_finished(event)
        if name == "session_initialized":
            label = str(event["session_name"])
            if event.get("log_name") and event["log_name"] != event["session_name"]:
                label = f"{label} [logs: {event['log_name']}]"
            message = f"I have prepared session {label} via {event['backend']} ({event['backend_session_name']})."
            if event.get("combined_log_path"):
                message += f" Its terminal log is being written to {event['combined_log_path']}."
            return message
        if name == "barrier_wait":
            statuses = ", ".join(f"{step_id}={status}" for step_id, status in event["statuses"].items())
            return (
                f"I am holding the barrier step {event['step_id']} while dependent work finishes. "
                f"Current dependency status: {statuses}. Elapsed wait: {event['elapsed_s']:.0f}s."
            )
        if name == "probe_retry":
            return (
                f"The readiness check {event['step_id']} is not passing yet. "
                f"I will retry attempt {event['attempt']} and there are about {event['remaining_s']:.0f}s left."
            )
        if name == "sleep_progress":
            return (
                f"This step is intentionally waiting before the next action. "
                f"Elapsed: {event['elapsed_s']:.0f}s. Remaining: {event['remaining_s']:.0f}s."
            )
        if name == "wait_started":
            return (
                f"I am now waiting for session {event['session_name']} to emit the pattern "
                f"{event['pattern']!r}. Timeout: {event['timeout_s']}s."
            )
        if name == "agent_iteration":
            return (
                f"I am refreshing the workflow state and deciding the next move "
                f"(agent pass {event['iteration']}/{event['max_iterations']})."
            )
        if name == "agent_note":
            note = str(event.get("content", "")).strip()
            if not note:
                return None
            return note
        if name == "agent_notifications":
            return (
                f"I just received {event['count']} background notification(s), "
                "so I will refresh the state before choosing the next action."
            )
        if name == "agent_tool_call":
            return self._format_agent_tool_call(event)
        if name == "agent_finished":
            return f"The supervising agent has finished with status {event['status']}."
        if name == "background_notifications":
            summaries = [str(item.get("summary", "")).strip() for item in event.get("notifications", [])]
            summaries = [item for item in summaries if item]
            if not summaries:
                return "I received a background update and will refresh the current state."
            return "Background update: " + "; ".join(summaries[:2])
        if name == "workflow_stalled":
            return (
                "No additional step is ready to run right now. I am collecting the current state because the "
                "workflow may be blocked."
            )
        return None

    def _handle_stream_event(self, event: dict[str, Any]) -> bool:
        name = event.get("event")
        if name == "agent_stream_started":
            self._start_agent_stream()
            return True
        if name == "agent_stream_delta":
            self._write_agent_stream_text(str(event.get("text", "")))
            return True
        if name == "agent_stream_finished":
            self._finish_agent_stream()
            return True
        if name == "planner_stream_started":
            self._start_agent_stream()
            return True
        if name == "planner_stream_delta":
            self._write_agent_stream_text(str(event.get("text", "")))
            return True
        if name == "planner_stream_finished":
            self._finish_agent_stream()
            return True
        return False

    def _handle_live_status_event(self, event: dict[str, Any]) -> bool:
        name = event.get("event")
        if name in {"probe_retry", "barrier_wait", "sleep_progress"}:
            message = self._format_event(event)
            if message:
                self._write_status(message)
            return True
        return False

    @staticmethod
    def _format_workflow_plan(event: dict[str, Any]) -> str:
        phases = [str(item) for item in event.get("phases", []) if str(item).strip()]
        numbered = " ".join(f"{index}) {phase}." for index, phase in enumerate(phases, start=1))
        prefix = (
            f"I organized the document into a workflow named {event['workflow_name']} with "
            f"{event['session_count']} session(s) and {event['step_count']} step(s)."
        )
        if numbered:
            return f"{prefix} The main phases are: {numbered}"
        return prefix

    @staticmethod
    def _format_step_started(event: dict[str, Any]) -> str:
        label = str(event.get("title") or event["step_id"])
        kind = str(event.get("kind") or "")
        session = event.get("session_name")
        base = f"I am starting step {event['index']}/{event['total']}: {label}."
        if kind == "probe":
            base += " This is a retrying check to verify that the target is actually ready."
        elif kind == "barrier":
            base += " This step waits for dependent background work before moving on."
        elif kind == "send_keys":
            base += " This is an interactive terminal action."
        elif kind == "sleep":
            base += " This is an intentional pause before the next action."
        elif kind == "wait":
            base += " I will wait for a specific terminal pattern before continuing."
        else:
            base += " I am now executing the declared action."
        if session:
            base += f" Session: {session}."
        return base

    @staticmethod
    def _format_step_finished(event: dict[str, Any]) -> str:
        label = str(event.get("title") or event["step_id"])
        summary = str(event.get("summary") or "").strip()
        if len(summary) > 220:
            summary = summary[:217].rstrip() + "..."
        if event["status"] == "background":
            message = f"Step {event['index']}/{event['total']} is now running in the background: {label}."
            if summary:
                message += f" {summary}"
            return message
        if event["status"] == "completed":
            message = f"Step {event['index']}/{event['total']} finished successfully: {label}."
            if summary:
                message += f" {summary}"
            return message
        if event["status"] == "failed":
            return f"Step {event['index']}/{event['total']} failed: {label}. {summary or 'Execution failed.'}"
        return f"Step {event['index']}/{event['total']} finished with status {event['status']}: {label}."

    @staticmethod
    def _format_agent_tool_call(event: dict[str, Any]) -> str:
        tool_name = str(event.get("tool_name") or "")
        detail = str(event.get("detail") or "").strip()
        if tool_name == "get_state":
            return "I am checking the full workflow state before I decide on the next action."
        if tool_name == "list_steps":
            return "I am checking which declared steps are ready to run now."
        if tool_name == "run_step":
            return f"I am now executing the next declared workflow step{': ' + detail if detail else '.'}"
        if tool_name == "list_sessions":
            return "I am checking which terminal sessions are already initialized."
        if tool_name == "capture_session":
            return f"I am reading the latest terminal output{': ' + detail if detail else '.'}"
        if tool_name == "run_command":
            return f"I am running an ad hoc command to verify or adjust the environment{': ' + detail if detail else '.'}"
        if tool_name == "wait_for_output":
            return f"I am waiting for a terminal signal before moving on{': ' + detail if detail else '.'}"
        if tool_name == "send_keys":
            return f"I am sending interactive input{': ' + detail if detail else '.'}"
        if tool_name == "list_background_tasks":
            return "I am checking the background watchers to see whether a server or wait condition has completed."
        if tool_name == "get_background_task":
            return f"I am inspecting a specific background watcher{': ' + detail if detail else '.'}"
        if tool_name == "complete_run":
            return "The run looks complete, so I am collecting the final summary now."
        if tool_name == "fail_run":
            return "The run cannot safely continue, so I am stopping and summarizing the failure."
        return f"I am invoking tool {tool_name}{': ' + detail if detail else '.'}"

    def _write(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        with self._lock:
            self._clear_status_line_locked()
            if self._agent_stream_open:
                print(file=self.stream, flush=True)
                self._agent_stream_open = False
            print(f"[progress {timestamp}] {message}", file=self.stream, flush=True)

    def _write_status(self, message: str) -> None:
        if not self.dynamic_refresh:
            self._write(message)
            return
        with self._lock:
            if self._agent_stream_open:
                print(file=self.stream, flush=True)
                self._agent_stream_open = False
            rendered = f"[progress] {message}"
            padding = max(0, self._status_line_width - len(rendered))
            self.stream.write("\r")
            self.stream.write(rendered)
            if padding:
                self.stream.write(" " * padding)
            self.stream.flush()
            self._status_line_open = True
            self._status_line_width = len(rendered)

    def _start_agent_stream(self) -> None:
        with self._lock:
            self._clear_status_line_locked()
            if self._agent_stream_open:
                print(file=self.stream, flush=True)
            timestamp = time.strftime("%H:%M:%S")
            print(f"[assistant {timestamp}] ", file=self.stream, end="", flush=True)
            self._agent_stream_open = True

    def _write_agent_stream_text(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._clear_status_line_locked()
            if not self._agent_stream_open:
                timestamp = time.strftime("%H:%M:%S")
                print(f"[assistant {timestamp}] ", file=self.stream, end="", flush=True)
                self._agent_stream_open = True
            print(text, file=self.stream, end="", flush=True)

    def _finish_agent_stream(self) -> None:
        with self._lock:
            if not self._agent_stream_open:
                return
            print(file=self.stream, flush=True)
            self._agent_stream_open = False

    @staticmethod
    def _resolve_dynamic_refresh(explicit: bool | None, stream: TextIO | None = None) -> bool:
        if explicit is not None:
            return explicit
        target = stream or sys.stderr
        isatty = getattr(target, "isatty", None)
        if callable(isatty):
            try:
                return bool(isatty())
            except Exception:
                return False
        return False

    def _clear_status_line_locked(self) -> None:
        if not self._status_line_open:
            return
        self.stream.write("\r")
        if self._status_line_width:
            self.stream.write(" " * self._status_line_width)
            self.stream.write("\r")
        self.stream.flush()
        self._status_line_open = False
        self._status_line_width = 0
