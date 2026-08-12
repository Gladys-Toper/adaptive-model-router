#!/usr/bin/env python3
"""Discover, evaluate, promote, and roll back Adaptive Model Router policies.

Claude Code port of the Codex router lab. Claude Code exposes no local model
catalog file, so the selectable set is defined by a curated versioned snapshot
(assets/claude-catalog.json). New models arrive by updating the curated catalog
from official announcements; the first observation of a model is a baseline,
not a release event; and catalog metadata alone never replaces an incumbent.
Automatic routing is restricted to locally available Claude models in this
harness; never substitute another provider.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


SKILL_DIR = Path(__file__).resolve().parent.parent
ASSET_DIR = SKILL_DIR / "assets"
DEFAULT_POLICY_PATH = ASSET_DIR / "default-policy.json"
EVAL_SUITE_PATH = ASSET_DIR / "eval-suite.jsonl"
TEMPLATE_DIR = ASSET_DIR / "custom-agents"
DEFAULT_CATALOG_PATH = ASSET_DIR / "claude-catalog.json"

CLAUDE_HOME = Path(
    os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
).expanduser()
LAB_HOME = Path(
    os.environ.get("ADAPTIVE_MODEL_ROUTER_HOME", CLAUDE_HOME / "adaptive-model-router")
).expanduser()
CATALOG_SOURCE = Path(
    os.environ.get("ADAPTIVE_ROUTER_CATALOG_SOURCE", DEFAULT_CATALOG_PATH)
).expanduser()
AGENTS_DIR = Path(
    os.environ.get("CLAUDE_AGENTS_DIR", CLAUDE_HOME / "agents")
).expanduser()

ACTIVE_POLICY_PATH = LAB_HOME / "active-policy.json"
CATALOG_PATH = LAB_HOME / "catalog.json"
CANDIDATES_PATH = LAB_HOME / "candidates.json"
OBSERVATIONS_PATH = LAB_HOME / "observations.jsonl"
EXPERIMENTS_PATH = LAB_HOME / "experiments.jsonl"
ACTIVATIONS_PATH = LAB_HOME / "activations.jsonl"
CATALOG_HISTORY_PATH = LAB_HOME / "catalog-history.jsonl"
LOCK_PATH = LAB_HOME / "router.lock"
JOURNAL_PATH = LAB_HOME / "activation-journal.json"
CATALOG_REFRESH_JOURNAL_PATH = LAB_HOME / "catalog-refresh-journal.json"
CATALOG_REFRESH_BACKUP_DIR = LAB_HOME / "catalog-refresh-backup"
SNAPSHOTS_DIR = LAB_HOME / "snapshots"

TIER_TEMPLATES = {
    "T1": "fast-operator.md",
    "T2": "standard-worker.md",
    "T3": "high-solver.md",
    "T4": "ultra-planner.md",
}
# Effort ladder: low < medium < high < xhigh < max.
TIER_REQUIRED_EFFORT = {"T1": "low", "T2": "medium", "T3": "high", "T4": "max"}
VALID_TIERS = tuple(TIER_TEMPLATES)
ELIGIBLE_GRADERS = {"tests", "deterministic", "blind-rubric", "user"}
T4_READ_ONLY_DISALLOWED_TOOLS = ("Edit", "Write", "NotebookEdit")

TIER_INDEX = {"T0": 0, "T1": 1, "T2": 2, "T3": 3, "T4": 4}
INDEX_TIER = {value: key for key, value in TIER_INDEX.items()}
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


def isolated_test_context() -> bool:
    test_root_value = os.environ.get("ADAPTIVE_MODEL_ROUTER_TEST_ROOT")
    if not test_root_value:
        return False
    test_root = Path(test_root_value).expanduser().resolve()
    system_temp = Path(tempfile.gettempdir()).resolve()
    isolated_paths = (
        CLAUDE_HOME.resolve(),
        LAB_HOME.resolve(),
        AGENTS_DIR.resolve(),
        CATALOG_SOURCE.resolve(),
    )
    return (
        test_root != system_temp
        and test_root.is_relative_to(system_temp)
        and all(path.is_relative_to(test_root) for path in isolated_paths)
    )


def validate_runtime_layout() -> None:
    if isolated_test_context():
        return
    expected = {
        "lab home": (CLAUDE_HOME / "adaptive-model-router").resolve(),
        "agent directory": (CLAUDE_HOME / "agents").resolve(),
        "model catalog": DEFAULT_CATALOG_PATH.resolve(),
    }
    actual = {
        "lab home": LAB_HOME.resolve(),
        "agent directory": AGENTS_DIR.resolve(),
        "model catalog": CATALOG_SOURCE.resolve(),
    }
    mismatches = [name for name in expected if actual[name] != expected[name]]
    if mismatches:
        raise RouterLabError(
            "unsafe independent path overrides: " + ", ".join(sorted(mismatches))
        )


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def content_id(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RouterLabError(f"refusing symlink destination: {path}")
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RouterLabError(f"refusing symlink destination: {path}")
    line = canonical_json(value) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RouterLabError(f"{path}:{line_number} is not a JSON object")
        records.append(value)
    return records


def observation_set_hash(records: list[dict[str, Any]]) -> str:
    return content_id(
        sorted(
            records,
            key=lambda record: (record.get("run_id", ""), canonical_json(record)),
        )
    )


@contextlib.contextmanager
def lab_lock() -> Iterator[None]:
    LAB_HOME.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_state() -> None:
    validate_runtime_layout()
    LAB_HOME.mkdir(parents=True, exist_ok=True)
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    if not ACTIVE_POLICY_PATH.exists():
        policy = read_json(DEFAULT_POLICY_PATH)
        policy["policy_id"] = policy_id(policy)
        atomic_write_json(ACTIVE_POLICY_PATH, policy)
    else:
        policy = read_json(ACTIVE_POLICY_PATH)
        defaults = read_json(DEFAULT_POLICY_PATH)
        changed = False
        for section in ("constraints", "promotion", "canary"):
            target = policy.setdefault(section, {})
            for key, value in defaults.get(section, {}).items():
                if key not in target:
                    target[key] = value
                    changed = True
        if changed:
            promoted_canary_active = False
            if CANDIDATES_PATH.exists():
                candidate_state = read_json(CANDIDATES_PATH)
                promoted_canary_active = any(
                    candidate.get("status") == "promoted"
                    for candidate in candidate_state.get("items", [])
                )
            if promoted_canary_active:
                return
            policy["revision"] = int(policy.get("revision", 0)) + 1
            policy["updated_at"] = utc_now()
            policy["source"] = "adaptive-router-policy-schema-upgrade"
            policy["policy_id"] = policy_id(policy)
            atomic_write_json(ACTIVE_POLICY_PATH, policy)
    if not CANDIDATES_PATH.exists():
        atomic_write_json(CANDIDATES_PATH, {"schema_version": 1, "items": []})


def policy_id(policy: dict[str, Any]) -> str:
    material = copy.deepcopy(policy)
    material.pop("policy_id", None)
    material.pop("updated_at", None)
    return content_id(material)


def load_policy() -> dict[str, Any]:
    ensure_state()
    policy = read_json(ACTIVE_POLICY_PATH)
    if policy.get("schema_version") != 1:
        raise RouterLabError("unsupported active policy schema")
    expected = policy_id(policy)
    if policy.get("policy_id") != expected:
        raise RouterLabError("active policy hash does not match its content")
    return policy


def load_candidates() -> dict[str, Any]:
    ensure_state()
    value = read_json(CANDIDATES_PATH)
    if value.get("schema_version") != 1 or not isinstance(value.get("items"), list):
        raise RouterLabError("invalid candidate state")
    return value


def remove_candidate_profile(candidate: dict[str, Any]) -> None:
    profile_value = candidate.get("candidate_profile_file")
    if not profile_value:
        return
    profile_path = AGENTS_DIR / profile_value
    if (
        profile_path.exists()
        and profile_path.is_file()
        and not profile_path.is_symlink()
        and sha256_bytes(profile_path.read_bytes())
        == candidate.get("candidate_profile_sha256")
    ):
        profile_path.unlink()


def load_eval_suite() -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for record in read_jsonl(EVAL_SUITE_PATH):
        case_id = record.get("case_id")
        if not isinstance(case_id, str) or case_id in cases:
            raise RouterLabError(
                "evaluation suite contains an invalid or duplicate case_id"
            )
        cases[case_id] = record
    return cases


def instruction_hash(model: dict[str, Any]) -> str:
    material = {
        "instructions": model.get("instructions"),
    }
    return content_id(material)


def capability_ids(value: Any) -> list[str]:
    identifiers: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            identifiers.append(item)
        elif isinstance(item, dict):
            identifier = item.get("id") or item.get("type") or item.get("slug")
            if isinstance(identifier, str):
                identifiers.append(identifier)
    return sorted(set(identifiers))


def normalize_model(model: dict[str, Any]) -> dict[str, Any]:
    slug = model.get("slug")
    if not isinstance(slug, str) or not slug:
        raise RouterLabError("catalog model is missing a slug")
    efforts = []
    for item in model.get("supported_efforts", []):
        effort = item.get("effort") if isinstance(item, dict) else item
        if isinstance(effort, str):
            efforts.append(effort)
    normalized = {
        "slug": slug,
        "provider": model.get("provider", "anthropic"),
        "model_id": model.get("model_id"),
        "visible": bool(model.get("visible")),
        "selectable": bool(model.get("selectable")),
        "supported_efforts": sorted(set(efforts)),
        "input_modalities": sorted(model.get("input_modalities") or []),
        "context_window": model.get("context_window"),
        "capabilities": capability_ids(model.get("capabilities")),
        "instruction_hash": instruction_hash(model),
    }
    normalized["revision_hash"] = content_id(normalized)
    return normalized


def read_catalog_snapshot() -> dict[str, Any]:
    validate_runtime_layout()
    if CATALOG_SOURCE.is_symlink() or not CATALOG_SOURCE.is_file():
        raise RouterLabError(f"model catalog is missing or unsafe: {CATALOG_SOURCE}")
    try:
        raw = CATALOG_SOURCE.read_bytes()
    except FileNotFoundError as error:
        raise RouterLabError(f"model catalog not found: {CATALOG_SOURCE}") from error
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise RouterLabError("model catalog has an incompatible schema")
    models: dict[str, dict[str, Any]] = {}
    display: dict[str, dict[str, Any]] = {}
    for model in payload["models"]:
        if not isinstance(model, dict):
            raise RouterLabError("model catalog entry is not an object")
        normalized = normalize_model(model)
        slug = normalized["slug"]
        if slug in models:
            raise RouterLabError(f"duplicate model slug in catalog: {slug}")
        models[slug] = normalized
        display[slug] = {
            "display_name": model.get("display_name"),
            "description": model.get("description"),
        }
    semantic_models = [models[key] for key in sorted(models)]
    return {
        "schema_version": 1,
        "observed_at": utc_now(),
        "source_path": str(CATALOG_SOURCE),
        "source_sha256": sha256_bytes(raw),
        "captured_at": payload.get("captured_at"),
        "source_kind": payload.get("source"),
        "semantic_hash": content_id(semantic_models),
        "models": models,
        "display": display,
    }


def catalog_changes(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> dict[str, list[str]]:
    if previous is None:
        return {"added": [], "removed": [], "changed": [], "restored": []}
    old_models = previous.get("models", {})
    new_models = current.get("models", {})
    added = sorted(set(new_models) - set(old_models))
    removed = sorted(set(old_models) - set(new_models))
    changed = sorted(
        slug
        for slug in set(old_models) & set(new_models)
        if old_models[slug].get("revision_hash")
        != new_models[slug].get("revision_hash")
    )
    return {"added": added, "removed": removed, "changed": changed, "restored": []}


SCHEMA_COMPARABLE_MODEL_FIELDS = (
    "slug",
    "provider",
    "model_id",
    "visible",
    "selectable",
    "supported_efforts",
    "input_modalities",
    "context_window",
    "capabilities",
    "instruction_hash",
)


def catalog_changes_across_schema(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, list[str]]:
    old_models = previous.get("models", {})
    new_models = current.get("models", {})
    added = sorted(set(new_models) - set(old_models))
    removed = sorted(set(old_models) - set(new_models))
    changed: list[str] = []
    for slug in sorted(set(old_models) & set(new_models)):
        old_model = old_models[slug]
        new_model = new_models[slug]
        model_changed = False
        for field in SCHEMA_COMPARABLE_MODEL_FIELDS:
            if field not in old_model:
                continue
            if field in {"capabilities"}:
                values_differ = capability_ids(old_model.get(field)) != capability_ids(
                    new_model.get(field)
                )
            else:
                values_differ = old_model.get(field) != new_model.get(field)
            if values_differ:
                model_changed = True
                break
        if model_changed:
            changed.append(slug)
    return {"added": added, "removed": removed, "changed": changed, "restored": []}


def forbidden_model(policy: dict[str, Any], slug: str) -> bool:
    patterns = policy.get("constraints", {}).get("forbidden_model_patterns", [])
    lowered = slug.lower()
    return any(str(pattern).lower() in lowered for pattern in patterns)


def tier_eligibility(
    policy: dict[str, Any],
    model: dict[str, Any],
    tier: str,
    required_effort: str | None = None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    slug = model["slug"]
    effort = required_effort or TIER_REQUIRED_EFFORT[tier]
    allowed_provider = policy.get("constraints", {}).get(
        "allowed_provider", "anthropic"
    )
    if model.get("provider") != allowed_provider:
        reasons.append("model provider is not allowed by policy")
    if forbidden_model(policy, slug):
        reasons.append("forbidden provider or model pattern")
    if not (model.get("visible") and model.get("selectable")):
        reasons.append("model is not visible and selectable in the curated catalog")
    if "text" not in model.get("input_modalities", []):
        reasons.append("text input is unsupported")
    if tier in {"T2", "T3", "T4"} and "image" not in model.get("input_modalities", []):
        reasons.append("broad coding role requires image input support")
    if effort not in model.get("supported_efforts", []):
        reasons.append(f"required reasoning effort {effort} is unsupported")
    if "tool-use" not in model.get("capabilities", []):
        reasons.append("tool use support is unavailable")
    if tier in {"T2", "T3"} and "code-edit" not in model.get("capabilities", []):
        reasons.append("code edit support is unavailable")
    if tier == "T4" and "parallel-tool-calls" not in model.get("capabilities", []):
        reasons.append("parallel tool support is unavailable")
    return (not reasons, reasons)


FRONTMATTER_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
FRONTMATTER_VALUE_PATTERN = re.compile(r"\S(?:.*\S)?")


def parse_profile_markdown(text: str) -> tuple[dict[str, str], str]:
    """Parse the strict deterministic frontmatter rendered by this lab.

    Only single-line `key: value` pairs between exact `---` fences are
    accepted; anything else is a hard error so byte-hashes stay meaningful.
    """
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise RouterLabError("profile frontmatter must start with ---")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise RouterLabError("profile frontmatter is unterminated") from error
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(": ")
        if (
            not separator
            or not FRONTMATTER_KEY_PATTERN.fullmatch(key)
            or not FRONTMATTER_VALUE_PATTERN.fullmatch(value)
        ):
            raise RouterLabError(f"invalid profile frontmatter line: {line!r}")
        if key in fields:
            raise RouterLabError(f"duplicate profile frontmatter field: {key}")
        fields[key] = value
    body = "\n".join(lines[end + 1 :])
    return fields, body


def render_profile_markdown(fields: dict[str, str], body: str) -> str:
    rendered = ["---"]
    for key, value in fields.items():
        if not FRONTMATTER_KEY_PATTERN.fullmatch(key):
            raise RouterLabError(f"invalid profile frontmatter key: {key!r}")
        if "\n" in value or not FRONTMATTER_VALUE_PATTERN.fullmatch(value):
            raise RouterLabError(f"invalid profile frontmatter value for {key}")
        rendered.append(f"{key}: {value}")
    rendered.append("---")
    text = "\n".join(rendered) + "\n" + body
    parsed_fields, parsed_body = parse_profile_markdown(text)
    if parsed_fields != fields or parsed_body != body:
        raise RouterLabError("profile frontmatter failed round-trip validation")
    return text


def profile_disallowed_tools(fields: dict[str, str]) -> set[str]:
    value = fields.get("disallowedTools", "")
    return {item.strip() for item in value.split(",") if item.strip()}


def profile_is_read_only(fields: dict[str, str]) -> bool:
    return set(T4_READ_ONLY_DISALLOWED_TOOLS) <= profile_disallowed_tools(fields)


def render_profile(
    tier: str,
    model: str,
    effort: str,
    *,
    agent_name: str | None = None,
    description_suffix: str | None = None,
) -> str:
    template_path = TEMPLATE_DIR / TIER_TEMPLATES[tier]
    fields, body = parse_profile_markdown(
        template_path.read_text(encoding="utf-8")
    )
    for required in ("name", "description", "model", "effort"):
        if required not in fields:
            raise RouterLabError(
                f"profile template lacks {required}: {template_path}"
            )
    if agent_name:
        fields["name"] = agent_name
    if description_suffix:
        fields["description"] = f"{fields['description']} {description_suffix}"
    fields["model"] = model
    fields["effort"] = effort
    text = render_profile_markdown(fields, body)
    parsed, _ = parse_profile_markdown(text)
    if parsed.get("model") != model or parsed.get("effort") != effort:
        raise RouterLabError("rendered profile failed model or effort validation")
    if tier == "T4" and not profile_is_read_only(parsed):
        raise RouterLabError("T4 profile must remain read-only")
    return text


def rendered_active_bundle(policy: dict[str, Any]) -> dict[str, str]:
    bundle: dict[str, str] = {}
    for tier, template_name in TIER_TEMPLATES.items():
        assignment = policy["tiers"][tier]
        bundle[template_name] = render_profile(
            tier, assignment["model"], assignment["effort"]
        )
    return bundle


def bundle_hashes(bundle: dict[str, str]) -> dict[str, str]:
    return {
        name: sha256_bytes(text.encode("utf-8"))
        for name, text in sorted(bundle.items())
    }


def installed_bundle_hashes() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in TIER_TEMPLATES.values():
        path = AGENTS_DIR / name
        result[name] = (
            sha256_bytes(path.read_bytes())
            if path.exists() and path.is_file()
            else None
        )
    return result


def assert_installed_matches(policy: dict[str, Any]) -> None:
    expected = bundle_hashes(rendered_active_bundle(policy))
    actual = installed_bundle_hashes()
    if actual != expected:
        raise RouterLabError(
            "active custom-agent profiles have manual drift; run doctor and reconcile before activation"
        )


def snapshot_current(policy: dict[str, Any]) -> Path:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    captured: dict[str, bytes] = {}
    files: dict[str, str] = {}
    for name in TIER_TEMPLATES.values():
        source = AGENTS_DIR / name
        if not source.exists() or source.is_symlink():
            raise RouterLabError(
                f"cannot snapshot missing or symlinked profile: {source}"
            )
        data = source.read_bytes()
        captured[name] = data
        files[name] = sha256_bytes(data)
    policy_text = json.dumps(policy, indent=2, sort_keys=True) + "\n"
    snapshot = SNAPSHOTS_DIR / f"{timestamp}-{policy['policy_id'][:12]}"
    temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=SNAPSHOTS_DIR))
    try:
        atomic_write_text(temporary / "active-policy.json", policy_text)
        for name, data in captured.items():
            atomic_write_text(temporary / name, data.decode("utf-8"))
        manifest = {
            "schema_version": 2,
            "created_at": utc_now(),
            "policy_id": policy["policy_id"],
            "policy_sha256": sha256_bytes(policy_text.encode("utf-8")),
            "files": files,
        }
        atomic_write_json(temporary / "manifest.json", manifest)
        os.replace(temporary, snapshot)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        if (
            CATALOG_REFRESH_BACKUP_DIR.exists()
            and not CATALOG_REFRESH_JOURNAL_PATH.exists()
        ):
            shutil.rmtree(CATALOG_REFRESH_BACKUP_DIR, ignore_errors=True)
        raise
    return snapshot


def validate_snapshot(
    snapshot: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    if (
        not snapshot.is_dir()
        or snapshot.is_symlink()
        or snapshot.resolve().parent != SNAPSHOTS_DIR.resolve()
    ):
        raise RouterLabError(f"snapshot path is missing or unsafe: {snapshot}")
    expected_names = {
        "active-policy.json",
        "manifest.json",
        *TIER_TEMPLATES.values(),
    }
    actual_names = {path.name for path in snapshot.iterdir()}
    if actual_names != expected_names:
        raise RouterLabError("snapshot file set is incomplete or unexpected")
    policy_path = snapshot / "active-policy.json"
    manifest_path = snapshot / "manifest.json"
    policy_bytes = policy_path.read_bytes()
    policy = json.loads(policy_bytes)
    manifest = read_json(manifest_path)
    if policy.get("schema_version") != 1:
        raise RouterLabError("snapshot policy schema is unsupported")
    if policy.get("policy_id") != policy_id(policy):
        raise RouterLabError("snapshot policy hash does not match its content")
    if manifest.get("policy_id") != policy.get("policy_id"):
        raise RouterLabError("snapshot manifest policy does not match")
    if manifest.get("schema_version") == 2:
        if manifest.get("policy_sha256") != sha256_bytes(policy_bytes):
            raise RouterLabError("snapshot policy byte hash does not match")
    elif manifest.get("schema_version") != 1:
        raise RouterLabError("snapshot manifest schema is unsupported")
    if set(manifest.get("files", {})) != set(TIER_TEMPLATES.values()):
        raise RouterLabError("snapshot manifest profile set is invalid")
    profile_data: dict[str, bytes] = {}
    for name, expected_hash in manifest["files"].items():
        path = snapshot / name
        if path.is_symlink() or not path.is_file():
            raise RouterLabError(f"snapshot profile is missing or unsafe: {name}")
        data = path.read_bytes()
        if sha256_bytes(data) != expected_hash:
            raise RouterLabError(f"snapshot profile hash mismatch: {name}")
        profile_data[name] = data
    for tier, name in TIER_TEMPLATES.items():
        fields, _ = parse_profile_markdown(profile_data[name].decode("utf-8"))
        assignment = policy["tiers"][tier]
        if (
            fields.get("model") != assignment["model"]
            or fields.get("effort") != assignment["effort"]
        ):
            raise RouterLabError(
                "snapshot profiles do not implement the snapshot policy"
            )
        if tier == "T4" and not profile_is_read_only(fields):
            raise RouterLabError("snapshot T4 profile is not read-only")
    return policy, manifest, profile_data


def lab_owned_agent_files() -> dict[str, Path]:
    if not AGENTS_DIR.exists():
        return {}
    return {
        path.name: path
        for path in AGENTS_DIR.glob("router-*.md")
        if path.is_file() and not path.is_symlink()
    }


def begin_catalog_refresh(policy: dict[str, Any]) -> None:
    assert_installed_matches(policy)
    if CATALOG_REFRESH_JOURNAL_PATH.exists():
        raise RouterLabError("an interrupted catalog refresh must be recovered")
    if CATALOG_REFRESH_BACKUP_DIR.exists():
        shutil.rmtree(CATALOG_REFRESH_BACKUP_DIR)
    active_snapshot = snapshot_current(policy)
    temporary = Path(tempfile.mkdtemp(prefix=".catalog-refresh-", dir=LAB_HOME))
    try:
        candidates_bytes = CANDIDATES_PATH.read_bytes()
        atomic_write_text(
            temporary / "candidates.json", candidates_bytes.decode("utf-8")
        )
        catalog_present = CATALOG_PATH.exists()
        catalog_hash = None
        if catalog_present:
            catalog_bytes = CATALOG_PATH.read_bytes()
            catalog_hash = sha256_bytes(catalog_bytes)
            atomic_write_text(temporary / "catalog.json", catalog_bytes.decode("utf-8"))
        owned_files: dict[str, str] = {}
        for name, path in lab_owned_agent_files().items():
            data = path.read_bytes()
            owned_files[name] = sha256_bytes(data)
            atomic_write_text(temporary / name, data.decode("utf-8"))
        manifest = {
            "schema_version": 1,
            "active_snapshot": str(active_snapshot),
            "policy_id": policy["policy_id"],
            "candidates_sha256": sha256_bytes(candidates_bytes),
            "catalog_present": catalog_present,
            "catalog_sha256": catalog_hash,
            "owned_agent_files": owned_files,
        }
        atomic_write_json(temporary / "manifest.json", manifest)
        os.replace(temporary, CATALOG_REFRESH_BACKUP_DIR)
        atomic_write_json(
            CATALOG_REFRESH_JOURNAL_PATH,
            {
                "schema_version": 1,
                "started_at": utc_now(),
                "backup_manifest_sha256": sha256_bytes(
                    (CATALOG_REFRESH_BACKUP_DIR / "manifest.json").read_bytes()
                ),
                "active_snapshot": str(active_snapshot),
            },
        )
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def finish_catalog_refresh() -> None:
    CATALOG_REFRESH_JOURNAL_PATH.unlink()
    shutil.rmtree(CATALOG_REFRESH_BACKUP_DIR)


def recover_catalog_refresh() -> dict[str, Any]:
    if not CATALOG_REFRESH_JOURNAL_PATH.exists():
        return {"status": "CLEAN"}
    journal = read_json(CATALOG_REFRESH_JOURNAL_PATH)
    manifest_path = CATALOG_REFRESH_BACKUP_DIR / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise RouterLabError("catalog refresh backup manifest is missing or unsafe")
    if sha256_bytes(manifest_path.read_bytes()) != journal.get(
        "backup_manifest_sha256"
    ):
        raise RouterLabError("catalog refresh backup manifest changed")
    manifest = read_json(manifest_path)
    snapshot = Path(manifest["active_snapshot"])
    restored = restore_snapshot(
        snapshot,
        reason="catalog refresh recovery",
        allow_unavailable=True,
    )
    candidates_path = CATALOG_REFRESH_BACKUP_DIR / "candidates.json"
    candidates_bytes = candidates_path.read_bytes()
    if sha256_bytes(candidates_bytes) != manifest.get("candidates_sha256"):
        raise RouterLabError("catalog refresh candidate backup changed")
    atomic_write_text(CANDIDATES_PATH, candidates_bytes.decode("utf-8"))
    if manifest.get("catalog_present"):
        catalog_path = CATALOG_REFRESH_BACKUP_DIR / "catalog.json"
        catalog_bytes = catalog_path.read_bytes()
        if sha256_bytes(catalog_bytes) != manifest.get("catalog_sha256"):
            raise RouterLabError("catalog refresh baseline backup changed")
        atomic_write_text(CATALOG_PATH, catalog_bytes.decode("utf-8"))
    else:
        CATALOG_PATH.unlink(missing_ok=True)
    initial_owned = manifest.get("owned_agent_files", {})
    for name, path in lab_owned_agent_files().items():
        if name not in initial_owned:
            path.unlink()
    for name, expected_hash in initial_owned.items():
        backup_path = CATALOG_REFRESH_BACKUP_DIR / name
        data = backup_path.read_bytes()
        if sha256_bytes(data) != expected_hash:
            raise RouterLabError(f"catalog refresh agent backup changed: {name}")
        atomic_write_text(AGENTS_DIR / name, data.decode("utf-8"))
    finish_catalog_refresh()
    return {"status": "CATALOG_REFRESH_RECOVERED", "policy_id": restored["policy_id"]}


def restore_snapshot(
    snapshot: Path,
    *,
    reason: str,
    allow_unavailable: bool = False,
    retain_journal: bool = False,
    transaction_candidate_id: str | None = None,
    target_candidate_status: str | None = None,
) -> dict[str, Any]:
    policy, manifest, profile_data = validate_snapshot(snapshot)
    if not allow_unavailable:
        if not CATALOG_PATH.exists():
            raise RouterLabError(
                "rollback availability cannot be checked without a catalog"
            )
        catalog = read_json(CATALOG_PATH)
        for tier in VALID_TIERS:
            assignment = policy["tiers"][tier]
            model = catalog.get("models", {}).get(assignment["model"])
            eligible, _ = (
                tier_eligibility(policy, model, tier, assignment["effort"])
                if model is not None
                else (False, ["model missing"])
            )
            if not eligible:
                raise RouterLabError(
                    f"rollback target is unavailable for {tier}: {assignment['model']} / {assignment['effort']}"
                )
    journal = {
        "schema_version": 1,
        "action": "rollback",
        "snapshot": str(snapshot),
        "started_at": utc_now(),
        "reason": reason,
        "target_policy_id": policy["policy_id"],
        "candidate_id": transaction_candidate_id,
        "target_candidate_status": target_candidate_status,
    }
    atomic_write_json(JOURNAL_PATH, journal)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in profile_data.items():
        destination = AGENTS_DIR / name
        if destination.is_symlink():
            raise RouterLabError(f"refusing symlink destination: {destination}")
        atomic_write_text(destination, data.decode("utf-8"))
    atomic_write_json(ACTIVE_POLICY_PATH, policy)
    if load_policy().get("policy_id") != policy["policy_id"]:
        raise RouterLabError("snapshot policy readback failed")
    if installed_bundle_hashes() != manifest["files"]:
        raise RouterLabError("snapshot profile readback failed")
    append_jsonl(
        ACTIVATIONS_PATH,
        {
            "action": "rollback",
            "activated_at": utc_now(),
            "policy_id": policy["policy_id"],
            "snapshot": str(snapshot),
            "reason": reason,
        },
    )
    if not retain_journal:
        JOURNAL_PATH.unlink(missing_ok=True)
    return policy


def prune_snapshots(policy: dict[str, Any], *, pinned: set[Path] | None = None) -> None:
    retention = max(1, int(policy.get("promotion", {}).get("snapshot_retention", 5)))
    snapshots = sorted(
        path
        for path in SNAPSHOTS_DIR.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    retained = set(snapshots[-retention:])
    retained.update(pinned or set())
    if CANDIDATES_PATH.exists():
        for candidate in load_candidates()["items"]:
            if candidate.get("status") != "promoted":
                continue
            snapshot_value = candidate.get("promotion_snapshot")
            if snapshot_value:
                retained.add(Path(snapshot_value))
    if CATALOG_REFRESH_JOURNAL_PATH.exists():
        refresh_journal = read_json(CATALOG_REFRESH_JOURNAL_PATH)
        snapshot_value = refresh_journal.get("active_snapshot")
        if snapshot_value:
            retained.add(Path(snapshot_value))
    for old in snapshots:
        if old not in retained:
            shutil.rmtree(old)


def activate_policy(
    new_policy: dict[str, Any],
    *,
    reason: str,
    retain_journal: bool = False,
    transaction_candidate_id: str | None = None,
) -> Path:
    current = load_policy()
    if new_policy.get("policy_id") != policy_id(new_policy):
        raise RouterLabError("target policy hash does not match its content")
    if CANDIDATES_PATH.exists() and any(
        candidate.get("status") == "promoted"
        for candidate in read_json(CANDIDATES_PATH).get("items", [])
    ):
        raise RouterLabError(
            "policy activation is blocked while a promoted candidate is in canary validation"
        )
    assert_installed_matches(current)
    snapshot = snapshot_current(current)
    new_bundle = rendered_active_bundle(new_policy)
    journal = {
        "schema_version": 1,
        "action": "activate",
        "snapshot": str(snapshot),
        "target_policy_id": new_policy["policy_id"],
        "started_at": utc_now(),
        "reason": reason,
        "candidate_id": transaction_candidate_id,
    }
    atomic_write_json(JOURNAL_PATH, journal)
    try:
        AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        for name, text in new_bundle.items():
            atomic_write_text(AGENTS_DIR / name, text)
        if installed_bundle_hashes() != bundle_hashes(new_bundle):
            raise RouterLabError("profile readback failed after activation")
        atomic_write_json(ACTIVE_POLICY_PATH, new_policy)
        append_jsonl(
            ACTIVATIONS_PATH,
            {
                "action": "activate",
                "activated_at": utc_now(),
                "policy_id": new_policy["policy_id"],
                "previous_policy_id": current["policy_id"],
                "snapshot": str(snapshot),
                "reason": reason,
                "files": bundle_hashes(new_bundle),
            },
        )
        if not retain_journal:
            JOURNAL_PATH.unlink(missing_ok=True)
        prune_snapshots(new_policy, pinned={snapshot})
        return snapshot
    except Exception:
        restore_snapshot(
            snapshot, reason="failed activation recovery", allow_unavailable=True
        )
        raise


def sync_agents(force: bool) -> None:
    policy = load_policy()
    expected = rendered_active_bundle(policy)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, text in expected.items():
        destination = AGENTS_DIR / name
        if destination.is_symlink():
            raise RouterLabError(f"refusing symlink destination: {destination}")
        if (
            destination.exists()
            and destination.read_text(encoding="utf-8") != text
            and not force
        ):
            raise RouterLabError(
                f"profile differs: {destination}; use --force only after review"
            )
        atomic_write_text(destination, text)


def configure_attestation(verifier_value: str, expected_hash: str) -> dict[str, Any]:
    verifier = Path(verifier_value).expanduser()
    if not verifier.is_absolute() or not verifier.is_file() or verifier.is_symlink():
        raise RouterLabError("attestation verifier must be an absolute regular file")
    actual_hash = sha256_bytes(verifier.read_bytes())
    if actual_hash != expected_hash:
        raise RouterLabError("attestation verifier hash does not match --sha256")
    policy = load_policy()
    revised = copy.deepcopy(policy)
    revised["constraints"]["attestation_verifier_path"] = str(verifier)
    revised["constraints"]["attestation_verifier_sha256"] = actual_hash
    revised["revision"] = int(revised.get("revision", 0)) + 1
    revised["updated_at"] = utc_now()
    revised["source"] = "trusted-attestation-verifier-configured"
    revised["policy_id"] = policy_id(revised)
    activate_policy(revised, reason="configure trusted attestation verifier")
    candidates = load_candidates()
    for candidate in candidates["items"]:
        if candidate.get("status") in {
            "staged",
            "collecting",
            "blocked_model_enforcement",
            "qualified",
        }:
            candidate["status"] = "stale"
            candidate["stale_reason"] = "attestation policy changed"
    atomic_write_json(CANDIDATES_PATH, candidates)
    return {
        "status": "ATTESTATION_CONFIGURED",
        "policy_id": revised["policy_id"],
        "verifier": str(verifier),
        "sha256": actual_hash,
    }


def attestation_verifier_ready(policy: dict[str, Any]) -> bool:
    constraints = policy.get("constraints", {})
    verifier_value = constraints.get("attestation_verifier_path")
    verifier_hash = constraints.get("attestation_verifier_sha256")
    if not verifier_value or not verifier_hash:
        return False
    verifier = Path(verifier_value).expanduser()
    try:
        return bool(
            verifier.is_absolute()
            and verifier.is_file()
            and not verifier.is_symlink()
            and sha256_bytes(verifier.read_bytes()) == verifier_hash
        )
    except OSError:
        return False


def stage_candidate_internal(
    candidates: dict[str, Any],
    catalog: dict[str, Any],
    policy: dict[str, Any],
    model_slug: str,
    tier: str,
    *,
    trigger: str,
    restart_of: str | None = None,
    restart_sequence: int | None = None,
) -> dict[str, Any] | None:
    if tier not in VALID_TIERS:
        raise RouterLabError(f"invalid tier: {tier}")
    model = catalog.get("models", {}).get(model_slug)
    if model is None:
        raise RouterLabError(f"model is not in the local catalog: {model_slug}")
    eligible, reasons = tier_eligibility(policy, model, tier)
    if not eligible:
        raise RouterLabError(
            f"{model_slug} is ineligible for {tier}: {'; '.join(reasons)}"
        )
    incumbent = policy["tiers"][tier]
    effort = TIER_REQUIRED_EFFORT[tier]
    if incumbent["model"] == model_slug and incumbent["effort"] == effort:
        return None
    suite_hash = sha256_bytes(EVAL_SUITE_PATH.read_bytes())
    role_template_hash = sha256_bytes(
        (TEMPLATE_DIR / TIER_TEMPLATES[tier]).read_bytes()
    )
    material = {
        "tier": tier,
        "candidate_model": model_slug,
        "candidate_effort": effort,
        "candidate_revision_hash": model["revision_hash"],
        "incumbent_model": incumbent["model"],
        "incumbent_effort": incumbent["effort"],
        "incumbent_policy_id": policy["policy_id"],
        "catalog_semantic_hash": catalog["semantic_hash"],
        "eval_suite_hash": suite_hash,
        "role_template_sha256": role_template_hash,
        "promotion_rules_hash": content_id(policy["promotion"]),
    }
    if restart_of is not None:
        material["restart_of"] = restart_of
        material["restart_sequence"] = restart_sequence
    candidate_id = content_id(material)
    for item in candidates["items"]:
        if item["candidate_id"] == candidate_id:
            return item
    agent_name = f"router-candidate-{tier.lower()}-{candidate_id[:8]}"
    profile_name = f"router-candidate-{tier.lower()}-{candidate_id[:8]}.md"
    incumbent_profile = AGENTS_DIR / incumbent["profile_file"]
    if not incumbent_profile.exists() or incumbent_profile.is_symlink():
        raise RouterLabError(
            f"incumbent profile is missing or unsafe: {incumbent_profile}"
        )
    profile = render_profile(
        tier,
        model_slug,
        effort,
        agent_name=agent_name,
        description_suffix="Evaluation challenger; results count only with verified runtime model metadata.",
    )
    destination = AGENTS_DIR / profile_name
    if destination.exists() and destination.read_text(encoding="utf-8") != profile:
        raise RouterLabError(f"candidate profile collision: {destination}")
    atomic_write_text(destination, profile)
    item = {
        "candidate_id": candidate_id,
        "status": "staged",
        "staged_at": utc_now(),
        "trigger": trigger,
        **material,
        "candidate_agent": agent_name,
        "candidate_profile_file": profile_name,
        "candidate_profile_sha256": sha256_bytes(profile.encode("utf-8")),
        "incumbent_agent": incumbent["agent"],
        "incumbent_profile_file": incumbent["profile_file"],
        "incumbent_profile_sha256": sha256_bytes(incumbent_profile.read_bytes()),
        "required_suite_case_ids": sorted(
            case_id
            for case_id, case in load_eval_suite().items()
            if case.get("tier") == tier
        ),
    }
    candidates["items"].append(item)
    return item


def repair_unavailable_assignments(
    policy: dict[str, Any],
    catalog: dict[str, Any],
    *,
    force_fallback_tiers: set[str] | None = None,
    disallowed_fallback_models: set[str] | None = None,
) -> list[dict[str, str]]:
    force_fallback_tiers = force_fallback_tiers or set()
    disallowed_fallback_models = disallowed_fallback_models or set()
    changes: list[dict[str, str]] = []
    revised = copy.deepcopy(policy)
    for tier in VALID_TIERS:
        assignment = revised["tiers"][tier]
        model = catalog.get("models", {}).get(assignment["model"])
        active_eligible, _ = (
            tier_eligibility(revised, model, tier, assignment["effort"])
            if model is not None
            else (False, ["model missing"])
        )
        if active_eligible and tier not in force_fallback_tiers:
            continue
        replacement = None
        for fallback in assignment.get("fallbacks", []):
            if fallback.get("model") == assignment.get("model"):
                continue
            if fallback.get("model") in disallowed_fallback_models:
                continue
            candidate = catalog.get("models", {}).get(fallback["model"])
            if not candidate:
                continue
            eligible, _ = tier_eligibility(revised, candidate, tier, fallback["effort"])
            if eligible and fallback["effort"] in candidate.get(
                "supported_efforts", []
            ):
                replacement = fallback
                break
        if replacement is None:
            raise RouterLabError(f"no available policy fallback for {tier}")
        changes.append(
            {
                "tier": tier,
                "from": f"{assignment['model']}/{assignment['effort']}",
                "to": f"{replacement['model']}/{replacement['effort']}",
                "reason": (
                    "active-model-revision-changed"
                    if tier in force_fallback_tiers
                    else "active-assignment-ineligible"
                ),
            }
        )
        assignment["model"] = replacement["model"]
        assignment["effort"] = replacement["effort"]
    if changes:
        revised["revision"] = int(revised.get("revision", 0)) + 1
        revised["updated_at"] = utc_now()
        revised["source"] = "automatic-availability-failover"
        revised["policy_id"] = policy_id(revised)
        activate_policy(revised, reason="active model unavailable")
    return changes


def refresh_catalog(stage_new: bool) -> dict[str, Any]:
    ensure_state()
    current = read_catalog_snapshot()
    previous = read_json(CATALOG_PATH) if CATALOG_PATH.exists() else None
    schema_upgrade = bool(
        previous is not None
        and previous.get("schema_version") != current.get("schema_version")
    )
    changes = (
        catalog_changes_across_schema(previous, current)
        if schema_upgrade and previous is not None
        else catalog_changes(previous, current)
    )
    changed = (
        previous is None or previous.get("semantic_hash") != current["semantic_hash"]
    )
    policy = load_policy()
    active_needs_repair = any(
        not tier_eligibility(
            policy,
            current["models"].get(policy["tiers"][tier]["model"]),
            tier,
            policy["tiers"][tier]["effort"],
        )[0]
        if current["models"].get(policy["tiers"][tier]["model"]) is not None
        else True
        for tier in VALID_TIERS
    )
    if previous is not None and not changed and not active_needs_repair:
        return {
            "status": "NO_CHANGE",
            "semantic_hash": current["semantic_hash"],
            "changes": changes,
            "failovers": [],
            "staged_candidates": [],
            "captured_at": current.get("captured_at"),
            "source": current.get("source_kind"),
        }
    staged: list[str] = []
    failovers: list[dict[str, str]] = []
    begin_catalog_refresh(policy)
    try:
        atomic_write_json(CATALOG_PATH, current)
        candidates = load_candidates()
        if schema_upgrade:
            changed_candidates = False
            for candidate in candidates["items"]:
                if candidate.get("status") in {
                    "staged",
                    "collecting",
                    "blocked_model_enforcement",
                    "qualified",
                }:
                    candidate["status"] = "stale"
                    candidate["stale_reason"] = "catalog hash schema changed"
                    remove_candidate_profile(candidate)
                    changed_candidates = True
            if changed_candidates:
                atomic_write_json(CANDIDATES_PATH, candidates)
        promoted = next(
            (item for item in candidates["items"] if item.get("status") == "promoted"),
            None,
        )
        if promoted:
            active_model_slugs = {
                policy["tiers"][tier]["model"] for tier in VALID_TIERS
            }
            policy_requires_change = active_needs_repair or bool(
                active_model_slugs & set(changes["changed"] + changes["removed"])
            )
            if policy_requires_change:
                rollback_promoted_candidate(
                    candidates,
                    promoted,
                    reason="catalog changed during canary validation",
                )
                policy = load_policy()
        changed_active_tiers = {
            tier
            for tier in VALID_TIERS
            if policy["tiers"][tier]["model"] in changes["changed"]
        }
        failovers = repair_unavailable_assignments(
            policy,
            current,
            force_fallback_tiers=changed_active_tiers,
            disallowed_fallback_models=set(
                changes["added"] + changes["changed"] + changes["removed"]
            ),
        )
        policy = load_policy()
        if stage_new and previous is not None and changed:
            candidates = load_candidates()
            for slug in changes["added"] + changes["changed"]:
                for tier in VALID_TIERS:
                    model = current["models"][slug]
                    eligible, _ = tier_eligibility(policy, model, tier)
                    if not eligible:
                        continue
                    item = stage_candidate_internal(
                        candidates,
                        current,
                        policy,
                        slug,
                        tier,
                        trigger="catalog-change",
                    )
                    if item:
                        staged.append(item["candidate_id"])
            atomic_write_json(CANDIDATES_PATH, candidates)
        if changed:
            append_jsonl(
                CATALOG_HISTORY_PATH,
                {
                    "observed_at": current["observed_at"],
                    "semantic_hash": current["semantic_hash"],
                    "captured_at": current.get("captured_at"),
                    "source": current.get("source_kind"),
                    "schema_upgrade": schema_upgrade,
                    "changes": changes,
                },
            )
        finish_catalog_refresh()
    except Exception as error:
        try:
            recover_catalog_refresh()
        except Exception as recovery_error:
            raise RouterLabError(
                f"catalog refresh failed ({error}); recovery also failed ({recovery_error})"
            ) from recovery_error
        raise
    return {
        "status": (
            "BASELINED"
            if previous is None
            else (
                (
                    "CHANGED_DURING_REBASELINE"
                    if any(changes[key] for key in ("added", "removed", "changed"))
                    else "REBASELINED"
                )
                if schema_upgrade
                else ("CHANGED" if changed else "NO_CHANGE")
            )
        ),
        "semantic_hash": current["semantic_hash"],
        "changes": changes,
        "failovers": failovers,
        "staged_candidates": staged,
        "captured_at": current.get("captured_at"),
        "source": current.get("source_kind"),
    }


def stage_candidate(model: str, tier: str) -> dict[str, Any]:
    if not CATALOG_PATH.exists():
        refresh_catalog(stage_new=False)
    catalog = read_json(CATALOG_PATH)
    live_catalog = read_catalog_snapshot()
    if live_catalog.get("semantic_hash") != catalog.get("semantic_hash"):
        raise RouterLabError("model catalog changed; refresh before staging")
    policy = load_policy()
    candidates = load_candidates()
    item = stage_candidate_internal(
        candidates, catalog, policy, model, tier, trigger="explicit-reoptimization"
    )
    atomic_write_json(CANDIDATES_PATH, candidates)
    return item or {"status": "already-active", "tier": tier, "model": model}


def restart_candidate(candidate_id: str, reason: str) -> dict[str, Any]:
    candidates = load_candidates()
    candidate = next(
        (item for item in candidates["items"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise RouterLabError(f"unknown candidate: {candidate_id}")
    if candidate.get("status") in {"promoted", "validated", "rolled_back"}:
        raise RouterLabError(f"candidate cannot restart from {candidate.get('status')}")
    policy = load_policy()
    catalog = read_json(CATALOG_PATH)
    freshness = candidate_freshness_reasons(candidate, policy, catalog)
    if freshness:
        raise RouterLabError(f"candidate is stale: {'; '.join(freshness)}")
    sequence = 1 + max(
        [
            int(item.get("restart_sequence", 0))
            for item in candidates["items"]
            if item.get("restart_of") == candidate_id
            or item.get("candidate_id") == candidate_id
        ],
        default=0,
    )
    candidate["status"] = "superseded"
    candidate["superseded_at"] = utc_now()
    candidate["superseded_reason"] = reason
    replacement = stage_candidate_internal(
        candidates,
        catalog,
        policy,
        candidate["candidate_model"],
        candidate["tier"],
        trigger="experiment-restart",
        restart_of=candidate_id,
        restart_sequence=sequence,
    )
    if replacement is None:
        raise RouterLabError("candidate restart did not produce a new experiment")
    atomic_write_json(CANDIDATES_PATH, candidates)
    remove_candidate_profile(candidate)
    return {
        "status": "RESTARTED",
        "superseded_candidate_id": candidate_id,
        "candidate_id": replacement["candidate_id"],
        "reason": reason,
    }


def verify_attestation(
    args: argparse.Namespace,
    policy: dict[str, Any],
    candidate: dict[str, Any],
    case: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    test_mode = os.environ.get("ADAPTIVE_MODEL_ROUTER_TEST_ATTESTATION") == "1"
    if test_mode and isolated_test_context():
        return (
            {
                "verified": bool(args.routing_verified),
                "run_id": args.run_id,
                "case_id": args.case_id,
                "arm": args.arm,
                "phase": (
                    "canary"
                    if candidate.get("status") == "promoted"
                    else "prepromotion"
                ),
                "tier": candidate["tier"],
                "family": case.get("family") if case else args.family,
                "holdout": bool(case.get("holdout")) if case else bool(args.holdout),
                "actual_model": args.enforced_model,
                "actual_effort": args.enforced_effort,
                "actual_provider": args.actual_provider,
                "profile_sha256": args.profile_sha256,
                "grader_kind": args.grader_kind,
                "grader_id_sha256": args.grader_id_sha256,
                "input_manifest_sha256": args.input_manifest_sha256,
                "evidence_sha256": args.evidence_sha256,
                "risk_floor_met": bool(args.risk_floor_met),
                "unauthorized_mutation": bool(args.unauthorized_mutation),
                "secret_exposure": bool(args.secret_exposure),
                "unsupported_routing": bool(args.unsupported_routing),
                "passed": bool(args.passed),
                "quality_score": args.quality_score,
                "critical_failure": bool(args.critical_failure),
                "latency_ms": args.latency_ms,
                "tokens": args.tokens,
                "cost_micros": args.cost_micros,
                "cost_source": args.cost_source,
                "escalations": args.escalations,
                "source": "isolated-test-bypass",
            },
            None,
        )
    constraints = policy.get("constraints", {})
    verifier_value = constraints.get("attestation_verifier_path")
    expected_verifier_hash = constraints.get("attestation_verifier_sha256")
    if not verifier_value or not expected_verifier_hash:
        return None, "no trusted attestation verifier is configured"
    if not args.attestation_file:
        return None, "attestation file is required"
    verifier = Path(verifier_value).expanduser()
    attestation_path = Path(args.attestation_file).expanduser()
    if not verifier.is_absolute() or not verifier.is_file() or verifier.is_symlink():
        return None, "configured attestation verifier is missing or unsafe"
    if sha256_bytes(verifier.read_bytes()) != expected_verifier_hash:
        return None, "configured attestation verifier hash mismatch"
    if not attestation_path.is_file() or attestation_path.is_symlink():
        return None, "attestation file is missing or unsafe"
    try:
        completed = subprocess.run(
            [str(verifier), str(attestation_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        attestation = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        return None, f"attestation verification failed: {error}"
    if not isinstance(attestation, dict) or attestation.get("verified") is not True:
        return None, "attestation verifier did not return a verified record"
    attestation["source"] = str(verifier)
    attestation["verifier_sha256"] = expected_verifier_hash
    return attestation, None


def record_observation(args: argparse.Namespace) -> dict[str, Any]:
    candidates = load_candidates()
    candidate = next(
        (
            item
            for item in candidates["items"]
            if item["candidate_id"] == args.candidate_id
        ),
        None,
    )
    if candidate is None:
        raise RouterLabError(f"unknown candidate: {args.candidate_id}")
    if candidate.get("status") in {
        "rejected",
        "stale",
        "superseded",
        "rolled_back",
        "validated",
    }:
        raise RouterLabError(f"candidate is terminal: {candidate.get('status')}")
    policy = load_policy()
    catalog = read_json(CATALOG_PATH)
    promoted_phase = candidate.get("status") == "promoted"
    phase = "canary" if promoted_phase else "prepromotion"
    freshness = (
        promoted_candidate_freshness_reasons(candidate, policy, catalog)
        if promoted_phase
        else candidate_freshness_reasons(candidate, policy, catalog)
    )
    if freshness:
        active_assignment = policy.get("tiers", {}).get(candidate.get("tier"), {})
        candidate_route_active = active_assignment.get("model") == candidate.get(
            "candidate_model"
        ) and active_assignment.get("effort") == candidate.get("candidate_effort")
        if promoted_phase and candidate_route_active:
            rollback = rollback_promoted_candidate(
                candidates,
                candidate,
                reason=f"promoted route drift: {'; '.join(freshness)}",
            )
            raise RouterLabError(
                f"promoted candidate drifted and was rolled back to {rollback['policy_id']}: "
                + "; ".join(freshness)
            )
        if promoted_phase:
            candidate["status"] = "superseded"
            candidate["superseded_at"] = utc_now()
            candidate["superseded_reason"] = "; ".join(freshness)
            atomic_write_json(CANDIDATES_PATH, candidates)
            control_path = AGENTS_DIR / candidate.get("control_profile_file", "")
            if (
                control_path.exists()
                and control_path.is_file()
                and sha256_bytes(control_path.read_bytes())
                == candidate.get("control_profile_sha256")
            ):
                control_path.unlink()
            raise RouterLabError(
                f"promoted candidate was superseded by another policy: {'; '.join(freshness)}"
            )
        candidate["status"] = "stale"
        atomic_write_json(CANDIDATES_PATH, candidates)
        remove_candidate_profile(candidate)
        raise RouterLabError(f"candidate is stale: {'; '.join(freshness)}")
    existing_observations = read_jsonl(OBSERVATIONS_PATH)
    existing_run_ids = {record.get("run_id") for record in existing_observations}
    if args.run_id in existing_run_ids:
        raise RouterLabError(f"duplicate run_id: {args.run_id}")
    if any(
        record.get("candidate_id") == args.candidate_id
        and record.get("case_id") == args.case_id
        and record.get("arm") == args.arm
        and record.get("eligible")
        for record in existing_observations
    ):
        raise RouterLabError(
            "an eligible observation already exists for this candidate/case/arm"
        )
    suite = load_eval_suite()
    case = suite.get(args.case_id)
    if case and case.get("tier") != candidate["tier"]:
        raise RouterLabError(
            f"suite case {args.case_id} belongs to {case.get('tier')}, not {candidate['tier']}"
        )
    attestation, attestation_error = verify_attestation(args, policy, candidate, case)
    if case:
        family = case.get("family")
        holdout = bool(case.get("holdout"))
    elif attestation:
        family = attestation.get("family")
        holdout = bool(attestation.get("holdout"))
    else:
        family = args.family
        holdout = False
    if not family:
        raise RouterLabError("family is required for a case outside the fixed suite")
    if attestation and attestation.get("tier") != candidate["tier"]:
        raise RouterLabError("attested case tier does not match the candidate tier")
    if promoted_phase:
        if args.arm == "active":
            expected_model = candidate["candidate_model"]
            expected_effort = candidate["candidate_effort"]
            expected_profile = candidate["active_profile_sha256"]
        elif args.arm == "control":
            expected_model = candidate["incumbent_model"]
            expected_effort = candidate["incumbent_effort"]
            expected_profile = candidate["control_profile_sha256"]
        else:
            raise RouterLabError("promoted candidates require active/control arms")
    else:
        if args.arm == "candidate":
            expected_model = candidate["candidate_model"]
            expected_effort = candidate["candidate_effort"]
            expected_profile = candidate["candidate_profile_sha256"]
        elif args.arm == "incumbent":
            expected_model = candidate["incumbent_model"]
            expected_effort = candidate["incumbent_effort"]
            expected_profile = candidate["incumbent_profile_sha256"]
        else:
            raise RouterLabError("staged candidates require incumbent/candidate arms")
    reasons: list[str] = []
    if attestation_error:
        reasons.append(attestation_error)
    if not attestation or attestation.get("verified") is not True:
        reasons.append("runtime routing was not independently attested")
    attested_fields = {
        "run_id": args.run_id,
        "case_id": args.case_id,
        "arm": args.arm,
        "phase": phase,
        "tier": candidate["tier"],
        "family": family,
        "holdout": holdout,
        "actual_model": args.enforced_model,
        "actual_effort": args.enforced_effort,
        "actual_provider": args.actual_provider,
        "profile_sha256": args.profile_sha256,
        "grader_kind": args.grader_kind,
        "grader_id_sha256": args.grader_id_sha256,
        "input_manifest_sha256": args.input_manifest_sha256,
        "evidence_sha256": args.evidence_sha256,
        "risk_floor_met": bool(args.risk_floor_met),
        "unauthorized_mutation": bool(args.unauthorized_mutation),
        "secret_exposure": bool(args.secret_exposure),
        "unsupported_routing": bool(args.unsupported_routing),
        "passed": bool(args.passed),
        "quality_score": args.quality_score,
        "critical_failure": bool(args.critical_failure),
        "latency_ms": args.latency_ms,
        "tokens": args.tokens,
        "cost_micros": args.cost_micros,
        "cost_source": args.cost_source,
        "escalations": args.escalations,
    }
    if attestation:
        for key, expected_value in attested_fields.items():
            if attestation.get(key) != expected_value:
                reasons.append(
                    f"attested {key} does not match the imported observation"
                )
    if args.enforced_model != expected_model:
        reasons.append("enforced model does not match the experiment arm")
    if args.enforced_effort != expected_effort:
        reasons.append("enforced effort does not match the experiment arm")
    if args.profile_sha256 != expected_profile:
        reasons.append("profile hash does not match the staged experiment")
    allowed_provider = policy.get("constraints", {}).get(
        "allowed_provider", "anthropic"
    )
    if args.actual_provider != allowed_provider:
        reasons.append("runtime provider is not allowed by policy")
    if args.grader_kind not in ELIGIBLE_GRADERS:
        reasons.append("grader is not independent and objective")
    if not re.fullmatch(r"[0-9a-f]{64}", args.grader_id_sha256 or ""):
        reasons.append("grader identity hash is missing or invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", args.input_manifest_sha256 or ""):
        reasons.append("input manifest hash is missing or invalid")
    if not args.risk_floor_met:
        reasons.append("task risk floor was not met")
    if args.unauthorized_mutation:
        reasons.append("run performed an unauthorized mutation")
    if args.secret_exposure:
        reasons.append("run exposed secret material")
    if args.unsupported_routing:
        reasons.append("run used an unsupported routing path")
    if args.cost_micros is not None and args.cost_source != "billing-receipt":
        reasons.append("cost requires a billing-receipt source")
    if not re.fullmatch(r"[0-9a-f]{64}", args.evidence_sha256 or ""):
        reasons.append("objective evidence hash is missing or invalid")
    if not (0.0 <= args.quality_score <= 1.0):
        reasons.append("quality_score must be between 0 and 1")
    for key in ("latency_ms", "tokens", "cost_micros", "escalations"):
        value = getattr(args, key)
        if value is not None and value < 0:
            reasons.append(f"{key} must be nonnegative")
    verified_safety_violations: list[str] = []
    attested_arm = attestation.get("arm") if attestation else None
    safety_expected: tuple[str, str, str] | None = None
    if promoted_phase and attested_arm == "active":
        safety_expected = (
            candidate["candidate_model"],
            candidate["candidate_effort"],
            candidate["active_profile_sha256"],
        )
    elif promoted_phase and attested_arm == "control":
        safety_expected = (
            candidate["incumbent_model"],
            candidate["incumbent_effort"],
            candidate["control_profile_sha256"],
        )
    elif not promoted_phase and attested_arm == "candidate":
        safety_expected = (
            candidate["candidate_model"],
            candidate["candidate_effort"],
            candidate["candidate_profile_sha256"],
        )
    elif not promoted_phase and attested_arm == "incumbent":
        safety_expected = (
            candidate["incumbent_model"],
            candidate["incumbent_effort"],
            candidate["incumbent_profile_sha256"],
        )
    if (
        attestation
        and attestation.get("verified") is True
        and attestation.get("phase") == phase
        and safety_expected is not None
    ):
        safety_model, safety_effort, safety_profile = safety_expected
        if attestation.get("actual_model") != safety_model:
            verified_safety_violations.append("runtime model mismatch")
        if attestation.get("actual_effort") != safety_effort:
            verified_safety_violations.append("runtime effort mismatch")
        if attestation.get("profile_sha256") != safety_profile:
            verified_safety_violations.append("runtime profile mismatch")
        if attestation.get("actual_provider") != allowed_provider:
            verified_safety_violations.append("runtime provider mismatch")
        if attestation.get("risk_floor_met") is not True:
            verified_safety_violations.append("task risk floor violation")
        if attestation.get("unauthorized_mutation") is True:
            verified_safety_violations.append("unauthorized mutation")
        if attestation.get("secret_exposure") is True:
            verified_safety_violations.append("secret exposure")
        if attestation.get("unsupported_routing") is True:
            verified_safety_violations.append("unsupported routing")
        if attestation.get("critical_failure") is True:
            verified_safety_violations.append("critical failure")
    verified_canary_violations = (
        verified_safety_violations
        if promoted_phase and attested_arm == "active"
        else []
    )
    observation = {
        "schema_version": 1,
        "observation_id": content_id(
            {"candidate_id": args.candidate_id, "run_id": args.run_id}
        ),
        "recorded_at": utc_now(),
        "candidate_id": args.candidate_id,
        "run_id": args.run_id,
        "case_id": args.case_id,
        "phase": phase,
        "tier": candidate["tier"],
        "family": family,
        "holdout": holdout,
        "arm": args.arm,
        "expected_model": expected_model,
        "expected_effort": expected_effort,
        "enforced_model": args.enforced_model,
        "enforced_effort": args.enforced_effort,
        "actual_provider": args.actual_provider,
        "routing_verified": bool(attestation and attestation.get("verified") is True),
        "attestation_sha256": content_id(attestation) if attestation else None,
        "attestation_source": attestation.get("source") if attestation else None,
        "attested_arm": attestation.get("arm") if attestation else None,
        "attested_phase": attestation.get("phase") if attestation else None,
        "attested_model": attestation.get("actual_model") if attestation else None,
        "attested_effort": attestation.get("actual_effort") if attestation else None,
        "attested_profile_sha256": (
            attestation.get("profile_sha256") if attestation else None
        ),
        "profile_sha256": args.profile_sha256,
        "grader_kind": args.grader_kind,
        "grader_id_sha256": args.grader_id_sha256,
        "input_manifest_sha256": args.input_manifest_sha256,
        "evidence_sha256": args.evidence_sha256,
        "risk_floor_met": bool(args.risk_floor_met),
        "unauthorized_mutation": bool(args.unauthorized_mutation),
        "secret_exposure": bool(args.secret_exposure),
        "unsupported_routing": bool(args.unsupported_routing),
        "passed": bool(args.passed),
        "quality_score": args.quality_score,
        "critical_failure": bool(args.critical_failure),
        "latency_ms": args.latency_ms,
        "tokens": args.tokens,
        "cost_micros": args.cost_micros,
        "cost_source": args.cost_source,
        "escalations": args.escalations,
        "eligible": not reasons,
        "ineligibility_reasons": reasons,
        "verified_safety_violations": verified_safety_violations,
        "verified_canary_violations": verified_canary_violations,
    }
    append_jsonl(OBSERVATIONS_PATH, observation)
    if candidate["status"] in {
        "staged",
        "blocked_model_enforcement",
        "qualified",
    } and (observation["eligible"] or observation["verified_safety_violations"]):
        candidate["status"] = "collecting"
        candidate.pop("last_evaluation", None)
        atomic_write_json(CANDIDATES_PATH, candidates)
    return observation


def metric_gain(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], key: str
) -> float | None:
    incumbent_values = [pair[0].get(key) for pair in pairs]
    candidate_values = [pair[1].get(key) for pair in pairs]
    valid = [
        (float(incumbent), float(candidate))
        for incumbent, candidate in zip(incumbent_values, candidate_values)
        if incumbent is not None and candidate is not None
    ]
    if len(valid) < max(4, len(pairs) // 2):
        return None
    incumbent_median = statistics.median(item[0] for item in valid)
    candidate_median = statistics.median(item[1] for item in valid)
    if incumbent_median == 0:
        return 0.0 if candidate_median == 0 else -1.0
    return (incumbent_median - candidate_median) / incumbent_median


def candidate_freshness_reasons(
    candidate: dict[str, Any], policy: dict[str, Any], catalog: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if candidate.get("incumbent_policy_id") != policy.get("policy_id"):
        reasons.append("incumbent policy changed")
    if candidate.get("catalog_semantic_hash") != catalog.get("semantic_hash"):
        reasons.append("catalog semantic hash changed")
    model = catalog.get("models", {}).get(candidate.get("candidate_model"))
    if model is None or model.get("revision_hash") != candidate.get(
        "candidate_revision_hash"
    ):
        reasons.append("candidate model revision changed")
    if candidate.get("eval_suite_hash") != sha256_bytes(EVAL_SUITE_PATH.read_bytes()):
        reasons.append("evaluation suite changed")
    template = TEMPLATE_DIR / TIER_TEMPLATES[candidate["tier"]]
    if candidate.get("role_template_sha256") != sha256_bytes(template.read_bytes()):
        reasons.append("role template changed")
    if candidate.get("promotion_rules_hash") != content_id(policy["promotion"]):
        reasons.append("promotion rules changed")
    try:
        live_catalog = read_catalog_snapshot()
    except RouterLabError as error:
        reasons.append(f"live model catalog is unavailable: {error}")
    else:
        if live_catalog.get("semantic_hash") != catalog.get("semantic_hash"):
            reasons.append("live model catalog differs from the refreshed baseline")
    return reasons


def promoted_candidate_freshness_reasons(
    candidate: dict[str, Any], policy: dict[str, Any], catalog: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if candidate.get("promoted_policy_id") != policy.get("policy_id"):
        reasons.append("promoted policy is no longer active")
    assignment = policy.get("tiers", {}).get(candidate.get("tier"), {})
    if assignment.get("model") != candidate.get("candidate_model"):
        reasons.append("active model differs from the promoted candidate")
    if assignment.get("effort") != candidate.get("candidate_effort"):
        reasons.append("active effort differs from the promoted candidate")
    model = catalog.get("models", {}).get(candidate.get("candidate_model"))
    if model is None or model.get("revision_hash") != candidate.get(
        "candidate_revision_hash"
    ):
        reasons.append("promoted model revision changed")
    active_profile = AGENTS_DIR / assignment.get("profile_file", "")
    active_hash = (
        sha256_bytes(active_profile.read_bytes())
        if active_profile.exists() and active_profile.is_file()
        else None
    )
    if active_hash != candidate.get("active_profile_sha256"):
        reasons.append("active profile changed")
    control_profile = AGENTS_DIR / candidate.get("control_profile_file", "")
    control_hash = (
        sha256_bytes(control_profile.read_bytes())
        if control_profile.exists() and control_profile.is_file()
        else None
    )
    if control_hash != candidate.get("control_profile_sha256"):
        reasons.append("canary control profile changed")
    try:
        live_catalog = read_catalog_snapshot()
    except RouterLabError as error:
        reasons.append(f"live model catalog is unavailable: {error}")
    else:
        if live_catalog.get("semantic_hash") != catalog.get("semantic_hash"):
            reasons.append("live model catalog differs from the refreshed baseline")
    return reasons


def evaluate_candidate(candidate_id: str, promote: bool) -> dict[str, Any]:
    candidates = load_candidates()
    candidate = next(
        (item for item in candidates["items"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise RouterLabError(f"unknown candidate: {candidate_id}")
    policy = load_policy()
    catalog = read_json(CATALOG_PATH)
    freshness = candidate_freshness_reasons(candidate, policy, catalog)
    if freshness:
        candidate["status"] = "stale"
        atomic_write_json(CANDIDATES_PATH, candidates)
        remove_candidate_profile(candidate)
        raise RouterLabError(f"candidate is stale: {'; '.join(freshness)}")
    all_observations = [
        record
        for record in read_jsonl(OBSERVATIONS_PATH)
        if record.get("candidate_id") == candidate_id
        and record.get("phase") == "prepromotion"
    ]
    observations = [record for record in all_observations if record.get("eligible")]
    evidence_set_sha256 = observation_set_hash(all_observations)
    verified_candidate_safety = [
        {
            "observation_id": record.get("observation_id"),
            "run_id": record.get("run_id"),
            "reasons": record.get("verified_safety_violations", []),
        }
        for record in all_observations
        if record.get("routing_verified")
        and record.get("attested_arm") == "candidate"
        and record.get("verified_safety_violations")
    ]
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for observation in observations:
        arm = observation.get("arm")
        if arm not in {"incumbent", "candidate"}:
            continue
        by_case.setdefault(observation["case_id"], {}).setdefault(arm, observation)
    pairs = [
        (arms["incumbent"], arms["candidate"])
        for arms in by_case.values()
        if "incumbent" in arms and "candidate" in arms
    ]
    mismatched_pair_cases = sorted(
        case_id
        for case_id, arms in by_case.items()
        if "incumbent" in arms
        and "candidate" in arms
        and (
            arms["incumbent"].get("input_manifest_sha256")
            != arms["candidate"].get("input_manifest_sha256")
            or arms["incumbent"].get("grader_id_sha256")
            != arms["candidate"].get("grader_id_sha256")
            or arms["incumbent"].get("grader_kind")
            != arms["candidate"].get("grader_kind")
            or arms["incumbent"].get("family") != arms["candidate"].get("family")
            or arms["incumbent"].get("holdout") != arms["candidate"].get("holdout")
        )
    )
    paired_case_ids = {
        case_id
        for case_id, arms in by_case.items()
        if "incumbent" in arms and "candidate" in arms
    }
    unpaired_candidate_cases = sorted(
        case_id
        for case_id, arms in by_case.items()
        if "candidate" in arms and "incumbent" not in arms
    )
    unpaired_incumbent_cases = sorted(
        case_id
        for case_id, arms in by_case.items()
        if "incumbent" in arms and "candidate" not in arms
    )
    required_suite_cases = set(candidate.get("required_suite_case_ids", []))
    missing_required_suite_cases = sorted(required_suite_cases - paired_case_ids)
    tier = candidate["tier"]
    promotion = policy["promotion"]
    min_pairs = int(promotion["min_pairs_by_tier"][tier])
    min_holdout = int(promotion["min_holdout_pairs"])
    min_families = int(promotion["min_task_families"])
    holdout_pairs = [pair for pair in pairs if pair[0].get("holdout")]
    families = {pair[0].get("family") for pair in pairs}
    incumbent_quality = (
        statistics.fmean(pair[0]["quality_score"] for pair in pairs) if pairs else 0.0
    )
    candidate_quality = (
        statistics.fmean(pair[1]["quality_score"] for pair in pairs) if pairs else 0.0
    )
    quality_gain = candidate_quality - incumbent_quality
    extra_failures = sum(pair[0]["passed"] and not pair[1]["passed"] for pair in pairs)
    candidate_critical_ids = {
        observation.get("observation_id")
        for observation in observations
        if observation.get("arm") == "candidate" and observation.get("critical_failure")
    }
    candidate_critical_ids.update(
        violation.get("observation_id") for violation in verified_candidate_safety
    )
    candidate_critical = len(candidate_critical_ids)
    efficiency = {
        key: metric_gain(pairs, key)
        for key in ("latency_ms", "tokens", "cost_micros", "escalations")
    }
    enough_data = (
        len(pairs) >= min_pairs
        and len(holdout_pairs) >= min_holdout
        and len(families) >= min_families
        and not missing_required_suite_cases
        and not unpaired_candidate_cases
        and not unpaired_incumbent_cases
        and not mismatched_pair_cases
    )
    no_safety_failure = candidate_critical == 0
    quality_noninferior = quality_gain >= -float(promotion["max_quality_drop"])
    failure_limit = int(promotion["max_extra_failures_by_tier"][tier])
    failure_gate = extra_failures <= failure_limit
    measurable = [value for value in efficiency.values() if value is not None]
    no_efficiency_regression = all(
        value >= -float(promotion["max_efficiency_regression"]) for value in measurable
    )
    improvement = quality_gain >= float(promotion["min_quality_gain"]) or any(
        value >= float(promotion["min_efficiency_gain"]) for value in measurable
    )
    qualified = all(
        [
            enough_data,
            no_safety_failure,
            quality_noninferior,
            failure_gate,
            no_efficiency_regression,
            improvement,
        ]
    )
    if not no_safety_failure:
        decision = "REJECTED"
        candidate["status"] = "rejected"
    elif not enough_data:
        if not observations and not attestation_verifier_ready(policy):
            # This surface cannot enforce and attest model selection without a
            # configured trusted verifier: no scored experiment may run.
            decision = "BLOCKED_MODEL_ENFORCEMENT"
            candidate["status"] = "blocked_model_enforcement"
            candidate["blocked_at"] = utc_now()
            candidate["blocked_reason"] = (
                "no trusted attestation verifier is configured"
            )
        else:
            decision = "COLLECTING"
            candidate["status"] = "collecting"
    elif qualified:
        decision = "QUALIFIED"
        candidate["status"] = "qualified"
    else:
        decision = "REJECTED"
        candidate["status"] = "rejected"
    metrics = {
        "pairs": len(pairs),
        "holdout_pairs": len(holdout_pairs),
        "families": sorted(family for family in families if family),
        "incumbent_quality": incumbent_quality,
        "candidate_quality": candidate_quality,
        "quality_gain": quality_gain,
        "extra_failures": extra_failures,
        "candidate_critical_failures": candidate_critical,
        "verified_candidate_safety_violations": verified_candidate_safety,
        "missing_required_suite_cases": missing_required_suite_cases,
        "unpaired_candidate_cases": unpaired_candidate_cases,
        "unpaired_incumbent_cases": unpaired_incumbent_cases,
        "mismatched_pair_cases": mismatched_pair_cases,
        "evidence_set_sha256": evidence_set_sha256,
        "efficiency_gains": efficiency,
    }
    candidate["last_evaluation"] = {
        "decision": decision,
        "evaluated_at": utc_now(),
        **metrics,
    }
    atomic_write_json(CANDIDATES_PATH, candidates)
    if candidate.get("status") == "rejected":
        remove_candidate_profile(candidate)
    append_jsonl(
        EXPERIMENTS_PATH,
        {
            "experiment_id": content_id(
                {
                    "candidate_id": candidate_id,
                    "cutoff": len(observations),
                    "metrics": metrics,
                }
            ),
            "candidate_id": candidate_id,
            "evaluated_at": utc_now(),
            "decision": decision,
            "policy_id": policy["policy_id"],
            "catalog_semantic_hash": catalog["semantic_hash"],
            "metrics": metrics,
        },
    )
    if qualified and promote:
        promote_candidate_internal(candidates, candidate, policy, catalog)
        decision = "PROMOTED"
    return {"decision": decision, "candidate_id": candidate_id, "metrics": metrics}


def promote_candidate_internal(
    candidates: dict[str, Any],
    candidate: dict[str, Any],
    policy: dict[str, Any],
    catalog: dict[str, Any],
) -> None:
    if candidate.get("status") != "qualified":
        raise RouterLabError("candidate is not qualified")
    if any(
        item.get("status") == "promoted"
        and item.get("candidate_id") != candidate.get("candidate_id")
        for item in candidates["items"]
    ):
        raise RouterLabError("another promoted candidate is still in canary validation")
    freshness = candidate_freshness_reasons(candidate, policy, catalog)
    if freshness:
        raise RouterLabError(f"candidate is stale: {'; '.join(freshness)}")
    current_evidence_hash = observation_set_hash(
        [
            record
            for record in read_jsonl(OBSERVATIONS_PATH)
            if record.get("candidate_id") == candidate["candidate_id"]
            and record.get("phase") == "prepromotion"
        ]
    )
    last_evaluation = candidate.get("last_evaluation", {})
    if (
        last_evaluation.get("decision") != "QUALIFIED"
        or last_evaluation.get("evidence_set_sha256") != current_evidence_hash
    ):
        raise RouterLabError("candidate qualification evidence is stale")
    if candidate["incumbent_policy_id"] != policy["policy_id"]:
        raise RouterLabError("candidate policy baseline is stale")
    model = catalog.get("models", {}).get(candidate["candidate_model"])
    if not model or model.get("revision_hash") != candidate["candidate_revision_hash"]:
        raise RouterLabError("candidate catalog revision is stale")
    revised = copy.deepcopy(policy)
    assignment = revised["tiers"][candidate["tier"]]
    assignment["model"] = candidate["candidate_model"]
    assignment["effort"] = candidate["candidate_effort"]
    revised["revision"] = int(revised.get("revision", 0)) + 1
    revised["updated_at"] = utc_now()
    revised["source"] = f"qualified-experiment:{candidate['candidate_id']}"
    revised["policy_id"] = policy_id(revised)
    control_agent = (
        f"router-control-{candidate['tier'].lower()}-{candidate['candidate_id'][:8]}"
    )
    control_profile_file = f"router-control-{candidate['tier'].lower()}-{candidate['candidate_id'][:8]}.md"
    control_profile = render_profile(
        candidate["tier"],
        candidate["incumbent_model"],
        candidate["incumbent_effort"],
        agent_name=control_agent,
        description_suffix="Retained incumbent control for post-promotion canary comparison.",
    )
    control_path = AGENTS_DIR / control_profile_file
    if (
        control_path.exists()
        and control_path.read_text(encoding="utf-8") != control_profile
    ):
        raise RouterLabError(f"control profile collision: {control_path}")
    atomic_write_text(control_path, control_profile)
    promotion_snapshot: Path | None = None
    candidate_persisted = False
    try:
        promotion_snapshot = activate_policy(
            revised,
            reason=f"qualified {candidate['candidate_id']}",
            retain_journal=True,
            transaction_candidate_id=candidate["candidate_id"],
        )
        candidate["status"] = "promoted"
        candidate["promoted_at"] = utc_now()
        candidate["promoted_policy_id"] = revised["policy_id"]
        candidate["promotion_snapshot"] = str(promotion_snapshot)
        candidate["promotion_snapshot_manifest_sha256"] = sha256_bytes(
            (promotion_snapshot / "manifest.json").read_bytes()
        )
        candidate["control_agent"] = control_agent
        candidate["control_profile_file"] = control_profile_file
        candidate["control_profile_sha256"] = sha256_bytes(
            control_profile.encode("utf-8")
        )
        active_profile = (
            AGENTS_DIR / revised["tiers"][candidate["tier"]]["profile_file"]
        )
        candidate["active_profile_sha256"] = sha256_bytes(active_profile.read_bytes())
        for other in candidates["items"]:
            if (
                other["candidate_id"] != candidate["candidate_id"]
                and other["tier"] == candidate["tier"]
                and other.get("status")
                in {"staged", "collecting", "blocked_model_enforcement", "qualified"}
            ):
                other["status"] = "superseded"
        atomic_write_json(CANDIDATES_PATH, candidates)
        persisted = next(
            (
                item
                for item in load_candidates()["items"]
                if item.get("candidate_id") == candidate["candidate_id"]
            ),
            None,
        )
        candidate_persisted = bool(
            persisted
            and persisted.get("status") == "promoted"
            and persisted.get("promoted_policy_id") == revised["policy_id"]
        )
        if not candidate_persisted:
            raise RouterLabError("candidate promotion state readback failed")
        JOURNAL_PATH.unlink()
    except Exception:
        if promotion_snapshot is not None and not candidate_persisted:
            restore_snapshot(
                promotion_snapshot,
                reason="incomplete promotion recovery",
                allow_unavailable=True,
            )
        if (
            not candidate_persisted
            and control_path.exists()
            and sha256_bytes(control_path.read_bytes())
            == sha256_bytes(control_profile.encode("utf-8"))
        ):
            control_path.unlink()
        raise
    candidate_profile = AGENTS_DIR / candidate["candidate_profile_file"]
    if (
        candidate_profile.exists()
        and sha256_bytes(candidate_profile.read_bytes())
        == candidate["candidate_profile_sha256"]
    ):
        candidate_profile.unlink()


def promote_candidate(candidate_id: str) -> dict[str, Any]:
    result = evaluate_candidate(candidate_id, promote=True)
    if result.get("decision") != "PROMOTED":
        raise RouterLabError(
            f"candidate did not requalify for promotion: {result.get('decision')}"
        )
    return {"status": "PROMOTED", "candidate_id": candidate_id}


def block_candidate(
    candidate_id: str, reason: str, fingerprint: str | None
) -> dict[str, Any]:
    candidates = load_candidates()
    candidate = next(
        (item for item in candidates["items"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None:
        raise RouterLabError(f"unknown candidate: {candidate_id}")
    if candidate.get("status") in {
        "promoted",
        "rejected",
        "stale",
        "superseded",
        "rolled_back",
        "validated",
    }:
        raise RouterLabError(f"candidate is terminal: {candidate.get('status')}")
    candidate["status"] = "blocked_model_enforcement"
    candidate["blocked_at"] = utc_now()
    candidate["blocked_reason"] = reason
    candidate["enforcement_fingerprint"] = fingerprint
    atomic_write_json(CANDIDATES_PATH, candidates)
    return {
        "status": "BLOCKED_MODEL_ENFORCEMENT",
        "candidate_id": candidate_id,
        "reason": reason,
        "fingerprint": fingerprint,
    }


def rollback_promoted_candidate(
    candidates: dict[str, Any], candidate: dict[str, Any], *, reason: str
) -> dict[str, Any]:
    snapshot_value = candidate.get("promotion_snapshot")
    expected_manifest_hash = candidate.get("promotion_snapshot_manifest_sha256")
    if not snapshot_value or not expected_manifest_hash:
        raise RouterLabError("promoted candidate has no bound rollback snapshot")
    snapshot = Path(snapshot_value)
    manifest_path = snapshot / "manifest.json"
    if not snapshot.is_dir() or not manifest_path.is_file():
        raise RouterLabError("promoted candidate rollback snapshot is missing")
    if sha256_bytes(manifest_path.read_bytes()) != expected_manifest_hash:
        raise RouterLabError("promoted candidate rollback snapshot manifest changed")
    manifest = read_json(manifest_path)
    if manifest.get("policy_id") != candidate.get("incumbent_policy_id"):
        raise RouterLabError(
            "promoted candidate rollback snapshot has the wrong policy"
        )
    restored = restore_snapshot(
        snapshot,
        reason=reason,
        allow_unavailable=True,
        retain_journal=True,
        transaction_candidate_id=candidate["candidate_id"],
        target_candidate_status="rolled_back",
    )
    if restored.get("policy_id") != candidate.get("incumbent_policy_id"):
        raise RouterLabError("canary rollback did not restore the incumbent policy")
    if load_policy().get("policy_id") != candidate.get("incumbent_policy_id"):
        raise RouterLabError("canary rollback policy readback failed")
    candidate["status"] = "rolled_back"
    candidate["rolled_back_at"] = utc_now()
    candidate["rollback_reason"] = reason
    control_path = AGENTS_DIR / candidate.get("control_profile_file", "")
    if (
        control_path.exists()
        and control_path.is_file()
        and sha256_bytes(control_path.read_bytes())
        == candidate.get("control_profile_sha256")
    ):
        control_path.unlink()
    atomic_write_json(CANDIDATES_PATH, candidates)
    persisted = next(
        (
            item
            for item in load_candidates()["items"]
            if item.get("candidate_id") == candidate.get("candidate_id")
        ),
        None,
    )
    if not persisted or persisted.get("status") != "rolled_back":
        raise RouterLabError("canary rollback candidate-state readback failed")
    JOURNAL_PATH.unlink()
    return {"status": "ROLLED_BACK", "policy_id": restored["policy_id"]}


def monitor_candidate(candidate_id: str, dry_run: bool) -> dict[str, Any]:
    candidates = load_candidates()
    candidate = next(
        (item for item in candidates["items"] if item["candidate_id"] == candidate_id),
        None,
    )
    if candidate is None or candidate.get("status") != "promoted":
        raise RouterLabError("canary monitoring requires an active promoted candidate")
    policy = load_policy()
    catalog = read_json(CATALOG_PATH)
    freshness = promoted_candidate_freshness_reasons(candidate, policy, catalog)
    if freshness:
        active_assignment = policy.get("tiers", {}).get(candidate.get("tier"), {})
        candidate_route_active = active_assignment.get("model") == candidate.get(
            "candidate_model"
        ) and active_assignment.get("effort") == candidate.get("candidate_effort")
        if candidate_route_active:
            rollback = rollback_promoted_candidate(
                candidates,
                candidate,
                reason=f"promoted route drift: {'; '.join(freshness)}",
            )
            result = {
                "decision": "ROLLED_BACK",
                "candidate_id": candidate_id,
                "freshness_violations": freshness,
                "rollback": rollback,
            }
            append_jsonl(
                EXPERIMENTS_PATH,
                {"type": "canary", "evaluated_at": utc_now(), **result},
            )
            return result
        candidate["status"] = "superseded"
        candidate["superseded_at"] = utc_now()
        candidate["superseded_reason"] = "; ".join(freshness)
        atomic_write_json(CANDIDATES_PATH, candidates)
        control_path = AGENTS_DIR / candidate.get("control_profile_file", "")
        if (
            control_path.exists()
            and control_path.is_file()
            and sha256_bytes(control_path.read_bytes())
            == candidate.get("control_profile_sha256")
        ):
            control_path.unlink()
        return {
            "decision": "SUPERSEDED",
            "candidate_id": candidate_id,
            "freshness_violations": freshness,
        }
    all_canary_observations = [
        record
        for record in read_jsonl(OBSERVATIONS_PATH)
        if record.get("candidate_id") == candidate_id
        and record.get("phase") == "canary"
    ]
    observations = [
        record for record in all_canary_observations if record.get("eligible")
    ]
    verified_violations = [
        {
            "observation_id": record.get("observation_id"),
            "run_id": record.get("run_id"),
            "reasons": record.get("verified_canary_violations", []),
        }
        for record in all_canary_observations
        if record.get("routing_verified") and record.get("verified_canary_violations")
    ]
    by_case: dict[str, dict[str, dict[str, Any]]] = {}
    for observation in observations:
        arm = observation.get("arm")
        if arm in {"control", "active"}:
            by_case.setdefault(observation["case_id"], {}).setdefault(arm, observation)
    mismatched_canary_cases = sorted(
        case_id
        for case_id, arms in by_case.items()
        if "control" in arms
        and "active" in arms
        and (
            arms["control"].get("input_manifest_sha256")
            != arms["active"].get("input_manifest_sha256")
            or arms["control"].get("grader_id_sha256")
            != arms["active"].get("grader_id_sha256")
            or arms["control"].get("grader_kind") != arms["active"].get("grader_kind")
        )
    )
    ordered_pairs = [
        (arms["control"], arms["active"])
        for _, arms in sorted(
            by_case.items(),
            key=lambda item: max(
                item[1].get("control", {}).get("recorded_at", ""),
                item[1].get("active", {}).get("recorded_at", ""),
            ),
        )
        if "control" in arms
        and "active" in arms
        and arms["control"].get("input_manifest_sha256")
        == arms["active"].get("input_manifest_sha256")
        and arms["control"].get("grader_id_sha256")
        == arms["active"].get("grader_id_sha256")
        and arms["control"].get("grader_kind") == arms["active"].get("grader_kind")
    ]
    active_critical = sum(
        "critical failure" in violation["reasons"] for violation in verified_violations
    )
    unpaired = sum(
        not ({"control", "active"} <= set(arms)) for arms in by_case.values()
    )
    canary = policy["canary"]
    window_size = int(canary["window_pairs"])
    windows = [
        ordered_pairs[index : index + window_size]
        for index in range(0, len(ordered_pairs), window_size)
        if len(ordered_pairs[index : index + window_size]) == window_size
    ]
    window_metrics: list[dict[str, Any]] = []
    for window in windows:
        control_quality = statistics.fmean(pair[0]["quality_score"] for pair in window)
        active_quality = statistics.fmean(pair[1]["quality_score"] for pair in window)
        gains = {
            key: metric_gain(window, key)
            for key in ("latency_ms", "tokens", "cost_micros", "escalations")
        }
        measured = [value for value in gains.values() if value is not None]
        bad = active_quality - control_quality < -float(
            canary["max_quality_drop"]
        ) or any(
            value < -float(canary["max_efficiency_regression"]) for value in measured
        )
        window_metrics.append(
            {
                "control_quality": control_quality,
                "active_quality": active_quality,
                "quality_gain": active_quality - control_quality,
                "efficiency_gains": gains,
                "bad": bad,
            }
        )
    consecutive = int(canary["consecutive_bad_windows"])
    statistical_regression = len(window_metrics) >= consecutive and all(
        window["bad"] for window in window_metrics[-consecutive:]
    )
    validation_ready = len(window_metrics) >= consecutive and all(
        not window["bad"] for window in window_metrics[-consecutive:]
    )
    rollback_required = bool(verified_violations) or statistical_regression
    if rollback_required:
        decision = "ROLLBACK_REQUIRED"
    elif not validation_ready or unpaired or mismatched_canary_cases:
        decision = "COLLECTING"
    else:
        decision = "HEALTHY"
    result = {
        "decision": decision,
        "candidate_id": candidate_id,
        "pairs": len(ordered_pairs),
        "unpaired_cases": unpaired,
        "mismatched_pair_cases": mismatched_canary_cases,
        "active_critical_failures": active_critical,
        "verified_safety_violations": verified_violations,
        "windows": window_metrics,
    }
    if rollback_required and not dry_run:
        rollback_result = rollback_promoted_candidate(
            candidates,
            candidate,
            reason=f"canary regression for {candidate_id}",
        )
        result["rollback"] = rollback_result
        result["decision"] = "ROLLED_BACK"
    elif decision == "HEALTHY" and not dry_run:
        result["decision"] = "VALIDATED"
        candidate["status"] = "validated"
        candidate["validated_at"] = utc_now()
        candidate["validation"] = {
            "evaluated_at": candidate["validated_at"],
            **copy.deepcopy(result),
        }
        atomic_write_json(CANDIDATES_PATH, candidates)
        control_path = AGENTS_DIR / candidate.get("control_profile_file", "")
        if (
            control_path.exists()
            and control_path.is_file()
            and sha256_bytes(control_path.read_bytes())
            == candidate.get("control_profile_sha256")
        ):
            control_path.unlink()
    append_jsonl(
        EXPERIMENTS_PATH,
        {"type": "canary", "evaluated_at": utc_now(), **result},
    )
    return result


def rollback_latest() -> dict[str, Any]:
    current = load_policy()
    candidates = load_candidates()
    promoted = next(
        (
            candidate
            for candidate in candidates["items"]
            if candidate.get("status") == "promoted"
            and candidate.get("promoted_policy_id") == current["policy_id"]
        ),
        None,
    )
    if promoted:
        return rollback_promoted_candidate(
            candidates,
            promoted,
            reason="operator-requested-promoted-candidate-rollback",
        )
    snapshots: list[tuple[Path, str]] = []
    for path in sorted(SNAPSHOTS_DIR.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            snapshot_policy, _, _ = validate_snapshot(path)
        except (RouterLabError, OSError, ValueError, json.JSONDecodeError):
            continue
        if snapshot_policy.get("policy_id") != current["policy_id"]:
            snapshots.append((path, snapshot_policy["policy_id"]))
    if not snapshots:
        raise RouterLabError("no complete non-current rollback snapshot is available")
    policy = restore_snapshot(snapshots[-1][0], reason="operator-requested-latest")
    return {"status": "ROLLED_BACK", "policy_id": policy["policy_id"]}


def recover_activation() -> dict[str, Any]:
    if CATALOG_REFRESH_JOURNAL_PATH.exists():
        return recover_catalog_refresh()
    if not JOURNAL_PATH.exists():
        return {"status": "CLEAN"}
    journal = read_json(JOURNAL_PATH)
    snapshot = Path(journal["snapshot"])
    candidate_id = journal.get("candidate_id")
    target_policy_id = journal.get("target_policy_id")
    if (
        journal.get("action") == "rollback"
        and candidate_id
        and journal.get("target_candidate_status") == "rolled_back"
    ):
        try:
            active = load_policy()
            assert_installed_matches(active)
        except (RouterLabError, OSError, ValueError, json.JSONDecodeError):
            active = None
        if not active or active.get("policy_id") != target_policy_id:
            restore_snapshot(
                snapshot,
                reason="candidate rollback recovery",
                allow_unavailable=True,
                retain_journal=True,
                transaction_candidate_id=candidate_id,
                target_candidate_status="rolled_back",
            )
        candidates = load_candidates()
        candidate = next(
            (
                item
                for item in candidates["items"]
                if item.get("candidate_id") == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise RouterLabError("rollback recovery candidate is missing")
        candidate["status"] = "rolled_back"
        candidate["rolled_back_at"] = utc_now()
        candidate["rollback_reason"] = "candidate rollback recovery"
        control_path = AGENTS_DIR / candidate.get("control_profile_file", "")
        if (
            control_path.exists()
            and control_path.is_file()
            and sha256_bytes(control_path.read_bytes())
            == candidate.get("control_profile_sha256")
        ):
            control_path.unlink()
        atomic_write_json(CANDIDATES_PATH, candidates)
        persisted = next(
            (
                item
                for item in load_candidates()["items"]
                if item.get("candidate_id") == candidate_id
            ),
            None,
        )
        if not persisted or persisted.get("status") != "rolled_back":
            raise RouterLabError("rollback recovery candidate-state readback failed")
        JOURNAL_PATH.unlink()
        append_jsonl(
            ACTIVATIONS_PATH,
            {
                "action": "candidate-rollback-recovery-finalized",
                "activated_at": utc_now(),
                "policy_id": target_policy_id,
                "candidate_id": candidate_id,
                "snapshot": str(snapshot),
            },
        )
        return {"status": "RECOVERED", "policy_id": target_policy_id}
    if journal.get("action") == "activate" and candidate_id and target_policy_id:
        candidates = load_candidates()
        candidate = next(
            (
                item
                for item in candidates["items"]
                if item.get("candidate_id") == candidate_id
            ),
            None,
        )
        try:
            active = load_policy()
            assert_installed_matches(active)
        except (RouterLabError, OSError, ValueError, json.JSONDecodeError):
            active = None
        if (
            active
            and active.get("policy_id") == target_policy_id
            and candidate
            and candidate.get("status") == "promoted"
            and candidate.get("promoted_policy_id") == target_policy_id
            and candidate.get("promotion_snapshot") == str(snapshot)
        ):
            validate_snapshot(snapshot)
            JOURNAL_PATH.unlink()
            append_jsonl(
                ACTIVATIONS_PATH,
                {
                    "action": "promotion-recovery-finalized",
                    "activated_at": utc_now(),
                    "policy_id": target_policy_id,
                    "candidate_id": candidate_id,
                    "snapshot": str(snapshot),
                },
            )
            return {"status": "FINALIZED", "policy_id": target_policy_id}
    policy = restore_snapshot(
        snapshot,
        reason="journal recovery",
        allow_unavailable=True,
        retain_journal=bool(candidate_id),
        transaction_candidate_id=candidate_id,
        target_candidate_status="rolled_back" if candidate_id else None,
    )
    if candidate_id:
        candidates = load_candidates()
        candidate = next(
            (
                item
                for item in candidates["items"]
                if item.get("candidate_id") == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise RouterLabError("promotion recovery candidate is missing")
        candidate["status"] = "rolled_back"
        candidate["rolled_back_at"] = utc_now()
        candidate["rollback_reason"] = "incomplete promotion recovery"
        control_path = AGENTS_DIR / candidate.get("control_profile_file", "")
        if (
            control_path.exists()
            and control_path.is_file()
            and sha256_bytes(control_path.read_bytes())
            == candidate.get("control_profile_sha256")
        ):
            control_path.unlink()
        atomic_write_json(CANDIDATES_PATH, candidates)
        JOURNAL_PATH.unlink()
    return {"status": "RECOVERED", "policy_id": policy["policy_id"]}


def execution_evidence_mode(policy: dict[str, Any]) -> str:
    """Runtime metadata is the default Claude evidence mode.

    ``runtime-metadata`` means first-party observed ``claude`` CLI stream output
    is the evidence of record (declared grade). ``external-verifier`` is only
    reachable once ``configure-attestation`` pins a verifier.
    """
    constraints = policy.get("constraints", {})
    if constraints.get("attestation_verifier_path") or constraints.get(
        "attestation_verifier_sha256"
    ):
        return "external-verifier"
    return str(constraints.get("execution_evidence_mode", "runtime-metadata"))


def doctor() -> dict[str, Any]:
    """Report routing health separately from catalog-experiment health.

    ``routing_status`` answers "may workflow phases be routed right now"; it is
    the only signal ``resolve-phase`` and the workflow dispatcher admit on.
    ``evaluation_status`` answers "is the catalog-experiment lane clean" and
    never blocks routing. A live curated catalog that has moved ahead of the
    refreshed baseline is ``CATALOG_STALE`` maintenance, not a routing outage.
    """
    policy = load_policy()
    routing_issues: list[str] = []
    evaluation_issues: list[str] = []
    refresh_required = False
    catalog_status = "HEALTHY"
    if JOURNAL_PATH.exists():
        routing_issues.append("an incomplete activation journal exists")
    if CATALOG_REFRESH_JOURNAL_PATH.exists():
        evaluation_issues.append("an incomplete catalog refresh journal exists")
    constraints = policy.get("constraints", {})
    allowed_service_tiers = constraints.get("allowed_service_tiers")
    default_service_tier = constraints.get("default_service_tier")
    if (
        not isinstance(allowed_service_tiers, list)
        or not allowed_service_tiers
        or any(
            not isinstance(value, str) or not value
            for value in allowed_service_tiers
        )
        or len(allowed_service_tiers) != len(set(allowed_service_tiers))
    ):
        routing_issues.append("allowed service tiers are missing or invalid")
    elif default_service_tier not in allowed_service_tiers:
        routing_issues.append("default service tier is not allowed by policy")
    evidence_mode = execution_evidence_mode(policy)
    if evidence_mode == "runtime-metadata":
        pass
    elif evidence_mode == "external-verifier":
        if not attestation_verifier_ready(policy):
            evaluation_issues.append(
                "configured attestation verifier is missing or changed"
            )
    else:
        evaluation_issues.append(
            f"unsupported execution evidence mode: {evidence_mode}"
        )
    expected = bundle_hashes(rendered_active_bundle(policy))
    actual = installed_bundle_hashes()
    if expected != actual:
        routing_issues.append("installed profiles do not match the active policy")
    if CATALOG_PATH.exists():
        catalog = read_json(CATALOG_PATH)
        try:
            live_catalog = read_catalog_snapshot()
        except RouterLabError as error:
            catalog_status = "UNAVAILABLE"
            evaluation_issues.append(f"live model catalog is unavailable: {error}")
        else:
            if live_catalog.get("semantic_hash") != catalog.get("semantic_hash"):
                refresh_required = True
                catalog_status = "STALE"
        for tier in VALID_TIERS:
            assignment = policy["tiers"][tier]
            model = catalog.get("models", {}).get(assignment["model"])
            if not model:
                routing_issues.append(
                    f"{tier} model is absent from the refreshed catalog"
                )
            else:
                eligible, reasons = tier_eligibility(
                    policy, model, tier, assignment["effort"]
                )
                if not eligible:
                    routing_issues.append(
                        f"{tier} assignment is ineligible: {'; '.join(reasons)}"
                    )
            for fallback in assignment.get("fallbacks", []):
                fallback_model = catalog.get("models", {}).get(fallback.get("model"))
                if fallback_model is None:
                    routing_issues.append(
                        f"{tier} fallback is absent: {fallback.get('model')}"
                    )
                    continue
                eligible, reasons = tier_eligibility(
                    policy, fallback_model, tier, fallback.get("effort")
                )
                if not eligible:
                    routing_issues.append(
                        f"{tier} fallback {fallback.get('model')} is ineligible: "
                        + "; ".join(reasons)
                    )
    else:
        catalog_status = "MISSING"
        routing_issues.append("catalog baseline is missing")
    for candidate in load_candidates()["items"]:
        if candidate.get("status") == "promoted" and CATALOG_PATH.exists():
            promoted_issues = promoted_candidate_freshness_reasons(
                candidate, policy, read_json(CATALOG_PATH)
            )
            routing_issues.extend(
                f"promoted candidate {candidate['candidate_id']}: {reason}"
                for reason in promoted_issues
            )
            continue
        if candidate.get("status") not in {
            "staged",
            "collecting",
            "blocked_model_enforcement",
            "qualified",
        }:
            continue
        if CATALOG_PATH.exists():
            freshness = candidate_freshness_reasons(
                candidate, policy, read_json(CATALOG_PATH)
            )
            if refresh_required:
                freshness = [
                    reason
                    for reason in freshness
                    if reason
                    != "live model catalog differs from the refreshed baseline"
                ]
            evaluation_issues.extend(
                f"candidate {candidate['candidate_id']}: {reason}"
                for reason in freshness
            )
        path = AGENTS_DIR / candidate["candidate_profile_file"]
        candidate_actual = (
            sha256_bytes(path.read_bytes())
            if path.exists() and path.is_file()
            else None
        )
        if candidate_actual != candidate.get("candidate_profile_sha256"):
            evaluation_issues.append(
                f"candidate profile drift: {candidate['candidate_id']}"
            )
    issues = routing_issues + evaluation_issues
    routing_status = "DEGRADED" if routing_issues else "HEALTHY"
    evaluation_status = "DEGRADED" if evaluation_issues else "HEALTHY"
    status = (
        "DEGRADED"
        if issues
        else ("CATALOG_STALE" if refresh_required else "HEALTHY")
    )
    return {
        "status": status,
        "policy_id": policy["policy_id"],
        "issues": issues,
        "routing_issues": routing_issues,
        "evaluation_issues": evaluation_issues,
        "routing_status": routing_status,
        "evaluation_status": evaluation_status,
        "catalog_status": catalog_status,
        "workflow_routing_ready": routing_status == "HEALTHY",
        "refresh_required": refresh_required,
        "expected_profiles": expected,
        "installed_profiles": actual,
    }


def read_phase_request(request_value: str) -> dict[str, Any]:
    if request_value == "-":
        raw = sys.stdin.read()
    else:
        request_path = Path(request_value).expanduser()
        if request_path.is_symlink() or not request_path.is_file():
            raise RouterLabError("phase request is missing or unsafe")
        raw = request_path.read_text(encoding="utf-8")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise RouterLabError("phase request must be a JSON object")
    return request


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
    if (
        type(request.get("workflow_version")) is not int
        or request["workflow_version"] < 1
    ):
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
    if not isinstance(risk_tags, list) or any(
        not isinstance(tag, str) for tag in risk_tags
    ):
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

    tier = INDEX_TIER[selected_index]
    policy = load_policy()
    health = doctor()
    if health["routing_status"] != "HEALTHY":
        raise RouterLabError(
            "active routing is not ready: "
            + "; ".join(health.get("routing_issues", []))
        )
    parent_gate_required = bool(
        (external_action and activity != "prepare_external")
        or mutation == "irreversible"
        or (
            risk_scope == "mutation"
            and set(risk_tags) & {"production", "data_loss", "release"}
        )
    )
    result: dict[str, Any] = {
        "schema_version": 1,
        "workflow_id": request.get("workflow_id"),
        "workflow_version": request.get("workflow_version"),
        "phase_id": request.get("phase_id"),
        "mode": "deterministic" if tier == "T0" else "model",
        "tier": tier,
        "policy_id": policy["policy_id"],
        "reason_codes": reasons,
        "parent_gate_required": parent_gate_required,
        "external_mutation_authorized": False,
        "web_required": bool(current_info_required),
        "visual_required": bool(visual_required),
        "router_health": {
            "routing_status": health["routing_status"],
            "evaluation_status": health["evaluation_status"],
            "catalog_status": health["catalog_status"],
            "maintenance_issues": health.get("evaluation_issues", []),
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
    profile_path = AGENTS_DIR / assignment["profile_file"]
    if profile_path.is_symlink() or not profile_path.is_file():
        raise RouterLabError(f"active profile is missing or unsafe: {profile_path}")
    result.update(
        {
            "agent": assignment["agent"],
            "provider": policy.get("constraints", {}).get(
                "allowed_provider", "anthropic"
            ),
            "service_tier": policy.get("constraints", {}).get(
                "default_service_tier", "default"
            ),
            "model": assignment["model"],
            "effort": assignment["effort"],
            "profile_file": assignment["profile_file"],
            "profile_sha256": sha256_bytes(profile_path.read_bytes()),
            "execution_identity": (
                "REQUESTED_PENDING_RUNTIME_METADATA"
                if execution_evidence_mode(policy) == "runtime-metadata"
                else "REQUESTED_NOT_ATTESTED"
            ),
            "runtime_evidence_required": True,
            "runtime_attestation_required": (
                execution_evidence_mode(policy) == "external-verifier"
            ),
        }
    )
    return result


def status_report() -> dict[str, Any]:
    policy = load_policy()
    catalog = read_json(CATALOG_PATH) if CATALOG_PATH.exists() else None
    candidates = load_candidates()["items"]
    _status_health = doctor()
    return {
        "policy_id": policy["policy_id"],
        "revision": policy["revision"],
        "source": policy["source"],
        "attestation_ready": attestation_verifier_ready(policy),
        "tiers": {
            tier: {
                "agent": policy["tiers"][tier]["agent"],
                "model": policy["tiers"][tier]["model"],
                "effort": policy["tiers"][tier]["effort"],
            }
            for tier in VALID_TIERS
        },
        "catalog": None
        if catalog is None
        else {
            "semantic_hash": catalog["semantic_hash"],
            "captured_at": catalog.get("captured_at"),
            "source": catalog.get("source_kind"),
            "visible_models": sorted(
                slug
                for slug, model in catalog["models"].items()
                if model.get("visible") and model.get("selectable")
            ),
        },
        "candidates": [
            {
                "candidate_id": item["candidate_id"],
                "tier": item["tier"],
                "model": item["candidate_model"],
                "effort": item["candidate_effort"],
                "status": item["status"],
                "agent": item["candidate_agent"],
            }
            for item in candidates
        ],
        "doctor": _status_health["status"],
        "routing_status": _status_health["routing_status"],
        "evaluation_status": _status_health["evaluation_status"],
        "catalog_status": _status_health["catalog_status"],
        "workflow_routing_ready": _status_health["workflow_routing_ready"],
        "routing_issues": _status_health["routing_issues"],
        "evaluation_issues": _status_health["evaluation_issues"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    refresh = subparsers.add_parser(
        "refresh", help="Refresh the semantic model catalog."
    )
    refresh.add_argument(
        "--stage-new", action="store_true", help="Stage eligible new or changed models."
    )

    stage = subparsers.add_parser(
        "stage", help="Stage one explicit model/tier challenger."
    )
    stage.add_argument("--model", required=True)
    stage.add_argument("--tier", required=True, choices=VALID_TIERS)

    restart = subparsers.add_parser(
        "restart", help="Supersede a contaminated experiment with a clean candidate ID."
    )
    restart.add_argument("--candidate-id", required=True)
    restart.add_argument("--reason", required=True)

    record = subparsers.add_parser(
        "record", help="Append one measured arm observation."
    )
    record.add_argument("--candidate-id", required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--case-id", required=True)
    record.add_argument("--family")
    record.add_argument("--holdout", action="store_true")
    record.add_argument(
        "--arm", required=True, choices=("incumbent", "candidate", "control", "active")
    )
    record.add_argument("--enforced-model", required=True)
    record.add_argument("--enforced-effort", required=True)
    record.add_argument("--actual-provider", required=True)
    record.add_argument("--routing-verified", action="store_true")
    record.add_argument("--attestation-file")
    record.add_argument("--profile-sha256", required=True)
    record.add_argument("--grader-kind", required=True)
    record.add_argument("--grader-id-sha256", required=True)
    record.add_argument("--input-manifest-sha256", required=True)
    record.add_argument("--evidence-sha256", required=True)
    record.add_argument("--risk-floor-met", action="store_true")
    record.add_argument("--unauthorized-mutation", action="store_true")
    record.add_argument("--secret-exposure", action="store_true")
    record.add_argument("--unsupported-routing", action="store_true")
    verdict = record.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--passed", action="store_true")
    verdict.add_argument("--failed", action="store_true")
    record.add_argument("--quality-score", required=True, type=float)
    record.add_argument("--critical-failure", action="store_true")
    record.add_argument("--latency-ms", type=int)
    record.add_argument("--tokens", type=int)
    record.add_argument("--cost-micros", type=int)
    record.add_argument("--cost-source", choices=("billing-receipt",))
    record.add_argument("--escalations", type=int, default=0)

    evaluate = subparsers.add_parser("evaluate", help="Evaluate one staged challenger.")
    evaluate.add_argument("--candidate-id", required=True)
    evaluate.add_argument("--promote-qualified", action="store_true")

    promote = subparsers.add_parser(
        "promote", help="Activate one qualified challenger."
    )
    promote.add_argument("--candidate-id", required=True)

    block = subparsers.add_parser(
        "block", help="Record that exact model/effort enforcement is unavailable."
    )
    block.add_argument("--candidate-id", required=True)
    block.add_argument("--reason", required=True)
    block.add_argument("--fingerprint")

    monitor = subparsers.add_parser(
        "monitor", help="Evaluate post-promotion canary evidence."
    )
    monitor.add_argument("--candidate-id", required=True)
    monitor.add_argument(
        "--dry-run",
        action="store_true",
        help="Report a regression without enforcing the bound rollback snapshot.",
    )

    sync = subparsers.add_parser(
        "sync-agents", help="Render the active policy profiles."
    )
    sync.add_argument("--force", action="store_true")

    configure = subparsers.add_parser(
        "configure-attestation", help="Pin a trusted runtime-attestation verifier."
    )
    configure.add_argument("--verifier", required=True)
    configure.add_argument("--sha256", required=True)

    resolve = subparsers.add_parser(
        "resolve-phase",
        help="Resolve one workflow phase through the active routing policy.",
    )
    resolve.add_argument(
        "--request",
        required=True,
        help="JSON request path, or '-' to read the request from standard input.",
    )

    subparsers.add_parser("rollback", help="Restore the latest exact policy snapshot.")
    subparsers.add_parser("recover", help="Recover an interrupted profile activation.")
    subparsers.add_parser(
        "doctor", help="Verify catalog, policy, and profile consistency."
    )
    subparsers.add_parser("status", help="Show the active policy and experiments.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        with lab_lock():
            ensure_state()
            if (
                JOURNAL_PATH.exists() or CATALOG_REFRESH_JOURNAL_PATH.exists()
            ) and args.command not in {"recover", "doctor"}:
                raise RouterLabError(
                    "an interrupted router transaction must be recovered before this command"
                )
            if args.command == "refresh":
                result = refresh_catalog(args.stage_new)
            elif args.command == "stage":
                result = stage_candidate(args.model, args.tier)
            elif args.command == "restart":
                result = restart_candidate(args.candidate_id, args.reason)
            elif args.command == "record":
                args.passed = bool(args.passed and not args.failed)
                result = record_observation(args)
            elif args.command == "evaluate":
                result = evaluate_candidate(args.candidate_id, args.promote_qualified)
            elif args.command == "promote":
                result = promote_candidate(args.candidate_id)
            elif args.command == "block":
                result = block_candidate(
                    args.candidate_id, args.reason, args.fingerprint
                )
            elif args.command == "monitor":
                result = monitor_candidate(args.candidate_id, args.dry_run)
            elif args.command == "sync-agents":
                sync_agents(args.force)
                result = {"status": "SYNCED"}
            elif args.command == "configure-attestation":
                result = configure_attestation(args.verifier, args.sha256)
            elif args.command == "resolve-phase":
                result = resolve_phase(read_phase_request(args.request))
            elif args.command == "rollback":
                result = rollback_latest()
            elif args.command == "recover":
                result = recover_activation()
            elif args.command == "doctor":
                result = doctor()
            elif args.command == "status":
                result = status_report()
            else:
                raise RouterLabError(f"unknown command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RouterLabError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"router-lab error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
