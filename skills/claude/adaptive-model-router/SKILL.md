---
name: adaptive-model-router
description: Route Claude Code work to the smallest proven Claude model and reasoning effort for each phase, discover newly available Claude models from the curated catalog, and reoptimize routing through paired evals, measured outcomes, promotion gates, and exact rollback. Use when choosing models or reasoning effort for subagents or workflows, dispatching via the Agent tool or Workflow tool, coordinating multi-agent work, controlling cost or latency, assigning planning and implementation, polling CI or PRs, handling release operations, evaluating a newly available Claude model, or checking whether the current model policy remains optimal.
---

# Adaptive Model Router

Spend reasoning on the phases that need it. Keep model selection adaptive, evidence-backed, and reversible.

## Operating contract

- Route each phase independently; do not label the whole request with one tier.
- Use the smallest tier that clears both the reasoning need and the risk floor.
- Treat `~/.claude/adaptive-model-router/active-policy.json` (under `$CLAUDE_CONFIG_DIR` when set) as model authority. Treat this skill's tier descriptions as role authority.
- Preserve the parent agent as coordinator unless the user requests a separate task or thread.
- Never claim that a model changed unless runtime metadata proves the selected model and effort.
- Restrict automatic routing to locally available Claude models in this harness; never substitute another provider.
- Scope guard: this policy governs routing of Claude Code's own working agents only. Model selection for products, experiments, teacher or judge roles, or anything user-facing stays with the user and the `model-recommender` skill, and an explicit user model choice always overrides the adaptive policy.

## Load the active route

Load the route once per session, not once per decision: run `python3 scripts/router_lab.py status` at the first routing decision, then reuse its assignments until a route fails, the policy changes, or the session is told otherwise. Fast path: for one or two dispatches, route directly from the table below; invoke the scripts only for multi-phase work, machine-readable phase requests, or after a routing failure. Run `python3 scripts/router_lab.py refresh --stage-new` only when the catalog baseline is absent or `assets/claude-catalog.json` was edited.

Use the four active assignments returned by the lab:

| Tier | Role and effort | Use for | Avoid |
| --- | --- | --- | --- |
| T0 deterministic | No model | Waiting, exact CI or PR status reads, scripted checks, formatter execution, file existence checks | Interpretation, diagnosis, or decisions |
| T1 fast operator | `fast-operator`, active policy model at `low` (baseline `haiku`) | Read-heavy scans, log filtering, status summaries, exact commands, mechanical PR creation with supplied inputs | Visual inputs, architecture, ambiguous debugging, sensitive edits, merge or deploy decisions |
| T2 standard worker | `standard-worker`, active policy model at `medium` (baseline `sonnet`) | Contained implementation, ordinary tests, localized debugging, straightforward documentation and review | Cross-system ambiguity or high-consequence judgment |
| T3 high solver | `high-solver`, active policy model at `high` (baseline `opus`) | Cross-file diagnosis, nuanced implementation, edge cases, difficult review, conflicting evidence, auth or release analysis | Passive waiting and repetitive polling |
| T4 ultra planner | `ultra-planner`, active policy model at its qualified deepest effort (baseline `fable` at `max`) | Hard architecture, novel multi-system planning, migrations, adversarial or security reasoning, adjudication | Routine coding, known execution steps, status checks, idle waiting |

Require exact `max` support for a new T4 challenger. A model without image input may serve T1 only when the phase has no image or visual-inspection requirement; keep it out of T2-T4. All currently cataloged Claude models accept image input, so this floor is presently inactive; retain it for future catalog entries.

T4 is the per-agent equivalent of the session effort slider's Ultracode position: `max` is the deepest effort expressible in agent frontmatter and Workflow calls. Ultracode itself is a session posture (deepest effort plus a standing multi-agent orchestration opt-in), not a routable effort value. Under Ultracode, this policy is what keeps exhaustive from meaning extravagant: scale coverage with more T1 and T2 workers, not top-tier models on mechanical work.

## Route the work

