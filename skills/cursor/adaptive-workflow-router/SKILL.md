---
name: adaptive-workflow-router
description: Compose deterministic application workflow graphs and resolve every cognitive phase through the Cursor adaptive model router, with evidence-bound dispatch packets. Use when planning multi-phase application work (coding, research, planning, debugging, review, data, writing, operations), binding a routed phase to exact dispatch inputs, or verifying a dispatch plan against the trusted workflow catalog.
---

# Adaptive Workflow Router (Cursor)

Own phase order, dependencies, artifacts, and completion gates for
multi-phase application work. `adaptive-model-router` remains the sole
authority for tiers, models, effort, and evidence grade — this skill never
declares a model, provider, tier, agent, or effort inside a workflow
definition (enforced by `workflow_plan.py`'s catalog validator).

`scripts/workflow_plan.py` is byte-identical to the Codex reference: same
route-request schema, same contract names (`adaptive-workflow.runtime-condition`,
`adaptive-workflow.phase-result`), same dispatch-packet and plan-identity
hashing. `assets/workflows.json` and `assets/resumption-policy.json` are
vendored byte-identical from `core/workflow/`. See
[references/workflow-contract.md](references/workflow-contract.md) for
exactly what is and is not Cursor-specific, most importantly: **no scripted
dispatcher in pass 1** — dispatch is a documented contract the parent agent
follows directly, and every model-bearing route is `REQUESTED_NOT_ATTESTED`.

## Compose and validate

```text
workflow_plan.py validate                                   # validate the installed catalog
workflow_plan.py list                                        # list installed application workflows
```

## Plan

```text
workflow_plan.py plan \
  --application COMMA_OR_REPEATED_APPLICATION_NAMES \
  --objective "what this plan accomplishes" \
  [--enable-condition NAME]... \
  [--scope|--ambiguity|--mutation|--risk-level|--risk-scope overrides] \
  [--risk-tag TAG]... [--visual-required] [--current-info-required] [--external-action] \
  [--output PLAN.json]
```

Every phase with `execution: route` resolves against the live Cursor router
(`../adaptive-model-router/scripts/router_lab.py resolve-phase`); the plan
fails closed if routing health is not `HEALTHY`. A phase whose resolved route
requires consequential mutation and lacks an ancestor `parent_gate` gets one
inserted automatically — the parent agent must explicitly authorize it.

## Bind one phase to exact dispatch inputs

```text
workflow_plan.py bind --plan PLAN.json --phase-key KEY \
  --prompt-file PROMPT.txt [--context-file FILE]... --cwd DIR \
  --sandbox read-only|workspace-write --tool-mode default|none \
  [--mutation-authorized] --wall-time-seconds N \
  [--output PACKET.json]
```

The resulting packet hashes the prompt, context files, and runtime contract,
and requires a Git-backed source commit (`require_authoritative_source_commit`)
— an unversioned working tree fails closed. Dispatch the bound packet through
one of the two documented surfaces in
`../adaptive-model-router/references/dispatch-contract.md`; there is no
`workflow_dispatch.py` in this tree to run it for you.

## Runtime-conditioned phases and plan authority

```text
workflow_plan.py activate-runtime --plan PLAN.json --phase-key KEY \
  --evidence-contract CONTRACT.json --output ACTIVATED.json
workflow_plan.py verify-plan --plan PLAN.json
```

These work exactly as documented for Codex/Claude: pure verification over
retained JSON evidence, no dispatch surface involved.

## Scope guard

Routes and composes Cursor's own working-agent phases only — never
user-facing or product model selection — and is fully independent of the
`liberai-*-router` skill stack.
