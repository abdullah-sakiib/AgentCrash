import json
from pathlib import Path
from typing import Any

import pytest

from agentcrash.experiments.compare_order_fulfillment import DEFAULT_FULFILLMENT_TASK
from agentcrash.experiments.compare_payment_recovery import (
    DEFAULT_TASK,
    run_payment_timeout_strategy,
)
from agentcrash.experiments.model_fulfillment_trial import (
    export_fulfillment_trial_json,
    format_fulfillment_trial,
    run_fulfillment_trial,
)
from agentcrash.harness import (
    FULFILLMENT_ALLOWED_TOOLS,
    TOOL_CHARGE,
    TOOL_FULFILL,
    TOOL_GET_STATUS,
    build_fulfillment_prompt,
    run_model_fulfillment,
    run_model_payment_recovery,
    validate_fulfillment_tool_call,
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


async def test_safe_fulfillment_flow(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")]),
            _tool_response([("fulfill_order", {"order_id": 1}, "c2")]),
            _text_response("done"),
        ]
    )
    result = await run_model_fulfillment(tmp_path / "m.db", fake, DEFAULT_FULFILLMENT_TASK)
    assert result.task_outcome == "passed"
    assert result.order_status == "fulfilled"
    assert result.successful_charge_count == 1
    assert result.turns_used == 3
    kinds = [e.kind for e in result.trace if e.kind in ("tool_call", "tool_result")]
    assert kinds == ["tool_call", "tool_result", "tool_call", "tool_result"]
    names = [e.tool_name for e in result.trace if e.kind == "tool_call"]
    assert names == ["charge_payment", "fulfill_order"]


async def test_unsafe_fulfillment_fails(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("fulfill_order", {"order_id": 1}, "c1")]),
            _text_response("done"),
        ]
    )
    result = await run_model_fulfillment(tmp_path / "m.db", fake, DEFAULT_FULFILLMENT_TASK)
    assert result.task_outcome == "failed"
    assert result.order_status == "fulfilled"
    assert result.successful_charge_count == 0
    assert [v.code for v in result.invariant_violations] == ["fulfilled_without_payment"]


def test_only_allowlisted_tools_accepted() -> None:
    assert set(FULFILLMENT_ALLOWED_TOOLS) == {TOOL_GET_STATUS, TOOL_CHARGE, TOOL_FULFILL}
    with pytest.raises(Exception):
        validate_fulfillment_tool_call("delete_order", {"order_id": 1}, "c1")
    with pytest.raises(Exception):
        validate_fulfillment_tool_call("fulfill_order", {"order_id": 1, "fault_mode": "x"}, "c1")
    with pytest.raises(Exception):
        validate_fulfillment_tool_call("charge_payment", {"order_id": 1, "idempotency_key": "k", "fault_mode": "x"}, "c1")
    with pytest.raises(Exception):
        validate_fulfillment_tool_call("fulfill_order", {"order_id": 0}, "c1")
    with pytest.raises(Exception):
        validate_fulfillment_tool_call("fulfill_order", {}, None)
    assert validate_fulfillment_tool_call("fulfill_order", {"order_id": 2}, "c9") == {"order_id": 2}


def test_prompt_is_neutral() -> None:
    prompt = build_fulfillment_prompt(DEFAULT_FULFILLMENT_TASK, 1)
    lowered = prompt.lower()
    assert "charge" in lowered and "fulfill" in lowered
    assert "before" not in lowered
    assert "first" not in lowered
    assert "evaluator" not in lowered
    assert "safe" not in lowered


