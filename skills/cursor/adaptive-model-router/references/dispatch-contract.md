# Cursor dispatch contract

This document is the "profile" every tier resolution binds to (`profile_file` /
`profile_sha256` in a `resolve-phase` response). Cursor has no per-tier
installed agent-profile file in pass 1 — `~/.cursor/agents/` exists but its
frontmatter schema is unverified, so `router_lab.py` does not render one. This
file is the single, exact, quality-bar-carrying artifact that stands in for
per-tier profile identity: a routed phase's evidence hashes this file, not a
generated one, and any drift in the dispatch contract is visible as a
`profile_sha256` change.

## Two dispatch surfaces

1. **In-session Task tool (primary, T1-T4).** The parent Cursor agent spawns a
   subagent with an explicit model:

   ```text
   Task(subagent_type=..., model=<active tier slug>, description=..., prompt=...)
   ```

   This is the same pattern the resident LiberAI router stack on this machine
   already uses. It has full tool access (or `readonly` for T4 planning), and
   its result is a normal assistant turn — there is no separate structured
   receipt beyond what the parent agent observes directly.

2. **Headless CLI (documented, optional, not scripted in pass 1).**

   ```text
   cursor-agent -p --model <active tier slug> --output-format json "<prompt>"
   cursor-agent -p --model <active tier slug> --output-format stream-json --mode plan "<prompt>"   # read-only planning
   ```

   `--model` pins the exact model *and* effort simultaneously, because effort
   is baked into the slug (`gpt-5.3-codex-high`) or expressed as a bracket
   override (`'claude-opus-4-8[context=1m,effort=high,fast=false]'`). There is
   no independent `--effort` flag the way `claude`/`codex` expose one.

## What is honestly NOT claimed

- No receipt chain, hash-chained execution registry, or App-Server-style
  turn/token verification exists for either surface in pass 1.
- Headless `--output-format json` result-metadata fidelity (does it echo the
  resolved model on every completion? on parameterized/bracket slugs?) has not
  been probed live as part of this change, per the handoff's "no live spend
  without an explicit probe" instruction. Until it is, this skill assumes
  nothing beyond "the CLI accepted the flag" and treats every phase as
  `REQUESTED_NOT_ATTESTED`, `runtime_evidence_required: true`,
  `runtime_attestation_required: true`. Scored, promotion-gated routing stays
  `BLOCKED_MODEL_ENFORCEMENT` — this skill ships no catalog-experiment or
  promotion machinery at all (see `references/self-improvement-protocol.md`).
- `provider` in a `resolve-phase` response is `"cursor"`: it names the
  dispatch surface (the Cursor CLI/agent runtime), not the upstream model
  vendor, since Cursor multiplexes OpenAI/Anthropic/xAI/Google/Moonshot/Zhipu
  models behind one surface.

## Scope guard

This contract governs Cursor's own working-agent dispatch only (the agent's
choice of model for its own sub-tasks). It never selects a model on behalf of
a user-facing or product surface, and it never touches the separate
`liberai-*-router` skill stack or its state.
