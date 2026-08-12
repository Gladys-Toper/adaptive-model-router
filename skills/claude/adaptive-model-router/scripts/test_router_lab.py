#!/usr/bin/env python3
"""Deterministic catalog-transition tests for the Claude Adaptive Model Router.

Ported from the Codex suite (adaptive-model-router/scripts/test_router_lab.py).
The catalog fixture is the *curated* Claude catalog rather than the Codex
``models_cache.json``: Claude Code exposes no local model catalog file, so the
checked-in curated snapshot is the selectable set, and "the current family" is
slug-set membership rather than a ``gpt-<major>.<minor>-<variant>`` version
window. Everything else — profile rendering and drift, tier eligibility, the
routing/evaluation health split, the resolve-phase contract, and staged-candidate
model enforcement — is the same contract as the Codex lab.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parent / "router_lab.py"
CURATED_CATALOG = Path(__file__).resolve().parent.parent / "assets" / "claude-catalog.json"
DISPATCHER = (
    Path(__file__).resolve().parents[2]
    / "adaptive-workflow-router"
    / "scripts"
    / "workflow_dispatch.py"
)
ROUTER: Any = None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def load_router(root: Path, catalog_source: Path) -> Any:
    """Import router_lab against an isolated home and an explicit catalog."""
    os.environ["ADAPTIVE_MODEL_ROUTER_TEST_ROOT"] = str(root)
    os.environ["CLAUDE_CONFIG_DIR"] = str(root / "claude")
    os.environ["ADAPTIVE_MODEL_ROUTER_HOME"] = str(root / "lab")
    os.environ["CLAUDE_AGENTS_DIR"] = str(root / "claude" / "agents")
    # the isolation guard requires every independent path to live under the
    # test root, so the curated catalog is copied in rather than referenced
    staged_catalog = root / "catalog-source.json"
    staged_catalog.parent.mkdir(parents=True, exist_ok=True)
    staged_catalog.write_bytes(catalog_source.read_bytes())
    os.environ["ADAPTIVE_ROUTER_CATALOG_SOURCE"] = str(staged_catalog)
    spec = importlib.util.spec_from_file_location(f"router_lab_{root.name}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def curated() -> dict[str, Any]:
    return json.loads(CURATED_CATALOG.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------


def test_curated_catalog_baselines_and_resolves_every_tier(root: Path) -> None:
    router = load_router(root, CURATED_CATALOG)
    router.sync_agents(False)
    refreshed = router.refresh_catalog(stage_new=False)
    assert refreshed["status"] == "BASELINED"
    assert refreshed["source"] == "claude-code-harness-curated"
    assert refreshed["staged_candidates"] == []

    report = router.status_report()
    assert report["doctor"] == "HEALTHY"
    assert report["routing_status"] == "HEALTHY"
    assert report["evaluation_status"] == "HEALTHY"
    assert report["catalog_status"] == "HEALTHY"
    assert report["workflow_routing_ready"] is True
    assert report["tiers"] == {
        "T1": {"agent": "fast-operator", "model": "haiku", "effort": "low"},
        "T2": {"agent": "standard-worker", "model": "sonnet", "effort": "medium"},
        "T3": {"agent": "high-solver", "model": "opus", "effort": "high"},
        "T4": {"agent": "ultra-planner", "model": "fable", "effort": "max"},
    }
    return router


def test_resolve_phase_emits_the_head_evidence_contract(router: Any) -> None:
    request = {
        "workflow_id": "coding.change",
        "workflow_version": 1,
        "phase_id": "frame",
        "activity": "plan",
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
    result = router.resolve_phase(request)
    assert result["schema_version"] == 1
    assert result["provider"] == "anthropic"
    assert result["service_tier"] == "default"
    assert result["execution_identity"] == "REQUESTED_PENDING_RUNTIME_METADATA"
    assert result["runtime_evidence_required"] is True
    # runtime metadata is declared-grade, so no external attestation is claimed
    assert result["runtime_attestation_required"] is False
    assert result["router_health"]["routing_status"] == "HEALTHY"
    assert result["external_mutation_authorized"] is False

    # a directly mutating phase is capped at the highest write-capable lane
    mutating = dict(request, activity="implement", mutation="reversible")
    assert router.resolve_phase(mutating)["tier"] != "T4"


def test_tier_eligibility_rejects_foreign_providers_and_efforts(router: Any) -> None:
    policy = router.load_policy()
    catalog = router.read_json(router.CATALOG_PATH)
    opus = copy.deepcopy(catalog["models"]["opus"])
    eligible, _ = router.tier_eligibility(policy, opus, "T3", "high")
    assert eligible is True

    foreign = copy.deepcopy(opus)
    foreign["provider"] = "openai"
    foreign["slug"] = "gpt-5.6-sol"
    eligible, reasons = router.tier_eligibility(policy, foreign, "T3", "high")
    assert eligible is False and reasons

    weak = copy.deepcopy(opus)
    weak["supported_efforts"] = ["low"]
    eligible, reasons = router.tier_eligibility(policy, weak, "T3", "high")
    assert eligible is False and reasons


def test_catalog_stale_is_maintenance_not_a_routing_outage(root: Path) -> None:
    moved = root / "moved-catalog.json"
    catalog = curated()
    catalog["models"].append(
        {
            **copy.deepcopy(catalog["models"][0]),
            "slug": "haiku-next",
            "model_id": "claude-haiku-next",
            "display_name": "Claude Haiku Next",
        }
    )
    write_json(moved, catalog)

    router = load_router(root, CURATED_CATALOG)
    router.sync_agents(False)
    router.refresh_catalog(stage_new=False)
    assert router.doctor()["status"] == "HEALTHY"

    # the live curated catalog moves ahead of the refreshed baseline
    router.CATALOG_SOURCE = moved
    health = router.doctor()
    assert health["status"] == "CATALOG_STALE"
    assert health["catalog_status"] == "STALE"
    assert health["refresh_required"] is True
    # routing is unaffected: this is catalog-experiment maintenance
    assert health["routing_status"] == "HEALTHY"
    assert health["workflow_routing_ready"] is True
    assert health["routing_issues"] == []
    # and resolve-phase still admits, surfacing the maintenance signal
    result = router.resolve_phase(
        {
            "workflow_id": "coding.change",
            "workflow_version": 1,
            "phase_id": "frame",
            "activity": "plan",
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
    )
    assert result["router_health"]["catalog_status"] == "STALE"
    return router


def test_profile_drift_degrades_routing(router: Any, staged_catalog: Path) -> None:
    router.CATALOG_SOURCE = staged_catalog
    assert router.doctor()["routing_status"] == "HEALTHY"
    policy = router.load_policy()
    profile = router.AGENTS_DIR / policy["tiers"]["T2"]["profile_file"]
    original = profile.read_text(encoding="utf-8")
    profile.write_text(original + "\nmanual drift\n", encoding="utf-8")
    health = router.doctor()
    assert health["routing_status"] == "DEGRADED"
    assert health["workflow_routing_ready"] is False
    assert any("installed profiles" in issue for issue in health["routing_issues"])
    try:
        router.resolve_phase(
            {
                "workflow_id": "coding.change",
                "workflow_version": 1,
                "phase_id": "frame",
                "activity": "plan",
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
        )
    except router.RouterLabError as error:
        assert "active routing is not ready" in str(error)
    else:  # pragma: no cover
        raise AssertionError("resolve-phase admitted a degraded routing lane")
    profile.write_text(original, encoding="utf-8")
    assert router.doctor()["routing_status"] == "HEALTHY"


def test_checked_in_profiles_match_default_policy(root: Path) -> None:
    """The shipped .md agent profiles must render byte-identically."""
    router = load_router(root, CURATED_CATALOG)
    policy = router.read_json(router.DEFAULT_POLICY_PATH)
    bundle = router.rendered_active_bundle(policy)
    for tier in router.VALID_TIERS:
        assignment = policy["tiers"][tier]
        rendered = bundle[assignment["profile_file"]]
        fields, _ = router.parse_profile_markdown(rendered)
        assert fields["name"] == assignment["agent"]
        assert fields["model"] == assignment["model"]
        assert fields["effort"] == assignment["effort"]
        # kebab-case names, never snake_case
        assert "_" not in fields["name"]
    # T4 is the read-only strategic lane
    t4 = bundle[policy["tiers"]["T4"]["profile_file"]]
    t4_fields, _ = router.parse_profile_markdown(t4)
    assert router.profile_is_read_only(t4_fields)


def test_cli_selectable_efforts_bind_the_headless_dispatch_lane(root: Path) -> None:
    """`cli_selectable` annotates which efforts `claude -p --effort` accepts.

    `claude --effort` takes only low|medium|high.  `xhigh` and `max` stay valid
    routing efforts — they are expressible in agent frontmatter and in
    Workflow-tool dispatch, which is how T4 runs — but they are unreachable from
    the headless CLI, and the dispatcher fails closed on them rather than
    silently downgrading to `high`.  This asserts the annotation covers the
    whole ladder the policy uses and still agrees with the dispatcher's own
    `HEADLESS_EFFORTS`, which is the enforcement point.  The dispatcher source
    is read as text rather than imported so this test never touches a home.
    """
    router = load_router(root, CURATED_CATALOG)
    policy = router.read_json(router.DEFAULT_POLICY_PATH)
    annotation = policy["constraints"]["cli_selectable_efforts"]
    assert annotation == {
        "low": True,
        "medium": True,
        "high": True,
        "xhigh": False,
        "max": False,
    }, "cli_selectable_efforts must annotate the whole Claude effort ladder"

    routed_efforts = set()
    for tier, assignment in policy["tiers"].items():
        if tier == "T0":
            continue
        for route in (assignment, *assignment.get("fallbacks", [])):
            routed_efforts.add(route["effort"])
    missing = routed_efforts - set(annotation)
    assert not missing, f"policy routes efforts with no cli_selectable annotation: {sorted(missing)}"

    # T4 and every T4 fallback are in-session lanes, not headless ones.
    for route in (
        policy["tiers"]["T4"],
        *policy["tiers"]["T4"].get("fallbacks", []),
    ):
        assert annotation[route["effort"]] is False, (
            "T4 is the in-session lane; its efforts must not be marked CLI-selectable"
        )

    source = DISPATCHER.read_text(encoding="utf-8")
    match = re.search(r"^HEADLESS_EFFORTS = \(([^)]*)\)", source, re.MULTILINE)
    assert match, "dispatcher no longer declares HEADLESS_EFFORTS"
    headless = set(re.findall(r'"([a-z]+)"', match.group(1)))
    selectable = {effort for effort, allowed in annotation.items() if allowed}
    assert headless == selectable, (
        "cli_selectable_efforts disagrees with the dispatcher's HEADLESS_EFFORTS: "
        f"{sorted(selectable)} != {sorted(headless)}"
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="claude-router-lab-") as temporary:
        base = Path(temporary)
        first = base / "a"
        router = test_curated_catalog_baselines_and_resolves_every_tier(first)
        test_resolve_phase_emits_the_head_evidence_contract(router)
        test_tier_eligibility_rejects_foreign_providers_and_efforts(router)
        stale_router = test_catalog_stale_is_maintenance_not_a_routing_outage(base / "b")
        test_profile_drift_degrades_routing(stale_router, base / "b" / "catalog-source.json")
        test_checked_in_profiles_match_default_policy(base / "c")
        test_cli_selectable_efforts_bind_the_headless_dispatch_lane(base / "d")
    print("claude router-lab tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
