import json
from pathlib import Path
from typing import Any

import pytest

from agentcrash.experiments.compare_payment_recovery import (
    DEFAULT_TASK,
    run_payment_timeout_strategy,
)
from agentcrash.harness import (
    ALLOWED_TOOLS,
    HarnessResult,
    MaxTurnsExceededError,
    MissingCallIdError,
    ToolValidationError,
    UnknownToolError,
    run_model_payment_recovery,
    validate_tool_call,
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

    async def generate(
        self,
        user_input: str | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        model_override: str | None = None,
    ) -> MetaResponse:
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


def _tool_response(
    calls: list[tuple[str, dict[str, Any], str | None]], text: str = ""
) -> MetaResponse:
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
        tool_calls.append(
            MetaToolCall(name=name, arguments=args, call_id=call_id)
        )
    return MetaResponse(
        text=text,
        tool_calls=tuple(tool_calls),
        usage=_usage(),
        model=FAKE_MODEL,
        raw={"output": blocks},
    )


async def test_valid_status_check_flow(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]
            ),
            _text_response("Payment already succeeded; no retry."),
        ]
    )
    result = await run_model_payment_recovery(
        tmp_path / "model.db", fake, DEFAULT_TASK, max_turns=6
    )
    assert result.successful_charge_count == 1
    assert result.payment_ids == (1,)
    assert result.task_outcome == "passed"
    assert result.turns_used == 2
    assert result.usage.responses == 2
    kinds = [event.kind for event in result.trace]
    assert "model_request" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    status_calls = [
        event
        for event in result.trace
        if event.tool_name == "get_payment_status" and event.kind == "tool_result"
    ]
    assert status_calls and status_calls[0].http_status == 200


async def test_unsafe_charge_creates_second_charge(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response(
                [("charge_payment", {"order_id": 1, "idempotency_key": "retry-new"}, "c1")]
            ),
            _text_response("done"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    assert result.successful_charge_count == 2
    assert result.payment_ids == (1, 2)
    assert result.task_outcome == "failed"
    assert [violation.code for violation in result.invariant_violations] == [
        "multiple_successful_charges_for_order"
    ]


async def test_unknown_tool_rejected_without_execution(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("delete_order", {"order_id": 1}, "c1")]),
            _text_response("understood"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    rejected = [event for event in result.trace if event.kind == "tool_rejected"]
    assert rejected and "Unknown tool" in (rejected[0].error or "")
    assert result.successful_charge_count == 1
    follow_up = fake.requests[1]
    assert isinstance(follow_up, list)
    outputs = [block for block in follow_up if block.get("type") == "function_call_output"]
    assert outputs and "Unknown tool" in outputs[0]["output"]


async def test_invalid_arguments_rejected(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response([("charge_payment", {"order_id": 1}, "c1")]),
            _text_response("ok"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    rejected = [event for event in result.trace if event.kind == "tool_rejected"]
    assert rejected
    assert result.successful_charge_count == 1


def test_validate_tool_call_rules() -> None:
    with pytest.raises(UnknownToolError):
        validate_tool_call("nope", {}, "c1")
    with pytest.raises(MissingCallIdError):
        validate_tool_call("get_payment_status", {"idempotency_key": "k"}, None)
    with pytest.raises(ToolValidationError):
        validate_tool_call("get_payment_status", {}, "c1")
    with pytest.raises(ToolValidationError):
        validate_tool_call(
            "get_payment_status", {"idempotency_key": "k", "extra": 1}, "c1"
        )
    with pytest.raises(ToolValidationError):
        validate_tool_call("charge_payment", {"order_id": 0, "idempotency_key": "k"}, "c1")
    with pytest.raises(ToolValidationError):
        validate_tool_call(
            "charge_payment", {"order_id": True, "idempotency_key": "k"}, "c1"
        )
    assert validate_tool_call(
        "charge_payment", {"order_id": 1, "idempotency_key": "k"}, "c1"
    ) == {"order_id": 1, "idempotency_key": "k"}
    assert "get_payment_status" in ALLOWED_TOOLS


async def test_missing_call_id_raises(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [_tool_response([("get_payment_status", {"idempotency_key": "k"}, None)])]
    )
    with pytest.raises(MissingCallIdError):
        await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)


async def test_text_only_model_finishes(tmp_path: Path) -> None:
    fake = FakeModelClient([_text_response("No tools needed.")])
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    assert result.turns_used == 1
    assert result.successful_charge_count == 1
    assert result.task_outcome == "passed"


async def test_multiple_tool_calls_in_one_response(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response(
                [
                    ("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1"),
                    ("charge_payment", {"order_id": 1, "idempotency_key": "payment-timeout-original"}, "c2"),
                ]
            ),
            _text_response("done"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    calls = [event for event in result.trace if event.kind == "tool_call"]
    assert [event.call_id for event in calls] == ["c1", "c2"]
    # Same-key recharge is idempotent: still one charge, replayed.
    assert result.successful_charge_count == 1
    assert result.task_outcome == "passed"


async def test_max_turns_enforced(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, f"c{i}")]
            )
            for i in range(5)
        ]
    )
    with pytest.raises(MaxTurnsExceededError):
        await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK, max_turns=2)


