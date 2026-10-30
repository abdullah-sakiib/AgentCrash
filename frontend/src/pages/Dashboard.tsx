import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchReports } from "../api";
import { ReportEntry } from "../types";
import StatusChip from "../components/StatusChip";
import { Empty, LoadError, Loading } from "../components/StateViews";

function kindChip(entry: ReportEntry) {
  return entry.supported ? (
    <StatusChip kind="supported" label={entry.kind} />
  ) : (
    <StatusChip kind="unsupported" label={entry.kind} />
  );
}

export default function Dashboard() {
  const [entries, setEntries] = useState<ReportEntry[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchReports(100, 0)
      .then((list) => {
        if (cancelled) return;
        setEntries(list.entries);
        setTotal(list.total);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <LoadError message={error} onRetry={() => window.location.reload()} />;
  if (entries === null) return <Loading what="dashboard" />;

  const supported = entries.filter((e) => e.supported);
  const byKind = new Map<string, number>();
  for (const entry of entries) byKind.set(entry.kind, (byKind.get(entry.kind) ?? 0) + 1);
  const recent = supported.slice(0, 5);

  return (
    <div>
      <h1>Dashboard</h1>
      <div className="panel">
        <h2>What AgentCrash measures</h2>
        <p>
          AgentCrash stress-tests agents that call business APIs: it injects a
          failure (for example, a payment timeout), then checks{" "}
          <strong>persisted business state</strong> — not the agent&apos;s own
          explanation. A trial can complete its task yet still be wrong for the
          business (a duplicate charge); only the invariant evaluation tells them
          apart.
        </p>
        <p>
          <strong>Recovery success</strong> means the agent demonstrably handled the
          injected failure with evaluation evidence behind it.{" "}
          <strong>Invariant violations</strong> (duplicate charges, unpaid
          fulfillments, duplicate orders) are counted separately from
          infrastructure errors. Unknown, missing, and not-applicable values are
          shown as such — never as zero.
        </p>
        <p>
          New here? Start with <Link to="/scenarios">Scenarios</Link> to see the
          three failure modes, then open a report below and follow it to its{" "}
          <strong>scorecard</strong>, and compare two runs under{" "}
          <Link to="/compare">Compare</Link>.
        </p>
      </div>
      <div className="panel">
        <h2>Experiment artifacts</h2>
        <p>
          {total} report{total === 1 ? "" : "s"} on disk · {supported.length} supported by
          the UI
        </p>
        <ul>
          {[...byKind.entries()].map(([kind, count]) => (
            <li key={kind}>
              <span className="mono">{kind}</span> — {count}
            </li>
          ))}
        </ul>
      </div>
      <div className="panel">
        <h2>Recent activity</h2>
        {recent.length === 0 ? (
          <Empty message="No supported reports yet. From the repository root, run .\demo.ps1 to produce a deterministic offline report." />
        ) : (
          <table className="grid">
            <thead>
              <tr>
                <th scope="col">Report</th>
                <th scope="col">Kind</th>
                <th scope="col">Modified</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((entry) => (
                <tr key={entry.name}>
                  <td>
                    <Link to={`/experiments/${encodeURIComponent(entry.name)}`}>
                      <span className="mono">{entry.name}</span>
                    </Link>
                  </td>
                  <td>{kindChip(entry)}</td>
                  <td className="muted">{entry.mtime ?? "unknown"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
