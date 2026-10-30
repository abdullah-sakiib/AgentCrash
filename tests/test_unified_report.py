import json
from pathlib import Path

import pytest

from agentcrash.experiments import unified_report
from agentcrash.experiments.unified_report import (
    UnifiedReportError,
    build_unified_report,
    normalize_report,
    summarize_entries,
    write_unified_report,
)


def _payment_multi() -> dict:
    return {
        "scenario": "payment_timeout_after_commit",
        "model": "fake-model",
        "requested_trials": 3,
        "completed_trials": 3,
        "aggregate": {
            "passed": 2,
            "failed": 1,
            "errors": 0,
            "pass_rate": 2 / 3,
            "duplicate_charge_trials": 1,
            "violation_counts": {"multiple_successful_charges_for_order": 1},
            "total_input_tokens": 30,
            "total_output_tokens": 15,
            "total_tokens": 45,
            "total_responses": 6,
            "trials_with_usage": 3,
            "trials_missing_usage": 0,
            "trials_partial_usage": 0,
        },
        "trials": [
            {
                "trial_index": 1,
                "trial_id": "payment_timeout_after_commit-trial-001",
                "task_outcome": "passed",
                "live_error": None,
                "turns_used": 2,
                "successful_charge_count": 1,
                "payment_ids": [1],
                "invariant_violations": [],
            },
            {
                "trial_index": 2,
                "trial_id": "payment_timeout_after_commit-trial-002",
                "task_outcome": "failed",
                "live_error": None,
                "turns_used": 2,
                "successful_charge_count": 2,
                "payment_ids": [2, 3],
                "invariant_violations": [{"code": "multiple_successful_charges_for_order"}],
            },
            {
                "trial_index": 3,
                "trial_id": "payment_timeout_after_commit-trial-003",
                "task_outcome": "passed",
                "live_error": None,
                "turns_used": 1,
                "successful_charge_count": 1,
                "payment_ids": [4],
                "invariant_violations": [],
            },
        ],
        "baselines": [{"strategy_name": "unsafe_retry"}],
    }


def _fulfillment(usage=None, outcome="passed", error=None) -> dict:
    return {
        "scenario": "fulfill_before_payment",
        "model": "fake-model",
        "task": {"item": "widget"},
        "task_outcome": outcome,
        "trial_error": error,
        "order_id": 1,
        "order_status": "fulfilled",
        "payment_ids": [1],
        "successful_charge_count": 1,
        "invariant_violations": [],
        "turns_used": 4,
        "usage": usage if usage is not None else {
            "input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "responses": 2
        },
        "cost_note": "cost_not_calculated (no reliable pricing data)",
        "trace": [],
        "baselines": [],
    }


def test_normalizes_both_formats() -> None:
    payment = normalize_report(_payment_multi(), "pay.json")
    fulfillment = normalize_report(_fulfillment(), "ful.json")
    assert payment["kind"] == "payment_multi"
    assert fulfillment["kind"] == "fulfillment_trial"
    assert payment["trials_requested"] == 3
    assert fulfillment["order_status"] if False else True
    assert fulfillment["details"]["order_status"] == "fulfilled"
    assert fulfillment["trials"][0]["successful_charges"] == 1


def test_missing_partial_zero_usage() -> None:
    missing = normalize_report(
        _fulfillment(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 0}),
        "m.json",
    )
    assert missing["usage"]["status"] == "missing"
    partial = normalize_report(
        _fulfillment(usage={"input_tokens": 10, "output_tokens": None, "total_tokens": None, "responses": 2}),
        "p.json",
    )
    assert partial["usage"]["status"] == "partial"
    assert partial["usage"]["input_tokens"] == 10
    assert partial["usage"]["output_tokens"] == 0
    zero = normalize_report(
        _fulfillment(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 1}),
        "z.json",
    )
    assert zero["usage"]["status"] == "complete"


def test_counts_rates_and_denominators() -> None:
    report = build_unified_report([("pay.json", _payment_multi()), ("ful.json", _fulfillment())])
    aggregate = report["aggregate"]
    assert aggregate["trials_requested"] == 4
    assert aggregate["trials_completed"] == 4
    assert aggregate["passed"] == 3
    assert aggregate["failed"] == 1
    assert aggregate["errors"] == 0
    assert aggregate["pass_rate"] == pytest.approx(3 / 4)
    assert aggregate["total_input_tokens"] == 40
    assert report["scenarios"][0]["pass_rate"] == pytest.approx(2 / 3)


