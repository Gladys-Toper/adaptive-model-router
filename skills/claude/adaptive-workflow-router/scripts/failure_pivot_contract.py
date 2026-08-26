#!/usr/bin/env python3
"""Platform-neutral, tamper-evident terminal-failure pivot contracts.

This module is deliberately pure. Dispatch adapters collect and re-check the
referenced files; this contract derives the only permitted coordinator action.
A T4 direction is never write authority: it can only produce a separately
validated, bounded T3 mutation contract.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import PurePath
from typing import Any

CONTROL_RETURN_NAME = "adaptive-workflow.control-return"
CONTROL_RETURN_VERSION = 2
PIVOT_NAME = "adaptive-workflow.pivot-from-receipt"
PIVOT_VERSION = 2
T4_DIRECTION_NAME = "adaptive-workflow.t4-direction-to-t3-mutation"
T4_DIRECTION_VERSION = 2
T4_RESULT_NAME = "adaptive-workflow.t4-result"
T4_RESULT_VERSION = 1
FAILURE_CLASSES = frozenset({"completed", "deterministic_setup_failure", "transient_failure", "grounded_cognitive_failure", "exit_gate_failure", "authority_required", "budget_or_context_exhaustion", "model_enforcement_failure", "harness_unavailable", "non_resumable_failure"})
RECOMMENDED_ACTIONS = frozenset({"t0_repair", "retry_same_route", "escalate", "ask_user", "partition", "resume", "abort"})
MODEL_TIERS = frozenset({"T0", "T1", "T2", "T3", "T4"})
_HEX = frozenset("0123456789abcdef")
# A consultation is one offline model request.  Diagnosis permits one initial
# request plus one bounded continuation for each of its eight read-only tool
# calls; `model_cycle_cap` remains one because the whole exchange is one T4
# adjudication, not an opportunity to start another model phase.
_T4_CONSULT_CAPS = {"token_cap": 16_000, "model_cycle_cap": 1, "tool_cycle_cap": 0, "api_call_cap": 1, "wall_time_seconds": 300}
_T4_DIAGNOSE_CAPS = {"token_cap": 24_000, "model_cycle_cap": 1, "tool_cycle_cap": 8, "api_call_cap": 9, "wall_time_seconds": 600}


class FailurePivotContractError(ValueError):
    """A receipt, pivot, or bounded re-entry contract is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _is_commit(value: Any) -> bool:
    return isinstance(value, str) and len(value) in {40, 64} and set(value) <= _HEX


def _is_absolute_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value and PurePath(value).is_absolute()


def _is_safe_relative_path(value: Any) -> bool:
    path = PurePath(value) if isinstance(value, str) and value else None
    return bool(path) and not path.is_absolute() and ".." not in path.parts and "." not in path.parts and "\x00" not in value


def _is_safe_mutation_scope_path(value: Any) -> bool:
    """A repository-relative write scope may never cover Git metadata."""
    path = PurePath(value) if _is_safe_relative_path(value) else None
    return bool(path) and path.parts[0] != ".git"


def _scope_is_within(candidate: str, authorized: str) -> bool:
    """Return whether one exact/descendant path stays within a sealed root."""
    return candidate == authorized or candidate.startswith(authorized + "/")


def _self_hashed(value: dict[str, Any], field: str) -> dict[str, Any]:
    return {**value, field: content_hash(value)}


