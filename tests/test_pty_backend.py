from __future__ import annotations

import os
import threading
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

    def test_run_command_handles_output_larger_than_tail_window(self) -> None:
        client = PtyClient(poll_interval_s=0.05, buffer_max_chars=2000)
        session_name = "pty-test-long-command"
        try:
            client.create_session(session_name, shell="/bin/bash")
            result = client.run_command(
                session_name,
                "python3 -c \"print('FIRST-LINE'); [print(f'line-{i}') for i in range(2000)]\"",
                timeout_s=10,
                lines=20,
            )
            self.assertEqual(result.exit_code, 0)
            self.assertIn("FIRST-LINE", result.output)
            self.assertIn("line-1999", result.output)
        finally:
            client.kill_session(session_name)

    def test_wait_for_pattern_survives_buffer_truncation(self) -> None:
        client = PtyClient(poll_interval_s=0.05, buffer_max_chars=1000)
        session_name = "pty-test-pattern-window"
        try:
            client.create_session(session_name, shell="/bin/bash")
            holder: dict[str, object] = {}

            def waiter() -> None:
                holder["wait"] = client.wait_for_pattern(session_name, "MAGIC_READY", timeout_s=5, lines=20)

            thread = threading.Thread(target=waiter)
            thread.start()
            client.send_literal(
                session_name,
                "python3 -c \"print('MAGIC_READY'); [print('x'*200) for _ in range(2000)]\"",
            )
            thread.join(timeout=6)
            self.assertFalse(thread.is_alive(), "wait thread did not finish")
            wait = holder["wait"]
            self.assertEqual(wait.status, "matched")
            self.assertEqual(wait.matched_pattern, "MAGIC_READY")
        finally:
            client.kill_session(session_name)


if __name__ == "__main__":
    unittest.main()
