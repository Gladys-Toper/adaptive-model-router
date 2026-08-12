#!/usr/bin/env python3
"""Discover, resolve, and report Cursor's adaptive model-routing policy.

This is the Cursor port of the adaptive routing harness's model router. It is
a deliberately scoped-down sibling of the Codex reference
(`adaptive-model-router/scripts/router_lab.py`): it keeps the same T0-T4 tier
model and the same activity/mutation/scope/ambiguity/risk-tag tier-floor
heuristic (`resolve_phase`), but ships no catalog-experiment, promotion,
canary, or rollback machinery, and no App-Server-style evidence verification.
See ../references/self-improvement-protocol.md for why.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parent.parent
ASSET_DIR = SKILL_DIR / "assets"
DEFAULT_POLICY_PATH = ASSET_DIR / "default-policy.json"
CURATED_CATALOG_PATH = ASSET_DIR / "cursor-catalog.json"
DISPATCH_CONTRACT_PATH = SKILL_DIR / "references" / "dispatch-contract.md"

CURSOR_HOME = Path(os.environ.get("CURSOR_HOME", Path.home() / ".cursor")).expanduser()
LAB_HOME = Path(
    os.environ.get("ADAPTIVE_MODEL_ROUTER_HOME", CURSOR_HOME / "adaptive-model-router")
).expanduser()

ACTIVE_POLICY_PATH = LAB_HOME / "active-policy.json"
CATALOG_PATH = LAB_HOME / "catalog.json"

VALID_TIERS = ("T0", "T1", "T2", "T3", "T4")
TIER_INDEX = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}
INDEX_TIER = {value: key for key, value in TIER_INDEX.items()}

# Platform-neutral phase-routing taxonomy, kept identical in spirit to the
# Codex reference's tables so tier assignment behaves the same way for the
# same phase facts on every platform (binding contract #4).
ACTIVITY_BASE_TIERS = {
    "wait": "T0",
    "poll": "T0",
    "exact_check": "T0",
    "inspect": "T1",
    "retrieve": "T1",
    "summarize": "T1",
    "prepare_external": "T1",
    "frame": "T2",
    "design": "T2",
    "implement": "T2",
    "analyze": "T2",
    "draft": "T2",
    "verify": "T2",
    "interpret": "T2",
    "execute_runbook": "T2",
    "diagnose": "T2",
    "synthesize": "T3",
    "plan": "T3",
    "review": "T2",
    "adjudicate": "T3",
    "release": "T3",
    "architecture": "T4",
    "threat_model": "T4",
}
MUTATION_FLOORS = {
    "none": "T0",
    "reversible": "T2",
    "persistent": "T2",
    "irreversible": "T3",
}
MUTATION_EXECUTION_CEILING = "T3"
SCOPE_FLOORS = {
    "local": "T0",
    "multi_file": "T2",
    "single_system": "T2",
    "cross_system": "T3",
}
AMBIGUITY_FLOORS = {
    "none": "T0",
    "bounded": "T2",
    "high": "T3",
    "novel": "T4",
}
RISK_LEVEL_FLOORS = {
    "low": "T1",
    "moderate": "T2",
    "high": "T3",
    "critical": "T4",
}
RISK_SCOPES = {"none", "evidence", "judgment", "mutation", "authority"}
RISK_TAG_FLOORS = {
    "public_interface": "T2",
    "dependencies": "T2",
    "build": "T2",
    "data_shape": "T2",
    "auth": "T3",
    "secrets": "T3",
    "billing": "T3",
    "security": "T3",
    "schema": "T3",
    "production": "T3",
    "data_loss": "T3",
    "privacy": "T3",
    "legal": "T3",
    "medical": "T3",
    "financial": "T3",
    "release": "T3",
}


class RouterLabError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def content_id(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise RouterLabError(f"missing or unsafe file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RouterLabError(f"refusing symlink destination: {path}")
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def load_default_policy() -> dict[str, Any]:
    return read_json(DEFAULT_POLICY_PATH)


def policy_id(policy: dict[str, Any]) -> str:
    identity = {key: value for key, value in policy.items() if key != "updated_at"}
    return content_id(identity)


def load_policy() -> dict[str, Any]:
    """Bootstrap from the seeded default policy if the lab home is empty."""
    if not ACTIVE_POLICY_PATH.exists():
        return load_default_policy()
    return read_json(ACTIVE_POLICY_PATH)


def load_catalog() -> dict[str, Any]:
    if CATALOG_PATH.exists():
        return read_json(CATALOG_PATH)
    return read_json(CURATED_CATALOG_PATH)


def catalog_slugs(catalog: dict[str, Any]) -> set[str]:
    return {model["slug"] for model in catalog.get("models", [])}


def probe_live_catalog() -> dict[str, Any] | None:
    """Probe `cursor-agent models`; return None on any failure (offline-safe)."""
    try:
        completed = subprocess.run(
            ["cursor-agent", "models"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    slugs: set[str] = set()
    for line in completed.stdout.splitlines():
        line = line.strip()
        if " - " not in line:
            continue
        slug = line.split(" - ", 1)[0].strip()
        if slug:
            slugs.add(slug)
    if not slugs:
        return None
    return {"schema_version": 1, "source": "cursor-agent models (live)", "live_slugs": sorted(slugs)}


def resolved_catalog_and_source() -> tuple[dict[str, Any], str]:
    """Resolve the catalog used by `refresh`, offline-safe and test-fixturable."""
    fixture = os.environ.get("CURSOR_MODEL_CATALOG")
    curated = read_json(CURATED_CATALOG_PATH)
    if fixture:
        live = read_json(Path(fixture).expanduser())
        missing = catalog_slugs(curated) - set(live.get("live_slugs", []))
        if missing:
            raise RouterLabError(
                "curated catalog has slugs no longer reported live: " + ", ".join(sorted(missing))
            )
        return curated, f"curated (confirmed against fixture:{fixture})"
    live = probe_live_catalog()
    if live is None:
        return curated, "curated-fallback (live probe unavailable)"
    missing = catalog_slugs(curated) - set(live["live_slugs"])
    if missing:
        raise RouterLabError(
            "curated catalog has slugs no longer reported live: " + ", ".join(sorted(missing))
        )
    return curated, "curated (confirmed against live cursor-agent models)"


def doctor() -> dict[str, Any]:
    issues: list[str] = []
    try:
        policy = load_policy()
    except RouterLabError as error:
        return {
            "routing_status": "DEGRADED",
            "workflow_routing_ready": False,
            "catalog_status": "UNKNOWN",
            "routing_issues": [str(error)],
        }
    if DISPATCH_CONTRACT_PATH.is_symlink() or not DISPATCH_CONTRACT_PATH.is_file():
        issues.append(f"dispatch contract is missing or unsafe: {DISPATCH_CONTRACT_PATH}")
    for tier in ("T1", "T2", "T3", "T4"):
        assignment = policy.get("tiers", {}).get(tier)
        if not isinstance(assignment, dict) or not assignment.get("model") or not assignment.get("agent"):
            issues.append(f"active policy is missing a model/agent assignment for {tier}")
    if policy.get("tiers", {}).get("T0", {}).get("mode") != "deterministic":
        issues.append("T0 must remain model-free")
    catalog_status = "FRESH" if CATALOG_PATH.exists() else "CATALOG_STALE"
    routing_status = "HEALTHY" if not issues else "DEGRADED"
    return {
        "routing_status": routing_status,
        "workflow_routing_ready": routing_status == "HEALTHY",
        "catalog_status": catalog_status,
        "routing_issues": issues,
    }


def refresh() -> dict[str, Any]:
    LAB_HOME.mkdir(parents=True, exist_ok=True)
    if ACTIVE_POLICY_PATH.exists():
        existing = read_json(ACTIVE_POLICY_PATH)
        if existing.get("source") != "adaptive-model-router" and "evidence_grade" not in existing:
            backup = LAB_HOME / f"active-policy.json.pre-harness-{dt.date.today().isoformat()}"
            if not backup.exists():
                ACTIVE_POLICY_PATH.rename(backup)
    catalog, catalog_source = resolved_catalog_and_source()
    policy = load_default_policy()
    seeded_slugs = {
        assignment["model"]
        for tier, assignment in policy["tiers"].items()
        if tier != "T0"
    }
    missing = seeded_slugs - catalog_slugs(catalog)
    if missing:
        raise RouterLabError(
            "seeded tier slugs are absent from the resolved catalog: " + ", ".join(sorted(missing))
        )
    policy = {**policy, "updated_at": utc_now()}
    atomic_write_json(
        CATALOG_PATH,
        {**catalog, "resolved_source": catalog_source, "refreshed_at": utc_now()},
    )
    atomic_write_json(ACTIVE_POLICY_PATH, policy)
    return status_report()


def status_report() -> dict[str, Any]:
    policy = load_policy()
    health = doctor()
    tiers: dict[str, Any] = {}
    for tier in VALID_TIERS:
        assignment = policy["tiers"].get(tier, {})
        if tier == "T0":
            tiers[tier] = {"mode": "deterministic"}
        else:
            tiers[tier] = {
                "agent": assignment.get("agent"),
                "model": assignment.get("model"),
                "effort": assignment.get("effort"),
            }
    return {
        "policy_id": policy_id(policy),
        "revision": policy.get("revision"),
        "source": policy.get("source"),
        "evidence_grade": policy.get("evidence_grade", "declared-weak-experimental"),
        "routing_status": health["routing_status"],
        "workflow_routing_ready": health["workflow_routing_ready"],
        "catalog_status": health["catalog_status"],
        "routing_issues": health["routing_issues"],
        "tiers": tiers,
    }


def resolve_phase(request: dict[str, Any]) -> dict[str, Any]:
    allowed_fields = {
        "workflow_id",
        "workflow_version",
        "phase_id",
        "activity",
        "mutation",
        "scope",
        "ambiguity",
        "risk_level",
        "risk_scope",
        "risk_tags",
        "visual_required",
        "current_info_required",
        "external_action",
    }
    unknown = sorted(set(request) - allowed_fields)
    if unknown:
        raise RouterLabError(f"unknown phase request fields: {', '.join(unknown)}")
    for field in ("workflow_id", "phase_id"):
        if not isinstance(request.get(field), str) or not request[field]:
            raise RouterLabError(f"{field} must be a non-empty string")
    if type(request.get("workflow_version")) is not int or request["workflow_version"] < 1:
        raise RouterLabError("workflow_version must be a positive integer")

    activity = request.get("activity")
    mutation = request.get("mutation", "none")
    scope = request.get("scope", "local")
    ambiguity = request.get("ambiguity", "none")
    risk_level = request.get("risk_level", "low")
    risk_scope = request.get("risk_scope", "none")
    risk_tags = request.get("risk_tags", [])
    visual_required = request.get("visual_required", False)
    current_info_required = request.get("current_info_required", False)
    external_action = request.get("external_action", False)

    if activity not in ACTIVITY_BASE_TIERS:
        raise RouterLabError(f"unknown phase activity: {activity}")
    if mutation not in MUTATION_FLOORS:
        raise RouterLabError(f"unknown mutation class: {mutation}")
    if scope not in SCOPE_FLOORS:
        raise RouterLabError(f"unknown phase scope: {scope}")
    if ambiguity not in AMBIGUITY_FLOORS:
        raise RouterLabError(f"unknown ambiguity class: {ambiguity}")
    if risk_level not in RISK_LEVEL_FLOORS:
        raise RouterLabError(f"unknown risk level: {risk_level}")
    if risk_scope not in RISK_SCOPES:
        raise RouterLabError(f"unknown risk scope: {risk_scope}")
    if not isinstance(risk_tags, list) or any(not isinstance(tag, str) for tag in risk_tags):
        raise RouterLabError("risk_tags must be a list of strings")
    if len(risk_tags) != len(set(risk_tags)):
        raise RouterLabError("risk_tags must not contain duplicates")
    unknown_tags = sorted(set(risk_tags) - set(RISK_TAG_FLOORS))
    if unknown_tags:
        raise RouterLabError(f"unknown risk tags: {', '.join(unknown_tags)}")
    for field, value in {
        "visual_required": visual_required,
        "current_info_required": current_info_required,
        "external_action": external_action,
    }.items():
        if not isinstance(value, bool):
            raise RouterLabError(f"{field} must be boolean")
    if risk_scope == "none" and risk_tags:
        raise RouterLabError("risk tags require a non-none risk_scope")

    selected_index = TIER_INDEX[ACTIVITY_BASE_TIERS[activity]]
    reasons = [f"activity:{activity}"]

    def apply_floor(tier: str, reason: str) -> None:
        nonlocal selected_index
        floor = TIER_INDEX[tier]
        if floor > selected_index:
            selected_index = floor
        if reason not in reasons:
            reasons.append(reason)

    apply_floor(MUTATION_FLOORS[mutation], f"mutation:{mutation}")
    apply_floor(SCOPE_FLOORS[scope], f"scope:{scope}")
    apply_floor(AMBIGUITY_FLOORS[ambiguity], f"ambiguity:{ambiguity}")
    if risk_scope == "evidence":
        evidence_floor = "T2" if risk_level in {"high", "critical"} else "T1"
        apply_floor(evidence_floor, f"evidence_risk:{risk_level}")
    elif risk_scope in {"judgment", "mutation", "authority"}:
        apply_floor(RISK_LEVEL_FLOORS[risk_level], f"risk:{risk_level}")
    if risk_scope == "authority":
        apply_floor("T3", "authority_floor")
    for tag in sorted(set(risk_tags)):
        tag_floor = RISK_TAG_FLOORS[tag]
        if risk_scope == "evidence" and TIER_INDEX[tag_floor] > TIER_INDEX["T2"]:
            tag_floor = "T2"
        apply_floor(tag_floor, f"risk_tag:{tag}")
    if visual_required:
        apply_floor("T2", "visual_capability")
    if current_info_required:
        reasons.append("current_information_required")
    if external_action:
        if activity == "prepare_external":
            reasons.append("prepared_external_action")
        else:
            apply_floor("T3", "external_action_floor")

    if mutation != "none" and selected_index > TIER_INDEX[MUTATION_EXECUTION_CEILING]:
        selected_index = TIER_INDEX[MUTATION_EXECUTION_CEILING]
        reasons.append(f"mutation_execution_ceiling:{MUTATION_EXECUTION_CEILING}")

    tier = INDEX_TIER[selected_index]
    policy = load_policy()
    health = doctor()
    if health["routing_status"] != "HEALTHY":
        raise RouterLabError(
            "active routing is not ready: " + "; ".join(health.get("routing_issues", []))
        )
    parent_gate_required = bool(
        (external_action and activity != "prepare_external")
        or mutation == "irreversible"
        or (risk_scope == "mutation" and set(risk_tags) & {"production", "data_loss", "release"})
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "workflow_id": request.get("workflow_id"),
        "workflow_version": request.get("workflow_version"),
        "phase_id": request.get("phase_id"),
        "mode": "deterministic" if tier == "T0" else "model",
        "tier": tier,
        "policy_id": policy_id(policy),
        "reason_codes": reasons,
        "parent_gate_required": parent_gate_required,
        "external_mutation_authorized": False,
        "web_required": bool(current_info_required),
        "visual_required": bool(visual_required),
        "router_health": {
            "routing_status": health["routing_status"],
            "catalog_status": health["catalog_status"],
        },
        "route_facts": {
            "activity": activity,
            "mutation": mutation,
            "scope": scope,
            "ambiguity": ambiguity,
            "risk_level": risk_level,
            "risk_scope": risk_scope,
            "risk_tags": sorted(set(risk_tags)),
            "external_action": external_action,
        },
    }
    if tier == "T0":
        result["execution_identity"] = "NOT_APPLICABLE"
        return result
    assignment = policy["tiers"][tier]
    result.update(
        {
            "agent": assignment["agent"],
            "provider": policy.get("constraints", {}).get("allowed_provider", "cursor"),
            "service_tier": policy.get("constraints", {}).get("default_service_tier", "default"),
            "model": assignment["model"],
            "effort": assignment["effort"],
            "profile_file": "references/dispatch-contract.md",
            "profile_sha256": sha256_bytes(DISPATCH_CONTRACT_PATH.read_bytes()),
            "execution_identity": "REQUESTED_NOT_ATTESTED",
            "runtime_evidence_required": True,
            "runtime_attestation_required": True,
        }
    )
    return result


def read_phase_request(value: str) -> dict[str, Any]:
    raw = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RouterLabError("phase request must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Report the active policy and routing health.")
    subparsers.add_parser("doctor", help="Report routing health only.")
    subparsers.add_parser(
        "refresh",
        help="Re-probe cursor-agent models, verify seeded slugs are live, write policy+catalog.",
    )
    resolve = subparsers.add_parser("resolve-phase", help="Resolve one phase request to a route.")
    resolve.add_argument("--request", required=True, help="Path to a phase-request JSON file, or - for stdin.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "status":
            result = status_report()
        elif args.command == "doctor":
            result = doctor()
        elif args.command == "refresh":
            result = refresh()
        elif args.command == "resolve-phase":
            result = resolve_phase(read_phase_request(args.request))
        else:
            raise RouterLabError(f"unknown command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RouterLabError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"router-lab error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
