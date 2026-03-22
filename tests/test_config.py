from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _bootstrap import SRC  # noqa: F401
from model_test_agent.config import Settings


class SettingsConfigTests(unittest.TestCase):
    def test_settings_load_project_mta_toml(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".mta.toml").write_text(
                "\n".join(
                    [
                        "[mta]",
                        'base_url = "http://config.example/v1"',
                        'model = "config-model"',
                        'session_backend = "pty"',
                        "planner_max_attempts = 5",
                        "timeout_s = 900",
                    ]
                )
            )
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings.from_env(cwd=root)

        self.assertEqual(settings.base_url, "http://config.example/v1")
        self.assertEqual(settings.model, "config-model")
        self.assertEqual(settings.agent_model, "config-model")
        self.assertEqual(settings.session_backend, "pty")
        self.assertEqual(settings.planner_max_attempts, 5)
        self.assertEqual(settings.default_timeout_s, 900)
        self.assertEqual(len(settings.config_paths), 1)

    def test_environment_overrides_project_and_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as home_tmpdir, tempfile.TemporaryDirectory() as project_tmpdir:
            home = Path(home_tmpdir)
            project = Path(project_tmpdir)
            (home / ".mta.toml").write_text(
                "\n".join(
                    [
                        "[mta]",
                        'base_url = "http://home.example/v1"',
                        'model = "home-model"',
                        'session_backend = "tmux"',
                    ]
                )
            )
            (project / ".mta.toml").write_text(
                "\n".join(
                    [
                        "[mta]",
                        'base_url = "http://project.example/v1"',
                        'model = "project-model"',
                        'session_backend = "pty"',
                    ]
                )
            )
            with patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "MTA_MODEL": "env-model",
                    "MTA_SESSION_BACKEND": "auto",
                },
                clear=True,
            ):
                settings = Settings.from_env(cwd=project)

        self.assertEqual(settings.base_url, "http://project.example/v1")
        self.assertEqual(settings.model, "env-model")
        self.assertEqual(settings.agent_model, "env-model")
        self.assertEqual(settings.session_backend, "auto")
        self.assertEqual(len(settings.config_paths), 2)


if __name__ == "__main__":
    unittest.main()