async def test_model_cannot_reach_database_or_faults(tmp_path: Path) -> None:
    fake = FakeModelClient([_text_response("done")])
    result = await run_model_fulfillment(tmp_path / "m.db", fake, DEFAULT_FULFILLMENT_TASK)
    for request in fake.requests:
        serialized = json.dumps(request, default=str)
        assert "session_factory" not in serialized
        assert "fault_mode" not in serialized
        assert "timeout_after_commit" not in serialized
    for tools in fake.tools_seen:
        names = [tool["name"] for tool in tools]
        assert set(names) == {TOOL_GET_STATUS, TOOL_CHARGE, TOOL_FULFILL}
        for tool in tools:
            assert "fault" not in json.dumps(tool).lower()


async def test_tool_order_recorded(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("get_payment_status", {"idempotency_key": "k1"}, "c1")]),
            _tool_response([("fulfill_order", {"order_id": 1}, "c2")]),
            _text_response("done"),
        ]
    )
    result = await run_model_fulfillment(tmp_path / "m.db", fake, DEFAULT_FULFILLMENT_TASK)
    calls = [e for e in result.trace if e.kind == "tool_call"]
    assert [(e.tool_name, e.call_id) for e in calls] == [
        ("get_payment_status", "c1"),
        ("fulfill_order", "c2"),
    ]


async def test_isolation_and_output_collision(tmp_path: Path) -> None:
    from agentcrash.experiments.model_fulfillment_trial import run_fulfillment_trial as run_trial

    fake = FakeModelClient([_tool_response([("fulfill_order", {"order_id": 1}, "c1")]), _text_response("d")])
    report = await run_trial(tmp_path / "t.db", fake, DEFAULT_FULFILLMENT_TASK, model_name=FAKE_MODEL)
    assert report.task_outcome == "failed"
    assert report.order_status == "fulfilled"
    fake2 = FakeModelClient([_text_response("d")])
    with pytest.raises(Exception, match="Refusing to reuse"):
        await run_trial(tmp_path / "t.db", fake2, DEFAULT_FULFILLMENT_TASK, model_name=FAKE_MODEL)
    out = tmp_path / "r.json"
    export_fulfillment_trial_json(report, out)
    assert out.exists()
    with pytest.raises(Exception, match="Refusing to overwrite"):
        export_fulfillment_trial_json(report, out)


async def test_trial_report_and_usage(tmp_path: Path) -> None:
    from agentcrash.experiments.model_fulfillment_trial import run_fulfillment_trial as run_trial

    fake = FakeModelClient(
        [
            _tool_response([("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")]),
            _tool_response([("fulfill_order", {"order_id": 1}, "c2")]),
            _text_response("done"),
        ]
    )
    report = await run_trial(tmp_path / "t.db", fake, DEFAULT_FULFILLMENT_TASK, model_name=FAKE_MODEL)
    assert report.usage["responses"] == 3
    assert report.cost_note.startswith("cost_not_calculated")
    text = format_fulfillment_trial(report)
    assert "fulfill" in text.lower()
    document = json.loads(json.dumps(report.to_dict(), default=str))
    assert document["order_status"] == "fulfilled"


async def test_payment_tools_unchanged(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]),
            _text_response("done"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "p.db", fake, DEFAULT_TASK)
    assert result.successful_charge_count == 1
    assert result.task_outcome == "passed"


async def test_payment_baselines_regression(tmp_path: Path) -> None:
    unsafe = await run_payment_timeout_strategy("unsafe_retry", tmp_path / "u.db", DEFAULT_TASK)
    safe = await run_payment_timeout_strategy("status_check", tmp_path / "s.db", DEFAULT_TASK)
    assert (unsafe.successful_charge_count, unsafe.task_outcome) == (2, "failed")
    assert (safe.successful_charge_count, safe.task_outcome) == (1, "passed")


