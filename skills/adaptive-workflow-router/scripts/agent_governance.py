#!/usr/bin/env python3
"""Locked admission ledger for workflow-planned adaptive agent trees.

This module deliberately owns only execution capacity and skill-read provenance.
It never selects a model, tier, provider, or workflow route.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any


STATE_SCHEMA_VERSION = 2
CAPACITY_MODE = "workflow-planned-descendant-tokens"
LEASE_ROLES = {"coordinator", "worker", "nested", "reviewer", "monitor"}
TERMINAL_STATUSES = {"COMPLETED", "ABORTED", "BLOCKED", "EXPIRED"}
HEX_SHA256 = set("0123456789abcdef")
WORK_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
CAPACITY_REQUEST_NAME = "adaptive-workflow.capacity-request"
CAPACITY_CONTRACT_NAME = "adaptive-workflow.parallel-capacity"
CAPACITY_SNAPSHOT_NAME = "adaptive-workflow.capacity-snapshot"
BRANCH_COMPLETION_NAME = "adaptive-workflow.branch-completion"
CAPACITY_POLICY_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "execution-budget-policy.json"
)


class GovernanceError(RuntimeError):
    """A capacity or provenance contract failed closed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _finite_positive(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def normalize_mutation_scope(value: Any) -> str:
    """Return one canonical repository-relative ownership scope."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
        or value.startswith("/")
        or value.endswith("/")
    ):
        raise GovernanceError("parallel capacity mutation scope is invalid")
    path = PurePosixPath(value)
    if (
        value != path.as_posix()
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise GovernanceError("parallel capacity mutation scope is invalid")
    return value


def mutation_scopes_conflict(left: str, right: str) -> bool:
    """Treat equal paths and ancestor/descendant paths as the same owner lane."""
    left_parts = PurePosixPath(normalize_mutation_scope(left)).parts
    right_parts = PurePosixPath(normalize_mutation_scope(right)).parts
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def mutation_scope_contains(scope: str, changed_path: str) -> bool:
    scope_parts = PurePosixPath(normalize_mutation_scope(scope)).parts
    path_parts = PurePosixPath(normalize_mutation_scope(changed_path)).parts
    return path_parts[: len(scope_parts)] == scope_parts


def validate_capacity_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "snapshot_source",
        "policy_sha256",
        "host_logical_cpus",
        "provider_weight_ceiling",
        "host_logical_cpus_per_weight_unit",
        "available_weight_units",
        "captured_at_epoch",
        "expires_at_epoch",
        "snapshot_sha256",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != fields:
        raise GovernanceError("parallel capacity snapshot has an invalid schema")
    unsigned = dict(snapshot)
    supplied = unsigned.pop("snapshot_sha256", None)
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("contract_name") != CAPACITY_SNAPSHOT_NAME
        or snapshot.get("contract_version") != 1
        or snapshot.get("snapshot_source") != "local-host-plus-active-policy"
        or not _valid_sha256(snapshot.get("policy_sha256"))
        or type(snapshot.get("host_logical_cpus")) is not int
        or snapshot["host_logical_cpus"] < 1
        or not _finite_positive(snapshot.get("provider_weight_ceiling"))
        or not _finite_positive(snapshot.get("host_logical_cpus_per_weight_unit"))
        or not _finite_positive(snapshot.get("available_weight_units"))
        or snapshot["available_weight_units"]
        > snapshot["provider_weight_ceiling"]
        or not isinstance(snapshot.get("captured_at_epoch"), (int, float))
        or isinstance(snapshot.get("captured_at_epoch"), bool)
        or not isinstance(snapshot.get("expires_at_epoch"), (int, float))
        or isinstance(snapshot.get("expires_at_epoch"), bool)
        or snapshot["expires_at_epoch"] <= snapshot["captured_at_epoch"]
        or snapshot["expires_at_epoch"] - snapshot["captured_at_epoch"] > 3600
        or supplied != content_hash(unsigned)
    ):
        raise GovernanceError("parallel capacity snapshot is invalid")
    return snapshot


def _execution_policy() -> tuple[dict[str, Any], str]:
    path = CAPACITY_POLICY_PATH.resolve()
    if CAPACITY_POLICY_PATH.is_symlink() or not path.is_file():
        raise GovernanceError("parallel capacity policy is missing or unsafe")
    raw = path.read_bytes()
    try:
        policy = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GovernanceError("parallel capacity policy is invalid") from error
    parallel = policy.get("parallel_capacity") if isinstance(policy, dict) else None
    weights = policy.get("model_resource_weights") if isinstance(policy, dict) else None
    unknown = (
        policy.get("unknown_model_resource_weights")
        if isinstance(policy, dict)
        else None
    )
    if (
        not isinstance(policy, dict)
        or policy.get("schema_version") != 1
        or policy.get("policy_name")
        != "adaptive-workflow.execution-budget-policy"
        or policy.get("policy_version") != 5
        or not isinstance(parallel, dict)
        or parallel.get("snapshot_source") != "local-host-plus-active-policy"
        or type(parallel.get("snapshot_ttl_seconds")) is not int
        or not 1 <= parallel["snapshot_ttl_seconds"] <= 3600
        or not _finite_positive(parallel.get("provider_weight_ceiling"))
        or not _finite_positive(
            parallel.get("host_logical_cpus_per_weight_unit")
        )
        or not isinstance(weights, dict)
        or not weights
        or not isinstance(unknown, dict)
    ):
        raise GovernanceError("parallel capacity policy has an invalid schema")
    resource_records = [*weights.values(), unknown]
    for record in resource_records:
        if (
            not isinstance(record, dict)
            or set(record) != {"cost_weight", "quota_weight"}
            or not _finite_positive(record.get("cost_weight"))
            or not _finite_positive(record.get("quota_weight"))
        ):
            raise GovernanceError("parallel capacity model weights are invalid")
    maximum_route_weight = max(
        max(float(record["cost_weight"]), float(record["quota_weight"]))
        for record in resource_records
    )
    if float(parallel["provider_weight_ceiling"]) < maximum_route_weight:
        raise GovernanceError(
            "parallel capacity ceiling cannot admit one supported route"
        )
    return policy, sha256_bytes(raw)


def trusted_capacity_snapshot(*, now_epoch: float | None = None) -> dict[str, Any]:
    """Derive capacity inside the governance authority, never from a caller."""
    execution_policy, policy_sha256 = _execution_policy()
    policy = execution_policy["parallel_capacity"]
    resource_records = [
        *execution_policy["model_resource_weights"].values(),
        execution_policy["unknown_model_resource_weights"],
    ]
    single_dispatch_floor = max(
        max(float(record["cost_weight"]), float(record["quota_weight"]))
        for record in resource_records
    )
    host_logical_cpus = os.cpu_count() or 1
    available = min(
        float(policy["provider_weight_ceiling"]),
        max(
            single_dispatch_floor,
            host_logical_cpus
            / float(policy["host_logical_cpus_per_weight_unit"]),
        ),
    )
    captured = float(time.time() if now_epoch is None else now_epoch)
    identity = {
        "schema_version": 1,
        "contract_name": CAPACITY_SNAPSHOT_NAME,
        "contract_version": 1,
        "snapshot_source": policy["snapshot_source"],
        "policy_sha256": policy_sha256,
        "host_logical_cpus": host_logical_cpus,
        "provider_weight_ceiling": float(policy["provider_weight_ceiling"]),
        "host_logical_cpus_per_weight_unit": float(
            policy["host_logical_cpus_per_weight_unit"]
        ),
        "available_weight_units": available,
        "captured_at_epoch": captured,
        "expires_at_epoch": captured + policy["snapshot_ttl_seconds"],
    }
    snapshot = {**identity, "snapshot_sha256": content_hash(identity)}
    return validate_capacity_snapshot(snapshot)


def validate_trusted_capacity_snapshot(
    snapshot: dict[str, Any], *, require_fresh: bool
) -> dict[str, Any]:
    validate_capacity_snapshot(snapshot)
    expected = trusted_capacity_snapshot(now_epoch=snapshot["captured_at_epoch"])
    if snapshot != expected:
        raise GovernanceError(
            "parallel capacity snapshot differs from governance policy or host"
        )
    if require_fresh and snapshot["expires_at_epoch"] <= time.time():
        raise GovernanceError("parallel capacity snapshot expired before tree admission")
    return snapshot


def _build_capacity_contract(
    request: dict[str, Any], capacity_snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Schedule an exact task DAG into bounded, conflict-free parallel waves."""
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "tree_id",
        "source_commit",
        "created_at_epoch",
        "expires_at_epoch",
        "branches",
    }
    if (
        not isinstance(request, dict)
        or set(request) != fields
        or request.get("schema_version") != 1
        or request.get("contract_name") != CAPACITY_REQUEST_NAME
        or request.get("contract_version") != 1
        or not isinstance(request.get("tree_id"), str)
        or not WORK_ID.fullmatch(request["tree_id"])
        or not _valid_commit(request.get("source_commit"))
        or not isinstance(request.get("created_at_epoch"), (int, float))
        or isinstance(request.get("created_at_epoch"), bool)
        or not isinstance(request.get("expires_at_epoch"), (int, float))
        or isinstance(request.get("expires_at_epoch"), bool)
        or request["expires_at_epoch"] <= request["created_at_epoch"]
        or request["expires_at_epoch"] - request["created_at_epoch"] > 86_400
        or not isinstance(request.get("branches"), list)
        or not request["branches"]
    ):
        raise GovernanceError("parallel capacity request has an invalid schema")
    capacity_snapshot = validate_capacity_snapshot(capacity_snapshot)
    branch_fields = {
        "branch_id",
        "depends_on",
        "mutation_scopes",
        "workflow_id",
        "workflow_version",
        "phase_id",
        "model",
        "effort",
        "cost_weight",
        "quota_weight",
        "token_cap",
        "model_cycle_cap",
        "tool_cycle_cap",
        "wall_time_seconds",
        "plan_id",
        "phase_key",
        "dispatch_packet_sha256",
        "sandbox",
        "network_access",
        "tool_mode",
        "mutation_authorized",
    }
    execution_policy, _ = _execution_policy()
    model_weights = execution_policy["model_resource_weights"]
    unknown_weights = execution_policy["unknown_model_resource_weights"]
    normalized: dict[str, dict[str, Any]] = {}
    for branch in request["branches"]:
        if not isinstance(branch, dict) or set(branch) != branch_fields:
            raise GovernanceError("parallel capacity branch has an invalid schema")
        branch_id = branch.get("branch_id")
        dependencies = branch.get("depends_on")
        scopes = branch.get("mutation_scopes")
        integer_limits = (
            branch.get("workflow_version"),
            branch.get("token_cap"),
            branch.get("model_cycle_cap"),
            branch.get("wall_time_seconds"),
        )
        if (
            not isinstance(branch_id, str)
            or not WORK_ID.fullmatch(branch_id)
            or branch_id in normalized
            or not isinstance(dependencies, list)
            or dependencies != sorted(set(dependencies))
            or any(
                not isinstance(item, str) or not WORK_ID.fullmatch(item)
                for item in dependencies
            )
            or branch_id in dependencies
            or not isinstance(scopes, list)
            or scopes != sorted(set(scopes))
            or any(not isinstance(item, str) or not item for item in scopes)
            or any(type(value) is not int or value < 1 for value in integer_limits)
            or type(branch.get("tool_cycle_cap")) is not int
            or branch["tool_cycle_cap"] < 0
            or any(
                not isinstance(branch.get(field), str) or not branch[field]
                for field in (
                    "workflow_id",
                    "phase_id",
                    "model",
                    "effort",
                    "plan_id",
                    "phase_key",
                    "dispatch_packet_sha256",
                    "sandbox",
                    "tool_mode",
                )
            )
            or not _valid_sha256(branch.get("plan_id"))
            or not _valid_sha256(branch.get("dispatch_packet_sha256"))
            or branch.get("sandbox") not in {"read-only", "workspace-write"}
            or branch.get("tool_mode") not in {"none", "default"}
            or type(branch.get("network_access")) is not bool
            or type(branch.get("mutation_authorized")) is not bool
            or (
                branch.get("sandbox") == "read-only"
                and branch.get("mutation_authorized") is True
            )
            or (
                branch.get("sandbox") == "workspace-write"
                and branch.get("mutation_authorized") is not True
            )
            or (
                branch.get("sandbox") == "workspace-write" and not scopes
            )
            or not _finite_positive(branch.get("cost_weight"))
            or not _finite_positive(branch.get("quota_weight"))
        ):
            raise GovernanceError("parallel capacity branch is invalid")
        try:
            canonical_scopes = [normalize_mutation_scope(item) for item in scopes]
        except GovernanceError as error:
            raise GovernanceError("parallel capacity branch is invalid") from error
        if canonical_scopes != scopes:
            raise GovernanceError("parallel capacity branch mutation scopes are not canonical")
        expected_weights = model_weights.get(branch["model"], unknown_weights)
        if any(
            float(branch[field]) != float(expected_weights[field])
            for field in ("cost_weight", "quota_weight")
        ):
            raise GovernanceError(
                f"parallel capacity branch model weights differ from policy: {branch_id}"
            )
        weight = max(float(branch["cost_weight"]), float(branch["quota_weight"]))
        if weight > float(capacity_snapshot["available_weight_units"]):
            raise GovernanceError(
                f"parallel capacity branch exceeds available weight: {branch_id}"
            )
        normalized[branch_id] = {**branch, "resource_weight": weight}
    unknown_dependencies = sorted(
        {
            dependency
            for branch in normalized.values()
            for dependency in branch["depends_on"]
            if dependency not in normalized
        }
    )
    if unknown_dependencies:
        raise GovernanceError("parallel capacity request has unknown dependencies")

    remaining = set(normalized)
    completed: set[str] = set()
    waves: list[list[str]] = []
    available = float(capacity_snapshot["available_weight_units"])
    while remaining:
        ready = sorted(
            branch_id
            for branch_id in remaining
            if set(normalized[branch_id]["depends_on"]) <= completed
        )
        if not ready:
            raise GovernanceError("parallel capacity dependency graph contains a cycle")
        wave: list[str] = []
        used_weight = 0.0
        owned_scopes: list[str] = []
        for branch_id in ready:
            branch = normalized[branch_id]
            branch_scopes = branch["mutation_scopes"]
            if any(
                mutation_scopes_conflict(candidate, owned)
                for candidate in branch_scopes
                for owned in owned_scopes
            ):
                continue
            if used_weight + branch["resource_weight"] > available + 1e-9:
                continue
            wave.append(branch_id)
            used_weight += branch["resource_weight"]
            owned_scopes.extend(branch_scopes)
        if not wave:
            raise GovernanceError("parallel capacity scheduler could not admit a ready branch")
        waves.append(wave)
        completed.update(wave)
        remaining.difference_update(wave)
    identity = {
        **request,
        "contract_name": CAPACITY_CONTRACT_NAME,
        "branches": [normalized[item["branch_id"]] for item in request["branches"]],
        "waves": waves,
        "descendant_tokens": len(normalized),
        "capacity_mode": CAPACITY_MODE,
        "capacity_snapshot": capacity_snapshot,
    }
    return {**identity, "contract_sha256": content_hash(identity)}


