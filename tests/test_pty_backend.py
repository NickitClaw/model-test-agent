from __future__ import annotations

import os
import unittest

from _bootstrap import SRC  # noqa: F401
from model_test_agent.runtime.pty import PtyClient


@unittest.skipIf(os.name == "nt", "PTY backend test requires a POSIX platform")
class PtyBackendTests(unittest.TestCase):
    def test_run_command_and_wait(self) -> None:
        client = PtyClient(poll_interval_s=0.05, buffer_max_chars=50000)
        session_name = "pty-test"
        try:
            client.create_session(session_name, shell="/bin/bash")
            result = client.run_command(
                session_name,
                "printf 'hello\\nworld\\n'",
                timeout_s=5,
                lines=80,
            )
            self.assertEqual(result.exit_code, 0)
            self.assertIn("hello", result.output)

            client.send_literal(session_name, "python3 -c \"print('ready')\"")
            wait = client.wait_for_pattern(session_name, "ready", timeout_s=5, lines=80)
            self.assertEqual(wait.status, "matched")
            self.assertIn("ready", wait.output)
        finally:
            client.kill_session(session_name)


if __name__ == "__main__":
    unittest.main()
