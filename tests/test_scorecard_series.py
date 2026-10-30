"""Offline tests for the M16 reliability series (pure computation only).

No live calls, no credentials, no artifact writes: scorecard documents are
built in memory from synthetic reports.
"""

import copy
import json

import pytest

from agentcrash.experiments import scorecard as sc


def _trace(n_calls=1):
    events = [
        {"sequence": 1, "kind": "model_request"},
        {"sequence": 2, "kind": "model_response"},
    ]
    seq = 3
    for _ in range(n_calls):
        events.append({"sequence": seq, "kind": "tool_call"})
        seq += 1
        events.append({"sequence": seq, "kind": "tool_result"})
        seq += 1
    return events


def _trial(scenario, outcome="passed", **overrides):
    trial = {
        "scenario": scenario,
        "mode": "live",
        "trial_index": 1,
        "trial_id": "t-1",
        "outcome": outcome,
        "error": None,
        "turns": 2,
        "successful_charges": 1,
        "payment_ids": [1],
        "order_ids": [],
        "violations": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "responses": 1,
        },
        "trace": _trace(),
        "evaluation_evidence": {
            "valid": outcome == "passed",
            "source": "persisted-state-evaluation",
        },
    }
    trial.update(overrides)
    return trial


def _report(entries):
    scenarios = []
    for name, trials in entries:
        scenarios.append(
            {
                "scenario": name,
                "mode": "live",
                "trials": trials,
                "trials_requested": len(trials),
            }
        )
    return {
        "experiment": "agentcrash-orchestrated-run",
        "mode": "live",
        "model": "fake-model",
        "scenarios": scenarios,
    }


def _doc(entries, strategy="s", source="r.json"):
    return sc.build_scorecard_document(
        _report(entries), strategy_id=strategy, source=source
    )


PAY = "payment_timeout_after_commit"
INTENT = "duplicate_order_for_intent"


def _pay_pass(**kw):
    return _trial(PAY, "passed", **kw)


def _pay_fail_dup(**kw):
    kw.setdefault("charges", 2)
    kw.setdefault("payment_ids", [1, 2])
    kw.setdefault("violations", [{"code": "multiple_successful_charges_for_order"}])
    return _trial(PAY, "failed", **kw)


def test_whitespace_strategy_ids_are_ineligible():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="   ", source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy="  ", source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    entry = trend["members"][0]
    assert entry["eligible"] is False
    assert any("unconfirmed configuration" in r for r in entry["reasons"])
    assert trend["series"]["eligible_count"] == 0


def test_padded_strategy_ids_match_after_normalization():
    baseline = _doc([(PAY, [_pay_pass()])], strategy=" s", source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy="s ", source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is True
    assert trend["members"][0]["reasons"] == []


def test_matching_strategy_ids_are_eligible():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="b.json"
    )
    member = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])], strategy="a", source="m.json"
    )
    trend = sc.build_series_trend(baseline, [member])
    assert trend["series_trend_version"] == 1
    assert trend["series"]["eligible_count"] == 1
    entry = trend["members"][0]
    assert entry["eligible"] is True
    assert entry["reasons"] == []
    assert entry["comparison_vs_baseline"] is not None
    assert entry["comparison_vs_baseline"]["compatible"] is True


