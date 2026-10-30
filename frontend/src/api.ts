import { ApiError, Health, ReportDocument, ReportList, ScenarioInfo } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, `Cannot reach the API server (${path}). Is the backend running?`);
  }
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail) message = body.detail;
    } catch {
      // Fall through with the HTTP status text.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export function fetchHealth(): Promise<Health> {
  return request<Health>("/api/health");
}

export function fetchScenarios(): Promise<{ scenarios: ScenarioInfo[] }> {
  return request<{ scenarios: ScenarioInfo[] }>("/api/scenarios");
}

export function fetchReports(limit = 100, offset = 0): Promise<ReportList> {
  return request<ReportList>(
    `/api/reports?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`,
  );
}

export function fetchReport(name: string): Promise<ReportDocument> {
  return request<ReportDocument>(`/api/reports/${encodeURIComponent(name)}`);
}

export interface DiagnosticDocument extends Record<string, unknown> {
  diagnostics_version: string;
  source?: string;
  diagnostics?: Record<string, unknown>[];
  trial_ref?: Record<string, unknown>;
}

export function fetchDiagnostics(name: string): Promise<DiagnosticDocument> {
  return request<DiagnosticDocument>(
    `/api/reports/${encodeURIComponent(name)}/diagnostics`,
  );
}

export function fetchTrialDiagnostic(
  name: string,
  params?: { scenario?: string; trial_id?: string; trial_index?: number },
): Promise<Record<string, unknown>> {
  const query = new URLSearchParams();
  if (params?.scenario) query.set("scenario", params.scenario);
  if (params?.trial_id) query.set("trial_id", params.trial_id);
  if (params?.trial_index !== undefined) query.set("trial_index", String(params.trial_index));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return request<Record<string, unknown>>(
    `/api/reports/${encodeURIComponent(name)}/diagnostics${suffix}`,
  );
}

export function fetchScorecard(
  name: string,
  strategyId?: string,
): Promise<ScorecardDocument> {
  const params = strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : "";
  return request<ScorecardDocument>(
    `/api/reports/${encodeURIComponent(name)}/scorecard${params}`,
  );
}

export function fetchComparison(
  baseline: string,
  candidate: string,
): Promise<ComparisonDocument> {
  return request<ComparisonDocument>(
    `/api/comparisons?baseline=${encodeURIComponent(baseline)}&candidate=${encodeURIComponent(candidate)}`,
  );
}

export function fetchSeries(
  baseline: string,
  members: string[],
  strategyId?: string,
  strategyIds?: Record<string, string>,
): Promise<SeriesTrendDocument> {
  return request<SeriesTrendDocument>("/api/series", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      baseline,
      members,
      ...(strategyId ? { strategy_id: strategyId } : {}),
      ...(strategyIds ? { strategy_ids: strategyIds } : {}),
    }),
  });
}

export interface RateRow {
  value: number | null;
  numerator: number | null;
  denominator: number | null;
  definition: string;
  applicable?: boolean;
  [key: string]: unknown;
}

export interface ScorecardScope extends Record<string, unknown> {
  scenario: string;
  reported_as?: string | null;
  fault: unknown;
  trial_counts: Record<string, unknown>;
  usage: { status?: string } & Record<string, unknown>;
  violations_by_code: Record<string, number>;
  cost: string;
}

export interface ScorecardDocument {
  scorecard_version: number;
  scope: { scenarios: string[]; modes: string[]; model: unknown; strategy_id: unknown };
  scopes: ScorecardScope[];
  overall: Record<string, RateRow>;
  cost_note: string;
  generated_from: Record<string, unknown>;
}

export interface MetricDeltaRow {
  baseline: unknown;
  candidate: unknown;
  delta: number | null;
  direction: string;
  reason: string | null;
  better_direction: string;
  definition: string;
  baseline_fraction?: string | null;
  candidate_fraction?: string | null;
  applicable?: boolean;
  [key: string]: unknown;
}

export interface ComparisonDocument {
  scorecard_comparison_version: number;
  compatible: boolean;
  reasons: string[];
  baseline: Record<string, unknown>;
  candidate: Record<string, unknown>;
  overall: Record<string, MetricDeltaRow>;
  scenarios: Record<
    string,
    {
      fault: { baseline: unknown; candidate: unknown };
      trial_counts: { baseline: unknown; candidate: unknown };
      reported_as: { baseline: unknown; candidate: unknown };
      metrics: Record<string, MetricDeltaRow>;
      violations_by_code: Record<
        string,
        { baseline: number; candidate: number; delta: number; direction: string }
      >;
    }
  >;
  violations_by_code: Record<
    string,
    { baseline: number; candidate: number; delta: number; direction: string }
  >;
  regressions_present: boolean;
  sample_size_warning: boolean;
  usage: Record<string, unknown>;
  cost: string;
  note: string;
}

export interface SeriesMemberEntry {
  source: string | null;
  eligible: boolean;
  reasons: string[];
  scorecard: {
    recovery: {
      value: number | null;
      numerator: number | null;
      denominator: number | null;
      fraction: string | null;
    };
    violations: Record<string, number>;
    trials_evaluable: number | null;
  } | null;
  comparison_vs_baseline: ComparisonDocument | null;
}

export interface SeriesTrendDocument {
  series_trend_version: number;
  baseline: Record<string, unknown>;
  members: SeriesMemberEntry[];
  series: {
    member_count: number;
    eligible_count: number;
    recovery_values: {
      source: string | null;
      value: number | null;
      numerator: number | null;
      denominator: number | null;
      fraction: string | null;
    }[];
    recovery_range: {
      min: number | null;
      max: number | null;
      span: number | null;
      n_values: number;
      denominators_equal: boolean | null;
      member_fractions: {
        source: string | null;
        fraction: string | null;
        numerator: number | null;
        denominator: number | null;
      }[];
      reason: string | null;
      note: string | null;
      definition: string;
      better_direction: string;
    };
    recurring_violations: Record<
      string,
      {
        members_affected: number;
        total_occurrences: number;
        member_sources: string[];
        per_member: {
          source: string | null;
          occurrences: number;
          evaluable_trials: number | null;
        }[];
        definition: string;
      }
    >;
    members_regressed_vs_baseline: string[];
    regressions_present: boolean;
    members_with_sample_warning: string[];
    members_with_unknown_recovery: string[];
  };
  warnings: string[];
  cost: string;
  note: string;
}
