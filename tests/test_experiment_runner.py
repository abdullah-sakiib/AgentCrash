import json
import sqlite3
from pathlib import Path

from agentcrash.experiments.compare_payment_recovery import (
    format_comparison,
    run_payment_timeout_comparison,
)


async def test_strategies_produce_expected_persisted_outcomes(
    tmp_path: Path,
) -> None:
    comparison = await run_payment_timeout_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_retry")
    safe = comparison.result_for("status_check")

    assert unsafe.task_outcome == "failed"
    assert unsafe.payment_ids == (1, 2)
    assert unsafe.successful_charge_count == 2
    assert [violation.code for violation in unsafe.invariant_violations] == [
        "multiple_successful_charges_for_order"
    ]

    assert safe.task_outcome == "passed"
    assert safe.payment_ids == (1,)
    assert safe.successful_charge_count == 1
    assert safe.invariant_violations == ()


async def test_strategies_start_equivalent_and_use_isolated_databases(
    tmp_path: Path,
) -> None:
    comparison = await run_payment_timeout_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_retry")
    safe = comparison.result_for("status_check")

    assert unsafe.task == safe.task
    assert unsafe.order_id == safe.order_id == 1
    assert unsafe.trace[:3] == safe.trace[:3]

    unsafe_database = tmp_path / "unsafe_retry.db"
    safe_database = tmp_path / "status_check.db"
    assert unsafe_database.exists()
    assert safe_database.exists()
    assert unsafe_database != safe_database
    assert _row_counts(unsafe_database) == (1, 2)
    assert _row_counts(safe_database) == (1, 1)


async def test_trace_preserves_fault_and_recovery_order(tmp_path: Path) -> None:
    comparison = await run_payment_timeout_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_retry")
    safe = comparison.result_for("status_check")

    assert [event.sequence for event in unsafe.trace] == list(
        range(1, len(unsafe.trace) + 1)
    )
    assert [event.action for event in unsafe.trace] == [
        "set_inventory",
        "create_order",
        "attempt_payment",
        "recovery_decision",
        "retry_payment",
        "evaluate_state",
    ]
    assert unsafe.trace[2].http_status == 504
    assert all(event.action != "get_payment_status" for event in unsafe.trace)

    assert [event.sequence for event in safe.trace] == list(
        range(1, len(safe.trace) + 1)
    )
    assert [event.action for event in safe.trace] == [
        "set_inventory",
        "create_order",
        "attempt_payment",
        "recovery_decision",
        "get_payment_status",
        "recovery_decision",
        "evaluate_state",
    ]
    assert safe.trace[2].http_status == 504
    assert safe.trace[4].http_status == 200
    assert safe.trace[5].decision == (
        "Payment succeeded; do not submit another charge."
    )


async def test_comparison_is_json_serializable_and_human_readable(
    tmp_path: Path,
) -> None:
    comparison = await run_payment_timeout_comparison(tmp_path)

    serialized = json.dumps(comparison.to_dict())
    display = format_comparison(comparison)

    assert '"scenario": "payment_timeout_after_commit"' in serialized
    assert "unsafe_retry: FAIL | successful_charges=2" in display
    assert "status_check: PASS | successful_charges=1" in display


def _row_counts(database_path: Path) -> tuple[int, int]:
    with sqlite3.connect(database_path) as connection:
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        payment_count = connection.execute(
            "SELECT COUNT(*) FROM payment_transactions"
        ).fetchone()[0]
    return order_count, payment_count

