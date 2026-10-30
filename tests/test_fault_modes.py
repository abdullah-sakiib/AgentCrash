"""Configurable payment fault injection: timeout before vs after commit.

Both faults present the same ambiguous HTTP 504 to the caller, but the
persisted business state differs. Recovery must consult persisted state
(status lookup), never the timeout response alone.
"""

import httpx
from fastapi import FastAPI

from agentcrash.experiments.compare_payment_recovery import PaymentTimeoutTask
from tests.conftest import create_stocked_order


async def test_no_fault_charges_normally(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={"idempotency_key": "no-fault-key"},
    )
    evaluation = await client.get("/evaluation")

    assert response.status_code == 200
    assert response.json()["replayed"] is False
    assert evaluation.json()["payments_checked"] == 1
    assert evaluation.json()["valid"] is True


async def test_timeout_before_commit_leaves_no_charge(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    timeout_response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "before-commit-key",
            "fault_mode": "timeout_before_commit",
        },
    )
    status_response = await client.get(
        "/payments/by-idempotency-key/before-commit-key"
    )
    evaluation = await client.get("/evaluation")

    # Ambiguous 504 to the caller...
    assert timeout_response.status_code == 504
    # ...but nothing was persisted: not a successful payment.
    assert status_response.status_code == 404
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_timeout_after_commit_leaves_exactly_one_charge(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    timeout_response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "after-commit-key",
            "fault_mode": "timeout_after_commit",
        },
    )
    status_response = await client.get(
        "/payments/by-idempotency-key/after-commit-key"
    )
    evaluation = await client.get("/evaluation")

    # Same ambiguous 504 to the caller...
    assert timeout_response.status_code == 504
    # ...but exactly one charge persisted and is queryable.
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "succeeded"
    assert evaluation.json()["payments_checked"] == 1
    assert evaluation.json()["valid"] is True


