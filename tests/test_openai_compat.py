from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from _bootstrap import SRC  # noqa: F401
from model_test_agent.openai_compat import OpenAICompatClient


class FakeResponse:
    def __init__(self, *, body: bytes | None = None, lines: list[bytes] | None = None, content_type: str = "application/json"):
        self._body = body or b""
        self._lines = lines or []
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class OpenAICompatTests(unittest.TestCase):
    def test_chat_stream_aggregates_content_and_tool_calls(self) -> None:
        chunks = [
            b'data: {"choices":[{"delta":{"role":"assistant","content":"I will check the state. "}}]}\n',
            b"\n",
            (
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function",'
                b'"function":{"name":"run_step","arguments":"{\\"step_id\\": \\""}}]}}]}\n'
            ),
            b"\n",
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"launch_server\\"}"}}]}}]}\n',
            b"\n",
            b"data: [DONE]\n",
            b"\n",
        ]
        deltas: list[dict[str, object]] = []
        client = OpenAICompatClient("http://example.com/v1")

        with patch("model_test_agent.openai_compat.request.urlopen", return_value=FakeResponse(lines=chunks, content_type="text/event-stream")):
            result = client.chat(
                model="test-model",
                messages=[{"role": "user", "content": "run"}],
                tools=[{"type": "function", "function": {"name": "run_step"}}],
                stream=True,
                on_delta=deltas.append,
            )

        self.assertEqual(result.message["content"], "I will check the state. ")
        self.assertEqual(result.message["tool_calls"][0]["function"]["name"], "run_step")
        self.assertEqual(result.message["tool_calls"][0]["function"]["arguments"], '{"step_id": "launch_server"}')
        self.assertEqual([item["type"] for item in deltas], ["content_start", "content_delta", "content_end"])

    def test_chat_stream_falls_back_to_buffered_json_when_endpoint_is_not_sse(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I will proceed with the ready step.",
                    }
                }
            ]
        }
        deltas: list[dict[str, object]] = []
        client = OpenAICompatClient("http://example.com/v1")

        with patch(
            "model_test_agent.openai_compat.request.urlopen",
            return_value=FakeResponse(body=json.dumps(payload).encode("utf-8"), content_type="application/json"),
        ):
            result = client.chat(
                model="test-model",
                messages=[{"role": "user", "content": "run"}],
                stream=True,
                on_delta=deltas.append,
            )

        self.assertEqual(result.message["content"], "I will proceed with the ready step.")
        self.assertEqual([item["type"] for item in deltas], ["content_start", "content_delta", "content_end"])

    def test_chat_stream_retries_with_buffered_request_when_stream_is_done_only(self) -> None:
        stream_response = FakeResponse(
            lines=[b"data: [DONE]\n", b"\n"],
            content_type="text/event-stream",
        )
        buffered_payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hello from the buffered fallback.",
                    }
                }
            ]
        }
        deltas: list[dict[str, object]] = []
        client = OpenAICompatClient("http://example.com/v1")

        with patch(
            "model_test_agent.openai_compat.request.urlopen",
            side_effect=[
                stream_response,
                FakeResponse(
                    body=json.dumps(buffered_payload).encode("utf-8"),
                    content_type="application/json",
                ),
            ],
        ) as mocked_urlopen:
            result = client.chat(
                model="test-model",
                messages=[{"role": "user", "content": "run"}],
                stream=True,
                on_delta=deltas.append,
            )

        self.assertEqual(result.message["content"], "Hello from the buffered fallback.")
        self.assertEqual([item["type"] for item in deltas], ["content_start", "content_delta", "content_end"])
        self.assertEqual(mocked_urlopen.call_count, 2)


if __name__ == "__main__":
    unittest.main()
