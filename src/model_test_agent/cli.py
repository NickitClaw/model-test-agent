from __future__ import annotations

import argparse
import json
import shutil
import textwrap
import time
from pathlib import Path
from typing import Any

from .agent import ModelTestAgent
from .config import Settings
from .document_loader import DocumentLoader
from .models import WorkflowSpec
from .planner import WorkflowPlanner
from .progress import ConsoleProgressReporter, summarize_workflow
from .runtime.executor import WorkflowExecutor
from .runtime.factory import resolve_session_backend
from .workflow_schema import get_workflow_json_schema
from .workflow_validation import build_and_validate_workflow
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
    if args.command == "runs":
        root = Path(args.root or settings.log_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        rows = _collect_runs(root, status=args.status, limit=args.limit)
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            print(_format_runs_table(rows, root=root))
        return
    if args.command == "show-run":
        root = Path(args.root or settings.log_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        payload = _resolve_run_summary(root=root, selector=args.run)
        if args.json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(_format_single_run(payload))
        return
    if args.command == "schema":
        payload = json.dumps(get_workflow_json_schema(), indent=2, ensure_ascii=False)
        if args.output:
            Path(args.output).write_text(payload)
        else:
            print(payload)
        return
    if args.command == "validate-workflow":
        workflow = _load_workflow(Path(args.workflow), normalize=False, enrich=False)
        print(f"workflow is valid: {workflow.name}")
        return
    if args.command == "plan":
        reporter = ConsoleProgressReporter()
        document = loader.load(args.document)
        reporter.emit({"event": "planning_started", "path": str(document.path)})
        reporter.emit(
            {
                "event": "document_loaded",
                "media_type": document.media_type,
                "line_count": document.text.count("\n") + 1 if document.text else 0,
                "char_count": len(document.text),
            }
        )
        try:
            workflow = WorkflowPlanner(settings, progress_callback=reporter.emit).plan(
                document,
                objective_hint=args.objective or "",
                extra_instructions=args.instructions or "",
            )
            reporter.emit({"event": "workflow_planned", **summarize_workflow(workflow)})
            payload = json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(payload)
            else:
                print(payload)
        finally:
            reporter.stop()
        return
    if args.command == "run-workflow":
        reporter = ConsoleProgressReporter()
        workflow = _load_workflow(Path(args.workflow), normalizer=normalizer, enricher=enricher)
        try:
            reporter.emit({"event": "workflow_execution_started", **summarize_workflow(workflow)})
            report = _run_workflow_with_agent(workflow, settings, reporter=reporter)
            print(
                _format_run_summary(
                    status=report.status,
                    summary=report.summary,
                    state=report.state,
                    iterations=report.iterations,
                )
            )
        finally:
            reporter.stop()
        return
    if args.command == "exec-workflow":
        reporter = ConsoleProgressReporter()
        workflow = _load_workflow(Path(args.workflow), normalizer=normalizer, enricher=enricher)
        try:
            reporter.emit({"event": "workflow_execution_started", **summarize_workflow(workflow)})
            _exec_workflow(workflow, settings, keep_sessions=args.keep_sessions, reporter=reporter)
        finally:
            reporter.stop()
        return
    if args.command == "run-document":
        reporter = ConsoleProgressReporter()
        document = loader.load(args.document)
        try:
            reporter.emit({"event": "planning_started", "path": str(document.path)})
            reporter.emit(
                {
                    "event": "document_loaded",
                    "media_type": document.media_type,
                    "line_count": document.text.count("\n") + 1 if document.text else 0,
                    "char_count": len(document.text),
                }
            )
            workflow = WorkflowPlanner(settings, progress_callback=reporter.emit).plan(
                document,
                objective_hint=args.objective or "",
                extra_instructions=args.instructions or "",
            )
            reporter.emit({"event": "workflow_planned", **summarize_workflow(workflow)})
            if args.plan_output:
                Path(args.plan_output).write_text(json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False))
            reporter.emit({"event": "workflow_execution_started", **summarize_workflow(workflow)})
            report = _run_workflow_with_agent(workflow, settings, reporter=reporter)
            print(
                _format_run_summary(
                    status=report.status,
                    summary=report.summary,
                    state=report.state,
                    iterations=report.iterations,
                )
            )
        finally:
            reporter.stop()
        return
    raise ValueError(f"Unsupported command: {args.command}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mta", description="Model test agent")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="Check local CLI prerequisites")
    doctor.set_defaults(command="doctor")

    runs = sub.add_parser("runs", help="List recent run summaries from .mta-runs")
    runs.add_argument("--root", help="Override run log root")
    runs.add_argument("--status", help="Filter by run status")
    runs.add_argument("--limit", type=int, default=10, help="Maximum runs to display")
    runs.add_argument("--json", action="store_true", help="Print runs as JSON")
    runs.set_defaults(command="runs")

    show_run = sub.add_parser("show-run", help="Show one structured run summary")
    show_run.add_argument("run", help="Run id, run directory name, or path to summary.json/run dir")
    show_run.add_argument("--root", help="Override run log root")
    show_run.add_argument("--json", action="store_true", help="Print the raw summary JSON")
    show_run.set_defaults(command="show-run")

    schema = sub.add_parser("schema", help="Print the workflow JSON Schema")
    schema.add_argument("--output", help="Write schema JSON to this path")
    schema.set_defaults(command="schema")

    validate = sub.add_parser("validate-workflow", help="Validate a workflow JSON file")
    validate.add_argument("workflow", help="Path to a workflow JSON file")
    validate.set_defaults(command="validate-workflow")

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


