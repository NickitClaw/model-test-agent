from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib  # type: ignore[attr-defined]
except ModuleNotFoundError:  # pragma: no cover - Python 3.10+ provides tomllib
    tomllib = None


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _config_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _env_bool(value, default)
    return default


def _config_int(value: Any, default: int) -> int:
    if value is None:
        return default
    return int(value)


def _config_float(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)


def _config_str(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


@dataclass
class Settings:
    base_url: str
    api_key: str
    model: str
    planner_model: str
    agent_model: str
    session_backend: str = "auto"
    tmux_bin: str = "tmux"
    log_root: str = ".mta-runs"
    poll_interval_s: float = 1.0
    session_buffer_max_chars: int = 200000
    default_capture_lines: int = 300
    default_timeout_s: int = 300
    planner_max_attempts: int = 3
    max_iterations: int = 60
    stream_agent_output: bool = True
    config_paths: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, *, cwd: Path | None = None) -> "Settings":
        config_values, config_paths = _load_config_values(cwd or Path.cwd())
        base_url = _env_or_default(
            "MTA_BASE_URL",
            "MBA_BASE_URL",
            fallback_env="OPENAI_BASE_URL",
            default=_config_str(config_values.get("base_url"), ""),
        )
        api_key = _env_or_default(
            "MTA_API_KEY",
            "MBA_API_KEY",
            fallback_env="OPENAI_API_KEY",
            default=_config_str(config_values.get("api_key"), ""),
        )
        model = _env_or_default(
            "MTA_MODEL",
            "MBA_MODEL",
            fallback_env="OPENAI_MODEL",
            default=_config_str(config_values.get("model"), ""),
        )
        planner_model = _env_or_default(
            "MTA_PLANNER_MODEL",
            "MBA_PLANNER_MODEL",
            default=_config_str(config_values.get("planner_model"), model),
        )
        agent_model = _env_or_default(
            "MTA_AGENT_MODEL",
            "MBA_AGENT_MODEL",
            default=_config_str(config_values.get("agent_model"), model),
        )
        session_backend = _env_or_default(
            "MTA_SESSION_BACKEND",
            "MBA_SESSION_BACKEND",
            default=_config_str(config_values.get("session_backend"), "auto"),
        )
        tmux_bin = _env_or_default(
            "MTA_TMUX_BIN",
            "MBA_TMUX_BIN",
            default=_config_str(config_values.get("tmux_bin"), "tmux"),
        )
        log_root = _env_or_default(
            "MTA_LOG_ROOT",
            "MBA_LOG_ROOT",
            default=_config_str(config_values.get("log_root"), ".mta-runs"),
        )
        poll_interval_s = float(
            os.getenv(
                "MTA_POLL_INTERVAL_S",
                os.getenv("MBA_POLL_INTERVAL_S", str(_config_float(config_values.get("poll_interval_s"), 1.0))),
            )
        )
        session_buffer_max_chars = int(
            os.getenv(
                "MTA_SESSION_BUFFER_MAX_CHARS",
                os.getenv(
                    "MBA_SESSION_BUFFER_MAX_CHARS",
                    str(_config_int(config_values.get("session_buffer_max_chars"), 200000)),
                ),
            )
        )
        default_capture_lines = int(
            os.getenv(
                "MTA_CAPTURE_LINES",
                os.getenv("MBA_CAPTURE_LINES", str(_config_int(config_values.get("default_capture_lines"), 300))),
            )
        )
        default_timeout_s = int(
            os.getenv(
                "MTA_TIMEOUT_S",
                os.getenv("MBA_TIMEOUT_S", str(_config_int(config_values.get("default_timeout_s"), 300))),
            )
        )
        planner_max_attempts = int(
            os.getenv(
                "MTA_PLANNER_MAX_ATTEMPTS",
                os.getenv(
                    "MBA_PLANNER_MAX_ATTEMPTS",
                    str(_config_int(config_values.get("planner_max_attempts"), 3)),
                ),
            )
        )
        max_iterations = int(
            os.getenv(
                "MTA_MAX_ITERATIONS",
                os.getenv("MBA_MAX_ITERATIONS", str(_config_int(config_values.get("max_iterations"), 60))),
            )
        )
        stream_agent_output = _env_bool(
            os.getenv("MTA_STREAM_AGENT_OUTPUT", os.getenv("MBA_STREAM_AGENT_OUTPUT")),
            _config_bool(config_values.get("stream_agent_output"), True),
        )
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            planner_model=planner_model,
            agent_model=agent_model,
            session_backend=session_backend,
            tmux_bin=tmux_bin,
            log_root=log_root,
            poll_interval_s=poll_interval_s,
            session_buffer_max_chars=session_buffer_max_chars,
            default_capture_lines=default_capture_lines,
            default_timeout_s=default_timeout_s,
            planner_max_attempts=planner_max_attempts,
            max_iterations=max_iterations,
            stream_agent_output=stream_agent_output,
            config_paths=config_paths,
        )

    def require_model_access(self) -> None:
        if not self.base_url:
            raise ValueError("Missing MTA_BASE_URL, MBA_BASE_URL, or OPENAI_BASE_URL")
        if not self.model:
            raise ValueError("Missing MTA_MODEL, MBA_MODEL, or OPENAI_MODEL")


