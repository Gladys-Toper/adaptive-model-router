---
name: adaptive-workflow-router
description: Select, compose, and run evidence-gated application workflows for coding, research, planning, debugging, review, data analysis, writing, and operations while using adaptive-model-router as the exclusive authority for model and reasoning-tier selection. Use for multi-phase work, mixed application tasks, parallel research or review, PR preparation, long-running loops, consequential actions, or workflow and routing optimization. Do not use for a single direct answer or one exact command.
---

# Adaptive Workflow Router

Use a deterministic application graph for known work and spend model reasoning only inside cognitive phases. Resolve every active cognitive phase through `adaptive-model-router`; never put a model, provider, tier, effort, or fallback in a workflow source.

## Start here

1. Read `../adaptive-model-router/SKILL.md` completely. It owns T0-T4, active assignments, risk floors, escalation, model discovery, experiments, promotion, and rollback.
2. Validate and inspect the installed workflows. Resolve `scripts/` relative to this skill directory; the examples assume that directory is the current working directory:

```text
python3 scripts/workflow_plan.py validate
python3 scripts/workflow_plan.py list
python3 scripts/test_workflow_plan.py
python3 scripts/test_workflow_dispatch.py
python3 scripts/resume_canary.py --wall-time-seconds 5
python3 scripts/workflow_dispatch.py check
python3 scripts/test_learning_loop.py
python3 scripts/learning_loop.py doctor
```

3. Select one root application. Compose additional workflows only when the request genuinely crosses applications.
4. Generate the route-bound plan before dispatching substantial work:

```text
python3 scripts/workflow_plan.py plan \
  --application coding \
  --objective "Implement the scoped change and prepare a PR" \
  --enable-condition delivery_requested
```

Use repeated `--application` values for mixed work; they compose sequentially in the requested order and wait for each run's active completion frontier, including an enabled delivery/publication branch. Useful floors include `--ambiguity novel`, `--risk-level high`, `--risk-tag security`, `--visual-required`, and `--current-info-required`. Enable only input conditions listed by the selected workflow; runtime conditions activate from evidence during execution.

## Choose the application

| Application | Workflow | Default shape |
| --- | --- | --- |
| Coding | `coding.change` | frame -> inspect -> design -> implement -> exact verification -> review -> optional delivery |
| Research | `research.synthesis` | frame -> decompose -> parallel retrieval -> synthesis -> conflict and citation gates |
| Planning | `planning.decision` | objective -> constraints -> alternatives -> tradeoffs -> selection -> stress test -> sequence |
| Debugging | `debugging.incident` | observe -> reproduce -> hypotheses -> discriminating tests -> diagnosis -> optional fix -> verify |
| Review | `review.audit` | freeze rubric -> mechanical checks and inspection -> adversarial probes -> validate -> adjudicate |
| Data | `data.analysis` | define metric -> inventory -> validate -> transform -> analyze -> robustness -> visualize -> report |
| Writing | `writing.document` | brief -> sources -> outline -> draft -> critique/revise -> fact check -> optional publication |
| Operations | `operations.release` | preflight -> plan -> read-only dry-run -> authorize -> single mutation lane -> poll -> verify -> close |

Use `research` before `planning` when a decision depends on current external evidence. Use `research` before `coding` when implementation depends on current APIs or standards. Use `review` after `coding` only when its independent rubric adds value beyond the coding workflow's built-in review.

## Execute the plan

The planner emits phase dependencies, activation mode, artifacts, exit gates, route requests, active-policy bindings, and requested route identities. It does not execute tools or confer authority.

Every T1-T4 phase must run through the pinned App Server dispatcher when this local harness is available:

```text
python3 scripts/workflow_plan.py bind \
  --plan PLAN.json \
  --phase-key WORKFLOW:PHASE \
  --prompt-file PHASE.md \
  --cwd /absolute/workspace/path \
  --sandbox read-only \
  --tool-mode default \
  --wall-time-seconds 900 \
  --output PHASE.dispatch.json

python3 scripts/workflow_dispatch.py run \
  --plan PLAN.json \
  --phase-key WORKFLOW:PHASE \
  --dispatch-packet PHASE.dispatch.json \
  --prompt-file PHASE.md \
  --cwd /absolute/workspace/path \
  --sandbox read-only
```

