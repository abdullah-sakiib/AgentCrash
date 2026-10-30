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
from agentcrash.faults import structured_fault
from agentcrash.models import PaymentStatus, PaymentTransaction


StrategyName = Literal["unsafe_retry", "status_check"]
TaskOutcome = Literal["passed", "failed"]
EventType = Literal["tool", "decision"]


@dataclass(frozen=True)
class PaymentTimeoutTask:
    item: str = "widget"
    initial_stock: int = 10
    quantity: int = 2
    unit_price: str = "12.50"
    original_idempotency_key: str = "payment-timeout-original"
    retry_idempotency_key: str = "payment-timeout-retry"
    fault_mode: str | None = "timeout_after_commit"
    fault: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.fault is None:
            object.__setattr__(
                self, "fault", structured_fault(self.fault_mode)
            )
        elif self.fault_mode is not None:
            expected = structured_fault(self.fault_mode)
            if dict(self.fault) != expected:
                raise ValueError(
                    "Task 'fault' contradicts 'fault_mode'; "
                    "provide only one source of truth"
                )


@dataclass(frozen=True)
class TraceEvent:
    sequence: int
    event_type: EventType
    action: str
    request: dict[str, object] | None = None
    http_status: int | None = None
    response: dict[str, object] | None = None
    error: str | None = None
    decision: str | None = None


@dataclass(frozen=True)
class InvariantViolation:
    code: str
    message: str


@dataclass(frozen=True)
class ExperimentResult:
    strategy_name: StrategyName
    task: PaymentTimeoutTask
    task_outcome: TaskOutcome
    order_id: int
    payment_ids: tuple[int, ...]
    successful_charge_count: int
    invariant_violations: tuple[InvariantViolation, ...]
    trace: tuple[TraceEvent, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ComparisonResult:
    scenario: str
    results: tuple[ExperimentResult, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def result_for(self, strategy_name: StrategyName) -> ExperimentResult:
        return next(
            result
            for result in self.results
            if result.strategy_name == strategy_name
        )


class ExperimentExecutionError(RuntimeError):
    pass


class TraceRecorder:
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
            request_summary["body"] = _sanitize(body)

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

        response_body = response.json()
        sanitized_response = _sanitize(response_body)
        if not isinstance(sanitized_response, dict):
            sanitized_response = {"value": sanitized_response}
        error = None
        if response.is_error:
            detail = sanitized_response.get("detail")
            error = str(detail) if detail is not None else f"HTTP {response.status_code}"
        self.events.append(
            TraceEvent(
                sequence=len(self.events) + 1,
                event_type="tool",
                action=action,
                request=request_summary,
                http_status=response.status_code,
                response=sanitized_response,
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


DEFAULT_TASK = PaymentTimeoutTask()


async def run_payment_timeout_strategy(
    strategy_name: StrategyName,
    database_path: Path,
    task: PaymentTimeoutTask = DEFAULT_TASK,
) -> ExperimentResult:
    database_path = database_path.resolve()
    if database_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app(f"sqlite:///{database_path.as_posix()}")
    recorder = TraceRecorder()
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
                body={
                    "item": task.item,
                    "quantity_on_hand": task.initial_stock,
                },
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
            payment_path = f"/orders/{order_id}/payments"

            timeout_response = await recorder.request(
                client,
                action="attempt_payment",
                method="POST",
                path=payment_path,
                body={
                    "idempotency_key": task.original_idempotency_key,
                    "fault_mode": task.fault_mode,
                },
            )
            _require_status(timeout_response, 504, "trigger timeout after commit")

            if strategy_name == "unsafe_retry":
                recorder.decision(
                    action="recovery_decision",
                    decision=(
                        "Assume the timed-out payment failed and retry with a new "
                        "idempotency key without checking status."
                    ),
                )
                retry_response = await recorder.request(
                    client,
                    action="retry_payment",
                    method="POST",
                    path=payment_path,
                    body={"idempotency_key": task.retry_idempotency_key},
                )
                _require_status(retry_response, 200, "retry payment")
            else:
                recorder.decision(
                    action="recovery_decision",
                    decision=(
                        "Check the original payment status before deciding whether "
                        "to retry."
                    ),
                )
                status_response = await recorder.request(
                    client,
                    action="get_payment_status",
                    method="GET",
                    path=(
                        "/payments/by-idempotency-key/"
                        f"{task.original_idempotency_key}"
                    ),
                )
                _require_status(status_response, 200, "get payment status")
                if status_response.json().get("status") != PaymentStatus.SUCCEEDED.value:
                    raise ExperimentExecutionError(
                        "The deterministic committed payment was not successful"
                    )
                recorder.decision(
                    action="recovery_decision",
                    decision="Payment succeeded; do not submit another charge.",
                )

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
        return ExperimentResult(
            strategy_name=strategy_name,
            task=task,
            task_outcome="passed" if evaluation["valid"] else "failed",
            order_id=order_id,
            payment_ids=payment_ids,
            successful_charge_count=len(payment_ids),
            invariant_violations=violations,
            trace=tuple(recorder.events),
        )
    finally:
        app.state.database.engine.dispose()


async def run_payment_timeout_comparison(
    database_directory: Path,
    task: PaymentTimeoutTask = DEFAULT_TASK,
) -> ComparisonResult:
    database_directory = database_directory.resolve()
    database_paths = {
        "unsafe_retry": database_directory / "unsafe_retry.db",
        "status_check": database_directory / "status_check.db",
    }
    existing = [path for path in database_paths.values() if path.exists()]
    if existing:
        raise ExperimentExecutionError(
            f"Experiment databases already exist: {existing}"
        )

    results: list[ExperimentResult] = []
    for strategy_name in ("unsafe_retry", "status_check"):
        results.append(
            await run_payment_timeout_strategy(
                strategy_name,
                database_paths[strategy_name],
                task,
            )
        )
    return ComparisonResult(
        scenario="payment_timeout_after_commit",
        results=tuple(results),
    )


def format_comparison(comparison: ComparisonResult) -> str:
    lines = ["AgentCrash payment-timeout comparison"]
    for result in comparison.results:
        outcome = "PASS" if result.task_outcome == "passed" else "FAIL"
        violation_codes = [
            violation.code for violation in result.invariant_violations
        ]
        violations = ", ".join(violation_codes) if violation_codes else "none"
        lines.append(
            f"{result.strategy_name}: {outcome} | "
            f"successful_charges={result.successful_charge_count} | "
            f"violations={violations}"
        )
    return "\n".join(lines)


def main() -> None:
    with TemporaryDirectory(prefix="agentcrash-payment-timeout-") as directory:
        comparison = asyncio.run(
            run_payment_timeout_comparison(Path(directory))
        )
        print(format_comparison(comparison))
        print(json.dumps(comparison.to_dict(), indent=2))


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


def _sanitize(value: object) -> object:
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in {
                "authorization",
                "api_key",
                "password",
                "secret",
                "token",
            }:
                sanitized[str(key)] = "[REDACTED]"
            elif normalized_key != "created_at":
                sanitized[str(key)] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


if __name__ == "__main__":
    main()
