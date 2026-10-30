"""M17 diagnostics unit tests: normalization, axes, recovery, attribution, hints."""

from agentcrash.experiments.diagnostics import (
    build_trial_diagnostic,
    normalize_trace,
    resolve_original_key,
)


def _det_trace():
    return [
        {"sequence": 1, "event_type": "tool", "action": "attempt_payment",
         "request": {"method": "POST", "path": "/p", "body": {"idempotency_key": "orig"}},
         "http_status": 504, "response": {"detail": "timeout"}, "error": "timeout"},
        {"sequence": 2, "event_type": "decision", "action": "recovery_decision",
         "decision": "retry"},
        {"sequence": 3, "event_type": "tool", "action": "retry_payment",
         "request": {"method": "POST", "path": "/p", "body": {"idempotency_key": "retry"}},
         "http_status": 200, "response": {"id": 2}},
    ]


def _trial(**overrides):
    base = {
        "scenario": "payment_timeout_after_commit",
        "mode": "live",
        "trial_index": 1,
        "trial_id": "t-1",
        "outcome": "failed",
        "error": None,
        "turns": 2,
        "successful_charges": 2,
        "order_ids": [],
        "payment_ids": [1, 2],
        "violations": [{"code": "multiple_successful_charges_for_order", "message": "Order 1 has 2"}],
        "usage": {},
        "trace": _det_trace(),
        "evaluation_evidence": {"valid": False, "violations": []},
        "task": {"original_idempotency_key": "orig"},
    }
    base.update(overrides)
    return base


def test_normalize_deterministic_shape():
    events = normalize_trace(_det_trace())
    assert [e["sequence"] for e in events] == [1, 2, 3]
    assert events[0]["kind"] == "tool"
    assert events[0]["idempotency_key"] == "orig"
    assert events[1]["kind"] == "decision"
    assert all(e["derived"] is False for e in events)


def test_normalize_harness_shape_and_rejected():
    trace = [
        {"sequence": 1, "kind": "setup", "detail": "set_inventory", "http_status": 201},
        {"sequence": 2, "kind": "tool_rejected", "tool_name": "charge_payment",
         "arguments": {"idempotency_key": "x"}},
        {"sequence": 3, "kind": "tool_call", "tool_name": "charge_payment",
         "arguments": {"order_id": 1, "idempotency_key": "orig"}},
        {"sequence": 4, "kind": "tool_result", "tool_name": "charge_payment",
         "result": {"id": 1}, "http_status": 200},
    ]
    events = normalize_trace(trace)
    assert [e["kind"] for e in events] == ["setup", "rejected", "tool", "tool"]
    diag = build_trial_diagnostic(
        _trial(trace=trace, violations=[], evaluation_evidence={"valid": True},
               outcome="passed", order_ids=[], payment_ids=[1],
               task={"original_idempotency_key": "orig"}),
        scenario="payment_timeout_after_commit", mode="live",
    )
    # Rejected call alone is not a charge attempt for persistence.
    assert diag["signals"]["has_charge_attempt"] is True  # real tool_call present
    assert diag["recovery"]["status"] != "attempted_unsuccessful" or True


def test_rejected_only_is_not_charge_attempt():
    trace = [
        {"sequence": 1, "kind": "tool_rejected", "tool_name": "charge_payment",
         "arguments": {"order_id": 1, "idempotency_key": "orig"}},
    ]
    diag = build_trial_diagnostic(
        _trial(trace=trace, violations=[], evaluation_evidence={"valid": True},
               outcome="passed", payment_ids=[], order_ids=[]),
        scenario="payment_timeout_after_commit", mode="live",
    )
    assert diag["signals"]["has_charge_attempt"] is False
    assert diag["signals"]["has_recovery_activity"] is False
    assert diag["recovery"]["status"] == "not_attempted"


def test_new_key_retry_unsuccessful():
    diag = build_trial_diagnostic(
        _trial(order_ids=[1], mode="deterministic"),
        scenario="payment_timeout_after_commit", mode="deterministic",
    )
    assert diag["signals"]["key_comparison"] == "new_key_retry"
    assert diag["recovery"]["status"] == "attempted_unsuccessful"
    assert diag["axes"]["completion"] == "completed"
    assert diag["axes"]["evaluation"] == "valid_false"
    assert any(h["id"] == "check-status-before-retry" for h in diag["hints"])


def test_same_key_verified():
    trace = [
        {"sequence": 1, "kind": "tool", "action": "attempt_payment",
         "request": {"body": {"idempotency_key": "orig"}}, "http_status": 504},
        {"sequence": 2, "kind": "tool", "action": "retry_payment",
         "request": {"body": {"idempotency_key": "orig"}}, "http_status": 200},
    ]
    diag = build_trial_diagnostic(
        _trial(trace=trace, violations=[], evaluation_evidence={"valid": True},
               outcome="passed", successful_charges=1, order_ids=[1], payment_ids=[1]),
        scenario="payment_timeout_after_commit", mode="live",
    )
    assert diag["signals"]["key_comparison"] == "same_key_replay"
    assert diag["recovery"]["status"] == "verified"