Before dispatch, record a fresh `skill-read` receipt for every applicable skill and bind the resulting self-hashed required-skills contract with `workflow_plan.py bind --required-skills-file`. Dispatch re-reads every listed skill, checks its hash and bounded read age, and fails closed when a required skill is absent, changed, stale, or never read. Completed workflow receipts retain `required_skills`, `skill_hashes`, `source_commit`, `exit_gate`, `files_changed`, and `verification`.

All execution belongs to the bounded governance ledger. There is no arbitrary global agent-count ceiling. The coordinator decomposes the task first into an exact branch DAG with route identity, dependencies, mutation scopes, model-specific cost/quota weights, and separate token, model-cycle, tool-cycle, and wall-time ceilings. `workflow_dispatch.py capacity-plan` ignores caller-authored capacity and the governance authority deterministically derives a time-bounded weighted snapshot from the installed execution policy and current host resources, then packs the branch DAG into conflict-free waves and self-hashes the result. Every branch's weights must exactly match the installed model policy. `open-tree` derives descendant tokens from that contract; callers cannot type an unbound fan-out count. At child admission, the locked ledger conserves active resource weight across all trees, so multiple valid plans cannot each consume the full envelope. Every child must use a verified workflow plan and exact planner packet, name one unused branch and coordinator lease, start in a clean task-bound worktree, and match the branch's route, packet hash, permissions, resource envelope, and exclusive hierarchical mutation scopes. Both committed and uncommitted writes outside those scopes, and any source-commit drift including an empty commit, block acceptance. A planned branch can reach `COMPLETED` only with its matching execution receipt in the harness's validated hash-chained registry; generic terminal calls cannot complete planned branches or coordinators. A later wave cannot start until every prior-wave branch completed with that evidence. `close-tree` cannot claim success with active, failed, or unfinished branches. Every terminal, expired, or stale lease emits an immediate receipt. Direct one-off dispatches get a one-child ephemeral tree, consume the conservative maximum model weight, and cannot spawn descendants.

The operational sequence is `capacity-plan -> open-tree -> run each ready wave -> close-tree`. Run independent branches concurrently with ordinary process orchestration; do not hold a model turn open while waiting. Poll branch processes with T0, then dispatch a model only for terminal audit or failure diagnosis. A capacity contract may describe more than nine branches. Its finite count is derived from the actual task DAG, not a product-wide cap.

Retain the `bind` command's JSON output as `PHASE.dispatch.json`; it cryptographically binds the selected planned phase to the exact prompt, one ordered precomputed context bundle, cwd, sandbox, network, tool mode, mutation authority, requested token/model-cycle/tool-cycle limits, budget-contract hashes, and wall-time contract. Dispatch rejects any changed input or permission. Prefer this planner-bound fixed bundle over model-led file discovery whenever the required evidence can be assembled deterministically.

Resumption is explicit and limited to read-only, offline, no-tools phases with at least two immutable work IDs. Generate a self-hashed `adaptive-workflow.resume-work-manifest`, then supply the same bounded options to `bind` and `run`:

```text
--sandbox read-only --tool-mode none --resumable \
--work-manifest WORK.json --max-continuations 1 \
--cumulative-wall-time-seconds 1800 \
--checkpoint-window-seconds 60 --shutdown-window-seconds 30 \
--checkpoint-ttl-seconds 3600
```

The pinned protocol cannot resume an interrupted turn. The dispatcher therefore steers for one explicit completed assistant checkpoint, terminally interrupts that turn, rejoins the same persistent thread, and starts a distinct continuation turn restricted to unfinished work IDs. This is `explicit_checkpoint_restart`, never genuine turn resumption. A reasoning-only interruption is `ABORTED_NO_RESUMABLE_CHECKPOINT`. Checkpoints and single-use claims are private, atomic, fsynced, self-hashed, transcript-bound, policy-bound, cumulative-budget-bound, and replay-rejected. After a process crash that occurred after checkpoint commit but before claim, recover with only:

```text
python3 scripts/workflow_dispatch.py resume \
  --checkpoint /absolute/run/resume-checkpoint.0001.json
```

The resume command accepts no route, permission, prompt, context, cwd, or budget overrides. A crash after claim but before a newer durable checkpoint is ambiguous and fails closed. Resumed multi-turn receipts are ordinary workflow provenance only and are not model-promotion or learning-loop evidence until those consumers support transcript chains.

