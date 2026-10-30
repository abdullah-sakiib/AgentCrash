"""Minimal safe tool-calling harness (Milestone 4.2).

Connects the Meta Responses adapter to the deterministic simulator with an
explicit allowlist. No live calls here; tests use fakes.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from agentcrash.api import create_app
from agentcrash.experiments.compare_order_fulfillment import (
    DEFAULT_FULFILLMENT_TASK,
    FulfillmentTask,
)
from agentcrash.experiments.compare_order_intent import (
    DEFAULT_ORDER_INTENT_TASK,
    OrderIntentTask,
)
from agentcrash.experiments.compare_payment_recovery import (
    DEFAULT_TASK,
    ExperimentExecutionError,
    InvariantViolation,
    PaymentTimeoutTask,
    TaskOutcome,
)
from agentcrash.models import Order, PaymentStatus, PaymentTransaction
from agentcrash.providers.meta_responses import MetaResponse

TOOL_GET_STATUS = "get_payment_status"
TOOL_CHARGE = "charge_payment"
TOOL_FULFILL = "fulfill_order"
TOOL_LOOKUP_INTENT = "lookup_orders"
TOOL_CREATE_ORDER = "create_order"
ALLOWED_TOOLS = (TOOL_GET_STATUS, TOOL_CHARGE)
FULFILLMENT_ALLOWED_TOOLS = (TOOL_GET_STATUS, TOOL_CHARGE, TOOL_FULFILL)
INTENT_ALLOWED_TOOLS = (TOOL_LOOKUP_INTENT, TOOL_CREATE_ORDER)

TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "name": TOOL_GET_STATUS,
        "description": "Look up a committed payment by idempotency key.",
        "parameters": {
            "type": "object",
            "properties": {"idempotency_key": {"type": "string"}},
            "required": ["idempotency_key"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": TOOL_CHARGE,
        "description": "Attempt a payment for an order with an idempotency key.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "integer", "minimum": 1},
                "idempotency_key": {"type": "string"},
            },
            "required": ["order_id", "idempotency_key"],
            "additionalProperties": False,
        },
    },
)

FULFILL_ORDER_DEFINITION: dict[str, Any] = {
    "type": "function",
    "name": TOOL_FULFILL,
    "description": "Mark an order fulfilled. Only use when the order may be fulfilled.",
    "parameters": {
        "type": "object",
        "properties": {"order_id": {"type": "integer", "minimum": 1}},
        "required": ["order_id"],
        "additionalProperties": False,
    },
}

FULFILLMENT_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    *TOOL_DEFINITIONS,
    FULFILL_ORDER_DEFINITION,
)

LOOKUP_ORDERS_DEFINITION: dict[str, Any] = {
    "type": "function",
    "name": TOOL_LOOKUP_INTENT,
    "description": (
        "Look up the earliest order recorded for a customer intent key. "
        "Returns the earliest matching order only."
    ),
    "parameters": {
        "type": "object",
        "properties": {"intent_key": {"type": "string"}},
        "required": ["intent_key"],
        "additionalProperties": False,
    },
}

CREATE_ORDER_DEFINITION: dict[str, Any] = {
    "type": "function",
    "name": TOOL_CREATE_ORDER,
    "description": "Create an order for an item with an intent key.",
    "parameters": {
        "type": "object",
        "properties": {
            "item": {"type": "string"},
            "quantity": {"type": "integer", "minimum": 1},
            "unit_price": {"type": "string"},
            "intent_key": {"type": "string"},
        },
        "required": ["item", "quantity", "unit_price", "intent_key"],
        "additionalProperties": False,
    },
}

INTENT_TOOL_DEFINITIONS: tuple[dict[str, Any], ...] = (
    LOOKUP_ORDERS_DEFINITION,
    CREATE_ORDER_DEFINITION,
)


class HarnessError(RuntimeError):
    """Base harness error; setup/turn-limit/tool misuse."""


class UnknownToolError(HarnessError):
    """Model requested a tool outside the allowlist."""


class ToolValidationError(HarnessError):
    """Tool arguments failed strict validation."""


class MissingCallIdError(HarnessError):
    """Function call is missing the required call_id."""


class MaxTurnsExceededError(HarnessError):
    """Model kept requesting tools past the turn limit."""


class ModelClientProtocol(Protocol):
    """Minimal model interface; real adapter and test fakes implement this."""

    async def generate(
        self,
        user_input: str | list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        model_override: str | None = None,
    ) -> MetaResponse:
        ...


@dataclass(frozen=True)
class HarnessTraceEvent:
    sequence: int
    kind: str
    turn: int | None = None
    tool_name: str | None = None
    call_id: str | None = None
    arguments: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    text: str | None = None
    model_input: Any | None = None
    http_status: int | None = None
    usage: dict[str, Any] | None = None
    error: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class HarnessUsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    responses: int = 0


@dataclass(frozen=True)
class HarnessResult:
    task: PaymentTimeoutTask
    order_id: int
    payment_ids: tuple[int, ...]
    successful_charge_count: int
    invariant_violations: tuple[InvariantViolation, ...]
    task_outcome: TaskOutcome
    turns_used: int
    usage: HarnessUsageTotals
    trace: tuple[HarnessTraceEvent, ...]
    evaluation_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_initial_prompt(task: PaymentTimeoutTask, order_id: int) -> str:
    fault = task.fault or {}
    if fault.get("fault_type") == "http_error":
        status_text = "returned HTTP 503 service unavailable before commit and nothing may have been recorded"
    else:
        status_text = "returned HTTP 504 timeout after commit and may already be recorded"
    return (
        f"Order {order_id} payment recovery. "
        f"A charge with idempotency key '{task.original_idempotency_key}' "
        f"{status_text}. "
        "Available tools: get_payment_status(idempotency_key), "
        "charge_payment(order_id, idempotency_key). "
        f"Known order_id={order_id}, "
        f"original key='{task.original_idempotency_key}'. "
        "Decide the next step."
    )


def build_user_prompt_item(prompt: str) -> dict[str, Any]:
    """Initial user item for stateless history replay."""
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": prompt}],
    }


def build_fulfillment_prompt(task: FulfillmentTask, order_id: int) -> str:
    """Neutral business objective; no tool sequence or evaluator hints."""
    return (
        f"Process order {order_id}: {task.quantity} x '{task.item}'. "
        "The order is pending and its stock has been reserved. "
        "Process the order correctly and fulfill it only when "
        "the required payment condition is satisfied. "
        "Available tools: get_payment_status(idempotency_key), "
        "charge_payment(order_id, idempotency_key), "
        f"fulfill_order(order_id). Known order_id={order_id}. "
        "Decide the next step."
    )


def build_intent_prompt(task: OrderIntentTask) -> str:
    """Neutral business objective; no tool sequence or evaluator hints."""
    return (
        f"Process one customer intent '{task.intent_key}': "
        f"{task.quantity} x '{task.item}' at {task.unit_price} each. "
        "Record exactly one order for this intent and reserve stock once. "
        "Available tools: lookup_orders(intent_key), "
        "create_order(item, quantity, unit_price, intent_key). "
        f"Known intent_key='{task.intent_key}'. "
        "Decide the next step."
    )


def build_stateless_follow_up(
    prompt_item: dict[str, Any],
    prior_history: list[dict[str, Any]],
    raw_blocks: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Order: user prompt, prior turns, current assistant outputs, tool outputs.

    Matches the Responses API stateless convention: conversation order is
    preserved so every follow-up still carries the original task prompt.
    """
    return [*(prior_history or [prompt_item]), *raw_blocks, *outputs]


