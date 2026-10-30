import json
from pathlib import Path
from typing import Any

import pytest

from agentcrash.experiments.compare_order_fulfillment import DEFAULT_FULFILLMENT_TASK
from agentcrash.experiments.compare_order_intent import DEFAULT_ORDER_INTENT_TASK
from agentcrash.experiments.compare_payment_recovery import DEFAULT_TASK
from agentcrash.experiments.run_experiment import (
    export_orchestrated_report,
    format_orchestrated_report,
    main,
    run_orchestrated_experiment,
)
from agentcrash.experiments.unified_report import build_unified_report
from agentcrash.providers.meta_responses import (
    MetaResponse,
    MetaToolCall,
    MetaUsage,
)

FAKE_MODEL = "fake-model"


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


def _usage(i: int | None = 10, o: int | None = 5) -> MetaUsage:
    total = (i or 0) + (o or 0) if i is not None and o is not None else None
    return MetaUsage(input_tokens=i, output_tokens=o, total_tokens=total, raw={})


def _text_response(text: str, usage: MetaUsage | None = None) -> MetaResponse:
    return MetaResponse(
        text=text,
        tool_calls=(),
        usage=usage if usage is not None else _usage(),
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


def _tool_response(calls: list[tuple[str, dict[str, Any], str | None]], text: str = "", usage: MetaUsage | None = None) -> MetaResponse:
    blocks: list[dict[str, Any]] = []
    tool_calls: list[MetaToolCall] = []
    if text:
        blocks.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for name, args, call_id in calls:
        blocks.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(args),
            }
        )
        tool_calls.append(MetaToolCall(name=name, arguments=args, call_id=call_id))
    return MetaResponse(
        text=text, tool_calls=tuple(tool_calls),
        usage=usage if usage is not None else _usage(), model=FAKE_MODEL,
        raw={"output": blocks},
    )


def _status_response(call_id: str = "c1") -> MetaResponse:
    return _tool_response(
        [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, call_id)]
    )


def _intent_create(key: str = "customer-order-7", call_id: str = "c2") -> MetaResponse:
    return _tool_response(
        [("create_order", {
            "item": "widget", "quantity": 2, "unit_price": "12.50", "intent_key": key,
        }, call_id)]
    )


async def test_deterministic_default_needs_no_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("AGENTCRASH_META_API_KEY", raising=False)
    report = await run_orchestrated_experiment(
        tmp_path, ["payment-timeout", "fulfill-before-payment", "duplicate-order-for-intent"],
        mode="deterministic", trial_count=1, max_turns=6,
    )
    assert report.mode == "deterministic"
    assert report.model is None
    assert report.scenarios_requested == 3
    assert report.scenarios_completed == 3
    by_key = {entry.scenario_key: entry for entry in report.scenarios}
    assert by_key["payment-timeout"].passed == 1
    assert by_key["payment-timeout"].failed == 1
    assert by_key["fulfill-before-payment"].passed == 1
    assert by_key["duplicate-order-for-intent"].passed == 1


async def test_scenario_selection_subset(tmp_path: Path) -> None:
    report = await run_orchestrated_experiment(
        tmp_path, ["duplicate-order-for-intent"],
        mode="deterministic", trial_count=1, max_turns=6,
    )
    assert [entry.scenario_key for entry in report.scenarios] == ["duplicate-order-for-intent"]
    assert report.aggregate["trials_completed"] == 2


async def test_invalid_inputs_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await run_orchestrated_experiment(tmp_path, ["nope"], mode="deterministic", trial_count=1, max_turns=6)
    with pytest.raises(ValueError):
        await run_orchestrated_experiment(tmp_path, [], mode="deterministic", trial_count=1, max_turns=6)
    with pytest.raises(ValueError):
        await run_orchestrated_experiment(tmp_path, ["payment-timeout"], mode="deterministic", trial_count=0, max_turns=6)
    with pytest.raises(ValueError):
        await run_orchestrated_experiment(tmp_path, ["payment-timeout"], mode="bogus", trial_count=1, max_turns=6)
    with pytest.raises(ValueError):
        await run_orchestrated_experiment(tmp_path, ["payment-timeout"], mode="deterministic", trial_count=3, max_turns=6)
    with pytest.raises(ValueError):
        await run_orchestrated_experiment(
            tmp_path, ["payment-timeout"], mode="live", trial_count=1, max_turns=6,
            model_name=FAKE_MODEL,
        )
    with pytest.raises(ValueError):
        await run_orchestrated_experiment(
            tmp_path, ["payment-timeout"], mode="live", trial_count=1, max_turns=6,
            model_client=FakeModelClient([_text_response("x")]),
            model_name=FAKE_MODEL,
        )