A `runtime_condition` is intentionally non-executable until its declared upstream evidence exists. First retain a self-hashed `adaptive-workflow.phase-result` record bound to the prior plan ID, exact dependency phase contract, declared produced artifacts, triggered condition, separate execution record, and canonical paths/hashes. Then create a self-hashed `adaptive-workflow.runtime-condition` contract that references that phase-result record and is bound to the prior plan ID plus exact target phase/condition. Run:

```text
python3 scripts/workflow_plan.py activate-runtime \
  --plan PLAN.json \
  --phase-key WORKFLOW:CONDITIONAL_PHASE \
  --evidence-contract CONDITION.json \
  --output PLAN.activated.json
```

Bind and dispatch only from the new activated plan. The prior plan remains immutable evidence. An arbitrary condition flag, local file, or detached hash is not activation authority.

Use `--sandbox workspace-write --mutation-authorized` only after the parent has authorized the exact reversible mutation. Worker dispatch never permits unrestricted host access; externally visible or irreversible actions remain parent-only. Ordinary dispatch starts a unique single-turn ephemeral thread; only an exact schema-2 resumption contract permits the persistent checkpoint/restart mode above. Both modes disable provider fallback, inject the active profile, explicitly select the policy-bound provider/model/effort/service tier, continuously drain JSONL, retain the raw transcript, and accept the phase only when complete first-party route identity from the current `thread/start` response or legacy bound `thread/settings/updated` notification, completion, message IDs, usage, and transcript evidence all agree. The thread response must always match provider, model, and service tier. Its effort is provisional until `turn/start`; when a bound settings notification is emitted, that notification is authoritative for turn effort and must match exactly. Without one, the thread response effort must match. Every completed accepted turn receives a crash-safe private hash-chained execution receipt binding its metadata, transcript, input manifest, identity, profile, workflow/phase, actual tokens, harness-started turn cycles, tool-call cycles, observed model-API-call usage updates, and wall time.

Execution has four independently enforced resource limits—total tokens, harness-started turn cycles, tool-call cycles, and wall time. The dispatcher also records every strictly advancing cumulative usage update as `model_api_call_updates`; this is observed accounting telemetry, not a reliable provider-call boundary and not an additional ordinary-runtime stop. One App Server turn can emit several usage-bearing responses before or after tools, including reasoning continuations. For tool-enabled cold starts, immutable prompt/context and built-in context reserve are conservatively sized for `tool_cycle_cap + 4` usage-bearing responses, and bounded replay growth uses the same estimate. Actual cumulative tokens remain the hard spend limit. Tool mode `none` remains a strict one-response, zero-tool phase, including resumable segments. The legacy `model_api_call_allowance` field is retained as the cold-start accounting estimate, and `--model-cycle-cap` remains a compatibility spelling for the harness-turn cap. Never make a model hold a streaming wait command open: unchanged CI, scheduler, and deployment waits are T0 deterministic polling, and a routed model wakes only for a terminal audit or failure diagnosis.

Cold-start envelopes are derived from the fixed phase contract. Once the exact workflow/version/phase/model/effort/tool-mode group has enough accepted executions, future limits use the configured successful-use percentile plus bounded headroom from `assets/execution-budget-policy.json`. An execution enters that distribution only when it has at least one registered quality receipt and every registered quality receipt accepts it; `COMPLETED` transport status alone is not task acceptance. Before learning, the harness re-reads the retained quality artifact, recomputes its verdict, re-runs a pinned deterministic grader or revalidates the registered blind-grader execution, and rejects any contradictory, missing, stale, or malformed provenance. Model-specific cost/quota weights reduce discretionary headroom for expensive or scarce routes; they never lower a cap beneath the measured successful percentile, even if a later cold-start formula is smaller. Failed, blocked, foreign-version, foreign-route, stale, ungraded, or quality-rejected records do not train the envelope. Retained failures may be used only as non-learning regression evidence when they demonstrate a deterministic invalidity in cold-start accounting. A caller may always request lower token, turn, tool, or wall limits; lowering the tool cap also lowers the cold-start usage-response estimate. A larger legitimate hard envelope requires a task-bound `adaptive-workflow.execution-budget-increase` contract, retained measured evidence, and a trusted completed `review.audit` execution receipt whose exact output approves the canonical proposal hash. The contract and requested limits are bound into the dispatch packet; an unrelated review or arbitrary rationale is not approval. Parallelism remains a task-plan decision: add a branch only when its measured benefit exceeds its weighted resource envelope and it does not overlap another mutation owner.

