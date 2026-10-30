# AgentCrash

AgentCrash is a deterministic local simulator for testing how API-using agents
recover from business-operation failures. It never calls a real payment service.

## Setup

```powershell
py -3.12 -m venv venv
.\venv\Scripts\python.exe -m pip install -e ".[test]"
```

## Compare payment-recovery strategies

This command gives both deterministic strategies equivalent state in separate
temporary SQLite databases, injects `timeout_after_commit`, evaluates persisted
state, and prints a concise comparison followed by structured JSON:

```powershell
.\venv\Scripts\python.exe -m agentcrash.experiments.compare_payment_recovery
```

Expected summary:

```text
AgentCrash payment-timeout comparison
unsafe_retry: FAIL | successful_charges=2 | violations=multiple_successful_charges_for_order
status_check: PASS | successful_charges=1 | violations=none
```

## Run the repeatable benchmark

The benchmark runs both strategies five times by default. Every trial/strategy
pair receives an equivalent task and a fresh SQLite database:

```powershell
.\venv\Scripts\python.exe -m agentcrash.experiments.benchmark_payment_recovery
```

Configure the number of trials per strategy and optionally export every trial,
trace, violation, and aggregate metric as readable UTF-8 JSON:

```powershell
.\venv\Scripts\python.exe -m agentcrash.experiments.benchmark_payment_recovery --trials 10 --output .\artifacts\payment-benchmark.json
```

Parent directories for the output file are created automatically. Trial counts
must be greater than zero.

Example aggregate output for three trials per strategy:

```text
AgentCrash deterministic payment-timeout benchmark
trials_per_strategy=3
strategy | trials | task_success | invariant_violations | duplicate_charges | total_successful_charges
unsafe_retry | 3 | 0.0% | 100.0% | 100.0% | 6
status_check | 3 | 100.0% | 0.0% | 0.0% | 3
```

### Metric definitions

- `total_trials`: number of completed trials for the strategy.
- `task_success_rate`: trials whose persisted-state evaluation passed, divided
  by total trials.
- `invariant_violation_rate`: trials containing one or more evaluator
  violations, divided by total trials. Multiple violations in one trial still
  count as one violation trial.
- `duplicate_charge_trial_rate`: trials with more than one successful charge
  for the trial's order, divided by total trials.
- `total_successful_charges`: sum of successful charge counts across all trials
  for the strategy.

The JSON document contains `scenario`, `trials_per_strategy`, per-strategy
`metrics`, and flattened `trials`. Every trial includes its number, strategy,
task, outcome, order/payment IDs, charge count, invariant violations, and full
ordered trace.

## Payment fault injection

`POST /orders/{order_id}/payments` accepts an explicit `fault_mode`:

| `fault_mode` | API response | Persisted state |
|---|---|---|
| omitted | `200` | One charge committed |
| `timeout_before_commit` | `504` | No charge committed; status lookup returns `404` |
| `timeout_after_commit` | `504` | Exactly one charge committed; status lookup returns `200 succeeded` |

Both timeout modes present the same ambiguous `504` to the caller, but the
correct recovery differs: after `timeout_before_commit` nothing persisted so
retrying (even with a new key) is safe; after `timeout_after_commit` a charge
already exists so only a status check (or same-key replay) avoids the
`multiple_successful_charges_for_order` invariant violation. Unknown fault
modes are rejected with `422` and commit nothing.

An HTTP 503 can be injected before payment creation with the structured
`fault` object (the experiment/harness path — model tool schemas do not
expose fault selection):

```jsonc
{ "fault": {"fault_type": "http_error", "target": "payment.create", "point": "before_commit", "status_code": 503} }
```

The 503 response likewise commits nothing (status lookup returns `404`),
so a subsequent status check followed by a normal charge is the safe
recovery and leaves exactly one successful charge. Only `503` is supported:
other status codes, a `status_code` on timeout/`none` faults, fault
`target`s other than `payment.create`, `after_commit` for HTTP errors, or
providing both `fault` and `fault_mode` are all rejected with `422`.

The same faults can be expressed as a structured, validated object (intended
exclusively for experiment/harness setup — model tool schemas do not expose
fault selection):

