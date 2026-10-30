import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import SeriesView from "./SeriesView";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const LIST = {
  total: 3,
  limit: 100,
  offset: 0,
  entries: [
    { name: "b.json", size_bytes: 10, mtime: "2026-10-30T00:00:00+00:00", kind: "orchestrated_run", supported: true },
    { name: "m1.json", size_bytes: 10, mtime: "2026-10-29T00:00:00+00:00", kind: "orchestrated_run", supported: true },
    { name: "m2.json", size_bytes: 10, mtime: "2026-10-28T00:00:00+00:00", kind: "orchestrated_run", supported: true },
  ],
};

const SERIES = {
  series_trend_version: 1,
  baseline: { source: "b.json", strategy_id: "s", model: "m", modes: ["live"] },
  members: [
    {
      source: "m1.json",
      eligible: true,
      reasons: [],
      scorecard: {
        recovery: { value: 1.0, numerator: 2, denominator: 2, fraction: "2/2" },
        violations: { multiple_successful_charges_for_order: 1 },
        trials_evaluable: 2,
      },
      comparison_vs_baseline: { compatible: true },
    },
    {
      source: "m2.json",
      eligible: false,
      reasons: ["unconfirmed configuration: strategy_id missing on baseline or member"],
      scorecard: {
        recovery: { value: 0.5, numerator: 1, denominator: 2, fraction: "1/2" },
        violations: {},
        trials_evaluable: 2,
      },
      comparison_vs_baseline: null,
    },
  ],
  series: {
    member_count: 2,
    eligible_count: 1,
    recovery_values: [
      { source: "m1.json", value: 1.0, numerator: 2, denominator: 2, fraction: "2/2" },
    ],
    recovery_range: {
      min: null,
      max: null,
      span: null,
      n_values: 1,
      denominators_equal: null,
      member_fractions: [{ source: "m1.json", fraction: "2/2", numerator: 2, denominator: 2 }],
      reason: "fewer than 2 known recovery values",
      note: null,
      definition: "max minus min of member overall recovery values",
      better_direction: "informational",
    },
    recurring_violations: {},
    members_regressed_vs_baseline: [],
    regressions_present: false,
    members_with_sample_warning: ["m1.json"],
    members_with_unknown_recovery: [],
  },
  warnings: ["1 member(s) excluded: see member reasons"],
  cost: "cost_not_calculated (no reliable pricing data)",
  note: "Descriptive only.",
};

function mockFetchFor(payloadByPath: Record<string, unknown>, capture?: { body?: string }) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string, init?: RequestInit) => {
      if (capture && url.includes("/api/series")) {
        capture.body = typeof init?.body === "string" ? init.body : "";
      }
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
      <SeriesView />
    </MemoryRouter>,
  );
}

function selectBaselineAndMember() {
  fireEvent.change(screen.getByLabelText("Baseline"), { target: { value: "b.json" } });
  fireEvent.click(screen.getByLabelText(/m1\.json/));
  fireEvent.change(screen.getByLabelText("Strategy ID"), { target: { value: "s" } });
}

