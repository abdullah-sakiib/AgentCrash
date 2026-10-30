import json
from pathlib import Path
from typing import Any

import pytest

from agentcrash.experiments.compare_payment_recovery import DEFAULT_TASK
from agentcrash.experiments.live_payment_recovery import (
    COST_NOTE,
    LIVE_STRATEGY,
    SCENARIO,
    export_live_report_json,
    format_live_report,
    run_live_trial,
    validate_live_config,
)
from agentcrash.providers.meta_responses import (
    MetaResponse,
    MetaToolCall,
    MetaUsage,
    MissingCredentialsError,
)

FAKE_MODEL = "muse-spark-1.3-contributor"


class FakeModelClient:
    def __init__(self, responses: list[MetaResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []

    async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
        self.requests.append(user_input)
        assert self._responses, "Fake ran out of scripted responses."
        return self._responses.pop(0)


def _text_response(text: str) -> MetaResponse:
    return MetaResponse(
        text=text,
        tool_calls=(),
        usage=MetaUsage(input_tokens=10, output_tokens=5, total_tokens=15, raw={}),
        model=FAKE_MODEL,
        raw={
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ]
        },
    )


def _status_tool_response() -> MetaResponse:
    args = {"idempotency_key": "payment-timeout-original"}
    return MetaResponse(
        text="",
        tool_calls=(MetaToolCall(name="get_payment_status", arguments=args, call_id="c1"),),
        usage=MetaUsage(input_tokens=10, output_tokens=5, total_tokens=15, raw={}),
        model=FAKE_MODEL,
        raw={
            "output": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "get_payment_status",
                    "arguments": json.dumps(args),
                }
            ]
        },
    )


def test_validate_live_config_missing_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("AGENTCRASH_META_API_KEY", raising=False)
    with pytest.raises(MissingCredentialsError):
        validate_live_config(dotenv_path=tmp_path / "empty.env")


def test_validate_live_config_uses_explicit_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("AGENTCRASH_META_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_API_KEY=fake-test-key\n", encoding="utf-8")
    config = validate_live_config(dotenv_path=env_file)
    assert config.api_key == "fake-test-key"
    assert config.model
    assert "fake-test-key" not in repr(config)


async def test_single_trial_report_shape(tmp_path: Path) -> None:
    fake = FakeModelClient([_status_tool_response(), _text_response("done")])
    report = await run_live_trial(
        tmp_path, fake, DEFAULT_TASK, max_turns=4, model_name=FAKE_MODEL
    )
    assert report.trial_count == 1
    assert report.scenario == SCENARIO
    assert report.model == FAKE_MODEL
    assert report.live_outcome == "passed"
    assert report.live_error is None
    assert report.successful_charge_count == 1
    assert report.turns_used == 2
    assert report.cost_note == COST_NOTE
    assert len(report.baselines) == 2
    assert [baseline.strategy_name for baseline in report.baselines] == [
        "unsafe_retry",
        "status_check",
    ]
    assert report.usage["responses"] == 2


async def test_single_trial_enforcement_no_reuse(tmp_path: Path) -> None:
    fake = FakeModelClient([_status_tool_response(), _text_response("done")])
    await run_live_trial(tmp_path, fake, DEFAULT_TASK, model_name=FAKE_MODEL)
    fake2 = FakeModelClient([_text_response("done")])
    with pytest.raises(Exception, match="Refusing to reuse"):
        await run_live_trial(tmp_path, fake2, DEFAULT_TASK, model_name=FAKE_MODEL)


async def test_rejects_nonpositive_max_turns_before_state(tmp_path: Path) -> None:
    fake = FakeModelClient([_text_response("done")])
    with pytest.raises(ValueError):
        await run_live_trial(tmp_path / "sub", fake, DEFAULT_TASK, max_turns=0)
    assert not (tmp_path / "sub").exists()


async def test_model_error_captured_without_retry(tmp_path: Path) -> None:
    class FailingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise RuntimeError("boom-failure")

    failing = FailingClient()
    report = await run_live_trial(tmp_path, failing, DEFAULT_TASK, model_name=FAKE_MODEL)
    assert report.live_outcome is None
    assert report.live_error is not None
    assert "boom-failure" in (report.live_error or "")
    assert failing.calls == 1
    assert report.trial_count == 1


async def test_trace_redaction(tmp_path: Path) -> None:
    fake = FakeModelClient([_status_tool_response(), _text_response("done")])
    report = await run_live_trial(tmp_path, fake, DEFAULT_TASK, model_name=FAKE_MODEL)
    serialized = json.dumps(report.to_dict(), default=str)
    assert "Bearer" not in serialized
    assert "Authorization" not in serialized
    assert "fake-test-key" not in serialized.lower()


def test_format_and_export_report(tmp_path: Path) -> None:
    import asyncio

    async def _run() -> Any:
        fake = FakeModelClient([_status_tool_response(), _text_response("done")])
        return await run_live_trial(tmp_path / "db", fake, DEFAULT_TASK, model_name=FAKE_MODEL)

    report = asyncio.run(_run())
    text = format_live_report(report)
    assert "live" in text
    assert LIVE_STRATEGY in text
    assert "unsafe_retry" in text
    assert "status_check" in text
    assert "tokens:" in text
    out = tmp_path / "out" / "live-report.json"
    exported = export_live_report_json(report, out)
    assert exported.exists()
    document = json.loads(exported.read_text(encoding="utf-8"))
    assert document["trial_count"] == 1
    assert document["scenario"] == SCENARIO


async def test_baselines_regression(tmp_path: Path) -> None:
    fake = FakeModelClient([_text_response("done")])
    report = await run_live_trial(tmp_path, fake, DEFAULT_TASK, model_name=FAKE_MODEL)
    by_name = {baseline.strategy_name: baseline for baseline in report.baselines}
    assert by_name["unsafe_retry"].successful_charge_count == 2
    assert by_name["unsafe_retry"].task_outcome == "failed"
    assert by_name["status_check"].successful_charge_count == 1
    assert by_name["status_check"].task_outcome == "passed"


async def test_api_key_redacted_from_error_report(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake_key = "fake-live-key-xyz-123"
    config = MetaResponsesConfig(api_key=fake_key)

    class LeakingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise RuntimeError(f"transport failed with key {fake_key} embedded")

    leaking = LeakingClient()
    report = await run_live_trial(
        tmp_path, leaking, DEFAULT_TASK, model_name=FAKE_MODEL, config=config
    )
    assert report.live_outcome is None
    assert report.live_error is not None
    assert fake_key not in (report.live_error or "")
    assert "[REDACTED]" in (report.live_error or "")
    assert leaking.calls == 1
    serialized = json.dumps(report.to_dict(), default=str)
    assert fake_key not in serialized
    assert "Bearer" not in serialized
