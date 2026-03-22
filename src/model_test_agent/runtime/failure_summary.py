from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ERROR_PATTERNS = (
    re.compile(r"traceback", re.IGNORECASE),
    re.compile(r"\bexception\b", re.IGNORECASE),
    re.compile(r"\berror\b", re.IGNORECASE),
    re.compile(r"\bfailed\b", re.IGNORECASE),
    re.compile(r"timed out", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"refused", re.IGNORECASE),
    re.compile(r"not found", re.IGNORECASE),
    re.compile(r"no such", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"address already in use", re.IGNORECASE),
    re.compile(r"cannot ", re.IGNORECASE),
)


@dataclass(frozen=True)
class FailureExcerpt:
    step_id: str
    title: str
    session_name: str | None
    summary: str
    excerpt: str
    source_kind: str
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "session_name": self.session_name,
            "summary": self.summary,
            "excerpt": self.excerpt,
            "source_kind": self.source_kind,
            "source_path": self.source_path,
        }


class FailureSummaryBuilder:
    def __init__(self, *, max_lines: int = 8, max_chars: int = 1200) -> None:
        self.max_lines = max_lines
        self.max_chars = max_chars

    def collect(
        self,
        *,
        steps: list[dict[str, Any]],
        sessions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        session_map = {str(item.get("name")): item for item in sessions if item.get("name")}
        rows: list[dict[str, Any]] = []
        for step in steps:
            if step.get("status") != "failed":
                continue
            excerpt = self._build_failure_excerpt(step=step, session_map=session_map)
            rows.append(excerpt.to_dict())
        return rows

    def _build_failure_excerpt(
        self,
        *,
        step: dict[str, Any],
        session_map: dict[str, dict[str, Any]],
    ) -> FailureExcerpt:
        result = step.get("result") or {}
        summary = str(result.get("summary") or "failed")
        step_id = str(step.get("id") or "")
        title = str(step.get("title") or step_id)
        session_name = str(step.get("session")) if step.get("session") else None

        step_output = self._best_excerpt(str(result.get("output") or ""))
        if step_output:
            return FailureExcerpt(
                step_id=step_id,
                title=title,
                session_name=session_name,
                summary=summary,
                excerpt=step_output,
                source_kind="step_output",
            )

        session = session_map.get(session_name or "")
        if session:
            for key, source_kind in (
                ("stderr_log_path", "stderr"),
                ("stdout_log_path", "stdout"),
                ("combined_log_path", "session"),
            ):
                source_path = str(session.get(key) or "").strip()
                if not source_path:
                    continue
                excerpt = self._excerpt_from_path(Path(source_path))
                if excerpt:
                    return FailureExcerpt(
                        step_id=step_id,
                        title=title,
                        session_name=session_name,
                        summary=summary,
                        excerpt=excerpt,
                        source_kind=source_kind,
                        source_path=source_path,
                    )

        return FailureExcerpt(
            step_id=step_id,
            title=title,
            session_name=session_name,
            summary=summary,
            excerpt=summary,
            source_kind="summary",
        )

    def _excerpt_from_path(self, path: Path) -> str:
        if not path.exists() or not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return self._best_excerpt(text)

    def _best_excerpt(self, text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if not lines:
            return ""
        match_index: int | None = None
        for index, line in enumerate(lines):
            if any(pattern.search(line) for pattern in ERROR_PATTERNS):
                match_index = index
        if match_index is None:
            excerpt_lines = lines[-self.max_lines :]
        else:
            start = max(0, match_index - 3)
            end = min(len(lines), max(match_index + 4, start + self.max_lines))
            excerpt_lines = lines[start:end]
        excerpt = "\n".join(excerpt_lines).strip()
        if len(excerpt) > self.max_chars:
            excerpt = "..." + excerpt[-(self.max_chars - 3) :]
        return excerpt
