"""Reliability scorecard: read-only aggregation over trial reports.

Pure computation — no I/O, no model calls, no simulator access. Consumes
plain report dicts (orchestrated runs, unified-normalized scenarios, legacy
live reports, deterministic comparisons) and produces a separate scorecard
document. Never rewrites input reports and never redefines legacy rates;
every new metric carries its own numerator and denominator.

The ``main()`` entry point below is a thin CLI wrapper that reads one
existing report file and writes a versioned sidecar document; all metric
logic stays in the pure functions above it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


COST_NOTE = "cost_not_calculated (no reliable pricing data)"

CHARGE_DUPLICATE_CODES = frozenset(
    {"multiple_successful_charges_for_order", "duplicate_successful_charge"}
)
ORDER_DUPLICATE_CODES = frozenset({"duplicate_orders_for_intent"})

# Explicit applicability table for duplicate-side-effect metrics, keyed by
# normalized scenario name (lowercased, non-alphanumeric stripped, so
# ``payment_timeout_after_commit`` and ``payment-timeout-after-commit``
# resolve identically). Unknown scenarios fail closed: both metrics report
# null / not applicable rather than guessing from substrings.
_SCENARIO_DUPLICATE_APPLICABILITY: dict[str, dict[str, bool]] = {
    "paymenttimeoutaftercommit": {"charge": True, "order": False},
    "fulfillbeforepayment": {"charge": False, "order": False},
    "duplicateorderforintent": {"charge": False, "order": True},
}


def _normalize_scenario_name(scenario: str) -> str:
    return "".join(ch for ch in (scenario or "").lower() if ch.isalnum())


def _duplicate_applicability(scenario: str) -> tuple[bool, bool]:
    """Return (charge_applicable, order_applicable) for a scenario name.

    Only the known scenarios above apply; anything else fails closed to
    (False, False) so unknown runs never inherit another scenario's
    duplicate metrics.
    """
    entry = _SCENARIO_DUPLICATE_APPLICABILITY.get(
        _normalize_scenario_name(scenario)
    )
    if entry is None:
        return False, False
    return entry["charge"], entry["order"]

# Faults the deterministic scenarios are known to inject. Live runs reuse
# the same values; anything else must be supplied explicitly per scope.
# Keys are canonical scenario names (see SCENARIO_ALIASES below).
DEFAULT_FAULTS: dict[str, dict[str, str]] = {
    "payment_timeout_after_commit": {
        "fault_type": "timeout",
        "target": "payment.create",
        "point": "after_commit",
    },
    "fulfill_before_payment": {"fault_type": "none"},
    "duplicate_order_for_intent": {"fault_type": "none"},
}

# Historical spellings that resolve to a canonical scenario name.
# ``fulfill_without_payment`` is emitted by the deterministic
# order-fulfillment comparison path while the orchestrated/live path and
# this module's fault table use ``fulfill_before_payment``; both names
# describe the same no-fault fulfillment scenario. Aliases are resolved
# before fault lookup and applicability checks, and the original spelling
# is preserved per scope as ``reported_as`` provenance. Historical
# artifacts are never rewritten.
SCENARIO_ALIASES: dict[str, str] = {
    "fulfill_without_payment": "fulfill_before_payment",
}


def _canonical_scenario_name(scenario: str) -> str:
    """Resolve a reported scenario name to its canonical form."""
    return SCENARIO_ALIASES.get(scenario, scenario)


def _rate(numerator: int, denominator: int) -> float | None:
    return (numerator / denominator) if denominator else None


def _is_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _violation_codes(violations: Any) -> list[str]:
    codes: list[str] = []
    if not isinstance(violations, (list, tuple)):
        return codes
    for entry in violations:
        if isinstance(entry, str):
            codes.append(entry)
        elif _is_mapping(entry) and isinstance(entry.get("code"), str):
            codes.append(entry["code"])
    return codes


def _usage_status(usage: Any) -> tuple[dict[str, Any], str]:
    """Classify token usage; None means missing, 0 is a confirmed zero.

    Booleans are never token counts (``True`` is an ``int`` subclass in
    Python), so they are rejected the same way as non-integer values.
    """
    fields = ["input_tokens", "output_tokens", "total_tokens"]
    values = {
        key: (usage.get(key) if _is_mapping(usage) else None) for key in fields
    }
    responses = usage.get("responses", 0) if _is_mapping(usage) else 0
    responses_int = (
        responses
        if isinstance(responses, int) and not isinstance(responses, bool)
        else 0
    )
    present = [
        isinstance(values[key], int) and not isinstance(values[key], bool)
        for key in fields
    ]
    if responses_int > 0 and any(present):
        with_usage, missing = 1, 0
    else:
        with_usage, missing = 0, 1
    partial = (
        1
        if responses_int > 0
        and any(value is None for value in values.values())
        and any(present)
        else 0
    )
    if missing:
        status = "missing"
    elif partial:
        status = "partial"
    else:
        status = "complete"
    normalized = {
        "input_tokens": values["input_tokens"]
        if isinstance(values["input_tokens"], int)
        and not isinstance(values["input_tokens"], bool)
        else 0,
        "output_tokens": values["output_tokens"]
        if isinstance(values["output_tokens"], int)
        and not isinstance(values["output_tokens"], bool)
        else 0,
        "total_tokens": values["total_tokens"]
        if isinstance(values["total_tokens"], int)
        and not isinstance(values["total_tokens"], bool)
        else 0,
        "responses": responses_int,
        "with_usage": with_usage,
        "missing_usage": missing,
        "partial_usage": partial,
        "status": status,
    }
    return normalized, status


def _event_kind(event: Any) -> Any:
    if not _is_mapping(event):
        return None
    return event.get("kind", event.get("event_type"))


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def classify_trial(trial: Mapping[str, Any], *, live_like: bool) -> dict[str, Any]:
    """Classify one trial without inferring beyond the record.

    Returns outcome class plus explicit completion/evaluation flags:
    - ``error``: recorded error; excluded from every quality denominator.
    - ``business_failure``: no error, outcome failed.
    - ``recovered``: no error, outcome passed, evaluation confirmed,
      completion evidenced.
    - ``passed_unattempted``: no error, outcome passed, evaluated, but
      nothing in the trace shows the agent acted (valid state, no recovery).
    - ``passed_unverified``: passed without evaluation evidence or without
      determinable completion — counted, never silently promoted.
    - ``unevaluable``: neither an evaluation-evidence verdict nor recorded
      violation codes exist to verify the outcome against.
    A recorded violation code list counts as evaluation evidence: codes are
    persisted-state evaluator output, so a failed trial with codes but no
    evidence dict is still evaluable (as a failure, never as a pass).
    """
    error = trial.get("error") or trial.get("live_error") or trial.get("trial_error")
    outcome = trial.get("outcome", trial.get("task_outcome"))
    evidence = trial.get("evaluation_evidence")
    evidence = evidence if _is_mapping(evidence) else {}
    evaluated = isinstance(evidence.get("valid"), bool)
    trace = trial.get("trace") or []
    trace = trace if isinstance(trace, (list, tuple)) else []
    has_tool_calls = any(_event_kind(e) == "tool_call" for e in trace)
    has_any_events = len(trace) > 0
    tool_calls = sum(1 for e in trace if _event_kind(e) == "tool_call")
    model_responses = sum(1 for e in trace if _event_kind(e) == "model_response")
    kind_based_trace = any(
        isinstance(e, Mapping) and e.get("kind") is not None for e in trace
    )
    if live_like:
        # A live agent trial counts as attempted only with tool-call evidence:
        # a valid state with no recorded action may be luck, not recovery.
        if has_tool_calls:
            completed: bool | None = True
        elif has_any_events:
            completed = False
        else:
            completed = None
    else:
        # Deterministic trials are scripted strategies: recorded events mean
        # the strategy executed. (No agent whose inaction must be detected.)
        completed = True if has_any_events else None
    violations = _violation_codes(trial.get("violations"))
    if "invariant_violations" in trial and not violations:
        violations = _violation_codes(trial.get("invariant_violations"))
    successful_charges = _as_int(
        trial.get("successful_charges", trial.get("successful_charge_count"))
    )
    order_ids = trial.get("order_ids") or []
    order_ids = list(order_ids) if isinstance(order_ids, (list, tuple)) else []

    if error is not None:
        trial_class = "error"
    elif not evaluated and not violations:
        trial_class = "unevaluable"
    elif outcome == "failed":
        trial_class = "business_failure"
    elif outcome == "passed" and completed is True:
        trial_class = "recovered"
    elif outcome == "passed" and completed is False:
        trial_class = "passed_unattempted"
    elif outcome == "passed":
        trial_class = "passed_unverified"
    else:
        trial_class = "unevaluable"

    inconsistent = evaluated and (
        (outcome == "passed" and evidence.get("valid") is False)
        or (outcome == "failed" and evidence.get("valid") is True)
    )
    if not inconsistent and outcome == "passed" and violations:
        # A pass verdict alongside recorded violations contradicts itself,
        # regardless of whether an evidence dict is present.
        inconsistent = True

    return {
        "class": trial_class,
        "error": error,
        "outcome": outcome,
        "completed": completed,
        "evaluated": evaluated,
        "inconsistent": inconsistent,
        "has_tool_calls": has_tool_calls,
        "tool_calls": tool_calls,
        "model_responses": model_responses,
        "kind_based_trace": kind_based_trace,
        "turns": trial.get("turns", trial.get("turns_used")),
        "violations": violations,
        "successful_charges": successful_charges,
        "order_ids": order_ids,
        "usage_status": _usage_status(trial.get("usage"))[1],
    }


def _median_int(values: list[int]) -> int | float | None:
    """Descriptive middle value; None when empty. No significance implied."""
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return None
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _valid_turn_count(value: Any) -> int | None:
    """Turns usable for efficiency: real ints only, strictly positive.

    Booleans, floats, negatives, zero, and missing values are rejected —
    a recovered trial reporting zero turns has no recorded turn data
    (unified normalization, for example, hardcodes turns to 0).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _reliability_depth(
    classified: list[dict[str, Any]],
    consistent: list[dict[str, Any]],
    completed: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Outcome consistency, evidence coverage, and recovery efficiency.

    All three are informational: none feeds quality denominators or
    regression detection. Populations mirror score_scope() semantics —
    consistency and coverage build on the evaluable set, efficiency on
    recovered trials with recorded turn counts.
    """
    eligible_classes = sorted({c["class"] for c in consistent})
    if len(consistent) < 2:
        consistency: dict[str, Any] = {
            "consistent": None,
            "classes": eligible_classes,
            "eligible_trials": len(consistent),
            "reason": "fewer than 2 eligible trials",
            "definition": _METRIC_DEFINITIONS["outcome_consistency"],
        }
    else:
        consistency = {
            "consistent": len(eligible_classes) == 1,
            "classes": eligible_classes,
            "eligible_trials": len(consistent),
            "reason": None,
            "definition": _METRIC_DEFINITIONS["outcome_consistency"],
        }

    evaluable_count = sum(
        1
        for c in classified
        if c["class"] != "error" and (c["evaluated"] or c["violations"])
    )
    coverage = {
        "value": _rate(evaluable_count, completed),
        "numerator": evaluable_count,
        "denominator": completed,
        "reason": None if completed else "no completed trial records",
        "definition": _METRIC_DEFINITIONS["evidence_coverage"],
    }

    recovered = [c for c in classified if c["class"] == "recovered"]
    recovery_turns: list[int] = []
    for record in recovered:
        turns = _valid_turn_count(record.get("turns"))
        if turns is not None:
            recovery_turns.append(turns)
    without_turn_data = len(recovered) - len(recovery_turns)
    if recovery_turns:
        efficiency: dict[str, Any] = {
            "median_turns_to_recovery": _median_int(recovery_turns),
            "recovered_trials": len(recovered),
            "recovered_without_turn_data": without_turn_data,
            "reason": None,
            "definition": _METRIC_DEFINITIONS["recovery_efficiency"],
        }
    elif recovered:
        efficiency = {
            "median_turns_to_recovery": None,
            "recovered_trials": len(recovered),
            "recovered_without_turn_data": without_turn_data,
            "reason": "no recovered trials with recorded turn counts",
            "definition": _METRIC_DEFINITIONS["recovery_efficiency"],
        }
    else:
        efficiency = {
            "median_turns_to_recovery": None,
            "recovered_trials": 0,
            "recovered_without_turn_data": 0,
            "reason": "no recovered trials",
            "definition": _METRIC_DEFINITIONS["recovery_efficiency"],
        }
    return consistency, coverage, efficiency


def _pooled_consistency(scopes: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Pool outcome classes across scopes without pooling turn scales.

    Missing blocks (historical v1 scorecards) fail closed to unavailable
    rather than guessing from a partial union.
    """
    union: set[str] = set()
    eligible_total = 0
    for scope in scopes:
        block = scope.get("outcome_consistency")
        if not _is_mapping(block):
            return {
                "consistent": None,
                "classes": sorted(union),
                "eligible_trials": eligible_total,
                "reason": (
                    "historical scorecard without reliability-depth fields"
                ),
                "definition": _METRIC_DEFINITIONS["outcome_consistency"],
            }
        eligible = block.get("eligible_trials")
        if isinstance(eligible, int) and not isinstance(eligible, bool):
            eligible_total += eligible
        for code in block.get("classes") or []:
            if isinstance(code, str):
                union.add(code)
    if eligible_total < 2:
        return {
            "consistent": None,
            "classes": sorted(union),
            "eligible_trials": eligible_total,
            "reason": "fewer than 2 eligible trials",
            "definition": _METRIC_DEFINITIONS["outcome_consistency"],
        }
    return {
        "consistent": len(union) == 1,
        "classes": sorted(union),
        "eligible_trials": eligible_total,
        "reason": None,
        "definition": _METRIC_DEFINITIONS["outcome_consistency"],
    }


def _coverage_numbers(scope: Mapping[str, Any]) -> tuple[int | None, int | None]:
    """Coverage numerator/denominator with a trial_counts fallback.

    Fresh scopes carry the depth block; historical v1 scopes fall back to
    trial_counts (same evaluable/completed semantics), so old sidecars
    still compare instead of crashing.
    """
    block = scope.get("evidence_coverage")
    if _is_mapping(block):
        num, den = block.get("numerator"), block.get("denominator")
        if (
            isinstance(num, int)
            and not isinstance(num, bool)
            and isinstance(den, int)
            and not isinstance(den, bool)
        ):
            return num, den
    counts = scope.get("trial_counts") or {}
    evaluable = counts.get("evaluable")
    completed = counts.get("completed")
    if (
        isinstance(evaluable, int)
        and not isinstance(evaluable, bool)
        and isinstance(completed, int)
        and not isinstance(completed, bool)
    ):
        return evaluable, completed
    return None, None


def _coverage_entry(scope: Mapping[str, Any]) -> dict[str, Any]:
    num, den = _coverage_numbers(scope)
    value = None
    if (
        isinstance(num, int)
        and not isinstance(num, bool)
        and isinstance(den, int)
        and not isinstance(den, bool)
        and den
    ):
        value = num / den
    return {
        "value": value,
        "numerator": num,
        "denominator": den,
        "reason": None if value is not None else (
            "no completed trial records"
            if den == 0
            else "insufficient evidence on at least one side (null value)"
        ),
        "definition": _METRIC_DEFINITIONS["evidence_coverage"],
    }


def _duplicate_counts(
    classified: list[dict[str, Any]],
    *,
    charge_applicable: bool,
    order_applicable: bool,
) -> dict[str, Any]:
    """Count duplicate side effects over evaluable trials only.

    The evaluable set matches :func:`score_scope`: non-error trials with
    either an evaluation-evidence verdict or recorded violation codes. A
    failed trial whose only evaluation output is a violation-code list
    (e.g. a normalized legacy row with no evidence dict) still counts, so
    ``violations_by_code`` and the duplicate-trial rates cannot disagree.
    """
    evaluable = [
        c
        for c in classified
        if c["class"] != "error" and (c["evaluated"] or c["violations"])
    ]
    charge_trials = (
        sum(
            1
            for c in evaluable
            if any(code in CHARGE_DUPLICATE_CODES for code in c["violations"])
            or (c["successful_charges"] is not None and c["successful_charges"] > 1)
        )
        if charge_applicable
        else None
    )
    charge_total = (
        sum(max(0, (c["successful_charges"] or 0) - 1) for c in evaluable)
        if charge_applicable
        else None
    )
    order_trials = (
        sum(
            1
            for c in evaluable
            if any(code in ORDER_DUPLICATE_CODES for code in c["violations"])
            or len(c["order_ids"]) > 1
        )
        if order_applicable
        else None
    )
    order_total = (
        sum(max(0, len(c["order_ids"]) - 1) for c in evaluable)
        if order_applicable
        else None
    )
    return {
        "duplicate_charge_trials": charge_trials,
        "total_duplicate_charges": charge_total,
        "duplicate_order_trials": order_trials,
        "total_duplicate_orders": order_total,
    }


def score_scope(
    trials: list[Mapping[str, Any]],
    *,
    scenario: str,
    mode: str = "unknown",
    model: str | None = None,
    strategy_id: str | None = None,
    fault: Mapping[str, Any] | None = None,
    requested: int | None = None,
    live_like: bool | None = None,
    reported_as: str | None = None,
) -> dict[str, Any]:
    """Score one scenario scope. All inputs are plain data; no I/O.

    ``reported_as`` records the original scenario spelling when an alias
    was canonicalized (e.g. ``fulfill_without_payment``); it is omitted
    otherwise so canonical inputs produce byte-identical scopes.
    """
    if live_like is None:
        live_like = mode == "live"
    classified = [classify_trial(trial, live_like=live_like) for trial in trials]
    completed = len(classified)
    errors = sum(1 for c in classified if c["class"] == "error")
    inconsistent = sum(1 for c in classified if c["inconsistent"])
    evaluable = [
        c
        for c in classified
        # A recorded violation-code list is itself evaluator output, so a
        # failed trial with codes but no evidence dict is still evaluable
        # (as a failure). A passed trial needs the full evidence dict —
        # otherwise a clean bill of health cannot be verified.
        if c["class"] != "error" and (c["evaluated"] or c["violations"])
    ]
    consistent = [c for c in evaluable if not c["inconsistent"]]
    completion_known = [c for c in consistent if c["completed"] is not None]
    recovered = sum(
        1 for c in completion_known if c["class"] == "recovered"
    )
    passed_raw = sum(
        1 for c in classified if c["error"] is None and c["outcome"] == "passed"
    )
    failed = sum(1 for c in classified if c["class"] == "business_failure")
    violations: dict[str, int] = {}
    for c in evaluable:
        for code in set(c["violations"]):
            violations[code] = violations.get(code, 0) + 1
    violation_trials = sum(
        1 for c in evaluable if c["violations"]
    )
    abandoned = sum(1 for c in classified if c["class"] == "passed_unattempted")
    unverified = sum(1 for c in classified if c["class"] == "passed_unverified")
    unevaluable = sum(1 for c in classified if c["class"] == "unevaluable")
    unknown_completion = sum(
        1
        for c in classified
        if c["class"] != "error" and c["completed"] is None
    )

    charge_applicable, order_applicable = _duplicate_applicability(scenario)
    duplicates = _duplicate_counts(
        classified,
        charge_applicable=charge_applicable,
        order_applicable=order_applicable,
    )
    consistency, coverage, efficiency = _reliability_depth(
        classified, consistent, completed
    )

    with_usage = sum(1 for c in classified if c["usage_status"] == "complete")
    partial_usage = sum(1 for c in classified if c["usage_status"] == "partial")
    missing_usage = sum(1 for c in classified if c["usage_status"] == "missing")

    def _trial_usage_total(key: str) -> int:
        total = 0
        for raw, c in zip(trials, classified):
            if c["usage_status"] == "missing" or not _is_mapping(raw.get("usage")):
                continue
            value = raw["usage"].get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        return total

    tool_call_trials = sum(1 for c in classified if c["has_tool_calls"])

    return {
        "scenario": scenario,
        "mode": mode,
        "model": model,
        "strategy_id": strategy_id,
        **({"reported_as": reported_as} if reported_as is not None else {}),
        "fault": dict(fault) if fault is not None else None,
        "trial_counts": {
            "requested": requested,
            "completed": completed,
            "evaluable": len(evaluable),
            "errored": errors,
            "unknown_completion": unknown_completion,
            "inconsistent_evidence": inconsistent,
        },
        "recovery_success_rate": {
            "value": _rate(recovered, len(completion_known)),
            "numerator": recovered,
            "denominator": len(completion_known),
            "definition": (
                "passed + evaluated + completion-evidenced trials "
                "over evaluable trials with known completion"
            ),
        },
        "task_pass_rate": {
            "value": _rate(passed_raw, completed),
            "numerator": passed_raw,
            "denominator": completed,
            "definition": "passed trials over completed trials (legacy-compatible)",
        },
        "invariant_violation_rate": {
            "value": _rate(violation_trials, len(evaluable)),
            "numerator": violation_trials,
            "denominator": len(evaluable),
            "definition": "trials with >=1 violation over evaluable trials",
        },
        "infrastructure_error_rate": {
            "value": _rate(errors, completed),
            "numerator": errors,
            "denominator": completed,
            "definition": "errored trials over completed trials",
        },
        "duplicate_charge_trial_rate": {
            "value": _rate(duplicates["duplicate_charge_trials"] or 0, len(evaluable))
            if charge_applicable
            else None,
            "numerator": duplicates["duplicate_charge_trials"],
            "denominator": len(evaluable) if charge_applicable else None,
            "definition": "trials with duplicate-charge evidence over evaluable trials",
            "total_duplicate_charges": duplicates["total_duplicate_charges"],
            "applicable": charge_applicable,
        },
        "duplicate_order_trial_rate": {
            "value": _rate(duplicates["duplicate_order_trials"] or 0, len(evaluable))
            if order_applicable
            else None,
            "numerator": duplicates["duplicate_order_trials"],
            "denominator": len(evaluable) if order_applicable else None,
            "definition": "trials with duplicate-order evidence over evaluable trials",
            "total_duplicate_orders": duplicates["total_duplicate_orders"],
            "applicable": order_applicable,
        },
        "passed_without_completion_evidence": abandoned,
        "passed_with_unknown_completion": sum(
            1 for c in classified if c["class"] == "passed_unverified"
        ),
        "outcome_consistency": consistency,
        "evidence_coverage": coverage,
        "recovery_efficiency": efficiency,
        "unevaluable_trials": unevaluable,
        "violations_by_code": dict(sorted(violations.items())),
        "usage": {
            "input_tokens": _trial_usage_total("input_tokens"),
            "output_tokens": _trial_usage_total("output_tokens"),
            "total_tokens": _trial_usage_total("total_tokens"),
            "responses": _trial_usage_total("responses"),
            "status": (
                "missing"
                if missing_usage == completed and completed
                else ("partial" if partial_usage else "complete" if completed else "missing")
            ),
            "trials_with_usage": with_usage,
            "trials_partial_usage": partial_usage,
            "trials_missing_usage": missing_usage,
        },
        "cost": COST_NOTE,
        # turns_total sums the recorded turn counts of completed trials.
        "turns_total": sum(
            t.get("turns") if isinstance(t.get("turns"), int) else 0
            for t in trials
            if _is_mapping(t)
        ),
        "tool_calls": {
            "total": sum(c["tool_calls"] for c in classified),
            # The mean is taken over the same evaluable population used by
            # the quality rates (non-error trials with an evidence verdict
            # or recorded violations). It is null when no evaluable trial
            # carries kind-based tool-call traces (e.g. deterministic
            # action-style traces have no countable tool calls, so the
            # mean is null rather than a misleading zero).
            "mean_per_evaluable_trial": (
                sum(c["tool_calls"] for c in evaluable) / len(evaluable)
                if evaluable
                and any(c["kind_based_trace"] for c in evaluable)
                else None
            ),
            "trials_with_tool_call_traces": sum(
                1 for c in classified if c["kind_based_trace"]
            ),
        },
        "model_responses": {
            "total": sum(c["model_responses"] for c in classified),
            "trials_with_response_traces": sum(
                1 for c in classified if c["kind_based_trace"]
            ),
        },
    }


def _scope_trials_from_orchestrated(scenario: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(t) for t in (scenario.get("trials") or []) if _is_mapping(t)]


_NORMALIZED_LIVE_KINDS = frozenset(
    {"payment_multi", "payment_single", "fulfillment_trial", "intent_trial"}
)


def _scope_from_normalized(
    entry: Mapping[str, Any],
    *,
    strategy_id: str | None,
    faults: Mapping[str, Mapping[str, Any]],
    default_faults: Mapping[str, Mapping[str, Any]],
    report_model: Any,
) -> dict[str, Any]:
    """Score one unified-normalized scenario entry.

    Normalized rows lack per-trial traces, usage, and evaluation evidence,
    so completion is determinable only for deterministic comparisons
    (recorded outcome proves the scripted strategy ran). Live-like entries
    without traces yield unknown completion and are explicitly qualified
    rather than counted as recovery.
    """
    name = str(entry.get("scenario", "unknown"))
    kind = entry.get("kind", "unknown")
    live_like = kind in _NORMALIZED_LIVE_KINDS
    details = entry.get("details")
    details = details if _is_mapping(details) else {}
    pay_ids = details.get("per_trial_payment_ids", [])
    pay_ids = pay_ids if isinstance(pay_ids, (list, tuple)) else []
    trial_rows = entry.get("trials") or []
    trials: list[dict[str, Any]] = []
    for index, row in enumerate(trial_rows):
        if not _is_mapping(row):
            continue
        violations = row.get("violations") or []
        codes = [
            v if isinstance(v, str) else v.get("code")
            for v in (violations if isinstance(violations, (list, tuple)) else [])
        ]
        codes = [c for c in codes if isinstance(c, str)]
        trials.append(
            {
                "outcome": row.get("outcome"),
                "error": None,
                "turns": row.get("turns", 0),
                "successful_charges": row.get("successful_charges", 0),
                "violations": [{"code": c} for c in codes],
                "payment_ids": list(pay_ids[index])
                if index < len(pay_ids) and isinstance(pay_ids[index], (list, tuple))
                else [],
                "order_ids": [],
                "usage": {},
                "trace": [],
                "evaluation_evidence": {},
            }
        )
    fault = faults.get(name, default_faults.get(name))
    canonical = _canonical_scenario_name(name)
    if canonical != name:
        fault = faults.get(canonical, default_faults.get(canonical))
    live_like = kind in _NORMALIZED_LIVE_KINDS
    return score_scope(
        trials,
        scenario=canonical,
        mode=(
            "live"
            if live_like
            else "deterministic"
            if kind == "deterministic_comparison"
            else "unknown"
        ),
        model=entry.get("model", report_model),
        strategy_id=strategy_id,
        fault=dict(fault) if fault is not None else None,
        requested=entry.get("trials_requested"),
        live_like=live_like,
        reported_as=name if canonical != name else None,
    )


def score_report(
    report: Mapping[str, Any],
    *,
    strategy_id: str | None = None,
    faults: Mapping[str, Mapping[str, Any]] | None = None,
    default_faults: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score a full report dict (orchestrated run or unified document)."""
    faults = faults or {}
    default_faults = default_faults if default_faults is not None else DEFAULT_FAULTS
    model = report.get("model")
    mode = report.get("mode", "unknown")

    raw_scenarios = report.get("scenarios")
    scopes: list[dict[str, Any]] = []
    if not (isinstance(raw_scenarios, (list, tuple)) and raw_scenarios and _is_mapping(raw_scenarios[0])):
        raise ValueError("score_report expects orchestrated-run or unified shape")
    if "kind" in raw_scenarios[0]:
        for entry in raw_scenarios:
            if not _is_mapping(entry):
                continue
            scopes.append(
                _scope_from_normalized(
                    entry,
                    strategy_id=strategy_id,
                    faults=faults,
                    default_faults=default_faults,
                    report_model=model,
                )
            )
    elif "trials" in raw_scenarios[0]:
        for entry in raw_scenarios:
            if not _is_mapping(entry):
                continue
            raw_name = str(entry.get("scenario", "unknown"))
            name = _canonical_scenario_name(raw_name)
            fault = faults.get(raw_name, default_faults.get(raw_name))
            if name != raw_name:
                fault = faults.get(name, default_faults.get(name))
            scopes.append(
                score_scope(
                    _scope_trials_from_orchestrated(entry),
                    scenario=name,
                    mode=str(entry.get("mode", mode or "unknown")),
                    model=entry.get("model", model),
                    strategy_id=strategy_id,
                    fault=dict(fault) if fault is not None else None,
                    requested=entry.get("trials_requested"),
                    live_like=str(entry.get("mode", mode)) == "live",
                    reported_as=raw_name if name != raw_name else None,
                )
            )
    else:
        raise ValueError("score_report expects orchestrated-run or unified shape")

    by_scenario = {s["scenario"]: s for s in scopes}
    return {
        "scorecard_version": 1,
        "scope": {
            "scenarios": sorted(by_scenario),
            "modes": sorted({s["mode"] for s in scopes}),
            "model": model,
            "strategy_id": strategy_id,
        },
        "scopes": scopes,
        "overall": _summarize_scopes(scopes),
        "cost_note": COST_NOTE,
    }


def _summarize_scopes(scopes: list[dict[str, Any]]) -> dict[str, Any]:
    requested = [s["trial_counts"]["requested"] for s in scopes]
    completed = sum(s["trial_counts"]["completed"] for s in scopes)
    evaluable = sum(s["trial_counts"]["evaluable"] for s in scopes)
    errors = sum(s["trial_counts"]["errored"] for s in scopes)
    recovered = sum(s["recovery_success_rate"]["numerator"] for s in scopes)
    rec_den = sum(
        s["recovery_success_rate"]["denominator"] or 0 for s in scopes
    )
    passed = sum(
        s["task_pass_rate"]["numerator"] for s in scopes
    )
    violation_trials = sum(
        s["invariant_violation_rate"]["numerator"] for s in scopes
    )
    viol_den = sum(s["invariant_violation_rate"]["denominator"] for s in scopes)
    usage_present = [
        s for s in scopes if s["usage"]["status"] != "missing"
    ]
    violations_by_scenario = {
        s["scenario"]: dict(s["violations_by_code"]) for s in scopes
    }
    overall_violations: dict[str, int] = {}
    for counts in violations_by_scenario.values():
        for code, count in counts.items():
            overall_violations[code] = overall_violations.get(code, 0) + count
    coverage_num = sum(
        (s.get("evidence_coverage") or {}).get("numerator") or 0 for s in scopes
    )
    coverage_den = sum(
        (s.get("evidence_coverage") or {}).get("denominator") or 0
        for s in scopes
    )
    return {
        "trials_requested": sum(r for r in requested if isinstance(r, int))
        if all(isinstance(r, int) for r in requested)
        else None,
        "trials_completed": completed,
        "trials_evaluable": evaluable,
        "trials_errored": errors,
        "recovery_success_rate": {
            "value": _rate(recovered, rec_den),
            "numerator": recovered,
            "denominator": rec_den,
        },
        "task_pass_rate": {
            "value": _rate(passed, completed),
            "numerator": passed,
            "denominator": completed,
        },
        "invariant_violation_rate": {
            "value": _rate(violation_trials, viol_den),
            "numerator": violation_trials,
            "denominator": viol_den,
        },
        "infrastructure_error_rate": {
            "value": _rate(errors, completed),
            "numerator": errors,
            "denominator": completed,
        },
        "evidence_coverage": {
            "value": _rate(coverage_num, coverage_den),
            "numerator": coverage_num,
            "denominator": coverage_den,
            "reason": None if coverage_den else "no completed trial records",
            "definition": _METRIC_DEFINITIONS["evidence_coverage"],
        },
        "outcome_consistency": _pooled_consistency(scopes),
        "recovery_efficiency": {
            "median_turns_to_recovery": None,
            "recovered_trials": sum(
                (s.get("recovery_efficiency") or {}).get("recovered_trials")
                or 0
                for s in scopes
            ),
            "recovered_without_turn_data": sum(
                (s.get("recovery_efficiency") or {}).get(
                    "recovered_without_turn_data"
                )
                or 0
                for s in scopes
            ),
            "reason": "stratify by scenario",
            "definition": _METRIC_DEFINITIONS["recovery_efficiency"],
        },
        "violations_by_code": dict(sorted(overall_violations.items())),
        "violations_by_scenario": violations_by_scenario,
    }


def compare_scorecards(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two scorecards without ranking incomparable runs.

    Compatible only when scenario sets, per-scenario faults, and
    per-scenario requested and completed trial counts all match.
    Requested counts are compared because a requested/completed gap can
    reflect dropouts: runs that started different numbers of trials never
    compare, even if the same number finished. An unknown requested count
    on either side also blocks comparison, mirroring the fault rule.
    Anything else yields compatible=false with reasons and no deltas.
    """
    a_scopes = {s["scenario"]: s for s in a.get("scopes", [])}
    b_scopes = {s["scenario"]: s for s in b.get("scopes", [])}
    reasons: list[str] = []
    if set(a_scopes) != set(b_scopes):
        reasons.append(
            f"scenario sets differ: {sorted(a_scopes)} vs {sorted(b_scopes)}"
        )
    for name in sorted(set(a_scopes) & set(b_scopes)):
        fa, fb = a_scopes[name].get("fault"), b_scopes[name].get("fault")
        if fa is None or fb is None:
            reasons.append(f"scenario {name!r}: fault unknown on at least one side")
        elif fa != fb:
            reasons.append(f"scenario {name!r}: fault differs ({fa} vs {fb})")
        ca = a_scopes[name]["trial_counts"]["completed"]
        cb = b_scopes[name]["trial_counts"]["completed"]
        if ca != cb:
            reasons.append(
                f"scenario {name!r}: completed trial counts differ ({ca} vs {cb})"
            )
        ra = a_scopes[name]["trial_counts"]["requested"]
        rb = b_scopes[name]["trial_counts"]["requested"]
        if ra is None or rb is None:
            reasons.append(
                f"scenario {name!r}: requested trial count unknown "
                "on at least one side"
            )
        elif ra != rb:
            reasons.append(
                f"scenario {name!r}: requested trial counts differ ({ra} vs {rb})"
            )
    if reasons:
        return {"compatible": False, "reasons": reasons, "deltas": {}}
    deltas: dict[str, Any] = {}
    for name in sorted(a_scopes):
        da = {k: a_scopes[name][k]["value"] for k in ("recovery_success_rate",)}
        db = {k: b_scopes[name][k]["value"] for k in ("recovery_success_rate",)}
        row = {}
        for key in da:
            va, vb = da[key], db[key]
            row[key] = None if va is None or vb is None else vb - va
        deltas[name] = row
    ad = {k: a["overall"][k]["value"] for k in ("recovery_success_rate",)}
    bd = {k: b["overall"][k]["value"] for k in ("recovery_success_rate",)}
    overall = {}
    for key in ad:
        va, vb = ad[key], bd[key]
        overall[key] = None if va is None or vb is None else vb - va
    deltas["overall"] = overall
    return {
        "compatible": True,
        "reasons": [],
        "deltas": deltas,
        "note": (
            "Numeric differences only; small samples carry no significance claim. "
            "Higher recovery success is better; lower violation/error rates are better."
        ),
    }


COMPARISON_VERSION = 1

COMPARISON_NOTE = (
    "Numeric differences only; small samples carry no significance claim. "
    "Higher recovery success is better; lower violation/error rates are better. "
    "Tool-call and usage differences are operational signals, not safety verdicts."
)

# (metric key, better_direction, safety_metric). "informational" metrics never
# drive regressions_present. Definitions mirror score_scope() verbatim.
_COMPARED_RATES: tuple[tuple[str, str, bool], ...] = (
    ("recovery_success_rate", "higher", True),
    ("task_pass_rate", "informational", False),
    ("invariant_violation_rate", "lower", True),
    ("infrastructure_error_rate", "lower", True),
    ("duplicate_charge_trial_rate", "lower", True),
    ("duplicate_order_trial_rate", "lower", True),
)
_SAFETY_RATE_KEYS = frozenset(k for k, _, s in _COMPARED_RATES if s)

_METRIC_DEFINITIONS: dict[str, str] = {
    "recovery_success_rate": (
        "passed + evaluated + completion-evidenced trials "
        "over evaluable trials with known completion"
    ),
    "task_pass_rate": "passed trials over completed trials (legacy-compatible)",
    "invariant_violation_rate": "trials with >=1 violation over evaluable trials",
    "infrastructure_error_rate": "errored trials over completed trials",
    "duplicate_charge_trial_rate": (
        "trials with duplicate-charge evidence over evaluable trials"
    ),
    "duplicate_order_trial_rate": (
        "trials with duplicate-order evidence over evaluable trials"
    ),
    "tool_calls_mean_per_evaluable_trial": (
        "mean tool calls per evaluable trial "
        "(operational cost signal only; more calls are not worse reliability)"
    ),
    "outcome_consistency": (
        "whether all eligible trials share one outcome class "
        "(informational: stable is not the same as successful)"
    ),
    "evidence_coverage": (
        "evaluable trials over completed trial records "
        "(informational: higher means a more trustworthy denominator, "
        "not a better agent)"
    ),
    "recovery_efficiency": (
        "median turns among recovered trials with recorded turn counts "
        "(informational: fewer turns means cheaper recovery, "
        "not safer recovery)"
    ),
}

_DUPLICATE_TOTAL_KEYS = {
    "duplicate_charge_trial_rate": (
        "total_duplicate_charges",
        "total_duplicate_charges",
    ),
    "duplicate_order_trial_rate": (
        "total_duplicate_orders",
        "total_duplicate_orders",
    ),
}


def _fraction(numerator: Any, denominator: Any) -> str | None:
    if isinstance(numerator, int) and isinstance(denominator, int):
        return f"{numerator}/{denominator}"
    return None


def _rate_direction(
    delta: float | None, better: str
) -> tuple[str, str | None]:
    """Map a numeric delta to a direction label plus an optional reason."""
    if delta is None:
        return "unavailable", "insufficient evidence on at least one side"
    if delta == 0:
        return "unchanged", None
    if better == "higher":
        return ("improved", None) if delta > 0 else ("regressed", None)
    if better == "lower":
        return ("improved", None) if delta < 0 else ("regressed", None)
    return (
        ("informational-increase", None)
        if delta > 0
        else ("informational-decrease", None)
    )


def _compare_rate_row(
    key: str,
    baseline_entry: Mapping[str, Any],
    candidate_entry: Mapping[str, Any],
    *,
    better: str,
    definition: str,
) -> dict[str, Any]:
    """Compare one rate row; never invents values for missing evidence."""
    baseline_value = baseline_entry.get("value")
    candidate_value = candidate_entry.get("value")
    baseline_num = baseline_entry.get("numerator")
    candidate_num = candidate_entry.get("numerator")
    baseline_den = baseline_entry.get("denominator")
    candidate_den = candidate_entry.get("denominator")
    row: dict[str, Any] = {
        "baseline": baseline_value,
        "candidate": candidate_value,
        "delta": None,
        "direction": "unavailable",
        "reason": None,
        "better_direction": better,
        "definition": definition,
        "baseline_numerator": baseline_num,
        "candidate_numerator": candidate_num,
        "baseline_denominator": baseline_den,
        "candidate_denominator": candidate_den,
        "baseline_fraction": _fraction(baseline_num, baseline_den),
        "candidate_fraction": _fraction(candidate_num, candidate_den),
    }
    if key in _DUPLICATE_TOTAL_KEYS:
        total_key = _DUPLICATE_TOTAL_KEYS[key][0]
        baseline_app = baseline_entry.get("applicable")
        candidate_app = candidate_entry.get("applicable")
        row["applicable"] = bool(baseline_app and candidate_app)
        row["baseline_" + total_key] = baseline_entry.get(total_key)
        row["candidate_" + total_key] = candidate_entry.get(total_key)
        if not row["applicable"]:
            row["reason"] = "duplicate metric not applicable to this scenario"
            return row
    if not isinstance(baseline_value, (int, float)) or not isinstance(
        candidate_value, (int, float)
    ):
        row["reason"] = "insufficient evidence on at least one side (null value)"
        return row
    delta = candidate_value - baseline_value
    direction, reason = _rate_direction(delta, better)
    row["delta"] = delta
    row["direction"] = direction
    row["reason"] = reason
    return row


def _compare_tool_mean_row(
    baseline_scope: Mapping[str, Any], candidate_scope: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare mean tool calls per evaluable trial (informational only)."""
    baseline_tools = baseline_scope.get("tool_calls") or {}
    candidate_tools = candidate_scope.get("tool_calls") or {}
    baseline_value = baseline_tools.get("mean_per_evaluable_trial")
    candidate_value = candidate_tools.get("mean_per_evaluable_trial")
    baseline_counts = baseline_scope.get("trial_counts") or {}
    candidate_counts = candidate_scope.get("trial_counts") or {}
    row: dict[str, Any] = {
        "baseline": baseline_value,
        "candidate": candidate_value,
        "delta": None,
        "direction": "unavailable",
        "reason": None,
        "better_direction": "informational",
        "definition": _METRIC_DEFINITIONS[
            "tool_calls_mean_per_evaluable_trial"
        ],
        "baseline_total_tool_calls": baseline_tools.get("total"),
        "candidate_total_tool_calls": candidate_tools.get("total"),
        "baseline_evaluable_trials": baseline_counts.get("evaluable"),
        "candidate_evaluable_trials": candidate_counts.get("evaluable"),
    }
    if not isinstance(baseline_value, (int, float)) or not isinstance(
        candidate_value, (int, float)
    ):
        row["reason"] = "insufficient evidence on at least one side (null value)"
        return row
    delta = candidate_value - baseline_value
    direction, reason = _rate_direction(delta, "informational")
    row["delta"] = delta
    row["direction"] = direction
    row["reason"] = reason
    return row


def _consistency_summary(value: Any, block: Mapping[str, Any]) -> str | None:
    """Human-readable consistency state for display, never a verdict."""
    if not isinstance(value, bool):
        return None
    eligible = block.get("eligible_trials")
    eligible_text = (
        f"{eligible} eligible"
        if isinstance(eligible, int) and not isinstance(eligible, bool)
        else "unknown eligibility"
    )
    if value:
        return f"consistent · {eligible_text}"
    classes = [
        code for code in (block.get("classes") or []) if isinstance(code, str)
    ]
    return f"mixed [{', '.join(classes)}] · {eligible_text}"


def _compare_efficiency_row(
    baseline_eff: Mapping[str, Any], candidate_eff: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare median recovery turns; informational, never a safety verdict."""
    baseline_value = baseline_eff.get("median_turns_to_recovery")
    candidate_value = candidate_eff.get("median_turns_to_recovery")
    row: dict[str, Any] = {
        "baseline": baseline_value,
        "candidate": candidate_value,
        "delta": None,
        "direction": "unavailable",
        "reason": None,
        "better_direction": "informational",
        "definition": _METRIC_DEFINITIONS["recovery_efficiency"],
        "baseline_recovered_trials": baseline_eff.get("recovered_trials"),
        "candidate_recovered_trials": candidate_eff.get("recovered_trials"),
        "baseline_recovered_without_turn_data": baseline_eff.get(
            "recovered_without_turn_data"
        ),
        "candidate_recovered_without_turn_data": candidate_eff.get(
            "recovered_without_turn_data"
        ),
    }
    if not isinstance(baseline_value, (int, float)) or isinstance(
        baseline_value, bool
    ):
        row["reason"] = baseline_eff.get("reason") or (
            "insufficient evidence on at least one side (null value)"
        )
        return row
    if not isinstance(candidate_value, (int, float)) or isinstance(
        candidate_value, bool
    ):
        row["reason"] = candidate_eff.get("reason") or (
            "insufficient evidence on at least one side (null value)"
        )
        return row
    delta = candidate_value - baseline_value
    direction, reason = _rate_direction(delta, "informational")
    row["delta"] = delta
    row["direction"] = direction
    row["reason"] = reason
    return row


def _compare_consistency_row(
    baseline_cons: Mapping[str, Any], candidate_cons: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare outcome consistency as a categorical transition.

    Never boolean arithmetic: the transition is classified explicitly and
    delta stays null. Informational only, so transitions can never drive
    regressions_present.
    """
    baseline_value = baseline_cons.get("consistent")
    candidate_value = candidate_cons.get("consistent")
    row: dict[str, Any] = {
        "baseline": baseline_value,
        "candidate": candidate_value,
        "baseline_fraction": _consistency_summary(
            baseline_value, baseline_cons
        ),
        "candidate_fraction": _consistency_summary(
            candidate_value, candidate_cons
        ),
        "transition": None,
        "delta": None,
        "direction": "unavailable",
        "reason": None,
        "better_direction": "informational",
        "definition": _METRIC_DEFINITIONS["outcome_consistency"],
        "baseline_eligible_trials": baseline_cons.get("eligible_trials"),
        "candidate_eligible_trials": candidate_cons.get("eligible_trials"),
        "baseline_classes": baseline_cons.get("classes"),
        "candidate_classes": candidate_cons.get("classes"),
    }
    if isinstance(baseline_value, bool) and isinstance(candidate_value, bool):
        if baseline_value == candidate_value:
            row["direction"] = "unchanged"
            row["transition"] = (
                f"{str(baseline_value).lower()}"
                f"->{str(candidate_value).lower()}"
            )
        elif candidate_value:
            row["direction"] = "improved"
            row["transition"] = "false->true"
        else:
            row["direction"] = "regressed"
            row["transition"] = "true->false"
    else:
        row["reason"] = (
            baseline_cons.get("reason")
            or candidate_cons.get("reason")
            or "insufficient evidence on at least one side "
            "(null or missing consistency)"
        )
    return row


def _scope_metric_rows(
    baseline_scope: Mapping[str, Any], candidate_scope: Mapping[str, Any]
) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for key, better, _ in _COMPARED_RATES:
        rows[key] = _compare_rate_row(
            key,
            baseline_scope.get(key) or {},
            candidate_scope.get(key) or {},
            better=better,
            definition=_METRIC_DEFINITIONS[key],
        )
    rows["tool_calls_mean_per_evaluable_trial"] = _compare_tool_mean_row(
        baseline_scope, candidate_scope
    )
    rows["evidence_coverage"] = _compare_rate_row(
        "evidence_coverage",
        _coverage_entry(baseline_scope),
        _coverage_entry(candidate_scope),
        better="informational",
        definition=_METRIC_DEFINITIONS["evidence_coverage"],
    )
    rows["outcome_consistency"] = _compare_consistency_row(
        baseline_scope.get("outcome_consistency") or {},
        candidate_scope.get("outcome_consistency") or {},
    )
    rows["recovery_efficiency"] = _compare_efficiency_row(
        baseline_scope.get("recovery_efficiency") or {},
        candidate_scope.get("recovery_efficiency") or {},
    )
    return rows


def _violation_code_diffs(
    baseline_codes: Mapping[str, Any], candidate_codes: Mapping[str, Any]
) -> dict[str, Any]:
    diffs: dict[str, Any] = {}
    for code in sorted(set(baseline_codes) | set(candidate_codes)):
        baseline_count = baseline_codes.get(code, 0)
        candidate_count = candidate_codes.get(code, 0)
        baseline_count = baseline_count if isinstance(baseline_count, int) else 0
        candidate_count = (
            candidate_count if isinstance(candidate_count, int) else 0
        )
        delta = candidate_count - baseline_count
        if delta < 0:
            direction = "improved"
        elif delta > 0:
            direction = "regressed"
        else:
            direction = "unchanged"
        diffs[code] = {
            "baseline": baseline_count,
            "candidate": candidate_count,
            "delta": delta,
            "direction": direction,
        }
    return diffs


def _scorecard_provenance(document: Mapping[str, Any]) -> dict[str, Any]:
    generated = document.get("generated_from")
    generated = generated if _is_mapping(generated) else {}
    scope = document.get("scope")
    scope = scope if _is_mapping(scope) else {}
    return {
        "source": generated.get("source"),
        "input_kind": generated.get("input_kind"),
        "scorecard_version": document.get("scorecard_version"),
        "strategy_id": scope.get("strategy_id"),
        "model": scope.get("model"),
    }


def _usage_status_rollup(scopes: list[Mapping[str, Any]]) -> str | None:
    if not scopes:
        return None
    statuses = [(s.get("usage") or {}).get("status") for s in scopes]
    if all(status == "complete" for status in statuses):
        return "complete"
    if all(status == "missing" for status in statuses):
        return "missing"
    return "partial"


def _usage_totals(scopes: list[Mapping[str, Any]]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
              "responses": 0}
    for scope in scopes:
        usage = scope.get("usage") or {}
        for field in totals:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[field] += value
    return totals


def _compare_usage(
    baseline_scopes: list[Mapping[str, Any]],
    candidate_scopes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    baseline_status = _usage_status_rollup(baseline_scopes)
    candidate_status = _usage_status_rollup(candidate_scopes)
    baseline_totals = _usage_totals(baseline_scopes)
    candidate_totals = _usage_totals(candidate_scopes)
    comparable = (
        baseline_status == "complete" and candidate_status == "complete"
    )
    fields: dict[str, Any] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens", "responses"):
        baseline_value = baseline_totals[field]
        candidate_value = candidate_totals[field]
        fields[field] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": (
                candidate_value - baseline_value if comparable else None
            ),
        }
    return {
        "comparable": comparable,
        "baseline_status": baseline_status,
        "candidate_status": candidate_status,
        "reason": (
            None
            if comparable
            else "usage deltas require complete usage on both sides"
        ),
        **fields,
    }


def _overall_from_scopes(
    scopes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Rebuild overall-style rate entries from scope evidence.

    The persisted ``overall`` block carries only four rates; overall
    duplicate and tool-call rows are summed here from the same published
    scope numerators so the comparison never re-derives metrics.
    """
    recovered_num = sum(
        (s.get("recovery_success_rate") or {}).get("numerator") or 0
        for s in scopes
    )
    recovered_den = sum(
        (s.get("recovery_success_rate") or {}).get("denominator") or 0
        for s in scopes
    )
    passed_num = sum(
        (s.get("task_pass_rate") or {}).get("numerator") or 0 for s in scopes
    )
    completed = sum((s.get("trial_counts") or {}).get("completed") or 0
                    for s in scopes)
    violation_num = sum(
        (s.get("invariant_violation_rate") or {}).get("numerator") or 0
        for s in scopes
    )
    violation_den = sum(
        (s.get("invariant_violation_rate") or {}).get("denominator") or 0
        for s in scopes
    )
    error_num = sum(
        (s.get("infrastructure_error_rate") or {}).get("numerator") or 0
        for s in scopes
    )
    charge_num = sum(
        ((s.get("duplicate_charge_trial_rate") or {}).get("numerator") or 0)
        for s in scopes
        if (s.get("duplicate_charge_trial_rate") or {}).get("applicable")
    )
    charge_den = sum(
        ((s.get("duplicate_charge_trial_rate") or {}).get("denominator") or 0)
        for s in scopes
        if (s.get("duplicate_charge_trial_rate") or {}).get("applicable")
    )
    order_num = sum(
        ((s.get("duplicate_order_trial_rate") or {}).get("numerator") or 0)
        for s in scopes
        if (s.get("duplicate_order_trial_rate") or {}).get("applicable")
    )
    order_den = sum(
        ((s.get("duplicate_order_trial_rate") or {}).get("denominator") or 0)
        for s in scopes
        if (s.get("duplicate_order_trial_rate") or {}).get("applicable")
    )
    charge_total = sum(
        ((s.get("duplicate_charge_trial_rate") or {}).get(
            "total_duplicate_charges") or 0)
        for s in scopes
        if (s.get("duplicate_charge_trial_rate") or {}).get("applicable")
    )
    order_total = sum(
        ((s.get("duplicate_order_trial_rate") or {}).get(
            "total_duplicate_orders") or 0)
        for s in scopes
        if (s.get("duplicate_order_trial_rate") or {}).get("applicable")
    )
    charge_applicable = any(
        (s.get("duplicate_charge_trial_rate") or {}).get("applicable")
        for s in scopes
    )
    order_applicable = any(
        (s.get("duplicate_order_trial_rate") or {}).get("applicable")
        for s in scopes
    )
    tool_total = sum((s.get("tool_calls") or {}).get("total") or 0
                     for s in scopes)
    evaluable = sum((s.get("trial_counts") or {}).get("evaluable") or 0
                    for s in scopes)
    coverage_num = 0
    coverage_den = 0
    for s in scopes:
        num, den = _coverage_numbers(s)
        if isinstance(num, int) and not isinstance(num, bool):
            coverage_num += num
        if isinstance(den, int) and not isinstance(den, bool):
            coverage_den += den
    efficiency_blocks = [s.get("recovery_efficiency") or {} for s in scopes]
    efficiency_has_data = any(
        "recovered_trials" in b for b in efficiency_blocks
    )
    return {
        "recovery_success_rate": {
            "value": _rate(recovered_num, recovered_den),
            "numerator": recovered_num,
            "denominator": recovered_den,
        },
        "task_pass_rate": {
            "value": _rate(passed_num, completed),
            "numerator": passed_num,
            "denominator": completed,
        },
        "invariant_violation_rate": {
            "value": _rate(violation_num, violation_den),
            "numerator": violation_num,
            "denominator": violation_den,
        },
        "infrastructure_error_rate": {
            "value": _rate(error_num, completed),
            "numerator": error_num,
            "denominator": completed,
        },
        "duplicate_charge_trial_rate": {
            "value": _rate(charge_num, charge_den)
            if charge_applicable and charge_den
            else None,
            "numerator": charge_num if charge_applicable else None,
            "denominator": charge_den if charge_applicable else None,
            "applicable": charge_applicable,
            "total_duplicate_charges": charge_total
            if charge_applicable
            else None,
        },
        "duplicate_order_trial_rate": {
            "value": _rate(order_num, order_den)
            if order_applicable and order_den
            else None,
            "numerator": order_num if order_applicable else None,
            "denominator": order_den if order_applicable else None,
            "applicable": order_applicable,
            "total_duplicate_orders": order_total
            if order_applicable
            else None,
        },
        "completed": completed,
        "tool_total": tool_total,
        "evaluable": evaluable,
        "evidence_coverage": {
            "value": _rate(coverage_num, coverage_den),
            "numerator": coverage_num,
            "denominator": coverage_den,
            "reason": None if coverage_den else "no completed trial records",
            "definition": _METRIC_DEFINITIONS["evidence_coverage"],
        },
        "outcome_consistency": _pooled_consistency(scopes),
        "recovery_efficiency": {
            "median_turns_to_recovery": None,
            "recovered_trials": (
                sum(
                    b.get("recovered_trials") or 0 for b in efficiency_blocks
                )
                if efficiency_has_data
                else None
            ),
            "recovered_without_turn_data": (
                sum(
                    b.get("recovered_without_turn_data") or 0
                    for b in efficiency_blocks
                )
                if efficiency_has_data
                else None
            ),
            "reason": "stratify by scenario",
            "definition": _METRIC_DEFINITIONS["recovery_efficiency"],
        },
    }


def _overall_metric_rows(
    baseline_scopes: list[Mapping[str, Any]],
    candidate_scopes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    baseline = _overall_from_scopes(baseline_scopes)
    candidate = _overall_from_scopes(candidate_scopes)
    rows: dict[str, Any] = {}
    for key, better, _ in _COMPARED_RATES:
        rows[key] = _compare_rate_row(
            key,
            baseline[key],
            candidate[key],
            better=better,
            definition=_METRIC_DEFINITIONS[key],
        )
    baseline_mean = _overall_tool_mean(baseline_scopes)
    candidate_mean = _overall_tool_mean(candidate_scopes)
    baseline_value = baseline_mean["value"]
    candidate_value = candidate_mean["value"]
    if not isinstance(baseline_value, (int, float)) or not isinstance(
        candidate_value, (int, float)
    ):
        direction, reason = (
            "unavailable",
            "insufficient evidence on at least one side (null value)",
        )
        delta = None
    else:
        delta = candidate_value - baseline_value
        direction, reason = _rate_direction(delta, "informational")
    rows["tool_calls_mean_per_evaluable_trial"] = {
        "baseline": baseline_value,
        "candidate": candidate_value,
        "delta": delta,
        "direction": direction,
        "reason": reason,
        "better_direction": "informational",
        "definition": _METRIC_DEFINITIONS[
            "tool_calls_mean_per_evaluable_trial"
        ],
        "baseline_total_tool_calls": baseline["tool_total"],
        "candidate_total_tool_calls": candidate["tool_total"],
        "baseline_evaluable_trials": baseline["evaluable"],
        "candidate_evaluable_trials": candidate["evaluable"],
    }
    rows["evidence_coverage"] = _compare_rate_row(
        "evidence_coverage",
        baseline["evidence_coverage"],
        candidate["evidence_coverage"],
        better="informational",
        definition=_METRIC_DEFINITIONS["evidence_coverage"],
    )
    rows["outcome_consistency"] = _compare_consistency_row(
        baseline["outcome_consistency"], candidate["outcome_consistency"]
    )
    rows["recovery_efficiency"] = _compare_efficiency_row(
        baseline["recovery_efficiency"], candidate["recovery_efficiency"]
    )
    return rows


def _overall_tool_mean(
    scopes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    total = sum((s.get("tool_calls") or {}).get("total") or 0 for s in scopes)
    evaluable_rows = [
        s for s in scopes if (s.get("trial_counts") or {}).get("evaluable")
    ]
    with_traces = [
        s
        for s in scopes
        if (s.get("tool_calls") or {}).get("trials_with_tool_call_traces")
    ]
    evaluable = sum(
        (s.get("trial_counts") or {}).get("evaluable") or 0 for s in scopes
    )
    if evaluable and with_traces:
        evaluable_total = sum(
            (s.get("tool_calls") or {}).get("total") or 0
            for s in evaluable_rows
        )
        evaluable_count = sum(
            (s.get("trial_counts") or {}).get("evaluable") or 0
            for s in evaluable_rows
        )
        value = (
            evaluable_total / evaluable_count if evaluable_count else None
        )
    else:
        value = None
    return {"value": value, "total": total, "evaluable": evaluable}


def _overall_violations(
    scopes: list[Mapping[str, Any]],
) -> dict[str, int]:
    totals: dict[str, int] = {}
    for scope in scopes:
        for code, count in ((scope.get("violations_by_code")) or {}).items():
            if isinstance(count, int):
                totals[code] = totals.get(code, 0) + count
    return dict(sorted(totals.items()))


def _collect_safety_denominators(rows: Mapping[str, Any]) -> list[int]:
    denominators: list[int] = []
    for key in _SAFETY_RATE_KEYS:
        row = rows.get(key) or {}
        for side in ("baseline_denominator", "candidate_denominator"):
            value = row.get(side)
            if isinstance(value, int) and not isinstance(value, bool):
                denominators.append(value)
    return denominators


def compare_scorecard_documents(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare two scorecard sidecars without ranking incompatible runs.

    The compatibility gate is :func:`compare_scorecards`, called verbatim:
    scenario sets, per-scenario faults, and per-scenario requested and
    completed counts must all match. Incompatible inputs yield reasons and
    empty metric sections with no improvement claims. Only compatible
    inputs are expanded into overall and per-scenario metric comparisons.
    """
    gate = compare_scorecards(baseline, candidate)
    baseline_prov = _scorecard_provenance(baseline)
    candidate_prov = _scorecard_provenance(candidate)
    if not gate.get("compatible"):
        return {
            "scorecard_comparison_version": COMPARISON_VERSION,
            "compatible": False,
            "reasons": list(gate.get("reasons", [])),
            "baseline": baseline_prov,
            "candidate": candidate_prov,
            "overall": {},
            "scenarios": {},
            "violations_by_code": {},
            "regressions_present": False,
            "sample_size_warning": False,
            "usage": {
                "comparable": False,
                "baseline_status": None,
                "candidate_status": None,
                "reason": "incompatible scorecards cannot be compared",
            },
            "cost": COST_NOTE,
            "note": COMPARISON_NOTE,
        }
    baseline_scopes = {s["scenario"]: s for s in baseline.get("scopes", [])}
    candidate_scopes = {s["scenario"]: s for s in candidate.get("scopes", [])}
    scenarios: dict[str, Any] = {}
    for name in sorted(baseline_scopes):
        baseline_scope = baseline_scopes[name]
        candidate_scope = candidate_scopes[name]
        scenarios[name] = {
            "fault": {
                "baseline": baseline_scope.get("fault"),
                "candidate": candidate_scope.get("fault"),
            },
            "trial_counts": {
                "baseline": dict(baseline_scope.get("trial_counts", {})),
                "candidate": dict(candidate_scope.get("trial_counts", {})),
            },
            "reported_as": {
                "baseline": baseline_scope.get("reported_as"),
                "candidate": candidate_scope.get("reported_as"),
            },
            "metrics": _scope_metric_rows(baseline_scope, candidate_scope),
            "violations_by_code": _violation_code_diffs(
                baseline_scope.get("violations_by_code") or {},
                candidate_scope.get("violations_by_code") or {},
            ),
        }
    overall_rows = _overall_metric_rows(
        [baseline_scopes[n] for n in sorted(baseline_scopes)],
        [candidate_scopes[n] for n in sorted(candidate_scopes)],
    )
    violations = _violation_code_diffs(
        _overall_violations([baseline_scopes[n] for n in sorted(baseline_scopes)]),
        _overall_violations(
            [candidate_scopes[n] for n in sorted(candidate_scopes)]
        ),
    )
    regressions_present = any(
        row.get("direction") == "regressed"
        for row in overall_rows.values()
        if row.get("better_direction") != "informational"
    ) or any(
        metric.get("direction") == "regressed"
        for scenario in scenarios.values()
        for metric in scenario["metrics"].values()
        if metric.get("better_direction") != "informational"
    )
    denominators: list[int] = []
    denominators.extend(_collect_safety_denominators(overall_rows))
    for scenario in scenarios.values():
        denominators.extend(
            _collect_safety_denominators(scenario["metrics"])
        )
    completed_counts: list[int] = []
    for scenario in scenarios.values():
        for side in ("baseline", "candidate"):
            completed = (scenario["trial_counts"][side] or {}).get("completed")
            if isinstance(completed, int) and not isinstance(completed, bool):
                completed_counts.append(completed)
    sample_size_warning = any(d < 5 for d in denominators) or any(
        c < 5 for c in completed_counts
    )
    usage = _compare_usage(
        [baseline_scopes[n] for n in sorted(baseline_scopes)],
        [candidate_scopes[n] for n in sorted(candidate_scopes)],
    )
    return {
        "scorecard_comparison_version": COMPARISON_VERSION,
        "compatible": True,
        "reasons": [],
        "baseline": baseline_prov,
        "candidate": candidate_prov,
        "overall": overall_rows,
        "scenarios": scenarios,
        "violations_by_code": violations,
        "regressions_present": regressions_present,
        "sample_size_warning": sample_size_warning,
        "usage": usage,
        "cost": COST_NOTE,
        "note": COMPARISON_NOTE,
    }


    usage = _compare_usage(
        [baseline_scopes[n] for n in sorted(baseline_scopes)],
        [candidate_scopes[n] for n in sorted(candidate_scopes)],
    )
    return {
        "scorecard_comparison_version": COMPARISON_VERSION,
        "compatible": True,
        "reasons": [],
        "baseline": baseline_prov,
        "candidate": candidate_prov,
        "overall": overall_rows,
        "scenarios": scenarios,
        "violations_by_code": violations,
        "regressions_present": regressions_present,
        "sample_size_warning": sample_size_warning,
        "usage": usage,
        "cost": COST_NOTE,
        "note": COMPARISON_NOTE,
    }


SERIES_TREND_VERSION = 1

SERIES_TREND_NOTE = (
    "Descriptive only; small samples carry no significance claim. "
    "No ranking. A series compares explicitly selected, mutually "
    "compatible runs against one baseline; it does not establish "
    "a series-wide direction."
)

SERIES_REGRESSION_NOTE = (
    "At least one member regressed against baseline; "
    "this does not establish a series-wide direction."
)

MAX_SERIES_MEMBERS = 50

_SERIES_RECOVERY_DEFINITION = (
    "overall recovery_success_rate per member run "
    "(informational: stability across runs is not proof of quality)"
)

_SERIES_RANGE_DEFINITION = (
    "max minus min of member overall recovery values "
    "(descriptive, unweighted spread only; not proof of better quality)"
)

_SERIES_RECURRENCE_DEFINITION = (
    "violation observed in at least two eligible members "
    "(recurrence is descriptive, not proof of causation)"
)


def _series_identity(document: Mapping[str, Any]) -> dict[str, Any]:
    """Strategy/model/mode identity used for series cohort checks.

    Strategy labels are normalized with ``strip()``: blank labels carry
    no identity and can never match, while ``" s"`` and ``"s"`` denote
    the same label.
    """
    scope = document.get("scope")
    scope = scope if _is_mapping(scope) else {}
    modes = scope.get("modes")
    modes = sorted(modes) if isinstance(modes, list) else []
    strategy = scope.get("strategy_id")
    strategy = strategy.strip() if isinstance(strategy, str) else None
    if not strategy:
        strategy = None
    return {
        "strategy_id": strategy,
        "modes": modes,
        "model": scope.get("model"),
    }


def _series_cohort_reasons(
    baseline_doc: Mapping[str, Any], member_doc: Mapping[str, Any]
) -> list[str]:
    """Fail-closed cohort checks: strategy, mode, and model must match.

    Missing strategy identity on either side excludes the member — null
    strategy IDs never count as a match.
    """
    reasons: list[str] = []
    baseline_id = _series_identity(baseline_doc)
    member_id = _series_identity(member_doc)
    baseline_strategy = baseline_id["strategy_id"]
    member_strategy = member_id["strategy_id"]
    if not (
        isinstance(baseline_strategy, str)
        and baseline_strategy
        and isinstance(member_strategy, str)
        and member_strategy
    ):
        reasons.append(
            "unconfirmed configuration: strategy_id missing on baseline "
            "or member; label runs before comparing a series"
        )
    elif baseline_strategy != member_strategy:
        reasons.append(
            f"strategy differs: {baseline_strategy!r} vs {member_strategy!r}"
        )
    if baseline_id["modes"] != member_id["modes"]:
        reasons.append(
            f"mode differs: {baseline_id['modes']} vs {member_id['modes']}"
        )
    if baseline_id["model"] != member_id["model"]:
        reasons.append(
            f"model differs: {baseline_id['model']!r} vs {member_id['model']!r}"
        )
    return reasons


def _series_member_source(document: Mapping[str, Any]) -> str | None:
    generated = document.get("generated_from")
    generated = generated if _is_mapping(generated) else {}
    source = generated.get("source")
    return source if isinstance(source, str) and source else None


def _series_member_summary(member_doc: Mapping[str, Any]) -> dict[str, Any]:
    """Per-member scorecard summary; never fabricates missing values."""
    overall = member_doc.get("overall")
    overall = overall if _is_mapping(overall) else {}
    recovery = overall.get("recovery_success_rate")
    recovery = recovery if _is_mapping(recovery) else {}
    violations = overall.get("violations_by_code")
    violations = violations if _is_mapping(violations) else {}
    evaluable = overall.get("trials_evaluable")
    evaluable = evaluable if isinstance(evaluable, int) else None
    return {
        "recovery": {
            "value": recovery.get("value"),
            "numerator": recovery.get("numerator"),
            "denominator": recovery.get("denominator"),
            "fraction": _fraction(
                recovery.get("numerator"), recovery.get("denominator")
            ),
        },
        "violations": {
            code: count
            for code, count in violations.items()
            if isinstance(count, int) and not isinstance(count, bool)
        },
        "trials_evaluable": evaluable,
    }


def _series_recovery_range(
    known: list[dict[str, Any]],
) -> dict[str, Any]:
    """Unweighted max-min spread over members with known recovery values."""
    if len(known) < 2:
        return {
            "min": None,
            "max": None,
            "span": None,
            "n_values": len(known),
            "denominators_equal": None,
            "member_fractions": [
                {
                    "source": entry["source"],
                    "fraction": entry["fraction"],
                    "numerator": entry["numerator"],
                    "denominator": entry["denominator"],
                }
                for entry in known
            ],
            "reason": "fewer than 2 known recovery values",
            "note": None,
            "definition": _SERIES_RANGE_DEFINITION,
            "better_direction": "informational",
        }
    values = [entry["value"] for entry in known]
    denominators = {entry["denominator"] for entry in known}
    integers = all(
        isinstance(entry["denominator"], int)
        and not isinstance(entry["denominator"], bool)
        for entry in known
    )
    equal = integers and len(denominators) == 1
    low, high = min(values), max(values)
    return {
        "min": low,
        "max": high,
        "span": high - low,
        "n_values": len(known),
        "denominators_equal": equal,
        "member_fractions": [
            {
                "source": entry["source"],
                "fraction": entry["fraction"],
                "numerator": entry["numerator"],
                "denominator": entry["denominator"],
            }
            for entry in known
        ],
        "reason": None,
        "note": None if equal else "unweighted spread; denominators differ",
        "definition": _SERIES_RANGE_DEFINITION,
        "better_direction": "informational",
    }


def _series_recurrence(
    eligible: list[dict[str, Any]],
) -> dict[str, Any]:
    """Violation codes recurring across eligible members.

    Only trials already evaluated by the scorecard participate: members are
    eligible, and per-member counts come from persisted-state evaluation.
    Unevaluable or excluded members never contribute zeros.
    """
    by_code: dict[str, dict[str, Any]] = {}
    for member in eligible:
        summary = member.get("scorecard") or {}
        violations = summary.get("violations") or {}
        evaluable = summary.get("trials_evaluable")
        for code, count in violations.items():
            if not isinstance(count, int) or isinstance(count, bool):
                continue
            entry = by_code.setdefault(
                code,
                {
                    "member_sources": [],
                    "total_occurrences": 0,
                    "per_member": [],
                },
            )
            if count > 0:
                entry["member_sources"].append(member["source"])
                entry["total_occurrences"] += count
            entry["per_member"].append(
                {
                    "source": member["source"],
                    "occurrences": count,
                    "evaluable_trials": evaluable,
                }
            )
    recurring: dict[str, Any] = {}
    for code in sorted(by_code):
        entry = by_code[code]
        if len(entry["member_sources"]) >= 2:
            recurring[code] = {
                "members_affected": len(entry["member_sources"]),
                "total_occurrences": entry["total_occurrences"],
                "member_sources": entry["member_sources"],
                "per_member": entry["per_member"],
                "definition": _SERIES_RECURRENCE_DEFINITION,
            }
    return recurring


def build_series_trend(
    baseline_doc: Mapping[str, Any],
    member_docs: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a descriptive reliability series anchored at one baseline.

    Pure computation over existing scorecard documents. Membership is
    explicit and ordered: each member is cohort-checked (strategy, mode,
    model) and then compared with :func:`compare_scorecard_documents`
    verbatim. Ineligible members stay visible with reasons but never affect
    aggregates. Nothing is ranked; no direction is claimed for the series.
    """
    seen: set[Any] = set()
    unique_members: list[Mapping[str, Any]] = []
    duplicate_sources: list[str] = []
    for index, document in enumerate(member_docs):
        source = _series_member_source(document)
        key = source if source is not None else ("#", index)
        if key in seen:
            duplicate_sources.append(
                source if source is not None else f"member index {index}"
            )
            continue
        seen.add(key)
        unique_members.append(document)

    baseline_province = _scorecard_provenance(baseline_doc)
    baseline_modes = _series_identity(baseline_doc)["modes"]
    baseline_entry = dict(baseline_province)
    baseline_entry["modes"] = baseline_modes

    members: list[dict[str, Any]] = []
    for document in unique_members:
        source = _series_member_source(document)
        entry: dict[str, Any] = {
            "source": source,
            "eligible": False,
            "reasons": [],
            "scorecard": None,
            "comparison_vs_baseline": None,
        }
        try:
            summary = _series_member_summary(document)
        except (ValueError, TypeError, AttributeError):
            entry["reasons"].append(
                "member scorecard is malformed and cannot be summarized"
            )
            members.append(entry)
            continue
        entry["scorecard"] = summary
        reasons = _series_cohort_reasons(baseline_doc, document)
        comparison: Mapping[str, Any] | None = None
        if not reasons:
            try:
                comparison = compare_scorecard_documents(
                    baseline_doc, document
                )
            except (ValueError, TypeError, AttributeError, KeyError):
                reasons.append("member scorecards cannot be compared")
                comparison = None
            else:
                if not comparison.get("compatible"):
                    reasons.extend(
                        str(reason)
                        for reason in comparison.get("reasons", [])
                    )
        entry["reasons"] = reasons
        if reasons:
            members.append(entry)
            continue
        entry["eligible"] = True
        entry["comparison_vs_baseline"] = comparison
        members.append(entry)

    eligible = [member for member in members if member["eligible"]]
    known_recovery = [
        {
            "source": member["source"],
            "value": member["scorecard"]["recovery"]["value"],
            "numerator": member["scorecard"]["recovery"]["numerator"],
            "denominator": member["scorecard"]["recovery"]["denominator"],
            "fraction": member["scorecard"]["recovery"]["fraction"],
        }
        for member in eligible
        if isinstance(
            member["scorecard"]["recovery"]["value"], (int, float)
        )
        and not isinstance(
            member["scorecard"]["recovery"]["value"], bool
        )
    ]
    regressed = sorted(
        str(member["source"]) if member["source"] is not None else "unknown"
        for member in eligible
        if (member["comparison_vs_baseline"] or {}).get(
            "regressions_present"
        )
        is True
    )
    warned = sorted(
        str(member["source"]) if member["source"] is not None else "unknown"
        for member in eligible
        if (member["comparison_vs_baseline"] or {}).get(
            "sample_size_warning"
        )
        is True
    )
    unknown_recovery = sorted(
        str(member["source"]) if member["source"] is not None else "unknown"
        for member in eligible
        if not isinstance(
            member["scorecard"]["recovery"]["value"], (int, float)
        )
        or isinstance(
            member["scorecard"]["recovery"]["value"], bool
        )
    )

    warnings: list[str] = []
    for duplicate in duplicate_sources:
        warnings.append(f"duplicate member '{duplicate}' ignored")
    excluded = [member for member in members if not member["eligible"]]
    if excluded:
        warnings.append(
            f"{len(excluded)} member(s) excluded: see member reasons"
        )
    if warned:
        warnings.append(
            "sample sizes below 5 in at least one member comparison; "
            "treat differences as fragile"
        )

    note = SERIES_TREND_NOTE
    if regressed:
        note = f"{note} {SERIES_REGRESSION_NOTE}"

    return {
        "series_trend_version": SERIES_TREND_VERSION,
        "baseline": baseline_entry,
        "members": members,
        "series": {
            "member_count": len(unique_members),
            "eligible_count": len(eligible),
            "recovery_values": known_recovery,
            "recovery_range": _series_recovery_range(known_recovery),
            "recurring_violations": _series_recurrence(eligible),
            "members_regressed_vs_baseline": regressed,
            "regressions_present": bool(regressed),
            "members_with_sample_warning": warned,
            "members_with_unknown_recovery": unknown_recovery,
        },
        "warnings": warnings,
        "cost": COST_NOTE,
        "note": note,
    }


def _format_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _format_rate_line(name: str, row: Mapping[str, Any]) -> str:
    baseline = row.get("baseline_fraction")
    candidate = row.get("candidate_fraction")
    if baseline is None and candidate is None:
        baseline = _format_value(row.get("baseline"))
        candidate = _format_value(row.get("candidate"))
    delta = row.get("delta")
    if isinstance(delta, float):
        delta_text = f"{delta:+.3f}"
    elif delta is None:
        delta_text = "n/a"
    else:
        delta_text = f"{delta:+}"
    direction = row.get("direction")
    if row.get("reason") and direction == "unavailable":
        return (
            f"{name}: baseline={baseline} candidate={candidate} "
            f"delta={delta_text} [{direction}: {row.get('reason')}]"
        )
    return (
        f"{name}: baseline={baseline} candidate={candidate} "
        f"delta={delta_text} [{direction}]"
    )


def format_comparison_text(comparison: Mapping[str, Any]) -> str:
    """Render a deterministic terminal summary; never declares a winner."""
    lines: list[str] = []
    baseline_src = (comparison.get("baseline") or {}).get("source")
    candidate_src = (comparison.get("candidate") or {}).get("source")
    if not comparison.get("compatible"):
        lines.append("Scorecard comparison: INCOMPATIBLE")
        lines.append(f"baseline={baseline_src} candidate={candidate_src}")
        for reason in comparison.get("reasons", []):
            lines.append(f"  - {reason}")
        lines.append(str(comparison.get("note", "")))
        return "\n".join(lines)
    lines.append("Scorecard comparison: compatible")
    lines.append(f"baseline={baseline_src} candidate={candidate_src}")
    lines.append("Overall:")
    for key in sorted((comparison.get("overall") or {})):
        lines.append(
            "  " + _format_rate_line(key, comparison["overall"][key])
        )
    for name in sorted(comparison.get("scenarios", {})):
        scenario = comparison["scenarios"][name]
        lines.append(f"Scenario '{name}':")
        for key in sorted(scenario.get("metrics", {})):
            lines.append(
                "  " + _format_rate_line(key, scenario["metrics"][key])
            )
        violations = scenario.get("violations_by_code", {})
        if violations:
            changes = ", ".join(
                f"{code}: {entry['baseline']}->{entry['candidate']} "
                f"({entry['direction']})"
                for code, entry in sorted(violations.items())
            )
            lines.append(f"  violations_by_code: {changes}")
        else:
            lines.append("  violations_by_code: none")
    overall_violations = comparison.get("violations_by_code", {})
    if overall_violations:
        lines.append(
            "Overall violations: "
            + ", ".join(
                f"{code}: {entry['baseline']}->{entry['candidate']} "
                f"({entry['direction']})"
                for code, entry in sorted(overall_violations.items())
            )
        )
    lines.append(
        f"Regressions present: "
        f"{'yes' if comparison.get('regressions_present') else 'no'}"
    )
    usage = comparison.get("usage") or {}
    if usage.get("comparable"):
        lines.append(
            "Usage (both sides complete): "
            f"input={usage['input_tokens']['baseline']}->"
            f"{usage['input_tokens']['candidate']} "
            f"output={usage['output_tokens']['baseline']}->"
            f"{usage['output_tokens']['candidate']} "
            f"total={usage['total_tokens']['baseline']}->"
            f"{usage['total_tokens']['candidate']} "
            f"responses={usage['responses']['baseline']}->"
            f"{usage['responses']['candidate']}"
        )
    else:
        lines.append(
            f"Usage not comparable "
            f"(baseline={usage.get('baseline_status')} "
            f"candidate={usage.get('candidate_status')})"
        )
    if comparison.get("sample_size_warning"):
        lines.append(
            "Sample-size warning: at least one compared rate rests on "
            "fewer than 5 trials; treat differences as fragile."
        )
    lines.append(f"Cost: {comparison.get('cost')}")
    lines.append(str(comparison.get("note", "")))
    return "\n".join(lines)


def _detect_input_kind(report: Any) -> str:
    """Classify a report dict without scoring it or touching the disk."""
    scenarios = report.get("scenarios") if _is_mapping(report) else None
    if (
        isinstance(scenarios, (list, tuple))
        and scenarios
        and _is_mapping(scenarios[0])
    ):
        if "kind" in scenarios[0]:
            return "unified"
        if "trials" in scenarios[0]:
            return "orchestrated_run"
    return "unknown"


def build_scorecard_document(
    report_dict: Mapping[str, Any],
    strategy_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Wrap :func:`score_report` output with source/provenance metadata.

    Pure: reads only ``report_dict`` and never touches the disk. Raises
    the same ``ValueError`` as :func:`score_report` for malformed input.
    """
    document = score_report(report_dict, strategy_id=strategy_id)
    document["generated_from"] = {
        "source": source,
        "input_kind": _detect_input_kind(report_dict),
        "scorecard_module": "agentcrash.experiments.scorecard",
    }
    return document


def write_scorecard_sidecar(serialized: str, output_path: Path) -> Path:
    """Write an already-serialized sidecar in one operation.

    Refuses to overwrite an existing path. Callers must fully compute and
    serialize the document before calling, so a failure upstream can never
    leave a partial or empty sidecar behind.
    """
    output_path = output_path.resolve()
    if output_path.exists():
        raise ValueError(f"Refusing to overwrite existing sidecar: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8")
    return output_path


def default_sidecar_path(input_path: Path) -> Path:
    """Sidecar location beside the source report: ``<stem>.scorecard.json``."""
    return input_path.with_name(input_path.stem + ".scorecard.json")


def default_comparison_path(baseline_path: Path, candidate_path: Path) -> Path:
    """Default comparison location beside the candidate sidecar."""
    return candidate_path.with_name(
        f"{candidate_path.stem}.vs.{baseline_path.stem}.comparison.json"
    )


def write_comparison_output(serialized: str, output_path: Path) -> Path:
    """Write an already-serialized comparison in one operation.

    Refuses to overwrite an existing path. Callers must fully compute and
    serialize the document before calling, so a failure upstream can never
    leave a partial or empty file behind.
    """
    output_path = output_path.resolve()
    if output_path.exists():
        raise ValueError(
            f"Refusing to overwrite existing comparison: {output_path}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(serialized, encoding="utf-8")
    return output_path


def _main_sidecar(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a versioned reliability scorecard sidecar from an existing "
            "experiment report (offline; no live calls, no credential access)."
        )
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="existing report JSON file"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=False,
        default=None,
        help="sidecar JSON path (default: <input-stem>.scorecard.json beside the input)",
    )
    parser.add_argument(
        "--strategy-id",
        default=None,
        help="strategy/configuration label recorded on the scorecard",
    )
    args = parser.parse_args(argv)

    # Write-safety order: read and parse, compute fully in memory, serialize
    # fully in memory, and only then check the destination and write once.
    try:
        text = args.input.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(f"Cannot read input {args.input}: {exc}")
    try:
        data = json.loads(text)
    except ValueError as exc:
        parser.error(f"Invalid JSON in {args.input}: {exc}")
    try:
        document = build_scorecard_document(
            data, strategy_id=args.strategy_id, source=str(args.input)
        )
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        parser.error(f"Cannot score report {args.input}: {exc}")
    try:
        serialized = json.dumps(document, indent=2) + "\n"
    except (ValueError, TypeError) as exc:
        parser.error(f"Cannot serialize scorecard: {exc}")
    output = args.output if args.output is not None else default_sidecar_path(args.input)
    if args.output is not None and output.resolve() == args.input.resolve():
        parser.error(f"Refusing to overwrite the source report: {args.input}")
    try:
        exported = write_scorecard_sidecar(serialized, output)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Scorecard sidecar: {exported}")


def _main_compare(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(
        prog="agentcrash.experiments.scorecard compare",
        description=(
            "Compare two scorecard sidecars (offline; no live calls, "
            "no credential access). Incompatible scorecards produce a "
            "machine-readable incompatible result, never a ranking."
        ),
    )
    parser.add_argument(
        "--baseline", type=Path, required=True, help="baseline sidecar JSON"
    )
    parser.add_argument(
        "--candidate", type=Path, required=True, help="candidate sidecar JSON"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=False,
        default=None,
        help=(
            "comparison JSON path (default: "
            "<candidate-stem>.vs.<baseline-stem>.comparison.json "
            "beside the candidate)"
        ),
    )
    args = parser.parse_args(argv)

    # Write-safety order: read and parse both inputs, compute fully in
    # memory, serialize fully in memory, and only then check the
    # destination and write once.
    try:
        baseline_text = args.baseline.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(f"Cannot read baseline {args.baseline}: {exc}")
    try:
        candidate_text = args.candidate.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(f"Cannot read candidate {args.candidate}: {exc}")
    try:
        baseline_data = json.loads(baseline_text)
    except ValueError as exc:
        parser.error(f"Invalid JSON in {args.baseline}: {exc}")
    try:
        candidate_data = json.loads(candidate_text)
    except ValueError as exc:
        parser.error(f"Invalid JSON in {args.candidate}: {exc}")
    try:
        document = compare_scorecard_documents(baseline_data, candidate_data)
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        parser.error(f"Cannot compare scorecards: {exc}")
    try:
        serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    except (ValueError, TypeError) as exc:
        parser.error(f"Cannot serialize comparison: {exc}")
    output = (
        args.output
        if args.output is not None
        else default_comparison_path(args.baseline, args.candidate)
    )
    if output.resolve() == args.baseline.resolve():
        parser.error(f"Refusing to overwrite the baseline input: {args.baseline}")
    if output.resolve() == args.candidate.resolve():
        parser.error(
            f"Refusing to overwrite the candidate input: {args.candidate}"
        )
    try:
        exported = write_comparison_output(serialized, output)
    except ValueError as exc:
        parser.error(str(exc))
    print(format_comparison_text(document))
    print(f"Comparison JSON: {exported}")


def main(argv: list[str] | None = None) -> None:
    """Dispatch to the sidecar or comparison path.

    The legacy ``--input ...`` invocation is preserved unchanged; the
    comparison path is selected only when the first argument is
    ``compare``.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    if args and args[0] == "compare":
        _main_compare(args[1:])
    else:
        _main_sidecar(args)


if __name__ == "__main__":
    main()
