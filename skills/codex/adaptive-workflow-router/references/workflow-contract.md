# Workflow contract

Read this file when editing `assets/workflows.json`, changing planner behavior, or evaluating a workflow candidate.

Contents: authority · catalog shape · control patterns · route request · conditions · exit gates · execution record · candidate experiment · safe authoring.

## Authority

| Component | Owns | Must not own |
| --- | --- | --- |
| Workflow catalog | Application phases, dependencies, artifacts, conditions, loop bounds, exit gates, authority boundaries | Models, providers, reasoning effort, tier assignments, model fallbacks |
| Workflow planner | Catalog validation, workflow composition, route requests, hashes, active-policy binding | Tool execution, runtime identity claims, external authorization |
| Adaptive model router | T0-T4, risk floors, active profiles, escalation, model experiments, promotion, canary, rollback | Application phase order and domain deliverables |
| Workflow dispatcher | Exact App Server selection, active-profile injection, bound transcript evidence, caps, fail-closed route acceptance, private hash-chained execution/quality receipts | Tier choice, workflow order, external authority, model promotion |
| Domain skill | How to perform a bounded phase | Model selection or cross-workflow authority |
| Parent and user | Scope expansion, consequential decisions, external and irreversible actions | Scored attestation unless trusted runtime metadata exists |

The dispatcher also owns execution admission, not routing. It has no fixed global agent-count cap. A coordinator first decomposes work into an exact branch DAG. Each branch binds workflow/version/phase, model/effort, verified plan ID, phase key, dispatch-packet hash, sandbox, network/tool/mutation authority, dependencies, canonical hierarchical mutation scopes, model-specific cost/quota weights, and independent token, model-cycle, tool-cycle, and wall-time ceilings. The governance authority—not a caller field—derives capacity from the installed policy plus current host resources, requires exact active-policy model weights, and packs ready branches into conflict-free waves under that expiring weighted snapshot. The self-hashed contract determines descendant tokens. A locked ledger also reserves active resource weight across every tree, preventing concurrent plans from independently consuming the full host/provider envelope while leaving total branch count unrestricted. Every child lease consumes one unused branch, must be a direct child of that coordinator, must start in a clean task-bound worktree, and cannot change its task, packet, permissions, route, or resource envelope. The dispatcher compares both committed and uncommitted changed paths to the branch's exclusive scope before accepting it; ancestor and descendant scopes conflict, and any source-commit drift—including an empty commit—blocks acceptance. A planned branch can become `COMPLETED` only when governance locates its exact execution receipt in the validated hash-chained registry and rechecks the receipt's lease, capacity contract, packet, source commit, verification, metadata, and input manifest. Generic completion cannot complete a coordinator or planned branch. Later waves require those evidenced terminal receipts from every prior wave. Expiry and stale cleanup terminalize leases and persist immediate receipts; a completed tree requires every planned branch to have completed. A single-phase harness dispatch receives one ephemeral child slot, reserves the conservative maximum model weight, and cannot spawn. Each accepted workflow execution receipt carries the bound `required_skills`, `skill_hashes`, `source_commit`, declared `exit_gate`, observed `files_changed`, explicit `verification`, lease, capacity-contract hash, branch, and wave. Required skills must have a fresh hash-matching read receipt; an absent, stale, changed, or unread skill rejects dispatch.

Source catalogs fail validation if a router-owned key appears anywhere. A resolved plan may contain router output because that output is derived from the active policy, not authored into the workflow.

## Catalog shape

The catalog contains:

- one schema, planner-contract, and route-contract version;
- route defaults that contain no activity;
- a closed set of reusable control patterns;
- versioned application workflows;
- one unconditional composition-terminal phase per workflow;
- ordered DAG phases whose dependencies must refer to earlier phases;
- artifacts and human-readable exit gates;
- explicit input or runtime conditions;
- hard iteration limits for loops;
- parent gates paired with every parent action.

The planner validates the full catalog before listing or planning. It hashes the complete catalog and each selected workflow. The hash changes when any source field changes.

