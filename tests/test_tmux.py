import unittest

from _bootstrap import SRC  # noqa: F401
from model_test_agent.runtime.tmux import TmuxClient


class TmuxTests(unittest.TestCase):
    def test_extract_segment(self) -> None:
        output = """
shell prompt
__MTA_START_abc__
hello
world
__MTA_DONE_abc__ 0
"""
        segment, exit_code = TmuxClient._extract_segment(output, "__MTA_START_abc__", "__MTA_DONE_abc__")
        self.assertEqual(segment, "hello\nworld")
        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
