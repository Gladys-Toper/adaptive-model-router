#!/usr/bin/env bash
# Install the adaptive routing skills for one agent platform.
#
# Published bytes are verified against SYNC-MANIFEST.json (sha256 + mode, per
# file, plus stray-file detection) before anything is copied, so a hand-edited
# or partially downloaded clone fails closed instead of being installed.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_PATH="${REPO_DIR}/SYNC-MANIFEST.json"
SKILLS=(adaptive-model-router adaptive-workflow-router)
PLATFORM="codex"
MODE="install"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
  cat <<'USAGE'
Usage: ./install.sh [--platform codex|claude|cursor] [--dry-run] [--verify-only]

  --platform codex    Install into ${CODEX_HOME:-~/.codex}/skills            (default)
  --platform claude   Install into ${CLAUDE_CONFIG_DIR:-~/.claude}/skills
  --platform cursor   Install into ${CURSOR_HOME:-~/.cursor}/skills          (experimental)

  --dry-run           Verify published bytes and print the exact install plan.
                      Nothing is copied and no agent home is touched.
  --verify-only       Verify published bytes against SYNC-MANIFEST.json and exit.
                      With --platform all (or no --platform), verifies every tree.
  -h, --help          Show this message.

Evidence grades: codex = enforced, claude = declared, cursor = declared-weak
(experimental). See README.md.
USAGE
}

die() { printf '%s\n' "$*" >&2; exit 1; }

PLATFORM_EXPLICIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --platform)
      [[ $# -ge 2 ]] || die "--platform requires a value."
      PLATFORM="$2"; PLATFORM_EXPLICIT=1; shift 2 ;;
    --platform=*)
      PLATFORM="${1#*=}"; PLATFORM_EXPLICIT=1; shift ;;
    --dry-run) MODE="dry-run"; shift ;;
    --verify-only) MODE="verify"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "Unknown argument: $1" ;;
  esac
done

if [[ "${MODE}" == "verify" && "${PLATFORM_EXPLICIT}" -eq 0 ]]; then
  PLATFORM="all"
fi

case "${PLATFORM}" in
  codex|claude|cursor) ;;
  all)
    [[ "${MODE}" == "verify" ]] || die "--platform all is only valid with --verify-only."
    ;;
  *) die "Unknown platform: ${PLATFORM} (expected codex, claude, or cursor)." ;;
esac

command -v python3 >/dev/null 2>&1 || die "Python 3 is required."