def build_capacity_contract(request: dict[str, Any]) -> dict[str, Any]:
    """Build a contract with governance-derived, non-caller capacity."""
    return _build_capacity_contract(request, trusted_capacity_snapshot())


def validate_capacity_contract(
    contract: dict[str, Any], *, tree_id: str, source_commit: str
) -> dict[str, Any]:
    if not isinstance(contract, dict):
        raise GovernanceError("parallel capacity contract must be an object")
    supplied = contract.get("contract_sha256")
    identity = dict(contract)
    identity.pop("contract_sha256", None)
    if supplied != content_hash(identity):
        raise GovernanceError("parallel capacity contract self-hash is invalid")
    if (
        contract.get("contract_name") != CAPACITY_CONTRACT_NAME
        or contract.get("tree_id") != tree_id
        or contract.get("source_commit") != source_commit
        or contract.get("capacity_mode") != CAPACITY_MODE
        or contract.get("expires_at_epoch", 0) <= time.time()
    ):
        raise GovernanceError("parallel capacity contract is stale or task-mismatched")
    request = {
        key: contract[key]
        for key in (
            "schema_version",
            "contract_version",
            "tree_id",
            "source_commit",
            "created_at_epoch",
            "expires_at_epoch",
            "branches",
        )
    }
    request["contract_name"] = CAPACITY_REQUEST_NAME
    request["branches"] = [
        {key: value for key, value in branch.items() if key != "resource_weight"}
        for branch in request["branches"]
    ]
    snapshot = validate_trusted_capacity_snapshot(
        contract.get("capacity_snapshot"), require_fresh=False
    )
    if _build_capacity_contract(request, snapshot) != contract:
        raise GovernanceError("parallel capacity contract is not deterministically derived")
    return contract


