from __future__ import annotations

import os
from dataclasses import dataclass


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
    max_iterations: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        base_url = os.getenv("MTA_BASE_URL", os.getenv("MBA_BASE_URL", os.getenv("OPENAI_BASE_URL", ""))).strip()
        api_key = os.getenv("MTA_API_KEY", os.getenv("MBA_API_KEY", os.getenv("OPENAI_API_KEY", ""))).strip()
        model = os.getenv("MTA_MODEL", os.getenv("MBA_MODEL", os.getenv("OPENAI_MODEL", ""))).strip()
        planner_model = os.getenv("MTA_PLANNER_MODEL", os.getenv("MBA_PLANNER_MODEL", model)).strip()
        agent_model = os.getenv("MTA_AGENT_MODEL", os.getenv("MBA_AGENT_MODEL", model)).strip()
        session_backend = os.getenv("MTA_SESSION_BACKEND", os.getenv("MBA_SESSION_BACKEND", "auto")).strip() or "auto"
        tmux_bin = os.getenv("MTA_TMUX_BIN", os.getenv("MBA_TMUX_BIN", "tmux")).strip() or "tmux"
        log_root = os.getenv("MTA_LOG_ROOT", os.getenv("MBA_LOG_ROOT", ".mta-runs")).strip() or ".mta-runs"
        poll_interval_s = float(os.getenv("MTA_POLL_INTERVAL_S", os.getenv("MBA_POLL_INTERVAL_S", "1.0")))
        session_buffer_max_chars = int(
            os.getenv("MTA_SESSION_BUFFER_MAX_CHARS", os.getenv("MBA_SESSION_BUFFER_MAX_CHARS", "200000"))
        )
        default_capture_lines = int(os.getenv("MTA_CAPTURE_LINES", os.getenv("MBA_CAPTURE_LINES", "300")))
        default_timeout_s = int(os.getenv("MTA_TIMEOUT_S", os.getenv("MBA_TIMEOUT_S", "300")))
        max_iterations = int(os.getenv("MTA_MAX_ITERATIONS", os.getenv("MBA_MAX_ITERATIONS", "60")))
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
            max_iterations=max_iterations,
        )

    def require_model_access(self) -> None:
        if not self.base_url:
            raise ValueError("Missing MTA_BASE_URL, MBA_BASE_URL, or OPENAI_BASE_URL")
        if not self.model:
            raise ValueError("Missing MTA_MODEL, MBA_MODEL, or OPENAI_MODEL")
