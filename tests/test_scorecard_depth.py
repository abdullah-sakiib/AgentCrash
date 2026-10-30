"""Focused tests for M15 reliability depth: consistency, coverage, efficiency.

Pure computation only. Covers scope-level blocks, overall rollups,
comparison rows, v1 backward compatibility, and regressions_present
immunity. No live calls, no credentials.
"""

import copy

from agentcrash.experiments import scorecard as sc


def _trace():
    return [
        {"sequence": 1, "kind": "model_request", "action": None},
        {"sequence": 2, "kind": "model_response", "action": None},
        {"sequence": 3, "kind": "tool_call", "action": None},
        {"sequence": 4, "kind": "tool_result", "action": None},
    ]


def _trial(
    outcome="passed",
    *,
    turns=2,
    error=None,
    violations=(),
    evaluated=True,
    trace=True,
):
    return {
        "scenario": "payment_timeout_after_commit",
        "mode": "live",
        "trial_index": 1,
        "trial_id": "t-1",
        "outcome": outcome,
        "error": error,
        "turns": turns,
        "successful_charges": 1,
        "payment_ids": [1],
        "order_ids": [],
        "violations": [{"code": code} for code in violations],
        "usage": {},
        "trace": _trace() if trace else [],
        "evaluation_evidence": (
            {
                "valid": outcome == "passed",
                "source": "persisted-state-evaluation",
            }
            if evaluated and error is None
            else {}
        ),
    }


def _scope(trials, scenario="payment_timeout_after_commit"):
    return sc.score_scope(
        trials,
        scenario=scenario,
        mode="live",
        model="m",
        strategy_id="s",
        fault={"fault_type": "timeout"},
        requested=len(trials),
        live_like=True,
    )


def _recovered(turns=2, **kw):
    return _trial("passed", turns=turns, **kw)


def _failed(**kw):
    kw.setdefault("violations", ("multiple_successful_charges_for_order",))
    return _trial("failed", **kw)


# --- Outcome consistency -------------------------------------------------


def test_consistency_no_eligible_trials():
    scope = _scope([_trial("failed", error="boom"), _trial(None, error="x")])
    block = scope["outcome_consistency"]
    assert block["consistent"] is None
    assert block["eligible_trials"] == 0
    assert block["reason"] == "fewer than 2 eligible trials"
    assert block["classes"] == []
    assert "stable is not the same as successful" in block["definition"]


def test_consistency_single_eligible_trial_is_null():
    scope = _scope([_recovered(), _trial("failed", error="boom")])
    block = scope["outcome_consistency"]
    assert block["consistent"] is None
    assert block["eligible_trials"] == 1
    assert block["reason"] == "fewer than 2 eligible trials"
    assert block["classes"] == ["recovered"]


def test_consistency_uniform_recovered():
    scope = _scope([_recovered(), _recovered(turns=4)])
    block = scope["outcome_consistency"]
    assert block["consistent"] is True
    assert block["classes"] == ["recovered"]
    assert block["eligible_trials"] == 2
    assert block["reason"] is None


def test_consistency_mixed_outcomes():
    scope = _scope([_recovered(), _failed()])
    block = scope["outcome_consistency"]
    assert block["consistent"] is False
    assert block["classes"] == ["business_failure", "recovered"]
    assert block["eligible_trials"] == 2


def test_consistency_excludes_errors_and_inconsistent():
    inconsistent = _trial(
        "passed", violations=("multiple_successful_charges_for_order",)
    )
    scope = _scope(
        [_recovered(), _trial("failed", error="boom"), inconsistent]
    )
    block = scope["outcome_consistency"]
    # Only the single recovered trial is eligible.
    assert block["eligible_trials"] == 1
    assert block["consistent"] is None


def test_consistency_counts_unattempted_classes():
    idle_trace = [
        {"sequence": 1, "kind": "model_request", "action": None},
        {"sequence": 2, "kind": "model_response", "action": None},
    ]
    quiet = _trial("passed", trace=False)
    quiet["trace"] = idle_trace
    other = _trial("passed", trace=False)
    other["trace"] = [dict(event) for event in idle_trace]
    scope = _scope([quiet, other])
    block = scope["outcome_consistency"]
    assert block["consistent"] is True
    assert block["classes"] == ["passed_unattempted"]


