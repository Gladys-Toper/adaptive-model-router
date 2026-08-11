#!/usr/bin/env python3
"""Deterministic checks for total-tree capacity and skill-read admission."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "agent_governance.py"
SPEC = importlib.util.spec_from_file_location("agent_governance", SCRIPT)
assert SPEC and SPEC.loader
GOV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GOV)
COMMIT = "9" * 40


def with_home(root: Path) -> None:
    os.environ["ADAPTIVE_WORKFLOW_GOVERNANCE_HOME"] = str(root)


def test_tree_cap_tokens_and_terminal_receipts(root: Path) -> None:
    with_home(root)
    coordinator = GOV.reserve_agent(
        tree_id="tree", role="coordinator", nested_capacity_tokens=8
    )["lease"]
    workers = []
    for number in range(8):
        workers.append(
            GOV.reserve_agent(
                tree_id="tree",
                role="worker",
                parent_lease_id=coordinator["lease_id"],
            )["lease"]
        )
    try:
        GOV.reserve_agent(
            tree_id="tree",
            role="reviewer",
            parent_lease_id=coordinator["lease_id"],
        )
    except GOV.GovernanceError as error:
        assert "capacity" in str(error)
    else:
        raise AssertionError("ninth child exceeded the hard total-tree cap")
    receipt = GOV.complete_agent(
        workers[0]["lease_id"], status="COMPLETED", reason="verified"
    )
    assert receipt["receipt_type"] == "agent-terminal"
    assert receipt["status"] == "COMPLETED"
    replacement = GOV.reserve_agent(
        tree_id="tree",
        role="monitor",
        parent_lease_id=coordinator["lease_id"],
    )["lease"]
    assert replacement["role"] == "monitor"


def test_nested_capacity_and_expiry_cleanup(root: Path) -> None:
    with_home(root)
    coordinator = GOV.reserve_agent(
        tree_id="nested", role="coordinator", nested_capacity_tokens=8
    )["lease"]
    parent = GOV.reserve_agent(
        tree_id="nested",
        role="nested",
        parent_lease_id=coordinator["lease_id"],
        nested_capacity_tokens=2,
        lease_seconds=1,
    )["lease"]
    child = GOV.reserve_agent(
        tree_id="nested",
        role="reviewer",
        parent_lease_id=parent["lease_id"],
        nested_capacity_tokens=1,
    )["lease"]
    assert child["nested_capacity_tokens"] == 1
    time.sleep(1.05)
    next_agent = GOV.reserve_agent(
        tree_id="nested",
        role="monitor",
        parent_lease_id=coordinator["lease_id"],
    )["lease"]
    assert next_agent["role"] == "monitor"
    state = GOV._load()
    terminal = state["trees"]["nested"]["leases"][parent["lease_id"]]
    assert terminal["terminal_status"] == "EXPIRED"
    assert terminal["terminal_receipt"]["receipt_sha256"]


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


def test_global_cap_cannot_be_bypassed_with_new_tree_ids(root: Path) -> None:
    with_home(root)
    coordinators = [
        GOV.reserve_agent(
            tree_id=f"tree-{index}", role="coordinator", nested_capacity_tokens=0
        )["lease"]
        for index in range(9)
    ]
    assert len(coordinators) == 9
    try:
        GOV.reserve_agent(
            tree_id="tree-bypass", role="coordinator", nested_capacity_tokens=0
        )
    except GOV.GovernanceError as error:
        assert "global" in str(error) and "cap" in str(error)
    else:
        raise AssertionError("a tenth agent bypassed the cap with a new tree ID")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="agent-governance-") as temporary:
        root = Path(temporary)
        test_tree_cap_tokens_and_terminal_receipts(root / "cap")
        test_nested_capacity_and_expiry_cleanup(root / "nested")
        test_required_skill_requires_fresh_read(root / "skills")
        test_direct_dispatch_closes_ephemeral_coordinator(root / "direct")
        test_global_cap_cannot_be_bypassed_with_new_tree_ids(root / "global")
