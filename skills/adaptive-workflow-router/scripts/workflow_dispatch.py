#!/usr/bin/env python3
"""Execute one route-bound workflow phase with verified Codex App Server metadata."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import math
import os
import queue
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path
from typing import Any, Callable

import agent_governance as governance


SKILL_DIR = Path(__file__).resolve().parent.parent
PLANNER = SKILL_DIR / "scripts" / "workflow_plan.py"
WORKFLOW_CATALOG = SKILL_DIR / "assets" / "workflows.json"
RESUMPTION_POLICY = SKILL_DIR / "assets" / "resumption-policy.json"
EXECUTION_BUDGET_POLICY = SKILL_DIR / "assets" / "execution-budget-policy.json"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve()
MODEL_SKILL = CODEX_HOME / "skills" / "adaptive-model-router"
ROUTER = MODEL_SKILL / "scripts" / "router_lab.py"
POLICY = CODEX_HOME / "adaptive-model-router" / "active-policy.json"
AGENTS = CODEX_HOME / "agents"
RUNS = CODEX_HOME / "adaptive-workflow-router" / "runs"
EXECUTION_REGISTRY = CODEX_HOME / "adaptive-workflow-router" / "execution-registry.jsonl"
EXECUTION_REGISTRY_LOCK = EXECUTION_REGISTRY.with_suffix(".jsonl.lock")
EXECUTION_REGISTRY_JOURNAL = EXECUTION_REGISTRY.with_suffix(".jsonl-journal.json")
# Installation is global infrastructure even when an individual workflow uses
# an isolated CODEX_HOME.  Keep this path identical to install_global.py so a
# caller cannot bypass admission by overriding CODEX_HOME.
INSTALL_JOURNAL = (
    Path.home() / ".codex" / "adaptive-workflow-router" / "install-journal.json"
)
INSTALL_VERIFY_TOKEN_ENV = "ADAPTIVE_WORKFLOW_INSTALL_VERIFY_TOKEN"
DETERMINISTIC_GRADER = SKILL_DIR / "scripts" / "deterministic_quality_grader.py"
MODEL_TIERS = {"T1", "T2", "T3", "T4"}
PLAN_IDENTITY_FIELDS = (
    "objective",
    "catalog_sha256",
    "route_contract_sha256",
    "selected_workflows",
    "composition_mode",
    "workflow_hashes",
    "router_policy_id",
    "overrides",
    "phases",
)
PLAN_RESOLUTION_FIELDS = (
    "policy_id",
    "mode",
    "tier",
    "agent",
    "provider",
    "service_tier",
    "model",
    "effort",
    "profile_file",
    "profile_sha256",
    "execution_identity",
    "runtime_evidence_required",
    "runtime_attestation_required",
    "workflow_id",
    "workflow_version",
    "phase_id",
    "route_facts",
    "web_required",
    "visual_required",
    "parent_gate_required",
    "external_mutation_authorized",
)
HEX_SHA256 = re.compile(r"[0-9a-f]{64}")
WORK_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
TOKEN_CAP_CONTRACT_NAME = "adaptive-workflow.fixed-token-cap"
BUDGET_INCREASE_CONTRACT_NAME = "adaptive-workflow.execution-budget-increase"
BUDGET_INCREASE_REVIEW_PROPOSAL_NAME = (
    "adaptive-workflow.execution-budget-increase-review"
)
CHECKPOINT_PREFIX = "ADAPTIVE_WORKFLOW_CHECKPOINT\n"
RESUME_AUTHORITY_NAME = "adaptive-workflow.resumption-authority"
RESUME_WORK_MANIFEST_NAME = "adaptive-workflow.resume-work-manifest"
RESUME_COMPLETION_MODES = {
    "uninterrupted",
    "explicit_checkpoint_restart",
    "genuine_resume",
}
CHECKPOINT_IDENTITY_FIELDS = {
    "schema_version",
    "contract_name",
    "contract_version",
    "checkpoint_id",
    "sequence",
    "previous_checkpoint_sha256",
    "created_at_unix_ms",
    "expires_at_unix_ms",
    "mode",
    "binding",
    "thread",
    "transcript_chain",
    "transcript_chain_sha256",
    "explicit_checkpoint",
    "usage",
    "elapsed_ms",
    "budgets",
    "interruption_reason",
}
NO_TOOLS_PASSIVE_ITEM_TYPES = {"userMessage", "reasoning", "agentMessage"}
RUNTIME_EVIDENCE_METHODS = {
    "thread/settings/updated",
    "turn/started",
    "item/started",
    "item/completed",
    "thread/tokenUsage/updated",
    "model/rerouted",
    "model/safetyBuffering/updated",
    "turn/completed",
}
VERIFIED_PLAN_CACHE: set[str] = set()


class DispatchError(RuntimeError):
    """A fail-closed workflow dispatch error."""


class DispatchUsageError(DispatchError):
    """A command-line contract error that must retain an abort artifact."""


class AppServerDeadline(DispatchError):
    """A bounded App Server read deadline elapsed without a protocol event."""


def source_commit_for(cwd: Path) -> str:
    """Bind receipts to the checked-out source without letting callers assert it."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        value = completed.stdout.strip().lower()
        if len(value) in {40, 64} and HEX_SHA256.fullmatch(value + "0" * (64 - len(value))):
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    # Isolated protocol tests do not necessarily have a Git worktree.  A live
    # caller can make this explicit, while the receipt still says it is unknown.
    return os.environ.get("ADAPTIVE_WORKFLOW_SOURCE_COMMIT", "0" * 40)


def _skill_requirements_from_packet(packet: dict[str, Any] | None, cwd: Path) -> dict[str, Any]:
    if packet is None:
        return {
            "required_skills": [],
            "skill_hashes": {},
            "source_commit": source_commit_for(cwd),
        }
    required = packet.get("required_skills", [])
    hashes = packet.get("skill_hashes", {})
    source_commit = packet.get("source_commit", source_commit_for(cwd))
    if (
        not isinstance(required, list)
        or not isinstance(hashes, dict)
        or not isinstance(source_commit, str)
        or len(source_commit) not in {40, 64}
    ):
        raise DispatchError("planner dispatch packet has invalid skill requirements")
    expected_hashes = {
        item.get("name"): item.get("skill_sha256")
        for item in required
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if hashes != expected_hashes:
        raise DispatchError("planner dispatch packet skill hashes do not match requirements")
    return {
        "required_skills": required,
        "skill_hashes": hashes,
        "source_commit": source_commit,
        **{
            key: packet[key]
            for key in (
                "required_skills_contract_path",
                "required_skills_contract_sha256",
            )
            if key in packet
        },
    }


def _files_changed(cwd: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain=v1"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if getattr(completed, "returncode", 0) != 0:
        return []
    names: set[str] = set()
    for line in completed.stdout.splitlines():
        if len(line) >= 4:
            names.add(line[3:].split(" -> ")[-1])
    return sorted(names)


def workflow_receipt_fields(
    args: argparse.Namespace, *, status: str, issues: list[str]
) -> dict[str, Any]:
    bound = getattr(args, "_skill_requirements", None)
    if not isinstance(bound, dict):
        bound = _skill_requirements_from_packet(None, args.cwd)
    phase = getattr(args, "_planned_phase", None)
    exit_gate = phase.get("exit_gate", []) if isinstance(phase, dict) else []
    return {
        "required_skills": bound["required_skills"],
        "skill_hashes": bound["skill_hashes"],
        "source_commit": bound["source_commit"],
        "exit_gate": exit_gate,
        "files_changed": _files_changed(args.cwd),
        "verification": {
            "dispatcher_acceptance": status == "COMPLETED",
            "runtime_identity_verified": status == "COMPLETED" and not issues,
            "issues": list(issues),
        },
    }


class DispatchArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DispatchUsageError(message)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def context_bundle_record(
    context_records: list[dict[str, Any]], *, mode: str
) -> dict[str, Any]:
    """Bind one ordered immutable context bundle instead of rediscovering context."""
    if mode not in {"precomputed-planner-packet", "direct-hashed"}:
        raise DispatchError("context bundle mode is invalid")
    identity = {
        "schema_version": 1,
        "mode": mode,
        "files": context_records,
        "total_bytes": sum(record["bytes"] for record in context_records),
    }
    return {**identity, "bundle_sha256": content_hash(identity)}


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        raise DispatchError("cannot calculate a budget percentile without samples")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def model_api_call_allowance(tool_mode: str, tool_cycle_cap: int) -> int:
    """Bound chargeable model calls independently from turns and tools."""
    if tool_mode == "none":
        return 1
    return tool_cycle_cap + 1


def load_execution_budget_policy() -> dict[str, Any]:
    policy = read_json(
        EXECUTION_BUDGET_POLICY.resolve(), "execution budget policy"
    )
    history = policy.get("accepted_history")
    weights = policy.get("model_resource_weights")
    unknown = policy.get("unknown_model_resource_weights")
    increase = policy.get("increase_contract")
    cold_start = policy.get("cold_start_accounting")
    if (
        policy.get("schema_version") != 1
        or policy.get("policy_name")
        != "adaptive-workflow.execution-budget-policy"
        or policy.get("policy_version") != 2
        or not isinstance(history, dict)
        or type(history.get("minimum_samples")) is not int
        or history["minimum_samples"] < 1
        or not isinstance(history.get("percentile"), (int, float))
        or not 0 < history["percentile"] <= 1
        or not isinstance(history.get("headroom"), (int, float))
        or history["headroom"] < 1
        or type(history.get("max_sample_age_seconds")) is not int
        or history["max_sample_age_seconds"] < 1
        or history.get("group_by")
        != [
            "workflow_id",
            "workflow_version",
            "phase_id",
            "model",
            "effort",
            "tool_mode",
        ]
        or not isinstance(weights, dict)
        or not isinstance(unknown, dict)
        or not isinstance(increase, dict)
        or cold_start
        != {
            "tool_enabled_model_api_call_allowance": "tool_cycle_cap_plus_one",
            "tool_mode_none_model_api_call_allowance": 1,
            "context_growth_horizon": 2,
            "work_allowance_scope": "phase_total",
        }
        or increase.get("contract_name") != BUDGET_INCREASE_CONTRACT_NAME
        or increase.get("contract_version") != 1
        or increase.get("review_workflow_id") != "review.audit"
    ):
        raise DispatchError("execution budget policy has an invalid schema")
    for record in [*weights.values(), unknown]:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("cost_weight"), (int, float))
            or not isinstance(record.get("quota_weight"), (int, float))
            or record["cost_weight"] <= 0
            or record["quota_weight"] <= 0
        ):
            raise DispatchError("execution budget model weights are invalid")
    return policy


def accepted_usage_samples(
    request: dict[str, Any],
    resolution: dict[str, Any],
    tool_mode: str,
    *,
    registry_path: Path | None = None,
    now_epoch: float | None = None,
) -> list[dict[str, Any]]:
    """Read only dispatcher-accepted, hash-chained usage for the exact route group."""
    policy = load_execution_budget_policy()
    path = registry_path or EXECUTION_REGISTRY
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        raise DispatchError("execution registry is missing or unsafe")
    receipts = _decode_receipt_registry(path.read_bytes())
    cutoff = (now_epoch or time.time()) - policy["accepted_history"][
        "max_sample_age_seconds"
    ]
    expected = {
        "workflow_id": request.get("workflow_id"),
        "workflow_version": request.get("workflow_version"),
        "phase_id": request.get("phase_id"),
        "model": resolution.get("model"),
        "effort": resolution.get("effort"),
        "tool_mode": tool_mode,
    }
    samples: list[dict[str, Any]] = []
    for receipt in receipts:
        usage = receipt.get("accepted_usage")
        if (
            receipt.get("receipt_type") != "execution"
            or receipt.get("status") != "COMPLETED"
            or not isinstance(receipt.get("issued_at_epoch"), (int, float))
            or receipt["issued_at_epoch"] < cutoff
            or not isinstance(usage, dict)
            or any(usage.get(field) != value for field, value in expected.items())
        ):
            continue
        turn_cycles = usage.get("turn_cycles", usage.get("model_cycles"))
        measured = {
            "total_tokens": usage.get("total_tokens"),
            "turn_cycles": turn_cycles,
            "tool_cycles": usage.get("tool_cycles"),
            "duration_ms": usage.get("duration_ms"),
        }
        if all(type(value) is int and value >= 0 for value in measured.values()):
            samples.append(measured)
    return samples