Repeated application selections compose sequentially in request order. Every root phase in a downstream workflow depends on the upstream workflow's active completion frontier: the last non-disabled phase in that run, including an enabled delivery/publication branch. This makes `research -> planning` and `research -> coding` explicit rather than relying on prose ordering.

## Control patterns

| Pattern | Use when | Avoid when |
| --- | --- | --- |
| `chain` | A downstream phase consumes a required upstream artifact | Steps are independent and latency matters |
| `route` | Input belongs to one clear application or specialist lane | Categories overlap enough that classification errors dominate |
| `fan_out` | Subtasks are independent or independent judgments add confidence | Workers need shared evolving context or would mutate the same state |
| `gather` | One owner must normalize and synthesize parallel results | Raw worker outputs are already complete and compatible |
| `orchestrator_workers` | Necessary subtasks emerge dynamically from evidence | A fixed chain is known and sufficient |
| `evaluator_optimizer` | A fixed rubric can demonstrate measurable improvement | The evaluator has no stable criteria or stopping condition |
| `human_gate` | Authority, preference, or risk ownership cannot be delegated | The next step is an exact read-only check |
| `bounded_agent_loop` | Evidence changes the next step and a hard bound exists | A deterministic loop or one-pass chain is sufficient |

## Route request

Every `execution: route` phase is expanded from defaults into a closed request:

```json
{
  "workflow_id": "coding.change",
  "workflow_version": 1,
  "phase_id": "implement",
  "activity": "implement",
  "mutation": "reversible",
  "scope": "multi_file",
  "ambiguity": "bounded",
  "risk_level": "moderate",
  "risk_scope": "mutation",
  "risk_tags": ["build"],
  "visual_required": false,
  "current_info_required": false,
  "external_action": false
}
```

The workflow planner never converts these facts into a tier. It passes them to `adaptive-model-router/scripts/router_lab.py resolve-phase`, which returns the active policy binding.

Overall task ambiguity and scope raise judgment phases but do not automatically inflate bounded evidence collection. Overall risk tags propagate into evidence, judgment, and mutation phases, while exact polling and checks remain deterministic. A prepared external packet can be assembled by a light route; authorization and execution remain separate parent phases.

## Conditions

`condition_mode: input` is enabled only through `--enable-condition`. `condition_mode: runtime` is evaluated from recorded phase evidence. A runtime-conditioned phase appears in the plan as `runtime_condition` and is not executable merely because it has a route binding. Its direct dependency must first emit a named/versioned/self-hashed `adaptive-workflow.phase-result` bound to the prior plan ID, exact source-phase contract, declared produced artifact names and canonical paths/hashes, triggered condition, and a separate retained execution-record path/hash. A named/versioned/self-hashed `adaptive-workflow.runtime-condition` contract then binds the prior plan ID and exact target phase/condition to that phase-result path/hash. `workflow_plan.py activate-runtime` verifies the full chain and emits a new self-hashed plan in which only that phase becomes `active`; it never overwrites the prior plan. Bind and dispatch from the activated plan.

An input or runtime conditional phase has explicit skip semantics. A disabled or untriggered phase satisfies its dependency without producing artifacts; an active phase must produce its declared artifacts before dependents run. A downstream phase that depends on such a branch must have an exit gate valid for both paths. Runtime recovery branches must rejoin a terminal path before delivery or composition continues.

## Exit gates and evidence

Exit gates state what must be true, not what a worker should claim. Prefer:

- command exit status and exact test results;
- artifact existence, schema, hash, and rendered inspection;
- claim-to-source entailment and resolvable citations;
- observed external receipts and terminal statuses;
- fixed rubrics with examples and independent calibration;
- named authority and exact action packets for consequential changes.

Do not infer completion from fluent prose. If an exact grader exists, run it. Subjective gates require a rubric and recorded evidence. A clean audit result means no validated findings under the stated scope; it is not proof that no defect exists.

## Execution record

A scored phase record contains:

```text
workflow id + version + workflow hash
phase id + control pattern + route facts
router policy id + requested profile hash
runtime-attested provider/model/effort when available
input manifest hash + artifact hashes
gate result + grader identity + transcript reference
quality + latency + tokens + cost + retries
safety signals + user correction or acceptance
```

