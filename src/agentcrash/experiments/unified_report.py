"""Offline unified multi-scenario report (Milestone 5.3).

Reads existing JSON reports and normalizes them into one consistent
document. Never calls the model API and never touches simulator databases.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

COST_NOTE = "cost_not_calculated (no reliable pricing data)"


class UnifiedReportError(ValueError):
    """Malformed input report or unsafe output path."""


def _require_mapping(value: Any, source: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UnifiedReportError(f"{source}: top-level JSON must be an object.")
    return value


def _detect_kind(doc: dict[str, Any], source: str) -> str:
    if doc.get("experiment") == "agentcrash-orchestrated-run" and isinstance(doc.get("scenarios"), (list, tuple)):
        return "orchestrated_run"
    if "aggregate" in doc and "trials" in doc and "requested_trials" in doc:
        return "payment_multi"
    if "trial_count" in doc and "live_outcome" in doc:
        return "payment_single"
    if "task_outcome" in doc and "trial_error" in doc and "order_status" in doc:
        return "fulfillment_trial"
    if "task_outcome" in doc and "trial_error" in doc and "order_ids" in doc:
        return "intent_trial"
    if isinstance(doc.get("results"), (list, tuple)) and "scenario" in doc:
        return "deterministic_comparison"
    raise UnifiedReportError(f"{source}: unrecognized report format.")


def _usage_status(usage: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Normalize one usage block; None fields are missing, 0 is confirmed zero.

    Booleans are never token counts (``True`` is an ``int`` subclass in
    Python), so they are rejected the same way as non-integer values.
    """
    fields = ["input_tokens", "output_tokens", "total_tokens"]
    values = {key: usage.get(key) for key in fields}
    responses = usage.get("responses")
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
    partial = 1 if (responses_int > 0 and any(value is None for value in values.values()) and any(present)) else 0
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
        "output_tokens": 0,  # filled by caller for multi; placeholder replaced below
        "total_tokens": 0,
        "responses": responses_int,
        "with_usage": with_usage,
        "missing_usage": missing,
        "partial_usage": partial,
        "status": status,
    }
    return normalized, status