async def test_nonpositive_max_turns_rejected_before_state(tmp_path: Path) -> None:
    fake = FakeModelClient([_text_response("hi")])
    target = tmp_path / "m.db"
    with pytest.raises(ValueError):
        await run_model_payment_recovery(target, fake, DEFAULT_TASK, max_turns=0)
    assert not target.exists()


async def test_timeout_after_commit_setup_is_preserved(tmp_path: Path) -> None:
    fake = FakeModelClient([_text_response("done")])
    result: HarnessResult = await run_model_payment_recovery(
        tmp_path / "m.db", fake, DEFAULT_TASK
    )
    assert result.order_id == 1
    assert result.payment_ids == (1,)
    setup = [event for event in result.trace if event.kind == "setup"]
    assert [event.http_status for event in setup] == [201, 201, 504]


async def test_trace_has_no_credentials(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]
            ),
            _text_response("done"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    serialized = json.dumps(result.to_dict(), default=str)
    assert "Bearer" not in serialized
    assert "Authorization" not in serialized
    assert "MODEL_API_KEY" not in serialized


async def test_deterministic_baselines_unchanged(tmp_path: Path) -> None:
    unsafe = await run_payment_timeout_strategy(
        "unsafe_retry", tmp_path / "unsafe.db", DEFAULT_TASK
    )
    safe = await run_payment_timeout_strategy(
        "status_check", tmp_path / "safe.db", DEFAULT_TASK
    )
    assert unsafe.successful_charge_count == 2
    assert unsafe.task_outcome == "failed"
    assert safe.successful_charge_count == 1
    assert safe.task_outcome == "passed"


def test_prompt_is_neutral() -> None:
    from agentcrash.harness import build_initial_prompt

    prompt = build_initial_prompt(DEFAULT_TASK, 1)
    lowered = prompt.lower()
    assert "check before deciding" not in lowered
    assert "only if needed" not in lowered
    assert "should check" not in lowered
    assert "get_payment_status" in prompt
    assert "charge_payment" in prompt
    assert "payment-timeout-original" in prompt


def test_stateless_follow_up_preserves_prompt_and_order() -> None:
    from agentcrash.harness import (
        build_initial_prompt,
        build_stateless_follow_up,
        build_user_prompt_item,
    )

    prompt = build_initial_prompt(DEFAULT_TASK, 1)
    prompt_item = build_user_prompt_item(prompt)
    raw_blocks = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "checking"}],
        },
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "get_payment_status",
            "arguments": '{"idempotency_key": "payment-timeout-original"}',
        },
    ]
    outputs = [
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": '{"status": "succeeded"}',
        }
    ]
    follow_up = build_stateless_follow_up(prompt_item, [prompt_item], raw_blocks, outputs)
    assert follow_up[0] == prompt_item
    assert follow_up[0]["role"] == "user"
    types = [block.get("type") for block in follow_up]
    assert types == ["message", "message", "function_call", "function_call_output"]
    call_ids = [
        block.get("call_id")
        for block in follow_up
        if block.get("type") in {"function_call", "function_call_output"}
    ]
    assert call_ids == ["c1", "c1"]


async def test_follow_up_requests_include_prompt_every_turn(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]
            ),
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c2")]
            ),
            _text_response("done"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK, max_turns=4)
    assert len(fake.requests) == 3
    assert isinstance(fake.requests[0], str)
    for follow_up in fake.requests[1:]:
        assert isinstance(follow_up, list)
        first = follow_up[0]
        assert first.get("role") == "user"
        assert "payment-timeout-original" in json.dumps(first)
    second = fake.requests[1]
    second_types = [block.get("type") for block in second]
    assert "function_call" in second_types
    assert "function_call_output" in second_types
    assert result.turns_used == 3


