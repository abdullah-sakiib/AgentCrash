"""Offline tests for the scorecard sidecar (Milestone 11).

Covers the pure document builder, the fulfillment-scenario alias, CLI
write-safety, and proof that existing report writers are unchanged.
No live calls, no credentials, no artifact rewrites: all files land in
pytest tmp directories.
"""

import copy
import json
from pathlib import Path

import pytest

from agentcrash.experiments import run_experiment, scorecard as sc


def _trace_kinds(*kinds):
    return [
        {"sequence": i + 1, "kind": kind, "action": None}
        for i, kind in enumerate(kinds)
    ]


def _live_trial(**overrides):
    trial = {
        "scenario": "payment_timeout_after_commit",
        "mode": "live",
        "trial_index": 1,
        "trial_id": "t-1",
        "outcome": "passed",
        "error": None,
        "turns": 2,
        "successful_charges": 1,
        "payment_ids": [1],
        "order_ids": [],
        "violations": [],
        "usage": {"input_tokens": 1, "output_tokens": 1,
                  "total_tokens": 2, "responses": 1},
        "trace": _trace_kinds("model_request", "model_response",
                              "tool_call", "tool_result"),
        "evaluation_evidence": {"valid": True,
                                "source": "persisted-state-evaluation"},
    }
    trial.update(overrides)
    return trial


def _orchestrated_report(trials_by_scenario):
    scenarios = []
    for name, trials in trials_by_scenario:
        scenarios.append({
            "scenario": name,
            "mode": "live",
            "trials": trials,
            "trials_requested": len(trials),
        })
    return {
        "experiment": "agentcrash-orchestrated-run",
        "mode": "live",
        "model": "fake-model",
        "scenarios": scenarios,
    }


def test_build_document_adds_provenance_and_keeps_version():
    report = _orchestrated_report(
        [("payment_timeout_after_commit", [_live_trial()])])
    document = sc.build_scorecard_document(report, strategy_id="s",
                                           source="r.json")
    assert document["scorecard_version"] == 1
    assert document["generated_from"] == {
        "source": "r.json",
        "input_kind": "orchestrated_run",
        "scorecard_module": "agentcrash.experiments.scorecard",
    }
    assert document["scope"]["strategy_id"] == "s"
    assert document["overall"]["recovery_success_rate"]["value"] == 1.0


def test_build_document_detects_unified_input_kind():
    unified = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "kind": "payment_multi",
            "scenario": "payment_timeout_after_commit",
            "model": "m",
            "trials_requested": 1,
            "trials_completed": 1,
            "passed": 1, "failed": 0, "errors": 0,
            "turns_total": 0,
            "usage": {"status": "missing"},
            "violation_counts": {},
            "trials": [{
                "trial_index": 1, "trial_id": "t", "outcome": "passed",
                "error": None, "turns": 0, "successful_charges": 1,
                "violations": [],
            }],
            "details": {},
        }],
    }
    document = sc.build_scorecard_document(unified, source="u.json")
    assert document["generated_from"]["input_kind"] == "unified"
    scope = document["scopes"][0]
    # Lossy input stays explicitly unknown: no recovery claim is invented.
    assert scope["recovery_success_rate"]["value"] is None
    assert scope["trial_counts"]["unknown_completion"] == 1
    assert scope["usage"]["status"] == "missing"


def test_build_document_rejects_malformed_input_without_side_effects():
    before = {"scenarios": []}
    snapshot = copy.deepcopy(before)
    with pytest.raises(ValueError):
        sc.build_scorecard_document(before, source="bad.json")
    assert before == snapshot


def test_build_document_does_not_mutate_input():
    report = _orchestrated_report(
        [("payment_timeout_after_commit", [_live_trial()])])
    snapshot = copy.deepcopy(report)
    sc.build_scorecard_document(report, source="r.json")
    assert report == snapshot


def test_alias_canonicalizes_with_provenance():
    trials = [_live_trial(scenario="fulfill_without_payment")]
    report = _orchestrated_report([("fulfill_without_payment", trials)])
    document = sc.build_scorecard_document(report, source="r.json")
    scope = document["scopes"][0]
    assert scope["scenario"] == "fulfill_before_payment"
    assert scope["reported_as"] == "fulfill_without_payment"
    assert scope["fault"] == {"fault_type": "none"}
    assert scope["duplicate_charge_trial_rate"]["applicable"] is False
    assert scope["duplicate_order_trial_rate"]["applicable"] is False


def test_canonical_name_has_no_reported_as_key():
    report = _orchestrated_report(
        [("fulfill_before_payment", [_live_trial()])])
    scope = sc.build_scorecard_document(report)["scopes"][0]
    assert scope["scenario"] == "fulfill_before_payment"
    assert "reported_as" not in scope


def test_alias_report_compares_equal_to_canonical_report():
    trials = [_live_trial()]
    canonical = sc.build_scorecard_document(
        _orchestrated_report(
            [("fulfill_before_payment", [dict(t) for t in trials])]))
    aliased = sc.build_scorecard_document(
        _orchestrated_report(
            [("fulfill_without_payment", [dict(t) for t in trials])]))
    result = sc.compare_scorecards(canonical, aliased)
    assert result["compatible"] is True
    assert result["reasons"] == []


