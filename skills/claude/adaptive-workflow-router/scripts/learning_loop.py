#!/usr/bin/env python3
"""Persistent, bounded policy learning for the adaptive harness (Claude Code).

The ledger coordinates model and workflow experiments but is not a second model
router.  Model lifecycle states are mirrored only from router_lab.py authority.
Workflow activation uses an exact-byte, journaled transaction owned here.

Platform note.  This is the Claude Code port of the Codex learning loop.  The
ledger, journal, recovery, Pareto, and SERENDIPITY machinery is byte-portable;
four seams are platform-bound and are the only places Claude semantics enter:

* **Homes.**  ``${CLAUDE_CONFIG_DIR:-~/.claude}`` replaces ``CODEX_HOME``.
* **Model identity.**  Claude Code exposes no versioned ``gpt-<major>.<minor>``
  families.  The routable set is the curated catalog slug set that
  ``router_lab.py`` baselines from ``adaptive-model-router/assets/claude-catalog.json``
  (aliases: ``haiku``/``sonnet``/``opus``/``fable``), so the Codex family regex
  and the active policy's version window are replaced by slug-set membership
  against that curated authority.  A "family" here is the canonical
  ``+``-joined routable slug set.
* **Evidence.**  There is no App Server.  The strongest first-party evidence is
  the hash-chained execution receipt ``workflow_dispatch.py`` wrote plus the raw
  ``runs/<id>/claude-cli.NNNN.jsonl`` stream it retained and hashed.  Both are
  ``evidence_grade: "declared"``; the verifier re-reads the registry chain and
  the retained stream bytes and derives identity, completion, usage, and
  duration from them.
* **Model-lane promotion.**  Declared runtime metadata is never scored model
  evidence.  A model observation still requires an eligible ``router_lab.py``
  observation whose ``execution_evidence_mode`` is ``external-verifier``; until
  ``router_lab.py configure-attestation`` pins one, model lanes stay
  ``BLOCKED_MODEL_ENFORCEMENT``.  Nothing here may weaken that.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import statistics
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = 2
SKILL_DIR = Path(__file__).resolve().parent.parent
CLAUDE_HOME = Path(
    os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
).expanduser()
HOME = Path(
    os.environ.get("ADAPTIVE_LEARNING_LOOP_HOME", CLAUDE_HOME / "adaptive-learning-loop")
).expanduser()
MODEL_ROUTER_HOME = Path(
    os.environ.get("ADAPTIVE_MODEL_ROUTER_HOME", CLAUDE_HOME / "adaptive-model-router")
).expanduser()
LEARNING_POLICY_PATH = SKILL_DIR / "assets" / "learning-loop-policy.json"
DISPATCH_REGISTRY_PATH = CLAUDE_HOME / "adaptive-workflow-router" / "execution-registry.jsonl"
DETERMINISTIC_GRADER_PATH = Path(__file__).resolve().parent / "deterministic_quality_grader.py"
# The curated catalog is the sibling model-router skill's checked-in asset --
# the same file router_lab.py baselines from.  It is the slug-set authority.
CURATED_CATALOG_PATH = (
    SKILL_DIR.parent / "adaptive-model-router" / "assets" / "claude-catalog.json"
)

STATES = {
    "STAGED",
    "COLLECTING",
    "BLOCKED_MODEL_ENFORCEMENT",
    "QUALIFIED",
    "REJECTED",
    "PROMOTED",
    "VALIDATED",
    "ROLLED_BACK",
    "STALE",
    "SUPERSEDED",
}
TERMINAL_STATES = {"REJECTED", "VALIDATED", "ROLLED_BACK", "STALE", "SUPERSEDED"}
TRANSITIONS = {
    "STAGED": {"COLLECTING", "BLOCKED_MODEL_ENFORCEMENT", "STALE", "SUPERSEDED"},
    "COLLECTING": {
        "COLLECTING",
        "BLOCKED_MODEL_ENFORCEMENT",
        "QUALIFIED",
        "REJECTED",
        "STALE",
        "SUPERSEDED",
    },
    "BLOCKED_MODEL_ENFORCEMENT": {"COLLECTING", "STALE", "SUPERSEDED"},
    "QUALIFIED": {"PROMOTED", "COLLECTING", "STALE", "SUPERSEDED"},
    "PROMOTED": {"VALIDATED", "ROLLED_BACK"},
    "REJECTED": set(),
    "VALIDATED": set(),
    "ROLLED_BACK": set(),
    "STALE": set(),
    "SUPERSEDED": set(),
}

FEATURE_KEYS = (
    "activity",
    "scope",
    "ambiguity",
    "risk",
    "risk_scope",
    "risk_tags",
    "mutation",
    "context_size",
    "tools",
    "current_information",
    "objective_grader",
    "workflow_family",
    "task_family",
    "holdout",
    "terminal_strategy",
)
BOOL_FEATURES = {"current_information", "objective_grader", "holdout", "terminal_strategy"}
LIST_FEATURES = {"risk_tags", "tools"}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
# Claude Code routes by curated catalog alias, not by version family.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
FAMILY_SEPARATOR = "+"
PROVIDER = "anthropic"
# Effort ladder: low < medium < high < xhigh < max (router_lab.py authority).
T4_MODEL_SLUG = "fable"
T4_EFFORT = "max"
DECLARED_EVIDENCE_GRADE = "declared"
CLAUDE_RUNTIME_LABEL = "claude-code-headless-cli"
CLAUDE_STREAM_SOURCE = "claude-code-headless-stream"
CLAUDE_METADATA_SOURCE = "claude-code-headless-metadata"
CLAUDE_STREAM_NAME_RE = re.compile(r"claude-cli\.\d+\.jsonl$")
BLOCKED_SCORED_STATUS = "BLOCKED_MODEL_ENFORCEMENT"
# router_lab.py execution-evidence modes.  "runtime-metadata" is the declared
# default and is never scored model evidence; only a pinned external verifier is.
ATTESTED_EVIDENCE_MODES = {"external-verifier"}
WORKFLOW_VARIABLES = {
    "workflow.decomposition",
    "workflow.context_packaging",
    "workflow.tool_mode",
    "workflow.review_escalation",
    "workflow.budget_calculation",
}
ROUTER_STATE_MAP = {
    "staged": "STAGED",
    "collecting": "COLLECTING",
    "blocked_model_enforcement": "BLOCKED_MODEL_ENFORCEMENT",
    "qualified": "QUALIFIED",
    "rejected": "REJECTED",
    "promoted": "PROMOTED",
    "validated": "VALIDATED",
    "rolled_back": "ROLLED_BACK",
    "stale": "STALE",
    "superseded": "SUPERSEDED",
}
SAFE_EVIDENCE_SOURCES = {
    "claude-code-headless-stream",
    "claude-code-headless-metadata",
}


class LearningError(RuntimeError):
    pass


class EvidenceError(LearningError):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
        raise LearningError(f"{label} must be a lowercase SHA-256")
    return value


def regular_file(path_value: str | Path, label: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise LearningError(f"{label} must be a retained regular non-symlink file")
    return path.resolve()


def file_sha256(path_value: str | Path, label: str = "file") -> str:
    return sha256_bytes(regular_file(path_value, label).read_bytes())


def read_bound_json(path_value: str | Path, expected_sha256: str, label: str) -> dict[str, Any]:
    path = regular_file(path_value, label)
    actual = sha256_bytes(path.read_bytes())
    if actual != require_hash(expected_sha256, f"{label} hash"):
        raise LearningError(f"{label} SHA-256 mismatch")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LearningError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise LearningError(f"{label} must contain a JSON object")
    return value


def self_hashed(value: dict[str, Any], field: str = "contract_sha256") -> dict[str, Any]:
    result = dict(value)
    result.pop(field, None)
    result[field] = digest(result)
    return result


def verify_self_hash(value: dict[str, Any], field: str, label: str) -> None:
    supplied = require_hash(value.get(field), f"{label}.{field}")
    bare = dict(value)
    bare.pop(field, None)
    if digest(bare) != supplied:
        raise LearningError(f"{label} self-hash mismatch")


def load_learning_policy() -> dict[str, Any]:
    path = regular_file(LEARNING_POLICY_PATH, "learning-loop policy")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise LearningError("unsupported learning-loop policy schema")
    fraction = value.get("serendipity", {}).get("capacity_fraction")
    if not isinstance(fraction, (int, float)) or not 0 <= float(fraction) <= 0.5:
        raise LearningError("learning policy has an invalid SERENDIPITY capacity fraction")
    return value


def normalize_features(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LearningError("task features must be an object")
    extra = set(value) - set(FEATURE_KEYS)
    if extra:
        raise LearningError("unknown closed task features: " + ", ".join(sorted(extra)))
    result: dict[str, Any] = {}
    for key in FEATURE_KEYS:
        if key in BOOL_FEATURES:
            item = value.get(key, False)
            if type(item) is not bool:
                raise LearningError(f"feature {key} must be Boolean")
        elif key in LIST_FEATURES:
            item = value.get(key, [])
            if not isinstance(item, list) or not all(isinstance(v, str) for v in item):
                raise LearningError(f"feature {key} must be a string list")
            item = sorted(set(item))
        else:
            item = value.get(key, "none")
            if not isinstance(item, str):
                raise LearningError(f"feature {key} must be a string")
            item = item.strip().lower()
        result[key] = item
    return result


def feature_hash(features: dict[str, Any]) -> str:
    return digest(normalize_features(features))


def family_text(value: Any) -> str:
    """Canonical text for a routable slug set (the Claude "family")."""
    if isinstance(value, str):
        slugs = [item for item in value.split(FAMILY_SEPARATOR) if item]
    elif isinstance(value, (list, tuple, set, frozenset)):
        slugs = list(value)
    else:
        raise LearningError(f"invalid routable model family: {value!r}")
    if not slugs:
        raise LearningError("routable model family must name at least one slug")
    for slug in slugs:
        if not isinstance(slug, str) or SLUG_RE.fullmatch(slug) is None:
            raise LearningError(f"invalid model slug: {slug!r}")
    unique = sorted(set(slugs))
    if len(unique) != len(slugs) and not isinstance(value, (set, frozenset)):
        raise LearningError("routable model family repeats a slug")
    return FAMILY_SEPARATOR.join(unique)


def family_slugs(value: Any) -> tuple[str, ...]:
    return tuple(family_text(value).split(FAMILY_SEPARATOR))


def derive_routable_family(catalog: Any, minimum_family: str | None = None) -> str:
    """Derive the routable curated slug set; never a version family."""
    if isinstance(catalog, dict):
        entries = catalog.get("models", catalog.get("items", []))
    else:
        entries = catalog
    if isinstance(entries, dict):
        entries = [
            {**(value if isinstance(value, dict) else {}), "slug": key}
            for key, value in entries.items()
        ]
    if not isinstance(entries, list):
        raise LearningError("catalog must contain a model list or mapping")
    slugs: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            slug, provider, visible, selectable, modalities = (
                entry,
                PROVIDER,
                True,
                True,
                None,
            )
        elif isinstance(entry, dict):
            slug = entry.get("slug", entry.get("id", entry.get("model")))
            provider = entry.get("provider", PROVIDER)
            visible = entry.get("visible", True)
            selectable = entry.get("selectable", True)
            modalities = entry.get("input_modalities")
        else:
            continue
        if (
            provider != PROVIDER
            or visible is not True
            or selectable is not True
            or not isinstance(slug, str)
        ):
            continue
        if modalities is not None and "text" not in modalities:
            continue
        slugs.append(slug)
    if not slugs:
        raise LearningError("catalog has no visible selectable Anthropic text model")
    family = family_text(slugs)
    if minimum_family is not None:
        dropped = set(family_slugs(minimum_family)) - set(family_slugs(family))
        if dropped:
            raise LearningError(
                "catalog regression would drop an established routable model: "
                + ", ".join(sorted(dropped))
            )
    return family


def curated_catalog_family() -> str:
    """The routable slug set of the curated catalog router_lab.py baselines."""
    path = regular_file(CURATED_CATALOG_PATH, "curated model catalog")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LearningError(f"curated model catalog is unreadable: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise LearningError("curated model catalog has an unsupported schema")
    return derive_routable_family(payload["models"])


def canonical_catalog_authority(
    bindings: dict[str, Any],
    declared_family: str,
    *,
    require_bound_file_hash: bool = False,
) -> dict[str, Any]:
    """Derive the family only from the live global router catalog authority."""
    authority_path = (MODEL_ROUTER_HOME / "catalog.json").resolve()
    supplied_path = Path(str(bindings.get("catalog_path", ""))).expanduser()
    if not supplied_path.is_absolute() or supplied_path.resolve() != authority_path:
        raise LearningError("cycle must bind the canonical live model catalog path")
    actual_catalog_file_hash = file_sha256(authority_path, "canonical model catalog")
    if require_bound_file_hash and actual_catalog_file_hash != bindings.get(
        "catalog_file_sha256"
    ):
        raise LearningError("canonical model catalog file hash differs from the cycle binding")
    try:
        catalog = json.loads(authority_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LearningError(f"canonical model catalog is unreadable: {error}") from error
    if not isinstance(catalog, dict):
        raise LearningError("canonical model catalog must contain a JSON object")
    if catalog.get("schema_version") != 1 or not isinstance(catalog.get("models"), dict):
        raise LearningError("canonical model catalog has an unsupported schema")
    for slug, model in catalog["models"].items():
        if not isinstance(model, dict) or model.get("slug") != slug:
            raise LearningError("canonical model catalog slug mapping is inconsistent")
    recomputed_semantic = digest([catalog["models"][key] for key in sorted(catalog["models"])])
    semantic = require_hash(catalog.get("semantic_hash"), "canonical catalog semantic hash")
    if semantic != recomputed_semantic or semantic != bindings.get("catalog_semantic_sha256"):
        raise LearningError("cycle catalog semantic hash differs from live authority")
    policy_path = (MODEL_ROUTER_HOME / "active-policy.json").resolve()
    supplied_policy_path = Path(str(bindings.get("router_policy_path", ""))).expanduser()
    if not supplied_policy_path.is_absolute() or supplied_policy_path.resolve() != policy_path:
        raise LearningError("cycle must bind the canonical active router-policy path")
    actual_policy_file_hash = file_sha256(policy_path, "canonical active router policy")
    if require_bound_file_hash and actual_policy_file_hash != bindings.get(
        "router_policy_file_sha256"
    ):
        raise LearningError("canonical active router-policy file hash differs from the cycle binding")
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LearningError(f"canonical active router policy is unreadable: {error}") from error
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise LearningError("canonical active router policy has an unsupported schema")
    constraints = policy.get("constraints", {})
    if not isinstance(constraints, dict):
        raise LearningError("canonical active router policy constraints are malformed")
    if constraints.get("allowed_provider", PROVIDER) != PROVIDER:
        raise LearningError("canonical active router policy does not permit only Anthropic")
    if constraints.get("require_local_catalog_availability") is not True:
        raise LearningError("active router policy must require curated-catalog availability")
    # Claude Code has no version window.  The routable set is the curated
    # catalog slug set, and the live global catalog must still be the curated
    # bytes router_lab.py baselined -- otherwise the scored surface has drifted.
    curated_catalog_sha256 = file_sha256(CURATED_CATALOG_PATH, "curated model catalog")
    if catalog.get("source_sha256") != curated_catalog_sha256:
        raise LearningError("live router catalog was not refreshed from the curated catalog bytes")
    family = derive_routable_family(catalog)
    if family != curated_catalog_family():
        raise LearningError("live router catalog slug set differs from the curated catalog authority")
    routable = family_slugs(family)
    model_scope = {
        "rule": "curated-catalog-slug-set",
        "curated_catalog_sha256": curated_catalog_sha256,
        "routable_models": list(routable),
    }
    for tier, configuration in (policy.get("tiers") or {}).items():
        if not isinstance(configuration, dict):
            continue
        tier_model = configuration.get("model")
        if tier_model is not None and tier_model not in routable:
            raise LearningError(f"active router policy tier {tier} routes outside the curated catalog")
    if family != family_text(declared_family):
        raise LearningError("declared current family differs from the live catalog authority")
    if bindings.get("model_scope_sha256") != digest(model_scope):
        raise LearningError("model-scope hash differs from the canonical curated slug set")
    allowed_service_tiers = constraints.get("allowed_service_tiers")
    if not isinstance(allowed_service_tiers, list) or not all(
        isinstance(item, str) and item for item in allowed_service_tiers
    ):
        raise LearningError("active router policy must enumerate its allowed service tiers")
    return {
        "path": str(authority_path),
        "file_sha256": actual_catalog_file_hash,
        "semantic_sha256": semantic,
        "family": family,
        "model_scope": model_scope,
        "allowed_service_tiers": sorted(set(allowed_service_tiers)),
        "router_policy_file_sha256": actual_policy_file_hash,
        "router_policy_id": policy.get("policy_id"),
        "models": catalog["models"],
    }


def assert_live_cycle_authority(
    cycle: dict[str, Any], *, require_bound_file_hash: bool = False
) -> dict[str, Any]:
    authority = canonical_catalog_authority(
        cycle["bindings"],
        cycle["current_family"],
        require_bound_file_hash=require_bound_file_hash,
    )
    control_policy = cycle["arms"]["control"]["router_policy_sha256"]
    if authority["router_policy_id"] != control_policy:
        raise LearningError("cycle incumbent router-policy ID differs from live authority")
    for arm_name, arm in cycle["arms"].items():
        route = arm["route"]
        model = authority["models"].get(route["model"])
        if (
            not isinstance(model, dict)
            or model.get("provider") != route["provider"]
            or model.get("visible") is not True
            or model.get("selectable") is not True
            or "text" not in (model.get("input_modalities") or [])
            or route["effort"] not in (model.get("supported_efforts") or [])
        ):
            raise LearningError(f"{arm_name} route is unavailable in the live catalog")
        # Claude Code carries no per-model service-tier table; the active router
        # policy enumerates the tiers instead.
        if route["service_tier"] not in authority["allowed_service_tiers"]:
            raise LearningError(f"{arm_name} route service tier is unavailable")
    return authority


def validate_route(route: dict[str, Any], current_family: str, *, t4: bool = False) -> None:
    required = {"provider", "model", "effort", "service_tier", "tier", "mutation", "sandbox"}
    missing = required - set(route)
    if missing:
        raise LearningError("route missing: " + ", ".join(sorted(missing)))
    if route["provider"] != PROVIDER:
        raise LearningError("only the Anthropic provider is eligible")
    slug = str(route["model"])
    if slug not in family_slugs(current_family):
        raise LearningError("experiment route is outside the curated catalog slug set")
    if t4:
        if route.get("tier") != "T4" or slug != T4_MODEL_SLUG or route.get("effort") != T4_EFFORT:
            raise LearningError(
                f"T4 must be the exact {T4_MODEL_SLUG}/{T4_EFFORT} route"
            )
        if route.get("mutation") != "none" or route.get("sandbox") != "read-only":
            raise LearningError("T4 terminal adjudication must be read-only")
        if route.get("fallback") not in (None, "none"):
            raise LearningError("T4 has no fallback")


def effective_lane(cycle: dict[str, Any]) -> str:
    lane = cycle.get("lane")
    if lane == "serendipity":
        mutation_lane = cycle.get("serendipity", {}).get("mutation_lane")
        if mutation_lane not in {"model", "workflow"}:
            raise LearningError("SERENDIPITY requires mutation_lane model or workflow")
        return mutation_lane
    if lane not in {"model", "workflow"}:
        raise LearningError("lane must be model, workflow, or serendipity")
    return lane


def validate_budget_manifest(value: dict[str, Any], task_manifest_sha256: str) -> dict[str, Any]:
    verify_self_hash(value, "budget_manifest_sha256", "run budget")
    if value.get("schema_version") != 1:
        raise LearningError("unsupported run-budget schema")
    if value.get("task_manifest_sha256") != require_hash(task_manifest_sha256, "task manifest hash"):
        raise LearningError("run budget is not bound to the task manifest")
    calls = value.get("calls")
    if not isinstance(calls, list):
        raise LearningError("run budget calls must be a list")
    aggregate = 0
    expected = 0
    call_ids: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("token_cap"), int) or call["token_cap"] <= 0:
            raise LearningError("every planned call needs a positive requirement-derived cap")
        if not isinstance(call.get("expected_inferences"), int) or call["expected_inferences"] <= 0:
            raise LearningError("every planned call needs an expected inference count")
        if not isinstance(call.get("context_growth_allowance"), int) or call["context_growth_allowance"] < 0:
            raise LearningError("every planned call needs context-growth allowance")
        if not isinstance(call.get("wall_time_seconds"), int) or call["wall_time_seconds"] <= 0:
            raise LearningError("every planned call needs wall-time allocation")
        if not isinstance(call.get("rationale"), str) or not call["rationale"].strip():
            raise LearningError("every planned call needs a rationale")
        call_id = call.get("call_id")
        if not isinstance(call_id, str) or not call_id or call_id in call_ids:
            raise LearningError("every planned call needs a unique nonempty call_id")
        call_ids.add(call_id)
        if call.get("arm") not in {"control", "challenger"}:
            raise LearningError("every planned call needs a control or challenger arm")
        if call.get("role") not in {"worker", "reviewer", "grader"}:
            raise LearningError("every planned call needs a valid role")
        if call.get("phase") not in {
            "primary",
            "tool_loop",
            "review",
            "grading",
            "remediation",
            "escalation",
        }:
            raise LearningError("every planned call needs a valid phase")
        if call.get("attempt_kind") not in {"initial", "retry", "rework"}:
            raise LearningError("every planned call needs a valid attempt kind")
        require_hash(call.get("execution_manifest_sha256"), "planned execution-manifest hash")
        if call.get("cap_mode", "derived") == "fixed":
            require_hash(call.get("measured_safe_evidence_sha256"), "fixed-cap evidence hash")
        aggregate += call["token_cap"]
        expected += call["expected_inferences"]
    for key in ("reviewer_allowance", "grader_allowance"):
        if not isinstance(value.get(key), int) or value[key] < 0:
            raise LearningError(f"run budget {key} must be nonnegative")
    aggregate += value["reviewer_allowance"] + value["grader_allowance"]
    if value.get("worst_case_aggregate_tokens") != aggregate:
        raise LearningError("run budget aggregate omits a per-call or reviewer/grader allowance")
    if value.get("expected_inference_count") != expected:
        raise LearningError("run budget inference count does not equal its calls")
    return value


def build_budget_manifest(
    task_manifest_sha256: str,
    calls: list[dict[str, Any]],
    reviewer_allowance: int = 0,
    grader_allowance: int = 0,
) -> dict[str, Any]:
    value = {
        "schema_version": 1,
        "task_manifest_sha256": require_hash(task_manifest_sha256, "task manifest hash"),
        "calls": calls,
        "expected_inference_count": sum(int(call["expected_inferences"]) for call in calls),
        "reviewer_allowance": reviewer_allowance,
        "grader_allowance": grader_allowance,
        "worst_case_aggregate_tokens": (
            sum(int(call["token_cap"]) for call in calls)
            + int(reviewer_allowance)
            + int(grader_allowance)
        ),
    }
    return self_hashed(value, "budget_manifest_sha256")


def validate_planned_invocations(
    shared_manifest: dict[str, Any], budget: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    plans = shared_manifest.get("planned_invocations_by_arm")
    if not isinstance(plans, dict) or set(plans) != {"control", "challenger"}:
        raise LearningError("shared manifest must bind planned invocations for both arms")
    budget_calls = {call["call_id"]: call for call in budget["calls"]}
    seen: set[str] = set()
    for arm, items in plans.items():
        if not isinstance(items, list) or not items:
            raise LearningError(f"{arm} requires at least one planned invocation")
        workers = 0
        for plan in items:
            if not isinstance(plan, dict):
                raise LearningError("planned invocation must be an object")
            planned_id = plan.get("planned_call_id")
            if not isinstance(planned_id, str) or not planned_id or planned_id in seen:
                raise LearningError("planned invocation IDs must be globally unique")
            seen.add(planned_id)
            call = budget_calls.get(planned_id)
            expected = {
                "arm": arm,
                "role": plan.get("role"),
                "phase": plan.get("phase"),
                "attempt_kind": plan.get("attempt_kind"),
                "token_cap": plan.get("token_cap"),
                "wall_time_seconds": plan.get("wall_time_cap_seconds"),
                "expected_inferences": plan.get("expected_inferences"),
                "execution_manifest_sha256": plan.get("execution_manifest_sha256"),
            }
            if call is None or any(call.get(key) != value for key, value in expected.items()):
                raise LearningError("planned invocation differs from its run-budget call")
            if plan["execution_manifest_sha256"] not in shared_manifest[
                "execution_manifest_sha256_by_arm"
            ][arm]:
                raise LearningError("planned invocation uses an unbound dispatcher manifest")
            if (
                type(plan.get("token_cap")) is not int
                or plan["token_cap"] <= 0
                or type(plan.get("wall_time_cap_seconds")) is not int
                or plan["wall_time_cap_seconds"] <= 0
                or type(plan.get("expected_inferences")) is not int
                or plan["expected_inferences"] <= 0
            ):
                raise LearningError("planned invocation caps and inference count must be positive")
            if (
                plan.get("role") == "worker"
                and plan.get("phase") == "primary"
                and plan.get("attempt_kind") == "initial"
            ):
                workers += 1
        if workers != 1:
            raise LearningError("each arm requires one planned primary initial worker")
    if seen != set(budget_calls):
        raise LearningError("every run-budget call must map one-to-one to a planned invocation")
    return plans


def trusted_receipts() -> dict[str, dict[str, Any]]:
    """Read and verify the fixed first-party dispatcher provenance registry."""
    path = DISPATCH_REGISTRY_PATH.expanduser()
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("enforcement", "trusted dispatcher receipt registry is unavailable")
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise EvidenceError("enforcement", "trusted dispatcher receipt registry ownership or mode is unsafe")
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise EvidenceError("enforcement", "trusted dispatcher registry lock is unsafe")
    receipts: dict[str, dict[str, Any]] = {}
    previous = "0" * 64
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    for sequence, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvidenceError("enforcement", "trusted dispatcher registry is malformed") from error
        if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
            raise EvidenceError("enforcement", "trusted dispatcher receipt schema is invalid")
        supplied = require_hash(receipt.get("receipt_sha256"), "dispatcher receipt hash")
        bare = dict(receipt)
        bare.pop("receipt_sha256", None)
        if (
            digest(bare) != supplied
            or receipt.get("sequence") != sequence
            or receipt.get("previous_receipt_sha256") != previous
        ):
            raise EvidenceError("enforcement", "trusted dispatcher receipt chain is invalid")
        receipt_id = receipt.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id or receipt_id in receipts:
            raise EvidenceError("enforcement", "trusted dispatcher receipt ID is invalid or duplicated")
        receipts[receipt_id] = receipt
        previous = supplied
    return receipts


def require_trusted_receipt(receipt_id: Any, receipt_type: str) -> dict[str, Any]:
    if not isinstance(receipt_id, str) or not receipt_id:
        raise EvidenceError("enforcement", f"trusted {receipt_type} receipt ID is required")
    receipt = trusted_receipts().get(receipt_id)
    if receipt is None or receipt.get("receipt_type") != receipt_type:
        raise EvidenceError("enforcement", f"trusted {receipt_type} receipt is missing or mismatched")
    return receipt


class Store:
    """Hash-chained ledger with atomic whole-file replacement and recovery."""

    def __init__(self, home: Path = HOME):
        self.home = Path(home)
        self.ledger = self.home / "ledger.jsonl"
        self.journal = self.home / "ledger-journal.json"
        self.authority_journal = self.home / "workflow-authority-journal.json"
        self.lock = self.home / "learning.lock"
        self.snapshots = self.home / "snapshots"
        self.fault_hook = lambda _point: None

    def setup(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        if self.home.is_symlink():
            raise LearningError("learning-loop home cannot be a symlink")
        self.snapshots.mkdir(exist_ok=True)
        if not self.ledger.exists():
            self._atomic_bytes(self.ledger, b"")
        if self.ledger.is_symlink() or self.lock.is_symlink():
            raise LearningError("learning-loop control files cannot be symlinks")

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.setup()
        with self.lock.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _fsync_dir(self, directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_bytes(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_dir(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        self._atomic_bytes(path, canonical_bytes(value) + b"\n")

    def _unlink(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        self._fsync_dir(path.parent)

    def _decode_events(self, content: bytes) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        previous = "0" * 64
        for index, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise LearningError(f"ledger line {index} is torn or malformed") from error
            if not isinstance(event, dict):
                raise LearningError(f"ledger line {index} is not an object")
            supplied = require_hash(event.get("event_sha256"), "event hash")
            bare = dict(event)
            bare.pop("event_sha256", None)
            if digest(bare) != supplied:
                raise LearningError(f"ledger line {index} self-hash mismatch")
            if event.get("sequence") != len(events) + 1 or event.get("previous_event_sha256") != previous:
                raise LearningError(f"ledger line {index} hash-chain mismatch")
            previous = supplied
            events.append(event)
        return events

    def events(self) -> list[dict[str, Any]]:
        with self.locked():
            self._recover_ledger_locked()
            self._recover_authority_locked()
            return self._decode_events(self.ledger.read_bytes())

    def _prepare_event(self, value: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
        event = dict(value)
        event.setdefault("schema_version", SCHEMA_VERSION)
        event.setdefault("event_id", uuid.uuid4().hex)
        event.setdefault("created_at_epoch", time.time())
        event["sequence"] = len(existing) + 1
        event["previous_event_sha256"] = existing[-1]["event_sha256"] if existing else "0" * 64
        event.pop("event_sha256", None)
        event["event_sha256"] = digest(event)
        return event

    def _append_locked(self, value: dict[str, Any]) -> dict[str, Any]:
        old = self.ledger.read_bytes()
        existing = self._decode_events(old)
        event = self._prepare_event(value, existing)
        if any(item.get("event_id") == event["event_id"] for item in existing):
            return next(item for item in existing if item.get("event_id") == event["event_id"])
        target = old + canonical_bytes(event) + b"\n"
        payload = {
            "schema_version": 1,
            "before_sha256": sha256_bytes(old),
            "after_sha256": sha256_bytes(target),
            "target_base64": base64.b64encode(target).decode("ascii"),
            "event_id": event["event_id"],
        }
        payload["journal_sha256"] = digest(payload)
        self._atomic_json(self.journal, payload)
        self.fault_hook("ledger_journal_written")
        self._atomic_bytes(self.ledger, target)
        self.fault_hook("ledger_replaced")
        self._unlink(self.journal)
        return event

    def append(self, value: dict[str, Any]) -> dict[str, Any]:
        with self.locked():
            self._recover_ledger_locked()
            self._recover_authority_locked()
            return self._append_locked(value)

    def _recover_ledger_locked(self) -> dict[str, Any]:
        if not self.journal.exists():
            return {"recovered": False}
        journal = json.loads(self.journal.read_text(encoding="utf-8"))
        supplied = require_hash(journal.get("journal_sha256"), "ledger journal hash")
        bare = dict(journal)
        bare.pop("journal_sha256", None)
        if digest(bare) != supplied:
            raise LearningError("ledger journal checksum mismatch")
        target = base64.b64decode(journal["target_base64"], validate=True)
        if sha256_bytes(target) != journal["after_sha256"]:
            raise LearningError("ledger journal target checksum mismatch")
        current = self.ledger.read_bytes()
        current_hash = sha256_bytes(current)
        if current_hash == journal["before_sha256"]:
            self._atomic_bytes(self.ledger, target)
        elif current_hash != journal["after_sha256"]:
            raise LearningError("ledger differs from both sides of interrupted transaction")
        self._decode_events(target)
        self._unlink(self.journal)
        return {"recovered": True, "event_id": journal["event_id"]}

    def recover(self) -> dict[str, Any]:
        with self.locked():
            ledger = self._recover_ledger_locked()
            authority = self._recover_authority_locked()
            return {"ledger": ledger, "workflow_authority": authority}

    def _recover_authority_locked(self) -> dict[str, Any]:
        if not self.authority_journal.exists():
            return {"recovered": False}
        journal = json.loads(self.authority_journal.read_text(encoding="utf-8"))
        supplied = require_hash(journal.get("journal_sha256"), "authority journal hash")
        bare = dict(journal)
        bare.pop("journal_sha256", None)
        if digest(bare) != supplied:
            raise LearningError("workflow authority journal checksum mismatch")
        active = Path(journal["active_path"])
        before = base64.b64decode(journal["before_base64"], validate=True)
        after = base64.b64decode(journal["after_base64"], validate=True)
        if sha256_bytes(before) != journal["before_sha256"] or sha256_bytes(after) != journal["after_sha256"]:
            raise LearningError("workflow authority journal bytes do not match hashes")
        current = active.read_bytes() if active.exists() else b""
        if sha256_bytes(current) == journal["before_sha256"]:
            self._atomic_bytes(active, after)
        elif sha256_bytes(current) != journal["after_sha256"]:
            raise LearningError("active workflow differs from both sides of interrupted transaction")
        if not any(
            event.get("event_id") == journal["state_event"]["event_id"]
            for event in self._decode_events(self.ledger.read_bytes())
        ):
            self._append_locked(journal["state_event"])
        self._unlink(self.authority_journal)
        return {"recovered": True, "cycle_id": journal["cycle_id"], "state": journal["target_state"]}


def replay(store: Store) -> dict[str, dict[str, Any]]:
    cycles: dict[str, dict[str, Any]] = {}
    for event in store.events():
        kind = event.get("event_type")
        cycle_id = event.get("cycle_id")
        if kind == "cycle.begin":
            if cycle_id in cycles:
                raise LearningError(f"duplicate cycle.begin for {cycle_id}")
            cycle = json.loads(json.dumps(event["cycle"]))
            cycle["observations"] = []
            cycle["aborted_observations"] = []
            cycle["state_history"] = [cycle["state"]]
            cycles[cycle_id] = cycle
        elif cycle_id in cycles and kind == "observation.recorded":
            cycles[cycle_id]["observations"].append(event["observation"])
        elif cycle_id in cycles and kind == "observation.aborted":
            cycles[cycle_id]["aborted_observations"].append(event)
        elif cycle_id in cycles and kind == "cycle.state":
            cycles[cycle_id]["state"] = event["state"]
            cycles[cycle_id]["state_history"].append(event["state"])
            cycles[cycle_id]["last_state_event"] = event
            if event.get("snapshot_manifest"):
                cycles[cycle_id]["snapshot_manifest"] = event["snapshot_manifest"]
    return cycles


def current_state(store: Store, cycle_id: str) -> dict[str, Any]:
    cycle = replay(store).get(cycle_id)
    if cycle is None:
        raise LearningError(f"unknown cycle: {cycle_id}")
    return cycle


def validate_cycle(cycle: dict[str, Any], store: Store | None = None) -> dict[str, Any]:
    lane = cycle.get("lane")
    base_lane = effective_lane(cycle)
    change = cycle.get("change")
    if not isinstance(change, dict) or set(change) < {"variable_id", "before", "after"}:
        raise LearningError("cycle change must contain variable_id, before, and after")
    variable = change["variable_id"]
    if base_lane == "model" and variable != "model.route_assignment":
        raise LearningError("model experiments may vary only model.route_assignment")
    if base_lane == "workflow" and variable not in WORKFLOW_VARIABLES:
        raise LearningError("workflow experiment variable is not in the safe allowlist")
    if change["before"] == change["after"]:
        raise LearningError("candidate must change exactly one causal variable")

    features = normalize_features(cycle.get("features", {}))
    current_family = family_text(cycle.get("current_family", ""))
    arms = cycle.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"control", "challenger"}:
        raise LearningError("cycle requires exactly control and challenger arms")
    for name, arm in arms.items():
        if not isinstance(arm, dict) or arm.get("action") != change["before" if name == "control" else "after"]:
            raise LearningError(f"{name} action is not the declared causal value")
        validate_route(arm.get("route", {}), current_family, t4=arm.get("route", {}).get("tier") == "T4")
        for key in ("profile_sha256", "router_policy_sha256", "workflow_policy_sha256"):
            require_hash(arm.get(key), f"{name}.{key}")
    control, challenger = arms["control"], arms["challenger"]
    if base_lane == "model":
        if control["workflow_policy_sha256"] != challenger["workflow_policy_sha256"]:
            raise LearningError("model experiment must freeze the workflow policy")
        if cycle.get("frozen", {}).get("workflow_policy_sha256") != control["workflow_policy_sha256"]:
            raise LearningError("model experiment omitted its frozen workflow hash")
    else:
        for key in ("route", "profile_sha256", "router_policy_sha256"):
            if control[key] != challenger[key]:
                raise LearningError("workflow experiment must freeze the model route and router policy")
        if cycle.get("frozen", {}).get("router_policy_sha256") != control["router_policy_sha256"]:
            raise LearningError("workflow experiment omitted its frozen router-policy hash")
        if control["workflow_policy_sha256"] == challenger["workflow_policy_sha256"]:
            raise LearningError("workflow candidate must bind a distinct workflow snapshot")
    if features["terminal_strategy"]:
        for arm in arms.values():
            validate_route(arm["route"], current_family, t4=True)

    bindings = cycle.get("bindings")
    if not isinstance(bindings, dict):
        raise LearningError("cycle bindings are required")
    for key in (
        "catalog_semantic_sha256",
        "catalog_file_sha256",
        "model_scope_sha256",
        "router_policy_file_sha256",
        "input_manifest_sha256",
        "grader_identity_sha256",
        "rubric_sha256",
        "evaluation_suite_sha256",
        "config_sha256",
        "run_budget_sha256",
    ):
        require_hash(bindings.get(key), f"bindings.{key}")
    if not isinstance(bindings.get("catalog_path"), str) or not isinstance(
        bindings.get("router_policy_path"), str
    ):
        raise LearningError("canonical catalog and router-policy paths are required")
    authority = assert_live_cycle_authority(
        {**cycle, "current_family": current_family}, require_bound_file_hash=True
    )
    shared_manifest = read_bound_json(
        bindings.get("input_manifest_path", ""),
        bindings["input_manifest_sha256"],
        "shared experiment input manifest",
    )
    if shared_manifest.get("schema_version") != 1:
        raise LearningError("unsupported shared experiment-manifest schema")
    for key in ("prompt_sha256", "context_sha256", "grader_identity_sha256", "rubric_sha256"):
        require_hash(shared_manifest.get(key), f"shared manifest {key}")
    if shared_manifest["grader_identity_sha256"] != bindings["grader_identity_sha256"] or shared_manifest["rubric_sha256"] != bindings["rubric_sha256"]:
        raise LearningError("shared manifest grader binding mismatch")
    if shared_manifest.get("service_tier") != arms["control"]["route"]["service_tier"] or shared_manifest.get("service_tier") != arms["challenger"]["route"]["service_tier"]:
        raise LearningError("paired arms must use the same explicitly bound service tier")
    if not isinstance(shared_manifest.get("tools"), list) or not isinstance(shared_manifest.get("permissions"), dict):
        raise LearningError("shared manifest must bind tools and permissions")
    if not isinstance(shared_manifest.get("retry_rule"), dict):
        raise LearningError("shared manifest must bind its retry rule")
    for key in ("token_cap_per_arm", "wall_time_cap_seconds_per_arm"):
        if type(shared_manifest.get(key)) is not int or shared_manifest[key] <= 0:
            raise LearningError(f"shared manifest {key} must be positive")
    execution_manifests = shared_manifest.get("execution_manifest_sha256_by_arm")
    if not isinstance(execution_manifests, dict) or set(execution_manifests) != {"control", "challenger"}:
        raise LearningError("shared manifest must bind each arm's dispatcher manifests")
    for values in execution_manifests.values():
        if not isinstance(values, list) or not values or not all(HASH_RE.fullmatch(str(item)) for item in values):
            raise LearningError("shared manifest dispatcher hashes are invalid")
    budget = read_bound_json(
        bindings.get("run_budget_path", ""),
        bindings["run_budget_sha256"],
        "run-budget manifest",
    )
    validate_budget_manifest(budget, bindings["input_manifest_sha256"])
    validate_planned_invocations(shared_manifest, budget)
    if not isinstance(bindings.get("runtime_binary"), str) or not bindings["runtime_binary"].startswith("/"):
        raise LearningError("headless runtime binary path must be absolute")
    if bindings.get("runtime_label") != CLAUDE_RUNTIME_LABEL:
        raise LearningError("headless runtime label must be the pinned Claude CLI runtime")
    if bindings.get("runtime_evidence_source") not in SAFE_EVIDENCE_SOURCES:
        raise LearningError("headless runtime evidence source is not an accepted Claude source")
    if not isinstance(bindings.get("evaluation_suite_version"), str) or not bindings["evaluation_suite_version"]:
        raise LearningError("evaluation-suite version is required")
    if not isinstance(bindings.get("evidence_ttl_seconds"), int) or bindings["evidence_ttl_seconds"] <= 0:
        raise LearningError("evidence TTL must be a positive integer")

    requirements = cycle.get("requirements", {})
    for key in (
        "development_pairs",
        "task_family_count",
        "holdout_pairs",
        "exact_t4_controls",
        "canary_windows",
    ):
        if not isinstance(requirements.get(key, 0), int) or requirements.get(key, 0) < 0:
            raise LearningError(f"requirements.{key} must be nonnegative")
    if requirements.get("development_pairs", 0) < 1:
        raise LearningError("at least one development pair is required")
    if requirements.get("canary_windows", 0) < 1:
        raise LearningError("at least one canary window is required")
    configured_canary_windows = load_learning_policy().get("promotion", {}).get(
        "workflow_canary_windows"
    )
    if requirements.get("canary_windows") != configured_canary_windows:
        raise LearningError("workflow canary windows must equal the configured learning policy")
    if not isinstance(cycle.get("hypothesis"), str) or not cycle["hypothesis"].strip():
        raise LearningError("cycle hypothesis is required")
    if not isinstance(cycle.get("candidate_id"), str) or not cycle["candidate_id"]:
        raise LearningError("candidate_id is required")

    if lane == "serendipity":
        serendipity = cycle.get("serendipity", {})
        if serendipity.get("predicted_immediate_improvement") is not False:
            raise LearningError("SERENDIPITY mutations must not be predicted immediate improvements")
        descriptor = serendipity.get("behavioral_descriptor")
        if not isinstance(descriptor, dict) or not descriptor:
            raise LearningError("SERENDIPITY requires a behavioral descriptor")
        minimum_quality = serendipity.get("minimum_quality")
        if (
            not isinstance(minimum_quality, (int, float))
            or isinstance(minimum_quality, bool)
            or not math.isfinite(float(minimum_quality))
        ):
            raise LearningError("SERENDIPITY requires an explicit finite minimum-quality floor")
        replay_families = serendipity.get("replay_task_families")
        if (
            not isinstance(replay_families, list)
            or not replay_families
            or not all(isinstance(item, str) and item for item in replay_families)
            or len(set(replay_families)) != len(replay_families)
        ):
            raise LearningError("SERENDIPITY requires precommitted unrelated replay task families")
        if features["task_family"] in replay_families:
            raise LearningError("SERENDIPITY replay families must differ from the source task family")
        allocation = serendipity.get("capacity_allocation")
        if not isinstance(allocation, dict):
            raise LearningError("SERENDIPITY requires a capacity allocation contract")
        verify_self_hash(allocation, "allocation_sha256", "SERENDIPITY capacity allocation")
        if allocation.get("allocated") is not True:
            raise LearningError("SERENDIPITY capacity was not allocated")
        if store is not None:
            head = store.events()[-1]["event_sha256"] if store.events() else "0" * 64
            if allocation.get("ledger_head_sha256") != head:
                raise LearningError("SERENDIPITY allocation is stale or already consumed")
            expected_allocation = allocate_serendipity(store)
            if allocation != expected_allocation:
                raise LearningError("SERENDIPITY allocation differs from the configured policy share")
    raw_lineage = cycle.get("lineage", {"archive_ids": []})
    if not isinstance(raw_lineage, dict) or set(raw_lineage) != {"archive_ids"}:
        raise LearningError("cycle lineage must be the closed archive_ids contract")
    archive_ids = raw_lineage["archive_ids"]
    if (
        not isinstance(archive_ids, list)
        or not all(isinstance(item, str) and item for item in archive_ids)
        or len(set(archive_ids)) != len(archive_ids)
    ):
        raise LearningError("cycle lineage archive IDs must be unique nonempty strings")
    lineage_entries: list[dict[str, Any]] = []
    if archive_ids:
        if store is None:
            raise LearningError("archive lineage requires the live append-only ledger")
        events = store.events()
        for archive_id in archive_ids:
            matches = [
                event
                for event in events
                if event.get("event_type") == "novelty.archived"
                and event.get("archive", {}).get("archive_id") == archive_id
            ]
            if len(matches) != 1:
                raise LearningError("lineage archive is missing or ambiguous")
            event = matches[0]
            lineage_entries.append(
                {
                    "archive_id": archive_id,
                    "archive_event_sha256": event["event_sha256"],
                    "archive_event_sequence": event["sequence"],
                    "archive_action_sha256": event["archive"]["action_sha256"],
                }
            )
        if [item["archive_event_sequence"] for item in lineage_entries] != sorted(
            item["archive_event_sequence"] for item in lineage_entries
        ):
            raise LearningError("lineage archives must be ordered by their prior ledger sequence")
    normalized = dict(cycle)
    normalized["features"] = features
    normalized["task_feature_sha256"] = feature_hash(features)
    normalized["current_family"] = current_family
    normalized["catalog_authority"] = {
        key: value for key, value in authority.items() if key != "models"
    }
    normalized["lineage"] = {
        "archive_ids": archive_ids,
        "entries": lineage_entries,
        "lineage_sha256": digest(lineage_entries),
    }
    scope_bindings = {
        key: value
        for key, value in bindings.items()
        if key
        not in {
            "catalog_file_sha256",
            "router_policy_file_sha256",
            "input_manifest_path",
            "run_budget_path",
        }
    }
    normalized["experiment_scope_sha256"] = digest(
        {
            "lane": lane,
            "mutation_lane": base_lane,
            "task_feature_sha256": normalized["task_feature_sha256"],
            "hypothesis": cycle["hypothesis"],
            "change": change,
            "arms": arms,
            "frozen": cycle.get("frozen", {}),
            "bindings": scope_bindings,
            "requirements": requirements,
            "lineage": normalized["lineage"],
            "serendipity": cycle.get("serendipity") if lane == "serendipity" else None,
        }
    )
    return normalized


def begin(store: Store, value: dict[str, Any]) -> dict[str, Any]:
    cycle = dict(value)
    cycle.setdefault("cycle_id", uuid.uuid4().hex)
    cycle.setdefault("run_id", uuid.uuid4().hex)
    cycle.setdefault("parent_cycle_id", None)
    cycle.setdefault("state", "STAGED")
    cycle.setdefault("created_at_epoch", time.time())
    if cycle["state"] != "STAGED":
        raise LearningError("new cycles must begin STAGED")
    existing = replay(store)
    if cycle["cycle_id"] in existing:
        raise LearningError("cycle_id already exists")
    if any(item.get("run_id") == cycle["run_id"] for item in existing.values()):
        raise LearningError("a scheduled run may begin at most one candidate")
    parent = cycle.get("parent_cycle_id")
    if parent is not None and parent not in existing:
        raise LearningError("parent_cycle_id does not exist")
    cycle = validate_cycle(cycle, store)
    store.append({"event_type": "cycle.begin", "cycle_id": cycle["cycle_id"], "cycle": cycle})
    return cycle


def allocate_serendipity(store: Store, fraction: float | None = None) -> dict[str, Any]:
    configured = float(load_learning_policy()["serendipity"]["capacity_fraction"])
    if fraction is not None and (
        not isinstance(fraction, (int, float))
        or isinstance(fraction, bool)
        or float(fraction) != configured
    ):
        raise LearningError("SERENDIPITY capacity fraction must equal the configured policy share")
    fraction = configured
    cycles = list(replay(store).values())
    used = sum(1 for cycle in cycles if cycle.get("lane") == "serendipity")
    slot_index = len(cycles) + 1
    target_after = math.floor(slot_index * float(fraction) + 1e-12)
    allocated = used < target_after
    head = store.events()[-1]["event_sha256"] if store.events() else "0" * 64
    return self_hashed(
        {
            "schema_version": 1,
            "fraction": float(fraction),
            "slot_index": slot_index,
            "serendipity_cycles_before": used,
            "allocated": allocated,
            "ledger_head_sha256": head,
        },
        "allocation_sha256",
    )


def load_execution_metadata(
    expected_runtime: dict[str, Any],
    dispatcher_receipt_id: str,
    expected_model_ids: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify one retained headless Claude dispatch from first-party bytes.

    Claude Code exposes no App Server, so there is no server-issued turn record
    to bind.  The evidence is the hash-chained execution receipt
    ``workflow_dispatch.py`` appended to its private registry plus the raw
    ``runs/<id>/claude-cli.NNNN.jsonl`` stream that same process spawned,
    retained, and hashed.  Both are ``evidence_grade: "declared"``.
    """
    receipt = require_trusted_receipt(dispatcher_receipt_id, "execution")
    if receipt.get("evidence_source") != CLAUDE_STREAM_SOURCE:
        raise EvidenceError(
            "enforcement", "execution evidence is not the retained headless Claude stream"
        )
    if receipt.get("evidence_grade") != DECLARED_EVIDENCE_GRADE:
        raise EvidenceError("enforcement", "execution receipt does not carry the declared grade")
    if receipt.get("scored_evidence_status") != BLOCKED_SCORED_STATUS:
        raise EvidenceError(
            "enforcement",
            "headless Claude receipts must declare BLOCKED_MODEL_ENFORCEMENT",
        )
    command = receipt.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) for item in command)
    ):
        raise EvidenceError("incomplete", "execution receipt omitted its dispatch command")
    observed_runtime = {
        "runtime_binary": command[0],
        "runtime_label": receipt.get("runtime"),
        "runtime_evidence_source": receipt.get("evidence_source"),
    }
    for key, value in observed_runtime.items():
        if expected_runtime.get(key) != value:
            raise EvidenceError("enforcement", f"{key} mismatch")
    metadata = receipt.get("runtime_metadata")
    if not isinstance(metadata, dict):
        raise EvidenceError("incomplete", "execution receipt omitted its runtime metadata")
    if (
        metadata.get("evidence_source") != CLAUDE_METADATA_SOURCE
        or metadata.get("evidence_grade") != DECLARED_EVIDENCE_GRADE
    ):
        raise EvidenceError("enforcement", "runtime metadata is not declared Claude CLI metadata")
    if metadata.get("is_error") is not False:
        raise EvidenceError("incomplete", "bound headless turn did not complete cleanly")
    session_id = metadata.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise EvidenceError("incomplete", "bound headless turn lacks a session ID")

    requested = {
        "provider": PROVIDER,
        "model": receipt.get("requested_model"),
        "effort": receipt.get("requested_effort"),
    }
    if any(not isinstance(value, str) or not value for value in requested.values()):
        raise EvidenceError("incomplete", "requested execution identity is required")
    # The retained command must literally be what the receipt claims it asked for.
    for flag, value in (("--model", requested["model"]), ("--effort", requested["effort"])):
        if flag not in command or command[command.index(flag) + 1 :][:1] != [value]:
            raise EvidenceError("enforcement", f"retained command does not carry {flag} {value}")
    expected_model_id = expected_model_ids.get(requested["model"])
    observed_models = receipt.get("observed_models")
    if (
        not isinstance(expected_model_id, str)
        or not isinstance(observed_models, list)
        or observed_models != [expected_model_id]
    ):
        raise EvidenceError("enforcement", "requested and observed execution identity mismatch")
    observed = dict(requested)

    usage = metadata.get("usage", {})
    if not isinstance(usage, dict):
        raise EvidenceError("incomplete", "runtime usage measurement is required")
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if type(input_tokens) is not int or input_tokens < 0 or type(output_tokens) is not int or output_tokens < 0:
        raise EvidenceError("incomplete", "measured input and output usage are required")
    duration_ms = metadata.get("duration_ms")
    if type(duration_ms) is not int or duration_ms <= 0:
        raise EvidenceError("incomplete", "bound headless turn lacks measured duration")

    stream_path = regular_file(receipt.get("stream_path", ""), "retained Claude CLI stream")
    if CLAUDE_STREAM_NAME_RE.search(stream_path.name) is None:
        raise EvidenceError("enforcement", "retained stream is not a claude-cli.NNNN.jsonl file")
    stream_hash = file_sha256(stream_path, "retained Claude CLI stream")
    if stream_hash != require_hash(receipt.get("stream_sha256"), "retained stream hash"):
        raise EvidenceError("enforcement", "retained Claude CLI stream hash mismatch")
    try:
        stream_events = [
            json.loads(line)
            for line in stream_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except json.JSONDecodeError as error:
        raise EvidenceError("enforcement", "retained Claude CLI stream is malformed") from error
    if not stream_events or not all(isinstance(item, dict) for item in stream_events):
        raise EvidenceError("enforcement", "retained Claude CLI stream events are malformed")
    if receipt.get("stream_event_count") != len(stream_events):
        raise EvidenceError("enforcement", "retained stream event count differs from the receipt")
    terminal = stream_events[-1]
    if terminal.get("type") != "result":
        raise EvidenceError("incomplete", "retained stream does not end with a terminal result")
    terminal_usage = terminal.get("usage")
    if (
        terminal.get("session_id") != session_id
        or terminal.get("is_error") not in (False, None)
        or terminal.get("duration_ms") != duration_ms
        or not isinstance(terminal_usage, dict)
        or terminal_usage.get("input_tokens") != input_tokens
        or terminal_usage.get("output_tokens") != output_tokens
    ):
        raise EvidenceError("enforcement", "retained stream differs from the receipt metadata")
    terminal_model_usage = terminal.get("modelUsage")
    if not isinstance(terminal_model_usage, dict) or sorted(terminal_model_usage) != observed_models:
        raise EvidenceError("enforcement", "retained stream model usage differs from the receipt")
    assistant_events = [item for item in stream_events if item.get("type") == "assistant"]
    if not assistant_events:
        raise EvidenceError("incomplete", "retained stream contains no assistant turn")
    streamed_models = {
        item.get("message", {}).get("model")
        for item in assistant_events
        if isinstance(item.get("message"), dict) and item["message"].get("model") is not None
    }
    if not streamed_models <= set(observed_models):
        raise EvidenceError("enforcement", "retained stream contains an unobserved model substitution")

    invocation = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "requested_identity": requested,
        "observed_identity": observed,
        # The headless lane passes --model/--effort directly; there is no agent
        # profile in the loop, so the phase contract is the bound per-call
        # identity the dispatcher verified before spawning.
        "phase_contract_sha256": require_hash(
            receipt.get("phase_contract_sha256"), "receipt phase contract hash"
        ),
        "input_manifest_sha256": require_hash(
            receipt.get("dispatch_packet_sha256"), "receipt dispatch packet hash"
        ),
        "prompt_sha256": require_hash(receipt.get("prompt_sha256"), "receipt prompt hash"),
        "session_id": session_id,
        "run_id": receipt.get("run_id"),
        "stream_path": str(stream_path),
        "stream_sha256": stream_hash,
        "dispatcher_receipt_id": dispatcher_receipt_id,
    }
    return receipt, invocation


