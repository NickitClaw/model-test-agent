from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from .agent import ModelTestAgent
from .config import Settings
from .document_loader import DocumentLoader
from .models import WorkflowSpec
from .planner import WorkflowPlanner
from .progress import ConsoleProgressReporter
from .runtime.executor import WorkflowExecutor
from .runtime.factory import resolve_session_backend
from .workflow_enricher import WorkflowEnricher
from .workflow_normalizer import WorkflowNormalizer


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    if not getattr(args, "command", None):
        parser.print_help()
        return
    settings = Settings.from_env()
    enricher = WorkflowEnricher()
    normalizer = WorkflowNormalizer()
    loader = DocumentLoader()
    if args.command == "doctor":
        _doctor(settings)
        return
    if args.command == "plan":
        document = loader.load(args.document)
        workflow = WorkflowPlanner(settings).plan(
            document,
            objective_hint=args.objective or "",
            extra_instructions=args.instructions or "",
        )
        payload = json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(payload)
        else:
            print(payload)
        return
    if args.command == "run-workflow":
        workflow = WorkflowSpec.from_dict(json.loads(Path(args.workflow).read_text()))
        workflow = normalizer.normalize(workflow, args.workflow)
        workflow = enricher.enrich(workflow)
        report = _run_workflow_with_agent(workflow, settings)
        print(_format_run_summary(status=report.status, summary=report.summary, state=report.state, iterations=report.iterations))
        return
    if args.command == "exec-workflow":
        workflow = WorkflowSpec.from_dict(json.loads(Path(args.workflow).read_text()))
        workflow = normalizer.normalize(workflow, args.workflow)
        workflow = enricher.enrich(workflow)
        _exec_workflow(workflow, settings, keep_sessions=args.keep_sessions)
        return
    if args.command == "run-document":
        document = loader.load(args.document)
        workflow = WorkflowPlanner(settings).plan(
            document,
            objective_hint=args.objective or "",
            extra_instructions=args.instructions or "",
        )
        if args.plan_output:
            Path(args.plan_output).write_text(json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False))
        report = _run_workflow_with_agent(workflow, settings)
        print(_format_run_summary(status=report.status, summary=report.summary, state=report.state, iterations=report.iterations))
        return
    raise ValueError(f"Unsupported command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mta", description="Model test agent")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="Check local CLI prerequisites")
    doctor.set_defaults(command="doctor")

    plan = sub.add_parser("plan", help="Plan a workflow from a document")
    plan.add_argument("document", help="Path to a Markdown/TXT/PDF document")
    plan.add_argument("--output", help="Write workflow JSON to this path")
    plan.add_argument("--objective", help="Optional objective hint")
    plan.add_argument("--instructions", help="Extra planning instructions")

    run_workflow = sub.add_parser("run-workflow", help="Run an existing workflow JSON file")
    run_workflow.add_argument("workflow", help="Path to a workflow JSON file")

    exec_workflow = sub.add_parser("exec-workflow", help="Run a workflow JSON directly without the LLM agent")
    exec_workflow.add_argument("workflow", help="Path to a workflow JSON file")
    exec_workflow.add_argument(
        "--keep-sessions",
        action="store_true",
        help="Do not tear down shell sessions after execution",
    )

    run_document = sub.add_parser("run-document", help="Plan and run a document in one step")
    run_document.add_argument("document", help="Path to a Markdown/TXT/PDF document")
    run_document.add_argument("--plan-output", help="Optional path to save the planned workflow JSON")
    run_document.add_argument("--objective", help="Optional objective hint")
    run_document.add_argument("--instructions", help="Extra planning instructions")
    return parser


def _doctor(settings: Settings) -> None:
    resolved_backend = resolve_session_backend(settings)
    print(f"session backend: {settings.session_backend} -> {resolved_backend}")
    rows = [
        ("tmux", settings.tmux_bin),
        ("ssh", "ssh"),
        ("docker", "docker"),
    ]
    for label, binary in rows:
        resolved = shutil.which(binary)
        state = "ok" if resolved else "missing"
        print(f"{label}: {state} ({resolved or binary})")
    model_state = "configured" if settings.base_url and settings.model else "missing"
    print(f"model access: {model_state}")


