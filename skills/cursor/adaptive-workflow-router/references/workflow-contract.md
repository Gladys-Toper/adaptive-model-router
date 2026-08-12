# Workflow contract (Cursor)

`workflow_plan.py` in this tree is **byte-identical** to the Codex reference
(`adaptive-workflow-router/scripts/workflow_plan.py`). It is genuinely
platform-neutral: its only two couplings are the sibling model-router path
(resolved relative to this skill's own directory, so it lands on
`../adaptive-model-router/scripts/router_lab.py`, i.e. the Cursor router) and
schema-validation of that router's `resolve-phase` response. Every planner
invariant — `PLANNER_CONTRACT_VERSION`, `RUNTIME_ACTIVATION_CONTRACT_NAME`,
`PHASE_RESULT_CONTRACT_NAME`, `FORBIDDEN_ROUTING_KEYS`, `ROUTE_FIELDS`,
`CATALOG_FIELDS`, `WORKFLOW_FIELDS`, `PHASE_FIELDS`, `ACTIVITIES`, `ORDERS`,
`UNVERSIONED_SOURCE_COMMIT` — is identical to Codex's, satisfying the
cross-platform contract test's frozen-name and identical-invariant
assertions.

`assets/workflows.json` and `assets/resumption-policy.json` are vendored
byte-identical from `core/workflow/assets/`. Workflow definitions contain no
model, provider, tier, agent, effort, or fallback keys — routing is always
resolved through `adaptive-model-router` at plan time, never declared in the
workflow source. `workflow_plan.py`'s own catalog validator enforces this
(`FORBIDDEN_ROUTING_KEYS`).

## What is Cursor-specific here

**Dispatch is a documented contract, not a scripted driver in pass 1.**
Codex's `workflow_dispatch.py` drives the pinned App Server binary over
JSON-RPC and issues hash-chained execution receipts. Cursor ships no analog
of that file. Instead:

- `workflow_plan.py plan` and `bind` work exactly as documented in the Codex
  skill: `plan` resolves every routed phase against the live Cursor router
  and produces a dispatch plan; `bind` binds one active routed phase to an
  exact, hash-verified prompt/context/runtime packet.
- What consumes a bound dispatch packet is the parent Cursor agent itself,
  using one of the two surfaces in
  `../adaptive-model-router/references/dispatch-contract.md` (in-session Task
  tool, or documented headless `cursor-agent -p --model <slug>`). There is no
  `workflow_dispatch.py check`/`run` command surface, because there is no
  receipt-issuing driver to health-check.
- `activate-runtime` and `verify-plan` work identically to Codex — they are
  pure functions over retained JSON evidence files and do not touch any
  dispatch surface.
- **No resumption lane**, same as Claude's port: the Cursor CLI/Task surface
  has no analog of `turn/steer` + `turn/interrupt` on an active turn, so the
  explicit-checkpoint-restart protocol (`--resumable` in `bind`) is not
  operationally usable even though the flag and its validation are present
  (inherited from the byte-identical planner). An interrupted phase is
  `ABORTED_NO_RESUMABLE_CHECKPOINT`; state it plainly rather than implying
  Cursor supports it.

## Evidence grade

Every model-bearing `resolve-phase` response from the Cursor router is
`REQUESTED_NOT_ATTESTED` with `runtime_evidence_required: true` and
`runtime_attestation_required: true` — there is no configured attestation
verifier and none is claimed. Scored, promotion-gated experiments stay
`BLOCKED_MODEL_ENFORCEMENT` permanently in pass 1 (see the model router's
`references/self-improvement-protocol.md`). This grade is deliberately
weaker than Claude's `declared` (first-party observed CLI stream) because
Cursor's pass-1 adaptation dispatches through the in-session Task tool by
default, which produces no retained, hashable transcript at all — the parent
agent's own observation of the turn is the only evidence, and it is not
retained or verifiable after the fact.

## Scope guard

Same as every platform: this workflow router composes and routes Cursor's
own working-agent phases only. It never selects a model for a user-facing or
product surface, and it is fully independent of the separate
`liberai-*-router` skill stack (different state home, different catalog,
different install location).
