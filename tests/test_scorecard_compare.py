"""Offline tests for scorecard comparison (Milestone 12).

Pure-function tests plus CLI write-safety tests. No live calls, no
credentials, no artifact rewrites: all files land in pytest tmp dirs.
"""

import copy
import json
from pathlib import Path

import pytest

from agentcrash.experiments import scorecard as sc


def _trace(n_calls):
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


def _trial(
    scenario,
    outcome="passed",
    *,
    error=None,
    calls=1,
    charges=1,
    violations=(),
    usage="complete",
    evaluated=True,
    order_ids=(),
):
    if usage == "complete":
        usage_map = {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "responses": 1,
        }
    elif usage == "partial":
        usage_map = {"input_tokens": 10, "responses": 1}
    elif usage == "bool":
        usage_map = {
            "input_tokens": True,
            "output_tokens": True,
            "total_tokens": True,
            "responses": True,
        }
    else:
        usage_map = {}
    return {
        "scenario": scenario,
        "mode": "live",
        "trial_index": 1,
        "trial_id": "t-1",
        "outcome": outcome,
        "error": error,
        "turns": 2,
        "successful_charges": charges,
        "payment_ids": [1] if charges else [],
        "order_ids": list(order_ids),
        "violations": [{"code": code} for code in violations],
        "usage": usage_map,
        "trace": _trace(calls),
        "evaluation_evidence": (
            {"valid": outcome == "passed", "source": "persisted-state-evaluation"}
            if evaluated and error is None
            else {}
        ),
    }


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
FULFILL = "fulfill_before_payment"


def _payment_pass(**kw):
    return _trial(PAY, "passed", **kw)


def _payment_fail_dup(**kw):
    kw.setdefault("charges", 2)
    kw.setdefault("violations", ("multiple_successful_charges_for_order",))
    return _trial(PAY, "failed", **kw)


def _intent_pass(order_ids=(1,), **kw):
    return _trial(INTENT, "passed", order_ids=order_ids, **kw)


def _intent_fail_dup(**kw):
    return _trial(
        INTENT,
        "failed",
        order_ids=(1, 2),
        violations=("duplicate_orders_for_intent",),
        **kw,
    )


def test_identical_scorecards_compare_unchanged_and_deterministic():
    doc = _doc([(PAY, [_payment_pass(), _payment_pass()])])
    first = sc.compare_scorecard_documents(doc, copy.deepcopy(doc))
    assert first["compatible"] is True
    assert first["reasons"] == []
    assert first["regressions_present"] is False
    for key, row in first["overall"].items():
        if key == "duplicate_order_trial_rate":
            assert row["direction"] == "unavailable"
            assert row["applicable"] is False
            continue
        if key == "outcome_consistency":
            # Categorical rows carry no numeric delta by design.
            assert row["direction"] == "unchanged"
            assert row["delta"] is None
            assert row["transition"] == "true->true"
            continue
        if key == "recovery_efficiency":
            # Overall efficiency is never pooled; stratify by scenario.
            assert row["direction"] == "unavailable"
            assert row["delta"] is None
            assert row["reason"] == "stratify by scenario"
            continue
        assert row["direction"] == "unchanged", key
        assert row["delta"] == 0
    assert "winner" not in first and "ranking" not in first
    assert json.dumps(first, sort_keys=True) == json.dumps(
        sc.compare_scorecard_documents(doc, copy.deepcopy(doc)), sort_keys=True
    )


