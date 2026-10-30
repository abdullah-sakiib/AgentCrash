import json
from pathlib import Path
from typing import Any

import pytest

from agentcrash.experiments.compare_order_fulfillment import run_fulfillment_comparison
from agentcrash.experiments.compare_order_intent import (
    DEFAULT_ORDER_INTENT_TASK,
    run_order_intent_comparison,
    run_order_intent_strategy,
)
from agentcrash.experiments.compare_payment_recovery import (
    DEFAULT_TASK,
    run_payment_timeout_strategy,
)
from agentcrash.experiments.model_intent_trial import (
    export_intent_trial_json,
    format_intent_trial,
    run_intent_trial,
)
from agentcrash.experiments.unified_report import build_unified_report
from agentcrash.harness import (
    INTENT_ALLOWED_TOOLS,
    TOOL_CREATE_ORDER,
    TOOL_LOOKUP_INTENT,
    build_intent_prompt,
    run_model_fulfillment,
    run_model_intent,
    run_model_payment_recovery,
    validate_intent_tool_call,
)
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
        self.tools_seen: list[Any] = []

    async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
        self.requests.append(user_input)
        self.tools_seen.append(tools)
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


def _tool_response(calls: list[tuple[str, dict[str, Any], str | None]], text: str = "") -> MetaResponse:
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
        text=text, tool_calls=tuple(tool_calls), usage=_usage(), model=FAKE_MODEL,
        raw={"output": blocks},
    )


def _order_body(key: str = "customer-order-7") -> dict[str, Any]:
    return {
        "item": "widget",
        "quantity": 2,
        "unit_price": "12.50",
        "intent_key": key,
    }


async def test_safe_intent_flow(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c1")]),
            _tool_response([("create_order", _order_body(), "c2")]),
            _text_response("done"),
        ]
    )
    result = await run_model_intent(tmp_path / "m.db", fake, DEFAULT_ORDER_INTENT_TASK)
    assert result.task_outcome == "passed"
    assert result.order_ids == (1,)
    assert result.inventory_on_hand == 8
    assert result.turns_used == 3
    kinds = [e.kind for e in result.trace if e.kind in ("tool_call", "tool_result")]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result"]
    names = [e.tool_name for e in result.trace if e.kind == "tool_call"]
    assert names == ["lookup_orders", "create_order"]


async def test_unsafe_intent_flow_fails(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("create_order", _order_body(), "c1")]),
            _tool_response([("create_order", _order_body(), "c2")]),
            _text_response("done"),
        ]
    )
    result = await run_model_intent(tmp_path / "m.db", fake, DEFAULT_ORDER_INTENT_TASK)
    assert result.task_outcome == "failed"
    assert result.order_ids == (1, 2)
    assert result.inventory_on_hand == 6
    assert [v.code for v in result.invariant_violations] == ["duplicate_orders_for_intent"]


def test_only_allowlisted_tools_accepted() -> None:
    assert set(INTENT_ALLOWED_TOOLS) == {TOOL_LOOKUP_INTENT, TOOL_CREATE_ORDER}
    with pytest.raises(Exception):
        validate_intent_tool_call("charge_payment", {"order_id": 1, "idempotency_key": "k"}, "c1")
    with pytest.raises(Exception):
        validate_intent_tool_call("create_order", {**_order_body(), "fault_mode": "x"}, "c1")
    with pytest.raises(Exception):
        validate_intent_tool_call("create_order", {"item": "widget"}, "c1")
    with pytest.raises(Exception):
        validate_intent_tool_call("lookup_orders", {"intent_key": "  "}, "c1")
    with pytest.raises(Exception):
        validate_intent_tool_call("lookup_orders", {"intent_key": "k"}, None)
    assert validate_intent_tool_call("lookup_orders", {"intent_key": "k"}, "c9") == {"intent_key": "k"}


def test_prompt_is_neutral() -> None:
    prompt = build_intent_prompt(DEFAULT_ORDER_INTENT_TASK)
    lowered = prompt.lower()
    assert "lookup" in lowered and "create" in lowered
    assert "first" not in lowered
    assert "before creating" not in lowered
    assert "evaluator" not in lowered
    assert "safe" not in lowered
    assert "duplicate" not in lowered


async def test_model_cannot_reach_database_or_faults(tmp_path: Path) -> None:
    fake = FakeModelClient([_text_response("done")])
    result = await run_model_intent(tmp_path / "m.db", fake, DEFAULT_ORDER_INTENT_TASK)
    assert result.task_outcome == "passed"
    for request in fake.requests:
        serialized = json.dumps(request, default=str)
        assert "session_factory" not in serialized
        assert "fault_mode" not in serialized
        assert "timeout_after_commit" not in serialized
    for tools in fake.tools_seen:
        names = [tool["name"] for tool in tools]
        assert set(names) == {TOOL_LOOKUP_INTENT, TOOL_CREATE_ORDER}
        for tool in tools:
            assert "fault" not in json.dumps(tool).lower()


