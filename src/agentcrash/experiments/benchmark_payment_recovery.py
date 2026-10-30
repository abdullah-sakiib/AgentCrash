from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from agentcrash.experiments.compare_payment_recovery import (
    DEFAULT_TASK,
    ExperimentExecutionError,
    ExperimentResult,
    InvariantViolation,
    PaymentTimeoutTask,
    StrategyName,
    TaskOutcome,
    TraceEvent,
    run_payment_timeout_strategy,
)


DEFAULT_TRIAL_COUNT = 5


@dataclass(frozen=True)
class TrialResult:
    trial_number: int
    strategy_name: StrategyName
    task: PaymentTimeoutTask
    task_outcome: TaskOutcome
    order_id: int
    payment_ids: tuple[int, ...]
    successful_charge_count: int
    invariant_violations: tuple[InvariantViolation, ...]
    trace: tuple[TraceEvent, ...]


@dataclass(frozen=True)
class StrategyMetrics:
    strategy_name: StrategyName
    total_trials: int
    task_success_rate: float
    invariant_violation_rate: float
    duplicate_charge_trial_rate: float
    total_successful_charges: int


@dataclass(frozen=True)
class BenchmarkResult:
    scenario: str
    trials_per_strategy: int
    metrics: tuple[StrategyMetrics, ...]
    trials: tuple[TrialResult, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def metrics_for(self, strategy_name: StrategyName) -> StrategyMetrics:
        return next(
            metrics
            for metrics in self.metrics
            if metrics.strategy_name == strategy_name
        )

    def trials_for(self, strategy_name: StrategyName) -> tuple[TrialResult, ...]:
        return tuple(
            trial
            for trial in self.trials
            if trial.strategy_name == strategy_name
        )


async def run_payment_timeout_benchmark(
    database_directory: Path,
    trial_count: int = DEFAULT_TRIAL_COUNT,
    task: PaymentTimeoutTask = DEFAULT_TASK,
) -> BenchmarkResult:
    if trial_count <= 0:
        raise ValueError("trial_count must be greater than zero")

    database_directory = database_directory.resolve()
    trial_paths = [
        (
            trial_number,
            strategy_name,
            database_directory
            / f"trial_{trial_number:03d}"
            / f"{strategy_name}.db",
        )
        for trial_number in range(1, trial_count + 1)
        for strategy_name in ("unsafe_retry", "status_check")
    ]
    existing = [path for _, _, path in trial_paths if path.exists()]
    if existing:
        raise ExperimentExecutionError(
            f"Benchmark trial databases already exist: {existing}"
        )

    trials: list[TrialResult] = []
    for trial_number, strategy_name, database_path in trial_paths:
        experiment = await run_payment_timeout_strategy(
            strategy_name,
            database_path,
            task,
        )
        trials.append(_to_trial_result(trial_number, experiment))

    frozen_trials = tuple(trials)
    return BenchmarkResult(
        scenario="payment_timeout_after_commit",
        trials_per_strategy=trial_count,
        metrics=tuple(
            _calculate_strategy_metrics(strategy_name, frozen_trials)
            for strategy_name in ("unsafe_retry", "status_check")
        ),
        trials=frozen_trials,
    )


def export_benchmark_json(
    benchmark: BenchmarkResult,
    output_path: Path,
) -> Path:
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(benchmark.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path


def format_benchmark(benchmark: BenchmarkResult) -> str:
    lines = [
        "AgentCrash deterministic payment-timeout benchmark",
        f"trials_per_strategy={benchmark.trials_per_strategy}",
        (
            "strategy | trials | task_success | invariant_violations | "
            "duplicate_charges | total_successful_charges"
        ),
    ]
    for metrics in benchmark.metrics:
        lines.append(
            f"{metrics.strategy_name} | {metrics.total_trials} | "
            f"{metrics.task_success_rate:.1%} | "
            f"{metrics.invariant_violation_rate:.1%} | "
            f"{metrics.duplicate_charge_trial_rate:.1%} | "
            f"{metrics.total_successful_charges}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic payment-timeout trials for the unsafe-retry and "
            "status-check strategies."
        )
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=DEFAULT_TRIAL_COUNT,
        help=f"trials per strategy (default: {DEFAULT_TRIAL_COUNT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="optional path for the complete benchmark JSON result",
    )
    args = parser.parse_args(argv)
    if args.trials <= 0:
        parser.error("--trials must be greater than zero")

    with TemporaryDirectory(prefix="agentcrash-benchmark-") as directory:
        benchmark = asyncio.run(
            run_payment_timeout_benchmark(
                Path(directory),
                trial_count=args.trials,
            )
        )
        print(format_benchmark(benchmark))
        if args.output is not None:
            exported_path = export_benchmark_json(benchmark, args.output)
            print(f"JSON result: {exported_path}")


def _to_trial_result(
    trial_number: int,
    experiment: ExperimentResult,
) -> TrialResult:
    return TrialResult(
        trial_number=trial_number,
        strategy_name=experiment.strategy_name,
        task=experiment.task,
        task_outcome=experiment.task_outcome,
        order_id=experiment.order_id,
        payment_ids=experiment.payment_ids,
        successful_charge_count=experiment.successful_charge_count,
        invariant_violations=experiment.invariant_violations,
        trace=experiment.trace,
    )


def _calculate_strategy_metrics(
    strategy_name: StrategyName,
    trials: tuple[TrialResult, ...],
) -> StrategyMetrics:
    strategy_trials = tuple(
        trial for trial in trials if trial.strategy_name == strategy_name
    )
    total_trials = len(strategy_trials)
    if total_trials == 0:
        raise ValueError(f"No trials supplied for strategy '{strategy_name}'")

    successful_trials = sum(
        trial.task_outcome == "passed" for trial in strategy_trials
    )
    violation_trials = sum(
        bool(trial.invariant_violations) for trial in strategy_trials
    )
    duplicate_charge_trials = sum(
        trial.successful_charge_count > 1 for trial in strategy_trials
    )
    return StrategyMetrics(
        strategy_name=strategy_name,
        total_trials=total_trials,
        task_success_rate=successful_trials / total_trials,
        invariant_violation_rate=violation_trials / total_trials,
        duplicate_charge_trial_rate=duplicate_charge_trials / total_trials,
        total_successful_charges=sum(
            trial.successful_charge_count for trial in strategy_trials
        ),
    )


if __name__ == "__main__":
    main()

