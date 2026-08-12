# Self-Improvement Protocol

Use a bounded Karpathy-style autoresearch loop: freeze one routing change, run paired experiments, keep a measured win, discard a loss, and retain an exact rollback.

## Sources of truth

Use these authorities in order:

1. `active-policy.json` (in `~/.claude/adaptive-model-router/`, under `$CLAUDE_CONFIG_DIR` when set) for the live tier-to-model mapping.
2. The curated `assets/claude-catalog.json` semantic snapshot for harness-available model availability and capabilities. Update this file in place to change the catalog; the `ADAPTIVE_ROUTER_CATALOG_SOURCE` override is honored only inside an isolated `ADAPTIVE_MODEL_ROUTER_TEST_ROOT` and rejected in real use.
3. Runtime execution metadata for the model and effort actually used.
4. Objective evaluation evidence for suitability.
5. Current official Anthropic and Claude Code documentation for candidate discovery and capability interpretation.

Claude Code exposes no account-level catalog file; the selectable model set is harness-defined, so the curated snapshot is the trusted catalog. New models arrive by updating the curated catalog from official Anthropic announcements. An official announcement may nominate a candidate but cannot prove harness availability. A profile file may request a model but cannot prove execution. Runtime self-report is not attestation.

## Catalog discovery

Hash behaviorally relevant fields: provider, slug, model id, visibility, selectability, supported reasoning efforts, input modalities, context window, declared capability flags, and instruction hash.

Exclude `captured_at`, descriptions, display names, list priority, and raw file mtime from change detection. Treat an instruction, capability, or model-id change as a new challenger revision. Treat the first observation as a baseline, not a release event. Catalog metadata alone never replaces an incumbent.

Stage only models that:

- originate in the trusted curated Claude catalog;
- are visible, selectable, and accept text;
- support the tier's exact effort and required tools;
- are not hidden or non-selectable service entries;
- carry provider `anthropic` and contain no non-Claude identifier (no `gpt`, `openai`, `gemini`, `grok`, `deepseek`, `llama`, `mistral`, `qwen`, or `kimi` pattern).

Require `low` for T1, `medium` for T2, `high` for T3, and exact `max` for a new T4 challenger; `xhigh` appears only in T4 fallback entries. A model lacking image input may serve only T1 phases whose bound input manifest proves that no visual inspection is required. All currently cataloged Claude models accept image input, so this floor is presently inactive; keep the mechanism for future catalog entries.

When an active model's semantic revision changes, move that tier to its first eligible fallback before scoring the new revision. Stage the changed revision against the fallback; never silently treat an untested revision as the incumbent.

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

Process at most one challenger and ten arm runs per scheduled session. Carry the remaining cases explicitly in candidate state. Stop at 25 minutes or 200,000 input-plus-output tokens per session. A budget stop is inconclusive.

Use at least the active policy's `min_pairs_by_tier`, four holdout pairs, and three task families. Use recent objectively scorable live work to supplement the fixed suite. Never tune the policy after viewing holdout results; a changed policy creates a new experiment.

## Enforcement gate

The Claude Agent and Workflow tools do not return trusted runtime model metadata, so a scored experiment requires a configured attestation verifier; without one, staging remains available and scoring reports `BLOCKED_MODEL_ENFORCEMENT`.

Accept a run only when the active policy pins an absolute verifier path and its SHA-256, and that verifier accepts a runner-produced attestation file. The verified JSON must provide:

- run, case, arm, phase, tier, family, and holdout identity;
- machine-readable actual provider, model, and reasoning effort;
- exact profile, canonical input-manifest, grader-identity, and objective-evidence hashes;
- grader kind, risk-floor result, unauthorized-mutation, secret-exposure, unsupported-routing, verdict, normalized quality, and critical-failure signals;
- latency, tokens, billed cost and receipt source when present, and escalation count.

A natural-language request, filename, UI label, expected default, generic spawned agent, caller-supplied verification flag, or model self-report does not satisfy the gate. The import must agree exactly with the verifier output. Reject aliases, fallbacks, parent overrides, provider mismatches, unequal input manifests, unequal graders, or mismatched efforts. If enforcement is unavailable, preserve the staged candidate and report `BLOCKED_MODEL_ENFORCEMENT` without spending evaluation tokens.

## External grading

Prefer graders in this order:

1. Deterministic tests and expected artifacts.
2. Static checks, diff boundaries, exact facts, and prohibited-action checks.
3. A blind rubric applied without revealing arm identity.
4. Explicit user acceptance.

The challenger never grades itself. Harness auto-review, unblinded preference, style similarity, and unsupported model confidence are diagnostic only.

Record pass/fail, a normalized quality score, critical violations, latency, token usage, actual billed cost when a receipt exists, escalations, and evidence hashes. Never infer cost from catalog priority, descriptions, or token counts.

## Promotion gates

Apply gates lexicographically; do not hide a safety regression inside a composite score.

Require:

- exact routing verification on every counted run;
- zero candidate critical failures;
- no forbidden provider, risk-floor violation, unauthorized mutation, secret exposure, or unsupported capability route;
- the tier's minimum pair count, holdout count, and family diversity;
- candidate quality no more than `max_quality_drop` below incumbent;
- extra incumbent-pass/candidate-fail cases no higher than the tier limit;
- no measured efficiency regression beyond `max_efficiency_regression`;
- either `min_quality_gain` or `min_efficiency_gain` on latency, token usage, billed cost, or escalation rate.

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

After promotion, observe objectively scorable work and compare paired active/control arms with the same input manifest and grader. Roll back immediately on a critical violation, provider/model/effort/profile mismatch, risk-floor breach, unauthorized mutation, secret exposure, unsupported route, or new critical T3/T4 failure. Roll back when quality falls beyond the configured noninferiority margin or resource use materially regresses across the configured consecutive windows. Do not mark the route `VALIDATED` until that many complete non-regressing windows exist. Restore the exact candidate-bound snapshot even when the catalog no longer lists its model; do not substitute an invented route.

Allow only one promoted canary at a time. Block unrelated policy activation until it validates or rolls back. If catalog drift requires any active-tier change during the canary, restore the canary's bound incumbent snapshot before applying availability repair.

## Event-driven behavior

Run a semantic catalog check only when an event warrants it: an official Anthropic model or effort announcement, an edit to the curated snapshot, a routing failure, or an explicit user request. Do not schedule checks; the curated catalog cannot change on its own, so a timed sweep can only waste tokens. On no semantic change and no active experiment, perform zero experiment calls. A schema-only catalog rebaseline triggers no experiment; comparable real changes detected during the rebaseline follow the normal challenger gates. On a true semantic change, stage compatible challengers. Evaluate only when the enforcement gate is available. Process one challenger per session and prioritize a promoted candidate's canary. Keep every candidate in an explicit terminal or active state: staged, collecting, blocked, qualified, rejected, promoted, validated, rolled back, stale, or superseded.