async def test_tool_order_recorded(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("create_order", _order_body(), "c1")]),
            _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c2")]),
            _text_response("done"),
        ]
    )
    result = await run_model_intent(tmp_path / "m.db", fake, DEFAULT_ORDER_INTENT_TASK)
    calls = [e for e in result.trace if e.kind == "tool_call"]
    assert [(e.tool_name, e.call_id) for e in calls] == [
        ("create_order", "c1"),
        ("lookup_orders", "c2"),
    ]


async def test_lookup_does_not_hide_duplicates(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("create_order", _order_body(), "c1")]),
            _tool_response([("create_order", _order_body(), "c2")]),
            _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c3")]),
            _text_response("all good, one order exists"),
        ]
    )
    result = await run_model_intent(tmp_path / "m.db", fake, DEFAULT_ORDER_INTENT_TASK)
    lookups = [e for e in result.trace if e.tool_name == "lookup_orders" and e.kind == "tool_result"]
    assert lookups and lookups[0].result is not None
    assert lookups[0].result["order"]["id"] == 1
    assert result.task_outcome == "failed"
    assert [v.code for v in result.invariant_violations] == ["duplicate_orders_for_intent"]


async def test_tool_error_recorded_and_trial_continues(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("create_order", {"item": "", "quantity": 0, "unit_price": "", "intent_key": ""}, "c1")]),
            _tool_response([("create_order", _order_body(), "c2")]),
            _text_response("done"),
        ]
    )
    result = await run_model_intent(tmp_path / "m.db", fake, DEFAULT_ORDER_INTENT_TASK)
    rejected = [e for e in result.trace if e.kind == "tool_rejected"]
    assert rejected
    assert result.task_outcome == "passed"
    assert result.order_ids == (1,)


async def test_isolation_and_output_collision(tmp_path: Path) -> None:
    from agentcrash.experiments.model_intent_trial import run_intent_trial as run_trial

    fake = FakeModelClient(
        [
            _tool_response([("create_order", _order_body(), "c1")]),
            _tool_response([("create_order", _order_body(), "c2")]),
            _text_response("done"),
        ]
    )
    report = await run_trial(tmp_path / "t.db", fake, DEFAULT_ORDER_INTENT_TASK, model_name=FAKE_MODEL)
    assert report.task_outcome == "failed"
    assert report.order_ids == (1, 2)
    assert report.inventory_on_hand == 6
    fake2 = FakeModelClient([_text_response("done")])
    with pytest.raises(Exception, match="Refusing to reuse"):
        await run_trial(tmp_path / "t.db", fake2, DEFAULT_ORDER_INTENT_TASK, model_name=FAKE_MODEL)
    out = tmp_path / "r.json"
    export_intent_trial_json(report, out)
    assert out.exists()
    with pytest.raises(Exception, match="Refusing to overwrite"):
        export_intent_trial_json(report, out)


async def test_trial_report_usage_and_errors(tmp_path: Path) -> None:
    from agentcrash.experiments.model_intent_trial import run_intent_trial as run_trial
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake = FakeModelClient(
        [
            _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c1")]),
            _tool_response([("create_order", _order_body(), "c2")]),
            _text_response("done"),
        ]
    )
    report = await run_trial(tmp_path / "t.db", fake, DEFAULT_ORDER_INTENT_TASK, model_name=FAKE_MODEL)
    assert report.usage["responses"] == 3
    assert report.cost_note.startswith("cost_not_calculated")
    text = format_intent_trial(report)
    assert "intent" in text.lower()
    document = json.loads(json.dumps(report.to_dict(), default=str))
    assert document["inventory_on_hand"] == 8

    fake_key = "fake-intent-key-xyz-123"
    config = MetaResponsesConfig(api_key=fake_key)

    class LeakingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise RuntimeError(f"transport failed with key {fake_key} embedded")

    leaking = LeakingClient()
    errored = await run_trial(
        tmp_path / "e.db", leaking, DEFAULT_ORDER_INTENT_TASK, model_name=FAKE_MODEL, config=config
    )
    assert errored.task_outcome is None
    assert fake_key not in (errored.trial_error or "")
    assert "[REDACTED]" in (errored.trial_error or "")
    assert leaking.calls == 1
    assert json.dumps(errored.to_dict(), default=str).find(fake_key) == -1