def load_capacity_contract(
    path: Path, *, tree_id: str, source_commit: str
) -> dict[str, Any]:
    absolute = path.expanduser()
    if not absolute.is_absolute() or absolute.is_symlink() or not absolute.is_file():
        raise GovernanceError("parallel capacity contract is missing or unsafe")
    try:
        contract = json.loads(absolute.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GovernanceError(f"parallel capacity contract is invalid: {error}") from error
    return validate_capacity_contract(
        contract, tree_id=tree_id, source_commit=source_commit
    )


def _home() -> Path:
    return Path(
        os.environ.get(
            "ADAPTIVE_WORKFLOW_GOVERNANCE_HOME",
            Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
            / "adaptive-workflow-router",
        )
    ).expanduser().resolve()


def state_path() -> Path:
    return _home() / "agent-governance.json"


def lock_path() -> Path:
    return _home() / "agent-governance.lock"


def execution_registry_path() -> Path:
    return _home() / "execution-registry.jsonl"


def install_journal_path() -> Path:
    return _home() / "install-journal.json"


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "capacity_mode": CAPACITY_MODE,
        "trees": {},
        "skill_reads": [],
    }


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX_SHA256


def _valid_commit(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and set(value) <= HEX_SHA256
    )


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise GovernanceError("governance state path is unsafe")
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_state() -> tuple[dict[str, Any], bool]:
    path = state_path()
    if not path.exists():
        return _default_state(), False
    if path.is_symlink() or not path.is_file():
        raise GovernanceError("governance state is unsafe")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GovernanceError(f"governance state is invalid: {error}") from error
    if not isinstance(state, dict):
        raise GovernanceError("governance state has an unsupported schema")
    migrated = False
    if state.get("schema_version") == 1:
        if (
            state.get("max_active_agents") != 9
            or not isinstance(state.get("trees"), dict)
            or not isinstance(state.get("skill_reads"), list)
        ):
            raise GovernanceError("governance state has an unsupported schema")
        state = {
            "schema_version": STATE_SCHEMA_VERSION,
            "capacity_mode": CAPACITY_MODE,
            "trees": state["trees"],
            "skill_reads": state["skill_reads"],
        }
        migrated = True
    if (
        state.get("schema_version") != STATE_SCHEMA_VERSION
        or state.get("capacity_mode") != CAPACITY_MODE
        or set(state) != {
            "schema_version",
            "capacity_mode",
            "trees",
            "skill_reads",
        }
        or not isinstance(state.get("trees"), dict)
        or not isinstance(state.get("skill_reads"), list)
    ):
        raise GovernanceError("governance state has an unsupported schema")
    return state, migrated


