import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import CompareView from "./CompareView";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const LIST = {
  total: 2,
  limit: 100,
  offset: 0,
  entries: [
    { name: "b.json", size_bytes: 10, mtime: "2026-10-30T00:00:00+00:00", kind: "orchestrated_run", supported: true },
    { name: "c.json", size_bytes: 10, mtime: "2026-10-29T00:00:00+00:00", kind: "orchestrated_run", supported: true },
  ],
};

function metricRow(baseline: unknown, candidate: unknown, delta: number | null, direction: string, extra = {}) {
  return {
    baseline,
    candidate,
    delta,
    direction,
    reason: direction === "unavailable" ? "insufficient evidence" : null,
    better_direction: "higher",
    definition: "test definition",
    baseline_fraction: "1/2",
    candidate_fraction: "2/2",
    ...extra,
  };
}

const COMPATIBLE = {
  scorecard_comparison_version: 1,
  compatible: true,
  reasons: [],
  baseline: { source: "b.json" },
  candidate: { source: "c.json" },
  overall: {
    recovery_success_rate: metricRow(0.5, 1.0, 0.5, "improved"),
    duplicate_order_trial_rate: {
      ...metricRow(null, null, null, "unavailable"),
      applicable: false,
    },
    evidence_coverage: {
      ...metricRow(0.5, 1.0, 0.5, "informational-increase"),
      better_direction: "informational",
      definition: "evaluable trials over completed trial records",
    },
    outcome_consistency: {
      baseline: false,
      candidate: true,
      baseline_fraction: "mixed [business_failure, recovered] · 2 eligible",
      candidate_fraction: "consistent · 2 eligible",
      transition: "false->true",
      delta: null,
      direction: "improved",
      reason: null,
      better_direction: "informational",
      definition: "whether all eligible trials share one outcome class",
      baseline_eligible_trials: 2,
      candidate_eligible_trials: 2,
    },
    recovery_efficiency: {
      baseline: 5,
      candidate: 2,
      delta: -3,
      direction: "informational-decrease",
      reason: null,
      better_direction: "informational",
      definition: "median turns among recovered trials",
      baseline_recovered_trials: 2,
      candidate_recovered_trials: 2,
    },
  },
  scenarios: {
    payment_timeout_after_commit: {
      fault: { baseline: { fault_type: "timeout" }, candidate: { fault_type: "timeout" } },
      trial_counts: { baseline: {}, candidate: {} },
      reported_as: { baseline: null, candidate: null },
      metrics: {
        recovery_success_rate: metricRow(0.0, 1.0, 1.0, "regressed"),
      },
      violations_by_code: {
        multiple_successful_charges_for_order: { baseline: 1, candidate: 0, delta: -1, direction: "improved" },
      },
    },
  },
  violations_by_code: {},
  regressions_present: true,
  sample_size_warning: true,
  usage: { comparable: false, baseline_status: "complete", candidate_status: "missing" },
  cost: "cost_not_calculated (no reliable pricing data)",
  note: "Numeric differences only.",
};

const INCOMPATIBLE = {
  ...COMPATIBLE,
  compatible: false,
  reasons: ["scenario sets differ: ['a'] vs ['b']"],
  overall: {},
  scenarios: {},
};

function mockFetchFor(payloadByPath: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      for (const [key, payload] of Object.entries(payloadByPath)) {
        if (url.includes(key)) {
          return Promise.resolve({ ok: true, status: 200, statusText: "OK", json: () => Promise.resolve(payload) });
        }
      }
      return Promise.resolve({ ok: false, status: 404, statusText: "Missing", json: () => Promise.resolve({ detail: "missing" }) });
    }),
  );
}

function renderView() {
  return render(
    <MemoryRouter>
      <CompareView />
    </MemoryRouter>,
  );
}

describe("CompareView", () => {
  it("renders compatible comparison with regression and sample warnings", async () => {
    mockFetchFor({ "/api/reports?": LIST, "/api/comparisons?": COMPATIBLE });
    renderView();
    await waitFor(() => expect(screen.getByText("Compare scorecards")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Baseline"), { target: { value: "b.json" } });
    fireEvent.change(screen.getByLabelText("Candidate"), { target: { value: "c.json" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(screen.getByText(/Regression warning/)).toBeInTheDocument());
    expect(screen.getByText(/Sample-size warning/)).toBeInTheDocument();
    expect(screen.getAllByText("improved").length).toBeGreaterThan(0);
    expect(screen.getByText(/not applicable/)).toBeInTheDocument();
    expect(screen.getByText(/multiple_successful_charges_for_order/)).toBeInTheDocument();
    expect(screen.queryByText(/winner/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/ranking/i)).not.toBeInTheDocument();
  });

  it("renders incompatibility reasons without deltas", async () => {
    mockFetchFor({ "/api/reports?": LIST, "/api/comparisons?": INCOMPATIBLE });
    renderView();
    await waitFor(() => expect(screen.getByText("Compare scorecards")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Baseline"), { target: { value: "b.json" } });
    fireEvent.change(screen.getByLabelText("Candidate"), { target: { value: "c.json" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() =>
      expect(screen.getByText(/no comparison possible/i)).toBeInTheDocument(),
    );
    expect(screen.getByText(/scenario sets differ/)).toBeInTheDocument();
  });

  it("renders depth rows with transitions and no verdict language", async () => {
    mockFetchFor({ "/api/reports?": LIST, "/api/comparisons?": COMPATIBLE });
    renderView();
    await waitFor(() => expect(screen.getByText("Compare scorecards")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Baseline"), { target: { value: "b.json" } });
    fireEvent.change(screen.getByLabelText("Candidate"), { target: { value: "c.json" } });
    fireEvent.click(screen.getByRole("button", { name: "Compare" }));
    await waitFor(() => expect(screen.getByText("evidence_coverage")).toBeInTheDocument());
    expect(screen.getByText("outcome_consistency")).toBeInTheDocument();
    expect(screen.getByText("recovery_efficiency")).toBeInTheDocument();
    expect(screen.getByText(/consistent · 2 eligible/)).toBeInTheDocument();
    expect(screen.queryByText(/winner/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/ranking/i)).not.toBeInTheDocument();
  });
});