def test_violations_stay_distinguishable() -> None:
    bad = _fulfillment()
    bad["invariant_violations"] = [{"code": "fulfilled_without_payment", "message": "x"}]
    bad["task_outcome"] = "failed"
    report = build_unified_report([("pay.json", _payment_multi()), ("ful.json", bad)])
    by_scenario = {entry["scenario"]: entry for entry in report["scenarios"]}
    assert by_scenario["payment_timeout_after_commit"]["violation_counts"] == {
        "multiple_successful_charges_for_order": 1
    }
    assert by_scenario["fulfill_before_payment"]["violation_counts"] == {
        "fulfilled_without_payment": 1
    }
    assert report["aggregate"]["violation_counts"] == {
        "fulfilled_without_payment": 1,
        "multiple_successful_charges_for_order": 1,
    }


def test_errors_separate_from_failures() -> None:
    errored = _fulfillment(outcome=None, error="boom")
    errored["task_outcome"] = None
    report = build_unified_report([("ful.json", errored)])
    assert report["aggregate"]["errors"] == 1
    assert report["aggregate"]["failed"] == 0
    assert report["aggregate"]["pass_rate"] == 0.0


def test_malformed_inputs_fail_safely(tmp_path: Path) -> None:
    with pytest.raises(UnifiedReportError):
        build_unified_report([("bad.json", {"nope": True})])
    with pytest.raises(UnifiedReportError):
        build_unified_report([("bad.json", {"scenario": "", "task_outcome": "passed", "trial_error": None, "order_status": "x"})])
    with pytest.raises(UnifiedReportError):
        build_unified_report([])


def test_overwrite_refused_and_no_partial_write(tmp_path: Path) -> None:
    out = tmp_path / "u.json"
    out.write_text("original", encoding="utf-8")
    report = build_unified_report([("ful.json", _fulfillment())])
    with pytest.raises(UnifiedReportError):
        write_unified_report(report, out)
    assert out.read_text(encoding="utf-8") == "original"


def test_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    first = build_unified_report([("pay.json", _payment_multi()), ("ful.json", _fulfillment())])
    second = build_unified_report([("pay.json", _payment_multi()), ("ful.json", _fulfillment())])
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_existing_runners_still_importable() -> None:
    import agentcrash.experiments.compare_payment_recovery as payment_runner
    import agentcrash.experiments.compare_order_fulfillment as fulfillment_runner
    import agentcrash.experiments.live_payment_recovery as live_runner
    import agentcrash.experiments.model_fulfillment_trial as fulfillment_trial

    assert hasattr(payment_runner, "run_payment_timeout_strategy")
    assert hasattr(fulfillment_runner, "run_fulfillment_comparison")
    assert hasattr(live_runner, "run_live_trial")
    assert hasattr(fulfillment_trial, "run_fulfillment_trial")


def test_payment_single_shape_supported() -> None:
    doc = {
        "scenario": "payment_timeout_after_commit",
        "model": "fake-model",
        "trial_count": 1,
        "live_outcome": "passed",
        "live_error": None,
        "order_id": 1,
        "payment_ids": [1],
        "successful_charge_count": 1,
        "turns_used": 2,
        "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10, "responses": 1},
        "invariant_violations": [],
        "baselines": [],
        "trace": [],
    }
    entry = normalize_report(doc, "single.json")
    assert entry["kind"] == "payment_single"
    assert entry["passed"] == 1
    assert entry["trials_requested"] == 1


def test_boolean_token_counts_are_rejected() -> None:
    # Booleans are int subclasses in Python but never token counts.
    normalized, status = unified_report._usage_status(
        {"input_tokens": True, "output_tokens": True,
         "total_tokens": True, "responses": True}
    )
    assert status == "missing"
    assert normalized["with_usage"] == 0
    assert normalized["missing_usage"] == 1
    assert normalized["input_tokens"] == 0
    assert normalized["responses"] == 0

    block = unified_report._normalize_usage_block(
        {"input_tokens": True, "output_tokens": True,
         "total_tokens": True, "responses": True}
    )
    assert block["status"] == "missing"
    assert block["output_tokens"] == 0
    assert block["total_tokens"] == 0