def _load() -> dict[str, Any]:
    state, _ = _load_state()
    return state


def _record_lock_contention_marker() -> None:
    value = os.environ.get("ADAPTIVE_WORKFLOW_LOCK_CONTENTION_MARKER")
    if value is None:
        return
    marker = Path(value).expanduser()
    home = _home()
    if (
        not marker.is_absolute()
        or marker.parent.resolve() != home
        or marker.exists()
        or marker.is_symlink()
    ):
        raise GovernanceError("governance contention marker is unsafe")
    descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, b"observed-blocked\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mutate(callback: Any) -> Any:
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lock.parent.is_symlink() or lock.is_symlink():
        raise GovernanceError("governance lock path is unsafe")
    with lock.open("a+", encoding="utf-8") as handle:
        os.chmod(lock, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _record_lock_contention_marker()
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state, migrated = _load_state()
        result, changed = callback(state)
        if changed or migrated:
            _atomic_write(state_path(), state)
        return result


def _active(lease: dict[str, Any]) -> bool:
    return lease.get("state") == "ACTIVE"


def _tree_active_count(tree: dict[str, Any]) -> int:
    return sum(1 for lease in tree.get("leases", {}).values() if _active(lease))


def _direct_resource_weight() -> float:
    policy, _ = _execution_policy()
    records = [
        *policy["model_resource_weights"].values(),
        policy["unknown_model_resource_weights"],
    ]
    return max(
        max(float(record["cost_weight"]), float(record["quota_weight"]))
        for record in records
    )


def _lease_resource_weight(lease: dict[str, Any]) -> float:
    value = lease.get("resource_weight")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) >= 0
    ):
        return float(value)
    return 0.0 if lease.get("role") == "coordinator" else _direct_resource_weight()


def _global_active_resource_weight(state: dict[str, Any]) -> float:
    return sum(
        _lease_resource_weight(lease)
        for tree in state["trees"].values()
        for lease in tree.get("leases", {}).values()
        if _active(lease)
    )


def _terminalize(tree: dict[str, Any], lease_id: str, status: str, reason: str) -> None:
    leases = tree["leases"]
    lease = leases[lease_id]
    if not _active(lease):
        return
    # Descendants cannot retain capacity after their parent exits.  Release them
    # first so each token returns through the exact parent chain.
    children = [
        child_id
        for child_id, child in leases.items()
        if _active(child) and child.get("parent_lease_id") == lease_id
    ]
    for child_id in children:
        _terminalize(tree, child_id, "EXPIRED", "ancestor lease terminated")
    parent_id = lease.get("parent_lease_id")
    if parent_id is not None:
        parent = leases.get(parent_id)
        if parent is not None and _active(parent):
            parent["nested_capacity_tokens"] += 1 + lease["nested_capacity_tokens"]
    now = time.time()
    receipt = {
        "schema_version": 1,
        "receipt_type": "agent-terminal",
        "lease_id": lease_id,
        "tree_id": lease["tree_id"],
        "role": lease["role"],
        "status": status,
        "reason": reason,
        "issued_at_epoch": now,
        "parent_lease_id": parent_id,
        "resource_weight": _lease_resource_weight(lease),
    }
    receipt["receipt_sha256"] = content_hash(receipt)
    lease.update(
        {
            "state": "TERMINAL",
            "terminal_status": status,
            "terminal_reason": reason,
            "terminal_at_epoch": now,
            "terminal_receipt": receipt,
        }
    )
    branch_id = lease.get("capacity_branch_id")
    branch_state = tree.get("capacity_branches", {}).get(branch_id)
    if (
        isinstance(branch_id, str)
        and isinstance(branch_state, dict)
        and branch_state.get("lease_id") == lease_id
    ):
        branch_state.update(
            {
                "state": "TERMINAL",
                "terminal_status": status,
                "terminal_receipt_sha256": receipt["receipt_sha256"],
            }
        )


def cleanup_expired(state: dict[str, Any], *, now: float | None = None) -> list[str]:
    current = time.time() if now is None else now
    cleaned: list[str] = []
    for tree in state["trees"].values():
        for lease_id, lease in list(tree["leases"].items()):
            if _active(lease) and lease.get("expires_at_epoch", 0) <= current:
                _terminalize(tree, lease_id, "EXPIRED", "lease expired")
                cleaned.append(lease_id)
    return cleaned


