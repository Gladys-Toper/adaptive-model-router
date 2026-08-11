# Adaptive Model Router for Codex

Evidence-gated model and workflow routing skills for Codex. The router assigns each phase to the smallest proven model/reasoning tier, while the workflow harness composes multi-phase work and preserves execution evidence, promotion gates, and rollback.

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
python3 skills/adaptive-workflow-router/scripts/test_learning_loop.py
```

## License

MIT. See [LICENSE](LICENSE).
