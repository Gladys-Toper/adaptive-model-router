#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import learning_loop as ll


class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_model_router_home = ll.MODEL_ROUTER_HOME
        self.original_dispatch_registry = ll.DISPATCH_REGISTRY_PATH
        self.store = ll.Store(self.root / "ledger")
        self.model_router_home = self.root / "model-router"
        self.model_router_home.mkdir()
        ll.MODEL_ROUTER_HOME = self.model_router_home
        self.registry_path = self.root / "trusted-execution-registry.jsonl"
        self.registry_path.write_bytes(b"")
        os.chmod(self.registry_path, 0o600)
        ll.DISPATCH_REGISTRY_PATH = self.registry_path
        self.receipt_counter = 0
        self.active_path = self.root / "workflows.json"
        self.candidate_path = self.root / "candidate-workflows.json"
        self.active_bytes = ll.canonical_bytes({"strategy": {"review": "always"}, "stable": True}) + b"\n"
        self.candidate_bytes = ll.canonical_bytes({"strategy": {"review": "on_risk"}, "stable": True}) + b"\n"
        self.active_path.write_bytes(self.active_bytes)
        self.candidate_path.write_bytes(self.candidate_bytes)
        self.route = {
            "provider": "openai",
            "model": "gpt-5.6-terra",
            "effort": "medium",
            "service_tier": "default",
            "tier": "T2",
            "mutation": "none",
            "sandbox": "read-only",
            "fallback": None,
        }
        self.router_policy_id = ll.digest("router")
        models = {
            f"gpt-5.6-{variant}": {
                "slug": f"gpt-5.6-{variant}",
                "provider": "openai",
                "visibility": "list",
                "available": True,
                "input_modalities": ["text"],
                "supported_reasoning_levels": ["low", "medium", "high", "ultra"],
                "service_tiers": ["default"],
            }
            for variant in ("luna", "terra", "sol")
        }
        self.catalog_semantic_hash = ll.digest([models[key] for key in sorted(models)])
        self.catalog_path = self.model_router_home / "catalog.json"
        self.catalog_hash = self.write(
            self.catalog_path,
            {
                "schema_version": 3,
                "semantic_hash": self.catalog_semantic_hash,
                "models": models,
            },
        )
        self.family_window = {
            "max_version_lag": 0,
            "latest_observed_family": "5.6",
            "allowed_families": ["5.6"],
        }
        self.router_policy_path = self.model_router_home / "active-policy.json"
        self.router_policy_file_hash = self.write(
            self.router_policy_path,
            {
                "schema_version": 1,
                "policy_id": self.router_policy_id,
                "constraints": {"model_version_window": self.family_window},
            },
        )
        self.execution_paths = {
            arm: self.root / f"{arm}-execution-manifest.json"
            for arm in ("control", "challenger")
        }
        self.execution_hashes = {
            arm: self.write(path, {"schema_version": 1, "arm": arm, "prompt": "same"})
            for arm, path in self.execution_paths.items()
        }
        self.shared = {
            "schema_version": 1,
            "prompt_sha256": ll.digest("prompt"),
            "context_sha256": ll.digest("context"),
            "tools": [],
            "permissions": {"sandbox": "read-only", "network": False},
            "service_tier": "default",
            "retry_rule": {"transient": 0, "cognitive": 0},
            "grader_identity_sha256": ll.file_sha256(ll.DETERMINISTIC_GRADER_PATH),
            "rubric_sha256": ll.digest("rubric"),
            "token_cap_per_arm": 180_000,
            "wall_time_cap_seconds_per_arm": 300,
            "execution_manifest_sha256_by_arm": {
                "control": [self.execution_hashes["control"]],
                "challenger": [self.execution_hashes["challenger"]],
            },
            "planned_invocations_by_arm": {
                arm: [self.plan(f"{arm}-worker", arm)]
                for arm in ("control", "challenger")
            },
        }
        self.shared_path = self.root / "shared.json"
        self.shared_hash = self.write(self.shared_path, self.shared)
        self.budget_path = self.root / "budget.json"
        self.rebuild_budget()
        self.bindings = {
            "catalog_path": str(self.catalog_path),
            "catalog_semantic_sha256": self.catalog_semantic_hash,
            "catalog_file_sha256": self.catalog_hash,
            "family_window_sha256": ll.digest(self.family_window),
            "router_policy_path": str(self.router_policy_path),
            "router_policy_file_sha256": self.router_policy_file_hash,
            "runner_path": "/Applications/ChatGPT.app/Contents/Resources/codex",
            "runner_version": "codex-cli test",
            "runner_sha256": ll.digest("runner"),
            "input_manifest_path": str(self.shared_path),
            "input_manifest_sha256": self.shared_hash,
            "grader_identity_sha256": self.shared["grader_identity_sha256"],
            "rubric_sha256": self.shared["rubric_sha256"],
            "evaluation_suite_version": "test-v1",
            "evaluation_suite_sha256": ll.digest("suite"),
            "config_sha256": ll.digest("config"),
            "run_budget_path": str(self.budget_path),
            "run_budget_sha256": self.budget_hash,
            "evidence_ttl_seconds": 3600,
        }

    def cleanup(self) -> None:
        ll.MODEL_ROUTER_HOME = self.original_model_router_home
        ll.DISPATCH_REGISTRY_PATH = self.original_dispatch_registry
        self.temporary.cleanup()

    def write(self, path: Path, value: object) -> str:
        content = ll.canonical_bytes(value) + b"\n"
        path.write_bytes(content)
        return ll.sha256_bytes(content)

    def plan(
        self,
        planned_call_id: str,
        arm: str,
        *,
        role: str = "worker",
        phase: str = "primary",
        attempt_kind: str = "initial",
        token_cap: int = 180_000,
        wall_time_cap_seconds: int = 300,
    ) -> dict:
        return {
            "planned_call_id": planned_call_id,
            "role": role,
            "phase": phase,
            "attempt_kind": attempt_kind,
            "token_cap": token_cap,
            "wall_time_cap_seconds": wall_time_cap_seconds,
            "expected_inferences": 1,
            "execution_manifest_sha256": self.execution_hashes[arm],
        }

    def rebuild_budget(self, reviewer_allowance: int = 10_000, grader_allowance: int = 5_000) -> None:
        calls = []
        for arm, plans in self.shared["planned_invocations_by_arm"].items():
            for plan in plans:
                calls.append(
                    {
                        "call_id": plan["planned_call_id"],
                        "arm": arm,
                        "role": plan["role"],
                        "phase": plan["phase"],
                        "attempt_kind": plan["attempt_kind"],
                        "token_cap": plan["token_cap"],
                        "expected_inferences": plan["expected_inferences"],
                        "context_growth_allowance": 20_000,
                        "wall_time_seconds": plan["wall_time_cap_seconds"],
                        "execution_manifest_sha256": plan["execution_manifest_sha256"],
                        "rationale": "requirement-derived paired evidence",
                        "cap_mode": "derived",
                    }
                )
        self.budget = ll.build_budget_manifest(
            self.shared_hash,
            calls,
            reviewer_allowance,
            grader_allowance,
        )
        self.budget_hash = self.write(self.budget_path, self.budget)

    def configure_plans(self, plans_by_arm: dict[str, list[dict]]) -> None:
        self.shared["planned_invocations_by_arm"] = plans_by_arm
        self.shared["execution_manifest_sha256_by_arm"] = {
            arm: sorted({plan["execution_manifest_sha256"] for plan in plans})
            for arm, plans in plans_by_arm.items()
        }
        self.shared_hash = self.write(self.shared_path, self.shared)
        self.rebuild_budget()
        self.bindings.update(
            {
                "input_manifest_sha256": self.shared_hash,
                "run_budget_sha256": self.budget_hash,
            }
        )

    def append_receipt(self, receipt_type: str, body: dict) -> str:
        self.receipt_counter += 1
        lines = [line for line in self.registry_path.read_text(encoding="utf-8").splitlines() if line]
        previous = "0" * 64 if not lines else json.loads(lines[-1])["receipt_sha256"]
        receipt_id = f"{receipt_type}-{self.receipt_counter}"
        receipt = {
            "schema_version": 1,
            "receipt_type": receipt_type,
            "receipt_id": receipt_id,
            "sequence": len(lines) + 1,
            "previous_receipt_sha256": previous,
            **body,
        }
        receipt["receipt_sha256"] = ll.digest(receipt)
        with self.registry_path.open("ab") as handle:
            handle.write(ll.canonical_bytes(receipt) + b"\n")
        return receipt_id

    def cycle_value(self, **overrides: object) -> dict:
        value = {
            "lane": "workflow",
            "candidate_id": "workflow-candidate",
            "current_family": "5.6",
            "features": {
                "activity": "implement",
                "scope": "local",
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
                "task_family": "coding",
                "holdout": False,
                "terminal_strategy": False,
            },
            "hypothesis": "risk-triggered review reduces overhead without quality loss",
            "change": {
                "variable_id": "workflow.review_escalation",
                "before": "always",
                "after": "on_risk",
                "affected_paths": ["/strategy/review"],
            },
            "arms": {
                "control": {
                    "action": "always",
                    "route": dict(self.route),
                    "profile_sha256": ll.digest("profile"),
                    "router_policy_sha256": ll.digest("router"),
                    "workflow_policy_sha256": ll.sha256_bytes(self.active_bytes),
                },
                "challenger": {
                    "action": "on_risk",
                    "route": dict(self.route),
                    "profile_sha256": ll.digest("profile"),
                    "router_policy_sha256": ll.digest("router"),
                    "workflow_policy_sha256": ll.sha256_bytes(self.candidate_bytes),
                },
            },
            "frozen": {"router_policy_sha256": ll.digest("router")},
            "bindings": dict(self.bindings),
            "requirements": {
                "development_pairs": 1,
                "task_family_count": 1,
                "holdout_pairs": 0,
                "exact_t4_controls": 0,
                "canary_windows": 2,
            },
            "quality_tolerance": 0.0,
            "minimum_token_savings": 0.05,
            "minimum_time_savings": 0.05,
            "max_relative_variance": 2.0,
            "workflow_authority": {
                "active_path": str(self.active_path),
                "candidate_path": str(self.candidate_path),
            },
        }
        value.update(overrides)
        return value

    def begin(self, name: str = "one", **overrides: object) -> dict:
        return ll.begin(
            self.store,
            self.cycle_value(cycle_id=f"cycle-{name}", run_id=f"run-{name}", **overrides),
        )

    def metadata(
        self,
        arm: str,
        case: str,
        tokens: int,
        wall: int,
        *,
        route: dict | None = None,
        profile_hash: str | None = None,
        source: str = "openai-codex-app-server-workflow",
        status: str = "COMPLETED",
        elapsed: int | None = None,
    ) -> tuple[Path, str, str]:
        selected = route or self.route
        transcript = self.root / f"{case}-{arm}-{tokens}-{wall}.jsonl"
        thread_id = f"thread-{case}-{arm}-{tokens}"
        turn_id = f"turn-{case}-{arm}-{tokens}"
        message_id = f"message-{case}-{arm}-{tokens}"
        transcript_duration = wall if elapsed is None else elapsed
        transcript_events = [
            {
                "method": "thread/settings/updated",
                "params": {
                    "threadId": thread_id,
                    "threadSettings": {
                        "modelProvider": selected["provider"],
                        "model": selected["model"],
                        "effort": selected["effort"],
                        "serviceTier": selected["service_tier"],
                    },
                },
            },
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": message_id}}},
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "tokenUsage": {
                        "total": {"inputTokens": tokens - 10, "outputTokens": 10, "totalTokens": tokens}
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "durationMs": transcript_duration}},
            },
        ]
        transcript.write_text("".join(ll.canonical_bytes(event).decode("utf-8") + "\n" for event in transcript_events), encoding="utf-8")
        value = {
            "schema_version": 1,
            "source": source,
            "status": status,
            "issues": [],
            "input_manifest_sha256": self.execution_hashes[arm],
            "profile_sha256": profile_hash or ll.digest("profile"),
            "runner": {
                "path": self.bindings["runner_path"],
                "version": self.bindings["runner_version"],
                "sha256": self.bindings["runner_sha256"],
            },
            "limits": {"elapsed_ms": wall if elapsed is None else elapsed},
            "server": {
                "requested_provider": selected["provider"],
                "requested_model": selected["model"],
                "requested_effort": selected["effort"],
                "requested_service_tier": selected["service_tier"],
                "observed_provider": selected["provider"],
                "observed_model": selected["model"],
                "observed_effort": selected["effort"],
                "observed_service_tier": selected["service_tier"],
                "reroutes": [],
                "safety_buffering": [],
                "thread_id": thread_id,
                "turn_id": turn_id,
                "turn_status": "completed",
                "completed_agent_message_ids": [message_id],
                "usage": {"input_tokens": tokens - 10, "output_tokens": 10},
                "total_tokens": tokens,
                "transcript_path": str(transcript),
                "transcript_sha256": ll.file_sha256(transcript),
            },
        }
        path = self.root / f"{case}-{arm}-{tokens}-{wall}-metadata.json"
        metadata_hash = self.write(path, value)
        identity = {
            "provider": selected["provider"],
            "model": selected["model"],
            "effort": selected["effort"],
            "service_tier": selected["service_tier"],
        }
        receipt_id = self.append_receipt(
            "execution",
            {
                "metadata_path": str(path.resolve()),
                "metadata_sha256": metadata_hash,
                "transcript_path": str(transcript.resolve()),
                "transcript_sha256": ll.file_sha256(transcript),
                "input_manifest_path": str(self.execution_paths[arm].resolve()),
                "input_manifest_sha256": self.execution_hashes[arm],
                "runner": value["runner"],
                "profile_sha256": value["profile_sha256"],
                "thread_id": thread_id,
                "turn_id": turn_id,
                "completed_agent_message_ids": [message_id],
                "requested_identity": identity,
                "observed_identity": identity,
                "usage": {
                    "input_tokens": tokens - 10,
                    "output_tokens": 10,
                    "total_tokens": tokens,
                },
                "duration_ms": transcript_duration,
                "status": status,
                "issues": [],
            },
        )
        return path, metadata_hash, receipt_id

    def invocation(
        self,
        arm: str,
        case: str,
        tokens: int,
        wall: int,
        *,
        planned_call_id: str,
        role: str,
        phase: str,
        attempt_kind: str = "initial",
        context_replay_tokens: int = 0,
        route: dict | None = None,
    ) -> dict:
        metadata_path, metadata_hash, receipt_id = self.metadata(
            arm,
            case,
            tokens,
            wall,
            route=route,
        )
        return {
            "invocation_id": f"{case}-{arm}-{planned_call_id}-{self.receipt_counter}",
            "planned_call_id": planned_call_id,
            "role": role,
            "phase": phase,
            "attempt_kind": attempt_kind,
            "context_replay_tokens": context_replay_tokens,
            "tier": (route or self.route)["tier"],
            "metadata_path": str(metadata_path),
            "metadata_sha256": metadata_hash,
            "dispatcher_receipt_id": receipt_id,
        }

    def artifact(
        self,
        cycle: dict,
        case: str,
        arm: str,
        *,
        family: str = "coding",
        partition: str = "development",
        tokens: int | None = None,
        wall: int | None = None,
        quality_control: float = 1.0,
        quality_challenger: float = 1.0,
        objective_challenger: bool = True,
        safety: dict | None = None,
        extra_invocations: list[dict] | None = None,
        route: dict | None = None,
        profile_hash: str | None = None,
        source: str = "openai-codex-app-server-workflow",
        elapsed: int | None = None,
        router_observation_id: str | None = None,
        created_at_epoch: float | None = None,
        evidence_phase: str = "prepromotion",
        canary_window_id: str | None = None,
        planned_call_id: str | None = None,
    ) -> tuple[Path, str]:
        tokens = tokens if tokens is not None else (1000 if arm == "control" else 700)
        wall = wall if wall is not None else (1000 if arm == "control" else 700)
        metadata_path, metadata_hash, dispatcher_receipt_id = self.metadata(
            arm, case, tokens, wall, route=route, profile_hash=profile_hash,
            source=source, elapsed=elapsed,
        )
        execution_receipt_ids = sorted(
            [dispatcher_receipt_id]
            + [item["dispatcher_receipt_id"] for item in (extra_invocations or [])]
        )
        quality = {
            "schema_version": 1,
            "case_id": case,
            "grader_kind": "deterministic",
            "grader_identity_sha256": self.bindings["grader_identity_sha256"],
            "rubric_sha256": self.bindings["rubric_sha256"],
            "evaluated_arm": arm,
            "evaluated_execution_receipt_ids": execution_receipt_ids,
            "arms": {
                "control": {"quality_score": quality_control, "objective_gates": {"exact": True}},
                "challenger": {
                    "quality_score": quality_challenger,
                    "objective_gates": {"exact": objective_challenger},
                },
            },
        }
        quality_path = self.root / f"{case}-{arm}-quality-{quality_control}-{quality_challenger}-{objective_challenger}.json"
        quality_hash = self.write(quality_path, quality)
        quality_receipt_id = self.append_receipt(
            "quality",
            {
                "quality_artifact_path": str(quality_path.resolve()),
                "quality_artifact_sha256": quality_hash,
                "grader_kind": "deterministic",
                "grader_identity_sha256": self.bindings["grader_identity_sha256"],
                "rubric_sha256": self.bindings["rubric_sha256"],
                "case_id": case,
                "evaluated_arm": arm,
                "evaluated_execution_receipt_ids": execution_receipt_ids,
                "producer_kind": "pinned-deterministic-grader",
                "producer_sha256": self.bindings["grader_identity_sha256"],
                "producer_path": str(ll.DETERMINISTIC_GRADER_PATH.resolve()),
            },
        )
        invocation = {
            "invocation_id": f"{case}-{arm}-worker",
            "planned_call_id": planned_call_id or f"{arm}-worker",
            "role": "worker",
            "phase": "primary",
            "attempt_kind": "initial",
            "context_replay_tokens": 0,
            "tier": (route or self.route)["tier"],
            "metadata_path": str(metadata_path),
            "metadata_sha256": metadata_hash,
            "dispatcher_receipt_id": dispatcher_receipt_id,
        }
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
            "bindings": {**self.bindings, "task_feature_sha256": cycle["task_feature_sha256"]},
            "invocations": [invocation] + (extra_invocations or []),
            "quality_artifact_path": str(quality_path),
            "quality_artifact_sha256": quality_hash,
            "quality_receipt_id": quality_receipt_id,
            "safety": safety
            or {
                "authorized_mutation": True,
                "secret_safe": True,
                "authority_safe": True,
                "critical_failure": False,
            },
            "arm_started_at_ms": 0,
            "accepted_at_ms": wall,
            "created_at_epoch": time.time() if created_at_epoch is None else created_at_epoch,
        }
        if router_observation_id:
            value["router_observation_id"] = router_observation_id
        if canary_window_id is not None:
            value["canary_window_id"] = canary_window_id
        path = self.root / f"{case}-{arm}-artifact-{tokens}-{wall}.json"
        return path, self.write(path, value)

    def pair(self, cycle: dict, case: str = "case", **overrides: object) -> None:
        for arm in ("control", "challenger"):
            path, hashed = self.artifact(cycle, case, arm, **overrides)
            ll.record(self.store, cycle["cycle_id"], str(path), hashed)

    def canary_pair(self, cycle: dict, case: str = "canary", window: str = "window-1", **overrides: object) -> None:
        for arm in ("control", "challenger"):
            path, hashed = self.artifact(
                cycle,
                case,
                arm,
                evidence_phase="canary",
                canary_window_id=window,
                **overrides,
            )
            ll.record_canary(self.store, cycle["cycle_id"], str(path), hashed)

    def qualify(self, cycle: dict) -> None:
        self.pair(cycle)
        self.assert_state(cycle, "QUALIFIED")

    def assert_state(self, cycle: dict, state: str) -> None:
        ll.advance(self.store, cycle["cycle_id"])
        if state != "COLLECTING":
            ll.advance(self.store, cycle["cycle_id"])
        actual = ll.current_state(self.store, cycle["cycle_id"])["state"]
        if actual != state:
            raise AssertionError(f"expected {state}, got {actual}")


class LearningLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.f = Fixture()

    def tearDown(self) -> None:
        self.f.cleanup()

    def test_feature_normalization_preserves_booleans_and_closes_schema(self) -> None:
        features = self.f.cycle_value()["features"]
        normalized = ll.normalize_features(features)
        self.assertIs(normalized["holdout"], False)
        with self.assertRaises(ll.LearningError):
            ll.normalize_features({**features, "surprise": "unbound"})

    def test_state_machine_and_one_causal_variable(self) -> None:
        cycle = self.f.begin()
        self.assertEqual(cycle["state"], "STAGED")
        with self.assertRaises(ll.LearningError):
            ll._append_state(self.f.store, cycle, "ROLLED_BACK", "illegal", {})
        mixed = self.f.cycle_value()
        mixed["change"] = {"variable_id": "model.route_assignment", "before": "a", "after": "b"}
        with self.assertRaises(ll.LearningError):
            ll.validate_cycle(mixed)
        duplicate = self.f.cycle_value(cycle_id="other", run_id="run-one")
        with self.assertRaises(ll.LearningError):
            ll.begin(self.f.store, duplicate)

    def test_dynamic_newest_family_no_downgrade_and_t4_invariant(self) -> None:
        catalog = [f"gpt-5.6-{name}" for name in ("luna", "terra", "sol")] + [
            f"gpt-5.7-{name}" for name in ("luna", "terra", "sol")
        ]
        self.assertEqual(ll.derive_newest_family(catalog, "5.6"), "5.7")
        with self.assertRaises(ll.LearningError):
            ll.derive_newest_family([f"gpt-5.6-{name}" for name in ("luna", "terra", "sol")], "5.7")
        with self.assertRaises(ll.LearningError):
            ll.validate_route(self.f.route, "5.7")
        t4 = {
            **self.f.route,
            "model": "gpt-5.6-sol",
            "effort": "ultra",
            "tier": "T4",
        }
        ll.validate_route(t4, "5.6", t4=True)
        with self.assertRaises(ll.LearningError):
            ll.validate_route({**t4, "sandbox": "workspace-write"}, "5.6", t4=True)

        cycle = self.f.begin("canonical-authority")
        self.assertEqual(cycle["catalog_authority"]["family"], "5.6")
        self.assertEqual(
            cycle["catalog_authority"]["semantic_sha256"],
            self.f.catalog_semantic_hash,
        )
        copied_catalog = self.f.root / "caller-selected-catalog.json"
        copied_catalog.write_bytes(self.f.catalog_path.read_bytes())
        forged_bindings = {
            **self.f.bindings,
            "catalog_path": str(copied_catalog),
            "catalog_file_sha256": ll.file_sha256(copied_catalog),
        }
        with self.assertRaises(ll.LearningError):
            self.f.begin("forged-catalog", bindings=forged_bindings)

        bad_policy_hash = self.f.write(
            self.f.router_policy_path,
            {
                "schema_version": 1,
                "policy_id": self.f.router_policy_id,
                "constraints": {
                    "model_version_window": {
                        "max_version_lag": 1,
                        "latest_observed_family": "5.6",
                        "allowed_families": ["5.5", "5.6"],
                    }
                },
            },
        )
        with self.assertRaises(ll.LearningError):
            self.f.begin(
                "forged-policy",
                bindings={
                    **self.f.bindings,
                    "router_policy_file_sha256": bad_policy_hash,
                },
            )

    def test_dynamic_budget_allows_more_than_200k_and_fixed_requires_proof(self) -> None:
        self.assertEqual(self.f.budget["worst_case_aggregate_tokens"], 375_000)
        self.assertEqual(self.f.budget["reviewer_allowance"], 10_000)
        self.assertEqual(self.f.budget["grader_allowance"], 5_000)
        ll.validate_budget_manifest(self.f.budget, self.f.shared_hash)
        bad = ll.build_budget_manifest(
            self.f.shared_hash,
            [
                {
                    "call_id": "fixed-control",
                    "arm": "control",
                    "role": "worker",
                    "phase": "primary",
                    "attempt_kind": "initial",
                    "token_cap": 10,
                    "expected_inferences": 1,
                    "context_growth_allowance": 0,
                    "wall_time_seconds": 1,
                    "execution_manifest_sha256": self.f.execution_hashes["control"],
                    "rationale": "fixed",
                    "cap_mode": "fixed",
                }
            ],
        )
        with self.assertRaises(ll.LearningError):
            ll.validate_budget_manifest(bad, self.f.shared_hash)
        omitted_allowance = dict(self.f.budget)
        omitted_allowance["worst_case_aggregate_tokens"] -= 15_000
        omitted_allowance = ll.self_hashed(omitted_allowance, "budget_manifest_sha256")
        with self.assertRaises(ll.LearningError):
            ll.validate_budget_manifest(omitted_allowance, self.f.shared_hash)

    def test_identity_mismatch_and_generic_spawn_block_without_route_contamination(self) -> None:
        for index, mode in enumerate(("mismatch", "spawn")):
            fixture = self.f if index == 0 else Fixture()
            try:
                cycle = fixture.begin(mode)
                path, hashed = fixture.artifact(cycle, mode, "control", source=("generic-spawn" if mode == "spawn" else "openai-codex-app-server-workflow"))
                if mode == "mismatch":
                    artifact = json.loads(path.read_text())
                    metadata_path = Path(artifact["invocations"][0]["metadata_path"])
                    metadata = json.loads(metadata_path.read_text())
                    metadata["server"]["observed_model"] = "gpt-5.6-luna"
                    artifact["invocations"][0]["metadata_sha256"] = fixture.write(metadata_path, metadata)
                    hashed = fixture.write(path, artifact)
                with self.assertRaises(ll.EvidenceError):
                    ll.record(fixture.store, cycle["cycle_id"], str(path), hashed)
                report = ll.evaluate_cycle(fixture.store, cycle["cycle_id"])
                self.assertEqual(report["state"], "BLOCKED_MODEL_ENFORCEMENT")
                self.assertFalse(report["active_routing_contaminated"])
            finally:
                if fixture is not self.f:
                    fixture.cleanup()

    def test_trusted_execution_and_quality_receipts_reject_caller_forgery(self) -> None:
        cycle = self.f.begin("receipt-provenance")
        path, hashed = self.f.artifact(cycle, "trusted", "control")
        observation = ll.record(self.f.store, cycle["cycle_id"], str(path), hashed)
        self.assertTrue(observation["invocations"][0]["dispatcher_receipt_id"].startswith("execution-"))
        self.assertTrue(observation["quality_receipt_id"].startswith("quality-"))
        receipt_types = {item["receipt_type"] for item in ll.trusted_receipts().values()}
        self.assertEqual(receipt_types, {"execution", "quality"})

        forged_path, forged_hash = self.f.artifact(cycle, "forged-grade", "challenger")
        forged_artifact = json.loads(forged_path.read_text(encoding="utf-8"))
        quality_path = Path(forged_artifact["quality_artifact_path"])
        quality = json.loads(quality_path.read_text(encoding="utf-8"))
        quality["arms"]["challenger"]["quality_score"] = 99.0
        forged_artifact["quality_artifact_sha256"] = self.f.write(quality_path, quality)
        forged_hash = self.f.write(forged_path, forged_artifact)
        with self.assertRaises(ll.EvidenceError):
            ll.record(self.f.store, cycle["cycle_id"], str(forged_path), forged_hash)
        report = ll.evaluate_cycle(self.f.store, cycle["cycle_id"])
        self.assertEqual(report["state"], "BLOCKED_MODEL_ENFORCEMENT")

    def test_cross_cycle_accumulation_and_holdout_isolation(self) -> None:
        requirements = {
            "development_pairs": 2,
            "task_family_count": 2,
            "holdout_pairs": 1,
            "exact_t4_controls": 0,
            "canary_windows": 2,
        }
        first = self.f.begin("first", requirements=requirements)
        path, hashed = self.f.artifact(first, "a", "control", family="coding")
        ll.record(self.f.store, first["cycle_id"], str(path), hashed)
        self.assertEqual(ll.advance(self.f.store, first["cycle_id"])["state"], "COLLECTING")
        second = self.f.begin("second", requirements=requirements, parent_cycle_id=first["cycle_id"])
        for case, family, arm in (("a", "coding", "challenger"), ("b", "operations", "control"), ("b", "operations", "challenger")):
            path, hashed = self.f.artifact(second, case, arm, family=family)
            ll.record(self.f.store, second["cycle_id"], str(path), hashed)
        self.assertEqual(ll.advance(self.f.store, second["cycle_id"])["state"], "COLLECTING")
        third = self.f.begin("third", requirements=requirements, parent_cycle_id=second["cycle_id"])
        for arm in ("control", "challenger"):
            path, hashed = self.f.artifact(third, "h", arm, family="research", partition="holdout")
            ll.record(self.f.store, third["cycle_id"], str(path), hashed)
        ll.advance(self.f.store, third["cycle_id"])
        qualified = ll.advance(self.f.store, third["cycle_id"])
        self.assertEqual(qualified["state"], "QUALIFIED")
        self.assertFalse(qualified["report"]["holdout_used_for_action_statistics"])
        updates = [e for e in self.f.store.events() if e.get("event_type") == "action_stat.update"]
        self.assertEqual(len(updates), 1)
        self.assertFalse(updates[0]["holdout_included"])

    def test_ttl_and_every_authority_drift_stales_without_deletion(self) -> None:
        created = 1000.0
        cycle = self.f.begin()
        for arm in ("control", "challenger"):
            path, hashed = self.f.artifact(cycle, "ttl", arm, created_at_epoch=created)
            ll.record(self.f.store, cycle["cycle_id"], str(path), hashed)
        self.assertEqual(ll.evaluate_cycle(self.f.store, cycle["cycle_id"], at_epoch=created + 3600)["state"], "STALE")
        for key in (
            "catalog_semantic_sha256",
            "family_window_sha256",
            "runner_sha256",
            "input_manifest_sha256",
            "grader_identity_sha256",
            "rubric_sha256",
            "evaluation_suite_sha256",
            "config_sha256",
            "run_budget_sha256",
        ):
            current = dict(self.f.bindings)
            current[key] = ll.digest(f"drift-{key}")
            with self.subTest(key=key):
                self.assertEqual(ll.evaluate_cycle(self.f.store, cycle["cycle_id"], at_epoch=created + 1, current_bindings=current)["state"], "STALE")
        before = len([e for e in self.f.store.events() if e.get("event_type") == "observation.recorded"])
        ll.sweep_stale(self.f.store, {**self.f.bindings, "catalog_semantic_sha256": ll.digest("new")}, created + 1)
        after = len([e for e in self.f.store.events() if e.get("event_type") == "observation.recorded"])
        self.assertEqual(before, after)
        self.assertTrue(any(e.get("event_type") == "evidence.stale" for e in self.f.store.events()))

    def test_missing_resource_remains_collecting(self) -> None:
        cycle = self.f.begin()
        path, hashed = self.f.artifact(cycle, "missing", "control", elapsed=0)
        with self.assertRaises(ll.EvidenceError):
            ll.record(self.f.store, cycle["cycle_id"], str(path), hashed)
        report = ll.evaluate_cycle(self.f.store, cycle["cycle_id"])
        self.assertEqual(report["state"], "COLLECTING")
        self.assertFalse(report["efficiency_scored"])

    def test_quality_and_objective_regressions_veto_efficiency(self) -> None:
        for name, quality, objective in (("quality", 0.5, True), ("objective", 1.0, False)):
            fixture = self.f if name == "quality" else Fixture()
            try:
                cycle = fixture.begin(name)
                fixture.pair(cycle, name, quality_challenger=quality, objective_challenger=objective)
                report = ll.evaluate_cycle(fixture.store, cycle["cycle_id"])
                self.assertEqual(report["state"], "REJECTED")
                self.assertFalse(report["efficiency_scored"])
            finally:
                if fixture is not self.f:
                    fixture.cleanup()

    def test_pareto_rejects_time_or_token_regression_and_ties(self) -> None:
        cases = (("time", 700, 1100), ("tokens", 1100, 700), ("tie", 1000, 1000))
        for index, (name, challenger_tokens, challenger_wall) in enumerate(cases):
            fixture = self.f if index == 0 else Fixture()
            try:
                cycle = fixture.begin(name)
                path, hashed = fixture.artifact(cycle, name, "control", tokens=1000, wall=1000)
                ll.record(fixture.store, cycle["cycle_id"], str(path), hashed)
                path, hashed = fixture.artifact(cycle, name, "challenger", tokens=challenger_tokens, wall=challenger_wall)
                ll.record(fixture.store, cycle["cycle_id"], str(path), hashed)
                report = ll.evaluate_cycle(fixture.store, cycle["cycle_id"])
                self.assertEqual(report["state"], "REJECTED")
            finally:
                if fixture is not self.f:
                    fixture.cleanup()

    def test_total_system_accounting_reviewer_overhead_reverses_worker_win(self) -> None:
        self.f.configure_plans(
            {
                "control": [self.f.plan("control-worker", "control")],
                "challenger": [
                    self.f.plan("challenger-worker", "challenger"),
                    self.f.plan(
                        "challenger-reviewer",
                        "challenger",
                        role="reviewer",
                        phase="review",
                        token_cap=1_000,
                    ),
                ],
            }
        )
        cycle = self.f.begin()
        path, hashed = self.f.artifact(cycle, "overhead", "control", tokens=1000, wall=1000)
        ll.record(self.f.store, cycle["cycle_id"], str(path), hashed)
        reviewer = self.f.invocation(
            "challenger",
            "reviewer",
            600,
            400,
            planned_call_id="challenger-reviewer",
            role="reviewer",
            phase="review",
            context_replay_tokens=100,
        )
        path, hashed = self.f.artifact(cycle, "overhead", "challenger", tokens=500, wall=900, extra_invocations=[reviewer])
        ll.record(self.f.store, cycle["cycle_id"], str(path), hashed)
        report = ll.evaluate_cycle(self.f.store, cycle["cycle_id"])
        self.assertEqual(report["state"], "REJECTED")
        current, _ = ll.experiment_observations(self.f.store, cycle)
        challenger = next(item for item in current if item["arm"] == "challenger")
        self.assertEqual(challenger["resources"]["total_tokens"], 1100)
        self.assertEqual(challenger["resources"]["reviewer_tokens"], 600)
        self.assertEqual(challenger["resources"]["invocation_count"], 2)

    def test_exact_planned_invocations_and_per_call_caps_are_fail_closed(self) -> None:
        extra = self.f.invocation(
            "challenger",
            "unplanned",
            100,
            100,
            planned_call_id="unplanned-reviewer",
            role="reviewer",
            phase="review",
        )
        cycle = self.f.begin("unplanned")
        path, hashed = self.f.artifact(
            cycle,
            "unplanned",
            "challenger",
            extra_invocations=[extra],
        )
        with self.assertRaises(ll.EvidenceError):
            ll.record(self.f.store, cycle["cycle_id"], str(path), hashed)

        second = Fixture()
        try:
            second.configure_plans(
                {
                    "control": [
                        second.plan("control-worker", "control", token_cap=500)
                    ],
                    "challenger": [second.plan("challenger-worker", "challenger")],
                }
            )
            bounded = second.begin("bounded")
            path, hashed = second.artifact(
                bounded,
                "over-cap",
                "control",
                tokens=501,
            )
            with self.assertRaises(ll.EvidenceError):
                ll.record(second.store, bounded["cycle_id"], str(path), hashed)
        finally:
            second.cleanup()

        third = Fixture()
        try:
            third.configure_plans(
                {
                    "control": [
                        third.plan("control-worker", "control"),
                        third.plan(
                            "control-reviewer",
                            "control",
                            role="reviewer",
                            phase="review",
                            token_cap=1_000,
                        ),
                    ],
                    "challenger": [third.plan("challenger-worker", "challenger")],
                }
            )
            missing = third.begin("missing-planned-call")
            path, hashed = third.artifact(missing, "missing-reviewer", "control")
            with self.assertRaises(ll.EvidenceError):
                ll.record(third.store, missing["cycle_id"], str(path), hashed)
        finally:
            third.cleanup()

    def test_sparse_escalation_compact_packets_and_one_remediation(self) -> None:
        features = self.f.cycle_value()["features"]
        self.assertEqual(ll.sparse_escalation(features, {"deterministic_complete": True})["tier"], "T0")
        self.assertEqual(ll.sparse_escalation(features, {})["tier"], "T2")
        self.assertEqual(ll.sparse_escalation({**features, "ambiguity": "high"}, {})["tier"], "T3")
        terminal = ll.sparse_escalation({**features, "terminal_strategy": True}, {})
        self.assertEqual((terminal["tier"], terminal["required_variant"], terminal["required_effort"]), ("T4", "sol", "ultra"))
        self.assertEqual(ll.sparse_escalation(features, {"remediation_count": 2})["decision"], "SURFACE_UNRESOLVED")
        packet = {
            "recommendation": "accept",
            "alternatives": [],
            "objective_results": {},
            "material_disagreements": [],
            "failure_signals": [],
            "evidence": [{"path": "/tmp/evidence.json", "sha256": ll.digest("evidence")}],
            "source_excerpts": ["bounded excerpt"],
        }
        self.assertEqual(ll.validate_review_packet(packet), packet)
        with self.assertRaises(ll.LearningError):
            ll.validate_review_packet({**packet, "source_excerpts": ["x" * 12001]})

    def test_ledger_crash_recovery_is_hash_chained_and_idempotent(self) -> None:
        fired = {"done": False}

        def fault(point: str) -> None:
            if point == "ledger_replaced" and not fired["done"]:
                fired["done"] = True
                raise RuntimeError("injected")

        self.f.store.fault_hook = fault
        with self.assertRaises(RuntimeError):
            ll.no_call(self.f.store, "crash")
        self.f.store.fault_hook = lambda _point: None
        recovered = self.f.store.recover()
        self.assertTrue(recovered["ledger"]["recovered"])
        self.assertFalse(self.f.store.recover()["ledger"]["recovered"])
        self.assertEqual(len(self.f.store.events()), 1)
        content = self.f.store.ledger.read_bytes().replace(b'"reason":"crash"', b'"reason":"drift"')
        self.f.store.ledger.write_bytes(content)
        with self.assertRaises(ll.LearningError):
            self.f.store.events()

    def test_workflow_authority_is_automatically_recovered_and_exactly_rolled_back(self) -> None:
        cycle = self.f.begin("authority-recovery")
        self.f.qualify(cycle)
        fired = {"done": False}

        def fault(point: str) -> None:
            if point == "authority_replaced" and not fired["done"]:
                fired["done"] = True
                raise RuntimeError("injected authority interruption")

        self.f.store.fault_hook = fault
        with self.assertRaises(RuntimeError):
            ll.promote_workflow(self.f.store, cycle["cycle_id"])
        self.assertTrue(self.f.store.authority_journal.exists())
        self.f.store.fault_hook = lambda _point: None

        # A normal public read, not an explicit doctor/recover command, completes the transaction.
        recovered = ll.current_state(self.f.store, cycle["cycle_id"])
        self.assertEqual(recovered["state"], "PROMOTED")
        self.assertFalse(self.f.store.authority_journal.exists())
        self.assertEqual(self.f.active_path.read_bytes(), self.f.candidate_bytes)

        rolled_back = ll.rollback_workflow(
            self.f.store,
            cycle["cycle_id"],
            "deterministic rollback proof",
        )
        self.assertEqual(rolled_back["state"], "ROLLED_BACK")
        self.assertEqual(self.f.active_path.read_bytes(), self.f.active_bytes)

    def test_qualification_freezes_evidence_and_promotion_requires_fresh_qualify(self) -> None:
        cycle = self.f.begin("fresh-qualification")
        self.f.qualify(cycle)
        path, hashed = self.f.artifact(cycle, "late", "control")
        before = len(self.f.store.events())
        with self.assertRaises(ll.LearningError):
            ll.record(self.f.store, cycle["cycle_id"], str(path), hashed)
        self.assertEqual(len(self.f.store.events()), before)
        self.assertEqual(ll.current_state(self.f.store, cycle["cycle_id"])["state"], "QUALIFIED")

        # Live authority drift makes the fresh promotion evaluation non-QUALIFY.
        catalog = json.loads(self.f.catalog_path.read_text(encoding="utf-8"))
        catalog["models"]["gpt-5.6-terra"]["instruction_hash"] = ll.digest("changed")
        catalog["semantic_hash"] = ll.digest(
            [catalog["models"][key] for key in sorted(catalog["models"])]
        )
        self.f.write(self.f.catalog_path, catalog)
        with self.assertRaises(ll.LearningError):
            ll.promote_workflow(self.f.store, cycle["cycle_id"])
        self.assertEqual(self.f.active_path.read_bytes(), self.f.active_bytes)
        self.assertEqual(ll.current_state(self.f.store, cycle["cycle_id"])["state"], "QUALIFIED")

    def test_healthy_canary_validates_after_complete_windows(self) -> None:
        cycle = self.f.begin()
        self.f.qualify(cycle)
        ll.promote_workflow(self.f.store, cycle["cycle_id"])
        collecting = ll.monitor_workflow(self.f.store, cycle["cycle_id"])
        self.assertEqual(collecting["decision"], "COLLECTING_CANARY")
        self.f.canary_pair(cycle, case="canary-one", window="window-1")
        still_collecting = ll.monitor_workflow(self.f.store, cycle["cycle_id"])
        self.assertEqual(still_collecting["decision"], "COLLECTING_CANARY")
        self.f.canary_pair(cycle, case="canary-two", window="window-2")
        result = ll.monitor_workflow(self.f.store, cycle["cycle_id"])
        self.assertEqual(result["state"], "VALIDATED")
        self.assertEqual(result["report"]["canary"]["completed_windows"], 2)

    def test_canary_regression_is_derived_from_evidence_and_auto_rolls_back(self) -> None:
        cycle = self.f.begin("canary-regression")
        self.f.qualify(cycle)
        ll.promote_workflow(self.f.store, cycle["cycle_id"])
        self.f.canary_pair(cycle, quality_challenger=0.5)
        result = ll.monitor_workflow(self.f.store, cycle["cycle_id"])
        self.assertEqual(result["state"], "ROLLED_BACK")
        self.assertEqual(self.f.active_path.read_bytes(), self.f.active_bytes)

    def test_no_call_retains_zero_experiment_calls(self) -> None:
        result = ll.no_call(self.f.store)
        self.assertEqual(result["experiment_calls"], 0)
        self.assertEqual(ll.status(self.f.store)["no_call_cycles"], 1)

    def test_model_lane_cannot_advance_or_recommend_without_router_authority(self) -> None:
        model_value = self.f.cycle_value()
        challenger_route = {**self.f.route, "model": "gpt-5.6-sol"}
        model_value.update(
            {
                "lane": "model",
                "candidate_id": "router-candidate",
                "change": {"variable_id": "model.route_assignment", "before": "terra-medium", "after": "sol-medium"},
                "arms": {
                    "control": {
                        **model_value["arms"]["control"],
                        "action": "terra-medium",
                    },
                    "challenger": {
                        **model_value["arms"]["challenger"],
                        "action": "sol-medium",
                        "route": challenger_route,
                        "profile_sha256": ll.digest("candidate-profile"),
                        "workflow_policy_sha256": model_value["arms"]["control"]["workflow_policy_sha256"],
                    },
                },
                "frozen": {"workflow_policy_sha256": model_value["arms"]["control"]["workflow_policy_sha256"]},
            }
        )
        cycle = ll.begin(self.f.store, {**model_value, "cycle_id": "model", "run_id": "model-run"})
        with self.assertRaises(ll.LearningError):
            ll.advance(self.f.store, cycle["cycle_id"])
        self.assertIsNone(ll.recommend(self.f.store, cycle["features"])["recommendation"])

    def test_serendipity_capacity_archive_replay_and_separate_scores(self) -> None:
        for index in range(9):
            self.f.begin(f"ordinary-{index}", candidate_id=f"candidate-{index}")
        with self.assertRaises(ll.LearningError):
            ll.allocate_serendipity(self.f.store, 0.2)
        allocation = ll.allocate_serendipity(self.f.store)
        self.assertEqual(allocation["fraction"], 0.1)
        self.assertEqual(allocation["slot_index"], 10)
        self.assertTrue(allocation["allocated"])
        value = self.f.cycle_value(
            lane="serendipity",
            candidate_id="novel-candidate",
            serendipity={
                "mutation_lane": "workflow",
                "predicted_immediate_improvement": False,
                "behavioral_descriptor": {"review_shape": "burst", "context": "counterfactual"},
                "minimum_quality": 0.9,
                "replay_task_families": ["research"],
                "capacity_allocation": allocation,
            },
            minimum_token_savings=0.5,
        )
        cycle = ll.begin(self.f.store, {**value, "cycle_id": "novel", "run_id": "novel-run"})
        self.f.pair(cycle, "novel", quality_challenger=0.8)
        archive = ll.archive_serendipity(self.f.store, cycle["cycle_id"])
        self.assertTrue(archive["informative_failure"])
        self.assertFalse(archive["scores"]["minimum_quality"]["passed"])
        self.assertIn("novelty", archive["scores"])
        self.assertIn("learning_progress", archive["scores"])
        self.assertIn("cross_task_transfer_value", archive["scores"])
        replay_path, replay_hash = self.f.artifact(
            cycle,
            "novel-replay",
            "challenger",
            family="research",
            partition="holdout",
            quality_challenger=1.0,
        )
        replay_observation = ll.record(
            self.f.store,
            cycle["cycle_id"],
            str(replay_path),
            replay_hash,
        )
        replay = ll.replay_archive_candidate(
            self.f.store,
            archive["archive_id"],
            {"observation_id": replay_observation["observation_id"]},
        )
        self.assertTrue(replay["transfer_pass"])
        self.assertFalse(any(e.get("event_type") == "action_stat.update" and e.get("cycle_id") == cycle["cycle_id"] for e in self.f.store.events()))
        with self.assertRaises(ll.LearningError):
            self.f.begin(
                "missing-lineage",
                candidate_id="missing-lineage-candidate",
                lineage={"archive_ids": ["not-an-archive"]},
            )
        validated = self.f.begin(
            "validated-step",
            candidate_id="validated-candidate",
            lineage={"archive_ids": [archive["archive_id"]]},
        )
        self.f.qualify(validated)
        ll.promote_workflow(self.f.store, validated["cycle_id"])
        self.f.canary_pair(validated, case="stepping-canary-one", window="window-1")
        self.f.canary_pair(validated, case="stepping-canary-two", window="window-2")
        ll.monitor_workflow(self.f.store, validated["cycle_id"])
        events = self.f.store.events()
        archive_event = next(
            event
            for event in events
            if event.get("event_type") == "novelty.archived"
            and event["archive"]["archive_id"] == archive["archive_id"]
        )
        target_begin = next(
            event
            for event in events
            if event.get("event_type") == "cycle.begin"
            and event.get("cycle_id") == validated["cycle_id"]
        )
        validated_event = next(
            event
            for event in events
            if event.get("event_type") == "cycle.state"
            and event.get("cycle_id") == validated["cycle_id"]
            and event.get("state") == "VALIDATED"
        )
        target = ll.current_state(self.f.store, validated["cycle_id"])
        self.assertLess(archive_event["sequence"], target_begin["sequence"])
        self.assertLess(target_begin["sequence"], validated_event["sequence"])
        causal_manifest = {
            "schema_version": 1,
            "archive_id": archive["archive_id"],
            "validated_cycle_id": validated["cycle_id"],
            "archive_action_sha256": archive["action_sha256"],
            "archive_event_sha256": archive_event["event_sha256"],
            "target_begin_event_sha256": target_begin["event_sha256"],
            "validated_state_event_sha256": validated_event["event_sha256"],
            "target_experiment_scope_sha256": target["experiment_scope_sha256"],
            "lineage_sha256": target["lineage"]["lineage_sha256"],
        }
        wrong_path = self.f.root / "wrong-causal-manifest.json"
        wrong_hash = self.f.write(
            wrong_path,
            {**causal_manifest, "archive_event_sha256": ll.digest("wrong-event")},
        )
        with self.assertRaises(ll.LearningError):
            ll.credit_stepping_stone(
                self.f.store,
                archive["archive_id"],
                validated["cycle_id"],
                str(wrong_path),
                wrong_hash,
            )
        causal_path = self.f.root / "causal-manifest.json"
        causal_hash = self.f.write(causal_path, causal_manifest)
        credit = ll.credit_stepping_stone(
            self.f.store,
            archive["archive_id"],
            validated["cycle_id"],
            str(causal_path),
            causal_hash,
        )
        self.assertFalse(credit["changes_promotion_state"])
        self.assertEqual(ll.status(self.f.store)["stepping_stone_credits"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
