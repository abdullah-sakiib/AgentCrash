from decimal import Decimal

import httpx
from fastapi import FastAPI
from sqlalchemy import update

from agentcrash.models import PaymentTransaction
from tests.conftest import create_stocked_order


async def test_evaluator_reads_persisted_state_and_detects_amount_mismatch(
    client: httpx.AsyncClient,
    app: FastAPI,
    order_payload: dict[str, object],
) -> None:
    order = await create_stocked_order(client, order_payload)
    charge_response = await client.post(
        f"/orders/{order['id']}/payments",
        json={"idempotency_key": "corrupt-me"},
    )
    assert charge_response.status_code == 200

    with app.state.database.session_factory() as session:
        session.execute(
            update(PaymentTransaction)
            .where(PaymentTransaction.idempotency_key == "corrupt-me")
            .values(amount=Decimal("1.00"))
        )
        session.commit()

    response = await client.get("/evaluation")

    assert response.status_code == 200
    result = response.json()
    assert result["valid"] is False
    assert result["orders_checked"] == 1
    assert result["payments_checked"] == 1
    assert [violation["code"] for violation in result["violations"]] == [
        "payment_amount_mismatch"
    ]


async def test_evaluator_detects_inconsistent_order_state(
    client: httpx.AsyncClient,
    app: FastAPI,
    order_payload: dict[str, object],
) -> None:
    order = await create_stocked_order(client, order_payload)
    charge_response = await client.post(
        f"/orders/{order['id']}/payments",
        json={"idempotency_key": "state-check"},
    )
    assert charge_response.status_code == 200

    from agentcrash.models import Order

    with app.state.database.session_factory() as session:
        session.execute(
            update(Order).where(Order.id == order["id"]).values(status="pending_payment")
        )
        session.commit()

    response = await client.get("/evaluation")

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert response.json()["violations"][0]["code"] == "pending_order_has_charge"


async def test_evaluator_detects_two_successful_charges_for_one_order(
    client: httpx.AsyncClient,
    order_payload: dict[str, object],
) -> None:
    order = await create_stocked_order(client, order_payload)
    payment_url = f"/orders/{order['id']}/payments"

    first_charge = await client.post(
        payment_url, json={"idempotency_key": "first-charge"}
    )
    second_charge = await client.post(
        payment_url, json={"idempotency_key": "second-charge"}
    )
    response = await client.get("/evaluation")

    assert first_charge.status_code == 200
    assert second_charge.status_code == 200
    result = response.json()
    assert result["valid"] is False
    assert result["orders_checked"] == 1
    assert result["payments_checked"] == 2
    assert result["violations"] == [
        {
            "code": "multiple_successful_charges_for_order",
            "message": (
                "Order 1 has 2 successful charges; payment_ids=[1, 2], "
                "idempotency_keys=['first-charge', 'second-charge']"
            ),
        }
    ]
