from __future__ import annotations

import errno
import os
import pty
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .session_backend import (
    CommandResult,
    SessionBackend,
    WaitResult,
    extract_segment,
    make_command_markers,
    wrap_command_with_markers,
)


KEY_ALIASES = {
    "Enter": "\n",
    "Return": "\n",
    "Escape": "\x1b",
    "Esc": "\x1b",
    "Tab": "\t",
    "Backspace": "\x7f",
    "Space": " ",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Home": "\x1b[H",
    "End": "\x1b[F",
}


@dataclass
class PtySession:
    name: str
    process: subprocess.Popen[bytes]
    master_fd: int
    storage_log_path: str
    buffer: str = ""
    buffer_start_chars: int = 0
    total_output_chars: int = 0
    storage_log_bytes: int = 0
    closed: bool = False
    combined_log_path: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(init=False)

    def __post_init__(self) -> None:
        self.condition = threading.Condition(self.lock)


class PtyClient(SessionBackend):
    backend_name = "pty"

    def __init__(self, *, poll_interval_s: float = 1.0, buffer_max_chars: int = 200000) -> None:
        self.poll_interval_s = poll_interval_s
        self.buffer_max_chars = buffer_max_chars
        self._sessions: dict[str, PtySession] = {}

    def session_exists(self, session_name: str) -> bool:
        session = self._sessions.get(session_name)
        return bool(session and not session.closed and session.process.poll() is None)

    def create_session(self, session_name: str, shell: str = "/bin/bash") -> None:
        if self.session_exists(session_name):
            return
        if session_name in self._sessions:
            self.kill_session(session_name)
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("TERM", env.get("TERM", "xterm-256color"))
        env["MTA_SESSION_NAME"] = session_name
        process = subprocess.Popen(
            shlex.split(shell),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            close_fds=True,
            start_new_session=True,
            bufsize=0,
        )
        os.close(slave_fd)
        storage_fd, storage_log_path = tempfile.mkstemp(prefix=f"mta-pty-{session_name}-", suffix=".log")
        os.close(storage_fd)
        session = PtySession(
            name=session_name,
            process=process,
            master_fd=master_fd,
            storage_log_path=storage_log_path,
        )
        self._sessions[session_name] = session
        thread = threading.Thread(target=self._reader_loop, args=(session,), daemon=True)
        thread.start()

    def kill_session(self, session_name: str) -> None:
        session = self._sessions.pop(session_name, None)
        if not session:
            return
        try:
            if session.process.poll() is None:
                os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
                try:
                    session.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(session.process.pid), signal.SIGKILL)
                    session.process.wait(timeout=2)
        except ProcessLookupError:
            pass
        finally:
            session.closed = True
            try:
                os.close(session.master_fd)
            except OSError:
                pass
            try:
                os.unlink(session.storage_log_path)
            except OSError:
                pass
            with session.condition:
                session.condition.notify_all()

    def send_literal(self, session_name: str, text: str, enter: bool = True) -> None:
        payload = text.encode("utf-8")
        if enter:
            payload += b"\n"
        self._write(session_name, payload)

    def send_keys(self, session_name: str, keys: list[str], press_enter: bool = False) -> None:
        chunks = [self._encode_key(key) for key in keys]
        if press_enter:
            chunks.append(b"\n")
        self._write(session_name, b"".join(chunks))

    def capture_pane(self, session_name: str, lines: int = 300) -> str:
        session = self._get_session(session_name)
        with session.lock:
            text = session.buffer
        return self._tail_lines(text, lines)

    def attach_combined_log(self, session_name: str, log_path: str) -> None:
        session = self._get_session(session_name)
        target = Path(log_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with session.lock:
            Path(log_path).write_text("")
            try:
                existing = Path(session.storage_log_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                existing = ""
            if existing:
                target.write_text(existing, encoding="utf-8")
            session.combined_log_path = str(target)

    def wait_for_pattern(
        self,
        session_name: str,
        pattern: str,
        *,
        timeout_s: int,
        fail_patterns: list[str] | None = None,
        lines: int = 300,
    ) -> WaitResult:
        session = self._get_session(session_name)
        return self._wait_for_pattern_stream(
            session=session,
            pattern=pattern,
            timeout_s=timeout_s,
            fail_patterns=fail_patterns,
            lines=lines,
        )

    def run_command(
        self,
        session_name: str,
        command: str,
        *,
        timeout_s: int,
        lines: int = 300,
    ) -> CommandResult:
        session = self._get_session(session_name)
        with session.lock:
            start_offset = session.storage_log_bytes
        token = uuid.uuid4().hex[:10]
        start_token, done_token = make_command_markers(token)
        wrapped = wrap_command_with_markers(command, start_token, done_token)
        self.send_literal(session_name, wrapped)
        wait = self._wait_for_pattern_stream(
            session=session,
            pattern=re.escape(done_token) + r" (\d+)",
            timeout_s=timeout_s,
            lines=lines,
            start_offset=start_offset,
            return_full_output=True,
        )
        if wait.status != "matched":
            raise TimeoutError(f"Timed out waiting for command in session {session_name}")
        output, exit_code = extract_segment(wait.output, start_token, done_token)
        return CommandResult(exit_code=exit_code, output=output)

    @staticmethod
    def build_export_commands(env: dict[str, str]) -> list[str]:
        return [f"export {key}={shlex.quote(value)}" for key, value in env.items()]

    def _get_session(self, session_name: str) -> PtySession:
        session = self._sessions.get(session_name)
        if not session:
            raise KeyError(f"Unknown PTY session: {session_name}")
        return session

    def _write(self, session_name: str, payload: bytes) -> None:
        session = self._get_session(session_name)
        total = 0
        while total < len(payload):
            written = os.write(session.master_fd, payload[total:])
            total += written

    def _reader_loop(self, session: PtySession) -> None:
        while True:
            try:
                chunk = os.read(session.master_fd, 4096)
            except OSError as exc:
                if exc.errno in {errno.EIO, errno.EBADF}:
                    break
                text = f"\n[MTA PTY reader error: {exc}]\n"
                self._append_log_chunk(session.storage_log_path, text)
                with session.condition:
                    session.storage_log_bytes += len(text.encode("utf-8"))
                    session.total_output_chars += len(text)
                    session.buffer = self._trim_buffer(session.buffer + text)
                    session.buffer_start_chars = session.total_output_chars - len(session.buffer)
                    session.condition.notify_all()
                self._append_log_chunk(session.combined_log_path, text)
                break
            if not chunk:
                break
            text = self._normalize_output(chunk)
            self._append_log_chunk(session.storage_log_path, text)
            self._append_log_chunk(session.combined_log_path, text)
            with session.condition:
                session.storage_log_bytes += len(text.encode("utf-8"))
                session.total_output_chars += len(text)
                session.buffer = self._trim_buffer(session.buffer + text)
                session.buffer_start_chars = session.total_output_chars - len(session.buffer)
                session.condition.notify_all()
        session.closed = True
        with session.condition:
            session.condition.notify_all()

    def _wait_for_pattern_stream(
        self,
        *,
        session: PtySession,
        pattern: str,
        timeout_s: int,
        fail_patterns: list[str] | None = None,
        lines: int = 300,
        start_offset: int | None = None,
        return_full_output: bool = False,
    ) -> WaitResult:
        ok_re = re.compile(pattern, re.MULTILINE)
        fail_res = [re.compile(item, re.MULTILINE) for item in fail_patterns or []]
        carryover_limit = max(4096, len(pattern) * 8, *(len(item) * 8 for item in (fail_patterns or [])))
        deadline = time.time() + timeout_s
        last_view = ""
        scan_tail = ""
        read_offset = start_offset
        with session.condition:
            if read_offset is None:
                last_view = self._tail_lines(session.buffer, lines)
                for fail_re in fail_res:
                    failed = fail_re.search(last_view)
                    if failed:
                        return WaitResult(
                            status="failed",
                            output=last_view,
                            matched_pattern=fail_re.pattern,
                            match_groups=failed.groups(),
                        )
                matched = ok_re.search(last_view)
                if matched:
                    return WaitResult(
                        status="matched",
                        output=last_view,
                        matched_pattern=ok_re.pattern,
                        match_groups=matched.groups(),
                    )
                read_offset = session.storage_log_bytes
            while True:
                chunk, read_offset = self._read_storage_log_since(session.storage_log_path, read_offset)
                if chunk:
                    scan_window = scan_tail + chunk
                    for fail_re in fail_res:
                        failed = fail_re.search(scan_window)
                        if failed:
                            output = self._full_or_tail_output(
                                session.storage_log_path,
                                start_offset,
                                read_offset,
                                lines=lines,
                                return_full_output=return_full_output,
                            )
                            return WaitResult(
                                status="failed",
                                output=output,
                                matched_pattern=fail_re.pattern,
                                match_groups=failed.groups(),
                            )
                    matched = ok_re.search(scan_window)
                    if matched:
                        output = self._full_or_tail_output(
                            session.storage_log_path,
                            start_offset,
                            read_offset,
                            lines=lines,
                            return_full_output=return_full_output,
                        )
                        return WaitResult(
                            status="matched",
                            output=output,
                            matched_pattern=ok_re.pattern,
                            match_groups=matched.groups(),
                        )
                    scan_tail = scan_window[-carryover_limit:]
                    last_view = self._full_or_tail_output(
                        session.storage_log_path,
                        start_offset,
                        read_offset,
                        lines=lines,
                        return_full_output=False,
                    )
                remaining = deadline - time.time()
                if remaining <= 0:
                    return WaitResult(status="timeout", output=last_view)
                session.condition.wait(timeout=min(self.poll_interval_s, remaining))

    @staticmethod
    def _read_storage_log_since(path: str, offset: int) -> tuple[str, int]:
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                data = handle.read()
                return data.decode("utf-8", errors="replace"), offset + len(data)
        except OSError:
            return "", offset

    def _full_or_tail_output(
        self,
        storage_log_path: str,
        start_offset: int | None,
        end_offset: int,
        *,
        lines: int,
        return_full_output: bool,
    ) -> str:
        if start_offset is None:
            return self._read_log_tail_lines(storage_log_path, end_offset=end_offset, lines=lines)
        text = self._read_log_slice(storage_log_path, start_offset, end_offset)
        if return_full_output:
            return text
        return self._tail_lines(text, lines)

    @staticmethod
    def _read_log_slice(path: str, start_offset: int, end_offset: int) -> str:
        try:
            with open(path, "rb") as handle:
                handle.seek(start_offset)
                data = handle.read(max(0, end_offset - start_offset))
                return data.decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _read_log_tail_lines(self, path: str, *, end_offset: int, lines: int) -> str:
        text = self._read_log_slice(path, 0, end_offset)
        return self._tail_lines(text, lines)

    def _trim_buffer(self, text: str) -> str:
        if len(text) <= self.buffer_max_chars:
            return text
        return text[-self.buffer_max_chars :]

    @staticmethod
    def _normalize_output(chunk: bytes) -> str:
        return chunk.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _append_log_chunk(log_path: str | None, text: str) -> None:
        if not log_path or not text:
            return
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            return

    @staticmethod
    def _tail_lines(text: str, lines: int) -> str:
        if lines <= 0:
            return text
        parts = text.splitlines()
        if not parts:
            return ""
        return "\n".join(parts[-lines:])

    @staticmethod
    def _encode_key(key: str) -> bytes:
        alias = KEY_ALIASES.get(key)
        if alias is not None:
            return alias.encode("utf-8")
        match = re.match(r"^(?:C|Ctrl)-(.+)$", key, re.IGNORECASE)
        if match:
            value = match.group(1)
            if len(value) == 1:
                return bytes([ord(value.upper()) & 0x1F])
        return key.encode("utf-8")
