#!/usr/bin/env python3
"""Deterministic tests for the Claude headless dispatcher.

No real ``claude`` process is ever spawned. ``FakeClaudeCLI`` is injected the
same way the Codex suite injects ``FakeAppServer`` (test_workflow_dispatch.py
:2622 defines the double, :2706 injects it), so hosted CI can prove the exact
flag assembly, stream retention, metadata extraction, receipt chain, drift
rejection, and admission barrier with no runner and no spend.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DISPATCH = _load("workflow_dispatch")
PLANNER = SCRIPTS / "workflow_plan.py"


class FakeClaudeCLI(DISPATCH.ClaudeCLI):
    """Record the exact assembled command and emit a canned stream-json turn."""

    binary = "fake-claude"
    calls: list[dict[str, Any]] = []
    result_overrides: dict[str, Any] = {}
    returncode = 0
    timeout_partial: str | None = None  # if set, simulate a wall-time timeout

    def run(
        self, command: list[str], *, prompt: str, cwd: Path, timeout: int
    ) -> tuple[int, str, str]:
        type(self).calls.append(
            {
                "command": list(command),
                "prompt": prompt,
                "cwd": str(cwd),
                "timeout": timeout,
            }
        )
        if type(self).timeout_partial is not None:
            return DISPATCH.DISPATCH_TIMEOUT_RETURNCODE, type(self).timeout_partial, ""
        events = [
            {"type": "system", "subtype": "init", "session_id": "sess-abc"},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "done"}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-abc",
                "duration_ms": 1234,
                "duration_api_ms": 1000,
                "num_turns": 1,
                "total_cost_usd": 0.0123,
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "modelUsage": {"sonnet": {"inputTokens": 100, "outputTokens": 50}},
                **type(self).result_overrides,
            },
        ]
        raw = "\n".join(json.dumps(event) for event in events) + "\n"
        return type(self).returncode, raw, ""


def run_planner(*arguments: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, str(PLANNER), *arguments],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or "planner failed")
    return completed


def build_fixture(root: Path) -> dict[str, Any]:
    """Produce a real plan + real planner-bound dispatch packet."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    plan = json.loads(
        run_planner(
            "plan", "--application", "coding", "--objective", "dispatch fixture"
        ).stdout
    )
    phase = next(
        item
        for item in plan["phases"]
        if item.get("activation") == "active"
        and item.get("route_resolution", {}).get("mode") == "model"
        and item.get("route_resolution", {}).get("tier") in {"T1", "T2", "T3"}
    )
    plan_path = root / "plan.json"
    prompt_path = root / "prompt.txt"
    cap_contract_path = root / "packet.cap.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    prompt_path.write_text("Frame the change.\n", encoding="utf-8")
    packet = json.loads(
        run_planner(
            "bind",
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
            "60",
            "--token-cap-contract-output",
            str(cap_contract_path),
        ).stdout
    )
    packet_path = root / "packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    # A second packet over the same plan and prompt, bound to the mutating lane
    # that has to run its own verification inside `cwd`.
    workspace_packet = json.loads(
        run_planner(
            "bind",
            "--plan",
            str(plan_path),
            "--phase-key",
            phase["phase_key"],
            "--prompt-file",
            str(prompt_path),
            "--cwd",
            str(root),
            "--sandbox",
            "workspace-write",
            "--tool-mode",
            "default",
            "--mutation-authorized",
            "--wall-time-seconds",
            "60",
            "--token-cap-contract-output",
            str(root / "workspace.cap.json"),
        ).stdout
    )
    workspace_packet_path = root / "workspace-packet.json"
    workspace_packet_path.write_text(json.dumps(workspace_packet), encoding="utf-8")
    return {
        "plan": plan,
        "plan_path": plan_path,
        "phase": phase,
        "prompt_path": prompt_path,
        "packet": packet,
        "packet_path": packet_path,
        "cap_contract_path": cap_contract_path,
        "workspace_packet": workspace_packet,
        "workspace_packet_path": workspace_packet_path,
        "workspace_cap_contract_path": root / "workspace.cap.json",
        "root": root,
    }


