import { MetricDeltaRow, RateRow } from "../api";

/** Display order only — all labels, values, and verdicts come from the backend. */
export const RATE_ORDER = [
  "recovery_success_rate",
  "task_pass_rate",
  "invariant_violation_rate",
  "infrastructure_error_rate",
  "duplicate_charge_trial_rate",
  "duplicate_order_trial_rate",
  "evidence_coverage",
];

export function isRateRow(value: unknown): value is RateRow {
  if (typeof value !== "object" || value === null) return false;
  const row = value as Record<string, unknown>;
  return "value" in row && "numerator" in row && "denominator" in row;
}

export function formatMetricValue(value: unknown): string {
  if (value === null || value === undefined) return "unknown";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  return String(value);
}

export function formatDelta(delta: number | null | unknown): string {
  if (typeof delta !== "number") return "n/a";
  const sign = delta > 0 ? "+" : "";
  return `${sign}${Number.isInteger(delta) ? delta : delta.toFixed(3)}`;
}

export function MetricState({ row }: { row: RateRow | MetricDeltaRow }) {
  if ("applicable" in row && row.applicable === false) {
    return <span className="chip chip-info">not applicable</span>;
  }
  const value = "value" in row ? row.value : undefined;
  if (value === null || value === undefined) {
    return <span className="chip chip-warn">unknown</span>;
  }
  return null;
}
