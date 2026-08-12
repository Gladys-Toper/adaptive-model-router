# Self-Improvement Protocol

Use a bounded Karpathy-style autoresearch loop: freeze one routing change, run paired experiments, keep a measured win, discard a loss, and retain an exact rollback.

## Sources of truth

Use these authorities in order:

1. `active-policy.json` for the live tier-to-model mapping.
2. The canonical normalized live `catalog.json` for semantic model availability, capabilities, and current-family derivation. It is refreshed from the trusted local `models_cache.json`; callers do not supply a replacement catalog.
3. Runtime execution metadata for the model and effort actually used.
4. Objective evaluation evidence for suitability.
5. Current official OpenAI model and Codex documentation for candidate discovery and capability interpretation.

Official announcements may nominate a candidate but cannot prove local availability. A profile file may request a model but cannot prove execution. Runtime self-report is not execution evidence.

## Catalog discovery

Hash behaviorally relevant fields: provider, slug, model revision or `comp_hash`, visibility, supported reasoning efforts, input modalities, raw and effective context limits, truncation policy, shell and patch modes, parallel/search and experimental-tool support, service/speed tiers, multi-agent version, verbosity and reasoning-summary support, and instruction hash. Derive the newest visible OpenAI text family from the canonical normalized live catalog, then combine it with the active policy's exact no-downgrade window. Reject a caller-declared family, caller-supplied catalog, or family-window hash that differs from that authority.

Exclude refetch timestamps, descriptions, display names, list priority, and raw file mtime from change detection. Instruction, capability, and model-revision changes remain behaviorally relevant and invalidate dependent candidate bindings. An exact presence-aware `instruction_hash`-only change on a still-eligible active route is a client instruction-contract rebaseline rather than provider/model evidence: retain the active route byte-for-byte, record old/new instruction and revision hashes plus the exact changed-field set, stale dependent prepromotion or canary evidence, and make no quality, validation, or promotion claim. The only multi-field continuity class is a uniform active-family transition whose exact field set is `{instruction_hash, supports_reasoning_summaries}`: every active slug must have one identical old-to-new pair, every provider/model/effort/service-tier assignment must remain eligible and unchanged, and the current-family plus T4 Sol/ultra invariants must pass from one catalog snapshot. Record the old/new summary capability, instruction and revision hashes, stale all dependent evidence, and make no model-quality claim. Any partial, nonuniform, additional, missing, or unclassified field follows fallback/fail-closed handling. A non-active challenger may be restaged only when its normalized execution route differs from the incumbent. Treat the first observation as a baseline, not a release event.

Stage only models that:

- originate in the trusted local OpenAI Codex catalog;
- are selectable and accept text;
- support the tier's exact effort and required tools;
- are not hidden service models;
- contain no `anthropic` or `claude` identifier.
- belong to the newest observed version family. Variants such as `luna`, `sol`, and `terra` inherit the numeric family in their slug and may be compared at different supported reasoning efforts. Never use an older family as an active route, fallback, incumbent, challenger, control, or canary arm. Never lower the family because newer models disappear from a later catalog snapshot; fail visibly instead of downgrading.

Daybreak is an exception only for bounded security evaluation: it is never auto-staged by catalog refresh and cannot become an active assignment, fallback, incumbent, control, or canary. An operator may explicitly stage it only as a T3 security candidate; promotion fails closed so that experiment cannot become a general route.

Require `low` for T1, `medium` for T2, `high` for T3, and exact `ultra` for a new T4 challenger. Permit a text-only coding model only for T1 phases whose bound input manifest proves that no image inspection is required.

Current active-variant policy is Luna/low for T1, Terra/medium for T2, Terra/high for T3, and Sol/ultra for T4. T4 is read-only strategic work; any directly mutating execution route is capped at T3 after a separately routed T4 plan or adjudication when needed. T4 may never activate or fall back to a non-Sol variant; an unavailable Sol/ultra route is a visible routing failure. Experiments may measure other current-family variants, but promotion must recheck active-variant requirements.

