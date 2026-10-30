"""Actionable reliability diagnostics (M17).

Pure, read-only computation over stored trial reports. No I/O, no model
calls, no simulator access. Never rewrites input reports and never redefines
evaluator or scorecard semantics.
"""

from __future__ import annotations

from typing import Any, Mapping

DIAGNOSTICS_VERSION = "m17/v1"

CHARGE_LABELS = frozenset({"attempt_payment", "retry_payment", "charge_payment"})
LOOKUP_LABELS = frozenset({"get_payment_status"})

EXPECTED_CONDITIONS: dict[str, str] = {
    "duplicate_successful_charge": (
        "At most one succeeded charge per idempotency key."
    ),
    "multiple_successful_charges_for_order": (
        "At most one succeeded charge per order."
    ),
    "duplicate_orders_for_intent": (
        "At most one order per customer intent key."
    ),
    "fulfilled_without_payment": (
        "Fulfilled orders require at least one succeeded charge."
    ),
    "payment_amount_mismatch": (
        "Each payment amount equals its order total."
    ),
    "paid_order_charge_count": (
        "Orders with status 'paid' require at least one succeeded charge."
    ),
    "pending_order_has_charge": (
        "Orders with status 'pending_payment' require no succeeded charge."
    ),
}

CHARGE_SCOPED_CODES = frozenset(
    {"multiple_successful_charges_for_order", "duplicate_successful_charge"}
)

_SENSITIVE_NAMES = frozenset(
    {"authorization", "api_key", "password", "secret", "token"}
)