def _idle_trial():
    """Passed, evaluated, but with no tool-call evidence: unattempted."""
    trial = _trial("passed", trace=False)
    trial["trace"] = [
        {"sequence": 1, "kind": "model_request", "action": None},
        {"sequence": 2, "kind": "model_response", "action": None},
    ]
    return trial


def _unverified_trial():
    """Passed and evaluated but completion unknown: unverified."""
    return _trial("passed", trace=False)


# --- Evidence coverage ----------------------------------------------------


def test_coverage_all_evaluable():
    scope = _scope([_recovered(), _failed()])
    block = scope["evidence_coverage"]
    assert block["value"] == 1.0
    assert block["numerator"] == 2
    assert block["denominator"] == 2
    assert block["reason"] is None


def test_coverage_penalizes_errors_and_counts_inconsistent():
    inconsistent = _trial(
        "passed", violations=("multiple_successful_charges_for_order",)
    )
    scope = _scope([_recovered(), _trial("failed", error="x"), inconsistent])
    block = scope["evidence_coverage"]
    # Error excluded; inconsistent-evidence trial stays evaluable.
    assert block["numerator"] == 2
    assert block["denominator"] == 3
    assert block["value"] == 2 / 3


def test_coverage_zero_completed_is_null():
    scope = _scope([])
    block = scope["evidence_coverage"]
    assert block["value"] is None
    assert block["numerator"] == 0
    assert block["denominator"] == 0
    assert block["reason"] == "no completed trial records"


# --- Recovery efficiency --------------------------------------------------


def test_efficiency_odd_median():
    scope = _scope([_recovered(turns=2), _recovered(turns=6), _recovered(turns=4)])
    block = scope["recovery_efficiency"]
    assert block["median_turns_to_recovery"] == 4
    assert block["recovered_trials"] == 3
    assert block["recovered_without_turn_data"] == 0
    assert block["reason"] is None


def test_efficiency_even_median():
    scope = _scope([_recovered(turns=2), _recovered(turns=4)])
    block = scope["recovery_efficiency"]
    assert block["median_turns_to_recovery"] == 3.0


def test_efficiency_rejects_non_recorded_turns():
    scope = _scope(
        [
            _recovered(turns=4),
            _recovered(turns=True),
            _recovered(turns=2.5),
            _recovered(turns=-1),
            _recovered(turns=0),
            _recovered(turns=None),
        ]
    )
    block = scope["recovery_efficiency"]
    assert block["median_turns_to_recovery"] == 4
    assert block["recovered_trials"] == 6
    assert block["recovered_without_turn_data"] == 5


def test_efficiency_no_recovered_trials():
    scope = _scope([_failed(), _trial("failed", error="x")])
    block = scope["recovery_efficiency"]
    assert block["median_turns_to_recovery"] is None
    assert block["recovered_trials"] == 0
    assert block["reason"] == "no recovered trials"


def test_efficiency_no_recorded_turns():
    scope = _scope([_recovered(turns=0), _recovered(turns=None)])
    block = scope["recovery_efficiency"]
    assert block["median_turns_to_recovery"] is None
    assert block["recovered_trials"] == 2
    assert block["recovered_without_turn_data"] == 2
    assert block["reason"] == "no recovered trials with recorded turn counts"


# --- Overall rollups ------------------------------------------------------


def _doc(entries, strategy="s"):
    return sc.build_scorecard_document(
        {
            "experiment": "agentcrash-orchestrated-run",
            "mode": "live",
            "model": "m",
            "scenarios": [
                {
                    "scenario": name,
                    "mode": "live",
                    "trials": trials,
                    "trials_requested": len(trials),
                }
                for name, trials in entries
            ],
        },
        strategy_id=strategy,
        source="r.json",
    )


