"""Unified orchestration for approved AgentCrash scenarios (Milestone 6.3).

One interface for deterministic and live execution across the three
business-safety scenarios. Offline deterministic runs are the default and
never instantiate a live API client. Live execution requires BOTH
``--mode live`` and the explicit ``--live`` opt-in flag plus valid
credentials. No automatic retries; every trial is accounted for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agentcrash.experiments.compare_order_fulfillment import (
    DEFAULT_FULFILLMENT_TASK,
    FulfillmentTask,
    run_fulfillment_comparison,
)
from agentcrash.experiments.compare_order_intent import (
    DEFAULT_ORDER_INTENT_TASK,
    OrderIntentTask,
    SCENARIO as INTENT_SCENARIO,
    run_order_intent_comparison,
)
from agentcrash.experiments.compare_payment_recovery import (
    DEFAULT_TASK,
    ExperimentExecutionError,
    PaymentTimeoutTask,
    run_payment_timeout_comparison,
)
from agentcrash.experiments.live_payment_recovery import (
    SCENARIO as PAYMENT_SCENARIO,
    run_live_trials,
)
from agentcrash.experiments.model_fulfillment_trial import (
    SCENARIO as FULFILLMENT_SCENARIO,
    run_fulfillment_trial,
)
from agentcrash.experiments.model_intent_trial import (
    SCENARIO as INTENT_TRIAL_SCENARIO,
    run_intent_trial,
)
from agentcrash.harness import _sanitize as _sanitize_trace
from agentcrash.harness import redact_api_key as _redact_trace_value
from agentcrash.providers.meta_responses import (
    MetaResponsesConfig,
    MissingCredentialsError,
    load_config_from_env,
)

EXPERIMENT_NAME = "agentcrash-orchestrated-run"
COST_NOTE = "cost_not_calculated (no reliable pricing data)"

PAYMENT_KEY = "payment-timeout"
FULFILLMENT_KEY = "fulfill-before-payment"
INTENT_KEY = "duplicate-order-for-intent"
SUPPORTED_SCENARIOS = (PAYMENT_KEY, FULFILLMENT_KEY, INTENT_KEY)


def _redact_error(message: str, config: MetaResponsesConfig | None = None) -> str:
    """Truncate and redact the exact configured key substring if present.

    Exact-substring replacement only; transformed or truncated key fragments
    are not claimed to be covered. Callers must pass the active config for
    any error path that could echo credentials.
    """
    text = message[:500]
    if config is not None and config.api_key and config.api_key in text:
        text = text.replace(config.api_key, "[REDACTED]")
    return text


def _trial_usage(input_tokens: Any, output_tokens: Any, total_tokens: Any, responses: Any) -> dict[str, Any]:
    """Normalize token usage; None means missing, 0 is a confirmed zero."""
    values = {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
        "total_tokens": total_tokens if isinstance(total_tokens, int) else None,
    }
    responses_int = responses if isinstance(responses, int) else 0
    present = [value is not None for value in values.values()]
    with_usage = 1 if (responses_int > 0 and any(present)) else 0
    missing = 0 if with_usage else 1
    partial = 1 if (responses_int > 0 and any(value is None for value in values.values()) and any(present)) else 0
    return {
        "input_tokens": values["input_tokens"] if values["input_tokens"] is not None else 0,
        "output_tokens": values["output_tokens"] if values["output_tokens"] is not None else 0,
        "total_tokens": values["total_tokens"] if values["total_tokens"] is not None else 0,
        "responses": responses_int,
        "with_usage": with_usage,
        "missing_usage": missing,
        "partial_usage": partial,
        "status": "missing" if missing else ("partial" if partial else "complete"),
    }


@dataclass(frozen=True)
class OrchestratedTrial:
    scenario: str
    mode: str
    trial_index: int
    trial_id: str
    outcome: str | None
    error: str | None
    turns: int
    successful_charges: int = 0
    order_status: str | None = None
    order_ids: tuple[int, ...] = ()
    inventory_on_hand: int | None = None
    payment_ids: tuple[int, ...] = ()
    violations: tuple[dict[str, str], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    trace: tuple[dict[str, Any], ...] = ()
    evaluation_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrchestratedScenario:
    scenario: str
    scenario_key: str
    mode: str
    status: str
    error: str | None
    trials_requested: int
    trials_completed: int
    passed: int
    failed: int
    errors: int
    pass_rate: float | None
    turns_total: int
    usage: dict[str, Any]
    violation_counts: dict[str, int]
    baselines: tuple[dict[str, Any], ...] = ()
    trials: tuple[OrchestratedTrial, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrchestratedReport:
    experiment: str
    mode: str
    model: str | None
    requested_trials: int
    scenarios_requested: int
    scenarios_completed: int
    aggregate: dict[str, Any]
    scenarios: tuple[OrchestratedScenario, ...]
    cost_note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _summarize_trials(trials: list[OrchestratedTrial]) -> dict[str, Any]:
    passed = sum(1 for trial in trials if trial.error is None and trial.outcome == "passed")
    failed = sum(1 for trial in trials if trial.error is None and trial.outcome == "failed")
    errors = sum(1 for trial in trials if trial.error is not None)
    completed = len(trials)

    def _rate(count: int) -> float | None:
        return (count / completed) if completed else None

    violation_counts: dict[str, int] = {}
    for trial in trials:
        for violation in {entry["code"] for entry in trial.violations}:
            violation_counts[violation] = violation_counts.get(violation, 0) + 1
    with_usage = sum(1 for trial in trials if trial.usage.get("status") == "complete")
    partial = sum(1 for trial in trials if trial.usage.get("status") == "partial")
    missing = sum(1 for trial in trials if trial.usage.get("status") == "missing")
    present = [trial for trial in trials if trial.usage.get("status") != "missing"]
    return {
        "requested": completed,
        "completed": completed,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "pass_rate": _rate(passed),
        "violation_counts": dict(sorted(violation_counts.items())),
        "total_input_tokens": sum(trial.usage.get("input_tokens", 0) for trial in present),
        "total_output_tokens": sum(trial.usage.get("output_tokens", 0) for trial in present),
        "total_tokens": sum(trial.usage.get("total_tokens", 0) for trial in present),
        "total_responses": sum(trial.usage.get("responses", 0) for trial in trials),
        "trials_with_usage": with_usage,
        "trials_partial_usage": partial,
        "trials_missing_usage": missing,
        "turns_total": sum(trial.turns for trial in trials),
    }


def _summarize_scenarios(entries: list[OrchestratedScenario]) -> dict[str, Any]:
    completed_trials = sum(entry.trials_completed for entry in entries)
    passed = sum(entry.passed for entry in entries)
    failed = sum(entry.failed for entry in entries)
    errors = sum(entry.errors for entry in entries)

    def _rate(count: int) -> float | None:
        return (count / completed_trials) if completed_trials else None

    by_scenario: dict[str, dict[str, int]] = {}
    overall: dict[str, int] = {}
    for entry in entries:
        by_scenario[entry.scenario] = dict(entry.violation_counts)
        for code, count in entry.violation_counts.items():
            overall[code] = overall.get(code, 0) + count
    return {
        "trials_requested": sum(entry.trials_requested for entry in entries),
        "trials_completed": completed_trials,
        "passed": passed,
        "pass_rate": _rate(passed),
        "failed": failed,
        "failure_rate": _rate(failed),
        "errors": errors,
        "error_rate": _rate(errors),
        "violation_counts": dict(sorted(overall.items())),
        "violations_by_scenario": by_scenario,
        "total_input_tokens": sum(entry.usage.get("input_tokens", 0) for entry in entries),
        "total_output_tokens": sum(entry.usage.get("output_tokens", 0) for entry in entries),
        "total_tokens": sum(entry.usage.get("total_tokens", 0) for entry in entries),
        "total_responses": sum(entry.usage.get("responses", 0) for entry in entries),
        "turns_total": sum(entry.turns_total for entry in entries),
        "incomplete_trials": [
            {"scenario": trial.scenario, "trial_index": trial.trial_index,
             "trial_id": trial.trial_id, "reason": trial.error or "business-invariant failure"}
            for entry in entries
            for trial in entry.trials
            if trial.error is not None or trial.outcome == "failed"
        ],
    }


def _baseline_entries(baselines: Any) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for baseline in baselines or []:
        if isinstance(baseline, dict):
            rows.append({
                "strategy_name": baseline.get("strategy_name", "unknown"),
                "task_outcome": baseline.get("task_outcome"),
                "successful_charge_count": baseline.get("successful_charge_count", 0),
                "violation_codes": list(baseline.get("violation_codes", [])),
            })
        else:
            rows.append({
                "strategy_name": getattr(baseline, "strategy_name", "unknown"),
                "task_outcome": getattr(baseline, "task_outcome", None),
                "successful_charge_count": getattr(baseline, "successful_charge_count", 0),
                "violation_codes": list(getattr(baseline, "violation_codes", []) or []),
            })
    return tuple(rows)


def _scenario_entry(
    *,
    scenario: str,
    scenario_key: str,
    mode: str,
    status: str,
    error: str | None,
    trials_requested: int,
    trials: list[OrchestratedTrial],
    baselines: tuple[dict[str, Any], ...] = (),
) -> OrchestratedScenario:
    summary = _summarize_trials(trials)
    usage_present = [trial for trial in trials if trial.usage.get("status") != "missing"]
    return OrchestratedScenario(
        scenario=scenario,
        scenario_key=scenario_key,
        mode=mode,
        status=status,
        error=error,
        trials_requested=trials_requested,
        trials_completed=len(trials),
        passed=summary["passed"],
        failed=summary["failed"],
        errors=summary["errors"],
        pass_rate=summary["pass_rate"],
        turns_total=summary["turns_total"],
        usage={
            "input_tokens": sum(trial.usage.get("input_tokens", 0) for trial in usage_present),
            "output_tokens": sum(trial.usage.get("output_tokens", 0) for trial in usage_present),
            "total_tokens": sum(trial.usage.get("total_tokens", 0) for trial in usage_present),
            "responses": sum(trial.usage.get("responses", 0) for trial in trials),
            "with_usage": summary["trials_with_usage"],
            "missing_usage": summary["trials_missing_usage"],
            "partial_usage": summary["trials_partial_usage"],
            "status": (
                "missing" if summary["trials_missing_usage"] == len(trials) and trials
                else ("partial" if summary["trials_partial_usage"] else "complete")
            ) if trials else "missing",
        },
        violation_counts=summary["violation_counts"],
        baselines=baselines,
        trials=tuple(trials),
    )


async def _run_deterministic_scenario(
    scenario_key: str, database_dir: Path
) -> OrchestratedScenario:
    """Run one deterministic baseline comparison in an isolated directory."""
    database_dir = database_dir.resolve()
    if scenario_key == PAYMENT_KEY:
        comparison = await run_payment_timeout_comparison(database_dir)
        scenario = PAYMENT_SCENARIO
    elif scenario_key == FULFILLMENT_KEY:
        from agentcrash.experiments.compare_order_fulfillment import run_fulfillment_comparison

        comparison = await run_fulfillment_comparison(database_dir)
        scenario = comparison.scenario
    else:
        comparison = await run_order_intent_comparison(database_dir)
        scenario = comparison.scenario

    trials: list[OrchestratedTrial] = []
    for index, result in enumerate(comparison.results, start=1):
        outcome = result.task_outcome
        violations = tuple(
            {"code": item["code"], "message": item["message"]}
            if isinstance(item, dict)
            else {"code": item.code, "message": item.message}
            for item in result.invariant_violations
        )
        raw_trace = [asdict(event) for event in result.trace]
        trace = tuple(_sanitize_trace(event) for event in raw_trace)
        successful_charges = getattr(result, "successful_charge_count", 0) or 0
        evaluation_evidence = _sanitize_trace({
            "valid": outcome == "passed",
            "orders_checked": None,
            "payments_checked": None,
            "successful_charge_count": successful_charges,
            "order_ids": list(getattr(result, "order_ids", None) or ()),
            "payment_ids": list(getattr(result, "payment_ids", None) or ()),
            "order_status": getattr(result, "order_status", None),
            "inventory_on_hand": getattr(result, "inventory_on_hand", None),
            "violations": [
                {"code": violation["code"], "message": violation["message"]}
                for violation in violations
            ],
            "source": "persisted-state-evaluation",
            "orders_checked_status": "unavailable",
            "payments_checked_status": "unavailable",
        })
        trials.append(
            OrchestratedTrial(
                scenario=scenario,
                mode="deterministic",
                trial_index=index,
                trial_id=result.strategy_name,
                outcome=outcome,
                error=None,
                turns=len(result.trace),
                successful_charges=successful_charges,
                order_status=getattr(result, "order_status", None),
                order_ids=tuple(getattr(result, "order_ids", None) or ()),
                inventory_on_hand=getattr(result, "inventory_on_hand", None),
                payment_ids=tuple(getattr(result, "payment_ids", None) or ()),
                violations=violations,
                usage=_trial_usage(None, None, None, 0),
                trace=trace,
                evaluation_evidence=evaluation_evidence,
            )
        )
    return _scenario_entry(
        scenario=scenario,
        scenario_key=scenario_key,
        mode="deterministic",
        status="completed",
        error=None,
        trials_requested=len(trials),
        trials=trials,
    )


async def _run_live_payment_scenario(
    database_dir: Path,
    task: PaymentTimeoutTask,
    *,
    trial_count: int,
    max_turns: int,
    model_name: str,
    config: MetaResponsesConfig,
    model_client_factory: Any,
) -> OrchestratedScenario:
    report = await run_live_trials(
        database_dir,
        model_client_factory,
        task,
        trial_count=trial_count,
        max_turns=max_turns,
        model_name=model_name,
        config=config,
    )
    trials: list[OrchestratedTrial] = []
    for trial in report.trials:
        violations = tuple(
            {"code": entry["code"], "message": entry.get("message", "")}
            for entry in trial.invariant_violations
        )
        trial_trace = tuple(_sanitize_trace(event) for event in (trial.trace or ()))
        trial_evidence = dict(getattr(trial, "evaluation_evidence", {}) or {})
        if not trial_evidence:
            trial_evidence = {
                "valid": trial.task_outcome == "passed" if trial.live_error is None else None,
                "orders_checked": None,
                "payments_checked": None,
                "successful_charge_count": trial.successful_charge_count,
                "order_id": trial.order_id,
                "payment_ids": list(trial.payment_ids),
                "violations": [
                    {"code": violation["code"], "message": violation.get("message", "")}
                    for violation in violations
                ],
                "source": "persisted-state-evaluation",
                "orders_checked_status": "unavailable",
                "payments_checked_status": "unavailable",
            }
        trials.append(
            OrchestratedTrial(
                scenario=report.scenario,
                mode="live",
                trial_index=trial.trial_index,
                trial_id=trial.trial_id,
                outcome=trial.task_outcome,
                error=trial.live_error,
                turns=trial.turns_used,
                successful_charges=trial.successful_charge_count,
                payment_ids=tuple(trial.payment_ids),
                violations=violations,
                usage=_trial_usage(
                    trial.usage.get("input_tokens"),
                    trial.usage.get("output_tokens"),
                    trial.usage.get("total_tokens"),
                    trial.usage.get("responses"),
                ),
                trace=trial_trace,
                evaluation_evidence=trial_evidence,
            )
        )
    return _scenario_entry(
        scenario=report.scenario,
        scenario_key=PAYMENT_KEY,
        mode="live",
        status="completed",
        error=None,
        trials_requested=trial_count,
        trials=trials,
        baselines=_baseline_entries([asdict(baseline) for baseline in report.baselines]),
    )


def _partial_harness_trace(exc: BaseException) -> tuple[dict[str, Any], ...]:
    """Sanitized harness events attached to an escaping exception, if any."""
    partial = getattr(exc, "harness_trace", None)
    if not partial:
        return ()
    return tuple(
        _sanitize_trace(asdict(event))
        if hasattr(event, "__dataclass_fields__")
        else _sanitize_trace(dict(event))
        for event in partial
    )


async def _run_live_single_trial_scenario(
    *,
    scenario_key: str,
    database_dir: Path,
    task: Any,
    run_single: Any,
    trial_count: int,
    max_turns: int,
    model_name: str,
    config: MetaResponsesConfig,
    model_client: Any,
) -> OrchestratedScenario:
    """Loop the single-trial runner once per requested trial; no retries.

    Each trial gets trial_XXX/model.db. A failing trial is recorded in
    place and the batch continues with the next trial.
    """
    scenario = INTENT_TRIAL_SCENARIO if scenario_key == INTENT_KEY else FULFILLMENT_SCENARIO
    trials: list[OrchestratedTrial] = []
    for trial_index in range(1, trial_count + 1):
        trial_dir = database_dir / f"trial_{trial_index:03d}"
        trial_id = f"{scenario}-trial-{trial_index:03d}"
        try:
            trial_dir.mkdir(parents=True, exist_ok=True)
            report = await run_single(
                trial_dir / "model.db",
                model_client,
                task,
                max_turns=max_turns,
                model_name=model_name,
                config=config,
            )
        except Exception as exc:
            error = _redact_error(str(exc), config)
            api_key = config.api_key if config is not None else None
            partial = tuple(
                _redact_trace_value(list(_partial_harness_trace(exc)), api_key)
            )
            trials.append(
                OrchestratedTrial(
                    scenario=scenario,
                    mode="live",
                    trial_index=trial_index,
                    trial_id=trial_id,
                    outcome=None,
                    error=error,
                    turns=0,
                    successful_charges=0,
                    order_status=None,
                    order_ids=(),
                    inventory_on_hand=None,
                    payment_ids=(),
                    violations=(),
                    usage=_trial_usage(None, None, None, 0),
                    trace=(*partial, {"kind": "error", "error": error}),
                    evaluation_evidence={},
                )
            )
            continue
        violations = tuple(
            {"code": entry["code"], "message": entry.get("message", "")}
            for entry in report.invariant_violations
        )
        order_ids = tuple(report.order_ids) if scenario_key == INTENT_KEY else ()
        report_trace = tuple(
            _sanitize_trace(event) for event in (report.trace or ())
        )
        report_evidence = dict(getattr(report, "evaluation_evidence", {}) or {})
        if not report_evidence:
            report_evidence = {
                "valid": report.task_outcome == "passed" if report.trial_error is None else None,
                "orders_checked": None,
                "payments_checked": None,
                "successful_charge_count": getattr(report, "successful_charge_count", 0) or 0,
                "order_id": getattr(report, "order_id", None),
                "order_status": getattr(report, "order_status", None),
                "order_ids": list(order_ids),
                "inventory_on_hand": getattr(report, "inventory_on_hand", None),
                "payment_ids": list(getattr(report, "payment_ids", None) or ()),
                "violations": [
                    {"code": violation["code"], "message": violation.get("message", "")}
                    for violation in violations
                ],
                "source": "persisted-state-evaluation",
                "orders_checked_status": "unavailable",
                "payments_checked_status": "unavailable",
            }
        trials.append(
            OrchestratedTrial(
                scenario=scenario,
                mode="live",
                trial_index=trial_index,
                trial_id=trial_id,
                outcome=report.task_outcome,
                error=report.trial_error,
                turns=report.turns_used,
                successful_charges=getattr(report, "successful_charge_count", 0) or 0,
                order_status=getattr(report, "order_status", None),
                order_ids=order_ids,
                inventory_on_hand=getattr(report, "inventory_on_hand", None),
                payment_ids=tuple(getattr(report, "payment_ids", None) or ()),
                violations=violations,
                usage=_trial_usage(
                    report.usage.get("input_tokens"),
                    report.usage.get("output_tokens"),
                    report.usage.get("total_tokens"),
                    report.usage.get("responses"),
                ),
                trace=report_trace,
                evaluation_evidence=report_evidence,
            )
        )
    return _scenario_entry(
        scenario=scenario,
        scenario_key=scenario_key,
        mode="live",
        status="completed",
        error=None,
        trials_requested=trial_count,
        trials=trials,
    )


async def _run_live_scenario_with_baselines(
    *,
    scenario_key: str,
    database_dir: Path,
    trial_count: int,
    max_turns: int,
    model_name: str,
    config: MetaResponsesConfig,
    model_client: Any,
) -> OrchestratedScenario:
    """Run deterministic baselines first, then live trials.

    A baseline infrastructure failure aborts the scenario before any model
    trial and is reported as a setup error without a partial trial list.
    """
    from agentcrash.experiments.compare_order_fulfillment import run_fulfillment_comparison

    baselines_dir = database_dir / "baselines"
    baselines_dir.mkdir(parents=True, exist_ok=True)
    try:
        if scenario_key == FULFILLMENT_KEY:
            comparison = await run_fulfillment_comparison(baselines_dir)
            task: Any = DEFAULT_FULFILLMENT_TASK
            run_single = run_fulfillment_trial
        else:
            comparison = await run_order_intent_comparison(baselines_dir)
            task = DEFAULT_ORDER_INTENT_TASK
            run_single = run_intent_trial
    except Exception as exc:
        return OrchestratedScenario(
            scenario=FULFILLMENT_SCENARIO if scenario_key == FULFILLMENT_KEY else INTENT_TRIAL_SCENARIO,
            scenario_key=scenario_key,
            mode="live",
            status="setup_error",
            error=_redact_error(str(exc), config),
            trials_requested=trial_count,
            trials_completed=0,
            passed=0,
            failed=0,
            errors=0,
            pass_rate=None,
            turns_total=0,
            usage=_trial_usage(None, None, None, 0),
            violation_counts={},
            baselines=(),
            trials=(),
        )
    entry = await _run_live_single_trial_scenario(
        scenario_key=scenario_key,
        database_dir=database_dir,
        task=task,
        run_single=run_single,
        trial_count=trial_count,
        max_turns=max_turns,
        model_name=model_name,
        config=config,
        model_client=model_client,
    )
    baseline_rows = tuple(
        {
            "strategy_name": result.strategy_name,
            "task_outcome": result.task_outcome,
            "successful_charge_count": getattr(result, "successful_charge_count", 0) or 0,
            "violation_codes": [violation.code for violation in result.invariant_violations],
        }
        for result in comparison.results
    )
    return OrchestratedScenario(
        scenario=entry.scenario,
        scenario_key=entry.scenario_key,
        mode=entry.mode,
        status=entry.status,
        error=entry.error,
        trials_requested=entry.trials_requested,
        trials_completed=entry.trials_completed,
        passed=entry.passed,
        failed=entry.failed,
        errors=entry.errors,
        pass_rate=entry.pass_rate,
        turns_total=entry.turns_total,
        usage=entry.usage,
        violation_counts=entry.violation_counts,
        baselines=baseline_rows,
        trials=entry.trials,
    )


async def run_orchestrated_experiment(
    database_dir: Path,
    scenario_keys: list[str],
    *,
    mode: str,
    trial_count: int,
    max_turns: int,
    model_client: Any | None = None,
    model_name: str | None = None,
    config: MetaResponsesConfig | None = None,
    model_client_factory: Any | None = None,
) -> OrchestratedReport:
    """Run selected scenarios with isolated state per trial.

    Deterministic mode runs each scenario's baseline comparison once and
    never touches model clients. Live mode runs trial_count model trials
    per scenario; failed trials are recorded, never retried.
    """
    if mode not in ("deterministic", "live"):
        raise ValueError("mode must be 'deterministic' or 'live'.")
    unknown = [key for key in scenario_keys if key not in SUPPORTED_SCENARIOS]
    if unknown:
        raise ValueError(f"Unknown scenario(s): {', '.join(unknown)}.")
    if not scenario_keys:
        raise ValueError("At least one scenario is required.")
    if trial_count <= 0:
        raise ValueError("trial_count must be greater than zero.")
    if max_turns <= 0:
        raise ValueError("max_turns must be greater than zero.")
    database_dir = database_dir.resolve()
    if mode == "deterministic":
        if trial_count != 1:
            raise ValueError("--trials applies to live mode only; deterministic runs one comparison per scenario.")
        if model_client is not None or model_client_factory is not None or config is not None:
            raise ValueError("Live clients must not be provided in deterministic mode.")
    else:
        if config is None or (model_client is None and model_client_factory is None):
            raise ValueError("Live mode requires a model client (or factory) and validated configuration.")

    database_dir.mkdir(parents=True, exist_ok=True)
    entries: list[OrchestratedScenario] = []
    for scenario_key in scenario_keys:
        scenario_dir = database_dir / scenario_key
        if scenario_dir.exists():
            raise ExperimentExecutionError(
                f"Refusing to reuse experiment database directory: {scenario_dir}"
            )
        scenario_dir.mkdir(parents=True, exist_ok=True)
        if mode == "deterministic":
            entries.append(await _run_deterministic_scenario(scenario_key, scenario_dir))
        elif scenario_key == PAYMENT_KEY:
            factory = model_client_factory or (lambda trial_index: model_client)
            entries.append(
                await _run_live_payment_scenario(
                    scenario_dir,
                    DEFAULT_TASK,
                    trial_count=trial_count,
                    max_turns=max_turns,
                    model_name=model_name or "unknown",
                    config=config,
                    model_client_factory=factory,
                )
            )
        else:
            entries.append(
                await _run_live_scenario_with_baselines(
                    scenario_key=scenario_key,
                    database_dir=scenario_dir,
                    trial_count=trial_count,
                    max_turns=max_turns,
                    model_name=model_name or "unknown",
                    config=config,
                    model_client=model_client,
                )
            )
    frozen = tuple(entries)
    return OrchestratedReport(
        experiment=EXPERIMENT_NAME,
        mode=mode,
        model=model_name,
        requested_trials=trial_count,
        scenarios_requested=len(frozen),
        scenarios_completed=len(frozen),
        aggregate=_summarize_scenarios(list(frozen)),
        scenarios=frozen,
        cost_note=COST_NOTE,
    )


def format_orchestrated_report(report: OrchestratedReport) -> str:
    aggregate = report.aggregate
    lines = [
        f"AgentCrash orchestrated experiment ({report.mode})",
        f"scenarios={report.scenarios_completed}/{report.scenarios_requested} "
        f"trials={aggregate['trials_completed']}/{aggregate['trials_requested']}",
    ]

    def _percent(rate: Any) -> str:
        return f"{rate:.1%}" if isinstance(rate, float) else "n/a"

    lines.append(
        f"pass: {aggregate['passed']}/{aggregate['trials_completed']} ({_percent(aggregate['pass_rate'])}) | "
        f"fail: {aggregate['failed']}/{aggregate['trials_completed']} ({_percent(aggregate['failure_rate'])}) | "
        f"errors: {aggregate['errors']}/{aggregate['trials_completed']} ({_percent(aggregate['error_rate'])})"
    )
    for entry in report.scenarios:
        if entry.status != "completed":
            lines.append(f"{entry.scenario} [{entry.mode}]: SETUP_ERROR | {entry.error}")
            continue
        lines.append(
            f"{entry.scenario} [{entry.mode}]: "
            f"trials={entry.trials_completed}/{entry.trials_requested} "
            f"passed={entry.passed} failed={entry.failed} errors={entry.errors} | "
            f"violations={entry.violation_counts or 'none'}"
        )
    lines.append(
        f"tokens: input={aggregate['total_input_tokens']} "
        f"output={aggregate['total_output_tokens']} total={aggregate['total_tokens']} "
        f"responses={aggregate['total_responses']}"
    )
    lines.append(f"cost: {report.cost_note}")
    return "\n".join(lines)


def export_orchestrated_report(report: OrchestratedReport, output_path: Path) -> Path:
    output_path = output_path.resolve()
    if output_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to overwrite existing report: {output_path}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return output_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run approved AgentCrash scenarios. Default is offline deterministic; "
        "live execution requires --mode live plus the explicit --live flag."
    )
    parser.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        choices=SUPPORTED_SCENARIOS,
        default=None,
        help="scenario to run (repeatable; default: all supported scenarios)",
    )
    parser.add_argument(
        "--mode",
        choices=("deterministic", "live"),
        default="deterministic",
        help="execution mode (default: deterministic, offline)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicit opt-in required for any live model execution",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="live model trials per scenario (default: 1; deterministic mode requires 1)",
    )
    parser.add_argument("--max-turns", type=int, default=6, help="harness turn limit")
    parser.add_argument("--output", type=Path, required=True, help="new JSON report path")
    args = parser.parse_args(argv)

    if args.trials <= 0:
        parser.error("--trials must be greater than zero.")
    if args.max_turns <= 0:
        parser.error("--max-turns must be greater than zero.")
    if args.mode == "live" and not args.live:
        parser.error("Live execution requires the explicit --live flag.")
    if args.live and args.mode != "live":
        parser.error("The --live flag requires --mode live.")
    if args.mode == "deterministic" and args.trials != 1:
        parser.error("--trials applies to live mode only; deterministic runs one comparison per scenario.")

    scenario_keys = args.scenarios or list(SUPPORTED_SCENARIOS)

    config: MetaResponsesConfig | None = None
    model_name: str | None = None
    if args.mode == "live":
        try:
            config = load_config_from_env()
        except (MissingCredentialsError, ValueError) as exc:
            parser.error(str(exc))
        if not config.model or not config.model.strip():
            parser.error("Configured model must not be empty.")
        model_name = config.model
        print("Live execution confirmed:")
        print(f"  scenarios={','.join(scenario_keys)}")
        print(f"  model={model_name}")
        print(f"  trials_per_scenario={args.trials}")
        print(f"  output={args.output}")

    if Path(args.output).exists():
        parser.error(f"Refusing to overwrite existing report: {args.output}")

    async def _run() -> OrchestratedReport:
        with TemporaryDirectory(prefix="agentcrash-orchestrated-") as directory:
            if args.mode == "deterministic":
                return await run_orchestrated_experiment(
                    Path(directory),
                    scenario_keys,
                    mode="deterministic",
                    trial_count=1,
                    max_turns=args.max_turns,
                )
            from agentcrash.providers.meta_responses import MetaResponsesClient

            client = MetaResponsesClient(config)
            return await run_orchestrated_experiment(
                Path(directory),
                scenario_keys,
                mode="live",
                trial_count=args.trials,
                max_turns=args.max_turns,
                model_client=client,
                model_name=model_name,
                config=config,
            )

    try:
        report = asyncio.run(_run())
    except (ExperimentExecutionError, ValueError) as exc:
        parser.error(str(exc))
    print(format_orchestrated_report(report))
    try:
        exported = export_orchestrated_report(report, args.output)
    except ExperimentExecutionError as exc:
        parser.error(str(exc))
    print(f"JSON report: {exported}")


if __name__ == "__main__":
    main()
