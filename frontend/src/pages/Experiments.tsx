import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { fetchReports } from "../api";
import { ReportList } from "../types";
import StatusChip from "../components/StatusChip";
import { Empty, LoadError, Loading } from "../components/StateViews";

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  return `${(size / 1024).toFixed(1)} KB`;
}

export default function Experiments() {
  const [list, setList] = useState<ReportList | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchReports(100, 0)
      .then((result) => {
        if (!cancelled) setList(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <LoadError message={error} onRetry={() => window.location.reload()} />;
  if (list === null) return <Loading what="experiment list" />;
  if (list.total === 0) {
    return (
      <Empty message="No experiment artifacts found. From the repository root, run .\demo.ps1 to produce a deterministic offline report." />
    );
  }

  return (
    <div>
      <h1>Experiments</h1>
      <p className="muted">
        Showing {list.entries.length} of {list.total} artifacts, newest first.{" "}
        <Link to="/compare">Compare two reports as baseline and candidate</Link>.
      </p>
      <div className="panel">
        <table className="grid">
          <thead>
            <tr>
              <th scope="col">Report</th>
              <th scope="col">Kind</th>
              <th scope="col">Status</th>
              <th scope="col">Size</th>
              <th scope="col">Modified</th>
            </tr>
          </thead>
          <tbody>
            {list.entries.map((entry) => (
              <tr key={entry.name}>
                <td>
                  <Link to={`/experiments/${encodeURIComponent(entry.name)}`}>
                    <span className="mono">{entry.name}</span>
                  </Link>
                </td>
                <td className="mono">{entry.kind}</td>
                <td>
                  {entry.supported ? (
                    <StatusChip kind="supported" />
                  ) : (
                    <StatusChip kind="unsupported" />
                  )}
                </td>
                <td>{formatBytes(entry.size_bytes)}</td>
                <td className="muted">{entry.mtime ?? "unknown"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