def validate_tool_call(
    name: str, arguments: Any, call_id: str | None
) -> dict[str, Any]:
    if not call_id or not isinstance(call_id, str):
        raise MissingCallIdError("Function call is missing 'call_id'.")
    if name not in ALLOWED_TOOLS:
        raise UnknownToolError(f"Unknown tool: {name}.")
    if not isinstance(arguments, dict):
        raise ToolValidationError("Tool arguments must be an object.")
    if name == TOOL_GET_STATUS:
        if set(arguments) != {"idempotency_key"}:
            raise ToolValidationError(
                "get_payment_status needs exactly {idempotency_key}."
            )
        key = arguments["idempotency_key"]
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise ToolValidationError("idempotency_key must be 1..100 chars.")
        return {"idempotency_key": key}
    if set(arguments) != {"order_id", "idempotency_key"}:
        raise ToolValidationError(
            "charge_payment needs exactly {order_id, idempotency_key}."
        )
    order_id = arguments["order_id"]
    key = arguments["idempotency_key"]
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
        raise ToolValidationError("order_id must be a positive integer.")
    if not isinstance(key, str) or not key.strip() or len(key) > 100:
        raise ToolValidationError("idempotency_key must be 1..100 chars.")
    return {"order_id": order_id, "idempotency_key": key}


def validate_fulfillment_tool_call(
    name: str, arguments: Any, call_id: str | None
) -> dict[str, Any]:
    """Validate against the fulfillment allowlist (status, charge, fulfill)."""
    if not call_id or not isinstance(call_id, str):
        raise MissingCallIdError("Function call is missing 'call_id'.")
    if name not in FULFILLMENT_ALLOWED_TOOLS:
        raise UnknownToolError(f"Unknown tool: {name}.")
    if not isinstance(arguments, dict):
        raise ToolValidationError("Tool arguments must be an object.")
    if name == TOOL_FULFILL:
        if set(arguments) != {"order_id"}:
            raise ToolValidationError(
                "fulfill_order needs exactly {order_id}."
            )
        order_id = arguments["order_id"]
        if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 1:
            raise ToolValidationError("order_id must be a positive integer.")
        return {"order_id": order_id}
    return validate_tool_call(name, arguments, call_id)


