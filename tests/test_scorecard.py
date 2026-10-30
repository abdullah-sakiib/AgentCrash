"""Focused tests for the reliability scorecard (pure computation only)."""

import json

from agentcrash.experiments import scorecard as sc


def _scope(trials, scenario="payment_timeout_after_commit", mode="live",
           model="fake-model", requested=None, fault=None):
    return sc.score_scope(
        trials,
        scenario=scenario,
        mode=mode,
        model=model,
        strategy_id="test-strategy",
        fault=fault,
        requested=requested if requested is not None else len(trials),
        live_like=(mode == "live"),
    )


def _trace_kinds(*kinds):
    return [
        {"sequence": i + 1, "kind": kind, "action": None}
        for i, kind in enumerate(kinds)
        if kind is not None
    ]


def _trace_actions(*actions):
    return [
        {"sequence": i + 1, "kind": None, "action": action}
        for i, action in enumerate(actions)
    ]


def _usage_complete(total=100):
    return {
        "input_tokens": total,
        "output_tokens": total,
        "total_tokens": total * 2,
        "responses": 2,
    }


def _live_trial(
    *,
    outcome="passed",
    error=None,
    tool_calls=1,
    violations=(),
    charges=1,
    usage=None,
    evaluated=True,
):
    trace = (
        ["model_request", "model_response"]
        + ["tool_call", "tool_result"] * tool_calls
    )
    return {
        "scenario": "payment_timeout_after_commit",
        "mode": "live",
        "trial_index": 1,
        "trial_id": "t-1",
        "outcome": outcome,
        "error": error,
        "turns": 2,
        "successful_charges": charges,
        "payment_ids": [1] if charges else [],
        "order_ids": [],
        "violations": [{"code": code} for code in violations],
        "usage": _usage_complete() if usage is None else usage,
        "trace": _trace_kinds(*trace) if tool_calls or outcome else [],
        "evaluation_evidence": (
            {"valid": outcome == "passed", "source": "persisted-state-evaluation"}
            if evaluated and error is None
            else {}
        ),
    }


def _det_trial(*, outcome="passed", duplicate=False):
    return {
        "outcome": outcome,
        "error": None,
        "turns": 0,
        "successful_charges": 2 if duplicate else 1,
        "payment_ids": [1, 2] if duplicate else [1],
        "order_ids": [],
        "violations": (
            [{"code": "multiple_successful_charges_for_order"}] if duplicate else []
        ),
        "usage": {},
        "trace": _trace_actions("set_inventory", "create_order", "evaluate_state"),
        "evaluation_evidence": {
            "valid": outcome == "passed",
            "source": "persisted-state-evaluation",
        },
    }


def _scope(trials, scenario="payment_timeout_after_commit", mode="live",
           model="m", requested=None, fault=None):
    entry = {
        "scenario": scenario,
        "mode": mode,
        "trials": trials,
        "trials_requested": requested if requested is not None else len(trials),
    }
    return sc.score_scope(
        [dict(t, scenario=scenario, mode=mode) for t in trials],
        scenario=scenario,
        mode=mode,
        model=model,
        fault=fault,
        requested=entry["trials_requested"],
        live_like=(mode == "live"),
    )


def test_mixed_outcomes_exclude_errors_from_quality_denominators():
    trials = [
        _live_trial(outcome="passed"),  # recovered
        _live_trial(outcome="failed", violations=["multiple_successful_charges_for_order"], charges=2),
        {**_live_trial(), "outcome": None, "error": "boom", "evaluation_evidence": {}},
    ]
    scope = _scope(trials)
    assert scope["recovery_success_rate"] == {"value": 0.5, "numerator": 1, "denominator": 2, "definition": scope["recovery_success_rate"]["definition"]}
    assert scope["invariant_violation_rate"]["numerator"] == 1
    assert scope["invariant_violation_rate"]["denominator"] == 2
    assert scope["infrastructure_error_rate"] == {"value": 1 / 3, "numerator": 1, "denominator": 3, "definition": scope["infrastructure_error_rate"]["definition"]}
    assert scope["trial_counts"]["errored"] == 1
    assert scope["trial_counts"]["completed"] == 3


