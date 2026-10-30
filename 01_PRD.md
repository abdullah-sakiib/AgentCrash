# AgentCrash — Product Requirements Document

**Status:** Draft for MVP  
**Last updated:** 2026-10-25

## 1. Product summary
AgentCrash is a developer tool for stress-testing AI agents that interact with business APIs. It runs agents against a deterministic simulated business environment, injects controlled failures, and evaluates persisted business state rather than trusting the agent's explanation.

The first environment is a small e-commerce system with inventory, orders, and simulated payments. The first failure scenario is a payment that commits successfully but returns HTTP 504 to the caller.

## 2. Problem statement
An agent can receive an error even though a business operation succeeded. If it assumes the operation failed and repeats it incorrectly, it may create harmful outcomes such as duplicate charges or inconsistent order state. Ordinary happy-path tests often miss these problems.

Developers need a reproducible way to create realistic failure conditions, run strategies under identical conditions, inspect tool-call traces, verify outcomes against business invariants, and compare strategies with repeatable metrics.

## 3. Target personas
**Primary:** AI agent engineer or developer building an agent that calls business APIs.  
**Secondary:** QA, reliability, or platform engineer responsible for testing agent-driven workflows.

## 4. MVP scope
- Deterministic e-commerce simulator.
- Inventory setup and order creation.
- Simulated payment transactions with idempotency keys.
- `timeout_after_commit`: payment commits, then response fails with HTTP 504.
- Payment-status lookup.
- Persisted-state invariant evaluator.
- Deterministic baseline strategies: unsafe retry with a new key; safe status lookup after timeout.
- Isolated runs with equivalent initial state.
- Ordered trace of actions, responses, and recovery decisions.
- CLI or simple command to compare results.
- Automated tests and local setup documentation.

## 5. MVP user stories
- As a developer, I can seed inventory and create an order for an agent task.
- I can trigger a payment timeout after commit deterministically.
- I can retry with the same key and verify the original payment is returned.
- I can simulate an unsafe retry with a different key and observe a second charge.
- I can evaluate persisted state and receive stable violation codes.
- I can compare safe and unsafe strategies from equivalent initial conditions.
- I can inspect a trace to understand why a run passed or failed.
- I can run tests locally and reproduce the result.

## 6. Success metrics
### Functional acceptance
- Same-key retries do not create another transaction.
- A different-key duplicate charge can be represented.
- The evaluator detects multiple successful charges for one order.
- The safe baseline ends with exactly one successful charge and no relevant invariant violations.
- The unsafe baseline ends with two successful charges and a duplicate-charge violation.
- Runs are isolated and equivalent at start.
- Each run produces an ordered trace and structured result.

### Research/product validation
Measure, do not assume: scenario validity rate, invariant violation detection, recovery success rate, false-success rate, discovery value beyond a baseline test budget, and cost/latency for future model-powered runs. Do not claim superiority over existing benchmarks without controlled evidence.

## 7. Strict non-goals
Not part of the MVP:
- Real payment processing or real money.
- Production e-commerce functionality.
- Generic support for every business API or a universal agent framework.
- Autonomous production testing or real customer data.
- Authentication, multi-tenancy, or hosted SaaS.
- A full dashboard or polished frontend.
- High-concurrency provider simulation, refunds, charge reversals, shipping, tax, coupons, or subscriptions.
- Random faults in the deterministic baseline.
- Model API integration before deterministic baselines work.
- Claims of novelty or superiority without evaluation evidence.

## 8. Constraints and principles
- Determinism first.
- Persisted state is the source of truth.
- Faults are explicitly configured and traceable.
- Keep the first vertical slice small.
- Never connect a real payment provider.
- Keep secrets out of traces and logs.
- Make failures explainable with stable violation codes.

## 9. MVP acceptance checklist
- [ ] Existing simulator tests pass.
- [ ] One command runs both baseline strategies end to end.
- [ ] Runs use isolated databases/state.
- [ ] Unsafe run produces a duplicate-charge violation.
- [ ] Safe run produces one successful charge and passes relevant invariants.
- [ ] Trace ordering is tested.
- [ ] README documents setup, commands, and expected output.
- [ ] Deterministic MVP requires no external model API.
