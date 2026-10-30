import { useEffect, useState } from "react";
import { fetchScenarios } from "../api";
import { ScenarioInfo } from "../types";
import { Empty, LoadError, Loading } from "../components/StateViews";

export default function Scenarios() {
  const [scenarios, setScenarios] = useState<ScenarioInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchScenarios()
      .then((result) => {
        if (!cancelled) setScenarios(result.scenarios);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <LoadError message={error} onRetry={() => window.location.reload()} />;
  if (scenarios === null) return <Loading what="scenarios" />;
  if (scenarios.length === 0) return <Empty message="No scenarios reported by the backend." />;

  return (
    <div>
      <h1>Scenarios</h1>
      <p className="muted">
        Failure scenarios the simulator can inject, with the business invariant each one tests
        and the safe versus unsafe agent behavior being compared.
      </p>
      {scenarios.map((scenario) => (
        <div className="panel" key={scenario.key}>
          <h2>
            <span className="mono">{scenario.key}</span>
          </h2>
          <p className="muted mono">
            {scenario.canonical_name}
            {scenario.aliases.length > 0 ? ` (also reported as ${scenario.aliases.join(", ")})` : ""}
          </p>
          <p>{scenario.description}</p>
          <table className="grid">
            <tbody>
              <tr>
                <th scope="row">Configured fault</th>
                <td>
                  <pre className="json">{JSON.stringify(scenario.fault, null, 2)}</pre>
                </td>
              </tr>
              <tr>
                <th scope="row">Invariant under test</th>
                <td className="mono">{scenario.invariants.join(", ")}</td>
              </tr>
              <tr>
                <th scope="row">Unsafe behavior</th>
                <td className="mono">{scenario.strategies.unsafe}</td>
              </tr>
              <tr>
                <th scope="row">Safe behavior</th>
                <td className="mono">{scenario.strategies.safe}</td>
              </tr>
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