Planned execution additionally uses `verify-plan` to regenerate the full plan from the installed trusted catalog/current router, replay any evidence-bound runtime activations, and reject a self-rehashed forged phase before checking its dispatch packet. The selected phase must be explicitly `active`; a merely runtime-conditional phase cannot execute until evidence-gated activation produces a new plan. Tool-enabled cold-start derivation uses the bounded-tool-loop profile and assumes a short evidence-first loop. Before dispatch, inspect all four limits: ordinary T1/T2 work above 110,000 cold-start tokens or an unusually large cycle envelope is a partitioning failure unless measured evidence and independent review justify it. Split the work, switch immutable analysis to no-tools, or move deterministic edits/checks to T0. Use `--token-cap-contract` for a fixed, task-bound evaluation cap; use `--budget-increase-contract` for any increase above the measured envelope. A no-tools route permits only bound-thread passive user, reasoning, and agent-message items; every other or unknown bound item type is interrupted and rejected, while foreign thread/turn events are ignored. A provider/model/effort/service-tier mismatch, reroute, safety buffering, missing evidence, cap breach, malformed invocation/event, or setup failure fails closed and remains an explicit artifact. Never retry such a failure through generic spawn or an inherited parent model.

Dispatcher health admission uses the router's explicit active-routing fields (`routing_status=HEALTHY` and `workflow_routing_ready=true`). A visible `CATALOG_STALE` maintenance state, evaluation-only degradation, or prepromotion candidate problem must not disable healthy active routes; active policy/profile faults still fail closed.

For read-only synthesis or review, prefer repeated `--context-file` inputs at bind time with `--tool-mode none`. The planner turns the ordered inputs into one immutable, self-hashed context-bundle record; the dispatcher verifies the bundle and embeds it once. This mode is valid only with a read-only sandbox and network disabled, derives a one-model-cycle budget from actual bytes, and rejects any attempted tool use. Direct hashed context remains available for bounded smoke/diagnostic work, but planned cognitive work should use the precomputed bundle.

For each ready phase:

1. Invoke any applicable domain skill before work begins.
2. Dispatch the route with `workflow_dispatch.py`; generic collaboration spawn does not expose enforceable model/effort selection and is not a route implementation.
3. Give a worker only the declared objective, inputs, outputs, evidence standard, and exit gate.
4. Fan out only independent work. Gather results through one synthesizing phase; do not let parallel workers mutate the same state.
5. Record exact commands, source links, artifact paths, checks, uncertainty, and failures before advancing. A tool-enabled phase must observe the local `node_repl` bridge reach `ready` before its model turn starts; missing readiness fails closed without spending a model call.
6. Treat `REQUESTED_PENDING_SERVER_METADATA` as a requested route, not proof of the model that ran. Only a completed dispatcher record and its registry receipt supply observed runtime identity. Ordinary dispatcher records are explicitly ineligible for model promotion; scored experiments must separately satisfy the router's grader/run/import contract. A scored workflow result additionally needs a registered quality receipt from the pinned deterministic grader or a receipt-bound independent blind grader; a caller-authored quality file is not evidence. These receipts are first-party observed harness provenance, not provider-signed proof. The legacy `REQUESTED_NOT_ATTESTED` identity remains valid only with its external-verifier contract.
7. Stop when the exit gate passes. A transient tool or network error gets one same-route retry; a grounded cognitive failure returns to the model router for reassessment.

T0 is reserved for exact checks, bounded polling, and predefined deterministic execution. Source selection, interpretation, diagnosis, synthesis, approval, and risk decisions are cognitive phases even when their output is short.

## Preserve authority boundaries

- `parent_gate` pauses for authority already present in the user's request or for an explicit parent/user decision.
- `parent_action` is executed only by the parent through the exact prepared action packet. A worker's route never grants permission.
- An operations release dry-run is read-only evidence collection. It validates the exact action packet without mutating the workspace or production; the authorized `parent_action` remains the single release mutation lane.
- Keep merge, deploy, rollback, publication, payment, destructive data operations, and other externally visible or irreversible actions outside worker autonomy.
- During incidents, use one mutation owner. Parallel agents may collect or analyze evidence but must not concurrently change production.
- Polling success is evidence, not merge or release authority.

