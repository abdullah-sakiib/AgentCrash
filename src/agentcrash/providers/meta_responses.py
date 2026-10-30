"""Minimal Meta Responses API adapter for Milestone 4, first slice.

Scope: transport-only. No business logic, no retries, no strategy harness.
The adapter sends a caller-supplied prompt plus optional function-tool
definitions and returns structured text, tool calls, and usage metadata.

Documented contract (per https://dev.meta.ai/docs/api-reference/responses/,
schemas, and tool-calling pages):
- ``POST https://api.meta.ai/v1/responses`` with ``Authorization: Bearer``.
- Request ``{"model", "input", "tools", "tool_choice"}``; flat function tools
  ``{"type": "function", "name", "description", "parameters"}``.
- Response ``output[]`` with ``message`` items containing ``output_text``
  content parts and ``function_call`` items with ``call_id``, ``name``,
  and JSON-string ``arguments``.
No live calls are made by this module or its tests.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values, load_dotenv

API_KEY_ENV_VAR = "MODEL_API_KEY"
PROJECT_API_KEY_ENV_VAR = "AGENTCRASH_META_API_KEY"
BASE_URL_ENV_VAR = "AGENTCRASH_META_BASE_URL"
MODEL_ENV_VAR = "AGENTCRASH_META_MODEL"

DEFAULT_MODEL = "muse-spark-1.3"
DEFAULT_BASE_URL = "https://api.meta.ai/v1/responses"
DEFAULT_TIMEOUT_SECONDS = 30.0


class MetaResponsesError(RuntimeError):
    """Base error for the Meta Responses adapter."""


class MissingCredentialsError(MetaResponsesError):
    """Raised when the API key environment variable is missing or blank."""


class MetaAPIError(MetaResponsesError):
    """Raised for non-2xx responses from the Meta Responses API."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"Meta API error {status_code} ({code}): {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class MalformedResponseError(MetaResponsesError):
    """Raised when a 2xx body does not match the documented contract."""


class MetaTimeoutError(MetaResponsesError):
    """Raised when the request exceeds the configured timeout."""


@dataclass(frozen=True)
class MetaResponsesConfig:
    """Connection settings. The API key is never included in repr or errors."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.api_key or not self.api_key.strip():
            raise MissingCredentialsError(
                f"{API_KEY_ENV_VAR} (or {PROJECT_API_KEY_ENV_VAR} override) "
                "must not be empty; set it in the environment."
            )
        if not self.base_url or not self.base_url.strip():
            raise ValueError("base_url must not be empty.")
        if not self.model or not self.model.strip():
            raise ValueError("model must not be empty.")
        if not self.timeout_seconds or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")

    def __repr__(self) -> str:
        return (
            "MetaResponsesConfig(api_key='[REDACTED]', "
            f"base_url={self.base_url!r}, model={self.model!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True)
class MetaToolCall:
    """A single function-tool invocation requested by the model."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass(frozen=True)
class MetaUsage:
    """Token/cost metadata echoed by the API (all fields optional upstream)."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetaResponse:
    """Structured adapter output; no business interpretation applied."""

    text: str = ""
    tool_calls: tuple[MetaToolCall, ...] = ()
    usage: MetaUsage | None = None
    model: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def default_dotenv_path() -> Path:
    """Root-level ``.env`` path (``src/agentcrash/providers/`` -> repo root)."""
    return Path(__file__).resolve().parents[3] / ".env"


def load_config_from_env(
    environ: dict[str, str] | os._Environ[str] | None = None,
    dotenv_path: str | Path | None = None,
) -> MetaResponsesConfig:
    """Build config from environment. Never logs or returns the raw key.

    A root-level ``.env`` file is loaded first via ``python-dotenv`` with
    ``override=False``, so real environment variables keep precedence.
    Primary key is ``MODEL_API_KEY`` per Meta docs. ``AGENTCRASH_META_API_KEY``
    is accepted as a deliberate project-specific override, justified by the
    existing ``AGENTCRASH_DATABASE_URL`` convention in ``main.py``.
    The project-specific variable takes precedence when both are set.
    """
    if environ is not None:
        env: dict[str, str] | os._Environ[str] = environ
        if dotenv_path is not None:
            file_values = dotenv_values(dotenv_path)
            merged = {
                key: value
                for key, value in file_values.items()
                if isinstance(value, str)
            }
            merged.update(environ)
            env = merged
    else:
        load_dotenv(dotenv_path=dotenv_path or default_dotenv_path(), override=False)
        env = os.environ
    api_key = (env.get(PROJECT_API_KEY_ENV_VAR) or "").strip() or (
        env.get(API_KEY_ENV_VAR) or ""
    ).strip()
    if not api_key:
        raise MissingCredentialsError(
            f"{API_KEY_ENV_VAR} (or {PROJECT_API_KEY_ENV_VAR} override) "
            "is not set; export it before live calls."
        )
    base_url = (env.get(BASE_URL_ENV_VAR) or DEFAULT_BASE_URL).strip()
    model = (env.get(MODEL_ENV_VAR) or DEFAULT_MODEL).strip()
    return MetaResponsesConfig(api_key=api_key, base_url=base_url, model=model)


def build_request_payload(
    *,
    model: str,
    user_input: str | list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the documented Responses JSON body (no credentials)."""
    if not model or not model.strip():
        raise ValueError("model must not be empty.")
    if isinstance(user_input, str):
        if not user_input.strip():
            raise ValueError("user_input must not be empty.")
        payload_input: Any = user_input
    elif isinstance(user_input, list) and user_input:
        for message in user_input:
            if not isinstance(message, dict):
                raise ValueError("user_input list entries must be dicts.")
        payload_input = user_input
    else:
        raise ValueError("user_input must be a non-empty string or list.")
    payload: dict[str, Any] = {"model": model, "input": payload_input}
    if tools is not None:
        for tool in tools:
            if not isinstance(tool, dict):
                raise ValueError("Each tool must be a dict.")
            if tool.get("type") != "function" or not str(
                tool.get("name") or ""
            ).strip():
                raise ValueError(
                    "Each tool must be "
                    "{'type': 'function', 'name': <name>, ...}."
                )
            if "parameters" in tool and tool["parameters"] is not None:
                if not isinstance(tool["parameters"], dict):
                    raise ValueError("'parameters' must be an object.")
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return payload


def _coerce_arguments(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(
                f"Tool-call arguments are not valid JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise MalformedResponseError(
                "Tool-call arguments JSON must decode to an object."
            )
        return parsed
    raise MalformedResponseError(
        "Tool-call arguments must be an object or JSON string."
    )


def parse_response_payload(data: Any) -> MetaResponse:
    """Parse a documented 2xx Responses body; raise on unexpected shapes.

    Documented ``output[]`` handling:
    - ``{"type": "message", "content": [{"type": "output_text",
      "text": ...}]}`` appends text.
    - ``{"type": "function_call", "call_id": ..., "name": ...,
      "arguments": "<JSON string>"}`` yields a tool call.
    Other output item types are ignored for forward compatibility.
    """
    if not isinstance(data, dict):
        raise MalformedResponseError(
            "Response body must be a JSON object to parse."
        )
    raw_output = data.get("output")
    if not isinstance(raw_output, list):
        raise MalformedResponseError("'output' must be a list.")
    text_parts: list[str] = []
    tool_calls: list[MetaToolCall] = []
    for block in raw_output:
        if not isinstance(block, dict):
            raise MalformedResponseError(
                "Each 'output' block must be an object."
            )
        block_type = str(block.get("type") or "")
        if block_type == "message":
            content = block.get("content")
            if not isinstance(content, list):
                raise MalformedResponseError(
                    "Message 'content' must be a list."
                )
            for part in content:
                if not isinstance(part, dict):
                    raise MalformedResponseError(
                        "Each message content part must be an object."
                    )
                if part.get("type") == "output_text":
                    part_text = part.get("text")
                    if not isinstance(part_text, str):
                        raise MalformedResponseError(
                            "'output_text' part needs string 'text'."
                        )
                    text_parts.append(part_text)
                # Other parts (e.g. refusal) are ignored.
        elif block_type == "function_call":
            name = block.get("name")
            call_id = block.get("call_id")
            if not name or not isinstance(name, str):
                raise MalformedResponseError(
                    "Function-call output block needs a 'name'."
                )
            if not call_id or not isinstance(call_id, str):
                raise MalformedResponseError(
                    "Function-call output block needs a 'call_id'."
                )
            tool_calls.append(
                MetaToolCall(
                    name=name,
                    arguments=_coerce_arguments(block.get("arguments")),
                    call_id=call_id,
                )
            )
        # Other item types (reasoning, etc.) are ignored.
    text = "".join(text_parts)
    usage: MetaUsage | None = None
    raw_usage = data.get("usage")
    if raw_usage is not None:
        if not isinstance(raw_usage, dict):
            raise MalformedResponseError("'usage' must be an object.")
        usage = MetaUsage(
            input_tokens=raw_usage.get("input_tokens"),
            output_tokens=raw_usage.get("output_tokens"),
            total_tokens=raw_usage.get("total_tokens"),
            raw=dict(raw_usage),
        )
    model = data.get("model")
    if model is not None and not isinstance(model, str):
        raise MalformedResponseError("'model' must be a string.")
    if not text and not tool_calls:
        raise MalformedResponseError(
            "Response has neither text nor tool calls to return."
        )
    return MetaResponse(
        text=text,
        tool_calls=tuple(tool_calls),
        usage=usage,
        model=model,
        raw=dict(data),
    )


class MetaResponsesClient:
    """Thin async transport for the Meta Responses API (single attempt)."""

    def __init__(
        self,
        config: MetaResponsesConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client

    def __repr__(self) -> str:
        return f"MetaResponsesClient(config={self._config!r})"

    async def generate(
        self,
        user_input: str | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model_override: str | None = None,
    ) -> MetaResponse:
        """POST once with an explicit timeout; never retry automatically."""
        model = model_override or self._config.model
        payload = build_request_payload(
            model=model,
            user_input=user_input,
            tools=tools,
            tool_choice=tool_choice,
        )
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self._config.timeout_seconds)
        if self._client is not None:
            return await self._post(self._client, headers, payload, timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await self._post(client, headers, payload, timeout)

    async def _post(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: httpx.Timeout,
    ) -> MetaResponse:
        try:
            response = await client.post(
                self._config.base_url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise MetaTimeoutError(
                f"Meta request timed out after "
                f"{self._config.timeout_seconds}s."
            ) from exc
        except httpx.HTTPError as exc:
            raise MetaResponsesError(
                f"Meta request transport failed: {type(exc).__name__}."
            ) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise self._api_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise MalformedResponseError(
                "Response body is not valid JSON."
            ) from exc
        return parse_response_payload(data)

    def _api_error(self, response: httpx.Response) -> MetaAPIError:
        code = "http_error"
        message = f"HTTP {response.status_code}."
        try:
            data = response.json()
        except ValueError:
            body = response.text.strip()
            if body:
                message = body[:500]
            return MetaAPIError(
                response.status_code, code, self._redact(message)
            )
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                raw_code = error.get("code")
                raw_message = error.get("message")
                if isinstance(raw_code, str) and raw_code.strip():
                    code = raw_code.strip()[:100]
                if isinstance(raw_message, str) and raw_message.strip():
                    message = raw_message.strip()[:500]
            elif isinstance(error, str) and error.strip():
                message = error.strip()[:500]
            else:
                top_code = data.get("code")
                top_message = data.get("message")
                if isinstance(top_code, str) and top_code.strip():
                    code = top_code.strip()[:100]
                if isinstance(top_message, str) and top_message.strip():
                    message = top_message.strip()[:500]
        return MetaAPIError(
            response.status_code, code, self._redact(message)
        )

    def _redact(self, message: str) -> str:
        key = self._config.api_key
        if key and key in message:
            return message.replace(key, "[REDACTED]")
        return message
