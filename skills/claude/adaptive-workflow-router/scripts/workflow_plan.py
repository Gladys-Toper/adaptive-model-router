#!/usr/bin/env python3
"""Validate application workflows and resolve each cognitive phase through the adaptive router."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


SKILL_DIR = Path(__file__).resolve().parent.parent
PLANNER_CONTRACT_VERSION = 1
RUNTIME_ACTIVATION_CONTRACT_NAME = "adaptive-workflow.runtime-condition"
PHASE_RESULT_CONTRACT_NAME = "adaptive-workflow.phase-result"
DEFAULT_CATALOG = SKILL_DIR / "assets" / "workflows.json"
RESUMPTION_POLICY = SKILL_DIR / "assets" / "resumption-policy.json"
DEFAULT_ROUTER = (
    SKILL_DIR.parent / "adaptive-model-router" / "scripts" / "router_lab.py"
)

FORBIDDEN_ROUTING_KEYS = {
    "agent",
    "effort",
    "fallback",
    "model",
    "models",
    "provider",
    "tier",
}
ROUTE_FIELDS = {
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
CATALOG_FIELDS = {
    "schema_version",
    "research_snapshot",
    "route_contract_version",
    "route_defaults",
    "patterns",
    "workflows",
}
WORKFLOW_FIELDS = {
    "version",
    "application",
    "aliases",
    "summary",
    "evaluation_family",
    "composition_terminal_phase",
    "terminal_artifacts",
    "phases",
}
PHASE_FIELDS = {
    "id",
    "pattern",
    "depends_on",
    "condition",
    "condition_mode",
    "max_iterations",
    "execution",
    "route",
    "produces",
    "exit_gate",
    "authority_for",
    "action_source",
}
EXECUTION_MODES = {"route", "parent_gate", "parent_action"}
CONDITION_MODES = {"input", "runtime"}
BOUNDED_PATTERNS = {"evaluator_optimizer", "bounded_agent_loop"}
EXACT_ACTIVITIES = {"wait", "poll", "exact_check"}
EVIDENCE_ACTIVITIES = {"inspect", "retrieve", "summarize", "verify"}
AMBIGUITY_OVERRIDE_ACTIVITIES = {
    "frame",
    "design",
    "analyze",
    "interpret",
    "diagnose",
    "synthesize",
    "plan",
    "review",
    "adjudicate",
    "release",
    "architecture",
    "threat_model",
}
VISUAL_OVERRIDE_ACTIVITIES = {
    "inspect",
    "verify",
    "review",
    "analyze",
    "draft",
    "exact_check",
}
CURRENT_INFO_OVERRIDE_ACTIVITIES = {"inspect", "retrieve"}
CONTROL_PATTERNS = {
    "chain",
    "route",
    "fan_out",
    "gather",
    "orchestrator_workers",
    "evaluator_optimizer",
    "human_gate",
    "bounded_agent_loop",
}
ACTIVITIES = {
    "wait",
    "poll",
    "exact_check",
    "inspect",
    "retrieve",
    "summarize",
    "prepare_external",
    "frame",
    "design",
    "implement",
    "analyze",
    "draft",
    "verify",
    "interpret",
    "execute_runbook",
    "diagnose",
    "synthesize",
    "plan",
    "review",
    "adjudicate",
    "release",
    "architecture",
    "threat_model",
}
RISK_TAGS = {
    "public_interface",
    "dependencies",
    "build",
    "data_shape",
    "auth",
    "secrets",
    "billing",
    "security",
    "schema",
    "production",
    "data_loss",
    "privacy",
    "legal",
    "medical",
    "financial",
    "release",
}
WORK_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
UNVERSIONED_SOURCE_COMMIT = "0" * 40

ORDERS = {
    "mutation": ["none", "reversible", "persistent", "irreversible"],
    "scope": ["local", "multi_file", "single_system", "cross_system"],
    "ambiguity": ["none", "bounded", "high", "novel"],
    "risk_level": ["low", "moderate", "high", "critical"],
    "risk_scope": ["none", "evidence", "judgment", "mutation", "authority"],
}
ROUTE_ENUMS = {
    "activity": sorted(ACTIVITIES),
    "mutation": ORDERS["mutation"],
    "scope": ORDERS["scope"],
    "ambiguity": ORDERS["ambiguity"],
    "risk_level": ORDERS["risk_level"],
    "risk_scope": ORDERS["risk_scope"],
}


class WorkflowError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def context_bundle_record(context_files: list[dict[str, Any]]) -> dict[str, Any]:
    identity = {
        "schema_version": 1,
        "mode": "precomputed-planner-packet",
        "files": context_files,
        "total_bytes": sum(record["bytes"] for record in context_files),
    }
    return {**identity, "bundle_sha256": content_hash(identity)}


def absolute_without_resolving(path: Path) -> Path:
    return path.expanduser().absolute()


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise WorkflowError(f"workflow catalog is missing or unsafe: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkflowError("workflow catalog must be a JSON object")
    return value


def find_forbidden_keys(value: Any, pointer: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_pointer = f"{pointer}/{key}"
            if key.lower() in FORBIDDEN_ROUTING_KEYS:
                found.append(child_pointer)
            found.extend(find_forbidden_keys(child, child_pointer))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_forbidden_keys(child, f"{pointer}/{index}"))
    return found


def validate_route_facts(route: dict[str, Any], prefix: str, errors: list[str]) -> None:
    missing = sorted(ROUTE_FIELDS - set(route))
    unknown = sorted(set(route) - ROUTE_FIELDS)
    if missing:
        errors.append(f"{prefix} missing fields: {', '.join(missing)}")
    if unknown:
        errors.append(f"{prefix} has unknown fields: {', '.join(unknown)}")
    for field, choices in ROUTE_ENUMS.items():
        if route.get(field) not in choices:
            errors.append(f"{prefix}.{field} is invalid")
    risk_tags = route.get("risk_tags")
    if not isinstance(risk_tags, list) or any(
        not isinstance(item, str) for item in risk_tags
    ):
        errors.append(f"{prefix}.risk_tags must be a list of strings")
        risk_tags = []
    else:
        unknown_tags = sorted(set(risk_tags) - RISK_TAGS)
        if unknown_tags:
            errors.append(
                f"{prefix}.risk_tags contains unknown values: {', '.join(unknown_tags)}"
            )
        if len(risk_tags) != len(set(risk_tags)):
            errors.append(f"{prefix}.risk_tags contains duplicates")
    for field in ("visual_required", "current_info_required", "external_action"):
        if not isinstance(route.get(field), bool):
            errors.append(f"{prefix}.{field} must be boolean")
    if route.get("risk_scope") == "none" and risk_tags:
        errors.append(f"{prefix}.risk_tags require a non-none risk_scope")
    if route.get("external_action") and route.get("activity") != "prepare_external":
        errors.append(
            f"{prefix}.external_action is allowed only for prepared external packets"
        )


def dependency_ids(phase: dict[str, Any]) -> list[str]:
    value = phase.get("depends_on")
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return value


def route_requires_parent_gate(route: dict[str, Any]) -> bool:
    risk_tags = route.get("risk_tags")
    if not isinstance(risk_tags, list):
        risk_tags = []
    return bool(
        route.get("mutation") == "irreversible"
        or (
            route.get("risk_scope") == "mutation"
            and set(risk_tags) & {"production", "data_loss", "release"}
        )
    )


def validate_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    unknown_catalog_fields = sorted(set(catalog) - CATALOG_FIELDS)
    if unknown_catalog_fields:
        errors.append(
            "catalog has unknown fields: " + ", ".join(unknown_catalog_fields)
        )
    if (
        type(catalog.get("schema_version")) is not int
        or catalog.get("schema_version") != 1
    ):
        errors.append("schema_version must be 1")
    if (
        type(catalog.get("route_contract_version")) is not int
        or catalog.get("route_contract_version") != 1
    ):
        errors.append("route_contract_version must be 1")
    defaults = catalog.get("route_defaults")
    if not isinstance(defaults, dict):
        errors.append("route_defaults must be an object")
        defaults = {}
    missing_defaults = sorted((ROUTE_FIELDS - {"activity"}) - set(defaults))
    unknown_defaults = sorted(set(defaults) - (ROUTE_FIELDS - {"activity"}))
    if missing_defaults:
        errors.append(f"route_defaults missing: {', '.join(missing_defaults)}")
    if unknown_defaults:
        errors.append(
            f"route_defaults has unknown fields: {', '.join(unknown_defaults)}"
        )
    if not missing_defaults and not unknown_defaults:
        validate_route_facts({"activity": "wait", **defaults}, "route_defaults", errors)
    patterns = catalog.get("patterns")
    if (
        not isinstance(patterns, list)
        or not patterns
        or any(not isinstance(item, str) for item in patterns)
    ):
        errors.append("patterns must be a non-empty list of strings")
        patterns = []
    elif len(patterns) != len(set(patterns)):
        errors.append("patterns contains duplicates")
    if set(patterns) != CONTROL_PATTERNS:
        missing_patterns = sorted(CONTROL_PATTERNS - set(patterns))
        unknown_patterns = sorted(set(patterns) - CONTROL_PATTERNS)
        if missing_patterns:
            errors.append("patterns missing: " + ", ".join(missing_patterns))
        if unknown_patterns:
            errors.append("patterns unknown: " + ", ".join(unknown_patterns))
    workflows = catalog.get("workflows")
    if not isinstance(workflows, dict) or not workflows:
        errors.append("workflows must be a non-empty object")
        workflows = {}
    forbidden = find_forbidden_keys(catalog)
    if forbidden:
        errors.append(
            "workflow source contains router-owned fields: " + ", ".join(forbidden)
        )

    aliases: dict[str, str] = {}
    for workflow_id, workflow in workflows.items():
        prefix = f"workflows.{workflow_id}"
        if not isinstance(workflow, dict):
            errors.append(f"{prefix} must be an object")
            continue
        unknown_workflow_fields = sorted(set(workflow) - WORKFLOW_FIELDS)
        if unknown_workflow_fields:
            errors.append(
                f"{prefix} has unknown fields: {', '.join(unknown_workflow_fields)}"
            )
        if type(workflow.get("version")) is not int or workflow["version"] < 1:
            errors.append(f"{prefix}.version must be a positive integer")
        for field in (
            "application",
            "summary",
            "evaluation_family",
            "composition_terminal_phase",
        ):
            if not isinstance(workflow.get(field), str) or not workflow[field]:
                errors.append(f"{prefix}.{field} must be a non-empty string")
        workflow_aliases = workflow.get("aliases")
        if not isinstance(workflow_aliases, list) or any(
            not isinstance(item, str) or not item for item in workflow_aliases
        ):
            errors.append(f"{prefix}.aliases must be a list of non-empty strings")
            workflow_aliases = []
        elif len(workflow_aliases) != len(set(workflow_aliases)):
            errors.append(f"{prefix}.aliases contains duplicates")
        names = [
            workflow_id,
            workflow.get("application"),
            *workflow_aliases,
        ]
        for name in names:
            if not isinstance(name, str) or not name:
                errors.append(f"{prefix}.aliases contains an invalid name")
                continue
            normalized = name.lower()
            if normalized in aliases and aliases[normalized] != workflow_id:
                errors.append(
                    f"workflow alias {name!r} is shared by {aliases[normalized]} and {workflow_id}"
                )
            aliases[normalized] = workflow_id

        phases = workflow.get("phases")
        if not isinstance(phases, list) or not phases:
            errors.append(f"{prefix}.phases must be a non-empty list")
            continue
        by_id: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        produced: set[str] = set()
        unconditional_produced: set[str] = set()
        producers: dict[str, set[str]] = {}
        for index, phase in enumerate(phases):
            phase_prefix = f"{prefix}.phases[{index}]"
            if not isinstance(phase, dict):
                errors.append(f"{phase_prefix} must be an object")
                continue
            unknown_phase_fields = sorted(set(phase) - PHASE_FIELDS)
            if unknown_phase_fields:
                errors.append(
                    f"{phase_prefix} has unknown fields: {', '.join(unknown_phase_fields)}"
                )
            phase_id = phase.get("id")
            if not isinstance(phase_id, str) or not phase_id:
                errors.append(f"{phase_prefix}.id must be a non-empty string")
                continue
            if phase_id in by_id:
                errors.append(f"{prefix} has duplicate phase id {phase_id}")
                continue
            by_id[phase_id] = phase
            ordered_ids.append(phase_id)
            if phase.get("pattern") not in patterns:
                errors.append(f"{phase_prefix}.pattern is not declared")
            if phase.get("execution") not in EXECUTION_MODES:
                errors.append(f"{phase_prefix}.execution is invalid")
            dependencies = phase.get("depends_on")
            if not isinstance(dependencies, list) or any(
                not isinstance(item, str) for item in dependencies
            ):
                errors.append(f"{phase_prefix}.depends_on must be a list of phase ids")
            outputs = phase.get("produces")
            if (
                not isinstance(outputs, list)
                or not outputs
                or any(not isinstance(item, str) or not item for item in outputs)
            ):
                errors.append(
                    f"{phase_prefix}.produces must be a non-empty string list"
                )
            else:
                produced.update(outputs)
                for output in outputs:
                    producers.setdefault(output, set()).add(phase_id)
                if phase.get("condition") is None:
                    unconditional_produced.update(outputs)
            gates = phase.get("exit_gate")
            if (
                not isinstance(gates, list)
                or not gates
                or any(not isinstance(item, str) or not item for item in gates)
            ):
                errors.append(
                    f"{phase_prefix}.exit_gate must be a non-empty string list"
                )
            condition = phase.get("condition")
            condition_mode = phase.get("condition_mode")
            if condition is None and condition_mode is not None:
                errors.append(f"{phase_prefix} has condition_mode without condition")
            if condition is not None:
                if not isinstance(condition, str) or not condition:
                    errors.append(
                        f"{phase_prefix}.condition must be a non-empty string"
                    )
                if condition_mode not in CONDITION_MODES:
                    errors.append(f"{phase_prefix}.condition_mode is invalid")
            if phase.get("pattern") in BOUNDED_PATTERNS:
                maximum = phase.get("max_iterations")
                if type(maximum) is not int or maximum < 1:
                    errors.append(
                        f"{phase_prefix}.max_iterations must bound iterative patterns"
                    )
            elif "max_iterations" in phase:
                errors.append(
                    f"{phase_prefix}.max_iterations is allowed only for bounded patterns"
                )
            if phase.get("execution") == "route":
                route = phase.get("route")
                if not isinstance(route, dict):
                    errors.append(f"{phase_prefix}.route must be an object")
                else:
                    merged = {**defaults, **route}
                    validate_route_facts(merged, f"{phase_prefix}.route", errors)
                if "authority_for" in phase or "action_source" in phase:
                    errors.append(
                        f"{phase_prefix} route execution cannot own external authority"
                    )
            elif "route" in phase:
                errors.append(
                    f"{phase_prefix}.route is only allowed for route execution"
                )
            if phase.get("execution") == "parent_gate":
                if phase.get("pattern") != "human_gate":
                    errors.append(
                        f"{phase_prefix} parent_gate must use the human_gate pattern"
                    )
                if not isinstance(phase.get("authority_for"), str) or not phase.get(
                    "authority_for"
                ):
                    errors.append(
                        f"{phase_prefix}.authority_for must name a parent action"
                    )
                if "action_source" in phase:
                    errors.append(
                        f"{phase_prefix} parent_gate cannot contain action_source"
                    )
            elif phase.get("pattern") == "human_gate":
                errors.append(
                    f"{phase_prefix} human_gate pattern requires parent_gate execution"
                )
            if phase.get("execution") == "parent_action":
                if not isinstance(phase.get("action_source"), str) or not phase.get(
                    "action_source"
                ):
                    errors.append(
                        f"{phase_prefix}.action_source must name a prepared artifact"
                    )
                if "authority_for" in phase:
                    errors.append(
                        f"{phase_prefix} parent_action cannot contain authority_for"
                    )

        position = {phase_id: index for index, phase_id in enumerate(ordered_ids)}
        composition_terminal = workflow.get("composition_terminal_phase")
        if composition_terminal not in by_id:
            errors.append(f"{prefix}.composition_terminal_phase does not name a phase")
        elif by_id[composition_terminal].get("condition") is not None:
            errors.append(f"{prefix}.composition_terminal_phase must be unconditional")

        def ancestors(phase_id: str) -> set[str]:
            result: set[str] = set()
            stack = list(dependency_ids(by_id[phase_id]))
            while stack:
                dependency = stack.pop()
                if dependency in result or dependency not in by_id:
                    continue
                result.add(dependency)
                stack.extend(dependency_ids(by_id[dependency]))
            return result

        for phase_id, phase in by_id.items():
            for dependency in dependency_ids(phase):
                if dependency not in by_id:
                    errors.append(
                        f"{prefix}.{phase_id} has unknown dependency {dependency}"
                    )
                elif position[dependency] >= position[phase_id]:
                    errors.append(
                        f"{prefix}.{phase_id} dependency {dependency} is not earlier in the DAG"
                    )
            if phase.get("execution") == "parent_gate":
                target = phase.get("authority_for")
                if not isinstance(target, str) or target not in by_id:
                    errors.append(
                        f"{prefix}.{phase_id} targets unknown authority phase {target}"
                    )
                elif by_id[target].get("execution") not in {"route", "parent_action"}:
                    errors.append(
                        f"{prefix}.{phase_id} authority target {target} is not executable"
                    )
                elif phase_id not in ancestors(target):
                    errors.append(
                        f"{prefix}.{target} does not depend on granting parent_gate {phase_id}"
                    )
            if phase.get("execution") == "parent_action":
                matching_gates = [
                    candidate_id
                    for candidate_id, candidate in by_id.items()
                    if candidate.get("execution") == "parent_gate"
                    and candidate.get("authority_for") == phase_id
                ]
                if not matching_gates:
                    errors.append(
                        f"{prefix}.{phase_id} has no parent_gate granting its authority"
                    )
                else:
                    phase_ancestors = ancestors(phase_id)
                    granting_gates = [
                        gate for gate in matching_gates if gate in phase_ancestors
                    ]
                    if not granting_gates:
                        errors.append(
                            f"{prefix}.{phase_id} does not depend on its granting parent_gate"
                        )
                    for gate in granting_gates:
                        if by_id[gate].get("condition") != phase.get(
                            "condition"
                        ) or by_id[gate].get("condition_mode") != phase.get(
                            "condition_mode"
                        ):
                            errors.append(
                                f"{prefix}.{phase_id} and parent_gate {gate} must share a condition"
                            )
                action_source = phase.get("action_source")
                source_producers = (
                    producers.get(action_source, set())
                    if isinstance(action_source, str)
                    else set()
                )
                if not source_producers & ancestors(phase_id):
                    errors.append(
                        f"{prefix}.{phase_id} action_source {action_source} is not produced by an ancestor"
                    )
            if phase.get("execution") == "route" and isinstance(
                phase.get("route"), dict
            ):
                merged_route = {**defaults, **phase["route"]}
                if route_requires_parent_gate(merged_route):
                    granting_gates = [
                        gate_id
                        for gate_id, gate in by_id.items()
                        if gate.get("execution") == "parent_gate"
                        and gate.get("authority_for") == phase_id
                        and gate_id in ancestors(phase_id)
                        and gate.get("condition") == phase.get("condition")
                        and gate.get("condition_mode") == phase.get("condition_mode")
                    ]
                    if not granting_gates:
                        errors.append(
                            f"{prefix}.{phase_id} consequential mutation lacks an ancestor parent_gate"
                        )
        terminals = workflow.get("terminal_artifacts")
        if (
            not isinstance(terminals, list)
            or not terminals
            or any(not isinstance(item, str) for item in terminals)
        ):
            errors.append(
                f"{prefix}.terminal_artifacts must be a non-empty string list"
            )
        else:
            missing_terminal = sorted(set(terminals) - produced)
            if missing_terminal:
                errors.append(
                    f"{prefix} terminal artifacts are never produced: {', '.join(missing_terminal)}"
                )
            conditional_terminal = sorted(set(terminals) - unconditional_produced)
            if conditional_terminal:
                errors.append(
                    f"{prefix} terminal artifacts are conditional: {', '.join(conditional_terminal)}"
                )
            if composition_terminal in by_id:
                terminal_frontier = ancestors(composition_terminal) | {
                    composition_terminal
                }
                detached_terminal = sorted(
                    artifact
                    for artifact in terminals
                    if not (producers.get(artifact, set()) & terminal_frontier)
                )
                if detached_terminal:
                    errors.append(
                        f"{prefix} terminal artifacts are outside the completion frontier: {', '.join(detached_terminal)}"
                    )

    if errors:
        raise WorkflowError("; ".join(errors))
    return {
        "status": "VALID",
        "schema_version": catalog["schema_version"],
        "research_snapshot": catalog.get("research_snapshot"),
        "catalog_sha256": content_hash(catalog),
        "workflow_count": len(workflows),
        "phase_count": sum(len(item["phases"]) for item in workflows.values()),
        "aliases": aliases,
    }


def max_value(field: str, current: str, candidate: str | None) -> str:
    if candidate is None:
        return current
    order = ORDERS[field]
    return order[max(order.index(current), order.index(candidate))]


def risk_scope_for(route: dict[str, Any]) -> str:
    if route["mutation"] != "none":
        return "mutation"
    if route["activity"] in {"inspect", "retrieve", "summarize", "verify"}:
        return "evidence"
    return "judgment"


def apply_overrides(route: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    revised = copy.deepcopy(route)
    activity = revised["activity"]
    if activity in EXACT_ACTIVITIES:
        if activity in VISUAL_OVERRIDE_ACTIVITIES:
            revised["visual_required"] = bool(
                revised["visual_required"] or args.visual_required
            )
        return revised
    if activity not in EVIDENCE_ACTIVITIES and activity != "prepare_external":
        revised["scope"] = max_value("scope", revised["scope"], args.scope)
    if activity in AMBIGUITY_OVERRIDE_ACTIVITIES:
        revised["ambiguity"] = max_value(
            "ambiguity", revised["ambiguity"], args.ambiguity
        )
    revised["risk_level"] = max_value(
        "risk_level", revised["risk_level"], args.risk_level
    )
    if activity not in EVIDENCE_ACTIVITIES and activity != "prepare_external":
        revised["risk_scope"] = max_value(
            "risk_scope", revised["risk_scope"], args.risk_scope
        )
    if revised["mutation"] != "none":
        revised["mutation"] = max_value("mutation", revised["mutation"], args.mutation)
    if activity != "prepare_external":
        revised["risk_tags"] = sorted(set(revised["risk_tags"]) | set(args.risk_tag))
    if revised["risk_tags"] and revised["risk_scope"] == "none":
        revised["risk_scope"] = risk_scope_for(revised)
    if activity in VISUAL_OVERRIDE_ACTIVITIES:
        revised["visual_required"] = bool(
            revised["visual_required"] or args.visual_required
        )
    if activity in CURRENT_INFO_OVERRIDE_ACTIVITIES:
        revised["current_info_required"] = bool(
            revised["current_info_required"] or args.current_info_required
        )
    if activity == "prepare_external":
        revised["external_action"] = bool(
            revised["external_action"] or args.external_action
        )
    return revised


def validate_resolution(request: dict[str, Any], result: dict[str, Any]) -> None:
    if result.get("schema_version") != 1:
        raise WorkflowError("adaptive model router returned an unsupported schema")
    for field in ("workflow_id", "workflow_version", "phase_id"):
        if result.get(field) != request[field]:
            raise WorkflowError(f"adaptive model router returned a mismatched {field}")
    policy_id = result.get("policy_id")
    if not isinstance(policy_id, str) or not policy_id:
        raise WorkflowError("adaptive model router omitted the active policy id")
    expected_route_facts = {
        field: request[field]
        for field in (
            "activity",
            "mutation",
            "scope",
            "ambiguity",
            "risk_level",
            "risk_scope",
            "risk_tags",
            "external_action",
        )
    }
    if result.get("route_facts") != expected_route_facts:
        raise WorkflowError("adaptive model router changed the submitted route facts")
    if result.get("web_required") is not request["current_info_required"]:
        raise WorkflowError(
            "adaptive model router returned a mismatched web requirement"
        )
    if result.get("visual_required") is not request["visual_required"]:
        raise WorkflowError(
            "adaptive model router returned a mismatched visual requirement"
        )
    if not isinstance(result.get("parent_gate_required"), bool):
        raise WorkflowError("adaptive model router omitted the parent-gate decision")
    if result.get("external_mutation_authorized") is not False:
        raise WorkflowError(
            "adaptive model router attempted to confer mutation authority"
        )
    mode = result.get("mode")
    tier = result.get("tier")
    if mode == "deterministic":
        if tier != "T0" or result.get("execution_identity") != "NOT_APPLICABLE":
            raise WorkflowError("adaptive model router returned an invalid T0 route")
    elif mode == "model":
        if tier not in {"T1", "T2", "T3", "T4"}:
            raise WorkflowError("adaptive model router returned an invalid model tier")
        for field in (
            "agent",
            "provider",
            "service_tier",
            "model",
            "effort",
            "profile_file",
            "profile_sha256",
        ):
            if not isinstance(result.get(field), str) or not result[field]:
                raise WorkflowError(
                    f"adaptive model router omitted required model field {field}"
                )
        execution_identity = result.get("execution_identity")
        if execution_identity == "REQUESTED_PENDING_RUNTIME_METADATA":
            valid_evidence_contract = (
                result.get("runtime_evidence_required") is True
                and result.get("runtime_attestation_required") is False
            )
        elif execution_identity == "REQUESTED_NOT_ATTESTED":
            valid_evidence_contract = (
                result.get("runtime_evidence_required") is True
                and result.get("runtime_attestation_required") is True
            )
        else:
            valid_evidence_contract = False
        if not valid_evidence_contract:
            raise WorkflowError(
                "adaptive model router returned an invalid runtime-evidence contract"
            )
    else:
        raise WorkflowError("adaptive model router returned an invalid execution mode")


def resolve_route(router: Path, request: dict[str, Any]) -> dict[str, Any]:
    if router.is_symlink() or not router.is_file():
        raise WorkflowError(f"adaptive model router is missing or unsafe: {router}")
    completed = subprocess.run(
        [sys.executable, str(router), "resolve-phase", "--request", "-"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise WorkflowError(f"phase routing failed: {detail}")
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise WorkflowError("adaptive model router returned a non-object response")
    validate_resolution(request, result)
    router_health = result.get("router_health")
    if (
        not isinstance(router_health, dict)
        or router_health.get("routing_status") != "HEALTHY"
    ):
        raise WorkflowError("adaptive model router routing health is not healthy")
    stable_resolution = copy.deepcopy(result)
    stable_resolution["router_health"] = {"routing_status": "HEALTHY"}
    return stable_resolution


def alias_map(catalog: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for workflow_id, workflow in catalog["workflows"].items():
        for name in [
            workflow_id,
            workflow["application"],
            *workflow.get("aliases", []),
        ]:
            result[name.lower()] = workflow_id
    return result


def select_workflows(catalog: dict[str, Any], requested: list[str]) -> list[str]:
    aliases = alias_map(catalog)
    selected: list[str] = []
    for value in requested:
        workflow_id = aliases.get(value.lower())
        if workflow_id is None:
            raise WorkflowError(f"unknown application workflow: {value}")
        if workflow_id not in selected:
            selected.append(workflow_id)
    return selected


def activation_for(phase: dict[str, Any], enabled: set[str]) -> str:
    condition = phase.get("condition")
    if condition is None:
        return "active"
    if phase.get("condition_mode") == "runtime":
        return "runtime_condition"
    return "active" if condition in enabled else "disabled"


def build_plan(
    catalog: dict[str, Any],
    catalog_path: Path,
    router: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if not args.objective.strip():
        raise WorkflowError("objective must be non-empty")
    selected = select_workflows(catalog, args.application)
    enabled = set(args.enable_condition)
    known_input_conditions = {
        phase["condition"]
        for workflow_id in selected
        for phase in catalog["workflows"][workflow_id]["phases"]
        if phase.get("condition_mode") == "input"
    }
    unknown_conditions = sorted(enabled - known_input_conditions)
    if unknown_conditions:
        raise WorkflowError(
            "enabled conditions do not belong to the selected workflows: "
            + ", ".join(unknown_conditions)
        )

    catalog_sha256 = content_hash(catalog)
    route_contract_sha256 = content_hash(
        {
            "planner_contract_version": PLANNER_CONTRACT_VERSION,
            "route_contract_version": catalog["route_contract_version"],
            "route_defaults": catalog["route_defaults"],
            "patterns": catalog["patterns"],
        }
    )
    phases: list[dict[str, Any]] = []
    policy_ids: set[str] = set()
    workflow_hashes: dict[str, str] = {}
    completion_frontiers: dict[str, str] = {}
    previous_terminal: str | None = None
    for workflow_id in selected:
        workflow = catalog["workflows"][workflow_id]
        workflow_hashes[workflow_id] = content_hash(
            {
                "route_contract_sha256": route_contract_sha256,
                "workflow": workflow,
            }
        )
        source_by_id = {phase["id"]: phase for phase in workflow["phases"]}

        def source_ancestors(phase_id: str) -> set[str]:
            result: set[str] = set()
            stack = list(source_by_id[phase_id]["depends_on"])
            while stack:
                dependency = stack.pop()
                if dependency in result:
                    continue
                result.add(dependency)
                stack.extend(source_by_id[dependency]["depends_on"])
            return result

        workflow_completion_frontier = (
            f"{workflow_id}:{workflow['composition_terminal_phase']}"
        )
        for phase in workflow["phases"]:
            activation = activation_for(phase, enabled)
            phase_key = f"{workflow_id}:{phase['id']}"
            dependencies = [f"{workflow_id}:{item}" for item in phase["depends_on"]]
            if not dependencies and previous_terminal is not None:
                dependencies.append(previous_terminal)
            record: dict[str, Any] = {
                "phase_key": phase_key,
                "workflow_id": workflow_id,
                "workflow_version": workflow["version"],
                "phase_id": phase["id"],
                "pattern": phase["pattern"],
                "depends_on": dependencies,
                "activation": activation,
                "condition": phase.get("condition"),
                "execution": phase["execution"],
                "produces": phase["produces"],
                "exit_gate": phase["exit_gate"],
            }
            if "max_iterations" in phase:
                record["max_iterations"] = phase["max_iterations"]
            if phase["execution"] == "route" and activation != "disabled":
                route = apply_overrides(
                    {**catalog["route_defaults"], **phase["route"]}, args
                )
                request = {
                    "workflow_id": workflow_id,
                    "workflow_version": workflow["version"],
                    "phase_id": phase["id"],
                    **route,
                }
                resolution = resolve_route(router, request)
                policy_ids.add(resolution["policy_id"])
                record["route_request"] = request
                record["route_resolution"] = resolution
                source_has_gate = any(
                    candidate.get("execution") == "parent_gate"
                    and candidate.get("authority_for") == phase["id"]
                    and candidate["id"] in source_ancestors(phase["id"])
                    for candidate in workflow["phases"]
                )
                if resolution["parent_gate_required"] and not source_has_gate:
                    gate_key = f"{phase_key}:authorize_mutation"
                    phases.append(
                        {
                            "phase_key": gate_key,
                            "workflow_id": workflow_id,
                            "workflow_version": workflow["version"],
                            "phase_id": f"{phase['id']}:authorize_mutation",
                            "pattern": "human_gate",
                            "depends_on": list(dependencies),
                            "activation": activation,
                            "condition": phase.get("condition"),
                            "execution": "parent_gate",
                            "authority_for": phase_key,
                            "produces": [f"{phase['id']}_mutation_authorization"],
                            "exit_gate": [
                                "the parent confirms the exact consequential mutation scope before worker execution"
                            ],
                            "generated_by_router": True,
                        }
                    )
                    record["depends_on"] = [gate_key]
            elif phase["execution"] == "parent_gate":
                record["authority_for"] = f"{workflow_id}:{phase['authority_for']}"
            elif phase["execution"] == "parent_action":
                record["action_source"] = phase["action_source"]
            phases.append(record)
            if activation != "disabled":
                workflow_completion_frontier = phase_key
        completion_frontiers[workflow_id] = workflow_completion_frontier
        previous_terminal = workflow_completion_frontier

    if len(policy_ids) > 1:
        raise WorkflowError(
            "router policy changed while the workflow plan was resolving"
        )
    policy_id = next(iter(policy_ids), None)
    overrides = {
        "scope": args.scope,
        "ambiguity": args.ambiguity,
        "mutation": args.mutation,
        "risk_level": args.risk_level,
        "risk_scope": args.risk_scope,
        "risk_tags": sorted(set(args.risk_tag)),
        "visual_required": args.visual_required,
        "current_info_required": args.current_info_required,
        "external_action": args.external_action,
        "enabled_conditions": sorted(enabled),
    }
    identity = {
        "objective": args.objective,
        "catalog_sha256": catalog_sha256,
        "route_contract_sha256": route_contract_sha256,
        "selected_workflows": selected,
        "composition_mode": "sequential_in_requested_order",
        "workflow_hashes": workflow_hashes,
        "router_policy_id": policy_id,
        "overrides": overrides,
        "phases": phases,
    }
    return {
        "schema_version": 1,
        "planner_contract_version": PLANNER_CONTRACT_VERSION,
        "plan_id": content_hash(identity),
        "objective": args.objective,
        "research_snapshot": catalog.get("research_snapshot"),
        "catalog_path": str(catalog_path),
        "catalog_sha256": catalog_sha256,
        "route_contract_sha256": route_contract_sha256,
        "router_path": str(router),
        "router_policy_id": policy_id,
        "execution_identity": "REQUESTED_ROUTES_PENDING_RUNTIME_EVIDENCE",
        "selected_workflows": selected,
        "composition_mode": "sequential_in_requested_order",
        "conditional_dependency_semantics": "skipped_satisfies_dependency_without_artifacts",
        "completion_frontiers": completion_frontiers,
        "workflow_hashes": workflow_hashes,
        "overrides": overrides,
        "phases": phases,
        "experiment_binding": {
            "workflow_comparison": "freeze router_policy_id and vary one workflow manifest",
            "model_comparison": "freeze workflow_hashes and vary one router policy assignment",
            "mutating_trials": "use dry-run or sandbox artifacts; never duplicate live writes",
        },
    }


def load_dispatch_plan(path: Path) -> dict[str, Any]:
    absolute = absolute_without_resolving(path)
    if absolute.is_symlink() or not absolute.is_file():
        raise WorkflowError(f"dispatch plan is missing or unsafe: {absolute}")
    try:
        value = json.loads(absolute.read_text(encoding="utf-8"))
    except UnicodeDecodeError as error:
        raise WorkflowError(f"dispatch plan is not UTF-8: {absolute}") from error
    if not isinstance(value, dict):
        raise WorkflowError("dispatch plan must be a JSON object")
    return value


def plan_identity(plan: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "objective",
        "catalog_sha256",
        "route_contract_sha256",
        "selected_workflows",
        "composition_mode",
        "workflow_hashes",
        "router_policy_id",
        "overrides",
        "phases",
    )
    missing = [field for field in fields if field not in plan]
    if missing:
        raise WorkflowError(
            "dispatch plan is missing identity fields: " + ", ".join(missing)
        )
    return {field: plan[field] for field in fields}


def legacy_router_health_by_phase(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return retained router-health snapshots after enforcing routing authority."""
    snapshots: dict[str, dict[str, Any]] = {}
    phases = plan.get("phases")
    if not isinstance(phases, list):
        raise WorkflowError("dispatch plan phases must be a list of objects")
    for phase in phases:
        if not isinstance(phase, dict) or "route_resolution" not in phase:
            continue
        phase_key = phase.get("phase_key")
        resolution = phase.get("route_resolution")
        router_health = (
            resolution.get("router_health") if isinstance(resolution, dict) else None
        )
        if (
            not isinstance(phase_key, str)
            or not phase_key
            or not isinstance(router_health, dict)
            or router_health.get("routing_status") != "HEALTHY"
        ):
            raise WorkflowError(
                "dispatch plan route resolution has an unhealthy routing status"
            )
        snapshots[phase_key] = copy.deepcopy(router_health)
    return snapshots