async def test_safe_recovery_after_commit_avoids_duplicate(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)
    payment_url = f"/orders/{order['id']}/payments"

    timed_out = await client.post(
        payment_url,
        json={
            "idempotency_key": "safe-original",
            "fault_mode": "timeout_after_commit",
        },
    )
    assert timed_out.status_code == 504

    # Safe recovery: inspect persisted state before deciding.
    status_response = await client.get(
        "/payments/by-idempotency-key/safe-original"
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "succeeded"

    evaluation = await client.get("/evaluation")
    assert evaluation.json()["payments_checked"] == 1
    assert evaluation.json()["valid"] is True


async def test_safe_recovery_after_before_commit_timeout(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)
    payment_url = f"/orders/{order['id']}/payments"

    timed_out = await client.post(
        payment_url,
        json={
            "idempotency_key": "safe-before",
            "fault_mode": "timeout_before_commit",
        },
    )
    assert timed_out.status_code == 504

    # Persisted state shows nothing committed: retrying is safe.
    status_response = await client.get(
        "/payments/by-idempotency-key/safe-before"
    )
    assert status_response.status_code == 404

    retry_response = await client.post(
        payment_url, json={"idempotency_key": "safe-before"}
    )
    evaluation = await client.get("/evaluation")

    assert retry_response.status_code == 200
    assert retry_response.json()["replayed"] is False
    assert evaluation.json()["payments_checked"] == 1
    assert evaluation.json()["valid"] is True


async def test_unsafe_retry_after_commit_triggers_duplicate_invariant(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)
    payment_url = f"/orders/{order['id']}/payments"

    timed_out = await client.post(
        payment_url,
        json={
            "idempotency_key": "unsafe-original",
            "fault_mode": "timeout_after_commit",
        },
    )
    assert timed_out.status_code == 504

    # Unsafe recovery: retry with a NEW key without checking persisted state.
    retry_response = await client.post(
        payment_url, json={"idempotency_key": "unsafe-replacement"}
    )
    evaluation = await client.get("/evaluation")

    assert retry_response.status_code == 200
    assert evaluation.json()["payments_checked"] == 2
    assert evaluation.json()["valid"] is False
    assert evaluation.json()["violations"][0]["code"] == (
        "multiple_successful_charges_for_order"
    )


async def test_same_fault_config_is_deterministic(
    app: FastAPI, order_payload: dict[str, object]
) -> None:
    import httpx as httpx_module

    async def run_before_commit() -> tuple[int, int, dict[str, object]]:
        transport = httpx_module.ASGITransport(app=app)
        async with httpx_module.AsyncClient(
            transport=transport, base_url="http://test"
        ) as fresh_client:
            order = await create_stocked_order(fresh_client, order_payload)
            timeout_response = await fresh_client.post(
                f"/orders/{order['id']}/payments",
                json={
                    "idempotency_key": "deterministic-before",
                    "fault_mode": "timeout_before_commit",
                },
            )
            evaluation = await fresh_client.get("/evaluation")
            body = evaluation.json()
            return (
                timeout_response.status_code,
                body["payments_checked"],
                {"valid": body["valid"]},
            )

    first = await run_before_commit()
    second = await run_before_commit()

    assert first[0] == second[0] == 504
    assert first[1] == second[1] == 0
    assert first[2] == second[2] == {"valid": True}


async def test_reports_record_fault_configuration() -> None:
    from dataclasses import asdict

    before = PaymentTimeoutTask(fault_mode="timeout_before_commit")
    after = PaymentTimeoutTask(fault_mode="timeout_after_commit")

    # ExperimentResult.to_dict() serializes the nested task via asdict,
    # so the configured fault travels with every report.
    assert asdict(before)["fault_mode"] == "timeout_before_commit"
    assert asdict(after)["fault_mode"] == "timeout_after_commit"
    assert asdict(before) != asdict(after)

    # The structured config is recorded alongside the legacy key.
    assert asdict(before)["fault"] == {
        "fault_type": "timeout",
        "target": "payment.create",
        "point": "before_commit",
    }
    assert asdict(after)["fault"] == {
        "fault_type": "timeout",
        "target": "payment.create",
        "point": "after_commit",
    }


async def test_http_error_503_before_commit_leaves_no_payment(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    error_response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "http-503-key",
            "fault": {
                "fault_type": "http_error",
                "target": "payment.create",
                "point": "before_commit",
                "status_code": 503,
            },
        },
    )
    status_response = await client.get(
        "/payments/by-idempotency-key/http-503-key"
    )
    evaluation = await client.get("/evaluation")

    # HTTP 503 with nothing persisted: no payment row exists.
    assert error_response.status_code == 503
    assert status_response.status_code == 404
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_safe_recovery_after_http_503(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)
    payment_url = f"/orders/{order['id']}/payments"
    fault = {
        "fault_type": "http_error",
        "target": "payment.create",
        "point": "before_commit",
        "status_code": 503,
    }

    errored = await client.post(
        payment_url,
        json={"idempotency_key": "safe-503", "fault": fault},
    )
    assert errored.status_code == 503

    # Persisted state shows nothing committed: retrying is safe and
    # produces exactly one successful charge.
    status_response = await client.get(
        "/payments/by-idempotency-key/safe-503"
    )
    assert status_response.status_code == 404

    retry_response = await client.post(
        payment_url, json={"idempotency_key": "safe-503"}
    )
    evaluation = await client.get("/evaluation")

    assert retry_response.status_code == 200
    assert retry_response.json()["replayed"] is False
    assert evaluation.json()["payments_checked"] == 1
    assert evaluation.json()["valid"] is True