def summarize_resources(invocations: list[dict[str, Any]], started_ms: int, accepted_ms: int) -> dict[str, Any]:
    if type(started_ms) is not int or type(accepted_ms) is not int or accepted_ms <= started_ms:
        raise EvidenceError("incomplete", "arm start and acceptance timestamps are required")
    if not invocations:
        raise EvidenceError("incomplete", "at least one measured model invocation is required")
    ids: set[str] = set()
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "context_replay_tokens": 0,
        "reviewer_tokens": 0,
        "grader_tokens": 0,
        "tool_loop_tokens": 0,
        "retry_tokens": 0,
        "rework_tokens": 0,
        "escalation_tokens": 0,
        "retry_count": 0,
        "rework_count": 0,
        "escalation_count": 0,
    }
    workers = 0
    for invocation in invocations:
        invocation_id = invocation.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id or invocation_id in ids:
            raise EvidenceError("incomplete", "invocation IDs must be unique and nonempty")
        ids.add(invocation_id)
        role = invocation.get("role")
        phase = invocation.get("phase")
        attempt = invocation.get("attempt_kind")
        if role not in {"worker", "reviewer", "grader"}:
            raise EvidenceError("incomplete", "invocation role is invalid")
        if phase not in {"primary", "tool_loop", "review", "grading", "remediation", "escalation"}:
            raise EvidenceError("incomplete", "invocation phase is invalid")
        if attempt not in {"initial", "retry", "rework"}:
            raise EvidenceError("incomplete", "attempt kind is invalid")
        input_tokens = invocation.get("input_tokens")
        output_tokens = invocation.get("output_tokens")
        replay_tokens = invocation.get("context_replay_tokens", 0)
        if any(type(value) is not int or value < 0 for value in (input_tokens, output_tokens, replay_tokens)):
            raise EvidenceError("incomplete", "invocation token measurements must be nonnegative integers")
        if replay_tokens > input_tokens:
            raise EvidenceError("incomplete", "context replay cannot exceed measured input tokens")
        token_count = input_tokens + output_tokens
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        totals["context_replay_tokens"] += replay_tokens
        if role == "worker" and phase == "primary" and attempt == "initial":
            workers += 1
        if role == "reviewer":
            totals["reviewer_tokens"] += token_count
        if role == "grader":
            totals["grader_tokens"] += token_count
        if phase == "tool_loop":
            totals["tool_loop_tokens"] += token_count
        if phase == "escalation":
            totals["escalation_tokens"] += token_count
            totals["escalation_count"] += 1
        if attempt == "retry":
            totals["retry_tokens"] += token_count
            totals["retry_count"] += 1
        if attempt == "rework" or phase == "remediation":
            totals["rework_tokens"] += token_count
            totals["rework_count"] += 1
    if workers != 1:
        raise EvidenceError("incomplete", "each arm requires exactly one primary initial worker")
    totals.update(
        {
            "total_tokens": totals["input_tokens"] + totals["output_tokens"],
            "wall_time_ms": accepted_ms - started_ms,
            "invocation_count": len(invocations),
        }
    )
    return totals


