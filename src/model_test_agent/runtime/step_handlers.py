from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, cast

from ..config import Settings
from ..models import (
    BarrierStep,
    CaptureStep,
    CommandStep,
    DecisionStep,
    ProbeStep,
    SendKeysStep,
    SleepStep,
    StepKind,
    StepResult,
    StepStatus,
    WaitStep,
    WorkflowStep,
)
from ..openai_compat import OpenAICompatClient
from .background import BackgroundTaskManager
from .session_manager import SessionManager
from .session_backend import SessionBackend


@dataclass
class StepExecutionContext:
    settings: Settings
    backend: SessionBackend
    background: BackgroundTaskManager
    session_manager: SessionManager
    step_statuses: dict[str, StepStatus]
    llm_client: OpenAICompatClient | None
    emit_progress: Callable[..., None]
    refresh_background_steps: Callable[[], None]
    run_command: Callable[..., dict[str, object]]
    wait_for_output: Callable[..., dict[str, object]]
    capture_session: Callable[..., dict[str, object]]
    send_keys: Callable[..., dict[str, object]]


class StepHandler(Protocol):
    kind: StepKind

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        ...


def default_step_handlers() -> list[StepHandler]:
    return [
        CommandStepHandler(),
        WaitStepHandler(),
        ProbeStepHandler(),
        SendKeysStepHandler(),
        SleepStepHandler(),
        BarrierStepHandler(),
        CaptureStepHandler(),
        DecisionStepHandler(),
    ]