`REQUESTED_PENDING_SERVER_METADATA` is valid for planning but is not execution. A planned adaptive T1-T4 phase is accepted only through `scripts/workflow_dispatch.py`, which binds the active policy and profile to explicit App Server provider/model/effort/service-tier selection and verifies the bound raw transcript. Planned execution additionally requires a self-hashed packet from `workflow_plan.py bind`; the packet binds the exact phase contract, prompt bytes, one ordered precomputed context bundle, cwd, sandbox, network, tool mode, mutation authority, requested token/model-cycle/tool-cycle limits, budget-contract hashes, and wall time, and dispatch rejects drift. Before packet validation, `workflow_plan.py verify-plan` regenerates the plan from the installed trusted catalog and current router, then replays and revalidates any phase-result-backed runtime activation chain. Dispatch rejects self-rehashed changes to patterns, dependencies, outputs, gates, route facts, or evidence flags, requires the exact policy/runtime-evidence binding, and executes only a phase whose activation is explicitly `active`; `runtime_condition` is not executable authority until `activate-runtime` emits a new evidence-bound plan. The service tier comes from active policy, never from a phase caller. Accepted first-party route identity may come from the current `thread/start` response (`modelProvider`, `model`, `reasoningEffort`, and `serviceTier`) or a legacy bound `thread/settings/updated` notification (`modelProvider`, `model`, `effort`, and `serviceTier`). Every present source must contain all four fields. The thread response must match provider, model, and service tier. Its effort is the pre-turn default; when the bound settings notification is emitted after `turn/start`, that notification is authoritative for effort and must match the requested route. If no settings notification appears, the response effort must match. JSON null means the normal default tier; an empty string, boolean, number, omitted field, or other malformed setting is rejected. Generic collaboration spawn is inherited-model execution because its public interface does not expose enforceable model and effort fields; it must never be presented as a routed phase. Ordinary App Server dispatch uses a unique ephemeral thread with exactly one bound turn and completion event. A newly bound schema-2 resumption contract may instead use a persistent thread, but every transcript segment still contains exactly one bound turn and terminal event; completion, messages, usage, reroutes, safety, and no-tools enforcement remain bound-thread/turn scoped, and foreign events are ignored.

The pinned App Server can rejoin a persistent thread but cannot continue an interrupted turn or restore its hidden inference state. Resumable authority is therefore limited to `mutation:none`, read-only, network-disabled, no-tools phases with an immutable ordered work manifest, cumulative wall/token budgets, bounded checkpoint/shutdown windows, TTL, and a policy-capped continuation count. At the soft deadline the dispatcher may accept only a canonical, nonce-bound, non-final completed assistant checkpoint that partitions all work IDs and includes compact result facts. It then requires terminal interruption, exact observed identity, no tools/reroute/safety substitution, measured thread-cumulative usage, and no later bound item before publishing a private atomic checkpoint. Recovery verifies every bound source byte and transcript, atomically claims the checkpoint once, calls `thread/resume`, requires exact interrupted-turn history, and starts a new turn for only the remaining IDs. Previous completed IDs/results must be preserved and progress must be monotonic. Hidden reasoning is never persisted; reasoning without an explicit checkpoint ends as `ABORTED_NO_RESUMABLE_CHECKPOINT`.

The final schema-2 receipt distinguishes `uninterrupted` from `explicit_checkpoint_restart`, hash-chains every transcript, and cross-links exactly one checkpoint and one claim per continuation. `genuine_resume` is reserved but cannot be emitted by the pinned protocol. Token usage is the monotonic thread-cumulative total rather than a sum of segment totals, and wall time never resets. A crash before claim can consume the committed checkpoint through `workflow_dispatch.py resume --checkpoint ABSOLUTE_PATH`; that command accepts no overrides. A crash after claim is ambiguous and fails closed. Schema-1 packets and their ordinary ephemeral behavior remain unchanged. Multi-turn resumed receipts declare `model_promotion_eligible:false` and are excluded from persistent learning until the learner has an explicit transcript-chain contract.

