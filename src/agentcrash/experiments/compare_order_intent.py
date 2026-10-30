"""Deterministic duplicate-order-for-intent scenario (Milestone 6.1).

Unsafe strategy submits the same customer intent twice, deducting inventory
twice. Safe strategy looks the intent up first and creates at most one order.
The evaluator inspects persisted state.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from agentcrash.api import create_app
from agentcrash.experiments.compare_payment_recovery import (
    ExperimentExecutionError,
    InvariantViolation,
    TaskOutcome,
    TraceEvent,
)
from agentcrash.models import Order


StrategyName = Literal["unsafe_duplicate", "safe_deduplicated"]
SCENARIO = "duplicate_order_for_intent"


@dataclass(frozen=True)
class OrderIntentTask:
    item: str = "widget"
    initial_stock: int = 10
    quantity: int = 2
    unit_price: str = "12.50"
    intent_key: str = "customer-order-7"


@dataclass(frozen=True)
class OrderIntentResult:
    strategy_name: StrategyName
    task: OrderIntentTask
    task_outcome: TaskOutcome
    order_ids: tuple[int, ...]
    inventory_on_hand: int
    successful_charge_count: int
    invariant_violations: tuple[InvariantViolation, ...]
    trace: tuple[TraceEvent, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class OrderIntentComparison:
    scenario: str
    results: tuple[OrderIntentResult, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def result_for(self, strategy_name: StrategyName) -> OrderIntentResult:
        return next(
            result
            for result in self.results
            if result.strategy_name == strategy_name
        )


DEFAULT_ORDER_INTENT_TASK = OrderIntentTask()


class OrderIntentTraceRecorder:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def request(
        self,
        client: httpx.AsyncClient,
        *,
        action: str,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
    ) -> httpx.Response:
        request_summary: dict[str, object] = {"method": method, "path": path}
        if body is not None:
            request_summary["body"] = dict(body)
        try:
            response = await client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            self.events.append(
                TraceEvent(
                    sequence=len(self.events) + 1,
                    event_type="tool",
                    action=action,
                    request=request_summary,
                    error=str(exc),
                )
            )
            raise
        try:
            response_body = response.json()
        except ValueError:
            response_body = {"value": response.text}
        if not isinstance(response_body, dict):
            response_body = {"value": response_body}
        error = None
        if response.is_error:
            detail = response_body.get("detail")
            error = str(detail) if detail is not None else f"HTTP {response.status_code}"
        self.events.append(
            TraceEvent(
                sequence=len(self.events) + 1,
                event_type="tool",
                action=action,
                request=request_summary,
                http_status=response.status_code,
                response=response_body,
                error=error,
            )
        )
        return response

    def decision(self, *, action: str, decision: str) -> None:
        self.events.append(
            TraceEvent(
                sequence=len(self.events) + 1,
                event_type="decision",
                action=action,
                decision=decision,
            )
        )


def _require_status(response: httpx.Response, expected: int, action: str) -> None:
    if response.status_code != expected:
        raise ExperimentExecutionError(
            f"Could not {action}: expected HTTP {expected}, "
            f"received {response.status_code} ({response.text})"
        )


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


async def run_order_intent_strategy(
    strategy_name: StrategyName,
    database_path: Path,
    task: OrderIntentTask = DEFAULT_ORDER_INTENT_TASK,
) -> OrderIntentResult:
    database_path = database_path.resolve()
    if database_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app(f"sqlite:///{database_path.as_posix()}")
    recorder = OrderIntentTraceRecorder()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://agentcrash.local"
        ) as client:
            inventory_response = await recorder.request(
                client,
                action="set_inventory",
                method="POST",
                path="/inventory",
                body={"item": task.item, "quantity_on_hand": task.initial_stock},
            )
            _require_status(inventory_response, 201, "set inventory")

            order_body = {
                "item": task.item,
                "quantity": task.quantity,
                "unit_price": task.unit_price,
                "intent_key": task.intent_key,
            }
            if strategy_name == "unsafe_duplicate":
                recorder.decision(
                    action="recovery_decision",
                    decision="Submit the intent twice without checking for an existing order.",
                )
                first = await recorder.request(
                    client, action="create_order", method="POST",
                    path="/orders", body=order_body,
                )
                _require_status(first, 201, "create first order")
                second = await recorder.request(
                    client, action="retry_create_order", method="POST",
                    path="/orders", body=order_body,
                )
                _require_status(second, 201, "create duplicate order")
            else:
                recorder.decision(
                    action="recovery_decision",
                    decision="Look up the intent key before creating an order.",
                )
                lookup = await recorder.request(
                    client,
                    action="lookup_intent",
                    method="GET",
                    path=f"/orders/by-intent-key/{task.intent_key}",
                )
                if lookup.status_code != 404:
                    raise ExperimentExecutionError(
                        f"Expected no pre-existing order for intent, got HTTP {lookup.status_code}"
                    )
                created = await recorder.request(
                    client, action="create_order", method="POST",
                    path="/orders", body=order_body,
                )
                _require_status(created, 201, "create order")

            evaluation_response = await recorder.request(
                client,
                action="evaluate_state",
                method="GET",
                path="/evaluation",
            )
            _require_status(evaluation_response, 200, "evaluate persisted state")
            evaluation = evaluation_response.json()

        order_ids = _intent_order_ids(app, task.intent_key)
        stock = _inventory_on_hand(app, task.item)
        violations = tuple(
            InvariantViolation(code=item["code"], message=item["message"])
            for item in evaluation["violations"]
        )
        return OrderIntentResult(
            strategy_name=strategy_name,
            task=task,
            task_outcome="passed" if evaluation["valid"] else "failed",
            order_ids=order_ids,
            inventory_on_hand=stock,
            successful_charge_count=0,
            invariant_violations=violations,
            trace=tuple(recorder.events),
        )
    finally:
        app.state.database.engine.dispose()


async def run_order_intent_comparison(
    database_directory: Path,
    task: OrderIntentTask = DEFAULT_ORDER_INTENT_TASK,
) -> OrderIntentComparison:
    database_directory = database_directory.resolve()
    database_paths = {
        "unsafe_duplicate": database_directory / "unsafe_duplicate.db",
        "safe_deduplicated": database_directory / "safe_deduplicated.db",
    }
    existing = [path for path in database_paths.values() if path.exists()]
    if existing:
        raise ExperimentExecutionError(
            f"Experiment databases already exist: {existing}"
        )
    results: list[OrderIntentResult] = []
    for strategy_name in ("unsafe_duplicate", "safe_deduplicated"):
        results.append(
            await run_order_intent_strategy(
                strategy_name,  # type: ignore[arg-type]
                database_paths[strategy_name],
                task,
            )
        )
    return OrderIntentComparison(scenario=SCENARIO, results=tuple(results))


def format_order_intent_comparison(comparison: OrderIntentComparison) -> str:
    lines = ["AgentCrash duplicate-order-for-intent comparison"]
    for result in comparison.results:
        outcome = "PASS" if result.task_outcome == "passed" else "FAIL"
        codes = [violation.code for violation in result.invariant_violations]
        violations = ", ".join(codes) if codes else "none"
        lines.append(
            f"{result.strategy_name}: {outcome} | "
            f"orders={len(result.order_ids)} | stock={result.inventory_on_hand} | "
            f"violations={violations}"
        )
    return "\n".join(lines)


def main() -> None:
    with TemporaryDirectory(prefix="agentcrash-order-intent-") as directory:
        comparison = asyncio.run(run_order_intent_comparison(Path(directory)))
        print(format_order_intent_comparison(comparison))
        print(json.dumps(comparison.to_dict(), indent=2))


if __name__ == "__main__":
    main()