class CommandStepHandler:
    kind = StepKind.COMMAND

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_command_step(step)
        if not step.session:
            raise ValueError(f"Command step {step.id} is missing session")
        state = ctx.session_manager.ensure_session(step.session)
        backend_session_name = state.backend_session_name
        timeout_s = step.timeout_s or ctx.settings.default_timeout_s
        command = ctx.session_manager.command_with_session_logging(step.command, state)
        if step.background:
            ctx.backend.send_literal(backend_session_name, command)
            task_id = None
            if step.ready_pattern:
                task_id = ctx.background.watch_output(
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
        result = ctx.backend.run_command(
            backend_session_name,
            command,
            timeout_s=timeout_s,
            lines=step.capture_lines,
        )
        cleaned_output = ctx.session_manager.clean_command_output(result.output)
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


class WaitStepHandler:
    kind = StepKind.WAIT

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_wait_step(step)
        if not step.session:
            raise ValueError(f"Wait step {step.id} is missing session")
        backend_session_name = ctx.session_manager.ensure_session(step.session).backend_session_name
        timeout_s = step.timeout_s or ctx.settings.default_timeout_s
        if step.background:
            task_id = ctx.background.watch_output(
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
        ctx.emit_progress(
            "wait_started",
            step_id=step.id,
            session_name=step.session,
            pattern=step.pattern,
            timeout_s=timeout_s,
        )
        wait = ctx.backend.wait_for_pattern(
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


class ProbeStepHandler:
    kind = StepKind.PROBE

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_probe_step(step)
        if not step.session:
            raise ValueError(f"Probe step {step.id} is missing session")
        state = ctx.session_manager.ensure_session(step.session)
        backend_session_name = state.backend_session_name
        command = ctx.session_manager.command_with_session_logging(step.command, state)
        timeout_s = step.timeout_s or ctx.settings.default_timeout_s
        deadline = time.time() + timeout_s
        last_output = ""
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            remaining = max(1, int(deadline - time.time()))
            result = ctx.backend.run_command(
                backend_session_name,
                command,
                timeout_s=min(remaining, max(5, ctx.settings.default_timeout_s)),
                lines=step.capture_lines,
            )
            cleaned_output = ctx.session_manager.clean_command_output(result.output)
            last_output = cleaned_output
            failed = any(re.search(pattern, cleaned_output, re.MULTILINE) for pattern in step.fail_patterns)
            success_match = True
            if step.success_patterns:
                success_match = any(
                    re.search(pattern, cleaned_output, re.MULTILINE) for pattern in step.success_patterns
                )
            if not failed and result.exit_code == step.expect_exit_code and success_match:
                return StepResult(
                    step_id=step.id,
                    status=StepStatus.COMPLETED,
                    summary=f"Probe succeeded after {attempt} attempts in session {step.session}",
                    output=cleaned_output,
                    metadata={"attempts": attempt, "exit_code": result.exit_code},
                )
            ctx.emit_progress(
                "probe_retry",
                step_id=step.id,
                attempt=attempt,
                elapsed_s=timeout_s - remaining,
                remaining_s=remaining,
            )
            time.sleep(step.interval_s)
        raise TimeoutError(f"Probe timed out for step {step.id}: {last_output}")


class SendKeysStepHandler:
    kind = StepKind.SEND_KEYS

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_send_keys_step(step)
        if not step.session:
            raise ValueError(f"Send-keys step {step.id} is missing session")
        ctx.send_keys(
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


class SleepStepHandler:
    kind = StepKind.SLEEP

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_sleep_step(step)
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
                ctx.emit_progress(
                    "sleep_progress",
                    step_id=step.id,
                    elapsed_s=step.seconds - remaining,
                    remaining_s=remaining,
                )
        return StepResult(step_id=step.id, status=StepStatus.COMPLETED, summary=f"Slept for {step.seconds}s")


class BarrierStepHandler:
    kind = StepKind.BARRIER

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_barrier_step(step)
        wait_for = step.wait_for or step.depends_on
        if not wait_for:
            return StepResult(step_id=step.id, status=StepStatus.COMPLETED, summary="Barrier had no targets")
        timeout_s = step.timeout_s or ctx.settings.default_timeout_s
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            ctx.refresh_background_steps()
            statuses = {item: ctx.step_statuses[item] for item in wait_for}
            if all(status is StepStatus.COMPLETED for status in statuses.values()):
                return StepResult(
                    step_id=step.id,
                    status=StepStatus.COMPLETED,
                    summary=f"Barrier released after steps {wait_for}",
                )
            failed = [item for item, status in statuses.items() if status is StepStatus.FAILED]
            if failed:
                raise RuntimeError(f"Barrier saw failed dependencies: {failed}")
            ctx.emit_progress(
                "barrier_wait",
                step_id=step.id,
                statuses={item: status.value for item, status in statuses.items()},
                elapsed_s=timeout_s - max(0.0, deadline - time.time()),
            )
            time.sleep(step.poll_interval_s)
        raise TimeoutError(f"Barrier timed out while waiting for: {wait_for}")


class CaptureStepHandler:
    kind = StepKind.CAPTURE

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_capture_step(step)
        session_name = step.source_session or step.session
        if not session_name:
            raise ValueError(f"Capture step {step.id} is missing source_session")
        capture = ctx.capture_session(session_name=session_name, lines=step.lines)
        return StepResult(
            step_id=step.id,
            status=StepStatus.COMPLETED,
            summary=f"Captured {step.lines} lines from session {session_name}",
            output=str(capture["output"]),
        )


class DecisionStepHandler:
    kind = StepKind.DECISION

    def execute(self, step: WorkflowStep, ctx: StepExecutionContext) -> StepResult:
        step = _as_decision_step(step)
        session_name = step.source_session or step.session
        output = ""
        if session_name:
            output = str(ctx.capture_session(session_name=session_name)["output"])
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
            if not ctx.llm_client:
                raise RuntimeError("Decision step requested llm_prompt but no LLM client is configured")
            payload = ctx.llm_client.complete_json(
                model=ctx.settings.agent_model,
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


def _as_command_step(step: WorkflowStep) -> CommandStep:
    return cast(CommandStep, step)


def _as_wait_step(step: WorkflowStep) -> WaitStep:
    return cast(WaitStep, step)


def _as_probe_step(step: WorkflowStep) -> ProbeStep:
    return cast(ProbeStep, step)


def _as_send_keys_step(step: WorkflowStep) -> SendKeysStep:
    return cast(SendKeysStep, step)


def _as_sleep_step(step: WorkflowStep) -> SleepStep:
    return cast(SleepStep, step)


def _as_barrier_step(step: WorkflowStep) -> BarrierStep:
    return cast(BarrierStep, step)


def _as_capture_step(step: WorkflowStep) -> CaptureStep:
    return cast(CaptureStep, step)


def _as_decision_step(step: WorkflowStep) -> DecisionStep:
    return cast(DecisionStep, step)
