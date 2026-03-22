from __future__ import annotations

import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..models import CommandStep, ProbeStep, SendKeysStep, SessionSpec, SessionTransport, WaitStep, WorkflowSpec
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


class SessionManager:
    def __init__(
        self,
        workflow: WorkflowSpec,
        settings: Settings,
        backend: SessionBackend,
        *,
        run_id: str,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.workflow = workflow
        self.settings = settings
        self.backend = backend
        self.run_id = run_id
        self._progress_callback = progress_callback
        self.log_dir = self._create_log_dir()
        self._session_log_names = self._plan_session_log_names()
        self._session_states: dict[str, SessionState] = {}

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

    def close_all(self) -> None:
        for state in self._session_states.values():
            self.backend.kill_session(state.backend_session_name)

    def ensure_session(self, logical_name: str) -> SessionState:
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
                backend_session_name=f"mta-{safe_workflow[:12]}-{safe_session[:12]}-{self.run_id}",
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

    def command_with_session_logging(self, command: str, state: SessionState) -> str:
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
    def clean_command_output(output: str) -> str:
        cleaned_lines: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if re.fullmatch(r"\[\d+\]\s+\d+", stripped):
                continue
            if re.fullmatch(r"\[\d+\][+-]?\s+Done\s+tee -a .+", stripped):
                continue
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    def _emit_progress(self, event: str, **payload: Any) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback({"event": event, **payload})

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
                self.command_with_session_logging(command, state),
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
        cleaned_output = self.clean_command_output(result.output)
        if result.exit_code != 0:
            detail = cleaned_output or f"command={command}"
            raise RuntimeError(f"Session setup failed while {description}: {detail}")

    def _create_log_dir(self) -> Path:
        root = Path(self.settings.log_root).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        safe_workflow = self._safe_name(self.workflow.name, fallback="workflow", max_len=40)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        log_dir = root / f"{timestamp}-{safe_workflow}-{self.run_id}"
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
