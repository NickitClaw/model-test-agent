from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from ..config import Settings
from ..models import (
    BarrierStep,
    CaptureStep,
    CommandStep,
    DecisionStep,
    ProbeStep,
    SendKeysStep,
    SleepStep,
    StepResult,
    StepStatus,
    WaitStep,
    WorkflowStep,
)
from ..openai_compat import OpenAICompatClient
from .background import BackgroundTaskManager
from .session_manager import SessionManager
from .session_backend import SessionBackend


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
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.background = background
        self.session_manager = session_manager
        self.step_statuses = step_statuses
        self.llm_client = llm_client
        self._progress_callback = progress_callback
        self._refresh_background_steps = refresh_background_steps or (lambda: None)

    def execute_step(self, step: WorkflowStep) -> StepResult:
        if isinstance(step, CommandStep):
            return self._execute_command_step(step)
        if isinstance(step, WaitStep):
            return self._execute_wait_step(step)
        if isinstance(step, ProbeStep):
            return self._execute_probe_step(step)
        if isinstance(step, SendKeysStep):
            return self._execute_send_keys_step(step)
        if isinstance(step, SleepStep):
            return self._execute_sleep_step(step)
        if isinstance(step, BarrierStep):
            return self._execute_barrier_step(step)
        if isinstance(step, CaptureStep):
            return self._execute_capture_step(step)
        if isinstance(step, DecisionStep):
            return self._execute_decision_step(step)
        raise TypeError(f"Unsupported step type: {type(step)!r}")

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

    def _execute_command_step(self, step: CommandStep) -> StepResult:
        if not step.session:
            raise ValueError(f"Command step {step.id} is missing session")
        state = self.session_manager.ensure_session(step.session)
        backend_session_name = state.backend_session_name
        timeout_s = step.timeout_s or self.settings.default_timeout_s
        command = self.session_manager.command_with_session_logging(step.command, state)
        if step.background:
            self.backend.send_literal(backend_session_name, command)
            task_id = None
            if step.ready_pattern:
                task_id = self.background.watch_output(
                    session_name=backend_session_name,
                    pattern=step.ready_pattern,
                    timeout_s=timeout_s,
                    fail_patterns=step.fail_patterns,
                    step_id=step.id,
                    lines=step.capture_lines,
                )
            if task_id:
                return StepResult(
                    step_id=step.id,
                    status=StepStatus.BACKGROUND,
                    summary=f"Background command dispatched in session {step.session}",
                    background_task_id=task_id,
                )
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                summary=f"Background command dispatched in session {step.session} without readiness watcher",
            )
        result = self.backend.run_command(
            backend_session_name,
            command,
            timeout_s=timeout_s,
            lines=step.capture_lines,
        )
        cleaned_output = self.session_manager.clean_command_output(result.output)
        if result.exit_code != 0:
            raise RuntimeError(f"Command exited with {result.exit_code}: {cleaned_output}")
        for pattern in step.fail_patterns:
            if re.search(pattern, cleaned_output, re.MULTILINE):
                raise RuntimeError(f"Failure pattern {pattern!r} matched")
        if step.success_patterns and not any(
            re.search(pattern, cleaned_output, re.MULTILINE) for pattern in step.success_patterns
        ):
            raise RuntimeError(
                f"None of the success patterns matched for step {step.id}: {step.success_patterns}"
            )
        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            summary=f"Command completed in session {step.session}",
            output=cleaned_output,
            metadata={"exit_code": result.exit_code},
        )

    def _execute_wait_step(self, step: WaitStep) -> StepResult:
        if not step.session:
            raise ValueError(f"Wait step {step.id} is missing session")
        backend_session_name = self.session_manager.ensure_session(step.session).backend_session_name
        timeout_s = step.timeout_s or self.settings.default_timeout_s
        if step.background:
            task_id = self.background.watch_output(
                session_name=backend_session_name,
                pattern=step.pattern,
                timeout_s=timeout_s,
                fail_patterns=step.fail_patterns,
                step_id=step.id,
                lines=step.capture_lines,
            )
            return StepResult(
                step_id=step.id,
                status=StepStatus.BACKGROUND,
                summary=f"Background wait started for session {step.session}",
                background_task_id=task_id,
            )
        self._emit_progress(
            "wait_started",
            step_id=step.id,
            session_name=step.session,
            pattern=step.pattern,
            timeout_s=timeout_s,
        )
        wait = self.backend.wait_for_pattern(
            backend_session_name,
            step.pattern,
            timeout_s=timeout_s,
            fail_patterns=step.fail_patterns,
            lines=step.capture_lines,
        )
        if wait.status != "matched":
            raise RuntimeError(wait.output or f"Wait step {step.id} failed with status {wait.status}")
        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            summary=f"Pattern {step.pattern!r} matched in session {step.session}",
            output=wait.output,
        )

    def _execute_probe_step(self, step: ProbeStep) -> StepResult:
        if not step.session:
            raise ValueError(f"Probe step {step.id} is missing session")
        state = self.session_manager.ensure_session(step.session)
        backend_session_name = state.backend_session_name
        command = self.session_manager.command_with_session_logging(step.command, state)
        timeout_s = step.timeout_s or self.settings.default_timeout_s
        deadline = time.time() + timeout_s
        last_output = ""
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            remaining = max(1, int(deadline - time.time()))
            result = self.backend.run_command(
                backend_session_name,
                command,
                timeout_s=min(remaining, max(5, self.settings.default_timeout_s)),
                lines=step.capture_lines,
            )
            cleaned_output = self.session_manager.clean_command_output(result.output)
            last_output = cleaned_output
            failed = any(re.search(pattern, cleaned_output, re.MULTILINE) for pattern in step.fail_patterns)
            success_match = True
            if step.success_patterns:
                success_match = any(re.search(pattern, cleaned_output, re.MULTILINE) for pattern in step.success_patterns)
            if not failed and result.exit_code == step.expect_exit_code and success_match:
                return StepResult(
                    step_id=step.id,
                    status=StepStatus.COMPLETED,
                    summary=f"Probe succeeded after {attempt} attempts in session {step.session}",
                    output=cleaned_output,
                    metadata={"attempts": attempt, "exit_code": result.exit_code},
                )
            self._emit_progress(
                "probe_retry",
                step_id=step.id,
                attempt=attempt,
                elapsed_s=timeout_s - remaining,
                remaining_s=remaining,
            )
            time.sleep(step.interval_s)
        raise TimeoutError(f"Probe timed out for step {step.id}: {last_output}")

    def _execute_send_keys_step(self, step: SendKeysStep) -> StepResult:
        if not step.session:
            raise ValueError(f"Send-keys step {step.id} is missing session")
        self.send_keys(
            session_name=step.session,
            keys=step.keys,
            literal=step.literal,
            press_enter=step.press_enter,
            delay_s=step.delay_s,
        )
        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            summary=f"Sent keys to session {step.session}",
        )

    def _execute_sleep_step(self, step: SleepStep) -> StepResult:
        if step.seconds <= 1.0:
            time.sleep(step.seconds)
        else:
            deadline = time.time() + step.seconds
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                sleep_for = min(1.0, remaining)
                time.sleep(sleep_for)
                remaining = max(0.0, deadline - time.time())
                self._emit_progress(
                    "sleep_progress",
                    step_id=step.id,
                    elapsed_s=step.seconds - remaining,
                    remaining_s=remaining,
                )
        return StepResult(step_id=step.id, status=StepStatus.COMPLETED, summary=f"Slept for {step.seconds}s")

    def _execute_barrier_step(self, step: BarrierStep) -> StepResult:
        wait_for = step.wait_for or step.depends_on
        if not wait_for:
            return StepResult(step_id=step.id, status=StepStatus.COMPLETED, summary="Barrier had no targets")
        timeout_s = step.timeout_s or self.settings.default_timeout_s
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self._refresh_background_steps()
            statuses = {item: self.step_statuses[item] for item in wait_for}
            if all(status is StepStatus.COMPLETED for status in statuses.values()):
                return StepResult(
                    step_id=step.id,
                    status=StepStatus.COMPLETED,
                    summary=f"Barrier released after steps {wait_for}",
                )
            failed = [item for item, status in statuses.items() if status is StepStatus.FAILED]
            if failed:
                raise RuntimeError(f"Barrier saw failed dependencies: {failed}")
            self._emit_progress(
                "barrier_wait",
                step_id=step.id,
                statuses={item: status.value for item, status in statuses.items()},
                elapsed_s=timeout_s - max(0.0, deadline - time.time()),
            )
            time.sleep(step.poll_interval_s)
        raise TimeoutError(f"Barrier timed out while waiting for: {wait_for}")

    def _execute_capture_step(self, step: CaptureStep) -> StepResult:
        session_name = step.source_session or step.session
        if not session_name:
            raise ValueError(f"Capture step {step.id} is missing source_session")
        capture = self.capture_session(session_name=session_name, lines=step.lines)
        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            summary=f"Captured {step.lines} lines from session {session_name}",
            output=str(capture["output"]),
        )

    def _execute_decision_step(self, step: DecisionStep) -> StepResult:
        session_name = step.source_session or step.session
        output = ""
        if session_name:
            output = str(self.capture_session(session_name=session_name)["output"])
        for rule in step.rules:
            if re.search(rule.pattern, output, re.MULTILINE):
                return StepResult(
                    step_id=step.id,
                    status=StepStatus.COMPLETED,
                    summary=f"Decision matched {rule.pattern!r}: {rule.action}",
                    output=output,
                    metadata={
                        "decision": {
                            "action": rule.action,
                            "target_step": rule.target_step,
                            "matched_pattern": rule.pattern,
                            "note": rule.note,
                        }
                    },
                )
        if step.llm_prompt:
            if not self.llm_client:
                raise RuntimeError("Decision step requested llm_prompt but no LLM client is configured")
            payload = self.llm_client.complete_json(
                model=self.settings.agent_model,
                system_prompt=(
                    "You are deciding how to proceed in a Linux CLI model benchmarking run. "
                    "Return JSON with action, target_step, and reason."
                ),
                user_prompt=(
                    f"Decision prompt:\n{step.llm_prompt}\n\n"
                    f"Recent output:\n{output[:step.max_output_chars]}"
                ),
            )
            return StepResult(
                step_id=step.id,
                status=StepStatus.COMPLETED,
                summary=f"LLM decision: {json.dumps(payload, ensure_ascii=False)}",
                output=output,
                metadata={"decision": payload},
            )
        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            summary=f"No rule matched. Default action: {step.default_action}",
            output=output,
            metadata={"decision": {"action": step.default_action}},
        )