def run_args(fixture: dict[str, Any], **overrides: Any) -> argparse.Namespace:
    values = {
        "plan": fixture["plan_path"],
        "phase_key": fixture["phase"]["phase_key"],
        "dispatch_packet": fixture["packet_path"],
        "prompt_file": fixture["prompt_path"],
        "sandbox": "read-only",
        "network_access": False,
        "tool_mode": "none",
        "mutation_authorized": False,
        "wall_time_seconds": 60,
        "token_cap": None,
        "token_cap_contract": fixture["cap_contract_path"],
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def reset_fake() -> None:
    FakeClaudeCLI.calls = []
    FakeClaudeCLI.result_overrides = {}
    FakeClaudeCLI.returncode = 0
    FakeClaudeCLI.timeout_partial = None
    _isolate_state()


def _isolate_state() -> None:
    """Redirect every state-writing path into a fresh per-test sandbox.

    State-writing tests must NEVER touch the live ~/.claude registry: on a
    real machine the registry already holds receipts, so genesis-hash
    assertions fail (the installed-verify failure this fixes) — and worse,
    the old cleanup pattern unlinked the operator's real receipt chain.
    Read-only tests (e.g. the router admission check) intentionally do not
    call reset_fake() and keep reading the live home, which is exactly what
    installed verification wants to certify.
    """
    sandbox = Path(tempfile.mkdtemp(prefix="claude-dispatch-state-"))
    DISPATCH.STATE_HOME = sandbox
    DISPATCH.RUNS = sandbox / "runs"
    DISPATCH.EXECUTION_REGISTRY = sandbox / "execution-registry.jsonl"
    DISPATCH.EXECUTION_REGISTRY_LOCK = sandbox / "execution-registry.jsonl.lock"
    DISPATCH.EXECUTION_REGISTRY_JOURNAL = (
        sandbox / "execution-registry.jsonl-journal.json"
    )


# ---------------------------------------------------------------------------


def test_flag_assembly_is_exact() -> None:
    client = FakeClaudeCLI()
    command = client.build_command(
        model="sonnet",
        effort="medium",
        max_budget_usd=1.0,
        permission_mode="plan",
        disallowed_tools=("Write", "Bash"),
    )
    assert command == [
        "fake-claude",
        "-p",
        "--model",
        "sonnet",
        "--effort",
        "medium",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-budget-usd",
        "1.0000",
        "--permission-mode",
        "plan",
        "--disallowedTools",
        "Write,Bash",
    ]
    bare = client.build_command(
        model="haiku",
        effort="low",
        max_budget_usd=0.5,
        permission_mode="acceptEdits",
        disallowed_tools=(),
    )
    assert "--disallowedTools" not in bare
    assert "--allowedTools" not in bare

    # Both tool-list flags are variadic in the real CLI, so they must remain the
    # trailing flags (the prompt travels on stdin) and each is one comma-joined
    # value that preserves patterns containing spaces.
    verifying = client.build_command(
        model="sonnet",
        effort="high",
        max_budget_usd=2.0,
        permission_mode="acceptEdits",
        disallowed_tools=("WebFetch", "WebSearch"),
        allowed_tools=("Bash(npm test:*)", "Bash(pytest:*)"),
    )
    assert verifying[-4:] == [
        "--allowedTools",
        "Bash(npm test:*),Bash(pytest:*)",
        "--disallowedTools",
        "WebFetch,WebSearch",
    ]


def test_max_effort_is_refused_not_downgraded() -> None:
    client = FakeClaudeCLI()
    for effort in ("max", "xhigh", "ultra", ""):
        try:
            client.build_command(
                model="fable",
                effort=effort,
                max_budget_usd=1.0,
                permission_mode="plan",
                disallowed_tools=(),
            )
        except DISPATCH.DispatchError as error:
            assert "T4_HEADLESS_UNSUPPORTED" in str(error)
        else:  # pragma: no cover - guarded above
            raise AssertionError(f"effort {effort!r} was not refused")


def test_run_retains_stream_extracts_metadata_and_chains_receipts(
    fixture: dict[str, Any]
) -> None:
    reset_fake()
    first = DISPATCH.command_run(run_args(fixture), CLI=FakeClaudeCLI)
    assert first["status"] == "COMPLETED"
    assert first["evidence_grade"] == "declared"
    assert first["evidence_source"] == "claude-code-headless-stream"
    assert first["scored_evidence_status"] == "BLOCKED_MODEL_ENFORCEMENT"
    assert first["session_id"] == "sess-abc"

    # the assembled command carried the resolved route and the bound permissions
    call = FakeClaudeCLI.calls[-1]
    assert call["command"][:2] == ["fake-claude", "-p"]
    assert "--output-format" in call["command"]
    assert call["command"][call["command"].index("--model") + 1] == (
        fixture["phase"]["route_resolution"]["model"]
    )
    assert call["command"][call["command"].index("--effort") + 1] in (
        "low",
        "medium",
        "high",
    )
    assert call["command"][call["command"].index("--permission-mode") + 1] == "plan"
    assert "Task" in call["command"][call["command"].index("--disallowedTools") + 1]
    assert call["timeout"] == 60

    # the raw stream was retained verbatim and hashed into the receipt
    stream_path = Path(first["stream_path"])
    assert stream_path.name == "claude-cli.0001.jsonl"
    raw = stream_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == first["stream_sha256"]
    assert json.loads(raw.splitlines()[-1])["type"] == "result"

    receipts = DISPATCH.read_registry_receipts()
    assert receipts[-1]["receipt_sha256"] == first["dispatcher_receipt_sha256"]
    receipt = receipts[-1]
    assert receipt["evidence_grade"] == "declared"
    assert receipt["runtime"] == "claude-code-headless-cli"
    assert receipt["stream_sha256"] == first["stream_sha256"]
    assert receipt["runtime_metadata"]["total_cost_usd"] == 0.0123
    assert receipt["runtime_metadata"]["evidence_source"] == (
        "claude-code-headless-metadata"
    )
    assert receipt["observed_models"] == ["sonnet"]
    assert receipt["observed_usage"] == {
        "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
    }
    assert receipt["previous_receipt_sha256"] == "0" * 64
    control_path = Path(first["control_return_path"])
    assert control_path.exists()
    assert first["control_return"]["failure_class"] == "completed"
    assert first["control_return"]["directive_sha256"] == json.loads(
        control_path.read_text(encoding="utf-8")
    )["directive_sha256"]

    second = DISPATCH.command_run(run_args(fixture), CLI=FakeClaudeCLI)
    chain = DISPATCH.read_registry_receipts()
    assert len(chain) == 2
    assert chain[1]["previous_receipt_sha256"] == chain[0]["receipt_sha256"]
    assert chain[1]["sequence"] == 2
    assert second["receipt_sequence"] == 2
    # a tampered registry line breaks the chain, fail-closed
    registry = DISPATCH.EXECUTION_REGISTRY
    lines = registry.read_bytes().splitlines()
    tampered = json.loads(lines[0])
    tampered["max_budget_usd"] = 999.0
    registry.write_bytes(
        json.dumps(tampered, sort_keys=True).encode("utf-8")
        + b"\n"
        + lines[1]
        + b"\n"
    )
    try:
        DISPATCH.read_registry_receipts()
    except DISPATCH.DispatchError as error:
        assert "hash" in str(error)
    else:  # pragma: no cover
        raise AssertionError("tampered registry was accepted")


def test_workspace_write_lane_can_verify_without_host_execution(
    fixture: dict[str, Any],
) -> None:
    """Harness issue #24 item 2: a mutating lane must run its own tests.

    ``acceptEdits`` alone auto-approves edits but refuses every Bash command
    ("This command requires approval"), which print mode can never answer, so
    the worker implemented and the coordinator verified.  The lane now carries a
    bounded ``--allowedTools`` verification vocabulary; the read-only lane is
    untouched and still receives no grant at all.
    """
    reset_fake()
    result = DISPATCH.command_run(
        run_args(
            fixture,
            dispatch_packet=fixture["workspace_packet_path"],
            token_cap_contract=fixture["workspace_cap_contract_path"],
            sandbox="workspace-write",
            tool_mode="default",
            mutation_authorized=True,
        ),
        CLI=FakeClaudeCLI,
    )
    assert result["status"] == "COMPLETED"
    command = FakeClaudeCLI.calls[-1]["command"]
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"

    granted = command[command.index("--allowedTools") + 1].split(",")
    assert granted == list(DISPATCH.WORKSPACE_VERIFICATION_ALLOWED_TOOLS)
    # bounded verification, not host-wide execution: every grant names a
    # build/test/lint runner, none is the bare tool or a raw interpreter.
    assert "Bash(pytest:*)" in granted
    assert "Bash(npm test:*)" in granted
    for pattern in granted:
        assert pattern.startswith("Bash(") and pattern.endswith(":*)")
    forbidden = {
        "Bash",
        "Bash(:*)",
        "Bash(bash:*)",
        "Bash(curl:*)",
        "Bash(git push:*)",
        "Bash(npm publish:*)",
        "Bash(pip install:*)",
        "Bash(python:*)",
        "Bash(python3:*)",
        "Bash(sh:*)",
        "Bash(sudo:*)",
    }
    assert not forbidden & set(granted)

    # the packet withheld network access, so the model's egress tools are denied
    denied = command[command.index("--disallowedTools") + 1].split(",")
    assert denied == ["WebFetch", "WebSearch"]

    receipt = DISPATCH.read_registry_receipts()[-1]
    assert receipt["permission_mode"] == "acceptEdits"
    assert receipt["allowed_tools"] == list(
        DISPATCH.WORKSPACE_VERIFICATION_ALLOWED_TOOLS
    )
    assert receipt["disallowed_tools"] == ["WebFetch", "WebSearch"]
    assert receipt["sandbox"] == "workspace-write"
    assert receipt["mutation_authorized"] is True

    # a workspace-write lane the packet did not authorize to mutate gets no
    # execution grant, and neither does a `none` tool mode
    mode, denials, grants = DISPATCH.permission_flags(
        {
            "sandbox": "workspace-write",
            "tool_mode": "default",
            "network_access": True,
            "mutation_authorized": False,
        }
    )
    assert (mode, denials, grants) == ("acceptEdits", (), ())
    assert DISPATCH.permission_flags(
        {
            "sandbox": "workspace-write",
            "tool_mode": "none",
            "network_access": False,
            "mutation_authorized": True,
        }
    ) == ("acceptEdits", DISPATCH.TOOL_MODE_NONE_DISALLOWED_TOOLS, ())

    # read-only lanes are unchanged: plan mode, the same denials, no grant
    for tool_mode, expected in (
        ("default", DISPATCH.READ_ONLY_DISALLOWED_TOOLS),
        ("none", DISPATCH.TOOL_MODE_NONE_DISALLOWED_TOOLS),
    ):
        assert DISPATCH.permission_flags(
            {
                "sandbox": "read-only",
                "tool_mode": tool_mode,
                "network_access": False,
                "mutation_authorized": True,
            }
        ) == ("plan", expected, ())
    read_only = DISPATCH.command_run(run_args(fixture), CLI=FakeClaudeCLI)
    assert read_only["status"] == "COMPLETED"
    read_only_command = FakeClaudeCLI.calls[-1]["command"]
    assert "--allowedTools" not in read_only_command
    assert read_only_command[read_only_command.index("--permission-mode") + 1] == (
        "plan"
    )
    assert read_only_command[
        read_only_command.index("--disallowedTools") + 1
    ].split(",") == list(DISPATCH.TOOL_MODE_NONE_DISALLOWED_TOOLS)
    reset_fake()


def test_terminal_result_is_mandatory(fixture: dict[str, Any]) -> None:
    reset_fake()
    try:
        DISPATCH.parse_stream('{"type": "assistant"}\n')
    except DISPATCH.DispatchError as error:
        assert "terminal result" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a stream without a result event was accepted")
    FakeClaudeCLI.result_overrides = {"is_error": True, "subtype": "error_max_turns"}
    try:
        DISPATCH.command_run(run_args(fixture), CLI=FakeClaudeCLI)
    except DISPATCH.DispatchError as error:
        assert "terminal error result" in str(error)
        assert "typed control return" in str(error)
    else:  # pragma: no cover
        raise AssertionError("an error result was accepted")
    receipt = DISPATCH.read_registry_receipts()[-1]
    assert receipt["terminal_category"] == "BUDGET_STOP"
    assert receipt["status"] == "ABORTED"
    assert (Path(receipt["stream_path"]).parent / "control-return.json").exists()
    reset_fake()


def test_cumulative_usage_cap_and_missing_usage_fail_closed(
    fixture: dict[str, Any],
) -> None:
    """The CLI's terminal usage is checked against the bound effective cap."""
    reset_fake()
    FakeClaudeCLI.result_overrides = {
        "usage": {"input_tokens": 100, "output_tokens": 50}
    }
    try:
        DISPATCH.command_run(run_args(fixture, token_cap=149), CLI=FakeClaudeCLI)
    except DISPATCH.DispatchError as error:
        # The caller cannot lower the cap at run time; make a bounded packet
        # below instead of relying on an unbound argument.
        assert "planner-bound effective cap" in str(error)
    else:  # pragma: no cover
        raise AssertionError("unbound cap override was accepted")

    # Rebind with an effective cap below the observed 150 tokens; a rehashed
    # packet alone could never accomplish this because its cap evidence binds
    # the phase/prompt/runtime tuple.
    cap_path = fixture["root"] / "low-cap.contract.json"
    bounded = json.loads(
        run_planner(
            "bind",
            "--plan", str(fixture["plan_path"]),
            "--phase-key", fixture["phase"]["phase_key"],
            "--prompt-file", str(fixture["prompt_path"]),
            "--cwd", str(fixture["root"]),
            "--sandbox", "read-only",
            "--tool-mode", "none",
            "--wall-time-seconds", "60",
            "--token-cap", "149",
            "--token-cap-contract-output", str(cap_path),
        ).stdout
    )
    bounded_path = fixture["root"] / "low-cap.packet.json"
    bounded_path.write_text(json.dumps(bounded), encoding="utf-8")
    try:
        DISPATCH.command_run(
            run_args(
                fixture,
                dispatch_packet=bounded_path,
                token_cap_contract=cap_path,
            ),
            CLI=FakeClaudeCLI,
        )
    except DISPATCH.DispatchError as error:
        assert "observed cumulative usage exceeded" in str(error)
        assert "typed control return" in str(error)
    else:  # pragma: no cover
        raise AssertionError("over-cap terminal stream was accepted")
    receipt = DISPATCH.read_registry_receipts()[-1]
    assert receipt["terminal_category"] == "BUDGET_STOP"
    assert receipt["token_cap"] == 149

    try:
        DISPATCH.observed_cumulative_tokens({"usage": {"input_tokens": 100}})
    except DISPATCH.DispatchError as error:
        assert "output_tokens" in str(error)
    else:  # pragma: no cover
        raise AssertionError("partial usage was accepted")

    FakeClaudeCLI.result_overrides = {"usage": {}}
    try:
        DISPATCH.command_run(run_args(fixture), CLI=FakeClaudeCLI)
    except DISPATCH.DispatchError as error:
        assert "cumulative usage" in str(error)
        assert "typed control return" in str(error)
    else:  # pragma: no cover
        raise AssertionError("missing terminal usage was accepted")
    receipt = DISPATCH.read_registry_receipts()[-1]
    assert receipt["terminal_category"] == "MODEL_ENFORCEMENT_FAILURE"
    assert receipt["status"] == "ABORTED"
    control = json.loads(
        (Path(receipt["stream_path"]).parent / "control-return.json").read_text(
            encoding="utf-8"
        )
    )
    assert control["failure_class"] == "model_enforcement_failure"
    reset_fake()


def test_typed_control_return_preserves_legacy_reads_and_t4_pivots_are_read_only(
    fixture: dict[str, Any],
) -> None:
    legacy_identity = {
        "schema_version": 1,
        "contract_name": DISPATCH.ADAPTIVE_CONTROL_RETURN_NAME,
        "contract_version": 1,
        "terminal_status": "ABORTED",
        "terminal_category": "SETUP_FAILURE",
        "recommended_action": "start_adaptive_workflow",
        "control_state": "OUTSIDE_ADAPTIVE_MODEL_EXECUTION",
        "allowed_parent_operations": ["t0_only"],
        "prohibited_parent_operations": ["continue_cognitive_work_in_inherited_parent_model"],
        "workflow_state": {
            "plan_bound": False, "phase_key": None,
            "resumable": False, "resume_checkpoint_present": False,
        },
    }
    legacy = {**legacy_identity, "directive_sha256": DISPATCH.content_hash(legacy_identity)}
    DISPATCH.validate_adaptive_control_return(legacy)
    assert DISPATCH.pivot_from_receipt(legacy)["action"] == "t0_repair"

    with tempfile.TemporaryDirectory(prefix="claude-v2-pivot-") as temporary:
        root = Path(temporary)
        for name in (
            "input-manifest.json",
            "prompt.txt",
            "execution-metadata.json",
            "execution-receipt.json",
            "aborted.json",
        ):
            (root / name).write_text('{"status":"ABORTED"}\n', encoding="utf-8")
        directive = DISPATCH.build_adaptive_control_return(
            terminal_status="ABORTED", terminal_category="GROUNDED_COGNITIVE_FAILURE",
            phase_key="coding:implement", root=root, cwd=fixture["root"], failed_tier="T3",
            issues=["grounded fixture"],
        )
        DISPATCH.validate_adaptive_control_return(directive)
        pivot = DISPATCH.pivot_from_receipt(directive)
        packet = pivot["packet"]
        assert packet["kind"] == "t4_consult"
        assert packet["dispatch_permitted"] is True
        assert packet["tier"] == "T4"
        assert packet["sandbox"] == "read-only"
        assert packet["network_access"] is False
        assert packet["tool_mode"] == "none"
        assert packet["mutation_authority"] is False
        assert packet["limits"] == {
            "token_cap": 16_000, "model_cycle_cap": 1,
            "tool_cycle_cap": 0, "api_call_cap": 1, "wall_time_seconds": 300,
        }
        assert packet["repository_scope"] == {
            "repository_path": str(fixture["root"].resolve()),
            "path_scope": [str(fixture["root"].resolve())],
            "source_commit": DISPATCH.source_commit_for(fixture["root"]),
        }
        assert len(directive["worktree_evidence"]["final"]["tool_visible_snapshot_sha256"]) == 64
        DISPATCH.validate_pivot(pivot)
        tampered = dict(pivot)
        tampered_packet = dict(pivot["packet"])
        tampered_packet["mutation_authority"] = True
        tampered_packet["packet_sha256"] = DISPATCH.content_hash(
            {key: value for key, value in tampered_packet.items() if key != "packet_sha256"}
        )
        tampered["packet"] = tampered_packet
        tampered["pivot_sha256"] = DISPATCH.content_hash(
            {key: value for key, value in tampered.items() if key != "pivot_sha256"}
        )
        try:
            DISPATCH.validate_pivot(tampered)
        except DISPATCH.DispatchError as error:
            assert "T4 pivot cannot authorize mutation" in str(error)
        else:
            raise AssertionError("mutating T4 pivot was accepted")
        marker = fixture["root"] / ".control-return-snapshot-tamper"
        marker.write_text("changed\n", encoding="utf-8")
        try:
            DISPATCH.validate_control_return_evidence(directive)
        except DISPATCH.DispatchError as error:
            assert "tool-visible worktree snapshot changed" in str(error)
        else:
            raise AssertionError("changed tool-visible worktree was accepted")
        finally:
            marker.unlink()
        (root / "aborted.json").write_text("{}\n", encoding="utf-8")
        try:
            DISPATCH.validate_control_return_evidence(directive)
        except DISPATCH.DispatchError as error:
            assert "hash changed" in str(error)
        else:
            raise AssertionError("changed terminal evidence was accepted")
def test_packet_and_permission_drift_are_rejected(fixture: dict[str, Any]) -> None:
    reset_fake()
    for overrides, expected in (
        ({"sandbox": "workspace-write"}, "permissions drifted"),
        ({"tool_mode": "default"}, "permissions drifted"),
        ({"network_access": True}, "permissions drifted"),
        ({"mutation_authorized": True}, "permissions drifted"),
        ({"wall_time_seconds": 61}, "permissions drifted"),
    ):
        try:
            DISPATCH.command_run(run_args(fixture, **overrides), CLI=FakeClaudeCLI)
        except DISPATCH.DispatchError as error:
            assert expected in str(error), (overrides, str(error))
        else:  # pragma: no cover
            raise AssertionError(f"{overrides} was accepted")

    # a re-hashed packet bound to a different prompt is rejected
    tampered = dict(fixture["packet"])
    tampered.pop("dispatch_packet_sha256")
    tampered["prompt_sha256"] = "0" * 64
    tampered["dispatch_packet_sha256"] = DISPATCH.content_hash(tampered)
    tampered_path = fixture["root"] / "tampered-packet.json"
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    try:
        DISPATCH.command_run(
            run_args(fixture, dispatch_packet=tampered_path), CLI=FakeClaudeCLI
        )
    except DISPATCH.DispatchError as error:
        assert "does not match the bound packet" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a prompt-drifted packet was accepted")

    # a packet whose self-hash was not refreshed is rejected outright
    broken = dict(fixture["packet"])
    broken["cwd"] = "/tmp"
    broken_path = fixture["root"] / "broken-packet.json"
    broken_path.write_text(json.dumps(broken), encoding="utf-8")
    try:
        DISPATCH.command_run(
            run_args(fixture, dispatch_packet=broken_path), CLI=FakeClaudeCLI
        )
    except DISPATCH.DispatchError as error:
        assert "self-hash is invalid" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a self-hash-drifted packet was accepted")

    # an unversioned source commit is refused (harness PR #11 rule)
    unversioned = dict(fixture["packet"])
    unversioned.pop("dispatch_packet_sha256")
    unversioned["source_commit"] = "0" * 40
    unversioned["dispatch_packet_sha256"] = DISPATCH.content_hash(unversioned)
    unversioned_path = fixture["root"] / "unversioned-packet.json"
    unversioned_path.write_text(json.dumps(unversioned), encoding="utf-8")
    try:
        DISPATCH.command_run(
            run_args(fixture, dispatch_packet=unversioned_path), CLI=FakeClaudeCLI
        )
    except DISPATCH.DispatchError as error:
        assert "unversioned harness source commit" in str(error)
    else:  # pragma: no cover
        raise AssertionError("an unversioned packet was accepted")

    # a resume contract can never appear on this surface
    resumable = dict(fixture["packet"])
    resumable.pop("dispatch_packet_sha256")
    resumable["resume_contract"] = {"mode": "explicit_checkpoint_restart"}
    resumable["dispatch_packet_sha256"] = DISPATCH.content_hash(resumable)
    resumable_path = fixture["root"] / "resumable-packet.json"
    resumable_path.write_text(json.dumps(resumable), encoding="utf-8")
    try:
        DISPATCH.command_run(
            run_args(fixture, dispatch_packet=resumable_path), CLI=FakeClaudeCLI
        )
    except DISPATCH.DispatchError as error:
        assert "ABORTED_NO_RESUMABLE_CHECKPOINT" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a resumable packet was accepted")


def test_install_journal_admission_blocks_executable_commands(root: Path) -> None:
    journal_path = root / "install-journal.json"
    journal = {
        "schema_version": 1,
        "state": "open",
        "verification_token_sha256": hashlib.sha256(b"secret").hexdigest(),
    }
    journal["self_sha256"] = hashlib.sha256(
        DISPATCH.canonical_json(journal).encode("utf-8")
    ).hexdigest()
    journal_path.write_text(json.dumps(journal), encoding="utf-8")

    for command in sorted(DISPATCH.ADMISSION_COMMANDS):
        try:
            DISPATCH.enforce_install_admission(
                command, journal_path=journal_path, environ={}
            )
        except DISPATCH.DispatchError as error:
            assert "installation is in progress" in str(error)
        else:  # pragma: no cover
            raise AssertionError(f"{command} bypassed the admission barrier")

    # the installer's own private token admits the staged dispatcher
    DISPATCH.enforce_install_admission(
        "run",
        journal_path=journal_path,
        environ={DISPATCH.INSTALL_VERIFY_TOKEN_ENV: "secret"},
    )
    # a wrong token does not
    try:
        DISPATCH.enforce_install_admission(
            "run",
            journal_path=journal_path,
            environ={DISPATCH.INSTALL_VERIFY_TOKEN_ENV: "wrong"},
        )
    except DISPATCH.DispatchError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a wrong verification token was admitted")
    # a corrupted journal fails closed
    journal_path.write_text(json.dumps({"state": "open"}), encoding="utf-8")
    try:
        DISPATCH.enforce_install_admission("run", journal_path=journal_path, environ={})
    except DISPATCH.DispatchError as error:
        assert "checksum mismatch" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a corrupted journal was admitted")
    # a non-executable command is never gated
    DISPATCH.enforce_install_admission(
        "capacity-plan", journal_path=journal_path, environ={}
    )


def test_check_reports_router_admission_fields() -> None:
    report = DISPATCH.command_check()
    assert report["status"] == "HEALTHY"
    assert report["workflow_routing_ready"] is True
    assert report["routing_status"] == "HEALTHY"
    assert report["evidence_grade"] == "declared"
    assert report["headless_efforts"] == ["low", "medium", "high"]
    assert report["in_session_tiers"] == ["T4"]
    assert report["t4_execution_modes"] == {
        "t4_consult": "UNAVAILABLE_CLAUDE_HEADLESS_ENFORCEMENT",
        "t4_diagnose": "UNAVAILABLE_CLAUDE_HEADLESS_ENFORCEMENT",
    }
    assert report["resumption"] == "ABORTED_NO_RESUMABLE_CHECKPOINT"


def test_wall_time_timeout_retains_partial_stream_and_aborted_receipt(
    fixture: dict[str, Any],
) -> None:
    reset_fake()
    partial = '{"type": "system", "subtype": "init", "session_id": "sess-timeout"}\n'
    FakeClaudeCLI.timeout_partial = partial
    try:
        DISPATCH.command_run(run_args(fixture), CLI=FakeClaudeCLI)
    except DISPATCH.DispatchError as error:
        assert "wall-time" in str(error), str(error)
    else:  # pragma: no cover
        raise AssertionError("timeout did not raise DispatchError")

    # partial stream must be retained on disk
    receipts = DISPATCH.read_registry_receipts()
    receipt = receipts[-1]
    stream_path = Path(receipt["stream_path"])
    assert stream_path.exists(), "stream file was not retained"
    assert stream_path.read_bytes() == partial.encode("utf-8")

    # receipt fields
    assert receipt["status"] == "ABORTED_WALL_TIME"
    assert receipt["execution_identity"] == "ABORTED_NO_RESUMABLE_CHECKPOINT"
    expected_sha = hashlib.sha256(partial.encode("utf-8")).hexdigest()
    assert receipt["stream_sha256"] == expected_sha
    assert receipt["stream_event_count"] == 1  # one parseable JSON line

    # receipt chain still validates (read_registry_receipts would raise on mismatch)
    DISPATCH.read_registry_receipts()
    reset_fake()


def test_close_tree_status_vocabulary_matches_governance() -> None:
    parser = DISPATCH.build_parser()

    # accepted uppercase choices parse without error
    for good in ("COMPLETED", "ABORTED", "BLOCKED"):
        args = parser.parse_args(["close-tree", "--lease-id", "x", "--status", good])
        assert args.status == good

    # lowercase and removed choices are rejected by argparse
    for bad in ("completed", "failed", "aborted"):
        try:
            parser.parse_args(["close-tree", "--lease-id", "x", "--status", bad])
        except SystemExit:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"--status {bad!r} was accepted but should be rejected")

    # every accepted choice is in the governance TERMINAL_STATUSES
    for choice in ("COMPLETED", "ABORTED", "BLOCKED"):
        assert choice in DISPATCH.governance.TERMINAL_STATUSES, (
            f"{choice!r} missing from governance.TERMINAL_STATUSES"
        )


def main() -> int:
    test_flag_assembly_is_exact()
    test_max_effort_is_refused_not_downgraded()
    test_check_reports_router_admission_fields()
    with tempfile.TemporaryDirectory(prefix="claude-dispatch-") as temporary:
        root = Path(temporary)
        fixture = build_fixture(root / "repo")
        test_run_retains_stream_extracts_metadata_and_chains_receipts(fixture)
        test_workspace_write_lane_can_verify_without_host_execution(fixture)
        test_terminal_result_is_mandatory(fixture)
        test_cumulative_usage_cap_and_missing_usage_fail_closed(fixture)
        test_typed_control_return_preserves_legacy_reads_and_t4_pivots_are_read_only(fixture)
        test_packet_and_permission_drift_are_rejected(fixture)
        test_install_journal_admission_blocks_executable_commands(root)
        test_wall_time_timeout_retains_partial_stream_and_aborted_receipt(fixture)
    test_close_tree_status_vocabulary_matches_governance()
    print("claude workflow-dispatch tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
