from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class StructuredRunLogger:
    def __init__(self, *, log_dir: Path, run_id: str, workflow_name: str) -> None:
        self.log_dir = log_dir
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.events_path = self.log_dir / "events.jsonl"
        self.summary_path = self.log_dir / "summary.json"
        self.events_path.touch(exist_ok=True)

    def log_event(self, event: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": time.time(),
            "run_id": self.run_id,
            "workflow": self.workflow_name,
            "event": event,
            "payload": payload,
        }
        self._append_jsonl(self.events_path, record)

    def write_summary(
        self,
        *,
        status: str,
        summary: str,
        state: dict[str, Any],
        iterations: int | None = None,
        failure_excerpts: list[dict[str, Any]] | None = None,
    ) -> None:
        payload = {
            "ts": time.time(),
            "run_id": self.run_id,
            "workflow": self.workflow_name,
            "status": status,
            "summary": summary,
            "iterations": iterations,
            "state": state,
            "failure_excerpts": failure_excerpts or [],
        }
        self.summary_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
            handle.write("\n")
