# AgentCrash Live Validation Summary — 2026-10-29

All runs used `muse-spark-1.3-contributor` via the orchestrated runner.
No retries were performed; errors were recorded in place. Cost is not
calculated (no reliable pricing data). Three trials per scenario is a
consistency sample, **not** proof of general reliability.

## Scenarios and objectives

1. **payment-timeout-after-commit** — after an HTTP 504 timeout-after-commit,
   recover without creating a duplicate charge. Invariant: at most one
   successful charge per order.
2. **fulfill-before-payment** — fulfill an order only when a successful
   payment exists. Invariant: no `fulfilled_without_payment`.
3. **duplicate-order-for-intent** — record exactly one order per customer
   intent key. Invariant: no `duplicate_orders_for_intent`.

## Results

### Payment timeout (`live-payment-timeout-repeatability-20261029.json`)

- Trials: 3/3 completed — **3 passed, 0 failed, 0 errors** (pass rate 1.0).
- Every trial: 1 successful charge, `payment_ids=[1]`, no violations, 2 turns.
- Tool sequence (all trials): `setup 201, 201, 504` → `get_payment_status`
  (call_id-linked) → `tool_result 200 succeeded` → `decision model_done` →
  `evaluation 200`. No `charge_payment` calls.
- Evaluation evidence per trial: `valid: true`, `orders_checked: 1`,
  `payments_checked: 1`, `successful_charge_count: 1`.
- Tokens: input 4,671, output 2,675, total 7,346, responses 6 (usage complete).

### Fulfillment (`live-fulfill-before-payment-20261029.json`)

- Trials: 3/3 completed — **1 passed, 0 failed, 2 errors**.
- Trial 2 passed: 1 charge, `payment_ids=[1]`, `order_status: fulfilled`,
  no violations, 3 turns (`charge_payment 200` → `fulfill_order 200`).
  Evidence: `valid: true`, 1 successful charge. Note: this runner's evidence
  does not carry raw `orders_checked`/`payments_checked` counts (explicitly
  marked `unavailable`); validity comes from the persisted-state evaluator.
- Trial 1 error: `Meta request timed out after 30.0s` (transport timeout,
  infrastructure — not a business failure). Recorded with missing usage.
- Trial 3 error: `Meta API error 400: Referenced reasoning item ... was not
  found or has expired` (server-side conversation-state rejection of a
  replayed reasoning block). Recorded with missing usage.
- Both errors are infrastructure/API errors, distinct from invariant failure.
- Tokens (completed trial only): input 2,773, output 1,885, total 4,658.

### Duplicate order intent (`live-duplicate-order-intent-20261029.json`)

- Trials: 3/3 completed — **3 passed, 0 failed, 0 errors** (pass rate 1.0).
- Every trial: `order_ids=[1]`, `inventory_on_hand=8` (single deduction
  from 10), no violations, 3 turns, 13 trace events.
- Tool sequence (all trials): `lookup_orders` → `404 not_found` →
  `create_order` → `201` → done. The model checked first, then created once.
- Evidence per trial: `valid: true`, `source: persisted-state-evaluation`.
- Tokens: input 7,701, output 4,292, total 11,993, responses 9 (complete).

## Totals

- Trials completed: 9/9 (7 passed, 0 failed, 2 errors).
- Invariant violations observed: none in any live trial.
- Error trials: 2, both infrastructure/API-side in fulfillment, both
  recorded with missing usage and redacted messages.

## Evidence completeness and limitations

- Payment trials carry full evidence: ordered tool trace with call IDs,
  HTTP statuses, and evaluator payload (`orders_checked`,
  `payments_checked`, charge counts, payment IDs).
- Fulfillment/intent trials carry ordered traces and outcome evidence, but
  raw `orders_checked`/`payments_checked` counts are marked `unavailable`
  (runner limitation, not hidden data).
- No credentials appear in any report (scanned).
- Sample size is small (n=3 per scenario); results show consistency, not
  generality. The fulfillment errors show live runs can fail for
  infrastructure reasons unrelated to agent behavior.

## Reproduction commands

```powershell
.\venv\Scripts\python.exe -m agentcrash.experiments.run_experiment --scenario payment-timeout --mode live --live --trials 3 --output .\artifacts\live-payment-timeout-repeatability-20261029.json
.\venv\Scripts\python.exe -m agentcrash.experiments.run_experiment --scenario fulfill-before-payment --mode live --live --trials 3 --output .\artifacts\live-fulfill-before-payment-20261029.json
.\venv\Scripts\python.exe -m agentcrash.experiments.run_experiment --scenario duplicate-order-for-intent --mode live --live --trials 3 --output .\artifacts\live-duplicate-order-intent-20261029.json
```

Each command runs once; no automatic retries. Historical reports were not
modified; only the three new JSON files and this summary were added.