def test_improved_candidate_marks_safety_metrics_improved():
    baseline = _doc([(PAY, [_payment_fail_dup(), _payment_pass()])])
    candidate = _doc([(PAY, [_payment_pass(), _payment_pass()])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    assert result["compatible"] is True
    assert result["overall"]["recovery_success_rate"]["direction"] == "improved"
    assert result["overall"]["invariant_violation_rate"]["direction"] == "improved"
    assert (
        result["overall"]["duplicate_charge_trial_rate"]["direction"]
        == "improved"
    )
    assert result["regressions_present"] is False
    row = result["overall"]["recovery_success_rate"]
    assert row["baseline_fraction"] == "1/2"
    assert row["candidate_fraction"] == "2/2"
    assert row["better_direction"] == "higher"
    assert "definition" in row and row["definition"]


def test_regressed_candidate_sets_regressions_present():
    baseline = _doc([(PAY, [_payment_pass(), _payment_pass()])])
    candidate = _doc([(PAY, [_payment_fail_dup(), _payment_pass()])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    assert result["overall"]["recovery_success_rate"]["direction"] == "regressed"
    assert result["regressions_present"] is True


def test_overall_improvement_cannot_hide_scenario_regression():
    baseline = _doc(
        [
            (PAY, [_payment_fail_dup(), _payment_fail_dup()]),
            (INTENT, [_intent_pass(), _intent_pass()]),
        ]
    )
    candidate = _doc(
        [
            (PAY, [_payment_pass(), _payment_pass()]),
            (INTENT, [_intent_fail_dup(), _intent_fail_dup()]),
        ]
    )
    result = sc.compare_scorecard_documents(baseline, candidate)
    assert result["compatible"] is True
    by_scenario = result["scenarios"]
    assert by_scenario[PAY]["metrics"]["recovery_success_rate"][
        "direction"
    ] == "improved"
    assert by_scenario[INTENT]["metrics"]["recovery_success_rate"][
        "direction"
    ] == "regressed"
    assert result["regressions_present"] is True
    text = sc.format_comparison_text(result)
    assert "regressed" in text
    assert "Regressions present: yes" in text


def test_each_compatibility_gate_blocks_without_ranking():
    def gate_cases():
        base = _doc([(PAY, [_payment_pass()])])
        other_scenario = _doc([(INTENT, [_payment_pass()])])
        other_fault = sc.build_scorecard_document(
            _report([(PAY, [_payment_pass()])]),
            strategy_id="s",
            source="x",
        )
        other_fault["scopes"][0]["fault"] = {"fault_type": "other"}
        unknown_fault = copy.deepcopy(base)
        unknown_fault["scopes"][0]["fault"] = None
        more_completed = _doc([(PAY, [_payment_pass(), _payment_pass()])])
        requested_mismatch = copy.deepcopy(base)
        requested_mismatch["scopes"][0]["trial_counts"]["requested"] = 9
        requested_mismatch["overall"]["trials_requested"] = 9
        unknown_requested = copy.deepcopy(base)
        unknown_requested["scopes"][0]["trial_counts"]["requested"] = None
        return [
            other_scenario,
            other_fault,
            unknown_fault,
            more_completed,
            requested_mismatch,
            unknown_requested,
        ]

    base = _doc([(PAY, [_payment_pass()])])
    for other in gate_cases():
        result = sc.compare_scorecard_documents(base, other)
        assert result["compatible"] is False
        assert result["reasons"]
        assert result["overall"] == {}
        assert result["scenarios"] == {}
        assert result["regressions_present"] is False
        assert "winner" not in result and "ranking" not in result


def test_alias_and_canonical_compare_compatibly():
    trials = [_trial(FULFILL, "passed")]
    canonical = _doc([(FULFILL, [dict(t) for t in trials])])
    aliased = _doc([("fulfill_without_payment", [dict(t) for t in trials])])
    assert aliased["scopes"][0]["scenario"] == FULFILL
    assert aliased["scopes"][0]["reported_as"] == "fulfill_without_payment"
    result = sc.compare_scorecard_documents(canonical, aliased)
    assert result["compatible"] is True
    assert result["regressions_present"] is False


def test_non_applicable_duplicates_stay_unavailable():
    doc = _doc([(FULFILL, [_trial(FULFILL, "passed")])])
    result = sc.compare_scorecard_documents(doc, copy.deepcopy(doc))
    for key in ("duplicate_charge_trial_rate", "duplicate_order_trial_rate"):
        row = result["overall"][key]
        assert row["direction"] == "unavailable"
        assert row["delta"] is None
        assert row["applicable"] is False
        assert row["baseline"] is None and row["candidate"] is None


def test_zero_denominator_metrics_stay_unavailable():
    def empty_payment():
        return _report([(PAY, [])])

    baseline = sc.build_scorecard_document(empty_payment(), source="b")
    candidate = sc.build_scorecard_document(empty_payment(), source="c")
    result = sc.compare_scorecard_documents(baseline, candidate)
    assert result["compatible"] is True
    row = result["overall"]["recovery_success_rate"]
    assert row["direction"] == "unavailable"
    assert row["baseline_fraction"] == "0/0"
    assert result["regressions_present"] is False


def test_error_rate_change_does_not_leak_into_violations():
    baseline = _doc([(PAY, [_payment_pass(), _payment_pass()])])
    errored = _trial(PAY, None, error="boom", evaluated=False)
    candidate = _doc([(PAY, [_payment_pass(), errored])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    assert result["compatible"] is True
    assert result["overall"]["infrastructure_error_rate"]["direction"] == (
        "regressed"
    )
    assert result["overall"]["invariant_violation_rate"]["direction"] == (
        "unchanged"
    )
    assert result["overall"]["invariant_violation_rate"]["delta"] == 0
    assert result["regressions_present"] is True  # error is a safety metric


def test_violation_codes_appear_disappear_and_change():
    baseline = _doc(
        [
            (
                PAY,
                [
                    _payment_fail_dup(
                        violations=("multiple_successful_charges_for_order",)
                    ),
                    _trial(
                        PAY,
                        "failed",
                        violations=("duplicate_successful_charge",),
                    ),
                ],
            )
        ]
    )
    candidate = _doc(
        [
            (
                PAY,
                [
                    _trial(
                        PAY,
                        "failed",
                        charges=3,
                        violations=("multiple_successful_charges_for_order",),
                    ),
                    _payment_pass(),
                ],
            )
        ]
    )
    result = sc.compare_scorecard_documents(baseline, candidate)
    diffs = result["scenarios"][PAY]["violations_by_code"]
    assert diffs["duplicate_successful_charge"]["direction"] == "improved"
    assert diffs["multiple_successful_charges_for_order"]["direction"] == (
        "unchanged"
    )
    assert result["violations_by_code"]["duplicate_successful_charge"][
        "delta"
    ] == -1


def test_usage_complete_pair_compares_with_deltas():
    baseline = _doc([(PAY, [_payment_pass()])])
    richer = _trial(PAY, "passed")
    richer["usage"] = {
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
        "responses": 2,
    }
    candidate = _doc([(PAY, [richer])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    usage = result["usage"]
    assert usage["comparable"] is True
    assert usage["baseline_status"] == "complete"
    assert usage["input_tokens"]["delta"] == 10
    assert usage["responses"]["delta"] == 1


def test_partial_or_missing_usage_has_no_deltas():
    baseline = _doc([(PAY, [_payment_pass()])])
    partial = _trial(PAY, "passed", usage="partial")
    candidate = _doc([(PAY, [partial])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    assert result["usage"]["comparable"] is False
    assert result["usage"]["input_tokens"]["delta"] is None
    assert result["usage"]["candidate_status"] == "partial"


def test_boolean_tainted_usage_is_not_comparable():
    baseline = _doc([(PAY, [_payment_pass()])])
    tainted = _trial(PAY, "passed", usage="bool")
    candidate = _doc([(PAY, [tainted])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    assert result["usage"]["comparable"] is False
    assert result["usage"]["candidate_status"] == "missing"


def test_cost_remains_nonnumeric():
    doc = _doc([(PAY, [_payment_pass()])])
    result = sc.compare_scorecard_documents(doc, copy.deepcopy(doc))
    assert result["cost"] == sc.COST_NOTE
    assert isinstance(result["cost"], str)


def test_tool_call_increases_are_informational_not_regressions():
    baseline = _doc([(PAY, [_payment_pass(calls=1)])])
    candidate = _doc([(PAY, [_payment_pass(calls=5)])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    row = result["overall"]["tool_calls_mean_per_evaluable_trial"]
    assert row["direction"] == "informational-increase"
    assert row["better_direction"] == "informational"
    assert result["regressions_present"] is False


def test_sample_size_warning_boundaries():
    small = _doc([(PAY, [_payment_pass(), _payment_pass()])])
    result = sc.compare_scorecard_documents(small, copy.deepcopy(small))
    assert result["sample_size_warning"] is True
    big = _doc([(PAY, [_payment_pass() for _ in range(5)])])
    result = sc.compare_scorecard_documents(big, copy.deepcopy(big))
    assert result["sample_size_warning"] is False


def test_terminal_summary_shows_regressions_without_winner():
    baseline = _doc([(PAY, [_payment_pass(), _payment_pass()])])
    candidate = _doc([(PAY, [_payment_fail_dup(), _payment_pass()])])
    result = sc.compare_scorecard_documents(baseline, candidate)
    text = sc.format_comparison_text(result)
    assert "compatible" in text
    assert "regressed" in text
    assert "Sample-size warning" in text
    assert "winner" not in text.lower() and "ranking" not in text.lower()
    incompatible = sc.compare_scorecard_documents(
        baseline, _doc([(INTENT, [_intent_pass()])])
    )
    text = sc.format_comparison_text(incompatible)
    assert "INCOMPATIBLE" in text


def _write_sidecar(tmp_path: Path, entries, name: str) -> Path:
    doc = _doc(entries, source=name)
    path = tmp_path / name
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def test_cli_compare_writes_default_path_beside_candidate(tmp_path: Path):
    baseline = _write_sidecar(
        tmp_path, [(PAY, [_payment_pass()])], "baseline.scorecard.json"
    )
    candidate = _write_sidecar(
        tmp_path, [(PAY, [_payment_pass()])], "candidate.scorecard.json"
    )
    sc.main(
        ["compare", "--baseline", str(baseline), "--candidate", str(candidate)]
    )
    expected = tmp_path / (
        "candidate.scorecard.vs.baseline.scorecard.comparison.json"
    )
    assert expected.exists()
    document = json.loads(expected.read_text(encoding="utf-8"))
    assert document["scorecard_comparison_version"] == 1
    assert document["compatible"] is True


def test_cli_compare_refuses_overwrite_and_input_collision(tmp_path: Path):
    baseline = _write_sidecar(
        tmp_path, [(PAY, [_payment_pass()])], "b.scorecard.json"
    )
    candidate = _write_sidecar(
        tmp_path, [(PAY, [_payment_pass()])], "c.scorecard.json"
    )
    sc.main(
        ["compare", "--baseline", str(baseline), "--candidate", str(candidate)]
    )
    with pytest.raises(SystemExit):
        sc.main(
            [
                "compare",
                "--baseline",
                str(baseline),
                "--candidate",
                str(candidate),
            ]
        )
    with pytest.raises(SystemExit):
        sc.main(
            [
                "compare",
                "--baseline",
                str(baseline),
                "--candidate",
                str(candidate),
                "--output",
                str(baseline),
            ]
        )
    with pytest.raises(SystemExit):
        sc.main(
            [
                "compare",
                "--baseline",
                str(baseline),
                "--candidate",
                str(candidate),
                "--output",
                str(candidate),
            ]
        )


def test_cli_compare_malformed_input_leaves_no_file(tmp_path: Path):
    candidate = _write_sidecar(
        tmp_path, [(PAY, [_payment_pass()])], "c.scorecard.json"
    )
    bad = tmp_path / "bad.scorecard.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        sc.main(
            ["compare", "--baseline", str(bad), "--candidate", str(candidate)]
        )
    assert not (tmp_path / "bad.vs.c.scorecard.comparison.json").exists()
    assert not (tmp_path / "c.scorecard.vs.bad.comparison.json").exists()


def test_cli_help_paths_preserved(capsys):
    with pytest.raises(SystemExit) as legacy:
        sc.main(["--help"])
    assert legacy.value.code == 0
    with pytest.raises(SystemExit) as compare:
        sc.main(["compare", "--help"])
    assert compare.value.code == 0


def test_legacy_sidecar_command_unchanged(tmp_path: Path):
    report = {
        "experiment": "agentcrash-orchestrated-run",
        "mode": "live",
        "model": "m",
        "scenarios": [
            {
                "scenario": PAY,
                "mode": "live",
                "trials": [_payment_pass()],
                "trials_requested": 1,
            }
        ],
    }
    src = tmp_path / "report.json"
    src.write_text(json.dumps(report), encoding="utf-8")
    sc.main(["--input", str(src)])
    sidecar = json.loads(
        (tmp_path / "report.scorecard.json").read_text(encoding="utf-8")
    )
    assert sidecar["scorecard_version"] == 1
    assert "scorecard_comparison_version" not in sidecar
    assert "compatible" not in sidecar
