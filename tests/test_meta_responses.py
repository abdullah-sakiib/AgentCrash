import httpx
import pytest

from agentcrash.providers.meta_responses import (
    API_KEY_ENV_VAR,
    PROJECT_API_KEY_ENV_VAR,
    MalformedResponseError,
    MetaAPIError,
    MetaResponsesClient,
    MetaResponsesConfig,
    MetaResponsesError,
    MetaTimeoutError,
    MissingCredentialsError,
    build_request_payload,
    load_config_from_env,
    parse_response_payload,
)

TEST_KEY = "test-key-abc123"
TEST_BASE_URL = "https://api.meta.ai/v1/responses"
TEST_MODEL = "muse-spark-1.3"


def _config(**overrides: object) -> MetaResponsesConfig:
    values: dict[str, object] = {
        "api_key": TEST_KEY,
        "base_url": TEST_BASE_URL,
        "model": TEST_MODEL,
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return MetaResponsesConfig(**values)  # type: ignore[arg-type]


def _client_for(
    handler: object, config: MetaResponsesConfig | None = None
) -> MetaResponsesClient:
    active = config or _config()
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    http_client = httpx.AsyncClient(transport=transport)
    return MetaResponsesClient(active, client=http_client)


def _documented_body() -> dict[str, object]:
    return {
        "id": "resp-1",
        "object": "response",
        "status": "completed",
        "model": TEST_MODEL,
        "output": [
            {
                "type": "message",
                "id": "msg-1",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "status checked", "annotations": []}
                ],
            },
            {
                "type": "function_call",
                "id": "fc-1",
                "call_id": "call-1",
                "name": "get_payment_status",
                "arguments": '{"idempotency_key": "original"}',
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
    }


async def test_success_parses_documented_output_items() -> None:
    body = _documented_body()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json=body)

    client = _client_for(handler)
    result = await client.generate(
        "Check payment status.",
        tools=[
            {
                "type": "function",
                "name": "get_payment_status",
                "description": "Look up a payment by key.",
                "parameters": {"type": "object"},
            }
        ],
    )

    assert result.text == "status checked"
    assert [(call.name, call.arguments) for call in result.tool_calls] == [
        ("get_payment_status", {"idempotency_key": "original"})
    ]
    assert result.tool_calls[0].call_id == "call-1"
    assert result.usage is not None
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 19
    assert result.model == TEST_MODEL


async def test_text_only_message_parses() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-2",
                "model": TEST_MODEL,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "all good"}
                        ],
                    }
                ],
            },
        )

    result = await _client_for(handler).generate("hello")
    assert result.text == "all good"
    assert result.tool_calls == ()


async def test_unknown_output_items_ignored() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-3",
                "model": TEST_MODEL,
                "output": [
                    {"type": "reasoning", "id": "rs-1"},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "done"}
                        ],
                    },
                ],
            },
        )

    result = await _client_for(handler).generate("hello")
    assert result.text == "done"


async def test_api_error_extracts_code_and_message() -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(
            401, json={"error": {"code": "auth_error", "message": "bad key"}}
        )

    client = _client_for(handler)
    with pytest.raises(MetaAPIError) as excinfo:
        await client.generate("hello")

    assert calls["count"] == 1
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "auth_error"
    assert "bad key" in excinfo.value.message
    assert TEST_KEY not in str(excinfo.value)


async def test_error_string_shape_and_top_level_shape() -> None:
    async def string_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid input"})

    with pytest.raises(MetaAPIError) as excinfo:
        await _client_for(string_handler).generate("hello")
    assert excinfo.value.code == "http_error"
    assert "invalid input" in excinfo.value.message

    async def top_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"code": "bad_request", "message": "oops"}
        )

    with pytest.raises(MetaAPIError) as excinfo2:
        await _client_for(top_handler).generate("hello")
    assert excinfo2.value.code == "bad_request"
    assert "oops" in excinfo2.value.message


async def test_server_error_without_json_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream failure")

    client = _client_for(handler)
    with pytest.raises(MetaAPIError) as excinfo:
        await client.generate("hello")

    assert excinfo.value.status_code == 500
    assert "upstream failure" in excinfo.value.message
    assert TEST_KEY not in str(excinfo.value)


async def test_malformed_bodies_raise() -> None:
    for bad in [
        {"unexpected": "shape"},
        {"output": "not-a-list"},
        {"output": [{"type": "message", "content": "bad"}]},
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text"}],
                }
            ]
        },
        {
            "output": [
                {"type": "function_call", "name": "x", "arguments": "{}"}
            ]
        },
        {
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c",
                    "name": "x",
                    "arguments": "{not-json}",
                }
            ]
        },
        {"output": []},
    ]:
        with pytest.raises(MalformedResponseError):
            parse_response_payload(bad)


async def test_invalid_json_body_raises_malformed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json{{{)")

    client = _client_for(handler)
    with pytest.raises(MalformedResponseError):
        await client.generate("hello")


async def test_timeout_is_wrapped_without_retry_or_key_leak() -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client_for(handler)
    with pytest.raises(MetaTimeoutError) as excinfo:
        await client.generate("hello")

    assert calls["count"] == 1
    assert TEST_KEY not in str(excinfo.value)


def test_missing_api_key_raises() -> None:
    with pytest.raises(MissingCredentialsError):
        load_config_from_env({})

    with pytest.raises(MissingCredentialsError):
        load_config_from_env({API_KEY_ENV_VAR: "   "})

    with pytest.raises(MissingCredentialsError):
        MetaResponsesConfig(api_key="  ")


def test_config_env_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, "official-key")
    monkeypatch.setenv(PROJECT_API_KEY_ENV_VAR, "project-key")
    assert load_config_from_env().api_key == "project-key"

    monkeypatch.delenv(PROJECT_API_KEY_ENV_VAR)
    assert load_config_from_env().api_key == "official-key"


def test_config_defaults_match_documented_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv(API_KEY_ENV_VAR, "env-key")
    for var in (
        "AGENTCRASH_META_BASE_URL",
        "AGENTCRASH_META_MODEL",
        PROJECT_API_KEY_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    config = load_config_from_env(dotenv_path=empty_env)
    assert config.base_url == "https://api.meta.ai/v1/responses"
    assert config.model == "muse-spark-1.3"


def test_credentials_redacted_from_repr_and_payload() -> None:
    config = _config()
    client = MetaResponsesClient(config)

    assert TEST_KEY not in repr(config)
    assert TEST_KEY not in repr(client)
    assert "[REDACTED]" in repr(config)

    payload = build_request_payload(model=config.model, user_input="hi")
    assert TEST_KEY not in str(payload)
    assert "Authorization" not in payload

    parsed = parse_response_payload(
        {
            "id": "r",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
        }
    )
    assert TEST_KEY not in str(parsed)

    try:
        raise MetaAPIError(400, "bad", "nope")
    except MetaResponsesError as exc:
        assert TEST_KEY not in str(exc)


def test_build_request_payload_validates_documented_tools() -> None:
    with pytest.raises(ValueError):
        build_request_payload(model="m", user_input="hi", tools=[{"type": "x"}])

    with pytest.raises(ValueError):
        build_request_payload(
            model="m", user_input="hi", tools=[{"type": "function"}]
        )

    with pytest.raises(ValueError):
        build_request_payload(
            model="m",
            user_input="hi",
            tools=[{"type": "function", "name": "x", "parameters": "bad"}],
        )

    with pytest.raises(ValueError):
        build_request_payload(model=" ", user_input="hi")

    with pytest.raises(ValueError):
        build_request_payload(model="m", user_input="  ")
