import httpx

from tests.conftest import create_stocked_order


async def test_normal_payment(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={"idempotency_key": "pay-order-1"},
    )

    assert response.status_code == 200
    assert response.json()["amount"] == "25.00"
    assert response.json()["status"] == "succeeded"
    assert response.json()["replayed"] is False


async def test_timeout_after_commit_is_queryable(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    timeout_response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "timeout-payment",
            "fault_mode": "timeout_after_commit",
        },
    )
    status_response = await client.get(
        "/payments/by-idempotency-key/timeout-payment"
    )

    assert timeout_response.status_code == 504
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "succeeded"
    assert status_response.json()["amount"] == "25.00"


async def test_retry_with_same_key_does_not_duplicate_charge(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)
    payment_url = f"/orders/{order['id']}/payments"
    faulted_body = {
        "idempotency_key": "stable-key",
        "fault_mode": "timeout_after_commit",
    }

    first_response = await client.post(payment_url, json=faulted_body)
    original_payment = await client.get(
        "/payments/by-idempotency-key/stable-key"
    )
    retry_response = await client.post(payment_url, json=faulted_body)
    evaluation_response = await client.get("/evaluation")

    assert first_response.status_code == 504
    assert original_payment.status_code == 200
    assert retry_response.status_code == 200
    assert retry_response.json()["id"] == original_payment.json()["id"]
    assert retry_response.json()["replayed"] is True
    assert evaluation_response.json() == {
        "valid": True,
        "orders_checked": 1,
        "payments_checked": 1,
        "violations": [],
    }


async def test_idempotency_key_cannot_be_reused_for_another_order(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    first_order = await create_stocked_order(client, order_payload)
    second_order_response = await client.post("/orders", json=order_payload)
    second_order = second_order_response.json()

    first_charge = await client.post(
        f"/orders/{first_order['id']}/payments",
        json={"idempotency_key": "one-intent"},
    )
    conflicting_charge = await client.post(
        f"/orders/{second_order['id']}/payments",
        json={"idempotency_key": "one-intent"},
    )

    assert first_charge.status_code == 200
    assert conflicting_charge.status_code == 409


async def test_unsafe_retry_with_different_key_creates_second_charge(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)
    payment_url = f"/orders/{order['id']}/payments"

    timed_out_charge = await client.post(
        payment_url,
        json={
            "idempotency_key": "original-intent",
            "fault_mode": "timeout_after_commit",
        },
    )
    original_payment = await client.get(
        "/payments/by-idempotency-key/original-intent"
    )
    unsafe_retry = await client.post(
        payment_url, json={"idempotency_key": "replacement-intent"}
    )
    evaluation = await client.get("/evaluation")

    assert timed_out_charge.status_code == 504
    assert original_payment.status_code == 200
    assert unsafe_retry.status_code == 200
    assert unsafe_retry.json()["id"] != original_payment.json()["id"]
    assert unsafe_retry.json()["idempotency_key"] == "replacement-intent"
    assert unsafe_retry.json()["replayed"] is False
    assert evaluation.json()["payments_checked"] == 2
    assert evaluation.json()["valid"] is False
    assert evaluation.json()["violations"][0]["code"] == (
        "multiple_successful_charges_for_order"
    )