def test_zero_denominators_yield_null():
    empty = _scope([], scenario="payment_timeout_after_commit")
    assert empty["recovery_success_rate"]["value"] is None
    assert empty["recovery_success_rate"]["denominator"] == 0
    assert empty["task_pass_rate"]["value"] is None
    assert empty["invariant_violation_rate"]["value"] is None
    assert empty["infrastructure_error_rate"]["value"] is None

    all_errors = _scope(
        [{**_live_trial(), "outcome": None, "error": "x", "evaluation_evidence": {}}]
    )
    assert all_errors["recovery_success_rate"]["value"] is None
    assert all_errors["recovery_success_rate"]["denominator"] == 0
    assert all_errors["invariant_violation_rate"]["value"] is None
    # Errors still count in their own rate.
    assert all_errors["infrastructure_error_rate"] == {
        "value": 1.0, "numerator": 1, "denominator": 1,
        "definition": all_errors["infrastructure_error_rate"]["definition"],
    }


def test_abandoned_pass_does_not_count_as_recovery():
    # Valid end state, but the agent never acted: no recovery demonstrated.
    abandoned = _live_trial(outcome="passed", tool_calls=0)
    abandoned["trace"] = _trace_kinds("model_request", "model_response")
    scope = _scope([abandoned])
    assert scope["recovery_success_rate"] == {
        "value": 0.0, "numerator": 0, "denominator": 1,
        "definition": scope["recovery_success_rate"]["definition"],
    }
    assert scope["passed_without_completion_evidence"] == 1
    # Legacy-compatible rate still counts the pass (documented difference).
    assert scope["task_pass_rate"]["value"] == 1.0


def test_duplicate_charges_counted_with_totals():
    trials = [
        _live_trial(outcome="failed",
                    violations=["multiple_successful_charges_for_order"],
                    charges=3),
        _live_trial(outcome="passed"),
    ]
    scope = _scope(trials, scenario="payment_timeout_after_commit")
    dup = scope["duplicate_charge_trial_rate"]
    assert dup["value"] == 0.5
    assert dup["numerator"] == 1
    assert dup["denominator"] == 2
    assert dup["total_duplicate_charges"] == 2
    assert dup["applicable"] is True
    # Fulfillment scenarios must not inherit payment metrics.
    scope_f = _scope(trials, scenario="fulfill_before_payment")
    assert scope_f["duplicate_charge_trial_rate"]["value"] is None
    assert scope_f["duplicate_charge_trial_rate"]["numerator"] is None
    assert scope_f["duplicate_charge_trial_rate"]["applicable"] is False


def test_duplicate_orders_counted_for_intent_scenarios():
    def intent_trial(order_ids, outcome):
        t = _live_trial(outcome=outcome)
        t["order_ids"] = order_ids
        t["scenario"] = "duplicate_order_for_intent"
        if len(order_ids) > 1:
            t["violations"] = [{"code": "duplicate_orders_for_intent"}]
        return t

    scope = _scope(
        [intent_trial([1], "passed"), intent_trial([1, 2], "failed")],
        scenario="duplicate_order_for_intent",
    )
    dup = scope["duplicate_order_trial_rate"]
    assert dup["value"] == 0.5
    assert dup["numerator"] == 1
    assert dup["denominator"] == 2
    assert dup["total_duplicate_orders"] == 1
    assert dup["applicable"] is True
    # Payment scopes must not report order duplicates.
    scope_p = _scope(
        [intent_trial([1], "passed")], scenario="payment_timeout_after_commit"
    )
    assert scope_p["duplicate_order_trial_rate"]["value"] is None
    assert scope_p["duplicate_order_trial_rate"]["applicable"] is False


