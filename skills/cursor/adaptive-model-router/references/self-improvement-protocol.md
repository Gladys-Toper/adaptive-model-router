# Self-improvement protocol (Cursor, pass 1)

The Codex reference implements a full evidence-gated catalog-experiment,
promotion, canary, and rollback pipeline (`stage`, `evaluate`, `promote`,
`monitor`, `rollback`) bound to first-party App Server receipts. Cursor's
adaptation ships **no scored-experiment or promotion machinery in pass 1** —
this is a deliberate, honestly-scoped cut, not an oversight:

- Cursor's headless surface has not had its result-metadata fidelity verified
  by a live probe (see `references/dispatch-contract.md`). Building a
  promotion pipeline on unverified evidence would be worse than shipping none.
- The Cursor CLI's `--model` slug space changes shape often (new families,
  new bracket parameters); a curated catalog with a manual review step is
  more honest than an automated "current-family" inference tuned for a single
  vendor's version-numbering scheme.

## What `router_lab.py` does instead

- `refresh` re-derives the catalog from a live `cursor-agent models` probe (or
  an offline fixture via `CURSOR_MODEL_CATALOG`), confirms every seeded tier
  slug still exists, and writes `catalog.json` + `active-policy.json` into
  `~/.cursor/adaptive-model-router/`. It never auto-selects a new tier
  assignment from catalog metadata alone.
- `status` / `doctor` report whether the active policy resolves and whether
  the catalog is fresh, using the same `routing_status` /
  `workflow_routing_ready` split the Codex and Claude routers use, so
  `resolve-phase` keeps serving a healthy policy through catalog maintenance.
- `resolve-phase` uses the same activity/mutation/scope/ambiguity/risk-tag
  tier-floor heuristic as the Codex reference (a platform-neutral reasoning
  taxonomy, not a model-specific one) to pick T0-T4, then binds that tier to
  the seeded Cursor model slug.
- Every model-bearing resolution is `REQUESTED_NOT_ATTESTED` with
  `runtime_attestation_required: true`. There is no path in this skill to
  clear that requirement, so no experiment can ever produce promotable
  evidence — `BLOCKED_MODEL_ENFORCEMENT` is permanent for pass 1.

## Changing the seeded tiers

Tier reassignment is a manual edit to `assets/default-policy.json` plus a
`router_lab.py refresh` to confirm the new slugs are live, reviewed like any
other code change. Promote this to an evidence-gated pipeline only after a
founder-authorized live probe establishes what headless result metadata
Cursor's CLI actually returns.
