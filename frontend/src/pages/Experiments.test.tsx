import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import Experiments from "./Experiments";
import ExperimentDetail from "./ExperimentDetail";

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

const LIST = {
  total: 2,
  limit: 100,
  offset: 0,
  entries: [
    { name: "run.json", size_bytes: 120, mtime: "2026-10-30T00:00:00+00:00", kind: "orchestrated_run", supported: true },
    { name: "notes.md", size_bytes: 9, mtime: "2026-10-29T00:00:00+00:00", kind: "markdown", supported: false },
  ],
};

const REPORT = {
  experiment: "agentcrash-orchestrated-run",
  mode: "deterministic",
  model: null,
  aggregate: {
    trials_requested: 2,
    trials_completed: 2,
    passed: 1,
    failed: 1,
    errors: 0,
  },
  scenarios: [
    {
      scenario: "payment_timeout_after_commit",
      mode: "deterministic",
      status: "completed",
      trials_requested: 2,
      trials_completed: 2,
      passed: 1,
      failed: 1,
      errors: 0,
      violation_counts: { multiple_successful_charges_for_order: 1 },
      trials: [
        {
          trial_index: 1,
          trial_id: "t-1",
          outcome: "passed",
          error: null,
          turns: 2,
          successful_charges: 1,
          payment_ids: [1],
          order_ids: [],
          violations: [],
          trace: [{ sequence: 1, kind: "setup", detail: "set_inventory" }],
          evaluation_evidence: { valid: true },
          usage: {},
        },
      ],
    },
  ],
};

describe("Experiments", () => {
  it("lists reports with kind and supported status", async () => {
    mockFetchOnce(LIST);
    render(
      <MemoryRouter>
        <Experiments />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("run.json")).toBeInTheDocument());
    expect(screen.getByText("orchestrated_run")).toBeInTheDocument();
    expect(screen.getByText("unsupported")).toBeInTheDocument();
    expect(screen.getByText("notes.md")).toBeInTheDocument();
  });

  it("shows an empty state that points at the offline demo", async () => {
    mockFetchOnce({ total: 0, limit: 100, offset: 0, entries: [] });
    render(
      <MemoryRouter>
        <Experiments />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByText(/demo\.ps1/)).toBeInTheDocument(),
    );
  });

  it("links to the comparison view", async () => {
    mockFetchOnce(LIST);
    render(
      <MemoryRouter>
        <Experiments />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByRole("link", { name: /Compare two reports/ })).toHaveAttribute(
        "href",
        "/compare",
      ),
    );
  });

  it("navigates to the report detail page", async () => {
    const listResponse = {
      ok: true,
      status: 200,
      statusText: "OK",
      json: () => Promise.resolve(LIST),
    };
    const reportResponse = {
      ok: true,
      status: 200,
      statusText: "OK",
      json: () => Promise.resolve(REPORT),
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((url: string) =>
        Promise.resolve(url.includes("/api/reports?") ? listResponse : reportResponse),
      ),
    );
    render(
      <MemoryRouter initialEntries={["/experiments"]}>
        <Routes>
          <Route path="/experiments" element={<Experiments />} />
          <Route path="/experiments/:name" element={<ExperimentDetail />} />
        </Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("run.json")).toBeInTheDocument());
    fireEvent.click(screen.getByText("run.json"));
    await waitFor(() =>
      expect(screen.getByText("payment_timeout_after_commit")).toBeInTheDocument(),
    );
  });
});

describe("ExperimentDetail", () => {
  function renderDetail() {
    return render(
      <MemoryRouter initialEntries={["/experiments/run.json"]}>
        <Routes>
          <Route path="/experiments/:name" element={<ExperimentDetail />} />
        </Routes>
      </MemoryRouter>,
    );
  }

  it("renders aggregate, scenario, and trial evidence", async () => {
    mockFetchOnce(REPORT);
    renderDetail();
    await waitFor(() =>
      expect(screen.getByText("payment_timeout_after_commit")).toBeInTheDocument(),
    );
    expect(screen.getByText(/multiple_successful_charges_for_order/)).toBeInTheDocument();
    expect(screen.getAllByText("PASS").length).toBeGreaterThan(0);
  });

  it("shows an error for unsupported reports", async () => {
    mockFetchOnce({ detail: "Artifact 'notes.md' is not a supported report." }, false, 404);
    renderDetail();
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/not a supported report/),
    );
  });

  it("shows a loading state first", () => {
    mockFetchOnce(new Promise(() => {}));
    renderDetail();
    expect(screen.getByRole("status")).toHaveTextContent(/Loading report/);
  });
});