def capacity_status() -> dict[str, Any]:
    """Return live lease counts without turning them into an admission cap."""

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cleaned = cleanup_expired(state)
        active_by_tree = {
            tree_id: _tree_active_count(tree)
            for tree_id, tree in state["trees"].items()
            if _tree_active_count(tree) > 0
        }
        snapshot = trusted_capacity_snapshot()
        active_weight = _global_active_resource_weight(state)
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "capacity_mode": CAPACITY_MODE,
            "fixed_agent_count_cap": False,
            "active_trees": len(active_by_tree),
            "active_leases": sum(active_by_tree.values()),
            "active_leases_by_tree": active_by_tree,
            "active_resource_weight": active_weight,
            "available_resource_weight": snapshot["available_weight_units"],
            "remaining_resource_weight": max(
                0.0, snapshot["available_weight_units"] - active_weight
            ),
            "capacity_snapshot_sha256": snapshot["snapshot_sha256"],
            "expired_leases_cleaned": sorted(cleaned),
        }, bool(cleaned)

    return _mutate(mutate)


def reserve_agent(
    *,
    tree_id: str,
    role: str,
    parent_lease_id: str | None = None,
    nested_capacity_tokens: int = 0,
    lease_seconds: int = 900,
    capacity_contract: dict[str, Any] | None = None,
    capacity_branch_id: str | None = None,
) -> dict[str, Any]:
    """Acquire one planned tree slot and conserve descendant capacity tokens.

    There is deliberately no process-wide agent-count ceiling. The workflow
    coordinator chooses a finite descendant budget after decomposing the task;
    every child and nested reservation consumes that budget atomically. Model,
    tool, token, wall-time, mutation-ownership, and provider-capacity limits are
    enforced by their own contracts rather than collapsed into an agent count.
    """
    if not isinstance(tree_id, str) or not tree_id or len(tree_id) > 128:
        raise GovernanceError("tree_id is invalid")
    if role not in LEASE_ROLES:
        raise GovernanceError("agent role is invalid")
    if type(nested_capacity_tokens) is not int or nested_capacity_tokens < 0:
        raise GovernanceError("nested capacity tokens must be a non-negative integer")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86_400:
        raise GovernanceError("lease_seconds must be between 1 and 86400")
    if capacity_contract is not None:
        source_commit = capacity_contract.get("source_commit")
        if not isinstance(source_commit, str):
            raise GovernanceError("parallel capacity contract omitted source commit")
        capacity_contract = validate_capacity_contract(
            capacity_contract, tree_id=tree_id, source_commit=source_commit
        )
        validate_trusted_capacity_snapshot(
            capacity_contract["capacity_snapshot"],
            require_fresh=role == "coordinator",
        )
    if capacity_branch_id is not None and (
        not isinstance(capacity_branch_id, str)
        or not WORK_ID.fullmatch(capacity_branch_id)
    ):
        raise GovernanceError("parallel capacity branch id is invalid")

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        journal = install_journal_path()
        if journal.exists() or journal.is_symlink():
            raise GovernanceError(
                "global harness installation blocks new agent admission"
            )
        cleanup_expired(state)
        parent_id = parent_lease_id
        tree = state["trees"].get(tree_id)
        now = time.time()
        if tree is None:
            if parent_id is not None:
                raise GovernanceError("parent lease belongs to an unknown tree")
            if role == "coordinator":
                if nested_capacity_tokens > 0:
                    if (
                        capacity_contract is None
                        or capacity_branch_id is not None
                        or capacity_contract["descendant_tokens"]
                        != nested_capacity_tokens
                    ):
                        raise GovernanceError(
                            "coordinator capacity must match one deterministic contract"
                        )
                elif capacity_contract is not None or capacity_branch_id is not None:
                    raise GovernanceError(
                        "zero-capacity coordinator cannot bind a parallel contract"
                    )
                root_id = uuid.uuid4().hex
                root = {
                    "lease_id": root_id,
                    "tree_id": tree_id,
                    "role": "coordinator",
                    "parent_lease_id": None,
                    "nested_capacity_tokens": nested_capacity_tokens,
                    "state": "ACTIVE",
                    "issued_at_epoch": now,
                    "expires_at_epoch": now + lease_seconds,
                    "resource_weight": 0.0,
                }
                tree = {
                    "coordinator_lease_id": root_id,
                    "leases": {root_id: root},
                    "direct_ephemeral": False,
                    "capacity_contract_sha256": (
                        capacity_contract["contract_sha256"]
                        if capacity_contract is not None
                        else None
                    ),
                    "capacity_contract": capacity_contract,
                    "capacity_branches": (
                        {
                            branch["branch_id"]: {
                                "state": "PLANNED",
                                "lease_id": None,
                                "wave": wave_index,
                            }
                            for wave_index, wave in enumerate(
                                capacity_contract["waves"]
                                if capacity_contract is not None
                                else []
                            )
                            for branch in (
                                next(
                                    item
                                    for item in capacity_contract["branches"]
                                    if item["branch_id"] == branch_id
                                )
                                for branch_id in wave
                            )
                        }
                        if capacity_contract is not None
                        else {}
                    ),
                }
                state["trees"][tree_id] = tree
                return {
                    "lease": dict(root),
                    "coordinator_lease_id": root_id,
                    "ephemeral_coordinator_lease_id": None,
                }, True
            else:
                # A direct dispatch gets an ephemeral tree sized only for this
                # child. Parallel work must use a deterministic capacity plan.
                if (
                    nested_capacity_tokens != 0
                    or capacity_contract is not None
                    or capacity_branch_id is not None
                ):
                    raise GovernanceError(
                        "direct dispatch cannot mint or consume planned parallel capacity"
                    )
                root_id = uuid.uuid4().hex
                root = {
                    "lease_id": root_id,
                    "tree_id": tree_id,
                    "role": "coordinator",
                    "parent_lease_id": None,
                    "nested_capacity_tokens": 1,
                    "state": "ACTIVE",
                    "issued_at_epoch": now,
                    "expires_at_epoch": now + lease_seconds,
                    "resource_weight": 0.0,
                }
                tree = {
                    "coordinator_lease_id": root_id,
                    "leases": {root_id: root},
                    "direct_ephemeral": True,
                    "capacity_contract_sha256": None,
                    "capacity_contract": None,
                    "capacity_branches": {},
                }
                state["trees"][tree_id] = tree
                parent_id = root_id
                ephemeral_coordinator_id = root_id
                resource_weight = _direct_resource_weight()
        else:
            ephemeral_coordinator_id = None
            stored_contract = tree.get("capacity_contract")
            if tree.get("direct_ephemeral") is True:
                raise GovernanceError("direct dispatch tree cannot accept another lease")
            if not isinstance(stored_contract, dict):
                raise GovernanceError(
                    "existing tree has no deterministic parallel capacity contract"
                )
            if (
                capacity_contract is None
                or capacity_contract.get("contract_sha256")
                != tree.get("capacity_contract_sha256")
                or capacity_branch_id is None
                or nested_capacity_tokens != 0
                or parent_id != tree.get("coordinator_lease_id")
            ):
                raise GovernanceError(
                    "child lease is not bound to the coordinator capacity plan"
                )
            branches = {
                branch["branch_id"]: branch
                for branch in stored_contract["branches"]
            }
            branch = branches.get(capacity_branch_id)
            branch_state = tree.get("capacity_branches", {}).get(
                capacity_branch_id
            )
            if not isinstance(branch, dict) or not isinstance(branch_state, dict):
                raise GovernanceError("parallel capacity branch is not in this tree")
            if branch_state.get("state") != "PLANNED":
                raise GovernanceError("parallel capacity branch was already consumed")
            for dependency in branch["depends_on"]:
                dependency_state = tree["capacity_branches"].get(dependency, {})
                if (
                    dependency_state.get("state") != "TERMINAL"
                    or dependency_state.get("terminal_status") != "COMPLETED"
                ):
                    raise GovernanceError(
                        "parallel capacity branch dependency is incomplete"
                    )
            for prior_wave in stored_contract["waves"][: branch_state["wave"]]:
                if any(
                    tree["capacity_branches"].get(branch_id, {}).get(
                        "terminal_status"
                    )
                    != "COMPLETED"
                    for branch_id in prior_wave
                ):
                    raise GovernanceError(
                        "parallel capacity prior wave is incomplete"
                    )
            resource_weight = float(branch["resource_weight"])
        parent = tree["leases"].get(parent_id or "")
        if parent is None or not _active(parent):
            raise GovernanceError("parent lease is missing or terminal")
        required_tokens = 1 + nested_capacity_tokens
        if parent["nested_capacity_tokens"] < required_tokens:
            raise GovernanceError("nested capacity tokens are exhausted")
        snapshot = trusted_capacity_snapshot()
        active_weight = _global_active_resource_weight(state)
        if active_weight + resource_weight > snapshot["available_weight_units"] + 1e-9:
            raise GovernanceError(
                "global weighted agent capacity is currently exhausted"
            )
        parent["nested_capacity_tokens"] -= required_tokens
        lease_id = uuid.uuid4().hex
        lease = {
            "lease_id": lease_id,
            "tree_id": tree_id,
            "role": role,
            "parent_lease_id": parent_id,
            "nested_capacity_tokens": nested_capacity_tokens,
            "state": "ACTIVE",
            "issued_at_epoch": now,
            "expires_at_epoch": now + lease_seconds,
            "capacity_contract_sha256": (
                capacity_contract.get("contract_sha256")
                if capacity_contract is not None
                else None
            ),
            "capacity_branch_id": capacity_branch_id,
            "resource_weight": resource_weight,
        }
        tree["leases"][lease_id] = lease
        if capacity_branch_id is not None:
            tree["capacity_branches"][capacity_branch_id].update(
                {"state": "ACTIVE", "lease_id": lease_id}
            )
        return {
            "lease": dict(lease),
            "coordinator_lease_id": tree["coordinator_lease_id"],
            "ephemeral_coordinator_lease_id": ephemeral_coordinator_id,
        }, True

    return _mutate(mutate)