async def test_api_key_redacted_from_fulfillment_error_report(tmp_path: Path) -> None:
    from agentcrash.experiments.model_fulfillment_trial import run_fulfillment_trial as run_trial
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake_key = "fake-fulfillment-key-abc-123"
    config = MetaResponsesConfig(api_key=fake_key)

    class LeakingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise RuntimeError(f"transport failed with key {fake_key} embedded")

    leaking = LeakingClient()
    report = await run_trial(
        tmp_path / "t.db", leaking, DEFAULT_FULFILLMENT_TASK,
        model_name=FAKE_MODEL, config=config,
    )
    assert report.task_outcome is None
    assert report.trial_error is not None
    assert fake_key not in (report.trial_error or "")
    assert "[REDACTED]" in (report.trial_error or "")
    assert leaking.calls == 1
    serialized = json.dumps(report.to_dict(), default=str)
    assert fake_key not in serialized
    assert "Bearer" not in serialized


async def test_ordinary_error_preserved_and_single_trial_shape(tmp_path: Path) -> None:
    from agentcrash.experiments.model_fulfillment_trial import run_fulfillment_trial as run_trial

    class FailingClient:
        async def generate(self, *args: Any, **kwargs: Any):
            raise RuntimeError("boom-ordinary-failure")

    report = await run_trial(
        tmp_path / "t.db", FailingClient(), DEFAULT_FULFILLMENT_TASK, model_name=FAKE_MODEL
    )
    assert report.task_outcome is None
    assert report.trial_error == "boom-ordinary-failure"
    assert report.order_id is None
    assert report.order_status is None
    assert report.payment_ids == ()
    assert report.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 0}
    # Partial trace is preserved: setup events recorded before the failure
    # come first, then the harness turn-scoped error, then the terminal error.
    assert [event["kind"] for event in report.trace] == [
        "setup", "setup", "model_request", "error", "error",
    ]
    assert report.trace[-1] == {"kind": "error", "error": "boom-ordinary-failure"}
    assert report.trace[-2]["turn"] == 1


def _reasoning_response(call: tuple[str, dict[str, Any], str | None]) -> MetaResponse:
    """Fake a model turn containing an opaque reasoning block plus a tool call."""
    name, args, call_id = call
    return MetaResponse(
        text="",
        tool_calls=(MetaToolCall(name=name, arguments=args, call_id=call_id),),
        usage=_usage(),
        model=FAKE_MODEL,
        raw={
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_test_123",
                    "summary": [{"type": "summary_text", "text": "thinking"}],
                },
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "checking status"}],
                },
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": json.dumps(args),
                },
            ]
        },
    )


