#!/usr/bin/env python3
"""Offline, deterministic test suite for the Cursor adaptive-model-router."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
LAB = SKILL_DIR / "scripts" / "router_lab.py"
SPEC = importlib.util.spec_from_file_location("router_lab", LAB)
assert SPEC and SPEC.loader
ROUTER_LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER_LAB)


def run(*arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LAB), *arguments],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def fresh_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CURSOR_HOME"] = str(home)
    env["ADAPTIVE_MODEL_ROUTER_HOME"] = str(home / "adaptive-model-router")
    return env


def fixture_catalog_path(tmp: Path) -> Path:
    curated = json.loads((SKILL_DIR / "assets" / "cursor-catalog.json").read_text())
    fixture = tmp / "cursor-models-fixture.json"
    fixture.write_text(
        json.dumps({"schema_version": 1, "source": "test-fixture", "live_slugs": [m["slug"] for m in curated["models"]]}),
        encoding="utf-8",
    )
    return fixture


def test_status_bootstraps_from_default_policy_before_refresh() -> None:
    with tempfile.TemporaryDirectory(prefix="cursor-router-status-") as tmp:
        env = fresh_env(Path(tmp))
        completed = run("status", env=env)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["tiers"]["T0"] == {"mode": "deterministic"}
        assert result["tiers"]["T1"]["agent"] == "fast_operator"
        assert result["tiers"]["T4"]["agent"] == "ultra_planner"
        assert result["catalog_status"] == "CATALOG_STALE"


def test_refresh_seeds_state_and_confirms_slugs_are_live() -> None:
    with tempfile.TemporaryDirectory(prefix="cursor-router-refresh-") as tmp:
        tmp_path = Path(tmp)
        env = fresh_env(tmp_path)
        env["CURSOR_MODEL_CATALOG"] = str(fixture_catalog_path(tmp_path))
        completed = run("refresh", env=env)
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["routing_status"] == "HEALTHY"
        assert result["catalog_status"] == "FRESH"
        assert (tmp_path / "adaptive-model-router" / "active-policy.json").is_file()
        assert (tmp_path / "adaptive-model-router" / "catalog.json").is_file()


def test_refresh_backs_up_a_foreign_pre_harness_stub() -> None:
    with tempfile.TemporaryDirectory(prefix="cursor-router-backup-") as tmp:
        tmp_path = Path(tmp)
        lab_home = tmp_path / "adaptive-model-router"
        lab_home.mkdir(parents=True)
        stub = lab_home / "active-policy.json"
        stub.write_text(json.dumps({"fable_requires_data_policy_ack": True}), encoding="utf-8")
        env = fresh_env(tmp_path)
        env["CURSOR_MODEL_CATALOG"] = str(fixture_catalog_path(tmp_path))
        completed = run("refresh", env=env)
        assert completed.returncode == 0, completed.stderr
        backups = list(lab_home.glob("active-policy.json.pre-harness-*"))
        assert len(backups) == 1
        assert json.loads(backups[0].read_text())["fable_requires_data_policy_ack"] is True


def test_refresh_fails_closed_when_a_seeded_slug_is_no_longer_live() -> None:
    with tempfile.TemporaryDirectory(prefix="cursor-router-missing-slug-") as tmp:
        tmp_path = Path(tmp)
        fixture = tmp_path / "sparse-catalog.json"
        fixture.write_text(json.dumps({"schema_version": 1, "live_slugs": ["auto"]}), encoding="utf-8")
        env = fresh_env(tmp_path)
        env["CURSOR_MODEL_CATALOG"] = str(fixture)
        completed = run("refresh", env=env)
        assert completed.returncode != 0
        assert "no longer reported live" in completed.stderr


def isolate_active_policy() -> None:
    """Point the in-process module at an empty lab home so tests never see a
    real ~/.cursor/adaptive-model-router (e.g. a pre-existing stub)."""
    tmp = Path(tempfile.mkdtemp(prefix="cursor-router-inprocess-"))
    ROUTER_LAB.LAB_HOME = tmp
    ROUTER_LAB.ACTIVE_POLICY_PATH = tmp / "active-policy.json"
    ROUTER_LAB.CATALOG_PATH = tmp / "catalog.json"


def test_resolve_phase_exact_check_is_deterministic_t0() -> None:
    isolate_active_policy()
    request = {
        "workflow_id": "w",
        "workflow_version": 1,
        "phase_id": "p",
        "activity": "exact_check",
        "mutation": "none",
        "scope": "local",
        "ambiguity": "none",
        "risk_level": "low",
        "risk_scope": "none",
        "risk_tags": [],
        "visual_required": False,
        "current_info_required": False,
        "external_action": False,
    }
    result = ROUTER_LAB.resolve_phase(request)
    assert result["mode"] == "deterministic"
    assert result["tier"] == "T0"
    assert result["execution_identity"] == "NOT_APPLICABLE"


def test_resolve_phase_visual_verify_floors_to_t2() -> None:
    isolate_active_policy()
    request = {
        "workflow_id": "w",
        "workflow_version": 1,
        "phase_id": "p",
        "activity": "verify",
        "mutation": "none",
        "scope": "local",
        "ambiguity": "none",
        "risk_level": "low",
        "risk_scope": "none",
        "risk_tags": [],
        "visual_required": True,
        "current_info_required": False,
        "external_action": False,
    }
    result = ROUTER_LAB.resolve_phase(request)
    assert result["mode"] == "model"
    assert result["tier"] == "T2"
    assert result["agent"] == "standard_worker"
    assert result["provider"] == "cursor"
    assert result["execution_identity"] == "REQUESTED_NOT_ATTESTED"
    assert result["runtime_evidence_required"] is True
    assert result["runtime_attestation_required"] is True


def test_resolve_phase_novel_critical_security_escalates_to_t4() -> None:
    isolate_active_policy()
    request = {
        "workflow_id": "w",
        "workflow_version": 1,
        "phase_id": "p",
        "activity": "frame",
        "mutation": "none",
        "scope": "local",
        "ambiguity": "novel",
        "risk_level": "critical",
        "risk_scope": "judgment",
        "risk_tags": ["security"],
        "visual_required": False,
        "current_info_required": False,
        "external_action": False,
    }
    result = ROUTER_LAB.resolve_phase(request)
    assert result["tier"] == "T4"
    assert result["agent"] == "ultra_planner"
    assert result["effort"] == "xhigh"


def test_resolve_phase_irreversible_mutation_requires_parent_gate() -> None:
    isolate_active_policy()
    request = {
        "workflow_id": "w",
        "workflow_version": 1,
        "phase_id": "p",
        "activity": "implement",
        "mutation": "irreversible",
        "scope": "local",
        "ambiguity": "none",
        "risk_level": "low",
        "risk_scope": "mutation",
        "risk_tags": [],
        "visual_required": False,
        "current_info_required": False,
        "external_action": False,
    }
    result = ROUTER_LAB.resolve_phase(request)
    assert result["parent_gate_required"] is True
    assert result["external_mutation_authorized"] is False
    assert result["tier"] == "T3"  # mutation:irreversible floor


def test_resolve_phase_rejects_unknown_fields() -> None:
    isolate_active_policy()
    try:
        ROUTER_LAB.resolve_phase({"workflow_id": "w", "workflow_version": 1, "phase_id": "p", "bogus": True})
    except ROUTER_LAB.RouterLabError as error:
        assert "unknown phase request fields" in str(error)
    else:
        raise AssertionError("unknown phase request field was accepted")


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok: {test.__name__}")
    print(f"{len(tests)} tests passed")