def test_overall_blocks_and_version():
    doc = _doc(
        [
            ("payment_timeout_after_commit", [_recovered(), _failed()]),
            ("fulfill_before_payment", [_recovered(), _recovered()]),
        ]
    )
    assert doc["scorecard_version"] == 1
    overall = doc["overall"]
    assert overall["evidence_coverage"]["numerator"] == 4
    assert overall["evidence_coverage"]["denominator"] == 4
    assert overall["evidence_coverage"]["value"] == 1.0
    assert overall["outcome_consistency"]["consistent"] is False
    assert overall["outcome_consistency"]["classes"] == [
        "business_failure",
        "recovered",
    ]
    # No pooled median: stratified per scenario by design.
    assert overall["recovery_efficiency"]["median_turns_to_recovery"] is None
    assert (
        overall["recovery_efficiency"]["reason"] == "stratify by scenario"
    )
    assert overall["recovery_efficiency"]["recovered_trials"] == 3


def test_overall_consistency_uniform_across_scenarios():
    doc = _doc(
        [
            ("payment_timeout_after_commit", [_recovered(), _recovered()]),
            ("fulfill_before_payment", [_recovered()]),
        ]
    )
    block = doc["overall"]["outcome_consistency"]
    assert block["consistent"] is True
    assert block["eligible_trials"] == 3


# --- Comparison -----------------------------------------------------------


def test_consistency_transitions():
    uniform = _doc([("payment_timeout_after_commit", [_recovered(), _recovered()])])
    mixed = _doc([("payment_timeout_after_commit", [_recovered(), _failed()])])
    improved = sc.compare_scorecard_documents(mixed, uniform)
    row = improved["scenarios"]["payment_timeout_after_commit"]["metrics"][
        "outcome_consistency"
    ]
    assert row["direction"] == "improved"
    assert row["transition"] == "false->true"
    assert row["delta"] is None
    assert row["better_direction"] == "informational"
    assert row["baseline_eligible_trials"] == 2

    regressed = sc.compare_scorecard_documents(uniform, mixed)
    row = regressed["scenarios"]["payment_timeout_after_commit"]["metrics"][
        "outcome_consistency"
    ]
    assert row["direction"] == "regressed"
    assert row["transition"] == "true->false"
    # Informational transitions never set the regression flag, even when
    # the class change itself carries no safety-rate movement: swap one
    # unattempted trial for an unverified one (neither recovered, neither
    # a violation) and only consistency flips.
    stable_base = _doc(
        [("payment_timeout_after_commit", [_idle_trial(), _idle_trial()])]
    )
    stable_cand = _doc(
        [("payment_timeout_after_commit", [_idle_trial(), _unverified_trial()])]
    )
    drift = sc.compare_scorecard_documents(stable_base, stable_cand)
    drift_row = drift["scenarios"]["payment_timeout_after_commit"][
        "metrics"
    ]["outcome_consistency"]
    assert drift_row["direction"] == "regressed"
    assert drift_row["transition"] == "true->false"
    assert drift["overall"]["recovery_success_rate"]["direction"] == (
        "unchanged"
    )
    assert drift["overall"]["invariant_violation_rate"]["direction"] == (
        "unchanged"
    )
    assert drift["regressions_present"] is False

    same = sc.compare_scorecard_documents(mixed, copy.deepcopy(mixed))
    row = same["scenarios"]["payment_timeout_after_commit"]["metrics"][
        "outcome_consistency"
    ]
    assert row["direction"] == "unchanged"
    assert row["transition"] == "false->false"


def test_consistency_unavailable_when_missing():
    doc = _doc([("payment_timeout_after_commit", [_recovered()])])
    other = _doc([("payment_timeout_after_commit", [_recovered()])])
    result = sc.compare_scorecard_documents(doc, other)
    row = result["scenarios"]["payment_timeout_after_commit"]["metrics"][
        "outcome_consistency"
    ]
    # Single eligible trial per side: null on both sides, with the
    # scope-level explanation propagated instead of a generic message.
    assert row["direction"] == "unavailable"
    assert row["delta"] is None
    assert "fewer than 2 eligible trials" in row["reason"]


