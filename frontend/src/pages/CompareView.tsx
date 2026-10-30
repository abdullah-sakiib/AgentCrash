import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ComparisonDocument,
  fetchComparison,
  fetchReports,
  MetricDeltaRow,
} from "../api";
import { RATE_ORDER, formatDelta, formatMetricValue } from "../components/metrics";
import { Empty, LoadError, Loading } from "../components/StateViews";
import StatusChip from "../components/StatusChip";

function DeltaTable({
  title,
  rows,
}: {
  title: string;
  rows: Record<string, MetricDeltaRow>;
}) {
  const keys = [
    ...RATE_ORDER.filter((k) => k in rows),
    ...Object.keys(rows).filter((k) => !RATE_ORDER.includes(k)),
  ];
  return (
    <div className="panel">
      <h2>{title}</h2>
      <table className="grid">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col">Baseline</th>
            <th scope="col">Candidate</th>
            <th scope="col">Delta</th>
            <th scope="col">Direction</th>
            <th scope="col">Definition</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((key) => {
            const row = rows[key];
            return (
              <tr key={key}>
                <td className="mono">{key}</td>
                <td className="mono">
                  {row.baseline_fraction ?? formatMetricValue(row.baseline)}
                </td>
                <td className="mono">
                  {row.candidate_fraction ?? formatMetricValue(row.candidate)}
                </td>
                <td className="mono">{formatDelta(row.delta)}</td>
                <td>
                  <StatusChip kind={row.direction} label={row.direction} />
                  {row.reason ? <div className="muted">{row.reason}</div> : null}
                  {row.applicable === false ? (
                    <div className="muted">not applicable</div>
                  ) : null}
                </td>
                <td className="muted">
                  {row.definition}
                  <div>Better when: {row.better_direction}</div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ViolationDiffs({
  diffs,
}: {
  diffs: ComparisonDocument["violations_by_code"];
}) {
  const codes = Object.keys(diffs);
  if (codes.length === 0) return <p className="muted">No violation changes.</p>;
  return (
    <ul>
      {codes.map((code) => {
        const entry = diffs[code];
        return (
          <li key={code} className="mono">
            {code}: {entry.baseline}→{entry.candidate} ({entry.direction})
          </li>
        );
      })}
    </ul>
  );
}

function ComparisonResult({ doc }: { doc: ComparisonDocument }) {
  if (!doc.compatible) {
    return (
      <div className="panel" role="alert">
        <h2>Incompatible scorecards — no comparison possible</h2>
        <ul>
          {doc.reasons.map((reason, i) => (
            <li key={i}>{reason}</li>
          ))}
        </ul>
        <p className="muted">{doc.note}</p>
      </div>
    );
  }
  const usage = doc.usage as {
    comparable?: boolean;
    baseline_status?: string;
    candidate_status?: string;
  };
  return (
    <div>
      {doc.regressions_present ? (
        <div className="panel" role="alert">
          <strong>Regression warning.</strong> At least one safety metric regressed.
          Check the per-scenario rows below.
        </div>
      ) : (
        <div className="panel">
          <StatusChip kind="pass" label="no regressions detected" />
        </div>
      )}
      <DeltaTable title="Overall" rows={doc.overall} />
      {Object.entries(doc.scenarios).map(([name, scenario]) => (
        <div key={name}>
          <h2>
            <span className="mono">{name}</span>
          </h2>
          <DeltaTable title={`Scenario: ${name}`} rows={scenario.metrics} />
          <div className="panel">
            <h2>Violation changes</h2>
            <ViolationDiffs diffs={scenario.violations_by_code} />
          </div>
        </div>
      ))}
      <div className="panel">
        <h2>Overall violation changes</h2>
        <ViolationDiffs diffs={doc.violations_by_code} />
      </div>
      <div className="panel">
        <h2>Usage and cost</h2>
        {usage.comparable ? (
          <p>Usage is comparable on both sides; see per-trial usage in the reports.</p>
        ) : (
          <p className="muted">
            Usage not comparable (baseline {String(usage.baseline_status)} / candidate{" "}
            {String(usage.candidate_status)}). Token counts are not treated as zero.
          </p>
        )}
        <p className="mono">{doc.cost}</p>
        {doc.sample_size_warning ? (
          <p role="note">
            <strong>Sample-size warning.</strong> At least one compared rate rests on
            fewer than 5 trials; treat differences as fragile.
          </p>
        ) : null}
        <p className="muted">{doc.note}</p>
      </div>
    </div>
  );
}

export default function CompareView() {
  const [params, setParams] = useSearchParams();
  const [options, setOptions] = useState<string[]>([]);
  const [doc, setDoc] = useState<ComparisonDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const baseline = params.get("baseline") ?? "";
  const candidate = params.get("candidate") ?? "";

  useEffect(() => {
    let cancelled = false;
    fetchReports(100, 0)
      .then((list) => {
        if (!cancelled)
          setOptions(list.entries.filter((e) => e.supported).map((e) => e.name));
      })
      .catch(() => {
        if (!cancelled) setOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function runComparison(nextBaseline: string, nextCandidate: string) {
    if (!nextBaseline || !nextCandidate) return;
    setLoading(true);
    setError(null);
    setDoc(null);
    fetchComparison(nextBaseline, nextCandidate)
      .then((result) => {
        setDoc(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
  }

  useEffect(() => {
    if (baseline && candidate) runComparison(baseline, candidate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <h1>Compare scorecards</h1>
      <div className="panel">
        <label>
          Baseline{" "}
          <select
            aria-label="Baseline report"
            value={baseline}
            onChange={(e) => {
              const next = new URLSearchParams(params);
              next.set("baseline", e.target.value);
              setParams(next);
            }}
          >
            <option value="">Select…</option>
            {options.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>{" "}
        <label>
          Candidate{" "}
          <select
            aria-label="Candidate report"
            value={candidate}
            onChange={(e) => {
              const next = new URLSearchParams(params);
              next.set("candidate", e.target.value);
              setParams(next);
            }}
          >
            <option value="">Select…</option>
            {options.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>{" "}
        <button
          type="button"
          className="theme-toggle"
          disabled={!baseline || !candidate || loading}
          onClick={() => runComparison(baseline, candidate)}
        >
          Compare
        </button>
      </div>
      {loading ? <Loading what="comparison" /> : null}
      {error ? <LoadError message={error} onRetry={() => baseline && candidate && runComparison(baseline, candidate)} /> : null}
      {doc ? <ComparisonResult doc={doc} /> : null}
      {!doc && !loading && !error ? (
        <Empty message="Select a baseline and a candidate report, then compare." />
      ) : null}
    </div>
  );
}
