from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from ..config import Settings
from ..models import StepResult, StepStatus, WorkflowSpec
from ..openai_compat import OpenAICompatClient
from .background import BackgroundTaskManager
from .factory import create_session_backend
from .failure_summary import FailureSummaryBuilder
from .session_backend import SessionBackend
from .session_manager import SessionManager, SessionState
from .step_runner import StepRunner
from .structured_logging import StructuredRunLogger


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
        self._run_id = uuid.uuid4().hex[:8]
        self._progress_callback = progress_callback
        self.session_manager = SessionManager(
            workflow,
            settings,
            self.backend,
            run_id=self._run_id,
            progress_callback=self._emit_progress,
        )
        self.log_dir = self.session_manager.log_dir
        self.structured_logger = StructuredRunLogger(
            log_dir=self.log_dir,
            run_id=self._run_id,
            workflow_name=self.workflow.name,
        )
        self.failure_summary_builder = FailureSummaryBuilder()
        self.step_runner = StepRunner(
            settings=settings,
            backend=self.backend,
            background=self.background,
            session_manager=self.session_manager,
            step_statuses=self._step_statuses,
            llm_client=self.llm_client,
            progress_callback=self._emit_progress,
            refresh_background_steps=self.refresh_background_steps,
        )

    def _emit_progress(self, event: str | dict[str, Any], **payload: Any) -> None:
        if isinstance(event, dict):
            record = dict(event)
            event_name = str(record.get("event", "unknown"))
            event_payload = {key: value for key, value in record.items() if key != "event"}
        else:
            event_name = event
            event_payload = payload
            record = {"event": event_name, **event_payload}
        self.structured_logger.log_event(event_name, event_payload)
        if self._progress_callback is None:
            return
        self._progress_callback(record)

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
        return self.session_manager.list_sessions()

    def list_background_tasks(self) -> list[dict[str, object]]:
        self.refresh_background_steps()
        return [task.to_dict() for task in self.background.list_tasks()]

    def get_background_task(self, task_id: str) -> dict[str, object]:
        self.refresh_background_steps()
        return self.background.get_task(task_id).to_dict()

    def describe_state(self, *, include_diagnostics: bool = False) -> dict[str, object]:
        self.refresh_background_steps()
        steps = self.list_steps()
        sessions = self.list_sessions()
        run = {
            "id": self._run_id,
            "backend": self.backend.backend_name,
            "log_dir": str(self.log_dir),
            "event_log_path": str(self.structured_logger.events_path),
            "summary_path": str(self.structured_logger.summary_path),
        }
        if include_diagnostics:
            run["failure_excerpts"] = self.failure_summary_builder.collect(
                steps=steps,
                sessions=sessions,
            )
        return {
            "run": run,
            "workflow": self.workflow.to_dict(),
            "steps": steps,
            "sessions": sessions,
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
        self.session_manager.close_all()

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
            result = self.step_runner.execute_step(step)
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
        return self.step_runner.run_command(
            session_name=session_name,
            command=command,
            timeout_s=timeout_s,
            background=background,
            ready_pattern=ready_pattern,
            fail_patterns=fail_patterns,
            capture_lines=capture_lines,
        )

    def wait_for_output(
        self,
        *,
        session_name: str,
        pattern: str,
        timeout_s: int | None = None,
        fail_patterns: list[str] | None = None,
        capture_lines: int | None = None,
    ) -> dict[str, object]:
        return self.step_runner.wait_for_output(
            session_name=session_name,
            pattern=pattern,
            timeout_s=timeout_s,
            fail_patterns=fail_patterns,
            capture_lines=capture_lines,
        )

    def capture_session(self, *, session_name: str, lines: int | None = None) -> dict[str, object]:
        return self.step_runner.capture_session(session_name=session_name, lines=lines)

    def send_keys(
        self,
        *,
        session_name: str,
        keys: list[str],
        literal: bool = False,
        press_enter: bool = False,
        delay_s: float = 0.0,
    ) -> dict[str, object]:
        return self.step_runner.send_keys(
            session_name=session_name,
            keys=keys,
            literal=literal,
            press_enter=press_enter,
            delay_s=delay_s,
        )

    def _ensure_session(self, logical_name: str) -> SessionState:
        return self.session_manager.ensure_session(logical_name)

    def _command_with_session_logging(self, command: str, state: SessionState) -> str:
        return self.session_manager.command_with_session_logging(command, state)

    @staticmethod
    def _clean_command_output(output: str) -> str:
        return SessionManager.clean_command_output(output)

    def write_summary_artifact(
        self,
        *,
        status: str,
        summary: str,
        state: dict[str, Any],
        iterations: int | None = None,
    ) -> None:
        failure_excerpts = list((state.get("run", {}) or {}).get("failure_excerpts") or [])
        if not failure_excerpts:
            failure_excerpts = self.failure_summary_builder.collect(
                steps=list(state.get("steps", []) or []),
                sessions=list(state.get("sessions", []) or []),
            )
            run = dict(state.get("run", {}) or {})
            run["failure_excerpts"] = failure_excerpts
            state = dict(state)
            state["run"] = run
        self.structured_logger.write_summary(
            status=status,
            summary=summary,
            state=state,
            iterations=iterations,
            failure_excerpts=failure_excerpts,
        )
