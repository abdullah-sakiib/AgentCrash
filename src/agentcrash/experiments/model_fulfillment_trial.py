"""Offline model-harness trial runner for the fulfillment scenario.

Uses fake or live model clients through the harness; the CLI demo uses
scripted fakes only (no network). Single isolated database per trial.
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
    ExperimentExecutionError,
    FulfillmentTask,
)
from agentcrash.harness import (
    FulfillmentHarnessResult,
    redact_api_key,
    run_model_fulfillment,
)
from agentcrash.providers.meta_responses import MetaResponsesConfig

SCENARIO = "fulfill_before_payment"
COST_NOTE = "cost_not_calculated (no reliable pricing data)"


@dataclass(frozen=True)
class FulfillmentTrialReport:
    scenario: str
    model: str
    task: FulfillmentTask
    task_outcome: str | None
    trial_error: str | None
    order_id: int | None
    order_status: str | None
    payment_ids: tuple[int, ...]
    successful_charge_count: int
    invariant_violations: tuple[dict[str, str], ...]
    turns_used: int
    usage: dict[str, Any]
    cost_note: str
    trace: tuple[dict[str, Any], ...]
    evaluation_evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _partial_trace(exc: BaseException) -> tuple[dict[str, Any], ...]:
    """Sanitized harness events recorded before the failure, if any."""
    partial = getattr(exc, "harness_trace", None)
    if not partial:
        return ()
    return tuple(
        asdict(event) if hasattr(event, "__dataclass_fields__") else dict(event)
        for event in partial
    )


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


async def run_fulfillment_trial(
    database_path: Path,
    model_client: Any,
    task: FulfillmentTask = DEFAULT_FULFILLMENT_TASK,
    *,
    max_turns: int = 6,
    model_name: str | None = None,
    config: MetaResponsesConfig | None = None,
) -> FulfillmentTrialReport:
    """Run one fulfillment harness trial in an isolated database."""
    if max_turns <= 0:
        raise ValueError("max_turns must be greater than zero.")
    database_path = database_path.resolve()
    if database_path.exists():
        raise ExperimentExecutionError(
            f"Refusing to reuse experiment database: {database_path}"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        live: FulfillmentHarnessResult | None = await run_model_fulfillment(
            database_path, model_client, task, max_turns=max_turns
        )
        trial_error: str | None = None
        error_trace: tuple[dict[str, Any], ...] = ()
    except Exception as exc:
        live = None
        trial_error = _redact_error(str(exc), config)
        error_trace = _partial_trace(exc)

    if live is None:
        api_key = config.api_key if config is not None else None
        return FulfillmentTrialReport(
            scenario=SCENARIO,
            model=model_name or "unknown",
            task=task,
            task_outcome=None,
            trial_error=trial_error,
            order_id=None,
            order_status=None,
            payment_ids=(),
            successful_charge_count=0,
            invariant_violations=(),
            turns_used=0,
            usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "responses": 0},
            cost_note=COST_NOTE,
            trace=tuple(
                redact_api_key(list((*error_trace, {"kind": "error", "error": trial_error})), api_key)
            ),
            evaluation_evidence={},
        )
    return FulfillmentTrialReport(
        scenario=SCENARIO,
        model=model_name or "unknown",
        task=task,
        task_outcome=live.task_outcome,
        trial_error=None,
        order_id=live.order_id,
        order_status=live.order_status,
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
        cost_note=COST_NOTE,
        trace=tuple(
            redact_api_key(
                [asdict(event) for event in live.trace],
                config.api_key if config is not None else None,
            )
        ),
        evaluation_evidence=dict(live.evaluation_evidence or {}),
    )


def format_fulfillment_trial(report: FulfillmentTrialReport) -> str:
    if report.trial_error is not None:
        status = f"ERROR | {report.trial_error}"
    else:
        outcome = "PASS" if report.task_outcome == "passed" else "FAIL"
        status = (
            f"{outcome} | status={report.order_status} | "
            f"successful_charges={report.successful_charge_count} | "
            f"turns={report.turns_used}"
        )
    lines = [
        "AgentCrash fulfillment model trial",
        f"scenario={report.scenario} model={report.model}",
        f"trial: {status}",
        (
            f"tokens: input={report.usage.get('input_tokens')} "
            f"output={report.usage.get('output_tokens')} "
            f"total={report.usage.get('total_tokens')} "
            f"responses={report.usage.get('responses')}"
        ),
        f"cost: {report.cost_note}",
    ]
    return "\n".join(lines)


def export_fulfillment_trial_json(
    report: FulfillmentTrialReport, output_path: Path
) -> Path:
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
        description="Run an offline fulfillment model trial with a scripted demo client."
    )
    parser.add_argument(
        "--demo",
        choices=("unsafe", "safe"),
        default="safe",
        help="scripted demo behavior (default: safe)",
    )
    parser.add_argument("--output", type=Path, help="optional path for the JSON report")
    parser.add_argument("--max-turns", type=int, default=6, help="harness turn limit")
    args = parser.parse_args(argv)
    if args.max_turns <= 0:
        parser.error("--max-turns must be greater than zero.")

    from agentcrash.providers.meta_responses import MetaResponse, MetaToolCall, MetaUsage

    def _tool(name: str, arguments: dict[str, Any], call_id: str) -> MetaResponse:
        return MetaResponse(
            text="",
            tool_calls=(MetaToolCall(name=name, arguments=arguments, call_id=call_id),),
            usage=MetaUsage(input_tokens=10, output_tokens=5, total_tokens=15, raw={}),
            model="demo",
            raw={
                "output": [
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": json.dumps(arguments),
                    }
                ]
            },
        )

    def _text(text: str) -> MetaResponse:
        return MetaResponse(
            text=text,
            tool_calls=(),
            usage=MetaUsage(input_tokens=10, output_tokens=5, total_tokens=15, raw={}),
            model="demo",
            raw={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ]
            },
        )

    if args.demo == "unsafe":
        script = [_tool("fulfill_order", {"order_id": 1}, "c1"), _text("done")]
    else:
        script = [
            _tool("charge_payment", {"order_id": 1, "idempotency_key": "demo-key"}, "c1"),
            _tool("fulfill_order", {"order_id": 1}, "c2"),
            _text("done"),
        ]

    class _DemoClient:
        def __init__(self) -> None:
            self._script = list(script)

        async def generate(self, user_input, *, tools=None, tool_choice=None, model_override=None):
            assert self._script, "Demo script exhausted."
            return self._script.pop(0)

    async def _run() -> FulfillmentTrialReport:
        with TemporaryDirectory(prefix="agentcrash-fulfillment-trial-") as directory:
            return await run_fulfillment_trial(
                Path(directory) / "trial.db",
                _DemoClient(),
                DEFAULT_FULFILLMENT_TASK,
                max_turns=args.max_turns,
                model_name="demo-scripted",
            )

    report = asyncio.run(_run())
    print(format_fulfillment_trial(report))
    if args.output is not None:
        exported = export_fulfillment_trial_json(report, args.output)
        print(f"JSON report: {exported}")


if __name__ == "__main__":
    main()