async def test_payment_and_fulfillment_unchanged(tmp_path: Path) -> None:
    safe_pay = FakeModelClient(
        [
            _tool_response([("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]),
            _text_response("done"),
        ]
    )
    payment = await run_model_payment_recovery(tmp_path / "p.db", safe_pay, DEFAULT_TASK)
    assert payment.task_outcome == "passed"
    assert payment.successful_charge_count == 1


async def test_deterministic_baselines_regression(tmp_path: Path) -> None:
    unsafe = await run_order_intent_strategy("unsafe_duplicate", tmp_path / "u.db", DEFAULT_ORDER_INTENT_TASK)
    safe = await run_order_intent_strategy("safe_deduplicated", tmp_path / "s.db", DEFAULT_ORDER_INTENT_TASK)
    assert (unsafe.order_ids, unsafe.task_outcome) == ((1, 2), "failed")
    assert (safe.order_ids, safe.task_outcome) == ((1,), "passed")
    payment = await run_payment_timeout_strategy("unsafe_retry", tmp_path / "pay.db", DEFAULT_TASK)
    assert payment.task_outcome == "failed"


async def test_unified_report_accepts_intent_comparison(tmp_path: Path) -> None:
    comparison = await run_order_intent_comparison(tmp_path)
    unified = build_unified_report([("intent.json", comparison.to_dict())])
    assert unified["scenarios"][0]["kind"] == "deterministic_comparison"
    assert unified["scenarios"][0]["scenario"] == "duplicate_order_for_intent"
    assert unified["scenarios"][0]["duplicate_charge_trials"] is None
    assert unified["scenarios"][0]["usage"]["status"] == "missing"
    assert unified["aggregate"]["violation_counts"] == {"duplicate_orders_for_intent": 1}
    assert unified["aggregate"]["total_tokens"] == 0
    text = json.dumps(unified)
    assert "duplicate_order_for_intent" in text


async def test_intent_evidence_comes_from_persisted_state(tmp_path: Path) -> None:
    """Evaluation evidence must reflect the simulator DB, not model claims."""
    from agentcrash.experiments.model_intent_trial import run_intent_trial as run_trial

    fake = FakeModelClient(
        [
            _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c1")]),
            _tool_response([("create_order", _order_body(), "c2")]),
            _text_response("done"),
        ]
    )
    report = await run_trial(
        tmp_path / "t.db", fake, DEFAULT_ORDER_INTENT_TASK, model_name=FAKE_MODEL
    )
    assert report.task_outcome == "passed"
    evidence = report.evaluation_evidence
    assert evidence["valid"] is True
    assert evidence["orders_checked"] == 1
    assert evidence["payments_checked"] == 0
    assert evidence["order_ids"] == [1]
    assert evidence["inventory_on_hand"] == 8
    assert evidence["violations"] == []
    assert evidence["source"] == "persisted-state-evaluation"


async def test_intent_error_preserves_partial_trace(tmp_path: Path) -> None:
    """A timeout after a lookup must keep prior events and stay an error."""
    from agentcrash.experiments.model_intent_trial import run_intent_trial as run_trial
    from agentcrash.providers.meta_responses import MetaTimeoutError

    class TimeoutAfterFirstTool:
        def __init__(self, responses: list[MetaResponse]) -> None:
            self._responses = list(responses)
            self.calls = 0

        async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
            self.calls += 1
            if self.calls > 1:
                raise MetaTimeoutError("Meta request timed out after 30.0s.")
            assert self._responses
            return self._responses.pop(0)

    fake = TimeoutAfterFirstTool(
        [
            _tool_response([("lookup_orders", {"intent_key": "customer-order-7"}, "c1")]),
        ]
    )
    report = await run_trial(
        tmp_path / "t.db", fake, DEFAULT_ORDER_INTENT_TASK, model_name=FAKE_MODEL
    )
    assert report.task_outcome is None
    assert report.trial_error is not None
    assert "timed out" in report.trial_error
    assert fake.calls == 2, "exactly one attempt per turn; no automatic retry"
    kinds = [event["kind"] for event in report.trace]
    assert kinds[:4] == ["setup", "model_request", "model_response", "tool_call"]
    assert kinds[-1] == "error"
    lookups = [event for event in report.trace if event["kind"] == "tool_result"]
    assert lookups and lookups[0]["http_status"] == 404
    assert report.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 0}


async def test_intent_nested_secret_redaction(tmp_path: Path) -> None:
    """A secret embedded in nested trace structures must not leak."""
    from agentcrash.experiments.model_intent_trial import run_intent_trial as run_trial
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake_key = "fake-intent-nested-secret-789"
    config = MetaResponsesConfig(api_key=fake_key)

    class LeakingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise RuntimeError(f"nested failure quoting key {fake_key} verbatim")

    report = await run_trial(
        tmp_path / "t.db", LeakingClient(), DEFAULT_ORDER_INTENT_TASK,
        model_name=FAKE_MODEL, config=config,
    )
    assert report.task_outcome is None
    assert report.trial_error is not None
    serialized = json.dumps(report.to_dict(), default=str)
    assert fake_key not in serialized
    assert "[REDACTED]" in serialized