On a completed turn, the dispatcher crash-safely appends a private execution receipt that binds the exact metadata, raw transcript, input manifest, profile, requested/observed identity, usage, duration, thread, turn, and completed message IDs. Completion is transport success, not task acceptance. A scored workflow observation must present that receipt and a separate quality receipt registered through `workflow_dispatch.py register-quality`. The quality receipt binds the case, arm, rubric, grader identity, exact execution receipts, normalized score, objective gates, and acceptance verdict to either the bundled pinned deterministic grader or an independently routed blind grader. A usage record enters an accepted-success budget distribution only when it has at least one registered quality receipt and every registered quality receipt passes. The learner re-reads all bound bytes; a caller-created metadata or quality file alone is never evidence. These are first-party harness provenance records, not provider-signed proof.

Ordinary workflow records use source `openai-codex-app-server-workflow`, declare `model_promotion_eligible: false`, and intentionally omit the router's scored `run`/grader verdict. They must not be imported as model evidence. A scored model experiment separately uses source `openai-codex-app-server` and the router's complete run, grader, input-manifest, and safety contract.

Execution budgets are phase-derived, not globally fixed, and token limits do not stand in for cycle or time limits. Execution has four independently enforced resource limits—total tokens, harness-started turn cycles, tool-call cycles, and wall time. The dispatcher separately records strictly advancing cumulative usage updates as `model_api_call_updates`. Those updates measure actual usage-bearing responses but do not reliably identify provider-call boundaries, so ordinary tool-enabled execution reports them without treating the cold-start estimate as a fifth hard limit. For tool-enabled cold starts, immutable prompt/context and built-in context reserve are conservatively sized for `tool_cycle_cap + 4` usage-bearing responses; bounded replay growth uses the same estimate, while actual cumulative tokens remain authoritative. Tool mode `none` and resumable segments retain a strict one-response, zero-tool contract. The legacy `model_api_call_allowance` field is the compatibility name for this accounting estimate; `model_cycle_cap` and `--model-cycle-cap` remain compatibility spellings for the harness-turn cap. Streaming waits inside a model phase are forbidden: unchanged external state is polled by T0 deterministic processes, and a model is dispatched only when a terminal state needs audit or a failure needs diagnosis.

Each executable model phase declares a ceiling in `token_budget.declared_token_cap`, with `schema_version: 1` and a `declaration_source` of `plan.phase-token-cap` or `planner.route-default`. The bound packet's `runtime_contract` has non-null integer `declared_token_cap`, `derived_token_cap`, and `effective_token_cap`, integer-or-null `caller_requested_token_cap`, `token_cap_contract_sha256`, `token_cap_task_binding_sha256`, `dispatch_packet_binding_sha256`, and `budget_increase_contract_sha256`; metadata and receipts retain the cap evidence without conflation. `requested_budget_limits.token_cap` is always the non-null effective request. Natural-language text is never parsed as a limit. `effective_token_cap` is the minimum of declared, derived, and a present caller-lowered cap. `workflow_plan.py plan --phase-token-cap PHASE_KEY=CAP` and `workflow_plan.py bind --token-cap CAP --token-cap-contract-output PATH` create the self-hashed `adaptive-workflow.fixed-token-cap` v2 contract and `PATH + '.evidence.json'`; `--token-cap-contract PATH` validates/reuses one. The v2 contract carries the full `task_binding` plus its hash; declared, derived, caller-requested, and effective cap values; `token_cap` equal to the effective cap; the retained evidence path and raw-byte hash; the evidence object's structural hash; and its own contract hash. The adjacent v2 evidence object repeats the full binding and all four cap values and is independently self-hashed. Its acyclic pre-packet task binding covers plan ID, phase key/contract, full route request/resolution, prompt/context hashes and bundle, cwd, source commit, cap values, exact planner-authorized mutation scope, and a prospective packet digest. Bind and run rederive all fields and reject a missing, stale, self-rehashed-substitute, or mismatched contract before a model turn begins. A caller may lower a cap without review, and each effective cap must be no greater than any explicit requested cap. A cap breach preserves evidence and emits `budget_or_context_exhaustion`; it does not authorize a larger cold-start envelope or an implicit budget increase. The reviewed budget-increase path remains separate and is the only path above the measured envelope.

