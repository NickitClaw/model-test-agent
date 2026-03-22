from __future__ import annotations

import json
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..models import (
    BarrierStep,
    CaptureStep,
    CommandStep,
    DecisionStep,
    ProbeStep,
    SendKeysStep,
    SessionSpec,
    SessionTransport,
    SleepStep,
    StepResult,
    StepStatus,
    WaitStep,
    WorkflowSpec,
    WorkflowStep,
)
from ..openai_compat import OpenAICompatClient
from .background import BackgroundTaskManager
from .factory import create_session_backend
from .session_backend import SessionBackend


@dataclass
class SessionState:
    logical_name: str
    log_name: str
    backend_session_name: str
    combined_log_path: str
    stdout_log_path: str
    stderr_log_path: str
    initialized: bool = False


class WorkflowExecutor:
    def __init__(
        self,
        workflow: WorkflowSpec,
        settings: Settings,
        *,
        backend: SessionBackend | None = None,
        background: BackgroundTaskManager | None = None,
        llm_client: OpenAICompatClient | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.workflow = workflow
        self.settings = settings
        self.backend = backend or create_session_backend(settings)
        self.background = background or BackgroundTaskManager(
            self.backend, default_lines=settings.default_capture_lines
        )
        self.llm_client = llm_client
        self._step_map = workflow.step_map()
        self._step_statuses = {step.id: StepStatus.PENDING for step in workflow.steps}
        self._step_results: dict[str, StepResult] = {}
        self._session_states: dict[str, SessionState] = {}
        self._run_id = uuid.uuid4().hex[:8]
        self._progress_callback = progress_callback
        self.log_dir = self._create_log_dir()
        self._session_log_names = self._plan_session_log_names()

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback({"event": event, **payload})

    def _step_position(self, step_id: str) -> tuple[int, int]:
        for index, step in enumerate(self.workflow.steps, start=1):
            if step.id == step_id:
                return index, len(self.workflow.steps)
        return 0, len(self.workflow.steps)

    def list_steps(self, *, status: str | None = None, only_ready: bool = False) -> list[dict[str, object]]:
        self.refresh_background_steps()
        rows: list[dict[str, object]] = []
        for step in self.workflow.steps:
            step_status = self._step_statuses[step.id]
            ready = self.is_step_ready(step.id)
            if status and step_status.value != status:
                continue
            if only_ready and not ready:
                continue
            row = {
                "id": step.id,
                "title": step.title,
                "kind": step.kind.value,
                "status": step_status.value,
                "ready": ready,
                "depends_on": list(step.depends_on),
                "session": step.session,
            }
            result = self._step_results.get(step.id)
            if result:
                row["result"] = result.to_dict()
            rows.append(row)
        return rows

    def list_sessions(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for name, spec in self.workflow.sessions.items():
            state = self._session_states.get(name)
            rows.append(
                {
                    "name": name,
                    "log_name": state.log_name if state else self._session_log_names.get(name),
                    "transport": spec.transport.value,
                    "backend": self.backend.backend_name,
                    "backend_session_name": state.backend_session_name if state else None,
                    "initialized": state.initialized if state else False,
                    "combined_log_path": state.combined_log_path if state else None,
                    "stdout_log_path": state.stdout_log_path if state else None,
                    "stderr_log_path": state.stderr_log_path if state else None,
                }
            )
        return rows

    def list_background_tasks(self) -> list[dict[str, object]]:
        self.refresh_background_steps()
        return [task.to_dict() for task in self.background.list_tasks()]

    def get_background_task(self, task_id: str) -> dict[str, object]:
        self.refresh_background_steps()
        return self.background.get_task(task_id).to_dict()

    def describe_state(self) -> dict[str, object]:
        self.refresh_background_steps()
        return {
            "run": {
                "id": self._run_id,
                "backend": self.backend.backend_name,
                "log_dir": str(self.log_dir),
            },
            "workflow": self.workflow.to_dict(),
            "steps": self.list_steps(),
            "sessions": self.list_sessions(),
            "background_tasks": self.list_background_tasks(),
        }

    def refresh_background_steps(self) -> None:
        for task in self.background.list_tasks():
            if not task.step_id:
                continue
            if self._step_statuses.get(task.step_id) is not StepStatus.BACKGROUND:
                continue
            if task.status == "completed":
                self._step_statuses[task.step_id] = StepStatus.COMPLETED
                self._step_results[task.step_id] = StepResult(
                    step_id=task.step_id,
                    status=StepStatus.COMPLETED,
                    summary=task.summary,
                    output=task.output,
                    background_task_id=task.task_id,
                )
            elif task.status in {"failed", "timeout", "error"}:
                self._step_statuses[task.step_id] = StepStatus.FAILED
                self._step_results[task.step_id] = StepResult(
                    step_id=task.step_id,
                    status=StepStatus.FAILED,
                    summary=task.summary,
                    output=task.output,
                    background_task_id=task.task_id,
                )

    def drain_notifications(self) -> list[dict[str, object]]:
        self.refresh_background_steps()
        return [notification.to_dict() for notification in self.background.drain_notifications()]

    def is_step_ready(self, step_id: str) -> bool:
        self.refresh_background_steps()
        if self._step_statuses[step_id] is not StepStatus.PENDING:
            return False
        step = self._step_map[step_id]
        for dep in step.depends_on:
            if self._step_statuses.get(dep) is not StepStatus.COMPLETED:
                return False
        return True

    def all_steps_finished(self) -> bool:
        self.refresh_background_steps()
        return all(
            status in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.SKIPPED}
            for status in self._step_statuses.values()
        )

    def close_all(self) -> None:
        for state in self._session_states.values():
            self.backend.kill_session(state.backend_session_name)

    def run_step(self, step_id: str) -> dict[str, object]:
        self.refresh_background_steps()
        if step_id not in self._step_map:
            raise KeyError(f"Unknown step: {step_id}")
        if self._step_statuses[step_id] in {StepStatus.COMPLETED, StepStatus.FAILED, StepStatus.BACKGROUND}:
            result = self._step_results.get(step_id)
            return result.to_dict() if result else {"step_id": step_id, "status": self._step_statuses[step_id].value}
        if not self.is_step_ready(step_id):
            raise RuntimeError(f"Step {step_id} is not ready")
        step = self._step_map[step_id]
        self._step_statuses[step_id] = StepStatus.RUNNING
        index, total = self._step_position(step_id)
        self._emit_progress(
            "step_started",
            step_id=step.id,
            kind=step.kind.value,
            title=step.title,
            session_name=step.session,
            index=index,
            total=total,
        )
        try:
            result = self._execute_step(step)
        except Exception as exc:
            if step.continue_on_error:
                result = StepResult(
                    step_id=step.id,
                    status=StepStatus.COMPLETED,
                    summary=f"Ignored error: {exc}",
                    metadata={"ignored_error": str(exc)},
                )
            else:
                result = StepResult(step_id=step.id, status=StepStatus.FAILED, summary=str(exc))
        self._step_statuses[step_id] = result.status
        self._step_results[step_id] = result
        self._emit_progress(
            "step_finished",
            step_id=step.id,
            kind=step.kind.value,
            title=step.title,
            session_name=step.session,
            status=result.status.value,
            summary=result.summary,
            index=index,
            total=total,
        )
        return result.to_dict()

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
        state = self._ensure_session(session_name)
        backend_session_name = state.backend_session_name
        command = self._command_with_session_logging(command, state)
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
        cleaned_output = self._clean_command_output(result.output)
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
        backend_session_name = self._ensure_session(session_name).backend_session_name
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
        backend_session_name = self._ensure_session(session_name).backend_session_name
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
        backend_session_name = self._ensure_session(session_name).backend_session_name
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

    def _execute_step(self, step: WorkflowStep) -> StepResult:
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

    def _execute_command_step(self, step: CommandStep) -> StepResult:
        if not step.session:
            raise ValueError(f"Command step {step.id} is missing session")
        state = self._ensure_session(step.session)
        backend_session_name = state.backend_session_name
        timeout_s = step.timeout_s or self.settings.default_timeout_s
        command = self._command_with_session_logging(step.command, state)
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
        cleaned_output = self._clean_command_output(result.output)
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
        backend_session_name = self._ensure_session(step.session).backend_session_name
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
        state = self._ensure_session(step.session)
        backend_session_name = state.backend_session_name
        command = self._command_with_session_logging(step.command, state)
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
            cleaned_output = self._clean_command_output(result.output)
            last_output = cleaned_output
            failed = False
            for pattern in step.fail_patterns:
                if re.search(pattern, cleaned_output, re.MULTILINE):
                    failed = True
                    break
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
            self.refresh_background_steps()
            statuses = {item: self._step_statuses[item] for item in wait_for}
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

    def _ensure_session(self, logical_name: str) -> SessionState:
        if logical_name not in self.workflow.sessions:
            raise KeyError(f"Unknown session: {logical_name}")
        if logical_name in self._session_states and self._session_states[logical_name].initialized:
            return self._session_states[logical_name]
        spec = self.workflow.sessions[logical_name]
        state = self._session_states.get(logical_name)
        if not state:
            safe_workflow = self._safe_name(self.workflow.name, fallback="workflow", max_len=12)
            safe_session = self._safe_name(logical_name, fallback="session", max_len=12)
            log_name = self._session_log_names[logical_name]
            session_log_dir = self.log_dir / log_name
            session_log_dir.mkdir(parents=True, exist_ok=True)
            combined_log_path = session_log_dir / "session.log"
            stdout_log_path = session_log_dir / "stdout.log"
            stderr_log_path = session_log_dir / "stderr.log"
            for path in (combined_log_path, stdout_log_path, stderr_log_path):
                path.touch(exist_ok=True)
            state = SessionState(
                logical_name=logical_name,
                log_name=log_name,
                backend_session_name=f"mta-{safe_workflow[:12]}-{safe_session[:12]}-{self._run_id}",
                combined_log_path=str(combined_log_path),
                stdout_log_path=str(stdout_log_path),
                stderr_log_path=str(stderr_log_path),
            )
            self._session_states[logical_name] = state
        if not self.backend.session_exists(state.backend_session_name):
            self.backend.create_session(state.backend_session_name, shell=spec.shell)
            self.backend.attach_combined_log(state.backend_session_name, state.combined_log_path)
            self._prepare_session(state, spec)
            self._emit_progress(
                "session_initialized",
                session_name=logical_name,
                log_name=state.log_name,
                backend=self.backend.backend_name,
                backend_session_name=state.backend_session_name,
                combined_log_path=state.combined_log_path,
                stdout_log_path=state.stdout_log_path,
                stderr_log_path=state.stderr_log_path,
            )
        state.initialized = True
        return state

    def _prepare_session(self, state: SessionState, spec: SessionSpec) -> None:
        backend_session_name = state.backend_session_name
        timeout_s = 60
        shell_program = ""
        try:
            shell_program = Path(shlex.split(spec.shell)[0]).name
        except (IndexError, ValueError):
            shell_program = ""
        if shell_program == "bash":
            self._run_setup_command(
                backend_session_name,
                "set +H; set +m",
                timeout_s=timeout_s,
                lines=40,
                description="configuring bash session options",
            )
        if spec.workdir:
            self._run_setup_command(
                backend_session_name,
                f"cd {shlex.quote(spec.workdir)}",
                timeout_s=timeout_s,
                lines=80,
                description=f"changing directory to {spec.workdir}",
            )
        for command in self.backend.build_export_commands(spec.env):
            self._run_setup_command(
                backend_session_name,
                command,
                timeout_s=timeout_s,
                lines=80,
                description=f"exporting environment via {command}",
            )
        connect_command = self._build_connect_command(spec)
        if connect_command:
            self.backend.send_literal(backend_session_name, connect_command)
            if spec.connect_ready_pattern:
                wait = self.backend.wait_for_pattern(
                    backend_session_name,
                    spec.connect_ready_pattern,
                    timeout_s=timeout_s,
                    lines=120,
                )
                if wait.status != "matched":
                    raise RuntimeError(
                        f"Failed to establish session transport {spec.transport.value}: {wait.output}"
                    )
        for command in spec.startup_commands:
            self._run_setup_command(
                backend_session_name,
                self._command_with_session_logging(command, state),
                timeout_s=timeout_s,
                lines=120,
                description=f"running startup command {command}",
            )

    def _run_setup_command(
        self,
        backend_session_name: str,
        command: str,
        *,
        timeout_s: int,
        lines: int,
        description: str,
    ) -> None:
        result = self.backend.run_command(
            backend_session_name,
            command,
            timeout_s=timeout_s,
            lines=lines,
        )
        cleaned_output = self._clean_command_output(result.output)
        if result.exit_code != 0:
            detail = cleaned_output or f"command={command}"
            raise RuntimeError(f"Session setup failed while {description}: {detail}")

    def _create_log_dir(self) -> Path:
        root = Path(self.settings.log_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        safe_workflow = self._safe_name(self.workflow.name, fallback="workflow", max_len=40)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_dir = root / f"{timestamp}-{safe_workflow}-{self._run_id}"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir

    @staticmethod
    def _safe_name(value: str, *, fallback: str, max_len: int) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
        if not safe:
            safe = fallback
        return safe[:max_len]

    def _plan_session_log_names(self) -> dict[str, str]:
        assigned: dict[str, str] = {}
        used: set[str] = set()
        for logical_name, spec in self.workflow.sessions.items():
            base_name = self._preferred_log_name(logical_name, spec)
            candidate = base_name
            suffix = 2
            while candidate in used:
                candidate = f"{base_name}_{suffix}"
                suffix += 1
            used.add(candidate)
            assigned[logical_name] = candidate
        return assigned

    def _preferred_log_name(self, logical_name: str, spec: SessionSpec) -> str:
        role = self._infer_session_role(logical_name)
        if role:
            return role
        lowered = logical_name.lower()
        if "server" in lowered:
            return "server"
        if "client" in lowered or "probe" in lowered or "curl" in lowered:
            return "client"
        if spec.transport is SessionTransport.SSH:
            return "ssh"
        if spec.transport in {SessionTransport.DOCKER_EXEC, SessionTransport.DOCKER_RUN}:
            return "docker"
        return self._safe_name(logical_name, fallback="session", max_len=40)

    def _infer_session_role(self, logical_name: str) -> str | None:
        server_score = 0
        client_score = 0
        lowered_name = logical_name.lower()
        if lowered_name.endswith("_client") or "_client_" in lowered_name or "client" in lowered_name:
            client_score += 2
        if lowered_name.endswith("_server") or "_server_" in lowered_name or "server" in lowered_name:
            server_score += 2
        for step in self.workflow.steps:
            if getattr(step, "session", None) != logical_name:
                continue
            if isinstance(step, CommandStep):
                haystack = f"{step.title} {step.command}".lower()
                if step.background:
                    server_score += 2
                if step.ready_pattern:
                    server_score += 1
                if self._looks_like_server_activity(haystack):
                    server_score += 2
                if self._looks_like_client_activity(haystack):
                    client_score += 2
            elif isinstance(step, ProbeStep):
                client_score += 3 if self._looks_like_client_activity(step.command.lower()) else 1
            elif isinstance(step, WaitStep):
                server_score += 1 if "ready" in step.pattern.lower() else 0
            elif isinstance(step, SendKeysStep):
                joined = " ".join(step.keys).lower()
                if "c-c" in joined or "ctrl-c" in joined:
                    server_score += 1
        if server_score > client_score and server_score > 0:
            return "server"
        if client_score > server_score and client_score > 0:
            return "client"
        return None

    @staticmethod
    def _looks_like_server_activity(text: str) -> bool:
        return any(
            token in text
            for token in (
                "server",
                "serve",
                "listen",
                "ready_pattern",
                "uvicorn",
                "gunicorn",
                "http.server",
                "startup-delay",
                "--host",
                "--port",
                "flask run",
                "fastapi",
                "vllm",
            )
        )

    @staticmethod
    def _looks_like_client_activity(text: str) -> bool:
        return any(
            token in text
            for token in (
                "curl ",
                "wget ",
                "http://",
                "https://",
                "benchmark",
                "client",
                "request",
                "probe",
                "healthz",
            )
        )

    def _command_with_session_logging(self, command: str, state: SessionState) -> str:
        if self._command_requires_tty(command):
            return command
        body = command.rstrip()
        if not body.endswith((";", "&")):
            body = f"{body};"
        token = uuid.uuid4().hex[:8]
        helper_name = f"__mta_logged_run_{token}"
        session_log_dir = Path(state.stdout_log_path).parent
        stdout_fifo = shlex.quote(str(session_log_dir / f"stdout-{token}.fifo"))
        stderr_fifo = shlex.quote(str(session_log_dir / f"stderr-{token}.fifo"))
        stdout_log = shlex.quote(state.stdout_log_path)
        stderr_log = shlex.quote(state.stderr_log_path)
        return (
            f"{helper_name}() {{ "
            f"local __mta_stdout_fifo={stdout_fifo}; "
            f"local __mta_stderr_fifo={stderr_fifo}; "
            'rm -f "$__mta_stdout_fifo" "$__mta_stderr_fifo"; '
            'mkfifo "$__mta_stdout_fifo" "$__mta_stderr_fifo"; '
            f'tee -a {stdout_log} < "$__mta_stdout_fifo" & '
            "local __mta_stdout_tee=$!; "
            f'tee -a {stderr_log} < "$__mta_stderr_fifo" >&2 & '
            "local __mta_stderr_tee=$!; "
            f"{{ {body} }} > \"$__mta_stdout_fifo\" 2> \"$__mta_stderr_fifo\"; "
            "local __mta_status=$?; "
            'wait "$__mta_stdout_tee" "$__mta_stderr_tee"; '
            'rm -f "$__mta_stdout_fifo" "$__mta_stderr_fifo"; '
            'return "$__mta_status"; '
            "}; "
            f"{helper_name}; "
            "__mta_status=$?; "
            f"unset -f {helper_name}; "
            '(exit "$__mta_status")'
        )

    @staticmethod
    def _command_requires_tty(command: str) -> bool:
        stripped = command.strip()
        if not stripped:
            return False
        interactive_tokens = (
            " -it ",
            " --interactive ",
            " --tty ",
            " docker attach ",
            " tmux attach",
            " ssh -tt ",
        )
        padded = f" {stripped} "
        if any(token in padded for token in interactive_tokens):
            return True
        try:
            parts = shlex.split(stripped)
        except ValueError:
            return False
        if not parts:
            return False
        program = parts[0]
        if program in {"vim", "vi", "nvim", "nano", "less", "more", "top", "htop", "watch", "tmux", "screen"}:
            return True
        if program in {"ssh", "sftp"}:
            return True
        if program in {"docker", "podman"} and len(parts) >= 2 and parts[1] in {"run", "exec"}:
            flags = set(parts[2:])
            if {"-i", "-t"} <= flags or any(item.startswith("-it") for item in parts[2:]):
                return True
        if program in {"bash", "sh", "zsh", "fish"} and len(parts) <= 2:
            return True
        if program == "cat":
            file_args = [item for item in parts[1:] if not item.startswith("-")]
            if not file_args:
                return True
        return False

    @staticmethod
    def _clean_command_output(output: str) -> str:
        cleaned_lines: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if re.fullmatch(r"\[\d+\]\s+\d+", stripped):
                continue
            if re.fullmatch(r"\[\d+\][+-]?\s+Done\s+tee -a .+", stripped):
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    @staticmethod
    def _build_connect_command(spec: SessionSpec) -> str | None:
        if spec.transport is SessionTransport.LOCAL:
            return None
        if spec.transport is SessionTransport.SSH:
            if not spec.ssh_host:
                raise ValueError(f"SSH session {spec.name} is missing ssh_host")
            target = spec.ssh_host
            if spec.ssh_user:
                target = f"{spec.ssh_user}@{target}"
            return f"ssh -tt -p {spec.ssh_port} {target}"
        if spec.transport is SessionTransport.DOCKER_EXEC:
            if not spec.docker_container:
                raise ValueError(f"Docker exec session {spec.name} is missing docker_container")
            return f"docker exec -it {spec.docker_container} {spec.shell}"
        if spec.transport is SessionTransport.DOCKER_RUN:
            if not spec.docker_image:
                raise ValueError(f"Docker run session {spec.name} is missing docker_image")
            args = " ".join(shlex.quote(item) for item in spec.docker_run_args)
            return f"docker run -it {args} {spec.docker_image} {spec.shell}".strip()
        raise ValueError(f"Unsupported transport: {spec.transport}")