When an active model's provider/model revision, capability, availability, or any semantic field outside the two explicit rebaseline classes changes, move that tier to its first eligible policy-legal fallback before scoring the new revision. Stage the changed revision against the fallback; never silently treat an untested revision as the incumbent. If no legal fallback exists, fail closed without partial policy or candidate mutation. Both narrow rebaseline classes above are continuity handling only and are never model evidence.

## Fixed experiment

Change exactly one tier mapping in a candidate. Bind the candidate to:

- incumbent policy hash;
- candidate catalog revision hash;
- exact model and effort;
- exact role-profile hash;
- evaluation-suite hash;
- promotion-rule hash.

Invalidate the experiment if any bound input changes.

Observations are append-only. If an eligible arm used the wrong manifest, grader, or environment, supersede the whole candidate with `router_lab.py restart`; never delete a losing run or replace one arm in place.

Run incumbent and challenger on identical isolated cases with the same prompt, context, tools, permissions, service tier, retry rule, and wall-time/token caps. Encode those inputs in one canonical manifest and require the same SHA-256 for both arms. Require the same independent grader identity hash on both arms. Randomize arm order when the runner permits it. A transient execution failure may receive one same-tier retry; a cognitive failure receives no hidden retry.

Every scored `router_lab.py record` import must include `--input-manifest-file`. The router reads, parses, and hashes that exact file instead of trusting a caller-supplied manifest hash. Its case identity, service tier, token cap, and wall-time cap must agree with the run and server metadata. Measured input plus output tokens must not exceed the manifest token cap, and the bound transcript's completed-turn duration must not exceed the manifest wall-time cap. Reject the arm and preserve an abort artifact on any mismatch or cap breach; do not append it as an eligible observation.

Process at most one challenger and ten arm runs per scheduled session. Carry the remaining cases explicitly in candidate state. Before the first model invocation, retain a run-budget manifest that enumerates every planned case, arm, retry allowance, requirement-derived per-call token cap, expected inference count, reviewer allowance, grader allowance, worst-case aggregate tokens, wall-time allocation, and rationale. Derive each cap from the canonical input manifest, including prompt, bounded context, tools, permissions, service tier, retry rule, measured runner input reserve, expected context growth, output allowance, and wall-time requirement. Paired arms use the same cap. When the persistent workflow learner coordinates the experiment, its shared manifest maps every worker, reviewer, grader, retry, rework, and escalation invocation one-to-one to a unique budget call ID and freezes role, phase, attempt kind, dispatcher-manifest hash, inference count, token cap, and wall-time cap. The aggregate includes every call plus the explicit reviewer and grader allowances; missing, extra, duplicated, misclassified, or over-cap work is ineligible. A fixed cap requires a named, versioned, self-hashed token-cap contract bound to the exact task-manifest hash plus an absolute retained measured-safe evidence path and matching hash; an arbitrary identifier or detached hash is not a contract. The cap is an inclusive maximum and an arm is rejected only when measured usage exceeds it. There is no universal session token ceiling. Stop at 25 minutes, do not launch unplanned calls, and never split a pair merely to fit a budget. A budget stop is inconclusive.

Use at least the active policy's `min_pairs_by_tier`, four holdout pairs, and three task families. Use recent objectively scorable live work to supplement the fixed suite. Never tune the policy after viewing holdout results; a changed policy creates a new experiment.

### Top-tier counterfactual baseline

Every T1, T2, or T3 candidate has an additional quality-preserving efficiency gate against the active T4 route. T4 candidates are exempt because T4 is the reference. This gate supplements the incumbent/challenger comparison; it never replaces the incumbent safety, quality, or efficiency gates.

Render a stable evaluation-only control profile from the candidate tier's exact role template, tools, permissions, and instruction contract, but bind its provider, model, and reasoning effort to the active T4 route. The control profile is not an active route and must never be used to bypass T4's read-only production contract. Within one comparison, candidate and top-tier arms must share the exact canonical input-manifest file, grader identity, prompt, context, tools, permissions, service tier, retry rule, token cap, and wall-time cap. Provider/model/effort and the resulting profile hash are the only intentional route differences.