def test_null_strategy_ids_are_ineligible():
    baseline = _doc([(PAY, [_pay_pass()])], strategy=None, source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy=None, source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    entry = trend["members"][0]
    assert entry["eligible"] is False
    assert any("unconfirmed configuration" in r for r in entry["reasons"])
    assert entry["comparison_vs_baseline"] is None
    assert trend["series"]["eligible_count"] == 0
    # Excluded members never affect aggregates.
    assert trend["series"]["recovery_range"]["n_values"] == 0
    assert trend["series"]["recurring_violations"] == {}


def test_baseline_null_member_set_is_ineligible():
    baseline = _doc([(PAY, [_pay_pass()])], strategy=None, source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy="a", source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is False


def test_baseline_set_member_null_is_ineligible():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a", source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy=None, source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is False


def test_strategy_mismatch_is_ineligible():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a", source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy="b", source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    entry = trend["members"][0]
    assert entry["eligible"] is False
    assert any("strategy differs" in r for r in entry["reasons"])


def test_mode_mismatch_is_ineligible():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a", source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy="a", source="m.json")
    member["scope"]["modes"] = ["deterministic"]
    trend = sc.build_series_trend(baseline, [member])
    entry = trend["members"][0]
    assert entry["eligible"] is False
    assert any("mode differs" in r for r in entry["reasons"])


def test_model_mismatch_is_ineligible():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a", source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy="a", source="m.json")
    member["scope"]["model"] = "other-model"
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is False
    assert any("model differs" in r for r in trend["members"][0]["reasons"])


def test_deterministic_null_model_matches():
    report = _report([(PAY, [_pay_pass()])])
    report["mode"] = "deterministic"
    report["model"] = None
    baseline = sc.build_scorecard_document(
        copy.deepcopy(report), strategy_id="a", source="b.json"
    )
    member = sc.build_scorecard_document(
        copy.deepcopy(report), strategy_id="a", source="m.json"
    )
    assert baseline["scope"]["model"] is None
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is True


def test_scenario_mismatch_uses_m12_gate():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a", source="b.json")
    member = _doc([(INTENT, [_pay_pass()])], strategy="a", source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    entry = trend["members"][0]
    assert entry["eligible"] is False
    assert any("scenario sets differ" in r for r in entry["reasons"])


def test_count_mismatch_uses_m12_gate():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="b.json"
    )
    member = _doc([(PAY, [_pay_pass()])], strategy="a", source="m.json")
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is False


def test_unknown_requested_counts_block():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a", source="b.json")
    member = _doc([(PAY, [_pay_pass()])], strategy="a", source="m.json")
    member["scopes"][0]["trial_counts"]["requested"] = None
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is False


def test_v1_sidecars_compare_without_crashing():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])],
        strategy="a",
        source="b.json",
    )
    member = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="m.json"
    )
    for document in (baseline, member):
        for scope in document["scopes"]:
            scope.pop("outcome_consistency", None)
            scope.pop("evidence_coverage", None)
            scope.pop("recovery_efficiency", None)
        for key in (
            "outcome_consistency",
            "evidence_coverage",
            "recovery_efficiency",
        ):
            document["overall"].pop(key, None)
    trend = sc.build_series_trend(baseline, [member])
    assert trend["members"][0]["eligible"] is True
    row = trend["members"][0]["comparison_vs_baseline"]["overall"][
        "recovery_success_rate"
    ]
    assert row["direction"] == "improved"


def test_empty_members_yield_null_aggregates():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a")
    trend = sc.build_series_trend(baseline, [])
    assert trend["series"]["member_count"] == 0
    assert trend["series"]["eligible_count"] == 0
    assert trend["series"]["recovery_range"]["reason"] == (
        "fewer than 2 known recovery values"
    )
    assert trend["series"]["regressions_present"] is False


def test_single_member_range_is_null():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a")
    member = _doc([(PAY, [_pay_pass()])], strategy="a")
    trend = sc.build_series_trend(baseline, [member])
    assert trend["series"]["recovery_range"]["reason"] == (
        "fewer than 2 known recovery values"
    )


def test_range_equal_denominators():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])], strategy="a", source="b.json"
    )
    first = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="m1.json"
    )
    second = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])], strategy="a", source="m2.json"
    )
    trend = sc.build_series_trend(baseline, [first, second])
    span = trend["series"]["recovery_range"]
    assert span["min"] == 0.5
    assert span["max"] == 1.0
    assert span["span"] == 0.5
    assert span["n_values"] == 2
    assert span["denominators_equal"] is True
    assert span["note"] is None
    assert [m["fraction"] for m in span["member_fractions"]] == ["2/2", "1/2"]


def test_range_unequal_denominators_reports_note():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])], strategy="a", source="b.json"
    )
    full = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="m1.json"
    )
    errored = _doc(
        [
            (
                PAY,
                [
                    _pay_fail_dup(),
                    _trial(PAY, None, error="boom", evaluated=False),
                ],
            )
        ],
        strategy="a",
        source="m2.json",
    )
    trend = sc.build_series_trend(baseline, [full, errored])
    assert trend["members"][1]["eligible"] is True
    span = trend["series"]["recovery_range"]
    assert span["min"] == 0.0
    assert span["max"] == 1.0
    assert span["denominators_equal"] is False
    assert span["note"] == "unweighted spread; denominators differ"
    assert all(f is not None for f in (span["min"], span["max"], span["span"]))


def test_missing_recovery_stays_unknown():
    baseline = _doc(
        [(PAY, [_pay_fail_dup(), _pay_fail_dup()])],
        strategy="a",
        source="b.json",
    )
    ghost = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="m.json"
    )
    ghost["overall"]["recovery_success_rate"] = {
        "value": None,
        "numerator": None,
        "denominator": 0,
    }
    ghost["scopes"][0]["recovery_success_rate"] = {
        "value": None,
        "numerator": None,
        "denominator": 0,
        "definition": "x",
    }
    trend = sc.build_series_trend(baseline, [ghost])
    assert trend["members"][0]["eligible"] is True
    assert trend["series"]["members_with_unknown_recovery"] == ["m.json"]
    assert trend["series"]["recovery_range"]["n_values"] == 0


