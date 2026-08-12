#!/usr/bin/env python3
"""Small integration suite for the installed workflow catalog and route contract."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parent.parent
PLANNER = SKILL_DIR / "scripts" / "workflow_plan.py"
SPEC = importlib.util.spec_from_file_location("workflow_plan", PLANNER)
assert SPEC and SPEC.loader
WORKFLOW_PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKFLOW_PLAN)
APPLICATIONS = (
    "coding",
    "research",
    "planning",
    "debugging",
    "review",
    "data",
    "writing",
    "operations",
)


def test_environment_cannot_assert_planned_source_provenance() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-plan-unversioned-") as temporary:
        previous = os.environ.get("ADAPTIVE_WORKFLOW_SOURCE_COMMIT")
        os.environ["ADAPTIVE_WORKFLOW_SOURCE_COMMIT"] = "b" * 40
        try:
            cwd = Path(temporary)
            assert (
                WORKFLOW_PLAN.source_commit_for(cwd)
                == WORKFLOW_PLAN.UNVERSIONED_SOURCE_COMMIT
            )
            try:
                WORKFLOW_PLAN.require_authoritative_source_commit(cwd)
            except WORKFLOW_PLAN.WorkflowError as error:
                assert "Git-backed source commit" in str(error)
            else:
                raise AssertionError("unversioned planned provenance was accepted")
        finally:
            if previous is None:
                os.environ.pop("ADAPTIVE_WORKFLOW_SOURCE_COMMIT", None)
            else:
                os.environ["ADAPTIVE_WORKFLOW_SOURCE_COMMIT"] = previous


def run_plan(*arguments: str) -> dict:
    completed = subprocess.run(
        [sys.executable, str(PLANNER), "plan", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or "workflow plan failed")
    return json.loads(completed.stdout)


def run_bind(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if "--cwd" in arguments:
        cwd = Path(arguments[arguments.index("--cwd") + 1])
        probe = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=False,
        )
        if cwd.is_dir() and probe.returncode != 0:
            subprocess.run(["git", "init", "-q", str(cwd)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(cwd),
                    "-c",
                    "user.name=Harness Test",
                    "-c",
                    "user.email=harness-test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "bind source",
                ],
                check=True,
            )
    completed = subprocess.run(
        [sys.executable, str(PLANNER), "bind", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or "workflow bind failed")
    return completed


def run_activate(
    *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(PLANNER), "activate-runtime", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(
            completed.stderr.strip() or "workflow runtime activation failed"
        )
    return completed


def run_verify_plan(
    *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(PLANNER), "verify-plan", *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or "workflow plan verification failed")
    return completed


def phase(plan: dict, key: str) -> dict:
    return next(item for item in plan["phases"] if item["phase_key"] == key)


def test_catalog_and_default_workflows() -> None:
    completed = subprocess.run(
        [sys.executable, str(PLANNER), "validate"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    for application in APPLICATIONS:
        result = run_plan("--application", application, "--objective", application)
        assert result["router_policy_id"]
        assert result["workflow_hashes"]
        assert (
            result["execution_identity"]
            == "REQUESTED_ROUTES_PENDING_RUNTIME_EVIDENCE"
        )
        for item in result["phases"]:
            resolution = item.get("route_resolution")
            if not resolution or resolution["mode"] != "model":
                continue
            assert resolution["provider"] == "openai"
            assert resolution["service_tier"] == "default"
            assert (
                resolution["execution_identity"]
                == "REQUESTED_PENDING_SERVER_METADATA"
            )
            assert resolution["runtime_evidence_required"] is True
            assert resolution["runtime_attestation_required"] is False


def test_conditions_and_completion_frontiers() -> None:
    coding = run_plan(
        "--application",
        "coding",
        "--objective",
        "delivery",
        "--enable-condition",
        "delivery_requested",
    )
    assert coding["completion_frontiers"]["coding.change"].endswith("poll_delivery")
    assert (
        phase(coding, "coding.change:execute_delivery")["execution"] == "parent_action"
    )
    assert (
        phase(coding, "coding.change:authorize_delivery")["execution"] == "parent_gate"
    )

    mixed = run_plan(
        "--application",
        "research",
        "--application",
        "coding",
        "--objective",
        "research then code",
    )
    assert phase(mixed, "coding.change:frame")["depends_on"] == [
        "research.synthesis:deliver"
    ]


def test_visual_and_risk_routing() -> None:
    visual = run_plan(
        "--application",
        "operations",
        "--objective",
        "visual verification",
        "--visual-required",
    )
    verify = phase(visual, "operations.release:verify")
    assert verify["route_request"]["visual_required"] is True
    assert verify["route_resolution"]["mode"] == "model"
    assert verify["route_resolution"]["tier"] == "T2"

    hard = run_plan(
        "--application",
        "planning",
        "--objective",
        "novel critical plan",
        "--ambiguity",
        "novel",
        "--risk-level",
        "critical",
        "--risk-tag",
        "security",
    )
    assert phase(hard, "planning.decision:frame")["route_resolution"]["tier"] == "T4"


def test_release_dry_run_is_read_only_even_when_routed_to_t4() -> None:
    plan = run_plan(
        "--application",
        "operations.release",
        "--objective",
        "Validate a critical novel release without performing the release.",
        "--scope",
        "cross_system",
        "--ambiguity",
        "novel",
        "--mutation",
        "none",
        "--risk-level",
        "critical",
        "--risk-scope",
        "judgment",
    )
    dry_run = phase(plan, "operations.release:dry_run")
    assert dry_run["workflow_version"] == 2
    assert dry_run["route_request"]["mutation"] == "none"
    assert dry_run["route_resolution"]["tier"] == "T4"
    assert dry_run["route_resolution"]["agent"] == "ultra_planner"
    assert dry_run["route_resolution"]["effort"] == "ultra"
    assert dry_run["route_resolution"]["parent_gate_required"] is False
    assert phase(plan, "operations.release:authorize")["execution"] == "parent_gate"
    assert phase(plan, "operations.release:execute")["execution"] == "parent_action"

    mutating_plan = run_plan(
        "--application",
        "operations.release",
        "--objective",
        "Validate and then prepare an authorized critical novel release.",
        "--scope",
        "cross_system",
        "--ambiguity",
        "novel",
        "--mutation",
        "reversible",
        "--risk-level",
        "critical",
        "--risk-scope",
        "judgment",
    )
    mutating_dry_run = phase(mutating_plan, "operations.release:dry_run")
    assert mutating_dry_run["route_request"]["mutation"] == "none"
    assert mutating_dry_run["route_resolution"]["tier"] == "T4"
    with tempfile.TemporaryDirectory(prefix="workflow-release-dry-run-") as temporary:
        root = Path(temporary)
        plan_path = root / "plan.json"
        prompt_path = root / "prompt.txt"
        cwd = root / "workspace"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        prompt_path.write_text("Validate the bound release packet.\n", encoding="utf-8")
        cwd.mkdir()
        packet = json.loads(
            run_bind(
                "--plan",
                str(plan_path),
                "--phase-key",
                dry_run["phase_key"],
                "--prompt-file",
                str(prompt_path),
                "--cwd",
                str(cwd),
                "--sandbox",
                "read-only",
                "--tool-mode",
                "none",
                "--wall-time-seconds",
                "60",
            ).stdout
        )
        assert packet["runtime_contract"] == {
            "sandbox": "read-only",
            "network_access": False,
            "tool_mode": "none",
            "mutation_authorized": False,
            "wall_time_seconds": 60,
            "requested_budget_limits": {
                "token_cap": None,
                "model_cycle_cap": None,
                "tool_cycle_cap": None,
            },
            "token_cap_contract_sha256": None,
            "budget_increase_contract_sha256": None,
        }


def test_invalid_condition_fails_closed() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(PLANNER),
            "plan",
            "--application",
            "operations",
            "--objective",
            "invalid condition",
            "--enable-condition",
            "operation_not_successful",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "enabled conditions" in completed.stderr


def test_legacy_external_verifier_contract_is_narrowly_compatible() -> None:
    plan = run_plan("--application", "coding", "--objective", "compatibility")
    routed = next(
        item
        for item in plan["phases"]
        if item.get("route_resolution", {}).get("mode") == "model"
    )
    request = routed["route_request"]
    legacy = copy.deepcopy(routed["route_resolution"])
    legacy["execution_identity"] = "REQUESTED_NOT_ATTESTED"
    legacy["runtime_evidence_required"] = True
    legacy["runtime_attestation_required"] = True
    WORKFLOW_PLAN.validate_resolution(request, legacy)
    legacy["runtime_evidence_required"] = False
    try:
        WORKFLOW_PLAN.validate_resolution(request, legacy)
    except WORKFLOW_PLAN.WorkflowError:
        pass
    else:
        raise AssertionError("contradictory legacy evidence contract was accepted")


def test_dispatch_packet_binds_exact_phase_and_runtime_inputs() -> None:
    plan = run_plan("--application", "coding", "--objective", "bind inputs")
    routed = next(
        item
        for item in plan["phases"]
        if item.get("activation") == "active"
        and item.get("route_resolution", {}).get("mode") == "model"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        plan_path = root / "plan.json"
        prompt_path = root / "prompt.txt"
        context_one = root / "context-one.txt"
        context_two = root / "context-two.txt"
        packet_output = root / "dispatch-packet.json"
        cwd = root / "workspace"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        prompt_raw = b"Preserve exact CRLF bytes.\r\n"
        context_one_raw = "first context\n".encode("utf-8")
        context_two_raw = "second context: caf\u00e9\n".encode("utf-8")
        prompt_path.write_bytes(prompt_raw)
        context_one.write_bytes(context_one_raw)
        context_two.write_bytes(context_two_raw)
        cwd.mkdir()
        arguments = (
            "--plan",
            str(plan_path),
            "--phase-key",
            routed["phase_key"],
            "--prompt-file",
            str(prompt_path),
            "--context-file",
            str(context_two),
            "--context-file",
            str(context_one),
            "--cwd",
            str(cwd),
            "--sandbox",
            "read-only",
            "--tool-mode",
            "none",
            "--wall-time-seconds",
            "73",
            "--output",
            str(packet_output),
        )
        packet = json.loads(run_bind(*arguments).stdout)
        assert json.loads(packet_output.read_text(encoding="utf-8")) == packet
        repeated = json.loads(run_bind(*arguments).stdout)
        assert packet == repeated
        assert set(packet) == {
            "schema_version",
            "planner_contract_version",
            "plan_id",
            "router_policy_id",
            "phase_key",
            "phase_contract_sha256",
            "prompt_sha256",
            "context_files",
            "context_bundle",
            "cwd",
            "runtime_contract",
            "required_skills",
            "skill_hashes",
            "source_commit",
            "dispatch_packet_sha256",
        }
        phase_identity = {
            "phase_key": routed["phase_key"],
            "workflow_id": routed["workflow_id"],
            "workflow_version": routed["workflow_version"],
            "phase_id": routed["phase_id"],
            "pattern": routed["pattern"],
            "produces": routed["produces"],
            "exit_gate": routed["exit_gate"],
            "route_request": routed["route_request"],
            "route_resolution": routed["route_resolution"],
        }
        assert packet["phase_contract_sha256"] == WORKFLOW_PLAN.content_hash(
            phase_identity
        )
        assert packet["prompt_sha256"] == hashlib.sha256(prompt_raw).hexdigest()
        assert packet["required_skills"] == []
        assert packet["skill_hashes"] == {}
        assert len(packet["source_commit"]) == 40
        assert packet["context_files"] == [
            {
                "path": str(context_two.resolve()),
                "sha256": hashlib.sha256(context_two_raw).hexdigest(),
                "bytes": len(context_two_raw),
            },
            {
                "path": str(context_one.resolve()),
                "sha256": hashlib.sha256(context_one_raw).hexdigest(),
                "bytes": len(context_one_raw),
            },
        ]
        assert packet["context_bundle"] == WORKFLOW_PLAN.context_bundle_record(
            packet["context_files"]
        )
        assert packet["cwd"] == str(cwd.resolve())
        assert packet["runtime_contract"] == {
            "sandbox": "read-only",
            "network_access": False,
            "tool_mode": "none",
            "mutation_authorized": False,
            "wall_time_seconds": 73,
            "requested_budget_limits": {
                "token_cap": None,
                "model_cycle_cap": None,
                "tool_cycle_cap": None,
            },
            "token_cap_contract_sha256": None,
            "budget_increase_contract_sha256": None,
        }
        packet_identity = dict(packet)
        packet_sha256 = packet_identity.pop("dispatch_packet_sha256")
        assert packet_sha256 == WORKFLOW_PLAN.content_hash(packet_identity)


def test_resumable_packet_is_explicit_policy_bound_and_legacy_safe() -> None:
    plan = run_plan("--application", "coding", "--objective", "resume safely")
    phase = next(
        item
        for item in plan["phases"]
        if item.get("activation") == "active"
        and item.get("route_request", {}).get("mutation") == "none"
        and item.get("route_resolution", {}).get("mode") == "model"
    )
    with tempfile.TemporaryDirectory(prefix="workflow-resume-packet-") as temporary:
        root = Path(temporary)
        plan_path = root / "plan.json"
        prompt_path = root / "prompt.txt"
        manifest_path = root / "work-manifest.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        prompt_path.write_text("Complete unit-a and unit-b.\n", encoding="utf-8")
        manifest_identity = {
            "schema_version": 1,
            "contract_name": "adaptive-workflow.resume-work-manifest",
            "contract_version": 1,
            "work_items": [
                {"id": "unit-a", "description": "Complete unit A."},
                {"id": "unit-b", "description": "Complete unit B."},
            ],
        }
        manifest_path.write_text(
            json.dumps(
                {
                    **manifest_identity,
                    "manifest_sha256": WORKFLOW_PLAN.content_hash(
                        manifest_identity
                    ),
                }
            ),
            encoding="utf-8",
        )
        legacy = json.loads(
            run_bind(
                "--plan",
                str(plan_path),
                "--phase-key",
                phase["phase_key"],
                "--prompt-file",
                str(prompt_path),
                "--cwd",
                str(root),
                "--sandbox",
                "read-only",
                "--tool-mode",
                "none",
                "--wall-time-seconds",
                "10",
            ).stdout
        )
        assert legacy["schema_version"] == 1
        assert "resume_contract" not in legacy
        arguments = (
            "--plan",
            str(plan_path),
            "--phase-key",
            phase["phase_key"],
            "--prompt-file",
            str(prompt_path),
            "--cwd",
            str(root),
            "--sandbox",
            "read-only",
            "--tool-mode",
            "none",
            "--wall-time-seconds",
            "10",
            "--resumable",
            "--work-manifest",
            str(manifest_path),
            "--max-continuations",
            "1",
            "--cumulative-wall-time-seconds",
            "20",
            "--checkpoint-window-seconds",
            "3",
            "--shutdown-window-seconds",
            "2",
            "--checkpoint-ttl-seconds",
            "3600",
        )
        packet = json.loads(run_bind(*arguments).stdout)
        assert packet["schema_version"] == 2
        contract = packet["resume_contract"]
        assert contract["mode"] == "explicit_checkpoint_restart"
        assert contract["max_continuations"] == 1
        assert contract["mutation_class"] == "none"
        assert contract["work_manifest"]["manifest_sha256"] == (
            WORKFLOW_PLAN.content_hash(manifest_identity)
        )
        bare_contract = dict(contract)
        supplied_contract_hash = bare_contract.pop("resume_contract_sha256")
        assert supplied_contract_hash == WORKFLOW_PLAN.content_hash(bare_contract)
        missing_authority = run_bind(
            "--plan",
            str(plan_path),
            "--phase-key",
            phase["phase_key"],
            "--prompt-file",
            str(prompt_path),
            "--cwd",
            str(root),
            "--sandbox",
            "read-only",
            "--tool-mode",
            "none",
            "--wall-time-seconds",
            "10",
            "--max-continuations",
            "1",
            check=False,
        )
        assert missing_authority.returncode != 0
        assert "require --resumable" in missing_authority.stderr


def test_dispatch_packet_rejects_inactive_and_unsafe_inputs() -> None:
    plan = run_plan("--application", "coding", "--objective", "reject inputs")
    active = next(
        item
        for item in plan["phases"]
        if item.get("activation") == "active"
        and item.get("route_resolution", {}).get("mode") == "model"
    )
    inactive = next(
        item
        for item in plan["phases"]
        if item.get("activation") == "runtime_condition"
        and item.get("route_resolution", {}).get("mode") == "model"
    )
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        plan_path = root / "plan.json"
        prompt_path = root / "prompt.txt"
        prompt_link = root / "prompt-link.txt"
        invalid_context = root / "invalid-context.txt"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        prompt_path.write_text("prompt\n", encoding="utf-8")
        prompt_link.symlink_to(prompt_path)
        invalid_context.write_bytes(b"\xff")

        def attempt(
            phase_key: str,
            prompt: Path,
            context: Path | None = None,
            cwd: Path = root,
            sandbox: str = "read-only",
            tool_mode: str = "default",
            network_access: bool = False,
        ) -> subprocess.CompletedProcess[str]:
            arguments = [
                "--plan",
                str(plan_path),
                "--phase-key",
                phase_key,
                "--prompt-file",
                str(prompt),
            ]
            if context is not None:
                arguments.extend(("--context-file", str(context)))
            arguments.extend(
                (
                    "--cwd",
                    str(cwd),
                    "--sandbox",
                    sandbox,
                    "--tool-mode",
                    tool_mode,
                    "--wall-time-seconds",
                    "30",
                )
            )
            if network_access:
                arguments.append("--network-access")
            return run_bind(*arguments, check=False)

        symlinked = attempt(active["phase_key"], prompt_link)
        assert symlinked.returncode != 0
        assert "missing or unsafe" in symlinked.stderr
        non_utf8 = attempt(active["phase_key"], prompt_path, invalid_context)
        assert non_utf8.returncode != 0
        assert "not UTF-8" in non_utf8.stderr
        missing_cwd = attempt(
            active["phase_key"], prompt_path, cwd=root / "does-not-exist"
        )
        assert missing_cwd.returncode != 0
        assert "cwd does not exist" in missing_cwd.stderr
        not_active = attempt(inactive["phase_key"], prompt_path)
        assert not_active.returncode != 0
        assert "phase is not active" in not_active.stderr
        unsafe_no_tools = attempt(
            active["phase_key"],
            prompt_path,
            sandbox="workspace-write",
            tool_mode="none",
            network_access=True,
        )
        assert unsafe_no_tools.returncode != 0
        assert "no-tools dispatch requires read-only" in unsafe_no_tools.stderr


def test_runtime_condition_activation_is_evidence_gated() -> None:
    plan = run_plan(
        "--application", "coding", "--objective", "activate failed verification"
    )
    runtime_phase = next(
        item
        for item in plan["phases"]
        if item.get("activation") == "runtime_condition"
        and item.get("route_resolution", {}).get("mode") == "model"
    )
    source_phase_key = runtime_phase["depends_on"][0]
    source_phase = phase(plan, source_phase_key)
    with tempfile.TemporaryDirectory(prefix="workflow-runtime-activation-") as temporary:
        root = Path(temporary)
        plan_path = root / "plan.json"
        execution_path = root / "deterministic-execution.json"
        produced_path = root / "verification-record.json"
        phase_result_path = root / "phase-result.json"
        contract_path = root / "activation-contract.json"
        output_path = root / "activated-plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        execution_path.write_text(
            '{"command":"exact-check","exit_code":1}\n', encoding="utf-8"
        )
        produced_path.write_text(
            '{"status":"failed","check":"deterministic"}\n', encoding="utf-8"
        )
        phase_result_identity = {
            "schema_version": 1,
            "contract_name": WORKFLOW_PLAN.PHASE_RESULT_CONTRACT_NAME,
            "contract_version": 1,
            "plan_id": plan["plan_id"],
            "phase_key": source_phase_key,
            "phase_contract_sha256": WORKFLOW_PLAN.content_hash(
                WORKFLOW_PLAN.phase_result_contract(source_phase)
            ),
            "triggered_conditions": [runtime_phase["condition"]],
            "execution_record_path": str(execution_path.resolve()),
            "execution_record_sha256": hashlib.sha256(
                execution_path.read_bytes()
            ).hexdigest(),
            "artifacts": [
                {
                    "name": source_phase["produces"][0],
                    "path": str(produced_path.resolve()),
                    "sha256": hashlib.sha256(produced_path.read_bytes()).hexdigest(),
                }
            ],
        }
        phase_result_path.write_text(
            json.dumps(
                {
                    **phase_result_identity,
                    "contract_sha256": WORKFLOW_PLAN.content_hash(
                        phase_result_identity
                    ),
                }
            ),
            encoding="utf-8",
        )
        identity = {
            "schema_version": 1,
            "contract_name": WORKFLOW_PLAN.RUNTIME_ACTIVATION_CONTRACT_NAME,
            "contract_version": 1,
            "plan_id": plan["plan_id"],
            "phase_key": runtime_phase["phase_key"],
            "condition": runtime_phase["condition"],
            "triggered": True,
            "source_phase_result_path": str(phase_result_path.resolve()),
            "source_phase_result_sha256": hashlib.sha256(
                phase_result_path.read_bytes()
            ).hexdigest(),
        }
        contract_path.write_text(
            json.dumps(
                {
                    **identity,
                    "contract_sha256": WORKFLOW_PLAN.content_hash(identity),
                }
            ),
            encoding="utf-8",
        )
        activated = json.loads(
            run_activate(
                "--plan",
                str(plan_path),
                "--phase-key",
                runtime_phase["phase_key"],
                "--evidence-contract",
                str(contract_path),
                "--output",
                str(output_path),
            ).stdout
        )
        assert json.loads(output_path.read_text(encoding="utf-8")) == activated
        activated_phase = phase(activated, runtime_phase["phase_key"])
        assert activated_phase["activation"] == "active"
        assert (
            activated_phase["runtime_activation"]["source_phase_result_sha256"]
            == identity["source_phase_result_sha256"]
        )
        WORKFLOW_PLAN.validate_dispatch_plan(activated)
        WORKFLOW_PLAN.select_dispatch_phase(
            activated, runtime_phase["phase_key"]
        )
        verified = json.loads(
            run_verify_plan("--plan", str(output_path)).stdout
        )
        assert verified["status"] == "VERIFIED"
        produced_path.write_text('{"status":"changed"}\n', encoding="utf-8")
        rejected = run_activate(
            "--plan",
            str(plan_path),
            "--phase-key",
            runtime_phase["phase_key"],
            "--evidence-contract",
            str(contract_path),
            "--output",
            str(root / "must-not-exist.json"),
            check=False,
        )
        assert rejected.returncode != 0
        assert "produced artifact hash does not match" in rejected.stderr


def test_plan_authority_rejects_self_rehashed_catalog_drift() -> None:
    plan = run_plan("--application", "review", "--objective", "reject forged plan")
    forged = copy.deepcopy(plan)
    routed = next(
        item
        for item in forged["phases"]
        if item.get("activation") == "active"
        and item.get("route_resolution", {}).get("mode") == "model"
    )
    routed["produces"] = ["forged_artifact"]
    forged["plan_id"] = WORKFLOW_PLAN.content_hash(
        WORKFLOW_PLAN.plan_identity(forged)
    )
    with tempfile.TemporaryDirectory(prefix="workflow-forged-plan-") as temporary:
        path = Path(temporary) / "forged-plan.json"
        path.write_text(json.dumps(forged), encoding="utf-8")
        rejected = run_verify_plan("--plan", str(path), check=False)
        assert rejected.returncode != 0
        assert "trusted workflow catalog" in rejected.stderr


def test_plan_authority_ignores_evaluation_only_router_health() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-router-health-") as temporary:
        root = Path(temporary)
        router = root / "alternating-router.py"
        state = root / "router-state.txt"
        plan_path = root / "plan.json"
        router.write_text(
            f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path

state_path = Path({str(state)!r})
try:
    call_count = int(state_path.read_text(encoding="utf-8"))
except FileNotFoundError:
    call_count = 0
state_path.write_text(str(call_count + 1), encoding="utf-8")
request = json.load(sys.stdin)
evaluation_status = "EVALUATED" if call_count % 2 else "PENDING"
catalog_status = "CURRENT" if call_count % 2 else "STALE"
result = {{
    "schema_version": 1,
    "workflow_id": request["workflow_id"],
    "workflow_version": request["workflow_version"],
    "phase_id": request["phase_id"],
    "policy_id": "deterministic-fake-policy",
    "route_facts": {{
        key: request[key]
        for key in (
            "activity", "mutation", "scope", "ambiguity", "risk_level",
            "risk_scope", "risk_tags", "external_action",
        )
    }},
    "web_required": request["current_info_required"],
    "visual_required": request["visual_required"],
    "parent_gate_required": False,
    "external_mutation_authorized": False,
    "mode": "model",
    "tier": "T1",
    "agent": "deterministic_fake",
    "provider": "fake",
    "service_tier": "test",
    "model": "fake-model",
    "effort": "low",
    "profile_file": "fake-profile.json",
    "profile_sha256": "fake-profile-sha256",
    "execution_identity": "REQUESTED_PENDING_SERVER_METADATA",
    "runtime_evidence_required": True,
    "runtime_attestation_required": False,
    "router_health": {{
        "routing_status": "HEALTHY",
        "evaluation_status": evaluation_status,
        "catalog_status": catalog_status,
        "maintenance_issues": [f"issue-{{call_count % 2}}"],
    }},
}}
print(json.dumps(result))
""",
            encoding="utf-8",
        )
        plan = run_plan(
            "--application",
            "coding",
            "--objective",
            "stable trusted plan",
            "--router",
            str(router),
        )
        resolutions = [
            item["route_resolution"]
            for item in plan["phases"]
            if "route_resolution" in item
        ]
        assert resolutions
        assert all(
            resolution["router_health"] == {"routing_status": "HEALTHY"}
            for resolution in resolutions
        )
        legacy = copy.deepcopy(plan)
        for index, resolution in enumerate(
            item["route_resolution"]
            for item in legacy["phases"]
            if "route_resolution" in item
        ):
            resolution["router_health"] = {
                "routing_status": "HEALTHY",
                "evaluation_status": "EVALUATED" if index % 2 else "PENDING",
                "catalog_status": "CURRENT" if index % 2 else "STALE",
                "maintenance_issues": [f"legacy-issue-{index % 2}"],
            }
        legacy["plan_id"] = WORKFLOW_PLAN.content_hash(
            WORKFLOW_PLAN.plan_identity(legacy)
        )
        plan_path.write_text(json.dumps(legacy), encoding="utf-8")
        verified = json.loads(
            run_verify_plan(
                "--plan", str(plan_path), "--router", str(router)
            ).stdout
        )
        assert verified["status"] == "VERIFIED"
        assert verified["plan_id"] == legacy["plan_id"]
        unhealthy = copy.deepcopy(legacy)
        next(
            item["route_resolution"]
            for item in unhealthy["phases"]
            if "route_resolution" in item
        )["router_health"]["routing_status"] = "DEGRADED"
        unhealthy["plan_id"] = WORKFLOW_PLAN.content_hash(
            WORKFLOW_PLAN.plan_identity(unhealthy)
        )
        plan_path.write_text(json.dumps(unhealthy), encoding="utf-8")
        rejected = run_verify_plan(
            "--plan", str(plan_path), "--router", str(router), check=False
        )
        assert rejected.returncode != 0
        assert "unhealthy routing status" in rejected.stderr


if __name__ == "__main__":
    for test in (
        test_environment_cannot_assert_planned_source_provenance,
        test_catalog_and_default_workflows,
        test_conditions_and_completion_frontiers,
        test_visual_and_risk_routing,
        test_release_dry_run_is_read_only_even_when_routed_to_t4,
        test_invalid_condition_fails_closed,
        test_legacy_external_verifier_contract_is_narrowly_compatible,
        test_dispatch_packet_binds_exact_phase_and_runtime_inputs,
        test_resumable_packet_is_explicit_policy_bound_and_legacy_safe,
        test_dispatch_packet_rejects_inactive_and_unsafe_inputs,
        test_runtime_condition_activation_is_evidence_gated,
        test_plan_authority_rejects_self_rehashed_catalog_drift,
        test_plan_authority_ignores_evaluation_only_router_health,
    ):
        test()
    print("workflow-plan integration tests passed")