Require five complete candidate-versus-top-tier comparisons covering at least three task families and one holdout. The candidate must be no more than 0.02 below the top-tier control on normalized quality, improve median measured tokens by at least 10%, improve median transcript-derived latency by at least 10%, and use strictly fewer tokens on at least four cases and strictly less transcript-derived latency on at least four cases. Both resource requirements must pass; saving only tokens or only latency is insufficient.

A top-tier control may be cached and reused only while fresh and only when the case, canonical manifest hash, grader identity and kind, service tier and caps, control-profile hash, active T4 provider/model/effort and model revision, catalog revision, runner path and hash, and promotion-rule hash all match exactly. Catalog, role-template, runner, grader, manifest, cap, or T4-route drift invalidates the cached control. Prefer the latest eligible exact match and retain all observations append-only.

Insufficient or stale top-tier coverage keeps the candidate `COLLECTING` without weakening active routing. Once coverage is complete, a quality, token, latency, positive-case, safety, or binding failure makes the candidate `REJECTED`. Ordinary workflow telemetry, ungraded smoke work, generic spawns, and self-reported model identity never count toward this baseline. Sample top-tier controls only for objectively graded candidate cases, reuse exact fresh controls where possible, and charge every new control invocation to the experiment's run and time budget.

## Enforcement gate

The user-selected default is observed server metadata. The policy must pin the absolute path and SHA-256 of an installed official Codex App Server executable. Accept its run metadata only when the turn completed and the record contains explicit requested and observed provider/model/effort/service-tier settings, thread and turn IDs, at least one completed agent-message ID, measured input/output usage, an absolute retained raw-JSONL transcript path and hash, and complete reroute and safety-buffer event lists. The router must read and hash that transcript itself and derive observed settings, message IDs, usage, completion, reroutes, safety buffering, and latency only from events scoped to the bound thread and turn. Events belonging to any other concurrent workflow are never evidence and never constitute a mismatch. These fields are available from the pinned App Server's `thread/start`, `turn/start`, `thread/settings/updated`, `item/completed`, `thread/tokenUsage/updated`, `model/rerouted`, `model/safetyBuffering/updated`, and `turn/completed` protocol surfaces. `turn/completed.turn.durationMs` is the latency authority; outer driver wall time and caller-supplied timing are not evidence, and any imported latency field must agree with it. This is a deliberately weaker operational standard: it proves what the pinned first-party client requested and observed from the server, but it is not a provider-signed proof of the model that generated the response.

When `adaptive-workflow-router/scripts/learning_loop.py` consumes a scored run, raw metadata is additionally insufficient by itself. The fixed workflow dispatcher must have crash-safely appended a private hash-chained execution receipt binding the exact metadata, transcript, input manifest, profile, identity, usage, duration, thread, turn, and completed messages. The retained grade must have a quality receipt binding the exact evaluated execution receipts, grader identity, rubric, case, and arm to the bundled deterministic grader or an independently routed blind grader. The learner re-reads and cross-checks every bound byte. These receipts are first-party harness provenance, not provider-signed evidence; generic spawns, caller-authored files, self-grading, and unregistered grades remain inadmissible.

- `external-verifier` pins an absolute verifier path and SHA-256 and requires that verifier to accept a runner-produced receipt. Use this mode when a compatible official receipt verifier exists.

In either mode, the normalized evidence must provide:

- run, case, arm, phase, tier, family, and holdout identity;
- machine-readable actual provider, model, and reasoning effort;
- exact profile, canonical input-manifest, grader-identity, and objective-evidence hashes;
- grader kind, risk-floor result, unauthorized-mutation, secret-exposure, unsupported-routing, verdict, normalized quality, and critical-failure signals;
- latency, tokens, billed cost and receipt source when present, and escalation count.

