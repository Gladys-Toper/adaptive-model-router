#!/usr/bin/env python3
"""Execute one route-bound workflow phase against the headless Claude Code CLI.

Platform note. The Codex dispatcher drives the pinned OpenAI Codex App Server
over JSON-RPC and can therefore bind server-issued turn metadata as *enforced*
evidence. Claude Code exposes no such protocol. The strongest first-party
evidence available here is the observed ``claude -p --output-format stream-json``
transcript this process itself spawned and retained. Every receipt this module
writes is therefore stamped ``evidence_grade: "declared"`` with evidence source
``claude-code-headless-stream``, and scored model-lane experiments stay
``BLOCKED_MODEL_ENFORCEMENT`` until an external attestation verifier is
configured. Nothing in this file may claim a stronger grade.

Two capabilities the Codex dispatcher has are structurally absent, not omitted:

* ``resume``/``smoke``. The CLI has ``--resume``/``--session-id`` but no analog
  of ``turn/steer`` + ``turn/interrupt`` against a live turn, so the explicit
  checkpoint-restart protocol cannot be honored. An interrupted phase is
  ``ABORTED_NO_RESUMABLE_CHECKPOINT``.
* Headless T4. ``claude --effort`` accepts only ``low|medium|high``. A T4
  (``fable``/``max``) phase fails closed as ``T4_HEADLESS_UNSUPPORTED`` and is
  dispatched in-session through the Agent/Workflow tool contract in SKILL.md.
  It is never silently downgraded to ``high``.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_governance as governance  # noqa: E402
import failure_pivot_contract as pivots  # noqa: E402
import token_cap_contract as token_caps  # noqa: E402
import workflow_plan as planner  # noqa: E402


SKILL_DIR = Path(__file__).resolve().parent.parent
PLANNER = SKILL_DIR / "scripts" / "workflow_plan.py"
WORKFLOW_CATALOG = SKILL_DIR / "assets" / "workflows.json"
EXECUTION_BUDGET_POLICY = SKILL_DIR / "assets" / "execution-budget-policy.json"
DETERMINISTIC_GRADER = SKILL_DIR / "scripts" / "deterministic_quality_grader.py"

CLAUDE_HOME = (
    Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser().resolve()
)
MODEL_SKILL = CLAUDE_HOME / "skills" / "adaptive-model-router"
ROUTER = MODEL_SKILL / "scripts" / "router_lab.py"
SIBLING_ROUTER = (
    SKILL_DIR.parent / "adaptive-model-router" / "scripts" / "router_lab.py"
)
POLICY = CLAUDE_HOME / "adaptive-model-router" / "active-policy.json"
AGENTS = CLAUDE_HOME / "agents"
STATE_HOME = CLAUDE_HOME / "adaptive-workflow-router"
RUNS = STATE_HOME / "runs"
EXECUTION_REGISTRY = STATE_HOME / "execution-registry.jsonl"
EXECUTION_REGISTRY_LOCK = EXECUTION_REGISTRY.with_suffix(".jsonl.lock")
EXECUTION_REGISTRY_JOURNAL = EXECUTION_REGISTRY.with_suffix(".jsonl-journal.json")
# Installation is global infrastructure even when an individual workflow runs
# under an isolated CLAUDE_CONFIG_DIR.  Keep this path pinned to the real home
# so a caller cannot bypass admission by overriding the environment.
INSTALL_JOURNAL = (
    Path.home() / ".claude" / "adaptive-workflow-router" / "install-journal.json"
)
INSTALL_VERIFY_TOKEN_ENV = "ADAPTIVE_WORKFLOW_INSTALL_VERIFY_TOKEN"

CLAUDE_BINARY = os.environ.get("ADAPTIVE_WORKFLOW_CLAUDE_BINARY", "claude")
EVIDENCE_SOURCE_STREAM = "claude-code-headless-stream"
EVIDENCE_SOURCE_METADATA = "claude-code-headless-metadata"
EVIDENCE_GRADE = "declared"
RUNTIME_LABEL = "claude-code-headless-cli"

MODEL_TIERS = {"T1", "T2", "T3", "T4"}
ADAPTIVE_CONTROL_RETURN_NAME = pivots.CONTROL_RETURN_NAME
ADAPTIVE_PIVOT_NAME = pivots.PIVOT_NAME
HEADLESS_EFFORTS = ("low", "medium", "high")
UNVERSIONED_SOURCE_COMMIT = "0" * 40
DISPATCH_TIMEOUT_RETURNCODE = -9001  # sentinel: wall-time cap expired before process exit
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
ADMISSION_COMMANDS = {
    "run",
    "open-tree",
    "close-tree",
    "skill-read",
    "register-quality",
    "check",
}
SANDBOX_PERMISSION_MODE = {
    "read-only": "plan",
    "workspace-write": "acceptEdits",
}
READ_ONLY_DISALLOWED_TOOLS = ("Write", "Edit", "NotebookEdit", "Bash")
TOOL_MODE_NONE_DISALLOWED_TOOLS = (
    "Write",
    "Edit",
    "NotebookEdit",
    "Bash",
    "Task",
    "WebFetch",
    "WebSearch",
)
# Egress tools are the model's own network lane, so a packet that withholds
# network access must withhold them too.  Applied to executing (workspace-write)
# lanes; read-only lanes keep exactly the posture they already had.
NETWORK_DENIED_TOOLS = ("WebFetch", "WebSearch")
# Bounded in-worktree verification vocabulary.
#
# ``acceptEdits`` auto-approves file edits but *not* ``Bash``: a mutating phase
# under it produces "This command requires approval" for every test command and
# print mode has no channel to answer, so the worker could implement but never
# verify its own change (harness issue #24 item 2).  The two ways to grant Bash
# headlessly are measured, not assumed (``claude -p`` probes, CLI 2026-08):
#
# * ``--permission-mode dontAsk`` alone does *not* unblock it - Bash comes back
#   "denied because Claude Code is running in don't ask mode".  It needs the
#   same explicit allow rules, so it buys nothing here and gives up the edit
#   auto-approval the lane exists for.
# * ``--permission-mode acceptEdits`` plus ``--allowedTools`` prefix rules does
#   unblock exactly the enumerated commands, and nothing else.
#
# So the posture is acceptEdits + this frozen allowlist.  Containment rests on
# three properties: every entry names a build/test/lint runner rather than a
# shell (no bare ``Bash``, no ``Bash(python3:*)`` - the CLI treats inline-code
# forms such as ``python3 -c ...`` as unmatched and still refuses them, which is
# the behavior we want); nothing here installs, publishes, escalates, or mutates
# version control; and the list is source, not a caller input, so a dispatch
# packet can never widen execution authority - only a reviewed change to these
# bytes, which the frozen release binding hashes, can.  Anything outside the
# vocabulary still fails closed and shows up in the retained stream.
WORKSPACE_VERIFICATION_ALLOWED_TOOLS = (
    "Bash(cargo build:*)",
    "Bash(cargo check:*)",
    "Bash(cargo clippy:*)",
    "Bash(cargo test:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
    "Bash(go build:*)",
    "Bash(go test:*)",
    "Bash(go vet:*)",
    "Bash(just check:*)",
    "Bash(just lint:*)",
    "Bash(just test:*)",
    "Bash(make check:*)",
    "Bash(make lint:*)",
    "Bash(make test:*)",
    "Bash(make typecheck:*)",
    "Bash(mypy:*)",
    "Bash(npm run build:*)",
    "Bash(npm run lint:*)",
    "Bash(npm run test:*)",
    "Bash(npm run typecheck:*)",
    "Bash(npm test:*)",
    "Bash(npx eslint:*)",
    "Bash(npx jest:*)",
    "Bash(npx prettier:*)",
    "Bash(npx tsc:*)",
    "Bash(npx vitest:*)",
    "Bash(pytest:*)",
    "Bash(python -m compileall:*)",
    "Bash(python -m pytest:*)",
    "Bash(python -m unittest:*)",
    "Bash(python3 -m compileall:*)",
    "Bash(python3 -m pytest:*)",
    "Bash(python3 -m unittest:*)",
    "Bash(ruff:*)",
)

canonical_json = planner.canonical_json
content_hash = planner.content_hash


class DispatchError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _control_return_evidence(root: Path) -> list[dict[str, str]]:
    """Reference immutable terminal artifacts without making them portable claims."""
    references: list[dict[str, str]] = []
    for name in (
        "input-manifest.json",
        "prompt.txt",
        "execution-metadata.json",
        "aborted.json",
        "execution-receipt.json",
    ):
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        references.append({"path": str(resolved), "sha256": sha256_bytes(resolved.read_bytes())})
    for path in sorted(root.glob("resume-checkpoint.*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        references.append({"path": str(resolved), "sha256": sha256_bytes(resolved.read_bytes())})
    return references


def _tool_visible_snapshot(cwd: Path) -> str:
    """Hash the regular repository files a read-only worker could observe.

    Claude's headless adapter deliberately does not execute T4, but its typed
    control returns must still use the canonical closed snapshot shape.  This
    is a deterministic local observation; it never invokes a model or grants
    a read/write capability.
    """
    root = cwd.expanduser().resolve(strict=True)
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            text=False, capture_output=True, check=False, timeout=20,
        )
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--binary", "HEAD"],
            text=False, capture_output=True, check=False, timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DispatchError("repository tool-visible snapshot cannot be sealed") from error
    if listed.returncode != 0 or diff.returncode != 0:
        raise DispatchError("repository tool-visible snapshot cannot be sealed")
    listed_bytes = listed.stdout if isinstance(listed.stdout, bytes) else (listed.stdout or "").encode("utf-8")
    diff_bytes = diff.stdout if isinstance(diff.stdout, bytes) else (diff.stdout or "").encode("utf-8")
    entries: list[dict[str, Any]] = []
    for raw in listed_bytes.split(b"\0"):
        if not raw:
            continue
        try:
            relative = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DispatchError("repository contains a non-UTF-8 tool-visible path") from error
        candidate = Path(relative)
        if not relative or candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
            raise DispatchError("repository tool-visible path is unsafe")
        absolute = root / candidate
        try:
            info = absolute.lstat()
        except OSError as error:
            raise DispatchError("repository changed while tool-visible scope was sealed") from error
        if not stat.S_ISREG(info.st_mode):
            raise DispatchError("repository tool-visible path is not a regular file")
        digest = hashlib.sha256()
        try:
            with absolute.open("rb", buffering=0) as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as error:
            raise DispatchError("repository tool-visible file cannot be read") from error
        entries.append({
            "path": candidate.as_posix(), "mode": info.st_mode,
            "kind": "file", "sha256": digest.hexdigest(),
        })
    return content_hash({
        "schema_version": 1,
        "git_diff_binary_sha256": sha256_bytes(diff_bytes),
        "entries": sorted(entries, key=lambda entry: entry["path"]),
    })


def worktree_snapshot(cwd: Path) -> dict[str, Any]:
    """Capture the canonical closed git/tool-visible terminal snapshot."""
    worktree = cwd.expanduser().resolve(strict=True)
    try:
        source_commit = source_commit_for(worktree)
        visible_snapshot = _tool_visible_snapshot(worktree)
        status = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            text=True, capture_output=True, check=False, timeout=10,
        )
        tracked_diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--binary"],
            text=True, capture_output=True, check=False, timeout=10,
        )
        if status.returncode != 0 or tracked_diff.returncode != 0:
            state, diff_sha = "unknown", None
        else:
            state = "dirty" if status.stdout.strip() else "clean"
            diff_sha = sha256_bytes(tracked_diff.stdout.encode("utf-8"))
    except (OSError, subprocess.SubprocessError, DispatchError):
        source_commit, state, diff_sha = UNVERSIONED_SOURCE_COMMIT, "unknown", None
        visible_snapshot = content_hash({"schema_version": 1, "unavailable": str(worktree)})
    return {
        "source_commit": source_commit,
        "worktree": str(worktree),
        "status": state,
        "tracked_diff_sha256": diff_sha,
        "tool_visible_snapshot_sha256": visible_snapshot,
    }


def build_adaptive_control_return(
    *,
    terminal_status: str,
    terminal_category: str | None,
    phase_key: str | None,
    root: Path,
    cwd: Path,
    resumable: bool = False,
    failed_tier: str | None = None,
    retry_escalation_count: int = 0,
    issues: list[str] | None = None,
    admission_worktree_evidence: dict[str, Any] | None = None,
    allowed_mutation_scope: str = "none",
) -> dict[str, Any]:
    """Build the v2 coordinator handoff; it never supplies execution authority."""
    worktree = cwd.expanduser().resolve(strict=True)
    final_snapshot = worktree_snapshot(worktree)
    admission_snapshot = admission_worktree_evidence or final_snapshot
    evidence = _control_return_evidence(root)
    try:
        return pivots.build_control_return(
            terminal_status=terminal_status,
            terminal_category=terminal_category,
            phase_key=phase_key,
            resumable=resumable,
            resume_checkpoint_present=any(
                "resume-checkpoint." in item["path"] for item in evidence
            ),
            evidence_references=evidence,
            retry_escalation_count=retry_escalation_count,
            issues=issues,
            failed_tier=failed_tier,
            worktree_evidence={
                "admission": admission_snapshot,
                "final": final_snapshot,
                "allowed_mutation_scope": allowed_mutation_scope,
            },
        )
    except pivots.FailurePivotContractError as error:
        raise DispatchError(str(error)) from error


def validate_adaptive_control_return(directive: dict[str, Any]) -> None:
    try:
        pivots.validate_control_return(directive)
    except pivots.FailurePivotContractError as error:
        raise DispatchError(str(error)) from error


def validate_control_return_evidence(directive: dict[str, Any]) -> None:
    """Re-read every v2 artifact before following a recovery handoff."""
    validate_adaptive_control_return(directive)
    if directive.get("schema_version") == 1:
        return
    for reference in directive["evidence_references"]:
        path = Path(reference["path"])
        if path.is_symlink() or not path.is_file():
            raise DispatchError("control-return evidence is missing or unsafe")
        if sha256_bytes(path.resolve(strict=True).read_bytes()) != reference["sha256"]:
            raise DispatchError("control-return evidence hash changed")
    final_snapshot = directive["worktree_evidence"]["final"]
    try:
        current_snapshot = worktree_snapshot(Path(final_snapshot["worktree"]))
    except (OSError, RuntimeError) as error:
        raise DispatchError("control-return worktree snapshot is unavailable") from error
    if current_snapshot != final_snapshot:
        raise DispatchError("control-return tool-visible worktree snapshot changed")


def pivot_from_receipt(
    directive: dict[str, Any], *, previous_pivot: dict[str, Any] | None = None
) -> dict[str, Any]:
    validate_control_return_evidence(directive)
    try:
        pivot = pivots.derive_pivot(directive, previous_pivot)
        pivots.validate_pivot(pivot)
        return pivot
    except pivots.FailurePivotContractError as error:
        raise DispatchError(str(error)) from error


def validate_pivot(pivot: dict[str, Any]) -> None:
    try:
        pivots.validate_pivot(pivot)
    except pivots.FailurePivotContractError as error:
        raise DispatchError(str(error)) from error


def read_bound_json(path: Path, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    unresolved = path.expanduser()
    if not unresolved.is_absolute():
        unresolved = (Path.cwd() / unresolved).absolute()
    if unresolved.is_symlink() or not unresolved.is_file():
        raise DispatchError(f"{label} is missing or unsafe: {unresolved}")
    resolved = unresolved.resolve(strict=True)
    raw = resolved.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DispatchError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise DispatchError(f"{label} must be a JSON object")
    return resolved, raw, value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_private_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# --------------------------------------------------------------------------
# Install-journal admission barrier
# --------------------------------------------------------------------------


def enforce_install_admission(
    command: str,
    *,
    journal_path: Path = INSTALL_JOURNAL,
    environ: dict[str, str] | None = None,
) -> None:
    """Block executable dispatch while a global install transaction is open.

    The installer may exercise the exact staged dispatcher during its private
    verification phase by presenting a random token whose SHA-256 is bound into
    the self-hashed install journal.  Ordinary callers never receive that token.
    A malformed, partial, symlinked, or stale journal therefore fails closed.
    """
    if command not in ADMISSION_COMMANDS:
        return
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    if journal_path.is_symlink():
        raise DispatchError("global harness installation journal is a symlink")
    try:
        info = journal_path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DispatchError("global harness installation journal is unsafe")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DispatchError(
            "global harness installation journal is unreadable"
        ) from error
    if not isinstance(journal, dict):
        raise DispatchError("global harness installation journal is malformed")
    supplied_self_hash = journal.get("self_sha256")
    bare = dict(journal)
    bare.pop("self_sha256", None)
    if supplied_self_hash != sha256_bytes(canonical_json(bare).encode("utf-8")):
        raise DispatchError("global harness installation journal checksum mismatch")
    expected_token_hash = journal.get("verification_token_sha256")
    supplied_token = (environ if environ is not None else os.environ).get(
        INSTALL_VERIFY_TOKEN_ENV
    )
    if (
        isinstance(expected_token_hash, str)
        and HEX_SHA256.fullmatch(expected_token_hash)
        and isinstance(supplied_token, str)
        and supplied_token
        and sha256_bytes(supplied_token.encode("utf-8")) == expected_token_hash
    ):
        return
    raise DispatchError(
        "a global harness installation is in progress; executable dispatch is closed"
    )


# --------------------------------------------------------------------------
# Hash-chained execution receipt registry
# --------------------------------------------------------------------------


def _decode_receipt_registry(value: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    previous = "0" * 64
    for sequence, line in enumerate(value.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise DispatchError("execution receipt registry is malformed") from error
        supplied = event.get("receipt_sha256") if isinstance(event, dict) else None
        if not isinstance(supplied, str) or not HEX_SHA256.fullmatch(supplied):
            raise DispatchError("execution receipt registry hash is invalid")
        bare = dict(event)
        bare.pop("receipt_sha256", None)
        if (
            content_hash(bare) != supplied
            or event.get("sequence") != sequence
            or event.get("previous_receipt_sha256") != previous
        ):
            raise DispatchError("execution receipt registry hash chain is invalid")
        previous = supplied
        events.append(event)
    return events


def _recover_receipt_registry_locked() -> None:
    if not EXECUTION_REGISTRY_JOURNAL.exists():
        return
    if EXECUTION_REGISTRY_JOURNAL.is_symlink():
        raise DispatchError("execution registry journal cannot be a symlink")
    _, _, journal = read_bound_json(
        EXECUTION_REGISTRY_JOURNAL, "execution registry journal"
    )
    supplied = journal.get("journal_sha256")
    bare = dict(journal)
    bare.pop("journal_sha256", None)
    if not isinstance(supplied, str) or content_hash(bare) != supplied:
        raise DispatchError("execution registry journal checksum mismatch")
    target = base64.b64decode(journal["target_base64"], validate=True)
    if sha256_bytes(target) != journal.get("after_sha256"):
        raise DispatchError("execution registry journal target checksum mismatch")
    current = EXECUTION_REGISTRY.read_bytes() if EXECUTION_REGISTRY.exists() else b""
    if sha256_bytes(current) == journal.get("before_sha256"):
        _atomic_private_bytes(EXECUTION_REGISTRY, target)
    elif sha256_bytes(current) != journal.get("after_sha256"):
        raise DispatchError(
            "execution registry differs from both journal transaction sides"
        )
    _decode_receipt_registry(target)
    EXECUTION_REGISTRY_JOURNAL.unlink()
    _fsync_directory(EXECUTION_REGISTRY_JOURNAL.parent)


def append_registry_receipt(
    receipt_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Crash-safely append first-party observed harness provenance."""
    if receipt_type not in {"execution", "quality"}:
        raise DispatchError("unsupported evidence receipt type")
    reserved = {
        "schema_version",
        "receipt_type",
        "receipt_id",
        "issued_at_epoch",
        "sequence",
        "previous_receipt_sha256",
        "receipt_sha256",
    }
    if set(payload) & reserved:
        raise DispatchError(
            "evidence receipt payload attempts to override registry authority fields"
        )
    EXECUTION_REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if EXECUTION_REGISTRY.parent.is_symlink():
        raise DispatchError("execution registry directory cannot be a symlink")
    os.chmod(EXECUTION_REGISTRY.parent, 0o700)
    with EXECUTION_REGISTRY_LOCK.open("a+", encoding="utf-8") as lock_handle:
        os.chmod(EXECUTION_REGISTRY_LOCK, 0o600)
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        _recover_receipt_registry_locked()
        if EXECUTION_REGISTRY.is_symlink():
            raise DispatchError("execution receipt registry cannot be a symlink")
        before = (
            EXECUTION_REGISTRY.read_bytes() if EXECUTION_REGISTRY.exists() else b""
        )
        existing = _decode_receipt_registry(before)
        receipt = {
            "schema_version": 1,
            "receipt_type": receipt_type,
            "receipt_id": uuid.uuid4().hex,
            "issued_at_epoch": time.time(),
            **payload,
            "sequence": len(existing) + 1,
            "previous_receipt_sha256": (
                existing[-1]["receipt_sha256"] if existing else "0" * 64
            ),
        }
        receipt["receipt_sha256"] = content_hash(receipt)
        after = before + canonical_json(receipt).encode("utf-8") + b"\n"
        journal = {
            "schema_version": 1,
            "before_sha256": sha256_bytes(before),
            "after_sha256": sha256_bytes(after),
            "target_base64": base64.b64encode(after).decode("ascii"),
            "receipt_id": receipt["receipt_id"],
        }
        journal["journal_sha256"] = content_hash(journal)
        _atomic_private_bytes(
            EXECUTION_REGISTRY_JOURNAL,
            (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        _atomic_private_bytes(EXECUTION_REGISTRY, after)
        EXECUTION_REGISTRY_JOURNAL.unlink()
        _fsync_directory(EXECUTION_REGISTRY_JOURNAL.parent)
    return receipt


def read_registry_receipts() -> list[dict[str, Any]]:
    if not EXECUTION_REGISTRY.exists():
        return []
    return _decode_receipt_registry(EXECUTION_REGISTRY.read_bytes())


# --------------------------------------------------------------------------
# Router health
# --------------------------------------------------------------------------


def router_path() -> Path:
    if ROUTER.is_file():
        return ROUTER
    if SIBLING_ROUTER.is_file():
        return SIBLING_ROUTER
    raise DispatchError("adaptive model router is not installed")


def router_status() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(router_path()), "status"],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise DispatchError(
            "adaptive model router status failed: "
            + (completed.stderr.strip() or "unknown error")
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DispatchError("adaptive model router status is unreadable") from error


def require_routing_admission(status: dict[str, Any]) -> None:
    if status.get("workflow_routing_ready") is not True or (
        status.get("routing_status") != "HEALTHY"
    ):
        raise DispatchError(
            "active routing is not ready: "
            + "; ".join(status.get("routing_issues") or ["unknown routing issue"])
        )


# --------------------------------------------------------------------------
# The Claude CLI client (the injectable seam)
# --------------------------------------------------------------------------


class ClaudeCLI:
    """Spawn one headless ``claude -p`` turn and retain its raw stream."""

    binary = CLAUDE_BINARY

    def __init__(self, *, binary: str | None = None) -> None:
        if binary:
            self.binary = binary

    def build_command(
        self,
        *,
        model: str,
        effort: str,
        max_budget_usd: float,
        permission_mode: str,
        disallowed_tools: tuple[str, ...],
        allowed_tools: tuple[str, ...] = (),
    ) -> list[str]:
        if effort not in HEADLESS_EFFORTS:
            raise DispatchError(
                "T4_HEADLESS_UNSUPPORTED: the claude CLI accepts only "
                "low|medium|high for --effort; dispatch this phase in-session"
            )
        command = [
            self.binary,
            "-p",
            "--model",
            model,
            "--effort",
            effort,
            "--output-format",
            "stream-json",
            # The CLI requires --verbose whenever --print emits stream-json;
            # without it the process exits before any event is produced.
            "--verbose",
            "--max-budget-usd",
            f"{max_budget_usd:.4f}",
            "--permission-mode",
            permission_mode,
        ]
        # `--allowedTools`/`--disallowedTools` are variadic in the CLI, so they
        # stay last and the prompt stays on stdin: a positional prompt after
        # either flag is swallowed as another tool pattern and the process exits
        # with "Input must be provided". Comma-joining is the documented form and
        # preserves patterns that contain spaces (`Bash(npm test:*)`).
        if allowed_tools:
            command += ["--allowedTools", ",".join(allowed_tools)]
        if disallowed_tools:
            command += ["--disallowedTools", ",".join(disallowed_tools)]
        return command

    def run(
        self,
        command: list[str],
        *,
        prompt: str,
        cwd: Path,
        timeout: int,
    ) -> tuple[int, str, str]:
        # The CLI refuses to start when it believes it is nested inside another
        # Claude Code session (CLAUDECODE guard). Headless dispatch children are
        # independent print-mode processes, so scrub the marker from their
        # environment; every other variable passes through unchanged.
        child_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=str(cwd),
                timeout=timeout,
                env=child_env,
            )
            return completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            # Normalize partial output: bytes → str, None → empty string.
            partial_stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            partial_stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            return DISPATCH_TIMEOUT_RETURNCODE, partial_stdout, partial_stderr


def parse_stream(raw: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the retained stream and return (events, terminal result event)."""
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise DispatchError("claude stream-json output is malformed") from error
        if not isinstance(event, dict):
            raise DispatchError("claude stream-json event must be an object")
        events.append(event)
    if not events:
        raise DispatchError("claude produced no stream events")
    terminal = events[-1]
    if terminal.get("type") != "result":
        raise DispatchError("claude stream did not end with a terminal result event")
    return events, terminal


def runtime_metadata(terminal: dict[str, Any]) -> dict[str, Any]:
    """Extract declared runtime metadata from the terminal result event."""
    usage = terminal.get("usage")
    metadata = {
        "evidence_source": EVIDENCE_SOURCE_METADATA,
        "evidence_grade": EVIDENCE_GRADE,
        "session_id": terminal.get("session_id"),
        "subtype": terminal.get("subtype"),
        "is_error": bool(terminal.get("is_error", False)),
        "duration_ms": terminal.get("duration_ms"),
        "duration_api_ms": terminal.get("duration_api_ms"),
        "num_turns": terminal.get("num_turns"),
        "total_cost_usd": terminal.get("total_cost_usd"),
        "usage": usage if isinstance(usage, dict) else {},
        "model_usage": (
            terminal.get("modelUsage")
            if isinstance(terminal.get("modelUsage"), dict)
            else {}
        ),
    }
    if not isinstance(metadata["session_id"], str) or not metadata["session_id"]:
        raise DispatchError("claude result event omitted the session id")
    return metadata


def observed_models(metadata: dict[str, Any]) -> list[str]:
    return sorted(str(slug) for slug in metadata.get("model_usage", {}))


def observed_cumulative_tokens(metadata: dict[str, Any]) -> dict[str, int]:
    """Return the terminal stream's cumulative input/output accounting.

    Claude's headless stream is not provider-signed evidence, but its terminal
    ``usage`` object is the only first-party observed cumulative accounting
    this adapter receives.  Do not substitute a guessed value (or just output
    tokens): an incomplete accounting record cannot establish that the fixed
    cap was respected.
    """
    usage = metadata.get("usage")
    if not isinstance(usage, dict):
        raise DispatchError("claude terminal result omitted cumulative usage")
    values: dict[str, int] = {}
    for field in ("input_tokens", "output_tokens"):
        value = usage.get(field)
        if type(value) is not int or value < 0:
            raise DispatchError(
                "claude terminal result has invalid cumulative usage " + field
            )
        values[field] = value
    values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    return values


# --------------------------------------------------------------------------
# Packet + plan binding
# --------------------------------------------------------------------------


def load_plan(path: Path) -> dict[str, Any]:
    _, _, plan = read_bound_json(path, "workflow plan")
    if plan.get("schema_version") != 1:
        raise DispatchError("workflow plan schema is unsupported")
    return plan


def select_phase(plan: dict[str, Any], phase_key: str) -> dict[str, Any]:
    for phase in plan.get("phases", []):
        if phase.get("phase_key") == phase_key:
            if phase.get("activation") != "active":
                raise DispatchError("planned phase is not active")
            return phase
    raise DispatchError(f"planned phase is absent: {phase_key}")


def verify_dispatch_packet(
    packet: dict[str, Any], plan: dict[str, Any], phase: dict[str, Any]
) -> None:
    supplied = packet.get("dispatch_packet_sha256")
    if not isinstance(supplied, str) or not HEX_SHA256.fullmatch(supplied):
        raise DispatchError("planner dispatch packet omitted its SHA-256 binding")
    identity = {
        key: value
        for key, value in packet.items()
        if key != "dispatch_packet_sha256"
    }
    if content_hash(identity) != supplied:
        raise DispatchError("planner dispatch packet self-hash is invalid")
    if packet.get("plan_id") != plan.get("plan_id"):
        raise DispatchError("planner dispatch packet is bound to a different plan")
    if packet.get("phase_key") != phase.get("phase_key"):
        raise DispatchError("planner dispatch packet is bound to a different phase")
    if packet.get("phase_contract_sha256") != content_hash(
        planner.phase_contract(phase)
    ):
        raise DispatchError("planned phase changed after the dispatch packet was bound")
    if packet.get("router_policy_id") != plan.get("router_policy_id"):
        raise DispatchError("planner dispatch packet router policy drifted")
    source_commit = packet.get("source_commit")
    if not isinstance(source_commit, str) or not source_commit:
        raise DispatchError("planner dispatch packet omitted its source commit")
    if source_commit == UNVERSIONED_SOURCE_COMMIT:
        raise DispatchError(
            "planned dispatch refuses an unversioned harness source commit"
        )
    if "resume_contract" in packet:
        raise DispatchError(
            "ABORTED_NO_RESUMABLE_CHECKPOINT: the Claude surface has no "
            "resumption lane"
        )


def verify_runtime_contract(
    packet: dict[str, Any], args: argparse.Namespace, prompt: str
) -> dict[str, Any]:
    contract = packet.get("runtime_contract")
    if not isinstance(contract, dict):
        raise DispatchError("planner dispatch packet omitted its runtime contract")
    if packet.get("prompt_sha256") != sha256_bytes(prompt.encode("utf-8")):
        raise DispatchError("dispatch prompt does not match the bound packet")
    drift = {
        "sandbox": (contract.get("sandbox"), args.sandbox),
        "tool_mode": (contract.get("tool_mode"), args.tool_mode),
        "network_access": (
            bool(contract.get("network_access")),
            bool(args.network_access),
        ),
        "mutation_authorized": (
            bool(contract.get("mutation_authorized")),
            bool(args.mutation_authorized),
        ),
        "wall_time_seconds": (
            contract.get("wall_time_seconds"),
            args.wall_time_seconds,
        ),
    }
    mismatched = sorted(key for key, (a, b) in drift.items() if a != b)
    if mismatched:
        raise DispatchError(
            "dispatch permissions drifted from the bound packet: "
            + ", ".join(mismatched)
        )
    return contract


def validate_bound_token_cap(
    packet: dict[str, Any], phase: dict[str, Any], args: argparse.Namespace
) -> int:
    """Require a self-hashed cap contract before Claude model invocation.

    The raw Claude stream does not expose a server-enforced token ceiling, so
    the adapter preserves the planner's fixed cap authority in the packet and
    validates the coupled evidence before it starts the CLI process.
    """
    runtime = packet.get("runtime_contract")
    if not isinstance(runtime, dict):
        raise DispatchError("planner dispatch packet omitted its runtime cap contract")
    required = (
        "declared_token_cap",
        "derived_token_cap",
        "effective_token_cap",
        "caller_requested_token_cap",
        "token_cap_contract_sha256",
        "token_cap_task_binding_sha256",
        "dispatch_packet_binding_sha256",
    )
    if any(field not in runtime for field in required):
        raise DispatchError("planner dispatch packet omitted a fixed token-cap binding")
    declared, derived, effective = (
        runtime["declared_token_cap"],
        runtime["derived_token_cap"],
        runtime["effective_token_cap"],
    )
    if (
        any(type(value) is not int or value < 1 for value in (declared, derived, effective))
        or effective > declared
        or effective > derived
        or runtime.get("requested_budget_limits", {}).get("token_cap") != effective
    ):
        raise DispatchError("planner dispatch packet has unsafe token-cap limits")
    if (
        not isinstance(runtime["token_cap_contract_sha256"], str)
        or not HEX_SHA256.fullmatch(runtime["token_cap_contract_sha256"])
        or not isinstance(runtime["token_cap_task_binding_sha256"], str)
        or not HEX_SHA256.fullmatch(runtime["token_cap_task_binding_sha256"])
        or not isinstance(runtime["dispatch_packet_binding_sha256"], str)
        or not HEX_SHA256.fullmatch(runtime["dispatch_packet_binding_sha256"])
    ):
        raise DispatchError("planner dispatch packet omitted a cap-contract binding")
    if args.token_cap is not None and args.token_cap != effective:
        raise DispatchError("--token-cap differs from the planner-bound effective cap")
    if args.token_cap_contract is None:
        raise DispatchError("planner-bound model execution requires --token-cap-contract")
    contract_path, contract_raw, contract = read_bound_json(
        args.token_cap_contract, "fixed token-cap contract"
    )
    if sha256_bytes(contract_raw) != runtime["token_cap_contract_sha256"]:
        raise DispatchError("provided fixed token-cap contract does not match planner packet")
    binding = token_caps.token_cap_binding(
        plan_id=packet["plan_id"],
        phase_key=phase["phase_key"],
        phase_contract_sha256=packet["phase_contract_sha256"],
        route_identity={
            "request": phase["route_request"],
            "resolution": phase["route_resolution"],
        },
        prompt_sha256=packet["prompt_sha256"],
        context_files=packet["context_files"],
        context_bundle=packet["context_bundle"],
        cwd=packet["cwd"],
        source_commit=packet["source_commit"],
        runtime_contract=runtime,
        dispatch_packet_binding_sha256=runtime["dispatch_packet_binding_sha256"],
    )
    binding_sha256 = token_caps.content_hash(binding)
    if binding_sha256 != runtime["token_cap_task_binding_sha256"]:
        raise DispatchError("planner dispatch packet fixed token-cap task binding changed")
    evidence_path, evidence_raw, evidence = read_bound_json(
        Path(contract["measured_safe_evidence_path"]), "fixed token-cap evidence"
    )
    if evidence_path == contract_path:
        raise DispatchError("fixed token-cap evidence is missing or does not bind this task")
    try:
        token_caps.validate_token_cap_contract(
            contract,
            task_binding=binding,
            runtime_contract=runtime,
            evidence=evidence,
            evidence_raw_sha256=sha256_bytes(evidence_raw),
        )
    except token_caps.TokenCapContractError as error:
        raise DispatchError(str(error)) from error
    return effective


def permission_flags(
    contract: dict[str, Any],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Spell the bound runtime contract as CLI permission flags.

    Returns ``(permission_mode, disallowed_tools, allowed_tools)``.  Every axis
    is derived from the packet and never widened past it: a ``none`` tool mode
    denies the whole mutating/agentic set and grants nothing, a read-only lane
    keeps its existing plan-mode posture untouched, and only a workspace-write
    lane that the packet *also* marks ``mutation_authorized`` receives the
    bounded verification vocabulary that lets it run its own tests in its own
    cwd (``WORKSPACE_VERIFICATION_ALLOWED_TOOLS``).  Withheld network access
    additionally revokes the model's egress tools on that executing lane.
    """
    sandbox = contract.get("sandbox")
    if sandbox not in SANDBOX_PERMISSION_MODE:
        raise DispatchError(f"unsupported sandbox: {sandbox}")
    permission_mode = SANDBOX_PERMISSION_MODE[sandbox]
    if contract.get("tool_mode") == "none":
        return permission_mode, TOOL_MODE_NONE_DISALLOWED_TOOLS, ()
    if sandbox == "read-only":
        return permission_mode, READ_ONLY_DISALLOWED_TOOLS, ()
    disallowed = () if contract.get("network_access") else NETWORK_DENIED_TOOLS
    allowed = (
        WORKSPACE_VERIFICATION_ALLOWED_TOOLS
        if contract.get("mutation_authorized")
        else ()
    )
    return permission_mode, disallowed, allowed


def budget_usd_for(resolution: dict[str, Any], contract: dict[str, Any]) -> float:
    """Derive a hard spend cap from the policy weight table and the wall clock."""
    _, _, policy = read_bound_json(
        EXECUTION_BUDGET_POLICY, "execution budget policy"
    )
    weights = policy.get("model_resource_weights", {})
    unknown = policy.get("unknown_model_resource_weights", {})
    record = weights.get(resolution.get("model"), unknown)
    cost_weight = float(record.get("cost_weight", 1.0))
    wall_time = int(contract.get("wall_time_seconds") or 60)
    return round(cost_weight * max(wall_time, 1) / 60.0, 4)


def _stream_event_count(raw: str) -> int:
    """Count parseable retained events without rejecting partial abort evidence."""
    count = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            count += 1
    return count


def persist_terminal_control_return(
    *,
    run_dir: Path,
    cwd: Path,
    receipt_payload: dict[str, Any],
    terminal_status: str,
    terminal_category: str | None,
    issues: list[str],
    phase_key: str,
    tier: str,
    admission_worktree_evidence: dict[str, Any],
    allowed_mutation_scope: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist terminal provenance before exposing either outcome to a caller.

    A failed Claude invocation has no resumable execution state.  Retaining the
    raw stream alone is insufficient: recovery needs a typed, hash-bound control
    return just as much as a successful phase does.  Write the failure marker
    before the registry receipt so the resulting control return can bind both
    immutable terminal artifacts without a self-reference.
    """
    if terminal_status != "COMPLETED":
        _atomic_private_bytes(
            run_dir / "aborted.json",
            (json.dumps({
                "status": terminal_status,
                "terminal_category": terminal_category,
                "issues": issues,
            }, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
    receipt = append_registry_receipt("execution", receipt_payload)
    _atomic_private_bytes(
        run_dir / "execution-receipt.json",
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    directive = build_adaptive_control_return(
        terminal_status=terminal_status,
        terminal_category=terminal_category,
        phase_key=phase_key,
        root=run_dir,
        cwd=cwd,
        failed_tier=tier,
        issues=issues,
        admission_worktree_evidence=admission_worktree_evidence,
        allowed_mutation_scope=allowed_mutation_scope,
    )
    _atomic_private_bytes(
        run_dir / "control-return.json",
        (json.dumps(directive, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return receipt, directive


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def command_check() -> dict[str, Any]:
    status = router_status()
    issues: list[str] = []
    if not WORKFLOW_CATALOG.is_file():
        issues.append("workflow catalog is missing")
    if not EXECUTION_BUDGET_POLICY.is_file():
        issues.append("execution budget policy is missing")
    if not DETERMINISTIC_GRADER.is_file():
        issues.append("deterministic quality grader is missing")
    try:
        receipts = read_registry_receipts()
    except DispatchError as error:
        issues.append(str(error))
        receipts = []
    routing_ready = status.get("workflow_routing_ready") is True
    if not routing_ready:
        issues.extend(
            f"router: {reason}" for reason in status.get("routing_issues") or []
        )
    return {
        "status": "HEALTHY" if routing_ready and not issues else "DEGRADED",
        "dispatch_surface": "claude-code-headless-cli",
        "evidence_grade": EVIDENCE_GRADE,
        "evidence_sources": [EVIDENCE_SOURCE_STREAM, EVIDENCE_SOURCE_METADATA],
        "headless_efforts": list(HEADLESS_EFFORTS),
        "headless_tiers": ["T1", "T2", "T3"],
        "in_session_tiers": ["T4"],
        "t4_execution_modes": {
            "t4_consult": "UNAVAILABLE_CLAUDE_HEADLESS_ENFORCEMENT",
            "t4_diagnose": "UNAVAILABLE_CLAUDE_HEADLESS_ENFORCEMENT",
        },
        "resumption": "ABORTED_NO_RESUMABLE_CHECKPOINT",
        "router_policy_id": status.get("policy_id"),
        "routing_status": status.get("routing_status"),
        "evaluation_status": status.get("evaluation_status"),
        "catalog_status": status.get("catalog_status"),
        "workflow_routing_ready": routing_ready,
        "receipt_count": len(receipts),
        "issues": issues,
    }


def command_run(args: argparse.Namespace, *, CLI: type[ClaudeCLI] = ClaudeCLI) -> dict[str, Any]:
    plan = load_plan(args.plan)
    phase = select_phase(plan, args.phase_key)
    resolution = phase.get("route_resolution", {})
    tier = resolution.get("tier")
    if resolution.get("mode") != "model" or tier not in MODEL_TIERS:
        raise DispatchError("only active T1-T4 model phases are dispatchable")
    _, _, packet = read_bound_json(args.dispatch_packet, "dispatch packet")
    verify_dispatch_packet(packet, plan, phase)
    prompt = args.prompt_file.expanduser().read_text(encoding="utf-8")
    contract = verify_runtime_contract(packet, args, prompt)
    token_cap = validate_bound_token_cap(packet, phase, args)

    effort = resolution.get("effort")
    if tier == "T4" or effort not in HEADLESS_EFFORTS:
        raise DispatchError(
            f"T4_HEADLESS_UNSUPPORTED: tier {tier} effort {effort!r} is not "
            "expressible through `claude --effort`; dispatch it in-session via "
            "the Agent/Workflow tool contract in SKILL.md"
        )

    status = router_status()
    require_routing_admission(status)
    if status.get("policy_id") != plan.get("router_policy_id"):
        raise DispatchError("active router policy drifted from the planned policy")

    permission_mode, disallowed, allowed = permission_flags(contract)
    budget = budget_usd_for(resolution, contract)
    client = CLI()
    command = client.build_command(
        model=resolution["model"],
        effort=effort,
        max_budget_usd=budget,
        permission_mode=permission_mode,
        disallowed_tools=disallowed,
        allowed_tools=allowed,
    )

    run_id = uuid.uuid4().hex
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cwd = Path(packet["cwd"]).expanduser()
    if not cwd.is_dir():
        raise DispatchError(f"dispatch cwd does not exist: {cwd}")
    admission_worktree_evidence = worktree_snapshot(cwd)
    allowed_mutation_scope = (
        "workspace-write" if contract["sandbox"] == "workspace-write" else "none"
    )
    _atomic_private_bytes(
        run_dir / "input-manifest.json",
        (json.dumps({
            "schema_version": 1,
            "plan_id": plan["plan_id"],
            "phase_key": phase["phase_key"],
            "dispatch_packet_sha256": packet["dispatch_packet_sha256"],
            "phase_contract_sha256": packet["phase_contract_sha256"],
            "prompt_sha256": packet["prompt_sha256"],
            "source_commit": packet["source_commit"],
        }, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    _atomic_private_bytes(run_dir / "prompt.txt", prompt.encode("utf-8"))

    started = time.time()
    returncode, stdout, stderr = client.run(
        command,
        prompt=prompt,
        cwd=cwd,
        timeout=int(contract["wall_time_seconds"]),
    )
    elapsed_ms = int((time.time() - started) * 1000)

    stream_path = run_dir / "claude-cli.0001.jsonl"
    stream_bytes = stdout.encode("utf-8")
    _atomic_private_bytes(stream_path, stream_bytes)
    stream_sha256 = sha256_bytes(stream_bytes)

    def terminal_payload(**extra: Any) -> dict[str, Any]:
        """The common observed record for success and every terminal abort."""
        return {
            "runtime": RUNTIME_LABEL,
            "evidence_source": EVIDENCE_SOURCE_STREAM,
            "evidence_grade": EVIDENCE_GRADE,
            "issued_at": utc_now(),
            "run_id": run_id,
            "plan_id": plan["plan_id"],
            "phase_key": phase["phase_key"],
            "workflow_id": resolution.get("workflow_id"),
            "workflow_version": resolution.get("workflow_version"),
            "phase_id": resolution.get("phase_id"),
            "router_policy_id": plan.get("router_policy_id"),
            "dispatch_packet_sha256": packet["dispatch_packet_sha256"],
            "phase_contract_sha256": packet["phase_contract_sha256"],
            "prompt_sha256": packet["prompt_sha256"],
            "source_commit": packet["source_commit"],
            "tier": tier,
            "requested_model": resolution["model"],
            "requested_effort": effort,
            "command": command,
            "permission_mode": permission_mode,
            "disallowed_tools": list(disallowed),
            "allowed_tools": list(allowed),
            "max_budget_usd": budget,
            "token_cap": token_cap,
            "sandbox": contract["sandbox"],
            "tool_mode": contract["tool_mode"],
            "network_access": bool(contract["network_access"]),
            "mutation_authorized": bool(contract["mutation_authorized"]),
            "stream_path": str(stream_path),
            "stream_sha256": stream_sha256,
            "stream_event_count": _stream_event_count(stdout),
            "elapsed_ms": elapsed_ms,
            "scored_evidence_status": "BLOCKED_MODEL_ENFORCEMENT",
            **extra,
        }

    def abort(
        *, category: str, issue: str, status: str = "ABORTED"
    ) -> None:
        receipt, directive = persist_terminal_control_return(
            run_dir=run_dir,
            cwd=cwd,
            receipt_payload=terminal_payload(
                status=status,
                terminal_category=category,
                terminal_issues=[issue],
                execution_identity="ABORTED_NO_RESUMABLE_CHECKPOINT",
            ),
            terminal_status="ABORTED",
            terminal_category=category,
            issues=[issue],
            phase_key=phase["phase_key"],
            tier=tier,
            admission_worktree_evidence=admission_worktree_evidence,
            allowed_mutation_scope=allowed_mutation_scope,
        )
        raise DispatchError(
            f"{issue}; terminal receipt {receipt['receipt_sha256'][:12]}; "
            f"typed control return {directive['directive_sha256'][:12]} at "
            f"{run_dir / 'control-return.json'}"
        )

    if returncode == DISPATCH_TIMEOUT_RETURNCODE:
        cap = int(contract["wall_time_seconds"])
        abort(
            category="BUDGET_STOP",
            status="ABORTED_WALL_TIME",
            issue=(
                f"claude headless dispatch exceeded its {cap}s wall-time cap; "
                f"partial stream retained at {stream_path}"
            ),
        )
    if returncode != 0:
        abort(
            category="TRANSIENT_FAILURE",
            issue="claude headless dispatch failed: " + (stderr.strip() or "unknown error"),
        )
    try:
        events, terminal = parse_stream(stdout)
        metadata = runtime_metadata(terminal)
        usage = observed_cumulative_tokens(metadata)
    except DispatchError as error:
        abort(category="MODEL_ENFORCEMENT_FAILURE", issue=str(error))
    if metadata["is_error"]:
        subtype = str(metadata.get("subtype") or "unknown")
        budget_like = any(token in subtype.lower() for token in ("max", "token", "context", "limit"))
        abort(
            category="BUDGET_STOP" if budget_like else "MODEL_ENFORCEMENT_FAILURE",
            issue=f"claude reported a terminal error result: {subtype}",
        )
    if usage["total_tokens"] > token_cap:
        abort(
            category="BUDGET_STOP",
            issue=(
                "claude observed cumulative usage exceeded effective token cap: "
                f"{usage['total_tokens']} > {token_cap}"
            ),
        )

    receipt, directive = persist_terminal_control_return(
        run_dir=run_dir,
        cwd=cwd,
        receipt_payload=terminal_payload(
            status="COMPLETED",
            terminal_category=None,
            terminal_issues=[],
            observed_models=observed_models(metadata),
            execution_identity=resolution.get("execution_identity"),
            runtime_metadata=metadata,
            observed_usage=usage,
        ),
        terminal_status="COMPLETED",
        terminal_category=None,
        issues=[],
        phase_key=phase["phase_key"],
        tier=tier,
        admission_worktree_evidence=admission_worktree_evidence,
        allowed_mutation_scope=allowed_mutation_scope,
    )
    return {
        "status": "COMPLETED",
        "run_id": run_id,
        "tier": tier,
        "model": resolution["model"],
        "effort": effort,
        "evidence_grade": EVIDENCE_GRADE,
        "evidence_source": EVIDENCE_SOURCE_STREAM,
        "stream_path": str(stream_path),
        "stream_sha256": stream_sha256,
        "session_id": metadata["session_id"],
        "total_cost_usd": metadata.get("total_cost_usd"),
        "dispatcher_receipt_sha256": receipt["receipt_sha256"],
        "receipt_sequence": receipt["sequence"],
        "observed_usage": usage,
        "control_return": directive,
        "control_return_path": str(run_dir / "control-return.json"),
        "scored_evidence_status": "BLOCKED_MODEL_ENFORCEMENT",
    }


def command_capacity_plan(args: argparse.Namespace) -> dict[str, Any]:
    """Derive a governed parallelism contract; capacity is never caller-supplied."""
    _, _, request = read_bound_json(args.request, "parallel capacity request")
    _, _, policy = read_bound_json(
        EXECUTION_BUDGET_POLICY, "execution budget policy"
    )
    for branch in request.get("branches", []):
        if not isinstance(branch, dict):
            raise DispatchError(
                "parallel capacity request contains a malformed branch"
            )
        expected = policy["model_resource_weights"].get(
            branch.get("model"), policy["unknown_model_resource_weights"]
        )
        if any(
            not isinstance(branch.get(field), (int, float))
            or isinstance(branch.get(field), bool)
            or float(branch[field]) != float(expected[field])
            for field in ("cost_weight", "quota_weight")
        ):
            raise DispatchError(
                "parallel capacity request model weights differ from active policy"
            )
    contract = governance.build_capacity_contract(request)
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise DispatchError("parallel capacity output must be a new safe file")
    output.write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return contract


def source_commit_for(cwd: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return UNVERSIONED_SOURCE_COMMIT
    return completed.stdout.strip()


def command_open_tree(args: argparse.Namespace) -> dict[str, Any]:
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise DispatchError("parallel tree cwd is missing")
    path = args.capacity_contract.expanduser().absolute()
    _, _, raw = read_bound_json(path, "parallel capacity contract")
    tree_id = raw.get("tree_id")
    if not isinstance(tree_id, str):
        raise DispatchError("parallel capacity contract omitted tree id")
    contract = governance.load_capacity_contract(
        path, tree_id=tree_id, source_commit=source_commit_for(cwd)
    )
    return governance.reserve_agent(
        tree_id=tree_id,
        role="coordinator",
        nested_capacity_tokens=contract["descendant_tokens"],
        lease_seconds=args.lease_seconds,
        capacity_contract=contract,
    )


def command_close_tree(args: argparse.Namespace) -> dict[str, Any]:
    return governance.complete_coordinator(
        args.lease_id, status=args.status, reason=args.reason
    )


def command_register_quality(args: argparse.Namespace) -> dict[str, Any]:
    _, raw, _ = read_bound_json(args.grade_file, "quality artifact")
    completed = subprocess.run(
        [sys.executable, str(DETERMINISTIC_GRADER)],
        input=raw.decode("utf-8"),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise DispatchError(
            "deterministic quality grader rejected the submission: "
            + (completed.stderr.strip() or "unknown error")
        )
    grade = json.loads(completed.stdout)
    receipt = append_registry_receipt(
        "quality",
        {
            "runtime": RUNTIME_LABEL,
            "evidence_source": EVIDENCE_SOURCE_STREAM,
            "evidence_grade": EVIDENCE_GRADE,
            "issued_at": utc_now(),
            "execution_receipt_sha256": args.execution_receipt_sha256,
            "grade": grade,
        },
    )
    return {
        "status": "QUALITY_REGISTERED",
        "receipt_sha256": receipt["receipt_sha256"],
        "grade": grade,
    }


def command_skill_read(args: argparse.Namespace) -> dict[str, Any]:
    root = (CLAUDE_HOME / "skills" / args.skill).resolve()
    target = (root / args.relative_path).resolve()
    if not str(target).startswith(str(root) + os.sep):
        raise DispatchError("skill read escapes the skill root")
    if target.is_symlink() or not target.is_file():
        raise DispatchError(f"skill file is missing or unsafe: {target}")
    raw = target.read_bytes()
    return {
        "status": "SKILL_READ",
        "skill": args.skill,
        "path": str(target),
        "sha256": sha256_bytes(raw),
        "content": raw.decode("utf-8"),
    }


def command_pivot_from_receipt(args: argparse.Namespace) -> dict[str, Any]:
    _, _, directive = read_bound_json(args.receipt, "control-return receipt")
    previous: dict[str, Any] | None = None
    if args.output is not None:
        output = args.output.expanduser().absolute()
        if output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise DispatchError("pivot output must be a new safe file")
    else:
        output = None
    pivot = pivot_from_receipt(directive, previous_pivot=previous)
    if output is not None:
        output.write_text(json.dumps(pivot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return pivot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch one route-bound workflow phase to the Claude CLI."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help="Report dispatch-surface health")

    run = commands.add_parser("run", help="Execute one active T1-T3 route")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--phase-key", required=True)
    run.add_argument("--dispatch-packet", type=Path, required=True)
    run.add_argument("--prompt-file", type=Path, required=True)
    run.add_argument(
        "--sandbox", choices=("read-only", "workspace-write"), default="read-only"
    )
    run.add_argument("--network-access", action="store_true")
    run.add_argument("--tool-mode", choices=("default", "none"), default="default")
    run.add_argument("--mutation-authorized", action="store_true")
    run.add_argument("--wall-time-seconds", type=int, required=True)
    run.add_argument("--token-cap", type=int)
    run.add_argument("--token-cap-contract", type=Path)

    pivot = commands.add_parser(
        "pivot-from-receipt",
        help="Derive the deterministic no-dispatch recovery action from a terminal receipt",
    )
    pivot.add_argument("--receipt", type=Path, required=True)
    pivot.add_argument("--output", type=Path)

    capacity = commands.add_parser(
        "capacity-plan", help="Derive a governed parallelism contract"
    )
    capacity.add_argument("--request", type=Path, required=True)
    capacity.add_argument("--output", type=Path, required=True)

    open_tree = commands.add_parser("open-tree", help="Open a governed agent tree")
    open_tree.add_argument("--capacity-contract", type=Path, required=True)
    open_tree.add_argument("--cwd", type=Path, required=True)
    open_tree.add_argument("--lease-seconds", type=int, default=900)

    close_tree = commands.add_parser("close-tree", help="Close a governed agent tree")
    close_tree.add_argument("--lease-id", required=True)
    close_tree.add_argument(
        "--status", choices=("COMPLETED", "ABORTED", "BLOCKED"), required=True
    )
    close_tree.add_argument("--reason", default="")

    quality = commands.add_parser(
        "register-quality", help="Register a deterministic quality receipt"
    )
    quality.add_argument("--grade-file", type=Path, required=True)
    quality.add_argument("--execution-receipt-sha256", required=True)

    skill = commands.add_parser("skill-read", help="Read one installed skill file")
    skill.add_argument("--skill", required=True)
    skill.add_argument("--relative-path", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        enforce_install_admission(args.command)
        if args.command == "check":
            result: dict[str, Any] = command_check()
        elif args.command == "run":
            result = command_run(args)
        elif args.command == "pivot-from-receipt":
            result = command_pivot_from_receipt(args)
        elif args.command == "capacity-plan":
            result = command_capacity_plan(args)
        elif args.command == "open-tree":
            result = command_open_tree(args)
        elif args.command == "close-tree":
            result = command_close_tree(args)
        elif args.command == "register-quality":
            result = command_register_quality(args)
        elif args.command == "skill-read":
            result = command_skill_read(args)
        else:  # pragma: no cover - argparse enforces the surface
            raise DispatchError(f"unsupported command: {args.command}")
    except (DispatchError, governance.GovernanceError) as error:
        print(f"workflow-dispatch error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