def test_missing_partial_zero_usage():
    missing = _live_trial()
    missing["usage"] = {}
    partial = _live_trial()
    partial["usage"] = {"input_tokens": 10, "responses": 1}
    zero = _live_trial()
    zero["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 1}
    scope = _scope([missing, partial, zero])
    assert scope["usage"]["trials_missing_usage"] == 1
    assert scope["usage"]["trials_partial_usage"] == 1
    assert scope["usage"]["trials_with_usage"] == 1
    # Missing usage contributes nothing; zero is a confirmed zero, not missing.
    assert scope["usage"]["total_tokens"] == 0
    assert scope["usage"]["status"] == "partial"
    assert scope["cost"] == sc.COST_NOTE


def test_inconsistent_evidence_excluded_and_counted():
    trial = _live_trial(outcome="passed")
    trial["evaluation_evidence"] = {"valid": False, "source": "x"}
    scope = _scope([trial])
    assert scope["trial_counts"]["inconsistent_evidence"] == 1
    assert scope["recovery_success_rate"] == {
        "value": None, "numerator": 0, "denominator": 0,
        "definition": scope["recovery_success_rate"]["definition"],
    }


def test_unified_normalized_input_is_qualified_not_silent():
    # Unified normalization drops traces/evidence: recovery must be
    # unavailable, not zero; legacy pass rate still computable.
    entry = {
        "scenario": "payment_timeout_after_commit",
        "model": "m",
        "trials_requested": 2,
        "trials_completed": 2,
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "usage": {"status": "missing"},
        "violation_counts": {},
        "trials": [
            {"outcome": "passed", "error": None, "turns": 0,
             "successful_charges": 1, "violations": []},
            {"outcome": "failed", "error": None, "turns": 0,
             "successful_charges": 1,
             "violations": ["multiple_successful_charges_for_order"]},
        ],
        "details": {},
    }
    scope = sc.score_scope(
        [
            {
                "outcome": t["outcome"], "error": t["error"],
                "turns": t["turns"], "successful_charges": t["successful_charges"],
                "violations": [{"code": c} for c in t["violations"]],
                "trace": [], "evaluation_evidence": {}, "usage": {},
            }
            for t in entry["trials"]
        ],
        scenario=entry["scenario"], mode="unknown", model="m",
        fault={"fault_type": "unknown"},
        requested=entry["trials_requested"], live_like=False,
    )
    assert scope["recovery_success_rate"]["value"] is None
    assert scope["trial_counts"]["unknown_completion"] == 2
    # Violations survive normalization and are still counted.
    assert scope["violations_by_code"] == {"multiple_successful_charges_for_order": 1}
    assert scope["task_pass_rate"]["value"] == 0.5


def test_compare_matching_scorecards_is_compatible():
    def report(model):
        return {
            "model": model, "mode": "live",
            "scenarios": [{
                "scenario": "payment_timeout_after_commit",
                "mode": "live", "trials": [_live_trial()],
                "trials_requested": 1,
            }],
        }

    a = sc.score_report(report("m1"), strategy_id="s")
    b = sc.score_report(report("m1"), strategy_id="s")
    result = sc.compare_scorecards(a, b)
    assert result["compatible"] is True
    assert result["reasons"] == []
    assert result["deltas"]["payment_timeout_after_commit"][
        "recovery_success_rate"] == 0.0


def test_compare_flags_mismatches_without_ranking():

    base = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "scenario": "payment_timeout_after_commit", "mode": "live",
            "trials": [_live_trial()], "trials_requested": 1,
        }],
    }
    other_scenario = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "scenario": "fulfill_before_payment", "mode": "live",
            "trials": [_live_trial()], "trials_requested": 1,
        }],
    }
    result = sc.compare_scorecards(sc.score_report(base), sc.score_report(other_scenario))
    assert result["compatible"] is False
    assert result["deltas"] == {}
    assert any("scenario" in r for r in result["reasons"])
    assert "winner" not in result and "ranking" not in result


def test_tool_calls_and_turns_aggregated_per_evaluable_trial():
    def trial_with(n_calls, n_responses, turns):
        trace = []
        seq = 1
        for _ in range(n_calls):
            trace.append({"sequence": seq, "kind": "tool_call", "action": None})
            seq += 1
        for _ in range(n_responses):
            trace.append({"sequence": seq, "kind": "model_response", "action": None})
            seq += 1
        return {
            "scenario": "payment_timeout_after_commit", "mode": "live",
            "outcome": "passed", "error": None, "turns": turns,
            "successful_charges": 1, "payment_ids": [1], "order_ids": [],
            "violations": [],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "responses": n_responses or 1},
            "trace": trace,
            "evaluation_evidence": {"valid": True, "source": "persisted-state-evaluation"},
        }

    scope = sc.score_scope(
        [trial_with(2, 2, 3), trial_with(1, 1, 2)],
        scenario="payment_timeout_after_commit", mode="live",
    )
    assert scope["turns_total"] == 5
    assert scope["tool_calls"] == {
        "total": 3, "mean_per_evaluable_trial": 1.5,
        "trials_with_tool_call_traces": 2,
    }
    assert scope["model_responses"] == {"total": 3, "trials_with_response_traces": 2}