def test_recurring_violations_need_two_members():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="b.json"
    )
    first = _doc(
        [(PAY, [_pay_fail_dup(), _pay_pass()])], strategy="a", source="m1.json"
    )
    second = _doc(
        [(PAY, [_pay_fail_dup(), _pay_pass()])], strategy="a", source="m2.json"
    )
    trend = sc.build_series_trend(baseline, [first, second])
    recurring = trend["series"]["recurring_violations"]
    assert list(recurring) == ["multiple_successful_charges_for_order"]
    entry = recurring["multiple_successful_charges_for_order"]
    assert entry["members_affected"] == 2
    assert entry["total_occurrences"] == 2
    assert len(entry["member_sources"]) == 2
    assert entry["per_member"][0]["evaluable_trials"] == 2


def test_one_off_violations_are_not_recurring():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="b.json"
    )
    member = _doc(
        [(PAY, [_pay_fail_dup(), _pay_pass()])], strategy="a", source="m.json"
    )
    trend = sc.build_series_trend(baseline, [member])
    assert trend["series"]["recurring_violations"] == {}


def test_unevaluable_evidence_is_not_zero_violations():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="b.json"
    )
    errored = _trial(PAY, None, error="boom", evaluated=False)
    member = _doc(
        [(PAY, [_pay_pass(), errored])], strategy="a", source="m.json"
    )
    trend = sc.build_series_trend(baseline, [member])
    # The errored trial cannot manufacture a violation-free recurrence:
    # only the clean passed trial contributes a zero-violation record.
    assert trend["members"][0]["eligible"] is True
    assert trend["series"]["recurring_violations"] == {}


def test_member_attributed_regressions():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="b.json"
    )
    worse = _doc(
        [(PAY, [_pay_fail_dup(), _pay_pass()])], strategy="a", source="m1.json"
    )
    same = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="m2.json"
    )
    trend = sc.build_series_trend(baseline, [worse, same])
    assert trend["series"]["regressions_present"] is True
    assert trend["series"]["members_regressed_vs_baseline"] == ["m1.json"]
    assert "At least one member regressed against baseline" in trend["note"]
    assert "does not establish a series-wide direction" in trend["note"]


def test_informational_movement_does_not_regress():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])], strategy="a", source="b.json"
    )
    member = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])], strategy="a", source="m.json"
    )
    member["overall"]["invariant_violation_rate"] = dict(
        member["overall"]["invariant_violation_rate"]
    )
    trend = sc.build_series_trend(baseline, [member])
    row = trend["members"][0]["comparison_vs_baseline"]["overall"][
        "evidence_coverage"
    ]
    assert row["better_direction"] == "informational"
    assert trend["series"]["regressions_present"] is False
    assert trend["series"]["members_regressed_vs_baseline"] == []


def test_true_to_false_consistency_does_not_regress_alone():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="b.json"
    )
    member = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="m.json"
    )
    # Force a consistency-only flip: identical safety metrics, mixed classes.
    member["scopes"][0]["outcome_consistency"] = {
        "consistent": False,
        "classes": ["business_failure", "recovered"],
        "eligible_trials": 2,
        "reason": None,
        "definition": "x",
    }
    baseline["scopes"][0]["outcome_consistency"] = {
        "consistent": True,
        "classes": ["recovered"],
        "eligible_trials": 2,
        "reason": None,
        "definition": "x",
    }
    trend = sc.build_series_trend(baseline, [member])
    row = trend["members"][0]["comparison_vs_baseline"]["overall"][
        "outcome_consistency"
    ]
    assert row["direction"] == "regressed"
    assert row["transition"] == "true->false"
    assert trend["series"]["regressions_present"] is False


def test_duplicate_members_deduped_with_warning():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a", source="b.json")
    first = _doc([(PAY, [_pay_pass()])], strategy="a", source="m.json")
    trend = sc.build_series_trend(baseline, [first, copy.deepcopy(first)])
    assert trend["series"]["member_count"] == 1
    assert any("duplicate member" in w for w in trend["warnings"])


def test_deterministic_output_ordering():
    baseline = _doc([(PAY, [_pay_pass()])], strategy="a")
    first = _doc([(PAY, [_pay_pass()])], strategy="a", source="m1.json")
    second = _doc([(PAY, [_pay_fail_dup()])], strategy="a", source="m2.json")
    one = sc.build_series_trend(baseline, [first, second])
    two = sc.build_series_trend(baseline, [first, second])
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)
    assert [m["source"] for m in one["members"]] == ["m1.json", "m2.json"]


def test_does_not_redefine_existing_metrics():
    baseline = _doc(
        [(PAY, [_pay_pass(), _pay_fail_dup()])], strategy="a", source="b.json"
    )
    member = _doc(
        [(PAY, [_pay_pass(), _pay_pass()])], strategy="a", source="m.json"
    )
    trend = sc.build_series_trend(baseline, [member])
    row = trend["members"][0]["comparison_vs_baseline"]["overall"][
        "recovery_success_rate"
    ]
    assert row["baseline_fraction"] == "1/2"
    assert row["candidate_fraction"] == "2/2"
    assert row["direction"] == "improved"