1. Decompose the request into discovery, planning, implementation, verification, polling, and release phases.
2. Remove phases that need no model. Prefer a deterministic tool, script, wait primitive, or automation.
3. Assign the smallest tier that can satisfy the phase.
4. Raise the tier to meet the risk floor.
5. Delegate only bounded work with clear inputs, outputs, and stopping conditions. Pass minimum useful context.
6. Reassess after evidence. De-escalate when ambiguity resolves; escalate after a grounded cognitive failure.

Dispatch routed phases on Claude Code surfaces:

- T1-T4 via the Agent tool with `subagent_type` set to `fast-operator`, `standard-worker`, `high-solver`, or `ultra-planner` once profiles are installed. The profile carries both model and effort; the Agent tool itself exposes a `model` parameter but no effort parameter, so prefer the installed profile for effort-exact routing.
- T1-T4 via the Workflow tool with `agent(prompt, {model, effort})` or `{agentType: "<profile>"}`, bound from `resolve-phase` output.
- T0 via deterministic Bash, scripts, Monitor, or background-task notifications — never a reasoning model idle-waiting or poll-looping. Prefer event-driven reads over watch loops.

For application workflows, let `adaptive-workflow-router` own phase order, dependencies, artifacts, and completion gates. Keep this skill as the sole authority for tiers, models, effort, escalation, promotion, and rollback. Resolve a machine-readable phase request with:

```text
router_lab.py resolve-phase --request REQUEST.json
cat REQUEST.json | router_lab.py resolve-phase --request -
```

The response binds the route to the active policy and profile hash. `REQUESTED_NOT_ATTESTED` means the route is selected but the actual runtime model still requires trusted execution metadata; never treat the requested identity as proof of execution.

## Apply risk floors

- Require at least T2 for changes affecting runtime behavior, data shape, public interfaces, security posture, dependencies, or builds. Allow T1 for an exact reversible typo or formatter-only edit.
- Require at least T3 for authentication, authorization, secrets, billing, security, destructive data changes, schema migrations, production deploys, merge decisions, or rollback design.
- Apply sensitive-domain floors to causal interpretation, design, decisions, and mutations. Allow tightly scoped T1 evidence collection that exposes no secret values and makes no sensitive judgment.
- Use T4 when T3-level risk combines with substantial ambiguity, novelty, interacting systems, weak observability, or repeated grounded failures.
- Keep final authority for irreversible or externally visible actions with the parent agent.
- Judge reasoning and consequence, not output length.

## Optimize execution

- Downroute to T1 without deliberation: broad file or code searches, log filtering, status reads and summaries, file inventories and counts, verbatim copies or format conversions, formatter runs, and document summaries. These dominate scan-heavy work and never need a top-tier model.
- Plan once at the necessary tier, then hand bounded execution to T2 or T1.
- Send large read-only discovery work to T1 and return distilled evidence to T3 or T4.
- Use T0 for polling. Use T1 for summaries; use T2 or T3 for failure diagnosis.
- Open a PR at T1 only from an already prepared branch with supplied title and body. Use T3 for merge, release, deploy, or rollback decisions.
- Do not duplicate agent work unless independent validation is worth the cost.
- Stop agents after their phase. Never spend a reasoning model on idle waiting.
- Preserve evidence paths, commands, test results, and uncertainty in handoffs.

## Escalate and recover

- Retry a transient tool, network, or provider error at the same tier.
- Escalate one tier after a reasoning miss, missed constraint, contradictory conclusion, or unverifiable plan.
- Do not repeat the same failed cognitive prompt more than once at one tier.
- Send conflicting lower-tier conclusions to T3; use T4 only for materially complex or high-risk conflicts.
- De-escalate after the plan, invariant, or exact command is established.

## Reoptimize the policy

Before catalog maintenance, experiments, promotion, or rollback, read [references/self-improvement-protocol.md](references/self-improvement-protocol.md) completely.

