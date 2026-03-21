from __future__ import annotations

import shlex
from pathlib import Path

from .models import CommandStep, ProbeStep, SessionTransport, WorkflowSpec


PLACEHOLDER_DIRS = {"/workspace", "/app", "/project", "/workdir"}
SCRIPT_LAUNCHERS = {"python", "python3", "python3.9", "bash", "sh", "zsh", "fish", "source"}
PATH_SUFFIXES = {".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".txt", ".md"}


class WorkflowNormalizer:
    def normalize(
        self,
        workflow: WorkflowSpec,
        source_path: str | Path | None,
        *,
        invocation_cwd: str | Path | None = None,
    ) -> WorkflowSpec:
        normalized = WorkflowSpec.from_dict(workflow.to_dict())
        cwd = Path(invocation_cwd or Path.cwd()).expanduser().resolve()
        source_dir = Path(source_path).expanduser().resolve().parent if source_path is not None else cwd
        commands_by_session = self._commands_by_session(normalized)
        for name, session in normalized.sessions.items():
            if session.transport is not SessionTransport.LOCAL:
                continue
            workdir = self._choose_workdir(
                invocation_cwd=cwd,
                source_dir=source_dir,
                current_workdir=session.workdir,
                commands=commands_by_session.get(name, []),
                startup_commands=session.startup_commands,
            )
            session.workdir = str(workdir)
        return normalized

    def _choose_workdir(
        self,
        *,
        invocation_cwd: Path,
        source_dir: Path,
        current_workdir: str | None,
        commands: list[str],
        startup_commands: list[str],
    ) -> Path:
        current_path = self._resolve_workdir(current_workdir, invocation_cwd)
        explicit_current = bool(current_workdir and current_workdir not in PLACEHOLDER_DIRS)
        relative_paths = self._extract_relative_paths([*commands, *startup_commands])

        if not relative_paths:
            if current_path and current_path.exists() and explicit_current:
                return current_path
            return invocation_cwd

        candidates = self._candidate_dirs(invocation_cwd, source_dir, current_path)
        scored = [(self._score_dir(candidate, relative_paths), index, candidate) for index, candidate in enumerate(candidates)]
        best_score, _, best_dir = max(scored, key=lambda item: (item[0], -item[1]))
        current_score = self._score_dir(current_path, relative_paths) if current_path and current_path.exists() else -1

        if current_path and current_path.exists() and explicit_current and current_score >= best_score:
            return current_path
        if best_score > 0:
            return best_dir
        if current_path and current_path.exists() and explicit_current:
            return current_path
        return invocation_cwd

    @staticmethod
    def _candidate_dirs(invocation_cwd: Path, source_dir: Path, current_path: Path | None) -> list[Path]:
        candidates: list[Path] = []
        for candidate in [invocation_cwd, *invocation_cwd.parents, source_dir, *source_dir.parents]:
            resolved = candidate.resolve()
            if resolved not in candidates:
                candidates.append(resolved)
        if current_path and current_path.resolve() not in candidates:
            candidates.insert(0, current_path.resolve())
        return candidates

    @staticmethod
    def _resolve_workdir(workdir: str | None, invocation_cwd: Path) -> Path | None:
        if not workdir:
            return None
        path = Path(workdir).expanduser()
        if not path.is_absolute():
            path = invocation_cwd / path
        return path.resolve()

    def _extract_relative_paths(self, commands: list[str]) -> list[Path]:
        paths: list[Path] = []
        for command in commands:
            try:
                tokens = shlex.split(command)
            except ValueError:
                continue
            skip_next = False
            for index, token in enumerate(tokens):
                if skip_next:
                    skip_next = False
                    continue
                if token in {"-c", "--command"}:
                    skip_next = True
                    continue
                if token.startswith("-") or "://" in token or token.startswith("$"):
                    continue
                previous = tokens[index - 1] if index > 0 else ""
                if previous in SCRIPT_LAUNCHERS and token not in {"-c", "--command"}:
                    candidate = Path(token)
                elif "/" in token or token.startswith(".") or Path(token).suffix.lower() in PATH_SUFFIXES:
                    candidate = Path(token)
                else:
                    continue
                if candidate.is_absolute():
                    continue
                paths.append(candidate)
        return paths

    @staticmethod
    def _score_dir(directory: Path | None, relative_paths: list[Path]) -> int:
        if directory is None or not directory.exists():
            return -1
        return sum(1 for relative_path in relative_paths if (directory / relative_path).exists())

    @staticmethod
    def _commands_by_session(workflow: WorkflowSpec) -> dict[str, list[str]]:
        commands: dict[str, list[str]] = {}
        for step in workflow.steps:
            if isinstance(step, (CommandStep, ProbeStep)) and step.session:
                commands.setdefault(step.session, []).append(step.command)
        return commands
