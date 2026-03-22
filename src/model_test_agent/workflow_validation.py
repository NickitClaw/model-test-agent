from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import BarrierStep, DecisionStep, StepBase, WorkflowSpec


@dataclass
class WorkflowValidationError(ValueError):
    message: str

    def __str__(self) -> str:
        return self.message


def build_and_validate_workflow(payload: dict[str, Any]) -> WorkflowSpec:
    workflow = WorkflowSpec.from_dict(payload)
    validate_workflow_spec(workflow)
    return workflow


def validate_workflow_spec(workflow: WorkflowSpec) -> None:
    if not workflow.name.strip():
        raise WorkflowValidationError("Workflow name must not be empty")
    if not workflow.steps:
        raise WorkflowValidationError("Workflow must define at least one step")

    known_sessions = set(workflow.sessions)
    seen_step_ids: set[str] = set()
    all_step_ids = [step.id for step in workflow.steps]
    for step in workflow.steps:
        if step.id in seen_step_ids:
            raise WorkflowValidationError(f"Duplicate step id: {step.id}")
        seen_step_ids.add(step.id)
        if not step.title.strip():
            raise WorkflowValidationError(f"Step {step.id} must have a title")
        _validate_step_references(step, known_sessions, seen_step_ids, all_step_ids)


def _validate_step_references(
    step: StepBase,
    known_sessions: set[str],
    seen_step_ids: set[str],
    all_step_ids: list[str],
) -> None:
    if step.session and step.session not in known_sessions:
        raise WorkflowValidationError(f"Step {step.id} references unknown session {step.session!r}")
    for dep in step.depends_on:
        if dep not in all_step_ids:
            raise WorkflowValidationError(f"Step {step.id} depends on unknown step {dep!r}")
        if dep == step.id:
            raise WorkflowValidationError(f"Step {step.id} cannot depend on itself")
    if isinstance(step, BarrierStep):
        for dep in step.wait_for:
            if dep not in all_step_ids:
                raise WorkflowValidationError(f"Barrier step {step.id} waits for unknown step {dep!r}")
    if isinstance(step, DecisionStep):
        for rule in step.rules:
            if rule.target_step and rule.target_step not in all_step_ids:
                raise WorkflowValidationError(
                    f"Decision step {step.id} targets unknown step {rule.target_step!r}"
                )
