from __future__ import annotations

import json
import re
from typing import Any, Callable

from .config import Settings
from .document_loader import DocumentContent
from .models import WorkflowSpec
from .openai_compat import OpenAICompatClient
from .workflow_validation import WorkflowValidationError, build_and_validate_workflow, validate_workflow_spec
from .workflow_enricher import WorkflowEnricher
from .workflow_normalizer import WorkflowNormalizer


PLANNER_SYSTEM_PROMPT = """You are a workflow planner for Linux-based AI model execution and benchmarking.

Convert a Markdown or PDF runbook into a workflow JSON object with this shape:

{
  "name": "short-workflow-name",
  "objective": "one-sentence objective",
  "description": "short summary",
  "sessions": {
    "server": {
      "transport": "local|ssh|docker_exec|docker_run",
      "shell": "/bin/bash",
      "workdir": "/path",
      "env": {"KEY": "VALUE"},
      "connect_ready_pattern": "[$#] ",
      "startup_commands": ["cd /repo"],
      "ssh_host": "host",
      "ssh_user": "ubuntu",
      "ssh_port": 22,
      "docker_container": "name",
      "docker_image": "image",
      "docker_run_args": ["--gpus", "all"]
    }
  },
  "steps": [
    {
      "id": "launch_server",
      "kind": "command",
      "title": "Launch server",
      "session": "server",
      "depends_on": [],
      "command": "python -m server",
      "background": true,
      "ready_pattern": "listening on",
      "fail_patterns": ["Traceback", "CUDA out of memory"],
      "timeout_s": 600
    },
    {
      "id": "wait_for_server",
      "kind": "barrier",
      "title": "Wait for server",
      "wait_for": ["launch_server"],
      "timeout_s": 600
    }
  ]
}

Rules:
- Preserve shell commands as faithfully as possible.
- Use one persistent session per long-lived shell, SSH login, Docker TTY, or tmux-like workspace.
- **CRITICAL**: When a document describes "creating a container" followed by "running commands inside the container", all those commands MUST use the SAME session as the docker_run/docker_exec step. Do NOT create separate local sessions for commands that should run inside the container.
- For docker_run with -it and a shell (like /bin/bash), the container provides a persistent session; subsequent commands described as "inside the container" must use that same session.
- For long-running processes like model servers, use `kind=command`, `background=true`, plus `ready_pattern`.
- Use `kind=wait` when the document explicitly says to wait for a log line.
- If the document omits common operator knowledge, infer it. Add readiness waits, probes, and cleanup steps when later steps depend on a server or background process.
- Extract host, port, and URL details from commands when the prose does not repeat them.
- If a server launch is followed by `curl`, benchmarking, or other client traffic, ensure the client step waits for real readiness before running.
- If the document omits shutdown instructions for a long-lived foreground server, add a cleanup step such as `Ctrl-C` in the server session.
- Use `kind=barrier` to synchronize multiple sessions before a dependent step starts.
- Use `kind=probe` when readiness should be checked by retrying a command such as `curl` or a TCP connection test.
- Use `kind=send_keys` for interactive actions in editors, prompts, or nested shells.
- Use `kind=decision` only when the next step depends on terminal output, test outcome, or an exception branch.
- Keep dependencies explicit with `depends_on`.
- Output valid JSON only. Do not wrap it in Markdown fences.
"""

PLANNER_NARRATION_SYSTEM_PROMPT = """You are briefly narrating how you will convert a Linux CLI runbook into an executable workflow.

Rules:
- Speak to the operator in first person.
- Use 2-4 short sentences total.
- Mention the main phases you see in the document and the next thing you will do.
- Do not reveal private chain-of-thought.
- Do not output JSON or Markdown bullets unless they fit naturally in one sentence.
"""