def apply_measured_success_envelope(
    base: dict[str, Any],
    request: dict[str, Any],
    resolution: dict[str, Any],
    tool_mode: str,
    *,
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Tighten cold-start limits from successful route-specific distributions."""
    policy = load_execution_budget_policy()
    samples = accepted_usage_samples(
        request, resolution, tool_mode, registry_path=registry_path
    )
    history = policy["accepted_history"]
    resource = policy["model_resource_weights"].get(
        resolution.get("model"), policy["unknown_model_resource_weights"]
    )
    weight = max(1.0, resource["cost_weight"], resource["quota_weight"])
    result = {
        **base,
        "accepted_history": {
            "sample_count": len(samples),
            "minimum_samples": history["minimum_samples"],
            "group": {
                "workflow_id": request.get("workflow_id"),
                "workflow_version": request.get("workflow_version"),
                "phase_id": request.get("phase_id"),
                "model": resolution.get("model"),
                "effort": resolution.get("effort"),
                "tool_mode": tool_mode,
            },
            "resource_weights": resource,
            "status": "cold-start",
        },
    }
    if len(samples) < history["minimum_samples"]:
        return result
    percentile = float(history["percentile"])
    # Higher-cost or scarcer models receive less discretionary headroom, but a
    # successful measured percentile is never cut below its observed value.
    weighted_headroom = 1.0 + (float(history["headroom"]) - 1.0) / weight
    observed = {
        field: _nearest_rank([sample[field] for sample in samples], percentile)
        for field in ("total_tokens", "turn_cycles", "tool_cycles", "duration_ms")
    }
    measured_token_cap = math.ceil(observed["total_tokens"] * weighted_headroom)
    measured_model_cap = max(
        1, math.ceil(observed["turn_cycles"] * weighted_headroom)
    )
    measured_tool_cap = (
        0
        if tool_mode == "none"
        else max(1, math.ceil(observed["tool_cycles"] * weighted_headroom))
    )
    measured_wall_cap = max(
        1, math.ceil(observed["duration_ms"] * weighted_headroom / 1000)
    )
    result.update(
        {
            "mode": "measured-success-envelope",
            "token_cap": measured_token_cap,
            "model_cycle_cap": measured_model_cap,
            "turn_cycle_cap": measured_model_cap,
            "tool_cycle_cap": measured_tool_cap,
            "model_api_call_allowance": model_api_call_allowance(
                tool_mode, measured_tool_cap
            ),
            "recommended_wall_time_seconds": measured_wall_cap,
            "accepted_history": {
                **result["accepted_history"],
                "status": "measured",
                "percentile": percentile,
                "headroom": history["headroom"],
                "weighted_headroom": weighted_headroom,
                "observed_percentile": observed,
            },
        }
    )
    return result


def read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise DispatchError(f"{label} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DispatchError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise DispatchError(f"{label} must be a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def allocate_run_directory(run_root: Path, canonical_name: str, *, mode: int) -> Path:
    """Atomically claim a new run directory without reusing an existing run."""
    run_root.mkdir(parents=True, exist_ok=True)
    suffix = 0
    while True:
        name = canonical_name if suffix == 0 else f"{canonical_name}-{suffix:04d}"
        candidate = run_root / name
        try:
            candidate.mkdir(mode=mode)
        except FileExistsError:
            suffix += 1
            continue
        return candidate


def _atomic_private_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
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


def _atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_private_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )


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
    journal = read_json(EXECUTION_REGISTRY_JOURNAL.resolve(), "execution registry journal")
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
        raise DispatchError("execution registry differs from both journal transaction sides")
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
        raise DispatchError("evidence receipt payload attempts to override registry authority fields")
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
        before = EXECUTION_REGISTRY.read_bytes() if EXECUTION_REGISTRY.exists() else b""
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
            (canonical_json(journal) + "\n").encode("utf-8"),
        )
        _atomic_private_bytes(EXECUTION_REGISTRY, after)
        EXECUTION_REGISTRY_JOURNAL.unlink()
        _fsync_directory(EXECUTION_REGISTRY_JOURNAL.parent)
        return receipt


def append_execution_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    return append_registry_receipt("execution", payload)


def append_quality_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    return append_registry_receipt("quality", payload)


def write_setup_failure_artifact(args: argparse.Namespace, error: Exception) -> Path | None:
    """Persist failures that occur before run_phase can create its normal artifact set."""
    output_dir = getattr(args, "output_dir", None)
    requested_root = (
        Path(output_dir).expanduser().resolve() if output_dir is not None else None
    )
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fallback_root = RUNS / f"{timestamp}-setup-failure-{uuid.uuid4().hex[:8]}"
    root: Path | None = None
    for candidate in (requested_root, fallback_root):
        if candidate is None:
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=False)
        except OSError:
            continue
        root = candidate
        break
    if root is None:
        try:
            root = Path(tempfile.mkdtemp(prefix="adaptive-workflow-setup-failure-"))
        except OSError:
            return None
    try:
        write_json(
            root / "aborted.json",
            {
                "schema_version": 1,
                "status": "ABORTED",
                "category": (
                    "USAGE_ERROR"
                    if isinstance(error, DispatchUsageError)
                    else "SETUP_FAILURE"
                ),
                "reason": str(error),
                "error_type": type(error).__name__,
                "phase_key": getattr(args, "phase_key", None),
                "requested_output_dir": (
                    str(requested_root) if requested_root is not None else None
                ),
            },
        )
    except OSError:
        return None
    return root


def failure_namespace_from_argv(argv: list[str]) -> argparse.Namespace:
    """Recover artifact-routing hints even when argparse rejects the invocation."""
    output_dir: Path | None = None
    phase_key: str | None = None
    for index, value in enumerate(argv):
        if value == "--output-dir" and index + 1 < len(argv):
            output_dir = Path(argv[index + 1])
        elif value.startswith("--output-dir="):
            output_dir = Path(value.split("=", 1)[1])
        elif value == "--phase-key" and index + 1 < len(argv):
            phase_key = argv[index + 1]
        elif value.startswith("--phase-key="):
            phase_key = value.split("=", 1)[1]
    return argparse.Namespace(output_dir=output_dir, phase_key=phase_key)


def run_json(command: list[str], *, input_value: dict[str, Any] | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        input=json.dumps(input_value) if input_value is not None else None,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DispatchError(f"command failed ({' '.join(command)}): {detail}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise DispatchError(f"command returned invalid JSON: {' '.join(command)}") from error
    if not isinstance(value, dict):
        raise DispatchError(f"command returned a non-object: {' '.join(command)}")
    return value


def router_status() -> dict[str, Any]:
    return run_json([sys.executable, str(ROUTER), "status"])


def resolve_phase(request: dict[str, Any]) -> dict[str, Any]:
    return run_json(
        [sys.executable, str(ROUTER), "resolve-phase", "--request", "-"],
        input_value=request,
    )


def validate_resolution(resolution: dict[str, Any]) -> None:
    if resolution.get("schema_version") != 1:
        raise DispatchError("route resolution has an unsupported schema")
    if resolution.get("mode") != "model" or resolution.get("tier") not in MODEL_TIERS:
        raise DispatchError("workflow_dispatch only executes T1-T4 model routes")
    for field in (
        "policy_id",
        "agent",
        "provider",
        "service_tier",
        "model",
        "effort",
        "profile_file",
        "profile_sha256",
    ):
        if not isinstance(resolution.get(field), str) or not resolution[field]:
            raise DispatchError(f"route resolution omitted {field}")
    if (
        resolution.get("execution_identity") != "REQUESTED_PENDING_SERVER_METADATA"
        or resolution.get("runtime_evidence_required") is not True
        or resolution.get("runtime_attestation_required") is not False
    ):
        raise DispatchError(
            "BLOCKED_MODEL_ENFORCEMENT: route does not require pending server metadata"
        )
    if resolution.get("external_mutation_authorized") is not False:
        raise DispatchError("route resolution attempted to confer mutation authority")


def safe_profile(policy: dict[str, Any], resolution: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    tier = resolution["tier"]
    assignment = policy.get("tiers", {}).get(tier)
    if not isinstance(assignment, dict):
        raise DispatchError(f"active policy omitted {tier}")
    expected = {
        "agent": assignment.get("agent"),
        "model": assignment.get("model"),
        "effort": assignment.get("effort"),
        "profile_file": assignment.get("profile_file"),
    }
    for field, value in expected.items():
        if resolution.get(field) != value:
            raise DispatchError(f"route {field} changed after active-policy resolution")
    filename = resolution["profile_file"]
    if Path(filename).name != filename:
        raise DispatchError("route profile filename is unsafe")
    profile_path = AGENTS / filename
    if profile_path.is_symlink() or not profile_path.is_file():
        raise DispatchError(f"route profile is missing or unsafe: {profile_path}")
    profile_bytes = profile_path.read_bytes()
    if sha256_bytes(profile_bytes) != resolution["profile_sha256"]:
        raise DispatchError("route profile changed after resolution")
    try:
        profile = tomllib.loads(profile_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise DispatchError(f"route profile is invalid: {error}") from error
    if (
        profile.get("name") != resolution["agent"]
        or profile.get("model") != resolution["model"]
        or profile.get("model_reasoning_effort") != resolution["effort"]
        or not isinstance(profile.get("developer_instructions"), str)
    ):
        raise DispatchError("route profile identity does not match the resolution")
    return profile_path, profile


def validate_router_health(status: dict[str, Any]) -> None:
    routing_status = status.get("routing_status")
    if (
        routing_status != "HEALTHY"
        or status.get("workflow_routing_ready") is not True
    ):
        raise DispatchError(
            "router active routing is not HEALTHY: "
            f"{routing_status or status.get('doctor')}"
        )


def validate_policy(
    policy: dict[str, Any], status: dict[str, Any], resolution: dict[str, Any]
) -> tuple[Path, str, dict[str, Any]]:
    validate_router_health(status)
    if status.get("execution_evidence_ready") is not True:
        raise DispatchError("BLOCKED_MODEL_ENFORCEMENT: execution evidence is not ready")
    if status.get("policy_id") != resolution["policy_id"] or policy.get(
        "policy_id"
    ) != resolution["policy_id"]:
        raise DispatchError("active policy changed after route resolution")
    constraints = policy.get("constraints", {})
    if constraints.get("execution_evidence_mode") != "server-metadata":
        raise DispatchError("BLOCKED_MODEL_ENFORCEMENT: server-metadata mode is not active")
    if constraints.get("allowed_provider") != resolution["provider"]:
        raise DispatchError("resolved provider is not allowed by active policy")
    allowed_service_tiers = constraints.get("allowed_service_tiers")
    default_service_tier = constraints.get("default_service_tier")
    if (
        not isinstance(allowed_service_tiers, list)
        or not allowed_service_tiers
        or any(not isinstance(value, str) or not value for value in allowed_service_tiers)
        or len(allowed_service_tiers) != len(set(allowed_service_tiers))
    ):
        raise DispatchError("active policy service-tier allowlist is invalid")
    if default_service_tier not in allowed_service_tiers:
        raise DispatchError("active policy default service tier is not allowed")
    if resolution["service_tier"] != default_service_tier:
        raise DispatchError("resolved service tier changed after active-policy resolution")
    runner_value = constraints.get("server_metadata_runner_path")
    runner_hash = constraints.get("server_metadata_runner_sha256")
    if not isinstance(runner_value, str) or not isinstance(runner_hash, str):
        raise DispatchError("BLOCKED_MODEL_ENFORCEMENT: pinned runner is not configured")
    runner = Path(runner_value).expanduser()
    if not runner.is_absolute() or runner.is_symlink() or not runner.is_file():
        raise DispatchError("BLOCKED_MODEL_ENFORCEMENT: pinned runner is missing or unsafe")
    if sha256_bytes(runner.read_bytes()) != runner_hash:
        raise DispatchError("BLOCKED_MODEL_ENFORCEMENT: pinned runner hash changed")
    _, profile = safe_profile(policy, resolution)
    return runner, runner_hash, profile


def validate_workflow_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != 1 or plan.get("planner_contract_version") != 1:
        raise DispatchError("workflow plan uses an unsupported schema")
    missing = [field for field in PLAN_IDENTITY_FIELDS if field not in plan]
    if missing:
        raise DispatchError(
            "workflow plan omitted identity fields: " + ", ".join(missing)
        )
    identity = {field: plan[field] for field in PLAN_IDENTITY_FIELDS}
    plan_id = plan.get("plan_id")
    if not isinstance(plan_id, str) or plan_id != content_hash(identity):
        raise DispatchError("workflow plan identity does not match plan_id")
    if plan.get("execution_identity") != "REQUESTED_ROUTES_PENDING_RUNTIME_EVIDENCE":
        raise DispatchError("workflow plan has a stale execution identity")
    if not isinstance(plan.get("router_policy_id"), str) or not plan[
        "router_policy_id"
    ]:
        raise DispatchError("workflow plan omitted router_policy_id")
    phases = plan.get("phases")
    if not isinstance(phases, list) or any(
        not isinstance(phase, dict) for phase in phases
    ):
        raise DispatchError("workflow plan phases must be a list of objects")


def validate_planned_phase(
    plan: dict[str, Any], phase_key: str
) -> dict[str, Any]:
    """Recheck planner semantics, not merely its self-hash."""
    validate_workflow_plan(plan)
    matches = [
        phase for phase in plan["phases"] if phase.get("phase_key") == phase_key
    ]
    if len(matches) != 1:
        raise DispatchError("plan phase key did not identify exactly one phase")
    phase = matches[0]
    if phase.get("execution") != "route" or phase.get("activation") != "active":
        raise DispatchError("selected plan phase is not an active routed phase")
    for field in ("phase_key", "workflow_id", "phase_id", "pattern"):
        if not isinstance(phase.get(field), str) or not phase[field]:
            raise DispatchError(f"selected plan phase omitted {field}")
    if phase["phase_key"] != f"{phase['workflow_id']}:{phase['phase_id']}":
        raise DispatchError("selected plan phase key does not match its identity")
    if type(phase.get("workflow_version")) is not int:
        raise DispatchError("selected plan phase omitted workflow_version")
    for field in ("produces", "exit_gate"):
        value = phase.get(field)
        if (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise DispatchError(f"selected plan phase has invalid {field}")
    request = phase.get("route_request")
    resolution = phase.get("route_resolution")
    if not isinstance(request, dict) or not isinstance(resolution, dict):
        raise DispatchError("selected plan phase omitted its route binding")
    for field in ("workflow_id", "workflow_version", "phase_id"):
        if request.get(field) != phase.get(field):
            raise DispatchError(f"selected plan route request mismatches {field}")
        if resolution.get(field) != request.get(field):
            raise DispatchError(f"selected plan route resolution mismatches {field}")
    validate_resolution(resolution)
    expected_route_facts = {
        field: request.get(field)
        for field in (
            "activity",
            "mutation",
            "scope",
            "ambiguity",
            "risk_level",
            "risk_scope",
            "risk_tags",
            "external_action",
        )
    }
    if resolution.get("route_facts") != expected_route_facts:
        raise DispatchError("selected plan route resolution changed its route facts")
    if resolution.get("web_required") is not request.get("current_info_required"):
        raise DispatchError("selected plan route resolution changed its web requirement")
    if resolution.get("visual_required") is not request.get("visual_required"):
        raise DispatchError(
            "selected plan route resolution changed its visual requirement"
        )
    if resolution.get("policy_id") != plan.get("router_policy_id"):
        raise DispatchError("selected plan route policy does not match the plan")
    return phase


def verify_planner_plan(path: Path, plan: dict[str, Any]) -> None:
    file_sha256 = sha256_bytes(path.read_bytes())
    if file_sha256 in VERIFIED_PLAN_CACHE:
        return
    report = run_json(
        [
            sys.executable,
            str(PLANNER),
            "--catalog",
            str(WORKFLOW_CATALOG),
            "verify-plan",
            "--plan",
            str(path),
            "--router",
            str(ROUTER),
        ]
    )
    if (
        report.get("status") != "VERIFIED"
        or report.get("plan_id") != plan.get("plan_id")
        or report.get("catalog_sha256") != plan.get("catalog_sha256")
        or report.get("router_policy_id") != plan.get("router_policy_id")
    ):
        raise DispatchError("workflow planner did not verify the plan authority")
    VERIFIED_PLAN_CACHE.add(file_sha256)


def load_route(
    args: argparse.Namespace, *, require_dispatch_packet: bool = True
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if args.plan:
        plan_path = args.plan.expanduser().absolute()
        plan = read_json(plan_path, "workflow plan")
        verify_planner_plan(plan_path, plan)
        phase = validate_planned_phase(plan, args.phase_key)
        request = phase.get("route_request")
        packet_path = getattr(args, "dispatch_packet", None)
        if packet_path is None and require_dispatch_packet:
            raise DispatchError(
                "--plan execution requires a planner-generated --dispatch-packet"
            )
        packet = (
            read_json(
                packet_path.expanduser().absolute(), "planner dispatch packet"
            )
            if packet_path is not None
            else None
        )
        return request, {
            "plan_path": str(plan_path.resolve(strict=True)),
            "plan_file_sha256": sha256_bytes(plan_path.read_bytes()),
            "plan_id": plan.get("plan_id"),
            "planner_contract_version": plan.get("planner_contract_version"),
            "plan_policy_id": plan.get("router_policy_id"),
            "phase_key": args.phase_key,
            "phase": phase,
            "planned_resolution": phase.get("route_resolution"),
            "dispatch_packet_path": (
                str(packet_path.expanduser().resolve())
                if packet_path is not None
                else None
            ),
            "dispatch_packet_file_sha256": (
                sha256_bytes(packet_path.expanduser().resolve().read_bytes())
                if packet_path is not None
                else None
            ),
            "dispatch_packet": packet,
        }
    if getattr(args, "dispatch_packet", None) is not None:
        raise DispatchError("--dispatch-packet is valid only with --plan")
    request = read_json(args.request.expanduser().absolute(), "phase request")
    return request, None


def phase_contract(phase: dict[str, Any]) -> dict[str, Any]:
    return {
        field: phase.get(field)
        for field in (
            "phase_key",
            "workflow_id",
            "workflow_version",
            "phase_id",
            "pattern",
            "produces",
            "exit_gate",
            "route_request",
            "route_resolution",
        )
    }


def expected_dispatch_packet(
    plan_binding: dict[str, Any],
    *,
    prompt_sha256: str,
    context_records: list[dict[str, Any]],
    cwd: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    resume_contract = build_expected_resume_contract(
        args, plan_binding.get("phase", {})
    )
    requirements = getattr(args, "_skill_requirements", None)
    if not isinstance(requirements, dict):
        requirements = _skill_requirements_from_packet(None, cwd)
    def optional_hash(value: Path | None) -> str | None:
        if value is None:
            return None
        unresolved = value.expanduser().absolute()
        if unresolved.is_symlink() or not unresolved.is_file():
            raise DispatchError("dispatch budget contract is missing or unsafe")
        return sha256_bytes(unresolved.resolve(strict=True).read_bytes())

    identity = {
        "schema_version": 2 if resume_contract is not None else 1,
        "planner_contract_version": plan_binding.get("planner_contract_version"),
        "plan_id": plan_binding.get("plan_id"),
        "router_policy_id": plan_binding.get("plan_policy_id"),
        "phase_key": plan_binding.get("phase_key"),
        "phase_contract_sha256": content_hash(
            phase_contract(plan_binding.get("phase", {}))
        ),
        "prompt_sha256": prompt_sha256,
        "context_files": context_records,
        "context_bundle": context_bundle_record(
            context_records, mode="precomputed-planner-packet"
        ),
        "cwd": str(cwd),
        "runtime_contract": {
            "sandbox": args.sandbox,
            "network_access": args.network_access,
            "tool_mode": args.tool_mode,
            "mutation_authorized": args.mutation_authorized,
            "wall_time_seconds": args.wall_time_seconds,
            "requested_budget_limits": {
                "token_cap": getattr(args, "token_cap", None),
                "model_cycle_cap": getattr(args, "model_cycle_cap", None),
                "tool_cycle_cap": getattr(args, "tool_cycle_cap", None),
            },
            "token_cap_contract_sha256": optional_hash(
                getattr(args, "token_cap_contract", None)
            ),
            "budget_increase_contract_sha256": optional_hash(
                getattr(args, "budget_increase_contract", None)
            ),
        },
        "required_skills": requirements["required_skills"],
        "skill_hashes": requirements["skill_hashes"],
        "source_commit": requirements["source_commit"],
        **{
            key: requirements[key]
            for key in (
                "required_skills_contract_path",
                "required_skills_contract_sha256",
            )
            if key in requirements
        },
    }
    if resume_contract is not None:
        identity["resume_contract"] = resume_contract
    return {**identity, "dispatch_packet_sha256": content_hash(identity)}


def validate_dispatch_packet(
    packet: dict[str, Any], expected: dict[str, Any]
) -> None:
    supplied_hash = packet.get("dispatch_packet_sha256")
    packet_identity = {
        key: value for key, value in packet.items() if key != "dispatch_packet_sha256"
    }
    if not isinstance(supplied_hash, str) or not HEX_SHA256.fullmatch(supplied_hash):
        raise DispatchError("planner dispatch packet omitted its SHA-256 binding")
    if content_hash(packet_identity) != supplied_hash:
        raise DispatchError("planner dispatch packet self-hash is invalid")
    if packet != expected:
        raise DispatchError(
            "planner dispatch packet does not match the exact phase prompt/context/runtime"
        )


def _read_bound_json(
    path: Path, label: str
) -> tuple[Path, bytes, dict[str, Any]]:
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


def load_resumption_policy() -> tuple[Path, bytes, dict[str, Any]]:
    path, raw, policy = _read_bound_json(RESUMPTION_POLICY, "resumption policy")
    expected_fields = {
        "allowed_modes",
        "checkpoint_contract_name",
        "checkpoint_contract_version",
        "claim_contract_name",
        "claim_contract_version",
        "max_checkpoint_bytes",
        "max_checkpoint_summary_bytes",
        "max_checkpoint_ttl_seconds",
        "max_continuations",
        "max_work_items",
        "policy_name",
        "policy_sha256",
        "policy_version",
        "safe_mutation_classes",
        "schema_version",
        "work_manifest_contract_name",
        "work_manifest_contract_version",
    }
    bare = dict(policy)
    supplied = bare.pop("policy_sha256", None)
    integer_fields = (
        "max_checkpoint_bytes",
        "max_checkpoint_summary_bytes",
        "max_checkpoint_ttl_seconds",
        "max_continuations",
        "max_work_items",
    )
    if (
        set(policy) != expected_fields
        or policy.get("schema_version") != 1
        or policy.get("policy_name") != "adaptive-workflow.resumption-policy"
        or policy.get("policy_version") != 1
        or policy.get("allowed_modes") != ["explicit_checkpoint_restart"]
        or policy.get("safe_mutation_classes") != ["none"]
        or policy.get("checkpoint_contract_name")
        != "adaptive-workflow.resume-checkpoint"
        or policy.get("checkpoint_contract_version") != 1
        or policy.get("claim_contract_name") != "adaptive-workflow.resume-claim"
        or policy.get("claim_contract_version") != 1
        or policy.get("work_manifest_contract_name")
        != "adaptive-workflow.resume-work-manifest"
        or policy.get("work_manifest_contract_version") != 1
        or any(
            type(policy.get(field)) is not int or policy[field] < 1
            for field in integer_fields
        )
        or supplied != content_hash(bare)
    ):
        raise DispatchError("resumption policy is invalid or self-hash mismatched")
    return path, raw, policy


def load_resume_work_manifest(
    path: Path, policy: dict[str, Any]
) -> dict[str, Any]:
    resolved, raw, manifest = _read_bound_json(path, "resume work manifest")
    expected_fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "work_items",
        "manifest_sha256",
    }
    bare = dict(manifest)
    supplied = bare.pop("manifest_sha256", None)
    items = manifest.get("work_items")
    if (
        set(manifest) != expected_fields
        or manifest.get("schema_version") != 1
        or manifest.get("contract_name")
        != policy["work_manifest_contract_name"]
        or manifest.get("contract_version")
        != policy["work_manifest_contract_version"]
        or not isinstance(items, list)
        or len(items) < 2
        or len(items) > policy["max_work_items"]
        or supplied != content_hash(bare)
    ):
        raise DispatchError("resume work manifest is invalid or self-hash mismatched")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "description"}:
            raise DispatchError("resume work manifest item schema is invalid")
        work_id = item.get("id")
        description = item.get("description")
        if (
            not isinstance(work_id, str)
            or not WORK_ID.fullmatch(work_id)
            or work_id in seen
            or not isinstance(description, str)
            or not description.strip()
            or len(description.encode("utf-8")) > 512
        ):
            raise DispatchError("resume work manifest item is invalid or duplicated")
        seen.add(work_id)
    return {
        "path": str(resolved),
        "file_sha256": sha256_bytes(raw),
        "manifest_sha256": manifest["manifest_sha256"],
        "work_items": items,
    }


def build_expected_resume_contract(
    args: argparse.Namespace, phase: dict[str, Any]
) -> dict[str, Any] | None:
    resumable = getattr(args, "resumable", False) is True
    names = (
        "work_manifest",
        "max_continuations",
        "cumulative_wall_time_seconds",
        "checkpoint_window_seconds",
        "shutdown_window_seconds",
        "checkpoint_ttl_seconds",
    )
    values = tuple(getattr(args, name, None) for name in names)
    if not resumable:
        if any(value is not None for value in values):
            raise DispatchError("resume options require --resumable")
        return None
    if any(value is None for value in values):
        raise DispatchError("--resumable requires every resume contract option")
    request = phase.get("route_request", {})
    if (
        request.get("mutation") != "none"
        or args.sandbox != "read-only"
        or args.tool_mode != "none"
        or args.network_access
        or args.mutation_authorized
    ):
        raise DispatchError(
            "resumable dispatch requires a non-mutating read-only no-tools phase"
        )
    policy_path, policy_raw, policy = load_resumption_policy()
    if (
        type(args.max_continuations) is not int
        or not 1 <= args.max_continuations <= policy["max_continuations"]
        or type(args.cumulative_wall_time_seconds) is not int
        or args.cumulative_wall_time_seconds < args.wall_time_seconds
        or type(args.checkpoint_window_seconds) is not int
        or args.checkpoint_window_seconds < 1
        or type(args.shutdown_window_seconds) is not int
        or args.shutdown_window_seconds < 1
        or args.checkpoint_window_seconds + args.shutdown_window_seconds
        >= args.wall_time_seconds
        or type(args.checkpoint_ttl_seconds) is not int
        or not 1
        <= args.checkpoint_ttl_seconds
        <= policy["max_checkpoint_ttl_seconds"]
    ):
        raise DispatchError("resumption timing, TTL, or continuation limit is unsafe")
    manifest = load_resume_work_manifest(args.work_manifest, policy)
    identity = {
        "contract_name": RESUME_AUTHORITY_NAME,
        "contract_version": 1,
        "mode": "explicit_checkpoint_restart",
        "policy_path": str(policy_path),
        "policy_file_sha256": sha256_bytes(policy_raw),
        "policy_sha256": policy["policy_sha256"],
        "work_manifest": manifest,
        "attempt_wall_time_seconds": args.wall_time_seconds,
        "cumulative_wall_time_seconds": args.cumulative_wall_time_seconds,
        "checkpoint_window_seconds": args.checkpoint_window_seconds,
        "shutdown_window_seconds": args.shutdown_window_seconds,
        "checkpoint_ttl_seconds": args.checkpoint_ttl_seconds,
        "max_continuations": args.max_continuations,
        "mutation_class": "none",
    }
    return {**identity, "resume_contract_sha256": content_hash(identity)}


def parse_explicit_checkpoint(
    item: dict[str, Any],
    *,
    nonce: str,
    work_manifest: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    if (
        item.get("type") != "agentMessage"
        or item.get("phase") not in (None, "commentary")
        or not isinstance(item.get("id"), str)
        or not item["id"]
    ):
        raise DispatchError("completed item is not a non-final assistant checkpoint")
    text = item.get("text")
    if not isinstance(text, str) or not text.startswith(CHECKPOINT_PREFIX):
        raise DispatchError("assistant message omitted the checkpoint envelope")
    encoded = text.encode("utf-8")
    if len(encoded) > policy["max_checkpoint_bytes"]:
        raise DispatchError("assistant checkpoint exceeds the policy size bound")
    payload_text = text[len(CHECKPOINT_PREFIX) :]
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as error:
        raise DispatchError("assistant checkpoint JSON is invalid") from error
    fields = {
        "checkpoint_nonce",
        "checkpoint_type",
        "completed_results",
        "completed_work_ids",
        "remaining_work_ids",
        "schema_version",
        "summary",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != fields
        or payload.get("schema_version") != 1
        or payload.get("checkpoint_type")
        != "adaptive-workflow.explicit-checkpoint"
        or payload.get("checkpoint_nonce") != nonce
        or canonical_json(payload) != payload_text
    ):
        raise DispatchError("assistant checkpoint envelope is noncanonical or mismatched")
    completed = payload.get("completed_work_ids")
    remaining = payload.get("remaining_work_ids")
    results = payload.get("completed_results")
    summary = payload.get("summary")
    manifest_ids = [item["id"] for item in work_manifest["work_items"]]
    if (
        not isinstance(completed, list)
        or not completed
        or not isinstance(remaining, list)
        or not remaining
        or not all(isinstance(value, str) for value in completed + remaining)
        or len(completed + remaining) != len(set(completed + remaining))
        or set(completed + remaining) != set(manifest_ids)
        or completed != [value for value in manifest_ids if value in completed]
        or remaining != [value for value in manifest_ids if value in remaining]
        or not isinstance(results, dict)
        or set(results) != set(completed)
        or any(
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode("utf-8")) > 4096
            for value in results.values()
        )
        or not isinstance(summary, str)
        or not summary.strip()
        or len(summary.encode("utf-8"))
        > policy["max_checkpoint_summary_bytes"]
    ):
        raise DispatchError("assistant checkpoint work partition is invalid")
    return {
        "message_id": item["id"],
        "message_sha256": sha256_bytes(encoded),
        "payload": payload,
        "payload_sha256": content_hash(payload),
    }


def transcript_chain_sha256(segments: list[dict[str, Any]]) -> str:
    return content_hash(
        [
            {
                "sequence": segment.get("sequence"),
                "path": segment.get("path"),
                "sha256": segment.get("sha256"),
                "thread_id": segment.get("thread_id"),
                "turn_id": segment.get("turn_id"),
                "turn_status": segment.get("turn_status"),
                "timing_path": segment.get("timing_path"),
                "timing_file_sha256": segment.get("timing_file_sha256"),
                "timing_sha256": segment.get("timing_sha256"),
                "segment_elapsed_ms": segment.get("segment_elapsed_ms"),
                "cumulative_elapsed_ms": segment.get("cumulative_elapsed_ms"),
            }
            for segment in segments
        ]
    )


def write_segment_timing(
    root: Path,
    *,
    sequence: int,
    thread_id: str,
    turn_id: str,
    segment_elapsed_ms: int,
    cumulative_elapsed_ms: int,
    previous_timing_sha256: str | None,
) -> tuple[Path, dict[str, Any]]:
    if (
        sequence < 1
        or not thread_id
        or not turn_id
        or segment_elapsed_ms < 0
        or cumulative_elapsed_ms < segment_elapsed_ms
    ):
        raise DispatchError("segment timing identity is invalid")
    identity = {
        "schema_version": 1,
        "contract_name": "adaptive-workflow.segment-timing",
        "contract_version": 1,
        "sequence": sequence,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "segment_elapsed_ms": segment_elapsed_ms,
        "cumulative_elapsed_ms": cumulative_elapsed_ms,
        "previous_timing_sha256": previous_timing_sha256,
    }
    timing = {**identity, "timing_sha256": content_hash(identity)}
    path = root / f"segment-timing.{sequence:04d}.json"
    if path.exists() or path.is_symlink():
        raise DispatchError("segment timing sequence already exists")
    _atomic_private_json(path, timing)
    return path, timing


def validate_segment_timing(
    segment: dict[str, Any],
    *,
    expected_sequence: int,
    previous_timing_sha256: str | None,
    previous_cumulative_elapsed_ms: int,
) -> tuple[str, int]:
    path = Path(segment.get("timing_path", ""))
    metadata = (
        path.stat()
        if path.is_absolute() and not path.is_symlink() and path.is_file()
        else None
    )
    if (
        metadata is None
        or stat.S_IMODE(metadata.st_mode) & 0o077
        or metadata.st_nlink != 1
        or sha256_bytes(path.read_bytes()) != segment.get("timing_file_sha256")
    ):
        raise DispatchError("resume segment timing file changed or is unsafe")
    try:
        timing = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DispatchError("resume segment timing file is malformed") from error
    if not isinstance(timing, dict):
        raise DispatchError("resume segment timing must be a JSON object")
    supplied = timing.get("timing_sha256")
    bare = dict(timing)
    bare.pop("timing_sha256", None)
    segment_elapsed_ms = timing.get("segment_elapsed_ms")
    cumulative_elapsed_ms = timing.get("cumulative_elapsed_ms")
    if (
        set(timing)
        != {
            "schema_version",
            "contract_name",
            "contract_version",
            "sequence",
            "thread_id",
            "turn_id",
            "segment_elapsed_ms",
            "cumulative_elapsed_ms",
            "previous_timing_sha256",
            "timing_sha256",
        }
        or timing.get("schema_version") != 1
        or timing.get("contract_name") != "adaptive-workflow.segment-timing"
        or timing.get("contract_version") != 1
        or timing.get("sequence") != expected_sequence
        or timing.get("thread_id") != segment.get("thread_id")
        or timing.get("turn_id") != segment.get("turn_id")
        or timing.get("previous_timing_sha256") != previous_timing_sha256
        or type(segment_elapsed_ms) is not int
        or segment_elapsed_ms < 0
        or cumulative_elapsed_ms
        != previous_cumulative_elapsed_ms + segment_elapsed_ms
        or supplied != content_hash(bare)
        or segment.get("timing_sha256") != supplied
        or segment.get("segment_elapsed_ms") != segment_elapsed_ms
        or segment.get("cumulative_elapsed_ms") != cumulative_elapsed_ms
    ):
        raise DispatchError("resume segment timing chain is inconsistent")
    return supplied, cumulative_elapsed_ms


def write_resume_checkpoint(
    root: Path, checkpoint: dict[str, Any], policy: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    if set(checkpoint) != CHECKPOINT_IDENTITY_FIELDS:
        raise DispatchError("resume checkpoint schema is incomplete")
    if (
        checkpoint.get("schema_version") != 1
        or checkpoint.get("contract_name") != policy["checkpoint_contract_name"]
        or checkpoint.get("contract_version")
        != policy["checkpoint_contract_version"]
        or type(checkpoint.get("sequence")) is not int
        or checkpoint["sequence"] < 1
        or checkpoint.get("mode") != "explicit_checkpoint_restart"
        or checkpoint.get("transcript_chain_sha256")
        != transcript_chain_sha256(checkpoint.get("transcript_chain", []))
    ):
        raise DispatchError("resume checkpoint identity or transcript chain is invalid")
    sealed = {**checkpoint, "checkpoint_sha256": content_hash(checkpoint)}
    raw = (json.dumps(sealed, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(raw) > policy["max_checkpoint_bytes"]:
        raise DispatchError("sealed resume checkpoint exceeds the policy size bound")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / f"resume-checkpoint.{checkpoint['sequence']:04d}.json"
    if path.exists() or path.is_symlink():
        raise DispatchError("resume checkpoint sequence already exists")
    _atomic_private_bytes(path, raw)
    return path, sealed


def load_resume_checkpoint(
    path: Path, *, now_unix_ms: int | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.expanduser().is_absolute():
        raise DispatchError("resume checkpoint path must be absolute")
    unresolved = path.expanduser().absolute()
    if unresolved.is_symlink() or not unresolved.is_file():
        raise DispatchError("resume checkpoint is missing or unsafe")
    metadata = unresolved.stat()
    if stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_nlink != 1:
        raise DispatchError("resume checkpoint permissions or link count are unsafe")
    policy_path, policy_raw, policy = load_resumption_policy()
    if metadata.st_size > policy["max_checkpoint_bytes"]:
        raise DispatchError("resume checkpoint exceeds the policy size bound")
    try:
        checkpoint = json.loads(unresolved.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DispatchError("resume checkpoint is partial or malformed") from error
    if not isinstance(checkpoint, dict):
        raise DispatchError("resume checkpoint must be a JSON object")
    if set(checkpoint) != CHECKPOINT_IDENTITY_FIELDS | {"checkpoint_sha256"}:
        raise DispatchError("resume checkpoint schema is invalid")
    supplied = checkpoint.get("checkpoint_sha256")
    bare = dict(checkpoint)
    bare.pop("checkpoint_sha256", None)
    if (
        not isinstance(supplied, str)
        or not HEX_SHA256.fullmatch(supplied)
        or content_hash(bare) != supplied
        or checkpoint.get("schema_version") != 1
        or checkpoint.get("contract_name") != policy["checkpoint_contract_name"]
        or checkpoint.get("contract_version")
        != policy["checkpoint_contract_version"]
        or checkpoint.get("mode") != "explicit_checkpoint_restart"
        or type(checkpoint.get("sequence")) is not int
        or checkpoint["sequence"] < 1
        or not isinstance(checkpoint.get("checkpoint_id"), str)
        or not checkpoint["checkpoint_id"]
    ):
        raise DispatchError("resume checkpoint self-hash or contract is invalid")
    binding = checkpoint.get("binding")
    if (
        not isinstance(binding, dict)
        or binding.get("resumption_policy_path") != str(policy_path)
        or binding.get("resumption_policy_file_sha256")
        != sha256_bytes(policy_raw)
        or binding.get("resumption_policy_sha256") != policy["policy_sha256"]
    ):
        raise DispatchError("resume checkpoint policy binding changed")
    now = int(time.time() * 1000) if now_unix_ms is None else now_unix_ms
    created = checkpoint.get("created_at_unix_ms")
    expires = checkpoint.get("expires_at_unix_ms")
    bound_resume_contract = binding.get("resume_contract")
    bound_ttl_seconds = (
        bound_resume_contract.get("checkpoint_ttl_seconds")
        if isinstance(bound_resume_contract, dict)
        else None
    )
    if (
        type(created) is not int
        or type(expires) is not int
        or type(bound_ttl_seconds) is not int
        or not 1
        <= bound_ttl_seconds
        <= policy["max_checkpoint_ttl_seconds"]
        or expires != created + bound_ttl_seconds * 1000
        or created > now + 30_000
        or now > expires
    ):
        raise DispatchError("resume checkpoint is stale or has an invalid timestamp")
    segments = checkpoint.get("transcript_chain")
    if (
        not isinstance(segments, list)
        or not segments
        or checkpoint.get("transcript_chain_sha256")
        != transcript_chain_sha256(segments)
    ):
        raise DispatchError("resume checkpoint transcript chain is invalid")
    previous_timing_sha256: str | None = None
    timing_cumulative_elapsed_ms = 0
    for sequence, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict) or segment.get("sequence") != sequence:
            raise DispatchError("resume checkpoint transcript sequence is invalid")
        segment_path = Path(segment.get("path", ""))
        segment_metadata = (
            segment_path.stat()
            if segment_path.is_absolute()
            and not segment_path.is_symlink()
            and segment_path.is_file()
            else None
        )
        if (
            segment_metadata is None
            or stat.S_IMODE(segment_metadata.st_mode) & 0o077
            or segment_metadata.st_nlink != 1
            or sha256_bytes(segment_path.read_bytes()) != segment.get("sha256")
        ):
            raise DispatchError("resume checkpoint transcript changed or is unsafe")
        previous_timing_sha256, timing_cumulative_elapsed_ms = (
            validate_segment_timing(
                segment,
                expected_sequence=sequence,
                previous_timing_sha256=previous_timing_sha256,
                previous_cumulative_elapsed_ms=timing_cumulative_elapsed_ms,
            )
        )
    checkpoint_sequence = checkpoint.get("sequence")
    expected_name = (
        f"resume-checkpoint.{checkpoint_sequence:04d}.json"
        if type(checkpoint_sequence) is int
        else ""
    )
    thread = checkpoint.get("thread")
    if (
        checkpoint_sequence != len(segments)
        or unresolved.name != expected_name
        or not isinstance(thread, dict)
        or not isinstance(thread.get("thread_id"), str)
        or not isinstance(thread.get("source_turn_id"), str)
        or not isinstance(thread.get("turn_ids"), list)
        or not thread["turn_ids"]
        or len(thread["turn_ids"]) != len(set(thread["turn_ids"]))
        or thread["turn_ids"] != [segment.get("turn_id") for segment in segments]
        or any(
            segment.get("thread_id") != thread["thread_id"]
            or segment.get("turn_status") != "interrupted"
            for segment in segments
        )
        or segments[-1].get("thread_id") != thread["thread_id"]
        or segments[-1].get("turn_id") != thread["source_turn_id"]
        or segments[-1].get("turn_status") != "interrupted"
    ):
        raise DispatchError("resume checkpoint thread, turn, or sequence is invalid")
    previous = checkpoint.get("previous_checkpoint_sha256")
    if checkpoint_sequence == 1:
        if previous is not None:
            raise DispatchError("first resume checkpoint has a previous hash")
    else:
        previous_path = unresolved.with_name(
            f"resume-checkpoint.{checkpoint_sequence - 1:04d}.json"
        )
        if previous_path.is_symlink() or not previous_path.is_file():
            raise DispatchError("resume checkpoint chain predecessor is missing")
        previous_value = json.loads(previous_path.read_text(encoding="utf-8"))
        if previous_value.get("checkpoint_sha256") != previous:
            raise DispatchError("resume checkpoint predecessor hash changed")
    budgets = checkpoint.get("budgets")
    usage = checkpoint.get("usage")
    elapsed_ms = checkpoint.get("elapsed_ms")
    if (
        not isinstance(budgets, dict)
        or not isinstance(usage, dict)
        or type(usage.get("total_tokens")) is not int
        or type(elapsed_ms) is not int
        or elapsed_ms != timing_cumulative_elapsed_ms
        or budgets.get("remaining_token_cap")
        != budgets.get("cumulative_token_cap") - usage["total_tokens"]
        or budgets.get("remaining_wall_time_ms")
        != budgets.get("cumulative_wall_time_ms") - elapsed_ms
        or budgets.get("remaining_token_cap", -1) < 0
        or budgets.get("remaining_wall_time_ms", -1) < 0
        or budgets.get("continuations_used") != checkpoint_sequence - 1
        or budgets.get("continuation_limit", -1) < budgets.get(
            "continuations_used", 0
        )
    ):
        raise DispatchError("resume checkpoint cumulative budgets are inconsistent")
    claim_path = unresolved.with_name(
        unresolved.name.replace("resume-checkpoint.", "resume-claim.")
    )
    if claim_path.exists() or claim_path.is_symlink():
        raise DispatchError("RESUME_CHECKPOINT_ALREADY_CLAIMED")
    return checkpoint, policy


def validate_resume_binding(
    checkpoint: dict[str, Any], expected_binding: dict[str, Any]
) -> None:
    if checkpoint.get("binding") != expected_binding:
        raise DispatchError("resume checkpoint binding changed before recovery")


def claim_resume_checkpoint(
    checkpoint_path: Path,
    checkpoint: dict[str, Any],
    *,
    continuation_input_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    claim_path = checkpoint_path.with_name(
        checkpoint_path.name.replace("resume-checkpoint.", "resume-claim.")
    )
    identity = {
        "schema_version": 1,
        "contract_name": "adaptive-workflow.resume-claim",
        "contract_version": 1,
        "checkpoint_path": str(checkpoint_path.resolve(strict=True)),
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "checkpoint_sequence": checkpoint["sequence"],
        "run_id": checkpoint["binding"]["run_id"],
        "thread_id": checkpoint["thread"]["thread_id"],
        "source_turn_id": checkpoint["thread"]["source_turn_id"],
        "continuation_ordinal": checkpoint["sequence"],
        "continuation_input_sha256": continuation_input_sha256,
        "claim_nonce": uuid.uuid4().hex,
        "created_at_unix_ms": int(time.time() * 1000),
    }
    claim = {**identity, "claim_sha256": content_hash(identity)}
    raw = (json.dumps(claim, indent=2, sort_keys=True) + "\n").encode("utf-8")
    claim_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(claim_path.parent, 0o700)
    try:
        descriptor = os.open(
            claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError as error:
        raise DispatchError("RESUME_CHECKPOINT_ALREADY_CLAIMED") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(claim_path.parent)
    except Exception:
        raise
    return claim_path, claim


def sandbox_policy(mode: str, cwd: Path, network_access: bool) -> dict[str, Any]:
    if mode == "read-only":
        return {"type": "readOnly", "networkAccess": network_access}
    if mode == "workspace-write":
        return {
            "type": "workspaceWrite",
            "writableRoots": [str(cwd)],
            "networkAccess": network_access,
        }
    raise DispatchError(f"unsupported worker sandbox: {mode}")


def local_mutation_tool_instructions(sandbox: str, tool_mode: str) -> str:
    if sandbox != "workspace-write" or tool_mode != "default":
        return ""
    return (
        " This is an authorized local workspace-write route. On this App Server "
        "the node_repl MCP `js` tool is the local execution and file-edit bridge, "
        "not an external connector. Use it with local filesystem APIs or execFile "
        "and the installed apply_patch command, remain inside the bound cwd, and "
        "verify the resulting files. Do not claim local tools are unavailable "
        "without first attempting node_repl."
    )


def validate_authority(
    args: argparse.Namespace, request: dict[str, Any], resolution: dict[str, Any], profile: dict[str, Any]
) -> None:
    mutation = request.get("mutation", "none")
    if args.tool_mode == "none" and (
        args.sandbox != "read-only" or args.network_access
    ):
        raise DispatchError(
            "the no-tools contract requires read-only sandboxing with network disabled"
        )
    if request.get("external_action") and request.get("activity") != "prepare_external":
        raise DispatchError("external actions remain parent-only and cannot be dispatched")
    if resolution.get("parent_gate_required"):
        raise DispatchError("parent-gated phases cannot be dispatched before parent action")
    if mutation == "irreversible":
        raise DispatchError("irreversible mutation remains parent-only")
    if args.sandbox == "read-only" and mutation != "none":
        raise DispatchError("a mutating route requires workspace-write dispatch")
    if args.sandbox != "read-only":
        if mutation == "none":
            raise DispatchError("a non-mutating route cannot receive write access")
        if not args.mutation_authorized:
            raise DispatchError("write access requires --mutation-authorized from the parent")
    if profile.get("sandbox_mode") == "read-only" and args.sandbox != "read-only":
        raise DispatchError("the active route profile is read-only")
    web_required = resolution.get("web_required") is True
    if web_required != args.network_access:
        raise DispatchError(
            "network access must exactly match the route's current-information requirement"
        )


def normalize_service_tier(value: Any) -> str:
    if value is None:
        return "default"
    if not isinstance(value, str) or not value:
        raise DispatchError(
            "service tier must be explicit JSON null or a non-empty string"
        )
    return value


def validate_explicit_token_cap(
    token_cap: int | None,
    token_cap_contract: Path | None,
    task_binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Allow a fixed cap only through a hashed, task-bound measured-safe contract."""
    if token_cap is None:
        if token_cap_contract is not None:
            raise DispatchError("--token-cap-contract requires --token-cap")
        return None
    if type(token_cap) is not int or token_cap < 1:
        raise DispatchError("--token-cap must be a positive integer")
    if token_cap_contract is None:
        raise DispatchError(
            "--token-cap requires a hashed --token-cap-contract JSON file"
        )
    if task_binding is None:
        raise DispatchError("fixed token-cap validation omitted the task binding")
    unresolved_path = token_cap_contract.expanduser()
    if not unresolved_path.is_absolute():
        unresolved_path = Path.cwd() / unresolved_path
    unresolved_path = unresolved_path.absolute()
    if unresolved_path.is_symlink() or not unresolved_path.is_file():
        raise DispatchError(
            f"fixed token-cap contract is missing or unsafe: {unresolved_path}"
        )
    path = unresolved_path.resolve(strict=True)
    contract = read_json(path, "fixed token-cap contract")
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "task_binding_sha256",
        "token_cap",
        "measured_safe_evidence_path",
        "measured_safe_evidence_sha256",
        "rationale",
        "contract_sha256",
    }
    if (
        set(contract) != fields
        or type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != 1
        or contract.get("contract_name") != TOKEN_CAP_CONTRACT_NAME
        or type(contract.get("contract_version")) is not int
        or contract.get("contract_version") != 1
        or type(contract.get("token_cap")) is not int
    ):
        raise DispatchError("fixed token-cap contract has an invalid schema")
    if contract.get("token_cap") != token_cap:
        raise DispatchError("fixed token-cap contract does not match --token-cap")
    binding_sha256 = content_hash(task_binding)
    if contract.get("task_binding_sha256") != binding_sha256:
        raise DispatchError("fixed token-cap contract does not match this exact task")
    evidence_sha256 = contract.get("measured_safe_evidence_sha256")
    if not isinstance(evidence_sha256, str) or not HEX_SHA256.fullmatch(
        evidence_sha256
    ):
        raise DispatchError("fixed token-cap contract omitted measured-safe evidence")
    evidence_value = contract.get("measured_safe_evidence_path")
    if not isinstance(evidence_value, str) or not evidence_value:
        raise DispatchError("fixed token-cap contract omitted its evidence path")
    evidence_path = Path(evidence_value).expanduser()
    if (
        not evidence_path.is_absolute()
        or evidence_path.is_symlink()
        or not evidence_path.is_file()
    ):
        raise DispatchError("fixed token-cap evidence is missing or unsafe")
    evidence_path = evidence_path.resolve()
    if evidence_path == path:
        raise DispatchError("fixed token-cap evidence must be separate from its contract")
    if sha256_bytes(evidence_path.read_bytes()) != evidence_sha256:
        raise DispatchError("fixed token-cap evidence hash does not match its artifact")
    rationale = contract.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise DispatchError("fixed token-cap contract omitted its rationale")
    identity = {
        key: contract[key] for key in fields if key != "contract_sha256"
    }
    if contract.get("contract_sha256") != content_hash(identity):
        raise DispatchError("fixed token-cap contract self-hash is invalid")
    return {
        "path": str(path),
        "file_sha256": sha256_bytes(path.read_bytes()),
        "contract_name": contract["contract_name"],
        "contract_version": contract["contract_version"],
        "contract_sha256": contract["contract_sha256"],
        "task_binding_sha256": binding_sha256,
        "measured_safe_evidence_path": str(evidence_path),
        "measured_safe_evidence_sha256": evidence_sha256,
        "rationale": rationale.strip(),
    }