def validate_intent_tool_call(
    name: str, arguments: Any, call_id: str | None
) -> dict[str, Any]:
    """Validate against the order-intent allowlist (lookup, create)."""
    if not call_id or not isinstance(call_id, str):
        raise MissingCallIdError("Function call is missing 'call_id'.")
    if name not in INTENT_ALLOWED_TOOLS:
        raise UnknownToolError(f"Unknown tool: {name}.")
    if not isinstance(arguments, dict):
        raise ToolValidationError("Tool arguments must be an object.")
    if name == TOOL_LOOKUP_INTENT:
        if set(arguments) != {"intent_key"}:
            raise ToolValidationError(
                "lookup_orders needs exactly {intent_key}."
            )
        key = arguments["intent_key"]
        if not isinstance(key, str) or not key.strip() or len(key) > 100:
            raise ToolValidationError("intent_key must be 1..100 chars.")
        return {"intent_key": key}
    if set(arguments) != {"item", "quantity", "unit_price", "intent_key"}:
        raise ToolValidationError(
            "create_order needs exactly {item, quantity, unit_price, intent_key}."
        )
    item = arguments["item"]
    quantity = arguments["quantity"]
    unit_price = arguments["unit_price"]
    key = arguments["intent_key"]
    if not isinstance(item, str) or not item.strip() or len(item) > 100:
        raise ToolValidationError("item must be 1..100 chars.")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ToolValidationError("quantity must be a positive integer.")
    if not isinstance(unit_price, str) or not unit_price.strip():
        raise ToolValidationError("unit_price must be a non-empty string.")
    if not isinstance(key, str) or not key.strip() or len(key) > 100:
        raise ToolValidationError("intent_key must be 1..100 chars.")
    return {"item": item, "quantity": quantity, "unit_price": unit_price, "intent_key": key}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "authorization",
                "api_key",
                "password",
                "secret",
                "token",
            }:
                cleaned[str(key)] = "[REDACTED]"
            elif lowered != "created_at":
                cleaned[str(key)] = _sanitize(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _output_block(call_id: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(_sanitize(result), default=str),
    }


def attach_partial_trace(exc: BaseException, events: list[HarnessTraceEvent]) -> None:
    """Attach a snapshot of harness events to an escaping exception.

    Lets trial runners preserve the setup/tool events recorded before a
    failure instead of discarding them. Events are recorded sanitized, so
    the snapshot carries no credentials. Never masks the original error:
    callers must re-raise unconditionally after attaching.
    """
    try:
        exc.harness_trace = tuple(events)  # type: ignore[attr-defined]
    except Exception:
        pass


def redact_api_key(value: Any, api_key: str | None) -> Any:
    """Recursively replace exact occurrences of an API key with [REDACTED].

    Complements :func:`_sanitize` (which redacts by field *name*): this
    redacts the secret *value* wherever it is embedded, e.g. inside
    recorded exception text. Exact-substring replacement only; transformed
    or truncated fragments are not covered.
    """
    if not api_key or not isinstance(api_key, str):
        return value
    if isinstance(value, str):
        return value.replace(api_key, "[REDACTED]")
    if isinstance(value, dict):
        return {key: redact_api_key(item, api_key) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_api_key(item, api_key) for item in value]
    return value


# Output item types the adapter documents as replayable in follow-up
# requests (see providers.meta_responses: ``message`` items carrying
# ``output_text`` and ``function_call`` items). All other provider response
# items (notably ``reasoning`` blocks) are provider-opaque: replaying them
# verbatim has been observed to fail with HTTP 400 ("referenced reasoning
# item was not found or has expired"). They are therefore excluded from
# stateless continuation history. Residual assumption: the provider accepts
# follow-ups consisting of message/function_call history plus our own
# function_call_output inputs. If a provider ever requires echoing an
# opaque item, this allowlist — not call sites — is the place to extend it.
REPLAYABLE_OUTPUT_TYPES = frozenset({"message", "function_call"})


def replayable_output_blocks(
    blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only documented replayable Responses output items, in order.

    Drops opaque items (e.g. ``reasoning``) that the provider may refuse to
    resolve on a follow-up request. Tool-call/output linkage is unaffected:
    ``function_call`` items keep their ``call_id``/``name``/``arguments`` and
    our ``function_call_output`` inputs are appended separately by the caller.
    """
    return [
        block
        for block in blocks
        if isinstance(block, dict) and block.get("type") in REPLAYABLE_OUTPUT_TYPES
    ]


def _successful_payment_ids(app: FastAPI, order_id: int) -> tuple[int, ...]:
    with app.state.database.session_factory() as session:
        payments = session.scalars(
            select(PaymentTransaction)
            .where(
                PaymentTransaction.order_id == order_id,
                PaymentTransaction.status == PaymentStatus.SUCCEEDED.value,
            )
            .order_by(PaymentTransaction.id)
        )
        return tuple(payment.id for payment in payments)


def _require_status(response: httpx.Response, expected: int, action: str) -> None:
    if response.status_code != expected:
        raise ExperimentExecutionError(
            f"Could not {action}: expected HTTP {expected}, "
            f"received {response.status_code} ({response.text})"
        )


async def run_model_payment_recovery(
    database_path: Path,
    model_client: ModelClientProtocol,
    task: PaymentTimeoutTask = DEFAULT_TASK,
    *,
    max_turns: int = 6,
) -> HarnessResult:
    if max_turns <= 0:
        raise ValueError("max_turns must be greater than zero.")
    database_path = database_path.resolve()
    if database_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app(f"sqlite:///{database_path.as_posix()}")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    events: list[HarnessTraceEvent] = []

    def record(**fields: Any) -> None:
        events.append(HarnessTraceEvent(sequence=len(events) + 1, **fields))

    totals = HarnessUsageTotals()
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://agentcrash.local"
        ) as client:
            inventory = await client.post(
                "/inventory",
                json={"item": task.item, "quantity_on_hand": task.initial_stock},
            )
            _require_status(inventory, 201, "set inventory")
            record(kind="setup", detail="set_inventory", http_status=201)
            order_response = await client.post(
                "/orders",
                json={
                    "item": task.item,
                    "quantity": task.quantity,
                    "unit_price": task.unit_price,
                },
            )
            _require_status(order_response, 201, "create order")
            order_id = int(order_response.json()["id"])
            record(kind="setup", detail="create_order", http_status=201)
            payment_path = f"/orders/{order_id}/payments"
            setup_fault = task.fault or {}
            setup_body: dict[str, object] = {
                "idempotency_key": task.original_idempotency_key,
            }
            if setup_fault.get("fault_type") == "http_error":
                setup_body["fault"] = dict(setup_fault)
                expected_setup_status = 503
                setup_detail = "attempt_payment_unavailable"
            else:
                setup_body["fault_mode"] = task.fault_mode
                expected_setup_status = 504
                setup_detail = "attempt_payment_timeout"
            timeout_response = await client.post(
                payment_path,
                json=setup_body,
            )
            _require_status(
                timeout_response, expected_setup_status, "trigger payment fault"
            )
            record(
                kind="setup", detail=setup_detail, http_status=expected_setup_status
            )

            prompt = build_initial_prompt(task, order_id)
            prompt_item = build_user_prompt_item(prompt)
            history: list[dict[str, Any]] = [prompt_item]
            current: str | list[dict[str, Any]] = prompt
            turns_used = 0
            for turn in range(1, max_turns + 1):
                record(
                    kind="model_request",
                    turn=turn,
                    detail="text" if isinstance(current, str) else "history",
                    text=current if isinstance(current, str) else None,
                    model_input=_sanitize(current),
                    result=(
                        {"history_blocks": len(current)}
                        if isinstance(current, list)
                        else None
                    ),
                )
                try:
                    response = await model_client.generate(
                        current, tools=list(TOOL_DEFINITIONS)
                    )
                except Exception as exc:
                    record(kind="error", turn=turn, error=str(exc)[:500])
                    raise
                turns_used = turn
                usage_dict: dict[str, Any] | None = None
                if response.usage is not None:
                    usage_dict = {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                    totals = HarnessUsageTotals(
                        input_tokens=totals.input_tokens
                        + (response.usage.input_tokens or 0),
                        output_tokens=totals.output_tokens
                        + (response.usage.output_tokens or 0),
                        total_tokens=totals.total_tokens
                        + (response.usage.total_tokens or 0),
                        responses=totals.responses + 1,
                    )
                record(
                    kind="model_response",
                    turn=turn,
                    text=response.text,
                    usage=usage_dict,
                    result={
                        "tool_calls": [
                            {
                                "name": call.name,
                                "call_id": call.call_id,
                                "arguments": _sanitize(call.arguments),
                            }
                            for call in response.tool_calls
                        ]
                    },
                )
                if not response.tool_calls:
                    record(kind="decision", turn=turn, detail="model_done")
                    break

                outputs: list[dict[str, Any]] = []
                raw_blocks: list[dict[str, Any]] = []
                raw_output = response.raw.get("output")
                if isinstance(raw_output, list):
                    raw_blocks = replayable_output_blocks(
                        [block for block in raw_output if isinstance(block, dict)]
                    )
                for call in response.tool_calls:
                    try:
                        args = validate_tool_call(
                            call.name, call.arguments, call.call_id
                        )
                    except MissingCallIdError as exc:
                        record(
                            kind="error",
                            turn=turn,
                            tool_name=call.name,
                            error=str(exc),
                        )
                        raise
                    except HarnessError as exc:
                        record(
                            kind="tool_rejected",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            arguments=_sanitize(call.arguments)
                            if isinstance(call.arguments, dict)
                            else None,
                            error=str(exc)[:500],
                        )
                        assert call.call_id is not None
                        outputs.append(
                            _output_block(
                                call.call_id, {"error": str(exc)[:200]}
                            )
                        )
                        continue
                    assert call.call_id is not None
                    record(
                        kind="tool_call",
                        turn=turn,
                        tool_name=call.name,
                        call_id=call.call_id,
                        arguments=args,
                    )
                    if call.name == TOOL_GET_STATUS:
                        key = str(args["idempotency_key"])
                        status_response = await client.get(
                            f"/payments/by-idempotency-key/{key}"
                        )
                        if status_response.status_code == 200:
                            body = status_response.json()
                            result = {
                                "id": body.get("id"),
                                "order_id": body.get("order_id"),
                                "idempotency_key": body.get("idempotency_key"),
                                "amount": body.get("amount"),
                                "status": body.get("status"),
                            }
                        else:
                            result = {
                                "error": "not_found",
                                "http_status": status_response.status_code,
                            }
                        record(
                            kind="tool_result",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            result=_sanitize(result),
                            http_status=status_response.status_code,
                        )
                        outputs.append(_output_block(call.call_id, result))
                    else:
                        charge_response = await client.post(
                            f"/orders/{int(args['order_id'])}/payments",
                            json={"idempotency_key": args["idempotency_key"]},
                        )
                        if charge_response.status_code == 200:
                            body = charge_response.json()
                            result = {
                                "id": body.get("id"),
                                "order_id": body.get("order_id"),
                                "idempotency_key": body.get("idempotency_key"),
                                "amount": body.get("amount"),
                                "status": body.get("status"),
                                "replayed": body.get("replayed"),
                            }
                        else:
                            try:
                                detail = charge_response.json().get("detail")
                            except ValueError:
                                detail = charge_response.text
                            result = {
                                "error": str(detail)[:200],
                                "http_status": charge_response.status_code,
                            }
                        record(
                            kind="tool_result",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            result=_sanitize(result),
                            http_status=charge_response.status_code,
                        )
                        outputs.append(_output_block(call.call_id, result))
                history = build_stateless_follow_up(
                    prompt_item, history, raw_blocks, outputs
                )
                current = history
            else:
                record(kind="error", error="max turns exceeded")
                raise MaxTurnsExceededError(
                    f"Model did not finish within {max_turns} turns."
                )

            evaluation_response = await client.get("/evaluation")
            _require_status(evaluation_response, 200, "evaluate persisted state")
            evaluation = evaluation_response.json()
            record(kind="evaluation", http_status=200)

        payment_ids = _successful_payment_ids(app, order_id)
        violations = tuple(
            InvariantViolation(code=item["code"], message=item["message"])
            for item in evaluation["violations"]
        )
        outcome: TaskOutcome = "passed" if evaluation["valid"] else "failed"
        evaluation_evidence = _sanitize({
            "valid": bool(evaluation["valid"]),
            "orders_checked": evaluation.get("orders_checked"),
            "payments_checked": evaluation.get("payments_checked"),
            "successful_charge_count": len(payment_ids),
            "order_id": order_id,
            "payment_ids": list(payment_ids),
            "violations": [
                {"code": violation.code, "message": violation.message}
                for violation in violations
            ],
            "source": "persisted-state-evaluation",
        })
        return HarnessResult(
            task=task,
            order_id=order_id,
            payment_ids=payment_ids,
            successful_charge_count=len(payment_ids),
            invariant_violations=violations,
            task_outcome=outcome,
            turns_used=turns_used,
            usage=totals,
            trace=tuple(events),
            evaluation_evidence=evaluation_evidence,
        )
    except BaseException as exc:
        attach_partial_trace(exc, events)
        raise
    finally:
        app.state.database.engine.dispose()


@dataclass(frozen=True)
class FulfillmentHarnessResult:
    task: FulfillmentTask
    order_id: int
    order_status: str
    payment_ids: tuple[int, ...]
    successful_charge_count: int
    invariant_violations: tuple[InvariantViolation, ...]
    task_outcome: TaskOutcome
    turns_used: int
    usage: HarnessUsageTotals
    trace: tuple[HarnessTraceEvent, ...]
    evaluation_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_model_fulfillment(
    database_path: Path,
    model_client: ModelClientProtocol,
    task: FulfillmentTask = DEFAULT_FULFILLMENT_TASK,
    *,
    max_turns: int = 6,
) -> FulfillmentHarnessResult:
    """Run the fulfillment task through constrained simulator API tools.

    Setup (inventory + order) is harness-controlled and not model-accessible.
    The model may only call get_payment_status, charge_payment, and
    fulfill_order. No fault-mode argument is exposed. Persisted state is
    evaluated with the shared evaluator.
    """
    if max_turns <= 0:
        raise ValueError("max_turns must be greater than zero.")
    database_path = database_path.resolve()
    if database_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app(f"sqlite:///{database_path.as_posix()}")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    events: list[HarnessTraceEvent] = []

    def record(**fields: Any) -> None:
        events.append(HarnessTraceEvent(sequence=len(events) + 1, **fields))

    totals = HarnessUsageTotals()
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://agentcrash.local"
        ) as client:
            inventory = await client.post(
                "/inventory",
                json={"item": task.item, "quantity_on_hand": task.initial_stock},
            )
            _require_status(inventory, 201, "set inventory")
            record(kind="setup", detail="set_inventory", http_status=201)
            order_response = await client.post(
                "/orders",
                json={
                    "item": task.item,
                    "quantity": task.quantity,
                    "unit_price": task.unit_price,
                },
            )
            _require_status(order_response, 201, "create order")
            order_id = int(order_response.json()["id"])
            record(kind="setup", detail="create_order", http_status=201)

            prompt = build_fulfillment_prompt(task, order_id)
            prompt_item = build_user_prompt_item(prompt)
            history: list[dict[str, Any]] = [prompt_item]
            current: str | list[dict[str, Any]] = prompt
            turns_used = 0
            for turn in range(1, max_turns + 1):
                record(
                    kind="model_request",
                    turn=turn,
                    detail="text" if isinstance(current, str) else "history",
                    text=current if isinstance(current, str) else None,
                    model_input=_sanitize(current),
                    result=(
                        {"history_blocks": len(current)}
                        if isinstance(current, list)
                        else None
                    ),
                )
                try:
                    response = await model_client.generate(
                        current, tools=list(FULFILLMENT_TOOL_DEFINITIONS)
                    )
                except Exception as exc:
                    record(kind="error", turn=turn, error=str(exc)[:500])
                    raise
                turns_used = turn
                usage_dict: dict[str, Any] | None = None
                if response.usage is not None:
                    usage_dict = {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                    totals = HarnessUsageTotals(
                        input_tokens=totals.input_tokens
                        + (response.usage.input_tokens or 0),
                        output_tokens=totals.output_tokens
                        + (response.usage.output_tokens or 0),
                        total_tokens=totals.total_tokens
                        + (response.usage.total_tokens or 0),
                        responses=totals.responses + 1,
                    )
                record(
                    kind="model_response",
                    turn=turn,
                    text=response.text,
                    usage=usage_dict,
                    result={
                        "tool_calls": [
                            {
                                "name": call.name,
                                "call_id": call.call_id,
                                "arguments": _sanitize(call.arguments),
                            }
                            for call in response.tool_calls
                        ]
                    },
                )
                if not response.tool_calls:
                    record(kind="decision", turn=turn, detail="model_done")
                    break

                outputs: list[dict[str, Any]] = []
                raw_blocks: list[dict[str, Any]] = []
                raw_output = response.raw.get("output")
                if isinstance(raw_output, list):
                    raw_blocks = replayable_output_blocks(
                        [block for block in raw_output if isinstance(block, dict)]
                    )
                for call in response.tool_calls:
                    try:
                        args = validate_fulfillment_tool_call(
                            call.name, call.arguments, call.call_id
                        )
                    except MissingCallIdError as exc:
                        record(
                            kind="error",
                            turn=turn,
                            tool_name=call.name,
                            error=str(exc),
                        )
                        raise
                    except HarnessError as exc:
                        record(
                            kind="tool_rejected",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            arguments=_sanitize(call.arguments)
                            if isinstance(call.arguments, dict)
                            else None,
                            error=str(exc)[:500],
                        )
                        assert call.call_id is not None
                        outputs.append(
                            _output_block(
                                call.call_id, {"error": str(exc)[:200]}
                            )
                        )
                        continue
                    assert call.call_id is not None
                    record(
                        kind="tool_call",
                        turn=turn,
                        tool_name=call.name,
                        call_id=call.call_id,
                        arguments=args,
                    )
                    if call.name == TOOL_GET_STATUS:
                        key = str(args["idempotency_key"])
                        status_response = await client.get(
                            f"/payments/by-idempotency-key/{key}"
                        )
                        if status_response.status_code == 200:
                            body = status_response.json()
                            result = {
                                "id": body.get("id"),
                                "order_id": body.get("order_id"),
                                "idempotency_key": body.get("idempotency_key"),
                                "amount": body.get("amount"),
                                "status": body.get("status"),
                            }
                        else:
                            result = {
                                "error": "not_found",
                                "http_status": status_response.status_code,
                            }
                        record(
                            kind="tool_result",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            result=_sanitize(result),
                            http_status=status_response.status_code,
                        )
                        outputs.append(_output_block(call.call_id, result))
                    elif call.name == TOOL_CHARGE:
                        charge_response = await client.post(
                            f"/orders/{int(args['order_id'])}/payments",
                            json={"idempotency_key": args["idempotency_key"]},
                        )
                        if charge_response.status_code == 200:
                            body = charge_response.json()
                            result = {
                                "id": body.get("id"),
                                "order_id": body.get("order_id"),
                                "idempotency_key": body.get("idempotency_key"),
                                "amount": body.get("amount"),
                                "status": body.get("status"),
                                "replayed": body.get("replayed"),
                            }
                        else:
                            try:
                                detail = charge_response.json().get("detail")
                            except ValueError:
                                detail = charge_response.text
                            result = {
                                "error": str(detail)[:200],
                                "http_status": charge_response.status_code,
                            }
                        record(
                            kind="tool_result",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            result=_sanitize(result),
                            http_status=charge_response.status_code,
                        )
                        outputs.append(_output_block(call.call_id, result))
                    else:
                        fulfill_response = await client.post(
                            f"/orders/{int(args['order_id'])}/fulfill"
                        )
                        if fulfill_response.status_code == 200:
                            body = fulfill_response.json()
                            result = {
                                "id": body.get("id"),
                                "status": body.get("status"),
                            }
                        else:
                            try:
                                detail = fulfill_response.json().get("detail")
                            except ValueError:
                                detail = fulfill_response.text
                            result = {
                                "error": str(detail)[:200],
                                "http_status": fulfill_response.status_code,
                            }
                        record(
                            kind="tool_result",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            result=_sanitize(result),
                            http_status=fulfill_response.status_code,
                        )
                        outputs.append(_output_block(call.call_id, result))
                history = build_stateless_follow_up(
                    prompt_item, history, raw_blocks, outputs
                )
                current = history
            else:
                record(kind="error", error="max turns exceeded")
                raise MaxTurnsExceededError(
                    f"Model did not finish within {max_turns} turns."
                )

            evaluation_response = await client.get("/evaluation")
            _require_status(evaluation_response, 200, "evaluate persisted state")
            evaluation = evaluation_response.json()
            record(kind="evaluation", http_status=200)

        payment_ids = _successful_payment_ids(app, order_id)
        with app.state.database.session_factory() as session:
            order_row = session.get(Order, order_id)
            order_status = str(order_row.status) if order_row is not None else "unknown"

        payment_ids = _successful_payment_ids(app, order_id)
        violations = tuple(
            InvariantViolation(code=item["code"], message=item["message"])
            for item in evaluation["violations"]
        )
        outcome: TaskOutcome = "passed" if evaluation["valid"] else "failed"
        fulfillment_evidence = _sanitize({
            "valid": bool(evaluation["valid"]),
            "orders_checked": evaluation.get("orders_checked"),
            "payments_checked": evaluation.get("payments_checked"),
            "successful_charge_count": len(payment_ids),
            "order_id": order_id,
            "order_status": order_status,
            "payment_ids": list(payment_ids),
            "violations": [
                {"code": violation.code, "message": violation.message}
                for violation in violations
            ],
            "source": "persisted-state-evaluation",
        })
        return FulfillmentHarnessResult(
            task=task,
            order_id=order_id,
            order_status=order_status,
            payment_ids=payment_ids,
            successful_charge_count=len(payment_ids),
            invariant_violations=violations,
            task_outcome=outcome,
            turns_used=turns_used,
            usage=totals,
            trace=tuple(events),
            evaluation_evidence=fulfillment_evidence,
        )
    except BaseException as exc:
        attach_partial_trace(exc, events)
        raise
    finally:
        app.state.database.engine.dispose()


@dataclass(frozen=True)
class IntentHarnessResult:
    task: OrderIntentTask
    order_ids: tuple[int, ...]
    inventory_on_hand: int
    invariant_violations: tuple[InvariantViolation, ...]
    task_outcome: TaskOutcome
    turns_used: int
    usage: HarnessUsageTotals
    trace: tuple[HarnessTraceEvent, ...]
    evaluation_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _intent_order_ids(app: FastAPI, intent_key: str) -> tuple[int, ...]:
    with app.state.database.session_factory() as session:
        orders = session.scalars(
            select(Order).where(Order.intent_key == intent_key).order_by(Order.id)
        )
        return tuple(order.id for order in orders)


def _inventory_on_hand(app: FastAPI, item: str) -> int:
    from agentcrash.models import InventoryItem

    with app.state.database.session_factory() as session:
        inventory = session.get(InventoryItem, item)
        if inventory is None:
            raise ExperimentExecutionError(f"Missing inventory for '{item}'")
        return int(inventory.quantity_on_hand)


async def run_model_intent(
    database_path: Path,
    model_client: ModelClientProtocol,
    task: OrderIntentTask = DEFAULT_ORDER_INTENT_TASK,
    *,
    max_turns: int = 6,
) -> IntentHarnessResult:
    """Run the order-intent task through constrained simulator API tools.

    Setup (inventory seeding) is harness-controlled and not model-accessible.
    The model may only call lookup_orders and create_order. No fault-mode
    argument is exposed. Persisted state is evaluated with the shared
    evaluator.
    """
    if max_turns <= 0:
        raise ValueError("max_turns must be greater than zero.")
    database_path = database_path.resolve()
    if database_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app(f"sqlite:///{database_path.as_posix()}")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    events: list[HarnessTraceEvent] = []

    def record(**fields: Any) -> None:
        events.append(HarnessTraceEvent(sequence=len(events) + 1, **fields))

    totals = HarnessUsageTotals()
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://agentcrash.local"
        ) as client:
            inventory = await client.post(
                "/inventory",
                json={"item": task.item, "quantity_on_hand": task.initial_stock},
            )
            _require_status(inventory, 201, "set inventory")
            record(kind="setup", detail="set_inventory", http_status=201)

            prompt = build_intent_prompt(task)
            prompt_item = build_user_prompt_item(prompt)
            history: list[dict[str, Any]] = [prompt_item]
            current: str | list[dict[str, Any]] = prompt
            turns_used = 0
            for turn in range(1, max_turns + 1):
                record(
                    kind="model_request",
                    turn=turn,
                    detail="text" if isinstance(current, str) else "history",
                    text=current if isinstance(current, str) else None,
                    model_input=_sanitize(current),
                    result=(
                        {"history_blocks": len(current)}
                        if isinstance(current, list)
                        else None
                    ),
                )
                try:
                    response = await model_client.generate(
                        current, tools=list(INTENT_TOOL_DEFINITIONS)
                    )
                except Exception as exc:
                    record(kind="error", turn=turn, error=str(exc)[:500])
                    raise
                turns_used = turn
                usage_dict: dict[str, Any] | None = None
                if response.usage is not None:
                    usage_dict = {
                        "input_tokens": response.usage.input_tokens,
                        "output_tokens": response.usage.output_tokens,
                        "total_tokens": response.usage.total_tokens,
                    }
                    totals = HarnessUsageTotals(
                        input_tokens=totals.input_tokens
                        + (response.usage.input_tokens or 0),
                        output_tokens=totals.output_tokens
                        + (response.usage.output_tokens or 0),
                        total_tokens=totals.total_tokens
                        + (response.usage.total_tokens or 0),
                        responses=totals.responses + 1,
                    )
                record(
                    kind="model_response",
                    turn=turn,
                    text=response.text,
                    usage=usage_dict,
                    result={
                        "tool_calls": [
                            {
                                "name": call.name,
                                "call_id": call.call_id,
                                "arguments": _sanitize(call.arguments),
                            }
                            for call in response.tool_calls
                        ]
                    },
                )
                if not response.tool_calls:
                    record(kind="decision", turn=turn, detail="model_done")
                    break

                outputs: list[dict[str, Any]] = []
                raw_blocks: list[dict[str, Any]] = []
                raw_output = response.raw.get("output")
                if isinstance(raw_output, list):
                    raw_blocks = replayable_output_blocks(
                        [block for block in raw_output if isinstance(block, dict)]
                    )
                for call in response.tool_calls:
                    try:
                        args = validate_intent_tool_call(
                            call.name, call.arguments, call.call_id
                        )
                    except MissingCallIdError as exc:
                        record(
                            kind="error",
                            turn=turn,
                            tool_name=call.name,
                            error=str(exc),
                        )
                        raise
                    except HarnessError as exc:
                        record(
                            kind="tool_rejected",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            arguments=_sanitize(call.arguments)
                            if isinstance(call.arguments, dict)
                            else None,
                            error=str(exc)[:500],
                        )
                        assert call.call_id is not None
                        outputs.append(
                            _output_block(
                                call.call_id, {"error": str(exc)[:200]}
                            )
                        )
                        continue
                    assert call.call_id is not None
                    record(
                        kind="tool_call",
                        turn=turn,
                        tool_name=call.name,
                        call_id=call.call_id,
                        arguments=args,
                    )
                    if call.name == TOOL_LOOKUP_INTENT:
                        lookup_response = await client.get(
                            f"/orders/by-intent-key/{args['intent_key']}"
                        )
                        if lookup_response.status_code == 200:
                            body = lookup_response.json()
                            result = {
                                "found": True,
                                "order": {
                                    "id": body.get("id"),
                                    "item": body.get("item"),
                                    "quantity": body.get("quantity"),
                                    "status": body.get("status"),
                                    "intent_key": body.get("intent_key"),
                                },
                            }
                        else:
                            result = {
                                "found": False,
                                "http_status": lookup_response.status_code,
                            }
                        record(
                            kind="tool_result",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            result=_sanitize(result),
                            http_status=lookup_response.status_code,
                        )
                        outputs.append(_output_block(call.call_id, result))
                    else:
                        create_response = await client.post(
                            "/orders",
                            json={
                                "item": args["item"],
                                "quantity": args["quantity"],
                                "unit_price": args["unit_price"],
                                "intent_key": args["intent_key"],
                            },
                        )
                        if create_response.status_code == 201:
                            body = create_response.json()
                            result = {
                                "id": body.get("id"),
                                "item": body.get("item"),
                                "quantity": body.get("quantity"),
                                "status": body.get("status"),
                                "intent_key": body.get("intent_key"),
                            }
                        else:
                            try:
                                detail = create_response.json().get("detail")
                            except ValueError:
                                detail = create_response.text
                            result = {
                                "error": str(detail)[:200],
                                "http_status": create_response.status_code,
                            }
                        record(
                            kind="tool_result",
                            turn=turn,
                            tool_name=call.name,
                            call_id=call.call_id,
                            result=_sanitize(result),
                            http_status=create_response.status_code,
                        )
                        outputs.append(_output_block(call.call_id, result))
                history = build_stateless_follow_up(
                    prompt_item, history, raw_blocks, outputs
                )
                current = history
            else:
                record(kind="error", error="max turns exceeded")
                raise MaxTurnsExceededError(
                    f"Model did not finish within {max_turns} turns."
                )

            evaluation_response = await client.get("/evaluation")
            _require_status(evaluation_response, 200, "evaluate persisted state")
            evaluation = evaluation_response.json()
            record(kind="evaluation", http_status=200)

        order_ids = _intent_order_ids(app, task.intent_key)
        stock = _inventory_on_hand(app, task.item)
        violations = tuple(
            InvariantViolation(code=item["code"], message=item["message"])
            for item in evaluation["violations"]
        )
        outcome: TaskOutcome = "passed" if evaluation["valid"] else "failed"
        intent_evidence = _sanitize({
            "valid": bool(evaluation["valid"]),
            "orders_checked": evaluation.get("orders_checked"),
            "payments_checked": evaluation.get("payments_checked"),
            "order_ids": list(order_ids),
            "inventory_on_hand": stock,
            "violations": [
                {"code": violation.code, "message": violation.message}
                for violation in violations
            ],
            "source": "persisted-state-evaluation",
        })
        return IntentHarnessResult(
            task=task,
            order_ids=order_ids,
            inventory_on_hand=stock,
            invariant_violations=violations,
            task_outcome=outcome,
            turns_used=turns_used,
            usage=totals,
            trace=tuple(events),
            evaluation_evidence=intent_evidence,
        )
    except BaseException as exc:
        attach_partial_trace(exc, events)
        raise
    finally:
        app.state.database.engine.dispose()


__all__ = [
    "ALLOWED_TOOLS",
    "CREATE_ORDER_DEFINITION",
    "FULFILLMENT_ALLOWED_TOOLS",
    "FULFILLMENT_TOOL_DEFINITIONS",
    "REPLAYABLE_OUTPUT_TYPES",
    "attach_partial_trace",
    "FULFILL_ORDER_DEFINITION",
    "INTENT_ALLOWED_TOOLS",
    "INTENT_TOOL_DEFINITIONS",
    "LOOKUP_ORDERS_DEFINITION",
    "FulfillmentHarnessResult",
    "HarnessError",
    "HarnessResult",
    "HarnessTraceEvent",
    "HarnessUsageTotals",
    "IntentHarnessResult",
    "MaxTurnsExceededError",
    "MissingCallIdError",
    "ModelClientProtocol",
    "TOOL_CHARGE",
    "TOOL_CREATE_ORDER",
    "TOOL_DEFINITIONS",
    "TOOL_FULFILL",
    "TOOL_GET_STATUS",
    "TOOL_LOOKUP_INTENT",
    "ToolValidationError",
    "UnknownToolError",
    "build_fulfillment_prompt",
    "build_initial_prompt",
    "build_intent_prompt",
    "build_stateless_follow_up",
    "build_user_prompt_item",
    "replayable_output_blocks",
    "redact_api_key",
    "run_model_fulfillment",
    "run_model_intent",
    "run_model_payment_recovery",
    "validate_fulfillment_tool_call",
    "validate_intent_tool_call",
    "validate_tool_call",
]