The lab is event-driven and currently dormant. The curated catalog changes only when Anthropic ships a model or effort change, so run `refresh` when such an announcement lands or the user asks — never on a schedule — and spend zero lab tokens otherwise. Until an attestation verifier is configured, scored experiments stay `BLOCKED_MODEL_ENFORCEMENT`: treat policy changes as manual, user-approved judgment calls, and do not stage experiments that cannot be scored.

- Discover candidates from the curated Claude catalog snapshot (`assets/claude-catalog.json`) using semantic capability hashes. In real use, new models arrive by editing that file in place from official Anthropic announcements; the `ADAPTIVE_ROUTER_CATALOG_SOURCE` override is honored only inside an isolated `ADAPTIVE_MODEL_ROUTER_TEST_ROOT` under the system temp dir and is rejected as an unsafe path override otherwise. The first observation of a model is a baseline, not a release event. Do not treat a capture timestamp, display name, or marketing description as evidence of improvement.
- Stage newly available compatible models as challengers; catalog metadata alone never replaces an incumbent.
- Change one tier assignment per experiment. Compare incumbent and challenger on identical cryptographically bound inputs and budgets.
- Count only runs accepted by the policy-pinned attestation verifier with machine-verifiable provider, model, effort, profile, input manifest, grader identity, and safety signals.
- Grade with deterministic tests, objective checks, blind rubrics, or user acceptance. Never let a challenger grade itself.
- Keep ties and inconclusive results on the incumbent.
- Promote only after the lab's quality, safety, holdout, family-diversity, and efficiency gates pass.
- Snapshot the full policy and all four profiles before activation. Keep the activation journal until policy and candidate state both commit. Roll back exact bytes on activation failure or a qualified regression.
- Retain the incumbent as a canary control after promotion. Require the configured complete windows before validation and automatically restore the candidate-bound snapshot on regression.
- The Claude Agent and Workflow tools do not return trusted runtime model metadata, so scored experiments require a configured attestation verifier (`configure-attestation`). Without one, staging still works; if the current surface cannot enforce and attest model selection, record `BLOCKED_MODEL_ENFORCEMENT`, run no scored experiment, and permit no promotion.

Use the lab commands:

```text
router_lab.py refresh --stage-new
router_lab.py stage --model MODEL --tier TIER
router_lab.py restart --candidate-id ID --reason REASON
router_lab.py configure-attestation --verifier PATH --sha256 HASH
router_lab.py resolve-phase --request REQUEST.json
router_lab.py record ...
router_lab.py evaluate --candidate-id ID
router_lab.py promote --candidate-id ID
router_lab.py monitor --candidate-id ID
router_lab.py rollback
router_lab.py recover
router_lab.py doctor
router_lab.py status
```

If one arm was imported with the wrong input manifest or grader, do not overwrite or cherry-pick the append-only evidence. Use `restart` to supersede the entire contaminated experiment and issue a fresh candidate ID.

## Use custom agents

Prefer `ultra-planner`, `high-solver`, `standard-worker`, and `fast-operator` when the surface supports explicit custom-agent selection (the Agent tool's `subagent_type` or the Workflow tool's `agentType`). The active policy renders their model and effort frontmatter into the agents directory (`~/.claude/agents/`, under `$CLAUDE_CONFIG_DIR` when set).

Candidate profiles use `router-candidate-t<N>-<hash8>.md` names. Use them only inside a controlled paired experiment. If the spawn surface exposes no custom role or model selector, treat all spawned work as inherited-model work and do not score it.

For installation on another machine, run `python3 scripts/install_agent_profiles.py` only when the user requests installation. Use `python3 scripts/router_lab.py sync-agents` to render the active adaptive policy.

## Report routing briefly

Expose a route manifest only when requested, when cost or latency matters, or when a tier changes:

```text
phase -> tier / active model / effort: reason; exit condition
```

Report catalog experiments as `STAGED`, `COLLECTING`, `BLOCKED_MODEL_ENFORCEMENT`, `QUALIFIED`, `REJECTED`, `PROMOTED`, `VALIDATED`, or `ROLLED_BACK`. Lead with task progress.