class WorkflowPlanner:
    def __init__(
        self,
        settings: Settings,
        client: OpenAICompatClient | None = None,
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or OpenAICompatClient(settings.base_url, settings.api_key)
        self.enricher = WorkflowEnricher()
        self.normalizer = WorkflowNormalizer()
        self._progress_callback = progress_callback

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback({"event": event, **payload})

    def plan(
        self,
        document: DocumentContent,
        *,
        objective_hint: str = "",
        extra_instructions: str = "",
    ) -> WorkflowSpec:
        self.settings.require_model_access()
        body = (
            f"Source path: {document.path}\n"
            f"Media type: {document.media_type}\n"
            f"Objective hint: {objective_hint or '(none)'}\n"
            f"Extra instructions: {extra_instructions or '(none)'}\n\n"
            "Document body follows.\n"
            "If the document is ambiguous, choose a safe, reviewable workflow and preserve the uncertainty in the step titles or description.\n\n"
            f"{document.text[:50000]}"
        )
        analysis = self.analyze_document(document)
        if analysis:
            self._emit_progress("document_analysis", **analysis)
        self._emit_progress("planning_model_call", model=self.settings.planner_model)
        self._stream_planning_note(
            document=document,
            objective_hint=objective_hint,
            extra_instructions=extra_instructions,
            analysis=analysis,
        )
        last_error: Exception | None = None
        retry_hint = ""
        for attempt in range(1, self.settings.planner_max_attempts + 1):
            payload = self.client.complete_json(
                model=self.settings.planner_model,
                system_prompt=PLANNER_SYSTEM_PROMPT,
                user_prompt=body + retry_hint,
            )
            try:
                workflow = build_and_validate_workflow(payload)
                self._annotate_planned_workflow(
                    workflow,
                    document=document,
                    analysis=analysis,
                    objective_hint=objective_hint,
                    extra_instructions=extra_instructions,
                    attempt=attempt,
                )
                workflow = self.normalizer.normalize(workflow, document.path)
                workflow = self.enricher.enrich(workflow)
                validate_workflow_spec(workflow)
                return workflow
            except (TypeError, ValueError, KeyError, WorkflowValidationError) as exc:
                last_error = exc
                if attempt >= self.settings.planner_max_attempts:
                    break
                retry_hint = (
                    "\n\nPlanner retry instructions:\n"
                    f"The previous JSON was invalid: {exc}\n"
                    "Return a corrected workflow JSON that satisfies the declared schema. "
                    "Do not omit required fields. Keep session references and step dependencies consistent."
                )
                self._emit_progress(
                    "planning_retry",
                    attempt=attempt + 1,
                    max_attempts=self.settings.planner_max_attempts,
                    reason=str(exc),
                )
        raise RuntimeError(f"Planner could not produce a valid workflow after {self.settings.planner_max_attempts} attempt(s): {last_error}")

    def _annotate_planned_workflow(
        self,
        workflow: WorkflowSpec,
        *,
        document: DocumentContent,
        analysis: dict[str, Any],
        objective_hint: str,
        extra_instructions: str,
        attempt: int,
    ) -> None:
        planning_meta = workflow.metadata.setdefault("planning", {})
        planning_meta.update(
            {
                "origin": "planner",
                "planner_model": self.settings.planner_model,
                "attempt": attempt,
                "document_path": str(document.path),
                "document_media_type": document.media_type,
                "objective_hint": objective_hint,
                "extra_instructions": extra_instructions,
                "analysis": analysis,
                "explanation": (
                    "This workflow skeleton was produced by the planning model from the source runbook. "
                    "Later deterministic passes may normalize paths and add inferred waits or cleanup."
                ),
            }
        )
        for session in workflow.sessions.values():
            self._set_initial_provenance(
                session.metadata,
                origin="planner",
                reason="Session was planned directly from the source runbook.",
            )
        for step in workflow.steps:
            self._set_initial_provenance(
                step.metadata,
                origin="planner",
                reason="Step was planned directly from the source runbook.",
            )

    @staticmethod
    def dump(workflow: WorkflowSpec) -> str:
        return json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False)

    def analyze_document(self, document: DocumentContent) -> dict[str, Any]:
        text = document.text or ""
        lines = text.splitlines()
        headings = [
            line.lstrip("#").strip()
            for line in lines
            if line.lstrip().startswith("#") and line.lstrip("#").strip()
        ][:6]
        commands = self._extract_command_lines(text)
        phases: list[str] = []
        for item in headings + commands:
            phase = self._phase_from_text(item)
            if phase and phase not in phases:
                phases.append(phase)
        if not phases:
            phases.append("extract the command flow and map it into sessions, waits, and cleanup")
        return {
            "heading_count": len(headings),
            "command_count": len(commands),
            "phases": phases[:5],
            "headings": headings[:4],
            "command_samples": commands[:4],
        }

    @staticmethod
    def _extract_command_lines(text: str) -> list[str]:
        commands: list[str] = []
        in_fence = False
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if not stripped:
                continue
            if in_fence:
                if not stripped.startswith("#"):
                    commands.append(stripped)
                continue
            if re.match(r"^\s*(?:\$|>\s|\d+\.\s+|[-*]\s+)", line):
                candidate = re.sub(r"^\s*(?:\$|>\s|\d+\.\s+|[-*]\s+)", "", line).strip()
                if candidate and any(ch.isalpha() for ch in candidate):
                    commands.append(candidate)
        return commands[:12]

    @staticmethod
    def _phase_from_text(text: str) -> str | None:
        lowered = text.lower()
        if any(token in lowered for token in ("ssh", "docker exec", "docker run -it", "vim", "tmux", "screen")):
            return "enter remote or interactive terminal environments"
        if any(
            token in lowered
            for token in ("launch", "start", "server", "serve", "uvicorn", "gunicorn", "http.server", "vllm")
        ):
            return "bring up the service or model server"
        if any(token in lowered for token in ("wait", "ready", "health", "probe", "curl", "wget", "/healthz")):
            return "wait for readiness and verify the service is responding"
        if any(token in lowered for token in ("benchmark", "client", "throughput", "latency", "req/s")):
            return "run client-side checks or performance measurements"
        if any(token in lowered for token in ("cleanup", "stop", "pkill", "killall", "ctrl-c", "c-c")):
            return "clean up the long-running processes and sessions"
        return None

    def _stream_planning_note(
        self,
        *,
        document: DocumentContent,
        objective_hint: str,
        extra_instructions: str,
        analysis: dict[str, Any],
    ) -> None:
        if not self.settings.stream_agent_output:
            return
        chat = getattr(self.client, "chat", None)
        if not callable(chat):
            return
        summary = json.dumps(analysis, ensure_ascii=False)
        excerpt = document.text[:8000]
        stream_open = False

        def on_delta(event: dict[str, Any]) -> None:
            nonlocal stream_open
            kind = event.get("type")
            if kind == "content_start":
                self._emit_progress("planner_stream_started")
                stream_open = True
            elif kind == "content_delta":
                text = str(event.get("text", ""))
                if text:
                    self._emit_progress("planner_stream_delta", text=text)
            elif kind == "content_end" and stream_open:
                self._emit_progress("planner_stream_finished")
                stream_open = False

        try:
            chat(
                model=self.settings.planner_model,
                messages=[
                    {"role": "system", "content": PLANNER_NARRATION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Objective hint: {objective_hint or '(none)'}\n"
                            f"Extra instructions: {extra_instructions or '(none)'}\n"
                            f"Document analysis: {summary}\n\n"
                            f"Document excerpt:\n{excerpt}"
                        ),
                    },
                ],
                stream=True,
                on_delta=on_delta,
                temperature=0.1,
                max_tokens=240,
            )
        except Exception:
            if stream_open:
                self._emit_progress("planner_stream_finished")
            self._emit_progress(
                "narration",
                message=(
                    "Streaming planner narration is unavailable on this endpoint, so I will continue "
                    "with buffered planning while keeping the terminal updated from local analysis."
                ),
            )

    @staticmethod
    def _set_initial_provenance(metadata: dict[str, Any], *, origin: str, reason: str) -> None:
        metadata.setdefault("provenance", {"origin": origin, "reason": reason})