The dispatcher records prompt/context size, active tier, activity, scope, ambiguity, capability factors, cold-start envelope, accepted-history group, successful-use percentile, model-specific cost/quota weights, `declared_token_cap`, `derived_token_cap`, `effective_token_cap`, `caller_requested_token_cap`, `token_cap_contract_sha256`, `token_cap_task_binding_sha256`, `dispatch_packet_binding_sha256`, `budget_increase_contract_sha256`, and any reviewed increase in the input manifest. Every completed execution receipt records actual use keyed by workflow ID/version, phase, observed model, effort, and tool mode. Only exact-group receipts inside the policy window with registered unanimously passing quality evidence enter future envelopes. Learning re-reads the quality artifact, recomputes its verdict, and revalidates the deterministic or blind-grader producer; a contradictory later receipt excludes the execution. Failed, blocked, ungraded, or quality-rejected executions never train accepted-success envelopes. Retained failures may be used only as non-learning regression evidence when they demonstrate a deterministic invalidity in the cold-start accounting formula. Lowering the tool cap also lowers the cold-start usage-response estimate. These measured envelopes also constrain parallel planning: the coordinator may use as many independent workers as the task justifies, but each branch remains separately budgeted and the contract-derived descendant tokens bind that execution plan.

For a tool-enabled phase, the dispatcher waits for the required local `node_repl` MCP bridge to report `ready` for the bound thread before sending `turn/start`. It records that readiness in execution metadata and fails closed on a bounded readiness timeout, so a model call is never spent on a tool contract that was not actually available. No-tools phases skip this gate and retain their empty-tool contract.

Global installation takes the governance ledger lock across the final stable drain check and creation of the install journal. New agent admission checks that journal only while holding the same lock. A dispatcher that passed its outer journal check before installation but then waited for the ledger lock is rejected after the journal commits; it cannot enter the ledger in the scan-to-journal gap. The installer rejects malformed governance schemas rather than treating them as quiescent.

After the policy minimum sample count, the exact workflow-version route envelope is the configured successful-use percentile plus bounded headroom; higher cost/quota weight narrows only discretionary headroom and never cuts beneath the observed successful percentile. That evidence-based envelope may be below or above a later cold-start formula. A lower caller cap is always safe. A larger cap than the measured envelope requires the supported `adaptive-workflow.execution-budget-increase` name/version, exact task-binding and measured/requested limits, retained evidence bytes/hash, non-empty rationale, valid self-hash, and an existing trusted completed `review.audit` execution receipt. The review metadata output must be exactly `APPROVE adaptive-workflow.execution-budget-increase <proposal_sha256>`, where the proposal binds task, limits, evidence hash, and rationale; a receipt cannot approve a different proposal. A fixed token cap remains appropriate for one-turn conformance or paired evaluation, but its fixed-task contract does not authorize an increase by itself. Persistent experiments additionally bind every worker, reviewer, grader, retry, rework, and escalation invocation one-to-one to a planned call ID with exact role, phase, attempt kind, dispatcher-manifest hash, model-cycle/tool-cycle/token/wall caps. Their worst-case aggregate includes explicit reviewer and grader allowances; missing, extra, duplicated, misclassified, or over-cap invocations are rejected. A no-tools phase is forced into a read-only, offline sandbox before its turn begins and accepts only passive user-message, reasoning, and agent-message item types. Any other or unknown item type is interrupted and rejected. Parser, setup, and runtime failures each retain an `ABORTED` artifact; if a requested output path is unavailable, retention falls back to the router run directory and then a unique temporary directory.

