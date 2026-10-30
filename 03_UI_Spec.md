# AgentCrash — UI and Visual Design Specification

**Status:** Future dashboard design reference; dashboard is out of MVP scope  
**Last updated:** 2026-10-25

## 1. Purpose and principles
AgentCrash currently exposes a local API and tests. This spec defines a restrained visual system for a future trace/comparison dashboard. Do not build a frontend in the current deterministic runner milestone.

Principles: developer-focused, data-dense, calm; distinguish observed state from agent claims; never use color alone for status; prioritize readable traces and copyable IDs; never imply a pass if evaluation did not complete.

## 2. Color tokens
These are proposed tokens, not existing implementation details.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#F8FAFC` | `#0B1220` | Page background |
| `--surface` | `#FFFFFF` | `#111827` | Panels |
| `--surface-muted` | `#F1F5F9` | `#1F2937` | Secondary surfaces |
| `--text` | `#0F172A` | `#F8FAFC` | Primary text |
| `--text-muted` | `#475569` | `#94A3B8` | Secondary text |
| `--border` | `#CBD5E1` | `#374151` | Borders |
| `--primary` | `#2563EB` | `#60A5FA` | Actions and links |
| `--success` | `#15803D` | `#4ADE80` | Pass/valid |
| `--warning` | `#A16207` | `#FBBF24` | Warning/incomplete |
| `--danger` | `#B91C1C` | `#F87171` | Violations/failure |
| `--info` | `#0369A1` | `#38BDF8` | Informational |
| `--focus` | `#7C3AED` | `#A78BFA` | Keyboard focus |

Use text labels such as `PASS`, `FAIL`, `INCOMPLETE`, and `ERROR` alongside color.

## 3. Typography
- UI: `Inter`, fallback `ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`.
- Code/IDs/traces: `ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace`.

| Token | Size | Line height | Use |
|---|---:|---:|---|
| `--font-xs` | 12px | 16px | Timestamps/helper text |
| `--font-sm` | 14px | 20px | Tables/secondary text |
| `--font-md` | 16px | 24px | Body |
| `--font-lg` | 18px | 28px | Section headings |
| `--font-xl` | 24px | 32px | Page headings |
| `--font-2xl` | 30px | 36px | Run summary |
| `--font-mono` | 13px | 20px | IDs, JSON, trace |

## 4. Spacing and layout
Base unit: 4px.

| Token | Value |
|---|---:|
| `--space-1` | 4px |
| `--space-2` | 8px |
| `--space-3` | 12px |
| `--space-4` | 16px |
| `--space-5` | 20px |
| `--space-6` | 24px |
| `--space-8` | 32px |
| `--space-10` | 40px |
| `--radius-sm` | 4px |
| `--radius-md` | 8px |
| `--radius-lg` | 12px |

- Main content max width: 1200px.
- Desktop page padding: 24px; mobile: 16px.
- Panel gap: 16px.
- Keep tables scrollable on narrow screens rather than hiding critical fields.

## 5. Theme rules
- Default to system preference when a frontend is built.
- Provide explicit light/dark toggle.
- Define every token in both themes.
- Verify WCAG AA contrast for normal text before release.
- Keep keyboard focus visible in both themes.
- Respect reduced-motion settings.

## 6. Core components and states
### Run status
- `PASS`: evaluation completed and no configured invariant violations found.
- `FAIL`: evaluation completed and one or more invariant violations found.
- `INCOMPLETE`: run stopped before meaningful evaluation.
- `ERROR`: infrastructure/setup/internal error prevented valid evaluation.

### Buttons
Primary, secondary, destructive. States: default, hover, focus-visible, active, disabled, loading. Confirm destructive data deletion.

### Trace event
Show sequence number, timestamp/elapsed time, action, request summary, HTTP status/exception, recovery decision, and relevant order/payment IDs.

### Violation panel
Show stable code, plain-language explanation, affected IDs, observed versus expected state, and evidence from persisted state.

### Empty/error states
- No runs: explain how to execute the local comparison command.
- No violations: say no configured invariants were violated; do not claim general correctness.
- No trace: mark incomplete/error.
- API unavailable: show connection error and retry action.
- Invalid configuration: identify field and validation message.
- Evaluation failure: distinguish evaluator error from a business violation.
- Missing run: show not found and return navigation.

## 7. Accessibility
Keyboard navigation, visible focus, semantic headings/table headers, status text in addition to color, reduced-motion support, and no animation-only state communication.

## 8. Scope guard
No React UI, dashboard, component library, or theme implementation is required for the current MVP.