def test_no_kind_based_traces_yields_null_operational_means():
    trial = _det_trial(outcome="passed")
    scope = sc.score_scope([trial], scenario="duplicate_order_for_intent", mode="deterministic")
    # Deterministic action-style traces carry no tool_call kinds: the
    # operational metrics must be null, not zero.
    assert scope["tool_calls"]["total"] == 0
    assert scope["tool_calls"]["mean_per_evaluable_trial"] is None
    assert scope["tool_calls"]["trials_with_tool_call_traces"] == 0
    # ...while completion is still established by the recorded strategy run.
    assert scope["recovery_success_rate"]["value"] == 1.0


def test_unified_document_end_to_end_with_known_and_unknown_faults():
    unified = {
        "scenarios": [
            {
                "kind": "payment_multi", "scenario": "payment_timeout_after_commit",
                "model": "m", "trials_requested": 1, "trials_completed": 1,
                "passed": 1, "failed": 0, "errors": 0, "pass_rate": 1.0,
                "turns_total": 0, "successful_charges_total": 1,
                "duplicate_charge_trials": 0, "usage": {"status": "missing"},
                "violation_counts": {}, "baselines": [],
                "trials": [{
                    "trial_index": 1, "trial_id": "t", "outcome": "passed",
                    "error": None, "turns": 0, "successful_charges": 1,
                    "violations": [],
                }],
                "details": {"per_trial_payment_ids": [[1]]},
            },
            {
                "kind": "mystery_kind", "scenario": "mystery_scenario",
                "model": "m", "trials_requested": 1, "trials_completed": 1,
                "passed": 1, "failed": 0, "errors": 0, "pass_rate": 1.0,
                "turns_total": 0, "successful_charges_total": 1,
                "duplicate_charge_trials": None, "usage": {"status": "missing"},
                "violation_counts": {}, "baselines": [],
                "trials": [{
                    "trial_index": 1, "trial_id": "t", "outcome": "passed",
                    "error": None, "turns": 0, "successful_charges": 1,
                    "violations": [],
                }],
                "details": {},
            },
        ],
    }
    report = sc.score_report(unified, strategy_id="s")
    by_name = {s["scenario"]: s for s in report["scopes"]}
    assert by_name["payment_timeout_after_commit"]["fault"] == {
        "fault_type": "timeout", "target": "payment.create", "point": "after_commit",
    }
    assert by_name["payment_timeout_after_commit"]["recovery_success_rate"]["value"] is None
    assert by_name["payment_timeout_after_commit"]["trial_counts"]["unknown_completion"] == 1
    assert by_name["mystery_scenario"]["duplicate_charge_trial_rate"]["value"] is None
    assert by_name["mystery_scenario"]["duplicate_charge_trial_rate"]["applicable"] is False
    assert by_name["mystery_scenario"]["fault"] is None
    # Self-comparison is flagged incompatible: the mystery scope carries an
    # unknown fault, so equivalence of conditions cannot be confirmed.
    cmp_result = sc.compare_scorecards(report, report)
    assert cmp_result["compatible"] is False
    assert any("fault" in r for r in cmp_result["reasons"])
    # A single-scope report with a known fault compares cleanly with itself.
    payment_only = sc.score_report(
        {"model": "m", "mode": "live", "scenarios": [unified["scenarios"][0]]},
        strategy_id="s",
    )
    assert sc.compare_scorecards(payment_only, payment_only)["compatible"] is True


def test_score_report_rejects_malformed_input():
    import pytest

    base = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "scenario": "payment_timeout_after_commit", "mode": "live",
            "trials": [_live_trial()], "trials_requested": 1,
        }],
    }
    with pytest.raises((ValueError, TypeError, AttributeError)):
        sc.score_report({})
    with pytest.raises((ValueError, TypeError, AttributeError)):
        sc.score_report({"scenarios": []})
    with pytest.raises((ValueError, TypeError, AttributeError)):
        sc.score_report({"scenarios": [{"no": "shape"}]})

    # Fault mismatch.
    a = sc.score_report(base)
    b = sc.score_report(base, faults={"payment_timeout_after_commit": {"fault_type": "other"}})
    result = sc.compare_scorecards(a, b)
    assert result["compatible"] is False
    assert any("fault" in r for r in result["reasons"])

    # Trial-count mismatch.
    one = sc.score_report(base)
    two_trials = dict(base)
    two = sc.score_report({
        "model": "m", "mode": "live",
        "scenarios": [{
            "scenario": "payment_timeout_after_commit", "mode": "live",
            "trials": [_live_trial(), _live_trial()], "trials_requested": 2,
        }],
    })
    result = sc.compare_scorecards(one, two)
    assert result["compatible"] is False
    assert any("trial" in r for r in result["reasons"])

    # Unknown fault on either side blocks comparison.
    unknown = sc.score_report(base, faults={})
    known = sc.score_report(
        base, faults={"payment_timeout_after_commit": {"fault_type": "timeout"}},
        default_faults={},
    )
    result = sc.compare_scorecards(unknown, known)
    assert result["compatible"] is False