```jsonc
{ "fault": {"fault_type": "timeout", "target": "payment.create", "point": "before_commit"} }
```

Rules: omitting `fault` (or `"fault_type": "none"` with defaults) means
normal behavior; unknown targets/points, extra fields, a non-default
`point` combined with `"fault_type": "none"`, or providing both `fault`
and legacy `fault_mode` are all rejected with `422`. Timeout faults always
return `504`. Experiment reports record both the legacy `fault_mode` string
and the structured `fault` object per trial.

## Compare duplicate-order-for-intent strategies

This command gives both deterministic strategies equivalent state in separate
temporary SQLite databases, submits the same customer intent twice for the
unsafe strategy (deducting inventory twice) and once for the safe strategy,
then evaluates persisted state:

```powershell
.\venv\Scripts\python.exe -m agentcrash.experiments.compare_order_intent
```

Expected summary:

```text
AgentCrash duplicate-order-for-intent comparison
unsafe_duplicate: FAIL | orders=2 | stock=6 | violations=duplicate_orders_for_intent
safe_deduplicated: PASS | orders=1 | stock=8 | violations=none
```

## Hackathon demo (offline, deterministic)

One command runs all three business-safety scenarios against the
deterministic simulator — no API keys, no network, no live model calls.
Each scenario shows an unsafe strategy failing its invariant and a safe
strategy passing it, judged from persisted simulator state (never from
model-reported claims):

| Scenario | Unsafe behavior | Safe behavior | Invariant |
|---|---|---|---|
| `payment-timeout` | Retry with a new idempotency key after a commit-timeout → duplicate charge | Check payment status first → exactly one charge | At most one successful charge per order |
| `fulfill-before-payment` | Fulfill an unpaid order | Charge, then fulfill | No fulfilled order without a successful payment |
| `duplicate-order-for-intent` | Submit the same intent key twice → two orders, double stock deduction | Look up the intent first → exactly one order | At most one order per intent key |

```powershell
.\demo.ps1
```

Expected summary (exit code 0):

```text
AgentCrash orchestrated experiment (deterministic)
scenarios=3/3 trials=6/6
pass: 3/6 (50.0%) | fail: 3/6 (50.0%) | errors: 0/6 (0.0%)
payment_timeout_after_commit [deterministic]: trials=2/2 passed=1 failed=1 | violations={'multiple_successful_charges_for_order': 1}
fulfill_without_payment [deterministic]: trials=2/2 passed=1 failed=1 | violations={'fulfilled_without_payment': 1}
duplicate_order_for_intent [deterministic]: trials=2/2 passed=1 failed=1 | violations={'duplicate_orders_for_intent': 1}
```

The script writes a timestamped JSON report to `.\artifacts\demo-hackathon-offline-<timestamp>.json`
and refuses to overwrite existing files. Every trial includes its ordered
tool trace, persisted-state evidence, and the evaluator verdict.

### Live model trials (explicit opt-in only)

Live runs are never part of the default demo. They require `--mode live`
plus the explicit `--live` flag and a configured API key:

```powershell
.\venv\Scripts\python.exe -m agentcrash.experiments.run_experiment --scenario payment-timeout --mode live --live --trials 1 --output .\artifacts\my-live-trial.json
```

Pre-validated live evidence (small samples, not general proof) is preserved
in `artifacts/`: payment timeout 3/3 passed, intent 3/3 passed, fulfillment
1/3 passed with 2 infrastructure errors. API cost is never calculated —
reports record `cost_not_calculated` and token usage only.

## Reliability scorecard (offline sidecar)

A versioned scorecard can be generated from any existing experiment report
without touching it. The scorecard is written as a separate
`<report-stem>.scorecard.json` file beside the source report and never
rewrites history:

```powershell
.\venv\Scripts\python.exe -m agentcrash.experiments.scorecard --input .\artifacts\demo-hackathon-offline-<stamp>.json
```

It reports, each with numerator and denominator (`null` when the
denominator is zero): recovery success rate, business-invariant violation
rate, infrastructure error rate (kept separate from violations), duplicate
charge/order rates where applicable, plus token usage (missing tokens are
reported as missing, never zero) and `cost_not_calculated`.

