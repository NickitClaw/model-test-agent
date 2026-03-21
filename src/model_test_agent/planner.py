from __future__ import annotations

import json

from .config import Settings
from .document_loader import DocumentContent
from .models import WorkflowSpec
from .openai_compat import OpenAICompatClient
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


class WorkflowPlanner:
    def __init__(self, settings: Settings, client: OpenAICompatClient | None = None) -> None:
        self.settings = settings
        self.client = client or OpenAICompatClient(settings.base_url, settings.api_key)
        self.enricher = WorkflowEnricher()
        self.normalizer = WorkflowNormalizer()

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
        payload = self.client.complete_json(
            model=self.settings.planner_model,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=body,
        )
        workflow = WorkflowSpec.from_dict(payload)
        workflow = self.normalizer.normalize(workflow, document.path)
        return self.enricher.enrich(workflow)

    @staticmethod
    def dump(workflow: WorkflowSpec) -> str:
        return json.dumps(workflow.to_dict(), indent=2, ensure_ascii=False)
