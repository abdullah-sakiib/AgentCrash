import json
import sqlite3
from pathlib import Path

import pytest

from agentcrash.experiments.benchmark_payment_recovery import (
    export_benchmark_json,
    format_benchmark,
    run_payment_timeout_benchmark,
)


async def test_runs_multiple_trials_for_each_strategy(tmp_path: Path) -> None:
    benchmark = await run_payment_timeout_benchmark(tmp_path, trial_count=3)

    assert benchmark.trials_per_strategy == 3
    assert len(benchmark.trials) == 6
    assert [trial.trial_number for trial in benchmark.trials_for("unsafe_retry")] == [
        1,
        2,
        3,
    ]
    assert [trial.trial_number for trial in benchmark.trials_for("status_check")] == [
        1,
        2,
        3,
    ]
    assert all(
        trial.successful_charge_count == 2
        for trial in benchmark.trials_for("unsafe_retry")
    )
    assert all(
        trial.successful_charge_count == 1
        for trial in benchmark.trials_for("status_check")
    )


async def test_every_trial_has_fresh_equivalent_state(tmp_path: Path) -> None:
    benchmark = await run_payment_timeout_benchmark(tmp_path, trial_count=2)
    initial_traces = [trial.trace[:3] for trial in benchmark.trials]

    assert all(trial.order_id == 1 for trial in benchmark.trials)
    assert all(trial.task == benchmark.trials[0].task for trial in benchmark.trials)
    assert all(trace == initial_traces[0] for trace in initial_traces)

    for trial_number in (1, 2):
        unsafe_database = (
            tmp_path / f"trial_{trial_number:03d}" / "unsafe_retry.db"
        )
        safe_database = tmp_path / f"trial_{trial_number:03d}" / "status_check.db"
        assert unsafe_database.exists()
        assert safe_database.exists()
        assert _row_counts(unsafe_database) == (1, 2)
        assert _row_counts(safe_database) == (1, 1)


async def test_aggregate_metrics_are_calculated_from_trial_results(
    tmp_path: Path,
) -> None:
    benchmark = await run_payment_timeout_benchmark(tmp_path, trial_count=3)
    unsafe = benchmark.metrics_for("unsafe_retry")
    safe = benchmark.metrics_for("status_check")

    assert unsafe.total_trials == 3
    assert unsafe.task_success_rate == 0.0
    assert unsafe.invariant_violation_rate == 1.0
    assert unsafe.duplicate_charge_trial_rate == 1.0
    assert unsafe.total_successful_charges == 6

    assert safe.total_trials == 3
    assert safe.task_success_rate == 1.0
    assert safe.invariant_violation_rate == 0.0
    assert safe.duplicate_charge_trial_rate == 0.0
    assert safe.total_successful_charges == 3

    display = format_benchmark(benchmark)
    assert "unsafe_retry | 3 | 0.0% | 100.0% | 100.0% | 6" in display
    assert "status_check | 3 | 100.0% | 0.0% | 0.0% | 3" in display


async def test_exports_complete_readable_json_to_requested_path(
    tmp_path: Path,
) -> None:
    benchmark = await run_payment_timeout_benchmark(
        tmp_path / "databases", trial_count=2
    )
    requested_path = tmp_path / "exports" / "benchmark.json"

    exported_path = export_benchmark_json(benchmark, requested_path)
    document = json.loads(exported_path.read_text(encoding="utf-8"))

    assert exported_path == requested_path.resolve()
    assert document["scenario"] == "payment_timeout_after_commit"
    assert document["trials_per_strategy"] == 2
    assert len(document["metrics"]) == 2
    assert len(document["trials"]) == 4
    assert document["trials"][0]["trial_number"] == 1
    assert document["trials"][0]["trace"][2]["http_status"] == 504


@pytest.mark.parametrize("trial_count", [0, -1])
async def test_rejects_non_positive_trial_counts(
    tmp_path: Path,
    trial_count: int,
) -> None:
    with pytest.raises(ValueError, match="trial_count must be greater than zero"):
        await run_payment_timeout_benchmark(tmp_path, trial_count=trial_count)
    assert list(tmp_path.iterdir()) == []


def _row_counts(database_path: Path) -> tuple[int, int]:
    with sqlite3.connect(database_path) as connection:
        order_count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        payment_count = connection.execute(
            "SELECT COUNT(*) FROM payment_transactions"
        ).fetchone()[0]
    return order_count, payment_count

