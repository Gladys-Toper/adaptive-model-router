# Adaptive Model Router

Evidence-gated model and workflow routing skills for coding agents. The model router assigns each phase of a task to the smallest proven model and reasoning tier; the workflow router composes multi-phase work and preserves execution evidence, promotion gates, and rollback.

This repository is the **published, installable** distribution. It is generated from the development harness — [`Gladys-Toper/adaptive-codex-harness`](https://github.com/Gladys-Toper/adaptive-codex-harness) — by `scripts/publish_sync.py`, which also writes `SYNC-MANIFEST.json` (the harness source commit plus a sha256, mode, and size for every published file). Send pull requests to the harness, not here; hand-edits to this tree are rejected by CI.

> ### Breaking path change
>
> Skills moved from `skills/<name>` to **`skills/<platform>/<name>`**:
>
> | Before | Now |
> |---|---|
> | `skills/adaptive-model-router/` | `skills/codex/adaptive-model-router/` |
> | `skills/adaptive-workflow-router/` | `skills/codex/adaptive-workflow-router/` |
>
> `./install.sh` with no arguments is unchanged — it still installs the Codex skills into `${CODEX_HOME:-~/.codex}/skills`. Anything that referenced the old paths directly (scripts, docs, symlinks, `git sparse-checkout` filters) must add the `codex/` segment.

## Platform matrix

| | Codex | Claude Code | Cursor |
|---|---|---|---|
| Published tree | `skills/codex/` | `skills/claude/` | `skills/cursor/` |
| Install target | `${CODEX_HOME:-~/.codex}/skills` | `${CLAUDE_CONFIG_DIR:-~/.claude}/skills` | `${CURSOR_HOME:-~/.cursor}/skills` |
| Runtime state | `~/.codex/adaptive-*` | `~/.claude/adaptive-*` | `~/.cursor/adaptive-model-router` |
| Model catalog | live `models_cache.json` | curated `claude-catalog.json` | curated `cursor-catalog.json` (confirmed against `cursor-agent models` when available) |
| Seeded tiers T1→T4 | `gpt-5.6-luna/low`, `gpt-5.6-terra/medium`, `gpt-5.6-terra/high`, `gpt-5.6-sol/ultra` | `haiku/low`, `sonnet/medium`, `opus/high`, `fable/max` | `composer-2.5-fast`, `gpt-5.3-codex`, `gpt-5.3-codex-high`, `gpt-5.3-codex-xhigh` |
| Subagent profiles | `~/.codex/agents/*.toml` | `~/.claude/agents/*.md` | not installed (profile schema unverified) |
| Scripted dispatcher | yes — App Server driver | yes — headless `claude -p` driver (T1–T3; T4 is in-session only) | no — documented dispatch contract only |
| Resumption lane | yes (`resume`, `smoke`, resume canary) | no — aborts are `ABORTED_NO_RESUMABLE_CHECKPOINT` | no |
| Governance ledger and receipts | yes | yes | no |
| Learning loop | yes | yes | no |
| Install transaction | transactional (journal, rollback, recovery) | plain copy with whole-tree backup | plain copy with whole-tree backup |

## Evidence grades

The grade states what the receipts actually prove, not how good the routing is. Scored model experiments (promotion of one model over another) require an **enforced** grade; every other platform keeps them `BLOCKED_MODEL_ENFORCEMENT` until an attestation verifier exists.

| Platform | Grade | What backs it | What is blocked |
|---|---|---|---|
| Codex | **enforced** | App Server protocol receipts, hash-chained execution registry, transcript binding, frozen release-source manifest | nothing — this is the reference lane |
| Claude Code | **declared** | first-party observed `claude -p --output-format stream-json` output, retained and hashed into a chained receipt (`claude-code-headless-stream`) | scored model promotion; headless T4 (`--effort` accepts only `low\|medium\|high`, so `fable/max` is dispatched in-session and refused headlessly as `T4_HEADLESS_UNSUPPORTED`) |
| Cursor | **declared-weak / experimental** | nothing beyond the planner and the routing policy — no receipts, no governance ledger, headless result metadata unverified | scored experiments, promotion, the learning loop, and any automated dispatch claim; routes carry `REQUESTED_NOT_ATTESTED` |

## Quick install

Requirements: Git, Python 3.11 or newer, and the agent you are installing for.

```bash
git clone --depth 1 https://github.com/Gladys-Toper/adaptive-model-router.git
cd adaptive-model-router
```

Before copying anything, the installer recomputes the sha256 and mode of every file in the platform's tree against `SYNC-MANIFEST.json` and refuses to install on any drift, missing file, stray file, or symlink.

### Codex (default)

```bash
./install.sh                 # identical to ./install.sh --platform codex
```

Requires a populated `${CODEX_HOME:-~/.codex}/models_cache.json` — open Codex once first. The installer refuses to overwrite an existing install, runs the model-router suite from the repo, copies both skills, renders the four agent profiles, baselines the catalog, and then runs the workflow planner, dispatcher, governance, and learning-loop suites from the installed tree.

### Claude Code

```bash
./install.sh --platform claude
```

Backs up any existing `~/.claude/skills/adaptive-*-router` to `<name>.pre-harness-<timestamp>`, copies both skills, renders `~/.claude/agents/{fast-operator,standard-worker,high-solver,ultra-planner}.md`, baselines the curated catalog, runs the four workflow suites from the installed tree, and ends with `workflow_dispatch.py check`.

### Cursor (experimental)

```bash
./install.sh --platform cursor
```

Plain copy, no install transaction. Backs up any existing skill tree and any pre-harness `~/.cursor/adaptive-model-router/active-policy.json` to `active-policy.json.pre-harness-<date>`, then writes the standard-schema policy. No agent profiles are installed. Dispatch is the documented contract in the skill — an in-session Task with an explicit model, or headless `cursor-agent -p --model <slug>` — with effort expressed only through slug variants and bracket parameters.

### Other flags

```bash
./install.sh --verify-only               # verify every published tree against SYNC-MANIFEST.json
./install.sh --platform claude --dry-run # verify, then print the exact install plan; touches nothing
./install.sh --help
```

## Using the skills

After installing, start a new session and say:

```text
Use adaptive-model-router to route this task.
```

For multi-phase work:

```text
Use adaptive-workflow-router to plan and route this task.
```

Runtime state, receipts, and generated evidence stay machine-local and are never committed to this repository.

## Validate without installing

Each platform's suites run offline from the published tree. Use an isolated home so nothing touches your live agent state.

```bash
# Claude
CLAUDE_CONFIG_DIR="$(mktemp -d)" python3 skills/claude/adaptive-model-router/scripts/test_router_lab.py
CLAUDE_CONFIG_DIR="$(mktemp -d)" python3 skills/claude/adaptive-workflow-router/scripts/test_workflow_dispatch.py

# Cursor
CURSOR_HOME="$(mktemp -d)" python3 skills/cursor/adaptive-model-router/scripts/test_router_lab.py
python3 skills/cursor/adaptive-workflow-router/scripts/test_workflow_plan.py

# Codex (needs a models_cache.json in CODEX_HOME; see .github/workflows/verify.yml for the fixture)
python3 skills/codex/adaptive-workflow-router/scripts/test_workflow_plan.py
```

`.github/workflows/verify.yml` runs all of the above on Python 3.12 for every platform, plus the `SYNC-MANIFEST.json` recomputation that makes hand-edits to this repository fail closed.

## Development

Source of truth: [`Gladys-Toper/adaptive-codex-harness`](https://github.com/Gladys-Toper/adaptive-codex-harness).

- Codex skills live at the harness root (`adaptive-model-router/`, `adaptive-workflow-router/`).
- Claude and Cursor ports live under `platforms/{claude,cursor}/`.
- Platform-neutral bytes are canonical in `core/` and vendored into each tree under a byte-parity CI gate.
- `scripts/publish_sync.py sync --published-root <this repo>` regenerates everything here.

## License

MIT. See [LICENSE](LICENSE).
