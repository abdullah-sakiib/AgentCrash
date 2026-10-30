# AgentCrash — Technical Requirements Document

**Status:** Proposed implementation baseline; reconcile with the current environment before locking  
**Last updated:** 2026-10-25

## 1. Architecture
Use a modular Python monolith: FastAPI HTTP layer, Pydantic validation, SQLAlchemy persistence, SQLite for isolated local runs, explicit fault injection, a persisted-state evaluator, deterministic runner, and pytest tests.

No external model API is required for the deterministic MVP.

## 2. Technology baseline
These are proposed pins, not a claim that these exact versions are currently installed. The existing `pyproject.toml` and environment are authoritative until reconciled. Do not blindly upgrade/downgrade; pin and test deliberately.

| Technology | Proposed version/policy | Purpose |
|---|---|---|
| Python | 3.12.x target; work has reportedly also been tested on 3.14.0 | Runtime |
| FastAPI | `0.115.12` | HTTP API |
| Uvicorn | `0.34.0` | ASGI server |
| Pydantic | `2.11.3` | Validation |
| SQLAlchemy | `2.0.40` | ORM |
| pytest | `8.3.5` | Tests |
| httpx | `0.28.1` | API/test client |
| Build backend | retain current `pyproject.toml` backend unless there is a reason to change | Packaging |

Before calling versions locked, reconcile actual dependencies, pin the selected versions, and maintain a reproducible lock with the project's chosen tooling.

## 3. Current folder structure
```text
agentcrash/
├── pyproject.toml
├── .gitignore
├── agentcrash.db                 # local runtime data; do not commit
├── src/agentcrash/
│   ├── main.py
│   ├── api.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── services.py
│   ├── faults.py
│   └── evaluator.py
└── tests/
    ├── conftest.py
    ├── test_payments.py
    └── test_evaluator.py
```

Proposed additions, only after approval:
```text
src/agentcrash/experiments/
├── __init__.py
└── compare_payment_recovery.py
tests/
└── test_experiment_runner.py
```

## 4. Third-party APIs
### Current MVP
- No external API calls.
- No real payment processor.
- Local FastAPI API and SQLite only.

### Future model integration
When access is available and deterministic baselines are stable:
- isolate the model provider behind a small adapter;
- store credentials in environment variables;
- never hardcode or log API keys;
- add timeouts, bounded retries, and cost/usage accounting;
- preserve no-network deterministic tests;
- do not let the model self-certify correctness.

No model endpoint or model identifier is locked here.

## 5. Security and reliability
- Never call a real payment provider or move real money.
- Never use real customer data or secrets in fixtures.
- Do not commit `.env`, keys, runtime databases, virtual environments, or logs.
- Validate API input with schemas.
- Use ORM/parameterized operations; do not concatenate untrusted input into SQL.
- Do not leak stack traces or secrets in public API responses.
- Redact credentials from traces.
- Isolate each experiment's database/state.
- Keep fault injection explicit and deterministic.
- Keep evaluator read-only with respect to business records.
- Distinguish setup errors from evaluated agent failures.
- Add migrations before changing an existing persistent schema.
- Do not infer production concurrency guarantees from SQLite behavior.

## 6. Testing strategy
- Unit tests for services, idempotency, and fault injection.
- Evaluator tests using persisted records.
- API tests for status codes and validation.
- End-to-end safe/unsafe recovery tests.
- Regression tests for stable violation codes and trace ordering.
- Run full pytest after behavioral changes.
- Tests must not rely on execution order or share the developer's persistent database.

## 7. Experiment result format
Each result should be JSON-serializable and include run/scenario IDs, strategy, setup/execution status, task outcome, order ID, payment IDs, successful charge count, invariant violations with stable codes, ordered trace, configuration/seed where relevant, and measured duration/model usage where applicable.

Do not hardcode expected results instead of evaluating actual state.

## 8. Performance boundaries
SQLite and synchronous handlers are appropriate for the initial local simulator. Do not optimize for high concurrency until a demonstrated need exists.

## 9. Next-milestone definition of done
- One command executes both deterministic baselines.
- Runs have equivalent but isolated initial state.
- Unsafe strategy creates two successful charges and is flagged.
- Safe strategy queries status and leaves one successful charge.
- Trace ordering is tested.
- Full tests pass.
- README documents setup and expected output.
