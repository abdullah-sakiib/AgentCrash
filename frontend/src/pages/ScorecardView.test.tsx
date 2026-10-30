import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ScorecardView from "./ScorecardView";

function mockFetchOnce(payload: unknown, ok = true, status = 200) {
  const mock = vi.fn().mockResolvedValue({
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: () => Promise.resolve(payload),
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const DOC = {
  scorecard_version: 1,
  scope: { scenarios: ["payment_timeout_after_commit"], modes: ["live"], model: "m", strategy_id: "s" },
  overall: {
    recovery_success_rate: {
      value: 0.5,
      numerator: 1,
      denominator: 2,
      definition: "passed + evaluated + completion-evidenced trials over evaluable trials",
    },
    duplicate_charge_trial_rate: {
      value: null,
      numerator: null,
      denominator: null,
      definition: "trials with duplicate-charge evidence",
      applicable: false,
      total_duplicate_charges: null,
    },
    evidence_coverage: {
      value: 1.0,
      numerator: 2,
      denominator: 2,
      reason: null,
      definition: "evaluable trials over completed trial records",
    },
    outcome_consistency: {
      consistent: true,
      classes: ["recovered"],
      eligible_trials: 2,
      reason: null,
      definition: "whether all eligible trials share one outcome class",
    },
    recovery_efficiency: {
      median_turns_to_recovery: 3,
      recovered_trials: 2,
      recovered_without_turn_data: 0,
      reason: null,
      definition: "median turns among recovered trials",
    },
  },
  scopes: [
    {
      scenario: "payment_timeout_after_commit",
      reported_as: null,
      fault: { fault_type: "timeout" },
      trial_counts: { requested: 2, completed: 2, evaluable: 2, errored: 0, unknown_completion: 0, inconsistent_evidence: 0 },
      recovery_success_rate: {
        value: 1.0,
        numerator: 2,
        denominator: 2,
        definition: "passed + evaluated + completion-evidenced trials",
      },
      evidence_coverage: {
        value: 1.0,
        numerator: 2,
        denominator: 2,
        reason: null,
        definition: "evaluable trials over completed trial records",
      },
      outcome_consistency: {
        consistent: false,
        classes: ["business_failure", "recovered"],
        eligible_trials: 2,
        reason: null,
        definition: "whether all eligible trials share one outcome class",
      },
      recovery_efficiency: {
        median_turns_to_recovery: null,
        recovered_trials: 1,
        recovered_without_turn_data: 1,
        reason: "no recovered trials with recorded turn counts",
        definition: "median turns among recovered trials",
      },
      usage: { status: "complete" },
      violations_by_code: {},
      cost: "cost_not_calculated (no reliable pricing data)",
    },
  ],
  cost_note: "cost_not_calculated (no reliable pricing data)",
  generated_from: { source: "r.json", input_kind: "orchestrated_run", scorecard_module: "agentcrash.experiments.scorecard" },
};

function renderView() {
  return render(
    <MemoryRouter initialEntries={["/scorecards/r.json"]}>
      <Routes>
        <Route path="/scorecards/:name" element={<ScorecardView />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ScorecardView", () => {
  it("renders metric values with fractions and definitions", async () => {
    mockFetchOnce(DOC);
    renderView();
    await waitFor(() => expect(screen.getByText("Overall")).toBeInTheDocument());
    expect(screen.getByText("1/2")).toBeInTheDocument();
    expect(screen.getAllByText(/passed \+ evaluated/).length).toBeGreaterThan(0);
  });

  it("preserves null and non-applicable states distinctly", async () => {
    mockFetchOnce(DOC);
    renderView();
    await waitFor(() => expect(screen.getByText("Overall")).toBeInTheDocument());
    expect(screen.getByText("not applicable")).toBeInTheDocument();
    expect(screen.queryByText("0/0")).not.toBeInTheDocument();
  });

  it("renders reliability depth with informational labeling", async () => {
    mockFetchOnce(DOC);
    renderView();
    await waitFor(() => expect(screen.getByText("Overall")).toBeInTheDocument());
    expect(screen.getAllByText("Reliability depth (informational)").length).toBeGreaterThan(0);
    expect(screen.getByText("consistent")).toBeInTheDocument();
    expect(screen.getByText("mixed")).toBeInTheDocument();
    // Coverage is a rate row with a fraction in the metrics tables.
    expect(screen.getAllByText("2/2").length).toBeGreaterThan(0);
    // No winner or verdict language is attached to depth metrics.
    expect(screen.queryByText(/winner/i)).not.toBeInTheDocument();
  });

  it("shows unknown depth states without fabricating values", async () => {
    mockFetchOnce(DOC);
    renderView();
    await waitFor(() => expect(screen.getByText("Overall")).toBeInTheDocument());
    expect(
      screen.getByText("no recovered trials with recorded turn counts"),
    ).toBeInTheDocument();
  });

  it("renders v1 scorecards without depth fields", async () => {
    const legacy = JSON.parse(JSON.stringify(DOC));
    delete legacy.overall.evidence_coverage;
    delete legacy.overall.outcome_consistency;
    delete legacy.overall.recovery_efficiency;
    for (const scope of legacy.scopes) {
      delete scope.evidence_coverage;
      delete scope.outcome_consistency;
      delete scope.recovery_efficiency;
    }
    mockFetchOnce(legacy);
    renderView();
    await waitFor(() => expect(screen.getByText("Overall")).toBeInTheDocument());
    expect(screen.queryByText("Reliability depth (informational)")).not.toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
  });

  it("shows an error for unscorable artifacts", async () => {
    mockFetchOnce({ detail: "Artifact 'x.json' cannot be scored." }, false, 422);
    renderView();
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/cannot be scored/),
    );
  });

  it("shows a loading state first", () => {
    mockFetchOnce(new Promise(() => {}));
    renderView();
    expect(screen.getByRole("status")).toHaveTextContent(/Loading scorecard/);
  });
});
