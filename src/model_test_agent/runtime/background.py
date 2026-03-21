from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from .session_backend import SessionBackend


@dataclass
class BackgroundTaskRecord:
    task_id: str
    step_id: str | None
    session_name: str
    status: str
    summary: str
    pattern: str | None = None
    fail_patterns: list[str] = field(default_factory=list)
    output: str = ""
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class BackgroundNotification:
    task_id: str
    step_id: str | None
    status: str
    summary: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


class BackgroundTaskManager:
    def __init__(self, backend: SessionBackend, *, default_lines: int = 300) -> None:
        self.backend = backend
        self.default_lines = default_lines
        self._tasks: dict[str, BackgroundTaskRecord] = {}
        self._lock = threading.Lock()
        self._notifications: queue.Queue[BackgroundNotification] = queue.Queue()

    def watch_output(
        self,
        *,
        session_name: str,
        pattern: str,
        timeout_s: int,
        fail_patterns: list[str] | None = None,
        step_id: str | None = None,
        lines: int | None = None,
    ) -> str:
        task_id = f"task-{uuid.uuid4().hex[:10]}"
        record = BackgroundTaskRecord(
            task_id=task_id,
            step_id=step_id,
            session_name=session_name,
            status="running",
            summary=f"Watching session {session_name} for pattern {pattern!r}",
            pattern=pattern,
            fail_patterns=list(fail_patterns or []),
        )
        with self._lock:
            self._tasks[task_id] = record
        thread = threading.Thread(
            target=self._watch_loop,
            kwargs={
                "task_id": task_id,
                "session_name": session_name,
                "pattern": pattern,
                "timeout_s": timeout_s,
                "fail_patterns": list(fail_patterns or []),
                "lines": lines or self.default_lines,
            },
            daemon=True,
        )
        thread.start()
        return task_id

    def _watch_loop(
        self,
        *,
        task_id: str,
        session_name: str,
        pattern: str,
        timeout_s: int,
        fail_patterns: list[str],
        lines: int,
    ) -> None:
        try:
            wait = self.backend.wait_for_pattern(
                session_name,
                pattern,
                timeout_s=timeout_s,
                fail_patterns=fail_patterns,
                lines=lines,
            )
            if wait.status == "matched":
                status = "completed"
                summary = f"Pattern {pattern!r} matched in {session_name}"
            elif wait.status == "failed":
                status = "failed"
                summary = f"Failure pattern {wait.matched_pattern!r} matched in {session_name}"
            else:
                status = "timeout"
                summary = f"Timed out waiting for pattern {pattern!r} in {session_name}"
            self._finish_task(task_id, status=status, summary=summary, output=wait.output)
        except Exception as exc:
            self._finish_task(task_id, status="error", summary=f"Watcher crashed: {exc}", output="")

    def _finish_task(self, task_id: str, *, status: str, summary: str, output: str) -> None:
        with self._lock:
            record = self._tasks[task_id]
            record.status = status
            record.summary = summary
            record.output = output
            record.finished_at = time.time()
            step_id = record.step_id
        self._notifications.put(
            BackgroundNotification(task_id=task_id, step_id=step_id, status=status, summary=summary)
        )

    def list_tasks(self) -> list[BackgroundTaskRecord]:
        with self._lock:
            return [self._copy_task(record) for record in self._tasks.values()]

    def get_task(self, task_id: str) -> BackgroundTaskRecord:
        with self._lock:
            return self._copy_task(self._tasks[task_id])

    def drain_notifications(self) -> list[BackgroundNotification]:
        items: list[BackgroundNotification] = []
        while True:
            try:
                items.append(self._notifications.get_nowait())
            except queue.Empty:
                return items

    @staticmethod
    def _copy_task(record: BackgroundTaskRecord) -> BackgroundTaskRecord:
        return BackgroundTaskRecord(**record.to_dict())
