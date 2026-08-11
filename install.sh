#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
TARGET_SKILLS_DIR="${TARGET_CODEX_HOME}/skills"

SKILLS=(adaptive-model-router adaptive-workflow-router)

command -v python3 >/dev/null 2>&1 || {
  echo "Python 3 is required." >&2
  exit 1
}

MODEL_CATALOG="${CODEX_MODEL_CATALOG:-${TARGET_CODEX_HOME}/models_cache.json}"
if [[ ! -f "${MODEL_CATALOG}" ]]; then
  echo "Codex's model catalog was not found at ${MODEL_CATALOG}." >&2
  echo "Open Codex once so it can populate the catalog, then rerun this installer." >&2
  exit 1
fi

for skill in "${SKILLS[@]}"; do
  if [[ -e "${TARGET_SKILLS_DIR}/${skill}" ]]; then
    echo "Refusing to overwrite existing ${TARGET_SKILLS_DIR}/${skill}." >&2
    echo "Move or remove that directory, then run this installer again." >&2
    exit 1
  fi
done

python3 "${REPO_DIR}/skills/adaptive-model-router/scripts/test_router_lab.py"

mkdir -p "${TARGET_SKILLS_DIR}"
for skill in "${SKILLS[@]}"; do
  cp -R "${REPO_DIR}/skills/${skill}" "${TARGET_SKILLS_DIR}/${skill}"
  echo "Installed ${skill}."
done

python3 "${TARGET_SKILLS_DIR}/adaptive-model-router/scripts/install_agent_profiles.py"
python3 "${TARGET_SKILLS_DIR}/adaptive-model-router/scripts/router_lab.py" refresh --stage-new
python3 "${TARGET_SKILLS_DIR}/adaptive-model-router/scripts/router_lab.py" sync-agents
python3 "${TARGET_SKILLS_DIR}/adaptive-model-router/scripts/router_lab.py" status
python3 "${TARGET_SKILLS_DIR}/adaptive-workflow-router/scripts/test_workflow_plan.py"
python3 "${TARGET_SKILLS_DIR}/adaptive-workflow-router/scripts/test_workflow_dispatch.py"
python3 "${TARGET_SKILLS_DIR}/adaptive-workflow-router/scripts/test_agent_governance.py"
python3 "${TARGET_SKILLS_DIR}/adaptive-workflow-router/scripts/test_learning_loop.py"

echo "Installation complete. Start a new Codex turn to use the skills."
