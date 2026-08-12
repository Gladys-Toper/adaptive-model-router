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
        ["git", "-C", str(root), "commit", "-q", "--allow-empty", "-m", "fixture"],
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
        ).stdout
    )
    packet_path = root / "packet.json"
    packet_path.write_text(json.dumps(packet), encoding="utf-8")
    return {
        "plan": plan,
        "plan_path": plan_path,
        "phase": phase,
        "prompt_path": prompt_path,
        "packet": packet,
        "packet_path": packet_path,
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
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def reset_fake() -> None:
    FakeClaudeCLI.calls = []
    FakeClaudeCLI.result_overrides = {}
    FakeClaudeCLI.returncode = 0


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
    assert receipt["previous_receipt_sha256"] == "0" * 64

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
    else:  # pragma: no cover
        raise AssertionError("an error result was accepted")
    reset_fake()


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
    assert report["resumption"] == "ABORTED_NO_RESUMABLE_CHECKPOINT"


def main() -> int:
    test_flag_assembly_is_exact()
    test_max_effort_is_refused_not_downgraded()
    test_check_reports_router_admission_fields()
    with tempfile.TemporaryDirectory(prefix="claude-dispatch-") as temporary:
        root = Path(temporary)
        fixture = build_fixture(root / "repo")
        test_run_retains_stream_extracts_metadata_and_chains_receipts(fixture)
        DISPATCH.EXECUTION_REGISTRY.unlink(missing_ok=True)
        test_terminal_result_is_mandatory(fixture)
        DISPATCH.EXECUTION_REGISTRY.unlink(missing_ok=True)
        test_packet_and_permission_drift_are_rejected(fixture)
        test_install_journal_admission_blocks_executable_commands(root)
    print("claude workflow-dispatch tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