def _exec_workflow(workflow: WorkflowSpec, settings: Settings, *, keep_sessions: bool = False) -> None:
    reporter = ConsoleProgressReporter()
    executor = WorkflowExecutor(workflow, settings, progress_callback=reporter.emit)
    reporter.bind_executor(executor)
    try:
        while not executor.all_steps_finished():
            ready = [item["id"] for item in executor.list_steps(only_ready=True)]
            if not ready:
                notifications = executor.drain_notifications()
                if notifications:
                    print(json.dumps({"notifications": notifications}, ensure_ascii=False, indent=2))
                    continue
                print(json.dumps(executor.describe_state(), ensure_ascii=False, indent=2))
                raise RuntimeError("No ready steps are available; workflow may be deadlocked")
            step_id = ready[0]
            result = executor.run_step(step_id)
            print(json.dumps({"step_id": step_id, "result": result}, ensure_ascii=False, indent=2))
            time.sleep(0.05)
        final_state = executor.describe_state()
        print(
            _format_run_summary(
                status=_derive_run_status(final_state),
                summary="Workflow finished",
                state=final_state,
            )
        )
    finally:
        reporter.stop()
        if not keep_sessions:
            executor.close_all()


def _run_workflow_with_agent(workflow: WorkflowSpec, settings: Settings):
    reporter = ConsoleProgressReporter()
    executor = WorkflowExecutor(workflow, settings, progress_callback=reporter.emit)
    reporter.bind_executor(executor)
    try:
        agent = ModelTestAgent(
            settings=settings,
            workflow=workflow,
            executor=executor,
            progress_callback=reporter.emit,
        )
        return agent.run()
    finally:
        reporter.stop()
        executor.close_all()


def _format_run_summary(
    *,
    status: str,
    summary: str,
    state: dict[str, Any],
    iterations: int | None = None,
) -> str:
    run = state.get("run", {})
    workflow = state.get("workflow", {})
    steps = state.get("steps", [])
    sessions = state.get("sessions", [])
    total = len(steps)
    completed = sum(1 for item in steps if item.get("status") in {"completed", "skipped"})
    failed = [item for item in steps if item.get("status") == "failed"]
    pending = [item for item in steps if item.get("status") == "pending"]
    background = [item for item in steps if item.get("status") == "background"]

    lines = [
        "Run Summary",
        f"workflow: {workflow.get('name', '')}",
        f"status: {status}",
        f"summary: {summary}",
    ]
    if iterations is not None:
        lines.append(f"agent iterations: {iterations}")
    if run.get("backend"):
        lines.append(f"backend: {run['backend']}")
    if run.get("log_dir"):
        lines.append(f"log dir: {run['log_dir']}")
    lines.append(
        "steps: "
        f"{completed}/{total} completed, "
        f"{len(failed)} failed, "
        f"{len(background)} background, "
        f"{len(pending)} pending"
    )

    if failed:
        lines.append("failed steps:")
        for item in failed:
            result = item.get("result") or {}
            detail = result.get("summary") or "failed"
            lines.append(f"- {item.get('id')}: {detail}")

    if background:
        lines.append("background steps:")
        for item in background:
            lines.append(f"- {item.get('id')}: {item.get('title')}")

    if sessions:
        lines.append("session logs:")
        for item in sessions:
            label = str(item.get("name") or "session")
            log_name = item.get("log_name")
            if log_name and log_name != item.get("name"):
                label = f"{label} [logs: {log_name}]"
            lines.append(
                f"- {label} ({item.get('transport')}/{item.get('backend')}): "
                f"session={item.get('combined_log_path')} | "
                f"stdout={item.get('stdout_log_path')} | "
                f"stderr={item.get('stderr_log_path')}"
            )

    return "\n".join(lines)


def _derive_run_status(state: dict[str, Any]) -> str:
    steps = state.get("steps", [])
    if any(item.get("status") == "failed" for item in steps):
        return "failed"
    if any(item.get("status") not in {"completed", "skipped"} for item in steps):
        return "incomplete"
    return "completed"


if __name__ == "__main__":
    main()