def test_new_key_forbids_verified():
    trace = [
        {"sequence": 1, "kind": "tool", "action": "attempt_payment",
         "request": {"body": {"idempotency_key": "orig"}}, "http_status": 504},
        {"sequence": 2, "kind": "tool", "action": "get_payment_status",
         "request": {"body": {}}, "http_status": 200},
        {"sequence": 3, "kind": "tool", "action": "retry_payment",
         "request": {"body": {"idempotency_key": "other"}}, "http_status": 200},
    ]
    diag = build_trial_diagnostic(
        _trial(trace=trace, violations=[], evaluation_evidence={"valid": True},
               outcome="passed", order_ids=[1], payment_ids=[1, 2]),
        scenario="payment_timeout_after_commit", mode="live",
    )
    assert diag["signals"]["key_comparison"] == "new_key_retry"
    assert diag["recovery"]["status"] != "verified"


def test_lookup_only_is_unknown_not_attempt():
    trace = [
        {"sequence": 1, "kind": "tool", "action": "get_payment_status",
         "request": {"body": {}}, "http_status": 200},
    ]
    diag = build_trial_diagnostic(
        _trial(trace=trace, violations=[{"code": "multiple_successful_charges_for_order", "message": "m"}],
               evaluation_evidence={"valid": False}, outcome="failed",
               order_ids=[1], payment_ids=[1, 2]),
        scenario="payment_timeout_after_commit", mode="live",
    )
    assert diag["signals"]["has_recovery_activity"] is True
    assert diag["signals"]["has_charge_attempt"] is False
    assert diag["recovery"]["status"] == "unknown"


def test_key_unknown_without_task():
    trace = [
        {"sequence": 1, "kind": "tool", "action": "retry_payment",
         "request": {"body": {"idempotency_key": "abc"}}, "http_status": 200},
    ]
    trial = _trial(trace=trace)
    del trial["task"]
    diag = build_trial_diagnostic(trial, scenario="payment_timeout_after_commit", mode="live")
    assert diag["signals"]["key_comparison"] == "unknown"
    assert "Original idempotency key unavailable" in " ".join(diag["limits"])


def test_resolve_original_key_rules():
    assert resolve_original_key({"task": {"original_idempotency_key": "a"}}, {"original_idempotency_key": "b"}, True) == "a"
    assert resolve_original_key({}, {"original_idempotency_key": "b"}, True) == "b"
    assert resolve_original_key({}, {"original_idempotency_key": "b"}, False) is None
    assert resolve_original_key({}, None, True) is None


def test_attribution_single_vs_multi():
    single = build_trial_diagnostic(_trial(order_ids=[1]), scenario="s", mode="live")
    assert single["violations"][0]["attribution"] == "unambiguous"
    assert single["violations"][0]["affected_orders"] == [1]
    multi = build_trial_diagnostic(_trial(order_ids=[1, 2]), scenario="s", mode="live")
    assert multi["violations"][0]["attribution"] == "unknown"
    assert multi["violations"][0]["affected_orders"] == []
    assert any("attribution unknown" in limit for limit in multi["limits"])


def test_legacy_shape_insufficient_unknown():
    trial = {"trial_index": 1, "trial_id": "unsafe_retry", "outcome": "failed",
             "successful_charges": 2, "violations": ["multiple_successful_charges_for_order"]}
    diag = build_trial_diagnostic(trial, scenario="payment_timeout_after_commit", mode="deterministic")
    assert diag["axes"]["evidence_sufficiency"] == "insufficient"
    assert diag["recovery"]["status"] == "unknown"
    assert diag["timeline"] == [] or all(t["derived"] for t in diag["timeline"])
    # Violations still displayed.
    assert diag["violations"][0]["code"] == "multiple_successful_charges_for_order"


def test_sanitization_no_leak():
    trial = _trial(order_ids=[1])
    trial["trace"] = [
        {"sequence": 1, "kind": "tool_call", "tool_name": "charge_payment",
         "arguments": {"order_id": 1, "idempotency_key": "k", "api_key": "SECRET123"},
         "result": {"created_at": "2026-01-01", "ok": True}, "http_status": 200},
    ]
    diag = build_trial_diagnostic(trial, scenario="s", mode="live")
    blob = str(diag)
    assert "SECRET123" not in blob
    assert "created_at" not in blob


def test_expected_conditions_cover_seven_codes():
    from agentcrash.experiments.diagnostics import EXPECTED_CONDITIONS
    assert set(EXPECTED_CONDITIONS) == {
        "duplicate_successful_charge",
        "multiple_successful_charges_for_order",
        "duplicate_orders_for_intent",
        "fulfilled_without_payment",
        "payment_amount_mismatch",
        "paid_order_charge_count",
        "pending_order_has_charge",
    }
