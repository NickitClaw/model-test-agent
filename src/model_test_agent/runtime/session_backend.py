from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


@dataclass
class WaitResult:
    status: str
    output: str
    matched_pattern: str | None = None
    match_groups: tuple[str, ...] = ()


@dataclass
class CommandResult:
    exit_code: int
    output: str


class SessionBackend(Protocol):
    backend_name: str

    def session_exists(self, session_name: str) -> bool:
        ...

    def create_session(self, session_name: str, shell: str = "/bin/bash") -> None:
        ...

    def kill_session(self, session_name: str) -> None:
        ...

    def send_literal(self, session_name: str, text: str, enter: bool = True) -> None:
        ...

    def send_keys(self, session_name: str, keys: list[str], press_enter: bool = False) -> None:
        ...

    def capture_pane(self, session_name: str, lines: int = 300) -> str:
        ...

    def attach_combined_log(self, session_name: str, log_path: str) -> None:
        ...

    def wait_for_pattern(
        self,
        session_name: str,
        pattern: str,
        *,
        timeout_s: int,
        fail_patterns: list[str] | None = None,
        lines: int = 300,
    ) -> WaitResult:
        ...

    def run_command(
        self,
        session_name: str,
        command: str,
        *,
        timeout_s: int,
        lines: int = 300,
    ) -> CommandResult:
        ...

    @staticmethod
    def build_export_commands(env: dict[str, str]) -> list[str]:
        ...


def make_command_markers(token: str) -> tuple[str, str]:
    return f"__MTA_START_{token}__", f"__MTA_DONE_{token}__"


def wrap_command_with_markers(command: str, start_token: str, done_token: str) -> str:
    return (
        f'printf "\\n{start_token}\\n"; '
        f"{command}; "
        f'__mta_status=$?; printf "\\n{done_token} %s\\n" "$__mta_status"'
    )


def extract_segment(output: str, start_token: str, done_token: str) -> tuple[str, int]:
    start_idx = output.rfind(start_token)
    if start_idx == -1:
        return output.strip(), 1
    tail = output[start_idx + len(start_token) :]
    match = re.search(re.escape(done_token) + r" (\d+)", tail)
    if not match:
        return tail.strip(), 1
    segment = tail[: match.start()].strip()
    return segment, int(match.group(1))