def test_deterministic_trials_count_as_completed():
    scope = _scope(
        [_det_trial(outcome="passed"), _det_trial(outcome="failed", duplicate=True)],
        scenario="payment_timeout_after_commit", mode="deterministic",
    )
    assert scope["recovery_success_rate"] == {
        "value": 0.5, "numerator": 1, "denominator": 2,
        "definition": scope["recovery_success_rate"]["definition"],
    }
    assert scope["duplicate_charge_trial_rate"]["numerator"] == 1


def _det_trial(*, outcome="passed", duplicate=False):
    return {
        "scenario": "payment_timeout_after_commit",
        "mode": "deterministic",
        "outcome": outcome,
        "error": None,
        "turns": 0,
        "successful_charges": 2 if duplicate else 1,
        "payment_ids": [1, 2] if duplicate else [1],
        "order_ids": [],
        "violations": (
            [{"code": "multiple_successful_charges_for_order"}] if duplicate else []
        ),
        "usage": {},
        "trace": [
            {"sequence": 1, "action": "set_inventory"},
            {"sequence": 5, "action": "evaluate_state"},
        ],
        "evaluation_evidence": {
            "valid": outcome == "passed",
            "source": "persisted-state-evaluation",
        },
    }


def test_violation_only_trial_counts_in_duplicate_rates():
    # Reviewer reproduction: a failed trial with no evidence dict but a
    # recorded duplicate-charge violation is evaluable, so violations_by_code
    # and the duplicate-charge rate must agree (previously 1 vs 0.0).
    trial = {
        "outcome": "failed", "error": None, "turns": 0,
        "successful_charges": 1,
        "violations": [{"code": "multiple_successful_charges_for_order"}],
        "trace": [], "evaluation_evidence": {}, "usage": {},
    }
    scope = _scope([trial], scenario="payment_timeout_after_commit",
                   mode="unknown")
    assert scope["violations_by_code"] == {
        "multiple_successful_charges_for_order": 1
    }
    assert scope["duplicate_charge_trial_rate"]["numerator"] == 1
    assert scope["duplicate_charge_trial_rate"]["denominator"] == 1
    assert scope["duplicate_charge_trial_rate"]["value"] == 1.0
    assert scope["duplicate_charge_trial_rate"]["total_duplicate_charges"] == 0


def test_normalized_violation_only_row_counts_in_duplicate_rates():
    # Same guarantee through the unified-normalized path, which never
    # carries per-trial evidence dicts.
    unified = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "kind": "payment_multi", "scenario": "payment_timeout_after_commit",
            "model": "m", "trials_requested": 1, "trials_completed": 1,
            "passed": 0, "failed": 1, "errors": 0, "pass_rate": 0.0,
            "turns_total": 0, "successful_charges_total": 1,
            "duplicate_charge_trials": 0, "usage": {"status": "missing"},
            "violation_counts": {"multiple_successful_charges_for_order": 1},
            "baselines": [],
            "trials": [{
                "trial_index": 1, "trial_id": "t", "outcome": "failed",
                "error": None, "turns": 0, "successful_charges": 1,
                "violations": ["multiple_successful_charges_for_order"],
            }],
            "details": {"per_trial_payment_ids": [[1]]},
        }],
    }
    report = sc.score_report(unified, strategy_id="s")
    scope = report["scopes"][0]
    assert scope["violations_by_code"] == {
        "multiple_successful_charges_for_order": 1
    }
    assert scope["duplicate_charge_trial_rate"]["numerator"] == 1
    assert scope["duplicate_charge_trial_rate"]["value"] == 1.0


