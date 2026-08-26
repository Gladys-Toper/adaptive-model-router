#!/usr/bin/env python3
"""Platform-neutral, task-bound fixed token-cap contracts.

The cap contract is deliberately structured authority. Natural-language
explanations are neither parsed nor trusted: a contract is valid only when its
closed schema, self-hash, evidence hash, complete task binding, and four cap
values agree with the current dispatch packet.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

TOKEN_CAP_CONTRACT_NAME = "adaptive-workflow.fixed-token-cap"
TOKEN_CAP_CONTRACT_VERSION = 2
TOKEN_CAP_BINDING_EVIDENCE_NAME = "adaptive-workflow.fixed-token-cap-evidence"
TOKEN_CAP_BINDING_EVIDENCE_VERSION = 2
TOKEN_CAP_TASK_BINDING_NAME = "adaptive-workflow.fixed-token-cap-task-binding"
TOKEN_CAP_TASK_BINDING_VERSION = 1

_CAP_FIELDS = (
    "declared_token_cap",
    "derived_token_cap",
    "caller_requested_token_cap",
    "effective_token_cap",
)
_HEX_SHA256 = frozenset("0123456789abcdef")


class TokenCapContractError(ValueError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _HEX_SHA256 for character in value)
    )


def _cap_values(runtime_contract: dict[str, Any]) -> dict[str, int | None]:
    if not isinstance(runtime_contract, dict):
        raise TokenCapContractError("token-cap runtime contract is invalid")
    values = {field: runtime_contract.get(field) for field in _CAP_FIELDS}
    for field in ("declared_token_cap", "derived_token_cap", "effective_token_cap"):
        if type(values[field]) is not int or values[field] < 1:
            raise TokenCapContractError("token caps must be positive integers")
    requested = values["caller_requested_token_cap"]
    if requested is not None and (type(requested) is not int or requested < 1):
        raise TokenCapContractError("token caps must be positive integers")
    expected_effective = min(
        values["declared_token_cap"],
        values["derived_token_cap"],
        requested if requested is not None else values["declared_token_cap"],
    )
    if values["effective_token_cap"] != expected_effective:
        raise TokenCapContractError("effective token cap does not preserve the lower-cap rule")
    return values


def parse_phase_token_caps(values: list[str]) -> dict[str, int]:
    caps: dict[str, int] = {}
    for value in values:
        if value.count("=") != 1:
            raise TokenCapContractError("phase-token-cap must use phase-key=positive-integer")
        phase_key, raw_cap = value.split("=", 1)
        if not phase_key or not raw_cap.isdecimal() or int(raw_cap) < 1 or phase_key in caps:
            raise TokenCapContractError("phase-token-cap must be positive and unique per phase")
        caps[phase_key] = int(raw_cap)
    return caps


def declared_token_budget(
    phase_key: str, tier: str, defaults: dict[str, int], explicit_caps: dict[str, int]
) -> dict[str, Any]:
    if tier not in defaults or type(defaults[tier]) is not int or defaults[tier] < 1:
        raise TokenCapContractError(f"model phase has no declared token-cap tier: {phase_key}")
    explicit = explicit_caps.get(phase_key)
    return {
        "schema_version": 1,
        "declared_token_cap": explicit if explicit is not None else defaults[tier],
        "declaration_source": "plan.phase-token-cap" if explicit is not None else "planner.route-default",
    }


def effective_token_cap(declared: int, derived: int, requested: int | None) -> int:
    if (
        type(declared) is not int
        or type(derived) is not int
        or (requested is not None and type(requested) is not int)
    ):
        raise TokenCapContractError("token caps must be positive integers")
    effective = min(declared, derived, requested if requested is not None else declared)
    return _cap_values(
        {
            "declared_token_cap": declared,
            "derived_token_cap": derived,
            "caller_requested_token_cap": requested,
            "effective_token_cap": effective,
        }
    )["effective_token_cap"]  # type: ignore[return-value]


def token_cap_binding(
    *,
    plan_id: str,
    phase_key: str,
    phase_contract_sha256: str,
    route_identity: dict[str, Any],
    prompt_sha256: str,
    context_files: list[dict[str, Any]],
    context_bundle: dict[str, Any],
    cwd: str,
    source_commit: str,
    runtime_contract: dict[str, Any],
    dispatch_packet_binding_sha256: str,
) -> dict[str, Any]:
    cap_values = _cap_values(runtime_contract)
    return {
        "schema_version": 2,
        "contract_name": TOKEN_CAP_TASK_BINDING_NAME,
        "contract_version": TOKEN_CAP_TASK_BINDING_VERSION,
        "source_commit": source_commit,
        "plan_id": plan_id,
        "phase_key": phase_key,
        "phase_contract_sha256": phase_contract_sha256,
        "route_identity": route_identity,
        "prompt_sha256": prompt_sha256,
        "context_files": context_files,
        "context_bundle": context_bundle,
        "cwd": cwd,
        "runtime_cap_limits": cap_values,
        "dispatch_packet_binding_sha256": dispatch_packet_binding_sha256,
    }


def _validate_task_binding(task_binding: dict[str, Any]) -> dict[str, int | None]:
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "source_commit",
        "plan_id",
        "phase_key",
        "phase_contract_sha256",
        "route_identity",
        "prompt_sha256",
        "context_files",
        "context_bundle",
        "cwd",
        "runtime_cap_limits",
        "dispatch_packet_binding_sha256",
    }
    if not isinstance(task_binding, dict) or set(task_binding) != fields:
        raise TokenCapContractError("fixed token-cap task binding is missing or has unknown fields")
    if (
        task_binding.get("schema_version") != 2
        or task_binding.get("contract_name") != TOKEN_CAP_TASK_BINDING_NAME
        or task_binding.get("contract_version") != TOKEN_CAP_TASK_BINDING_VERSION
    ):
        raise TokenCapContractError("fixed token-cap task binding version is unsupported")
    for field in ("source_commit", "plan_id", "phase_key", "cwd"):
        if not isinstance(task_binding.get(field), str) or not task_binding[field]:
            raise TokenCapContractError("fixed token-cap task binding has an invalid identity field")
    for field in (
        "phase_contract_sha256",
        "prompt_sha256",
        "dispatch_packet_binding_sha256",
    ):
        if not _is_sha256(task_binding.get(field)):
            raise TokenCapContractError("fixed token-cap task binding has an invalid hash")
    if (
        not isinstance(task_binding.get("route_identity"), dict)
        or not task_binding["route_identity"]
        or not isinstance(task_binding.get("context_files"), list)
        or not isinstance(task_binding.get("context_bundle"), dict)
    ):
        raise TokenCapContractError("fixed token-cap task binding has invalid route or context evidence")
    for context in task_binding["context_files"]:
        if (
            not isinstance(context, dict)
            or set(context) != {"path", "sha256", "bytes"}
            or not isinstance(context.get("path"), str)
            or not context["path"]
            or not _is_sha256(context.get("sha256"))
            or type(context.get("bytes")) is not int
            or context["bytes"] < 0
        ):
            raise TokenCapContractError("fixed token-cap task binding has invalid context evidence")
    return _cap_values(task_binding["runtime_cap_limits"])


def token_cap_evidence_path(contract_path: Path) -> Path:
    return contract_path.with_name(contract_path.name + ".evidence.json")


def build_token_cap_evidence(
    task_binding: dict[str, Any], runtime_contract: dict[str, Any]
) -> dict[str, Any]:
    cap_values = _cap_values(runtime_contract)
    binding_caps = _validate_task_binding(task_binding)
    if binding_caps != cap_values:
        raise TokenCapContractError("token-cap task binding differs from runtime cap contract")
    identity = {
        "schema_version": 2,
        "contract_name": TOKEN_CAP_BINDING_EVIDENCE_NAME,
        "contract_version": TOKEN_CAP_BINDING_EVIDENCE_VERSION,
        "task_binding": task_binding,
        "task_binding_sha256": content_hash(task_binding),
        **cap_values,
    }
    return {**identity, "evidence_sha256": content_hash(identity)}


def build_token_cap_contract(
    task_binding: dict[str, Any], runtime_contract: dict[str, Any], evidence_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    cap_values = _cap_values(runtime_contract)
    evidence = build_token_cap_evidence(task_binding, runtime_contract)
    evidence_raw = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
    identity = {
        "schema_version": 2,
        "contract_name": TOKEN_CAP_CONTRACT_NAME,
        "contract_version": TOKEN_CAP_CONTRACT_VERSION,
        "task_binding": task_binding,
        "task_binding_sha256": content_hash(task_binding),
        **cap_values,
        "token_cap": cap_values["effective_token_cap"],
        "measured_safe_evidence_path": str(evidence_path),
        "measured_safe_evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
        "evidence_sha256": evidence["evidence_sha256"],
    }
    return {**identity, "contract_sha256": content_hash(identity)}, evidence


def validate_token_cap_contract(
    contract: dict[str, Any],
    *,
    task_binding: dict[str, Any],
    runtime_contract: dict[str, Any],
    evidence: dict[str, Any],
    evidence_raw_sha256: str,
) -> None:
    """Validate cap evidence against this exact bind/run input.

    A self-rehashed contract/evidence pair is insufficient: both copies of the
    complete task binding and all four cap values must equal the binding freshly
    derived from the current packet. Callers supply the raw evidence-file digest
    after path-safe loading, so the contract cannot point at another byte stream.
    """
    if not isinstance(contract, dict) or not isinstance(evidence, dict):
        raise TokenCapContractError("fixed token-cap contract or evidence is invalid")
    expected_caps = _cap_values(runtime_contract)
    binding_caps = _validate_task_binding(task_binding)
    if binding_caps != expected_caps:
        raise TokenCapContractError("token-cap task binding differs from runtime cap contract")
    expected_binding_hash = content_hash(task_binding)
    contract_fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "task_binding",
        "task_binding_sha256",
        *_CAP_FIELDS,
        "token_cap",
        "measured_safe_evidence_path",
        "measured_safe_evidence_sha256",
        "evidence_sha256",
        "contract_sha256",
    }
    contract_identity = {
        field: contract.get(field) for field in contract_fields if field != "contract_sha256"
    }
    if (
        set(contract) != contract_fields
        or contract.get("schema_version") != 2
        or contract.get("contract_name") != TOKEN_CAP_CONTRACT_NAME
        or contract.get("contract_version") != TOKEN_CAP_CONTRACT_VERSION
        or contract.get("task_binding") != task_binding
        or contract.get("task_binding_sha256") != expected_binding_hash
        or any(contract.get(field) != expected_caps[field] for field in _CAP_FIELDS)
        or contract.get("token_cap") != expected_caps["effective_token_cap"]
        or not isinstance(contract.get("measured_safe_evidence_path"), str)
        or not contract["measured_safe_evidence_path"]
        or not _is_sha256(contract.get("measured_safe_evidence_sha256"))
        or not _is_sha256(contract.get("evidence_sha256"))
        or contract.get("contract_sha256") != content_hash(contract_identity)
    ):
        raise TokenCapContractError("fixed token-cap contract is missing, mismatched, or self-hash invalid")
    evidence_fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "task_binding",
        "task_binding_sha256",
        *_CAP_FIELDS,
        "evidence_sha256",
    }
    evidence_identity = {
        field: evidence.get(field) for field in evidence_fields if field != "evidence_sha256"
    }
    if (
        set(evidence) != evidence_fields
        or evidence.get("schema_version") != 2
        or evidence.get("contract_name") != TOKEN_CAP_BINDING_EVIDENCE_NAME
        or evidence.get("contract_version") != TOKEN_CAP_BINDING_EVIDENCE_VERSION
        or evidence.get("task_binding") != task_binding
        or evidence.get("task_binding_sha256") != expected_binding_hash
        or any(evidence.get(field) != expected_caps[field] for field in _CAP_FIELDS)
        or evidence.get("evidence_sha256") != content_hash(evidence_identity)
        or contract.get("evidence_sha256") != evidence.get("evidence_sha256")
        or contract.get("measured_safe_evidence_sha256") != evidence_raw_sha256
    ):
        raise TokenCapContractError("fixed token-cap evidence is missing or does not bind this task")