Every terminal routed dispatcher invocation atomically persists a mode-`0600`, self-hashed `adaptive-workflow.control-return` v2 sidecar at `<run>/control-return/adaptive-control-return.json` and emits exactly one final `ADAPTIVE_CONTROL_RETURN <canonical-json>` stdout marker. Version-1 receipts remain readable and validate as legacy evidence. V2 is a typed decision with `terminal_status`, `terminal_category`, `failure_class`, `recommended_action`, `failed_phase`, `failed_tier`, `retry_escalation_count`, `failed_exit_gate`, `evidence_references`, `worktree_evidence`, `completed_checks`, `checkpoint_evidence`, `execution_receipt_evidence`, `failure_evidence`, `next_route_request`, `parent_user_question`, `workflow_state`, and `directive_sha256`. `evidence_references` contains immutable `{path,sha256}` records from one retained run root. `worktree_evidence` contains admission and final snapshots, each binding source commit, absolute worktree, clean/dirty state, tracked-diff hash, and `tool_visible_snapshot_sha256`, plus the non-authorizing coarse mutation mode. Its normalized class is exactly one of `completed`, `deterministic_setup_failure`, `transient_failure`, `grounded_cognitive_failure`, `exit_gate_failure`, `authority_required`, `budget_or_context_exhaustion`, `model_enforcement_failure`, `harness_unavailable`, or `non_resumable_failure`; its recommended action is exactly one of `t0_repair`, `retry_same_route`, `escalate`, `ask_user`, `partition`, `resume`, or `abort`.

`workflow_dispatch.py pivot-from-receipt --receipt ABSOLUTE_RECEIPT --output ABSOLUTE_PIVOT` validates a terminal control-return and produces a self-hashed, tamper-evident `adaptive-workflow.pivot-from-receipt` v2 next-action record with `pivot_sha256`. It is idempotent only by reusing an exact validated prior pivot for the same receipt, and it does not execute a model, grant authority, grant mutation permission, or raise a cap. Its deterministic state machine is: `USAGE_ERROR` and deterministic `SETUP_FAILURE` produce `t0_repair` with one corrected retry; a repeated identical repair failure produces an `abort` harness incident. A transient tool/network failure with a validated resumable checkpoint produces `resume`; one without that checkpoint produces one `retry_same_route`, then `abort` as a blocker if repeated. Every `grounded_cognitive_failure` produces `t4_consult` when immutable evidence is complete and non-contradictory, otherwise `t4_diagnose`; an `exit_gate_failure` returns to the model router for reassessment. Authority, user, or external-action requirements produce `ask_user` with the exact question packet and no further worker dispatch; they never request T4. Budget/context exhaustion produces `partition`, never an automatic budget increase. Harness unavailability produces `abort` with a copyable read-only break-glass consultation packet. A non-resumable interruption produces `abort`.

For a blocked tool-enabled mutation, the pivot binds source commit, worktree identity, clean/dirty status, tracked-diff hash, the exact tool-visible snapshot hash, completed checks, terminal evidence, and resume checkpoint when one exists. Tool-visible scope is limited to tracked and nonignored-untracked paths; `.git`, ignored paths, symlinks, and anything outside the repository are unavailable. Each descriptor-opened file is hashed against the sealed snapshot before bytes are returned, so a change-and-restore race fails. T4 receives only this compact evidence and can return direction for an exact bounded T3 mutation contract; it cannot return a repository patch or authorize external action. T4 never mutates. Every `grounded_cognitive_failure` with complete, immutable, non-contradictory evidence selects `t4_consult`: offline, no tools, read-only, exactly one model cycle/API response, and the packet's strict cap. Incomplete or contradictory evidence selects `t4_diagnose`, which permits only bounded repository list/read/search tools on the packet's exact snapshot-bound scope, with network disabled and strict token/tool/model-cycle/API-call/wall caps. Before T4 starts, the runner's feature enumeration must exactly match the reviewed set and every feature is disabled; an unknown feature fails closed. Neither mode may expose mutation tools. A retained `adaptive-workflow.t4-result` binds the strict parsed output to the execution receipt, source pivot/control-return, evidence, repository snapshots, lineage, and caps. T4 direction is not mutation authority: the retained result must first be a hash-matched context file in the fresh governed T3 packet, and that packet must consume and revalidate the bounded `adaptive-workflow.t4-direction-to-t3-mutation` v2 contract against those retained anchors, the fresh prompt/phase, and `planner_authorized_mutation_scope`; the model-selected paths must be descendants of that planner scope. No direction text is appended after packet or cap binding, and a separate parent `--mutation-authorized` grant remains mandatory. This operational handoff is outside adaptive model execution and is excluded from execution, quality, governance, and learning receipts; inherited-model cognitive continuation is prohibited.