def _normalize_usage_block(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        usage = {}
    normalized, _ = _usage_status(usage)
    normalized["output_tokens"] = sum(
        value
        for value in [usage.get("output_tokens")]
        if isinstance(value, int) and not isinstance(value, bool)
    )
    normalized["total_tokens"] = sum(
        value
        for value in [usage.get("total_tokens")]
        if isinstance(value, int) and not isinstance(value, bool)
    )
    return normalized


def normalize_report(doc: dict[str, Any], source: str) -> dict[str, Any]:
    """Normalize one scenario report; preserves scenario-specific details."""
    doc = _require_mapping(doc, source)
    kind = _detect_kind(doc, source)
    if kind == "orchestrated_run":
        return _normalize_orchestrated(doc, source)
    scenario = doc.get("scenario")
    model = doc.get("model", "unknown")
    if not isinstance(scenario, str) or not scenario:
        raise UnifiedReportError(f"{source}: missing 'scenario'.")

    if kind == "deterministic_comparison":
        # Offline deterministic baseline comparisons (no model, no token usage).
        # Each strategy result counts as one trial; payment-specific metrics
        # such as duplicate-charge counts are never inherited here.
        results = doc.get("results")
        if not isinstance(results, (list, tuple)) or not results:
            raise UnifiedReportError(f"{source}: bad deterministic comparison shape.")
        trial_rows = []
        for index, result in enumerate(results, start=1):
            if not isinstance(result, dict):
                raise UnifiedReportError(f"{source}: bad comparison entry.")
            outcome = result.get("task_outcome")
            raw_violations = result.get("invariant_violations", [])
            codes = [
                entry.get("code") if isinstance(entry, dict) else entry
                for entry in raw_violations
                if isinstance(entry, (dict, str))
            ]
            trial_rows.append(
                {
                    "trial_index": index,
                    "trial_id": result.get("strategy_name"),
                    "outcome": outcome,
                    "error": None,
                    "turns": 0,
                    "successful_charges": result.get("successful_charge_count", 0),
                    "violations": [code for code in codes if isinstance(code, str)],
                }
            )
        passed = sum(1 for row in trial_rows if row["outcome"] == "passed")
        failed = sum(1 for row in trial_rows if row["outcome"] == "failed")
        violation_counts: dict[str, int] = {}
        for row in trial_rows:
            for code in set(row["violations"]):
                violation_counts[code] = violation_counts.get(code, 0) + 1
        completed = len(trial_rows)
        return {
            "source": source,
            "kind": kind,
            "scenario": scenario,
            "model": "deterministic-baseline",
            "trials_requested": completed,
            "trials_completed": completed,
            "passed": passed,
            "failed": failed,
            "errors": 0,
            "pass_rate": (passed / completed) if completed else None,
            "turns_total": 0,
            "successful_charges_total": sum(int(row["successful_charges"] or 0) for row in trial_rows),
            "duplicate_charge_trials": None,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "responses": 0,
                "with_usage": 0,
                "missing_usage": completed,
                "partial_usage": 0,
                "status": "missing",
            },
            "violation_counts": dict(sorted(violation_counts.items())),
            "baselines": [],
            "trials": trial_rows,
            "details": {},
        }

    if kind == "payment_multi":
        aggregate = doc.get("aggregate")
        trials = doc.get("trials")
        if not isinstance(aggregate, dict) or not isinstance(trials, list):
            raise UnifiedReportError(f"{source}: bad payment multi-trial shape.")
        requested = doc.get("requested_trials")
        completed = doc.get("completed_trials")
        trial_rows = []
        for trial in trials:
            if not isinstance(trial, dict):
                raise UnifiedReportError(f"{source}: bad trial entry.")
            trial_rows.append(
                {
                    "trial_index": trial.get("trial_index"),
                    "trial_id": trial.get("trial_id"),
                    "outcome": trial.get("task_outcome"),
                    "error": trial.get("live_error"),
                    "turns": trial.get("turns_used", 0),
                    "successful_charges": trial.get("successful_charge_count", 0),
                    "violations": [
                        entry.get("code") if isinstance(entry, dict) else entry
                        for entry in trial.get("invariant_violations", [])
                    ],
                }
            )
        usage = {
            "input_tokens": aggregate.get("total_input_tokens", 0),
            "output_tokens": aggregate.get("total_output_tokens", 0),
            "total_tokens": aggregate.get("total_tokens", 0),
            "responses": aggregate.get("total_responses", 0),
            "with_usage": aggregate.get("trials_with_usage", 0),
            "missing_usage": aggregate.get("trials_missing_usage", 0),
            "partial_usage": aggregate.get("trials_partial_usage", 0),
            "status": (
                "missing"
                if aggregate.get("trials_with_usage", 0) == 0
                else ("partial" if aggregate.get("trials_partial_usage", 0) else "complete")
            ),
        }
        violation_counts = dict(aggregate.get("violation_counts", {}))
        return {
            "source": source,
            "kind": kind,
            "scenario": scenario,
            "model": model,
            "trials_requested": requested,
            "trials_completed": completed,
            "passed": aggregate.get("passed", 0),
            "failed": aggregate.get("failed", 0),
            "errors": aggregate.get("errors", 0),
            "pass_rate": aggregate.get("pass_rate"),
            "turns_total": sum(int(row["turns"] or 0) for row in trial_rows),
            "successful_charges_total": sum(int(row["successful_charges"] or 0) for row in trial_rows),
            "duplicate_charge_trials": aggregate.get("duplicate_charge_trials", 0),
            "usage": usage,
            "violation_counts": violation_counts,
            "baselines": doc.get("baselines", []),
            "trials": trial_rows,
            "details": {"per_trial_payment_ids": [
                trial.get("payment_ids", []) if isinstance(trial, dict) else []
                for trial in trials
            ]},
        }

    # Single-trial shapes: payment_single (LiveTrialReport), fulfillment_trial,
    # and intent_trial (IntentTrialReport).
    outcome_key = "live_outcome" if kind == "payment_single" else "task_outcome"
    error_key = "live_error" if kind == "payment_single" else "trial_error"
    outcome = doc.get(outcome_key)
    error = doc.get(error_key)
    if error is not None:
        passed, failed, errors = 0, 0, 1
    elif outcome == "passed":
        passed, failed, errors = 1, 0, 0
    else:
        passed, failed, errors = 0, 1, 0
    usage = _normalize_usage_block(doc.get("usage"))
    violations = doc.get("invariant_violations", [])
    codes: dict[str, int] = {}
    for entry in violations if isinstance(violations, list) else []:
        code = entry.get("code") if isinstance(entry, dict) else entry
        if isinstance(code, str):
            codes[code] = codes.get(code, 0) + 1
    details: dict[str, Any] = {}
    if kind == "fulfillment_trial":
        details["order_status"] = doc.get("order_status")
    if kind == "payment_single":
        details["trial_count"] = doc.get("trial_count", 1)
    if kind == "intent_trial":
        details["order_ids"] = doc.get("order_ids", [])
        details["inventory_on_hand"] = doc.get("inventory_on_hand")
    trial_rows = [
        {
            "trial_index": 1,
            "trial_id": None,
            "outcome": outcome,
            "error": error,
            "turns": doc.get("turns_used", 0),
            "successful_charges": doc.get("successful_charge_count", 0),
            "violations": list(codes),
        }
    ]
    completed = 1
    pass_rate: float | None = 1.0 if passed else 0.0
    return {
        "source": source,
        "kind": kind,
        "scenario": scenario,
        "model": model,
        "trials_requested": 1,
        "trials_completed": completed,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": pass_rate,
        "turns_total": int(doc.get("turns_used") or 0),
        "successful_charges_total": int(doc.get("successful_charge_count") or 0),
        "duplicate_charge_trials": None,
        "usage": usage,
        "violation_counts": codes,
        "baselines": doc.get("baselines", []),
        "trials": trial_rows,
        "details": details,
    }