def _env_or_default(
    primary_env: str,
    legacy_env: str,
    *,
    fallback_env: str | None = None,
    default: str,
) -> str:
    value = os.getenv(primary_env)
    if value is None:
        value = os.getenv(legacy_env)
    if value is None and fallback_env:
        value = os.getenv(fallback_env)
    if value is None:
        return default
    return value.strip() or default


def _load_config_values(cwd: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    merged: dict[str, Any] = {}
    paths: list[str] = []
    explicit = _explicit_config_path()
    for path in _discover_config_paths(cwd):
        if explicit and not path.exists():
            raise ValueError(f"Configured MTA config file does not exist: {path}")
        if not path.exists():
            continue
        payload = _load_config_file(path)
        scoped = _extract_settings_scope(payload)
        if not scoped:
            continue
        merged.update(scoped)
        paths.append(str(path))
    return merged, tuple(paths)


def _explicit_config_path() -> Path | None:
    raw = os.getenv("MTA_CONFIG", os.getenv("MBA_CONFIG", "")).strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _discover_config_paths(cwd: Path) -> list[Path]:
    explicit = _explicit_config_path()
    if explicit is not None:
        return [explicit]
    paths: list[Path] = []
    home_config = _first_existing([Path.home() / ".mta.toml", Path.home() / ".mba.toml"])
    if home_config is not None:
        paths.append(home_config)
    project_config = _find_project_config(cwd.resolve())
    if project_config is not None and project_config not in paths:
        paths.append(project_config)
    return paths


def _first_existing(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path.exists():
            return path
    return None


def _find_project_config(cwd: Path) -> Path | None:
    for directory in [cwd, *cwd.parents]:
        for name in (".mta.toml", ".mba.toml"):
            candidate = directory / name
            if candidate.exists():
                return candidate
    return None


def _load_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:  # pragma: no branch
        return tomllib.loads(text)
    return _parse_basic_toml(text)


def _extract_settings_scope(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("mta"), dict):
        return _normalize_config_aliases(dict(payload["mta"]))
    if isinstance(payload.get("mba"), dict):
        return _normalize_config_aliases(dict(payload["mba"]))
    return _normalize_config_aliases(dict(payload))


def _normalize_config_aliases(values: dict[str, Any]) -> dict[str, Any]:
    aliases = {
        "timeout_s": "default_timeout_s",
        "capture_lines": "default_capture_lines",
    }
    normalized = dict(values)
    for alias, canonical in aliases.items():
        if alias in normalized and canonical not in normalized:
            normalized[canonical] = normalized[alias]
    return normalized


def _parse_basic_toml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current = root
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            current = root.setdefault(section, {})
            continue
        key, sep, value = raw_line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = _strip_inline_comment(value.strip())
        current[key] = _parse_basic_toml_value(value)
    return root


def _strip_inline_comment(value: str) -> str:
    in_quote: str | None = None
    escaped = False
    result: list[str] = []
    for char in value:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\" and in_quote == '"':
            result.append(char)
            escaped = True
            continue
        if char in {'"', "'"}:
            if in_quote == char:
                in_quote = None
            elif in_quote is None:
                in_quote = char
            result.append(char)
            continue
        if char == "#" and in_quote is None:
            break
        result.append(char)
    return "".join(result).strip()


def _parse_basic_toml_value(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
