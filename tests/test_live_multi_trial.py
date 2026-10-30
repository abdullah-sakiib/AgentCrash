import json
from pathlib import Path
from typing import Any

import pytest

from agentcrash.experiments.compare_payment_recovery import DEFAULT_TASK
from agentcrash.experiments.live_payment_recovery import (
    export_multi_trial_json,
    format_multi_trial_report,
    run_live_trial,
    run_live_trials,
)
from agentcrash.providers.meta_responses import (
    MetaResponse,
    MetaToolCall,
    MetaUsage,
)

FAKE_MODEL = "muse-spark-1.3-contributor"


class FakeModelClient:
    def __init__(self, responses: list[MetaResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[Any] = []
        self.calls = 0

    async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
        self.calls += 1
        self.requests.append(user_input)
        assert self._responses, "Fake ran out of scripted responses."
        return self._responses.pop(0)


def _usage(i: int = 10, o: int = 5) -> MetaUsage:
    return MetaUsage(input_tokens=i, output_tokens=o, total_tokens=i + o, raw={})


def _text_response(text: str) -> MetaResponse:
    return MetaResponse(
        text=text,
        tool_calls=(),
        usage=_usage(),
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


def _status_tool_response(call_id: str = "c1") -> MetaResponse:
    args = {"idempotency_key": "payment-timeout-original"}
    return MetaResponse(
        text="",
        tool_calls=(MetaToolCall(name="get_payment_status", arguments=args, call_id=call_id),),
        usage=_usage(),
        model=FAKE_MODEL,
        raw={
            "output": [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "get_payment_status",
                    "arguments": json.dumps(args),
                }
            ]
        },
    )


def _charge_tool_response(key: str = "retry-new", call_id: str = "c1") -> MetaResponse:
    args = {"order_id": 1, "idempotency_key": key}
    return MetaResponse(
        text="",
        tool_calls=(MetaToolCall(name="charge_payment", arguments=args, call_id=call_id),),
        usage=_usage(),
        model=FAKE_MODEL,
        raw={
            "output": [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "charge_payment",
                    "arguments": json.dumps(args),
                }
            ]
        },
    )


def _make_factory(scripts: dict[int, list[MetaResponse]]):
    created: dict[int, FakeModelClient] = {}

    def _factory(trial_index: int) -> FakeModelClient:
        client = FakeModelClient(list(scripts[trial_index]))
        created[trial_index] = client
        return client

    _factory.created = created  # type: ignore[attr-defined]
    return _factory


async def test_multi_trial_isolation_and_pass(tmp_path: Path) -> None:
    factory = _make_factory(
        {
            1: [_status_tool_response(), _text_response("done-1")],
            2: [_status_tool_response(), _text_response("done-2")],
        }
    )
    report = await run_live_trials(tmp_path, factory, DEFAULT_TASK, trial_count=2, model_name=FAKE_MODEL)
    assert report.requested_trials == 2
    assert report.completed_trials == 2
    assert report.aggregate.passed == 2
    assert report.aggregate.pass_rate == 1.0
    assert report.aggregate.duplicate_charge_trials == 0
    assert [trial.trial_index for trial in report.trials] == [1, 2]
    assert [trial.trial_id for trial in report.trials] == [
        "payment_timeout_after_commit-trial-001",
        "payment_timeout_after_commit-trial-002",
    ]
    assert (tmp_path / "trial_001" / "model_recovery.db").exists()
    assert (tmp_path / "trial_002" / "model_recovery.db").exists()
    for trial in report.trials:
        assert trial.order_id == 1
        assert trial.payment_ids == (1,)


async def test_aggregate_counts_failures_duplicates_errors(tmp_path: Path) -> None:
    def _failing_factory(trial_index: int):
        class FailingClient:
            def __init__(self) -> None:
                self.calls = 0

            async def generate(self, *args: Any, **kwargs: Any):
                self.calls += 1
                raise RuntimeError("boom")

        return FailingClient()

    scripts = {
        1: [_status_tool_response(), _text_response("ok")],
        2: [_charge_tool_response(), _text_response("dup")],
    }

    def _mixed_factory(trial_index: int):
        if trial_index == 3:
            return _failing_factory(trial_index)
        return FakeModelClient(list(scripts[trial_index]))

    report = await run_live_trials(tmp_path, _mixed_factory, DEFAULT_TASK, trial_count=3, model_name=FAKE_MODEL)
    assert report.aggregate.requested_trials == 3
    assert report.aggregate.completed_trials == 3
    assert report.aggregate.passed == 1
    assert report.aggregate.failed == 1
    assert report.aggregate.errors == 1
    assert report.aggregate.pass_rate == pytest.approx(1 / 3)
    assert report.aggregate.failure_rate == pytest.approx(1 / 3)
    assert report.aggregate.error_rate == pytest.approx(1 / 3)
    assert report.aggregate.duplicate_charge_trials == 1
    assert report.aggregate.duplicate_charge_rate == pytest.approx(1 / 3)
    assert report.aggregate.violation_counts.get("multiple_successful_charges_for_order") == 1


async def test_token_totals_and_missing_usage(tmp_path: Path) -> None:
    factory = _make_factory(
        {
            1: [_status_tool_response(), _text_response("ok")],
            2: [_status_tool_response(), _text_response("ok")],
        }
    )
    report = await run_live_trials(tmp_path, factory, DEFAULT_TASK, trial_count=2, model_name=FAKE_MODEL)
    assert report.aggregate.total_responses == 4
    assert report.aggregate.total_input_tokens == 40
    assert report.aggregate.total_output_tokens == 20
    assert report.aggregate.total_tokens == 60
    assert report.aggregate.trials_with_usage == 2
    assert report.aggregate.trials_missing_usage == 0


async def test_error_trial_missing_usage_counted(tmp_path: Path) -> None:
    def _factory(trial_index: int):
        if trial_index == 1:
            return FakeModelClient([_status_tool_response(), _text_response("ok")])

        class FailingClient:
            async def generate(self, *args: Any, **kwargs: Any):
                raise RuntimeError("nope")

        return FailingClient()

    report = await run_live_trials(tmp_path, _factory, DEFAULT_TASK, trial_count=2, model_name=FAKE_MODEL)
    assert report.aggregate.errors == 1
    assert report.aggregate.trials_missing_usage == 1
    assert report.aggregate.trials_with_usage == 1


async def test_no_automatic_retry_on_failure(tmp_path: Path) -> None:
    calls: dict[int, int] = {}

    def _factory(trial_index: int):
        class CountingClient(FakeModelClient):
            async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
                calls[trial_index] = calls.get(trial_index, 0) + 1
                raise RuntimeError("fail-once")

        return CountingClient([])

    report = await run_live_trials(tmp_path, _factory, DEFAULT_TASK, trial_count=2, model_name=FAKE_MODEL)
    assert calls == {1: 1, 2: 1}
    assert report.aggregate.errors == 2


async def test_rejects_nonpositive_trials_before_state(tmp_path: Path) -> None:
    def _factory(trial_index: int):
        raise AssertionError("factory must not be called")

    with pytest.raises(ValueError):
        await run_live_trials(tmp_path / "sub", _factory, DEFAULT_TASK, trial_count=0)
    assert not (tmp_path / "sub").exists()


async def test_refuses_existing_database(tmp_path: Path) -> None:
    (tmp_path / "trial_001").mkdir(parents=True)
    (tmp_path / "trial_001" / "model_recovery.db").write_text("x", encoding="utf-8")

    def _factory(trial_index: int):
        return FakeModelClient([_text_response("hi")])

    with pytest.raises(Exception, match="Refusing to reuse"):
        await run_live_trials(tmp_path, _factory, DEFAULT_TASK, trial_count=1)


def test_export_and_format_multi_trial(tmp_path: Path) -> None:
    import asyncio

    async def _run():
        factory = _make_factory({1: [_status_tool_response(), _text_response("ok")]})
        return await run_live_trials(tmp_path / "db", factory, DEFAULT_TASK, trial_count=1, model_name=FAKE_MODEL)

    report = asyncio.run(_run())
    text = format_multi_trial_report(report)
    assert "requested=1 completed=1" in text
    assert "pass:" in text
    assert "unsafe_retry" in text
    out = tmp_path / "out" / "multi.json"
    exported = export_multi_trial_json(report, out)
    document = json.loads(exported.read_text(encoding="utf-8"))
    assert document["requested_trials"] == 1
    assert len(document["trials"]) == 1
    assert "aggregate" in document


async def test_single_trial_backward_compat(tmp_path: Path) -> None:
    from agentcrash.experiments.live_payment_recovery import run_live_trial

    fake = FakeModelClient([_status_tool_response(), _text_response("done")])
    report = await run_live_trial(tmp_path, fake, DEFAULT_TASK, model_name=FAKE_MODEL)
    assert report.trial_count == 1
    assert report.live_outcome == "passed"


async def test_factory_exception_recorded_and_batch_continues(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake_key = "fake-factory-key-abc-123"
    config = MetaResponsesConfig(api_key=fake_key)
    calls: dict[int, int] = {}

    def _factory(trial_index: int):
        calls[trial_index] = calls.get(trial_index, 0) + 1
        if trial_index == 1:
            raise RuntimeError(f"factory boom with {fake_key} inside")
        return FakeModelClient([_status_tool_response(), _text_response("recovered")])

    from agentcrash.experiments.live_payment_recovery import run_live_trials as _run

    report = await _run(
        tmp_path, _factory, DEFAULT_TASK, trial_count=2, model_name=FAKE_MODEL, config=config
    )
    assert report.requested_trials == 2
    assert report.completed_trials == 2
    assert report.trials[0].live_error is not None
    assert report.trials[0].task_outcome is None
    assert report.trials[1].task_outcome == "passed"
    assert report.aggregate.errors == 1
    assert report.aggregate.passed == 1
    assert report.aggregate.error_rate == pytest.approx(1 / 2)
    assert report.aggregate.pass_rate == pytest.approx(1 / 2)
    assert calls == {1: 1, 2: 1}
    serialized = json.dumps(report.to_dict(), default=str)
    assert fake_key not in serialized
    assert "[REDACTED]" in (report.trials[0].live_error or "")


async def test_partial_usage_counted_without_masking_zero(tmp_path: Path) -> None:
    from agentcrash.experiments.live_payment_recovery import (
        ModelTrialRecord,
        summarize_trials,
    )

    def _record(index: int, usage: dict, outcome: str | None = "passed") -> ModelTrialRecord:
        return ModelTrialRecord(
            trial_index=index,
            trial_id=f"payment_timeout_after_commit-trial-{index:03d}",
            model=FAKE_MODEL,
            scenario="payment_timeout_after_commit",
            task_outcome=outcome,
            live_error=None,
            order_id=1,
            payment_ids=(1,),
            successful_charge_count=1,
            invariant_violations=(),
            turns_used=1,
            usage=usage,
            trace=(),
        )

    trials = (
        _record(1, {"input_tokens": 10, "output_tokens": None, "total_tokens": None, "responses": 2}),
        _record(2, {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 1}),
    )
    aggregate = summarize_trials(trials, 2)
    assert aggregate.total_input_tokens == 10
    assert aggregate.total_output_tokens == 0
    assert aggregate.total_tokens == 0
    assert aggregate.trials_with_usage == 2
    assert aggregate.trials_partial_usage == 1
    assert aggregate.trials_missing_usage == 0