describe("SeriesView", () => {
  it("submits baseline, members, and strategy id", async () => {
    const capture: { body?: string } = {};
    mockFetchFor({ "/api/reports?": LIST, "/api/series": SERIES }, capture);
    renderView();
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    selectBaselineAndMember();
    fireEvent.click(screen.getByRole("button", { name: "Build series" }));
    await waitFor(() => expect(screen.getByText("Recovery range (informational)")).toBeInTheDocument());
    const body = JSON.parse(capture.body ?? "{}");
    expect(body.baseline).toBe("b.json");
    expect(body.members).toEqual(["m1.json"]);
    expect(body.strategy_id).toBe("s");
  });

  it("renders eligible and excluded members with reasons", async () => {
    mockFetchFor({ "/api/reports?": LIST, "/api/series": SERIES });
    renderView();
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    selectBaselineAndMember();
    fireEvent.click(screen.getByLabelText(/m2\.json/));
    fireEvent.click(screen.getByRole("button", { name: "Build series" }));
    await waitFor(() => expect(screen.getByText("eligible")).toBeInTheDocument());
    expect(screen.getByText("excluded")).toBeInTheDocument();
    expect(screen.getByText(/unconfirmed configuration/)).toBeInTheDocument();
  });

  it("renders the unequal-denominator note when denominators differ", async () => {
    const unequal = {
      ...SERIES,
      series: {
        ...SERIES.series,
        recovery_range: {
          ...SERIES.series.recovery_range,
          min: 0.5,
          max: 1.0,
          span: 0.5,
          n_values: 2,
          denominators_equal: false,
          reason: null,
          note: "unweighted spread; denominators differ",
        },
      },
    };
    mockFetchFor({ "/api/reports?": LIST, "/api/series": unequal });
    renderView();
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    selectBaselineAndMember();
    fireEvent.click(screen.getByRole("button", { name: "Build series" }));
    await waitFor(() => expect(screen.getByText(/unweighted spread; denominators differ/)).toBeInTheDocument());
    expect(screen.queryByText(/fewer than 2 known recovery values/)).not.toBeInTheDocument();
  });

  it("renders the unknown-range reason when fewer than 2 values exist", async () => {
    mockFetchFor({ "/api/reports?": LIST, "/api/series": SERIES });
    renderView();
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Baseline"), { target: { value: "b.json" } });
    fireEvent.click(screen.getByLabelText(/m1\.json/));
    fireEvent.change(screen.getByLabelText("Strategy ID"), { target: { value: "s" } });
    fireEvent.click(screen.getByRole("button", { name: "Build series" }));
    await waitFor(() => expect(screen.getByText(/fewer than 2 known recovery values/)).toBeInTheDocument());
  });

  it("renders recurring violations with member context", async () => {
    const recurring = {
      ...SERIES,
      series: {
        ...SERIES.series,
        recurring_violations: {
          multiple_successful_charges_for_order: {
            members_affected: 2,
            total_occurrences: 3,
            member_sources: ["m1.json", "m3.json"],
            per_member: [
              { source: "m1.json", occurrences: 2, evaluable_trials: 2 },
              { source: "m3.json", occurrences: 1, evaluable_trials: 1 },
            ],
            definition: "violation observed in at least two eligible members",
          },
        },
      },
    };
    mockFetchFor({ "/api/reports?": LIST, "/api/series": recurring });
    renderView();
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    selectBaselineAndMember();
    fireEvent.click(screen.getByRole("button", { name: "Build series" }));
    await waitFor(() =>
      expect(screen.getByText("multiple_successful_charges_for_order")).toBeInTheDocument(),
    );
    expect(screen.getByText(/2 members affected/)).toBeInTheDocument();
    expect(screen.getByText(/m3\.json: 1 occurrence\(s\), 1 evaluable trials/)).toBeInTheDocument();
  });

  it("attributes regressions to members without winner language", async () => {
    const regressed = {
      ...SERIES,
      series: {
        ...SERIES.series,
        members_regressed_vs_baseline: ["m1.json"],
        regressions_present: true,
      },
    };
    mockFetchFor({ "/api/reports?": LIST, "/api/series": regressed });
    renderView();
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    selectBaselineAndMember();
    fireEvent.click(screen.getByRole("button", { name: "Build series" }));
    await waitFor(() => expect(screen.getByText(/Regression warning/)).toBeInTheDocument());
    expect(screen.getAllByText("m1.json").length).toBeGreaterThan(0);
    expect(screen.queryByText(/winner/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/ranking/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/statistical/i)).not.toBeInTheDocument();
  });

  it("shows empty and error states", async () => {
    mockFetchFor({ "/api/reports?": LIST, "/api/series": SERIES });
    renderView();
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    expect(screen.getByText(/Select a baseline, tick member reports/)).toBeInTheDocument();
    cleanup();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        url.includes("/api/reports?")
          ? Promise.resolve({ ok: true, status: 200, statusText: "OK", json: () => Promise.resolve(LIST) })
          : Promise.resolve({ ok: false, status: 500, statusText: "Error", json: () => Promise.resolve({ detail: "boom" }) }),
      ),
    );
    render(
      <MemoryRouter>
        <SeriesView />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Reliability series")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Baseline"), { target: { value: "b.json" } });
    fireEvent.click(screen.getByLabelText(/m1\.json/));
    fireEvent.change(screen.getByLabelText("Strategy ID"), { target: { value: "s" } });
    fireEvent.click(screen.getByRole("button", { name: "Build series" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/boom/));
  });
});
