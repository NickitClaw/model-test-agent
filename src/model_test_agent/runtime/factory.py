from __future__ import annotations

import shutil

from ..config import Settings
from .pty import PtyClient
from .session_backend import SessionBackend
from .tmux import TmuxClient


def resolve_session_backend(settings: Settings) -> str:
    choice = settings.session_backend
    if choice == "auto":
        return "tmux" if shutil.which(settings.tmux_bin) else "pty"
    return choice


def create_session_backend(settings: Settings) -> SessionBackend:
    resolved = resolve_session_backend(settings)
    if resolved == "tmux":
        if not shutil.which(settings.tmux_bin):
            raise RuntimeError(
                f"Session backend 'tmux' was requested but {settings.tmux_bin!r} is not available"
            )
        return TmuxClient(
            tmux_bin=settings.tmux_bin,
            poll_interval_s=settings.poll_interval_s,
        )
    if resolved == "pty":
        return PtyClient(
            poll_interval_s=settings.poll_interval_s,
            buffer_max_chars=settings.session_buffer_max_chars,
        )
    raise ValueError(f"Unsupported session backend: {settings.session_backend}")