def _validate_self_hash(value: Any, field: str, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FailurePivotContractError(f"{label} is invalid")
    bare = dict(value)
    supplied = bare.pop(field, None)
    if not _is_hash(supplied) or supplied != content_hash(bare):
        raise FailurePivotContractError(f"{label} checksum mismatch")
    return bare


def normalize_failure_class(terminal_status: str, terminal_category: str | None, issues: list[str] | None = None) -> str:
    if terminal_status == "COMPLETED":
        return "completed"
    category = terminal_category.upper() if terminal_category is not None else None
    if category in {"USAGE_ERROR", "SETUP_FAILURE", "CHECKPOINT_COMMIT_FAILURE"}:
        return "deterministic_setup_failure"
    if category in {"TRANSIENT_FAILURE", "NETWORK_FAILURE", "TOOL_TRANSIENT_FAILURE"}:
        return "transient_failure"
    if category in {"EXIT_GATE_FAILURE", "QUALITY_GATE_FAILURE"}:
        return "exit_gate_failure"
    if category in {"PARENT_OR_USER_AUTHORITY_REQUIRED", "PARENT_GATE_REQUIRED", "EXTERNAL_ACTION_REQUIRED", "IRREVERSIBLE_ACTION_REQUIRED", "USER_INPUT_REQUIRED"}:
        return "authority_required"
    if category in {"BUDGET_STOP", "BUDGET_OR_CONTEXT_EXHAUSTION", "CONTEXT_EXHAUSTION"}:
        return "budget_or_context_exhaustion"
    if category in {"BLOCKED_MODEL_ENFORCEMENT", "MODEL_OR_RUNTIME_REJECTION", "MODEL_ENFORCEMENT_FAILURE"}:
        return "model_enforcement_failure"
    if category in {"HARNESS_UNAVAILABLE", "HARNESS_INCIDENT"}:
        return "harness_unavailable"
    if category in {"GROUNDED_COGNITIVE_FAILURE", "COGNITIVE_FAILURE"}:
        return "grounded_cognitive_failure"
    # Retained terminal categories are authoritative.  Issue prose is only a
    # compatibility fallback for historical terminals that did not record a
    # category; it must never override either a recognized category or an
    # explicit unknown category.
    if terminal_category is None:
        text = " ".join(issues or []).lower()
        if "cap exceeded" in text or ("context" in text and "exhaust" in text):
            return "budget_or_context_exhaustion"
    return "non_resumable_failure"


def _legacy_recommended_action(terminal_status: str, terminal_category: str | None) -> str:
    """Rebuild the deterministic recommendation emitted by the v1 dispatcher."""
    if terminal_status == "COMPLETED":
        return "t0_only"
    if terminal_category in {
        "PARENT_OR_USER_AUTHORITY_REQUIRED",
        "PARENT_GATE_REQUIRED",
        "EXTERNAL_ACTION_REQUIRED",
        "IRREVERSIBLE_ACTION_REQUIRED",
        "USER_INPUT_REQUIRED",
    }:
        return "ask_or_advise_user"
    return "start_adaptive_workflow"


def recommended_action(failure_class: str) -> str:
    actions = {"completed": "abort", "deterministic_setup_failure": "t0_repair", "transient_failure": "retry_same_route", "grounded_cognitive_failure": "escalate", "exit_gate_failure": "escalate", "authority_required": "ask_user", "budget_or_context_exhaustion": "partition", "model_enforcement_failure": "abort", "harness_unavailable": "abort", "non_resumable_failure": "abort"}
    try:
        return actions[failure_class]
    except KeyError as error:
        raise FailurePivotContractError("unknown failure class") from error


def _validate_references(references: Any) -> list[dict[str, str]]:
    if not isinstance(references, list):
        raise FailurePivotContractError("evidence references are invalid")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for reference in references:
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise FailurePivotContractError("evidence reference is invalid")
        path, digest = reference.get("path"), reference.get("sha256")
        if not isinstance(path, str) or not path or path in seen or not _is_hash(digest):
            raise FailurePivotContractError("evidence reference is invalid")
        seen.add(path)
        normalized.append({"path": path, "sha256": digest})
    return normalized


def _normalize_worktree_evidence(value: dict[str, Any] | None) -> dict[str, Any]:
    value = value or {}
    # Keep the first v2 draft readable while new receipts retain both snapshots.
    if "admission" not in value and "final" not in value:
        flat = {key: value.get(key) for key in ("source_commit", "worktree", "status", "tracked_diff_sha256", "tool_visible_snapshot_sha256")}
        value = {"admission": flat, "final": flat, "allowed_mutation_scope": value.get("allowed_mutation_scope", "none")}
    if set(value) != {"admission", "final", "allowed_mutation_scope"} or value["allowed_mutation_scope"] not in {"none", "workspace-write"}:
        raise FailurePivotContractError("worktree evidence is not closed")
    normalized: dict[str, Any] = {"allowed_mutation_scope": value["allowed_mutation_scope"]}
    for label in ("admission", "final"):
        snapshot = value[label]
        if not isinstance(snapshot, dict) or set(snapshot) != {"source_commit", "worktree", "status", "tracked_diff_sha256", "tool_visible_snapshot_sha256"}:
            raise FailurePivotContractError("worktree snapshot is invalid")
        if not _is_commit(snapshot["source_commit"]) or not _is_absolute_path(snapshot["worktree"]) or snapshot["status"] not in {"clean", "dirty", "unknown"} or (snapshot["tracked_diff_sha256"] is not None and not _is_hash(snapshot["tracked_diff_sha256"])) or not _is_hash(snapshot["tool_visible_snapshot_sha256"]):
            raise FailurePivotContractError("worktree snapshot is invalid")
        normalized[label] = dict(snapshot)
    return normalized


def _validate_evidence_conflicts(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise FailurePivotContractError("evidence conflicts are invalid")
    normalized: list[dict[str, str]] = []
    for conflict in value:
        if not isinstance(conflict, dict) or set(conflict) != {"kind", "sha256"} or not isinstance(conflict["kind"], str) or not conflict["kind"] or not _is_hash(conflict["sha256"]):
            raise FailurePivotContractError("evidence conflict is invalid")
        normalized.append({"kind": conflict["kind"], "sha256": conflict["sha256"]})
    return normalized


def _required_terminal_evidence_name(terminal_status: str) -> str:
    """Select the one retained terminal artifact from the actual terminal state."""
    return "execution-receipt.json" if terminal_status == "COMPLETED" else "aborted.json"


def _unique_named_evidence(
    references: list[dict[str, str]], filename: str
) -> dict[str, str] | None:
    """Select one exact run artifact; duplicate terminal markers are ambiguous."""
    matches = [
        item for item in references
        if item["path"].rsplit("/", 1)[-1] == filename
    ]
    if len(matches) > 1:
        raise FailurePivotContractError("terminal evidence is ambiguous")
    return matches[0] if matches else None


def _evidence_state(
    references: list[dict[str, str]],
    worktree: dict[str, Any],
    conflicts: list[dict[str, str]],
    terminal_status: str,
) -> str:
    if conflicts:
        return "contradictory"
    names = {reference["path"].rsplit("/", 1)[-1] for reference in references}
    # A failure run normally retains `aborted.json`, not a contradictory pair
    # of an execution receipt and an abort marker.  The captured prompt makes
    # the failed reasoning path auditable as well as its terminal artifact.
    required = {
        "input-manifest.json",
        "execution-metadata.json",
        "prompt.txt",
        _required_terminal_evidence_name(terminal_status),
    }
    snapshots = (worktree["admission"], worktree["final"])
    exact_snapshots = all(snapshot["status"] != "unknown" and snapshot["tracked_diff_sha256"] is not None and _is_hash(snapshot["tool_visible_snapshot_sha256"]) and len(snapshot["source_commit"]) in {40, 64} for snapshot in snapshots)
    return "complete" if required <= names and exact_snapshots else "incomplete"


def _derive_evidence_root(references: list[dict[str, str]]) -> str:
    """Derive one lexical run root without touching the filesystem."""
    if not references:
        return "/"
    paths = [reference["path"] for reference in references]
    if any(not _is_absolute_path(path) for path in paths):
        raise FailurePivotContractError("evidence paths must be absolute")
    try:
        root = os.path.commonpath(paths)
    except ValueError as error:
        raise FailurePivotContractError("evidence paths do not share one root") from error
    # The common path can be one of the terminal files when the evidence set
    # has one member.  Its parent is then still a stable, retained run root.
    if len(paths) == 1:
        root = os.path.dirname(root)
    if not _is_absolute_path(root):
        raise FailurePivotContractError("evidence root is invalid")
    return root


def _validate_evidence_root(root: Any, references: list[dict[str, str]]) -> str:
    if not _is_absolute_path(root):
        raise FailurePivotContractError("evidence root is invalid")
    normalized = os.path.normpath(root)
    prefix = normalized if normalized == "/" else normalized + os.sep
    if any(reference["path"] != normalized and not reference["path"].startswith(prefix) for reference in references):
        raise FailurePivotContractError("evidence reference escapes its retained run root")
    return normalized


def _terminal_artifact_facts(
    *,
    terminal_status: str,
    terminal_category: str | None,
    execution_receipt_evidence: dict[str, str] | None,
    failure_evidence: dict[str, str] | None,
) -> dict[str, Any]:
    terminal_evidence_kind = _required_terminal_evidence_name(terminal_status)
    terminal_evidence = (
        execution_receipt_evidence
        if terminal_evidence_kind == "execution-receipt.json"
        else failure_evidence
    )
    return {
        "terminal_status": terminal_status,
        "terminal_category": terminal_category,
        "terminal_evidence_kind": terminal_evidence_kind,
        "terminal_evidence_sha256": terminal_evidence["sha256"] if terminal_evidence else None,
        "execution_receipt_sha256": (
            execution_receipt_evidence["sha256"] if execution_receipt_evidence else None
        ),
        "failure_evidence_sha256": failure_evidence["sha256"] if failure_evidence else None,
    }


def _same_run_evidence_sha256(
    *, evidence_root: str, references: list[dict[str, str]], terminal_artifact_facts: dict[str, Any], worktree: dict[str, Any]
) -> str:
    return content_hash({
        "evidence_root": evidence_root,
        "references": references,
        "terminal_artifact_facts": terminal_artifact_facts,
        "worktree_admission": worktree["admission"],
        "worktree_final": worktree["final"],
    })


def _closed_packet(kind: str, **payload: Any) -> dict[str, Any]:
    return _self_hashed({"schema_version": 1, "kind": kind, **payload}, "packet_sha256")


def _evidence_bundle(references: list[dict[str, str]], state: str) -> dict[str, Any]:
    return _self_hashed({"schema_version": 1, "completeness": state, "references": references}, "evidence_bundle_sha256")


def _lineage_identity(phase_key: str | None, worktree: dict[str, Any], plan_id: str, phase_contract_sha256: str) -> str:
    """Stable retry identity; terminal evidence itself intentionally changes."""
    return content_hash({
        "phase_key": phase_key,
        "plan_id": plan_id,
        "phase_contract_sha256": phase_contract_sha256,
        "source_commit": worktree["admission"]["source_commit"],
        "worktree": worktree["admission"]["worktree"],
        "allowed_mutation_scope": worktree["allowed_mutation_scope"],
    })


def _t4_scope(worktree: dict[str, Any]) -> dict[str, Any]:
    final = worktree["final"]
    return {"repository_path": final["worktree"], "path_scope": [final["worktree"]], "source_commit": worktree["admission"]["source_commit"]}


def _validate_t4_scope_and_evidence(scope: Any, evidence: Any) -> None:
    if not isinstance(scope, dict) or set(scope) != {"repository_path", "path_scope", "source_commit"} or not _is_absolute_path(scope["repository_path"]) or not isinstance(scope["path_scope"], list) or scope["path_scope"] != [scope["repository_path"]] or not _is_commit(scope["source_commit"]):
        raise FailurePivotContractError("T4 repository scope is invalid")
    bundle = _validate_self_hash(evidence, "evidence_bundle_sha256", "T4 evidence bundle")
    if set(bundle) != {"schema_version", "completeness", "references"} or bundle.get("schema_version") != 1 or bundle.get("completeness") not in {"complete", "incomplete", "contradictory"}:
        raise FailurePivotContractError("T4 evidence bundle is invalid")
    _validate_references(bundle["references"])


def _validate_packet(packet: Any) -> dict[str, Any]:
    bare = _validate_self_hash(packet, "packet_sha256", "pivot packet")
    if bare.get("schema_version") != 1 or not isinstance(bare.get("kind"), str):
        raise FailurePivotContractError("pivot packet schema is invalid")
    expected: dict[str, set[str]] = {
        "t0_repair": {"schema_version", "kind", "dispatch_permitted", "retry_limit", "repair_only", "prohibited"},
        "parent_harness_incident": {"schema_version", "kind", "dispatch_permitted", "reason"},
        "same_route_retry": {"schema_version", "kind", "dispatch_permitted", "retry_limit", "same_route", "identical_prompt_permitted"},
        "validated_checkpoint_resume": {"schema_version", "kind", "dispatch_permitted", "checkpoint_required", "same_route"},
        "parent_blocker": {"schema_version", "kind", "dispatch_permitted", "reason"},
        "adaptive_route_reassessment": {"schema_version", "kind", "dispatch_permitted", "escalate_one_tier", "forbid_identical_prompt"},
        "parent_or_user_question": {"schema_version", "kind", "dispatch_permitted", "question"},
        "partition_before_retry": {"schema_version", "kind", "dispatch_permitted", "budget_increase_permitted", "checkpoint_present"},
        "terminal_parent_blocker": {"schema_version", "kind", "dispatch_permitted"},
        "read_only_break_glass_consultation": {"schema_version", "kind", "dispatch_permitted", "tier", "read_only", "network_access", "mutation_authority", "required_reentry_tier", "evidence_bundle"},
        "t4_consult": {"schema_version", "kind", "dispatch_permitted", "tier", "sandbox", "network_access", "tool_mode", "mutation_authority", "repository_scope", "evidence_bundle", "limits", "required_return"},
        "t4_diagnose": {"schema_version", "kind", "dispatch_permitted", "tier", "sandbox", "network_access", "tool_mode", "mutation_authority", "repository_scope", "evidence_bundle", "limits", "required_return"},
    }
    kind = bare["kind"]
    if kind not in expected or set(bare) != expected[kind]:
        raise FailurePivotContractError("pivot packet is not closed")
    if kind.startswith("t4_") or kind == "read_only_break_glass_consultation":
        if bare.get("tier") != "T4" or bare.get("mutation_authority") is not False or bare.get("network_access") is not False:
            raise FailurePivotContractError("T4 pivot cannot authorize mutation or network")
    if kind in {"t4_consult", "t4_diagnose"}:
        caps = _T4_CONSULT_CAPS if kind == "t4_consult" else _T4_DIAGNOSE_CAPS
        if bare["dispatch_permitted"] is not True or bare["sandbox"] != "read-only" or bare["tool_mode"] != ("none" if kind == "t4_consult" else "read_only") or bare["limits"] != caps or bare["required_return"] != T4_DIRECTION_NAME:
            raise FailurePivotContractError("T4 packet caps or tool mode are invalid")
        _validate_t4_scope_and_evidence(bare["repository_scope"], bare["evidence_bundle"])
    return bare


def build_control_return(*, terminal_status: str, terminal_category: str | None, phase_key: str | None, resumable: bool, resume_checkpoint_present: bool, evidence_references: list[dict[str, str]], retry_escalation_count: int = 0, failed_exit_gate: str | None = None, issues: list[str] | None = None, failed_tier: str | None = None, worktree_evidence: dict[str, Any] | None = None, completed_checks: list[str] | None = None, parent_user_question: dict[str, Any] | None = None, evidence_conflicts: list[dict[str, str]] | None = None, lineage_plan_id: str = "unplanned", lineage_phase_contract_sha256: str | None = None, lineage_prompt_sha256: str | None = None, lineage_dispatch_packet_sha256: str | None = None, evidence_root: str | None = None) -> dict[str, Any]:
    if retry_escalation_count < 0 or (failed_tier is not None and failed_tier not in MODEL_TIERS):
        raise FailurePivotContractError("control-return retry count or tier is invalid")
    references, worktree, conflicts = _validate_references(evidence_references), _normalize_worktree_evidence(worktree_evidence), _validate_evidence_conflicts(evidence_conflicts)
    failure_class = normalize_failure_class(terminal_status, terminal_category, issues)
    checkpoint_evidence = [item for item in references if "resume-checkpoint." in item["path"]]
    evidence_state = _evidence_state(references, worktree, conflicts, terminal_status)
    route_request = _closed_packet("adaptive_route_reassessment", dispatch_permitted=True, escalate_one_tier=True, forbid_identical_prompt=True) if failure_class in {"grounded_cognitive_failure", "exit_gate_failure"} else None
    if failure_class == "authority_required" and parent_user_question is None:
        parent_user_question = _closed_packet("parent_or_user_question", dispatch_permitted=False, question="Approve, reject, or provide the required authority before any further dispatch.")
    if parent_user_question is not None:
        if _validate_packet(parent_user_question).get("kind") != "parent_or_user_question":
            raise FailurePivotContractError("authority question packet is invalid")
    normalized_issues = list(issues or [])
    if any(not isinstance(issue, str) for issue in normalized_issues):
        raise FailurePivotContractError("control-return issues are invalid")
    if not isinstance(lineage_plan_id, str) or not lineage_plan_id or (lineage_phase_contract_sha256 is not None and not _is_hash(lineage_phase_contract_sha256)) or (lineage_prompt_sha256 is not None and not _is_hash(lineage_prompt_sha256)) or (lineage_dispatch_packet_sha256 is not None and not _is_hash(lineage_dispatch_packet_sha256)):
        raise FailurePivotContractError("control-return lineage input is invalid")
    lineage_phase_contract_sha256 = lineage_phase_contract_sha256 or content_hash({"unplanned_phase": phase_key})
    lineage_prompt_sha256 = lineage_prompt_sha256 or content_hash({"unbound_prompt": phase_key})
    lineage_dispatch_packet_sha256 = lineage_dispatch_packet_sha256 or content_hash({"unbound_dispatch_packet": phase_key})
    evidence_root = _validate_evidence_root(evidence_root or _derive_evidence_root(references), references)
    execution_receipt_evidence = _unique_named_evidence(references, "execution-receipt.json")
    failure_evidence = _unique_named_evidence(references, "aborted.json")
    terminal_artifact_facts = _terminal_artifact_facts(
        terminal_status=terminal_status,
        terminal_category=terminal_category,
        execution_receipt_evidence=execution_receipt_evidence,
        failure_evidence=failure_evidence,
    )
    identity = {"schema_version": 2, "contract_name": CONTROL_RETURN_NAME, "contract_version": CONTROL_RETURN_VERSION, "terminal_status": terminal_status, "terminal_category": terminal_category, "failure_issues": normalized_issues, "failure_class": failure_class, "recommended_action": recommended_action(failure_class), "control_state": "OUTSIDE_ADAPTIVE_MODEL_EXECUTION", "allowed_parent_operations": ["pivot_from_receipt", "inspect_hash_bound_evidence"], "prohibited_parent_operations": ["continue_cognitive_work_in_inherited_parent_model", "unbounded_budget_increase", "unscoped_mutation", "repeat_identical_failed_prompt"], "retry_escalation_count": retry_escalation_count, "failed_phase": phase_key, "failed_tier": failed_tier, "failed_exit_gate": failed_exit_gate, "evidence_root": evidence_root, "evidence_references": references, "evidence_conflicts": conflicts, "evidence_completeness": evidence_state, "worktree_evidence": worktree, "lineage_plan_id": lineage_plan_id, "lineage_phase_contract_sha256": lineage_phase_contract_sha256, "lineage_prompt_sha256": lineage_prompt_sha256, "lineage_dispatch_packet_sha256": lineage_dispatch_packet_sha256, "lineage_identity_sha256": _lineage_identity(phase_key, worktree, lineage_plan_id, lineage_phase_contract_sha256), "completed_checks": completed_checks or [], "checkpoint_evidence": checkpoint_evidence, "execution_receipt_evidence": execution_receipt_evidence, "failure_evidence": failure_evidence, "terminal_artifact_facts": terminal_artifact_facts, "same_run_evidence_sha256": _same_run_evidence_sha256(evidence_root=evidence_root, references=references, terminal_artifact_facts=terminal_artifact_facts, worktree=worktree), "next_route_request": route_request, "parent_user_question": parent_user_question, "workflow_state": {"plan_bound": phase_key is not None, "phase_key": phase_key, "resumable": resumable, "resume_checkpoint_present": bool(checkpoint_evidence)}}
    return _self_hashed(identity, "directive_sha256")


def validate_control_return(directive: dict[str, Any]) -> None:
    if not isinstance(directive, dict):
        raise FailurePivotContractError("control-return is invalid")
    if directive.get("schema_version") == 1 and directive.get("contract_version") == 1:
        bare = _validate_self_hash(directive, "directive_sha256", "legacy control-return")
        expected = {
            "schema_version", "contract_name", "contract_version", "terminal_status",
            "terminal_category", "recommended_action", "control_state",
            "allowed_parent_operations", "prohibited_parent_operations", "workflow_state",
        }
        state = bare.get("workflow_state")
        if (
            set(bare) != expected
            or bare.get("schema_version") != 1
            or bare.get("contract_name") != CONTROL_RETURN_NAME
            or bare.get("contract_version") != 1
            or not isinstance(bare.get("terminal_status"), str)
            or not bare["terminal_status"]
            or (
                bare.get("terminal_category") is not None
                and not isinstance(bare["terminal_category"], str)
            )
            or bare.get("recommended_action") != _legacy_recommended_action(
                bare["terminal_status"], bare["terminal_category"]
            )
            or bare.get("control_state") != "OUTSIDE_ADAPTIVE_MODEL_EXECUTION"
            or bare.get("allowed_parent_operations")
            not in (
                ["t0_only"],
                ["t0_only", "ask_or_advise_user", "start_adaptive_workflow"],
            )
            or bare.get("prohibited_parent_operations")
            != ["continue_cognitive_work_in_inherited_parent_model"]
            or not isinstance(state, dict)
            or set(state)
            != {"plan_bound", "phase_key", "resumable", "resume_checkpoint_present"}
            or type(state.get("plan_bound")) is not bool
            or (
                state.get("phase_key") is not None
                and not isinstance(state["phase_key"], str)
            )
            or type(state.get("resumable")) is not bool
            or type(state.get("resume_checkpoint_present")) is not bool
            or state["plan_bound"] != (state["phase_key"] is not None)
            or (state["resume_checkpoint_present"] and not state["resumable"])
        ):
            raise FailurePivotContractError("legacy control-return is invalid")
        return
    bare = _validate_self_hash(directive, "directive_sha256", "control-return")
    expected = {"schema_version", "contract_name", "contract_version", "terminal_status", "terminal_category", "failure_issues", "failure_class", "recommended_action", "control_state", "allowed_parent_operations", "prohibited_parent_operations", "retry_escalation_count", "failed_phase", "failed_tier", "failed_exit_gate", "evidence_root", "evidence_references", "evidence_conflicts", "evidence_completeness", "worktree_evidence", "lineage_plan_id", "lineage_phase_contract_sha256", "lineage_prompt_sha256", "lineage_dispatch_packet_sha256", "lineage_identity_sha256", "completed_checks", "checkpoint_evidence", "execution_receipt_evidence", "failure_evidence", "terminal_artifact_facts", "same_run_evidence_sha256", "next_route_request", "parent_user_question", "workflow_state"}
    if set(bare) != expected or bare.get("schema_version") != 2 or bare.get("contract_name") != CONTROL_RETURN_NAME or bare.get("contract_version") != CONTROL_RETURN_VERSION or bare.get("failure_class") not in FAILURE_CLASSES or bare.get("recommended_action") != recommended_action(bare["failure_class"]):
        raise FailurePivotContractError("control-return schema is invalid")
    if not isinstance(bare["terminal_status"], str) or (bare["terminal_category"] is not None and not isinstance(bare["terminal_category"], str)) or not isinstance(bare["failure_issues"], list) or any(not isinstance(issue, str) for issue in bare["failure_issues"]) or type(bare["retry_escalation_count"]) is not int or bare["retry_escalation_count"] < 0 or bare["failed_tier"] not in (None, *MODEL_TIERS) or bare["evidence_completeness"] not in {"complete", "incomplete", "contradictory"} or not isinstance(bare["completed_checks"], list):
        raise FailurePivotContractError("control-return fields are invalid")
    references, worktree, conflicts = _validate_references(bare["evidence_references"]), _normalize_worktree_evidence(bare["worktree_evidence"]), _validate_evidence_conflicts(bare["evidence_conflicts"])
    evidence_root = _validate_evidence_root(bare["evidence_root"], references)
    if not isinstance(bare["lineage_plan_id"], str) or not bare["lineage_plan_id"] or not _is_hash(bare["lineage_phase_contract_sha256"]) or not _is_hash(bare["lineage_prompt_sha256"]) or not _is_hash(bare["lineage_dispatch_packet_sha256"]) or bare["lineage_identity_sha256"] != _lineage_identity(bare["failed_phase"], worktree, bare["lineage_plan_id"], bare["lineage_phase_contract_sha256"]):
        raise FailurePivotContractError("control-return lineage identity is invalid")
    if bare["evidence_completeness"] != _evidence_state(
        references, worktree, conflicts, bare["terminal_status"]
    ):
        raise FailurePivotContractError("control-return evidence completeness is forged")
    if bare["failure_class"] != normalize_failure_class(
        bare["terminal_status"], bare["terminal_category"], bare["failure_issues"]
    ):
        raise FailurePivotContractError("control-return failure class is not derived from terminal facts")
    if bare["checkpoint_evidence"] != [item for item in references if "resume-checkpoint." in item["path"]]:
        raise FailurePivotContractError("control-return checkpoint evidence pointer is invalid")
    if bare["execution_receipt_evidence"] != _unique_named_evidence(references, "execution-receipt.json"):
        raise FailurePivotContractError("control-return execution receipt pointer is invalid")
    if bare["failure_evidence"] != _unique_named_evidence(references, "aborted.json"):
        raise FailurePivotContractError("control-return failure evidence pointer is invalid")
    expected_terminal_facts = _terminal_artifact_facts(
        terminal_status=bare["terminal_status"],
        terminal_category=bare["terminal_category"],
        execution_receipt_evidence=bare["execution_receipt_evidence"],
        failure_evidence=bare["failure_evidence"],
    )
    if bare["terminal_artifact_facts"] != expected_terminal_facts or bare["same_run_evidence_sha256"] != _same_run_evidence_sha256(evidence_root=evidence_root, references=references, terminal_artifact_facts=expected_terminal_facts, worktree=worktree):
        raise FailurePivotContractError("control-return terminal evidence chain is invalid")
    state = bare["workflow_state"]
    if not isinstance(state, dict) or set(state) != {"plan_bound", "phase_key", "resumable", "resume_checkpoint_present"} or type(state["plan_bound"]) is not bool or (state["phase_key"] is not None and not isinstance(state["phase_key"], str)) or type(state["resumable"]) is not bool or type(state["resume_checkpoint_present"]) is not bool:
        raise FailurePivotContractError("control-return workflow state is invalid")
    if state["phase_key"] != bare["failed_phase"] or state["plan_bound"] != (bare["failed_phase"] is not None):
        raise FailurePivotContractError("control-return phase state is inconsistent")
    if state["resume_checkpoint_present"] != bool(bare["checkpoint_evidence"]):
        raise FailurePivotContractError("control-return checkpoint state is inconsistent")
    if bare["allowed_parent_operations"] != ["pivot_from_receipt", "inspect_hash_bound_evidence"] or bare["prohibited_parent_operations"] != ["continue_cognitive_work_in_inherited_parent_model", "unbounded_budget_increase", "unscoped_mutation", "repeat_identical_failed_prompt"]:
        raise FailurePivotContractError("control-return parent operation set is invalid")
    expected_route = (
        _closed_packet("adaptive_route_reassessment", dispatch_permitted=True, escalate_one_tier=True, forbid_identical_prompt=True)
        if bare["failure_class"] in {"grounded_cognitive_failure", "exit_gate_failure"}
        else None
    )
    if bare["next_route_request"] != expected_route:
        raise FailurePivotContractError("control-return next route request is not derived from terminal facts")
    if bare["failure_class"] == "authority_required":
        if bare["parent_user_question"] is None or _validate_packet(bare["parent_user_question"]).get("kind") != "parent_or_user_question":
            raise FailurePivotContractError("authority control-return omitted its exact parent question")
    elif bare["parent_user_question"] is not None:
        raise FailurePivotContractError("non-authority control-return cannot carry a parent question")


def _t4_packet(directive: dict[str, Any]) -> dict[str, Any]:
    kind = "t4_consult" if directive["evidence_completeness"] == "complete" else "t4_diagnose"
    caps = _T4_CONSULT_CAPS if kind == "t4_consult" else _T4_DIAGNOSE_CAPS
    return _closed_packet(kind, dispatch_permitted=True, tier="T4", sandbox="read-only", network_access=False, tool_mode="none" if kind == "t4_consult" else "read_only", mutation_authority=False, repository_scope=_t4_scope(directive["worktree_evidence"]), evidence_bundle=_evidence_bundle(directive["evidence_references"], directive["evidence_completeness"]), limits=caps, required_return=T4_DIRECTION_NAME)


def derive_pivot(directive: dict[str, Any], previous_pivot: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_control_return(directive)
    legacy = directive["schema_version"] == 1
    if legacy:
        failure_class, tier, attempts, state = normalize_failure_class(directive["terminal_status"], directive.get("terminal_category")), None, 0, directive["workflow_state"]
        lineage_identity = content_hash({"legacy_receipt": directive["directive_sha256"], "phase_key": state.get("phase_key")})
    else:
        failure_class, tier, attempts, state = directive["failure_class"], directive["failed_tier"], directive["retry_escalation_count"], directive["workflow_state"]
        lineage_identity = directive["lineage_identity_sha256"]
    receipt_hash, prior_hash = directive["directive_sha256"], None
    if previous_pivot is not None:
        validate_pivot(previous_pivot)
        if previous_pivot["receipt_sha256"] == receipt_hash:
            raise FailurePivotContractError("same-receipt pivot is idempotent; do not use it as prior lineage")
        if previous_pivot.get("phase_key") != state.get("phase_key") or previous_pivot.get("lineage_identity_sha256") != lineage_identity:
            raise FailurePivotContractError("prior pivot phase lineage does not match")
        attempts, prior_hash = max(attempts, previous_pivot["retry_escalation_count"]), previous_pivot["pivot_sha256"]
    # A v1 boolean checkpoint claim was not bound to retained evidence.  Keep
    # it available for lineage compatibility, but never let it authorize a
    # resume decision.
    action = recommended_action(failure_class)
    checkpoint = False if legacy else state.get("resume_checkpoint_present", False)
    if legacy and failure_class == "grounded_cognitive_failure":
        action, packet = "abort", _closed_packet(
            "parent_blocker",
            dispatch_permitted=False,
            reason="legacy_v1_requires_v2_reconstruction_and_rebind",
        )
    elif legacy and failure_class == "exit_gate_failure":
        action, packet = "escalate", _closed_packet(
            "adaptive_route_reassessment",
            dispatch_permitted=True,
            escalate_one_tier=True,
            forbid_identical_prompt=True,
        )
    elif legacy and failure_class == "authority_required":
        action, packet = "ask_user", _closed_packet(
            "parent_or_user_question",
            dispatch_permitted=False,
            question=(
                "The legacy v1 control return did not retain the exact authority "
                "question; supply the exact question before any further dispatch."
            ),
        )
    elif failure_class == "deterministic_setup_failure":
        action, packet = ("abort", _closed_packet("parent_harness_incident", dispatch_permitted=False, reason="repeated_identical_setup_failure")) if attempts >= 1 else (action, _closed_packet("t0_repair", dispatch_permitted=True, retry_limit=1, repair_only=True, prohibited=["model_dispatch", "t4_consultation"]))
    elif failure_class == "transient_failure":
        action, packet = ("resume", _closed_packet("validated_checkpoint_resume", dispatch_permitted=True, checkpoint_required=True, same_route=True)) if checkpoint else (("abort", _closed_packet("parent_blocker", dispatch_permitted=False, reason="repeated_transient_failure")) if attempts >= 1 else (action, _closed_packet("same_route_retry", dispatch_permitted=True, retry_limit=1, same_route=True, identical_prompt_permitted=True)))
    elif failure_class == "grounded_cognitive_failure":
        # A grounded cognitive failure is already evidence that the current
        # route cannot resolve the blocker.  The evidence state, rather than
        # the failed tier or an arbitrary retry count, deterministically picks
        # the bounded T4 mode.  This deliberately prevents a T1/T2 failure
        # from re-entering a generic escalation loop with the same missing or
        # contradictory packet.
        packet = _t4_packet(directive)
    elif failure_class == "exit_gate_failure":
        # Exit-gate failures remain ordinary one-tier adaptive route
        # reassessments.  They do not meet the grounded-cognitive predicate
        # needed to enter either bounded T4 mode.
        packet = directive["next_route_request"]
    elif failure_class == "authority_required":
        action, packet = "ask_user", directive["parent_user_question"]
    elif failure_class == "budget_or_context_exhaustion":
        action, packet = "partition", _closed_packet("partition_before_retry", dispatch_permitted=True, budget_increase_permitted=False, checkpoint_present=checkpoint)
    elif failure_class == "harness_unavailable":
        action, packet = "abort", _closed_packet("read_only_break_glass_consultation", dispatch_permitted=False, tier="T4", read_only=True, network_access=False, mutation_authority=False, required_reentry_tier="T3", evidence_bundle=_evidence_bundle(directive.get("evidence_references", []), directive.get("evidence_completeness", "incomplete")))
    else:
        action, packet = "abort", _closed_packet("terminal_parent_blocker", dispatch_permitted=False)
    return _self_hashed({"schema_version": 2, "contract_name": PIVOT_NAME, "contract_version": PIVOT_VERSION, "receipt_sha256": receipt_hash, "prior_pivot_sha256": prior_hash, "lineage_identity_sha256": lineage_identity, "failure_class": failure_class, "phase_key": state.get("phase_key"), "retry_escalation_count": attempts + 1, "action": action, "packet": packet}, "pivot_sha256")


def validate_pivot(pivot: dict[str, Any]) -> None:
    if not isinstance(pivot, dict):
        raise FailurePivotContractError("pivot is invalid")
    if pivot.get("schema_version") == 1 and pivot.get("contract_version") == 1:
        bare = _validate_self_hash(pivot, "pivot_sha256", "legacy pivot")
        if bare.get("contract_name") != PIVOT_NAME or type(bare.get("attempt")) is not int or bare["attempt"] < 1 or not isinstance(bare.get("packet"), dict):
            raise FailurePivotContractError("legacy pivot schema is invalid")
        return
    bare = _validate_self_hash(pivot, "pivot_sha256", "pivot")
    expected = {"schema_version", "contract_name", "contract_version", "receipt_sha256", "prior_pivot_sha256", "lineage_identity_sha256", "failure_class", "phase_key", "retry_escalation_count", "action", "packet"}
    if set(bare) != expected or bare.get("schema_version") != 2 or bare.get("contract_name") != PIVOT_NAME or bare.get("contract_version") != PIVOT_VERSION or not _is_hash(bare.get("receipt_sha256")) or not _is_hash(bare.get("lineage_identity_sha256")) or (bare["prior_pivot_sha256"] is not None and not _is_hash(bare["prior_pivot_sha256"])) or bare.get("failure_class") not in FAILURE_CLASSES or bare.get("action") not in RECOMMENDED_ACTIONS or type(bare.get("retry_escalation_count")) is not int or bare["retry_escalation_count"] < 1:
        raise FailurePivotContractError("pivot schema is invalid")
    _validate_packet(bare["packet"])


def _validate_t4_result_payload(pivot: dict[str, Any], value: Any) -> dict[str, Any]:
    """Parse the only model-originated part of a T4 result.

    Every other result field is observed and supplied by the runtime.  This
    prevents a model from inventing an authority, a receipt, or a source tree.
    """
    packet = pivot["packet"]
    summary_field = "adjudication" if packet["kind"] == "t4_consult" else "diagnosis"
    expected = {"schema_version", "kind", summary_field, "direction", "allowed_mutation_scope", "next_tier", "prohibited"}
    if (
        not isinstance(value, dict) or set(value) != expected
        or value.get("schema_version") != 1
        or value.get("kind") != packet["kind"] + "_result"
        or not isinstance(value.get(summary_field), str) or not value[summary_field].strip()
        or not isinstance(value.get("direction"), str) or not value["direction"].strip()
        or value.get("next_tier") != "T3"
        or value.get("prohibited") != ["repository_patch", "external_action", "authority_grant", "budget_increase"]
        or not isinstance(value.get("allowed_mutation_scope"), list) or not value["allowed_mutation_scope"]
        or any(not _is_safe_mutation_scope_path(path) for path in value["allowed_mutation_scope"])
        or len(set(value["allowed_mutation_scope"])) != len(value["allowed_mutation_scope"])
    ):
        raise FailurePivotContractError("T4 result payload is invalid")
    return dict(value)


def _validate_t4_observed_runtime(
    observed: Any, *, pivot: dict[str, Any], control_return: dict[str, Any]
) -> dict[str, Any]:
    expected = {
        "execution_receipt_sha256", "model_output_sha256", "repository_path", "source_commit",
        "plan_id", "phase_key", "phase_contract_sha256", "prompt_sha256", "worktree_admission",
        "worktree_final", "sandbox", "network_access", "tool_mode", "mutation_authority", "limits",
    }
    if not isinstance(observed, dict) or set(observed) != expected:
        raise FailurePivotContractError("T4 observed runtime provenance is not closed")
    packet = pivot["packet"]
    if (
        not _is_hash(observed["execution_receipt_sha256"])
        or not _is_hash(observed["model_output_sha256"])
        or observed["repository_path"] != packet["repository_scope"]["repository_path"]
        or observed["source_commit"] != packet["repository_scope"]["source_commit"]
        or observed["plan_id"] != control_return["lineage_plan_id"]
        or observed["phase_key"] != control_return["failed_phase"]
        or observed["phase_contract_sha256"] != control_return["lineage_phase_contract_sha256"]
        or observed["prompt_sha256"] != control_return["lineage_prompt_sha256"]
        or observed["worktree_admission"] != control_return["worktree_evidence"]["admission"]
        or observed["worktree_final"] != control_return["worktree_evidence"]["final"]
        or observed["sandbox"] != "read-only" or observed["network_access"] is not False
        or observed["tool_mode"] != packet["tool_mode"] or observed["mutation_authority"] is not False
        or observed["limits"] != packet["limits"]
    ):
        raise FailurePivotContractError("T4 observed runtime provenance does not bind the pivot")
    return dict(observed)


def _validate_t4_observed_shape(
    observed: Any, *, mode: str, scope: dict[str, Any], lineage: dict[str, Any], limits: dict[str, int]
) -> None:
    expected = {
        "execution_receipt_sha256", "model_output_sha256", "repository_path", "source_commit",
        "plan_id", "phase_key", "phase_contract_sha256", "prompt_sha256", "worktree_admission",
        "worktree_final", "sandbox", "network_access", "tool_mode", "mutation_authority", "limits",
    }
    if not isinstance(observed, dict) or set(observed) != expected or not _is_hash(observed["execution_receipt_sha256"]) or not _is_hash(observed["model_output_sha256"]) or observed["repository_path"] != scope["repository_path"] or observed["source_commit"] != scope["source_commit"] or observed["plan_id"] != lineage["plan_id"] or observed["phase_key"] != lineage["phase_key"] or observed["phase_contract_sha256"] != lineage["phase_contract_sha256"] or observed["prompt_sha256"] != lineage["prompt_sha256"] or not isinstance(observed["worktree_admission"], dict) or not isinstance(observed["worktree_final"], dict) or observed["sandbox"] != "read-only" or observed["network_access"] is not False or observed["tool_mode"] != ("none" if mode == "t4_consult" else "read_only") or observed["mutation_authority"] is not False or observed["limits"] != limits:
        raise FailurePivotContractError("T4 result runtime provenance is invalid")


def _validate_t4_model_output_binding(
    observed: dict[str, Any], payload: dict[str, Any]
) -> None:
    """Bind parsed T4 output to the runtime-observed canonical output digest.

    T4 JSON has exactly one accepted representation after the strict parser:
    canonical JSON.  The dispatcher records this digest from that retained,
    parsed output before it constructs a result.  Therefore a rehashed result
    whose prose/scope changed but whose observed output provenance did not is
    rejected without attempting to judge the legitimate diagnosis text.
    """
    if observed["model_output_sha256"] != content_hash(payload):
        raise FailurePivotContractError(
            "T4 result payload is not bound to the observed model output"
        )


def build_t4_result(*, t4_pivot: dict[str, Any], t4_control_return: dict[str, Any], result_payload: dict[str, Any], observed_runtime: dict[str, Any]) -> dict[str, Any]:
    """Build the closed T4 terminal artifact from output plus observed facts."""
    validate_pivot(t4_pivot)
    validate_control_return(t4_control_return)
    if t4_control_return.get("schema_version") != 2 or t4_pivot["packet"].get("kind") not in {"t4_consult", "t4_diagnose"}:
        raise FailurePivotContractError("T4 result requires a v2 T4 pivot and control-return")
    if derive_pivot(t4_control_return) != t4_pivot:
        raise FailurePivotContractError("T4 result pivot is not the deterministic control-return pivot")
    payload = _validate_t4_result_payload(t4_pivot, result_payload)
    observed = _validate_t4_observed_runtime(observed_runtime, pivot=t4_pivot, control_return=t4_control_return)
    _validate_t4_model_output_binding(observed, payload)
    facts = t4_control_return["terminal_artifact_facts"]
    identity = {
        "schema_version": 1, "contract_name": T4_RESULT_NAME, "contract_version": T4_RESULT_VERSION,
        "source_pivot_sha256": t4_pivot["pivot_sha256"],
        "source_control_return_sha256": t4_control_return["directive_sha256"],
        "source_failed_execution_receipt_sha256": facts["execution_receipt_sha256"],
        "source_failure_evidence_sha256": facts["failure_evidence_sha256"],
        "source_evidence_bundle_sha256": t4_pivot["packet"]["evidence_bundle"]["evidence_bundle_sha256"],
        "source_same_run_evidence_sha256": t4_control_return["same_run_evidence_sha256"],
        "mode": t4_pivot["packet"]["kind"],
        "repository_scope": t4_pivot["packet"]["repository_scope"],
        "source_lineage": {
            "plan_id": t4_control_return["lineage_plan_id"], "phase_key": t4_control_return["failed_phase"],
            "phase_contract_sha256": t4_control_return["lineage_phase_contract_sha256"],
            "prompt_sha256": t4_control_return["lineage_prompt_sha256"],
            "dispatch_packet_sha256": t4_control_return["lineage_dispatch_packet_sha256"],
            "checkpoint_evidence_sha256": [item["sha256"] for item in t4_control_return["checkpoint_evidence"]],
        },
        "limits": t4_pivot["packet"]["limits"], "observed_runtime": observed,
        "result_payload": payload,
        "prohibited": ["repository_patch", "external_action", "authority_grant", "budget_increase"],
    }
    return _self_hashed(identity, "result_sha256")


def validate_t4_result(
    result: dict[str, Any], *, t4_pivot: dict[str, Any] | None = None,
    t4_control_return: dict[str, Any] | None = None,
) -> None:
    bare = _validate_self_hash(result, "result_sha256", "T4 result")
    expected = {
        "schema_version", "contract_name", "contract_version", "source_pivot_sha256", "source_control_return_sha256",
        "source_failed_execution_receipt_sha256", "source_failure_evidence_sha256", "source_evidence_bundle_sha256",
        "source_same_run_evidence_sha256", "mode", "repository_scope", "source_lineage", "limits", "observed_runtime",
        "result_payload", "prohibited",
    }
    if set(bare) != expected or bare.get("schema_version") != 1 or bare.get("contract_name") != T4_RESULT_NAME or bare.get("contract_version") != T4_RESULT_VERSION or not _is_hash(bare.get("source_pivot_sha256")) or not _is_hash(bare.get("source_control_return_sha256")) or not _is_hash(bare.get("source_evidence_bundle_sha256")) or not _is_hash(bare.get("source_same_run_evidence_sha256")) or (bare["source_failed_execution_receipt_sha256"] is not None and not _is_hash(bare["source_failed_execution_receipt_sha256"])) or (bare["source_failure_evidence_sha256"] is not None and not _is_hash(bare["source_failure_evidence_sha256"])) or bare.get("mode") not in {"t4_consult", "t4_diagnose"} or bare.get("prohibited") != ["repository_patch", "external_action", "authority_grant", "budget_increase"]:
        raise FailurePivotContractError("T4 result schema is invalid")
    synthetic_pivot = {
        "packet": {"kind": bare["mode"], "repository_scope": bare["repository_scope"], "limits": bare["limits"]}
    }
    _validate_t4_scope_and_evidence(bare["repository_scope"], {"schema_version": 1, "completeness": "complete", "references": [] , "evidence_bundle_sha256": content_hash({"schema_version": 1, "completeness": "complete", "references": []})})
    lineage = bare["source_lineage"]
    if not isinstance(lineage, dict) or set(lineage) != {"plan_id", "phase_key", "phase_contract_sha256", "prompt_sha256", "dispatch_packet_sha256", "checkpoint_evidence_sha256"} or not isinstance(lineage["plan_id"], str) or not lineage["plan_id"] or (lineage["phase_key"] is not None and not isinstance(lineage["phase_key"], str)) or not _is_hash(lineage["phase_contract_sha256"]) or not _is_hash(lineage["prompt_sha256"]) or not _is_hash(lineage["dispatch_packet_sha256"]) or not isinstance(lineage["checkpoint_evidence_sha256"], list) or any(not _is_hash(item) for item in lineage["checkpoint_evidence_sha256"]):
        raise FailurePivotContractError("T4 result lineage is invalid")
    _validate_t4_observed_shape(
        bare["observed_runtime"], mode=bare["mode"], scope=bare["repository_scope"],
        lineage=lineage, limits=bare["limits"],
    )
    # Reuse the strict model-payload parser with a tiny synthetic pivot; its
    # scope/caps are checked below against the actual source when supplied.
    payload = _validate_t4_result_payload(synthetic_pivot, bare["result_payload"])
    _validate_t4_model_output_binding(bare["observed_runtime"], payload)
    if t4_pivot is not None or t4_control_return is not None:
        if t4_pivot is None or t4_control_return is None:
            raise FailurePivotContractError("T4 result source validation requires both pivot and control-return")
        validate_pivot(t4_pivot)
        validate_control_return(t4_control_return)
        rebuilt = build_t4_result(
            t4_pivot=t4_pivot, t4_control_return=t4_control_return,
            result_payload=bare["result_payload"], observed_runtime=bare["observed_runtime"],
        )
        if rebuilt != result:
            raise FailurePivotContractError("T4 result is not bound to retained pivot/control evidence")


def _fresh_t3_identity(packet: Any) -> dict[str, Any]:
    if not isinstance(packet, dict):
        raise FailurePivotContractError("fresh T3 dispatch packet is invalid")
    bare = dict(packet)
    packet_hash = bare.pop("dispatch_packet_sha256", None)
    if not _is_hash(packet_hash) or packet_hash != content_hash(bare):
        raise FailurePivotContractError("fresh T3 dispatch packet hash is invalid")
    runtime = packet.get("runtime_contract")
    required = {"plan_id", "phase_key", "phase_contract_sha256", "prompt_sha256", "cwd", "source_commit", "runtime_contract"}
    planner_scope = runtime.get("planner_authorized_mutation_scope") if isinstance(runtime, dict) else None
    if not required <= set(packet) or not isinstance(packet["plan_id"], str) or not packet["plan_id"] or not isinstance(packet["phase_key"], str) or not packet["phase_key"] or not _is_hash(packet["phase_contract_sha256"]) or not _is_hash(packet["prompt_sha256"]) or not _is_absolute_path(packet["cwd"]) or not _is_commit(packet["source_commit"]) or not isinstance(runtime, dict) or runtime.get("sandbox") != "workspace-write" or runtime.get("mutation_authorized") is not True or not isinstance(planner_scope, list) or not planner_scope or planner_scope != sorted(planner_scope) or len(set(planner_scope)) != len(planner_scope) or any(not _is_safe_mutation_scope_path(path) for path in planner_scope):
        raise FailurePivotContractError("fresh T3 dispatch packet is not a governed mutation packet")
    return {
        "dispatch_packet_sha256": packet_hash, "plan_id": packet["plan_id"], "phase_key": packet["phase_key"],
        "phase_contract_sha256": packet["phase_contract_sha256"], "prompt_sha256": packet["prompt_sha256"],
        "cwd": packet["cwd"], "source_commit": packet["source_commit"],
        "planner_authorized_mutation_scope": list(planner_scope),
    }


def build_t4_direction_to_t3_mutation_contract(
    *,
    t4_result: dict[str, Any],
    t4_pivot: dict[str, Any],
    t4_control_return: dict[str, Any],
    fresh_t3_dispatch_packet: dict[str, Any],
) -> dict[str, Any]:
    """Convert a retained T4 result into direction for one fresh T3 packet.

    This accepts neither a caller-authored direction nor a caller-authored
    scope.  Those facts are sealed in the T4 result; mutation is still granted
    only by the parent and the fresh planner dispatch packet.
    """
    # A self-hashed T4 result is only a serialization.  It becomes actionable
    # only when the retained source pivot and control return rebuild it.
    validate_t4_result(
        t4_result, t4_pivot=t4_pivot, t4_control_return=t4_control_return
    )
    fresh = _fresh_t3_identity(fresh_t3_dispatch_packet)
    lineage = t4_result["source_lineage"]
    if fresh["dispatch_packet_sha256"] == lineage["dispatch_packet_sha256"] or fresh["cwd"] != t4_result["repository_scope"]["repository_path"] or fresh["source_commit"] != t4_result["repository_scope"]["source_commit"] or fresh["prompt_sha256"] == lineage["prompt_sha256"]:
        raise FailurePivotContractError("fresh T3 packet repeats or escapes the failed T4 lineage")
    payload = t4_result["result_payload"]
    if any(
        not any(_scope_is_within(path, authorized) for authorized in fresh["planner_authorized_mutation_scope"])
        for path in payload["allowed_mutation_scope"]
    ):
        raise FailurePivotContractError(
            "T4 result mutation scope escapes the fresh planner-authorized scope"
        )
    identity = {
        "schema_version": 2, "contract_name": T4_DIRECTION_NAME, "contract_version": T4_DIRECTION_VERSION,
        "source_t4_result_sha256": t4_result["result_sha256"], "source_pivot_sha256": t4_result["source_pivot_sha256"],
        "source_control_return_sha256": t4_result["source_control_return_sha256"],
        "source_failed_execution_receipt_sha256": t4_result["source_failed_execution_receipt_sha256"],
        "source_evidence_bundle_sha256": t4_result["source_evidence_bundle_sha256"],
        "source_same_run_evidence_sha256": t4_result["source_same_run_evidence_sha256"],
        "repository_path": t4_result["repository_scope"]["repository_path"], "source_commit": t4_result["repository_scope"]["source_commit"],
        "source_lineage": lineage, "checkpoint_evidence_sha256": lineage["checkpoint_evidence_sha256"],
        "fresh_t3": fresh, "next_tier": "T3", "direction": payload["direction"],
        "allowed_mutation_scope": payload["allowed_mutation_scope"], "parent_mutation_authority": "not_granted",
        "prohibited": ["repository_patch", "external_action", "authority_grant", "budget_increase"],
    }
    return _self_hashed(identity, "contract_sha256")


def validate_t4_direction_to_t3_mutation_contract(
    contract: dict[str, Any], *, t4_result: dict[str, Any], t4_pivot: dict[str, Any],
    t4_control_return: dict[str, Any], fresh_t3_dispatch_packet: dict[str, Any],
) -> None:
    bare = _validate_self_hash(contract, "contract_sha256", "T4-to-T3 mutation contract")
    expected = {
        "schema_version", "contract_name", "contract_version", "source_t4_result_sha256", "source_pivot_sha256",
        "source_control_return_sha256", "source_failed_execution_receipt_sha256", "source_evidence_bundle_sha256",
        "source_same_run_evidence_sha256", "repository_path", "source_commit", "source_lineage", "checkpoint_evidence_sha256",
        "fresh_t3", "next_tier", "direction", "allowed_mutation_scope", "parent_mutation_authority", "prohibited",
    }
    if set(bare) != expected or bare.get("schema_version") != 2 or bare.get("contract_name") != T4_DIRECTION_NAME or bare.get("contract_version") != T4_DIRECTION_VERSION or not _is_hash(bare.get("source_t4_result_sha256")) or not _is_hash(bare.get("source_pivot_sha256")) or not _is_hash(bare.get("source_control_return_sha256")) or not _is_hash(bare.get("source_evidence_bundle_sha256")) or not _is_hash(bare.get("source_same_run_evidence_sha256")) or (bare["source_failed_execution_receipt_sha256"] is not None and not _is_hash(bare["source_failed_execution_receipt_sha256"])) or not _is_absolute_path(bare.get("repository_path")) or not _is_commit(bare.get("source_commit")) or bare.get("next_tier") != "T3" or bare.get("parent_mutation_authority") != "not_granted" or bare.get("prohibited") != ["repository_patch", "external_action", "authority_grant", "budget_increase"] or not isinstance(bare.get("direction"), str) or not bare["direction"].strip() or not isinstance(bare.get("allowed_mutation_scope"), list) or not bare["allowed_mutation_scope"] or any(not _is_safe_mutation_scope_path(path) for path in bare["allowed_mutation_scope"]):
        raise FailurePivotContractError("T4-to-T3 mutation contract is invalid")
    if bare["checkpoint_evidence_sha256"] != bare["source_lineage"].get("checkpoint_evidence_sha256"):
        raise FailurePivotContractError("T4-to-T3 checkpoint lineage is invalid")
    rebuilt = build_t4_direction_to_t3_mutation_contract(
        t4_result=t4_result,
        t4_pivot=t4_pivot,
        t4_control_return=t4_control_return,
        fresh_t3_dispatch_packet=fresh_t3_dispatch_packet,
    )
    if rebuilt != contract:
        raise FailurePivotContractError(
            "T4 direction is not bound to retained result, pivot/control anchors, and fresh T3 packet"
        )
