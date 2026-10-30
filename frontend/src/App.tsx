import { useEffect, useState } from "react";
import { Link, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Experiments from "./pages/Experiments";
import ExperimentDetail from "./pages/ExperimentDetail";
import Scenarios from "./pages/Scenarios";
import ScorecardView from "./pages/ScorecardView";
import CompareView from "./pages/CompareView";
import SeriesView from "./pages/SeriesView";

type Theme = "light" | "dark" | "system";

function currentTheme(): Exclude<Theme, "system"> {
  if (typeof window === "undefined") return "light";
  const stored = window.localStorage.getItem("agentcrash-theme");
  if (stored === "light" || stored === "dark") return stored;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export default function App() {
  const [theme, setTheme] = useState<Theme>("system");
  const [resolved, setResolved] = useState<"light" | "dark">(currentTheme());

  useEffect(() => {
    if (theme === "system") {
      document.documentElement.removeAttribute("data-theme");
      setResolved(currentTheme());
    } else {
      document.documentElement.setAttribute("data-theme", theme);
      setResolved(theme);
    }
    window.localStorage.setItem("agentcrash-theme", theme);
  }, [theme]);

  return (
    <div className="app-shell">
      <nav className="app-nav" aria-label="Primary">
        <span className="brand">AgentCrash</span>
        <Link to="/">Dashboard</Link>
        <Link to="/experiments">Experiments</Link>
        <Link to="/scenarios">Scenarios</Link>
        <Link to="/compare">Compare</Link>
        <Link to="/series">Series</Link>
        <button
          type="button"
          className="theme-toggle"
          aria-label={`Theme: ${theme}. Activate to switch.`}
          onClick={() =>
            setTheme((t) => (t === "light" ? "dark" : t === "dark" ? "system" : "light"))
          }
        >
          Theme: {theme} ({resolved})
        </button>
      </nav>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/experiments" element={<Experiments />} />
        <Route path="/experiments/:name" element={<ExperimentDetail />} />
        <Route path="/scenarios" element={<Scenarios />} />
        <Route path="/scorecards/:name" element={<ScorecardView />} />
        <Route path="/compare" element={<CompareView />} />
        <Route path="/series" element={<SeriesView />} />
      </Routes>
    </div>
  );
}