def branch_completion_evidence(
    lease: dict[str, Any], execution_receipt: dict[str, Any]
) -> dict[str, Any]:
    identity = {
        "schema_version": 1,
        "contract_name": BRANCH_COMPLETION_NAME,
        "contract_version": 1,
        "lease_id": lease.get("lease_id"),
        "capacity_contract_sha256": lease.get("capacity_contract_sha256"),
        "capacity_branch_id": lease.get("capacity_branch_id"),
        "execution_receipt_id": execution_receipt.get("receipt_id"),
        "execution_receipt_sha256": execution_receipt.get("receipt_sha256"),
    }
    return {**identity, "completion_sha256": content_hash(identity)}


def _read_bound_json_file(path_value: Any, expected_sha256: Any) -> dict[str, Any]:
    if not isinstance(path_value, str) or not _valid_sha256(expected_sha256):
        raise GovernanceError("branch completion artifact binding is invalid")
    path = Path(path_value)
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or str(path.resolve(strict=True)) != path_value
        or sha256_bytes(path.read_bytes()) != expected_sha256
    ):
        raise GovernanceError("branch completion artifact is missing or changed")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GovernanceError("branch completion artifact is unreadable") from error
    if not isinstance(value, dict):
        raise GovernanceError("branch completion artifact is malformed")
    return value


def _registered_execution_receipt(
    receipt_id: str, receipt_sha256: str
) -> dict[str, Any]:
    path = execution_registry_path()
    if path.is_symlink() or not path.is_file():
        raise GovernanceError("execution registry is missing or unsafe")
    previous = "0" * 64
    target: dict[str, Any] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise GovernanceError("execution registry is unreadable") from error
    for line in lines:
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as error:
            raise GovernanceError("execution registry contains invalid JSON") from error
        if not isinstance(receipt, dict):
            raise GovernanceError("execution registry contains a malformed receipt")
        unsigned = dict(receipt)
        supplied = unsigned.pop("receipt_sha256", None)
        if (
            not _valid_sha256(supplied)
            or receipt.get("previous_receipt_sha256") != previous
            or supplied != content_hash(unsigned)
        ):
            raise GovernanceError("execution registry receipt chain is invalid")
        previous = supplied
        if receipt.get("receipt_id") == receipt_id:
            target = receipt
    if target is None or target.get("receipt_sha256") != receipt_sha256:
        raise GovernanceError("planned branch completion receipt is not registered")
    return target