A natural-language request, profile filename, UI label, expected default, generic spawned agent, caller-supplied verification flag, or model self-report does not satisfy the gate. In `server-metadata` mode, the import must agree exactly with the metadata record and bound transcript. Before promotion, reject missing turn or completed-message identity, an incomplete turn, reroute, safety-buffer substitution, alias, provider mismatch, unequal service tier, unequal manifest, unequal grader, or mismatched effort before appending any observation; preserve an abort artifact and quarantine the candidate as `BLOCKED_MODEL_ENFORCEMENT`. On a promoted active canary, a structurally verified provider/model/effort/service-tier mismatch, reroute, or safety-buffer event is retained solely so `monitor` can enforce the exact rollback snapshot. In `external-verifier` mode, the import must agree exactly with verifier output. If neither mode can produce acceptable evidence, preserve the candidate and report `BLOCKED_MODEL_ENFORCEMENT` without spending more evaluation tokens.

## External grading

Prefer graders in this order:

1. Deterministic tests and expected artifacts.
2. Static checks, diff boundaries, exact facts, and prohibited-action checks.
3. A blind rubric applied without revealing arm identity.
4. Explicit user acceptance.

The challenger never grades itself. `codex-auto-review`, unblinded preference, style similarity, and unsupported model confidence are diagnostic only.

Record pass/fail, a normalized quality score, critical violations, latency, token usage, actual billed cost when a receipt exists, escalations, and evidence hashes. Never infer cost from catalog priority, descriptions, or token counts.

## Promotion gates

Apply gates lexicographically; do not hide a safety regression inside a composite score.

Require:

- a fresh evaluation under the current live catalog, incumbent policy, profiles, suite, and promotion rules that still returns exact qualification; a stale `QUALIFIED` state label is not activation authority;
- exact routing verification on every counted run;
- zero candidate critical failures;
- no forbidden provider, risk-floor violation, unauthorized mutation, secret exposure, or unsupported capability route;
- the tier's minimum pair count, holdout count, and family diversity;
- candidate quality no more than `max_quality_drop` below incumbent;
- extra incumbent-pass/candidate-fail cases no higher than the tier limit;
- no measured efficiency regression beyond `max_efficiency_regression`;
- either `min_quality_gain` or `min_efficiency_gain` on latency, token usage, billed cost, or escalation rate.

For T1-T3, also require the complete top-tier counterfactual gate: five exact pairs, three task families, one holdout, quality drop no greater than 0.02, both median token and transcript-latency gains of at least 10%, and at least four positive cases for each resource. Missing or stale counterfactual evidence is `COLLECTING`, not qualification; a complete counterfactual that misses any threshold is `REJECTED`. T4 is exempt.

Ties, missing comparable metrics, mixed catalog revisions, and insufficient evidence retain the incumbent. A new T3 or T4 candidate may not regress on any incumbent-passing critical case.

## Activation and rollback

Before promotion:

1. Recheck the catalog revision and incumbent policy hash.
2. Verify installed profiles exactly match the active policy.
3. Snapshot the active policy and all four profile files.
4. Render and parse all four target profiles.
5. Journal the activation, replace files atomically one by one, and verify hashes by readback.
6. Persist and read back the candidate's promoted state while the journal remains live.
7. Clear the journal only after both policy and candidate state commit. Recovery finalizes a fully committed promotion or restores its exact bound snapshot.

Restore the snapshot if activation fails. Retain append-only observations and experiment results.

After promotion, observe objectively scorable work and import paired active/control canary arms with the same input manifest and grader. `monitor` accepts no caller verdict or caller window count; it derives identity, safety, quality, resource regression, and complete configured windows from retained imported evidence. Roll back immediately on a critical violation, provider/model/effort/profile mismatch, risk-floor breach, unauthorized mutation, secret exposure, unsupported route, or new critical T3/T4 failure. Roll back when quality falls beyond the configured noninferiority margin or resource use materially regresses across the configured consecutive windows. Incomplete evidence remains `PROMOTED`; do not mark the route `VALIDATED` until the configured number of complete non-regressing windows exists. Restore the exact candidate-bound snapshot even when the catalog no longer lists its model; do not substitute an invented route.