# ---------------------------------------------------------------------------
# Published-byte verification (replaces a blind `cp -R`)
# ---------------------------------------------------------------------------
verify_published_tree() {
  local scope="$1"
  python3 - "${MANIFEST_PATH}" "${REPO_DIR}" "${scope}" <<'PY'
import hashlib
import json
import stat
import sys
from pathlib import Path

manifest_path, repo_dir, scope = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

EXCLUDED_NAMES = {"__pycache__", ".DS_Store", ".git", ".pytest_cache"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
MANIFEST_TYPE = "adaptive-router.publish-sync-manifest"
SCHEMA_VERSION = 1


def fail(message: str) -> None:
    print(f"FAILED published-byte verification: {message}", file=sys.stderr)
    raise SystemExit(1)


if not manifest_path.is_file():
    fail(f"SYNC-MANIFEST.json is missing at {manifest_path}")
try:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
except json.JSONDecodeError as error:
    fail(f"SYNC-MANIFEST.json is unreadable: {error}")
if (
    not isinstance(manifest, dict)
    or manifest.get("type") != MANIFEST_TYPE
    or manifest.get("schema_version") != SCHEMA_VERSION
    or not isinstance(manifest.get("files"), list)
):
    fail("SYNC-MANIFEST.json has an unsupported schema")

prefix = "skills" if scope == "all" else f"skills/{scope}"
expected: dict[str, dict] = {}
for entry in manifest["files"]:
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        fail("SYNC-MANIFEST.json has a malformed file entry")
    path = entry["path"]
    if path == prefix or path.startswith(prefix + "/"):
        if path in expected:
            fail(f"SYNC-MANIFEST.json lists {path} twice")
        expected[path] = entry
if not expected:
    fail(f"SYNC-MANIFEST.json records no files under {prefix}")

root = repo_dir / prefix
if not root.is_dir():
    fail(f"published tree is missing: {root}")

observed: set[str] = set()
for path in sorted(root.rglob("*")):
    relative = path.relative_to(repo_dir)
    if any(part in EXCLUDED_NAMES for part in relative.parts):
        continue
    if relative.name.endswith(EXCLUDED_SUFFIXES):
        continue
    if path.is_symlink():
        fail(f"symlink is forbidden in a published tree: {relative.as_posix()}")
    if path.is_dir():
        continue
    observed.add(relative.as_posix())

problems: list[str] = []
for path in sorted(observed - set(expected)):
    problems.append(f"{path}: present on disk but absent from SYNC-MANIFEST.json")
for path in sorted(set(expected) - observed):
    problems.append(f"{path}: recorded in SYNC-MANIFEST.json but missing on disk")

for path in sorted(set(expected) & observed):
    entry = expected[path]
    target = repo_dir / path
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != entry.get("sha256"):
        problems.append(f"{path}: sha256 differs from SYNC-MANIFEST.json")
        continue
    raw_mode = stat.S_IMODE(target.lstat().st_mode)
    mode = 0o755 if raw_mode & 0o111 else 0o644
    if f"0o{mode:03o}" != entry.get("mode"):
        problems.append(
            f"{path}: mode 0o{mode:03o} differs from SYNC-MANIFEST.json {entry.get('mode')}"
        )
    elif target.stat().st_size != entry.get("size"):
        problems.append(f"{path}: size differs from SYNC-MANIFEST.json")

if problems:
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    fail(f"{len(problems)} published file(s) drifted from SYNC-MANIFEST.json")

print(
    f"Verified {len(expected)} published file(s) under {prefix} "
    f"against SYNC-MANIFEST.json (harness commit "
    f"{manifest.get('harness_source_commit', 'unknown')[:12]})."
)
PY
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
source_dir() { printf '%s/skills/%s/%s' "${REPO_DIR}" "${PLATFORM}" "$1"; }

refuse_existing() {
  local skills_dir="$1" skill
  for skill in "${SKILLS[@]}"; do
    if [[ -e "${skills_dir}/${skill}" ]]; then
      echo "Refusing to overwrite existing ${skills_dir}/${skill}." >&2
      echo "Move or remove that directory, then run this installer again." >&2
      exit 1
    fi
  done
}

backup_existing() {
  local skills_dir="$1" skill backup
  for skill in "${SKILLS[@]}"; do
    if [[ -e "${skills_dir}/${skill}" ]]; then
      backup="${skills_dir}/${skill}.pre-harness-${STAMP}"
      mv "${skills_dir}/${skill}" "${backup}"
      echo "Backed up existing ${skill} to ${backup}."
    fi
  done
}

copy_skills() {
  local skills_dir="$1" skill
  mkdir -p "${skills_dir}"
  for skill in "${SKILLS[@]}"; do
    cp -R "$(source_dir "${skill}")" "${skills_dir}/${skill}"
    echo "Installed ${skill}."
  done
}

plan_line() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------------------
# codex — enforced grade; behavior preserved from the single-platform installer
# ---------------------------------------------------------------------------
install_codex() {
  local codex_home="${CODEX_HOME:-${HOME}/.codex}"
  local skills_dir="${codex_home}/skills"
  local catalog="${CODEX_MODEL_CATALOG:-${codex_home}/models_cache.json}"

  if [[ "${MODE}" == "dry-run" ]]; then
    echo "Plan (codex):"
    plan_line "requires model catalog ${catalog}"
    plan_line "refuses to overwrite an existing ${skills_dir}/<skill>"
    plan_line "source ${REPO_DIR}/skills/codex/{${SKILLS[0]},${SKILLS[1]}}"
    plan_line "target ${skills_dir}/{${SKILLS[0]},${SKILLS[1]}}"
    plan_line "then: install_agent_profiles.py, router_lab.py refresh --stage-new, sync-agents, status"
    plan_line "then: test_workflow_plan.py, test_workflow_dispatch.py, test_agent_governance.py, test_learning_loop.py"
    return 0
  fi

  if [[ ! -f "${catalog}" ]]; then
    echo "Codex's model catalog was not found at ${catalog}." >&2
    echo "Open Codex once so it can populate the catalog, then rerun this installer." >&2
    exit 1
  fi
  refuse_existing "${skills_dir}"

  python3 "$(source_dir adaptive-model-router)/scripts/test_router_lab.py"

  copy_skills "${skills_dir}"

  python3 "${skills_dir}/adaptive-model-router/scripts/install_agent_profiles.py"
  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" refresh --stage-new
  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" sync-agents
  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" status
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_workflow_plan.py"
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_workflow_dispatch.py"
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_agent_governance.py"
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_learning_loop.py"

  echo "Installation complete. Start a new Codex turn to use the skills."
}

# ---------------------------------------------------------------------------
# claude — declared grade
# ---------------------------------------------------------------------------
install_claude() {
  local claude_home="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"
  local skills_dir="${claude_home}/skills"
  local model_source workflow_source
  model_source="$(source_dir adaptive-model-router)"
  workflow_source="$(source_dir adaptive-workflow-router)"

  if [[ "${MODE}" == "dry-run" ]]; then
    echo "Plan (claude):"
    plan_line "runs from the repo: test_router_lab.py"
    plan_line "backs up any existing ${skills_dir}/<skill> to <skill>.pre-harness-${STAMP}"
    plan_line "source ${REPO_DIR}/skills/claude/{${SKILLS[0]},${SKILLS[1]}}"
    plan_line "target ${skills_dir}/{${SKILLS[0]},${SKILLS[1]}}"
    plan_line "then: install_agent_profiles.py -> ${claude_home}/agents/{fast-operator,standard-worker,high-solver,ultra-planner}.md"
    plan_line "then: router_lab.py refresh --stage-new, sync-agents, status"
    plan_line "then: test_workflow_plan.py, test_workflow_dispatch.py, test_agent_governance.py, test_learning_loop.py"
    plan_line "then: workflow_dispatch.py check"
    return 0
  fi

  python3 "${model_source}/scripts/test_router_lab.py"

  mkdir -p "${skills_dir}"
  backup_existing "${skills_dir}"
  copy_skills "${skills_dir}"

  python3 "${skills_dir}/adaptive-model-router/scripts/install_agent_profiles.py"
  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" refresh --stage-new
  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" sync-agents
  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" status
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_workflow_plan.py"
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_workflow_dispatch.py"
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_agent_governance.py"
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_learning_loop.py"
  python3 "${skills_dir}/adaptive-workflow-router/scripts/workflow_dispatch.py" check

  echo "Installation complete. Dispatch evidence is DECLARED grade: receipts are"
  echo "first-party observed CLI output, so scored model experiments stay blocked."
  echo "Start a new Claude Code session to use the skills."
}

# ---------------------------------------------------------------------------
# cursor — declared-weak / experimental grade, plain copy, no transaction
# ---------------------------------------------------------------------------
install_cursor() {
  local cursor_home="${CURSOR_HOME:-${HOME}/.cursor}"
  local skills_dir="${cursor_home}/skills"
  local state_dir="${ADAPTIVE_MODEL_ROUTER_HOME:-${cursor_home}/adaptive-model-router}"
  local stub="${state_dir}/active-policy.json"
  local model_source workflow_source
  model_source="$(source_dir adaptive-model-router)"
  workflow_source="$(source_dir adaptive-workflow-router)"

  if [[ "${MODE}" == "dry-run" ]]; then
    echo "Plan (cursor, EXPERIMENTAL):"
    plan_line "runs from the repo: test_router_lab.py, test_workflow_plan.py"
    plan_line "backs up any existing ${skills_dir}/<skill> to <skill>.pre-harness-${STAMP}"
    plan_line "backs up a non-harness ${stub} to active-policy.json.pre-harness-<date>"
    plan_line "source ${REPO_DIR}/skills/cursor/{${SKILLS[0]},${SKILLS[1]}}"
    plan_line "target ${skills_dir}/{${SKILLS[0]},${SKILLS[1]}} (plain copy, no transaction)"
    plan_line "then: router_lab.py refresh, status  (no agent profiles: ~/.cursor/agents is unverified)"
    plan_line "then: test_workflow_plan.py from the installed tree"
    return 0
  fi

  python3 "${model_source}/scripts/test_router_lab.py"
  python3 "${workflow_source}/scripts/test_workflow_plan.py"

  # Supersede the hand-written pre-harness policy stub, preserving it.
  if [[ -f "${stub}" ]]; then
    python3 - "${stub}" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path

stub = Path(sys.argv[1])
try:
    existing = json.loads(stub.read_text(encoding="utf-8"))
except (json.JSONDecodeError, OSError):
    existing = {}
if isinstance(existing, dict) and (
    existing.get("source") == "adaptive-model-router" or "evidence_grade" in existing
):
    print(f"Existing {stub} is already a harness policy; leaving it in place.")
    raise SystemExit(0)
backup = stub.with_name(f"active-policy.json.pre-harness-{dt.date.today().isoformat()}")
if backup.exists():
    print(f"Pre-harness policy backup already exists at {backup}; leaving it in place.")
    raise SystemExit(0)
stub.rename(backup)
print(f"Backed up the pre-harness policy stub to {backup}.")
PY
  fi

  mkdir -p "${skills_dir}"
  backup_existing "${skills_dir}"
  copy_skills "${skills_dir}"

  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" refresh
  python3 "${skills_dir}/adaptive-model-router/scripts/router_lab.py" status
  python3 "${skills_dir}/adaptive-workflow-router/scripts/test_workflow_plan.py"

  echo "Installation complete (EXPERIMENTAL, declared-weak evidence)."
  echo "Cursor gets a plain copy with no install transaction, no scripted dispatcher,"
  echo "no receipts, and no governance ledger; scored experiments are blocked."
  echo "Dispatch is the documented contract in the skill: in-session Task with an"
  echo "explicit model, or headless 'cursor-agent -p --model <slug>'."
}

# ---------------------------------------------------------------------------
main() {
  verify_published_tree "${PLATFORM}"
  if [[ "${MODE}" == "verify" ]]; then
    return 0
  fi
  case "${PLATFORM}" in
    codex) install_codex ;;
    claude) install_claude ;;
    cursor) install_cursor ;;
  esac
}

main