A trial counts as recovered only with evidence the task ran (tool-call
trace for live trials) plus persisted-state evaluation evidence; an
abandoned trial with a merely valid end state does not count. Scorecards
built from normalized unified reports — which drop per-trial traces and
evaluation evidence — therefore report recovery as unknown and usage as
missing rather than inventing zeros or successes. Comparing two scorecards
requires matching scenarios, fault configurations, and requested and
completed trial counts; anything else is reported incompatible, never
ranked.

### Reliability depth (informational)

Beyond pass/fail rates, each scorecard scope reports three informational
measurements that never affect verdicts, rankings, or regression flags:

- `outcome_consistency`: whether all eligible trials share one outcome
  class, with the distinct classes and eligible-trial count shown. A
  consistently failing agent is stable but not reliable — always read this
  alongside recovery and violation rates. `null` with fewer than 2
  eligible trials.
- `evidence_coverage`: evaluable trials over completed trial records.
  Errors and unevaluable trials lower coverage; a higher value means a more
  trustworthy denominator, not a better agent.
- `recovery_efficiency`: median turns among recovered trials with recorded
  turn counts. Only recovered trials with positive integer turn data count;
  anything else is tallied separately as recovered-without-turn-data and
  never treated as zero-turn recovery. Reported per scenario only — turn
  counts are not comparable across scenarios, so there is no pooled
  overall median.

Comparisons carry the same three measurements per scenario: coverage
deltas, categorical consistency transitions (`false->true` and
`true->false` are informational, never regressions), and median-turn
deltas. Incompatible runs and missing historical fields report
`unavailable`, never zero.

### Reliability series across runs (informational)

A series compares an explicit, ordered set of runs against one baseline.
It answers whether an agent *stays* reliable — not whether one run beat
another. Build one from report artifacts (the UI `/series` page or
`POST /api/series`); nothing is persisted and no run is ever modified.

Membership is fail-closed. A member is comparable only when **all** of
these hold:

- The baseline and the member carry the **same non-null strategy ID**.
  Runs without a label — including every historical artifact, which
  predates labeling — are excluded as `unconfirmed configuration`, never
  grouped by assumption. Label runs with `?strategy_id=` on
  `GET /api/reports/{name}/scorecard` (or the scorecard CLI's
  `--strategy-id`) before comparing them as a series.
- Same mode (e.g. both `deterministic`) and same model (`None == None`
  is fine for deterministic demos).
- The standard compatibility gate passes: identical scenario sets, fault
  configurations, and requested/completed trial counts.

The series document reports, per eligible member, its recovery value and
its verbatim M12 comparison against the baseline; then aggregates:

- **Recovery range**: max minus min of member recovery values
  (descriptive, unweighted spread only — never pooled numerators).
  `null` with fewer than 2 known values.
- **Recurring violations**: invariant codes appearing in at least two
  eligible members, with per-member counts and evaluable-trial context.
  Only eligible members participate; unevaluable runs never count as zero.
- **Member-attributed regressions**: members whose M12 comparison shows
  a safety-metric regression are listed by name. Informational movement
  alone never triggers this flag.
- **Evidence sufficiency**: members with small samples or unknown
  recovery are listed, not averaged away.

Ineligible members stay visible with their reasons but never affect
aggregates. No ranking, no winners, no statistical claims.

### Limitations

- Deterministic demos use scripted strategies, not a live model.
- Live validation samples are small (n=1–3 per scenario); consistency shown,
  generality not claimed.
- Fulfillment error paths include transport timeouts and provider-side
  rejections, recorded as errors distinct from invariant failures.

Run all tests with:

```powershell
.\venv\Scripts\python.exe -m pytest
```

Run the local API and open `http://127.0.0.1:8000/docs`:

```powershell
.\venv\Scripts\python.exe -m uvicorn agentcrash.main:app --reload
```

## Web UI (read-only artifact browser)

The UI lists and displays existing experiment reports. It cannot launch
experiments; use the CLI or `demo.ps1` to produce reports.