def test_duplicate_applicability_all_scenarios_and_unknown():
    payment = _scope([_live_trial()], scenario="payment_timeout_after_commit")
    assert payment["duplicate_charge_trial_rate"]["applicable"] is True
    assert payment["duplicate_order_trial_rate"]["applicable"] is False
    assert payment["duplicate_order_trial_rate"]["value"] is None

    fulfill = _scope([_live_trial()], scenario="fulfill_before_payment")
    assert fulfill["duplicate_charge_trial_rate"]["applicable"] is False
    assert fulfill["duplicate_charge_trial_rate"]["value"] is None
    assert fulfill["duplicate_order_trial_rate"]["applicable"] is False
    assert fulfill["duplicate_order_trial_rate"]["value"] is None

    intent = _scope(
        [_live_trial()], scenario="duplicate_order_for_intent")
    assert intent["duplicate_order_trial_rate"]["applicable"] is True
    assert intent["duplicate_charge_trial_rate"]["applicable"] is False
    assert intent["duplicate_charge_trial_rate"]["value"] is None

    # Hyphenated spellings resolve to the same scenarios.
    hyphen = _scope([_live_trial()], scenario="payment-timeout-after-commit")
    assert hyphen["duplicate_charge_trial_rate"]["applicable"] is True
    hyphen_intent = _scope(
        [_live_trial()], scenario="duplicate-order-for-intent")
    assert hyphen_intent["duplicate_order_trial_rate"]["applicable"] is True

    # Unknown and lookalike names fail closed to null, never inheriting
    # another scenario's metrics.
    for unknown in ("mystery_scenario", "payment_timeout_before_commit",
                    "fulfill_before_payment_extra"):
        scope = _scope([_live_trial()], scenario=unknown)
        assert scope["duplicate_charge_trial_rate"]["value"] is None
        assert scope["duplicate_charge_trial_rate"]["numerator"] is None
        assert scope["duplicate_charge_trial_rate"]["applicable"] is False
        assert scope["duplicate_order_trial_rate"]["value"] is None
        assert scope["duplicate_order_trial_rate"]["applicable"] is False


def test_tool_call_mean_uses_evaluable_population():
    evaluated = _live_trial(outcome="passed")
    evaluated["trace"] = _trace_kinds("model_request", "model_response",
                                      "tool_call", "tool_result",
                                      "tool_call", "tool_result")
    unevaluable = _live_trial(outcome="passed")
    unevaluable["trace"] = _trace_kinds(
        "model_request", "model_response",
        *["tool_call", "tool_result"] * 5)
    unevaluable["evaluation_evidence"] = {}
    scope = _scope([evaluated, unevaluable])
    assert scope["trial_counts"]["evaluable"] == 1
    assert scope["tool_calls"]["total"] == 7
    assert scope["tool_calls"]["mean_per_evaluable_trial"] == 2.0


def test_boolean_usage_values_are_rejected():
    trial = _live_trial()
    trial["usage"] = {"input_tokens": True, "output_tokens": True,
                      "total_tokens": True, "responses": True}
    scope = _scope([trial])
    assert scope["usage"]["status"] == "missing"
    assert scope["usage"]["trials_missing_usage"] == 1
    assert scope["usage"]["trials_with_usage"] == 0
    assert scope["usage"]["input_tokens"] == 0
    assert scope["usage"]["total_tokens"] == 0
    assert scope["usage"]["responses"] == 0


def test_compare_flags_requested_count_mismatch():
    base = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "scenario": "payment_timeout_after_commit", "mode": "live",
            "trials": [_live_trial()], "trials_requested": 1,
        }],
    }
    requested_two = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "scenario": "payment_timeout_after_commit", "mode": "live",
            "trials": [_live_trial()], "trials_requested": 2,
        }],
    }
    result = sc.compare_scorecards(
        sc.score_report(base), sc.score_report(requested_two))
    assert result["compatible"] is False
    assert any("requested" in r for r in result["reasons"])
    assert result["deltas"] == {}

    # Unknown requested counts block comparison, like unknown faults.
    missing_requested = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "scenario": "payment_timeout_after_commit", "mode": "live",
            "trials": [_live_trial()],
        }],
    }
    result = sc.compare_scorecards(
        sc.score_report(base), sc.score_report(missing_requested))
    assert result["compatible"] is False
    assert any("requested" in r for r in result["reasons"])