def validate_branch_completion(
    tree: dict[str, Any], lease: dict[str, Any], evidence: Any
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "lease_id",
        "capacity_contract_sha256",
        "capacity_branch_id",
        "execution_receipt_id",
        "execution_receipt_sha256",
        "completion_sha256",
    }
    if not isinstance(evidence, dict) or set(evidence) != fields:
        raise GovernanceError("planned branch completion evidence is missing")
    unsigned = dict(evidence)
    supplied = unsigned.pop("completion_sha256", None)
    if (
        evidence.get("schema_version") != 1
        or evidence.get("contract_name") != BRANCH_COMPLETION_NAME
        or evidence.get("contract_version") != 1
        or evidence.get("lease_id") != lease.get("lease_id")
        or evidence.get("capacity_contract_sha256")
        != lease.get("capacity_contract_sha256")
        or evidence.get("capacity_branch_id")
        != lease.get("capacity_branch_id")
        or not isinstance(evidence.get("execution_receipt_id"), str)
        or not _valid_sha256(evidence.get("execution_receipt_sha256"))
        or supplied != content_hash(unsigned)
    ):
        raise GovernanceError("planned branch completion evidence is invalid")
    receipt = _registered_execution_receipt(
        evidence["execution_receipt_id"], evidence["execution_receipt_sha256"]
    )
    contract = tree.get("capacity_contract")
    branch_id = lease["capacity_branch_id"]
    branch = next(
        (
            item
            for item in contract.get("branches", [])
            if item.get("branch_id") == branch_id
        ),
        None,
    ) if isinstance(contract, dict) else None
    parallel = receipt.get("parallel_capacity")
    receipt_lease = receipt.get("agent_lease")
    verification = receipt.get("verification")
    if (
        receipt.get("receipt_type") != "execution"
        or receipt.get("status") != "COMPLETED"
        or receipt.get("issues") != []
        or receipt.get("source_commit") != contract.get("source_commit")
        or not isinstance(branch, dict)
        or not isinstance(parallel, dict)
        or parallel.get("contract_sha256") != contract.get("contract_sha256")
        or parallel.get("branch_id") != branch_id
        or parallel.get("dispatch_packet_sha256")
        != branch.get("dispatch_packet_sha256")
        or receipt.get("dispatch_packet_sha256")
        != branch.get("dispatch_packet_sha256")
        or not isinstance(receipt_lease, dict)
        or any(
            receipt_lease.get(field) != lease.get(field)
            for field in (
                "lease_id",
                "tree_id",
                "capacity_contract_sha256",
                "capacity_branch_id",
                "resource_weight",
            )
        )
        or verification
        != {
            "dispatcher_acceptance": True,
            "runtime_identity_verified": True,
            "issues": [],
        }
    ):
        raise GovernanceError("registered execution did not complete this planned branch")
    metadata = _read_bound_json_file(
        receipt.get("metadata_path"), receipt.get("metadata_sha256")
    )
    manifest = _read_bound_json_file(
        receipt.get("input_manifest_path"), receipt.get("input_manifest_sha256")
    )
    if (
        metadata.get("status") != "COMPLETED"
        or metadata.get("issues") != []
        or manifest.get("parallel_capacity") != parallel
        or manifest.get("dispatch_packet_sha256")
        != branch.get("dispatch_packet_sha256")
    ):
        raise GovernanceError("planned branch completion artifacts do not match")
    return receipt


def complete_agent(lease_id: str, *, status: str, reason: str = "") -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise GovernanceError("terminal agent status is invalid")

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cleanup_expired(state)
        for tree in state["trees"].values():
            if lease_id not in tree["leases"]:
                continue
            lease = tree["leases"][lease_id]
            if lease.get("role") == "coordinator":
                raise GovernanceError("coordinator completion requires close-tree authority")
            if status == "COMPLETED" and lease.get("capacity_branch_id") is not None:
                raise GovernanceError(
                    "planned branch completion requires registered dispatcher evidence"
                )
            if _active(lease):
                _terminalize(tree, lease_id, status, reason or status.lower())
            receipt = lease.get("terminal_receipt")
            if not isinstance(receipt, dict):
                raise GovernanceError("terminal lease omitted its receipt")
            return dict(receipt), True
        raise GovernanceError("agent lease is unknown")

    return _mutate(mutate)


def complete_coordinator(
    lease_id: str, *, status: str, reason: str = ""
) -> dict[str, Any]:
    """Close exactly one coordinator tree after all planned branches settle."""
    if status not in TERMINAL_STATUSES:
        raise GovernanceError("terminal agent status is invalid")

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cleanup_expired(state)
        for tree in state["trees"].values():
            lease = tree.get("leases", {}).get(lease_id)
            if lease is None:
                continue
            if lease.get("role") != "coordinator":
                raise GovernanceError("tree close requires a coordinator lease")
            active_children = [
                child
                for child in tree["leases"].values()
                if _active(child) and child.get("parent_lease_id") == lease_id
            ]
            if active_children:
                raise GovernanceError("coordinator still has active child leases")
            if status == "COMPLETED" and any(
                branch.get("terminal_status") != "COMPLETED"
                for branch in tree.get("capacity_branches", {}).values()
            ):
                raise GovernanceError(
                    "completed coordinator has unfinished or failed planned branches"
                )
            if _active(lease):
                _terminalize(tree, lease_id, status, reason or status.lower())
            receipt = lease.get("terminal_receipt")
            if not isinstance(receipt, dict):
                raise GovernanceError("terminal coordinator omitted its receipt")
            return dict(receipt), True
        raise GovernanceError("coordinator lease is unknown")

    return _mutate(mutate)