def _normalize_orchestrated(doc: dict[str, Any], source: str) -> dict[str, Any]:
    """Normalize an orchestrated multi-scenario run as one summary entry.

    The nested scenario breakdown is preserved verbatim in details so no
    per-scenario evidence is lost; invariants stay grouped per scenario in
    the aggregate via summarize_entries.
    """
    scenarios = doc.get("scenarios")
    if not isinstance(scenarios, (list, tuple)) or not scenarios:
        raise UnifiedReportError(f"{source}: bad orchestrated run shape.")
    aggregate = doc.get("aggregate")
    if not isinstance(aggregate, dict):
        raise UnifiedReportError(f"{source}: bad orchestrated aggregate shape.")
    nested: list[dict[str, Any]] = []
    for entry in scenarios:
        if not isinstance(entry, dict):
            raise UnifiedReportError(f"{source}: bad orchestrated scenario entry.")
        entry_trials = entry.get("trials", [])
        if not isinstance(entry_trials, (list, tuple)):
            entry_trials = []
        trials_with_trace = sum(
            1 for trial in entry_trials
            if isinstance(trial, dict) and trial.get("trace")
        )
        trials_with_evidence = sum(
            1 for trial in entry_trials
            if isinstance(trial, dict) and trial.get("evaluation_evidence")
        )
        nested.append({
            "scenario": entry.get("scenario", "unknown"),
            "mode": entry.get("mode", "unknown"),
            "status": entry.get("status", "unknown"),
            "trials_requested": entry.get("trials_requested", 0),
            "trials_completed": entry.get("trials_completed", 0),
            "passed": entry.get("passed", 0),
            "failed": entry.get("failed", 0),
            "errors": entry.get("errors", 0),
            "violation_counts": entry.get("violation_counts", {}),
            "trials_with_trace": trials_with_trace,
            "trials_with_evaluation_evidence": trials_with_evidence,
        })
    return {
        "source": source,
        "kind": "orchestrated_run",
        "scenario": f"orchestrated-run({len(nested)} scenarios)",
        "model": doc.get("model") or "unknown",
        "trials_requested": aggregate.get("trials_requested", 0),
        "trials_completed": aggregate.get("trials_completed", 0),
        "passed": aggregate.get("passed", 0),
        "failed": aggregate.get("failed", 0),
        "errors": aggregate.get("errors", 0),
        "pass_rate": aggregate.get("pass_rate"),
        "turns_total": aggregate.get("turns_total", 0),
        "successful_charges_total": 0,
        "duplicate_charge_trials": None,
        "usage": {
            "input_tokens": aggregate.get("total_input_tokens", 0),
            "output_tokens": aggregate.get("total_output_tokens", 0),
            "total_tokens": aggregate.get("total_tokens", 0),
            "responses": aggregate.get("total_responses", 0),
            "with_usage": 0,
            "missing_usage": 0,
            "partial_usage": 0,
            "status": "complete" if aggregate.get("trials_completed", 0) else "missing",
        },
        "violation_counts": dict(aggregate.get("violation_counts", {})),
        "baselines": [],
        "trials": nested,
        "details": {"mode": doc.get("mode", "unknown")},
    }


