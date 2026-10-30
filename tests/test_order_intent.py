import json
import sqlite3
from pathlib import Path

import httpx

from agentcrash.api import create_app
from agentcrash.experiments.compare_order_fulfillment import run_fulfillment_comparison
from agentcrash.experiments.compare_order_intent import (
    DEFAULT_ORDER_INTENT_TASK,
    format_order_intent_comparison,
    run_order_intent_comparison,
    run_order_intent_strategy,
)
from agentcrash.experiments.compare_payment_recovery import run_payment_timeout_comparison
from agentcrash.experiments.unified_report import build_unified_report


async def test_unsafe_duplicate_violates_and_safe_passes(tmp_path: Path) -> None:
    comparison = await run_order_intent_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_duplicate")
    safe = comparison.result_for("safe_deduplicated")

    assert unsafe.task_outcome == "failed"
    assert unsafe.order_ids == (1, 2)
    assert unsafe.inventory_on_hand == 6
    assert [v.code for v in unsafe.invariant_violations] == ["duplicate_orders_for_intent"]

    assert safe.task_outcome == "passed"
    assert safe.order_ids == (1,)
    assert safe.inventory_on_hand == 8
    assert safe.invariant_violations == ()


async def test_intent_repeatable_and_isolated(tmp_path: Path) -> None:
    first = await run_order_intent_comparison(tmp_path / "run1")
    second = await run_order_intent_comparison(tmp_path / "run2")

    assert first.result_for("unsafe_duplicate").task_outcome == "failed"
    assert second.result_for("unsafe_duplicate").task_outcome == "failed"
    assert first.result_for("safe_deduplicated").task_outcome == "passed"
    assert second.result_for("safe_deduplicated").task_outcome == "passed"
    assert (tmp_path / "run1" / "unsafe_duplicate.db").exists()
    assert (tmp_path / "run2" / "unsafe_duplicate.db").exists()
    assert _order_count(tmp_path / "run1" / "unsafe_duplicate.db") == 2
    assert _order_count(tmp_path / "run1" / "safe_deduplicated.db") == 1


async def test_intent_trace_order(tmp_path: Path) -> None:
    comparison = await run_order_intent_comparison(tmp_path)
    unsafe = comparison.result_for("unsafe_duplicate")
    safe = comparison.result_for("safe_deduplicated")

    assert [e.sequence for e in unsafe.trace] == list(range(1, len(unsafe.trace) + 1))
    assert [e.action for e in unsafe.trace] == [
        "set_inventory",
        "recovery_decision",
        "create_order",
        "retry_create_order",
        "evaluate_state",
    ]
    assert [e.action for e in safe.trace] == [
        "set_inventory",
        "recovery_decision",
        "lookup_intent",
        "create_order",
        "evaluate_state",
    ]
    assert safe.trace[2].http_status == 404


async def test_intent_lookup_and_invalid_inputs(tmp_path: Path) -> None:
    app = create_app(f"sqlite:///{(tmp_path / 'api.db').as_posix()}")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.get("/orders/by-intent-key/nobody")
        assert missing.status_code == 404

        unknown_item = await client.post(
            "/orders",
            json={"item": "nope", "quantity": 1, "unit_price": "1.00", "intent_key": "k1"},
        )
        assert unknown_item.status_code == 404

        bad_key = await client.post(
            "/orders",
            json={"item": "widget", "quantity": 1, "unit_price": "1.00", "intent_key": ""},
        )
        assert bad_key.status_code == 422

        await client.post("/inventory", json={"item": "widget", "quantity_on_hand": 1})
        too_many = await client.post(
            "/orders",
            json={"item": "widget", "quantity": 5, "unit_price": "1.00", "intent_key": "k2"},
        )
        assert too_many.status_code == 409

        created = await client.post(
            "/orders",
            json={"item": "widget", "quantity": 1, "unit_price": "1.00", "intent_key": "k3"},
        )
        assert created.status_code == 201
        assert created.json()["intent_key"] == "k3"
        found = await client.get("/orders/by-intent-key/k3")
        assert found.status_code == 200
        assert found.json()["id"] == created.json()["id"]
        evaluation = await client.get("/evaluation")
        assert evaluation.json()["valid"] is True


async def test_pre_existing_database_migrates(tmp_path: Path) -> None:
    from agentcrash.database import Database

    database_path = tmp_path / "legacy.db"
    database = Database(f"sqlite:///{database_path.as_posix()}")
    database.create_all()
    with database.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_orders_intent_key")
        connection.exec_driver_sql("ALTER TABLE orders DROP COLUMN intent_key")
    assert "intent_key" not in _order_columns(database_path)

    migrated = Database(f"sqlite:///{database_path.as_posix()}")
    migrated.create_all()
    assert "intent_key" in _order_columns(database_path)

    app = create_app(f"sqlite:///{database_path.as_posix()}")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/inventory", json={"item": "widget", "quantity_on_hand": 10})
        first = await client.post(
            "/orders",
            json={"item": "widget", "quantity": 2, "unit_price": "12.50", "intent_key": "legacy"},
        )
        second = await client.post(
            "/orders",
            json={"item": "widget", "quantity": 2, "unit_price": "12.50", "intent_key": "legacy"},
        )
        assert first.status_code == 201
        assert second.status_code == 201
        evaluation = await client.get("/evaluation")
        assert evaluation.json()["valid"] is False
        assert evaluation.json()["violations"][0]["code"] == "duplicate_orders_for_intent"


async def test_existing_scenarios_unchanged(tmp_path: Path) -> None:
    payment = await run_payment_timeout_comparison(tmp_path / "payment")
    assert payment.result_for("unsafe_retry").successful_charge_count == 2
    assert payment.result_for("unsafe_retry").task_outcome == "failed"
    assert payment.result_for("status_check").successful_charge_count == 1
    assert payment.result_for("status_check").task_outcome == "passed"

    fulfillment = await run_fulfillment_comparison(tmp_path / "fulfillment")
    assert fulfillment.result_for("unsafe_fulfill").task_outcome == "failed"
    assert fulfillment.result_for("safe_pay_then_fulfill").task_outcome == "passed"


async def test_intent_serializable_and_unified(tmp_path: Path) -> None:
    comparison = await run_order_intent_comparison(tmp_path)
    serialized = json.dumps(comparison.to_dict())
    display = format_order_intent_comparison(comparison)
    assert '"scenario": "duplicate_order_for_intent"' in serialized
    assert "unsafe_duplicate: FAIL" in display
    assert "safe_deduplicated: PASS" in display

    unified = build_unified_report([("intent.json", json.loads(serialized))])
    assert unified["scenarios"][0]["kind"] == "deterministic_comparison"
    assert unified["scenarios"][0]["passed"] == 1
    assert unified["scenarios"][0]["failed"] == 1
    assert unified["scenarios"][0]["usage"]["status"] == "missing"
    assert unified["scenarios"][0]["duplicate_charge_trials"] is None
    assert unified["aggregate"]["violation_counts"] == {"duplicate_orders_for_intent": 1}
    assert unified["aggregate"]["total_tokens"] == 0


def _order_count(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


def _order_columns(database_path: Path) -> list[str]:
    with sqlite3.connect(database_path) as connection:
        return [row[1] for row in connection.execute("PRAGMA table_info(orders)").fetchall()]