def budget_increase_review_proposal(
    *,
    task_binding_sha256: str,
    measured_limits: dict[str, int],
    requested_limits: dict[str, int],
    evidence_sha256: str,
    rationale: str,
) -> dict[str, Any]:
    identity = {
        "schema_version": 1,
        "proposal_name": BUDGET_INCREASE_REVIEW_PROPOSAL_NAME,
        "task_binding_sha256": task_binding_sha256,
        "measured_limits": measured_limits,
        "requested_limits": requested_limits,
        "evidence_sha256": evidence_sha256,
        "rationale": rationale.strip(),
    }
    return {**identity, "proposal_sha256": content_hash(identity)}


def validate_budget_increase_contract(
    contract_path: Path,
    *,
    task_binding: dict[str, Any],
    measured_limits: dict[str, int],
    requested_limits: dict[str, int],
    registry_path: Path | None = None,
) -> dict[str, Any]:
    """Require a trusted independent review before enlarging a measured envelope."""
    unresolved = contract_path.expanduser().absolute()
    if unresolved.is_symlink() or not unresolved.is_file():
        raise DispatchError("budget increase contract is missing or unsafe")
    path = unresolved.resolve(strict=True)
    contract = read_json(path, "budget increase contract")
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "task_binding_sha256",
        "measured_limits",
        "requested_limits",
        "review_execution_receipt_id",
        "review_proposal_sha256",
        "evidence_path",
        "evidence_sha256",
        "rationale",
        "contract_sha256",
    }
    if (
        set(contract) != fields
        or contract.get("schema_version") != 1
        or contract.get("contract_name") != BUDGET_INCREASE_CONTRACT_NAME
        or contract.get("contract_version") != 1
        or contract.get("task_binding_sha256") != content_hash(task_binding)
        or contract.get("measured_limits") != measured_limits
        or contract.get("requested_limits") != requested_limits
        or not isinstance(contract.get("review_execution_receipt_id"), str)
        or not isinstance(contract.get("review_proposal_sha256"), str)
        or not HEX_SHA256.fullmatch(contract["review_proposal_sha256"])
        or not isinstance(contract.get("rationale"), str)
        or not contract["rationale"].strip()
    ):
        raise DispatchError("budget increase contract has an invalid task binding")
    if not any(
        requested_limits[field] > measured_limits[field]
        for field in measured_limits
    ):
        raise DispatchError("budget increase contract does not increase any limit")
    evidence_value = contract.get("evidence_path")
    evidence_hash = contract.get("evidence_sha256")
    if (
        not isinstance(evidence_value, str)
        or not isinstance(evidence_hash, str)
        or not HEX_SHA256.fullmatch(evidence_hash)
    ):
        raise DispatchError("budget increase contract omitted measured evidence")
    evidence_path = Path(evidence_value).expanduser()
    if (
        not evidence_path.is_absolute()
        or evidence_path.is_symlink()
        or not evidence_path.is_file()
        or sha256_bytes(evidence_path.read_bytes()) != evidence_hash
    ):
        raise DispatchError("budget increase evidence is missing or changed")
    review_proposal = budget_increase_review_proposal(
        task_binding_sha256=contract["task_binding_sha256"],
        measured_limits=measured_limits,
        requested_limits=requested_limits,
        evidence_sha256=evidence_hash,
        rationale=contract["rationale"],
    )
    if contract["review_proposal_sha256"] != review_proposal["proposal_sha256"]:
        raise DispatchError("budget increase review proposal binding is invalid")
    registry = registry_path or EXECUTION_REGISTRY
    if registry.is_symlink() or not registry.is_file():
        raise DispatchError("budget increase review registry is missing or unsafe")
    review_id = contract["review_execution_receipt_id"]
    review = next(
        (
            receipt
            for receipt in _decode_receipt_registry(registry.read_bytes())
            if receipt.get("receipt_id") == review_id
        ),
        None,
    )
    if (
        not isinstance(review, dict)
        or review.get("receipt_type") != "execution"
        or review.get("status") != "COMPLETED"
        or review.get("accepted_usage", {}).get("workflow_id") != "review.audit"
    ):
        raise DispatchError(
            "budget increase requires a trusted completed review.audit receipt"
        )
    review_metadata_value = review.get("metadata_path")
    review_metadata_hash = review.get("metadata_sha256")
    if (
        not isinstance(review_metadata_value, str)
        or not isinstance(review_metadata_hash, str)
        or not HEX_SHA256.fullmatch(review_metadata_hash)
    ):
        raise DispatchError("budget increase review receipt omitted bound metadata")
    review_metadata_path = Path(review_metadata_value).expanduser()
    if (
        not review_metadata_path.is_absolute()
        or review_metadata_path.is_symlink()
        or not review_metadata_path.is_file()
        or sha256_bytes(review_metadata_path.read_bytes()) != review_metadata_hash
    ):
        raise DispatchError("budget increase review metadata is missing or changed")
    review_metadata = read_json(
        review_metadata_path.resolve(strict=True), "budget increase review metadata"
    )
    review_server = review_metadata.get("server")
    review_output = (
        review_server.get("output_text")
        if isinstance(review_server, dict)
        else None
    )
    expected_review_output = (
        f"APPROVE {BUDGET_INCREASE_CONTRACT_NAME} "
        f"{review_proposal['proposal_sha256']}"
    )
    if (
        review_metadata.get("status") != "COMPLETED"
        or review_output != expected_review_output
        or review.get("final_output_sha256")
        != sha256_bytes(expected_review_output.encode("utf-8"))
    ):
        raise DispatchError(
            "review.audit receipt does not approve this exact budget increase proposal"
        )
    identity = {key: contract[key] for key in fields if key != "contract_sha256"}
    if contract.get("contract_sha256") != content_hash(identity):
        raise DispatchError("budget increase contract self-hash is invalid")
    return {
        "path": str(path),
        "file_sha256": sha256_bytes(path.read_bytes()),
        "contract_sha256": contract["contract_sha256"],
        "review_execution_receipt_id": review_id,
        "review_execution_receipt_sha256": review["receipt_sha256"],
        "review_proposal_sha256": review_proposal["proposal_sha256"],
        "evidence_path": str(evidence_path.resolve()),
        "evidence_sha256": evidence_hash,
        "rationale": contract["rationale"].strip(),
    }


