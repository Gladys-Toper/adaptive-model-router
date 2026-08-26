# Workflow contract (Cursor)

`workflow_plan.py` in this tree shares the core planner schema with the Codex
reference (`adaptive-workflow-router/scripts/workflow_plan.py`). Its platform
neutral planner invariants and its only two router couplings are the sibling model-router path
(resolved relative to this skill's own directory, so it lands on
`../adaptive-model-router/scripts/router_lab.py`, i.e. the Cursor router) and
schema-validation of that router's `resolve-phase` response. Every planner
invariant — `PLANNER_CONTRACT_VERSION`, `RUNTIME_ACTIVATION_CONTRACT_NAME`,
`PHASE_RESULT_CONTRACT_NAME`, `FORBIDDEN_ROUTING_KEYS`, `ROUTE_FIELDS`,
`CATALOG_FIELDS`, `WORKFLOW_FIELDS`, `PHASE_FIELDS`, `ACTIVITIES`, `ORDERS`,
`UNVERSIONED_SOURCE_COMMIT` — remain aligned with the Codex planner contract.

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

- `workflow_plan.py plan` and `bind` share the core pure planner schema:
  `plan` resolves every routed phase against the live Cursor router and
  produces a dispatch plan; `bind` binds one active routed phase, including
  structured declared/derived/effective token-cap data, to an immutable packet.
- A bound dispatch packet is planner evidence only. Cursor retains no terminal
  execution receipt or transcript that can prove the requested route ran,
  enforce its cap, or support a recovery decision.
- There is deliberately no `workflow_dispatch.py` `check`/`run` command,
  `pivot-from-receipt` CLI, receipt validator, or runtime-action command. The
  Cursor projection must fail closed rather than claim that unsupported runtime
  enforcement, repair, resumption, or pivoting occurred.
- `activate-runtime` and `verify-plan` work identically to Codex — they are
  pure functions over retained planner evidence files and do not touch any
  dispatch surface. They cannot validate an unretained Cursor runtime receipt.
- **No resumption lane**, same as Claude's port: the Cursor CLI/Task surface
  has no analog of `turn/steer` + `turn/interrupt` on an active turn, so
  `bind --resumable` is rejected with `ABORTED_NO_RESUMABLE_CHECKPOINT`. An
  interrupted phase must be reported plainly; the projection never mints an
  unenforceable resumption contract.

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

## Shared T4 consultation contract

Every grounded cognitive failure deterministically selects one of two
read-only T4 outcomes: `t4_consult` consumes an immutable precomputed,
complete, internally consistent evidence bundle with offline/read-only scope,
`tool_mode: none`, one model cycle, zero tool cycles, and strict token/wall
caps. `t4_diagnose` is selected only for incomplete or contradictory evidence;
it is network-disabled with a repository-read-only filesystem, exact
repository/path scope, bounded read-only tools only, no mutation tools, and
strict tool-cycle, token, model-cycle, and wall-time caps. An authority blocker
produces `ask_user`, not another model dispatch. Neither outcome may authorize
or perform a mutation, deployment, external action, or other state change; its
result is bounded direction only. An actual mutation requires a freshly
planned and bound T3-or-lower packet that consumes and revalidates that
direction contract.

Cursor can retain and validate this planner contract, but it has no dispatcher,
terminal receipt, or runtime tool-enforcement surface. It must fail closed if
asked to claim that a T4 consultation/diagnosis ran, used permitted tools, or
authorized a mutation.

## Scope guard

Same as every platform: this workflow router composes and routes Cursor's
own working-agent phases only. It never selects a model for a user-facing or
product surface, and it is fully independent of the separate
`liberai-*-router` skill stack (different state home, different catalog,
different install location).
