from __future__ import annotations

import re
import time
from typing import Any, Callable, Iterable

from ..config import Settings
from ..models import StepKind, StepResult, StepStatus, WorkflowStep
from ..openai_compat import OpenAICompatClient
from .background import BackgroundTaskManager
from .session_manager import SessionManager
from .session_backend import SessionBackend
from .step_handlers import StepExecutionContext, StepHandler, default_step_handlers


class StepRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        backend: SessionBackend,
        background: BackgroundTaskManager,
        session_manager: SessionManager,
        step_statuses: dict[str, StepStatus],
        llm_client: OpenAICompatClient | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        refresh_background_steps: Callable[[], None] | None = None,
        handlers: Iterable[StepHandler] | None = None,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.background = background
        self.session_manager = session_manager
        self.step_statuses = step_statuses
        self.llm_client = llm_client
        self._progress_callback = progress_callback
        self._refresh_background_steps = refresh_background_steps or (lambda: None)
        self._context = StepExecutionContext(
            settings=self.settings,
            backend=self.backend,
            background=self.background,
            session_manager=self.session_manager,
            step_statuses=self.step_statuses,
            llm_client=self.llm_client,
            emit_progress=self._emit_progress,
            refresh_background_steps=self._refresh_background_steps,
            run_command=self.run_command,
            wait_for_output=self.wait_for_output,
            capture_session=self.capture_session,
            send_keys=self.send_keys,
        )
        self._handlers: dict[StepKind, StepHandler] = {}
        for handler in default_step_handlers():
            self.register_handler(handler)
        for handler in handlers or []:
            self.register_handler(handler)

    def execute_step(self, step: WorkflowStep) -> StepResult:
        handler = self._handlers.get(step.kind)
        if handler is None:
            raise TypeError(f"No step handler is registered for kind {step.kind.value!r}")
        return handler.execute(step, self._context)

    def register_handler(self, handler: StepHandler) -> None:
        self._handlers[handler.kind] = handler

    def run_command(
        self,
        *,
        session_name: str,
        command: str,
        timeout_s: int | None = None,
        background: bool = False,
        ready_pattern: str | None = None,
        fail_patterns: list[str] | None = None,
        capture_lines: int | None = None,
    ) -> dict[str, object]:
        capture_lines = capture_lines or self.settings.default_capture_lines
        timeout_s = timeout_s or self.settings.default_timeout_s
        state = self.session_manager.ensure_session(session_name)
        backend_session_name = state.backend_session_name
        command = self.session_manager.command_with_session_logging(command, state)
        if background:
            self.backend.send_literal(backend_session_name, command)
            task_id = None
            if ready_pattern:
                task_id = self.background.watch_output(
                    session_name=backend_session_name,
                    pattern=ready_pattern,
                    timeout_s=timeout_s,
                    fail_patterns=fail_patterns,
                    lines=capture_lines,
                )
            return {
                "status": "background",
                "summary": "Command dispatched in background",
                "task_id": task_id,
            }
        result = self.backend.run_command(
            backend_session_name,
            command,
            timeout_s=timeout_s,
            lines=capture_lines,
        )
        cleaned_output = self.session_manager.clean_command_output(result.output)
        if result.exit_code != 0:
            raise RuntimeError(f"Command failed with exit code {result.exit_code}: {cleaned_output}")
        if fail_patterns:
            for pattern in fail_patterns:
                if re.search(pattern, cleaned_output, re.MULTILINE):
                    raise RuntimeError(f"Failure pattern {pattern!r} matched: {cleaned_output}")
        return {"status": "completed", "exit_code": result.exit_code, "output": cleaned_output}

    def wait_for_output(
        self,
        *,
        session_name: str,
        pattern: str,
        timeout_s: int | None = None,
        fail_patterns: list[str] | None = None,
        capture_lines: int | None = None,
    ) -> dict[str, object]:
        capture_lines = capture_lines or self.settings.default_capture_lines
        timeout_s = timeout_s or self.settings.default_timeout_s
        backend_session_name = self.session_manager.ensure_session(session_name).backend_session_name
        wait = self.backend.wait_for_pattern(
            backend_session_name,
            pattern,
            timeout_s=timeout_s,
            fail_patterns=fail_patterns,
            lines=capture_lines,
        )
        if wait.status != "matched":
            raise RuntimeError(wait.output or f"Wait failed with status {wait.status}")
        return {"status": "completed", "output": wait.output, "matched_pattern": wait.matched_pattern}

    def capture_session(self, *, session_name: str, lines: int | None = None) -> dict[str, object]:
        lines = lines or self.settings.default_capture_lines
        backend_session_name = self.session_manager.ensure_session(session_name).backend_session_name
        return {"session": session_name, "output": self.backend.capture_pane(backend_session_name, lines=lines)}

    def send_keys(
        self,
        *,
        session_name: str,
        keys: list[str],
        literal: bool = False,
        press_enter: bool = False,
        delay_s: float = 0.0,
    ) -> dict[str, object]:
        backend_session_name = self.session_manager.ensure_session(session_name).backend_session_name
        if literal:
            for index, item in enumerate(keys):
                self.backend.send_literal(
                    backend_session_name,
                    item,
                    enter=press_enter and index == len(keys) - 1,
                )
                if delay_s:
                    time.sleep(delay_s)
        else:
            self.backend.send_keys(backend_session_name, keys, press_enter=press_enter)
            if delay_s:
                time.sleep(delay_s)
        return {"status": "completed", "summary": f"Sent keys to {session_name}"}

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback({"event": event, **payload})
