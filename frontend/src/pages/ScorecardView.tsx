import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fetchScorecard, RateRow, ScorecardDocument } from "../api";
import { isRateRow, RATE_ORDER, formatMetricValue, MetricState } from "../components/metrics";
import { LoadError, Loading } from "../components/StateViews";

function RateTable({ title, rows }: { title: string; rows: Record<string, RateRow> }) {
  const keys = RATE_ORDER.filter((k) => k in rows);
  const extra = Object.keys(rows).filter((k) => !RATE_ORDER.includes(k) && isRateRow(rows[k]));
  return (
    <div className="panel">
      <h2>{title}</h2>
      <table className="grid">
        <thead>
          <tr>
            <th scope="col">Metric</th>
            <th scope="col">Value</th>
            <th scope="col">Fraction</th>
            <th scope="col">Definition</th>
          </tr>
        </thead>
        <tbody>
          {[...keys, ...extra].map((key) => {
            const row = rows[key];
            return (
              <tr key={key}>
                <td className="mono">{key}</td>
                <td>
                  <MetricState row={row} />
                  {row.applicable !== false && row.value !== null ? (
                    <span className="mono">{formatMetricValue(row.value)}</span>
                  ) : null}
                </td>
                <td className="mono">
                  {row.numerator !== null && row.denominator !== null
                    ? `${row.numerator}/${row.denominator}`
                    : "—"}
                </td>
                <td className="muted">{row.definition}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function asBlock(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function formatClasses(value: unknown): string {
  if (!Array.isArray(value) || value.length === 0) return "—";
  return value.map((entry) => String(entry)).join(", ");
}

/** Reliability depth: consistency and efficiency are informational only —
never verdicts. Missing blocks (historical sidecars) render nothing. */
function DepthPanel({
  consistency,
  efficiency,
}: {
  consistency: unknown;
  efficiency: unknown;
}) {
  const cons = asBlock(consistency);
  const eff = asBlock(efficiency);
  if (!cons && !eff) return null;
  return (
    <div className="panel">
      <h2>Reliability depth (informational)</h2>
      {cons ? (
        <div>
          <h3>Outcome consistency</h3>
          <p>
            {cons.consistent === true ? (
              <span className="mono">consistent</span>
            ) : cons.consistent === false ? (
              <span className="mono">mixed</span>
            ) : (
              <span className="chip chip-warn">unknown</span>
            )}{" "}
            <span className="muted">
              {typeof cons.eligible_trials === "number"
                ? `${cons.eligible_trials} eligible trials`
                : null}
            </span>
          </p>
          <p className="mono">classes: {formatClasses(cons.classes)}</p>
          {typeof cons.reason === "string" && cons.reason ? (
            <p className="muted">{cons.reason}</p>
          ) : null}
          {typeof cons.definition === "string" ? (
            <p className="muted">{cons.definition}</p>
          ) : null}
        </div>
      ) : null}
      {eff ? (
        <div>
          <h3>Recovery efficiency</h3>
          <p>
            median turns to recovery:{" "}
            {typeof eff.median_turns_to_recovery === "number" ? (
              <span className="mono">{eff.median_turns_to_recovery}</span>
            ) : (
              <span className="chip chip-warn">unknown</span>
            )}
          </p>
          <p className="muted">
            {typeof eff.recovered_trials === "number"
              ? `${eff.recovered_trials} recovered trials`
              : null}
            {typeof eff.recovered_without_turn_data === "number" &&
            eff.recovered_without_turn_data > 0
              ? ` (${eff.recovered_without_turn_data} without recorded turn data)`
              : null}
          </p>
          {typeof eff.reason === "string" && eff.reason ? (
            <p className="muted">{eff.reason}</p>
          ) : null}
          {typeof eff.definition === "string" ? (
            <p className="muted">{eff.definition}</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function ScorecardView() {
  const { name = "" } = useParams<{ name: string }>();
  const [doc, setDoc] = useState<ScorecardDocument | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDoc(null);
    setError(null);
    fetchScorecard(name)
      .then((result) => {
        if (!cancelled) setDoc(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [name]);

  if (error)
    return (
      <div>
        <p>
          <Link to="/experiments">← All experiments</Link>
        </p>
        <LoadError message={error} onRetry={() => window.location.reload()} />
      </div>
    );
  if (doc === null) return <Loading what={`scorecard for ${name}`} />;

  return (
    <div>
      <p>
        <Link to={`/experiments/${encodeURIComponent(name)}`}>← Report {name}</Link>
      </p>
      <h1>
        Scorecard <span className="muted mono">v{doc.scorecard_version}</span>
      </h1>
      <p className="muted">
        strategy {String(doc.scope.strategy_id ?? "—")} · model{" "}
        {String(doc.scope.model ?? "—")} · modes {doc.scope.modes.join(", ")}
      </p>
      <RateTable title="Overall" rows={doc.overall} />
      <DepthPanel
        consistency={(doc.overall as Record<string, unknown>).outcome_consistency}
        efficiency={(doc.overall as Record<string, unknown>).recovery_efficiency}
      />
      {doc.scopes.map((scope) => {
        const rates: Record<string, RateRow> = {};
        for (const [key, value] of Object.entries(scope)) {
          if (isRateRow(value)) rates[key] = value;
        }
        const violations = Object.entries(scope.violations_by_code ?? {});
        return (
          <div key={scope.scenario}>
            <h2>
              <span className="mono">{scope.scenario}</span>
              {scope.reported_as ? (
                <span className="muted"> (reported as {String(scope.reported_as)})</span>
              ) : null}
            </h2>
            <RateTable title={`Scope: ${scope.scenario}`} rows={rates} />
            <DepthPanel
              consistency={scope.outcome_consistency}
              efficiency={scope.recovery_efficiency}
            />
            <div className="panel">
              <h2>Evidence</h2>
              <p>
                trials evaluable {String(scope.trial_counts.evaluable)} · errored{" "}
                {String(scope.trial_counts.errored)} · unknown completion{" "}
                {String(scope.trial_counts.unknown_completion)}
              </p>
              <p>
                violations:{" "}
                {violations.length > 0 ? (
                  <span className="mono">
                    {violations.map(([code, count]) => `${code}×${count}`).join(", ")}
                  </span>
                ) : (
                  <span className="muted">none</span>
                )}
              </p>
              <p className="muted">
                usage {String(scope.usage.status ?? "unknown")} · cost {scope.cost}
              </p>
            </div>
          </div>
        );
      })}
    </div>
  );
}