def summarize_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate across scenarios. Denominator: total completed trials."""
    requested = sum(int(entry["trials_requested"] or 0) for entry in entries)
    completed = sum(int(entry["trials_completed"] or 0) for entry in entries)
    passed = sum(int(entry["passed"] or 0) for entry in entries)
    failed = sum(int(entry["failed"] or 0) for entry in entries)
    errors = sum(int(entry["errors"] or 0) for entry in entries)

    def _rate(count: int) -> float | None:
        return (count / completed) if completed else None

    violation_counts: dict[str, int] = {}
    for entry in entries:
        for code, count in entry["violation_counts"].items():
            violation_counts[code] = violation_counts.get(code, 0) + int(count or 0)
    usage_present = [entry for entry in entries if entry["usage"]["status"] != "missing"]
    return {
        "scenarios_requested": len(entries),
        "scenarios_completed": len(entries),
        "trials_requested": requested,
        "trials_completed": completed,
        "passed": passed,
        "pass_rate": _rate(passed),
        "failed": failed,
        "failure_rate": _rate(failed),
        "errors": errors,
        "error_rate": _rate(errors),
        "violation_counts": dict(sorted(violation_counts.items())),
        "total_input_tokens": sum(entry["usage"]["input_tokens"] for entry in usage_present),
        "total_output_tokens": sum(entry["usage"]["output_tokens"] for entry in usage_present),
        "total_tokens": sum(entry["usage"]["total_tokens"] for entry in usage_present),
        "total_responses": sum(entry["usage"]["responses"] for entry in entries),
        "scenarios_with_usage": sum(1 for entry in entries if entry["usage"]["status"] == "complete"),
        "scenarios_partial_usage": sum(1 for entry in entries if entry["usage"]["status"] == "partial"),
        "scenarios_missing_usage": sum(1 for entry in entries if entry["usage"]["status"] == "missing"),
        "turns_total": sum(int(entry["turns_total"] or 0) for entry in entries),
    }


def build_unified_report(documents: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Build the unified document; raises on the first malformed input."""
    if not documents:
        raise UnifiedReportError("At least one input report is required.")
    entries = [normalize_report(doc, source) for source, doc in documents]
    return {
        "note": "offline unified report; no live calls; per-scenario invariants differ",
        "scenarios_requested": len(entries),
        "scenarios_completed": len(entries),
        "aggregate": summarize_entries(entries),
        "scenarios": entries,
        "cost_note": COST_NOTE,
    }


def write_unified_report(report: dict[str, Any], output_path: Path) -> Path:
    output_path = output_path.resolve()
    if output_path.exists():
        raise UnifiedReportError(f"Refusing to overwrite existing report: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return output_path


def format_unified_report(report: dict[str, Any]) -> str:
    aggregate = report["aggregate"]
    lines = [
        "AgentCrash unified multi-scenario report",
        f"scenarios={aggregate['scenarios_completed']}/{aggregate['scenarios_requested']} "
        f"trials={aggregate['trials_completed']}/{aggregate['trials_requested']}",
    ]

    def _percent(rate: Any) -> str:
        return f"{rate:.1%}" if isinstance(rate, float) else "n/a"

    lines.append(
        f"pass: {aggregate['passed']}/{aggregate['trials_completed']} ({_percent(aggregate['pass_rate'])}) | "
        f"fail: {aggregate['failed']}/{aggregate['trials_completed']} ({_percent(aggregate['failure_rate'])}) | "
        f"errors: {aggregate['errors']}/{aggregate['trials_completed']} ({_percent(aggregate['error_rate'])})"
    )
    for entry in report["scenarios"]:
        if entry["kind"] == "deterministic_comparison":
            if entry["errors"]:
                outcome = "ERROR"
            else:
                outcome = f"{entry['passed']} passed / {entry['failed']} failed"
        else:
            outcome = (
                "ERROR" if entry["errors"]
                else ("PASS" if entry["failed"] == 0 else "FAIL")
            )
        lines.append(
            f"{entry['scenario']} [{entry['kind']}]: {outcome} | "
            f"trials={entry['trials_completed']}/{entry['trials_requested']} "
            f"passed={entry['passed']} failed={entry['failed']} errors={entry['errors']} | "
            f"violations={entry['violation_counts'] or 'none'} | "
            f"usage={entry['usage']['status']}"
        )
    lines.append(
        f"tokens: input={aggregate['total_input_tokens']} "
        f"output={aggregate['total_output_tokens']} total={aggregate['total_tokens']} "
        f"responses={aggregate['total_responses']} "
        f"(usage complete={aggregate['scenarios_with_usage']} "
        f"partial={aggregate['scenarios_partial_usage']} "
        f"missing={aggregate['scenarios_missing_usage']})"
    )
    lines.append(f"cost: {report['cost_note']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Summarize existing scenario JSON reports offline (no live calls)."
    )
    parser.add_argument(
        "--input",
        dest="inputs",
        action="append",
        required=True,
        type=Path,
        help="existing scenario report JSON (repeatable)",
    )
    parser.add_argument("--output", type=Path, required=True, help="new JSON report path")
    args = parser.parse_args(argv)

    documents: list[tuple[str, dict[str, Any]]] = []
    for path in args.inputs:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            parser.error(f"Cannot read input {path}: {exc}")
        try:
            data = json.loads(text)
        except ValueError as exc:
            parser.error(f"Invalid JSON in {path}: {exc}")
        documents.append((str(path), data))

    try:
        report = build_unified_report(documents)
    except UnifiedReportError as exc:
        parser.error(str(exc))
    try:
        exported = write_unified_report(report, args.output)
    except UnifiedReportError as exc:
        parser.error(str(exc))
    print(format_unified_report(report))
    print(f"JSON report: {exported}")


if __name__ == "__main__":
    main()
