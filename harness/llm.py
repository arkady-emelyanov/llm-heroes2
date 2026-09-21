"""Streaming, schema-constrained calls to an OpenAI-compatible endpoint.

Why this exists rather than `ChatOpenAI.with_structured_output`: that helper buffers the whole
reply and drops the `reasoning_content` field entirely, so a reasoning model produces several
silent minutes and then an answer with its thinking thrown away. This talks to the endpoint
directly, which keeps both the streaming and the reasoning.

LangGraph still orchestrates the turn; only the model call is our own.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Iterator


class ModelError(RuntimeError):
    """The model answered, but not with something usable."""


def _iter_sse(response) -> Iterator[dict]:
    """Yields the JSON payload of each `data:` line of a server-sent-events stream."""
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()

        if not line.startswith("data:"):
            continue

        payload = line[5:].strip()

        if payload == "[DONE]":
            return

        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            # A malformed frame is not worth aborting a several-minute call over.
            continue


class StreamingClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: float = 1800.0,
        reasoning_effort: str | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout = timeout
        self._reasoning_effort = reasoning_effort

    def complete_json(
        self,
        messages: list[dict],
        schema: dict,
        schema_name: str,
        on_reasoning: Callable[[str], None] | None = None,
        on_content: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None,
    ) -> tuple[dict, int]:
        """Streams one schema-constrained completion.

        Returns the parsed object and how many characters of reasoning it took to get there.
        `on_reasoning` is called with each fragment of thinking as it arrives, which is the whole
        point of streaming here.

        `reasoning_effort` overrides the client's default for this one call. Not every call is worth
        the same amount of thinking, and a call that thinks too long runs its output budget out
        before it answers - which arrives as truncated JSON, not as an error.
        """
        body = {
            "model": self._model,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "stream": True,
            "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema}},
        }

        # Only sent when asked for: servers that do not understand it may reject the request
        # outright rather than ignoring it.
        effort = reasoning_effort or self._reasoning_effort
        if effort:
            if effort == "off":
                # How llama.cpp turns thinking off for models whose template supports it.
                body["chat_template_kwargs"] = {"enable_thinking": False}
            else:
                body["reasoning_effort"] = effort

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        request = urllib.request.Request(self._url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")

        content = ""
        reasoning_chars = 0
        finish_reason = None

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                for frame in _iter_sse(response):
                    choices = frame.get("choices") or []
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta") or {}

                    reasoning = delta.get("reasoning_content") or ""
                    if reasoning:
                        reasoning_chars += len(reasoning)
                        if on_reasoning:
                            on_reasoning(reasoning)

                    piece = delta.get("content") or ""
                    if piece:
                        content += piece
                        if on_content:
                            on_content(piece)

                    finish_reason = choice.get("finish_reason") or finish_reason
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ModelError(f"the endpoint returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"cannot reach {self._url}: {exc.reason}") from exc

        if not content.strip():
            # Almost always the model thinking until its output budget ran out. Saying so beats a
            # bare JSON parse error, because the fix is a bigger budget rather than a better prompt.
            hint = " It ran out of output budget while thinking; raise --max-tokens." if finish_reason == "length" else ""
            raise ModelError(f"the model produced {reasoning_chars} characters of reasoning but no answer.{hint}")

        try:
            return json.loads(content), reasoning_chars
        except json.JSONDecodeError as exc:
            raise ModelError(f"the model's reply was not valid JSON ({exc}): {content[:200]}") from exc