def derived_token_budget(
    request: dict[str, Any],
    resolution: dict[str, Any],
    prompt: str,
    *,
    exact_output: str | None,
    tool_mode: str = "default",
) -> dict[str, Any]:
    """Derive a bounded phase budget from its declared requirements, without a global ceiling."""
    prompt_tokens_estimate = max(1, (len(prompt.encode("utf-8")) + 3) // 4)
    built_in_context_reserve = 20_000
    if exact_output is not None or tool_mode == "none":
        estimated_inferences = 1
        exact_output_tokens = (
            (len(exact_output.encode("utf-8")) + 3) // 4
            if exact_output is not None
            else 0
        )
        work_allowance = max(
            2_000,
            exact_output_tokens,
            {"T1": 4_000, "T2": 12_000, "T3": 32_000, "T4": 64_000}[
                resolution["tier"]
            ],
        )
        factors = {
            "activity_turns": 1,
            "scope": 1.0,
            "ambiguity": 1.0,
            "tier": resolution["tier"],
            "exact_output": exact_output is not None,
            "tool_mode": tool_mode,
        }
    else:
        # Tool-enabled turns replay their accumulated context.  Budgeting eight
        # to ten full turns made a small T2 patch eligible to consume hundreds
        # of thousands of input tokens.  The dispatcher now funds a short,
        # evidence-first loop; callers split larger work into independently
        # bound phases instead of buying an unbounded transcript.
        activity_turns, context_growth = {
            "inspect": (2, 4_000),
            "retrieve": (3, 5_000),
            "summarize": (2, 2_000),
            "prepare_external": (2, 3_000),
            "frame": (2, 3_000),
            "design": (2, 4_000),
            "implement": (2, 6_000),
            "analyze": (3, 5_000),
            "draft": (2, 4_000),
            "verify": (2, 5_000),
            "interpret": (2, 4_000),
            "execute_runbook": (3, 5_000),
            "diagnose": (3, 6_000),
            "synthesize": (2, 4_000),
            "plan": (2, 3_000),
            "review": (2, 5_000),
            "adjudicate": (2, 4_000),
            "release": (2, 5_000),
            "architecture": (2, 4_000),
            "threat_model": (3, 5_000),
        }.get(request.get("activity"), (2, 4_000))
        scope_factor = {
            "local": 1.0,
            "multi_file": 1.35,
            "single_system": 1.65,
            "cross_system": 2.2,
        }.get(request.get("scope"), 1.0)
        ambiguity_factor = {
            "none": 0.85,
            "bounded": 1.0,
            "high": 1.5,
            "novel": 2.0,
        }.get(request.get("ambiguity"), 1.0)
        capability_factor = 1.0
        if request.get("current_info_required"):
            capability_factor += 0.25
        if request.get("visual_required"):
            capability_factor += 0.25
        complexity_factor = math.sqrt(
            scope_factor * ambiguity_factor * capability_factor
        )
        estimated_inferences = max(1, math.ceil(activity_turns * complexity_factor))
        work_allowance = {
            "T1": 6_000,
            "T2": 16_000,
            "T3": 32_000,
            "T4": 64_000,
        }[resolution["tier"]]
        factors = {
            "activity_turns": activity_turns,
            "scope": scope_factor,
            "ambiguity": ambiguity_factor,
            "capability": capability_factor,
            "context_growth_per_inference": context_growth,
            "tier": resolution["tier"],
            "exact_output": False,
            "efficiency_profile": "bounded-tool-loop-api-accounted-v2",
        }
    turn_cycle_cap = estimated_inferences
    tool_cycle_cap = (
        0 if exact_output is not None or tool_mode == "none" else turn_cycle_cap * 2
    )
    api_call_allowance = model_api_call_allowance(tool_mode, tool_cycle_cap)
    per_api_call_input = built_in_context_reserve + prompt_tokens_estimate
    growth_total = 0
    if exact_output is None and tool_mode != "none":
        context_growth_horizon = 2
        growth_steps = sum(
            min(index, context_growth_horizon)
            for index in range(api_call_allowance)
        )
        growth_total = round(
            context_growth
            * scope_factor
            * growth_steps
        )
        factors["context_growth_horizon"] = context_growth_horizon
    derived_cap = (
        per_api_call_input * api_call_allowance + growth_total + work_allowance
    )
    derived_cap = ((derived_cap + 999) // 1_000) * 1_000
    return {
        "mode": "derived",
        "budget_formula_version": 2,
        "token_cap": derived_cap,
        "model_cycle_cap": turn_cycle_cap,
        "turn_cycle_cap": turn_cycle_cap,
        "tool_cycle_cap": tool_cycle_cap,
        "model_api_call_allowance": api_call_allowance,
        "recommended_wall_time_seconds": {
            "T1": 300,
            "T2": 600,
            "T3": 900,
            "T4": 1200,
        }[resolution["tier"]],
        "prompt_tokens_estimate": prompt_tokens_estimate,
        "built_in_context_reserve_per_api_call": built_in_context_reserve,
        "built_in_context_reserve_per_inference": built_in_context_reserve,
        "estimated_inferences": estimated_inferences,
        "estimated_turn_cycles": turn_cycle_cap,
        "estimated_context_growth_tokens": growth_total,
        "estimated_bounded_replayed_growth_tokens": growth_total,
        "work_allowance": work_allowance,
        "factors": factors,
    }


def scoped_to_turn(event: dict[str, Any], thread_id: str, turn_id: str) -> bool:
    params = event.get("params", {})
    if not isinstance(params, dict):
        raise DispatchError("App Server event params must be an object")
    if params.get("threadId") != thread_id:
        return False
    event_turn_id = params.get("turnId")
    if event_turn_id is not None:
        return event_turn_id == turn_id
    nested_turn = params.get("turn")
    if isinstance(nested_turn, dict) and nested_turn.get("id") is not None:
        return nested_turn.get("id") == turn_id
    return True


def no_tools_item_violation(
    event: dict[str, Any], thread_id: str, turn_id: str
) -> bool:
    if event.get("method") != "item/started" or not scoped_to_turn(
        event, thread_id, turn_id
    ):
        return False
    params = event.get("params", {})
    item = params.get("item", {})
    if not isinstance(item, dict):
        return True
    item_type = item.get("type")
    return (
        not isinstance(item_type, str)
        or item_type not in NO_TOOLS_PASSIVE_ITEM_TYPES
    )


def observed_cycle_counts(
    events: list[dict[str, Any]], thread_id: str, turn_id: str
) -> dict[str, int]:
    """Count harness-started turns and tool calls as separate resources.

    App Server may emit several ``reasoning`` items during one provider turn;
    those items are not evidence of separate turns. The harness can prove each
    ``turn/start`` it issued, so that is the compatibility ``model_cycles`` unit.
    """
    started_turn_ids: set[str] = set()
    tool_ids: set[str] = set()
    for event in events:
        if not scoped_to_turn(event, thread_id, turn_id):
            continue
        if event.get("method") == "turn/started":
            nested_turn = event.get("params", {}).get("turn", {})
            nested_turn_id = (
                nested_turn.get("id") if isinstance(nested_turn, dict) else None
            )
            if isinstance(nested_turn_id, str) and nested_turn_id:
                started_turn_ids.add(nested_turn_id)
            continue
        if event.get("method") not in {"item/started", "item/completed"}:
            continue
        item = event.get("params", {}).get("item", {})
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            continue
        if item_type not in NO_TOOLS_PASSIVE_ITEM_TYPES:
            tool_ids.add(item_id)
    return {
        "model_cycles": len(started_turn_ids),
        "turn_cycles": len(started_turn_ids),
        "tool_cycles": len(tool_ids),
    }


def observed_model_api_call_updates(
    events: list[dict[str, Any]], thread_id: str, turn_id: str
) -> int:
    """Count strictly advancing cumulative usage updates for the bound turn."""
    count = 0
    highest_total = -1
    for event in events:
        if event.get("method") != "thread/tokenUsage/updated" or not scoped_to_turn(
            event, thread_id, turn_id
        ):
            continue
        total_tokens = (
            event.get("params", {})
            .get("tokenUsage", {})
            .get("total", {})
            .get("totalTokens")
        )
        if type(total_tokens) is int and total_tokens > highest_total:
            highest_total = total_tokens
            count += 1
    return count


def scoped_usage_totals(
    events: list[dict[str, Any]], thread_id: str, turn_id: str
) -> list[int]:
    """Return integer cumulative totals in retained event order."""
    totals: list[int] = []
    for event in events:
        if event.get("method") != "thread/tokenUsage/updated" or not scoped_to_turn(
            event, thread_id, turn_id
        ):
            continue
        total_tokens = (
            event.get("params", {})
            .get("tokenUsage", {})
            .get("total", {})
            .get("totalTokens")
        )
        if type(total_tokens) is int:
            totals.append(total_tokens)
    return totals


def validate_runtime_event_shapes(
    events: list[dict[str, Any]], thread_id: str
) -> None:
    """Reject structurally malformed evidence events before normalization."""
    for event in events:
        method = event.get("method")
        if method not in RUNTIME_EVIDENCE_METHODS:
            continue
        params = event.get("params")
        if not isinstance(params, dict):
            raise DispatchError(f"{method} event params must be an object")
        event_thread_id = params.get("threadId")
        if not isinstance(event_thread_id, str) or not event_thread_id:
            raise DispatchError(f"{method} event omitted threadId")
        if event_thread_id != thread_id:
            continue
        if method == "thread/settings/updated" and not isinstance(
            params.get("threadSettings"), dict
        ):
            raise DispatchError("thread/settings/updated omitted threadSettings")
        if method in {"turn/started", "turn/completed"}:
            turn = params.get("turn")
            if (
                not isinstance(turn, dict)
                or not isinstance(turn.get("id"), str)
                or not turn["id"]
            ):
                raise DispatchError(f"{method} omitted its turn identity")
            if method == "turn/completed" and not isinstance(
                turn.get("status"), str
            ):
                raise DispatchError("turn/completed omitted its turn status")
        if method in {"item/started", "item/completed"}:
            item = params.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise DispatchError(f"{method} omitted its item identity")
        if method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage")
            if not isinstance(token_usage, dict) or not isinstance(
                token_usage.get("total"), dict
            ):
                raise DispatchError(
                    "thread/tokenUsage/updated omitted its total usage object"
                )


def _runtime_record_unchecked(
    events: list[dict[str, Any]],
    *,
    thread_id: str,
    turn_id: str,
    requested: dict[str, str],
    token_cap: int,
    model_cycle_cap: int | None = None,
    tool_cycle_cap: int | None = None,
    model_api_call_allowance: int | None = None,
    tool_mode: str = "default",
    require_tool_use: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    validate_runtime_event_shapes(events, thread_id)
    same_thread_turn_ids: list[str] = []
    for event in events:
        params = event.get("params", {})
        if params.get("threadId") != thread_id:
            continue
        event_turn_id = params.get("turnId")
        nested_turn = params.get("turn")
        if event_turn_id is None and isinstance(nested_turn, dict):
            event_turn_id = nested_turn.get("id")
        if isinstance(event_turn_id, str) and event_turn_id:
            same_thread_turn_ids.append(event_turn_id)
    unique_thread_turn_ids = sorted(set(same_thread_turn_ids))
    started_turn_ids = [
        event.get("params", {}).get("turn", {}).get("id")
        for event in events
        if event.get("method") == "turn/started"
        and event.get("params", {}).get("threadId") == thread_id
    ]
    settings_events = [
        event
        for event in events
        if event.get("method") == "thread/settings/updated"
        and scoped_to_turn(event, thread_id, turn_id)
    ]
    messages = [
        event.get("params", {}).get("item", {})
        for event in events
        if event.get("method") == "item/completed"
        and scoped_to_turn(event, thread_id, turn_id)
        and event.get("params", {}).get("item", {}).get("type") == "agentMessage"
    ]
    usage_events = [
        event
        for event in events
        if event.get("method") == "thread/tokenUsage/updated"
        and scoped_to_turn(event, thread_id, turn_id)
    ]
    completed = [
        event
        for event in events
        if event.get("method") == "turn/completed"
        and scoped_to_turn(event, thread_id, turn_id)
    ]
    reroutes = [
        event.get("params", {})
        for event in events
        if event.get("method") == "model/rerouted"
        and scoped_to_turn(event, thread_id, turn_id)
    ]
    safety = [
        event.get("params", {})
        for event in events
        if event.get("method") == "model/safetyBuffering/updated"
        and scoped_to_turn(event, thread_id, turn_id)
    ]
    bound_item_types: list[str] = []
    for event in events:
        if event.get("method") not in {"item/started", "item/completed"} or not scoped_to_turn(
            event, thread_id, turn_id
        ):
            continue
        item_type = event.get("params", {}).get("item", {}).get("type")
        normalized_item_type = (
            item_type if isinstance(item_type, str) and item_type else "<missing>"
        )
        if normalized_item_type not in bound_item_types:
            bound_item_types.append(normalized_item_type)
    tool_item_types = [
        item_type
        for item_type in bound_item_types
        if item_type not in NO_TOOLS_PASSIVE_ITEM_TYPES
    ]
    settings = (
        settings_events[-1].get("params", {}).get("threadSettings", {})
        if settings_events
        else {}
    )
    observed = {
        "provider": settings.get("modelProvider"),
        "model": settings.get("model"),
        "effort": settings.get("effort"),
        "service_tier": normalize_service_tier(settings.get("serviceTier")),
    }
    usage = (
        usage_events[-1]
        .get("params", {})
        .get("tokenUsage", {})
        .get("total", {})
        if usage_events
        else {}
    )
    input_tokens = usage.get("inputTokens")
    output_tokens = usage.get("outputTokens")
    total_tokens = usage.get("totalTokens")
    cycles = observed_cycle_counts(events, thread_id, turn_id)
    api_call_updates = observed_model_api_call_updates(events, thread_id, turn_id)
    usage_totals = scoped_usage_totals(events, thread_id, turn_id)
    turn_status = (
        completed[-1].get("params", {}).get("turn", {}).get("status")
        if completed
        else None
    )
    issues: list[str] = []
    if not settings_events:
        issues.append("missing thread/settings/updated for the bound thread")
    if unique_thread_turn_ids != [turn_id] or started_turn_ids != [turn_id]:
        issues.append("ephemeral workflow thread was not bound to exactly one turn")
    if len(completed) != 1:
        issues.append("bound workflow did not contain exactly one turn/completed event")
    for event in settings_events:
        thread_settings = event.get("params", {}).get("threadSettings")
        if not isinstance(thread_settings, dict):
            issues.append("bound thread/settings/updated omitted thread settings")
            continue
        missing_settings = sorted(
            {"modelProvider", "model", "effort", "serviceTier"}
            - set(thread_settings)
        )
        if missing_settings:
            issues.append(
                "bound thread settings omitted explicit fields: "
                + ", ".join(missing_settings)
            )
        event_observed = {
            "provider": thread_settings.get("modelProvider"),
            "model": thread_settings.get("model"),
            "effort": thread_settings.get("effort"),
            "service_tier": normalize_service_tier(thread_settings.get("serviceTier")),
        }
        for field in ("provider", "model", "effort", "service_tier"):
            if event_observed[field] != requested[field]:
                issues.append(
                    f"bound thread settings changed {field} from the requested route"
                )
    for field in ("provider", "model", "effort", "service_tier"):
        if observed[field] != requested[field]:
            issues.append(f"observed {field} does not match requested {field}")
    if turn_status != "completed":
        issues.append("bound turn did not complete")
    message_ids = [item.get("id") for item in messages]
    if (
        not message_ids
        or any(not isinstance(value, str) or not value for value in message_ids)
        or len(message_ids) != len(set(message_ids))
    ):
        issues.append("completed agent-message IDs are missing or invalid")
    if (
        type(input_tokens) is not int
        or type(output_tokens) is not int
        or type(total_tokens) is not int
        or input_tokens < 0
        or output_tokens < 0
        or total_tokens != input_tokens + output_tokens
    ):
        issues.append("measured token usage is missing or invalid")
    elif total_tokens > token_cap:
        issues.append(f"token cap exceeded: {total_tokens} > {token_cap}")
    if any(current < previous for previous, current in zip(usage_totals, usage_totals[1:])):
        issues.append("thread-cumulative token usage moved backwards")
    if model_cycle_cap is not None and cycles["model_cycles"] > model_cycle_cap:
        issues.append(
            "turn cycle cap exceeded: "
            f"{cycles['model_cycles']} > {model_cycle_cap}"
        )
    if tool_cycle_cap is not None and cycles["tool_cycles"] > tool_cycle_cap:
        issues.append(
            "tool cycle cap exceeded: "
            f"{cycles['tool_cycles']} > {tool_cycle_cap}"
        )
    if (
        model_api_call_allowance is not None
        and api_call_updates > model_api_call_allowance
    ):
        issues.append(
            "model API-call allowance exceeded: "
            f"{api_call_updates} > {model_api_call_allowance}"
        )
    if reroutes:
        issues.append("runtime reroute observed")
    if safety:
        issues.append("runtime safety buffering observed")
    if tool_mode == "none" and tool_item_types:
        issues.append("tool use observed under the no-tools contract")
    if require_tool_use and not tool_item_types:
        issues.append("mutating workspace route completed without observed tool use")
    output_item = next(
        (item for item in reversed(messages) if item.get("phase") == "final_answer"),
        messages[-1] if messages else {},
    )
    return (
        {
            "requested_provider": requested["provider"],
            "requested_model": requested["model"],
            "requested_effort": requested["effort"],
            "requested_service_tier": requested["service_tier"],
            "observed_provider": observed["provider"],
            "observed_model": observed["model"],
            "observed_effort": observed["effort"],
            "observed_service_tier": observed["service_tier"],
            "thread_id": thread_id,
            "turn_id": turn_id,
            "thread_turn_ids": unique_thread_turn_ids,
            "turn_status": turn_status,
            "completed_agent_message_ids": message_ids,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "total_tokens": total_tokens,
            **cycles,
            "model_api_call_updates": api_call_updates,
            "reroutes": reroutes,
            "safety_buffering": safety,
            "tool_item_types": tool_item_types,
            "output_text": output_item.get("text"),
        },
        issues,
    )


def runtime_record(
    events: list[dict[str, Any]],
    *,
    thread_id: str,
    turn_id: str,
    requested: dict[str, str],
    token_cap: int,
    model_cycle_cap: int | None = None,
    tool_cycle_cap: int | None = None,
    model_api_call_allowance: int | None = None,
    tool_mode: str = "default",
    require_tool_use: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize malformed protocol shapes into a retained dispatch error."""
    try:
        return _runtime_record_unchecked(
            events,
            thread_id=thread_id,
            turn_id=turn_id,
            requested=requested,
            token_cap=token_cap,
            model_cycle_cap=model_cycle_cap,
            tool_cycle_cap=tool_cycle_cap,
            model_api_call_allowance=model_api_call_allowance,
            tool_mode=tool_mode,
            require_tool_use=require_tool_use,
        )
    except DispatchError:
        raise
    except Exception as error:
        raise DispatchError(
            f"malformed App Server event shape: {type(error).__name__}: {error}"
        ) from error


def execution_status(issues: list[str]) -> str:
    if not issues:
        return "COMPLETED"
    if any("cap exceeded" in issue for issue in issues):
        return "BUDGET_STOP"
    return "BLOCKED_MODEL_ENFORCEMENT"


def interrupted_segment_record(
    events: list[dict[str, Any]],
    *,
    thread_id: str,
    turn_id: str,
    requested: dict[str, str],
    token_cap: int,
    model_api_call_allowance: int = 1,
) -> tuple[dict[str, Any], list[str]]:
    runtime, issues = runtime_record(
        events,
        thread_id=thread_id,
        turn_id=turn_id,
        requested=requested,
        token_cap=token_cap,
        model_cycle_cap=1,
        tool_cycle_cap=0,
        model_api_call_allowance=model_api_call_allowance,
        tool_mode="none",
    )
    issues = [issue for issue in issues if issue != "bound turn did not complete"]
    if runtime.get("turn_status") != "interrupted":
        issues.append("checkpoint source turn was not terminally interrupted")
    return runtime, issues


def validate_checkpoint_runtime_state(
    checkpoint: dict[str, Any],
    *,
    resume_contract: dict[str, Any],
    requested: dict[str, str],
    token_cap: int,
) -> None:
    """Re-derive resumable usage and authority from every retained transcript."""
    previous_total = -1
    minimum_elapsed_ms = 0
    previous_timing_sha256: str | None = None
    timing_cumulative_elapsed_ms = 0
    final_usage: dict[str, int] | None = None
    final_events: list[dict[str, Any]] | None = None
    final_segment: dict[str, Any] | None = None
    cumulative_api_call_updates = 0
    turn_cycle_cap = resume_contract["max_continuations"] + 1
    for sequence, segment in enumerate(checkpoint["transcript_chain"], start=1):
        previous_timing_sha256, timing_cumulative_elapsed_ms = (
            validate_segment_timing(
                segment,
                expected_sequence=sequence,
                previous_timing_sha256=previous_timing_sha256,
                previous_cumulative_elapsed_ms=timing_cumulative_elapsed_ms,
            )
        )
        events = transcript_events(Path(segment["path"]))
        runtime, issues = interrupted_segment_record(
            events,
            thread_id=segment["thread_id"],
            turn_id=segment["turn_id"],
            requested=requested,
            token_cap=token_cap,
        )
        if issues:
            raise DispatchError(
                "retained resume transcript failed runtime validation: "
                + "; ".join(issues)
            )
        if runtime["total_tokens"] < previous_total:
            raise DispatchError(
                "retained thread-cumulative token usage moved backwards"
            )
        previous_total = runtime["total_tokens"]
        cumulative_api_call_updates += runtime["model_api_call_updates"]
        if sequence > turn_cycle_cap or cumulative_api_call_updates > turn_cycle_cap:
            raise DispatchError("retained resume cycle allowance was exceeded")
        final_usage = {
            **runtime["usage"],
            "total_tokens": runtime["total_tokens"],
            "model_cycles": sequence,
            "turn_cycles": sequence,
            "tool_cycles": 0,
            "model_api_call_updates": cumulative_api_call_updates,
        }
        final_events = events
        final_segment = segment
        terminal_durations = [
            event.get("params", {}).get("turn", {}).get("durationMs")
            for event in events
            if event.get("method") == "turn/completed"
            and scoped_to_turn(event, segment["thread_id"], segment["turn_id"])
        ]
        if (
            len(terminal_durations) != 1
            or type(terminal_durations[0]) is not int
            or terminal_durations[0] < 0
        ):
            raise DispatchError(
                "retained resume transcript omitted measured turn duration"
            )
        minimum_elapsed_ms += terminal_durations[0]
    explicit = checkpoint.get("explicit_checkpoint")
    payload = explicit.get("payload") if isinstance(explicit, dict) else None
    nonce = payload.get("checkpoint_nonce") if isinstance(payload, dict) else None
    if (
        final_events is None
        or final_segment is None
        or not isinstance(explicit, dict)
        or not isinstance(nonce, str)
        or not nonce
    ):
        raise DispatchError("retained explicit checkpoint evidence is incomplete")
    matching_items = [
        event.get("params", {}).get("item", {})
        for event in final_events
        if event.get("method") == "item/completed"
        and scoped_to_turn(
            event, final_segment["thread_id"], final_segment["turn_id"]
        )
        and event.get("params", {}).get("item", {}).get("id")
        == explicit.get("message_id")
    ]
    if len(matching_items) != 1:
        raise DispatchError(
            "retained explicit checkpoint message is absent or duplicated"
        )
    _, _, policy = load_resumption_policy()
    parsed = parse_explicit_checkpoint(
        matching_items[0],
        nonce=nonce,
        work_manifest=resume_contract["work_manifest"],
        policy=policy,
    )
    if parsed != explicit:
        raise DispatchError(
            "retained explicit checkpoint differs from transcript truth"
        )
    elapsed_ms = checkpoint.get("elapsed_ms")
    cumulative_wall_time_ms = (
        resume_contract["cumulative_wall_time_seconds"] * 1000
    )
    retained_usage = checkpoint.get("usage")
    legacy_final_usage = (
        {
            key: final_usage[key]
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        if final_usage is not None
        else None
    )
    if (
        final_usage is None
        or retained_usage not in (final_usage, legacy_final_usage)
        or type(elapsed_ms) is not int
        or elapsed_ms != timing_cumulative_elapsed_ms
        or not minimum_elapsed_ms <= elapsed_ms <= cumulative_wall_time_ms
    ):
        raise DispatchError("retained resume usage or elapsed time was altered")
    expected_budgets = {
        "cumulative_token_cap": token_cap,
        "remaining_token_cap": token_cap - final_usage["total_tokens"],
        "cumulative_wall_time_ms": cumulative_wall_time_ms,
        "remaining_wall_time_ms": cumulative_wall_time_ms - elapsed_ms,
        "continuation_limit": resume_contract["max_continuations"],
        "continuations_used": checkpoint["sequence"] - 1,
    }
    if checkpoint.get("budgets") != expected_budgets:
        raise DispatchError("retained cumulative resume budgets were altered")


def validate_checkpoint_progress(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> None:
    if previous is None:
        return
    prior_payload = previous["explicit_checkpoint"]["payload"]
    current_payload = current["payload"]
    prior_completed = prior_payload["completed_work_ids"]
    current_completed = current_payload["completed_work_ids"]
    if (
        not set(prior_completed).issubset(current_completed)
        or len(current_completed) <= len(prior_completed)
        or any(
            current_payload["completed_results"].get(work_id)
            != prior_payload["completed_results"].get(work_id)
            for work_id in prior_completed
        )
    ):
        raise DispatchError(
            "explicit checkpoint did not preserve and advance prior completed work"
        )


def checkpoint_steer_prompt(
    nonce: str, work_manifest: dict[str, Any]
) -> str:
    manifest = canonical_json(work_manifest["work_items"])
    return (
        "The bounded execution segment is ending. Stop ordinary work and use no "
        "tools. Emit one non-final assistant checkpoint message in the commentary "
        "phase. Do not expose reasoning. The message must be exactly the prefix "
        f"{CHECKPOINT_PREFIX!r} followed immediately by canonical compact JSON with "
        "exact keys checkpoint_nonce, checkpoint_type, completed_results, "
        "completed_work_ids, remaining_work_ids, schema_version, summary. "
        "checkpoint_type is adaptive-workflow.explicit-checkpoint; schema_version "
        f"is 1; checkpoint_nonce is {nonce!r}. Partition every bound work ID exactly "
        "once between completed_work_ids and remaining_work_ids, preserving manifest "
        "order. completed_results maps completed IDs in the same order to compact "
        "result-only facts. Both lists must be nonempty. Bound work manifest: "
        f"{manifest}"
    )


def continuation_prompt(checkpoint: dict[str, Any]) -> str:
    payload = checkpoint["explicit_checkpoint"]["payload"]
    remaining = payload["remaining_work_ids"]
    completed_results = payload["completed_results"]
    return (
        "Continue the exact bound workflow as a fresh turn from an explicit sealed "
        "checkpoint. This is not continuation of hidden reasoning. Do not repeat any "
        "completed work ID. Execute only the remaining IDs, use no tools, preserve all "
        "original constraints from the persistent thread, and return the final answer "
        "when every remaining ID is complete. Sealed completed results: "
        f"{canonical_json(completed_results)}. Remaining work IDs: "
        f"{canonical_json(remaining)}. Checkpoint SHA-256: "
        f"{checkpoint['checkpoint_sha256']}."
    )


class AppServer:
    def __init__(self, runner: Path, transcript: Path, stderr_path: Path):
        self.transcript = transcript
        self.stderr_path = stderr_path
        transcript.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if transcript.exists() or transcript.is_symlink():
            raise DispatchError(f"App Server transcript path already exists: {transcript}")
        descriptor = os.open(
            transcript, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        os.close(descriptor)
        self.process = subprocess.Popen(
            [str(runner), "app-server", "--listen", "stdio://", "--disable", "multi_agent_v2"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.stdout: queue.Queue[bytes] = queue.Queue()
        self.stderr: queue.Queue[bytes] = queue.Queue()
        for stream, target in (
            (self.process.stdout, self.stdout),
            (self.process.stderr, self.stderr),
        ):
            threading.Thread(
                target=self._drain, args=(stream, target), daemon=True
            ).start()

    def set_transcript(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            raise DispatchError(f"App Server transcript path already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        self.transcript = path

    def _append_transcript(self, line: bytes) -> None:
        with self.transcript.open("ab") as output:
            output.write(line)
            output.flush()

    @staticmethod
    def _drain(stream: Any, target: queue.Queue[bytes]) -> None:
        while True:
            line = stream.readline()
            if not line:
                return
            target.put(line)

    def send(self, value: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise DispatchError("App Server stdin is unavailable")
        self.process.stdin.write(
            (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
        )
        self.process.stdin.flush()

    def next_event(self, deadline: float) -> dict[str, Any]:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerDeadline("App Server event deadline elapsed")
            try:
                line = self.stdout.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self.process.poll() is not None:
                    raise DispatchError(f"App Server exited {self.process.returncode}")
                continue
            self._append_transcript(line)
            if time.monotonic() > deadline:
                raise AppServerDeadline("App Server event deadline elapsed")
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise DispatchError(f"App Server emitted invalid JSONL: {error}") from error
            if not isinstance(event, dict):
                raise DispatchError("App Server emitted a non-object JSONL event")
            return event

    def wait_for(
        self, predicate: Callable[[dict[str, Any]], bool], deadline: float
    ) -> dict[str, Any]:
        while True:
            event = self.next_event(deadline)
            if predicate(event):
                if "error" in event:
                    raise DispatchError(f"App Server rejected request: {event['error']}")
                return event

    def close(self) -> None:
        idle_deadline = time.monotonic() + 0.25
        while time.monotonic() < idle_deadline:
            try:
                line = self.stdout.get(timeout=0.05)
            except queue.Empty:
                continue
            self._append_transcript(line)
            idle_deadline = time.monotonic() + 0.1
        errors: list[bytes] = []
        while True:
            try:
                errors.append(self.stderr.get_nowait())
            except queue.Empty:
                break
        self.stderr_path.write_bytes(b"".join(errors))
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


def transcript_events(path: Path) -> list[dict[str, Any]]:
    try:
        values = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DispatchError(f"raw App Server transcript is invalid: {error}") from error
    if any(not isinstance(value, dict) for value in values):
        raise DispatchError("raw App Server transcript contains a non-object")
    return values


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64] or "phase"


def _resume_binding(
    *,
    root: Path,
    args: argparse.Namespace,
    manifest: dict[str, Any],
    plan_binding: dict[str, Any],
    resolution: dict[str, Any],
    active_policy: dict[str, Any],
    runner: Path,
    runner_hash: str,
    runner_version: str,
    resume_contract: dict[str, Any],
) -> dict[str, Any]:
    input_manifest_path = (root / "input-manifest.json").resolve(strict=True)
    packet = plan_binding["dispatch_packet"]
    return {
        "run_id": root.name,
        "run_root": str(root.resolve(strict=True)),
        "plan_path": plan_binding["plan_path"],
        "plan_file_sha256": plan_binding["plan_file_sha256"],
        "plan_id": plan_binding["plan_id"],
        "phase_key": plan_binding["phase_key"],
        "phase_contract_sha256": packet["phase_contract_sha256"],
        "dispatch_packet_path": plan_binding["dispatch_packet_path"],
        "dispatch_packet_file_sha256": plan_binding[
            "dispatch_packet_file_sha256"
        ],
        "dispatch_packet_sha256": packet["dispatch_packet_sha256"],
        "input_manifest_path": str(input_manifest_path),
        "input_manifest_sha256": sha256_bytes(input_manifest_path.read_bytes()),
        "prompt_path": str(args.prompt_file.expanduser().resolve(strict=True)),
        "raw_prompt_sha256": manifest["raw_prompt_sha256"],
        "prompt_sha256": manifest["prompt_sha256"],
        "context_files": manifest["context_files"],
        "active_policy_path": str(POLICY),
        "active_policy_file_sha256": sha256_bytes(POLICY.read_bytes()),
        "active_policy_id": resolution["policy_id"],
        "resumption_policy_path": resume_contract["policy_path"],
        "resumption_policy_file_sha256": resume_contract[
            "policy_file_sha256"
        ],
        "resumption_policy_sha256": resume_contract["policy_sha256"],
        "resume_contract": resume_contract,
        "route": {
            "provider": resolution["provider"],
            "model": resolution["model"],
            "effort": resolution["effort"],
            "service_tier": resolution["service_tier"],
            "tier": resolution["tier"],
            "profile_file": resolution["profile_file"],
            "profile_sha256": resolution["profile_sha256"],
        },
        "permissions": manifest["permissions"],
        "cwd": manifest["cwd"],
        "tool_surface": manifest["tool_surface"],
        "runner": {
            "path": str(runner),
            "sha256": runner_hash,
            "version": runner_version,
        },
        "active_policy_schema_version": active_policy.get("schema_version"),
    }


def _checkpoint_references(root: Path) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for path in sorted(root.glob("resume-checkpoint.[0-9][0-9][0-9][0-9].json")):
        if path.is_symlink() or not path.is_file():
            raise DispatchError("retained checkpoint chain contains an unsafe path")
        value = json.loads(path.read_text(encoding="utf-8"))
        supplied = value.get("checkpoint_sha256") if isinstance(value, dict) else None
        bare = dict(value) if isinstance(value, dict) else {}
        bare.pop("checkpoint_sha256", None)
        if supplied != content_hash(bare):
            raise DispatchError("retained checkpoint chain self-hash is invalid")
        references.append(
            {
                "sequence": value.get("sequence"),
                "path": str(path.resolve()),
                "sha256": sha256_bytes(path.read_bytes()),
                "checkpoint_sha256": supplied,
            }
        )
    if [item["sequence"] for item in references] != list(
        range(1, len(references) + 1)
    ):
        raise DispatchError("retained checkpoint sequence has a gap")
    return references


def _claim_references(root: Path) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for path in sorted(root.glob("resume-claim.[0-9][0-9][0-9][0-9].json")):
        if path.is_symlink() or not path.is_file():
            raise DispatchError("retained claim chain contains an unsafe path")
        value = json.loads(path.read_text(encoding="utf-8"))
        supplied = value.get("claim_sha256") if isinstance(value, dict) else None
        bare = dict(value) if isinstance(value, dict) else {}
        bare.pop("claim_sha256", None)
        if (
            supplied != content_hash(bare)
            or value.get("schema_version") != 1
            or value.get("contract_name") != "adaptive-workflow.resume-claim"
            or value.get("contract_version") != 1
        ):
            raise DispatchError("retained claim self-hash is invalid")
        references.append(
            {
                "sequence": value.get("checkpoint_sequence"),
                "path": str(path.resolve()),
                "sha256": sha256_bytes(path.read_bytes()),
                "claim_sha256": supplied,
            }
        )
    if [item["sequence"] for item in references] != list(
        range(1, len(references) + 1)
    ):
        raise DispatchError("retained claim sequence has a gap")
    return references


def validated_resume_artifact_chain(
    root: Path,
    transcript_chain: list[dict[str, Any]],
    completion_mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checkpoints = _checkpoint_references(root)
    claims = _claim_references(root)
    expected = 0 if completion_mode == "uninterrupted" else len(transcript_chain) - 1
    if len(checkpoints) != expected or len(claims) != expected:
        raise DispatchError(
            "completed resume artifact cardinality does not match transcript history"
        )
    for sequence in range(1, expected + 1):
        checkpoint_path = root / f"resume-checkpoint.{sequence:04d}.json"
        claim_path = root / f"resume-claim.{sequence:04d}.json"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        claim = json.loads(claim_path.read_text(encoding="utf-8"))
        if (
            checkpoint.get("transcript_chain") != transcript_chain[:sequence]
            or checkpoint.get("transcript_chain_sha256")
            != transcript_chain_sha256(transcript_chain[:sequence])
            or claim.get("checkpoint_path") != str(checkpoint_path.resolve())
            or claim.get("checkpoint_sha256")
            != checkpoint.get("checkpoint_sha256")
            or claim.get("checkpoint_sequence") != sequence
            or claim.get("thread_id")
            != checkpoint.get("thread", {}).get("thread_id")
            or claim.get("source_turn_id")
            != checkpoint.get("thread", {}).get("source_turn_id")
            or claim.get("continuation_input_sha256")
            != sha256_bytes(continuation_prompt(checkpoint).encode("utf-8"))
        ):
            raise DispatchError(
                "completed resume checkpoint/claim chain is inconsistent"
            )
    return checkpoints, claims


def _run_resumable_phase(
    *,
    args: argparse.Namespace,
    root: Path,
    request: dict[str, Any],
    resolution: dict[str, Any],
    active_policy: dict[str, Any],
    runner: Path,
    runner_hash: str,
    profile: dict[str, Any],
    prompt: str,
    requested: dict[str, str],
    token_budget: dict[str, Any],
    token_cap: int,
    manifest: dict[str, Any],
    plan_binding: dict[str, Any],
    started_at: float,
    initial_checkpoint: dict[str, Any] | None,
    initial_checkpoint_path: Path | None,
) -> int:
    resume_contract = plan_binding["dispatch_packet"].get("resume_contract")
    if not isinstance(resume_contract, dict):
        raise DispatchError("resumable execution omitted its bound authority")
    _, _, resume_policy = load_resumption_policy()
    effective_limits = token_budget.get("effective_limits", {})
    turn_cycle_cap = effective_limits.get(
        "turn_cycle_cap", resume_contract["max_continuations"] + 1
    )
    api_call_allowance = effective_limits.get(
        "model_api_call_allowance", resume_contract["max_continuations"] + 1
    )
    work_manifest = resume_contract["work_manifest"]
    runner_version = subprocess.run(
        [str(runner), "--version"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    ).stdout.strip()
    binding = _resume_binding(
        root=root,
        args=args,
        manifest=manifest,
        plan_binding=plan_binding,
        resolution=resolution,
        active_policy=active_policy,
        runner=runner,
        runner_hash=runner_hash,
        runner_version=runner_version,
        resume_contract=resume_contract,
    )
    if initial_checkpoint is not None:
        validate_resume_binding(initial_checkpoint, binding)
        validate_checkpoint_runtime_state(
            initial_checkpoint,
            resume_contract=resume_contract,
            requested=requested,
            token_cap=token_cap,
        )

    transcript_chain = (
        list(initial_checkpoint["transcript_chain"])
        if initial_checkpoint is not None
        else []
    )
    cumulative_usage = (
        dict(initial_checkpoint["usage"])
        if initial_checkpoint is not None
        else {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    )
    cumulative_elapsed_ms = (
        initial_checkpoint["elapsed_ms"] if initial_checkpoint is not None else 0
    )
    pending_checkpoint = initial_checkpoint
    pending_checkpoint_path = initial_checkpoint_path
    thread_id = (
        initial_checkpoint["thread"]["thread_id"]
        if initial_checkpoint is not None
        else None
    )
    turn_ids = (
        list(initial_checkpoint["thread"]["turn_ids"])
        if initial_checkpoint is not None
        else []
    )
    cumulative_usage.setdefault("model_cycles", len(turn_ids))
    cumulative_usage.setdefault("turn_cycles", len(turn_ids))
    cumulative_usage.setdefault("tool_cycles", 0)
    cumulative_usage.setdefault("model_api_call_updates", len(turn_ids))

    def abort(category: str, reasons: list[str]) -> int:
        completion_mode = (
            "aborted_without_resumable_checkpoint"
            if category == "ABORTED_NO_RESUMABLE_CHECKPOINT"
            else "aborted"
        )
        checkpoint_chain = _checkpoint_references(root)
        claim_chain = _claim_references(root)
        artifact = {
            "schema_version": 1,
            "status": (
                "ABORTED_NO_RESUMABLE_CHECKPOINT"
                if category == "ABORTED_NO_RESUMABLE_CHECKPOINT"
                else "ABORTED"
            ),
            "category": category,
            "reasons": reasons,
            "thread_id": thread_id,
            "turn_ids": turn_ids,
            "completion_mode": completion_mode,
            "genuine_resume_supported": False,
            "input_manifest_sha256": sha256_bytes(
                (root / "input-manifest.json").read_bytes()
            ),
            "transcript_chain": transcript_chain,
            "transcript_chain_sha256": transcript_chain_sha256(transcript_chain),
            "checkpoint_chain": checkpoint_chain,
            "claim_chain": claim_chain,
            "usage": cumulative_usage,
            "elapsed_ms": cumulative_elapsed_ms,
        }
        aborted_path = root / "aborted.json"
        _atomic_private_json(aborted_path, artifact)
        input_manifest_path = (root / "input-manifest.json").resolve()
        receipt = append_execution_receipt(
            {
                "metadata_path": str(aborted_path.resolve()),
                "metadata_sha256": sha256_bytes(aborted_path.read_bytes()),
                "transcript_chain": transcript_chain,
                "transcript_chain_sha256": transcript_chain_sha256(
                    transcript_chain
                ),
                "checkpoint_chain": checkpoint_chain,
                "claim_chain": claim_chain,
                "input_manifest_path": str(input_manifest_path),
                "input_manifest_sha256": sha256_bytes(
                    input_manifest_path.read_bytes()
                ),
                "runner": {
                    "path": str(runner),
                    "sha256": runner_hash,
                    "version": runner_version,
                },
                "profile_sha256": resolution["profile_sha256"],
                "dispatch_packet_sha256": manifest["dispatch_packet_sha256"],
                "thread_id": thread_id,
                "turn_ids": turn_ids,
                "completed_agent_message_ids": [],
                "requested_identity": dict(requested),
                "observed_identity": None,
                "usage": cumulative_usage,
                "duration_ms": cumulative_elapsed_ms,
                "final_output_sha256": sha256_bytes(b""),
                "completion_mode": completion_mode,
                "genuine_resume_supported": False,
                "model_promotion_eligible": False,
                "status": artifact["status"],
                "issues": reasons,
            }
        )
        _atomic_private_json(
            root / "execution-receipt.json",
            {
                "schema_version": 2,
                "registry_path": str(EXECUTION_REGISTRY),
                "registry_sha256": sha256_bytes(EXECUTION_REGISTRY.read_bytes()),
                "receipt_id": receipt["receipt_id"],
                "receipt_sha256": receipt["receipt_sha256"],
            },
        )
        print(json.dumps(artifact, indent=2, sort_keys=True))
        return 2

    while True:
        segment_started = time.monotonic()
        continuation_ordinal = len(transcript_chain)
        if continuation_ordinal > resume_contract["max_continuations"]:
            return abort(
                "CONTINUATION_LIMIT",
                ["bound continuation limit exhausted before completion"],
            )
        if len(turn_ids) >= turn_cycle_cap:
            return abort(
                "BUDGET_STOP",
                [f"turn cycle cap exhausted: {len(turn_ids)} >= {turn_cycle_cap}"],
            )
        if cumulative_usage["model_api_call_updates"] >= api_call_allowance:
            return abort(
                "BUDGET_STOP",
                [
                    "model API-call allowance exhausted: "
                    f"{cumulative_usage['model_api_call_updates']} "
                    f">= {api_call_allowance}"
                ],
            )
        if cumulative_usage["total_tokens"] >= token_cap:
            return abort(
                "BUDGET_STOP",
                ["cumulative token budget is exhausted before continuation"],
            )
        remaining_wall_ms = (
            resume_contract["cumulative_wall_time_seconds"] * 1000
            - cumulative_elapsed_ms
        )
        minimum_segment_ms = (
            resume_contract["checkpoint_window_seconds"]
            + resume_contract["shutdown_window_seconds"]
            + 1
        ) * 1000
        if remaining_wall_ms < minimum_segment_ms:
            return abort(
                "BUDGET_STOP",
                ["remaining cumulative wall-time budget cannot fund a safe segment"],
            )

        if pending_checkpoint is not None:
            assert pending_checkpoint_path is not None
            try:
                pending_checkpoint, _ = load_resume_checkpoint(
                    pending_checkpoint_path
                )
                validate_resume_binding(pending_checkpoint, binding)
                validate_checkpoint_runtime_state(
                    pending_checkpoint,
                    resume_contract=resume_contract,
                    requested=requested,
                    token_cap=token_cap,
                )
            except DispatchError as error:
                return abort("RESUME_CHECKPOINT_REVALIDATION", [str(error)])
        continuation_input = (
            continuation_prompt(pending_checkpoint)
            if pending_checkpoint is not None
            else prompt
        )
        claim_committed = False
        if pending_checkpoint is not None:
            assert pending_checkpoint_path is not None
            try:
                claim_resume_checkpoint(
                    pending_checkpoint_path,
                    pending_checkpoint,
                    continuation_input_sha256=sha256_bytes(
                        continuation_input.encode("utf-8")
                    ),
                )
                claim_committed = True
            except DispatchError as error:
                return abort("ABORTED_AMBIGUOUS_RESUME_CLAIM", [str(error)])

        sequence = len(transcript_chain) + 1
        transcript = root / f"app-server.{sequence:04d}.jsonl"
        stderr_path = root / f"app-server.{sequence:04d}.stderr.log"
        segment_cap_seconds = min(
            resume_contract["attempt_wall_time_seconds"],
            max(1, remaining_wall_ms // 1000),
        )
        hard_deadline = segment_started + segment_cap_seconds
        steer_deadline = hard_deadline - (
            resume_contract["checkpoint_window_seconds"]
            + resume_contract["shutdown_window_seconds"]
        )
        interrupt_deadline = hard_deadline - resume_contract[
            "shutdown_window_seconds"
        ]
        server: AppServer | None = None
        turn_id: str | None = None
        checkpoint_nonce = uuid.uuid4().hex
        checkpoint_candidate: dict[str, Any] | None = None
        checkpoint_parse_failures: list[str] = []
        candidate_seen = False
        semantic_after_candidate = False
        steer_sent = False
        steer_acknowledged = False
        interrupt_sent = False
        live_failure: str | None = None
        terminal_event: dict[str, Any] | None = None
        try:
            server = AppServer(runner, transcript, stderr_path)
            server.send(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "adaptive_workflow_dispatch",
                            "title": "Adaptive Workflow Dispatch",
                            "version": "2.0.0",
                        },
                        "capabilities": {"experimentalApi": True},
                    },
                }
            )
            server.wait_for(lambda event: event.get("id") == 1, hard_deadline)
            if thread_id is None:
                thread_params: dict[str, Any] = {
                    "model": resolution["model"],
                    "modelProvider": resolution["provider"],
                    "serviceTier": (
                        None
                        if resolution["service_tier"] == "default"
                        else resolution["service_tier"]
                    ),
                    "cwd": str(args.cwd.expanduser().resolve()),
                    "runtimeWorkspaceRoots": [str(args.cwd.expanduser().resolve())],
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": False,
                    "allowProviderModelFallback": False,
                    "dynamicTools": [],
                    "environments": [],
                    "selectedCapabilityRoots": [],
                    "baseInstructions": (
                        "You are a bounded no-tools workflow worker. Execute only the "
                        "bound work manifest. Use stable work IDs, emit no hidden "
                        "reasoning, and obey checkpoint steering exactly."
                    ),
                    "developerInstructions": profile[
                        "developer_instructions"
                    ].strip()
                    + "\n\nThis is a strict read-only no-tools resumable phase. "
                    "Never spawn agents or perform external actions.",
                }
                server.send(
                    {"id": 2, "method": "thread/start", "params": thread_params}
                )
                response = server.wait_for(
                    lambda event: event.get("id") == 2, hard_deadline
                )
                thread_id = response.get("result", {}).get("thread", {}).get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    raise DispatchError("App Server omitted the persistent thread ID")
            else:
                server.send(
                    {
                        "id": 2,
                        "method": "thread/resume",
                        "params": {
                            "threadId": thread_id,
                            "model": resolution["model"],
                            "modelProvider": resolution["provider"],
                            "serviceTier": (
                                None
                                if resolution["service_tier"] == "default"
                                else resolution["service_tier"]
                            ),
                            "cwd": str(args.cwd.expanduser().resolve()),
                            "sandbox": "read-only",
                            "excludeTurns": False,
                        },
                    }
                )
                response = server.wait_for(
                    lambda event: event.get("id") == 2, hard_deadline
                )
                resumed_thread = response.get("result", {}).get("thread", {})
                if resumed_thread.get("id") != thread_id:
                    raise DispatchError("thread/resume returned a foreign thread")
                retained_turns = resumed_thread.get("turns")
                if not isinstance(retained_turns, list):
                    raise DispatchError("thread/resume omitted retained turn history")
                if (
                    len(retained_turns) != len(turn_ids)
                    or any(
                        not isinstance(turn, dict)
                        or turn.get("id") != expected_turn_id
                        or turn.get("status") != "interrupted"
                        for turn, expected_turn_id in zip(
                            retained_turns, turn_ids, strict=True
                        )
                    )
                ):
                    raise DispatchError("thread/resume returned foreign turn history")
            server.send(
                {
                    "id": 3,
                    "method": "turn/start",
                    "params": {
                        "threadId": thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": continuation_input,
                                "text_elements": [],
                            }
                        ],
                        "model": resolution["model"],
                        "effort": resolution["effort"],
                        "serviceTier": (
                            None
                            if resolution["service_tier"] == "default"
                            else resolution["service_tier"]
                        ),
                        "sandboxPolicy": sandbox_policy(
                            "read-only", args.cwd.expanduser().resolve(), False
                        ),
                    },
                }
            )
            response = server.wait_for(
                lambda event: event.get("id") == 3, hard_deadline
            )
            turn_id = response.get("result", {}).get("turn", {}).get("id")
            if not isinstance(turn_id, str) or not turn_id or turn_id in turn_ids:
                raise DispatchError("continuation did not receive a distinct turn ID")
            turn_ids.append(turn_id)

            while terminal_event is None:
                now = time.monotonic()
                next_deadline = (
                    steer_deadline
                    if not steer_sent
                    else interrupt_deadline if not interrupt_sent else hard_deadline
                )
                if now >= next_deadline:
                    if not steer_sent:
                        server.send(
                            {
                                "id": 4,
                                "method": "turn/steer",
                                "params": {
                                    "threadId": thread_id,
                                    "expectedTurnId": turn_id,
                                    "input": [
                                        {
                                            "type": "text",
                                            "text": checkpoint_steer_prompt(
                                                checkpoint_nonce, work_manifest
                                            ),
                                            "text_elements": [],
                                        }
                                    ],
                                },
                            }
                        )
                        steer_sent = True
                        continue
                    if not interrupt_sent:
                        server.send(
                            {
                                "id": 5,
                                "method": "turn/interrupt",
                                "params": {
                                    "threadId": thread_id,
                                    "turnId": turn_id,
                                },
                            }
                        )
                        interrupt_sent = True
                        continue
                    raise AppServerDeadline("resumable segment hard deadline elapsed")
                try:
                    event = server.next_event(next_deadline)
                except AppServerDeadline:
                    continue
                if event.get("id") == 4:
                    if "error" in event:
                        live_failure = f"turn/steer failed: {event['error']}"
                    else:
                        steer_acknowledged = True
                    continue
                if event.get("method") in {
                    "model/rerouted",
                    "model/safetyBuffering/updated",
                } and scoped_to_turn(event, thread_id, turn_id):
                    live_failure = "reroute or safety buffering observed"
                if no_tools_item_violation(event, thread_id, turn_id):
                    live_failure = "tool use observed under the no-tools contract"
                if (
                    event.get("method") == "thread/tokenUsage/updated"
                    and scoped_to_turn(event, thread_id, turn_id)
                ):
                    measured = (
                        event.get("params", {})
                        .get("tokenUsage", {})
                        .get("total", {})
                        .get("totalTokens")
                    )
                    if type(measured) is int and measured >= token_cap:
                        live_failure = (
                            f"cumulative token cap exhausted: {measured} >= {token_cap}"
                        )
                if (
                    candidate_seen
                    and event.get("method") in {"item/started", "item/completed"}
                    and scoped_to_turn(event, thread_id, turn_id)
                ):
                    semantic_after_candidate = True
                if (
                    event.get("method") == "item/completed"
                    and scoped_to_turn(event, thread_id, turn_id)
                ):
                    item = event.get("params", {}).get("item", {})
                    if steer_acknowledged and item.get("type") == "agentMessage":
                        try:
                            checkpoint_candidate = parse_explicit_checkpoint(
                                item,
                                nonce=checkpoint_nonce,
                                work_manifest=work_manifest,
                                policy=resume_policy,
                            )
                            candidate_seen = True
                            if not interrupt_sent:
                                server.send(
                                    {
                                        "id": 5,
                                        "method": "turn/interrupt",
                                        "params": {
                                            "threadId": thread_id,
                                            "turnId": turn_id,
                                        },
                                    }
                                )
                                interrupt_sent = True
                        except DispatchError as error:
                            checkpoint_parse_failures.append(str(error))
                if live_failure and not interrupt_sent:
                    server.send(
                        {
                            "id": 5,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        }
                    )
                    interrupt_sent = True
                if (
                    event.get("method") == "turn/completed"
                    and scoped_to_turn(event, thread_id, turn_id)
                ):
                    terminal_event = event
        except Exception as error:
            if server is not None:
                server.close()
                server = None
            segment_elapsed = int((time.monotonic() - segment_started) * 1000)
            cumulative_elapsed_ms += segment_elapsed
            if transcript.is_file():
                _fsync_file(transcript)
                transcript_chain.append(
                    {
                        "sequence": sequence,
                        "path": str(transcript.resolve()),
                        "sha256": sha256_bytes(transcript.read_bytes()),
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "turn_status": "unknown",
                    }
                )
            return abort(
                (
                    "ABORTED_AMBIGUOUS_RESUME_CLAIM"
                    if claim_committed
                    else "ABORTED_NO_RESUMABLE_CHECKPOINT"
                ),
                [str(error)],
            )
        finally:
            if server is not None:
                server.close()

        _fsync_file(transcript)
        events = transcript_events(transcript)
        cumulative_api_call_updates = (
            cumulative_usage["model_api_call_updates"]
            + observed_model_api_call_updates(events, thread_id, turn_id)
        )
        if cumulative_api_call_updates > api_call_allowance:
            live_failure = (
                "model API-call allowance exceeded: "
                f"{cumulative_api_call_updates} > {api_call_allowance}"
            )
        segment_elapsed = int((time.monotonic() - segment_started) * 1000)
        cumulative_elapsed_ms += segment_elapsed
        cumulative_wall_cap_ms = (
            resume_contract["cumulative_wall_time_seconds"] * 1000
        )
        if cumulative_elapsed_ms > cumulative_wall_cap_ms:
            live_failure = (
                "wall-time cap exceeded: "
                f"{cumulative_elapsed_ms} > {cumulative_wall_cap_ms}"
            )
        turn_status = (
            terminal_event.get("params", {}).get("turn", {}).get("status")
            if terminal_event
            else None
        )
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            return abort(
                "SEGMENT_TIMING_COMMIT_FAILURE",
                ["terminal segment omitted thread or turn identity"],
            )
        try:
            timing_path, timing = write_segment_timing(
                root,
                sequence=sequence,
                thread_id=thread_id,
                turn_id=turn_id,
                segment_elapsed_ms=segment_elapsed,
                cumulative_elapsed_ms=cumulative_elapsed_ms,
                previous_timing_sha256=(
                    transcript_chain[-1].get("timing_sha256")
                    if transcript_chain
                    else None
                ),
            )
        except DispatchError as error:
            return abort("SEGMENT_TIMING_COMMIT_FAILURE", [str(error)])
        transcript_chain.append(
            {
                "sequence": sequence,
                "path": str(transcript.resolve()),
                "sha256": sha256_bytes(transcript.read_bytes()),
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn_status": turn_status,
                "timing_path": str(timing_path.resolve()),
                "timing_file_sha256": sha256_bytes(timing_path.read_bytes()),
                "timing_sha256": timing["timing_sha256"],
                "segment_elapsed_ms": segment_elapsed,
                "cumulative_elapsed_ms": cumulative_elapsed_ms,
            }
        )
        if live_failure:
            return abort("BLOCKED_MODEL_ENFORCEMENT", [live_failure])
        if turn_status == "completed":
            runtime, issues = runtime_record(
                events,
                thread_id=thread_id,
                turn_id=turn_id,
                requested=requested,
                token_cap=token_cap,
                model_cycle_cap=1,
                tool_cycle_cap=0,
                model_api_call_allowance=1,
                tool_mode="none",
            )
            if checkpoint_candidate is not None:
                issues.append(
                    "checkpoint candidate completed as a final turn instead of an interrupted source"
                )
            final_messages = [
                event
                for event in events
                if event.get("method") == "item/completed"
                and scoped_to_turn(event, thread_id, turn_id)
                and event.get("params", {}).get("item", {}).get("type")
                == "agentMessage"
                and event.get("params", {}).get("item", {}).get("phase")
                == "final_answer"
            ]
            if not final_messages:
                issues.append(
                    "resumable completed turn omitted an explicit final-answer message"
                )
                issues.extend(checkpoint_parse_failures[-1:])
            if issues:
                return abort("BLOCKED_MODEL_ENFORCEMENT", issues)
            if runtime["total_tokens"] < cumulative_usage["total_tokens"]:
                return abort(
                    "BLOCKED_MODEL_ENFORCEMENT",
                    ["thread-cumulative token usage moved backwards"],
                )
            cumulative_usage = {
                **runtime["usage"],
                "total_tokens": runtime["total_tokens"],
                "model_cycles": len(turn_ids),
                "turn_cycles": len(turn_ids),
                "tool_cycles": 0,
                "model_api_call_updates": cumulative_api_call_updates,
            }
            completion_mode = (
                "uninterrupted"
                if len(transcript_chain) == 1
                else "explicit_checkpoint_restart"
            )
            try:
                checkpoint_chain, claim_chain = validated_resume_artifact_chain(
                    root, transcript_chain, completion_mode
                )
            except DispatchError as error:
                return abort("RESUME_ARTIFACT_CHAIN_FAILURE", [str(error)])
            metadata = {
                "schema_version": 2,
                "source": "openai-codex-app-server-workflow-resumed",
                "evidence_use": "ordinary-workflow-observed-identity",
                "model_promotion_eligible": False,
                "status": "COMPLETED",
                "completion_mode": completion_mode,
                "genuine_resume_supported": False,
                "policy_id": resolution["policy_id"],
                "tier": resolution["tier"],
                "profile_sha256": resolution["profile_sha256"],
                "input_manifest_sha256": sha256_bytes(
                    (root / "input-manifest.json").read_bytes()
                ),
                "runner": {
                    "path": str(runner),
                    "sha256": runner_hash,
                    "version": runner_version,
                },
                "server": {
                    **runtime,
                    "thread_id": thread_id,
                    "turn_ids": turn_ids,
                    "usage": cumulative_usage,
                    "total_tokens": cumulative_usage["total_tokens"],
                    "transcript_chain": transcript_chain,
                    "transcript_chain_sha256": transcript_chain_sha256(
                        transcript_chain
                    ),
                    "checkpoint_chain": checkpoint_chain,
                    "claim_chain": claim_chain,
                },
                "limits": {
                    "token_cap": token_cap,
                    "model_cycle_cap": turn_cycle_cap,
                    "turn_cycle_cap": turn_cycle_cap,
                    "tool_cycle_cap": 0,
                    "model_api_call_allowance": api_call_allowance,
                    "token_budget": token_budget,
                    "cumulative_wall_time_seconds": resume_contract[
                        "cumulative_wall_time_seconds"
                    ],
                    "elapsed_ms": cumulative_elapsed_ms,
                    "continuation_limit": resume_contract["max_continuations"],
                },
                "issues": [],
            }
            write_json(root / "execution-metadata.json", metadata)
            metadata_path = (root / "execution-metadata.json").resolve()
            input_manifest_path = (root / "input-manifest.json").resolve()
            receipt = append_execution_receipt(
                {
                    "metadata_path": str(metadata_path),
                    "metadata_sha256": sha256_bytes(metadata_path.read_bytes()),
                    "transcript_chain": transcript_chain,
                    "transcript_chain_sha256": transcript_chain_sha256(
                        transcript_chain
                    ),
                    "checkpoint_chain": checkpoint_chain,
                    "claim_chain": claim_chain,
                    "input_manifest_path": str(input_manifest_path),
                    "input_manifest_sha256": sha256_bytes(
                        input_manifest_path.read_bytes()
                    ),
                    "runner": metadata["runner"],
                    "profile_sha256": resolution["profile_sha256"],
                    "dispatch_packet_sha256": manifest[
                        "dispatch_packet_sha256"
                    ],
                    "thread_id": thread_id,
                    "turn_ids": turn_ids,
                    "completed_agent_message_ids": runtime[
                        "completed_agent_message_ids"
                    ],
                    "requested_identity": dict(requested),
                    "observed_identity": {
                        "provider": runtime["observed_provider"],
                        "model": runtime["observed_model"],
                        "effort": runtime["observed_effort"],
                        "service_tier": runtime["observed_service_tier"],
                    },
                    "usage": cumulative_usage,
                    "duration_ms": cumulative_elapsed_ms,
                    "accepted_usage": {
                        "workflow_id": request.get("workflow_id"),
                        "workflow_version": request.get("workflow_version"),
                        "phase_id": request.get("phase_id"),
                        "activity": request.get("activity"),
                        "model": runtime["observed_model"],
                        "effort": runtime["observed_effort"],
                        "tool_mode": "none",
                        "total_tokens": cumulative_usage["total_tokens"],
                        "turn_cycles": cumulative_usage["turn_cycles"],
                        "tool_cycles": 0,
                        "model_api_call_updates": cumulative_usage[
                            "model_api_call_updates"
                        ],
                        "duration_ms": cumulative_elapsed_ms,
                    },
                    "final_output_sha256": sha256_bytes(
                        (runtime.get("output_text") or "").encode("utf-8")
                    ),
                    "completion_mode": completion_mode,
                    "genuine_resume_supported": False,
                    "status": "COMPLETED",
                    "issues": [],
                    **workflow_receipt_fields(args, status="COMPLETED", issues=[]),
                }
            )
            write_json(
                root / "execution-receipt.json",
                {
                    "schema_version": 2,
                    "registry_path": str(EXECUTION_REGISTRY),
                    "registry_sha256": sha256_bytes(
                        EXECUTION_REGISTRY.read_bytes()
                    ),
                    "receipt_id": receipt["receipt_id"],
                    "receipt_sha256": receipt["receipt_sha256"],
                },
            )
            printed = dict(metadata)
            printed["dispatcher_receipt_id"] = receipt["receipt_id"]
            printed["dispatcher_receipt_sha256"] = receipt["receipt_sha256"]
            print(json.dumps(printed, indent=2, sort_keys=True))
            return 0

        if turn_status != "interrupted":
            return abort(
                "ABORTED_NO_RESUMABLE_CHECKPOINT",
                ["turn ended without a completed or interrupted terminal state"],
            )
        runtime, issues = interrupted_segment_record(
            events,
            thread_id=thread_id,
            turn_id=turn_id,
            requested=requested,
            token_cap=token_cap,
            model_api_call_allowance=1,
        )
        if checkpoint_candidate is None:
            issues = [
                issue
                for issue in issues
                if issue != "completed agent-message IDs are missing or invalid"
            ]
        if issues:
            return abort("BLOCKED_MODEL_ENFORCEMENT", issues)
        if runtime["total_tokens"] < cumulative_usage["total_tokens"]:
            return abort(
                "BLOCKED_MODEL_ENFORCEMENT",
                ["thread-cumulative token usage moved backwards"],
            )
        cumulative_usage = {
            **runtime["usage"],
            "total_tokens": runtime["total_tokens"],
            "model_cycles": len(turn_ids),
            "turn_cycles": len(turn_ids),
            "tool_cycles": 0,
            "model_api_call_updates": cumulative_api_call_updates,
        }
        if checkpoint_candidate is None:
            return abort(
                "ABORTED_NO_RESUMABLE_CHECKPOINT",
                ["turn ended without a sealed explicit completed assistant checkpoint"]
                + checkpoint_parse_failures[-1:],
            )
        if semantic_after_candidate:
            return abort(
                "ABORTED_NO_RESUMABLE_CHECKPOINT",
                ["semantic output followed the checkpoint candidate before interruption"],
            )
        try:
            validate_checkpoint_progress(pending_checkpoint, checkpoint_candidate)
        except DispatchError as error:
            return abort("ABORTED_NO_RESUMABLE_CHECKPOINT", [str(error)])
        created_ms = int(time.time() * 1000)
        previous_checkpoint_sha256 = (
            pending_checkpoint["checkpoint_sha256"]
            if pending_checkpoint is not None
            else None
        )
        checkpoint_identity = {
            "schema_version": 1,
            "contract_name": resume_policy["checkpoint_contract_name"],
            "contract_version": resume_policy["checkpoint_contract_version"],
            "checkpoint_id": uuid.uuid4().hex,
            "sequence": sequence,
            "previous_checkpoint_sha256": previous_checkpoint_sha256,
            "created_at_unix_ms": created_ms,
            "expires_at_unix_ms": created_ms
            + resume_contract["checkpoint_ttl_seconds"] * 1000,
            "mode": "explicit_checkpoint_restart",
            "binding": binding,
            "thread": {
                "thread_id": thread_id,
                "source_turn_id": turn_id,
                "turn_ids": turn_ids,
            },
            "transcript_chain": transcript_chain,
            "transcript_chain_sha256": transcript_chain_sha256(
                transcript_chain
            ),
            "explicit_checkpoint": checkpoint_candidate,
            "usage": cumulative_usage,
            "elapsed_ms": cumulative_elapsed_ms,
            "budgets": {
                "cumulative_token_cap": token_cap,
                "remaining_token_cap": token_cap
                - cumulative_usage["total_tokens"],
                "cumulative_wall_time_ms": resume_contract[
                    "cumulative_wall_time_seconds"
                ]
                * 1000,
                "remaining_wall_time_ms": resume_contract[
                    "cumulative_wall_time_seconds"
                ]
                * 1000
                - cumulative_elapsed_ms,
                "continuation_limit": resume_contract["max_continuations"],
                "continuations_used": sequence - 1,
            },
            "interruption_reason": "cooperative_segment_checkpoint",
        }
        try:
            pending_checkpoint_path, pending_checkpoint = write_resume_checkpoint(
                root, checkpoint_identity, resume_policy
            )
        except DispatchError as error:
            return abort("CHECKPOINT_COMMIT_FAILURE", [str(error)])


def run_phase(args: argparse.Namespace) -> int:
    request, plan_binding = load_route(args)
    resolution = resolve_phase(request)
    validate_resolution(resolution)
    if plan_binding:
        if plan_binding["plan_policy_id"] != resolution["policy_id"]:
            raise DispatchError("plan policy changed before phase dispatch")
        planned = plan_binding.get("planned_resolution")
        if not isinstance(planned, dict) or any(
            planned.get(field) != resolution.get(field)
            for field in PLAN_RESOLUTION_FIELDS
        ):
            raise DispatchError("phase resolution changed after plan creation")
    policy = read_json(POLICY, "active model policy")
    status = router_status()
    runner, runner_hash, profile = validate_policy(policy, status, resolution)
    validate_authority(args, request, resolution, profile)
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_absolute() or not cwd.is_dir():
        raise DispatchError(f"working directory is missing: {cwd}")
    packet = plan_binding.get("dispatch_packet") if plan_binding else None
    requirements = _skill_requirements_from_packet(packet, cwd)
    if requirements["source_commit"] != source_commit_for(cwd):
        raise DispatchError("dispatch source commit changed after packet binding")
    try:
        governance.validate_skill_requirements(
            requirements["required_skills"],
            source_commit=requirements["source_commit"],
        )
    except governance.GovernanceError as error:
        raise DispatchError(f"required skill admission failed: {error}") from error
    args._skill_requirements = requirements
    args._planned_phase = plan_binding.get("phase") if plan_binding else None
    unresolved_prompt_path = args.prompt_file.expanduser().absolute()
    if unresolved_prompt_path.is_symlink() or not unresolved_prompt_path.is_file():
        raise DispatchError(
            f"prompt file is missing or unsafe: {unresolved_prompt_path}"
        )
    prompt_path = unresolved_prompt_path.resolve(strict=True)
    prompt_bytes = prompt_path.read_bytes()
    try:
        prompt = prompt_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DispatchError(f"prompt file is not UTF-8: {prompt_path}") from error
    if not prompt.strip():
        raise DispatchError("phase prompt is empty")
    context_records: list[dict[str, Any]] = []
    context_blocks: list[str] = []
    for value in args.context_file:
        unresolved_context_path = value.expanduser().absolute()
        if (
            unresolved_context_path.is_symlink()
            or not unresolved_context_path.is_file()
        ):
            raise DispatchError(
                f"context file is missing or unsafe: {unresolved_context_path}"
            )
        context_path = unresolved_context_path.resolve(strict=True)
        context_bytes = context_path.read_bytes()
        try:
            context_text = context_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DispatchError(f"context file is not UTF-8: {context_path}") from error
        context_records.append(
            {
                "path": str(context_path),
                "sha256": sha256_bytes(context_bytes),
                "bytes": len(context_bytes),
            }
        )
        context_blocks.append(
            f"\n\n[BEGIN BOUND CONTEXT: {context_path}]\n"
            f"{context_text}\n[END BOUND CONTEXT: {context_path}]"
        )
    context_bundle = context_bundle_record(
        context_records,
        mode=(
            "precomputed-planner-packet" if plan_binding else "direct-hashed"
        ),
    )
    if plan_binding:
        expected_packet = expected_dispatch_packet(
            plan_binding,
            prompt_sha256=sha256_bytes(prompt_bytes),
            context_records=context_records,
            cwd=cwd,
            args=args,
        )
        validate_dispatch_packet(
            plan_binding["dispatch_packet"], expected_packet
        )
    resume_contract = (
        plan_binding["dispatch_packet"].get("resume_contract")
        if plan_binding
        else None
    )
    requested_model_cycle_cap = getattr(args, "model_cycle_cap", None)
    requested_tool_cycle_cap = getattr(args, "tool_cycle_cap", None)
    budget_increase_contract_path = getattr(
        args, "budget_increase_contract", None
    )
    if resume_contract is not None and (
        args.token_cap is not None
        or requested_model_cycle_cap is not None
        or requested_tool_cycle_cap not in {None, 0}
        or budget_increase_contract_path is not None
        or args.expect_exact_output is not None
    ):
        raise DispatchError(
            "resumable execution uses only its bound derived cumulative budget"
        )
    prompt = prompt + "".join(context_blocks)
    selected_service_tier = resolution["service_tier"]
    requested_service = (
        None if selected_service_tier == "default" else selected_service_tier
    )
    requested = {
        "provider": resolution["provider"],
        "model": resolution["model"],
        "effort": resolution["effort"],
        "service_tier": normalize_service_tier(requested_service),
    }
    tool_surface = (
        "none"
        if args.tool_mode == "none"
        else (
            "codex-builtins-only"
            if not args.network_access
            else "network-capable-default"
        )
    )
    token_cap_task_binding = {
        "schema_version": 1,
        "phase_request_sha256": content_hash(request),
        "route_identity": {
            field: resolution.get(field) for field in PLAN_RESOLUTION_FIELDS
        },
        "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        "context_files": context_records,
        "context_bundle": context_bundle,
        "cwd": str(cwd),
        "runner_sha256": runner_hash,
        "profile_sha256": resolution["profile_sha256"],
        "tool_surface": tool_surface,
        "permissions": {
            "approval_policy": "never",
            "sandbox": args.sandbox,
            "network_access": args.network_access,
            "mutation_authorized": args.mutation_authorized,
        },
        "service_tier": requested_service,
        "normalized_service_tier": selected_service_tier,
        "retry_rule": {"cognitive_retries": 0, "transient_retries": 0},
        "wall_time_cap_seconds": args.wall_time_seconds,
        "expected_exact_output": args.expect_exact_output,
        "dispatch_packet_sha256": (
            plan_binding["dispatch_packet"]["dispatch_packet_sha256"]
            if plan_binding
            else None
        ),
        "source_commit": source_commit_for(cwd),
    }
    fixed_cap_contract = (
        validate_explicit_token_cap(
            args.token_cap,
            getattr(args, "token_cap_contract", None),
            token_cap_task_binding,
        )
        if args.token_cap_contract is not None
        else None
    )
    cold_start_budget = derived_token_budget(
        request,
        resolution,
        prompt,
        exact_output=args.expect_exact_output,
        tool_mode=args.tool_mode,
    )
    estimated_inferences = cold_start_budget.get("estimated_inferences", 1)
    cold_start_budget = {
        **cold_start_budget,
        "model_cycle_cap": cold_start_budget.get(
            "model_cycle_cap", estimated_inferences
        ),
        "turn_cycle_cap": cold_start_budget.get(
            "turn_cycle_cap",
            cold_start_budget.get("model_cycle_cap", estimated_inferences),
        ),
        "tool_cycle_cap": cold_start_budget.get(
            "tool_cycle_cap",
            0 if args.tool_mode == "none" else estimated_inferences * 2,
        ),
        "model_api_call_allowance": cold_start_budget.get(
            "model_api_call_allowance",
            model_api_call_allowance(
                args.tool_mode,
                cold_start_budget.get(
                    "tool_cycle_cap",
                    0 if args.tool_mode == "none" else estimated_inferences * 2,
                ),
            ),
        ),
        "recommended_wall_time_seconds": cold_start_budget.get(
            "recommended_wall_time_seconds", args.wall_time_seconds
        ),
    }
    token_budget = apply_measured_success_envelope(
        cold_start_budget, request, resolution, args.tool_mode
    )
    measured_limits = {
        "token_cap": token_budget["token_cap"],
        "model_cycle_cap": token_budget["model_cycle_cap"],
        "turn_cycle_cap": token_budget.get(
            "turn_cycle_cap", token_budget["model_cycle_cap"]
        ),
        "tool_cycle_cap": token_budget["tool_cycle_cap"],
        "model_api_call_allowance": token_budget.get(
            "model_api_call_allowance",
            model_api_call_allowance(
                args.tool_mode, token_budget["tool_cycle_cap"]
            ),
        ),
        "wall_time_seconds": min(
            args.wall_time_seconds,
            token_budget["recommended_wall_time_seconds"],
        ),
    }
    requested_limits = {
        "token_cap": (
            args.token_cap
            if args.token_cap is not None
            else measured_limits["token_cap"]
        ),
        "model_cycle_cap": (
            requested_model_cycle_cap
            if requested_model_cycle_cap is not None
            else measured_limits["model_cycle_cap"]
        ),
        "turn_cycle_cap": (
            requested_model_cycle_cap
            if requested_model_cycle_cap is not None
            else measured_limits["turn_cycle_cap"]
        ),
        "tool_cycle_cap": (
            requested_tool_cycle_cap
            if requested_tool_cycle_cap is not None
            else measured_limits["tool_cycle_cap"]
        ),
        "wall_time_seconds": (
            args.wall_time_seconds
            if budget_increase_contract_path is not None
            else measured_limits["wall_time_seconds"]
        ),
    }
    requested_limits["model_api_call_allowance"] = model_api_call_allowance(
        args.tool_mode, requested_limits["tool_cycle_cap"]
    )
    if resume_contract is not None:
        resumable_turn_cap = resume_contract["max_continuations"] + 1
        for limits in (measured_limits, requested_limits):
            limits["model_cycle_cap"] = resumable_turn_cap
            limits["turn_cycle_cap"] = resumable_turn_cap
            limits["tool_cycle_cap"] = 0
            limits["model_api_call_allowance"] = resumable_turn_cap
    if args.tool_mode == "none" and requested_limits["tool_cycle_cap"] != 0:
        raise DispatchError("a no-tools phase must have a zero tool-cycle cap")
    increased = any(
        requested_limits[field] > measured_limits[field]
        for field in measured_limits
    )
    budget_task_binding = {
        **token_cap_task_binding,
        "measured_limits": measured_limits,
        "requested_limits": requested_limits,
    }
    budget_increase = None
    if increased:
        if budget_increase_contract_path is None:
            raise DispatchError(
                "a requested limit exceeds the measured envelope; "
                "--budget-increase-contract is required"
            )
        budget_increase = validate_budget_increase_contract(
            budget_increase_contract_path,
            task_binding=budget_task_binding,
            measured_limits=measured_limits,
            requested_limits=requested_limits,
        )
    elif budget_increase_contract_path is not None:
        raise DispatchError(
            "--budget-increase-contract is valid only when a requested limit increases"
        )
    token_budget = {
        **token_budget,
        "cold_start_limits": {
            "token_cap": cold_start_budget["token_cap"],
            "model_cycle_cap": cold_start_budget["model_cycle_cap"],
            "turn_cycle_cap": cold_start_budget["turn_cycle_cap"],
            "tool_cycle_cap": cold_start_budget["tool_cycle_cap"],
            "model_api_call_allowance": cold_start_budget[
                "model_api_call_allowance"
            ],
            "wall_time_seconds": cold_start_budget[
                "recommended_wall_time_seconds"
            ],
        },
        "measured_limits": measured_limits,
        "effective_limits": requested_limits,
        "fixed_task_contract": fixed_cap_contract,
        "budget_increase_contract": budget_increase,
    }
    token_cap = requested_limits["token_cap"]
    model_cycle_cap = requested_limits["model_cycle_cap"]
    tool_cycle_cap = requested_limits["tool_cycle_cap"]
    api_call_allowance = requested_limits["model_api_call_allowance"]
    effective_wall_time_seconds = requested_limits["wall_time_seconds"]
    initial_checkpoint_path = getattr(args, "resume_checkpoint", None)
    initial_checkpoint: dict[str, Any] | None = None
    if initial_checkpoint_path is not None:
        initial_checkpoint_path = initial_checkpoint_path.expanduser().absolute()
        initial_checkpoint, _ = load_resume_checkpoint(initial_checkpoint_path)
        if resume_contract is None:
            raise DispatchError("legacy dispatch packets cannot consume checkpoints")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if initial_checkpoint is not None:
        root = initial_checkpoint_path.parent.resolve(strict=True)
        if args.output_dir is not None and args.output_dir.expanduser().resolve() != root:
            raise DispatchError("resume output directory differs from checkpoint run")
        if initial_checkpoint.get("binding", {}).get("run_root") != str(root):
            raise DispatchError("resume checkpoint is foreign to this run directory")
    else:
        mode = 0o700 if resume_contract is not None else 0o755
        if args.output_dir:
            root = args.output_dir.expanduser().resolve()
            root.mkdir(parents=True, exist_ok=False, mode=mode)
        else:
            root = allocate_run_directory(
                RUNS,
                f"{timestamp}-{safe_slug(request['phase_id'])}",
                mode=mode,
            )
        if resume_contract is not None:
            os.chmod(root, 0o700)
    copied_prompt = root / "prompt.txt"
    if initial_checkpoint is None:
        copied_prompt.write_text(prompt, encoding="utf-8")
        if resume_contract is not None:
            os.chmod(copied_prompt, 0o600)
    elif (
        copied_prompt.is_symlink()
        or not copied_prompt.is_file()
        or copied_prompt.read_text(encoding="utf-8") != prompt
    ):
        raise DispatchError("retained run prompt changed before resume")
    manifest = {
        "schema_version": 1,
        "phase_request": request,
        "raw_prompt_sha256": sha256_bytes(prompt_bytes),
        "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
        "cwd": str(cwd),
        "runtime": "pinned-openai-codex-app-server",
        "tool_surface": tool_surface,
        "context_files": context_records,
        "context_bundle": context_bundle,
        "runner_sha256": runner_hash,
        "profile_sha256": resolution["profile_sha256"],
        "permissions": {
            "approval_policy": "never",
            "sandbox": args.sandbox,
            "network_access": args.network_access,
            "mutation_authorized": args.mutation_authorized,
        },
        "service_tier": requested_service,
        "normalized_service_tier": selected_service_tier,
        "retry_rule": {"cognitive_retries": 0, "transient_retries": 0},
        "wall_time_cap_seconds": effective_wall_time_seconds,
        "requested_wall_time_ceiling_seconds": args.wall_time_seconds,
        "token_budget": token_budget,
        "token_cap_task_binding_sha256": content_hash(token_cap_task_binding),
        "dispatch_packet_sha256": (
            plan_binding["dispatch_packet"]["dispatch_packet_sha256"]
            if plan_binding
            else None
        ),
        "expected_exact_output": args.expect_exact_output,
    }
    if resume_contract is not None:
        manifest["resume_contract"] = resume_contract
    if initial_checkpoint is None:
        write_json(root / "input-manifest.json", manifest)
        write_json(root / "route-resolution.json", resolution)
        if plan_binding:
            write_json(root / "plan-binding.json", plan_binding)
        if resume_contract is not None:
            for retained in (
                root / "input-manifest.json",
                root / "route-resolution.json",
                root / "plan-binding.json",
            ):
                os.chmod(retained, 0o600)
    else:
        retained_manifest = read_json(
            (root / "input-manifest.json").resolve(strict=True),
            "retained input manifest",
        )
        if retained_manifest != manifest:
            raise DispatchError("retained input manifest changed before resume")
    started_at = time.monotonic()
    if resume_contract is not None:
        assert plan_binding is not None
        return _run_resumable_phase(
            args=args,
            root=root,
            request=request,
            resolution=resolution,
            active_policy=policy,
            runner=runner,
            runner_hash=runner_hash,
            profile=profile,
            prompt=prompt,
            requested=requested,
            token_budget=token_budget,
            token_cap=token_cap,
            manifest=manifest,
            plan_binding=plan_binding,
            started_at=started_at,
            initial_checkpoint=initial_checkpoint,
            initial_checkpoint_path=initial_checkpoint_path,
        )
    transcript = root / "app-server.jsonl"
    stderr_path = root / "app-server.stderr.log"
    server: AppServer | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    cap_interrupt_reason: str | None = None
    try:
        server = AppServer(runner, transcript, stderr_path)
        deadline = started_at + effective_wall_time_seconds
        server.send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "adaptive_workflow_dispatch",
                        "title": "Adaptive Workflow Dispatch",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        server.wait_for(lambda event: event.get("id") == 1, deadline)
        base_instructions = (
            "You are a bounded Codex workflow worker. Execute only the supplied phase, "
            "use the minimum necessary tools, preserve unrelated user work, verify the "
            "declared exit condition, and return concise evidence. Never expand scope, "
            "spawn another agent, or claim external authority."
        )
        developer_instructions = (
            profile["developer_instructions"].strip()
            + "\n\nThis route is bound to the supplied phase only. Do not spawn agents. "
            "Do not claim external authority. Stop and report any required scope expansion. "
            f"The derived phase budget allows at most {model_cycle_cap} "
            f"harness-started turn cycles, at most {tool_cycle_cap} tool cycles, "
            f"and at most {api_call_allowance} chargeable model API calls "
            "(the initial call plus follow-up calls after tool results), "
            f"with {token_cap} total tokens and "
            f"{effective_wall_time_seconds} seconds; minimize redundant tool loops."
        )
        if args.tool_mode == "none":
            developer_instructions += (
                " This is a strict no-tools phase. Use only the bound context in the user "
                "message and return the requested result in one response."
            )
        developer_instructions += local_mutation_tool_instructions(
            args.sandbox, args.tool_mode
        )
        thread_params: dict[str, Any] = {
            "model": resolution["model"],
            "modelProvider": resolution["provider"],
            "serviceTier": requested_service,
            "cwd": str(cwd),
            "runtimeWorkspaceRoots": [str(cwd)],
            "approvalPolicy": "never",
            "sandbox": args.sandbox,
            "ephemeral": True,
            "allowProviderModelFallback": False,
            "baseInstructions": base_instructions,
            "developerInstructions": developer_instructions,
        }
        if not args.network_access:
            thread_params.update(
                {
                    "dynamicTools": [],
                    "environments": [],
                    "selectedCapabilityRoots": [],
                }
            )
        server.send(
            {"id": 2, "method": "thread/start", "params": thread_params}
        )
        thread_response = server.wait_for(lambda event: event.get("id") == 2, deadline)
        thread_id = thread_response.get("result", {}).get("thread", {}).get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise DispatchError("App Server omitted the thread ID")
        server.send(
            {
                "id": 3,
                "method": "turn/start",
                "params": {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                    "model": resolution["model"],
                    "effort": resolution["effort"],
                    "serviceTier": requested_service,
                    "sandboxPolicy": sandbox_policy(
                        args.sandbox, cwd, args.network_access
                    ),
                },
            }
        )
        turn_response = server.wait_for(lambda event: event.get("id") == 3, deadline)
        turn_id = turn_response.get("result", {}).get("turn", {}).get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise DispatchError("App Server omitted the turn ID")
        interrupted = False
        # One ordinary dispatch owns exactly one harness-started model turn.
        # Reasoning items may fragment within it and are not separate cycles.
        observed_turn_cycle_ids: set[str] = {turn_id}
        observed_tool_cycle_ids: set[str] = set()
        observed_api_call_updates = 0
        highest_usage_total = -1
        while True:
            event = server.next_event(deadline)
            if event.get("method") == "item/started" and scoped_to_turn(
                event, thread_id, turn_id
            ):
                item = event.get("params", {}).get("item", {})
                item_type = item.get("type") if isinstance(item, dict) else None
                item_id = item.get("id") if isinstance(item, dict) else None
                if isinstance(item_id, str) and item_id:
                    if item_type not in NO_TOOLS_PASSIVE_ITEM_TYPES:
                        observed_tool_cycle_ids.add(item_id)
                if (
                    len(observed_turn_cycle_ids) > model_cycle_cap
                    and not interrupted
                ):
                    cap_interrupt_reason = (
                        "turn cycle cap exceeded: "
                        f"{len(observed_turn_cycle_ids)} > {model_cycle_cap}"
                    )
                    server.send(
                        {
                            "id": 4,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        }
                    )
                    interrupted = True
                if (
                    len(observed_tool_cycle_ids) > tool_cycle_cap
                    and not interrupted
                ):
                    cap_interrupt_reason = (
                        "tool cycle cap exceeded: "
                        f"{len(observed_tool_cycle_ids)} > {tool_cycle_cap}"
                    )
                    server.send(
                        {
                            "id": 4,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        }
                    )
                    interrupted = True
            if (
                args.tool_mode == "none"
                and no_tools_item_violation(event, thread_id, turn_id)
                and not interrupted
            ):
                item = event.get("params", {}).get("item", {})
                item_type = item.get("type") if isinstance(item, dict) else None
                cap_interrupt_reason = (
                    "tool use observed under the no-tools contract: "
                    + (item_type if isinstance(item_type, str) else "<missing>")
                )
                server.send(
                    {
                        "id": 4,
                        "method": "turn/interrupt",
                        "params": {"threadId": thread_id, "turnId": turn_id},
                    }
                )
                interrupted = True
            if (
                event.get("method") == "thread/tokenUsage/updated"
                and scoped_to_turn(event, thread_id, turn_id)
            ):
                measured = (
                    event.get("params", {})
                    .get("tokenUsage", {})
                    .get("total", {})
                    .get("totalTokens")
                )
                if type(measured) is int and measured > highest_usage_total:
                    highest_usage_total = measured
                    observed_api_call_updates += 1
                if (
                    observed_api_call_updates > api_call_allowance
                    and not interrupted
                ):
                    cap_interrupt_reason = (
                        "model API-call allowance exceeded: "
                        f"{observed_api_call_updates} > {api_call_allowance}"
                    )
                    server.send(
                        {
                            "id": 4,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        }
                    )
                    interrupted = True
                if type(measured) is int and measured > token_cap and not interrupted:
                    cap_interrupt_reason = f"token cap exceeded: {measured} > {token_cap}"
                    server.send(
                        {
                            "id": 4,
                            "method": "turn/interrupt",
                            "params": {"threadId": thread_id, "turnId": turn_id},
                        }
                    )
                    interrupted = True
            if (
                event.get("method") == "turn/completed"
                and scoped_to_turn(event, thread_id, turn_id)
            ):
                break
    except Exception as error:
        if server:
            server.close()
            server = None
        aborted = {
            "schema_version": 1,
            "status": "ABORTED",
            "reason": str(error),
            "error_type": type(error).__name__,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "route": resolution,
            "input_manifest_sha256": sha256_bytes(
                (root / "input-manifest.json").read_bytes()
            ),
            "transcript_path": str(transcript),
            "transcript_sha256": (
                sha256_bytes(transcript.read_bytes()) if transcript.is_file() else None
            ),
            "stderr_path": str(stderr_path),
            "stderr_sha256": (
                sha256_bytes(stderr_path.read_bytes())
                if stderr_path.is_file()
                else None
            ),
        }
        write_json(root / "aborted.json", aborted)
        print(json.dumps(aborted, indent=2, sort_keys=True))
        return 2
    finally:
        if server:
            server.close()
    try:
        events = transcript_events(transcript)
        runtime, issues = runtime_record(
            events,
            thread_id=thread_id,
            turn_id=turn_id,
            requested=requested,
            token_cap=token_cap,
            model_cycle_cap=model_cycle_cap,
            tool_cycle_cap=tool_cycle_cap,
            model_api_call_allowance=api_call_allowance,
            tool_mode=args.tool_mode,
            require_tool_use=(
                request.get("mutation") != "none"
                and args.sandbox == "workspace-write"
                and args.tool_mode == "default"
            ),
        )
    except Exception as error:
        aborted = {
            "schema_version": 1,
            "status": "ABORTED",
            "reason": str(error),
            "error_type": type(error).__name__,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "route": resolution,
            "transcript_path": str(transcript),
            "transcript_sha256": (
                sha256_bytes(transcript.read_bytes()) if transcript.is_file() else None
            ),
        }
        write_json(root / "aborted.json", aborted)
        print(json.dumps(aborted, indent=2, sort_keys=True))
        return 2
    if cap_interrupt_reason and cap_interrupt_reason not in issues:
        issues.append(cap_interrupt_reason)
    if (
        args.expect_exact_output is not None
        and (runtime.get("output_text") or "").strip() != args.expect_exact_output
    ):
        issues.append("final output did not match the deterministic smoke expectation")
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    if elapsed_ms > effective_wall_time_seconds * 1000:
        issues.append(
            "wall-time cap exceeded: "
            f"{elapsed_ms} > {effective_wall_time_seconds * 1000}"
        )
    transcript_hash = sha256_bytes(transcript.read_bytes())
    version = subprocess.run(
        [str(runner), "--version"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    ).stdout.strip()
    final_status = execution_status(issues)
    metadata = {
        "schema_version": 1,
        "source": "openai-codex-app-server-workflow",
        "evidence_use": "ordinary-workflow-observed-identity",
        "model_promotion_eligible": False,
        "status": final_status,
        "policy_id": resolution["policy_id"],
        "tier": resolution["tier"],
        "profile_sha256": resolution["profile_sha256"],
        "input_manifest_sha256": sha256_bytes(
            (root / "input-manifest.json").read_bytes()
        ),
        "runner": {"path": str(runner), "sha256": runner_hash, "version": version},
        "server": {
            **runtime,
            "transcript_path": str(transcript),
            "transcript_sha256": transcript_hash,
        },
        "limits": {
            "token_cap": token_cap,
            "model_cycle_cap": model_cycle_cap,
            "turn_cycle_cap": model_cycle_cap,
            "tool_cycle_cap": tool_cycle_cap,
            "model_api_call_allowance": api_call_allowance,
            "token_budget": token_budget,
            "wall_time_seconds": effective_wall_time_seconds,
            "requested_wall_time_ceiling_seconds": args.wall_time_seconds,
            "elapsed_ms": elapsed_ms,
        },
        "issues": issues,
    }
    write_json(root / "execution-metadata.json", metadata)
    receipt: dict[str, Any] | None = None
    if not issues:
        try:
            completed_turns = [
                event.get("params", {}).get("turn", {})
                for event in events
                if event.get("method") == "turn/completed"
                and scoped_to_turn(event, thread_id, turn_id)
            ]
            duration_ms = (
                completed_turns[0].get("durationMs")
                if len(completed_turns) == 1
                else None
            )
            if type(duration_ms) is not int or duration_ms <= 0:
                raise DispatchError("completed turn omitted its measured duration")
            metadata_path = (root / "execution-metadata.json").resolve()
            input_manifest_path = (root / "input-manifest.json").resolve()
            receipt = append_execution_receipt(
                {
                    "metadata_path": str(metadata_path),
                    "metadata_sha256": sha256_bytes(metadata_path.read_bytes()),
                    "transcript_path": str(transcript.resolve()),
                    "transcript_sha256": transcript_hash,
                    "input_manifest_path": str(input_manifest_path),
                    "input_manifest_sha256": sha256_bytes(
                        input_manifest_path.read_bytes()
                    ),
                    "runner": {
                        "path": str(runner),
                        "sha256": runner_hash,
                        "version": version,
                    },
                    "profile_sha256": resolution["profile_sha256"],
                    "dispatch_packet_sha256": manifest.get(
                        "dispatch_packet_sha256"
                    ),
                    "thread_id": runtime["thread_id"],
                    "turn_id": runtime["turn_id"],
                    "completed_agent_message_ids": runtime[
                        "completed_agent_message_ids"
                    ],
                    "requested_identity": {
                        "provider": runtime["requested_provider"],
                        "model": runtime["requested_model"],
                        "effort": runtime["requested_effort"],
                        "service_tier": runtime["requested_service_tier"],
                    },
                    "observed_identity": {
                        "provider": runtime["observed_provider"],
                        "model": runtime["observed_model"],
                        "effort": runtime["observed_effort"],
                        "service_tier": runtime["observed_service_tier"],
                    },
                    "usage": {
                        **runtime["usage"],
                        "total_tokens": runtime["total_tokens"],
                        "model_cycles": runtime["model_cycles"],
                        "turn_cycles": runtime["turn_cycles"],
                        "tool_cycles": runtime["tool_cycles"],
                        "model_api_call_updates": runtime[
                            "model_api_call_updates"
                        ],
                    },
                    "duration_ms": duration_ms,
                    "accepted_usage": {
                        "workflow_id": request.get("workflow_id"),
                        "workflow_version": request.get("workflow_version"),
                        "phase_id": request.get("phase_id"),
                        "activity": request.get("activity"),
                        "model": runtime["observed_model"],
                        "effort": runtime["observed_effort"],
                        "tool_mode": args.tool_mode,
                        "total_tokens": runtime["total_tokens"],
                        "turn_cycles": runtime["turn_cycles"],
                        "tool_cycles": runtime["tool_cycles"],
                        "model_api_call_updates": runtime[
                            "model_api_call_updates"
                        ],
                        "duration_ms": duration_ms,
                    },
                    "final_output_sha256": sha256_bytes(
                        (runtime.get("output_text") or "").encode("utf-8")
                    ),
                    "status": "COMPLETED",
                    "issues": [],
                    **workflow_receipt_fields(args, status="COMPLETED", issues=[]),
                }
            )
            write_json(
                root / "execution-receipt.json",
                {
                    "schema_version": 1,
                    "registry_path": str(EXECUTION_REGISTRY),
                    "registry_sha256": sha256_bytes(EXECUTION_REGISTRY.read_bytes()),
                    "receipt_id": receipt["receipt_id"],
                    "receipt_sha256": receipt["receipt_sha256"],
                },
            )
        except Exception as error:
            aborted = {
                "schema_version": 1,
                "status": "ABORTED",
                "category": "EVIDENCE_REGISTRY_FAILURE",
                "reason": str(error),
                "execution_metadata_path": str(
                    (root / "execution-metadata.json").resolve()
                ),
                "execution_metadata_sha256": sha256_bytes(
                    (root / "execution-metadata.json").read_bytes()
                ),
                "transcript_path": str(transcript),
                "transcript_sha256": transcript_hash,
            }
            write_json(root / "aborted.json", aborted)
            print(json.dumps(aborted, indent=2, sort_keys=True))
            return 2
    if issues:
        write_json(
            root / "aborted.json",
            {
                "schema_version": 1,
                "status": "ABORTED",
                "category": final_status,
                "reasons": issues,
                "execution_metadata_sha256": sha256_bytes(
                    (root / "execution-metadata.json").read_bytes()
                ),
                "transcript_path": str(transcript),
                "transcript_sha256": transcript_hash,
            },
        )
    printed = dict(metadata)
    if receipt is not None:
        printed["dispatcher_receipt_id"] = receipt["receipt_id"]
        printed["dispatcher_receipt_sha256"] = receipt["receipt_sha256"]
    print(json.dumps(printed, indent=2, sort_keys=True))
    return 0 if not issues else 2


def check_health() -> int:
    issues: list[str] = []
    details: dict[str, Any] = {"model_invocations": 0}
    try:
        status = router_status()
        policy = read_json(POLICY, "active model policy")
        request = {
            "workflow_id": "adaptive-workflow.health",
            "workflow_version": 1,
            "phase_id": "bounded_inspection",
            "activity": "inspect",
            "mutation": "none",
            "scope": "local",
            "ambiguity": "none",
            "risk_level": "low",
            "risk_scope": "evidence",
            "risk_tags": [],
            "visual_required": False,
            "current_info_required": False,
            "external_action": False,
        }
        resolution = resolve_phase(request)
        validate_resolution(resolution)
        runner, runner_hash, _ = validate_policy(policy, status, resolution)
        profile_checks: dict[str, Any] = {}
        for tier in sorted(MODEL_TIERS):
            assignment = policy.get("tiers", {}).get(tier)
            if not isinstance(assignment, dict):
                raise DispatchError(f"active policy omitted {tier}")
            profile_file = assignment.get("profile_file")
            if not isinstance(profile_file, str) or Path(profile_file).name != profile_file:
                raise DispatchError(f"active policy has an unsafe {tier} profile")
            profile_path = AGENTS / profile_file
            if profile_path.is_symlink() or not profile_path.is_file():
                raise DispatchError(f"active {tier} profile is missing or unsafe")
            profile = tomllib.loads(profile_path.read_text(encoding="utf-8"))
            if (
                profile.get("name") != assignment.get("agent")
                or profile.get("model") != assignment.get("model")
                or profile.get("model_reasoning_effort") != assignment.get("effort")
            ):
                raise DispatchError(f"active {tier} profile identity drifted")
            profile_checks[tier] = {
                "agent": assignment.get("agent"),
                "model": assignment.get("model"),
                "effort": assignment.get("effort"),
                "sandbox_mode": profile.get("sandbox_mode", "workspace-write"),
                "profile_sha256": sha256_bytes(profile_path.read_bytes()),
            }

        critical_mutation_request = {
            "workflow_id": "adaptive-workflow.health",
            "workflow_version": 1,
            "phase_id": "critical_mutation_capability",
            "activity": "implement",
            "mutation": "reversible",
            "scope": "cross_system",
            "ambiguity": "novel",
            "risk_level": "critical",
            "risk_scope": "mutation",
            "risk_tags": ["build"],
            "visual_required": False,
            "current_info_required": False,
            "external_action": False,
        }
        critical_mutation_resolution = resolve_phase(critical_mutation_request)
        validate_resolution(critical_mutation_resolution)
        validate_policy(policy, status, critical_mutation_resolution)
        _, critical_mutation_profile = safe_profile(
            policy, critical_mutation_resolution
        )
        critical_mutation_args = argparse.Namespace(
            sandbox="workspace-write",
            network_access=False,
            tool_mode="default",
            mutation_authorized=True,
        )
        validate_authority(
            critical_mutation_args,
            critical_mutation_request,
            critical_mutation_resolution,
            critical_mutation_profile,
        )
        details["critical_mutation_capability"] = {
            "tier": critical_mutation_resolution["tier"],
            "agent": critical_mutation_resolution["agent"],
            "profile_sandbox_mode": critical_mutation_profile.get(
                "sandbox_mode", "workspace-write"
            ),
            "status": "VALIDATED",
        }
        planner_validate = subprocess.run(
            [sys.executable, str(PLANNER), "validate"],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        planner_plan = subprocess.run(
            [
                sys.executable,
                str(PLANNER),
                "plan",
                "--router",
                str(ROUTER),
                "--application",
                "coding",
                "--objective",
                "adaptive workflow health gate",
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if planner_validate.returncode != 0:
            issues.append(planner_validate.stderr.strip() or "planner validation failed")
        if planner_plan.returncode != 0:
            issues.append(planner_plan.stderr.strip() or "planner route binding failed")
        else:
            plan = json.loads(planner_plan.stdout)
            if plan.get("execution_identity") != "REQUESTED_ROUTES_PENDING_RUNTIME_EVIDENCE":
                issues.append("planner emitted a stale execution identity")
            dispatcher_plan_bindings = 0
            nonactive_route_rejections = 0
            plan_file_sha256: str | None = None
            with tempfile.TemporaryDirectory(
                prefix="adaptive-workflow-health-plan-"
            ) as temporary:
                plan_path = Path(temporary) / "plan.json"
                write_json(plan_path, plan)
                plan_file_sha256 = sha256_bytes(plan_path.read_bytes())
                for phase in plan.get("phases", []):
                    planned = phase.get("route_resolution")
                    if not isinstance(planned, dict) or planned.get("mode") != "model":
                        continue
                    phase_args = argparse.Namespace(
                        plan=plan_path,
                        phase_key=phase.get("phase_key"),
                        request=None,
                        dispatch_packet=None,
                    )
                    if phase.get("activation") != "active":
                        try:
                            load_route(phase_args, require_dispatch_packet=False)
                        except DispatchError as error:
                            if "not an active routed phase" not in str(error):
                                raise
                            nonactive_route_rejections += 1
                        else:
                            raise DispatchError(
                                "dispatcher accepted a non-active planned phase: "
                                f"{phase.get('phase_key')}"
                            )
                        continue
                    phase_request, binding = load_route(
                        phase_args, require_dispatch_packet=False
                    )
                    fresh = resolve_phase(phase_request)
                    validate_resolution(fresh)
                    if not binding or any(
                        planned.get(field) != fresh.get(field)
                        for field in PLAN_RESOLUTION_FIELDS
                    ):
                        raise DispatchError(
                            f"dispatcher rejected planner binding: {phase.get('phase_key')}"
                        )
                    validate_policy(policy, status, fresh)
                    _, phase_profile = safe_profile(policy, fresh)
                    phase_mutates = phase_request.get("mutation", "none") != "none"
                    phase_network = fresh.get("web_required") is True
                    packet_args = argparse.Namespace(
                        sandbox="workspace-write" if phase_mutates else "read-only",
                        network_access=phase_network,
                        tool_mode=(
                            "default" if phase_mutates or phase_network else "none"
                        ),
                        mutation_authorized=phase_mutates,
                        wall_time_seconds=60,
                    )
                    validate_authority(
                        packet_args,
                        phase_request,
                        fresh,
                        phase_profile,
                    )
                    packet = expected_dispatch_packet(
                        binding,
                        prompt_sha256=sha256_bytes(b"workflow dispatch health gate"),
                        context_records=[],
                        cwd=SKILL_DIR,
                        args=packet_args,
                    )
                    validate_dispatch_packet(packet, packet)
                    dispatcher_plan_bindings += 1
            details["dispatcher_plan_bindings_validated"] = dispatcher_plan_bindings
            details["dispatch_packets_validated"] = dispatcher_plan_bindings
            details["nonactive_route_rejections_validated"] = (
                nonactive_route_rejections
            )
            details["trusted_plan_authority_verifications"] = (
                1 if plan_file_sha256 in VERIFIED_PLAN_CACHE else 0
            )

        release_plan = subprocess.run(
            [
                sys.executable,
                str(PLANNER),
                "plan",
                "--router",
                str(ROUTER),
                "--application",
                "operations.release",
                "--objective",
                "validate a critical novel release before parent execution",
                "--scope",
                "cross_system",
                "--ambiguity",
                "novel",
                "--mutation",
                "reversible",
                "--risk-level",
                "critical",
                "--risk-scope",
                "judgment",
            ],
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if release_plan.returncode != 0:
            issues.append(
                release_plan.stderr.strip() or "release dry-run planning failed"
            )
        else:
            release_value = json.loads(release_plan.stdout)
            dry_run = next(
                (
                    phase
                    for phase in release_value.get("phases", [])
                    if phase.get("phase_key") == "operations.release:dry_run"
                ),
                None,
            )
            if not isinstance(dry_run, dict):
                raise DispatchError("release plan omitted operations.release:dry_run")
            dry_run_request = dry_run.get("route_request")
            dry_run_resolution = dry_run.get("route_resolution")
            if not isinstance(dry_run_request, dict) or not isinstance(
                dry_run_resolution, dict
            ):
                raise DispatchError("release dry-run omitted its route binding")
            if dry_run_request.get("mutation") != "none":
                raise DispatchError(
                    "operations.release:dry_run must remain read-only even when the overall release mutates"
                )
            validate_resolution(dry_run_resolution)
            validate_policy(policy, status, dry_run_resolution)
            _, dry_run_profile = safe_profile(policy, dry_run_resolution)
            dry_run_args = argparse.Namespace(
                sandbox="read-only",
                network_access=False,
                tool_mode="none",
                mutation_authorized=False,
                wall_time_seconds=60,
            )
            validate_authority(
                dry_run_args,
                dry_run_request,
                dry_run_resolution,
                dry_run_profile,
            )
            with tempfile.TemporaryDirectory(
                prefix="adaptive-workflow-release-health-plan-"
            ) as temporary:
                release_plan_path = Path(temporary) / "release-plan.json"
                write_json(release_plan_path, release_value)
                phase_args = argparse.Namespace(
                    plan=release_plan_path,
                    phase_key="operations.release:dry_run",
                    request=None,
                    dispatch_packet=None,
                )
                bound_request, release_binding = load_route(
                    phase_args, require_dispatch_packet=False
                )
                if bound_request != dry_run_request or not release_binding:
                    raise DispatchError(
                        "release dry-run planner authority binding changed"
                    )
                packet = expected_dispatch_packet(
                    release_binding,
                    prompt_sha256=sha256_bytes(
                        b"workflow release dry-run dispatch health gate"
                    ),
                    context_records=[],
                    cwd=SKILL_DIR,
                    args=dry_run_args,
                )
                validate_dispatch_packet(packet, packet)
                details["dispatcher_plan_bindings_validated"] = (
                    details.get("dispatcher_plan_bindings_validated", 0) + 1
                )
                details["dispatch_packets_validated"] = (
                    details.get("dispatch_packets_validated", 0) + 1
                )
            details["release_dry_run_contract"] = {
                "workflow_version": dry_run.get("workflow_version"),
                "tier": dry_run_resolution.get("tier"),
                "agent": dry_run_resolution.get("agent"),
                "mutation": dry_run_request.get("mutation"),
                "profile_sandbox_mode": dry_run_profile.get(
                    "sandbox_mode", "workspace-write"
                ),
                "dispatch_packet_sha256": packet["dispatch_packet_sha256"],
                "status": "VALIDATED",
            }
        details.update(
            {
                "policy_id": status.get("policy_id"),
                "model_version_window": status.get("model_version_window"),
                "tiers": status.get("tiers"),
                "execution_evidence_mode": status.get("execution_evidence_mode"),
                "execution_evidence_ready": status.get("execution_evidence_ready"),
                "runner": {"path": str(runner), "sha256": runner_hash},
                "profiles": profile_checks,
                "planner_validate": planner_validate.returncode == 0,
                "planner_route_binding": planner_plan.returncode == 0,
                "generic_spawn_enforces_route": False,
                "required_model_dispatch": "pinned-openai-codex-app-server",
                "ordinary_execution_model_promotion_eligible": False,
            }
        )
    except Exception as error:
        issues.append(str(error))
    report = {
        "schema_version": 1,
        "status": "HEALTHY" if not issues else "DEGRADED",
        "workflow_dispatch_ready": not issues,
        **details,
        "issues": issues,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not issues else 2


def smoke_request(tier: str) -> dict[str, Any]:
    route_facts = {
        "T1": {
            "activity": "inspect",
            "scope": "local",
            "ambiguity": "none",
            "risk_level": "low",
            "risk_scope": "evidence",
        },
        "T2": {
            "activity": "review",
            "scope": "local",
            "ambiguity": "none",
            "risk_level": "low",
            "risk_scope": "judgment",
        },
        "T3": {
            "activity": "adjudicate",
            "scope": "multi_file",
            "ambiguity": "bounded",
            "risk_level": "high",
            "risk_scope": "judgment",
        },
        "T4": {
            "activity": "architecture",
            "scope": "cross_system",
            "ambiguity": "novel",
            "risk_level": "critical",
            "risk_scope": "judgment",
        },
    }[tier]
    return {
        "workflow_id": "adaptive-workflow.live-smoke",
        "workflow_version": 1,
        "phase_id": f"{tier.lower()}_observed_identity",
        "mutation": "none",
        "risk_tags": [],
        "visual_required": False,
        "current_info_required": False,
        "external_action": False,
        **route_facts,
    }


def run_smoke(args: argparse.Namespace) -> int:
    tier = args.tier
    if tier == "rotating":
        tiers = ("T1", "T2", "T3", "T4")
        tier = tiers[dt.datetime.now(dt.timezone.utc).date().toordinal() % len(tiers)]
    with tempfile.TemporaryDirectory(prefix="adaptive-workflow-smoke-") as temporary:
        root = Path(temporary)
        request_path = root / "request.json"
        prompt_path = root / "prompt.txt"
        write_json(request_path, smoke_request(tier))
        prompt_path.write_text(
            "Return exactly ROUTE_OK with no punctuation, formatting, commentary, or tool use.\n",
            encoding="utf-8",
        )
        run_args = argparse.Namespace(
            plan=None,
            phase_key=None,
            request=request_path,
            prompt_file=prompt_path,
            cwd=SKILL_DIR,
            sandbox="read-only",
            network_access=False,
            mutation_authorized=False,
            token_cap=args.token_cap,
            token_cap_contract=args.token_cap_contract,
            model_cycle_cap=None,
            tool_cycle_cap=0,
            budget_increase_contract=None,
            context_file=[],
            tool_mode="none",
            wall_time_seconds=args.wall_time_seconds,
            output_dir=None,
            expect_exact_output="ROUTE_OK",
        )
        return run_phase(run_args)


def registry_receipt(receipt_id: str) -> dict[str, Any]:
    if not receipt_id:
        raise DispatchError("producer receipt ID is required")
    EXECUTION_REGISTRY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with EXECUTION_REGISTRY_LOCK.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        _recover_receipt_registry_locked()
        if EXECUTION_REGISTRY.is_symlink() or not EXECUTION_REGISTRY.is_file():
            raise DispatchError("execution receipt registry is unavailable")
        matches = [
            item
            for item in _decode_receipt_registry(EXECUTION_REGISTRY.read_bytes())
            if item.get("receipt_id") == receipt_id
        ]
    if len(matches) != 1:
        raise DispatchError("producer receipt is missing or ambiguous")
    return matches[0]


def register_quality(args: argparse.Namespace) -> int:
    quality_path = args.quality_artifact.expanduser().resolve()
    quality = read_json(quality_path, "quality artifact")
    if quality.get("schema_version") != 1:
        raise DispatchError("quality artifact schema is unsupported")
    grader_kind = quality.get("grader_kind")
    if grader_kind not in {"deterministic", "blind_independent"}:
        raise DispatchError(
            "quality registration supports pinned deterministic or registered blind-independent graders"
        )
    grader_identity = quality.get("grader_identity_sha256")
    rubric_hash = quality.get("rubric_sha256")
    case_id = quality.get("case_id")
    evaluated_arm = quality.get("evaluated_arm")
    evaluated_receipts = quality.get("evaluated_execution_receipt_ids")
    if (
        not isinstance(grader_identity, str)
        or not HEX_SHA256.fullmatch(grader_identity)
        or not isinstance(rubric_hash, str)
        or not HEX_SHA256.fullmatch(rubric_hash)
        or not isinstance(case_id, str)
        or not case_id
        or evaluated_arm not in {"control", "challenger"}
        or not isinstance(evaluated_receipts, list)
        or not evaluated_receipts
        or evaluated_receipts != sorted(set(evaluated_receipts))
        or not all(isinstance(item, str) and item for item in evaluated_receipts)
    ):
        raise DispatchError("quality artifact omitted its grader, rubric, case, arm, or execution binding")
    for receipt_id in evaluated_receipts:
        producer_execution = registry_receipt(receipt_id)
        if producer_execution.get("receipt_type") != "execution":
            raise DispatchError("quality artifact references a non-execution receipt")
    quality_hash = sha256_bytes(quality_path.read_bytes())
    payload: dict[str, Any] = {
        "quality_artifact_path": str(quality_path),
        "quality_artifact_sha256": quality_hash,
        "grader_kind": grader_kind,
        "grader_identity_sha256": grader_identity,
        "rubric_sha256": rubric_hash,
        "case_id": case_id,
        "evaluated_arm": evaluated_arm,
        "evaluated_execution_receipt_ids": evaluated_receipts,
    }
    if grader_kind == "deterministic":
        if args.grader_executable is None or args.grader_input is None or args.producer_receipt_id:
            raise DispatchError(
                "deterministic quality requires --grader-executable and --grader-input only"
            )
        executable = args.grader_executable.expanduser().resolve()
        grader_input = args.grader_input.expanduser().resolve()
        if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
            raise DispatchError("deterministic grader executable is missing or unsafe")
        if executable != DETERMINISTIC_GRADER.resolve():
            raise DispatchError("deterministic quality must use the bundled closed grader")
        if grader_input.is_symlink() or not grader_input.is_file():
            raise DispatchError("deterministic grader input is missing or unsafe")
        executable_hash = sha256_bytes(executable.read_bytes())
        if executable_hash != grader_identity:
            raise DispatchError("deterministic grader executable hash differs from its identity")
        completed = subprocess.run(
            [str(executable), *args.grader_arg],
            input=grader_input.read_bytes(),
            capture_output=True,
            timeout=args.grader_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise DispatchError(
                f"deterministic grader failed with exit {completed.returncode}"
            )
        try:
            observed_quality = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise DispatchError("deterministic grader did not emit one JSON object") from error
        if observed_quality != quality:
            raise DispatchError("deterministic grader output differs from the retained quality artifact")
        payload.update(
            {
                "producer_kind": "pinned-deterministic-grader",
                "producer_path": str(executable),
                "producer_sha256": executable_hash,
                "grader_input_path": str(grader_input),
                "grader_input_sha256": sha256_bytes(grader_input.read_bytes()),
                "producer_stdout_sha256": sha256_bytes(completed.stdout),
            }
        )
    else:
        if args.producer_receipt_id is None or args.grader_executable or args.grader_input:
            raise DispatchError(
                "blind-independent quality requires only --producer-receipt-id"
            )
        producer = registry_receipt(args.producer_receipt_id)
        if producer.get("receipt_type") != "execution":
            raise DispatchError("blind grader producer is not an execution receipt")
        metadata = read_json(Path(producer["metadata_path"]), "grader execution metadata")
        try:
            observed_quality = json.loads(metadata.get("server", {}).get("output_text", ""))
        except json.JSONDecodeError as error:
            raise DispatchError("blind grader output is not strict JSON") from error
        if observed_quality != quality or quality.get("blind") is not True:
            raise DispatchError("blind grader output differs from the retained blind quality artifact")
        identity = content_hash(
            {
                "observed_identity": producer["observed_identity"],
                "profile_sha256": producer["profile_sha256"],
            }
        )
        if identity != grader_identity:
            raise DispatchError("blind grader execution differs from the bound grader identity")
        payload.update(
            {
                "producer_kind": "registered-blind-independent-grader",
                "producer_receipt_id": producer["receipt_id"],
                "producer_output_sha256": producer["final_output_sha256"],
            }
        )
    receipt = append_quality_receipt(payload)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def resume_phase(args: argparse.Namespace) -> int:
    checkpoint_path = args.checkpoint.expanduser().absolute()
    checkpoint, _ = load_resume_checkpoint(checkpoint_path)
    binding = checkpoint.get("binding", {})
    resume_contract = binding.get("resume_contract")
    if not isinstance(resume_contract, dict):
        raise DispatchError("resume checkpoint omitted its authority contract")
    root = checkpoint_path.parent.resolve(strict=True)
    if (root / "execution-receipt.json").exists():
        raise DispatchError("resume run already has a final execution receipt")
    namespace = argparse.Namespace(
        command="run",
        request=None,
        plan=Path(binding["plan_path"]),
        phase_key=binding["phase_key"],
        dispatch_packet=Path(binding["dispatch_packet_path"]),
        prompt_file=Path(binding["prompt_path"]),
        context_file=[Path(item["path"]) for item in binding["context_files"]],
        cwd=Path(binding["cwd"]),
        sandbox=binding["permissions"]["sandbox"],
        network_access=binding["permissions"]["network_access"],
        tool_mode="none",
        mutation_authorized=binding["permissions"]["mutation_authorized"],
        token_cap=None,
        token_cap_contract=None,
        model_cycle_cap=None,
        tool_cycle_cap=None,
        budget_increase_contract=None,
        wall_time_seconds=resume_contract["attempt_wall_time_seconds"],
        output_dir=root,
        expect_exact_output=None,
        resumable=True,
        work_manifest=Path(resume_contract["work_manifest"]["path"]),
        max_continuations=resume_contract["max_continuations"],
        cumulative_wall_time_seconds=resume_contract[
            "cumulative_wall_time_seconds"
        ],
        checkpoint_window_seconds=resume_contract[
            "checkpoint_window_seconds"
        ],
        shutdown_window_seconds=resume_contract["shutdown_window_seconds"],
        checkpoint_ttl_seconds=resume_contract["checkpoint_ttl_seconds"],
        resume_checkpoint=checkpoint_path,
    )
    try:
        return run_phase(namespace)
    except Exception as error:
        failure = {
            "schema_version": 1,
            "status": "ABORTED",
            "category": "RESUME_SETUP_FAILURE",
            "reason": str(error),
            "error_type": type(error).__name__,
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        }
        _atomic_private_json(
            root
            / (
                "resume-setup-failure."
                + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + ".json"
            ),
            failure,
        )
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = DispatchArgumentParser(description=__doc__)
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=DispatchArgumentParser
    )
    commands.add_parser("check", help="Run deterministic planner/dispatcher health gates")
    skill_read = commands.add_parser(
        "skill-read", help="Record a fresh, hash-bound read of a required skill"
    )
    skill_read.add_argument("--name", required=True)
    skill_read.add_argument("--skill", type=Path, required=True)
    skill_read.add_argument("--source-commit", required=True)
    skill_read.add_argument("--max-age-seconds", type=int, default=3600)
    smoke = commands.add_parser("smoke", help="Run one observed-identity live route smoke")
    smoke.add_argument("--tier", choices=("T1", "T2", "T3", "T4", "rotating"), default="rotating")
    smoke.add_argument(
        "--token-cap",
        type=int,
        help="Optional explicit override; otherwise derive from the smoke contract",
    )
    smoke.add_argument(
        "--token-cap-contract",
        type=Path,
        help="Hashed fixed-task contract JSON that binds --token-cap",
    )
    smoke.add_argument("--wall-time-seconds", type=int, default=180)
    quality = commands.add_parser(
        "register-quality",
        help="Register a pinned deterministic or receipt-bound blind quality result",
    )
    quality.add_argument("--quality-artifact", type=Path, required=True)
    quality.add_argument("--grader-executable", type=Path)
    quality.add_argument("--grader-input", type=Path)
    quality.add_argument("--grader-arg", action="append", default=[])
    quality.add_argument("--grader-timeout-seconds", type=int, default=300)
    quality.add_argument("--producer-receipt-id")
    resume = commands.add_parser(
        "resume",
        help="Consume one sealed explicit checkpoint with no runtime overrides",
    )
    resume.add_argument("--checkpoint", type=Path, required=True)
    run = commands.add_parser("run", help="Execute one active T1-T4 route")
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--request", type=Path)
    source.add_argument("--plan", type=Path)
    run.add_argument("--phase-key")
    run.add_argument(
        "--dispatch-packet",
        type=Path,
        help="Planner-generated packet binding a planned phase to exact inputs",
    )
    run.add_argument("--prompt-file", type=Path, required=True)
    run.add_argument("--context-file", type=Path, action="append", default=[])
    run.add_argument("--cwd", type=Path, required=True)
    run.add_argument(
        "--sandbox",
        choices=("read-only", "workspace-write"),
        default="read-only",
    )
    run.add_argument("--network-access", action="store_true")
    run.add_argument(
        "--tool-mode", choices=("default", "none"), default="default"
    )
    run.add_argument("--mutation-authorized", action="store_true")
    run.add_argument("--token-cap", type=int)
    run.add_argument(
        "--token-cap-contract",
        type=Path,
        help="Hashed fixed-task contract JSON that binds --token-cap",
    )
    run.add_argument(
        "--model-cycle-cap",
        type=int,
        help="Optional cap at or below the measured envelope",
    )
    run.add_argument(
        "--tool-cycle-cap",
        type=int,
        help="Optional cap at or below the measured envelope",
    )
    run.add_argument(
        "--budget-increase-contract",
        type=Path,
        help="Reviewed task-bound contract required to exceed a measured limit",
    )
    run.add_argument("--wall-time-seconds", type=int, default=900)
    run.add_argument("--resumable", action="store_true")
    run.add_argument("--work-manifest", type=Path)
    run.add_argument("--max-continuations", type=int)
    run.add_argument("--cumulative-wall-time-seconds", type=int)
    run.add_argument("--checkpoint-window-seconds", type=int)
    run.add_argument("--shutdown-window-seconds", type=int)
    run.add_argument("--checkpoint-ttl-seconds", type=int)
    run.add_argument("--output-dir", type=Path)
    run.add_argument("--tree-id")
    run.add_argument(
        "--agent-role", choices=sorted(governance.LEASE_ROLES), default="worker"
    )
    run.add_argument("--parent-lease-id")
    run.add_argument("--nested-capacity-tokens", type=int, default=0)
    run.add_argument("--lease-seconds", type=int, default=900)
    run.add_argument("--expect-exact-output", help=argparse.SUPPRESS)
    return parser


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

    if command not in {"run", "resume", "smoke"}:
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
        raise DispatchError("global harness installation journal is unreadable") from error
    if not isinstance(journal, dict):
        raise DispatchError("global harness installation journal is malformed")
    supplied_self_hash = journal.get("self_sha256")
    bare = dict(journal)
    bare.pop("self_sha256", None)
    if supplied_self_hash != sha256_bytes(canonical_json(bare).encode("utf-8")):
        raise DispatchError("global harness installation journal checksum mismatch")
    expected_token_hash = journal.get("verification_token_sha256")
    environment = os.environ if environ is None else environ
    supplied_token = environment.get(INSTALL_VERIFY_TOKEN_ENV)
    if (
        isinstance(expected_token_hash, str)
        and HEX_SHA256.fullmatch(expected_token_hash)
        and isinstance(supplied_token, str)
        and supplied_token
        and hmac.compare_digest(
            hashlib.sha256(supplied_token.encode("utf-8")).hexdigest(),
            expected_token_hash,
        )
    ):
        return
    raise DispatchError(
        "global adaptive-workflow-router installation is in progress; "
        "run the installer recovery command before dispatching"
    )


def main() -> int:
    parser = build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args()
        enforce_install_admission(args.command)
        if args.command == "check":
            return check_health()
        if args.command == "skill-read":
            receipt = governance.record_skill_read(
                name=args.name,
                path=args.skill,
                source_commit=args.source_commit,
                max_age_seconds=args.max_age_seconds,
            )
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0
        if args.command == "smoke":
            if (args.token_cap is not None and args.token_cap < 1) or args.wall_time_seconds < 1:
                parser.error("smoke caps must be positive")
            if bool(args.token_cap is not None) != bool(args.token_cap_contract):
                parser.error("--token-cap and --token-cap-contract must be supplied together")
            return run_smoke(args)
        if args.command == "register-quality":
            if args.grader_timeout_seconds < 1:
                parser.error("--grader-timeout-seconds must be positive")
            return register_quality(args)
        if args.command == "resume":
            return resume_phase(args)
        if bool(args.plan) != bool(args.phase_key):
            parser.error("--plan and --phase-key must be supplied together")
        if bool(args.plan) != bool(args.dispatch_packet):
            parser.error(
                "--dispatch-packet is required with --plan and forbidden with --request"
            )
        resume_option_values = (
            args.work_manifest,
            args.max_continuations,
            args.cumulative_wall_time_seconds,
            args.checkpoint_window_seconds,
            args.shutdown_window_seconds,
            args.checkpoint_ttl_seconds,
        )
        if args.resumable and not args.plan:
            parser.error("--resumable requires a planner-bound dispatch packet")
        if not args.resumable and any(
            value is not None for value in resume_option_values
        ):
            parser.error("resume options require --resumable")
        if args.token_cap is not None and args.token_cap < 1:
            parser.error("--token-cap must be positive")
        if args.token_cap_contract is not None and args.token_cap is None:
            parser.error("--token-cap-contract requires --token-cap")
        if args.model_cycle_cap is not None and args.model_cycle_cap < 1:
            parser.error("--model-cycle-cap must be positive")
        if args.tool_cycle_cap is not None and args.tool_cycle_cap < 0:
            parser.error("--tool-cycle-cap cannot be negative")
        if args.wall_time_seconds < 1:
            parser.error("--wall-time-seconds must be positive")
        tree_id = args.tree_id or f"dispatch-{uuid.uuid4().hex}"
        reservation = governance.reserve_agent(
            tree_id=tree_id,
            role=args.agent_role,
            parent_lease_id=args.parent_lease_id,
            nested_capacity_tokens=args.nested_capacity_tokens,
            lease_seconds=args.lease_seconds,
        )
        lease = reservation["lease"]
        args._agent_lease = lease
        try:
            result = run_phase(args)
        except Exception as error:
            governance.complete_reservation(
                reservation, status="ABORTED", reason=str(error)
            )
            raise
        terminal = governance.complete_reservation(
            reservation,
            status="COMPLETED" if result == 0 else "BLOCKED",
            reason="dispatcher accepted phase" if result == 0 else "dispatcher rejected phase",
        )
        # The execution artifact is printed by run_phase.  This short, immediate
        # receipt makes capacity release observable even when the worker output
        # is truncated by a calling harness.
        print(json.dumps(terminal, sort_keys=True))
        return result
    except Exception as error:
        failure_args = args or failure_namespace_from_argv(sys.argv[1:])
        artifact = write_setup_failure_artifact(failure_args, error)
        if artifact:
            artifact_path = artifact / "aborted.json"
            suffix = (
                f"; artifact: {artifact_path}; "
                f"sha256: {sha256_bytes(artifact_path.read_bytes())}"
            )
        else:
            suffix = "; artifact retention failed"
        print(f"workflow_dispatch: {error}{suffix}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
