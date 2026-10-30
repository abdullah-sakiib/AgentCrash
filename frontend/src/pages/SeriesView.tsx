import { useEffect, useState } from "react";
import { fetchReports, fetchSeries, SeriesTrendDocument } from "../api";
import { formatMetricValue } from "../components/metrics";
import { Empty, LoadError, Loading } from "../components/StateViews";
import StatusChip from "../components/StatusChip";

function MemberTable({ doc }: { doc: SeriesTrendDocument }) {
  return (
    <div className="panel">
      <h2>Members</h2>
      <table className="grid">
        <thead>
          <tr>
            <th scope="col">Report</th>
            <th scope="col">Eligible</th>
            <th scope="col">Recovery</th>
            <th scope="col">Reasons</th>
          </tr>
        </thead>
        <tbody>
          {doc.members.map((member) => (
            <tr key={String(member.source)}>
              <td className="mono">{String(member.source ?? "—")}</td>
              <td>
                {member.eligible ? (
                  <StatusChip kind="pass" label="eligible" />
                ) : (
                  <StatusChip kind="unavailable" label="excluded" />
                )}
              </td>
              <td className="mono">
                {member.scorecard
                  ? (member.scorecard.recovery.fraction ??
                    formatMetricValue(member.scorecard.recovery.value))
                  : "—"}
              </td>
              <td>
                {member.reasons.length === 0 ? (
                  <span className="muted">mutually compatible with baseline</span>
                ) : (
                  <ul>
                    {member.reasons.map((reason, i) => (
                      <li key={i}>{reason}</li>
                    ))}
                  </ul>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RangePanel({ doc }: { doc: SeriesTrendDocument }) {
  const range = doc.series.recovery_range;
  return (
    <div className="panel">
      <h2>Recovery range (informational)</h2>
      {range.min === null || range.max === null || range.span === null ? (
        <p>
          <StatusChip kind="unavailable" label="unknown" />{" "}
          <span className="muted">{range.reason}</span>
        </p>
      ) : (
        <p className="mono">
          min {formatMetricValue(range.min)} · max {formatMetricValue(range.max)} · spread{" "}
          {formatMetricValue(range.span)} over {range.n_values} members
        </p>
      )}
      <ul>
        {range.member_fractions.map((entry) => (
          <li key={String(entry.source)} className="mono">
            {String(entry.source)}: {entry.fraction ?? "unknown"}
          </li>
        ))}
      </ul>
      {range.note ? <p className="muted">{range.note}</p> : null}
      <p className="muted">{range.definition} A smaller spread is not proof of better quality.</p>
    </div>
  );
}

function RecurrencePanel({ doc }: { doc: SeriesTrendDocument }) {
  const codes = Object.keys(doc.series.recurring_violations);
  return (
    <div className="panel">
      <h2>Recurring violations (informational)</h2>
      {codes.length === 0 ? (
        <p className="muted">
          No violation recurred in at least two eligible members. One-off violations, if
          any, appear in each member&apos;s own comparison.
        </p>
      ) : (
        codes.map((code) => {
          const entry = doc.series.recurring_violations[code];
          return (
            <div key={code}>
              <h3 className="mono">{code}</h3>
              <p>
                {entry.members_affected} members affected · {entry.total_occurrences}{" "}
                total occurrences
              </p>
              <ul>
                {entry.per_member.map((row) => (
                  <li key={String(row.source)} className="mono">
                    {String(row.source)}: {row.occurrences} occurrence(s),{" "}
                    {row.evaluable_trials ?? "unknown"} evaluable trials
                  </li>
                ))}
              </ul>
              <p className="muted">{entry.definition}</p>
            </div>
          );
        })
      )}
    </div>
  );
}

function SeriesResult({ doc }: { doc: SeriesTrendDocument }) {
  const baseline = doc.baseline as { source?: unknown; strategy_id?: unknown };
  return (
    <div>
      <p className="muted">
        Baseline <span className="mono">{String(baseline.source ?? "—")}</span> ·
        strategy <span className="mono">{String(baseline.strategy_id ?? "—")}</span> ·{" "}
        {doc.series.eligible_count} of {doc.series.member_count} members eligible
      </p>
      {doc.series.regressions_present ? (
        <div className="panel" role="alert">
          <strong>Regression warning.</strong> The following members regressed against
          the baseline:{" "}
          <span className="mono">
            {doc.series.members_regressed_vs_baseline.join(", ")}
          </span>
          . This does not establish a series-wide direction.
        </div>
      ) : (
        <div className="panel">
          <StatusChip kind="pass" label="no member regressed against baseline" />
        </div>
      )}
      <MemberTable doc={doc} />
      <RangePanel doc={doc} />
      <RecurrencePanel doc={doc} />
      <div className="panel">
        <h2>Evidence sufficiency</h2>
        {doc.series.members_with_sample_warning.length > 0 ? (
          <p role="note">
            <strong>Sample-size warning.</strong> Small samples in:{" "}
            <span className="mono">
              {doc.series.members_with_sample_warning.join(", ")}
            </span>
            . Treat differences as fragile.
          </p>
        ) : (
          <p className="muted">No member triggered the small-sample warning.</p>
        )}
        {doc.series.members_with_unknown_recovery.length > 0 ? (
          <p>
            Unknown recovery for:{" "}
            <span className="mono">
              {doc.series.members_with_unknown_recovery.join(", ")}
            </span>
          </p>
        ) : null}
        {doc.warnings.length > 0 ? (
          <ul>
            {doc.warnings.map((warning, i) => (
              <li key={i} className="muted">
                {warning}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div className="panel">
        <h2>Cost</h2>
        <p className="mono">{doc.cost}</p>
        <p className="muted">{doc.note}</p>
      </div>
    </div>
  );
}

export default function SeriesView() {
  const [options, setOptions] = useState<string[]>([]);
  const [baseline, setBaseline] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [strategyId, setStrategyId] = useState("");
  const [doc, setDoc] = useState<SeriesTrendDocument | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

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

  function toggleMember(name: string) {
    setSelected((current) =>
      current.includes(name) ? current.filter((n) => n !== name) : [...current, name],
    );
  }

  function runSeries() {
    if (!baseline || selected.length === 0) return;
    setLoading(true);
    setError(null);
    setDoc(null);
    fetchSeries(baseline, selected, strategyId || undefined)
      .then((result) => {
        setDoc(result);
        setLoading(false);
      })
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
  }

  return (
    <div>
      <h1>Reliability series</h1>
      <p className="muted">
        Compare an explicit, ordered set of runs against one baseline. Members are
        only eligible when strategy, mode, and model match the baseline and the
        standard compatibility checks pass. Label runs with a shared strategy ID;
        unlabeled or mismatched runs are excluded as unconfirmed, never grouped by
        assumption.
      </p>
      <div className="panel">
        <label>
          Baseline{" "}
          <select aria-label="Baseline report" value={baseline} onChange={(e) => setBaseline(e.target.value)}>
            <option value="">Select…</option>
            {options.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>{" "}
        <label>
          Strategy ID{" "}
          <input
            aria-label="Strategy ID"
            type="text"
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
            placeholder="e.g. status-check-v1"
          />
        </label>
        <fieldset>
          <legend>Members (in order)</legend>
          {options
            .filter((name) => name !== baseline)
            .map((name) => (
              <label key={name} style={{ display: "block" }}>
                <input
                  type="checkbox"
                  checked={selected.includes(name)}
                  onChange={() => toggleMember(name)}
                />{" "}
                <span className="mono">{name}</span>
              </label>
            ))}
        </fieldset>
        <button
          type="button"
          className="theme-toggle"
          disabled={!baseline || selected.length === 0 || loading}
          onClick={runSeries}
        >
          Build series
        </button>
      </div>
      {loading ? <Loading what="series" /> : null}
      {error ? <LoadError message={error} onRetry={runSeries} /> : null}
      {doc ? <SeriesResult doc={doc} /> : null}
      {!doc && !loading && !error ? (
        <Empty message="Select a baseline, tick member reports, enter the shared strategy ID, then build the series." />
      ) : null}
    </div>
  );
}
