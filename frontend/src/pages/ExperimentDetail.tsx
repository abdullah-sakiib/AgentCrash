import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fetchDiagnostics, fetchReport } from "../api";
import { ReportDocument } from "../types";
import StatusChip from "../components/StatusChip";
import { Empty, LoadError, Loading } from "../components/StateViews";

type Dict = Record<string, unknown>;

function isDict(value: unknown): value is Dict {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "—";
}

function outcomeChip(trial: Dict) {
  if (typeof trial.error === "string" && trial.error) return <StatusChip kind="error" />;
  if (trial.outcome === "passed") return <StatusChip kind="pass" />;
  if (trial.outcome === "failed") return <StatusChip kind="fail" />;
  return <StatusChip kind="incomplete" />;
}

function Violations({ violations }: { violations: unknown }) {
  const list = asList(violations);
  if (list.length === 0) return <span className="muted">none</span>;
  return (
    <ul>
      {list.map((v, i) => (
        <li key={i} className="mono">
          {isDict(v) && typeof v.code === "string" ? v.code : JSON.stringify(v)}
        </li>
      ))}
    </ul>
  );
}

function TraceEvents({ trace }: { trace: unknown }) {
  const events = asList(trace);
  if (events.length === 0) return <p className="muted">No trace events recorded.</p>;
  return (
    <ul className="trace-list">
      {events.map((event, i) => {
        if (!isDict(event)) return <li key={i} className="mono">{JSON.stringify(event)}</li>;
        const parts = [
          `#${String(event.sequence ?? i)}`,
          String(event.kind ?? "event"),
          event.turn !== undefined && event.turn !== null ? `turn ${String(event.turn)}` : null,
          typeof event.tool_name === "string" ? event.tool_name : null,
          typeof event.detail === "string" ? event.detail : null,
          typeof event.http_status === "number" ? `HTTP ${event.http_status}` : null,
          typeof event.error === "string" ? `error: ${event.error}` : null,
        ].filter(Boolean);
        return (
          <li key={i} className="mono">
            {parts.join(" · ")}
          </li>
        );
      })}
    </ul>
  );
}