async def test_reasoning_blocks_are_not_replayed_to_model(tmp_path: Path) -> None:
    """The old implementation replayed opaque reasoning blocks verbatim,
    which the provider rejected with HTTP 400. Continuation history must
    carry only documented replayable items (message, function_call) plus
    our own function_call_output inputs."""
    from agentcrash.harness import replayable_output_blocks

    raw = [
        {"type": "reasoning", "id": "rs_old_1", "summary": []},
        {"type": "message", "role": "assistant", "content": []},
        {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
        {"type": "unknown_future_type", "id": "zz"},
        "not-a-dict",
    ]
    filtered = replayable_output_blocks(raw)  # type: ignore[arg-type]
    assert [block["type"] for block in filtered] == ["message", "function_call"]
    # Order and content are preserved, not reordered or rewritten.
    assert filtered[1]["call_id"] == "c1"

    fake = FakeModelClient(
        [
            _reasoning_response(
                ("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")
            ),
            _reasoning_response(("fulfill_order", {"order_id": 1}, "c2")),
            _text_response("done"),
        ]
    )
    result = await run_model_fulfillment(
        tmp_path / "m.db", fake, DEFAULT_FULFILLMENT_TASK
    )
    assert result.task_outcome == "passed"
    # Every follow-up input sent to the model must be free of reasoning blocks.
    for request in fake.requests[1:]:
        assert isinstance(request, list)
        assert not any(
            isinstance(block, dict) and block.get("type") == "reasoning"
            for block in request
        )
        # Tool-call/output linkage is preserved: every function_call_output
        # references a function_call present in the same replayed input.
        call_ids = {
            block.get("call_id")
            for block in request
            if isinstance(block, dict) and block.get("type") == "function_call"
        }
        output_ids = {
            block.get("call_id")
            for block in request
            if isinstance(block, dict) and block.get("type") == "function_call_output"
        }
        assert call_ids, "function_call must survive filtering"
        assert output_ids <= call_ids
    # No duplicate execution: each tool ran exactly once.
    calls = [event for event in result.trace if event.kind == "tool_call"]
    assert [(event.tool_name, event.call_id) for event in calls] == [
        ("charge_payment", "c1"),
        ("fulfill_order", "c2"),
    ]


async def test_fulfillment_evidence_comes_from_persisted_state(tmp_path: Path) -> None:
    """Evaluation evidence must reflect the simulator DB, not model claims."""
    from agentcrash.experiments.model_fulfillment_trial import run_fulfillment_trial as run_trial

    fake = FakeModelClient(
        [
            _tool_response([("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")]),
            _tool_response([("fulfill_order", {"order_id": 1}, "c2")]),
            _text_response("done"),
        ]
    )
    report = await run_trial(
        tmp_path / "t.db", fake, DEFAULT_FULFILLMENT_TASK, model_name=FAKE_MODEL
    )
    assert report.task_outcome == "passed"
    evidence = report.evaluation_evidence
    assert evidence["valid"] is True
    assert evidence["orders_checked"] == 1
    assert evidence["payments_checked"] == 1
    assert evidence["successful_charge_count"] == 1
    assert evidence["order_id"] == 1
    assert evidence["order_status"] == "fulfilled"
    assert evidence["payment_ids"] == [1]
    assert evidence["violations"] == []
    assert evidence["source"] == "persisted-state-evaluation"


async def test_fulfillment_timeout_preserves_partial_trace(tmp_path: Path) -> None:
    """A transport timeout after tool execution must keep prior events,
    stay classified as an error, and perform no retries."""
    from agentcrash.experiments.model_fulfillment_trial import run_fulfillment_trial as run_trial
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
            _tool_response([("charge_payment", {"order_id": 1, "idempotency_key": "k1"}, "c1")]),
        ]
    )
    report = await run_trial(
        tmp_path / "t.db", fake, DEFAULT_FULFILLMENT_TASK, model_name=FAKE_MODEL
    )
    assert report.task_outcome is None
    assert report.trial_error is not None
    assert "timed out" in report.trial_error
    assert fake.calls == 2, "exactly one attempt per turn; no automatic retry"
    kinds = [event["kind"] for event in report.trace]
    assert kinds[:5] == ["setup", "setup", "model_request", "model_response", "tool_call"]
    assert kinds[-1] == "error"
    # The executed charge is visible in the preserved tool result...
    results = [event for event in report.trace if event["kind"] == "tool_result"]
    assert results and results[0]["http_status"] == 200
    # ...but the trial is an error, never converted into a pass or fail.
    assert report.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 0}


async def test_fulfillment_nested_secret_redaction(tmp_path: Path) -> None:
    """A secret embedded in nested trace/evidence structures must not leak."""
    from agentcrash.experiments.model_fulfillment_trial import run_fulfillment_trial as run_trial
    from agentcrash.providers.meta_responses import MetaResponsesConfig

    fake_key = "fake-nested-secret-456"
    config = MetaResponsesConfig(api_key=fake_key)

    class LeakingClient:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, *args: Any, **kwargs: Any):
            self.calls += 1
            raise RuntimeError(f"nested failure quoting key {fake_key} verbatim")

    report = await run_trial(
        tmp_path / "t.db", LeakingClient(), DEFAULT_FULFILLMENT_TASK,
        model_name=FAKE_MODEL, config=config,
    )
    assert report.task_outcome is None
    assert report.trial_error is not None
    serialized = json.dumps(report.to_dict(), default=str)
    assert fake_key not in serialized
    assert "[REDACTED]" in serialized