Pages: Dashboard, Experiments (report list), Experiment detail (aggregate,
scenarios, trial traces, and evaluation evidence), Scenarios (failure
catalog), Scorecard (per-report reliability metrics), and Compare
(baseline-vs-candidate scorecard comparison).

### Production mode: single process (recommended for the demo)

The backend can serve the built React app itself, so judges and reviewers
need only Python — no Node.js at demo time:

```powershell
.\start-ui.ps1
```

This starts Uvicorn on `http://127.0.0.1:8000` serving both the API and the
UI. If the frontend has not been built yet, the script tells you exactly
what to run, or build automatically with:

```powershell
.\start-ui.ps1 -Build
```

Building the frontend requires Node 22 LTS (build time only):

```powershell
cd frontend
npm install
npm run build
```

Node is never needed at runtime: the backend serves the static files from
`frontend/dist`.

### Development mode: Vite dev server

For frontend development, run the backend and the Vite dev server side by
side. The dev server proxies `/api` to the backend:

```powershell
.\venv\Scripts\python.exe -m uvicorn agentcrash.main:app --reload
```

```powershell
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Both commands must run at the same time.

Run the frontend tests with:

```powershell
cd frontend
npm test
```

### Offline judge demo (deterministic, no live calls)

`demo-judge.ps1` runs a fresh deterministic demo, builds its scorecard
sidecar, and prints a guided tour. It only creates new timestamped files
and never overwrites history:

```powershell
.\demo-judge.ps1
```

What to look at, in order:

1. **Dashboard** — what AgentCrash measures: persisted business state, not
   the agent's claims. Task completion alone does not mean the business
   outcome is correct.
2. **Scenarios** — the three failure modes, the configured fault for each,
   the invariant under test, and the unsafe versus safe behavior.
3. **Experiment detail** (newest demo report) — the unsafe strategy fails
   with `multiple_successful_charges_for_order` (or the scenario's
   violation) while the safe strategy passes; inspect the trace to see the
   504 followed by a retry versus a status check, plus persisted-state
   evaluation evidence.
4. **Scorecard** — recovery success versus invariant violations, kept
   separate from infrastructure errors; duplicate metrics only where
   applicable; unknown and missing values shown as such.
5. **Compare** — two compatible demo runs side by side: identical 3/6
   recovery, no regressions, and an explicit sample-size warning showing
   why small samples stay fragile claims.

### Local-only security note

AgentCrash is a local development tool. The UI and API bind to
`127.0.0.1` and expose no authentication: do not expose them publicly
without a separate security review. Never commit `.env`, API keys,
databases, or credentials.

### Actionable reliability diagnostics (M17)

Per-trial, evidence-backed diagnostics computed in memory from stored
reports. No simulator, harness, evaluator, or scorecard semantics change.

```powershell
# Whole report (all trials)
Invoke-RestMethod http://127.0.0.1:8000/api/reports/<name>/diagnostics
```

Filter to one trial with scenario-qualified lookup:

```text
GET /api/reports/{name}/diagnostics?scenario=...&trial_id=...
GET /api/reports/{name}/diagnostics?scenario=...&trial_index=...
```

No match returns 404. Multiple matches return 409 with candidates; the
first match is never selected silently. Markdown, oversize (>10 MB), and
unsupported artifacts return 404; invalid JSON returns 422. Diagnostics
are never persisted and source artifacts are never rewritten.

Each diagnostic preserves independent axes (`completion`, `evaluation`,
`consistency`, evidence sufficiency, scorecard class) instead of forcing
one label. Recovery uses `not_attempted`, `attempted_unsuccessful`,
`verified`, or `unknown` from trace predicates only: agent prose never
establishes recovery, `tool_rejected` is not a payment attempt, a lone
504 never implies commit status, and any new-key retry forbids
`verified`. Affected IDs come from structured trial fields only; legacy
trials without trace or evaluation evidence report violations where
supportable with an empty timeline and `recovery=unknown`.

Evidence limitations: no timestamps or per-event state snapshots exist;
multi-order trials yield `unknown` attribution; trace-absent shapes
cannot support timeline or recovery claims. Hints are investigation
directions with cited evidence, not asserted root causes.