function Diagnosis({ diagnostic }: { diagnostic: unknown }) {
  if (!isDict(diagnostic)) return null;
  const axes = isDict(diagnostic.axes) ? diagnostic.axes : {};
  const recovery = isDict(diagnostic.recovery) ? diagnostic.recovery : {};
  const signals = isDict(diagnostic.signals) ? diagnostic.signals : {};
  const timeline = asList(diagnostic.timeline);
  const violations = asList(diagnostic.violations);
  const hints = asList(diagnostic.hints);
  const limits = asList(diagnostic.limits);
  return (
    <div className="diagnosis">
      <h4>Diagnosis</h4>
      <p>{typeof diagnostic.headline === "string" ? diagnostic.headline : "Diagnostic summary unavailable."}</p>
      <table className="grid">
        <tbody>
          <tr>
            <th scope="row">Completion</th>
            <td className="mono">{asString(axes.completion)}</td>
          </tr>
          <tr>
            <th scope="row">Evaluation</th>
            <td className="mono">{asString(axes.evaluation)}</td>
          </tr>
          <tr>
            <th scope="row">Consistency</th>
            <td className="mono">{asString(axes.consistency)}</td>
          </tr>
          <tr>
            <th scope="row">Evidence</th>
            <td className="mono">{asString(axes.evidence_sufficiency)}</td>
          </tr>
          <tr>
            <th scope="row">Recovery</th>
            <td className="mono">
              {asString(recovery.status)} · {asString(recovery.strategy_context)}
            </td>
          </tr>
          <tr>
            <th scope="row">Key comparison</th>
            <td className="mono">{asString(signals.key_comparison)}</td>
          </tr>
        </tbody>
      </table>
      {timeline.length > 0 ? (
        <>
          <h4>Evidence timeline</h4>
          <ul className="trace-list">
            {timeline.map((entry, i) => {
              if (!isDict(entry)) return <li key={i} className="mono">{JSON.stringify(entry)}</li>;
              const tag = entry.derived === true ? "derived" : "observed";
              return (
                <li key={i} className="mono">
                  {`#${String(entry.sequence ?? i)} · ${String(entry.kind ?? "event")} · ${String(entry.label ?? "")} · ${tag}`}
                  {typeof entry.key_fact === "string" ? ` — ${entry.key_fact}` : null}
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <p className="muted">No diagnostic timeline; evidence insufficient.</p>
      )}
      {violations.length > 0 ? (
        <>
          <h4>Invariant diagnosis</h4>
          <ul>
            {violations.map((v, i) => {
              if (!isDict(v)) return <li key={i} className="mono">{JSON.stringify(v)}</li>;
              return (
                <li key={i}>
                  <span className="mono">{String(v.code)}</span>
                  {typeof v.expected === "string" ? <div>{v.expected}</div> : null}
                  <div className="muted mono">
                    attribution {asString(v.attribution)} · orders {JSON.stringify(v.affected_orders ?? [])} · payments{" "}
                    {JSON.stringify(v.affected_payments ?? [])}
                  </div>
                  {typeof v.message === "string" && v.message ? (
                    <div className="mono muted">{v.message}</div>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
      {hints.length > 0 ? (
        <>
          <h4>Investigate next</h4>
          <ul>
            {hints.map((h, i) => {
              if (!isDict(h)) return <li key={i} className="mono">{JSON.stringify(h)}</li>;
              return (
                <li key={i}>
                  <span className="mono">{String(h.id)}</span>
                  {typeof h.guidance === "string" ? <div>{h.guidance}</div> : null}
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
      {limits.length > 0 ? (
        <p className="muted">Limits: {limits.map((l) => String(l)).join(" | ")}</p>
      ) : null}
    </div>
  );
}

function diagnosticKey(scenario: string, trial: Dict, index: number): string {
  const id = trial.trial_id ?? trial.strategy_name;
  const idx = trial.trial_index ?? index;
  return `${scenario}::${typeof id === "string" || typeof id === "number" ? String(id) : "?"}::${String(idx)}`;
}

function TrialCard({ trial, index, diagnostic }: { trial: Dict; index: number; diagnostic?: Dict | null }) {
  const anchor = `trial-${index}`;
  const violations = asList(trial.violations ?? trial.invariant_violations);
  return (
    <details className="trial-detail" id={anchor}>
      <summary>
        {outcomeChip(trial)} Trial {String(trial.trial_index ?? index)}{" "}
        <span className="muted mono">{asString(trial.trial_id ?? trial.strategy_name)}</span>
      </summary>
      <table className="grid">
        <tbody>
          <tr>
            <th scope="row">Outcome</th>
            <td>
              {asString(trial.outcome ?? trial.task_outcome)}{" "}
              {typeof trial.error === "string" && trial.error ? (
                <span className="mono">{trial.error}</span>
              ) : null}
            </td>
          </tr>
          <tr>
            <th scope="row">Turns</th>
            <td>{String(trial.turns ?? trial.turns_used ?? "—")}</td>
          </tr>
          <tr>
            <th scope="row">Successful charges</th>
            <td>{String(trial.successful_charges ?? trial.successful_charge_count ?? "—")}</td>
          </tr>
          <tr>
            <th scope="row">Order / payment IDs</th>
            <td className="mono">
              orders {JSON.stringify(trial.order_ids ?? [])} payments{" "}
              {JSON.stringify(trial.payment_ids ?? [])}
            </td>
          </tr>
          <tr>
            <th scope="row">Violations</th>
            <td>
              <Violations violations={violations} />
            </td>
          </tr>
        </tbody>
      </table>
      <h4>Trace</h4>
      <TraceEvents trace={trial.trace} />
      {diagnostic ? <Diagnosis diagnostic={diagnostic} /> : null}
      {isDict(trial.evaluation_evidence) && Object.keys(trial.evaluation_evidence).length > 0 ? (
        <>
          <h4>Evaluation evidence (persisted state)</h4>
          <pre className="json">{JSON.stringify(trial.evaluation_evidence, null, 2)}</pre>
        </>
      ) : null}
      {isDict(trial.usage) && Object.keys(trial.usage).length > 0 ? (
        <>
          <h4>Usage</h4>
          <pre className="json">{JSON.stringify(trial.usage, null, 2)}</pre>
        </>
      ) : null}
    </details>
  );
}

function OrchestratedDetail({ report, diagnosticsByKey }: { report: Dict; diagnosticsByKey: Map<string, Dict> }) {
  const aggregate = isDict(report.aggregate) ? report.aggregate : {};
  const scenarios = asList(report.scenarios);
  return (
    <div>
      <div className="panel">
        <h2>Aggregate</h2>
        <p className="muted">
          mode {asString(report.mode)} · model {asString(report.model)} · scenarios{" "}
          {String(aggregate.scenarios_completed ?? "?")}/{String(aggregate.scenarios_requested ?? "?")} ·
          trials {String(aggregate.trials_completed ?? "?")}/{String(aggregate.trials_requested ?? "?")}
        </p>
        <p>
          pass {String(aggregate.passed ?? "?")} · fail {String(aggregate.failed ?? "?")} ·
          errors {String(aggregate.errors ?? "?")}
        </p>
      </div>
      {scenarios.map((entry, si) => {
        if (!isDict(entry)) return null;
        const trials = asList(entry.trials);
        const scenarioName = asString(entry.scenario);
        return (
          <div className="panel" key={si}>
            <h2>
              <span className="mono">{asString(entry.scenario)}</span>{" "}
              <span className="muted">{asString(entry.mode)}</span>
            </h2>
            <p>
              trials {String(entry.trials_completed)}/{String(entry.trials_requested)} ·
              passed {String(entry.passed)} · failed {String(entry.failed)} · errors{" "}
              {String(entry.errors)}
            </p>
            <p className="muted">
              violations:{" "}
              {isDict(entry.violation_counts) && Object.keys(entry.violation_counts).length > 0
                ? JSON.stringify(entry.violation_counts)
                : "none"}
            </p>
            {trials.map((trial, ti) =>
              isDict(trial) ? <TrialCard key={ti} trial={trial} index={ti + 1} diagnostic={diagnosticsByKey.get(diagnosticKey(scenarioName === "—" ? "" : scenarioName, trial, ti + 1)) ?? null} /> : null,
            )}
          </div>
        );
      })}
    </div>
  );
}

function GenericDetail({ report, diagnosticsByKey }: { report: Dict; diagnosticsByKey: Map<string, Dict> }) {
  const entries = asList(report.scenarios);
  return (
    <div>
      {isDict(report.aggregate) ? (
        <div className="panel">
          <h2>Aggregate</h2>
          <pre className="json">{JSON.stringify(report.aggregate, null, 2)}</pre>
        </div>
      ) : null}
      {entries.map((entry, i) => {
        if (!isDict(entry)) return null;
        const scenarioName = asString(entry.scenario);
        return (
          <div className="panel" key={i}>
            <h2>
              <span className="mono">{asString(entry.scenario)}</span>{" "}
              {typeof entry.kind === "string" ? (
                <span className="muted">[{entry.kind}]</span>
              ) : null}
            </h2>
            <p>
              trials {String(entry.trials_completed)}/{String(entry.trials_requested)} ·
              passed {String(entry.passed)} · failed {String(entry.failed)} · errors{" "}
              {String(entry.errors)}
            </p>
            {asList(entry.trials).map((trial, ti) =>
              isDict(trial) ? <TrialCard key={ti} trial={trial} index={ti + 1} diagnostic={diagnosticsByKey.get(diagnosticKey(scenarioName === "—" ? "" : scenarioName, trial, ti + 1)) ?? null} /> : null,
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function ExperimentDetail() {
  const { name = "" } = useParams<{ name: string }>();
  const [report, setReport] = useState<ReportDocument | null>(null);
  const [diagnosticsByKey, setDiagnosticsByKey] = useState<Map<string, Dict>>(new Map());
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setReport(null);
    setDiagnosticsByKey(new Map());
    setError(null);
    fetchReport(name)
      .then((doc) => {
        if (!cancelled) setReport(doc);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, [name]);

  useEffect(() => {
    let cancelled = false;
    if (report === null || !isDict(report)) return;
    fetchDiagnostics(name)
      .then((doc) => {
        if (cancelled) return;
        const next = new Map<string, Dict>();
        for (const item of asList((doc as Dict).diagnostics)) {
          if (!isDict(item)) continue;
          const ref = isDict(item.trial_ref) ? item.trial_ref : {};
          const scenario = typeof ref.scenario === "string" ? ref.scenario : "";
          const id = typeof ref.trial_id === "string" || typeof ref.trial_id === "number" ? String(ref.trial_id) : "?";
          const idx = typeof ref.trial_index === "number" ? String(ref.trial_index) : "?";
          next.set(`${scenario}::${id}::${idx}`, item);
        }
        // Fallback: index-based keys when trial_ref matching fails.
        if (next.size === 0) setDiagnosticsByKey(new Map());
        else setDiagnosticsByKey(next);
      })
      .catch(() => {
        if (!cancelled) setDiagnosticsByKey(new Map());
      });
    return () => {
      cancelled = true;
    };
  }, [name, report]);

  if (error)
    return (
      <div>
        <p>
          <Link to="/experiments">← All experiments</Link>
        </p>
        <LoadError message={error} onRetry={() => window.location.reload()} />
      </div>
    );
  if (report === null) return <Loading what={`report ${name}`} />;
  if (!isDict(report)) return <Empty message="Report is empty or has an unexpected shape." />;

  const isOrchestrated = report.experiment === "agentcrash-orchestrated-run";
  return (
    <div>
      <p>
        <Link to="/experiments">← All experiments</Link> ·{" "}
        <Link to={`/scorecards/${encodeURIComponent(name)}`}>View scorecard</Link>
      </p>
      <h1 className="mono">{name}</h1>
      {isOrchestrated ? <OrchestratedDetail report={report} diagnosticsByKey={diagnosticsByKey} /> : <GenericDetail report={report} diagnosticsByKey={diagnosticsByKey} />}
    </div>
  );
}