## Candidate workflow experiment

1. Copy the catalog to an isolated candidate path and validate it with `--catalog`.
2. State one hypothesis and change one causal variable: phase order, decomposition, prompt contract, gate, loop bound, or parallelization choice.
3. Freeze the router policy, task inputs, source bundle, tools, budgets, and graders.
4. Run incumbent and candidate across the same development cases with multiple trials where variance matters.
5. Use deterministic outcome graders first, blind calibrated rubrics for subjective outcomes, and safety vetoes.
6. Inspect transcripts and failures. Reject gains caused by grader exploitation, missing work, scope reduction, correlated reviewers, or more authority.
7. Require non-regression on held-out cases and workflow-family slices. Keep an inconclusive result on the incumbent.
8. Activate by replacing the catalog only after a fresh evaluation still returns exact qualification under current live authority, an exact snapshot, and a rollback path exist. A prior `QUALIFIED` label alone is not activation authority, and no new prepromotion evidence may be appended after qualification.
9. Record post-promotion control/active canary arms as trusted observations, then run the payload-free monitor. It derives identity, quality, safety, authority, and configured complete windows from those retained pairs; incomplete evidence remains `PROMOTED`, and a qualified regression restores the exact snapshot.

For a model experiment, invert the isolation: freeze the workflow hash and let the adaptive model router vary one tier assignment.

Persistent experiments use `scripts/learning_loop.py` and the detailed [learning-loop contract](learning-loop-contract.md). The learning ledger may qualify and journal-activate an exact workflow snapshot, but it may only mirror model states already committed by `router_lab.py`. At cycle creation it derives the current family from the canonical live global-router catalog and active no-downgrade window; a caller cannot choose a different catalog or family. It binds one candidate per scheduled cycle, trusted dispatcher and quality receipts, exact cross-cycle evidence and TTL, complete planned worker/reviewer/grader/retry/rework resources, hard quality and safety gates, Pareto token and wall-time dimensions, holdout isolation, and byte-exact workflow rollback. Every public ledger read or mutation recovers interrupted ledger and workflow-authority journals before proceeding. Dispatcher or experiment failure remains separate from healthy active routing.

The bounded SERENDIPITY lane reserves exactly the policy-configured share of cycle capacity for safe single-variable novelty search; callers cannot enlarge it. Descriptor-indexed append-only archive entries retain separate minimum-quality, novelty, learning-progress, and unrelated-task transfer scores. Safe informative failures receive no promotion credit. Replays use only precommitted unrelated held-out families and never update production action statistics. A later ordinary cycle can claim an archive only through lineage frozen at begin, with archive-before-begin-before-validation ordering and an exact causal manifest before retrospective stepping-stone credit is recorded. That credit is diagnostic only. Novelty, surprise, transfer, or credit never overrides current-family/T4 invariants, execution identity, authority, safety, correctness, quality, sample, holdout, promotion, canary, or rollback gates.

## Safe authoring rules

- Prefer deterministic nodes for known routing, polling, retries, and exact checks.
- Prefer one agent with tools until separation or independent context measurably helps.
- Keep parallel writes out of a workflow. Use one mutation owner and many read-only evidence workers.
- Treat an operations release dry-run as read-only evidence collection; only the later authorized parent action owns the release mutation.
- Bound turns, retries, stalls, and loops.
- Keep current-information retrieval source-backed and date-aware.
- Keep private retrieval separate from open-web tools when exfiltration is plausible.
- Keep prepared actions separate from authority and execution.
- Do not encode provider or model names in workflow content, including examples.
