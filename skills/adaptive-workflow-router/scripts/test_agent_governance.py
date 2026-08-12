#!/usr/bin/env python3
"""Deterministic checks for planned tree capacity and skill-read admission."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import uuid
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "agent_governance.py"
SPEC = importlib.util.spec_from_file_location("agent_governance", SCRIPT)
assert SPEC and SPEC.loader
GOV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GOV)
COMMIT = "9" * 40


def with_home(root: Path) -> None:
    os.environ["ADAPTIVE_WORKFLOW_GOVERNANCE_HOME"] = str(root)


def capacity_contract(
    tree_id: str,
    branch_count: int,
    *,
    dependencies: dict[str, list[str]] | None = None,
    mutation_scopes: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    dependencies = dependencies or {}
    mutation_scopes = mutation_scopes or {}
    now = time.time()
    branches = []
    for index in range(branch_count):
        branch_id = f"branch-{index:02d}"
        scopes = mutation_scopes.get(branch_id, [])
        branches.append(
            {
                "branch_id": branch_id,
                "depends_on": dependencies.get(branch_id, []),
                "mutation_scopes": scopes,
                "workflow_id": "coding.change",
                "workflow_version": 1,
                "phase_id": "inspect",
                "model": "gpt-5.6-luna",
                "effort": "low",
                "cost_weight": 0.5,
                "quota_weight": 0.75,
                "token_cap": 10_000,
                "model_cycle_cap": 1,
                "tool_cycle_cap": 0,
                "wall_time_seconds": 60,
                "plan_id": "a" * 64,
                "phase_key": "inspect",
                "dispatch_packet_sha256": f"{index:064x}",
                "sandbox": "workspace-write" if scopes else "read-only",
                "network_access": False,
                "tool_mode": "default",
                "mutation_authorized": bool(scopes),
            }
        )
    return GOV.build_capacity_contract(
        {
            "schema_version": 1,
            "contract_name": GOV.CAPACITY_REQUEST_NAME,
            "contract_version": 1,
            "tree_id": tree_id,
            "source_commit": COMMIT,
            "created_at_epoch": now,
            "expires_at_epoch": now + 3600,
            "branches": branches,
        }
    )


def complete_planned(
    reservation: dict[str, object], contract: dict[str, object]
) -> dict[str, object]:
    lease = reservation["lease"]
    assert isinstance(lease, dict)
    branch_id = lease["capacity_branch_id"]
    branch = next(
        item for item in contract["branches"] if item["branch_id"] == branch_id
    )
    artifact_root = GOV._home() / "completion-artifacts" / lease["lease_id"]
    artifact_root.mkdir(parents=True)
    parallel = {
        "contract_sha256": contract["contract_sha256"],
        "branch_id": branch_id,
        "dispatch_packet_sha256": branch["dispatch_packet_sha256"],
    }
    metadata_path = (artifact_root / "execution-metadata.json").resolve()
    manifest_path = (artifact_root / "input-manifest.json").resolve()
    metadata_path.write_text(
        GOV.canonical_json({"status": "COMPLETED", "issues": []}) + "\n",
        encoding="utf-8",
    )
    manifest_path.write_text(
        GOV.canonical_json(
            {
                "parallel_capacity": parallel,
                "dispatch_packet_sha256": branch["dispatch_packet_sha256"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = GOV.execution_registry_path()
    registry.parent.mkdir(parents=True, exist_ok=True)
    previous = "0" * 64
    if registry.exists():
        previous = json.loads(registry.read_text(encoding="utf-8").splitlines()[-1])[
            "receipt_sha256"
        ]
    receipt = {
        "schema_version": 1,
        "receipt_type": "execution",
        "receipt_id": uuid.uuid4().hex,
        "previous_receipt_sha256": previous,
        "status": "COMPLETED",
        "issues": [],
        "source_commit": contract["source_commit"],
        "dispatch_packet_sha256": branch["dispatch_packet_sha256"],
        "parallel_capacity": parallel,
        "agent_lease": {
            key: lease[key]
            for key in (
                "lease_id",
                "tree_id",
                "capacity_contract_sha256",
                "capacity_branch_id",
                "resource_weight",
            )
        },
        "verification": {
            "dispatcher_acceptance": True,
            "runtime_identity_verified": True,
            "issues": [],
        },
        "metadata_path": str(metadata_path),
        "metadata_sha256": GOV.sha256_bytes(metadata_path.read_bytes()),
        "input_manifest_path": str(manifest_path),
        "input_manifest_sha256": GOV.sha256_bytes(manifest_path.read_bytes()),
    }
    receipt["receipt_sha256"] = GOV.content_hash(receipt)
    with registry.open("a", encoding="utf-8") as handle:
        handle.write(GOV.canonical_json(receipt) + "\n")
    evidence = GOV.branch_completion_evidence(lease, receipt)
    return GOV.complete_reservation(
        reservation,
        status="COMPLETED",
        reason="verified dispatcher completion",
        completion_evidence=evidence,
    )


def test_tree_planned_tokens_and_terminal_receipts(root: Path) -> None:
    with_home(root)
    contract = capacity_contract("tree", 12)
    coordinator = GOV.reserve_agent(
        tree_id="tree",
        role="coordinator",
        nested_capacity_tokens=12,
        capacity_contract=contract,
    )["lease"]
    workers = []
    receipts = []
    for wave in contract["waves"]:
        admitted = [
            GOV.reserve_agent(
                tree_id="tree",
                role="worker",
                parent_lease_id=coordinator["lease_id"],
                capacity_contract=contract,
                capacity_branch_id=branch_id,
            )
            for branch_id in wave
        ]
        workers.extend(reservation["lease"] for reservation in admitted)
        receipts.extend(
            complete_planned(reservation, contract)["agent_terminal_receipt"]
            for reservation in admitted
        )
    try:
        GOV.reserve_agent(
            tree_id="tree",
            role="reviewer",
            parent_lease_id=coordinator["lease_id"],
            capacity_contract=contract,
            capacity_branch_id="branch-00",
        )
    except GOV.GovernanceError as error:
        assert "capacity" in str(error)
    else:
        raise AssertionError("a child exceeded the workflow-planned tree budget")
    receipt = receipts[0]
    assert receipt["receipt_type"] == "agent-terminal"
    assert receipt["status"] == "COMPLETED"
    try:
        GOV.reserve_agent(
            tree_id="tree",
            role="monitor",
            parent_lease_id=coordinator["lease_id"],
            capacity_contract=contract,
            capacity_branch_id="branch-00",
        )
    except GOV.GovernanceError as error:
        assert "consumed" in str(error)
    else:
        raise AssertionError("completed capacity branch was reused")


def test_capacity_waves_require_completed_dependencies(root: Path) -> None:
    with_home(root)
    contract = capacity_contract(
        "nested",
        2,
        dependencies={"branch-01": ["branch-00"]},
    )
    coordinator = GOV.reserve_agent(
        tree_id="nested",
        role="coordinator",
        nested_capacity_tokens=2,
        capacity_contract=contract,
    )["lease"]
    first_reservation = GOV.reserve_agent(
        tree_id="nested",
        role="worker",
        parent_lease_id=coordinator["lease_id"],
        capacity_contract=contract,
        capacity_branch_id="branch-00",
    )
    first = first_reservation["lease"]
    try:
        GOV.complete_agent(first["lease_id"], status="COMPLETED")
    except GOV.GovernanceError as error:
        assert "registered dispatcher evidence" in str(error)
    else:
        raise AssertionError("planned branch completed without dispatcher evidence")
    try:
        GOV.complete_agent(coordinator["lease_id"], status="ABORTED")
    except GOV.GovernanceError as error:
        assert "close-tree" in str(error)
    else:
        raise AssertionError("coordinator bypassed close-tree settlement")
    try:
        GOV.reserve_agent(
            tree_id="nested",
            role="reviewer",
            parent_lease_id=coordinator["lease_id"],
            capacity_contract=contract,
            capacity_branch_id="branch-01",
        )
    except GOV.GovernanceError as error:
        assert "dependency" in str(error) or "wave" in str(error)
    else:
        raise AssertionError("dependent branch started before its prerequisite")
    complete_planned(first_reservation, contract)
    second_reservation = GOV.reserve_agent(
        tree_id="nested",
        role="reviewer",
        parent_lease_id=coordinator["lease_id"],
        capacity_contract=contract,
        capacity_branch_id="branch-01",
    )
    second = second_reservation["lease"]
    assert second["capacity_branch_id"] == "branch-01"
    complete_planned(second_reservation, contract)
    coordinator_receipt = GOV.complete_coordinator(
        coordinator["lease_id"], status="COMPLETED", reason="all branches passed"
    )
    assert coordinator_receipt["status"] == "COMPLETED"
    state = GOV._load()
    terminal = state["trees"]["nested"]["leases"][first["lease_id"]]
    assert terminal["terminal_status"] == "COMPLETED"
    assert terminal["terminal_receipt"]["receipt_sha256"]


def test_capacity_scheduler_parallelizes_without_scope_conflicts() -> None:
    contract = capacity_contract(
        "waves",
        4,
        mutation_scopes={
            "branch-00": ["src/a.ts"],
            "branch-01": ["src/a.ts"],
        },
    )
    assert contract["descendant_tokens"] == 4
    scheduled = [branch_id for wave in contract["waves"] for branch_id in wave]
    assert sorted(scheduled) == [f"branch-{index:02d}" for index in range(4)]
    assert len(scheduled) == len(set(scheduled))
    assert all(
        not ({"branch-00", "branch-01"} <= set(wave))
        for wave in contract["waves"]
    )


def test_capacity_scheduler_treats_ancestor_scopes_as_conflicts() -> None:
    contract = capacity_contract(
        "hierarchical",
        3,
        mutation_scopes={
            "branch-00": ["src"],
            "branch-01": ["src/a.ts"],
            "branch-02": ["tests"],
        },
    )
    scheduled = [branch_id for wave in contract["waves"] for branch_id in wave]
    assert sorted(scheduled) == [f"branch-{index:02d}" for index in range(3)]
    assert len(scheduled) == len(set(scheduled))
    assert all(
        not ({"branch-00", "branch-01"} <= set(wave))
        for wave in contract["waves"]
    )


def test_capacity_snapshot_tampering_fails_closed() -> None:
    contract = capacity_contract("snapshot", 2)
    tampered = dict(contract)
    tampered["capacity_snapshot"] = dict(contract["capacity_snapshot"])
    tampered["capacity_snapshot"].update(
        {
            "host_logical_cpus": 1_998,
            "provider_weight_ceiling": 999.0,
            "available_weight_units": 999.0,
        }
    )
    snapshot_identity = dict(tampered["capacity_snapshot"])
    snapshot_identity.pop("snapshot_sha256")
    tampered["capacity_snapshot"]["snapshot_sha256"] = GOV.content_hash(
        snapshot_identity
    )
    identity = dict(tampered)
    identity.pop("contract_sha256")
    tampered["contract_sha256"] = GOV.content_hash(identity)
    try:
        GOV.validate_capacity_contract(
            tampered, tree_id="snapshot", source_commit=COMMIT
        )
    except GOV.GovernanceError as error:
        assert "snapshot" in str(error) or "derived" in str(error)
    else:
        raise AssertionError("caller-authored capacity survived validation")
    try:
        GOV.reserve_agent(
            tree_id="snapshot",
            role="coordinator",
            nested_capacity_tokens=2,
            capacity_contract=tampered,
        )
    except GOV.GovernanceError as error:
        assert "snapshot" in str(error) or "host" in str(error)
    else:
        raise AssertionError("caller-authored capacity obtained a coordinator lease")


def test_weighted_capacity_is_conserved_across_trees(root: Path) -> None:
    with_home(root)
    left_contract = capacity_contract("left", 32)
    right_contract = capacity_contract("right", 1)
    left = GOV.reserve_agent(
        tree_id="left",
        role="coordinator",
        nested_capacity_tokens=32,
        capacity_contract=left_contract,
    )["lease"]
    right = GOV.reserve_agent(
        tree_id="right",
        role="coordinator",
        nested_capacity_tokens=1,
        capacity_contract=right_contract,
    )["lease"]
    admitted = [
        GOV.reserve_agent(
            tree_id="left",
            role="worker",
            parent_lease_id=left["lease_id"],
            capacity_contract=left_contract,
            capacity_branch_id=branch_id,
        )
        for branch_id in left_contract["waves"][0]
    ]
    try:
        GOV.reserve_agent(
            tree_id="right",
            role="worker",
            parent_lease_id=right["lease_id"],
            capacity_contract=right_contract,
            capacity_branch_id="branch-00",
        )
    except GOV.GovernanceError as error:
        assert "global weighted" in str(error)
    else:
        raise AssertionError("a second tree oversubscribed global weighted capacity")
    complete_planned(admitted[0], left_contract)
    released = GOV.reserve_agent(
        tree_id="right",
        role="worker",
        parent_lease_id=right["lease_id"],
        capacity_contract=right_contract,
        capacity_branch_id="branch-00",
    )["lease"]
    assert released["resource_weight"] == 0.75


def test_required_skill_requires_fresh_read(root: Path) -> None:
    with_home(root)
    root.mkdir(parents=True, exist_ok=True)
    skill = root / "SKILL.md"
    skill.write_text("skill body\n", encoding="utf-8")
    receipt = GOV.record_skill_read(
        name="sample", path=skill, source_commit=COMMIT, max_age_seconds=60
    )
    requirement = {
        "name": "sample",
        "path": str(skill.resolve()),
        "skill_sha256": receipt["skill_sha256"],
        "read_receipt_id": receipt["receipt_id"],
        "max_age_seconds": 60,
    }
    assert GOV.validate_skill_requirements([requirement], source_commit=COMMIT) == {
        "sample": receipt["skill_sha256"]
    }
    skill.write_text("changed\n", encoding="utf-8")
    try:
        GOV.validate_skill_requirements([requirement], source_commit=COMMIT)
    except GOV.GovernanceError as error:
        assert "stale" in str(error)
    else:
        raise AssertionError("changed skill was accepted without a new read")


def test_direct_dispatch_closes_ephemeral_coordinator(root: Path) -> None:
    with_home(root)
    reservation = GOV.reserve_agent(tree_id="direct", role="reviewer")
    ephemeral = reservation["ephemeral_coordinator_lease_id"]
    assert isinstance(ephemeral, str) and ephemeral
    receipts = GOV.complete_reservation(
        reservation, status="COMPLETED", reason="review complete"
    )
    assert receipts["agent_terminal_receipt"]["status"] == "COMPLETED"
    assert receipts["coordinator_terminal_receipt"]["lease_id"] == ephemeral
    state = GOV._load()
    tree = state["trees"]["direct"]
    assert GOV._tree_active_count(tree) == 0


def test_expired_direct_dispatch_is_terminalized(root: Path) -> None:
    with_home(root)
    reservation = GOV.reserve_agent(
        tree_id="expires", role="monitor", lease_seconds=1
    )
    time.sleep(1.05)
    status = GOV.capacity_status()
    assert status["active_leases"] == 0
    state = GOV._load()
    lease_id = reservation["lease"]["lease_id"]
    lease = state["trees"]["expires"]["leases"][lease_id]
    assert lease["terminal_status"] == "EXPIRED"
    assert lease["terminal_receipt"]["receipt_sha256"]


def test_no_arbitrary_global_agent_count_cap(root: Path) -> None:
    with_home(root)
    coordinators = [
        GOV.reserve_agent(
            tree_id=f"tree-{index}", role="coordinator", nested_capacity_tokens=0
        )["lease"]
        for index in range(32)
    ]
    assert len(coordinators) == 32
    status = GOV.capacity_status()
    assert status["fixed_agent_count_cap"] is False
    assert status["active_leases"] == 32
    assert status["active_trees"] == 32


def test_install_journal_blocks_new_admission(root: Path) -> None:
    with_home(root)
    root.mkdir(parents=True, exist_ok=True)
    GOV.install_journal_path().write_text("{}\n", encoding="utf-8")
    try:
        GOV.reserve_agent(tree_id="blocked", role="worker")
    except GOV.GovernanceError as error:
        assert "installation blocks" in str(error)
    else:
        raise AssertionError("new agent entered after installation admission barrier")


def test_legacy_nine_agent_state_migrates_without_losing_receipts(root: Path) -> None:
    with_home(root)
    root.mkdir(parents=True, exist_ok=True)
    legacy = {
        "schema_version": 1,
        "max_active_agents": 9,
        "trees": {},
        "skill_reads": [{"sentinel": "preserved"}],
    }
    GOV._atomic_write(GOV.state_path(), legacy)
    coordinator = GOV.reserve_agent(
        tree_id="migrated", role="coordinator", nested_capacity_tokens=0
    )["lease"]
    assert coordinator["nested_capacity_tokens"] == 0
    migrated = GOV._load()
    assert migrated["schema_version"] == 2
    assert migrated["capacity_mode"] == "workflow-planned-descendant-tokens"
    assert "max_active_agents" not in migrated
    assert migrated["skill_reads"] == [{"sentinel": "preserved"}]


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agent-governance-") as temporary:
        root = Path(temporary)
        test_tree_planned_tokens_and_terminal_receipts(root / "cap")
        test_capacity_waves_require_completed_dependencies(root / "nested")
        test_capacity_scheduler_parallelizes_without_scope_conflicts()
        test_capacity_scheduler_treats_ancestor_scopes_as_conflicts()
        test_capacity_snapshot_tampering_fails_closed()
        test_weighted_capacity_is_conserved_across_trees(root / "weighted")
        test_required_skill_requires_fresh_read(root / "skills")
        test_direct_dispatch_closes_ephemeral_coordinator(root / "direct")
        test_expired_direct_dispatch_is_terminalized(root / "expires")
        test_no_arbitrary_global_agent_count_cap(root / "global")
        test_install_journal_blocks_new_admission(root / "install-barrier")
        test_legacy_nine_agent_state_migrates_without_losing_receipts(
            root / "migration"
        )
