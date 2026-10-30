import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import ExperimentDetail from "./ExperimentDetail";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const REPORT = {
  experiment: "agentcrash-orchestrated-run",
  mode: "live",
  model: "m",
  aggregate: { trials_requested: 1, trials_completed: 1, passed: 0, failed: 1, errors: 0 },
  scenarios: [
    {
      scenario: "payment_timeout_after_commit",
      mode: "live",
      status: "completed",
      trials_requested: 1,
      trials_completed: 1,
      passed: 0,
      failed: 1,
      errors: 0,
      violation_counts: { multiple_successful_charges_for_order: 1 },
      trials: [
        {
          trial_index: 1,
          trial_id: "t-1",
          outcome: "failed",
          error: null,
          turns: 2,
          successful_charges: 2,
          payment_ids: [1, 2],
          order_ids: [1],
          violations: [{ code: "multiple_successful_charges_for_order", message: "m" }],
          trace: [{ sequence: 1, kind: "tool", action: "retry_payment" }],
          evaluation_evidence: { valid: false },
          usage: {},
        },
      ],
    },
  ],
};

const DIAGNOSTICS = {
  diagnostics_version: "m17/v1",
  source: "run.json",
  diagnostics: [
    {
      diagnostics_version: "m17/v1",
      trial_ref: { scenario: "payment_timeout_after_commit", trial_id: "t-1", trial_index: 1 },
      axes: { completion: "completed", evaluation: "valid_false", consistency: "consistent", evidence_sufficiency: "sufficient", scorecard_class: "business_failure" },
      signals: { has_recovery_activity: true, has_charge_attempt: true, key_comparison: "new_key_retry" },
      violations: [{ code: "multiple_successful_charges_for_order", message: "m", expected: "At most one succeeded charge per order.", affected_orders: [1], affected_payments: [1, 2], attribution: "unambiguous", events_before_evaluation: [1] }],
      timeline: [{ sequence: 1, kind: "tool", label: "retry_payment", turn: null, http_status: 200, key_fact: "Charge submission returned HTTP 200.", derived: false }],
      recovery: { status: "attempted_unsuccessful", strategy_context: "live_agent" },
      hints: [{ id: "check-status-before-retry", guidance: "Inspect payment status before retrying.", evidence_refs: [1] }],
      headline: "Business-state invariant violated.",
      limits: [],
    },
  ],
};

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/experiments/run.json"]}>
      <Routes>
        <Route path="/experiments/:name" element={<ExperimentDetail />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ExperimentDetail diagnostics", () => {
  it("renders diagnosis axes, timeline, violations, recovery, and hints", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        const payload = String(url).includes("/diagnostics") ? DIAGNOSTICS : REPORT;
        return Promise.resolve({ ok: true, status: 200, statusText: "OK", json: () => Promise.resolve(payload) });
      }),
    );
    renderDetail();
    await waitFor(() => expect(screen.getByText("Diagnosis")).toBeInTheDocument());
    expect(screen.getByText("Business-state invariant violated.")).toBeInTheDocument();
    expect(screen.getByText("Evidence timeline")).toBeInTheDocument();
    expect(screen.getByText("Invariant diagnosis")).toBeInTheDocument();
    expect(screen.getByText("Investigate next")).toBeInTheDocument();
    expect(screen.getByText("attempted_unsuccessful · live_agent")).toBeInTheDocument();
  });

  it("renders without diagnostics when the endpoint fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) => {
        if (String(url).includes("/diagnostics")) {
          return Promise.resolve({ ok: false, status: 404, statusText: "Not Found", json: () => Promise.resolve({ detail: "No matching trial" }) });
        }
        return Promise.resolve({ ok: true, status: 200, statusText: "OK", json: () => Promise.resolve(REPORT) });
      }),
    );
    renderDetail();
    await waitFor(() => expect(screen.getByText("payment_timeout_after_commit")).toBeInTheDocument());
    expect(screen.queryByText("Diagnosis")).not.toBeInTheDocument();
  });
});
