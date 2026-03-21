from __future__ import annotations

import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Protocol

from .session_backend import (
    CommandResult,
    SessionBackend,
    WaitResult,
    extract_segment,
    make_command_markers,
    wrap_command_with_markers,
)


class CommandRunner(Protocol):
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        ...


class SubprocessRunner:
    def run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, check=True, capture_output=True, text=True)


TmuxCommandResult = CommandResult


class TmuxClient(SessionBackend):
    backend_name = "tmux"

    def __init__(
        self,
        *,
        tmux_bin: str = "tmux",
        runner: CommandRunner | None = None,
        poll_interval_s: float = 1.0,
    ) -> None:
        self.tmux_bin = tmux_bin
        self.runner = runner or SubprocessRunner()
        self.poll_interval_s = poll_interval_s

    def session_exists(self, session_name: str) -> bool:
        result = subprocess.run(
            [self.tmux_bin, "has-session", "-t", session_name],
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def create_session(self, session_name: str, shell: str = "/bin/bash") -> None:
        self.runner.run([self.tmux_bin, "new-session", "-d", "-s", session_name, shell])

    def kill_session(self, session_name: str) -> None:
        subprocess.run(
            [self.tmux_bin, "kill-session", "-t", session_name],
            check=False,
            capture_output=True,
            text=True,
        )

    def send_literal(self, session_name: str, text: str, enter: bool = True) -> None:
        self.runner.run([self.tmux_bin, "send-keys", "-t", session_name, "-l", text])
        if enter:
            self.runner.run([self.tmux_bin, "send-keys", "-t", session_name, "Enter"])

    def send_keys(self, session_name: str, keys: list[str], press_enter: bool = False) -> None:
        for key in keys:
            self.runner.run([self.tmux_bin, "send-keys", "-t", session_name, key])
        if press_enter:
            self.runner.run([self.tmux_bin, "send-keys", "-t", session_name, "Enter"])

    def capture_pane(self, session_name: str, lines: int = 300) -> str:
        result = self.runner.run(
            [self.tmux_bin, "capture-pane", "-p", "-t", session_name, "-S", f"-{lines}"]
        )
        return result.stdout

    def attach_combined_log(self, session_name: str, log_path: str) -> None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).touch(exist_ok=True)
        self.runner.run(
            [self.tmux_bin, "pipe-pane", "-o", "-t", session_name, f"cat >> {shlex.quote(log_path)}"]
        )

    def wait_for_pattern(
        self,
        session_name: str,
        pattern: str,
        *,
        timeout_s: int,
        fail_patterns: list[str] | None = None,
        lines: int = 300,
    ) -> WaitResult:
        ok_re = re.compile(pattern, re.MULTILINE)
        fail_res = [re.compile(item, re.MULTILINE) for item in fail_patterns or []]
        deadline = time.time() + timeout_s
        latest = ""
        while time.time() < deadline:
            latest = self.capture_pane(session_name, lines=lines)
            for fail_re in fail_res:
                failed = fail_re.search(latest)
                if failed:
                    return WaitResult(
                        status="failed",
                        output=latest,
                        matched_pattern=fail_re.pattern,
                        match_groups=failed.groups(),
                    )
            matched = ok_re.search(latest)
            if matched:
                return WaitResult(
                    status="matched",
                    output=latest,
                    matched_pattern=ok_re.pattern,
                    match_groups=matched.groups(),
                )
            time.sleep(self.poll_interval_s)
        return WaitResult(status="timeout", output=latest)

    def run_command(
        self,
        session_name: str,
        command: str,
        *,
        timeout_s: int,
        lines: int = 300,
    ) -> TmuxCommandResult:
        token = uuid.uuid4().hex[:10]
        start_token, done_token = make_command_markers(token)
        wrapped = wrap_command_with_markers(command, start_token, done_token)
        self.send_literal(session_name, wrapped)
        wait = self.wait_for_pattern(
            session_name,
            re.escape(done_token) + r" (\d+)",
            timeout_s=timeout_s,
            lines=lines,
        )
        if wait.status != "matched":
            raise TimeoutError(f"Timed out waiting for command in session {session_name}")
        output, exit_code = extract_segment(wait.output, start_token, done_token)
        return TmuxCommandResult(exit_code=exit_code, output=output)

    @staticmethod
    def build_export_commands(env: dict[str, str]) -> list[str]:
        return [f"export {key}={shlex.quote(value)}" for key, value in env.items()]

    @staticmethod
    def _extract_segment(output: str, start_token: str, done_token: str) -> tuple[str, int]:
        return extract_segment(output, start_token, done_token)