def test_efficiency_comparison_is_informational():
    baseline = _doc([("payment_timeout_after_commit", [_recovered(turns=5), _recovered(turns=5)])])
    candidate = _doc([("payment_timeout_after_commit", [_recovered(turns=2), _recovered(turns=2)])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    row = result["scenarios"]["payment_timeout_after_commit"]["metrics"][
        "recovery_efficiency"
    ]
    assert row["baseline"] == 5
    assert row["candidate"] == 2
    assert row["delta"] == -3
    assert row["direction"] == "informational-decrease"
    assert row["better_direction"] == "informational"
    assert result["regressions_present"] is False
    # Overall efficiency stays unavailable even when scopes compare.
    overall_row = result["overall"]["recovery_efficiency"]
    assert overall_row["direction"] == "unavailable"
    assert overall_row["reason"] == "stratify by scenario"


def test_coverage_comparison_delta():
    baseline = _doc(
        [("payment_timeout_after_commit", [_recovered(), _trial("failed", error="x")])]
    )
    candidate = _doc(
        [("payment_timeout_after_commit", [_recovered(), _recovered()])]
    )
    result = sc.compare_scorecard_documents(baseline, candidate)
    row = result["overall"]["evidence_coverage"]
    assert row["baseline_fraction"] == "1/2"
    assert row["candidate_fraction"] == "2/2"
    assert row["delta"] == 0.5
    assert row["direction"] == "informational-increase"
    assert result["regressions_present"] is False


def _strip_depth(document):
    """Simulate a historical v1 sidecar without reliability-depth fields."""
    pruned = copy.deepcopy(document)
    for scope in pruned["scopes"]:
        for key in (
            "outcome_consistency",
            "evidence_coverage",
            "recovery_efficiency",
        ):
            scope.pop(key, None)
    for key in (
        "outcome_consistency",
        "evidence_coverage",
        "recovery_efficiency",
    ):
        pruned["overall"].pop(key, None)
    return pruned


def test_v1_sidecars_compare_without_crashing():
    fresh = _doc([("payment_timeout_after_commit", [_recovered(), _failed()])])
    legacy = _strip_depth(fresh)
    assert legacy["scorecard_version"] == 1
    assert "outcome_consistency" not in legacy["scopes"][0]
    result = sc.compare_scorecard_documents(legacy, copy.deepcopy(fresh))
    assert result["compatible"] is True
    consistency = result["scenarios"]["payment_timeout_after_commit"][
        "metrics"
    ]["outcome_consistency"]
    assert consistency["direction"] == "unavailable"
    assert consistency["delta"] is None
    # Coverage falls back to trial_counts, so v1 scopes still compare.
    coverage = result["scenarios"]["payment_timeout_after_commit"][
        "metrics"
    ]["evidence_coverage"]
    assert coverage["direction"] == "unchanged"
    assert coverage["delta"] == 0
    # Overall historical gap is explicit, not fabricated.
    assert result["overall"]["outcome_consistency"]["direction"] == (
        "unavailable"
    )
    assert "historical scorecard" in (
        result["overall"]["outcome_consistency"]["reason"]
    )
    assert result["regressions_present"] is False


def test_v1_sidecar_overall_scoring_unchanged():
    legacy_report = {
        "experiment": "agentcrash-orchestrated-run",
        "mode": "live",
        "model": "m",
        "scenarios": [
            {
                "scenario": "payment_timeout_after_commit",
                "mode": "live",
                "trials": [_recovered(), _failed()],
                "trials_requested": 2,
            }
        ],
    }
    doc = sc.build_scorecard_document(legacy_report, source="old.json")
    assert doc["scorecard_version"] == 1
    # Legacy rates are untouched by the new blocks.
    assert doc["overall"]["recovery_success_rate"]["value"] == 0.5
    assert doc["overall"]["invariant_violation_rate"]["value"] == 0.5