## Handle conditions and loops

- Input conditions such as `delivery_requested`, `fix_requested`, `consequential_plan`, and `publication_requested` are enabled explicitly with `--enable-condition`.
- Runtime conditions such as a failed verification or material source conflict activate only when recorded evidence satisfies them.
- Evaluator-optimizer and agent loops have hard iteration bounds in the catalog. When the bound is reached, report the unresolved state; do not silently continue.
- Use a fixed rubric for evaluator loops. Self-critique alone is not independent proof.

## Improve without confounding

Use the Karpathy-style loop as controlled hill climbing: freeze the harness, make one change, measure, keep only evidence-backed improvements, and revert the rest.

- To test a model route, freeze `workflow_hashes`, inputs, tools, budgets, graders, and source corpus. Let `adaptive-model-router` run paired incumbent/challenger trials and own promotion or rollback.
- To test a workflow, freeze `router_policy_id` and change one workflow graph, prompt contract, gate, or parallelization choice. Compare identical task sets across multiple trials.
- Grade outcomes with deterministic checks first, blind rubrics or calibrated humans for subjective quality, and safety vetoes. Read failed transcripts; do not optimize only an aggregate score.
- Use held-out tasks and post-promotion canaries. Keep ties and inconclusive results on the incumbent.
- Run mutating workflow trials in a sandbox or dry-run environment. Never compare variants by duplicating real PRs, emails, deployments, payments, or production writes.
- For research comparisons, capture one source bundle and give both arms the same evidence; separate live searches confound the comparison.
- Record workflow version/hash, phase, active policy, runtime attestation, input and artifact hashes, gate results, quality, latency, tokens, cost, retries, and user corrections.

New coding models require no workflow rewrite: every plan resolves phases against the model router's current active policy. Its catalog monitor stages new candidates and promotes only through paired evals.

For persistent cross-cycle policy learning, use `scripts/learning_loop.py`. At cycle creation it derives the current family from the canonical live global-router catalog and active no-downgrade window; the caller cannot nominate a catalog or family. It accepts only receipt-bound App Server execution and trusted quality evidence, maps every worker/reviewer/grader/retry/rework invocation one-to-one to a planned budget call, aggregates measured total-system resources, applies quality-first Pareto gates, preserves a hash-chained ledger, and journal-activates only an ordinary workflow candidate that still returns exact `QUALIFY` under a fresh evaluation. Once qualified, prepromotion evidence is frozen. Every public read or mutation first recovers interrupted ledger and workflow-authority journals. It never originates or recommends a model promotion: `router_lab.py` remains the exclusive model candidate, promotion, canary, and rollback authority. Scheduled runs begin at most one candidate and use `no-call` when none is eligible.

SERENDIPITY is a bounded novelty-search capacity lane, not a production bypass. Its allocation is ledger-head-bound and fixed to the policy-configured share; callers cannot enlarge it. Each mutation changes one safe allowlisted model or workflow variable without predicting immediate improvement. The append-only descriptor archive scores minimum quality, novelty, learning progress, and unrelated-task transfer separately. Safe informative failures may remain archived with zero promotion credit; unsafe or incorrect failures cannot enter the archive. Periodic replays use only precommitted unrelated held-out families and never update production action statistics. A later ordinary validated candidate may give an archive retrospective stepping-stone credit only when its begin-time lineage and retained causal manifest prove archive-before-begin-before-validation order. Novelty, surprise, transfer, or credit never overrides current-family/T4 invariants, execution identity, safety, authority, correctness, quality, sample, holdout, promotion, canary, or rollback gates.

For workflow canaries, record trusted post-promotion control/active artifacts with `learning_loop.py record-canary`, then run `learning_loop.py monitor-workflow CYCLE_ID` without a caller verdict payload. The monitor derives regressions and complete configured windows from retained evidence, keeps incomplete canaries `PROMOTED`, and restores the exact candidate-bound snapshot on a qualified regression.

Read [references/workflow-contract.md](references/workflow-contract.md) before changing workflow definitions or evaluation rules. Read [references/learning-loop-contract.md](references/learning-loop-contract.md) before changing persistent learning, evidence reuse, SERENDIPITY, activation, or recovery. Read [references/research-basis.md](references/research-basis.md) before changing the pattern taxonomy or application graphs.
