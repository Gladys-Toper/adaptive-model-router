# Adaptive Model Router for Codex

Evidence-gated model and workflow routing skills for Codex. The router assigns each phase to the smallest proven model/reasoning tier, while the workflow harness composes multi-phase work and preserves execution evidence, promotion gates, and rollback.

Planned work prefers fixed, hash-bound context bundles. Tokens, harness-started model turns, tool calls, API calls, and wall time are capped independently; internal reasoning fragments do not inflate the model-turn count. Tool-enabled work waits for its local execution bridge before model dispatch.

Accepted completion receipts record actual use by workflow, observed model, and effort. Only revalidated, unanimously quality-accepted receipts enter successful-use distributions. Future envelopes combine those measured distributions with model-specific cost and quota weights; a larger envelope requires retained evidence and an independently reviewed increase contract.

Parallelism is task-derived rather than agent-count-capped. A deterministic planner packs an exact branch DAG into dependency-safe, mutation-conflict-free waves under a trusted global weighted-capacity snapshot. More than nine branches are valid when the work and capacity justify them; unbound descendants, source drift, scope overlap, and completion without a matching execution receipt fail closed.

Unchanged CI, scheduler, deployment, and merge state is polled by deterministic processes. A routed model wakes only for terminal audit or failure diagnosis; it never holds a streaming wait tool open.

## Included skills

- `adaptive-model-router` — model, reasoning-tier, evaluation, promotion, and rollback authority.
- `adaptive-workflow-router` — multi-phase workflow planning and execution harness. It depends on `adaptive-model-router`.

## Quick install

Requirements: Codex, Git, and Python 3.11 or newer.

```bash
git clone --depth 1 https://github.com/Gladys-Toper/adaptive-model-router.git
cd adaptive-model-router
./install.sh
```

Start a new Codex turn after installation. You can then say:

```text
Use $adaptive-model-router to route this task.
```

For a multi-phase workflow:

```text
Use $adaptive-workflow-router to plan and route this task.
```

## Install through Codex

You can also give Codex this repository and ask:

```text
Install both skills from https://github.com/Gladys-Toper/adaptive-model-router
```

The skills are installed under `$CODEX_HOME/skills` (normally `~/.codex/skills`). Router runtime state and generated evidence remain machine-local and are not included in this repository.

## Validate

```bash
python3 skills/adaptive-model-router/scripts/test_router_lab.py
python3 skills/adaptive-workflow-router/scripts/test_workflow_plan.py
python3 skills/adaptive-workflow-router/scripts/test_workflow_dispatch.py
python3 skills/adaptive-workflow-router/scripts/test_agent_governance.py
python3 skills/adaptive-workflow-router/scripts/test_learning_loop.py
```

## License

MIT. See [LICENSE](LICENSE).
