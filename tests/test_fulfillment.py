import json
import sqlite3
from pathlib import Path

from agentcrash.experiments.compare_order_fulfillment import (
    DEFAULT_FULFILLMENT_TASK,
    format_fulfillment_comparison,
    run_fulfillment_comparison,
    run_fulfillment_strategy,
)
from agentcrash.experiments.compare_payment_recovery import (
    run_payment_timeout_comparison,
)


async def test_unsafe_fulfill_violates_and_safe_passes(tmp_path: Path) -> None:
    comparison = await run_fulfillment_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_fulfill")
    safe = comparison.result_for("safe_pay_then_fulfill")

    assert unsafe.task_outcome == "failed"
    assert unsafe.order_status == "fulfilled"
    assert unsafe.successful_charge_count == 0
    assert [v.code for v in unsafe.invariant_violations] == ["fulfilled_without_payment"]

    assert safe.task_outcome == "passed"
    assert safe.order_status == "fulfilled"
    assert safe.successful_charge_count == 1
    assert safe.invariant_violations == ()


async def test_fulfillment_repeatable_and_isolated(tmp_path: Path) -> None:
    first = await run_fulfillment_comparison(tmp_path / "run1")
    second = await run_fulfillment_comparison(tmp_path / "run2")

    assert first.result_for("unsafe_fulfill").task_outcome == "failed"
    assert second.result_for("unsafe_fulfill").task_outcome == "failed"
    assert first.result_for("safe_pay_then_fulfill").task_outcome == "passed"
    assert second.result_for("safe_pay_then_fulfill").task_outcome == "passed"
    assert (tmp_path / "run1" / "unsafe_fulfill.db").exists()
    assert (tmp_path / "run2" / "unsafe_fulfill.db").exists()
    assert _row_counts(tmp_path / "run1" / "unsafe_fulfill.db") == (1, 0)
    assert _row_counts(tmp_path / "run1" / "safe_pay_then_fulfill.db") == (1, 1)


async def test_fulfillment_trace_order(tmp_path: Path) -> None:
    comparison = await run_fulfillment_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_fulfill")
    safe = comparison.result_for("safe_pay_then_fulfill")

    assert [e.sequence for e in unsafe.trace] == list(range(1, len(unsafe.trace) + 1))
    assert [e.action for e in unsafe.trace] == [
        "set_inventory",
        "create_order",
        "recovery_decision",
        "fulfill_order",
        "evaluate_state",
    ]
    assert [e.action for e in safe.trace] == [
        "set_inventory",
        "create_order",
        "recovery_decision",
        "charge_payment",
        "fulfill_order",
        "evaluate_state",
    ]


async def test_fulfill_missing_order_returns_404(tmp_path) -> None:
    # Uses API-level check via fresh isolated DB file.
    from agentcrash.api import create_app
    import httpx

    app = create_app(f"sqlite:///{(tmp_path / 'missing.db').as_posix()}")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        response = await test_client.post("/orders/999/fulfill")
        assert response.status_code == 404


async def test_payment_baselines_unchanged(tmp_path: Path) -> None:
    comparison = await run_payment_timeout_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_retry")
    safe = comparison.result_for("status_check")
    assert unsafe.task_outcome == "failed"
    assert unsafe.successful_charge_count == 2
    assert safe.task_outcome == "passed"
    assert safe.successful_charge_count == 1


async def test_fulfillment_comparison_serializable(tmp_path: Path) -> None:
    comparison = await run_fulfillment_comparison(tmp_path)
    serialized = json.dumps(comparison.to_dict())
    display = format_fulfillment_comparison(comparison)
    assert '"scenario": "fulfill_without_payment"' in serialized
    assert "unsafe_fulfill: FAIL" in display
    assert "safe_pay_then_fulfill: PASS" in display


def _row_counts(database_path: Path) -> tuple[int, int]:
    with sqlite3.connect(database_path) as connection:
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        payment_count = connection.execute(
            "SELECT COUNT(*) FROM payment_transactions"
        ).fetchone()[0]
    return order_count, payment_count