def test_cli_rejects_live_without_opt_in(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--mode", "live", "--trials", "1", "--output", str(tmp_path / "r.json")])
    with pytest.raises(SystemExit):
        main(["--live", "--output", str(tmp_path / "r.json")])


def test_cli_rejects_bad_counts(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--trials", "0", "--output", str(tmp_path / "r.json")])
    with pytest.raises(SystemExit):
        main(["--trials", "3", "--output", str(tmp_path / "r.json")])


def test_cli_refuses_existing_output(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    out.write_text("original", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--scenario", "payment-timeout", "--output", str(out)])
    assert out.read_text(encoding="utf-8") == "original"


async def test_live_payment_trials_isolated_and_counted(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    def _factory(trial_index: int) -> FakeModelClient:
        return FakeModelClient([_status_response(), _text_response("done")])

    config = MetaResponsesConfig(api_key="fake-key")
    report = await run_orchestrated_experiment(
        tmp_path, ["payment-timeout"], mode="live", trial_count=2, max_turns=6,
        model_client_factory=_factory, model_name=FAKE_MODEL, config=config,
    )
    entry = report.scenarios[0]
    assert entry.trials_requested == 2
    assert entry.trials_completed == 2
    assert entry.passed == 2
    assert entry.errors == 0
    assert [trial.payment_ids for trial in entry.trials] == [(1,), (1,)]
    assert (tmp_path / "payment-timeout" / "trial_001" / "model_recovery.db").exists()
    assert (tmp_path / "payment-timeout" / "trial_002" / "model_recovery.db").exists()
    assert report.aggregate["trials_completed"] == 2
    assert report.aggregate["pass_rate"] == pytest.approx(1.0)


async def test_live_error_recorded_without_retry_and_batch_continues(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    calls: dict[int, int] = {}
    factory_calls: list[int] = []

    def _factory(trial_index: int):
        factory_calls.append(trial_index)
        calls[trial_index] = calls.get(trial_index, 0)

        class FailingClient:
            async def generate(self, *args: Any, **kwargs: Any):
                calls[trial_index] += 1
                raise RuntimeError("boom")

        if trial_index == 2:
            return FakeModelClient([_status_response(), _text_response("ok")])
        return FailingClient()

    config = MetaResponsesConfig(api_key="fake-key")
    report = await run_orchestrated_experiment(
        tmp_path, ["payment-timeout"], mode="live", trial_count=2, max_turns=6,
        model_client_factory=_factory, model_name=FAKE_MODEL, config=config,
    )
    entry = report.scenarios[0]
    assert entry.errors == 1
    assert entry.passed == 1
    assert factory_calls == [1, 2]
    assert calls == {1: 1, 2: 0}
    assert report.aggregate["errors"] == 1
    assert len(report.aggregate["incomplete_trials"]) == 1


async def test_live_intent_safe_and_unsafe(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    config = MetaResponsesConfig(api_key="fake-key")
    safe = FakeModelClient([
        _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c1")]),
        _intent_create(),
        _text_response("done"),
    ])
    report = await run_orchestrated_experiment(
        tmp_path / "safe", ["duplicate-order-for-intent"], mode="live",
        trial_count=1, max_turns=6, model_client=safe, model_name=FAKE_MODEL, config=config,
    )
    entry = report.scenarios[0]
    assert entry.passed == 1
    assert entry.trials[0].order_ids == (1,)

    unsafe = FakeModelClient([
        _intent_create(call_id="c1"),
        _intent_create(call_id="c2"),
        _text_response("done"),
    ])
    report2 = await run_orchestrated_experiment(
        tmp_path / "unsafe", ["duplicate-order-for-intent"], mode="live",
        trial_count=1, max_turns=6, model_client=unsafe, model_name=FAKE_MODEL, config=config,
    )
    assert report2.scenarios[0].failed == 1
    assert report2.scenarios[0].trials[0].order_ids == (1, 2)
    assert report2.aggregate["violation_counts"] == {"duplicate_orders_for_intent": 1}


async def test_usage_missing_partial_zero(tmp_path: Path) -> None:
    from agentcrash.experiments.run_experiment import _trial_usage
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    missing = _trial_usage(0, 0, 0, 0)
    assert missing["status"] == "missing"
    assert missing["missing_usage"] == 1
    partial = _trial_usage(10, None, None, 2)
    assert partial["status"] == "partial"
    assert partial["input_tokens"] == 10
    assert partial["output_tokens"] == 0
    assert partial["partial_usage"] == 1
    zero = _trial_usage(0, 0, 0, 1)
    assert zero["status"] == "complete"

    config = MetaResponsesConfig(api_key="fake-key")

    class FailingClient:
        async def generate(self, *args: Any, **kwargs: Any):
            raise RuntimeError("nope")

    report = await run_orchestrated_experiment(
        tmp_path, ["duplicate-order-for-intent"], mode="live",
        trial_count=1, max_turns=6, model_client=FailingClient(),
        model_name=FAKE_MODEL, config=config,
    )
    trial = report.scenarios[0].trials[0]
    assert trial.error is not None
    assert trial.usage["status"] == "missing"
    assert report.aggregate["total_input_tokens"] == 0


async def test_error_redaction_in_orchestration(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake_key = "fake-orchestration-key-456"
    config = MetaResponsesConfig(api_key=fake_key)

    class LeakingClient:
        async def generate(self, *args: Any, **kwargs: Any):
            raise RuntimeError(f"transport failed with key {fake_key} embedded")

    report = await run_orchestrated_experiment(
        tmp_path, ["duplicate-order-for-intent"], mode="live",
        trial_count=1, max_turns=6, model_client=LeakingClient(),
        model_name=FAKE_MODEL, config=config,
    )
    serialized = json.dumps(report.to_dict(), default=str)
    assert fake_key not in serialized
    assert "[REDACTED]" in serialized


async def test_existing_database_refused(tmp_path: Path) -> None:
    from agentcrash.experiments.compare_payment_recovery import ExperimentExecutionError

    target = tmp_path / "payment-timeout" / "unsafe_retry.db"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    with pytest.raises(ExperimentExecutionError):
        await run_orchestrated_experiment(
            tmp_path, ["payment-timeout"], mode="deterministic", trial_count=1, max_turns=6,
        )


async def test_deterministic_reports_are_stable(tmp_path: Path) -> None:
    first = await run_orchestrated_experiment(
        tmp_path / "a", ["payment-timeout", "duplicate-order-for-intent"],
        mode="deterministic", trial_count=1, max_turns=6,
    )
    second = await run_orchestrated_experiment(
        tmp_path / "b", ["payment-timeout", "duplicate-order-for-intent"],
        mode="deterministic", trial_count=1, max_turns=6,
    )
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)


def test_unified_report_consumes_orchestrated_output(tmp_path: Path) -> None:
    import asyncio

    report = asyncio.run(run_orchestrated_experiment(
        tmp_path / "db", ["duplicate-order-for-intent"],
        mode="deterministic", trial_count=1, max_turns=6,
    ))
    unified = build_unified_report([("orchestrated.json", report.to_dict())])
    assert unified["scenarios"][0]["kind"] == "orchestrated_run"
    assert "duplicate_order_for_intent" in json.dumps(unified)


def test_unified_report_consumes_intent_single_trial() -> None:
    doc = {
        "scenario": "duplicate_order_for_intent",
        "model": "fake-model",
        "task": {"item": "widget"},
        "task_outcome": "passed",
        "trial_error": None,
        "order_ids": [1],
        "inventory_on_hand": 8,
        "payment_ids": [],
        "successful_charge_count": 0,
        "invariant_violations": [],
        "turns_used": 2,
        "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10, "responses": 1},
        "cost_note": "cost_not_calculated (no reliable pricing data)",
        "trace": [],
        "baselines": [],
    }
    unified = build_unified_report([("intent.json", doc)])
    assert unified["scenarios"][0]["kind"] == "intent_trial"
    assert unified["scenarios"][0]["passed"] == 1


def test_cli_deterministic_all_scenarios_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("AGENTCRASH_META_API_KEY", raising=False)
    out = tmp_path / "orchestrated.json"
    main(["--output", str(out)])
    assert out.exists()
    text = capsys.readouterr().out
    assert "orchestrated experiment (deterministic)" in text
    assert "JSON report" in text


async def test_live_single_trial_unexpected_raise_preserves_prior_trials(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig
    from agentcrash.experiments import run_experiment as run_mod

    fake_key = "fake-unexpected-key-789"
    config = MetaResponsesConfig(api_key=fake_key)
    real_run_single = run_mod.run_fulfillment_trial

    async def _always_raise(database_path, model_client, task, *, max_turns=6, model_name=None, config=None):
        if "trial_002" in str(database_path):
            raise RuntimeError(f"infra boom with {fake_key} inside")
        return await real_run_single(
            database_path, model_client, task,
            max_turns=max_turns, model_name=model_name, config=config,
        )

    monkeypatch2 = pytest.MonkeyPatch()
    monkeypatch2.setattr(run_mod, "run_fulfillment_trial", _always_raise)
    try:
        good2 = FakeModelClient([
            _tool_response([("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")]),
            _tool_response([("fulfill_order", {"order_id": 1}, "c2")]),
            _text_response("done"),
        ])
        report2 = await run_orchestrated_experiment(
            tmp_path / "second", ["fulfill-before-payment"], mode="live",
            trial_count=2, max_turns=6, model_client=good2,
            model_name=FAKE_MODEL, config=config,
        )
    finally:
        monkeypatch2.undo()
    assert report2.scenarios[0].trials_completed == 2
    assert report2.scenarios[0].trials[0].error is None
    assert report2.scenarios[0].trials[0].outcome == "passed"
    assert report2.scenarios[0].trials[1].error is not None
    assert fake_key not in (report2.scenarios[0].trials[1].error or "")
    assert "[REDACTED]" in (report2.scenarios[0].trials[1].error or "")
    assert report2.scenarios[0].errors == 1
    assert report2.scenarios[0].passed == 1
    assert report2.aggregate["errors"] == 1
    assert report2.aggregate["passed"] == 1
    assert len(report2.aggregate["incomplete_trials"]) == 1
    serialized = json.dumps(report2.to_dict(), default=str)
    assert fake_key not in serialized


async def test_live_intent_unexpected_raise_preserves_prior_trials(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig
    from agentcrash.experiments import run_experiment as run_mod

    fake_key = "fake-intent-unexpected-key-321"
    config = MetaResponsesConfig(api_key=fake_key)
    real_run_single = run_mod.run_intent_trial

    async def _flaky_run_single(database_path, model_client, task, *, max_turns=6, model_name=None, config=None):
        if "trial_002" in str(database_path):
            raise RuntimeError(f"intent infra boom {fake_key}")
        return await real_run_single(
            database_path, model_client, task,
            max_turns=max_turns, model_name=model_name, config=config,
        )

    monkeypatch_run = pytest.MonkeyPatch()
    monkeypatch_run.setattr(run_mod, "run_intent_trial", _flaky_run_single)
    try:
        good = FakeModelClient([
            _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c1")]),
            _intent_create(),
            _text_response("done"),
        ])
        report = await run_orchestrated_experiment(
            tmp_path, ["duplicate-order-for-intent"], mode="live",
            trial_count=2, max_turns=6, model_client=good,
            model_name=FAKE_MODEL, config=config,
        )
    finally:
        monkeypatch_run.undo()
    assert report.scenarios[0].trials_completed == 2
    assert report.scenarios[0].trials[0].error is None
    assert report.scenarios[0].trials[0].outcome == "passed"
    assert report.scenarios[0].trials[1].error is not None
    assert fake_key not in (report.scenarios[0].trials[1].error or "")
    assert "[REDACTED]" in (report.scenarios[0].trials[1].error or "")
    assert report.scenarios[0].errors == 1
    assert report.scenarios[0].passed == 1
    assert report.aggregate["trials_completed"] == 2
    serialized = json.dumps(report.to_dict(), default=str)
    assert fake_key not in serialized


async def test_live_payment_evidence_preserved_and_redacted(tmp_path: Path) -> None:
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake_key = "fake-evidence-key-999"
    config = MetaResponsesConfig(api_key=fake_key)

    def _factory(trial_index: int) -> FakeModelClient:
        return FakeModelClient([_status_response(), _text_response("done")])

    report = await run_orchestrated_experiment(
        tmp_path, ["payment-timeout"], mode="live",
        trial_count=1, max_turns=6, model_client_factory=_factory,
        model_name=FAKE_MODEL, config=config,
    )
    trial = report.scenarios[0].trials[0]
    assert trial.error is None
    assert trial.outcome == "passed"
    kinds = [event.get("kind") for event in trial.trace]
    assert "model_request" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    calls = [event for event in trial.trace if event.get("kind") == "tool_call"]
    assert calls and calls[0]["tool_name"] == "get_payment_status"
    assert calls[0]["call_id"]
    assert calls[0]["arguments"] == {"idempotency_key": "payment-timeout-original"}
    results = [event for event in trial.trace if event.get("kind") == "tool_result"]
    assert results and results[0]["http_status"] == 200
    assert results[0]["result"]["status"] == "succeeded"
    sequence = [event.get("sequence") for event in trial.trace]
    assert sequence == sorted(sequence)
    evidence = trial.evaluation_evidence
    assert evidence["valid"] is True
    assert evidence["successful_charge_count"] == 1
    assert evidence["payment_ids"] == [1]
    assert evidence["source"] == "persisted-state-evaluation"
    serialized = json.dumps(report.to_dict(), default=str)
    assert "Bearer" not in serialized
    assert "Authorization" not in serialized
    assert fake_key not in serialized


async def test_live_fulfillment_evidence_and_trace_preserved(tmp_path: Path) -> None:
    """Fulfillment live trials must carry trace + persisted-state evidence."""
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    config = MetaResponsesConfig(api_key="fake-key")
    safe = FakeModelClient([
        _tool_response([("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")]),
        _tool_response([("fulfill_order", {"order_id": 1}, "c2")]),
        _text_response("done"),
    ])
    report = await run_orchestrated_experiment(
        tmp_path, ["fulfill-before-payment"], mode="live",
        trial_count=1, max_turns=6, model_client=safe,
        model_name=FAKE_MODEL, config=config,
    )
    trial = report.scenarios[0].trials[0]
    assert trial.error is None
    assert trial.outcome == "passed"
    kinds = [event.get("kind") for event in trial.trace]
    assert "tool_call" in kinds and "tool_result" in kinds
    names = [event.get("tool_name") for event in trial.trace if event.get("kind") == "tool_call"]
    assert names == ["charge_payment", "fulfill_order"]
    evidence = trial.evaluation_evidence
    assert evidence["valid"] is True
    assert evidence["orders_checked"] == 1
    assert evidence["payments_checked"] == 1
    assert evidence["successful_charge_count"] == 1
    assert evidence["payment_ids"] == [1]
    assert evidence["order_status"] == "fulfilled"
    assert evidence["source"] == "persisted-state-evaluation"


async def test_live_intent_evidence_and_trace_preserved(tmp_path: Path) -> None:
    """Intent live trials must carry trace + persisted-state evidence."""
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    config = MetaResponsesConfig(api_key="fake-key")
    safe = FakeModelClient([
        _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c1")]),
        _intent_create(),
        _text_response("done"),
    ])
    report = await run_orchestrated_experiment(
        tmp_path, ["duplicate-order-for-intent"], mode="live",
        trial_count=1, max_turns=6, model_client=safe,
        model_name=FAKE_MODEL, config=config,
    )
    trial = report.scenarios[0].trials[0]
    assert trial.error is None
    assert trial.outcome == "passed"
    assert trial.order_ids == (1,)
    evidence = trial.evaluation_evidence
    assert evidence["valid"] is True
    assert evidence["orders_checked"] == 1
    assert evidence["order_ids"] == [1]
    assert evidence["inventory_on_hand"] == 8
    assert evidence["source"] == "persisted-state-evaluation"


async def test_orchestrated_error_keeps_partial_trace(tmp_path: Path) -> None:
    """Orchestrated error trials preserve pre-failure events, stay errors.

    The model client fails on turn 2 after a tool already executed: the
    error report must retain those tool events (not just a bare error),
    stay classified as an error, and perform no retries.
    """
    from agentcrash.providers.meta_responses import MetaResponsesConfig, MetaTimeoutError

    config = MetaResponsesConfig(api_key="fake-key")

    class TimeoutAfterThreeTurns:
        """Serves trial 1 fully, then fails trial 2 after one tool call."""

        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
            self.calls += 1
            if self.calls == 5:
                raise MetaTimeoutError("Meta request timed out after 30.0s.")
            if self.calls in (1, 4):
                return _tool_response(
                    [("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")]
                )
            if self.calls == 2:
                return _tool_response([("fulfill_order", {"order_id": 1}, "c2")])
            return _text_response("done")

    client = TimeoutAfterThreeTurns()
    report = await run_orchestrated_experiment(
        tmp_path, ["fulfill-before-payment"], mode="live",
        trial_count=2, max_turns=6, model_client=client,
        model_name=FAKE_MODEL, config=config,
    )
    entry = report.scenarios[0]
    assert entry.trials_completed == 2
    assert entry.trials[0].outcome == "passed"
    assert entry.trials[0].error is None
    failed = entry.trials[1]
    assert failed.error is not None
    assert "timed out" in failed.error
    assert failed.outcome is None
    failed_kinds = [event.get("kind") for event in failed.trace]
    assert "tool_call" in failed_kinds, "pre-failure tool events must survive"
    assert "tool_result" in failed_kinds
    assert failed_kinds[-1] == "error"
    assert failed.usage["responses"] == 0
    assert failed.usage["status"] == "missing"
    assert client.calls == 5, "one attempt per turn; no automatic retry"
    assert report.aggregate["errors"] == 1
    assert report.aggregate["passed"] == 1
    assert len(report.aggregate["incomplete_trials"]) == 1