def restore_legacy_router_health(
    regenerated: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Rebind canonical regeneration to legacy non-authoritative health snapshots."""
    snapshots = legacy_router_health_by_phase(plan)
    if not any(
        snapshot != {"routing_status": "HEALTHY"}
        for snapshot in snapshots.values()
    ):
        return regenerated
    restored = copy.deepcopy(regenerated)
    for phase in restored["phases"]:
        phase_key = phase.get("phase_key")
        if phase_key not in snapshots:
            continue
        resolution = phase.get("route_resolution")
        if not isinstance(resolution, dict):
            continue
        resolution["router_health"] = copy.deepcopy(snapshots[phase_key])
    restored["plan_id"] = content_hash(plan_identity(restored))
    return restored


def validate_dispatch_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != 1:
        raise WorkflowError("dispatch plan schema_version must be 1")
    if plan.get("planner_contract_version") != PLANNER_CONTRACT_VERSION:
        raise WorkflowError("dispatch plan uses an unsupported planner contract")
    plan_id = plan.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise WorkflowError("dispatch plan omitted plan_id")
    if plan_id != content_hash(plan_identity(plan)):
        raise WorkflowError("dispatch plan identity does not match plan_id")
    policy_id = plan.get("router_policy_id")
    if not isinstance(policy_id, str) or not policy_id:
        raise WorkflowError("dispatch plan omitted router_policy_id")
    phases = plan.get("phases")
    if not isinstance(phases, list) or any(
        not isinstance(item, dict) for item in phases
    ):
        raise WorkflowError("dispatch plan phases must be a list of objects")


def select_dispatch_phase(
    plan: dict[str, Any], phase_key: str
) -> dict[str, Any]:
    validate_dispatch_plan(plan)
    matches = [
        item for item in plan["phases"] if item.get("phase_key") == phase_key
    ]
    if len(matches) != 1:
        raise WorkflowError(
            f"dispatch phase key must identify exactly one phase: {phase_key}"
        )
    phase = matches[0]
    if phase.get("activation") != "active":
        raise WorkflowError(f"dispatch phase is not active: {phase_key}")
    if phase.get("execution") != "route":
        raise WorkflowError(f"dispatch phase is not routed: {phase_key}")
    request = phase.get("route_request")
    resolution = phase.get("route_resolution")
    if not isinstance(request, dict) or not isinstance(resolution, dict):
        raise WorkflowError(f"dispatch phase omitted route binding: {phase_key}")
    if resolution.get("mode") != "model":
        raise WorkflowError(f"dispatch phase is not a model route: {phase_key}")
    for field in ("workflow_id", "workflow_version", "phase_id"):
        if request.get(field) != phase.get(field):
            raise WorkflowError(
                f"dispatch phase route request mismatches {field}: {phase_key}"
            )
    validate_resolution(request, resolution)
    if resolution.get("policy_id") != plan["router_policy_id"]:
        raise WorkflowError(
            f"dispatch phase policy does not match the plan: {phase_key}"
        )
    for field in ("phase_key", "workflow_id", "phase_id", "pattern"):
        if not isinstance(phase.get(field), str) or not phase[field]:
            raise WorkflowError(f"dispatch phase omitted {field}: {phase_key}")
    if type(phase.get("workflow_version")) is not int:
        raise WorkflowError(f"dispatch phase omitted workflow_version: {phase_key}")
    for field in ("produces", "exit_gate"):
        if not isinstance(phase.get(field), list) or any(
            not isinstance(item, str) or not item for item in phase[field]
        ):
            raise WorkflowError(f"dispatch phase has invalid {field}: {phase_key}")
    return phase


def valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def read_bound_artifact(path_value: Any, label: str) -> tuple[Path, str]:
    if not isinstance(path_value, str) or not path_value:
        raise WorkflowError(f"{label} omitted its artifact path")
    path = Path(path_value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise WorkflowError(f"{label} artifact is missing or unsafe")
    path = path.resolve(strict=True)
    if str(path) != path_value:
        raise WorkflowError(f"{label} artifact path is not canonical")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def phase_result_contract(phase: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "phase_key",
        "workflow_id",
        "workflow_version",
        "phase_id",
        "pattern",
        "depends_on",
        "activation",
        "condition",
        "execution",
        "produces",
        "exit_gate",
        "route_request",
        "route_resolution",
    )
    return {field: phase.get(field) for field in fields}


def validate_phase_result(
    plan: dict[str, Any], target_phase: dict[str, Any], condition: str, path: Path
) -> dict[str, Any]:
    source = load_json(path)
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "plan_id",
        "phase_key",
        "phase_contract_sha256",
        "triggered_conditions",
        "execution_record_path",
        "execution_record_sha256",
        "artifacts",
        "contract_sha256",
    }
    if (
        set(source) != fields
        or type(source.get("schema_version")) is not int
        or source.get("schema_version") != 1
        or source.get("contract_name") != PHASE_RESULT_CONTRACT_NAME
        or type(source.get("contract_version")) is not int
        or source.get("contract_version") != 1
        or source.get("plan_id") != plan.get("plan_id")
    ):
        raise WorkflowError("runtime source phase-result has an invalid schema")
    source_phase_key = source.get("phase_key")
    if (
        not isinstance(source_phase_key, str)
        or source_phase_key not in target_phase.get("depends_on", [])
    ):
        raise WorkflowError("runtime source phase-result is not a direct dependency")
    matches = [
        phase for phase in plan["phases"] if phase.get("phase_key") == source_phase_key
    ]
    if len(matches) != 1:
        raise WorkflowError("runtime source phase-result phase is missing from the plan")
    source_phase = matches[0]
    if source.get("phase_contract_sha256") != content_hash(
        phase_result_contract(source_phase)
    ):
        raise WorkflowError("runtime source phase-result changed its phase contract")
    triggered = source.get("triggered_conditions")
    if (
        not isinstance(triggered, list)
        or any(not isinstance(item, str) or not item for item in triggered)
        or len(triggered) != len(set(triggered))
        or condition not in triggered
    ):
        raise WorkflowError("runtime source phase-result did not trigger the condition")
    execution_path, execution_hash = read_bound_artifact(
        source.get("execution_record_path"), "runtime execution record"
    )
    if (
        not valid_sha256(source.get("execution_record_sha256"))
        or source["execution_record_sha256"] != execution_hash
    ):
        raise WorkflowError("runtime execution record hash does not match")
    artifacts = source.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise WorkflowError("runtime source phase-result omitted produced artifacts")
    allowed_names = set(source_phase.get("produces", []))
    seen_names: set[str] = set()
    normalized_artifacts: list[dict[str, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"name", "path", "sha256"}:
            raise WorkflowError("runtime source phase-result has an invalid artifact")
        name = artifact.get("name")
        if not isinstance(name, str) or name not in allowed_names or name in seen_names:
            raise WorkflowError("runtime source phase-result artifact is not declared")
        artifact_path, artifact_hash = read_bound_artifact(
            artifact.get("path"), "runtime produced"
        )
        if artifact_path == execution_path:
            raise WorkflowError(
                "runtime produced artifact must be separate from its execution record"
            )
        if not valid_sha256(artifact.get("sha256")) or artifact["sha256"] != artifact_hash:
            raise WorkflowError("runtime produced artifact hash does not match")
        seen_names.add(name)
        normalized_artifacts.append(
            {"name": name, "path": str(artifact_path), "sha256": artifact_hash}
        )
    identity = {key: source[key] for key in fields if key != "contract_sha256"}
    if source.get("contract_sha256") != content_hash(identity):
        raise WorkflowError("runtime source phase-result self-hash is invalid")
    return {
        "phase_result_contract_path": str(path.resolve(strict=True)),
        "phase_result_contract_file_sha256": hashlib.sha256(
            path.read_bytes()
        ).hexdigest(),
        "phase_result_contract_sha256": source["contract_sha256"],
        "source_phase_key": source_phase_key,
        "execution_record_path": str(execution_path),
        "execution_record_sha256": execution_hash,
        "artifacts": normalized_artifacts,
    }


def activate_runtime_phase(
    plan: dict[str, Any], phase_key: str, evidence_contract_path: Path
) -> dict[str, Any]:
    """Activate one runtime-conditioned phase from retained phase-result evidence."""
    validate_dispatch_plan(plan)
    matches = [phase for phase in plan["phases"] if phase.get("phase_key") == phase_key]
    if len(matches) != 1:
        raise WorkflowError(f"runtime phase key must identify exactly one phase: {phase_key}")
    phase = matches[0]
    if phase.get("activation") != "runtime_condition":
        raise WorkflowError(f"phase is not awaiting a runtime condition: {phase_key}")
    if phase.get("execution") != "route":
        raise WorkflowError(f"runtime-conditioned phase is not routed: {phase_key}")
    condition = phase.get("condition")
    if not isinstance(condition, str) or not condition:
        raise WorkflowError(f"runtime-conditioned phase omitted its condition: {phase_key}")
    contract_path = absolute_without_resolving(evidence_contract_path)
    if contract_path.is_symlink() or not contract_path.is_file():
        raise WorkflowError(
            f"runtime-condition evidence contract is missing or unsafe: {contract_path}"
        )
    contract = load_json(contract_path)
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "plan_id",
        "phase_key",
        "condition",
        "triggered",
        "source_phase_result_path",
        "source_phase_result_sha256",
        "contract_sha256",
    }
    if (
        set(contract) != fields
        or type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != 1
        or contract.get("contract_name") != RUNTIME_ACTIVATION_CONTRACT_NAME
        or type(contract.get("contract_version")) is not int
        or contract.get("contract_version") != 1
        or contract.get("triggered") is not True
    ):
        raise WorkflowError("runtime-condition evidence contract has an invalid schema")
    if (
        contract.get("plan_id") != plan.get("plan_id")
        or contract.get("phase_key") != phase_key
        or contract.get("condition") != condition
    ):
        raise WorkflowError("runtime-condition evidence does not match this plan phase")
    phase_result_path, phase_result_hash = read_bound_artifact(
        contract.get("source_phase_result_path"), "runtime source phase-result"
    )
    if (
        not valid_sha256(contract.get("source_phase_result_sha256"))
        or contract["source_phase_result_sha256"] != phase_result_hash
    ):
        raise WorkflowError("runtime source phase-result hash does not match")
    identity = {key: contract[key] for key in fields if key != "contract_sha256"}
    if contract.get("contract_sha256") != content_hash(identity):
        raise WorkflowError("runtime-condition evidence contract self-hash is invalid")
    phase_result = validate_phase_result(plan, phase, condition, phase_result_path)
    activated = copy.deepcopy(plan)
    activated_phase = next(
        item for item in activated["phases"] if item.get("phase_key") == phase_key
    )
    activated_phase["activation"] = "active"
    activated_phase["runtime_activation"] = {
        "contract_name": contract["contract_name"],
        "contract_version": contract["contract_version"],
        "contract_path": str(contract_path.resolve(strict=True)),
        "contract_file_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
        "contract_sha256": contract["contract_sha256"],
        "source_phase_result_path": str(phase_result_path),
        "source_phase_result_sha256": phase_result_hash,
        **phase_result,
    }
    activated["plan_id"] = content_hash(plan_identity(activated))
    select_dispatch_phase(activated, phase_key)
    return activated


def verify_plan_authority(
    plan: dict[str, Any], catalog_path: Path, router: Path
) -> dict[str, Any]:
    """Regenerate a plan from the trusted catalog and replay bound activations."""
    validate_dispatch_plan(plan)
    catalog_path = absolute_without_resolving(catalog_path)
    catalog = load_json(catalog_path)
    validate_catalog(catalog)
    selected = plan.get("selected_workflows")
    overrides = plan.get("overrides")
    if (
        not isinstance(plan.get("objective"), str)
        or not plan["objective"].strip()
        or not isinstance(selected, list)
        or not selected
        or any(not isinstance(item, str) or not item for item in selected)
        or not isinstance(overrides, dict)
    ):
        raise WorkflowError("dispatch plan omitted its reproducible planner inputs")
    required_overrides = {
        "scope",
        "ambiguity",
        "mutation",
        "risk_level",
        "risk_scope",
        "risk_tags",
        "visual_required",
        "current_info_required",
        "external_action",
        "enabled_conditions",
    }
    if set(overrides) != required_overrides:
        raise WorkflowError("dispatch plan overrides are not reproducible")
    regenerated = build_plan(
        catalog,
        catalog_path,
        absolute_without_resolving(router),
        argparse.Namespace(
            objective=plan.get("objective"),
            application=selected,
            enable_condition=overrides["enabled_conditions"],
            scope=overrides["scope"],
            ambiguity=overrides["ambiguity"],
            mutation=overrides["mutation"],
            risk_level=overrides["risk_level"],
            risk_scope=overrides["risk_scope"],
            risk_tag=overrides["risk_tags"],
            visual_required=overrides["visual_required"],
            current_info_required=overrides["current_info_required"],
            external_action=overrides["external_action"],
        ),
    )
    regenerated = restore_legacy_router_health(regenerated, plan)
    remaining = {
        phase["phase_key"]: phase["runtime_activation"]
        for phase in plan["phases"]
        if isinstance(phase.get("runtime_activation"), dict)
    }
    while remaining:
        progressed = False
        for phase_key, expected_record in list(remaining.items()):
            contract_value = expected_record.get("contract_path")
            if not isinstance(contract_value, str):
                raise WorkflowError("activated plan omitted its activation contract path")
            contract_path = Path(contract_value)
            if contract_path.is_symlink() or not contract_path.is_file():
                raise WorkflowError("activated plan contract is missing or unsafe")
            contract = load_json(contract_path)
            if contract.get("plan_id") != regenerated.get("plan_id"):
                continue
            regenerated = activate_runtime_phase(
                regenerated, phase_key, contract_path
            )
            actual_record = next(
                phase["runtime_activation"]
                for phase in regenerated["phases"]
                if phase.get("phase_key") == phase_key
            )
            if actual_record != expected_record:
                raise WorkflowError("activated plan changed its activation evidence")
            del remaining[phase_key]
            progressed = True
        if not progressed:
            raise WorkflowError("activated plan evidence does not form a valid plan chain")
    if plan_identity(regenerated) != plan_identity(plan):
        raise WorkflowError("dispatch plan does not match the trusted workflow catalog")
    for field in (
        "schema_version",
        "planner_contract_version",
        "plan_id",
        "research_snapshot",
        "catalog_path",
        "catalog_sha256",
        "route_contract_sha256",
        "router_path",
        "router_policy_id",
        "execution_identity",
        "conditional_dependency_semantics",
        "completion_frontiers",
        "experiment_binding",
    ):
        if plan.get(field) != regenerated.get(field):
            raise WorkflowError(f"dispatch plan changed authoritative field {field}")
    return {
        "schema_version": 1,
        "status": "VERIFIED",
        "plan_id": plan["plan_id"],
        "catalog_sha256": plan["catalog_sha256"],
        "router_policy_id": plan["router_policy_id"],
        "runtime_activations": sum(
            1 for phase in plan["phases"] if "runtime_activation" in phase
        ),
    }


def phase_contract(phase: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase_key": phase["phase_key"],
        "workflow_id": phase["workflow_id"],
        "workflow_version": phase["workflow_version"],
        "phase_id": phase["phase_id"],
        "pattern": phase["pattern"],
        "produces": phase["produces"],
        "exit_gate": phase["exit_gate"],
        "route_request": phase["route_request"],
        "route_resolution": phase["route_resolution"],
    }


def read_dispatch_text(path: Path, label: str) -> tuple[Path, bytes]:
    absolute = absolute_without_resolving(path)
    if absolute.is_symlink() or not absolute.is_file():
        raise WorkflowError(f"{label} is missing or unsafe: {absolute}")
    resolved = absolute.resolve(strict=True)
    raw = resolved.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise WorkflowError(f"{label} is not UTF-8: {resolved}") from error
    return resolved, raw


def read_bound_json(path: Path, label: str) -> tuple[Path, bytes, dict[str, Any]]:
    absolute = absolute_without_resolving(path)
    if absolute.is_symlink() or not absolute.is_file():
        raise WorkflowError(f"{label} is missing or unsafe: {absolute}")
    resolved = absolute.resolve(strict=True)
    raw = resolved.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkflowError(f"{label} is invalid: {error}") from error
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} must be a JSON object")
    return resolved, raw, value


def load_resumption_policy() -> tuple[Path, bytes, dict[str, Any]]:
    path, raw, policy = read_bound_json(RESUMPTION_POLICY, "resumption policy")
    required = {
        "allowed_modes",
        "checkpoint_contract_name",
        "checkpoint_contract_version",
        "claim_contract_name",
        "claim_contract_version",
        "max_checkpoint_bytes",
        "max_checkpoint_summary_bytes",
        "max_checkpoint_ttl_seconds",
        "max_continuations",
        "max_work_items",
        "policy_name",
        "policy_sha256",
        "policy_version",
        "safe_mutation_classes",
        "schema_version",
        "work_manifest_contract_name",
        "work_manifest_contract_version",
    }
    bare = dict(policy)
    supplied = bare.pop("policy_sha256", None)
    integer_fields = (
        "max_checkpoint_bytes",
        "max_checkpoint_summary_bytes",
        "max_checkpoint_ttl_seconds",
        "max_continuations",
        "max_work_items",
    )
    if (
        set(policy) != required
        or policy.get("schema_version") != 1
        or policy.get("policy_name") != "adaptive-workflow.resumption-policy"
        or policy.get("policy_version") != 1
        or policy.get("allowed_modes") != ["explicit_checkpoint_restart"]
        or policy.get("safe_mutation_classes") != ["none"]
        or policy.get("checkpoint_contract_name")
        != "adaptive-workflow.resume-checkpoint"
        or policy.get("checkpoint_contract_version") != 1
        or policy.get("claim_contract_name") != "adaptive-workflow.resume-claim"
        or policy.get("claim_contract_version") != 1
        or policy.get("work_manifest_contract_name")
        != "adaptive-workflow.resume-work-manifest"
        or policy.get("work_manifest_contract_version") != 1
        or any(
            type(policy.get(field)) is not int or policy[field] < 1
            for field in integer_fields
        )
        or supplied != content_hash(bare)
    ):
        raise WorkflowError("resumption policy is invalid or self-hash mismatched")
    return path, raw, policy


def load_work_manifest(path: Path, policy: dict[str, Any]) -> dict[str, Any]:
    resolved, raw, manifest = read_bound_json(path, "resume work manifest")
    fields = {
        "schema_version",
        "contract_name",
        "contract_version",
        "work_items",
        "manifest_sha256",
    }
    bare = dict(manifest)
    supplied = bare.pop("manifest_sha256", None)
    items = manifest.get("work_items")
    if (
        set(manifest) != fields
        or manifest.get("schema_version") != 1
        or manifest.get("contract_name")
        != policy["work_manifest_contract_name"]
        or manifest.get("contract_version")
        != policy["work_manifest_contract_version"]
        or not isinstance(items, list)
        or len(items) < 2
        or len(items) > policy["max_work_items"]
        or supplied != content_hash(bare)
    ):
        raise WorkflowError("resume work manifest is invalid or self-hash mismatched")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"id", "description"}:
            raise WorkflowError("resume work manifest item has an invalid schema")
        work_id = item.get("id")
        description = item.get("description")
        if (
            not isinstance(work_id, str)
            or not WORK_ID.fullmatch(work_id)
            or work_id in seen
            or not isinstance(description, str)
            or not description.strip()
            or len(description.encode("utf-8")) > 512
        ):
            raise WorkflowError("resume work manifest item is invalid or duplicated")
        seen.add(work_id)
    return {
        "path": str(resolved),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "manifest_sha256": manifest["manifest_sha256"],
        "work_items": items,
    }


def build_resume_contract(
    args: argparse.Namespace, phase: dict[str, Any]
) -> dict[str, Any] | None:
    option_values = (
        args.work_manifest,
        args.max_continuations,
        args.cumulative_wall_time_seconds,
        args.checkpoint_window_seconds,
        args.shutdown_window_seconds,
        args.checkpoint_ttl_seconds,
    )
    if not args.resumable:
        if any(value is not None for value in option_values):
            raise WorkflowError("resume options require --resumable")
        return None
    # Claude surface: there is no analog of the App Server turn/steer +
    # turn/interrupt pair, so an explicit-checkpoint-restart contract cannot be
    # honored. Refuse the lane instead of minting an unenforceable contract.
    raise WorkflowError(
        "resumable dispatch is unsupported on the Claude surface: "
        "ABORTED_NO_RESUMABLE_CHECKPOINT"
    )
    if any(value is None for value in option_values):
        raise WorkflowError("--resumable requires every resume contract option")
    request = phase.get("route_request", {})
    if (
        request.get("mutation") != "none"
        or args.sandbox != "read-only"
        or args.tool_mode != "none"
        or args.network_access
        or args.mutation_authorized
    ):
        raise WorkflowError(
            "resumable dispatch requires a non-mutating read-only no-tools phase"
        )
    policy_path, policy_raw, policy = load_resumption_policy()
    if (
        type(args.max_continuations) is not int
        or not 1 <= args.max_continuations <= policy["max_continuations"]
        or type(args.cumulative_wall_time_seconds) is not int
        or args.cumulative_wall_time_seconds < args.wall_time_seconds
        or type(args.checkpoint_window_seconds) is not int
        or args.checkpoint_window_seconds < 1
        or type(args.shutdown_window_seconds) is not int
        or args.shutdown_window_seconds < 1
        or args.checkpoint_window_seconds + args.shutdown_window_seconds
        >= args.wall_time_seconds
        or type(args.checkpoint_ttl_seconds) is not int
        or not 1
        <= args.checkpoint_ttl_seconds
        <= policy["max_checkpoint_ttl_seconds"]
    ):
        raise WorkflowError("resumption timing, TTL, or continuation limit is unsafe")
    work_manifest = load_work_manifest(args.work_manifest, policy)
    identity = {
        "contract_name": "adaptive-workflow.resumption-authority",
        "contract_version": 1,
        "mode": "explicit_checkpoint_restart",
        "policy_path": str(policy_path),
        "policy_file_sha256": hashlib.sha256(policy_raw).hexdigest(),
        "policy_sha256": policy["policy_sha256"],
        "work_manifest": work_manifest,
        "attempt_wall_time_seconds": args.wall_time_seconds,
        "cumulative_wall_time_seconds": args.cumulative_wall_time_seconds,
        "checkpoint_window_seconds": args.checkpoint_window_seconds,
        "shutdown_window_seconds": args.shutdown_window_seconds,
        "checkpoint_ttl_seconds": args.checkpoint_ttl_seconds,
        "max_continuations": args.max_continuations,
        "mutation_class": "none",
    }
    return {**identity, "resume_contract_sha256": content_hash(identity)}


def resolve_dispatch_cwd(path: Path) -> Path:
    absolute = absolute_without_resolving(path)
    if not absolute.is_dir():
        raise WorkflowError(f"dispatch cwd does not exist: {absolute}")
    return absolute.resolve(strict=True)


def source_commit_for(cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        value = completed.stdout.strip().lower()
        if len(value) in {40, 64} and all(character in "0123456789abcdef" for character in value):
            return value
    except (OSError, subprocess.SubprocessError):
        pass
    return UNVERSIONED_SOURCE_COMMIT


def require_authoritative_source_commit(cwd: Path) -> str:
    """Require Git-derived provenance for every planned dispatch packet."""
    source_commit = source_commit_for(cwd)
    if source_commit == UNVERSIONED_SOURCE_COMMIT:
        raise WorkflowError(
            "planned dispatch requires a Git-backed source commit"
        )
    return source_commit


def load_required_skills(path: Path | None, cwd: Path) -> dict[str, Any]:
    """Load the immutable skill contract; dispatch verifies fresh read receipts."""
    if path is None:
        return {
            "required_skills": [],
            "skill_hashes": {},
            "source_commit": source_commit_for(cwd),
        }
    _, raw, contract = read_bound_json(path, "required skills contract")
    identity = dict(contract)
    supplied = identity.pop("contract_sha256", None)
    required = contract.get("required_skills")
    source_commit = contract.get("source_commit")
    if (
        set(contract) != {
            "schema_version",
            "contract_name",
            "source_commit",
            "required_skills",
            "contract_sha256",
        }
        or contract.get("schema_version") != 1
        or contract.get("contract_name") != "adaptive-workflow.required-skills"
        or supplied != content_hash(identity)
        or not isinstance(required, list)
        or not isinstance(source_commit, str)
        or len(source_commit) not in {40, 64}
        or source_commit != source_commit_for(cwd)
    ):
        raise WorkflowError("required skills contract is invalid or source-stale")
    hashes: dict[str, str] = {}
    for item in required:
        if not isinstance(item, dict) or set(item) != {
            "name", "path", "skill_sha256", "read_receipt_id", "max_age_seconds"
        }:
            raise WorkflowError("required skills contract has an invalid skill entry")
        name = item.get("name")
        skill_hash = item.get("skill_sha256")
        if (
            not isinstance(name, str)
            or not name
            or name in hashes
            or not isinstance(skill_hash, str)
            or len(skill_hash) != 64
            or any(character not in "0123456789abcdef" for character in skill_hash)
        ):
            raise WorkflowError("required skills contract has duplicate or invalid names")
        hashes[name] = skill_hash
    return {
        "required_skills": required,
        "skill_hashes": hashes,
        "source_commit": source_commit,
        "required_skills_contract_path": str(path.expanduser().resolve(strict=True)),
        "required_skills_contract_sha256": hashlib.sha256(raw).hexdigest(),
    }


def build_dispatch_packet(args: argparse.Namespace) -> dict[str, Any]:
    if args.wall_time_seconds < 1:
        raise WorkflowError("wall-time-seconds must be a positive integer")
    token_cap = getattr(args, "token_cap", None)
    token_cap_contract = getattr(args, "token_cap_contract", None)
    model_cycle_cap = getattr(args, "model_cycle_cap", None)
    tool_cycle_cap = getattr(args, "tool_cycle_cap", None)
    budget_increase_contract = getattr(args, "budget_increase_contract", None)
    if token_cap is not None and token_cap < 1:
        raise WorkflowError("token-cap must be a positive integer")
    if token_cap_contract is not None and token_cap is None:
        raise WorkflowError("token-cap-contract requires token-cap")
    if model_cycle_cap is not None and model_cycle_cap < 1:
        raise WorkflowError("model-cycle-cap must be a positive integer")
    if tool_cycle_cap is not None and tool_cycle_cap < 0:
        raise WorkflowError("tool-cycle-cap cannot be negative")
    if args.tool_mode == "none" and (
        args.sandbox != "read-only" or args.network_access
    ):
        raise WorkflowError(
            "no-tools dispatch requires read-only sandboxing with network disabled"
        )
    plan = load_dispatch_plan(args.plan)
    phase = select_dispatch_phase(plan, args.phase_key)
    resume_contract = build_resume_contract(args, phase)
    _, prompt = read_dispatch_text(args.prompt_file, "dispatch prompt")
    context_files: list[dict[str, Any]] = []
    for context_path in args.context_file:
        resolved, raw = read_dispatch_text(context_path, "dispatch context file")
        context_files.append(
            {
                "path": str(resolved),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        )
    cwd = resolve_dispatch_cwd(args.cwd)
    require_authoritative_source_commit(cwd)
    skills = load_required_skills(args.required_skills_file, cwd)

    def optional_hash(value: Path | None) -> str | None:
        if value is None:
            return None
        resolved, raw = read_dispatch_text(value, "dispatch budget contract")
        del resolved
        return hashlib.sha256(raw).hexdigest()

    identity = {
        "schema_version": 2 if resume_contract is not None else 1,
        "planner_contract_version": plan["planner_contract_version"],
        "plan_id": plan["plan_id"],
        "router_policy_id": plan["router_policy_id"],
        "phase_key": phase["phase_key"],
        "phase_contract_sha256": content_hash(phase_contract(phase)),
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "context_files": context_files,
        "context_bundle": context_bundle_record(context_files),
        "cwd": str(cwd),
        "runtime_contract": {
            "sandbox": args.sandbox,
            "network_access": args.network_access,
            "tool_mode": args.tool_mode,
            "mutation_authorized": args.mutation_authorized,
            "wall_time_seconds": args.wall_time_seconds,
            "requested_budget_limits": {
                "token_cap": token_cap,
                "model_cycle_cap": model_cycle_cap,
                "tool_cycle_cap": tool_cycle_cap,
            },
            "token_cap_contract_sha256": optional_hash(
                token_cap_contract
            ),
            "budget_increase_contract_sha256": optional_hash(
                budget_increase_contract
            ),
        },
        **skills,
    }
    if resume_contract is not None:
        identity["resume_contract"] = resume_contract
    return {
        **identity,
        "dispatch_packet_sha256": content_hash(identity),
    }


def write_dispatch_packet(path: Path, packet: dict[str, Any]) -> Path:
    absolute = absolute_without_resolving(path)
    if absolute.is_symlink():
        raise WorkflowError(f"dispatch packet output is unsafe: {absolute}")
    parent = absolute.parent.resolve(strict=True)
    if not parent.is_dir():
        raise WorkflowError(f"dispatch packet output directory is missing: {parent}")
    target = parent / absolute.name
    temporary = target.with_name(
        f".{target.name}.{packet['dispatch_packet_sha256'][:12]}.tmp"
    )
    if temporary.exists() or temporary.is_symlink():
        raise WorkflowError(f"dispatch packet temporary path already exists: {temporary}")
    try:
        temporary.write_text(
            json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
    except OSError:
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise
    return target


def write_activated_plan(path: Path, plan: dict[str, Any]) -> Path:
    absolute = absolute_without_resolving(path)
    if absolute.is_symlink():
        raise WorkflowError(f"activated plan output is unsafe: {absolute}")
    parent = absolute.parent.resolve(strict=True)
    if not parent.is_dir():
        raise WorkflowError(f"activated plan output directory is missing: {parent}")
    target = parent / absolute.name
    temporary = target.with_name(f".{target.name}.{plan['plan_id'][:12]}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise WorkflowError(f"activated plan temporary path already exists: {temporary}")
    try:
        temporary.write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
    except OSError:
        if temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog",
        type=Path,
        default=DEFAULT_CATALOG,
        help="Workflow catalog to validate or plan.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the workflow catalog.")
    subparsers.add_parser("list", help="List installed application workflows.")

    plan = subparsers.add_parser(
        "plan", help="Compose workflows and resolve every active cognitive phase."
    )
    plan.add_argument("--application", action="append", required=True)
    plan.add_argument("--objective", required=True)
    plan.add_argument("--router", type=Path, default=DEFAULT_ROUTER)
    plan.add_argument("--enable-condition", action="append", default=[])
    plan.add_argument("--scope", choices=ORDERS["scope"])
    plan.add_argument("--ambiguity", choices=ORDERS["ambiguity"])
    plan.add_argument("--mutation", choices=ORDERS["mutation"])
    plan.add_argument("--risk-level", choices=ORDERS["risk_level"])
    plan.add_argument("--risk-scope", choices=ORDERS["risk_scope"])
    plan.add_argument(
        "--risk-tag", action="append", choices=sorted(RISK_TAGS), default=[]
    )
    plan.add_argument("--visual-required", action="store_true")
    plan.add_argument("--current-info-required", action="store_true")
    plan.add_argument("--external-action", action="store_true")
    plan.add_argument(
        "--output",
        type=Path,
        help="Atomically retain the generated dispatch plan at this path",
    )

    bind = subparsers.add_parser(
        "bind", help="Bind one active routed phase to exact dispatch inputs."
    )
    bind.add_argument("--plan", type=Path, required=True)
    bind.add_argument("--phase-key", required=True)
    bind.add_argument("--prompt-file", type=Path, required=True)
    bind.add_argument("--context-file", type=Path, action="append", default=[])
    bind.add_argument("--cwd", type=Path, required=True)
    bind.add_argument(
        "--sandbox", choices=("read-only", "workspace-write"), required=True
    )
    bind.add_argument("--network-access", action="store_true")
    bind.add_argument("--tool-mode", choices=("default", "none"), required=True)
    bind.add_argument("--mutation-authorized", action="store_true")
    bind.add_argument("--wall-time-seconds", type=int, required=True)
    bind.add_argument("--token-cap", type=int)
    bind.add_argument("--token-cap-contract", type=Path)
    bind.add_argument("--model-cycle-cap", type=int)
    bind.add_argument("--tool-cycle-cap", type=int)
    bind.add_argument("--budget-increase-contract", type=Path)
    bind.add_argument("--resumable", action="store_true")
    bind.add_argument("--work-manifest", type=Path)
    bind.add_argument("--max-continuations", type=int)
    bind.add_argument("--cumulative-wall-time-seconds", type=int)
    bind.add_argument("--checkpoint-window-seconds", type=int)
    bind.add_argument("--shutdown-window-seconds", type=int)
    bind.add_argument("--checkpoint-ttl-seconds", type=int)
    bind.add_argument(
        "--required-skills-file",
        type=Path,
        help="Self-hashed required-skill/read-receipt contract bound into the packet",
    )
    bind.add_argument(
        "--output",
        type=Path,
        help="Atomically retain the dispatch packet at this path",
    )
    activate = subparsers.add_parser(
        "activate-runtime",
        help="Activate one runtime-conditioned phase from retained evidence.",
    )
    activate.add_argument("--plan", type=Path, required=True)
    activate.add_argument("--phase-key", required=True)
    activate.add_argument("--evidence-contract", type=Path, required=True)
    activate.add_argument("--output", type=Path, required=True)
    verify_plan = subparsers.add_parser(
        "verify-plan",
        help="Regenerate a dispatch plan from the trusted catalog and verify it.",
    )
    verify_plan.add_argument("--plan", type=Path, required=True)
    verify_plan.add_argument("--router", type=Path, default=DEFAULT_ROUTER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "bind":
            result = build_dispatch_packet(args)
            if args.output is not None:
                write_dispatch_packet(args.output, result)
        elif args.command == "activate-runtime":
            plan_path = absolute_without_resolving(args.plan)
            output_path = absolute_without_resolving(args.output)
            if plan_path == output_path:
                raise WorkflowError(
                    "activated plan output must not replace its prior plan"
                )
            result = activate_runtime_phase(
                load_dispatch_plan(plan_path),
                args.phase_key,
                args.evidence_contract,
            )
            write_activated_plan(output_path, result)
        elif args.command == "verify-plan":
            result = verify_plan_authority(
                load_dispatch_plan(args.plan),
                absolute_without_resolving(args.catalog),
                absolute_without_resolving(args.router),
            )
        else:
            catalog_path = absolute_without_resolving(args.catalog)
            catalog = load_json(catalog_path)
            validation = validate_catalog(catalog)
            if args.command == "validate":
                result = validation
            elif args.command == "list":
                result = {
                    "research_snapshot": catalog.get("research_snapshot"),
                    "catalog_sha256": validation["catalog_sha256"],
                    "workflows": [
                        {
                            "workflow_id": workflow_id,
                            "application": workflow["application"],
                            "aliases": workflow.get("aliases", []),
                            "summary": workflow["summary"],
                            "phases": len(workflow["phases"]),
                        }
                        for workflow_id, workflow in catalog["workflows"].items()
                    ],
                }
            elif args.command == "plan":
                result = build_plan(
                    catalog,
                    catalog_path,
                    absolute_without_resolving(args.router),
                    args,
                )
                if args.output is not None:
                    write_activated_plan(args.output, result)
            else:
                raise WorkflowError(f"unknown command: {args.command}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (
        WorkflowError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"workflow-plan error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