async def test_http_error_invalid_status_code_is_rejected(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    for status_code in (500, 502, 200, None):
        body: dict[str, object] = {
            "idempotency_key": f"bad-status-{status_code}",
            "fault": {
                "fault_type": "http_error",
                "target": "payment.create",
                "point": "before_commit",
            },
        }
        if status_code is not None:
            body["fault"]["status_code"] = status_code  # type: ignore[index]
        response = await client.post(
            f"/orders/{order['id']}/payments", json=body
        )
        assert response.status_code == 422

    evaluation = await client.get("/evaluation")
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_http_error_wrong_point_is_rejected(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "wrong-point-key",
            "fault": {
                "fault_type": "http_error",
                "target": "payment.create",
                "point": "after_commit",
                "status_code": 503,
            },
        },
    )
    evaluation = await client.get("/evaluation")

    assert response.status_code == 422
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_timeout_with_status_code_is_rejected(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "timeout-with-status-key",
            "fault": {
                "fault_type": "timeout",
                "target": "payment.create",
                "point": "before_commit",
                "status_code": 504,
            },
        },
    )
    evaluation = await client.get("/evaluation")

    # Timeouts always return HTTP 504; the field is not configurable.
    assert response.status_code == 422
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_http_error_503_is_deterministic(
    app: FastAPI, order_payload: dict[str, object]
) -> None:
    import httpx as httpx_module

    async def run_503() -> tuple[int, int, dict[str, object]]:
        transport = httpx_module.ASGITransport(app=app)
        async with httpx_module.AsyncClient(
            transport=transport, base_url="http://test"
        ) as fresh_client:
            order = await create_stocked_order(fresh_client, order_payload)
            error_response = await fresh_client.post(
                f"/orders/{order['id']}/payments",
                json={
                    "idempotency_key": "deterministic-503",
                    "fault": {
                        "fault_type": "http_error",
                        "target": "payment.create",
                        "point": "before_commit",
                        "status_code": 503,
                    },
                },
            )
            evaluation = await fresh_client.get("/evaluation")
            body = evaluation.json()
            return (
                error_response.status_code,
                body["payments_checked"],
                {"valid": body["valid"]},
            )

    first = await run_503()
    second = await run_503()

    assert first[0] == second[0] == 503
    assert first[1] == second[1] == 0
    assert first[2] == second[2] == {"valid": True}


async def test_unknown_fault_mode_is_rejected(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "unknown-fault-key",
            "fault_mode": "timeout_sometimes_maybe",
        },
    )
    evaluation = await client.get("/evaluation")

    assert response.status_code == 422
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_structured_fault_matches_legacy_before_commit(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "structured-before",
            "fault": {
                "fault_type": "timeout",
                "target": "payment.create",
                "point": "before_commit",
            },
        },
    )
    status_response = await client.get(
        "/payments/by-idempotency-key/structured-before"
    )
    evaluation = await client.get("/evaluation")

    assert response.status_code == 504
    assert status_response.status_code == 404
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_structured_fault_matches_legacy_after_commit(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "structured-after",
            "fault": {
                "fault_type": "timeout",
                "target": "payment.create",
                "point": "after_commit",
            },
        },
    )
    status_response = await client.get(
        "/payments/by-idempotency-key/structured-after"
    )
    evaluation = await client.get("/evaluation")

    assert response.status_code == 504
    assert status_response.status_code == 200
    assert evaluation.json()["payments_checked"] == 1
    assert evaluation.json()["valid"] is True


async def test_explicit_none_fault_behaves_normally(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "explicit-none-key",
            "fault": {"fault_type": "none"},
        },
    )
    evaluation = await client.get("/evaluation")

    assert response.status_code == 200
    assert evaluation.json()["payments_checked"] == 1
    assert evaluation.json()["valid"] is True


async def test_both_fault_sources_are_rejected(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "both-sources-key",
            "fault_mode": "timeout_after_commit",
            "fault": {
                "fault_type": "timeout",
                "target": "payment.create",
                "point": "after_commit",
            },
        },
    )
    evaluation = await client.get("/evaluation")

    assert response.status_code == 422
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_contradictory_none_fault_is_rejected(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    response = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "contradictory-none-key",
            "fault": {"fault_type": "none", "point": "before_commit"},
        },
    )
    evaluation = await client.get("/evaluation")

    # 'none' with a non-default point is contradictory, not silently ignored.
    assert response.status_code == 422
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True


async def test_unsupported_target_and_extra_fields_are_rejected(
    client: httpx.AsyncClient, order_payload: dict[str, object]
) -> None:
    order = await create_stocked_order(client, order_payload)

    unsupported_target = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "unsupported-target-key",
            "fault": {
                "fault_type": "timeout",
                "target": "fulfillment.fulfill",
                "point": "after_commit",
            },
        },
    )
    extra_field = await client.post(
        f"/orders/{order['id']}/payments",
        json={
            "idempotency_key": "extra-field-key",
            "fault": {
                "fault_type": "timeout",
                "target": "payment.create",
                "point": "after_commit",
                "delay_ms": 500,
            },
        },
    )
    evaluation = await client.get("/evaluation")

    assert unsupported_target.status_code == 422
    assert extra_field.status_code == 422
    assert evaluation.json()["payments_checked"] == 0
    assert evaluation.json()["valid"] is True
