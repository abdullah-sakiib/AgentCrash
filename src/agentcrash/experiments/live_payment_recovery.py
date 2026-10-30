"""Controlled live Muse Spark payment-recovery trials (Milestones 4.3/4.4).

Single-trial workflow preserved; optional multi-trial mode runs N isolated
trials with aggregate statistics. Reuses the Milestone 4.2 harness, simulator,
fault injection, and evaluator. Default tests stay offline with fakes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from agentcrash.experiments.compare_payment_recovery import (
    DEFAULT_TASK,
    ExperimentExecutionError,
    PaymentTimeoutTask,
    run_payment_timeout_strategy,
)
from agentcrash.harness import (
    HarnessResult,
    redact_api_key,
    run_model_payment_recovery,
)
from agentcrash.providers.meta_responses import (
    MetaResponsesConfig,
    MissingCredentialsError,
    load_config_from_env,
)

SCENARIO = "payment_timeout_after_commit"
LIVE_STRATEGY = "model_recovery"
# No reliable pricing data in-repo; report tokens only.
COST_NOTE = "cost_not_calculated (no reliable pricing data)"


@dataclass(frozen=True)
class ModelTrialRecord:
    trial_index: int
    trial_id: str
    model: str
    scenario: str
    task_outcome: str | None
    live_error: str | None
    order_id: int | None
    payment_ids: tuple[int, ...]
    successful_charge_count: int
    invariant_violations: tuple[dict[str, str], ...]
    turns_used: int
    usage: dict[str, Any]
    trace: tuple[dict[str, Any], ...]
    evaluation_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MultiTrialAggregate:
    requested_trials: int
    completed_trials: int
    passed: int
    pass_rate: float | None
    failed: int
    failure_rate: float | None
    errors: int
    error_rate: float | None
    duplicate_charge_trials: int
    duplicate_charge_rate: float | None
    violation_counts: dict[str, int]
    total_input_tokens: int
    total_output_tokens: int
    total_tokens: int
    total_responses: int
    trials_with_usage: int
    trials_missing_usage: int
    trials_partial_usage: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MultiTrialReport:
    scenario: str
    model: str
    task: PaymentTimeoutTask
    requested_trials: int
    completed_trials: int
    aggregate: MultiTrialAggregate
    trials: tuple[ModelTrialRecord, ...]
    baselines: tuple[BaselineSummary, ...]
    cost_note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BaselineSummary:
    strategy_name: str
    task_outcome: str
    successful_charge_count: int
    violation_codes: tuple[str, ...]


@dataclass(frozen=True)
class LiveTrialReport:
    scenario: str
    trial_count: int
    model: str
    task: PaymentTimeoutTask
    live_outcome: str | None
    live_error: str | None
    order_id: int | None
    payment_ids: tuple[int, ...]
    successful_charge_count: int
    turns_used: int
    usage: dict[str, Any]
    cost_note: str
    invariant_violations: tuple[dict[str, str], ...]
    baselines: tuple[BaselineSummary, ...]
    trace: tuple[dict[str, Any], ...]
    evaluation_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_live_config(dotenv_path: str | Path | None = None) -> MetaResponsesConfig:
    """Load and validate live config without calling the model.

    Fails loudly on missing credentials or invalid values; never falls back
    to another model silently (the configured model is used as-is).
    """
    config = load_config_from_env(dotenv_path=dotenv_path)
    if not config.model or not config.model.strip():
        raise ValueError("Configured model must not be empty.")
    if not config.base_url or not config.base_url.strip():
        raise ValueError("Configured base URL must not be empty.")
    if not config.timeout_seconds or config.timeout_seconds <= 0:
        raise ValueError("Configured timeout must be greater than zero.")
    return config


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


def _partial_trace(exc: BaseException) -> tuple[dict[str, Any], ...]:
    """Sanitized harness events recorded before the failure, if any."""
    partial = getattr(exc, "harness_trace", None)
    if not partial:
        return ()
    return tuple(
        asdict(event) if hasattr(event, "__dataclass_fields__") else dict(event)
        for event in partial
    )


async def run_live_trial(
    database_dir: Path,
    model_client: Any,
    task: PaymentTimeoutTask = DEFAULT_TASK,
    *,
    max_turns: int = 6,
    model_name: str | None = None,
    config: MetaResponsesConfig | None = None,
) -> LiveTrialReport:
    """Run exactly one live-model trial plus both deterministic baselines.

    Single-trial by construction: no trial_count parameter, no loops, no
    retries. All three runs get fresh isolated databases in database_dir.
    """
    if max_turns <= 0:
        raise ValueError("max_turns must be greater than zero.")
    database_dir = database_dir.resolve()
    database_dir.mkdir(parents=True, exist_ok=True)
    live_path = database_dir / "model_recovery.db"
    unsafe_path = database_dir / "unsafe_retry.db"
    safe_path = database_dir / "status_check.db"
    existing = [path for path in (live_path, unsafe_path, safe_path) if path.exists()]
    if existing:
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment databases: {existing}"
        )

    unsafe = await run_payment_timeout_strategy("unsafe_retry", unsafe_path, task)
    safe = await run_payment_timeout_strategy("status_check", safe_path, task)
    baselines = (
        BaselineSummary(
            strategy_name="unsafe_retry",
            task_outcome=unsafe.task_outcome,
            successful_charge_count=unsafe.successful_charge_count,
            violation_codes=tuple(v.code for v in unsafe.invariant_violations),
        ),
        BaselineSummary(
            strategy_name="status_check",
            task_outcome=safe.task_outcome,
            successful_charge_count=safe.successful_charge_count,
            violation_codes=tuple(v.code for v in safe.invariant_violations),
        ),
    )

    try:
        live: HarnessResult | None = await run_model_payment_recovery(
            live_path, model_client, task, max_turns=max_turns
        )
        live_error: str | None = None
        error_trace: tuple[dict[str, Any], ...] = ()
    except Exception as exc:
        live = None
        live_error = _redact_error(str(exc), config)
        error_trace = _partial_trace(exc)

    if live is None:
        api_key = config.api_key if config is not None else None
        return LiveTrialReport(
            scenario=SCENARIO,
            trial_count=1,
            model=model_name or "unknown",
            task=task,
            live_outcome=None,
            live_error=live_error,
            order_id=None,
            payment_ids=(),
            successful_charge_count=0,
            turns_used=0,
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 0},
            cost_note=COST_NOTE,
            invariant_violations=(),
            baselines=baselines,
            trace=tuple(
                redact_api_key(list((*error_trace, {"kind": "error", "error": live_error})), api_key)
            ),
            evaluation_evidence={},
        )
    return LiveTrialReport(
        scenario=SCENARIO,
        trial_count=1,
        model=model_name or "unknown",
        task=task,
        live_outcome=live.task_outcome,
        live_error=None,
        order_id=live.order_id,
        payment_ids=live.payment_ids,
        successful_charge_count=live.successful_charge_count,
        turns_used=live.turns_used,
        usage={
            "input_tokens": live.usage.input_tokens,
            "output_tokens": live.usage.output_tokens,
            "total_tokens": live.usage.total_tokens,
            "responses": live.usage.responses,
        },
        cost_note=COST_NOTE,
        invariant_violations=tuple(
            {"code": violation.code, "message": violation.message}
            for violation in live.invariant_violations
        ),
        baselines=baselines,
        trace=tuple(
            redact_api_key(
                [asdict(event) for event in live.trace],
                config.api_key if config is not None else None,
            )
        ),
        evaluation_evidence=dict(getattr(live, "evaluation_evidence", {}) or {}),
    )


def _trial_id(index: int) -> str:
    return f"{SCENARIO}-trial-{index:03d}"


def summarize_trials(
    trials: tuple[ModelTrialRecord, ...], requested_trials: int
) -> MultiTrialAggregate:
    """Aggregate with completed_trials as the denominator; None rate if zero.

    Every requested trial is accounted for: passed + failed + errors ==
    completed. Duplicate-charge and violation counts are trial-level.
    Token totals sum only non-None fields (None contributes 0); legitimate
    integer 0 is preserved as 0. trials_with_usage counts trials with at
    least one non-None token field and responses > 0; trials_missing_usage
    counts the remainder (including error trials with zero responses);
    trials_partial_usage counts trials with responses > 0 where some but
    not all token fields are None.
    """
    completed = len(trials)
    passed = sum(1 for trial in trials if trial.live_error is None and trial.task_outcome == "passed")
    failed = sum(1 for trial in trials if trial.live_error is None and trial.task_outcome == "failed")
    errors = sum(1 for trial in trials if trial.live_error is not None)
    duplicates = sum(1 for trial in trials if trial.successful_charge_count > 1)
    violation_counts: dict[str, int] = {}
    for trial in trials:
        for violation in {entry["code"] for entry in trial.invariant_violations}:
            violation_counts[violation] = violation_counts.get(violation, 0) + 1
    total_in = sum(v for trial in trials for v in [trial.usage.get("input_tokens")] if isinstance(v, int))
    total_out = sum(v for trial in trials for v in [trial.usage.get("output_tokens")] if isinstance(v, int))
    total_tokens = sum(v for trial in trials for v in [trial.usage.get("total_tokens")] if isinstance(v, int))
    total_responses = sum(int(trial.usage.get("responses") or 0) for trial in trials)
    with_usage = 0
    partial = 0
    for trial in trials:
        responses = int(trial.usage.get("responses") or 0)
        fields = [
            trial.usage.get("input_tokens"),
            trial.usage.get("output_tokens"),
            trial.usage.get("total_tokens"),
        ]
        present = [isinstance(value, int) for value in fields]
        if responses > 0 and any(present):
            with_usage += 1
        if responses > 0 and any(value is None for value in fields) and any(present):
            partial += 1
    missing = completed - with_usage

    def _rate(count: int) -> float | None:
        return (count / completed) if completed else None

    return MultiTrialAggregate(
        requested_trials=requested_trials,
        completed_trials=completed,
        passed=passed,
        pass_rate=_rate(passed),
        failed=failed,
        failure_rate=_rate(failed),
        errors=errors,
        error_rate=_rate(errors),
        duplicate_charge_trials=duplicates,
        duplicate_charge_rate=_rate(duplicates),
        violation_counts=dict(sorted(violation_counts.items())),
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_tokens=total_tokens,
        total_responses=total_responses,
        trials_with_usage=with_usage,
        trials_missing_usage=missing,
        trials_partial_usage=partial,
    )


async def run_live_trials(
    database_dir: Path,
    model_client_factory: Any,
    task: PaymentTimeoutTask = DEFAULT_TASK,
    *,
    trial_count: int,
    max_turns: int = 6,
    model_name: str | None = None,
    config: MetaResponsesConfig | None = None,
) -> MultiTrialReport:
    """Run N isolated model trials plus one deterministic baseline pair.

    Validates trial_count and max_turns before creating any database or
    calling any API. Each trial gets trial_XXX/model_recovery.db; baselines
    run once in baselines/. No retries; every model-trial error (including
    client-factory failures) is recorded in place and the batch continues.
    Baseline infrastructure failure aborts the batch before model trials
    begin and propagates without a partial report; deterministic baselines
    and evaluator semantics are unchanged.
    """
    if trial_count <= 0:
        raise ValueError("trial_count must be greater than zero.")
    if max_turns <= 0:
        raise ValueError("max_turns must be greater than zero.")
    database_dir = database_dir.resolve()
    targets = [database_dir / f"trial_{index:03d}" / "model_recovery.db" for index in range(1, trial_count + 1)]
    targets += [database_dir / "baselines" / "unsafe_retry.db", database_dir / "baselines" / "safe_check.db"]
    existing = [path for path in targets if path.exists()]
    if existing:
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment databases: {existing}"
        )
    database_dir.mkdir(parents=True, exist_ok=True)
    for trial_index in range(1, trial_count + 1):
        (database_dir / f"trial_{trial_index:03d}").mkdir(parents=True, exist_ok=True)
    (database_dir / "baselines").mkdir(parents=True, exist_ok=True)

    unsafe = await run_payment_timeout_strategy(
        "unsafe_retry", database_dir / "baselines" / "unsafe_retry.db", task
    )
    safe = await run_payment_timeout_strategy(
        "status_check", database_dir / "baselines" / "safe_check.db", task
    )
    baselines = (
        BaselineSummary(
            strategy_name="unsafe_retry",
            task_outcome=unsafe.task_outcome,
            successful_charge_count=unsafe.successful_charge_count,
            violation_codes=tuple(v.code for v in unsafe.invariant_violations),
        ),
        BaselineSummary(
            strategy_name="status_check",
            task_outcome=safe.task_outcome,
            successful_charge_count=safe.successful_charge_count,
            violation_codes=tuple(v.code for v in safe.invariant_violations),
        ),
    )

    records: list[ModelTrialRecord] = []
    for trial_index in range(1, trial_count + 1):
        trial_dir = database_dir / f"trial_{trial_index:03d}"
        trial_db = trial_dir / "model_recovery.db"
        try:
            client = model_client_factory(trial_index)
            live = await run_model_payment_recovery(trial_db, client, task, max_turns=max_turns)
            error: str | None = None
            error_trace: tuple[dict[str, Any], ...] = ()
        except Exception as exc:
            live = None
            error = _redact_error(str(exc), config)
            error_trace = _partial_trace(exc)
        if live is None:
            api_key = config.api_key if config is not None else None
            records.append(
                ModelTrialRecord(
                    trial_index=trial_index,
                    trial_id=_trial_id(trial_index),
                    model=model_name or "unknown",
                    scenario=SCENARIO,
                    task_outcome=None,
                    live_error=error,
                    order_id=None,
                    payment_ids=(),
                    successful_charge_count=0,
                    invariant_violations=(),
                    turns_used=0,
                    usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 0},
                    trace=tuple(
                        redact_api_key(list((*error_trace, {"kind": "error", "error": error})), api_key)
                    ),
                    evaluation_evidence={},
                )
            )
        else:
            records.append(
                ModelTrialRecord(
                    trial_index=trial_index,
                    trial_id=_trial_id(trial_index),
                    model=model_name or "unknown",
                    scenario=SCENARIO,
                    task_outcome=live.task_outcome,
                    live_error=None,
                    order_id=live.order_id,
                    payment_ids=live.payment_ids,
                    successful_charge_count=live.successful_charge_count,
                    invariant_violations=tuple(
                        {"code": violation.code, "message": violation.message}
                        for violation in live.invariant_violations
                    ),
                    turns_used=live.turns_used,
                    usage={
                        "input_tokens": live.usage.input_tokens,
                        "output_tokens": live.usage.output_tokens,
                        "total_tokens": live.usage.total_tokens,
                        "responses": live.usage.responses,
                    },
                    trace=tuple(
                        redact_api_key(
                            [asdict(event) for event in live.trace],
                            config.api_key if config is not None else None,
                        )
                    ),
                    evaluation_evidence=dict(getattr(live, "evaluation_evidence", {}) or {}),
                )
            )
    frozen = tuple(records)
    return MultiTrialReport(
        scenario=SCENARIO,
        model=model_name or "unknown",
        task=task,
        requested_trials=trial_count,
        completed_trials=len(frozen),
        aggregate=summarize_trials(frozen, trial_count),
        trials=frozen,
        baselines=baselines,
        cost_note=COST_NOTE,
    )


def format_multi_trial_report(report: MultiTrialReport) -> str:
    aggregate = report.aggregate
    lines = [
        "AgentCrash live payment-timeout multi-trial evaluation",
        f"scenario={report.scenario} requested={aggregate.requested_trials} completed={aggregate.completed_trials}",
        f"model={report.model}",
    ]

    def _percent(rate: float | None) -> str:
        return f"{rate:.1%}" if rate is not None else "n/a"

    lines.append(
        f"pass: {aggregate.passed}/{aggregate.completed_trials} ({_percent(aggregate.pass_rate)}) | "
        f"fail: {aggregate.failed}/{aggregate.completed_trials} ({_percent(aggregate.failure_rate)}) | "
        f"errors: {aggregate.errors}/{aggregate.completed_trials} ({_percent(aggregate.error_rate)})"
    )
    lines.append(
        f"duplicate_charge_trials: {aggregate.duplicate_charge_trials}/{aggregate.completed_trials} "
        f"({_percent(aggregate.duplicate_charge_rate)})"
    )
    if aggregate.violation_counts:
        details = ", ".join(f"{code}={count}" for code, count in aggregate.violation_counts.items())
    else:
        details = "none"
    lines.append(f"violations_by_type: {details}")
    lines.append(
        f"tokens: input={aggregate.total_input_tokens} output={aggregate.total_output_tokens} "
        f"total={aggregate.total_tokens} responses={aggregate.total_responses} "
        f"(with_usage={aggregate.trials_with_usage} missing={aggregate.trials_missing_usage} "
        f"partial={aggregate.trials_partial_usage})"
    )
    lines.append(f"cost: {report.cost_note}")
    for baseline in report.baselines:
        outcome = "PASS" if baseline.task_outcome == "passed" else "FAIL"
        codes = ",".join(baseline.violation_codes) if baseline.violation_codes else "none"
        lines.append(
            f"{baseline.strategy_name}: {outcome} | "
            f"successful_charges={baseline.successful_charge_count} | violations={codes}"
        )
    return "\n".join(lines)


def export_multi_trial_json(report: MultiTrialReport, output_path: Path) -> Path:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return output_path


def format_live_report(report: LiveTrialReport) -> str:
    lines = [
        "AgentCrash live payment-timeout trial",
        f"scenario={report.scenario} trial_count={report.trial_count}",
        f"model={report.model}",
    ]
    if report.live_error is not None:
        lines.append(f"live: ERROR | {report.live_error}")
    else:
        outcome = "PASS" if report.live_outcome == "passed" else "FAIL"
        lines.append(
            f"live ({LIVE_STRATEGY}): {outcome} | "
            f"successful_charges={report.successful_charge_count} | "
            f"turns={report.turns_used}"
        )
    for baseline in report.baselines:
        outcome = "PASS" if baseline.task_outcome == "passed" else "FAIL"
        codes = ",".join(baseline.violation_codes) if baseline.violation_codes else "none"
        lines.append(
            f"{baseline.strategy_name}: {outcome} | "
            f"successful_charges={baseline.successful_charge_count} | "
            f"violations={codes}"
        )
    usage = report.usage
    lines.append(
        f"tokens: input={usage.get('input_tokens')} "
        f"output={usage.get('output_tokens')} total={usage.get('total_tokens')} "
        f"responses={usage.get('responses')}"
    )
    lines.append(f"cost: {report.cost_note}")
    return "\n".join(lines)


def export_live_report_json(report: LiveTrialReport, output_path: Path) -> Path:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8")
    return output_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run live Muse Spark payment-recovery trials (default: exactly one)."
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate configuration without calling the live model",
    )
    parser.add_argument("--dry-run", action="store_true", help="alias for --validate-only")
    parser.add_argument("--output", type=Path, help="optional path for the JSON report")
    parser.add_argument("--max-turns", type=int, default=6, help="harness turn limit")
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="number of independent live trials (default: 1)",
    )
    args = parser.parse_args(argv)

    if args.max_turns <= 0:
        parser.error("--max-turns must be greater than zero.")
    if args.trials <= 0:
        parser.error("--trials must be greater than zero.")

    try:
        config = validate_live_config()
    except (MissingCredentialsError, ValueError) as exc:
        parser.error(str(exc))

    if args.validate_only or args.dry_run:
        print("AgentCrash live trial configuration: OK")
        print(f"model={config.model}")
        print(f"base_url={config.base_url}")
        print(f"timeout_seconds={config.timeout_seconds}")
        print("live model call: skipped (validate-only)")
        return

    from agentcrash.providers.meta_responses import MetaResponsesClient

    if args.trials == 1:
        async def _run_one() -> LiveTrialReport:
            client = MetaResponsesClient(config)
            with TemporaryDirectory(prefix="agentcrash-live-") as directory:
                return await run_live_trial(
                    Path(directory),
                    client,
                    DEFAULT_TASK,
                    max_turns=args.max_turns,
                    model_name=config.model,
                    config=config,
                )

        report = asyncio.run(_run_one())
        print(format_live_report(report))
        if args.output is not None:
            exported = export_live_report_json(report, args.output)
            print(f"JSON report: {exported}")
        if report.live_error is not None:
            raise SystemExit(1)
        return

    async def _run_many() -> MultiTrialReport:
        with TemporaryDirectory(prefix="agentcrash-live-multi-") as directory:
            def _factory(trial_index: int):
                return MetaResponsesClient(config)

            return await run_live_trials(
                Path(directory),
                _factory,
                DEFAULT_TASK,
                trial_count=args.trials,
                max_turns=args.max_turns,
                model_name=config.model,
                config=config,
            )

    multi = asyncio.run(_run_many())
    print(format_multi_trial_report(multi))
    if args.output is not None:
        exported = export_multi_trial_json(multi, args.output)
        print(f"JSON report: {exported}")
    if multi.aggregate.errors > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
