#!/usr/bin/env python3
"""Locked admission ledger for globally bounded adaptive-workflow agent trees.

This module deliberately owns only execution capacity and skill-read provenance.
It never selects a model, tier, provider, or workflow route.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


MAX_ACTIVE_AGENTS = 9
LEASE_ROLES = {"coordinator", "worker", "nested", "reviewer", "monitor"}
TERMINAL_STATUSES = {"COMPLETED", "ABORTED", "BLOCKED", "EXPIRED"}
HEX_SHA256 = set("0123456789abcdef")


class GovernanceError(RuntimeError):
    """A capacity or provenance contract failed closed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "max_active_agents": MAX_ACTIVE_AGENTS,
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


def _load() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return _default_state()
    if path.is_symlink() or not path.is_file():
        raise GovernanceError("governance state is unsafe")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GovernanceError(f"governance state is invalid: {error}") from error
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != 1
        or state.get("max_active_agents") != MAX_ACTIVE_AGENTS
        or not isinstance(state.get("trees"), dict)
        or not isinstance(state.get("skill_reads"), list)
    ):
        raise GovernanceError("governance state has an unsupported schema")
    return state


def _mutate(callback: Any) -> Any:
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if lock.parent.is_symlink() or lock.is_symlink():
        raise GovernanceError("governance lock path is unsafe")
    with lock.open("a+", encoding="utf-8") as handle:
        os.chmod(lock, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        state = _load()
        result, changed = callback(state)
        if changed:
            _atomic_write(state_path(), state)
        return result


def _active(lease: dict[str, Any]) -> bool:
    return lease.get("state") == "ACTIVE"


def _tree_active_count(tree: dict[str, Any]) -> int:
    return sum(1 for lease in tree.get("leases", {}).values() if _active(lease))


def _global_active_count(state: dict[str, Any]) -> int:
    return sum(_tree_active_count(tree) for tree in state.get("trees", {}).values())


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


def cleanup_expired(state: dict[str, Any], *, now: float | None = None) -> list[str]:
    current = time.time() if now is None else now
    cleaned: list[str] = []
    for tree in state["trees"].values():
        for lease_id, lease in list(tree["leases"].items()):
            if _active(lease) and lease.get("expires_at_epoch", 0) <= current:
                _terminalize(tree, lease_id, "EXPIRED", "lease expired")
                cleaned.append(lease_id)
    return cleaned


def reserve_agent(
    *,
    tree_id: str,
    role: str,
    parent_lease_id: str | None = None,
    nested_capacity_tokens: int = 0,
    lease_seconds: int = 900,
) -> dict[str, Any]:
    """Acquire one globally capped slot and conserve descendant capacity tokens."""
    if not isinstance(tree_id, str) or not tree_id or len(tree_id) > 128:
        raise GovernanceError("tree_id is invalid")
    if role not in LEASE_ROLES:
        raise GovernanceError("agent role is invalid")
    if type(nested_capacity_tokens) is not int or nested_capacity_tokens < 0:
        raise GovernanceError("nested capacity tokens must be a non-negative integer")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 86_400:
        raise GovernanceError("lease_seconds must be between 1 and 86400")

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cleanup_expired(state)
        if _global_active_count(state) >= MAX_ACTIVE_AGENTS:
            raise GovernanceError("hard global active-agent capacity (9) reached")
        parent_id = parent_lease_id
        tree = state["trees"].get(tree_id)
        now = time.time()
        if tree is None:
            if parent_id is not None or role == "coordinator":
                if role != "coordinator" and parent_id is not None:
                    raise GovernanceError("parent lease belongs to an unknown tree")
                root_id = uuid.uuid4().hex
                root_tokens = MAX_ACTIVE_AGENTS - 1
                if nested_capacity_tokens > root_tokens:
                    raise GovernanceError("requested nested capacity exceeds the tree cap")
                root = {
                    "lease_id": root_id,
                    "tree_id": tree_id,
                    "role": "coordinator",
                    "parent_lease_id": None,
                    "nested_capacity_tokens": root_tokens,
                    "state": "ACTIVE",
                    "issued_at_epoch": now,
                    "expires_at_epoch": now + lease_seconds,
                }
                tree = {"coordinator_lease_id": root_id, "leases": {root_id: root}}
                state["trees"][tree_id] = tree
                if role == "coordinator":
                    root["nested_capacity_tokens"] = nested_capacity_tokens
                    return {
                        "lease": dict(root),
                        "coordinator_lease_id": root_id,
                        "ephemeral_coordinator_lease_id": None,
                    }, True
                parent_id = root_id
            else:
                # Backwards-compatible direct dispatch: create the coordinator
                # and bind the request as its child in one atomic transaction.
                root_id = uuid.uuid4().hex
                root = {
                    "lease_id": root_id,
                    "tree_id": tree_id,
                    "role": "coordinator",
                    "parent_lease_id": None,
                    "nested_capacity_tokens": MAX_ACTIVE_AGENTS - 1,
                    "state": "ACTIVE",
                    "issued_at_epoch": now,
                    "expires_at_epoch": now + lease_seconds,
                }
                tree = {"coordinator_lease_id": root_id, "leases": {root_id: root}}
                state["trees"][tree_id] = tree
                parent_id = root_id
                ephemeral_coordinator_id = root_id
        else:
            ephemeral_coordinator_id = None
        parent = tree["leases"].get(parent_id or "")
        if parent is None or not _active(parent):
            raise GovernanceError("parent lease is missing or terminal")
        required_tokens = 1 + nested_capacity_tokens
        if parent["nested_capacity_tokens"] < required_tokens:
            raise GovernanceError("nested capacity tokens are exhausted")
        if _global_active_count(state) >= MAX_ACTIVE_AGENTS:
            raise GovernanceError("hard global active-agent capacity (9) reached")
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
        }
        tree["leases"][lease_id] = lease
        return {
            "lease": dict(lease),
            "coordinator_lease_id": tree["coordinator_lease_id"],
            "ephemeral_coordinator_lease_id": ephemeral_coordinator_id,
        }, True

    return _mutate(mutate)


def complete_agent(lease_id: str, *, status: str, reason: str = "") -> dict[str, Any]:
    if status not in TERMINAL_STATUSES:
        raise GovernanceError("terminal agent status is invalid")

    def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cleanup_expired(state)
        for tree in state["trees"].values():
            if lease_id not in tree["leases"]:
                continue
            lease = tree["leases"][lease_id]
            if _active(lease):
                _terminalize(tree, lease_id, status, reason or status.lower())
            receipt = lease.get("terminal_receipt")
            if not isinstance(receipt, dict):
                raise GovernanceError("terminal lease omitted its receipt")
            return dict(receipt), True
        raise GovernanceError("agent lease is unknown")

    return _mutate(mutate)


def complete_reservation(
    reservation: dict[str, Any], *, status: str, reason: str = ""
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
