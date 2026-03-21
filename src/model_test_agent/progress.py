from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, TextIO


class ConsoleProgressReporter:
    def __init__(self, *, stream: TextIO | None = None, poll_interval_s: float = 2.0) -> None:
        self.stream = stream or sys.stderr
        self.poll_interval_s = poll_interval_s
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: Any = None
        self._running_since: dict[str, float] = {}
        self._last_snapshot: str = ""
        self._last_snapshot_ts = 0.0

    def bind_executor(self, executor: Any) -> None:
        self._executor = executor
        if self._thread is None:
            self._thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def emit(self, event: dict[str, Any]) -> None:
        message = self._format_event(event)
        if message:
            self._write(message)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            if self._executor is None:
                continue
            try:
                steps = self._executor.list_steps()
            except Exception:
                continue
            summary = self._format_snapshot(steps)
            now = time.time()
            if summary != self._last_snapshot or now - self._last_snapshot_ts >= 10.0:
                self._last_snapshot = summary
                self._last_snapshot_ts = now
                self._write(summary)

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
            active_text.append(f"{item['id']}[{item['status']}] {elapsed}s")
        summary = f"progress {completed}/{total} done"
        if active_text:
            summary += " | active: " + ", ".join(active_text[:3])
        if failed:
            summary += f" | failed: {failed}"
        return summary

    def _format_event(self, event: dict[str, Any]) -> str | None:
        name = event.get("event")
        if name == "step_started":
            return (
                f"starting step {event['index']}/{event['total']}: "
                f"{event['step_id']} ({event['kind']})"
            )
        if name == "step_finished":
            return (
                f"finished step {event['index']}/{event['total']}: "
                f"{event['step_id']} -> {event['status']}"
            )
        if name == "session_initialized":
            label = str(event["session_name"])
            if event.get("log_name") and event["log_name"] != event["session_name"]:
                label = f"{label} [logs: {event['log_name']}]"
            message = f"session ready: {label} via {event['backend']} ({event['backend_session_name']})"
            if event.get("combined_log_path"):
                message += f" log={event['combined_log_path']}"
            return message
        if name == "barrier_wait":
            return (
                f"barrier {event['step_id']} waiting {event['elapsed_s']:.0f}s "
                f"for {json.dumps(event['statuses'], ensure_ascii=False)}"
            )
        if name == "probe_retry":
            return (
                f"probe {event['step_id']} attempt {event['attempt']} "
                f"elapsed={event['elapsed_s']:.0f}s remaining={event['remaining_s']:.0f}s"
            )
        if name == "sleep_progress":
            return (
                f"sleep {event['step_id']} elapsed={event['elapsed_s']:.0f}s "
                f"remaining={event['remaining_s']:.0f}s"
            )
        if name == "wait_started":
            return (
                f"waiting for pattern in {event['session_name']}: {event['pattern']} "
                f"(timeout {event['timeout_s']}s)"
            )
        if name == "agent_iteration":
            return f"agent iteration {event['iteration']}/{event['max_iterations']} waiting for model response"
        if name == "agent_notifications":
            return f"agent received {event['count']} background notification(s)"
        if name == "agent_tool_call":
            detail = event.get("detail")
            return f"agent tool call: {event['tool_name']}" + (f" {detail}" if detail else "")
        if name == "agent_finished":
            return f"agent finished: {event['status']}"
        return None

    def _write(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        with self._lock:
            print(f"[progress {timestamp}] {message}", file=self.stream, flush=True)
