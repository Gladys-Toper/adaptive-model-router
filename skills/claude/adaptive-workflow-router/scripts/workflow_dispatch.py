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
HEADLESS_EFFORTS = ("low", "medium", "high")
UNVERSIONED_SOURCE_COMMIT = "0" * 40
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

canonical_json = planner.canonical_json
content_hash = planner.content_hash


class DispatchError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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
            "--max-budget-usd",
            f"{max_budget_usd:.4f}",
            "--permission-mode",
            permission_mode,
        ]
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
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=str(cwd),
            timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr


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


def permission_flags(contract: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    sandbox = contract.get("sandbox")
    if sandbox not in SANDBOX_PERMISSION_MODE:
        raise DispatchError(f"unsupported sandbox: {sandbox}")
    permission_mode = SANDBOX_PERMISSION_MODE[sandbox]
    if contract.get("tool_mode") == "none":
        disallowed = TOOL_MODE_NONE_DISALLOWED_TOOLS
    elif sandbox == "read-only":
        disallowed = READ_ONLY_DISALLOWED_TOOLS
    else:
        disallowed = ()
    return permission_mode, disallowed


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

    permission_mode, disallowed = permission_flags(contract)
    budget = budget_usd_for(resolution, contract)
    client = CLI()
    command = client.build_command(
        model=resolution["model"],
        effort=effort,
        max_budget_usd=budget,
        permission_mode=permission_mode,
        disallowed_tools=disallowed,
    )

    run_id = uuid.uuid4().hex
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    cwd = Path(packet["cwd"]).expanduser()
    if not cwd.is_dir():
        raise DispatchError(f"dispatch cwd does not exist: {cwd}")

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

    if returncode != 0:
        raise DispatchError(
            "claude headless dispatch failed: " + (stderr.strip() or "unknown error")
        )
    events, terminal = parse_stream(stdout)
    metadata = runtime_metadata(terminal)
    if metadata["is_error"]:
        raise DispatchError(
            f"claude reported a terminal error result: {metadata.get('subtype')}"
        )

    receipt = append_registry_receipt(
        "execution",
        {
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
            "observed_models": observed_models(metadata),
            "execution_identity": resolution.get("execution_identity"),
            "command": command,
            "permission_mode": permission_mode,
            "disallowed_tools": list(disallowed),
            "max_budget_usd": budget,
            "sandbox": contract["sandbox"],
            "tool_mode": contract["tool_mode"],
            "network_access": bool(contract["network_access"]),
            "mutation_authorized": bool(contract["mutation_authorized"]),
            "stream_path": str(stream_path),
            "stream_sha256": stream_sha256,
            "stream_event_count": len(events),
            "elapsed_ms": elapsed_ms,
            "runtime_metadata": metadata,
            "scored_evidence_status": "BLOCKED_MODEL_ENFORCEMENT",
        },
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
        "--status", choices=("completed", "failed", "aborted"), default="completed"
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
