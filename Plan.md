# AgentCrash — Master Project Plan

**Status:** Working plan  
**Last updated:** 2026-10-25

## 1. Goal
Build a reproducible crash-test laboratory for AI agents that operate business APIs. Inject controlled failures, capture agent actions, and evaluate persisted business state. The first demonstration is a payment timeout after commit that can cause a duplicate charge if an agent retries unsafely.

## 2. Principles
1. Understand and test each layer before adding the next.
2. Deterministic baselines before model-powered behavior.
3. Persisted state is the source of truth.
4. Every experiment must be reproducible and isolated.
5. Keep scope narrow until the first end-to-end demonstration works.
6. Make no novelty/superiority claims without evidence.
7. The developer must understand and run each milestone before moving on.

## 3. Current status

### Milestone 1 — deterministic simulator and evaluator
**Reported implemented; latest reported test result: 8 passed.**

- [x] FastAPI application skeleton.
- [x] Inventory/order/payment simulation.
- [x] SQLite persistence.
- [x] Same-key idempotent retry.
- [x] `timeout_after_commit` fault.
- [x] Payment-status lookup.
- [x] Persisted-state invariant evaluation.
- [x] Duplicate successful charges detected per order.
- [x] Tests for payment and evaluator behavior.

**Caveat:** This result comes from the developer/Codex report; it has not been independently rerun while creating this document.

### Milestone 1.5 — developer understanding and manual verification
**Current focus; do not skip.**

- [ ] Run full pytest suite and inspect verbose test names.
- [ ] Start Uvicorn and inspect `/docs`.
- [ ] Manually create inventory and an order.
- [ ] Trigger timeout and inspect payment status.
- [ ] Run evaluator and understand its output.
- [ ] Confirm tests use isolated state and do not alter the default DB.
- [ ] Reconcile docs with actual `pyproject.toml`, API routes, and SQLAlchemy models.
- [ ] Developer can explain one request from API through service/database to evaluator.

**Exit condition:** The developer can reproduce and explain the core scenario without Codex doing all the work.

### Milestone 2 — deterministic experiment runner
**Implemented; verified locally with the full test suite.**

- [x] Add unsafe-retry baseline.
- [x] Add safe-status-check baseline.
- [x] Give each run equivalent but isolated initial state.
- [x] Capture ordered request/response/decision trace.
- [x] Evaluate persisted state after each strategy.
- [x] Emit structured JSON results.
- [x] Add one command to compare strategies.
- [x] Add end-to-end tests for contrasting outcomes.

**Acceptance:** Unsafe strategy produces two successful charges and is flagged; safe strategy leaves one successful charge and passes relevant invariants.

### Milestone 3 — repeatable deterministic benchmark
**Implemented; verified locally with the full test suite.**

- [x] Run both deterministic strategies for a configurable trial count.
- [x] Give every trial/strategy pair fresh, equivalent SQLite state.
- [x] Preserve the deterministic timeout scenario and Milestone 2 behavior.
- [x] Include trial number, outcome, payments, violations, and trace per trial.
- [x] Calculate per-strategy success, violation, duplicate-charge, and charge metrics.
- [x] Export complete readable JSON to a caller-selected path.
- [x] Reject non-positive trial counts before creating state.
- [x] Add repeatability, isolation, metrics, export, and compatibility tests.
- [x] Document the benchmark command, output, and metric definitions.

Metric definitions:
- Task success rate: passed persisted-state evaluations / total trials.
- Invariant-violation rate: trials with any evaluator violation / total trials.
- Duplicate-charge trial rate: trials with more than one successful charge for
  the order / total trials.
- Total successful charges: sum of successful charge counts across trials.

Further scenario/trace hardening:
- [ ] Add stable run IDs if results are persisted beyond one command.
- [ ] Represent setup, strategy, and evaluator execution errors as structured results.

### Milestone 4 — model-powered strategy
**Wait until deterministic runner is stable and Meta API access is confirmed.**

- [ ] Confirm current API access, limits, and documentation.
- [ ] Add a small provider adapter outside core simulator logic.
- [ ] Keep credentials in environment variables.
- [ ] Add a model-powered strategy with a restricted tool set.
- [ ] Add timeouts, bounded retries, and usage/cost accounting.
- [ ] Preserve no-network deterministic tests.
- [ ] Compare against baselines on identical scenarios.
- [ ] Keep evaluator independent of the model's self-report.

### Milestone 5 — evaluation experiment
- [ ] Choose a small set of reproducible scenarios.
- [ ] Compare hand-written baseline tests and any proposed AI-guided generation fairly.
- [ ] Use comparable test budgets.
- [ ] Report task completion, invariant violations, false-success rate, recovery success, scenario validity, cost, and latency where measured.
- [ ] Include limitations and negative results.
- [ ] Avoid claims of novelty or superiority unless data supports them.

### Milestone 6 — demo and documentation
- [ ] Main experiment runs with one documented command.
- [ ] Show unsafe and safe traces.
- [ ] Show evaluator output from persisted state.
- [ ] Provide README and architecture diagram.
- [ ] Prepare demo script with clean setup and expected output.
- [ ] Freeze scope before polishing.

### Future only if justified
Dashboard/trace viewer, additional fault families, more business domains, hosted runs, multi-user auth/RLS, SaaS features. None is required for the first end-to-end demo.

## 4. Proposed repository layout after Milestone 2
```text
agentcrash/
├── 01_PRD.md
├── 02_AppFlow.md
├── 03_UI_Spec.md
├── 04_TRD.md
├── 05_Schema.md
├── Plan.md
├── README.md
├── pyproject.toml
├── src/agentcrash/
│   ├── main.py
│   ├── api.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── services.py
│   ├── faults.py
│   ├── evaluator.py
│   └── experiments/
│       └── compare_payment_recovery.py  # planned, not implemented
└── tests/
    ├── conftest.py
    ├── test_payments.py
    ├── test_evaluator.py
    └── test_experiment_runner.py        # planned, not implemented
```

## 5. Working agreement for coding-agent tasks
1. Ask Codex to inspect before editing.
2. Ask it to explain the proposed change briefly.
3. Keep each task to one milestone.
4. Require tests for new behavior.
5. Require the full test suite to run.
6. Ask for changed files, commands, actual results, and limitations.
7. Review diffs; do not accept test claims without output.
8. Manually run/inspect behavior where practical.
9. Update this plan after changes are understood and verified.

Codex is an implementation assistant, not the product owner. The developer decides scope and architecture.

## 6. MVP definition of done
- [x] Local setup is repeatable and documented.
- [x] Simulator/evaluator tests pass.
- [x] Safe and unsafe strategies use equivalent isolated state.
- [x] Unsafe run creates duplicate charge and evaluator violation.
- [x] Safe run creates one successful charge and no relevant violations.
- [x] Ordered traces show timeout and recovery decision.
- [x] Results come from actual persisted state.
- [ ] Developer can explain and reproduce the demo.
- [x] No real payments, customer data, or required model API calls.