def test_alias_applies_on_normalized_path():
    unified = {
        "model": "m", "mode": "live",
        "scenarios": [{
            "kind": "fulfillment_trial",
            "scenario": "fulfill_without_payment",
            "model": "m",
            "trials_requested": 1,
            "trials_completed": 1,
            "passed": 1, "failed": 0, "errors": 0,
            "turns_total": 0,
            "usage": {"status": "missing"},
            "violation_counts": {},
            "trials": [{
                "trial_index": 1, "trial_id": "t", "outcome": "passed",
                "error": None, "turns": 0, "successful_charges": 0,
                "violations": [],
            }],
            "details": {},
        }],
    }
    scope = sc.build_scorecard_document(unified)["scopes"][0]
    assert scope["scenario"] == "fulfill_before_payment"
    assert scope["reported_as"] == "fulfill_without_payment"
    assert scope["fault"] == {"fault_type": "none"}


def _write_report(tmp_path: Path, report: dict, name: str = "report.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def test_cli_writes_default_sidecar_beside_input(tmp_path: Path) -> None:
    src = _write_report(
        tmp_path,
        _orchestrated_report(
            [("payment_timeout_after_commit", [_live_trial()])]))
    sc.main(["--input", str(src), "--strategy-id", "demo"])
    sidecar = tmp_path / "report.scorecard.json"
    assert sidecar.exists()
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    assert document["scorecard_version"] == 1
    assert document["generated_from"]["source"] == str(src)
    # Stable literal regardless of invocation style (python -m runs the
    # module as __main__, so __name__ must not be used here).
    assert document["generated_from"]["scorecard_module"] == (
        "agentcrash.experiments.scorecard"
    )
    assert document["scope"]["strategy_id"] == "demo"
    assert document["overall"]["recovery_success_rate"]["value"] == 1.0


def test_cli_respects_explicit_output(tmp_path: Path) -> None:
    src = _write_report(
        tmp_path, _orchestrated_report(
            [("payment_timeout_after_commit", [_live_trial()])]))
    out = tmp_path / "nested" / "custom.json"
    sc.main(["--input", str(src), "--output", str(out)])
    assert out.exists()
    assert not (tmp_path / "report.scorecard.json").exists()


def test_cli_refuses_to_overwrite_sidecar(tmp_path: Path) -> None:
    src = _write_report(
        tmp_path, _orchestrated_report(
            [("payment_timeout_after_commit", [_live_trial()])]))
    sc.main(["--input", str(src)])
    sidecar = tmp_path / "report.scorecard.json"
    before = sidecar.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        sc.main(["--input", str(src)])
    assert sidecar.read_text(encoding="utf-8") == before


def test_cli_refuses_to_overwrite_source_report(tmp_path: Path) -> None:
    src = _write_report(
        tmp_path, _orchestrated_report(
            [("payment_timeout_after_commit", [_live_trial()])]))
    before = src.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        sc.main(["--input", str(src), "--output", str(src)])
    assert src.read_text(encoding="utf-8") == before
    assert not (tmp_path / "report.scorecard.json").exists()


def test_cli_malformed_json_creates_no_sidecar(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        sc.main(["--input", str(bad)])
    assert not (tmp_path / "bad.scorecard.json").exists()


def test_cli_unscorable_report_creates_no_sidecar(tmp_path: Path) -> None:
    src = _write_report(tmp_path, {"scenarios": []})
    with pytest.raises(SystemExit):
        sc.main(["--input", str(src)])
    assert not (tmp_path / "report.scorecard.json").exists()


def test_cli_missing_input_creates_no_sidecar(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        sc.main(["--input", str(tmp_path / "absent.json")])
    assert not (tmp_path / "absent.scorecard.json").exists()


def test_orchestrated_export_contains_no_scorecard(tmp_path: Path) -> None:
    # The sidecar feature must not alter existing report writers: an
    # exported orchestrated report carries no scorecard section.
    trial = run_experiment.OrchestratedTrial(
        scenario="payment_timeout_after_commit",
        mode="deterministic",
        trial_index=1,
        trial_id="t-1",
        outcome="passed",
        error=None,
        turns=2,
        successful_charges=1,
        violations=(),
        usage={},
        trace=(),
        evaluation_evidence={"valid": True},
    )
    scenario = run_experiment.OrchestratedScenario(
        scenario="payment_timeout_after_commit",
        scenario_key="payment-timeout",
        mode="deterministic",
        status="complete",
        error=None,
        trials_requested=1,
        trials_completed=1,
        passed=1,
        failed=0,
        errors=0,
        pass_rate=1.0,
        turns_total=2,
        usage={},
        violation_counts={},
        trials=(trial,),
    )
    report = run_experiment.OrchestratedReport(
        experiment=run_experiment.EXPERIMENT_NAME,
        mode="deterministic",
        model=None,
        requested_trials=1,
        scenarios_requested=1,
        scenarios_completed=1,
        aggregate={},
        scenarios=(scenario,),
        cost_note=run_experiment.COST_NOTE,
    )
    out = tmp_path / "orchestrated.json"
    run_experiment.export_orchestrated_report(report, out)
    exported = json.loads(out.read_text(encoding="utf-8"))
    assert "scorecard" not in exported
    assert "scorecard" not in json.dumps(exported)
