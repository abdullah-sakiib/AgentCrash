import { describe, expect, it, vi, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Dashboard from "./Dashboard";

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
  total: 3,
  limit: 100,
  offset: 0,
  entries: [
    { name: "b.json", size_bytes: 100, mtime: "2026-10-30T00:00:00+00:00", kind: "orchestrated_run", supported: true },
    { name: "a.json", size_bytes: 50, mtime: "2026-10-29T00:00:00+00:00", kind: "orchestrated_run", supported: true },
    { name: "notes.md", size_bytes: 10, mtime: "2026-10-28T00:00:00+00:00", kind: "markdown", supported: false },
  ],
};

describe("Dashboard", () => {
  it("renders counts and recent supported reports", async () => {
    mockFetchOnce(LIST);
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText("Dashboard")).toBeInTheDocument());
    expect(screen.getByText(/3 reports on disk/)).toBeInTheDocument();
    expect(screen.getByText(/2 supported by/)).toBeInTheDocument();
    expect(screen.getByText("b.json")).toBeInTheDocument();
    expect(screen.queryByText("notes.md")).not.toBeInTheDocument();
  });

  it("shows an empty state that points at the offline demo", async () => {
    mockFetchOnce({ total: 0, limit: 100, offset: 0, entries: [] });
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByText(/demo\.ps1/)).toBeInTheDocument(),
    );
  });

  it("explains what AgentCrash measures", async () => {
    mockFetchOnce(LIST);
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByText("What AgentCrash measures")).toBeInTheDocument(),
    );
    expect(screen.getByText(/persisted business state/)).toBeInTheDocument();
  });

  it("shows an error state with retry when the API is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new Error("connection refused")),
    );
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(/Cannot reach the API server/),
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows a loading state before the response arrives", () => {
    mockFetchOnce(new Promise(() => {}));
    render(
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>,
    );
    expect(screen.getByRole("status")).toHaveTextContent(/Loading dashboard/);
  });
});
