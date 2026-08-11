#!/usr/bin/env python3
"""Deterministic catalog-transition tests for the Adaptive Model Router."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import tempfile
import types
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parent / "router_lab.py"
ROUTER: Any = None


def raw_model(slug: str, instructions: str) -> dict[str, Any]:
    return {
        "slug": slug,
        "visibility": "list",
        "supported_reasoning_levels": [
            {"effort": effort}
            for effort in ("low", "medium", "high", "ultra")
        ],
        "input_modalities": ["text", "image"],
        "shell_type": "shell_command",
        "apply_patch_tool_type": "freeform",
        "supports_parallel_tool_calls": True,
        "supports_search_tool": True,
        "supported_in_api": True,
        "context_window": 272000,
        "max_context_window": 272000,
        "effective_context_window_percent": 95,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "comp_hash": "3000",
        "multi_agent_version": "v2",
        "tool_mode": "code_mode_only",
        "web_search_tool_type": "text_and_image",
        "experimental_supported_tools": [],
        "service_tiers": [{"id": "priority"}],
        "additional_speed_tiers": ["fast"],
        "supports_image_detail_original": True,
        "supports_reasoning_summaries": True,
        "support_verbosity": True,
        "use_responses_lite": True,
        "base_instructions": instructions,
        "model_messages": {"instructions_template": instructions},
    }


def raw_catalog(sol_instructions: str) -> dict[str, Any]:
    return {
        "fetched_at": "2026-07-16T00:00:00Z",
        "etag": "test",
        "client_version": "test",
        "models": [
            raw_model("gpt-5.6-luna", "luna"),
            raw_model("gpt-5.6-terra", "terra"),
            raw_model("gpt-5.6-sol", sol_instructions),
        ],
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(ROUTER.canonical_json(value) + "\n" for value in values),
        encoding="utf-8",
    )


def refresh_revision(model: dict[str, Any]) -> None:
    material = copy.deepcopy(model)
    material.pop("revision_hash", None)
    model["revision_hash"] = ROUTER.content_id(material)


def test_presence_aware_change_classification(old_catalog: dict[str, Any]) -> None:
    slug = "gpt-5.6-sol"
    instruction_current = copy.deepcopy(old_catalog)
    instruction_current["models"][slug]["instruction_hash"] = "new-instructions"
    refresh_revision(instruction_current["models"][slug])
    changes = ROUTER.catalog_change_classification(
        old_catalog, instruction_current, schema_upgrade=False
    )
    assert changes["instruction_only"] == [slug]
    assert changes["semantic_changed"] == []
    assert changes["change_details"][slug]["changed_fields"] == [
        "instruction_hash"
    ]

    semantic_fields = {
        "comp_hash": "4000",
        "provider": "different-provider",
        "visibility": "hidden",
        "supported_reasoning_levels": ["low"],
        "input_modalities": ["text"],
        "shell_type": "different-shell",
        "apply_patch_tool_type": None,
        "context_window": 1,
        "service_tiers": ["different-tier"],
    }
    for field, value in semantic_fields.items():
        current = copy.deepcopy(old_catalog)
        current["models"][slug][field] = value
        refresh_revision(current["models"][slug])
        classified = ROUTER.catalog_change_classification(
            old_catalog, current, schema_upgrade=False
        )
        assert classified["instruction_only"] == [], field
        assert classified["semantic_changed"] == [slug], field
        assert field in classified["change_details"][slug]["changed_fields"], field

    missing_old = copy.deepcopy(old_catalog)
    del missing_old["models"][slug]["comp_hash"]
    refresh_revision(missing_old["models"][slug])
    presence_change = ROUTER.catalog_change_classification(
        missing_old, old_catalog, schema_upgrade=False
    )
    assert presence_change["semantic_changed"] == [slug]
    assert "comp_hash" in presence_change["change_details"][slug]["changed_fields"]

    unknown_current = copy.deepcopy(old_catalog)
    unknown_current["models"][slug]["future_semantic_field"] = "changed"
    refresh_revision(unknown_current["models"][slug])
    unknown_change = ROUTER.catalog_change_classification(
        old_catalog, unknown_current, schema_upgrade=False
    )
    assert unknown_change["semantic_changed"] == [slug]
    assert (
        ROUTER.UNCLASSIFIED_MODEL_FIELDS_MARKER
        in unknown_change["change_details"][slug]["changed_fields"]
    )


def test_instruction_only_refresh_preserves_active_policy(root: Path) -> None:
    catalog_source = ROUTER.CATALOG_SOURCE
    write_json(catalog_source, raw_catalog("old-sol-instructions"))
    old_catalog = ROUTER.read_catalog_snapshot()

    policy = json.loads(ROUTER.DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    policy["constraints"]["model_version_window"] = {
        "max_version_lag": 0,
        "latest_observed_family": "5.6",
        "allowed_families": ["5.6"],
    }
    policy["policy_id"] = ROUTER.policy_id(policy)
    ROUTER.atomic_write_json(ROUTER.ACTIVE_POLICY_PATH, policy)
    ROUTER.atomic_write_json(ROUTER.CATALOG_PATH, old_catalog)
    ROUTER.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in ROUTER.rendered_active_bundle(policy).items():
        ROUTER.atomic_write_text(ROUTER.AGENTS_DIR / name, text)

    candidates = {"schema_version": 1, "items": []}
    old_candidate = ROUTER.stage_candidate_internal(
        candidates,
        old_catalog,
        policy,
        "gpt-5.6-sol",
        "T2",
        trigger="test-old-instruction-contract",
    )
    assert old_candidate is not None
    top_tier = old_candidate["top_tier_baseline_binding"]
    assert top_tier["required"] is True
    assert top_tier["reference_tier"] == "T4"
    assert top_tier["model"] == policy["tiers"]["T4"]["model"]
    assert top_tier["effort"] == policy["tiers"]["T4"]["effort"]
    parsed_control = __import__("tomllib").loads(
        (ROUTER.AGENTS_DIR / top_tier["profile_file"]).read_text(encoding="utf-8")
    )
    assert parsed_control["model"] == "gpt-5.6-sol"
    assert parsed_control["model_reasoning_effort"] == "ultra"
    ROUTER.atomic_write_json(ROUTER.CANDIDATES_PATH, candidates)

    policy_before = ROUTER.ACTIVE_POLICY_PATH.read_bytes()
    profiles_before = ROUTER.installed_bundle_hashes()
    write_json(catalog_source, raw_catalog("new-sol-instructions"))
    result = ROUTER.refresh_catalog(stage_new=True)

    assert result["status"] == "CHANGED"
    assert result["failovers"] == []
    assert result["changes"]["instruction_only"] == ["gpt-5.6-sol"]
    assert result["changes"]["semantic_changed"] == []
    assert ROUTER.ACTIVE_POLICY_PATH.read_bytes() == policy_before
    assert ROUTER.installed_bundle_hashes() == profiles_before
    assert [item["tier"] for item in result["active_revision_rebaselines"]] == [
        "T4"
    ]
    rebaseline = result["active_revision_rebaselines"][0]
    assert rebaseline["model"] == "gpt-5.6-sol"
    assert rebaseline["effort"] == "ultra"
    assert rebaseline["changed_fields"] == ["instruction_hash"]
    assert rebaseline["old_instruction_hash"] != rebaseline["new_instruction_hash"]
    assert rebaseline["old_revision_hash"] != rebaseline["new_revision_hash"]

    state = ROUTER.load_candidates()["items"]
    old = next(
        item for item in state if item["candidate_id"] == old_candidate["candidate_id"]
    )
    assert old["status"] == "stale"
    active_candidates = [
        item
        for item in state
        if item.get("status") in {"staged", "collecting", "qualified"}
    ]
    assert len(active_candidates) == 1
    replacement = active_candidates[0]
    assert replacement["tier"] == "T2"
    assert replacement["candidate_model"] == "gpt-5.6-sol"
    assert replacement["candidate_revision_hash"] == rebaseline[
        "new_revision_hash"
    ]

    history = ROUTER.read_jsonl(ROUTER.CATALOG_HISTORY_PATH)
    assert history[-1]["active_revision_rebaselines"] == result[
        "active_revision_rebaselines"
    ]
    health = ROUTER.doctor()
    assert health["catalog_status"] == "HEALTHY"
    assert health["routing_status"] == "HEALTHY"
    assert health["refresh_required"] is False
    assert not ROUTER.CATALOG_REFRESH_JOURNAL_PATH.exists()
    assert not ROUTER.CATALOG_REFRESH_BACKUP_DIR.exists()

    current_catalog = ROUTER.read_json(ROUTER.CATALOG_PATH)
    overfull = ROUTER.load_candidates()
    for tier in ("T1", "T3"):
        extra = ROUTER.stage_candidate_internal(
            overfull,
            current_catalog,
            policy,
            "gpt-5.6-sol",
            tier,
            trigger="test-overfull-reconciliation",
        )
        assert extra is not None
    ROUTER.atomic_write_json(ROUTER.CANDIDATES_PATH, overfull)
    assert len(ROUTER.active_prepromotion_candidates(overfull)) == 3
    reconciled = ROUTER.refresh_catalog(stage_new=True)
    assert reconciled["status"] == "RECONCILED"
    remaining = ROUTER.active_prepromotion_candidates(ROUTER.load_candidates())
    assert len(remaining) == 1
    assert remaining[0]["tier"] == "T2"

    try:
        ROUTER.evaluate_candidate(old_candidate["candidate_id"], promote=True)
    except ROUTER.RouterLabError as error:
        assert "candidate is stale" in str(error)
    else:
        raise AssertionError("stale pre-refresh evidence remained promotion-eligible")


def test_mutating_execution_never_selects_the_read_only_t4_profile() -> None:
    base = {
        "workflow_id": "adaptive-router.capability-regression",
        "workflow_version": 1,
        "phase_id": "critical_cross_system_phase",
        "activity": "implement",
        "scope": "cross_system",
        "ambiguity": "novel",
        "risk_level": "critical",
        "risk_scope": "mutation",
        "risk_tags": ["production"],
        "visual_required": False,
        "current_info_required": False,
        "external_action": False,
    }
    strategic = ROUTER.resolve_phase({**base, "mutation": "none"})
    assert strategic["tier"] == "T4"
    mutating = ROUTER.resolve_phase({**base, "mutation": "reversible"})
    assert mutating["tier"] == "T3"
    assert "mutation_execution_ceiling:T3" in mutating["reason_codes"]
    assert mutating["profile_file"] == "high-solver.toml"


def baseline_fixture() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    binding = {
        "required": True,
        "reference_tier": "T4",
        "model": "gpt-5.6-sol",
        "effort": "ultra",
        "model_revision_hash": "revision",
        "profile_sha256": "a" * 64,
        "catalog_semantic_hash": "b" * 64,
        "runner_identity_sha256": "c" * 64,
        "config_sha256": ROUTER.content_id(
            ROUTER.top_tier_baseline_config(ROUTER.load_policy())
        ),
    }
    candidate = {"candidate_id": "candidate", "tier": "T1", "top_tier_baseline_binding": binding}
    candidates: list[dict[str, Any]] = []
    controls: list[dict[str, Any]] = []
    for index in range(5):
        case_id = f"baseline-{index}"
        family = ("documents", "commands", "reviews")[index % 3]
        candidate_record = {
            "observation_id": f"candidate-{index}",
            "run_id": f"candidate-run-{index}",
            "candidate_id": "candidate",
            "phase": "prepromotion",
            "eligible": True,
            "arm": "candidate",
            "case_id": case_id,
            "family": family,
            "holdout": index == 4,
            "input_manifest_sha256": f"{index + 1:064x}",
            "grader_kind": "deterministic",
            "grader_id_sha256": f"{index + 11:064x}",
            "service_tier": "default",
            "token_cap": 20000,
            "wall_time_cap_ms": 180000,
            "runner_version": "codex-cli test",
            "passed": True,
            "quality_score": 0.99,
            "tokens": 800,
            "latency_ms": 800,
        }
        identity = {
            "tier": "T1",
            "case_id": case_id,
            "input_manifest_sha256": candidate_record["input_manifest_sha256"],
            "grader_kind": "deterministic",
            "grader_id_sha256": candidate_record["grader_id_sha256"],
            "service_tier": "default",
            "token_cap": 20000,
            "wall_time_cap_ms": 180000,
            "profile_sha256": binding["profile_sha256"],
            "model": binding["model"],
            "effort": binding["effort"],
            "model_revision_hash": binding["model_revision_hash"],
            "catalog_semantic_hash": binding["catalog_semantic_hash"],
            "runner_identity_sha256": binding["runner_identity_sha256"],
            "runner_version": "codex-cli test",
            "config_sha256": binding["config_sha256"],
            "family": family,
            "holdout": index == 4,
        }
        control = {
            "observation_id": f"control-{index}",
            "run_id": f"control-run-{index}",
            "recorded_at": ROUTER.utc_now(),
            "eligible": True,
            "critical_failure": False,
            "verified_safety_violations": [],
            "passed": True,
            "quality_score": 1.0,
            "tokens": 1000,
            "latency_ms": 1000,
            "top_tier_cache_identity": identity,
        }
        candidates.append(candidate_record)
        controls.append(control)
    return candidate, candidates, controls


def test_top_tier_baseline_gate_and_cache() -> None:
    policy = ROUTER.load_policy()
    candidate, observations, controls = baseline_fixture()
    write_jsonl(ROUTER.TOP_TIER_CONTROLS_PATH, [])
    missing = ROUTER.top_tier_baseline_metrics(candidate, observations, policy)
    assert missing["state"] == "COLLECTING"

    write_jsonl(ROUTER.TOP_TIER_CONTROLS_PATH, controls)
    passing = ROUTER.top_tier_baseline_metrics(candidate, observations, policy)
    assert passing["state"] == "PASSED"
    assert passing["pairs"] == 5
    assert passing["median_token_gain"] == 0.2
    assert passing["median_latency_gain"] == 0.2
    assert passing["positive_token_cases"] == 5
    assert passing["positive_latency_cases"] == 5

    latency_only_failure = copy.deepcopy(controls)
    for control in latency_only_failure:
        control["latency_ms"] = 840
    write_jsonl(ROUTER.TOP_TIER_CONTROLS_PATH, latency_only_failure)
    one_metric = ROUTER.top_tier_baseline_metrics(candidate, observations, policy)
    assert one_metric["state"] == "FAILED"
    assert one_metric["median_token_gain"] >= 0.10
    assert one_metric["median_latency_gain"] < 0.10

    mismatched = copy.deepcopy(controls)
    mismatched[0]["top_tier_cache_identity"]["grader_id_sha256"] = "f" * 64
    write_jsonl(ROUTER.TOP_TIER_CONTROLS_PATH, mismatched)
    mismatch = ROUTER.top_tier_baseline_metrics(candidate, observations, policy)
    assert mismatch["state"] == "COLLECTING"

    expired = copy.deepcopy(controls)
    old = "2026-01-01T00:00:00Z"
    for control in expired:
        control["recorded_at"] = old
    write_jsonl(ROUTER.TOP_TIER_CONTROLS_PATH, expired)
    stale = ROUTER.top_tier_baseline_metrics(candidate, observations, policy)
    assert stale["state"] == "COLLECTING"

    critical = copy.deepcopy(controls)
    critical[0]["critical_failure"] = True
    write_jsonl(ROUTER.TOP_TIER_CONTROLS_PATH, critical)
    unsafe = ROUTER.top_tier_baseline_metrics(candidate, observations, policy)
    assert unsafe["state"] == "COLLECTING"

    exempt = ROUTER.top_tier_baseline_metrics(
        {"candidate_id": "t4", "tier": "T4"}, [], policy
    )
    assert exempt["state"] == "EXEMPT"


def test_input_manifest_validation(root: Path) -> None:
    policy = ROUTER.load_policy()
    manifest = {
        "schema_version": 1,
        "case_id": "case",
        "prompt": "bounded prompt",
        "context": "embedded",
        "tools": [],
        "permissions": {"sandbox": "read-only", "approval_policy": "never"},
        "retry_rule": {"cognitive_retries": 0, "transient_retries": 0},
        "service_tier": None,
        "token_cap": 20000,
        "wall_time_cap_ms": 180000,
    }
    path = root / "manifest.json"
    write_json(path, manifest)
    args = types.SimpleNamespace(
        input_manifest_file=str(path.resolve()),
        input_manifest_sha256=ROUTER.sha256_bytes(path.read_bytes()),
        case_id="case",
    )
    validated = ROUTER.validate_input_manifest(args, policy)
    assert validated["service_tier"] == "default"
    assert validated["token_cap"] == 20000

    missing_prompt = copy.deepcopy(manifest)
    missing_prompt.pop("prompt")
    write_json(path, missing_prompt)
    args.input_manifest_sha256 = ROUTER.sha256_bytes(path.read_bytes())
    try:
        ROUTER.validate_input_manifest(args, policy)
    except ROUTER.RouterLabError as error:
        assert "prompt" in str(error)
    else:
        raise AssertionError("manifest without workload bindings was accepted")


def test_transcript_latency_is_bound_and_derived() -> None:
    events = [
        {
            "method": "turn/started",
            "params": {
                "threadId": "thread",
                "turn": {"id": "turn", "startedAt": 100},
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "other",
                "turn": {
                    "id": "turn",
                    "startedAt": 100,
                    "completedAt": 101,
                    "durationMs": 1,
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread",
                "turn": {
                    "id": "turn",
                    "startedAt": 100,
                    "completedAt": 105,
                    "durationMs": 4823,
                },
            },
        },
    ]
    assert ROUTER.transcript_turn_latency_ms(events, "thread", "turn") == 4823
    zero_duration = copy.deepcopy(events)
    zero_duration[-1]["params"]["turn"]["completedAt"] = 100
    zero_duration[-1]["params"]["turn"]["durationMs"] = 0
    try:
        ROUTER.transcript_turn_latency_ms(zero_duration, "thread", "turn")
    except ROUTER.RouterLabError as error:
        assert "timing" in str(error)
    else:
        raise AssertionError("zero transcript duration was accepted")


def load_isolated_router(root: Path) -> Any:
    codex_home = root / "codex-home"
    os.environ["ADAPTIVE_MODEL_ROUTER_TEST_ROOT"] = str(root)
    os.environ["CODEX_HOME"] = str(codex_home)
    os.environ["ADAPTIVE_MODEL_ROUTER_HOME"] = str(
        codex_home / "adaptive-model-router"
    )
    os.environ["CODEX_AGENTS_DIR"] = str(codex_home / "agents")
    os.environ["CODEX_MODEL_CATALOG"] = str(codex_home / "models_cache.json")
    spec = importlib.util.spec_from_file_location("router_lab_isolated", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="adaptive-router-regression-") as temporary:
        test_root = Path(temporary)
        ROUTER = load_isolated_router(test_root)
        write_json(ROUTER.CATALOG_SOURCE, raw_catalog("old-sol-instructions"))
        baseline = ROUTER.read_catalog_snapshot()
        test_presence_aware_change_classification(baseline)
        test_instruction_only_refresh_preserves_active_policy(test_root)
        test_mutating_execution_never_selects_the_read_only_t4_profile()
        test_top_tier_baseline_gate_and_cache()
        test_input_manifest_validation(test_root)
        test_transcript_latency_is_bound_and_derived()
    print("adaptive-model-router deterministic tests passed")