def _load_workflow(
    path: Path,
    *,
    normalizer: WorkflowNormalizer | None = None,
    enricher: WorkflowEnricher | None = None,
    normalize: bool = True,
    enrich: bool = True,
) -> WorkflowSpec:
    payload = json.loads(path.read_text())
    workflow = build_and_validate_workflow(payload)
    if normalize:
        workflow = (normalizer or WorkflowNormalizer()).normalize(workflow, path)
    if enrich:
        workflow = (enricher or WorkflowEnricher()).enrich(workflow)
    return workflow


def _collect_runs(root: Path, *, status: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for summary_path in sorted(root.glob("*/summary.json"), reverse=True):
        try:
            payload = json.loads(summary_path.read_text())
        except Exception:
            continue
        if status and str(payload.get("status")) != status:
            continue
        state = payload.get("state", {})
        run = state.get("run", {})
        rows.append(
            {
                "run_id": payload.get("run_id") or run.get("id"),
                "workflow": payload.get("workflow"),
                "status": payload.get("status"),
                "summary": payload.get("summary"),
                "iterations": payload.get("iterations"),
                "backend": run.get("backend"),
                "log_dir": run.get("log_dir"),
                "summary_path": str(summary_path),
                "ts": payload.get("ts"),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _resolve_run_summary(*, root: Path, selector: str) -> dict[str, Any]:
    candidate = Path(selector).expanduser()
    summary_path: Path | None = None
    if candidate.exists():
        if candidate.is_dir():
            summary_path = candidate / "summary.json"
        else:
            summary_path = candidate
    else:
        by_id = sorted(root.glob(f"*{selector}*/summary.json"))
        if by_id:
            summary_path = by_id[-1]
        else:
            direct = root / selector / "summary.json"
            if direct.exists():
                summary_path = direct
    if summary_path is None or not summary_path.exists():
        raise FileNotFoundError(f"Could not resolve run summary for {selector!r}")
    return json.loads(summary_path.read_text())


def _format_runs_table(rows: list[dict[str, Any]], *, root: Path) -> str:
    if not rows:
        return f"No runs found under {root}"
    lines = [f"Recent runs under {root}"]
    for item in rows:
        lines.append(
            f"- {item.get('run_id')} | {item.get('status')} | {item.get('workflow')} | "
            f"backend={item.get('backend')} | dir={item.get('log_dir')}"
        )
        summary = str(item.get("summary") or "").strip()
        if summary:
            lines.append(f"  summary: {summary}")
    return "\n".join(lines)


def _format_single_run(payload: dict[str, Any]) -> str:
    state = payload.get("state", {})
    return _format_run_summary(
        status=str(payload.get("status") or "unknown"),
        summary=str(payload.get("summary") or ""),
        state=state,
        iterations=payload.get("iterations"),
    )


def _exec_workflow(
    workflow: WorkflowSpec,
    settings: Settings,
    *,
    keep_sessions: bool = False,
    reporter: ConsoleProgressReporter | None = None,
) -> None:
    own_reporter = reporter is None
    reporter = reporter or ConsoleProgressReporter()
    executor = WorkflowExecutor(workflow, settings, progress_callback=reporter.emit)
    reporter.bind_executor(executor)
    try:
        while not executor.all_steps_finished():
            ready = [item["id"] for item in executor.list_steps(only_ready=True)]
            if not ready:
                notifications = executor.drain_notifications()
                if notifications:
                    reporter.emit({"event": "background_notifications", "notifications": notifications})
                    continue
                reporter.emit({"event": "workflow_stalled"})
                raise RuntimeError("No ready steps are available; workflow may be deadlocked")
            step_id = ready[0]
            executor.run_step(step_id)
            time.sleep(0.05)
        final_state = executor.describe_state(include_diagnostics=True)
        print(
            _format_run_summary(
                status=_derive_run_status(final_state),
                summary="Workflow finished",
                state=final_state,
            )
        )
        executor.write_summary_artifact(
            status=_derive_run_status(final_state),
            summary="Workflow finished",
            state=final_state,
        )
    finally:
        if own_reporter:
            reporter.stop()
        if not keep_sessions:
            executor.close_all()


def _run_workflow_with_agent(
    workflow: WorkflowSpec,
    settings: Settings,
    *,
    reporter: ConsoleProgressReporter | None = None,
):
    own_reporter = reporter is None
    reporter = reporter or ConsoleProgressReporter()
    executor = WorkflowExecutor(workflow, settings, progress_callback=reporter.emit)
    reporter.bind_executor(executor)
    try:
        agent = ModelTestAgent(
            settings=settings,
            workflow=workflow,
            executor=executor,
            progress_callback=reporter.emit,
        )
        report = agent.run()
        executor.write_summary_artifact(
            status=report.status,
            summary=report.summary,
            state=report.state,
            iterations=report.iterations,
        )
        return report
    finally:
        if own_reporter:
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
    failure_excerpts = list((run.get("failure_excerpts") or []))
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
    if run.get("event_log_path"):
        lines.append(f"events: {run['event_log_path']}")
    if run.get("summary_path"):
        lines.append(f"summary json: {run['summary_path']}")
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

    if failure_excerpts:
        lines.append("failure excerpts:")
        for item in failure_excerpts[:3]:
            source_kind = item.get("source_kind") or "summary"
            source_path = item.get("source_path")
            source = f" [{source_kind}]"
            if source_path:
                source += f" {source_path}"
            lines.append(f"- {item.get('step_id')}{source}")
            excerpt = str(item.get("excerpt") or "").strip()
            if excerpt:
                lines.extend(textwrap.indent(excerpt, "  ").splitlines())
        if len(failure_excerpts) > 3:
            lines.append(f"... {len(failure_excerpts) - 3} more failure excerpt(s) omitted")

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
