from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings
from .models import WorkflowSpec
from .openai_compat import OpenAICompatClient
from .runtime.executor import WorkflowExecutor


AGENT_SYSTEM_PROMPT = """You are a supervisor agent for Linux AI model execution and benchmarking.

You are given a workflow extracted from Markdown/PDF documentation. Your job is to execute it safely and efficiently.

Operating rules:
- Prefer `run_step` for declared workflow steps.
- Use `get_state`, `list_steps`, and `capture_session` often enough to stay grounded in the real terminal state.
- Use `run_command` and `send_keys` as escape hatches when the workflow needs small corrections or interactive recovery.
- Do not skip failed steps silently.
- When a server or watcher runs in the background, keep moving on other independent work and synchronize with barriers or state checks.
- Before each batch of tool calls, write 1-2 short operator-facing sentences that explain what you are about to do or what you are waiting for.
- Keep that narration high level. Do not reveal private chain-of-thought. Do not dump JSON.
- When you are done, call `complete_run`.
- If the run cannot continue, call `fail_run`.
"""


@dataclass
class AgentRunReport:
    status: str
    summary: str
    iterations: int
    state: dict[str, Any]


class ModelTestAgent:
    def __init__(
        self,
        *,
        settings: Settings,
        workflow: WorkflowSpec,
        executor: WorkflowExecutor | None = None,
        client: OpenAICompatClient | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or OpenAICompatClient(settings.base_url, settings.api_key)
        self.executor = executor or WorkflowExecutor(
            workflow,
            settings,
            llm_client=self.client,
            progress_callback=progress_callback,
        )
        self._final_status: str | None = None
        self._final_summary: str = ""
        self._tools = self._build_tools()
        self._progress_callback = progress_callback

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback({"event": event, **payload})

    def _describe_state(self, *, include_diagnostics: bool = False) -> dict[str, Any]:
        try:
            return self.executor.describe_state(include_diagnostics=include_diagnostics)
        except TypeError:
            return self.executor.describe_state()

    def run(self) -> AgentRunReport:
        self.settings.require_model_access()
        workflow_json = json.dumps(self.executor.workflow.to_dict(), ensure_ascii=False, indent=2)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Execute this workflow.\n\n"
                    f"{workflow_json}\n\n"
                    "Start by inspecting the state and then proceed."
                ),
            },
        ]
        iterations = 0
        stream_enabled = self.settings.stream_agent_output
        while iterations < self.settings.max_iterations:
            iterations += 1
            self._emit_progress(
                "agent_iteration",
                iteration=iterations,
                max_iterations=self.settings.max_iterations,
            )
            notifications = self.executor.drain_notifications()
            if notifications:
                self._emit_progress("agent_notifications", count=len(notifications))
                messages.append(
                    {
                        "role": "user",
                        "content": "Background notifications:\n" + json.dumps(notifications, ensure_ascii=False),
                    }
                )
            stalled_reason = self._stalled_failure_reason()
            if stalled_reason:
                self._final_status = "failed"
                self._final_summary = stalled_reason
                break
            streamed_any_text = False
            stream_started = False

            def on_delta(event: dict[str, Any]) -> None:
                nonlocal streamed_any_text, stream_started
                if event.get("type") == "content_start":
                    self._emit_progress("agent_stream_started")
                    stream_started = True
                elif event.get("type") == "content_delta":
                    text = str(event.get("text", ""))
                    if text:
                        streamed_any_text = True
                        self._emit_progress("agent_stream_delta", text=text)
                elif event.get("type") == "content_end" and stream_started:
                    self._emit_progress("agent_stream_finished")
                    stream_started = False

            try:
                response = self.client.chat(
                    model=self.settings.agent_model,
                    messages=messages,
                    tools=[tool["schema"] for tool in self._tools.values()],
                    stream=stream_enabled,
                    on_delta=on_delta if stream_enabled else None,
                )
            except Exception:
                if stream_started:
                    self._emit_progress("agent_stream_finished")
                if not stream_enabled:
                    raise
                stream_enabled = False
                self._emit_progress(
                    "narration",
                    message=(
                        "Streaming model output is unavailable on this endpoint, so I am falling back "
                        "to buffered assistant responses for the rest of this run."
                    ),
                )
                response = self.client.chat(
                    model=self.settings.agent_model,
                    messages=messages,
                    tools=[tool["schema"] for tool in self._tools.values()],
                )
            message = response.message
            assistant_message: dict[str, Any] = {"role": "assistant"}
            if "content" in message:
                assistant_message["content"] = message.get("content", "")
            note = OpenAICompatClient._message_text(message).strip()
            if note and not streamed_any_text:
                note = " ".join(note.split())
                if len(note) > 320:
                    note = note[:317].rstrip() + "..."
                self._emit_progress("agent_note", content=note)
            if message.get("tool_calls"):
                assistant_message["tool_calls"] = message["tool_calls"]
            messages.append(assistant_message)
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                for call in tool_calls:
                    detail = self._tool_detail(call)
                    self._emit_progress(
                        "agent_tool_call",
                        tool_name=call["function"]["name"],
                        detail=detail,
                    )
                    result = self._invoke_tool(call)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
                if self._final_status:
                    break
                continue
            if self.executor.all_steps_finished():
                self._final_status = self._final_status or "completed"
                self._final_summary = self._final_summary or str(message.get("content", "")).strip() or "Run finished"
                break
        state = self._describe_state(include_diagnostics=True)
        self._final_status, self._final_summary = self._normalize_final_outcome(
            state=state,
            status=self._final_status,
            summary=self._final_summary,
        )
        if not self._final_status:
            self._final_status = "failed"
            self._final_summary = self._final_summary or "Agent loop ended without explicit completion"
        self._emit_progress("agent_finished", status=self._final_status)
        return AgentRunReport(
            status=self._final_status,
            summary=self._final_summary,
            iterations=iterations,
            state=state,
        )

    @staticmethod
    def _tool_detail(tool_call: dict[str, Any]) -> str:
        try:
            args = json.loads(tool_call["function"].get("arguments", "{}"))
        except json.JSONDecodeError:
            return ""
        if "step_id" in args:
            return f"step_id={args['step_id']}"
        if "session_name" in args:
            return f"session={args['session_name']}"
        return ""

    def _build_tools(self) -> dict[str, dict[str, Any]]:
        return {
            "get_state": self._tool(
                "get_state",
                "Return the full workflow state, step statuses, sessions, and background tasks.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _: self.executor.describe_state(),
            ),
            "list_steps": self._tool(
                "list_steps",
                "List workflow steps with optional status filtering and ready-only filtering.",
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "only_ready": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                lambda args: self.executor.list_steps(
                    status=args.get("status"),
                    only_ready=bool(args.get("only_ready", False)),
                ),
            ),
            "run_step": self._tool(
                "run_step",
                "Execute one declared workflow step by id.",
                {
                    "type": "object",
                    "properties": {"step_id": {"type": "string"}},
                    "required": ["step_id"],
                    "additionalProperties": False,
                },
                lambda args: self.executor.run_step(step_id=str(args["step_id"])),
            ),
            "list_sessions": self._tool(
                "list_sessions",
                "List declared sessions and whether they were initialized.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _: self.executor.list_sessions(),
            ),
            "capture_session": self._tool(
                "capture_session",
                "Capture recent output from a session.",
                {
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string"},
                        "lines": {"type": "integer"},
                    },
                    "required": ["session_name"],
                    "additionalProperties": False,
                },
                lambda args: self.executor.capture_session(
                    session_name=str(args["session_name"]),
                    lines=int(args.get("lines", self.settings.default_capture_lines)),
                ),
            ),
            "run_command": self._tool(
                "run_command",
                "Run an ad hoc command in an existing session.",
                {
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string"},
                        "command": {"type": "string"},
                        "timeout_s": {"type": "integer"},
                        "background": {"type": "boolean"},
                        "ready_pattern": {"type": "string"},
                        "capture_lines": {"type": "integer"},
                        "fail_patterns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["session_name", "command"],
                    "additionalProperties": False,
                },
                lambda args: self.executor.run_command(
                    session_name=str(args["session_name"]),
                    command=str(args["command"]),
                    timeout_s=int(args["timeout_s"]) if args.get("timeout_s") is not None else None,
                    background=bool(args.get("background", False)),
                    ready_pattern=args.get("ready_pattern"),
                    capture_lines=int(args["capture_lines"]) if args.get("capture_lines") is not None else None,
                    fail_patterns=[str(item) for item in args.get("fail_patterns", [])],
                ),
            ),
            "wait_for_output": self._tool(
                "wait_for_output",
                "Wait for a pattern in a session output.",
                {
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string"},
                        "pattern": {"type": "string"},
                        "timeout_s": {"type": "integer"},
                        "capture_lines": {"type": "integer"},
                        "fail_patterns": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["session_name", "pattern"],
                    "additionalProperties": False,
                },
                lambda args: self.executor.wait_for_output(
                    session_name=str(args["session_name"]),
                    pattern=str(args["pattern"]),
                    timeout_s=int(args["timeout_s"]) if args.get("timeout_s") is not None else None,
                    capture_lines=int(args["capture_lines"]) if args.get("capture_lines") is not None else None,
                    fail_patterns=[str(item) for item in args.get("fail_patterns", [])],
                ),
            ),
            "send_keys": self._tool(
                "send_keys",
                "Send keystrokes or literal text into an interactive session.",
                {
                    "type": "object",
                    "properties": {
                        "session_name": {"type": "string"},
                        "keys": {"type": "array", "items": {"type": "string"}},
                        "literal": {"type": "boolean"},
                        "press_enter": {"type": "boolean"},
                        "delay_s": {"type": "number"},
                    },
                    "required": ["session_name", "keys"],
                    "additionalProperties": False,
                },
                lambda args: self.executor.send_keys(
                    session_name=str(args["session_name"]),
                    keys=[str(item) for item in args["keys"]],
                    literal=bool(args.get("literal", False)),
                    press_enter=bool(args.get("press_enter", False)),
                    delay_s=float(args.get("delay_s", 0.0)),
                ),
            ),
            "list_background_tasks": self._tool(
                "list_background_tasks",
                "List background watcher tasks.",
                {"type": "object", "properties": {}, "additionalProperties": False},
                lambda _: self.executor.list_background_tasks(),
            ),
            "get_background_task": self._tool(
                "get_background_task",
                "Inspect one background watcher task by id.",
                {
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
                lambda args: self.executor.get_background_task(str(args["task_id"])),
            ),
            "complete_run": self._tool(
                "complete_run",
                "Mark the workflow run as completed.",
                {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                    "additionalProperties": False,
                },
                self._complete_run,
            ),
            "fail_run": self._tool(
                "fail_run",
                "Mark the workflow run as failed.",
                {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
                self._fail_run,
            ),
        }

    @staticmethod
    def _tool(
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        return {
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
            "handler": handler,
        }

    def _invoke_tool(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        function = tool_call["function"]
        name = function["name"]
        if name not in self._tools:
            return {"error": f"Unknown tool: {name}"}
        args = function.get("arguments", "{}")
        if isinstance(args, str):
            parsed_args = json.loads(args or "{}")
        else:
            parsed_args = args
        try:
            result = self._tools[name]["handler"](parsed_args)
        except Exception as exc:
            return {"error": str(exc)}
        if isinstance(result, dict):
            return result
        return {"result": result}

    def _complete_run(self, args: dict[str, Any]) -> dict[str, str]:
        self._final_status = "completed"
        self._final_summary = str(args["summary"])
        return {"status": self._final_status, "summary": self._final_summary}

    def _fail_run(self, args: dict[str, Any]) -> dict[str, str]:
        self._final_status = "failed"
        self._final_summary = str(args["reason"])
        return {"status": self._final_status, "summary": self._final_summary}

    def _stalled_failure_reason(self) -> str | None:
        failed_steps = self.executor.list_steps(status="failed")
        if not failed_steps:
            return None
        ready_steps = self.executor.list_steps(only_ready=True)
        background_steps = self.executor.list_steps(status="background")
        if ready_steps or background_steps:
            return None
        failed_ids = ", ".join(str(item["id"]) for item in failed_steps[:3])
        return f"Workflow cannot continue because step(s) failed: {failed_ids}"

    @staticmethod
    def _normalize_final_outcome(
        *,
        state: dict[str, Any],
        status: str | None,
        summary: str,
    ) -> tuple[str | None, str]:
        steps = state.get("steps", [])
        failed_steps = [item for item in steps if item.get("status") == "failed"]
        unfinished_steps = [
            item for item in steps if item.get("status") not in {"completed", "skipped", "failed"}
        ]
        if failed_steps:
            detail = ", ".join(str(item.get("id")) for item in failed_steps[:3])
            if status == "failed" and summary:
                return "failed", summary
            return "failed", f"Workflow failed at step(s): {detail}"
        if unfinished_steps and status == "completed":
            detail = ", ".join(str(item.get("id")) for item in unfinished_steps[:3])
            return "failed", f"Workflow was marked completed with unfinished step(s): {detail}"
        return status, summary