def complete_reservation(
    reservation: dict[str, Any],
    *,
    status: str,
    reason: str = "",
    completion_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Terminalize a dispatch lease and its auto-created coordinator atomically."""
    if status not in TERMINAL_STATUSES:
        raise GovernanceError("terminal agent status is invalid")
    if not isinstance(reservation, dict):
        raise GovernanceError("agent reservation is invalid")
    lease = reservation.get("lease")
    ephemeral_id = reservation.get("ephemeral_coordinator_lease_id")
    if (
        not isinstance(lease, dict)
        or not isinstance(lease.get("lease_id"), str)
        or not lease["lease_id"]
        or (ephemeral_id is not None and not isinstance(ephemeral_id, str))
    ):
        raise GovernanceError("agent reservation is invalid")

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cleanup_expired(state)
        tree = state["trees"].get(lease.get("tree_id"))
        if not isinstance(tree, dict) or lease["lease_id"] not in tree["leases"]:
            raise GovernanceError("agent reservation is unknown")
        stored = tree["leases"][lease["lease_id"]]
        if status == "COMPLETED" and stored.get("capacity_branch_id") is not None:
            validate_branch_completion(tree, stored, completion_evidence)
        elif completion_evidence is not None:
            raise GovernanceError(
                "dispatcher completion evidence is valid only for a completed planned branch"
            )
        if _active(stored):
            _terminalize(tree, lease["lease_id"], status, reason or status.lower())
        agent_receipt = stored.get("terminal_receipt")
        if not isinstance(agent_receipt, dict):
            raise GovernanceError("terminal lease omitted its receipt")
        coordinator_receipt: dict[str, Any] | None = None
        if ephemeral_id is not None:
            if (
                tree.get("coordinator_lease_id") != ephemeral_id
                or stored.get("parent_lease_id") != ephemeral_id
                or ephemeral_id not in tree["leases"]
            ):
                raise GovernanceError("ephemeral coordinator binding is invalid")
            coordinator = tree["leases"][ephemeral_id]
            if _active(coordinator):
                _terminalize(
                    tree,
                    ephemeral_id,
                    status,
                    f"ephemeral dispatch tree closed after {status.lower()}",
                )
            coordinator_receipt = coordinator.get("terminal_receipt")
            if not isinstance(coordinator_receipt, dict):
                raise GovernanceError("ephemeral coordinator omitted its receipt")
        return {
            "agent_terminal_receipt": dict(agent_receipt),
            "coordinator_terminal_receipt": (
                dict(coordinator_receipt)
                if coordinator_receipt is not None
                else None
            ),
        }, True

    return _mutate(mutate)


def record_skill_read(
    *, name: str, path: Path, source_commit: str, max_age_seconds: int = 3600
) -> dict[str, Any]:
    if not isinstance(name, str) or not name:
        raise GovernanceError("skill name is invalid")
    if not _valid_commit(source_commit):
        raise GovernanceError("source_commit must be a Git object ID")
    if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86_400:
        raise GovernanceError("skill read max age is invalid")
    resolved = path.expanduser()
    if not resolved.is_absolute() or resolved.is_symlink() or not resolved.is_file():
        raise GovernanceError("skill file is missing or unsafe")
    resolved = resolved.resolve(strict=True)
    skill_hash = sha256_bytes(resolved.read_bytes())

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cleanup_expired(state)
        previous = state["skill_reads"][-1]["receipt_sha256"] if state["skill_reads"] else "0" * 64
        receipt = {
            "schema_version": 1,
            "receipt_type": "skill-read",
            "receipt_id": uuid.uuid4().hex,
            "name": name,
            "path": str(resolved),
            "skill_sha256": skill_hash,
            "source_commit": source_commit,
            "max_age_seconds": max_age_seconds,
            "read_at_epoch": time.time(),
            "previous_receipt_sha256": previous,
        }
        receipt["receipt_sha256"] = content_hash(receipt)
        state["skill_reads"].append(receipt)
        return dict(receipt), True

    return _mutate(mutate)


def _skill_receipt(state: dict[str, Any], receipt_id: str) -> dict[str, Any] | None:
    previous = "0" * 64
    for receipt in state["skill_reads"]:
        bare = dict(receipt)
        supplied = bare.pop("receipt_sha256", None)
        if supplied != content_hash(bare) or receipt.get("previous_receipt_sha256") != previous:
            raise GovernanceError("skill-read receipt chain is invalid")
        previous = supplied
        if receipt.get("receipt_id") == receipt_id:
            return receipt
    return None


def validate_skill_requirements(
    required_skills: list[dict[str, Any]], *, source_commit: str
) -> dict[str, str]:
    """Re-read every required skill and require a fresh, matching read receipt."""
    if not _valid_commit(source_commit):
        raise GovernanceError("source_commit must be a Git object ID")
    if not isinstance(required_skills, list):
        raise GovernanceError("required_skills must be a list")

    def mutate(state: dict[str, Any]) -> tuple[dict[str, str], bool]:
        cleaned = cleanup_expired(state)
        hashes: dict[str, str] = {}
        seen: set[str] = set()
        now = time.time()
        for item in required_skills:
            if not isinstance(item, dict) or set(item) != {
                "name", "path", "skill_sha256", "read_receipt_id", "max_age_seconds"
            }:
                raise GovernanceError("required skill has an invalid schema")
            name = item.get("name")
            path_value = item.get("path")
            if not isinstance(name, str) or not name or name in seen:
                raise GovernanceError("required skill name is invalid or duplicated")
            seen.add(name)
            if (
                not isinstance(path_value, str)
                or not _valid_sha256(item.get("skill_sha256"))
                or not isinstance(item.get("read_receipt_id"), str)
                or type(item.get("max_age_seconds")) is not int
                or not 1 <= item["max_age_seconds"] <= 86_400
            ):
                raise GovernanceError("required skill provenance is invalid")
            path = Path(path_value)
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise GovernanceError(f"required skill is absent: {name}")
            if sha256_bytes(path.resolve(strict=True).read_bytes()) != item["skill_sha256"]:
                raise GovernanceError(f"required skill is stale: {name}")
            receipt = _skill_receipt(state, item["read_receipt_id"])
            if receipt is None:
                raise GovernanceError(f"required skill was not read: {name}")
            if (
                receipt.get("name") != name
                or receipt.get("path") != str(path.resolve(strict=True))
                or receipt.get("skill_sha256") != item["skill_sha256"]
                or receipt.get("source_commit") != source_commit
                or now - receipt.get("read_at_epoch", 0) > item["max_age_seconds"]
            ):
                raise GovernanceError(f"required skill read is stale or mismatched: {name}")
            hashes[name] = item["skill_sha256"]
        return hashes, bool(cleaned)

    return _mutate(mutate)
