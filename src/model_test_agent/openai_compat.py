from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib import request


@dataclass
class ChatCompletionResult:
    message: dict[str, Any]
    raw_response: dict[str, Any]


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str = "", timeout_s: int = 120):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4000,
        response_format: dict[str, Any] | None = None,
        stream: bool = False,
        on_delta: Callable[[dict[str, Any]], None] | None = None,
    ) -> ChatCompletionResult:
        return self._chat(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=stream,
            on_delta=on_delta,
            allow_empty_stream_retry=stream,
            emit_buffered_deltas=stream,
        )

    def _chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None,
        stream: bool,
        on_delta: Callable[[dict[str, Any]], None] | None,
        allow_empty_stream_retry: bool,
        emit_buffered_deltas: bool,
    ) -> ChatCompletionResult:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format
        if stream:
            payload["stream"] = True
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = request.Request(
            url=f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        with request.urlopen(req, timeout=self.timeout_s) as resp:
            content_type = str(getattr(resp, "headers", {}).get("Content-Type", "")).lower()
            if stream and "text/event-stream" in content_type:
                result = self._consume_stream(resp, on_delta=on_delta)
                if allow_empty_stream_retry and self._should_retry_empty_stream(result):
                    return self._chat(
                        model=model,
                        messages=messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                        stream=False,
                        on_delta=on_delta,
                        allow_empty_stream_retry=False,
                        emit_buffered_deltas=True,
                    )
                return result
            raw = json.loads(resp.read().decode("utf-8"))
        return self._result_from_raw(raw, on_delta=on_delta if emit_buffered_deltas else None)

    def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 4000,
    ) -> dict[str, Any]:
        result = self.chat(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        content = self._message_text(result.message)
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return json.loads(self._extract_json(content))

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            return "".join(parts)
        return str(content)

    @staticmethod
    def _extract_json(text: str) -> str:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"Could not locate JSON object in response: {text}")
        return text[start : end + 1]

    def _consume_stream(
        self,
        resp: Any,
        *,
        on_delta: Callable[[dict[str, Any]], None] | None = None,
    ) -> ChatCompletionResult:
        message: dict[str, Any] = {"role": "assistant", "content": ""}
        tool_calls: dict[int, dict[str, Any]] = {}
        raw_events: list[dict[str, Any]] = []
        stream_open = False

        for payload in self._iter_sse_payloads(resp):
            if payload == "[DONE]":
                break
            raw = json.loads(payload)
            raw_events.append(raw)
            choice = ((raw.get("choices") or [{}])[0]) if isinstance(raw.get("choices"), list) else {}
            delta = choice.get("delta") or {}
            role = delta.get("role")
            if role:
                message["role"] = role

            content_text = self._delta_text(delta)
            if content_text:
                if not stream_open and on_delta:
                    on_delta({"type": "content_start"})
                stream_open = True
                message["content"] = str(message.get("content", "")) + content_text
                if on_delta:
                    on_delta({"type": "content_delta", "text": content_text})

            for tool_call in delta.get("tool_calls", []):
                index = int(tool_call.get("index", 0))
                current = tool_calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                if tool_call.get("id"):
                    current["id"] = str(tool_call["id"])
                if tool_call.get("type"):
                    current["type"] = str(tool_call["type"])
                function = tool_call.get("function") or {}
                if function.get("name"):
                    current["function"]["name"] += str(function["name"])
                if function.get("arguments"):
                    current["function"]["arguments"] += str(function["arguments"])

        if stream_open and on_delta:
            on_delta({"type": "content_end"})
        if tool_calls:
            message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
        return ChatCompletionResult(
            message=message,
            raw_response={"object": "chat.completion.stream", "events": raw_events},
        )

    @staticmethod
    def _iter_sse_payloads(resp: Iterable[bytes]) -> Iterable[str]:
        data_lines: list[str] = []
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield "\n".join(data_lines)

    @classmethod
    def _result_from_raw(
        cls,
        raw: dict[str, Any],
        *,
        on_delta: Callable[[dict[str, Any]], None] | None = None,
    ) -> ChatCompletionResult:
        try:
            message = raw["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"Invalid chat completion response: {raw}") from exc
        if on_delta:
            text = cls._message_text(message)
            if text:
                on_delta({"type": "content_start"})
                on_delta({"type": "content_delta", "text": text})
                on_delta({"type": "content_end"})
        return ChatCompletionResult(message=message, raw_response=raw)

    @classmethod
    def _delta_text(cls, delta: dict[str, Any]) -> str:
        content = delta.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(str(item.get("text", "")))
                    elif "text" in item:
                        parts.append(str(item["text"]))
                else:
                    parts.append(str(item))
            return "".join(parts)
        return str(content or "")

    @classmethod
    def _should_retry_empty_stream(cls, result: ChatCompletionResult) -> bool:
        message = result.message
        text = cls._message_text(message).strip()
        tool_calls = message.get("tool_calls")
        return not text and not tool_calls
