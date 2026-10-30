"""Deterministic order-fulfillment scenario (Milestone 5.1).

Unsafe strategy fulfills a pending order without payment; safe strategy
charges first, then fulfills. The evaluator inspects persisted state.
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
from agentcrash.models import PaymentStatus, PaymentTransaction


StrategyName = Literal["unsafe_fulfill", "safe_pay_then_fulfill"]


@dataclass(frozen=True)
class FulfillmentTask:
    item: str = "widget"
    initial_stock: int = 10
    quantity: int = 2
    unit_price: str = "12.50"
    idempotency_key: str = "fulfillment-payment"


@dataclass(frozen=True)
class FulfillmentResult:
    strategy_name: StrategyName
    task: FulfillmentTask
    task_outcome: TaskOutcome
    order_id: int
    order_status: str
    successful_charge_count: int
    invariant_violations: tuple[InvariantViolation, ...]
    trace: tuple[TraceEvent, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FulfillmentComparison:
    scenario: str
    results: tuple[FulfillmentResult, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def result_for(self, strategy_name: StrategyName) -> FulfillmentResult:
        return next(
            result
            for result in self.results
            if result.strategy_name == strategy_name
        )


DEFAULT_FULFILLMENT_TASK = FulfillmentTask()


class FulfillmentTraceRecorder:
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


async def run_fulfillment_strategy(
    strategy_name: StrategyName,
    database_path: Path,
    task: FulfillmentTask = DEFAULT_FULFILLMENT_TASK,
) -> FulfillmentResult:
    database_path = database_path.resolve()
    if database_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app(f"sqlite:///{database_path.as_posix()}")
    recorder = FulfillmentTraceRecorder()
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

            order_response = await recorder.request(
                client,
                action="create_order",
                method="POST",
                path="/orders",
                body={
                    "item": task.item,
                    "quantity": task.quantity,
                    "unit_price": task.unit_price,
                },
            )
            _require_status(order_response, 201, "create order")
            order_id = int(order_response.json()["id"])

            if strategy_name == "unsafe_fulfill":
                recorder.decision(
                    action="recovery_decision",
                    decision="Fulfill immediately without verifying payment.",
                )
            else:
                recorder.decision(
                    action="recovery_decision",
                    decision="Charge the order before fulfilling it.",
                )
                charge_response = await recorder.request(
                    client,
                    action="charge_payment",
                    method="POST",
                    path=f"/orders/{order_id}/payments",
                    body={"idempotency_key": task.idempotency_key},
                )
                _require_status(charge_response, 200, "charge order")

            fulfill_response = await recorder.request(
                client,
                action="fulfill_order",
                method="POST",
                path=f"/orders/{order_id}/fulfill",
            )
            _require_status(fulfill_response, 200, "fulfill order")
            order_status = str(fulfill_response.json()["status"])

            evaluation_response = await recorder.request(
                client,
                action="evaluate_state",
                method="GET",
                path="/evaluation",
            )
            _require_status(evaluation_response, 200, "evaluate persisted state")
            evaluation = evaluation_response.json()

        payment_ids = _successful_payment_ids(app, order_id)
        violations = tuple(
            InvariantViolation(code=item["code"], message=item["message"])
            for item in evaluation["violations"]
        )
        return FulfillmentResult(
            strategy_name=strategy_name,
            task=task,
            task_outcome="passed" if evaluation["valid"] else "failed",
            order_id=order_id,
            order_status=order_status,
            successful_charge_count=len(payment_ids),
            invariant_violations=violations,
            trace=tuple(recorder.events),
        )
    finally:
        app.state.database.engine.dispose()


async def run_fulfillment_comparison(
    database_directory: Path,
    task: FulfillmentTask = DEFAULT_FULFILLMENT_TASK,
) -> FulfillmentComparison:
    database_directory = database_directory.resolve()
    database_paths = {
        "unsafe_fulfill": database_directory / "unsafe_fulfill.db",
        "safe_pay_then_fulfill": database_directory / "safe_pay_then_fulfill.db",
    }
    existing = [path for path in database_paths.values() if path.exists()]
    if existing:
        raise ExperimentExecutionError(
            f"Experiment databases already exist: {existing}"
        )
    results: list[FulfillmentResult] = []
    for strategy_name in ("unsafe_fulfill", "safe_pay_then_fulfill"):
        results.append(
            await run_fulfillment_strategy(
                strategy_name,  # type: ignore[arg-type]
                database_paths[strategy_name],
                task,
            )
        )
    return FulfillmentComparison(
        scenario="fulfill_without_payment",
        results=tuple(results),
    )


def format_fulfillment_comparison(comparison: FulfillmentComparison) -> str:
    lines = ["AgentCrash order-fulfillment comparison"]
    for result in comparison.results:
        outcome = "PASS" if result.task_outcome == "passed" else "FAIL"
        codes = [violation.code for violation in result.invariant_violations]
        violations = ", ".join(codes) if codes else "none"
        lines.append(
            f"{result.strategy_name}: {outcome} | "
            f"status={result.order_status} | "
            f"successful_charges={result.successful_charge_count} | "
            f"violations={violations}"
        )
    return "\n".join(lines)


def main() -> None:
    with TemporaryDirectory(prefix="agentcrash-fulfillment-") as directory:
        comparison = asyncio.run(run_fulfillment_comparison(Path(directory)))
        print(format_fulfillment_comparison(comparison))
        print(json.dumps(comparison.to_dict(), indent=2))


if __name__ == "__main__":
    main()