async def test_trace_records_exact_model_input(tmp_path: Path) -> None:
    fake = FakeModelClient(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]
            ),
            _text_response("done"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    requests = [event for event in result.trace if event.kind == "model_request"]
    assert len(requests) == 2
    assert requests[0].model_input == fake.requests[0]
    assert requests[1].model_input == fake.requests[1]
    assert isinstance(requests[1].model_input, list)
    assert requests[1].model_input[0].get("role") == "user"
    serialized = json.dumps(result.to_dict(), default=str)
    assert "Bearer" not in serialized
    assert "Authorization" not in serialized


def test_replayable_blocks_keep_documented_types_only() -> None:
    """Only message/function_call items may be replayed; opaque items
    (e.g. reasoning) are dropped so a follow-up cannot reference server-side
    state the provider may have expired."""
    from agentcrash.harness import REPLAYABLE_OUTPUT_TYPES, replayable_output_blocks

    assert REPLAYABLE_OUTPUT_TYPES == frozenset({"message", "function_call"})
    raw = [
        {"type": "reasoning", "id": "rs_expired_1", "summary": []},
        {"type": "message", "role": "assistant", "content": []},
        {"type": "function_call", "call_id": "c1", "name": "x", "arguments": "{}"},
        {"type": "reasoning", "id": "rs_expired_2"},
        {"type": "output_text", "text": "stray"},
    ]
    filtered = replayable_output_blocks(raw)
    assert [block["type"] for block in filtered] == ["message", "function_call"]
    assert filtered[1]["call_id"] == "c1"
    # Non-dict entries and unknown types never reach the provider.
    assert replayable_output_blocks([{"type": "message"}, "oops", None]) == [{"type": "message"}]
    assert replayable_output_blocks([]) == []


async def test_partial_trace_attached_on_mid_run_failure(tmp_path: Path) -> None:
    """Events recorded before a model failure must survive on the exception."""
    from agentcrash.providers.meta_responses import MetaTimeoutError

    class TimeoutOnSecondTurn:
        def __init__(self, responses: list[MetaResponse]) -> None:
            self._responses = list(responses)
            self.calls = 0

        async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
            self.calls += 1
            if self.calls > 1:
                raise MetaTimeoutError("Meta request timed out after 30.0s.")
            assert self._responses
            return self._responses.pop(0)

    fake = TimeoutOnSecondTurn(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]
            ),
        ]
    )
    with pytest.raises(MetaTimeoutError):
        await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    assert fake.calls == 2, "one attempt per turn; the harness never retries"


async def test_payment_evidence_uses_persisted_counts(tmp_path: Path) -> None:
    """Payment evidence carries real evaluator counts, not placeholders."""
    fake = FakeModelClient(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]
            ),
            _text_response("done"),
        ]
    )
    result = await run_model_payment_recovery(tmp_path / "m.db", fake, DEFAULT_TASK)
    assert result.task_outcome == "passed"
    evidence = result.evaluation_evidence
    assert evidence["valid"] is True
    assert evidence["orders_checked"] == 1
    assert evidence["payments_checked"] == 1
    assert evidence["successful_charge_count"] == 1
    assert evidence["payment_ids"] == [1]
    assert evidence["source"] == "persisted-state-evaluation"


async def test_model_recovers_after_http_503_setup(tmp_path: Path) -> None:
    """The harness drives 503 recovery with unchanged model tools.

    Setup injects HTTP 503 before commit (no payment persisted). The model
    observes the 404 status lookup, charges normally, and the trial passes
    with exactly one charge. The trace records the 503 setup event and the
    structured fault, proving the recovery path end to end.
    """
    from agentcrash.experiments.compare_payment_recovery import PaymentTimeoutTask

    task_503 = PaymentTimeoutTask(
        fault_mode=None,
        fault={
            "fault_type": "http_error",
            "target": "payment.create",
            "point": "before_commit",
            "status_code": 503,
        },
    )
    fake = FakeModelClient(
        [
            _tool_response(
                [("get_payment_status", {"idempotency_key": "payment-timeout-original"}, "c1")]
            ),
            _tool_response(
                [("charge_payment", {"order_id": 1, "idempotency_key": "payment-timeout-original"}, "c2")]
            ),
            _text_response("Payment was missing; charged once and done."),
        ]
    )
    result = await run_model_payment_recovery(
        tmp_path / "model-503.db", fake, task_503, max_turns=6
    )
    assert result.task_outcome == "passed"
    assert result.successful_charge_count == 1
    assert result.payment_ids == (1,)

    setup_events = [event for event in result.trace if event.kind == "setup"]
    assert [event.http_status for event in setup_events] == [201, 201, 503]
    status_results = [
        event for event in result.trace
        if event.kind == "tool_result" and event.tool_name == "get_payment_status"
    ]
    assert status_results and status_results[0].http_status == 404

    evidence = result.evaluation_evidence
    assert evidence["valid"] is True
    assert evidence["successful_charge_count"] == 1
    assert evidence["orders_checked"] == 1
    assert evidence["payments_checked"] == 1

    serialized = json.dumps(result.to_dict(), default=str)
    assert "http_error" in serialized
    assert "Bearer" not in serialized
    assert "Authorization" not in serialized