def _sanitize_value(value: Any) -> Any:
    """Mirror harness sanitization: redact secrets, drop created_at."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SENSITIVE_NAMES:
                cleaned[str(key)] = "[REDACTED]"
            elif lowered != "created_at":
                cleaned[str(key)] = _sanitize_value(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _sequence_of(event: Mapping[str, Any]) -> int | None:
    return _as_int(event.get("sequence"))


def _extract_key(event: Mapping[str, Any]) -> str | None:
    for container_key in ("arguments", "request"):
        container = event.get(container_key)
        body: Any = None
        if isinstance(container, Mapping):
            if "idempotency_key" in container and isinstance(
                container.get("idempotency_key"), str
            ):
                candidate = str(container.get("idempotency_key"))
                if candidate.strip():
                    return candidate
            body = container.get("body")
        if isinstance(body, Mapping) and isinstance(body.get("idempotency_key"), str):
            candidate = str(body.get("idempotency_key"))
            if candidate.strip():
                return candidate
    result = event.get("result")
    if isinstance(result, Mapping) and isinstance(result.get("idempotency_key"), str):
        candidate = str(result.get("idempotency_key"))
        if candidate.strip():
            return candidate
    response = event.get("response")
    if isinstance(response, Mapping) and isinstance(
        response.get("idempotency_key"), str
    ):
        candidate = str(response.get("idempotency_key"))
        if candidate.strip():
            return candidate
    return None


def normalize_trace(trace: Any) -> list[dict[str, Any]]:
    """Normalize deterministic and harness trace shapes without inventing data."""
    if not isinstance(trace, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(trace):
        if not isinstance(raw, Mapping):
            normalized.append(
                {
                    "sequence": None,
                    "source_kind": None,
                    "kind": "unknown",
                    "label": f"event_{index}",
                    "turn": None,
                    "http_status": None,
                    "idempotency_key": None,
                    "error": None,
                    "derived": False,
                }
            )
            continue
        event = dict(raw)
        sequence = _sequence_of(event)
        raw_kind = event.get("kind", event.get("event_type"))
        action = event.get("action")
        detail = event.get("detail")
        tool_name = event.get("tool_name")
        label_raw = tool_name or action or detail
        label = str(label_raw) if label_raw is not None else "event"
        http_status = event.get("http_status")
        http_status = http_status if isinstance(http_status, int) else None
        turn = event.get("turn")
        turn = turn if isinstance(turn, int) and not isinstance(turn, bool) else None
        error = event.get("error")
        error = str(error)[:500] if isinstance(error, str) and error else None

        if raw_kind == "tool" or event.get("event_type") == "tool":
            kind = "tool"
        elif raw_kind == "decision" or event.get("event_type") == "decision":
            kind = "decision"
        elif raw_kind == "setup":
            kind = "setup"
        elif raw_kind == "tool_rejected":
            kind = "rejected"
        elif raw_kind in ("model_request", "model_response"):
            kind = "model"
        elif raw_kind == "evaluation":
            kind = "evaluation"
        elif raw_kind == "error":
            kind = "error"
        elif raw_kind == "tool_call":
            kind = "tool"
        elif raw_kind == "tool_result":
            kind = "tool"
        else:
            kind = "unknown"
        normalized.append(
            {
                "sequence": sequence,
                "source_kind": str(raw_kind) if raw_kind is not None else None,
                "kind": kind,
                "label": label,
                "turn": turn,
                "http_status": http_status,
                "idempotency_key": _extract_key(event),
                "error": error,
                "derived": False,
            }
        )
    normalized.sort(
        key=lambda e: (e["sequence"] is None, e["sequence"] if e["sequence"] is not None else 0)
    )
    return normalized


def _violation_codes(trial: Mapping[str, Any]) -> list[str]:
    codes: list[str] = []
    for key in ("violations", "invariant_violations"):
        entries = trial.get(key)
        if isinstance(entries, (list, tuple)):
            for entry in entries:
                if isinstance(entry, str) and entry not in codes:
                    codes.append(entry)
                elif isinstance(entry, Mapping) and isinstance(entry.get("code"), str):
                    if entry["code"] not in codes:
                        codes.append(str(entry["code"]))
    return codes


def _evaluation_valid(trial: Mapping[str, Any]) -> bool | None:
    evidence = trial.get("evaluation_evidence")
    if isinstance(evidence, Mapping) and isinstance(evidence.get("valid"), bool):
        return bool(evidence["valid"])
    return None


def _order_ids(trial: Mapping[str, Any]) -> list[int]:
    ids: list[int] = []
    single = trial.get("order_id")
    if _as_int(single) is not None and single not in ids:
        ids.append(int(single))
    raw = trial.get("order_ids")
    if isinstance(raw, (list, tuple)):
        for value in raw:
            if _as_int(value) is not None and int(value) not in ids:
                ids.append(int(value))
    return ids


def _payment_ids(trial: Mapping[str, Any]) -> list[int]:
    raw = trial.get("payment_ids")
    ids: list[int] = []
    if isinstance(raw, (list, tuple)):
        for value in raw:
            if _as_int(value) is not None and int(value) not in ids:
                ids.append(int(value))
    return ids


def resolve_original_key(
    trial: Mapping[str, Any],
    report_task: Any,
    report_task_unambiguous: bool,
) -> str | None:
    """Conservative original-key resolution; unknown unless proven."""
    task = trial.get("task")
    if isinstance(task, Mapping):
        candidate = task.get("original_idempotency_key")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if report_task_unambiguous and isinstance(report_task, Mapping):
        candidate = report_task.get("original_idempotency_key")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _charge_attempts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["kind"] == "tool" and e["label"] in CHARGE_LABELS
    ]


def _successful_lookups(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["kind"] == "tool"
        and e["label"] in LOOKUP_LABELS
        and e["http_status"] == 200
    ]


def build_trial_diagnostic(
    trial: Mapping[str, Any],
    *,
    scenario: str = "unknown",
    mode: str = "unknown",
    report_task: Any = None,
    report_task_unambiguous: bool = False,
) -> dict[str, Any]:
    """Build one trial diagnostic; never raises on malformed input."""
    from agentcrash.experiments.scorecard import classify_trial

    trial_mode = trial.get("mode") if isinstance(trial.get("mode"), str) else mode
    live_like = trial_mode == "live"
    try:
        classified = classify_trial(trial, live_like=live_like)
    except Exception:
        classified = {
            "class": "unevaluable",
            "error": None,
            "outcome": None,
            "completed": None,
            "evaluated": False,
            "inconsistent": False,
            "violations": [],
        }
    violations = list(classified.get("violations") or _violation_codes(trial))
    evaluated = bool(classified.get("evaluated"))
    valid = _evaluation_valid(trial)
    outcome = classified.get("outcome")
    if outcome is None:
        outcome = trial.get("outcome", trial.get("task_outcome"))

    if valid is True:
        evaluation_axis = "valid_true"
    elif valid is False:
        evaluation_axis = "valid_false"
    else:
        evaluation_axis = "missing"

    error_present = classified.get("error") is not None
    trace = normalize_trace(trial.get("trace"))
    has_trace = len(trace) > 0
    if error_present:
        completion = "error"
    elif classified.get("completed") is True:
        completion = "completed"
    elif classified.get("completed") is False:
        completion = "not_completed"
    else:
        completion = "unknown"

    consistency = "inconsistent" if classified.get("inconsistent") else "consistent"
    evidence_sufficiency = (
        "sufficient" if (valid is not None and has_trace) else "insufficient"
    )

    # Recovery signals.
    charges = _charge_attempts(trace)
    lookups = _successful_lookups(trace)
    has_charge_attempt = len(charges) > 0
    has_recovery_activity = has_charge_attempt or len(lookups) > 0
    original_key = resolve_original_key(trial, report_task, report_task_unambiguous)
    subsequent_keys = [e["idempotency_key"] for e in charges if e["idempotency_key"]]
    if original_key is None or not subsequent_keys:
        key_comparison = "unknown"
    elif all(key == original_key for key in subsequent_keys):
        key_comparison = "same_key_replay"
    elif any(key != original_key for key in subsequent_keys):
        key_comparison = "new_key_retry"
    else:
        key_comparison = "unknown"

    order_ids = _order_ids(trial)
    single_order = len(order_ids) == 1
    live_context = "live_agent" if live_like else "strategy_executed"

    same_key_verified = (
        evidence_sufficiency == "sufficient"
        and valid is True
        and not violations
        and single_order
        and key_comparison == "same_key_replay"
    )
    lookup_only_verified = (
        evidence_sufficiency == "sufficient"
        and valid is True
        and not violations
        and single_order
        and len(lookups) > 0
        and not has_charge_attempt
    )
    if not has_trace:
        recovery_status = "unknown"
    elif not has_recovery_activity:
        recovery_status = "not_attempted"
    elif (
        has_charge_attempt
        and evidence_sufficiency == "sufficient"
        and (valid is False or violations)
    ):
        recovery_status = "attempted_unsuccessful"
    elif same_key_verified or lookup_only_verified:
        recovery_status = "verified"
    else:
        recovery_status = "unknown"
    # New-key retry always forbids verified, even if a branch above matched.
    if key_comparison == "new_key_retry" and recovery_status == "verified":
        recovery_status = "unknown"
    if key_comparison == "new_key_retry" and recovery_status not in (
        "attempted_unsuccessful",
    ):
        # A new-key retry with failed/violated state is unsuccessful;
        # otherwise the outcome cannot be verified from available evidence.
        if valid is False or violations:
            recovery_status = "attempted_unsuccessful"
        else:
            recovery_status = "unknown"

    # Violation explanations with structured-only attribution.
    payment_ids = _payment_ids(trial)
    violation_entries: list[dict[str, Any]] = []
    for code in violations:
        expected = EXPECTED_CONDITIONS.get(code, "No documented expectation.")
        if single_order:
            affected_orders = list(order_ids)
            if code in CHARGE_SCOPED_CODES and payment_ids:
                affected_payments = list(payment_ids)
            elif code == "payment_amount_mismatch" and len(payment_ids) == 1:
                affected_payments = list(payment_ids)
            else:
                affected_payments = []
            attribution = "unambiguous" if affected_orders else "unknown"
            if code == "duplicate_successful_charge":
                # Key-level scope is not present in trial evidence.
                affected_orders = []
                affected_payments = []
                attribution = "unknown"
        else:
            affected_orders = []
            affected_payments = []
            attribution = "unknown"
        message = ""
        for key in ("violations", "invariant_violations"):
            entries = trial.get(key)
            if isinstance(entries, (list, tuple)):
                for entry in entries:
                    if isinstance(entry, Mapping) and entry.get("code") == code:
                        raw_message = entry.get("message", "")
                        message = str(raw_message)[:1000] if raw_message else ""
                        break
        preceding = [
            e["sequence"]
            for e in trace
            if e["kind"] == "tool" and e["sequence"] is not None
        ][:10]
        violation_entries.append(
            {
                "code": code,
                "message": _sanitize_value(message),
                "expected": expected,
                "affected_orders": affected_orders,
                "affected_payments": affected_payments,
                "attribution": attribution,
                "events_before_evaluation": preceding,
            }
        )

    # Timeline with derived evaluation marker.
    timeline: list[dict[str, Any]] = []
    for event in trace:
        if event["label"] in CHARGE_LABELS and event["http_status"] == 504:
            key_fact = "Charge submission returned HTTP 504; commit state unknown from this response alone."
        elif event["label"] in LOOKUP_LABELS and event["http_status"] == 200:
            key_fact = "Status lookup returned the persisted charge result."
        elif event["label"] in CHARGE_LABELS and event["http_status"] == 200:
            key_fact = "Charge submission returned HTTP 200."
        elif event["kind"] == "rejected":
            key_fact = "Tool call rejected before simulator execution; no state could persist from this event."
        elif event["kind"] == "setup":
            key_fact = "Harness-controlled setup; not agent recovery."
        else:
            key_fact = "Recorded trace event."
        timeline.append(
            {
                "sequence": event["sequence"],
                "kind": event["kind"],
                "label": _sanitize_value(event["label"]),
                "turn": event["turn"],
                "http_status": event["http_status"],
                "key_fact": key_fact,
                "derived": False,
            }
        )
    if valid is not None or violations:
        timeline.append(
            {
                "sequence": None,
                "kind": "evaluation",
                "label": "evaluate_state",
                "turn": None,
                "http_status": 200 if valid is not None else None,
                "key_fact": "Persisted-state evaluation verdict; authoritative for business state.",
                "derived": True,
            }
        )

    # Evidence-gated hints.
    hints: list[dict[str, Any]] = []
    limits: list[str] = []

    def _seq(label: str) -> list[Any]:
        return [e["sequence"] for e in trace if e["label"] == label and e["sequence"] is not None]

    has_504 = any(e["http_status"] == 504 for e in trace if e["kind"] == "tool")
    has_503 = any(e["http_status"] == 503 for e in trace if e["kind"] == "tool")
    has_409 = any(e["http_status"] == 409 for e in trace)
    has_rejected = any(e["kind"] == "rejected" for e in trace)
    has_turn_exhausted = any(
        (e["kind"] == "error" and e.get("error") and "max turns" in str(e.get("error")).lower())
        for e in trace
    ) or (isinstance(classified.get("error"), str) and "max turns" in classified["error"].lower())

    if (
        has_504
        and key_comparison == "new_key_retry"
        and any(c in CHARGE_SCOPED_CODES for c in violations)
        and evidence_sufficiency == "sufficient"
    ):
        hints.append(
            {
                "id": "check-status-before-retry",
                "guidance": "Inspect payment status before retrying a potentially committed operation.",
                "evidence_refs": _seq("attempt_payment") + _seq("retry_payment") + _seq("charge_payment"),
            }
        )
    if (
        has_503
        and key_comparison == "same_key_replay"
        and valid is True
        and evidence_sufficiency == "sufficient"
    ):
        hints.append(
            {
                "id": "same-key-replay-pattern",
                "guidance": "Same-key replay after an ambiguous failure preserved exactly-once state; keep this pattern.",
                "evidence_refs": _seq("attempt_payment") + _seq("charge_payment"),
            }
        )
    if "fulfilled_without_payment" in violations and evidence_sufficiency == "sufficient":
        hints.append(
            {
                "id": "verify-payment-before-fulfill",
                "guidance": "Verify persisted payment state before fulfillment.",
                "evidence_refs": [e["sequence"] for e in trace if e["sequence"] is not None][:5],
            }
        )
    if "duplicate_orders_for_intent" in violations and evidence_sufficiency == "sufficient":
        hints.append(
            {
                "id": "lookup-intent-before-create",
                "guidance": "Look up the customer intent before creating another order.",
                "evidence_refs": [e["sequence"] for e in trace if e["sequence"] is not None][:5],
            }
        )
    if has_409:
        hints.append(
            {
                "id": "key-bound-to-other-order",
                "guidance": "Idempotency keys are order-bound; do not reuse a key across orders.",
                "evidence_refs": [e["sequence"] for e in trace if e["http_status"] == 409 and e["sequence"] is not None],
            }
        )
    if has_rejected:
        hints.append(
            {
                "id": "tool-contract",
                "guidance": "Inspect agent tool-call construction; rejected calls never reach the simulator.",
                "evidence_refs": [e["sequence"] for e in trace if e["kind"] == "rejected" and e["sequence"] is not None],
            }
        )
    if has_turn_exhausted:
        hints.append(
            {
                "id": "turn-budget",
                "guidance": "Inspect turn budget and termination logic; the agent did not finish within bounds.",
                "evidence_refs": [],
            }
        )
    if evidence_sufficiency == "insufficient":
        limits.append("Evidence insufficient for timeline or recovery conclusions; re-run the trial for full diagnostics.")
    if key_comparison == "unknown" and has_charge_attempt:
        limits.append("Original idempotency key unavailable; key comparison unknown.")
    if not single_order and violations:
        limits.append("Multiple or missing order scope; affected-ID attribution unknown.")
    if has_504:
        limits.append("Commit point comes from configured fault details and persisted evaluation, not from the 504 alone.")

    if error_present:
        headline = "Infrastructure or model error recorded; business state evaluated separately."
    elif valid is False or violations:
        headline = "Business-state invariant violated." if recovery_status == "attempted_unsuccessful" else "Business-state invariant violated; recovery status unknown."
    elif valid is True and recovery_status == "verified":
        headline = "Recovered with verified persisted-state evidence."
    elif valid is True and recovery_status == "not_attempted":
        headline = "Passed without observed agent recovery activity."
    elif valid is True:
        headline = "Passed; recovery status unknown from available evidence."
    else:
        headline = "Insufficient evidence to establish the trial outcome."

    return {
        "diagnostics_version": DIAGNOSTICS_VERSION,
        "trial_ref": {
            "scenario": scenario,
            "trial_id": trial.get("trial_id", trial.get("strategy_name")),
            "trial_index": trial.get("trial_index"),
        },
        "axes": {
            "completion": completion,
            "evaluation": evaluation_axis,
            "consistency": consistency,
            "evidence_sufficiency": evidence_sufficiency,
            "scorecard_class": classified.get("class"),
        },
        "signals": {
            "has_recovery_activity": has_recovery_activity,
            "has_charge_attempt": has_charge_attempt,
            "key_comparison": key_comparison,
        },
        "violations": violation_entries,
        "timeline": timeline,
        "recovery": {"status": recovery_status, "strategy_context": live_context},
        "hints": hints,
        "headline": headline,
        "limits": limits,
    }


def iter_report_trials(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Enumerate (scenario, trial, report_task, unambiguous) without mutation."""
    from agentcrash.experiments.unified_report import _detect_kind

    try:
        kind = _detect_kind(dict(report), "diagnostics")
    except Exception:
        kind = "orchestrated_run" if isinstance(report.get("scenarios"), list) else "unknown"
    entries: list[dict[str, Any]] = []
    if kind == "orchestrated_run" or (
        kind == "unknown" and isinstance(report.get("scenarios"), list)
    ):
        for scenario_entry in report.get("scenarios", []):  # type: ignore[union-attr]
            if not isinstance(scenario_entry, Mapping):
                continue
            scenario = str(scenario_entry.get("scenario", "unknown"))
            for trial in scenario_entry.get("trials", []) or []:
                if isinstance(trial, Mapping):
                    entries.append(
                        {
                            "scenario": scenario,
                            "trial": trial,
                            "report_task": None,
                            "unambiguous": False,
                        }
                    )
        return entries
    if kind == "deterministic_comparison":
        scenario = str(report.get("scenario", "unknown"))
        for index, result in enumerate(report.get("results", []) or [], start=1):  # type: ignore[union-attr]
            if isinstance(result, Mapping):
                trial = dict(result)
                trial.setdefault("trial_index", index)
                trial.setdefault("trial_id", result.get("strategy_name"))
                entries.append(
                    {
                        "scenario": scenario,
                        "trial": trial,
                        "report_task": None,
                        "unambiguous": False,
                    }
                )
        return entries
    if kind in ("payment_multi", "payment_single", "fulfillment_trial", "intent_trial"):
        scenario = str(report.get("scenario", "unknown"))
        task = report.get("task")
        if kind == "payment_multi":
            trials = report.get("trials", [])
            unambiguous = (
                isinstance(task, Mapping)
                and "original_idempotency_key" in task
                and isinstance(report.get("scenario"), str)
            )
            for trial in trials or []:  # type: ignore[union-attr]
                if isinstance(trial, Mapping):
                    entries.append(
                        {
                            "scenario": scenario,
                            "trial": trial,
                            "report_task": task,
                            "unambiguous": bool(unambiguous),
                        }
                    )
            return entries
        trial = {key: report.get(key) for key in report.keys() if key != "task"}
        trial["task"] = task
        entries.append(
            {"scenario": scenario, "trial": trial, "report_task": None, "unambiguous": False}
        )
        return entries
    # Unified envelope: scenarios carry minimal trial rows.
    scenarios = report.get("scenarios")
    if isinstance(scenarios, list):
        for scenario_entry in scenarios:
            if not isinstance(scenario_entry, Mapping):
                continue
            scenario = str(scenario_entry.get("scenario", "unknown"))
            for trial in scenario_entry.get("trials", []) or []:
                if isinstance(trial, Mapping):
                    entries.append(
                        {
                            "scenario": scenario,
                            "trial": trial,
                            "report_task": None,
                            "unambiguous": False,
                        }
                    )
    return entries


def build_report_diagnostics(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build diagnostics for every trial in a report."""
    results: list[dict[str, Any]] = []
    for entry in iter_report_trials(report):
        trial = entry["trial"]
        mode = trial.get("mode") if isinstance(trial.get("mode"), str) else report.get("mode", "unknown")
        results.append(
            build_trial_diagnostic(
                trial,
                scenario=entry["scenario"],
                mode=str(mode),
                report_task=entry["report_task"],
                report_task_unambiguous=bool(entry["unambiguous"]),
            )
        )
    return results