def _router_observation(cycle: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    observation_id = artifact.get("router_observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise EvidenceError("enforcement", "model-lane evidence requires a router observation ID")
    path = MODEL_ROUTER_HOME / "observations.jsonl"
    if path.is_symlink() or not path.is_file():
        raise EvidenceError("enforcement", "router observation authority is unavailable")
    matches = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if value.get("observation_id") == observation_id:
                matches.append(value)
    if len(matches) != 1:
        raise EvidenceError("enforcement", "router observation ID is missing or ambiguous")
    record = matches[0]
    expected_arm = "incumbent" if artifact.get("arm") == "control" else "candidate"
    if record.get("candidate_id") != cycle["candidate_id"] or record.get("arm") != expected_arm:
        raise EvidenceError("enforcement", "router observation candidate or arm mismatch")
    if record.get("case_id") != artifact.get("case_id") or record.get("eligible") is not True:
        raise EvidenceError("enforcement", "router observation is not eligible for this case")
    if record.get("execution_evidence_mode") not in ATTESTED_EVIDENCE_MODES:
        raise EvidenceError(
            "enforcement",
            "router observation lacks accepted execution evidence: declared Claude "
            "runtime metadata is never scored model evidence, so the model lane "
            "stays BLOCKED_MODEL_ENFORCEMENT until router_lab.py "
            "configure-attestation pins an external verifier",
        )
    return record


def validate_observation_artifact(cycle: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    if artifact.get("schema_version") != 1:
        raise EvidenceError("enforcement", "unsupported observation-artifact schema")
    for key in ("cycle_id", "candidate_id", "experiment_scope_sha256"):
        expected = cycle["cycle_id"] if key == "cycle_id" else cycle[key]
        if artifact.get(key) != expected:
            raise EvidenceError("enforcement", f"artifact {key} mismatch")
    arm = artifact.get("arm")
    if arm not in {"control", "challenger"}:
        raise EvidenceError("enforcement", "artifact arm is invalid")
    if artifact.get("partition") not in {"development", "holdout"}:
        raise EvidenceError("enforcement", "artifact partition is invalid")
    if not isinstance(artifact.get("case_id"), str) or not artifact["case_id"]:
        raise EvidenceError("incomplete", "case ID is required")
    if not isinstance(artifact.get("task_family"), str) or not artifact["task_family"]:
        raise EvidenceError("incomplete", "task family is required")
    evidence_phase = artifact.get("evidence_phase")
    if evidence_phase not in {"prepromotion", "canary"}:
        raise EvidenceError("enforcement", "artifact evidence phase is invalid")
    canary_window_id = artifact.get("canary_window_id")
    if evidence_phase == "canary" and (
        not isinstance(canary_window_id, str) or not canary_window_id
    ):
        raise EvidenceError("incomplete", "canary evidence requires a window ID")
    try:
        live_authority = assert_live_cycle_authority(cycle)
    except LearningError as error:
        raise EvidenceError("stale", str(error)) from error
    expected_model_ids = {
        slug: value.get("model_id")
        for slug, value in live_authority["models"].items()
        if isinstance(value, dict)
    }
    bindings = artifact.get("bindings", {})
    expected_bindings = cycle["bindings"]
    for key in (
        "catalog_semantic_sha256",
        "catalog_file_sha256",
        "catalog_path",
        "model_scope_sha256",
        "router_policy_file_sha256",
        "router_policy_path",
        "runtime_binary",
        "runtime_label",
        "runtime_evidence_source",
        "input_manifest_sha256",
        "grader_identity_sha256",
        "rubric_sha256",
        "evaluation_suite_version",
        "evaluation_suite_sha256",
        "config_sha256",
        "run_budget_sha256",
    ):
        if bindings.get(key) != expected_bindings.get(key):
            raise EvidenceError("stale", f"artifact binding drift: {key}")
    if bindings.get("task_feature_sha256") != cycle["task_feature_sha256"]:
        raise EvidenceError("stale", "artifact task-feature hash drift")

    expected_arm = cycle["arms"][arm]
    shared_manifest = read_bound_json(
        expected_bindings["input_manifest_path"],
        expected_bindings["input_manifest_sha256"],
        "shared experiment input manifest",
    )
    allowed_execution_manifests = set(shared_manifest["execution_manifest_sha256_by_arm"][arm])
    planned = shared_manifest["planned_invocations_by_arm"][arm]
    plans_by_id = {item["planned_call_id"]: item for item in planned}
    invocations: list[dict[str, Any]] = []
    worker_identity: dict[str, Any] | None = None
    grader_identities: list[dict[str, Any]] = []
    invocation_specs = artifact.get("invocations")
    if not isinstance(invocation_specs, list) or not invocation_specs:
        raise EvidenceError("incomplete", "artifact invocations are required")
    supplied_plan_ids = [spec.get("planned_call_id") for spec in invocation_specs]
    if (
        len(set(supplied_plan_ids)) != len(supplied_plan_ids)
        or set(supplied_plan_ids) != set(plans_by_id)
    ):
        raise EvidenceError("incomplete", "artifact invocations do not exactly cover the bound plan")
    for spec in invocation_specs:
        plan = plans_by_id[spec["planned_call_id"]]
        for key in ("role", "phase", "attempt_kind"):
            if spec.get(key) != plan.get(key):
                raise EvidenceError("enforcement", f"invocation {key} differs from its planned call")
        _receipt, measured = load_execution_metadata(
            {
                "runtime_binary": expected_bindings["runtime_binary"],
                "runtime_label": expected_bindings["runtime_label"],
                "runtime_evidence_source": expected_bindings["runtime_evidence_source"],
            },
            spec.get("dispatcher_receipt_id", ""),
            expected_model_ids,
        )
        if (
            measured["input_manifest_sha256"] not in allowed_execution_manifests
            or measured["input_manifest_sha256"] != plan["execution_manifest_sha256"]
        ):
            raise EvidenceError("enforcement", "dispatcher input manifest is not bound by the shared experiment manifest")
        if measured["input_tokens"] + measured["output_tokens"] > plan["token_cap"]:
            raise EvidenceError("incomplete", "measured invocation exceeded its bound token cap")
        if measured["duration_ms"] > plan["wall_time_cap_seconds"] * 1000:
            raise EvidenceError("incomplete", "measured invocation exceeded its bound wall-time cap")
        measured.update(
            {
                "invocation_id": spec.get("invocation_id"),
                "planned_call_id": spec.get("planned_call_id"),
                "role": spec.get("role"),
                "phase": spec.get("phase"),
                "attempt_kind": spec.get("attempt_kind"),
                "context_replay_tokens": spec.get("context_replay_tokens", 0),
            }
        )
        validate_route(
            {
                **measured["observed_identity"],
                # The headless stream carries no service tier; the bound arm's
                # tier is already policy-checked by assert_live_cycle_authority.
                "service_tier": expected_arm["route"]["service_tier"],
                "tier": spec.get("tier", expected_arm["route"]["tier"]),
                "mutation": "none",
                "sandbox": "read-only",
                "fallback": None,
            },
            cycle["current_family"],
            t4=spec.get("tier") == "T4",
        )
        if spec.get("role") == "worker" and spec.get("phase") == "primary" and spec.get("attempt_kind") == "initial":
            worker_identity = measured["observed_identity"]
            if worker_identity != {
                key: expected_arm["route"][key]
                for key in ("provider", "model", "effort")
            }:
                raise EvidenceError("enforcement", "primary worker identity differs from the bound arm")
        if spec.get("role") == "grader":
            grader_identities.append(measured["observed_identity"])
        invocations.append(measured)
    if worker_identity is None:
        raise EvidenceError("incomplete", "primary worker evidence is missing")

    quality_path = artifact.get("quality_artifact_path", "")
    quality_hash = artifact.get("quality_artifact_sha256", "")
    quality_receipt = require_trusted_receipt(
        artifact.get("quality_receipt_id"), "quality"
    )
    quality = read_bound_json(quality_path, quality_hash, "quality artifact")
    if quality.get("schema_version") != 1 or quality.get("case_id") != artifact["case_id"]:
        raise EvidenceError("enforcement", "quality artifact case or schema mismatch")
    if quality.get("grader_identity_sha256") != expected_bindings["grader_identity_sha256"]:
        raise EvidenceError("enforcement", "quality grader identity mismatch")
    if quality.get("rubric_sha256") != expected_bindings["rubric_sha256"]:
        raise EvidenceError("enforcement", "quality rubric mismatch")
    grader_kind = quality.get("grader_kind")
    if grader_kind not in {"deterministic", "blind_independent", "user_acceptance"}:
        raise EvidenceError("enforcement", "quality grader kind is not eligible")
    expected_quality_receipt = {
        "quality_artifact_path": str(regular_file(quality_path, "quality artifact")),
        "quality_artifact_sha256": quality_hash,
        "grader_kind": grader_kind,
        "grader_identity_sha256": expected_bindings["grader_identity_sha256"],
        "rubric_sha256": expected_bindings["rubric_sha256"],
        "case_id": artifact["case_id"],
        "evaluated_arm": arm,
        "evaluated_execution_receipt_ids": sorted(
            item["dispatcher_receipt_id"] for item in invocations
        ),
    }
    if (
        quality.get("evaluated_arm") != arm
        or quality.get("evaluated_execution_receipt_ids")
        != expected_quality_receipt["evaluated_execution_receipt_ids"]
    ):
        raise EvidenceError("enforcement", "quality artifact is not bound to this arm execution")
    if any(quality_receipt.get(key) != value for key, value in expected_quality_receipt.items()):
        raise EvidenceError("enforcement", "trusted quality receipt differs from the retained grade")
    if grader_kind == "deterministic":
        grader_path = regular_file(
            quality_receipt.get("producer_path", ""), "deterministic grader executable"
        )
        if (
            quality_receipt.get("producer_kind") != "pinned-deterministic-grader"
            or quality_receipt.get("producer_sha256")
            != expected_bindings["grader_identity_sha256"]
            or grader_path != DETERMINISTIC_GRADER_PATH.resolve()
            or file_sha256(grader_path, "deterministic grader executable")
            != expected_bindings["grader_identity_sha256"]
        ):
            raise EvidenceError("enforcement", "deterministic grade lacks pinned producer provenance")
    if grader_kind == "blind_independent":
        if quality.get("blind") is not True or not grader_identities:
            raise EvidenceError("enforcement", "blind independent grading evidence is missing")
        if any(identity == worker_identity for identity in grader_identities):
            raise EvidenceError("enforcement", "the challenger or worker cannot grade itself")
        grader_receipts = {
            item["dispatcher_receipt_id"]
            for item in invocations
            if item["role"] == "grader"
        }
        if quality_receipt.get("producer_receipt_id") not in grader_receipts:
            raise EvidenceError("enforcement", "blind grade is not bound to its grader execution")
    if grader_kind == "user_acceptance" and quality_receipt.get("producer_kind") != "explicit-user-acceptance":
        raise EvidenceError("enforcement", "user acceptance lacks trusted explicit authority")
    arm_quality = quality.get("arms", {}).get(arm)
    if not isinstance(arm_quality, dict):
        raise EvidenceError("incomplete", "quality artifact omitted this arm")
    score = arm_quality.get("quality_score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(float(score)):
        raise EvidenceError("incomplete", "measured quality score is required")
    objective_gates = arm_quality.get("objective_gates")
    if not isinstance(objective_gates, dict) or not objective_gates or not all(type(value) is bool for value in objective_gates.values()):
        raise EvidenceError("incomplete", "objective gate measurements are required")

    safety = artifact.get("safety", {})
    for key in ("authorized_mutation", "secret_safe", "authority_safe", "critical_failure"):
        if type(safety.get(key)) is not bool:
            raise EvidenceError("incomplete", f"safety signal {key} is required")
    resources = summarize_resources(
        invocations,
        artifact.get("arm_started_at_ms"),
        artifact.get("accepted_at_ms"),
    )
    if resources["total_tokens"] > shared_manifest["token_cap_per_arm"]:
        raise EvidenceError("incomplete", "arm exceeded the shared total-token cap")
    if resources["wall_time_ms"] > shared_manifest["wall_time_cap_seconds_per_arm"] * 1000:
        raise EvidenceError("incomplete", "arm exceeded the shared end-to-end wall-time cap")
    if effective_lane(cycle) == "model":
        router_record = _router_observation(cycle, artifact)
        if router_record.get("attested_model") != worker_identity["model"] or router_record.get("attested_effort") != worker_identity["effort"]:
            raise EvidenceError("enforcement", "router observation identity differs from aggregate evidence")
    else:
        router_record = None
    result = {
        "observation_id": artifact.get("observation_id") or digest(artifact),
        "origin_cycle_id": cycle["cycle_id"],
        "candidate_id": cycle["candidate_id"],
        "experiment_scope_sha256": cycle["experiment_scope_sha256"],
        "case_id": artifact["case_id"],
        "arm": arm,
        "partition": artifact["partition"],
        "evidence_phase": evidence_phase,
        "canary_window_id": canary_window_id,
        "task_family": artifact["task_family"],
        "bindings": bindings,
        "worker_identity": worker_identity,
        "profile_sha256": expected_arm["profile_sha256"],
        "router_policy_sha256": expected_arm["router_policy_sha256"],
        "workflow_policy_sha256": expected_arm["workflow_policy_sha256"],
        "quality_score": float(score),
        "objective_gates": objective_gates,
        "quality_artifact_sha256": quality_hash,
        "quality_receipt_id": artifact.get("quality_receipt_id"),
        "grader_kind": grader_kind,
        "safety": safety,
        "resources": resources,
        "invocations": invocations,
        "router_observation_id": artifact.get("router_observation_id"),
        "created_at_epoch": artifact.get("created_at_epoch", time.time()),
        "expires_at_epoch": artifact.get("created_at_epoch", time.time())
        + cycle["bindings"]["evidence_ttl_seconds"],
        "promotion_eligible": (
            all(objective_gates.values())
            and safety["authorized_mutation"]
            and safety["secret_safe"]
            and safety["authority_safe"]
            and not safety["critical_failure"]
        ),
    }
    return result


def _used_execution_receipts(store: Store) -> set[str]:
    used: set[str] = set()
    for event in store.events():
        if event.get("event_type") not in {
            "observation.recorded",
            "canary.observation.recorded",
        }:
            continue
        for invocation in event.get("observation", {}).get("invocations", []):
            receipt_id = invocation.get("dispatcher_receipt_id")
            if isinstance(receipt_id, str):
                used.add(receipt_id)
    return used


def _ensure_fresh_receipts(store: Store, observation: dict[str, Any]) -> None:
    supplied = [item["dispatcher_receipt_id"] for item in observation["invocations"]]
    if len(supplied) != len(set(supplied)) or set(supplied) & _used_execution_receipts(store):
        raise EvidenceError("enforcement", "dispatcher execution receipt was reused")


def record(store: Store, cycle_id: str, artifact_path: str, artifact_sha256: str) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if cycle["state"] not in {"STAGED", "COLLECTING", "BLOCKED_MODEL_ENFORCEMENT"}:
        raise LearningError("prepromotion evidence is frozen after qualification")
    try:
        artifact = read_bound_json(artifact_path, artifact_sha256, "observation artifact")
        observation = validate_observation_artifact(cycle, artifact)
        if observation["evidence_phase"] != "prepromotion":
            raise EvidenceError("enforcement", "canary evidence must use record-canary")
        _ensure_fresh_receipts(store, observation)
    except (EvidenceError, LearningError) as error:
        category = error.category if isinstance(error, EvidenceError) else "enforcement"
        store.append(
            {
                "event_type": "observation.aborted",
                "cycle_id": cycle_id,
                "experiment_scope_sha256": cycle["experiment_scope_sha256"],
                "artifact_path": str(Path(artifact_path).expanduser()),
                "artifact_sha256": artifact_sha256,
                "category": category,
                "reason": str(error),
            }
        )
        raise
    if any(
        event.get("event_type") == "observation.recorded"
        and event.get("observation", {}).get("observation_id") == observation["observation_id"]
        for event in store.events()
    ):
        raise LearningError("observation ID already exists")
    store.append(
        {
            "event_type": "observation.recorded",
            "cycle_id": cycle_id,
            "experiment_scope_sha256": cycle["experiment_scope_sha256"],
            "artifact_path": str(regular_file(artifact_path, "observation artifact")),
            "artifact_sha256": artifact_sha256,
            "observation": observation,
        }
    )
    return observation


def record_canary(
    store: Store, cycle_id: str, artifact_path: str, artifact_sha256: str
) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if effective_lane(cycle) != "workflow" or cycle.get("lane") == "serendipity":
        raise LearningError("canary recording applies only to an ordinary workflow candidate")
    if cycle["state"] != "PROMOTED":
        raise LearningError("canary evidence requires a PROMOTED workflow candidate")
    try:
        artifact = read_bound_json(artifact_path, artifact_sha256, "canary observation artifact")
        observation = validate_observation_artifact(cycle, artifact)
        if observation["evidence_phase"] != "canary":
            raise EvidenceError("enforcement", "prepromotion evidence cannot enter the canary ledger")
        _ensure_fresh_receipts(store, observation)
    except (EvidenceError, LearningError) as error:
        category = error.category if isinstance(error, EvidenceError) else "enforcement"
        store.append(
            {
                "event_type": "canary.observation.aborted",
                "cycle_id": cycle_id,
                "experiment_scope_sha256": cycle["experiment_scope_sha256"],
                "artifact_path": str(Path(artifact_path).expanduser()),
                "artifact_sha256": artifact_sha256,
                "category": category,
                "reason": str(error),
            }
        )
        raise
    if any(
        event.get("observation", {}).get("observation_id") == observation["observation_id"]
        for event in store.events()
        if event.get("event_type") in {"observation.recorded", "canary.observation.recorded"}
    ):
        raise LearningError("observation ID already exists")
    store.append(
        {
            "event_type": "canary.observation.recorded",
            "cycle_id": cycle_id,
            "experiment_scope_sha256": cycle["experiment_scope_sha256"],
            "artifact_path": str(regular_file(artifact_path, "canary observation artifact")),
            "artifact_sha256": artifact_sha256,
            "observation": observation,
        }
    )
    return observation


def observation_compatibility(
    cycle: dict[str, Any],
    observation: dict[str, Any],
    at_epoch: float,
    current_bindings: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    if observation.get("experiment_scope_sha256") != cycle["experiment_scope_sha256"]:
        reasons.append("experiment scope")
    current = current_bindings or cycle["bindings"]
    bound = observation.get("bindings", {})
    for key in (
        "catalog_semantic_sha256",
        "model_scope_sha256",
        "runtime_binary",
        "runtime_label",
        "runtime_evidence_source",
        "input_manifest_sha256",
        "grader_identity_sha256",
        "rubric_sha256",
        "evaluation_suite_version",
        "evaluation_suite_sha256",
        "config_sha256",
        "run_budget_sha256",
    ):
        if bound.get(key) != current.get(key):
            reasons.append(key)
    if bound.get("task_feature_sha256") != cycle["task_feature_sha256"]:
        reasons.append("task feature")
    if at_epoch >= observation.get("expires_at_epoch", 0):
        reasons.append("TTL expired")
    if reasons:
        return "STALE", reasons
    return "CURRENT", []


def experiment_observations(
    store: Store,
    cycle: dict[str, Any],
    at_epoch: float | None = None,
    current_bindings: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    at = time.time() if at_epoch is None else at_epoch
    current: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    for event in store.events():
        if event.get("event_type") != "observation.recorded":
            continue
        observation = event["observation"]
        if observation.get("experiment_scope_sha256") != cycle["experiment_scope_sha256"]:
            continue
        status, reasons = observation_compatibility(cycle, observation, at, current_bindings)
        if status == "CURRENT":
            current.append(observation)
        else:
            stale.append({"observation": observation, "reasons": reasons})
    return current, stale


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _coefficient(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = abs(_mean(values))
    return statistics.pstdev(values) / mean if mean > 1e-12 else float("inf")


def _pair_observations(observations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    buckets: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    for observation in observations:
        key = (observation["partition"], observation["case_id"])
        buckets.setdefault(key, {"control": [], "challenger": []})[observation["arm"]].append(observation)
    pairs: list[dict[str, Any]] = []
    incomplete = 0
    for (partition, case_id), arms in sorted(buckets.items()):
        if not arms["control"] or not arms["challenger"]:
            incomplete += 1
            continue
        control = max(arms["control"], key=lambda item: item["created_at_epoch"])
        challenger = max(arms["challenger"], key=lambda item: item["created_at_epoch"])
        if control["bindings"]["input_manifest_sha256"] != challenger["bindings"]["input_manifest_sha256"]:
            incomplete += 1
            continue
        pairs.append(
            {
                "partition": partition,
                "case_id": case_id,
                "task_family": control["task_family"],
                "control": control,
                "challenger": challenger,
            }
        )
    return pairs, incomplete


def evaluate_cycle(
    store: Store,
    cycle_id: str,
    at_epoch: float | None = None,
    current_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    try:
        assert_live_cycle_authority(cycle)
    except LearningError as error:
        return {
            "state": "STALE",
            "decision": "KEEP_INCUMBENT",
            "efficiency_scored": False,
            "reason": f"live catalog or router-policy authority drift: {error}",
        }
    current, stale = experiment_observations(store, cycle, at_epoch, current_bindings)
    aborted = [
        event
        for event in store.events()
        if event.get("event_type") == "observation.aborted"
        and event.get("experiment_scope_sha256") == cycle["experiment_scope_sha256"]
    ]
    enforcement = [event for event in aborted if event.get("category") == "enforcement"]
    if enforcement:
        return {
            "state": "BLOCKED_MODEL_ENFORCEMENT",
            "decision": "BLOCK",
            "efficiency_scored": False,
            "errors": [event["reason"] for event in enforcement],
            "active_routing_contaminated": False,
        }
    pairs, incomplete = _pair_observations(current)
    development = [pair for pair in pairs if pair["partition"] == "development"]
    holdout = [pair for pair in pairs if pair["partition"] == "holdout"]
    requirements = cycle["requirements"]
    t4_controls = sum(
        1
        for observation in current
        if observation["arm"] == "control"
        and observation["worker_identity"]["model"] == T4_MODEL_SLUG
        and observation["worker_identity"]["effort"] == T4_EFFORT
    )
    count_report = {
        "development_pairs": len(development),
        "holdout_pairs": len(holdout),
        "task_families": sorted({pair["task_family"] for pair in development}),
        "exact_t4_controls": t4_controls,
        "incomplete_case_buckets": incomplete,
        "stale_observations": len(stale),
        "aborted_observations": len(aborted),
    }
    enough = (
        len(development) >= requirements.get("development_pairs", 1)
        and len(count_report["task_families"]) >= requirements.get("task_family_count", 1)
        and len(holdout) >= requirements.get("holdout_pairs", 0)
        and t4_controls >= requirements.get("exact_t4_controls", 0)
    )
    if not enough:
        return {
            "state": "STALE" if not current and stale else "COLLECTING",
            "decision": "KEEP_INCUMBENT",
            "efficiency_scored": False,
            "counts": count_report,
            "reason": "exact comparable evidence requirements are incomplete",
        }

    all_pairs = development + holdout
    hard_failures: list[str] = []
    for pair in all_pairs:
        challenger = pair["challenger"]
        safety = challenger["safety"]
        if safety["critical_failure"]:
            hard_failures.append(f"{pair['case_id']}: critical failure")
        if not safety["authorized_mutation"] or not safety["secret_safe"] or not safety["authority_safe"]:
            hard_failures.append(f"{pair['case_id']}: safety or authority failure")
        if not all(challenger["objective_gates"].values()):
            hard_failures.append(f"{pair['case_id']}: objective gate failure")
    quality_tolerance = float(cycle.get("quality_tolerance", 0.0))
    quality_regressions = [
        pair["case_id"]
        for pair in all_pairs
        if pair["challenger"]["quality_score"]
        < pair["control"]["quality_score"] - quality_tolerance
    ]
    if hard_failures or quality_regressions:
        return {
            "state": "REJECTED",
            "decision": "REJECT_COGNITIVE_OR_SAFETY_REGRESSION",
            "efficiency_scored": False,
            "counts": count_report,
            "hard_failures": hard_failures,
            "quality_regression_cases": quality_regressions,
        }

    token_savings = [
        (pair["control"]["resources"]["total_tokens"] - pair["challenger"]["resources"]["total_tokens"])
        / pair["control"]["resources"]["total_tokens"]
        for pair in development
        if pair["control"]["resources"]["total_tokens"] > 0
    ]
    time_savings = [
        (pair["control"]["resources"]["wall_time_ms"] - pair["challenger"]["resources"]["wall_time_ms"])
        / pair["control"]["resources"]["wall_time_ms"]
        for pair in development
        if pair["control"]["resources"]["wall_time_ms"] > 0
    ]
    if len(token_savings) != len(development) or len(time_savings) != len(development):
        return {
            "state": "COLLECTING",
            "decision": "KEEP_INCUMBENT",
            "efficiency_scored": False,
            "reason": "complete nonzero token and wall-time evidence is required",
            "counts": count_report,
        }
    token_gain, time_gain = _mean(token_savings), _mean(time_savings)
    max_variance = float(cycle.get("max_relative_variance", 1.0))
    uncertainty = {
        "token_coefficient_of_variation": _coefficient(token_savings),
        "time_coefficient_of_variation": _coefficient(time_savings),
    }
    if any(value > max_variance for value in uncertainty.values()):
        return {
            "state": "COLLECTING",
            "decision": "KEEP_INCUMBENT",
            "efficiency_scored": True,
            "reason": "efficiency variance remains excessive",
            "counts": count_report,
            "uncertainty": uncertainty,
            "reward_dimensions": {"token_savings": token_gain, "time_savings": time_gain},
        }
    control_quality = _mean([pair["control"]["quality_score"] for pair in all_pairs])
    challenger_quality = _mean([pair["challenger"]["quality_score"] for pair in all_pairs])
    minimum_token = float(cycle.get("minimum_token_savings", 0.0))
    minimum_time = float(cycle.get("minimum_time_savings", 0.0))
    pareto_pass = token_gain >= minimum_token and time_gain >= minimum_time and (token_gain > 0 or time_gain > 0)
    total_resources: dict[str, dict[str, int]] = {}
    for arm in ("control", "challenger"):
        rows = [pair[arm]["resources"] for pair in development]
        total_resources[arm] = {
            key: sum(int(row[key]) for row in rows)
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "context_replay_tokens",
                "reviewer_tokens",
                "grader_tokens",
                "tool_loop_tokens",
                "retry_tokens",
                "rework_tokens",
                "escalation_tokens",
                "invocation_count",
                "retry_count",
                "rework_count",
                "escalation_count",
                "wall_time_ms",
            )
        }
    report = {
        "state": "QUALIFIED" if pareto_pass else "REJECTED",
        "decision": "QUALIFY" if pareto_pass else "KEEP_INCUMBENT_PARETO",
        "efficiency_scored": True,
        "counts": count_report,
        "quality": {
            "control": control_quality,
            "challenger": challenger_quality,
            "margin": challenger_quality - control_quality,
            "tolerance": quality_tolerance,
        },
        "reward_dimensions": {
            "quality_margin": challenger_quality - control_quality,
            "token_savings": token_gain,
            "time_savings": time_gain,
            "scalar_reward": min(token_gain, time_gain),
        },
        "uncertainty": uncertainty,
        "confidence": min(1.0, len(development) / max(1, requirements.get("development_pairs", 1))),
        "resources": total_resources,
        "holdout_used_for_action_statistics": False,
    }
    if effective_lane(cycle) == "model" and report["state"] == "QUALIFIED":
        report["state"] = "COLLECTING"
        report["decision"] = "EVIDENCE_READY_AWAIT_ROUTER_LAB"
        report["authority"] = "router_lab.py"
    return report


def _append_state(
    store: Store,
    cycle: dict[str, Any],
    state: str,
    rationale: str,
    report: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    prior = cycle["state"]
    if state not in TRANSITIONS.get(prior, set()):
        raise LearningError(f"illegal state transition {prior} -> {state}")
    event = {
        "event_type": "cycle.state",
        "cycle_id": cycle["cycle_id"],
        "from_state": prior,
        "state": state,
        "decision": report.get("decision"),
        "rationale": rationale,
        "report": report,
        **extra,
    }
    store.append(event)
    return {"cycle_id": cycle["cycle_id"], "state": state, "report": report}


def advance(store: Store, cycle_id: str, rationale: str = "evidence evaluation") -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if effective_lane(cycle) == "model":
        raise LearningError("model states must be synchronized from router_lab.py authority")
    report = evaluate_cycle(store, cycle_id)
    target = report["state"]
    if target not in {"COLLECTING", "BLOCKED_MODEL_ENFORCEMENT", "QUALIFIED", "REJECTED", "STALE"}:
        raise LearningError("evaluation did not produce a prepromotion state")
    if cycle["state"] == "STAGED" and target in {"QUALIFIED", "REJECTED"}:
        return _append_state(
            store,
            cycle,
            "COLLECTING",
            "candidate entered explicit evidence collection before decision",
            {**report, "pending_decision_state": target},
        )
    result = _append_state(store, cycle, target, rationale, report)
    if target == "QUALIFIED" and cycle.get("lane") != "serendipity":
        store.append(
            {
                "event_type": "action_stat.update",
                "cycle_id": cycle_id,
                "task_feature_sha256": cycle["task_feature_sha256"],
                "action_sha256": digest(cycle["arms"]["challenger"]["action"]),
                "eligible_development_pairs": report["counts"]["development_pairs"],
                "reward_dimensions": report["reward_dimensions"],
                "holdout_included": False,
            }
        )
    return result


def sync_model_authority(store: Store, cycle_id: str) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if effective_lane(cycle) != "model" or cycle.get("lane") == "serendipity":
        raise LearningError("sync-model applies only to ordinary model experiments")
    candidates_path = MODEL_ROUTER_HOME / "candidates.json"
    policy_path = MODEL_ROUTER_HOME / "active-policy.json"
    if candidates_path.is_symlink() or not candidates_path.is_file():
        raise LearningError("router candidate authority is unavailable")
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    matches = [item for item in candidates.get("items", []) if item.get("candidate_id") == cycle["candidate_id"]]
    if len(matches) != 1:
        raise LearningError("router candidate is missing or ambiguous")
    candidate = matches[0]
    challenger = cycle["arms"]["challenger"]
    control = cycle["arms"]["control"]
    expected_candidate = {
        "tier": challenger["route"]["tier"],
        "candidate_model": challenger["route"]["model"],
        "candidate_effort": challenger["route"]["effort"],
        "candidate_profile_sha256": challenger["profile_sha256"],
        "incumbent_model": control["route"]["model"],
        "incumbent_effort": control["route"]["effort"],
        "incumbent_profile_sha256": control["profile_sha256"],
        "incumbent_policy_id": control["router_policy_sha256"],
        "catalog_semantic_hash": cycle["bindings"]["catalog_semantic_sha256"],
        "eval_suite_hash": cycle["bindings"]["evaluation_suite_sha256"],
    }
    mismatched = [key for key, value in expected_candidate.items() if candidate.get(key) != value]
    if mismatched:
        raise LearningError("router candidate authority binding drift: " + ", ".join(mismatched))
    router_state = ROUTER_STATE_MAP.get(candidate.get("status"))
    if router_state is None:
        raise LearningError("router candidate has an unsupported state")
    if router_state not in TRANSITIONS.get(cycle["state"], set()):
        if router_state == cycle["state"]:
            return {"cycle_id": cycle_id, "state": router_state, "changed": False}
        raise LearningError(f"router authority state cannot be mirrored from {cycle['state']} to {router_state}")
    report = evaluate_cycle(store, cycle_id)
    if router_state in {"QUALIFIED", "PROMOTED", "VALIDATED"} and report.get("decision") not in {
        "EVIDENCE_READY_AWAIT_ROUTER_LAB",
        "QUALIFY",
    }:
        raise LearningError("local aggregate evidence is not ready for the router authority state")
    authority = {
        "candidate_state_sha256": file_sha256(candidates_path, "router candidate authority"),
        "active_policy_sha256": file_sha256(policy_path, "router active policy") if policy_path.is_file() else None,
        "candidate_id": cycle["candidate_id"],
        "authority": "router_lab.py",
    }
    return _append_state(store, cycle, router_state, "mirrored router_lab.py authority", report, authority=authority)


def _json_leaf_diffs(before: Any, after: Any, prefix: str = "") -> list[str]:
    if type(before) is not type(after):
        return [prefix or "/"]
    if isinstance(before, dict):
        paths: list[str] = []
        for key in sorted(set(before) | set(after)):
            child = f"{prefix}/{key}"
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_json_leaf_diffs(before[key], after[key], child))
        return paths
    if isinstance(before, list):
        if before == after:
            return []
        return [prefix or "/"]
    return [] if before == after else [prefix or "/"]


def validate_workflow_authority(cycle: dict[str, Any]) -> tuple[Path, Path, bytes, bytes, list[str]]:
    authority = cycle.get("workflow_authority")
    if not isinstance(authority, dict):
        raise LearningError("workflow activation requires an exact authority manifest")
    active_path = regular_file(authority.get("active_path", ""), "active workflow policy")
    candidate_path = regular_file(authority.get("candidate_path", ""), "candidate workflow policy")
    before = active_path.read_bytes()
    after = candidate_path.read_bytes()
    if sha256_bytes(before) != cycle["arms"]["control"]["workflow_policy_sha256"]:
        raise LearningError("active workflow bytes do not match the bound incumbent")
    if sha256_bytes(after) != cycle["arms"]["challenger"]["workflow_policy_sha256"]:
        raise LearningError("candidate workflow bytes do not match the bound challenger")
    try:
        before_json, after_json = json.loads(before), json.loads(after)
    except json.JSONDecodeError as error:
        raise LearningError("workflow authority files must be JSON") from error
    diffs = _json_leaf_diffs(before_json, after_json)
    allowed_paths = cycle["change"].get("affected_paths")
    if not isinstance(allowed_paths, list) or not allowed_paths or not all(isinstance(item, str) for item in allowed_paths):
        raise LearningError("workflow change requires explicit affected_paths")
    if not diffs or any(not any(path == allowed or path.startswith(allowed + "/") for allowed in allowed_paths) for path in diffs):
        raise LearningError("candidate workflow contains a second causal change")
    return active_path, candidate_path, before, after, diffs


def _authority_transaction(
    store: Store,
    cycle: dict[str, Any],
    active_path: Path,
    before: bytes,
    after: bytes,
    target_state: str,
    rationale: str,
    report: dict[str, Any],
    snapshot_manifest: dict[str, Any],
) -> dict[str, Any]:
    if target_state not in TRANSITIONS.get(cycle["state"], set()):
        raise LearningError(f"illegal state transition {cycle['state']} -> {target_state}")
    state_event = {
        "event_type": "cycle.state",
        "event_id": digest(
            {
                "cycle_id": cycle["cycle_id"],
                "from": cycle["state"],
                "to": target_state,
                "before": sha256_bytes(before),
                "after": sha256_bytes(after),
            }
        ),
        "cycle_id": cycle["cycle_id"],
        "from_state": cycle["state"],
        "state": target_state,
        "decision": "ACTIVATE" if target_state == "PROMOTED" else "ROLLBACK",
        "rationale": rationale,
        "report": report,
        "snapshot_manifest": snapshot_manifest,
        "changed_or_restored_file": {
            "path": str(active_path),
            "before_sha256": sha256_bytes(before),
            "after_sha256": sha256_bytes(after),
        },
    }
    with store.locked():
        store._recover_ledger_locked()
        if store.authority_journal.exists():
            raise LearningError("an interrupted workflow authority transaction requires recovery")
        if sha256_bytes(active_path.read_bytes()) != sha256_bytes(before):
            raise LearningError("active workflow changed before transaction commit")
        journal = {
            "schema_version": 1,
            "cycle_id": cycle["cycle_id"],
            "target_state": target_state,
            "active_path": str(active_path),
            "before_sha256": sha256_bytes(before),
            "after_sha256": sha256_bytes(after),
            "before_base64": base64.b64encode(before).decode("ascii"),
            "after_base64": base64.b64encode(after).decode("ascii"),
            "state_event": state_event,
        }
        journal["journal_sha256"] = digest(journal)
        store._atomic_json(store.authority_journal, journal)
        store.fault_hook("authority_journal_written")
        store._atomic_bytes(active_path, after)
        store.fault_hook("authority_replaced")
        store._append_locked(state_event)
        store.fault_hook("authority_state_recorded")
        store._unlink(store.authority_journal)
    return {"cycle_id": cycle["cycle_id"], "state": target_state, "file_sha256": sha256_bytes(after)}


def promote_workflow(store: Store, cycle_id: str) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if (
        effective_lane(cycle) != "workflow"
        or cycle.get("lane") == "serendipity"
        or cycle["state"] != "QUALIFIED"
    ):
        raise LearningError("only an ordinary QUALIFIED workflow candidate can be promoted here")
    report = evaluate_cycle(store, cycle_id)
    if report.get("state") != "QUALIFIED" or report.get("decision") != "QUALIFY":
        raise LearningError("workflow promotion requires a fresh exact QUALIFY evaluation")
    active_path, _candidate_path, before, after, diffs = validate_workflow_authority(cycle)
    snapshot_dir = store.snapshots / cycle_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / "incumbent-workflows.json"
    store._atomic_bytes(snapshot_path, before)
    manifest = self_hashed(
        {
            "schema_version": 1,
            "cycle_id": cycle_id,
            "active_path": str(active_path),
            "snapshot_path": str(snapshot_path),
            "incumbent_sha256": sha256_bytes(before),
            "candidate_sha256": sha256_bytes(after),
            "changed_paths": diffs,
        },
        "snapshot_manifest_sha256",
    )
    manifest_path = snapshot_dir / "manifest.json"
    store._atomic_json(manifest_path, manifest)
    bound_manifest = {
        "snapshot_contract": manifest,
        "manifest_path": str(manifest_path),
        "manifest_file_sha256": file_sha256(manifest_path),
    }
    return _authority_transaction(
        store,
        cycle,
        active_path,
        before,
        after,
        "PROMOTED",
        "qualified workflow activation with exact incumbent snapshot",
        report,
        bound_manifest,
    )


def rollback_workflow(
    store: Store,
    cycle_id: str,
    reason: str,
    *,
    allow_active_drift: bool = False,
) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if effective_lane(cycle) != "workflow" or cycle["state"] != "PROMOTED":
        raise LearningError("only a PROMOTED workflow candidate can roll back here")
    manifest = cycle.get("snapshot_manifest")
    if not isinstance(manifest, dict):
        raise LearningError("promoted cycle lacks its bound rollback manifest")
    contract = manifest.get("snapshot_contract")
    if not isinstance(contract, dict):
        raise LearningError("rollback snapshot contract is missing")
    verify_self_hash(contract, "snapshot_manifest_sha256", "rollback snapshot manifest")
    manifest_path = regular_file(manifest.get("manifest_path", ""), "rollback manifest")
    if file_sha256(manifest_path) != manifest.get("manifest_file_sha256"):
        raise LearningError("rollback manifest file hash mismatch")
    snapshot_path = regular_file(contract.get("snapshot_path", ""), "rollback snapshot")
    snapshot = snapshot_path.read_bytes()
    if sha256_bytes(snapshot) != contract.get("incumbent_sha256"):
        raise LearningError("rollback snapshot bytes were tampered")
    active_path = regular_file(contract.get("active_path", ""), "active workflow policy")
    current = active_path.read_bytes()
    if not allow_active_drift and sha256_bytes(current) != contract.get("candidate_sha256"):
        raise LearningError("active workflow is not the candidate bound to this rollback")
    report = {
        "decision": "ROLLBACK",
        "reason": reason,
        "exact_snapshot_sha256": sha256_bytes(snapshot),
        "replaced_active_sha256": sha256_bytes(current),
        "active_drift_was_tolerated": allow_active_drift,
    }
    return _authority_transaction(
        store,
        cycle,
        active_path,
        current,
        snapshot,
        "ROLLED_BACK",
        reason,
        report,
        manifest,
    )


def monitor_workflow(store: Store, cycle_id: str) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if effective_lane(cycle) != "workflow" or cycle["state"] != "PROMOTED":
        raise LearningError("workflow monitor requires a PROMOTED candidate")
    manifest = cycle["snapshot_manifest"]
    contract = manifest.get("snapshot_contract", {})
    active_path = regular_file(contract["active_path"], "active workflow policy")
    actual = file_sha256(active_path)
    failure_reasons: list[str] = []
    try:
        assert_live_cycle_authority(cycle)
    except LearningError as error:
        failure_reasons.append(f"live route authority drift: {error}")
    if actual != contract["candidate_sha256"]:
        failure_reasons.append("active workflow hash differs from the promoted candidate")
    events = [
        event
        for event in store.events()
        if event.get("cycle_id") == cycle_id
        and event.get("event_type")
        in {"canary.observation.recorded", "canary.observation.aborted"}
    ]
    enforcement_aborts = [
        event
        for event in events
        if event.get("event_type") == "canary.observation.aborted"
        and event.get("category") == "enforcement"
    ]
    if enforcement_aborts:
        failure_reasons.extend(
            f"canary enforcement failure: {event.get('reason')}" for event in enforcement_aborts
        )
    observations = [
        event["observation"]
        for event in events
        if event.get("event_type") == "canary.observation.recorded"
    ]
    current_observations: list[dict[str, Any]] = []
    stale_canary_observations = 0
    for observation in observations:
        compatibility, reasons = observation_compatibility(
            cycle, observation, time.time()
        )
        if compatibility == "CURRENT":
            current_observations.append(observation)
        else:
            stale_canary_observations += 1
    buckets: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    for observation in current_observations:
        key = (observation["canary_window_id"], observation["case_id"])
        buckets.setdefault(key, {"control": [], "challenger": []})[observation["arm"]].append(
            observation
        )
    window_outcomes: dict[str, list[bool]] = {}
    incomplete_pairs = 0
    quality_tolerance = float(cycle.get("quality_tolerance", 0.0))
    for (window_id, case_id), arms in sorted(buckets.items()):
        if len(arms["control"]) != 1 or len(arms["challenger"]) != 1:
            incomplete_pairs += 1
            window_outcomes.setdefault(window_id, []).append(False)
            continue
        control = arms["control"][0]
        challenger = arms["challenger"][0]
        safety = challenger["safety"]
        if (
            challenger["worker_identity"]
            != {
                key: cycle["arms"]["challenger"]["route"][key]
                # The headless stream carries no observed service tier.
                for key in ("provider", "model", "effort")
            }
            or not all(challenger["objective_gates"].values())
            or challenger["quality_score"]
            < control["quality_score"] - quality_tolerance
            or not safety["authorized_mutation"]
            or not safety["secret_safe"]
            or not safety["authority_safe"]
            or safety["critical_failure"]
        ):
            failure_reasons.append(f"{window_id}/{case_id}: canary quality, identity, or safety regression")
            window_outcomes.setdefault(window_id, []).append(False)
        else:
            window_outcomes.setdefault(window_id, []).append(True)
    passed_windows = {
        window_id
        for window_id, outcomes in window_outcomes.items()
        if outcomes and all(outcomes)
    }
    required_windows = cycle["requirements"]["canary_windows"]
    canary_report = {
        "required_windows": required_windows,
        "passed_windows": sorted(passed_windows),
        "completed_windows": len(passed_windows),
        "recorded_observations": len(observations),
        "stale_observations": stale_canary_observations,
        "incomplete_pairs": incomplete_pairs,
        "aborted_observations": len(
            [event for event in events if event.get("event_type") == "canary.observation.aborted"]
        ),
        "active_sha256": actual,
        "candidate_sha256": contract["candidate_sha256"],
        "failure_reasons": failure_reasons,
        "evidence_event_sha256": [event["event_sha256"] for event in events],
    }
    store.append(
        {
            "event_type": "canary.monitor",
            "cycle_id": cycle_id,
            "canary": canary_report,
            "violation": bool(failure_reasons),
        }
    )
    if failure_reasons:
        return rollback_workflow(
            store,
            cycle_id,
            "canary identity, quality, safety, or authority regression: "
            + "; ".join(failure_reasons),
            allow_active_drift=True,
        )
    if len(passed_windows) < required_windows:
        return {
            "cycle_id": cycle_id,
            "state": "PROMOTED",
            "decision": "COLLECTING_CANARY",
            "canary": canary_report,
        }
    return _append_state(
        store,
        cycle,
        "VALIDATED",
        "configured canary windows passed",
        {"decision": "VALIDATE", "canary": canary_report},
        snapshot_manifest=manifest,
    )


def _descriptor_tokens(value: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()

    def walk(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key in sorted(item):
                walk(item[key], f"{path}.{key}" if path else key)
        elif isinstance(item, list):
            for entry in item:
                walk(entry, path)
        else:
            tokens.add(f"{path}={json.dumps(item, sort_keys=True)}")

    walk(value, "")
    return tokens


def novelty_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    a, b = _descriptor_tokens(left), _descriptor_tokens(right)
    if not a and not b:
        return 0.0
    return 1.0 - len(a & b) / len(a | b)


def novelty_archives(store: Store) -> list[dict[str, Any]]:
    archives: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for event in store.events():
        if event.get("event_type") == "novelty.archived":
            archive = json.loads(json.dumps(event["archive"]))
            archive["archive_event_sha256"] = event["event_sha256"]
            archive["archive_event_sequence"] = event["sequence"]
            archives.append(archive)
            by_id[archive["archive_id"]] = archive
        elif event.get("event_type") == "novelty.score_updated":
            archive = by_id.get(event.get("archive_id"))
            if archive is not None:
                archive["scores"]["cross_task_transfer_value"] = event[
                    "cross_task_transfer_value"
                ]
                archive["transfer_replay_count"] = event["replay_count"]
                archive["latest_score_event_sha256"] = event["event_sha256"]
    return archives


def archive_serendipity(store: Store, cycle_id: str) -> dict[str, Any]:
    cycle = current_state(store, cycle_id)
    if cycle.get("lane") != "serendipity":
        raise LearningError("only a SERENDIPITY cycle enters the novelty archive")
    report = evaluate_cycle(store, cycle_id)
    current, _stale = experiment_observations(store, cycle)
    challengers = [item for item in current if item["arm"] == "challenger"]
    if not challengers:
        raise LearningError("SERENDIPITY archive requires a measured challenger")
    safe = all(
        item["safety"]["authorized_mutation"]
        and item["safety"]["secret_safe"]
        and item["safety"]["authority_safe"]
        and not item["safety"]["critical_failure"]
        and all(item["objective_gates"].values())
        for item in challengers
    )
    if not safe:
        raise LearningError("unsafe or incorrect failures remain in the ledger but cannot enter the novelty archive")
    descriptor = cycle["serendipity"]["behavioral_descriptor"]
    prior = novelty_archives(store)
    archive_id = digest(
        {
            "cycle_id": cycle_id,
            "descriptor": descriptor,
            "action": cycle["arms"]["challenger"]["action"],
        }
    )
    if any(item["archive_id"] == archive_id for item in prior):
        raise LearningError("SERENDIPITY cycle is already present in the novelty archive")
    novelty = min(
        (novelty_distance(descriptor, item["behavioral_descriptor"]) for item in prior),
        default=1.0,
    )
    quality = _mean([item["quality_score"] for item in challengers])
    same_descriptor = [
        item["scores"]["minimum_quality"]["observed"]
        for item in prior
        if item["descriptor_sha256"] == digest(descriptor)
    ]
    learning_progress = abs(quality - _mean(same_descriptor)) if same_descriptor else 0.0
    transfer = 0.0
    minimum_quality = float(cycle.get("serendipity", {}).get("minimum_quality", 0.0))
    candidate_semantics = {
        "mutation_lane": effective_lane(cycle),
        "variable_id": cycle["change"]["variable_id"],
        "action_sha256": digest(cycle["arms"]["challenger"]["action"]),
        "profile_sha256": cycle["arms"]["challenger"]["profile_sha256"],
        "router_policy_sha256": cycle["arms"]["challenger"]["router_policy_sha256"],
        "workflow_policy_sha256": cycle["arms"]["challenger"]["workflow_policy_sha256"],
    }
    archive = {
        "archive_id": archive_id,
        "cycle_id": cycle_id,
        "source_task_family": cycle["features"]["task_family"],
        "behavioral_descriptor": descriptor,
        "descriptor_sha256": digest(descriptor),
        "action_sha256": digest(cycle["arms"]["challenger"]["action"]),
        "candidate_semantics": candidate_semantics,
        "candidate_semantics_sha256": digest(candidate_semantics),
        "precommitted_replay_task_families": cycle["serendipity"][
            "replay_task_families"
        ],
        "scores": {
            "minimum_quality": {
                "observed": quality,
                "threshold": minimum_quality,
                "passed": quality >= minimum_quality,
            },
            "novelty": novelty,
            "learning_progress": learning_progress,
            "cross_task_transfer_value": transfer,
        },
        "informative_failure": report["state"] != "QUALIFIED",
        "promotion_credit": False,
        "safety_and_authority_passed": True,
        "experiment_scope_sha256": cycle["experiment_scope_sha256"],
    }
    store.append({"event_type": "novelty.archived", "cycle_id": cycle_id, "archive": archive})
    return archive


def replay_archive_candidate(store: Store, archive_id: str, result: dict[str, Any]) -> dict[str, Any]:
    matches = [item for item in novelty_archives(store) if item["archive_id"] == archive_id]
    if len(matches) != 1:
        raise LearningError("novelty archive ID is missing or ambiguous")
    archive = matches[0]
    if not isinstance(result, dict) or not isinstance(result.get("observation_id"), str):
        raise LearningError("novelty replay requires an exact recorded observation ID")
    observations = [
        event["observation"]
        for event in store.events()
        if event.get("event_type") == "observation.recorded"
        and event.get("observation", {}).get("observation_id") == result["observation_id"]
    ]
    if len(observations) != 1:
        raise LearningError("novelty replay observation is missing or ambiguous")
    observation = observations[0]
    origin = current_state(store, observation["origin_cycle_id"])
    candidate_semantics = {
        "mutation_lane": effective_lane(origin),
        "variable_id": origin["change"]["variable_id"],
        "action_sha256": ll_digest_action(origin),
        "profile_sha256": origin["arms"]["challenger"]["profile_sha256"],
        "router_policy_sha256": origin["arms"]["challenger"]["router_policy_sha256"],
        "workflow_policy_sha256": origin["arms"]["challenger"]["workflow_policy_sha256"],
    }
    if (
        observation["partition"] != "holdout"
        or observation["arm"] != "challenger"
        or ll_digest_action(origin) != archive["action_sha256"]
        or digest(candidate_semantics) != archive["candidate_semantics_sha256"]
    ):
        raise LearningError("novelty replay must be a held-out challenger of the archived action")
    if (
        observation["task_family"] == archive["source_task_family"]
        or observation["task_family"]
        not in archive["precommitted_replay_task_families"]
    ):
        raise LearningError("novelty replay must use an unrelated held-out task family")
    if set(result) != {"observation_id"}:
        raise LearningError("novelty replay accepts only the exact observation ID")
    minimum_quality = archive["scores"]["minimum_quality"]["threshold"]
    safety = observation["safety"]
    transfer_pass = (
        observation["promotion_eligible"] is True
        and all(observation["objective_gates"].values())
        and safety["authorized_mutation"]
        and safety["secret_safe"]
        and safety["authority_safe"]
        and not safety["critical_failure"]
        and observation["quality_score"] >= minimum_quality
    )
    replay_result = {
        "observation_id": observation["observation_id"],
        "heldout_task_family": observation["task_family"],
        "quality_score": observation["quality_score"],
        "minimum_quality": float(minimum_quality),
        "transfer_pass": transfer_pass,
        "evidence_sha256": digest(observation),
    }
    event = {
        "event_type": "novelty.replay",
        "cycle_id": archive["cycle_id"],
        "archive_id": archive_id,
        "result": replay_result,
        "promotion_credit": False,
    }
    store.append(event)
    replay_events = [
        item
        for item in store.events()
        if item.get("event_type") == "novelty.replay"
        and item.get("archive_id") == archive_id
    ]
    transfer = _mean(
        [1.0 if item["result"]["transfer_pass"] else 0.0 for item in replay_events]
    )
    store.append(
        {
            "event_type": "novelty.score_updated",
            "cycle_id": archive["cycle_id"],
            "archive_id": archive_id,
            "score": "cross_task_transfer_value",
            "cross_task_transfer_value": transfer,
            "replay_count": len(replay_events),
            "source_replay_event_sha256": [item["event_sha256"] for item in replay_events],
            "changes_promotion_state": False,
        }
    )
    return event["result"]


def ll_digest_action(cycle: dict[str, Any]) -> str:
    return digest(cycle["arms"]["challenger"]["action"])


def credit_stepping_stone(
    store: Store,
    archive_id: str,
    validated_cycle_id: str,
    causal_manifest_path: str,
    causal_manifest_sha256: str,
) -> dict[str, Any]:
    events = store.events()
    archive_events = [
        event
        for event in events
        if event.get("event_type") == "novelty.archived"
        and event.get("archive", {}).get("archive_id") == archive_id
    ]
    if len(archive_events) != 1:
        raise LearningError("novelty archive ID is missing or ambiguous")
    archive_event = archive_events[0]
    archive = archive_event["archive"]
    target = current_state(store, validated_cycle_id)
    if target["state"] != "VALIDATED":
        raise LearningError("stepping-stone credit requires a later VALIDATED improvement")
    if target.get("lane") == "serendipity":
        raise LearningError("a SERENDIPITY cycle cannot receive stepping-stone promotion credit")
    lineage_entry = next(
        (
            item
            for item in target.get("lineage", {}).get("entries", [])
            if item.get("archive_id") == archive_id
        ),
        None,
    )
    if lineage_entry is None:
        raise LearningError("validated candidate did not bind this archive in its causal lineage")
    begin_events = [
        event
        for event in events
        if event.get("event_type") == "cycle.begin" and event.get("cycle_id") == validated_cycle_id
    ]
    validated_events = [
        event
        for event in events
        if event.get("event_type") == "cycle.state"
        and event.get("cycle_id") == validated_cycle_id
        and event.get("state") == "VALIDATED"
    ]
    if len(begin_events) != 1 or len(validated_events) != 1:
        raise LearningError("validated cycle ledger ordering is missing or ambiguous")
    begin_event, validated_event = begin_events[0], validated_events[0]
    if not (
        archive_event["sequence"] < begin_event["sequence"] < validated_event["sequence"]
    ):
        raise LearningError("stepping-stone credit requires archive-before-begin-before-validation order")
    if (
        lineage_entry.get("archive_event_sha256") != archive_event["event_sha256"]
        or lineage_entry.get("archive_event_sequence") != archive_event["sequence"]
        or lineage_entry.get("archive_action_sha256") != archive["action_sha256"]
    ):
        raise LearningError("validated cycle lineage no longer matches the archived event")
    if any(
        event.get("event_type") == "novelty.stepping_stone_credit"
        and event.get("archive_id") == archive_id
        and event.get("validated_cycle_id") == validated_cycle_id
        for event in events
    ):
        raise LearningError("stepping-stone credit already exists for this archive and cycle")
    causal_manifest = read_bound_json(
        causal_manifest_path,
        causal_manifest_sha256,
        "stepping-stone causal manifest",
    )
    if (
        causal_manifest.get("schema_version") != 1
        or causal_manifest.get("archive_id") != archive_id
        or causal_manifest.get("validated_cycle_id") != validated_cycle_id
        or causal_manifest.get("archive_action_sha256") != archive["action_sha256"]
        or causal_manifest.get("archive_event_sha256") != archive_event["event_sha256"]
        or causal_manifest.get("target_begin_event_sha256") != begin_event["event_sha256"]
        or causal_manifest.get("validated_state_event_sha256")
        != validated_event["event_sha256"]
        or causal_manifest.get("target_experiment_scope_sha256")
        != target["experiment_scope_sha256"]
        or causal_manifest.get("lineage_sha256")
        != target["lineage"]["lineage_sha256"]
    ):
        raise LearningError("stepping-stone causal manifest does not bind the exact lineage")
    event = {
        "event_type": "novelty.stepping_stone_credit",
        "cycle_id": validated_cycle_id,
        "archive_id": archive_id,
        "validated_cycle_id": validated_cycle_id,
        "causal_manifest_sha256": causal_manifest_sha256,
        "causal_manifest_path": str(regular_file(causal_manifest_path, "stepping-stone causal manifest")),
        "credit_kind": "retrospective_causal_stepping_stone",
        "changes_promotion_state": False,
    }
    store.append(event)
    return event


def sparse_escalation(features: dict[str, Any], signals: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_features(features)
    remediation_count = signals.get("remediation_count", 0)
    if type(remediation_count) is not int or remediation_count < 0:
        raise LearningError("remediation count must be a nonnegative integer")
    if remediation_count > 1:
        return {"tier": None, "decision": "SURFACE_UNRESOLVED", "remediation_allowed": False}
    if signals.get("deterministic_complete") is True and signals.get("model_needed") is not True:
        return {"tier": "T0", "decision": "DETERMINISTIC_COMPLETE", "remediation_allowed": remediation_count == 0}
    terminal = (
        normalized["terminal_strategy"]
        or signals.get("disputed_promotion") is True
        or signals.get("critical_cross_system_conflict") is True
        or signals.get("harness_policy_architecture") is True
    )
    if terminal:
        return {
            "tier": "T4",
            "decision": "TERMINAL_READ_ONLY_ADJUDICATION",
            "required_model": T4_MODEL_SLUG,
            "required_effort": T4_EFFORT,
            "sandbox": "read-only",
            "remediation_allowed": remediation_count == 0,
        }
    material = (
        normalized["ambiguity"] in {"high", "novel"}
        or normalized["risk"] in {"high", "critical"}
        or signals.get("material_conflict") is True
        or signals.get("objective_failure") is True
    )
    if material:
        return {
            "tier": "T3",
            "decision": "INDEPENDENT_MATERIAL_REVIEW",
            "remediation_allowed": remediation_count == 0,
        }
    tier = "T1" if normalized["mutation"] == "none" and normalized["scope"] in {"small", "local"} else "T2"
    return {"tier": tier, "decision": "ROUTINE_EXECUTION", "remediation_allowed": remediation_count == 0}


def validate_review_packet(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "recommendation",
        "alternatives",
        "objective_results",
        "material_disagreements",
        "failure_signals",
        "evidence",
        "source_excerpts",
    }
    if set(value) - allowed:
        raise LearningError("review packet contains unbounded upstream material")
    if not isinstance(value.get("evidence"), list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and HASH_RE.fullmatch(str(item.get("sha256", "")))
        for item in value["evidence"]
    ):
        raise LearningError("review packet evidence paths and hashes are required")
    excerpts = value.get("source_excerpts", [])
    if not isinstance(excerpts, list) or not all(isinstance(item, str) for item in excerpts):
        raise LearningError("review excerpts must be a string list")
    if sum(len(item.encode("utf-8")) for item in excerpts) > 12_000:
        raise LearningError("review excerpts exceed the compact-packet bound")
    if any(
        "claude-cli." in item
        or "raw transcript" in item.lower()
        or "raw stream" in item.lower()
        for item in excerpts
    ) or any(
        CLAUDE_STREAM_NAME_RE.search(str(item.get("path", ""))) is not None
        for item in value["evidence"]
    ):
        raise LearningError("raw transcripts do not belong in compact review packets")
    return value


def sweep_stale(store: Store, current_bindings: dict[str, Any], at_epoch: float | None = None) -> dict[str, int]:
    at = time.time() if at_epoch is None else at_epoch
    already = {
        event.get("observation_id")
        for event in store.events()
        if event.get("event_type") == "evidence.stale"
    }
    count = 0
    for cycle in replay(store).values():
        _current, stale = experiment_observations(store, cycle, at, current_bindings)
        for item in stale:
            observation_id = item["observation"]["observation_id"]
            if observation_id in already:
                continue
            store.append(
                {
                    "event_type": "evidence.stale",
                    "cycle_id": cycle["cycle_id"],
                    "observation_id": observation_id,
                    "reasons": item["reasons"],
                }
            )
            already.add(observation_id)
            count += 1
    return {"marked_stale": count}


def recommend(store: Store, features: dict[str, Any]) -> dict[str, Any]:
    target = feature_hash(features)
    rows = [
        cycle
        for cycle in replay(store).values()
        if cycle["state"] == "VALIDATED"
        and cycle["task_feature_sha256"] == target
        and effective_lane(cycle) == "workflow"
        and cycle.get("lane") != "serendipity"
    ]
    if not rows:
        return {
            "task_feature_sha256": target,
            "recommendation": None,
            "authority": "active router/workflow policy remains authoritative",
            "exploration_count": 1,
        }
    winner = max(rows, key=lambda cycle: cycle.get("created_at_epoch", 0))
    return {
        "task_feature_sha256": target,
        "recommendation": winner["arms"]["challenger"]["action"],
        "recommendation_kind": "validated_workflow_advisory",
        "authority": "workflow activation transaction",
        "exploration_count": 0,
    }


def no_call(store: Store, reason: str = "no eligible experiment") -> dict[str, Any]:
    event = store.append(
        {
            "event_type": "cycle.no_call",
            "cycle_id": f"no-call-{uuid.uuid4().hex}",
            "reason": reason,
            "experiment_calls": 0,
        }
    )
    return {"status": "NO_CALL", "experiment_calls": 0, "reason": reason, "event_sha256": event["event_sha256"]}


def serendipity_schedule(store: Store) -> dict[str, Any]:
    policy = load_learning_policy()["serendipity"]
    events = store.events()
    cycles = [event for event in events if event.get("event_type") == "cycle.begin"]
    replays = [event for event in events if event.get("event_type") == "novelty.replay"]
    last_replay_sequence = replays[-1]["sequence"] if replays else 0
    cycles_since_replay = sum(
        1 for event in cycles if event["sequence"] > last_replay_sequence
    )
    interval = int(policy["replay_interval_cycles"])
    archives = novelty_archives(store)
    return {
        "capacity": allocate_serendipity(store),
        "replay_interval_cycles": interval,
        "cycles_since_replay": cycles_since_replay,
        "replay_due": bool(archives) and cycles_since_replay >= interval,
        "eligible_archive_ids": [archive["archive_id"] for archive in archives],
        "diagnostic_only": True,
        "promotion_gate_bypass": False,
    }


def status(store: Store) -> dict[str, Any]:
    cycles = replay(store)
    counts = {state: 0 for state in sorted(STATES)}
    for cycle in cycles.values():
        counts[cycle["state"]] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "learning_policy_sha256": file_sha256(LEARNING_POLICY_PATH, "learning-loop policy"),
        "home": str(store.home),
        "ledger_sha256": file_sha256(store.ledger, "learning ledger"),
        "cycles": len(cycles),
        "states": counts,
        "explicit_candidates": [
            {
                "cycle_id": cycle["cycle_id"],
                "candidate_id": cycle["candidate_id"],
                "lane": cycle["lane"],
                "state": cycle["state"],
            }
            for cycle in cycles.values()
            if cycle["state"] not in TERMINAL_STATES
        ],
        "novelty_archive_size": len(novelty_archives(store)),
        "serendipity_schedule": serendipity_schedule(store),
        "stepping_stone_credits": sum(
            1 for event in store.events() if event.get("event_type") == "novelty.stepping_stone_credit"
        ),
        "action_stat_updates": sum(1 for event in store.events() if event.get("event_type") == "action_stat.update"),
        "no_call_cycles": sum(1 for event in store.events() if event.get("event_type") == "cycle.no_call"),
    }


def doctor(store: Store) -> dict[str, Any]:
    try:
        recovery = store.recover()
        events = store.events()
        replay(store)
        return {
            "status": "HEALTHY",
            "home": str(store.home),
            "events": len(events),
            "ledger_sha256": file_sha256(store.ledger, "learning ledger"),
            "recovery": recovery,
            "model_transition_authority": "router_lab.py",
            "workflow_transition_authority": "journaled exact-byte transaction",
        }
    except Exception as error:
        return {"status": "DEGRADED", "home": str(store.home), "error": str(error)}


SIMULATION_CLAUDE_BINARY = "/usr/local/bin/claude"


def simulation_catalog_models() -> dict[str, dict[str, Any]]:
    """Normalize the curated catalog the way router_lab.py writes catalog.json."""
    payload = json.loads(
        regular_file(CURATED_CATALOG_PATH, "curated model catalog").read_text(encoding="utf-8")
    )
    models: dict[str, dict[str, Any]] = {}
    for entry in payload["models"]:
        slug = entry["slug"]
        models[slug] = {
            "slug": slug,
            "provider": entry.get("provider", PROVIDER),
            "model_id": entry.get("model_id"),
            "visible": bool(entry.get("visible")),
            "selectable": bool(entry.get("selectable")),
            "supported_efforts": sorted(set(entry.get("supported_efforts") or [])),
            "input_modalities": sorted(entry.get("input_modalities") or []),
            "context_window": entry.get("context_window"),
            "capabilities": sorted(set(entry.get("capabilities") or [])),
            "instruction_hash": digest({"instructions": entry.get("instructions")}),
        }
        models[slug]["revision_hash"] = digest(models[slug])
    return models


def _simulation_write(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_bytes(value) + b"\n"
    path.write_bytes(content)
    return sha256_bytes(content)


def simulation() -> dict[str, Any]:
    """Deterministic multi-cycle workflow activation and exact rollback proof."""
    global MODEL_ROUTER_HOME, DISPATCH_REGISTRY_PATH
    prior_model_router_home = MODEL_ROUTER_HOME
    prior_dispatch_registry_path = DISPATCH_REGISTRY_PATH
    with tempfile.TemporaryDirectory(prefix="adaptive-learning-simulation-") as temporary:
        root = Path(temporary)
        MODEL_ROUTER_HOME = root / "model-router"
        MODEL_ROUTER_HOME.mkdir()
        DISPATCH_REGISTRY_PATH = root / "execution-registry.jsonl"
        DISPATCH_REGISTRY_PATH.write_bytes(b"")
        os.chmod(DISPATCH_REGISTRY_PATH, 0o600)
        registry_events: list[dict[str, Any]] = []

        def registry_receipt(receipt_type: str, payload: dict[str, Any]) -> dict[str, Any]:
            receipt = {
                "schema_version": 1,
                "receipt_type": receipt_type,
                "receipt_id": uuid.uuid4().hex,
                "issued_at_epoch": time.time(),
                **payload,
                "sequence": len(registry_events) + 1,
                "previous_receipt_sha256": (
                    registry_events[-1]["receipt_sha256"]
                    if registry_events
                    else "0" * 64
                ),
            }
            receipt["receipt_sha256"] = digest(receipt)
            registry_events.append(receipt)
            DISPATCH_REGISTRY_PATH.write_bytes(
                b"".join(canonical_bytes(item) + b"\n" for item in registry_events)
            )
            os.chmod(DISPATCH_REGISTRY_PATH, 0o600)
            return receipt

        store = Store(root / "ledger")
        active_path = root / "workflows.json"
        candidate_path = root / "candidate-workflows.json"
        active_bytes = canonical_bytes({"strategy": {"review": "always"}, "stable": True}) + b"\n"
        candidate_bytes = canonical_bytes({"strategy": {"review": "on_material_risk"}, "stable": True}) + b"\n"
        active_path.write_bytes(active_bytes)
        candidate_path.write_bytes(candidate_bytes)
        curated_family = curated_catalog_family()
        curated_slugs = family_slugs(curated_family)
        route = {
            "provider": PROVIDER,
            "model": "sonnet" if "sonnet" in curated_slugs else curated_slugs[0],
            "effort": "medium",
            "service_tier": "default",
            "tier": "T2",
            "mutation": "none",
            "sandbox": "read-only",
            "fallback": None,
        }
        router_policy_id = digest("router-policy")
        models = simulation_catalog_models()
        catalog = {
            "schema_version": 1,
            "source_path": str(CURATED_CATALOG_PATH),
            "source_sha256": file_sha256(CURATED_CATALOG_PATH, "curated model catalog"),
            "semantic_hash": digest([models[key] for key in sorted(models)]),
            "models": models,
        }
        catalog_path = MODEL_ROUTER_HOME / "catalog.json"
        _simulation_write(catalog_path, catalog)
        policy = {
            "schema_version": 1,
            "policy_id": router_policy_id,
            "constraints": {
                "allowed_provider": PROVIDER,
                "allowed_service_tiers": ["default", "priority"],
                "require_local_catalog_availability": True,
            },
            "tiers": {"T2": {"model": route["model"], "effort": "medium"}},
        }
        policy_path = MODEL_ROUTER_HOME / "active-policy.json"
        _simulation_write(policy_path, policy)
        execution_manifest_paths = {
            arm: root / f"{arm}-execution-manifest.json"
            for arm in ("control", "challenger")
        }
        execution_hashes = {
            arm: _simulation_write(path, {"schema_version": 1, "arm": arm})
            for arm, path in execution_manifest_paths.items()
        }
        grader_identity = file_sha256(
            DETERMINISTIC_GRADER_PATH, "bundled deterministic grader"
        )
        shared_manifest = {
            "schema_version": 1,
            "prompt_sha256": digest("prompt"),
            "context_sha256": digest("context"),
            "tools": [],
            "permissions": {"sandbox": "read-only", "network": False},
            "service_tier": "default",
            "retry_rule": {"transient": 0, "cognitive": 0},
            "grader_identity_sha256": grader_identity,
            "rubric_sha256": digest("rubric"),
            "token_cap_per_arm": 180_000,
            "wall_time_cap_seconds_per_arm": 300,
            "execution_manifest_sha256_by_arm": {
                "control": [execution_hashes["control"]],
                "challenger": [execution_hashes["challenger"]],
            },
            "planned_invocations_by_arm": {
                arm: [
                    {
                        "planned_call_id": f"{arm}-worker",
                        "role": "worker",
                        "phase": "primary",
                        "attempt_kind": "initial",
                        "token_cap": 180_000,
                        "wall_time_cap_seconds": 300,
                        "expected_inferences": 1,
                        "execution_manifest_sha256": execution_hashes[arm],
                    }
                ]
                for arm in ("control", "challenger")
            },
        }
        shared_path = root / "shared-input-manifest.json"
        shared_hash = _simulation_write(shared_path, shared_manifest)
        budget = build_budget_manifest(
            shared_hash,
            [
                {
                    "call_id": "control-worker",
                    "arm": "control",
                    "role": "worker",
                    "phase": "primary",
                    "attempt_kind": "initial",
                    "execution_manifest_sha256": execution_hashes["control"],
                    "token_cap": 180_000,
                    "expected_inferences": 1,
                    "context_growth_allowance": 40_000,
                    "wall_time_seconds": 300,
                    "rationale": "derived from the bound context and objective grader",
                    "cap_mode": "derived",
                },
                {
                    "call_id": "challenger-worker",
                    "arm": "challenger",
                    "role": "worker",
                    "phase": "primary",
                    "attempt_kind": "initial",
                    "execution_manifest_sha256": execution_hashes["challenger"],
                    "token_cap": 180_000,
                    "expected_inferences": 1,
                    "context_growth_allowance": 40_000,
                    "wall_time_seconds": 300,
                    "rationale": "same requirement-derived bound as the control",
                    "cap_mode": "derived",
                },
            ],
            reviewer_allowance=20_000,
            grader_allowance=10_000,
        )
        budget_path = root / "run-budget.json"
        budget_hash = _simulation_write(budget_path, budget)
        bindings = {
            "catalog_semantic_sha256": catalog["semantic_hash"],
            "catalog_file_sha256": file_sha256(catalog_path),
            "catalog_path": str(catalog_path),
            "model_scope_sha256": digest(
                {
                    "rule": "curated-catalog-slug-set",
                    "curated_catalog_sha256": catalog["source_sha256"],
                    "routable_models": list(curated_slugs),
                }
            ),
            "router_policy_file_sha256": file_sha256(policy_path),
            "router_policy_path": str(policy_path),
            "runtime_binary": SIMULATION_CLAUDE_BINARY,
            "runtime_label": CLAUDE_RUNTIME_LABEL,
            "runtime_evidence_source": CLAUDE_STREAM_SOURCE,
            "input_manifest_path": str(shared_path),
            "input_manifest_sha256": shared_hash,
            "grader_identity_sha256": shared_manifest["grader_identity_sha256"],
            "rubric_sha256": shared_manifest["rubric_sha256"],
            "evaluation_suite_version": "simulation-v1",
            "evaluation_suite_sha256": digest("suite"),
            "config_sha256": digest("config"),
            "run_budget_path": str(budget_path),
            "run_budget_sha256": budget_hash,
            "evidence_ttl_seconds": 86_400,
        }
        base = {
            "lane": "workflow",
            "candidate_id": "simulation-workflow-candidate",
            "current_family": curated_family,
            "features": {
                "activity": "implement",
                "scope": "cross_system",
                "ambiguity": "low",
                "risk": "moderate",
                "risk_scope": "harness",
                "risk_tags": [],
                "mutation": "reversible",
                "context_size": "medium",
                "tools": [],
                "current_information": False,
                "objective_grader": True,
                "workflow_family": "coding.change",
                "task_family": "harness",
                "holdout": False,
                "terminal_strategy": False,
            },
            "hypothesis": "material-risk review preserves quality while reducing routine overhead",
            "change": {
                "variable_id": "workflow.review_escalation",
                "before": "always",
                "after": "on_material_risk",
                "affected_paths": ["/strategy/review"],
            },
            "arms": {
                "control": {
                    "action": "always",
                    "route": route,
                    "profile_sha256": digest("profile"),
                    "router_policy_sha256": router_policy_id,
                    "workflow_policy_sha256": sha256_bytes(active_bytes),
                },
                "challenger": {
                    "action": "on_material_risk",
                    "route": route,
                    "profile_sha256": digest("profile"),
                    "router_policy_sha256": router_policy_id,
                    "workflow_policy_sha256": sha256_bytes(candidate_bytes),
                },
            },
            "frozen": {"router_policy_sha256": router_policy_id},
            "bindings": bindings,
            "requirements": {
                "development_pairs": 2,
                "task_family_count": 2,
                "holdout_pairs": 1,
                "exact_t4_controls": 0,
                "canary_windows": 2,
            },
            "quality_tolerance": 0.0,
            "minimum_token_savings": 0.1,
            "minimum_time_savings": 0.1,
            "max_relative_variance": 1.0,
            "workflow_authority": {"active_path": str(active_path), "candidate_path": str(candidate_path)},
        }

        def metadata(arm: str, case: str, tokens: int, wall: int) -> str:
            """Emit one Claude-shaped retained stream plus its registry receipt."""
            nonce = uuid.uuid4().hex[:8]
            run_dir = root / "runs" / nonce
            run_dir.mkdir(parents=True, exist_ok=True)
            stream_path = run_dir / "claude-cli.0001.jsonl"
            session_id = f"session-{case}-{arm}-{nonce}"
            model_id = models[route["model"]]["model_id"]
            usage = {"input_tokens": tokens - 100, "output_tokens": 100}
            model_usage = {model_id: {"inputTokens": tokens - 100, "outputTokens": 100}}
            stream_events = [
                {"type": "system", "subtype": "init", "session_id": session_id},
                {
                    "type": "assistant",
                    "session_id": session_id,
                    "message": {"id": f"msg-{nonce}", "model": model_id, "role": "assistant"},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": session_id,
                    "duration_ms": wall,
                    "duration_api_ms": wall,
                    "num_turns": 1,
                    "total_cost_usd": 0.01,
                    "usage": usage,
                    "modelUsage": model_usage,
                },
            ]
            stream_path.write_text(
                "".join(canonical_bytes(event).decode("utf-8") + "\n" for event in stream_events),
                encoding="utf-8",
            )
            command = [
                SIMULATION_CLAUDE_BINARY,
                "-p",
                "--model",
                route["model"],
                "--effort",
                route["effort"],
                "--output-format",
                "stream-json",
                "--permission-mode",
                "plan",
            ]
            receipt = registry_receipt(
                "execution",
                {
                    "runtime": CLAUDE_RUNTIME_LABEL,
                    "evidence_source": CLAUDE_STREAM_SOURCE,
                    "evidence_grade": DECLARED_EVIDENCE_GRADE,
                    "run_id": nonce,
                    "command": command,
                    "requested_model": route["model"],
                    "requested_effort": route["effort"],
                    "observed_models": [model_id],
                    "dispatch_packet_sha256": execution_hashes[arm],
                    "phase_contract_sha256": digest("phase-contract"),
                    "prompt_sha256": digest("prompt"),
                    "stream_path": str(stream_path.resolve()),
                    "stream_sha256": file_sha256(stream_path),
                    "stream_event_count": len(stream_events),
                    "elapsed_ms": wall,
                    "runtime_metadata": {
                        "evidence_source": CLAUDE_METADATA_SOURCE,
                        "evidence_grade": DECLARED_EVIDENCE_GRADE,
                        "session_id": session_id,
                        "subtype": "success",
                        "is_error": False,
                        "duration_ms": wall,
                        "num_turns": 1,
                        "total_cost_usd": 0.01,
                        "usage": usage,
                        "model_usage": model_usage,
                    },
                    "scored_evidence_status": BLOCKED_SCORED_STATUS,
                },
            )
            return receipt["receipt_id"]

        def artifact(
            cycle: dict[str, Any],
            case: str,
            family: str,
            partition: str,
            arm: str,
            *,
            evidence_phase: str = "prepromotion",
            canary_window_id: str | None = None,
            critical_failure: bool = False,
        ) -> tuple[Path, str]:
            control_tokens, challenger_tokens = 1000, 700
            control_wall, challenger_wall = 1000, 700
            tokens = control_tokens if arm == "control" else challenger_tokens
            wall = control_wall if arm == "control" else challenger_wall
            execution_receipt_id = metadata(arm, case, tokens, wall)
            quality = {
                "schema_version": 1,
                "case_id": case,
                "evaluated_arm": arm,
                "evaluated_execution_receipt_ids": [execution_receipt_id],
                "grader_kind": "deterministic",
                "grader_identity_sha256": bindings["grader_identity_sha256"],
                "rubric_sha256": bindings["rubric_sha256"],
                "arms": {arm: {"quality_score": 1.0, "objective_gates": {"exact": True}}},
            }
            quality_path = root / f"{case}-{arm}-{uuid.uuid4().hex[:8]}-quality.json"
            quality_hash = _simulation_write(quality_path, quality)
            quality_receipt = registry_receipt(
                "quality",
                {
                    "quality_artifact_path": str(quality_path.resolve()),
                    "quality_artifact_sha256": quality_hash,
                    "grader_kind": "deterministic",
                    "grader_identity_sha256": grader_identity,
                    "rubric_sha256": bindings["rubric_sha256"],
                    "case_id": case,
                    "evaluated_arm": arm,
                    "evaluated_execution_receipt_ids": [execution_receipt_id],
                    "producer_kind": "pinned-deterministic-grader",
                    "producer_path": str(DETERMINISTIC_GRADER_PATH.resolve()),
                    "producer_sha256": grader_identity,
                },
            )
            value = {
                "schema_version": 1,
                "cycle_id": cycle["cycle_id"],
                "candidate_id": cycle["candidate_id"],
                "experiment_scope_sha256": cycle["experiment_scope_sha256"],
                "case_id": case,
                "task_family": family,
                "partition": partition,
                "arm": arm,
                "evidence_phase": evidence_phase,
                "canary_window_id": canary_window_id,
                "bindings": {**bindings, "task_feature_sha256": cycle["task_feature_sha256"]},
                "invocations": [
                    {
                        "invocation_id": f"{case}-{arm}-worker",
                        "planned_call_id": f"{arm}-worker",
                        "role": "worker",
                        "phase": "primary",
                        "attempt_kind": "initial",
                        "context_replay_tokens": 0,
                        "tier": "T2",
                        "dispatcher_receipt_id": execution_receipt_id,
                    }
                ],
                "quality_artifact_path": str(quality_path),
                "quality_artifact_sha256": quality_hash,
                "quality_receipt_id": quality_receipt["receipt_id"],
                "safety": {
                    "authorized_mutation": True,
                    "secret_safe": True,
                    "authority_safe": True,
                    "critical_failure": critical_failure,
                },
                "arm_started_at_ms": 0,
                "accepted_at_ms": wall,
                "created_at_epoch": time.time(),
            }
            path = root / f"{case}-{arm}-artifact.json"
            return path, _simulation_write(path, value)

        first = begin(store, {**base, "cycle_id": "cycle-1", "run_id": "run-1"})
        path, hashed = artifact(first, "case-a", "coding", "development", "control")
        record(store, first["cycle_id"], str(path), hashed)
        first_state = advance(store, first["cycle_id"])["state"]

        second = begin(
            store,
            {**base, "cycle_id": "cycle-2", "run_id": "run-2", "parent_cycle_id": first["cycle_id"]},
        )
        for case, family, arm in (
            ("case-a", "coding", "challenger"),
            ("case-b", "operations", "control"),
            ("case-b", "operations", "challenger"),
        ):
            path, hashed = artifact(second, case, family, "development", arm)
            record(store, second["cycle_id"], str(path), hashed)
        second_state = advance(store, second["cycle_id"])["state"]

        third = begin(
            store,
            {**base, "cycle_id": "cycle-3", "run_id": "run-3", "parent_cycle_id": second["cycle_id"]},
        )
        for arm in ("control", "challenger"):
            path, hashed = artifact(third, "case-holdout", "research", "holdout", arm)
            record(store, third["cycle_id"], str(path), hashed)
        advance(store, third["cycle_id"])
        qualified = advance(store, third["cycle_id"])
        comparison = evaluate_cycle(store, third["cycle_id"])
        promoted = promote_workflow(store, third["cycle_id"])
        candidate_active = active_path.read_bytes() == candidate_bytes
        for arm in ("control", "challenger"):
            path, hashed = artifact(
                third,
                "canary-regression",
                "operations",
                "development",
                arm,
                evidence_phase="canary",
                canary_window_id="window-1",
                critical_failure=arm == "challenger",
            )
            record_canary(store, third["cycle_id"], str(path), hashed)
        rolled_back = monitor_workflow(store, third["cycle_id"])
        exact_rollback = active_path.read_bytes() == active_bytes
        result = {
            "schema_version": 1,
            "cycle_1": first_state,
            "cycle_2": second_state,
            "cycle_3": qualified["state"],
            "quality_safe_efficiency": comparison["reward_dimensions"],
            "promotion": promoted["state"],
            "candidate_bytes_activated": candidate_active,
            "canary": "REGRESSION_DETECTED",
            "rollback": rolled_back["state"],
            "exact_rollback": exact_rollback,
            "restored_sha256": file_sha256(active_path),
            "incumbent_sha256": sha256_bytes(active_bytes),
            "worst_case_budget_tokens": budget["worst_case_aggregate_tokens"],
            "universal_200k_ceiling_present": False,
            "trusted_receipts": len(registry_events),
        }
        MODEL_ROUTER_HOME = prior_model_router_home
        DISPATCH_REGISTRY_PATH = prior_dispatch_registry_path
        return result


def _payload(argument: argparse.Namespace) -> dict[str, Any]:
    if getattr(argument, "json", None):
        return json.loads(Path(argument.json).expanduser().read_text(encoding="utf-8"))
    return json.loads(getattr(argument, "data", None) or "{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=HOME)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    commands.add_parser("recover")
    commands.add_parser("status")
    for name in ("begin", "recommend", "no-call", "budget", "capacity"):
        command = commands.add_parser(name)
        command.add_argument("--json")
        command.add_argument("--data")
    record_command = commands.add_parser("record")
    record_command.add_argument("cycle_id")
    record_command.add_argument("artifact_path")
    record_command.add_argument("artifact_sha256")
    canary_record_command = commands.add_parser("record-canary")
    canary_record_command.add_argument("cycle_id")
    canary_record_command.add_argument("artifact_path")
    canary_record_command.add_argument("artifact_sha256")
    evaluate_command = commands.add_parser("evaluate")
    evaluate_command.add_argument("cycle_id")
    advance_command = commands.add_parser("advance")
    advance_command.add_argument("cycle_id")
    sync_command = commands.add_parser("sync-model")
    sync_command.add_argument("cycle_id")
    promote_command = commands.add_parser("promote-workflow")
    promote_command.add_argument("cycle_id")
    monitor_command = commands.add_parser("monitor-workflow")
    monitor_command.add_argument("cycle_id")
    rollback_command = commands.add_parser("rollback-workflow")
    rollback_command.add_argument("cycle_id")
    rollback_command.add_argument("reason")
    archive_command = commands.add_parser("archive-serendipity")
    archive_command.add_argument("cycle_id")
    replay_command = commands.add_parser("replay-archive")
    replay_command.add_argument("archive_id")
    replay_command.add_argument("--json")
    replay_command.add_argument("--data")
    credit_command = commands.add_parser("credit-stepping-stone")
    credit_command.add_argument("archive_id")
    credit_command.add_argument("validated_cycle_id")
    credit_command.add_argument("causal_manifest_path")
    credit_command.add_argument("causal_manifest_sha256")
    stale_command = commands.add_parser("sweep-stale")
    stale_command.add_argument("--json")
    stale_command.add_argument("--data")
    commands.add_parser("simulate")
    arguments = parser.parse_args()
    store = Store(arguments.home)
    try:
        if arguments.command == "doctor":
            result = doctor(store)
        elif arguments.command == "recover":
            result = store.recover()
        elif arguments.command == "status":
            store.recover()
            result = status(store)
        elif arguments.command == "begin":
            result = begin(store, _payload(arguments))
        elif arguments.command == "record":
            result = record(store, arguments.cycle_id, arguments.artifact_path, arguments.artifact_sha256)
        elif arguments.command == "record-canary":
            result = record_canary(
                store,
                arguments.cycle_id,
                arguments.artifact_path,
                arguments.artifact_sha256,
            )
        elif arguments.command == "evaluate":
            result = evaluate_cycle(store, arguments.cycle_id)
        elif arguments.command == "advance":
            result = advance(store, arguments.cycle_id)
        elif arguments.command == "sync-model":
            result = sync_model_authority(store, arguments.cycle_id)
        elif arguments.command == "promote-workflow":
            result = promote_workflow(store, arguments.cycle_id)
        elif arguments.command == "monitor-workflow":
            result = monitor_workflow(store, arguments.cycle_id)
        elif arguments.command == "rollback-workflow":
            result = rollback_workflow(store, arguments.cycle_id, arguments.reason)
        elif arguments.command == "recommend":
            result = recommend(store, _payload(arguments).get("features", _payload(arguments)))
        elif arguments.command == "no-call":
            result = no_call(store, _payload(arguments).get("reason", "no eligible experiment"))
        elif arguments.command == "budget":
            payload = _payload(arguments)
            result = build_budget_manifest(
                payload["task_manifest_sha256"],
                payload.get("calls", []),
                payload.get("reviewer_allowance", 0),
                payload.get("grader_allowance", 0),
            )
        elif arguments.command == "capacity":
            result = allocate_serendipity(
                store,
                float(
                    _payload(arguments).get(
                        "fraction",
                        load_learning_policy()["serendipity"]["capacity_fraction"],
                    )
                ),
            )
        elif arguments.command == "archive-serendipity":
            result = archive_serendipity(store, arguments.cycle_id)
        elif arguments.command == "replay-archive":
            result = replay_archive_candidate(store, arguments.archive_id, _payload(arguments))
        elif arguments.command == "credit-stepping-stone":
            result = credit_stepping_stone(
                store,
                arguments.archive_id,
                arguments.validated_cycle_id,
                arguments.causal_manifest_path,
                arguments.causal_manifest_sha256,
            )
        elif arguments.command == "sweep-stale":
            result = sweep_stale(store, _payload(arguments))
        else:
            result = simulation()
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") != "DEGRADED" else 2
    except Exception as error:
        print(json.dumps({"status": "ERROR", "error": str(error), "command": arguments.command}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