Allow only one promoted canary at a time. Block unrelated policy activation until it validates or rolls back. If catalog drift requires any active-tier change during the canary, restore the canary's bound incumbent snapshot before applying availability repair.

## Scheduled behavior

### Persistent coordination and SERENDIPITY

`adaptive-workflow-router/scripts/learning_loop.py` may aggregate complete worker, reviewer, grader, retry, rework, escalation, invocation, and end-to-end resource evidence across scheduled cycles. It derives the family from the canonical live router catalog and active no-downgrade policy, freezes the workflow hash for a model experiment, binds exact `router_lab.py` observation IDs, and waits for this router's candidate state before recording `QUALIFIED`, `PROMOTED`, `VALIDATED`, or `ROLLED_BACK`. Every measured invocation maps one-to-one to a planned budget call, and trusted execution plus quality receipts are required. Every public ledger read or mutation recovers both ledger and workflow-authority journals before proceeding. It cannot activate a model, render an active model profile, substitute a route, or recommend its own model action.

The bounded SERENDIPITY capacity lane may nominate a safe current-family single-route mutation that is not predicted to improve the immediate objective. Capacity is derived from the policy-configured share and cycle history; a caller cannot enlarge it. The descriptor-indexed archive retains minimum quality, novelty, learning progress, and cross-task transfer as separate dimensions. Safe informative failures remain append-only with zero promotion credit, while unsafe, incorrect, unauthorized, wrong-family, or untrusted results cannot enter the archive. Periodic replays use only precommitted unrelated held-out task families and never tune production action statistics after holdout exposure. A later ordinary cycle must freeze ordered archive lineage at begin, after each named archive exists, and a causal manifest must bind archive, target-begin, validated-state, action, scope, and lineage hashes before retrospective stepping-stone credit is recorded. That credit changes no promotion state. Novelty, learning progress, transfer, surprise, and credit cannot waive exact execution identity, current-family or T4 invariants, objective correctness, safety, authority, quality noninferiority, minimum sample/holdout gates, top-tier controls, promotion policy, canary monitoring, or exact rollback.

Enforce the rolling version window and adopt current-family active routes after each catalog refresh and before any scored run.

Run a daily semantic catalog check. `doctor` reports active-routing, evaluation, and catalog health separately. `resolve-phase` and the workflow dispatcher are gated only by `routing_status=HEALTHY` and `workflow_routing_ready=true`; evaluation-runner failures, prepromotion candidate problems, and catalog-refresh maintenance must remain visible without disabling a healthy active policy. `CATALOG_STALE` is an instruction to run `refresh`, not a workflow outage. Doctor must surface every prepromotion candidate whose incumbent policy, catalog revision, evaluation suite, template set, or promotion rules no longer match current authority. Refresh reconciles those bindings by marking the candidate `STALE` without scored calls; only after health is restored may it stage at most one clean current-family challenger, which remains `STAGED` with zero calls for discovery by a new task. Stop scored work on evaluation degradation, but run a deterministic `resolve-phase` smoke check and report whether normal workflow routing remains ready. On no semantic change and no active experiment, perform zero experiment calls. A schema-only catalog rebaseline triggers no experiment; comparable real changes detected during the rebaseline follow the normal challenger gates. An instruction-contract-only active-route rebaseline also triggers no scored call and must be reported with its exact hashes. On a true semantic change, stage compatible challengers. Evaluate only when the enforcement gate is available. Process one challenger per session and prioritize a promoted candidate's canary. For every provider mismatch, cap breach, runner failure, malformed transcript, or other experiment error: preserve a concrete error artifact, perform the safe bounded remediation in the same cron run (recover, rollback, quarantine, stale, or stop further calls), rerun health plus the routing smoke check, and surface the error and action in the cron result. Keep every candidate in an explicit terminal or active state: staged, collecting, blocked, qualified, rejected, promoted, validated, rolled back, stale, or superseded.
