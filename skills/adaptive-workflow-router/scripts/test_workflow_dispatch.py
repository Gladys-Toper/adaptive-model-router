#!/usr/bin/env python3
"""Deterministic tests for workflow App Server evidence enforcement."""

from __future__ import annotations

import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Iterator


SCRIPT = Path(__file__).resolve().parent / "workflow_dispatch.py"
SPEC = importlib.util.spec_from_file_location("workflow_dispatch", SCRIPT)
assert SPEC and SPEC.loader
DISPATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DISPATCH)


REQUESTED = {
    "provider": "openai",
    "model": "gpt-5.6-luna",
    "effort": "low",
    "service_tier": "default",
}
THREAD = "thread-1"
TURN = "turn-1"
HISTORICAL_MUTATION_REQUESTED = {
    "provider": "openai",
    "model": "gpt-5.6-terra",
    "effort": "high",
    "service_tier": "default",
}
HISTORICAL_NO_TOOL_MUTATION_CASES = (
    {
        "transcript_sha256": "d31ce1de854fb55a11e0093d1da4ccc5261e2df198cd815301224d87928d4c8c",
        "thread_id": "019f6cd6-8dd7-7000-a17c-fb7c5bc2186b",
        "turn_id": "019f6cd6-8f5a-7371-8779-f57a16b85210",
        "message_id": "msg_0e483d7222659b87016a594d930d38819385d6a38a4500e691",
        "input_tokens": 255586,
        "output_tokens": 1028,
        "output_text": (
            "Blocked: this session exposes no workspace shell/file-edit tool, only "
            "external connectors; using GitHub would violate the explicit "
            "no-GitHub-mutation constraint.\n\n- Files changed: none.\n- "
            "Verification: not runnable without shell access.\n- No cloud or "
            "external mutation occurred."
        ),
    },
    {
        "transcript_sha256": "ef093db4488b0fc6105a7cdf449addc5802cc63e51f568709577bc2f84de60df",
        "thread_id": "019f6cd7-4722-75c0-bb1d-ff571e80a26f",
        "turn_id": "019f6cd7-487f-78a1-9c78-da702d920127",
        "message_id": "msg_02e30a47508ef85c016a594dbd4c1081978de1ea4f4e5fe484",
        "input_tokens": 187901,
        "output_tokens": 985,
        "output_text": (
            "Blocked: this session exposes no local shell or filesystem-edit tool—"
            "only orchestration/connectors—so I cannot safely modify or verify the "
            "authorized worktree files.\n\nNo files changed; no cloud/external "
            "mutation occurred."
        ),
    },
)


def test_install_journal_blocks_execution_except_bound_verifier() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-install-barrier-") as temporary:
        root = Path(temporary)
        journal_path = root / "install-journal.json"
        token = "private-installer-verification-token"
        journal = {
            "schema_version": 1,
            "type": "adaptive-workflow-router.install-journal",
            "transaction_id": "test-transaction",
            "verification_token_sha256": DISPATCH.sha256_bytes(
                token.encode("utf-8")
            ),
            "stage": "VERIFYING",
        }
        journal["self_sha256"] = DISPATCH.sha256_bytes(
            DISPATCH.canonical_json(journal).encode("utf-8")
        )
        journal_path.write_text(
            DISPATCH.canonical_json(journal) + "\n", encoding="utf-8"
        )
        os.chmod(journal_path, 0o600)

        for command in ("run", "resume", "smoke"):
            try:
                DISPATCH.enforce_install_admission(
                    command, journal_path=journal_path, environ={}
                )
            except DISPATCH.DispatchError as error:
                assert "installation is in progress" in str(error)
            else:
                raise AssertionError(f"install journal admitted {command}")
            try:
                DISPATCH.enforce_install_admission(
                    command,
                    journal_path=journal_path,
                    environ={DISPATCH.INSTALL_VERIFY_TOKEN_ENV: "wrong"},
                )
            except DISPATCH.DispatchError:
                pass
            else:
                raise AssertionError("wrong verification token was admitted")
            DISPATCH.enforce_install_admission(
                command,
                journal_path=journal_path,
                environ={DISPATCH.INSTALL_VERIFY_TOKEN_ENV: token},
            )

        # Non-executing health/registration commands remain inspectable.
        for command in ("check", "register-quality"):
            DISPATCH.enforce_install_admission(
                command, journal_path=journal_path, environ={}
            )

        journal["stage"] = "tampered"
        journal_path.write_text(
            DISPATCH.canonical_json(journal) + "\n", encoding="utf-8"
        )
        try:
            DISPATCH.enforce_install_admission(
                "run",
                journal_path=journal_path,
                environ={DISPATCH.INSTALL_VERIFY_TOKEN_ENV: token},
            )
        except DISPATCH.DispatchError as error:
            assert "checksum mismatch" in str(error)
        else:
            raise AssertionError("tampered journal admitted verifier token")


@contextlib.contextmanager
def isolated_registry(root: Path) -> Iterator[Path]:
    """Bind registry operations to a private deterministic test directory."""
    originals = (
        DISPATCH.EXECUTION_REGISTRY,
        DISPATCH.EXECUTION_REGISTRY_LOCK,
        DISPATCH.EXECUTION_REGISTRY_JOURNAL,
    )
    registry = root / "execution-registry.jsonl"
    DISPATCH.EXECUTION_REGISTRY = registry
    DISPATCH.EXECUTION_REGISTRY_LOCK = root / "execution-registry.jsonl.lock"
    DISPATCH.EXECUTION_REGISTRY_JOURNAL = (
        root / "execution-registry.jsonl-journal.json"
    )
    try:
        yield registry
    finally:
        (
            DISPATCH.EXECUTION_REGISTRY,
            DISPATCH.EXECUTION_REGISTRY_LOCK,
            DISPATCH.EXECUTION_REGISTRY_JOURNAL,
        ) = originals


@contextlib.contextmanager
def patched_dispatch(**replacements: Any) -> Iterator[None]:
    originals = {name: getattr(DISPATCH, name) for name in replacements}
    for name, value in replacements.items():
        setattr(DISPATCH, name, value)
    try:
        yield
    finally:
        for name, value in originals.items():
            setattr(DISPATCH, name, value)


def retained_execution_payload(root: Path, output_text: str = "done") -> dict:
    """Create a complete retained run and its registry payload."""
    root.mkdir(parents=True, exist_ok=True)
    transcript = root / "app-server.jsonl"
    transcript_values = events()
    transcript_values[-1]["params"]["turn"]["durationMs"] = 25
    transcript.write_text(
        "".join(DISPATCH.canonical_json(value) + "\n" for value in transcript_values),
        encoding="utf-8",
    )
    manifest = root / "input-manifest.json"
    DISPATCH.write_json(manifest, {"schema_version": 1, "case": "receipt-test"})
    metadata = root / "execution-metadata.json"
    metadata_value = {
        "schema_version": 1,
        "source": "openai-codex-app-server-workflow",
        "status": "COMPLETED",
        "issues": [],
        "profile_sha256": "b" * 64,
        "input_manifest_sha256": DISPATCH.sha256_bytes(manifest.read_bytes()),
        "runner": {
            "path": "/Applications/Codex.app/Contents/Resources/codex-app-server",
            "sha256": "c" * 64,
            "version": "test-runner",
        },
        "server": {
            "thread_id": THREAD,
            "turn_id": TURN,
            "output_text": output_text,
            "transcript_path": str(transcript.resolve()),
            "transcript_sha256": DISPATCH.sha256_bytes(transcript.read_bytes()),
        },
    }
    DISPATCH.write_json(metadata, metadata_value)
    return {
        "metadata_path": str(metadata.resolve()),
        "metadata_sha256": DISPATCH.sha256_bytes(metadata.read_bytes()),
        "transcript_path": str(transcript.resolve()),
        "transcript_sha256": DISPATCH.sha256_bytes(transcript.read_bytes()),
        "input_manifest_path": str(manifest.resolve()),
        "input_manifest_sha256": DISPATCH.sha256_bytes(manifest.read_bytes()),
        "runner": metadata_value["runner"],
        "profile_sha256": "b" * 64,
        "dispatch_packet_sha256": None,
        "thread_id": THREAD,
        "turn_id": TURN,
        "completed_agent_message_ids": ["message-1"],
        "requested_identity": dict(REQUESTED),
        "observed_identity": dict(REQUESTED),
        "usage": {
            "input_tokens": 90,
            "output_tokens": 10,
            "total_tokens": 100,
        },
        "duration_ms": 25,
        "final_output_sha256": DISPATCH.sha256_bytes(output_text.encode("utf-8")),
        "status": "COMPLETED",
        "issues": [],
    }


def events() -> list[dict]:
    return [
        {
            "method": "thread/settings/updated",
            "params": {
                "threadId": THREAD,
                "threadSettings": {
                    "modelProvider": "openai",
                    "model": "gpt-5.6-luna",
                    "effort": "low",
                    "serviceTier": "default",
                },
            },
        },
        {
            "method": "turn/started",
            "params": {
                "threadId": THREAD,
                "turn": {"id": TURN, "status": "inProgress"},
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": THREAD,
                "turnId": TURN,
                "item": {
                    "type": "agentMessage",
                    "id": "message-1",
                    "phase": "final_answer",
                    "text": "done",
                },
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": THREAD,
                "turnId": TURN,
                "tokenUsage": {
                    "total": {
                        "inputTokens": 90,
                        "outputTokens": 10,
                        "totalTokens": 100,
                    }
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": THREAD,
                "turn": {"id": TURN, "status": "completed"},
            },
        },
    ]


def historical_no_tool_mutation_events(case: dict[str, Any]) -> list[dict]:
    thread_id = case["thread_id"]
    turn_id = case["turn_id"]
    total_tokens = case["input_tokens"] + case["output_tokens"]
    return [
        {
            "method": "thread/settings/updated",
            "params": {
                "threadId": thread_id,
                "threadSettings": {
                    "modelProvider": HISTORICAL_MUTATION_REQUESTED["provider"],
                    "model": HISTORICAL_MUTATION_REQUESTED["model"],
                    "effort": HISTORICAL_MUTATION_REQUESTED["effort"],
                    "serviceTier": HISTORICAL_MUTATION_REQUESTED["service_tier"],
                },
            },
        },
        {
            "method": "turn/started",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "inProgress"},
            },
        },
        {
            "method": "item/started",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "id": case["message_id"],
                    "phase": "final_answer",
                    "text": "",
                },
            },
        },
        {
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": {
                    "type": "agentMessage",
                    "id": case["message_id"],
                    "phase": "final_answer",
                    "text": case["output_text"],
                },
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "tokenUsage": {
                    "total": {
                        "inputTokens": case["input_tokens"],
                        "outputTokens": case["output_tokens"],
                        "totalTokens": total_tokens,
                    }
                },
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": thread_id,
                "turn": {"id": turn_id, "status": "completed", "durationMs": 1},
            },
        },
    ]


def checkpoint_fixture(root: Path) -> tuple[Path, dict, dict]:
    _, policy_raw, policy = DISPATCH.load_resumption_policy()
    transcript = root / "app-server.0001.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("{}\n", encoding="utf-8")
    os.chmod(transcript, 0o600)
    timing_path, timing = DISPATCH.write_segment_timing(
        root,
        sequence=1,
        thread_id=THREAD,
        turn_id=TURN,
        segment_elapsed_ms=2_000,
        cumulative_elapsed_ms=2_000,
        previous_timing_sha256=None,
    )
    binding = {
        "run_id": root.name,
        "run_root": str(root.resolve()),
        "plan_path": str((root / "plan.json").resolve()),
        "plan_file_sha256": "1" * 64,
        "plan_id": "plan-1",
        "phase_key": "phase-1",
        "phase_contract_sha256": "2" * 64,
        "dispatch_packet_path": str((root / "packet.json").resolve()),
        "dispatch_packet_file_sha256": "3" * 64,
        "dispatch_packet_sha256": "4" * 64,
        "input_manifest_path": str((root / "input-manifest.json").resolve()),
        "input_manifest_sha256": "5" * 64,
        "prompt_path": str((root / "prompt.txt").resolve()),
        "raw_prompt_sha256": "6" * 64,
        "resumption_policy_path": str(DISPATCH.RESUMPTION_POLICY.resolve()),
        "resumption_policy_file_sha256": DISPATCH.sha256_bytes(policy_raw),
        "resumption_policy_sha256": policy["policy_sha256"],
        "prompt_sha256": "a" * 64,
        "context_files": [
            {
                "path": str((root / "context.txt").resolve()),
                "sha256": "7" * 64,
                "bytes": 7,
            }
        ],
        "active_policy_path": str((root / "active-policy.json").resolve()),
        "active_policy_file_sha256": "8" * 64,
        "active_policy_id": "policy-1",
        "resume_contract": {
            "mode": "explicit_checkpoint_restart",
            "checkpoint_ttl_seconds": 60,
        },
        "route": {
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "effort": "low",
            "service_tier": "default",
            "tier": "T1",
            "profile_file": "workflow-t1.toml",
            "profile_sha256": "9" * 64,
        },
        "permissions": {
            "approval_policy": "never",
            "sandbox": "read-only",
            "network_access": False,
            "mutation_authorized": False,
        },
        "cwd": str(root.resolve()),
        "tool_surface": "none",
        "runner": {
            "path": "/Applications/ChatGPT.app/Contents/Resources/codex",
            "sha256": "d" * 64,
            "version": "codex-cli 0.144.2",
        },
        "active_policy_schema_version": 4,
    }
    segment = {
        "sequence": 1,
        "path": str(transcript.resolve()),
        "sha256": DISPATCH.sha256_bytes(transcript.read_bytes()),
        "thread_id": THREAD,
        "turn_id": TURN,
        "turn_status": "interrupted",
        "timing_path": str(timing_path.resolve()),
        "timing_file_sha256": DISPATCH.sha256_bytes(timing_path.read_bytes()),
        "timing_sha256": timing["timing_sha256"],
        "segment_elapsed_ms": 2_000,
        "cumulative_elapsed_ms": 2_000,
    }
    now = int(DISPATCH.time.time() * 1000)
    identity = {
        "schema_version": 1,
        "contract_name": policy["checkpoint_contract_name"],
        "contract_version": policy["checkpoint_contract_version"],
        "checkpoint_id": "checkpoint-1",
        "sequence": 1,
        "previous_checkpoint_sha256": None,
        "created_at_unix_ms": now,
        "expires_at_unix_ms": now + 60_000,
        "mode": "explicit_checkpoint_restart",
        "binding": binding,
        "thread": {
            "thread_id": THREAD,
            "source_turn_id": TURN,
            "turn_ids": [TURN],
        },
        "transcript_chain": [segment],
        "transcript_chain_sha256": DISPATCH.transcript_chain_sha256([segment]),
        "explicit_checkpoint": {
            "message_id": "message-checkpoint",
            "message_sha256": "b" * 64,
            "payload": {"remaining_work_ids": ["unit-b"]},
            "payload_sha256": "c" * 64,
        },
        "usage": {"input_tokens": 60, "output_tokens": 10, "total_tokens": 70},
        "elapsed_ms": 2_000,
        "budgets": {
            "cumulative_token_cap": 200,
            "remaining_token_cap": 130,
            "cumulative_wall_time_ms": 10_000,
            "remaining_wall_time_ms": 8_000,
            "continuation_limit": 1,
            "continuations_used": 0,
        },
        "interruption_reason": "cooperative_segment_checkpoint",
    }
    path, sealed = DISPATCH.write_resume_checkpoint(root, identity, policy)
    return path, sealed, binding


def evaluate(values: list[dict], cap: int = 200) -> tuple[dict, list[str]]:
    return DISPATCH.runtime_record(
        values,
        thread_id=THREAD,
        turn_id=TURN,
        requested=REQUESTED,
        token_cap=cap,
    )


def test_exact_metadata_is_accepted() -> None:
    record, issues = evaluate(events())
    assert issues == []
    assert record["observed_model"] == REQUESTED["model"]
    assert record["completed_agent_message_ids"] == ["message-1"]
    assert record["total_tokens"] == 100


def test_explicit_checkpoint_envelope_is_strict_and_reasoning_never_qualifies() -> None:
    _, _, policy = DISPATCH.load_resumption_policy()
    manifest = {
        "work_items": [
            {"id": "unit-a", "description": "Complete A."},
            {"id": "unit-b", "description": "Complete B."},
        ]
    }
    nonce = "nonce-1"
    payload = {
        "checkpoint_nonce": nonce,
        "checkpoint_type": "adaptive-workflow.explicit-checkpoint",
        "completed_results": {"unit-a": "A is complete."},
        "completed_work_ids": ["unit-a"],
        "remaining_work_ids": ["unit-b"],
        "schema_version": 1,
        "summary": "A completed; B remains.",
    }
    item = {
        "type": "agentMessage",
        "id": "checkpoint-message",
        "phase": "commentary",
        "text": DISPATCH.CHECKPOINT_PREFIX + DISPATCH.canonical_json(payload),
    }
    parsed = DISPATCH.parse_explicit_checkpoint(
        item, nonce=nonce, work_manifest=manifest, policy=policy
    )
    assert parsed["payload"] == payload
    nonalphabetic_manifest = {
        "work_items": [
            {"id": "unit-z", "description": "Complete Z."},
            {"id": "unit-a", "description": "Complete A."},
            {"id": "unit-b", "description": "Complete B."},
        ]
    }
    nonalphabetic_payload = {
        **payload,
        "completed_results": {"unit-z": "Z complete.", "unit-a": "A complete."},
        "completed_work_ids": ["unit-z", "unit-a"],
        "remaining_work_ids": ["unit-b"],
    }
    nonalphabetic_item = {
        **item,
        "text": DISPATCH.CHECKPOINT_PREFIX
        + DISPATCH.canonical_json(nonalphabetic_payload),
    }
    assert DISPATCH.parse_explicit_checkpoint(
        nonalphabetic_item,
        nonce=nonce,
        work_manifest=nonalphabetic_manifest,
        policy=policy,
    )["payload"] == nonalphabetic_payload
    for invalid in (
        {**item, "phase": "final_answer"},
        {**item, "type": "reasoning"},
        {**item, "text": "ordinary commentary"},
        {
            **item,
            "text": DISPATCH.CHECKPOINT_PREFIX
            + json.dumps(payload, indent=2, sort_keys=True),
        },
    ):
        try:
            DISPATCH.parse_explicit_checkpoint(
                invalid, nonce=nonce, work_manifest=manifest, policy=policy
            )
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("invalid checkpoint envelope was accepted")


def test_resume_checkpoint_is_atomic_private_self_hashed_and_single_use() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-resume-checkpoint-") as temporary:
        root = Path(temporary) / "run"
        path, sealed, binding = checkpoint_fixture(root)
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        assert oct(root.stat().st_mode & 0o777) == "0o700"
        loaded, _ = DISPATCH.load_resume_checkpoint(path)
        assert loaded == sealed
        DISPATCH.validate_resume_binding(loaded, binding)
        drifted = json.loads(json.dumps(loaded))
        drifted["binding"]["route"]["model"] = "gpt-5.6-sol"
        drifted_bare = dict(drifted)
        drifted_bare.pop("checkpoint_sha256")
        drifted["checkpoint_sha256"] = DISPATCH.content_hash(drifted_bare)
        try:
            DISPATCH.validate_resume_binding(drifted, binding)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("self-rehashed route drift was accepted")
        claim_path, claim = DISPATCH.claim_resume_checkpoint(
            path,
            sealed,
            continuation_input_sha256="d" * 64,
        )
        assert oct(claim_path.stat().st_mode & 0o777) == "0o600"
        assert claim["checkpoint_sha256"] == sealed["checkpoint_sha256"]
        for action in (
            lambda: DISPATCH.claim_resume_checkpoint(
                path, sealed, continuation_input_sha256="d" * 64
            ),
            lambda: DISPATCH.load_resume_checkpoint(path),
        ):
            try:
                action()
            except DISPATCH.DispatchError as error:
                assert "CLAIMED" in str(error)
            else:
                raise AssertionError("claimed checkpoint was replayed")


def test_resume_checkpoint_rejects_partial_transcript_and_budget_tampering() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-resume-tamper-") as temporary:
        root = Path(temporary) / "run"
        path, sealed, _ = checkpoint_fixture(root)
        transcript = Path(sealed["transcript_chain"][0]["path"])
        transcript.write_text("tampered\n", encoding="utf-8")
        try:
            DISPATCH.load_resume_checkpoint(path)
        except DISPATCH.DispatchError as error:
            assert "transcript" in str(error)
        else:
            raise AssertionError("altered transcript was accepted")
    with tempfile.TemporaryDirectory(prefix="workflow-resume-budget-") as temporary:
        root = Path(temporary) / "run"
        path, sealed, _ = checkpoint_fixture(root)
        sealed["budgets"]["remaining_token_cap"] += 1
        bare = dict(sealed)
        bare.pop("checkpoint_sha256")
        sealed["checkpoint_sha256"] = DISPATCH.content_hash(bare)
        DISPATCH._atomic_private_json(path, sealed)
        try:
            DISPATCH.load_resume_checkpoint(path)
        except DISPATCH.DispatchError as error:
            assert "budget" in str(error)
        else:
            raise AssertionError("self-rehashed cumulative budget drift was accepted")
    with tempfile.TemporaryDirectory(prefix="workflow-resume-mode-") as temporary:
        root = Path(temporary) / "run"
        path, sealed, _ = checkpoint_fixture(root)
        sealed["mode"] = "genuine_resume"
        bare = dict(sealed)
        bare.pop("checkpoint_sha256")
        sealed["checkpoint_sha256"] = DISPATCH.content_hash(bare)
        DISPATCH._atomic_private_json(path, sealed)
        try:
            DISPATCH.load_resume_checkpoint(path)
        except DISPATCH.DispatchError as error:
            assert "contract" in str(error)
        else:
            raise AssertionError("unsupported genuine-resume mode was accepted")
    with tempfile.TemporaryDirectory(prefix="workflow-resume-elapsed-") as temporary:
        root = Path(temporary) / "run"
        path, sealed, _ = checkpoint_fixture(root)
        sealed["elapsed_ms"] = 15
        sealed["budgets"]["remaining_wall_time_ms"] = 9_985
        bare = dict(sealed)
        bare.pop("checkpoint_sha256")
        sealed["checkpoint_sha256"] = DISPATCH.content_hash(bare)
        DISPATCH._atomic_private_json(path, sealed)
        try:
            DISPATCH.load_resume_checkpoint(path)
        except DISPATCH.DispatchError as error:
            assert "budget" in str(error)
        else:
            raise AssertionError("self-rehashed wall-time recovery was accepted")
    with tempfile.TemporaryDirectory(prefix="workflow-resume-ttl-") as temporary:
        root = Path(temporary) / "run"
        path, sealed, _ = checkpoint_fixture(root)
        sealed["expires_at_unix_ms"] = (
            sealed["created_at_unix_ms"] + 86_400_000
        )
        bare = dict(sealed)
        bare.pop("checkpoint_sha256")
        sealed["checkpoint_sha256"] = DISPATCH.content_hash(bare)
        DISPATCH._atomic_private_json(path, sealed)
        try:
            DISPATCH.load_resume_checkpoint(path)
        except DISPATCH.DispatchError as error:
            assert "stale" in str(error)
        else:
            raise AssertionError("packet-bound checkpoint TTL was expanded")
    with tempfile.TemporaryDirectory(prefix="workflow-resume-partial-") as temporary:
        root = Path(temporary) / "run"
        root.mkdir(parents=True, mode=0o700)
        path = root / "resume-checkpoint.0001.json"
        path.write_text("{", encoding="utf-8")
        os.chmod(path, 0o600)
        try:
            DISPATCH.load_resume_checkpoint(path)
        except DISPATCH.DispatchError as error:
            assert "partial" in str(error)
        else:
            raise AssertionError("partial checkpoint was accepted")


def test_resume_checkpoint_binding_tamper_matrix_fails_closed() -> None:
    mutations = (
        ("prompt", lambda binding: binding.__setitem__("prompt_sha256", "e" * 64)),
        (
            "context",
            lambda binding: binding["context_files"][0].__setitem__(
                "sha256", "e" * 64
            ),
        ),
        (
            "model",
            lambda binding: binding["route"].__setitem__("model", "gpt-5.6-sol"),
        ),
        (
            "effort",
            lambda binding: binding["route"].__setitem__("effort", "ultra"),
        ),
        (
            "service tier",
            lambda binding: binding["route"].__setitem__("service_tier", "priority"),
        ),
        (
            "policy",
            lambda binding: binding.__setitem__("active_policy_id", "foreign-policy"),
        ),
        (
            "permission",
            lambda binding: binding["permissions"].__setitem__(
                "network_access", True
            ),
        ),
        ("plan", lambda binding: binding.__setitem__("plan_id", "foreign-plan")),
        ("phase", lambda binding: binding.__setitem__("phase_key", "foreign-phase")),
        (
            "runner",
            lambda binding: binding["runner"].__setitem__("sha256", "e" * 64),
        ),
        ("execution", lambda binding: binding.__setitem__("run_id", "foreign-run")),
    )
    for label, mutate in mutations:
        with tempfile.TemporaryDirectory(
            prefix=f"workflow-resume-binding-{label.replace(' ', '-')}-"
        ) as temporary:
            root = Path(temporary) / "run"
            path, sealed, expected_binding = checkpoint_fixture(root)
            expected_binding = json.loads(json.dumps(expected_binding))
            mutate(sealed["binding"])
            bare = dict(sealed)
            bare.pop("checkpoint_sha256")
            sealed["checkpoint_sha256"] = DISPATCH.content_hash(bare)
            DISPATCH._atomic_private_json(path, sealed)
            try:
                loaded, _ = DISPATCH.load_resume_checkpoint(path)
                DISPATCH.validate_resume_binding(loaded, expected_binding)
            except DISPATCH.DispatchError:
                pass
            else:
                raise AssertionError(
                    f"self-rehashed {label} checkpoint tampering was accepted"
                )


def test_route_mismatch_is_rejected() -> None:
    values = events()
    values[0]["params"]["threadSettings"]["model"] = "gpt-5.6-sol"
    _, issues = evaluate(values)
    assert "observed model does not match requested model" in issues


def test_missing_service_tier_field_is_rejected() -> None:
    values = events()
    del values[0]["params"]["threadSettings"]["serviceTier"]
    _, issues = evaluate(values)
    assert any(
        "omitted explicit fields: serviceTier" in issue for issue in issues
    )


def test_malformed_service_tier_is_rejected() -> None:
    for malformed in ("", False, 0):
        values = events()
        values[0]["params"]["threadSettings"]["serviceTier"] = malformed
        try:
            evaluate(values)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError(f"malformed service tier was accepted: {malformed!r}")


def test_any_bound_thread_settings_mismatch_is_rejected() -> None:
    values = events()
    values.insert(
        0,
        {
            "method": "thread/settings/updated",
            "params": {
                "threadId": THREAD,
                "threadSettings": {
                    **values[0]["params"]["threadSettings"],
                    "model": "gpt-5.6-sol",
                },
            },
        },
    )
    _, issues = evaluate(values)
    assert "bound thread settings changed model from the requested route" in issues


def test_reroute_and_safety_buffering_are_rejected() -> None:
    values = events()
    values.extend(
        [
            {
                "method": "model/rerouted",
                "params": {"threadId": THREAD, "turnId": TURN},
            },
            {
                "method": "model/safetyBuffering/updated",
                "params": {"threadId": THREAD, "turnId": TURN},
            },
        ]
    )
    _, issues = evaluate(values)
    assert "runtime reroute observed" in issues
    assert "runtime safety buffering observed" in issues


def test_wrong_turn_cannot_supply_evidence() -> None:
    values = events()
    for event in values[1:]:
        params = event["params"]
        if "turnId" in params:
            params["turnId"] = "unrelated-turn"
        if isinstance(params.get("turn"), dict):
            params["turn"]["id"] = "unrelated-turn"
    _, issues = evaluate(values)
    assert "bound turn did not complete" in issues
    assert "completed agent-message IDs are missing or invalid" in issues
    assert "measured token usage is missing or invalid" in issues


def test_second_turn_on_ephemeral_thread_is_rejected() -> None:
    values = events()
    values.append(
        {
            "method": "turn/started",
            "params": {
                "threadId": THREAD,
                "turn": {"id": "turn-2", "status": "inProgress"},
            },
        }
    )
    _, issues = evaluate(values)
    assert "ephemeral workflow thread was not bound to exactly one turn" in issues


def test_duplicate_turn_completion_is_rejected() -> None:
    values = events()
    values.append(values[-1].copy())
    _, issues = evaluate(values)
    assert "bound workflow did not contain exactly one turn/completed event" in issues


def test_token_cap_is_enforced() -> None:
    _, issues = evaluate(events(), cap=99)
    assert "token cap exceeded: 100 > 99" in issues


def test_smoke_route_facts_resolve_to_requested_tiers() -> None:
    for tier in ("T1", "T2", "T3", "T4"):
        resolution = DISPATCH.resolve_phase(DISPATCH.smoke_request(tier))
        assert resolution["tier"] == tier
        assert resolution["service_tier"] == "default"
        assert resolution["execution_identity"] == "REQUESTED_PENDING_SERVER_METADATA"


def test_catalog_staleness_does_not_disable_healthy_routing() -> None:
    DISPATCH.validate_router_health(
        {
            "doctor": "CATALOG_STALE",
            "catalog_status": "STALE",
            "routing_status": "HEALTHY",
            "workflow_routing_ready": True,
        }
    )
    for status in (
        {
            "doctor": "DEGRADED",
            "routing_status": "DEGRADED",
            "workflow_routing_ready": False,
        },
        {
            "doctor": "HEALTHY",
            "routing_status": "HEALTHY",
            "workflow_routing_ready": False,
        },
    ):
        try:
            DISPATCH.validate_router_health(status)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("dispatcher accepted unhealthy active routing")


def test_unrestricted_worker_sandbox_is_rejected() -> None:
    try:
        DISPATCH.sandbox_policy("danger-full-access", Path("/tmp"), False)
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("unrestricted worker sandbox was accepted")


def test_token_budget_is_derived_from_phase_requirements() -> None:
    local = DISPATCH.smoke_request("T1")
    local["phase_id"] = "inspection"
    local_resolution = {"tier": "T1"}
    local_budget = DISPATCH.derived_token_budget(
        local, local_resolution, "Inspect the supplied files.", exact_output=None
    )
    complex_request = DISPATCH.smoke_request("T4")
    complex_budget = DISPATCH.derived_token_budget(
        complex_request,
        {"tier": "T4"},
        "Design the cross-system architecture from the supplied evidence.",
        exact_output=None,
    )
    smoke_budget = DISPATCH.derived_token_budget(
        local, local_resolution, "Return ROUTE_OK.", exact_output="ROUTE_OK"
    )
    assert local_budget["mode"] == "derived"
    assert complex_budget["token_cap"] > local_budget["token_cap"]
    assert local_budget["token_cap"] > smoke_budget["token_cap"]
    assert complex_budget["factors"]["scope"] > local_budget["factors"]["scope"]


def test_bound_context_no_tools_budget_is_single_inference() -> None:
    request = DISPATCH.smoke_request("T3")
    budget = DISPATCH.derived_token_budget(
        request,
        {"tier": "T3"},
        "Review the supplied bound context.",
        exact_output=None,
        tool_mode="none",
    )
    assert budget["estimated_inferences"] == 1
    assert budget["estimated_context_growth_tokens"] == 0


def test_no_tools_contract_rejects_tool_items() -> None:
    values = events()
    values.insert(
        1,
        {
            "method": "item/started",
            "params": {
                "threadId": THREAD,
                "turnId": TURN,
                "item": {"type": "commandExecution", "id": "tool-1"},
            },
        },
    )
    _, issues = DISPATCH.runtime_record(
        values,
        thread_id=THREAD,
        turn_id=TURN,
        requested=REQUESTED,
        token_cap=200,
        tool_mode="none",
    )
    assert "tool use observed under the no-tools contract" in issues


def test_historical_mutating_transcripts_require_observed_tool_use() -> None:
    assert {
        case["transcript_sha256"] for case in HISTORICAL_NO_TOOL_MUTATION_CASES
    } == {
        "d31ce1de854fb55a11e0093d1da4ccc5261e2df198cd815301224d87928d4c8c",
        "ef093db4488b0fc6105a7cdf449addc5802cc63e51f568709577bc2f84de60df",
    }
    for case in HISTORICAL_NO_TOOL_MUTATION_CASES:
        values = historical_no_tool_mutation_events(case)
        record, issues = DISPATCH.runtime_record(
            values,
            thread_id=case["thread_id"],
            turn_id=case["turn_id"],
            requested=HISTORICAL_MUTATION_REQUESTED,
            token_cap=case["input_tokens"] + case["output_tokens"],
            require_tool_use=True,
        )
        assert record["tool_item_types"] == []
        assert record["output_text"] == case["output_text"]
        assert "mutating workspace route completed without observed tool use" in issues
        assert DISPATCH.execution_status(issues) == "BLOCKED_MODEL_ENFORCEMENT"

        values.insert(
            3,
            {
                "method": "item/completed",
                "params": {
                    "threadId": case["thread_id"],
                    "turnId": case["turn_id"],
                    "item": {"type": "mcpToolCall", "id": "local-tool-1"},
                },
            },
        )
        record, issues = DISPATCH.runtime_record(
            values,
            thread_id=case["thread_id"],
            turn_id=case["turn_id"],
            requested=HISTORICAL_MUTATION_REQUESTED,
            token_cap=case["input_tokens"] + case["output_tokens"],
            require_tool_use=True,
        )
        assert record["tool_item_types"] == ["mcpToolCall"]
        assert "mutating workspace route completed without observed tool use" not in issues
        assert DISPATCH.execution_status(issues) == "COMPLETED"


def test_workspace_write_instructions_identify_local_execution_bridge() -> None:
    instructions = DISPATCH.local_mutation_tool_instructions(
        "workspace-write", "default"
    )
    assert "node_repl" in instructions
    assert "MCP `js` tool" in instructions
    assert "local execution and file-edit bridge" in instructions
    assert "apply_patch" in instructions
    assert DISPATCH.local_mutation_tool_instructions("read-only", "default") == ""
    assert DISPATCH.local_mutation_tool_instructions("workspace-write", "none") == ""


def test_no_tools_contract_rejects_unknown_and_extended_item_types() -> None:
    for item_type in (
        "imageGeneration",
        "sleep",
        "subAgentActivity",
        "futureUnknownAction",
    ):
        values = events()
        values.insert(
            1,
            {
                "method": "item/started",
                "params": {
                    "threadId": THREAD,
                    "turnId": TURN,
                    "item": {"type": item_type, "id": "action-1"},
                },
            },
        )
        record, issues = DISPATCH.runtime_record(
            values,
            thread_id=THREAD,
            turn_id=TURN,
            requested=REQUESTED,
            token_cap=200,
            tool_mode="none",
        )
        assert item_type in record["tool_item_types"]
        assert "tool use observed under the no-tools contract" in issues


def test_no_tools_live_interrupt_ignores_foreign_thread_events() -> None:
    foreign = {
        "method": "item/started",
        "params": {
            "threadId": "foreign-thread",
            "turnId": "foreign-turn",
            "item": {"type": "commandExecution", "id": "foreign-tool"},
        },
    }
    bound = {
        "method": "item/started",
        "params": {
            "threadId": THREAD,
            "turnId": TURN,
            "item": {"type": "commandExecution", "id": "bound-tool"},
        },
    }
    assert not DISPATCH.no_tools_item_violation(foreign, THREAD, TURN)
    assert DISPATCH.no_tools_item_violation(bound, THREAD, TURN)


def test_malformed_event_shapes_become_dispatch_errors() -> None:
    values = events()
    values[0]["params"] = "not-an-object"
    try:
        evaluate(values)
    except DISPATCH.DispatchError as error:
        assert "event params must be an object" in str(error)
    else:
        raise AssertionError("malformed event shape escaped dispatch validation")
    values = events()
    values.append({"method": "turn/completed"})
    try:
        evaluate(values)
    except DISPATCH.DispatchError as error:
        assert "event params must be an object" in str(error)
    else:
        raise AssertionError("known event without params escaped dispatch validation")


def test_no_tools_contract_requires_read_only_offline_execution() -> None:
    args = argparse.Namespace(
        tool_mode="none",
        sandbox="workspace-write",
        network_access=False,
        mutation_authorized=True,
    )
    request = {"mutation": "reversible", "external_action": False}
    resolution = {"parent_gate_required": False, "web_required": False}
    profile = {"sandbox_mode": "workspace-write"}
    try:
        DISPATCH.validate_authority(args, request, resolution, profile)
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("no-tools execution received workspace-write access")


def test_planner_dispatch_packet_binds_exact_inputs() -> None:
    phase = {
        "phase_key": "coding:inspect",
        "workflow_id": "coding",
        "workflow_version": 1,
        "phase_id": "inspect",
        "pattern": "chain",
        "produces": ["evidence"],
        "exit_gate": ["evidence retained"],
        "route_request": {"activity": "inspect"},
        "route_resolution": {"tier": "T1"},
    }
    binding = {
        "planner_contract_version": 1,
        "plan_id": "a" * 64,
        "plan_policy_id": "b" * 64,
        "phase_key": phase["phase_key"],
        "phase": phase,
    }
    args = argparse.Namespace(
        sandbox="read-only",
        network_access=False,
        tool_mode="none",
        mutation_authorized=False,
        wall_time_seconds=60,
    )
    packet = DISPATCH.expected_dispatch_packet(
        binding,
        prompt_sha256="c" * 64,
        context_records=[],
        cwd=Path("/tmp"),
        args=args,
    )
    DISPATCH.validate_dispatch_packet(packet, packet)
    changed = DISPATCH.expected_dispatch_packet(
        binding,
        prompt_sha256="d" * 64,
        context_records=[],
        cwd=Path("/tmp"),
        args=args,
    )
    try:
        DISPATCH.validate_dispatch_packet(packet, changed)
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("dispatch packet accepted a changed prompt")


def workflow_plan(activation: str = "active") -> dict:
    request = DISPATCH.smoke_request("T1")
    request.update(
        {
            "workflow_id": "coding",
            "workflow_version": 1,
            "phase_id": "inspect",
        }
    )
    resolution = DISPATCH.resolve_phase(request)
    phase = {
        "phase_key": "coding:inspect",
        "workflow_id": "coding",
        "workflow_version": 1,
        "phase_id": "inspect",
        "execution": "route",
        "activation": activation,
        "pattern": "chain",
        "produces": ["evidence"],
        "exit_gate": ["evidence retained"],
        "route_request": request,
        "route_resolution": resolution,
    }
    identity = {
        "objective": "test exact plan identity",
        "catalog_sha256": "a" * 64,
        "route_contract_sha256": "b" * 64,
        "selected_workflows": ["coding"],
        "composition_mode": "single",
        "workflow_hashes": {"coding": "c" * 64},
        "router_policy_id": resolution["policy_id"],
        "overrides": [],
        "phases": [phase],
    }
    return {
        "schema_version": 1,
        "planner_contract_version": 1,
        "execution_identity": "REQUESTED_ROUTES_PENDING_RUNTIME_EVIDENCE",
        **identity,
        "plan_id": DISPATCH.content_hash(identity),
    }


def test_workflow_plan_identity_and_active_phase_are_enforced() -> None:
    plan = workflow_plan()
    DISPATCH.validate_workflow_plan(plan)
    DISPATCH.validate_planned_phase(plan, "coding:inspect")
    changed = {**plan, "objective": "tampered objective"}
    try:
        DISPATCH.validate_workflow_plan(changed)
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("workflow dispatcher accepted a tampered plan")
    with tempfile.TemporaryDirectory(prefix="workflow-plan-binding-") as temporary:
        plan_path = Path(temporary) / "plan.json"
        DISPATCH.write_json(plan_path, workflow_plan("runtime_condition"))
        args = argparse.Namespace(
            plan=plan_path,
            phase_key="coding:inspect",
            request=None,
            dispatch_packet=None,
        )
        try:
            DISPATCH.load_route(args, require_dispatch_packet=False)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("dispatcher accepted a non-active planned phase")


def rehash_plan(plan: dict) -> None:
    identity = {field: plan[field] for field in DISPATCH.PLAN_IDENTITY_FIELDS}
    plan["plan_id"] = DISPATCH.content_hash(identity)


def test_self_rehashed_semantically_invalid_plans_are_rejected() -> None:
    phase_mismatch = json.loads(json.dumps(workflow_plan()))
    phase_mismatch["phases"][0]["phase_id"] = "changed"
    rehash_plan(phase_mismatch)
    try:
        DISPATCH.validate_planned_phase(phase_mismatch, "coding:inspect")
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("self-rehashed phase/request mismatch was accepted")

    evidence_mismatch = json.loads(json.dumps(workflow_plan()))
    evidence_mismatch["phases"][0]["route_resolution"][
        "runtime_evidence_required"
    ] = False
    rehash_plan(evidence_mismatch)
    try:
        DISPATCH.validate_planned_phase(evidence_mismatch, "coding:inspect")
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("self-rehashed runtime-evidence mismatch was accepted")


def test_explicit_token_cap_requires_a_fixed_task_contract() -> None:
    task_binding = {"schema_version": 1, "prompt_sha256": "a" * 64}
    try:
        DISPATCH.validate_explicit_token_cap(100, None, task_binding)
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("an unbound explicit cap was accepted")
    with tempfile.TemporaryDirectory(prefix="workflow-cap-contract-") as temporary:
        path = Path(temporary) / "contract.json"
        evidence_path = Path(temporary) / "measured-safe-evidence.json"
        evidence_path.write_text(
            '{"status":"measured-safe","total_tokens":88}\n', encoding="utf-8"
        )
        identity = {
            "schema_version": 1,
            "contract_name": DISPATCH.TOKEN_CAP_CONTRACT_NAME,
            "contract_version": 1,
            "task_binding_sha256": DISPATCH.content_hash(task_binding),
            "token_cap": 100,
            "measured_safe_evidence_path": str(evidence_path),
            "measured_safe_evidence_sha256": DISPATCH.sha256_bytes(
                evidence_path.read_bytes()
            ),
            "rationale": "measured safe on the exact fixed task",
        }
        DISPATCH.write_json(
            path,
            {**identity, "contract_sha256": DISPATCH.content_hash(identity)},
        )
        record = DISPATCH.validate_explicit_token_cap(100, path, task_binding)
        assert record and record["task_binding_sha256"] == DISPATCH.content_hash(
            task_binding
        )
        changed_binding = {**task_binding, "prompt_sha256": "b" * 64}
        try:
            DISPATCH.validate_explicit_token_cap(100, path, changed_binding)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("fixed cap accepted a different task binding")
        for field, malformed in (
            ("schema_version", True),
            ("contract_version", True),
            ("token_cap", 100.0),
        ):
            malformed_identity = {**identity, field: malformed}
            DISPATCH.write_json(
                path,
                {
                    **malformed_identity,
                    "contract_sha256": DISPATCH.content_hash(malformed_identity),
                },
            )
            try:
                DISPATCH.validate_explicit_token_cap(100, path, task_binding)
            except DISPATCH.DispatchError:
                pass
            else:
                raise AssertionError(f"fixed cap accepted malformed {field}")
        DISPATCH.write_json(
            path,
            {**identity, "contract_sha256": DISPATCH.content_hash(identity)},
        )
        symlink_path = Path(temporary) / "contract-link.json"
        symlink_path.symlink_to(path)
        try:
            DISPATCH.validate_explicit_token_cap(100, symlink_path, task_binding)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("fixed cap accepted a symlinked contract")
        try:
            DISPATCH.validate_explicit_token_cap(True, path, task_binding)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("fixed cap accepted a boolean token cap")
        evidence_path.write_text('{"status":"changed"}\n', encoding="utf-8")
        try:
            DISPATCH.validate_explicit_token_cap(100, path, task_binding)
        except DISPATCH.DispatchError:
            pass
        else:
            raise AssertionError("fixed cap accepted changed measured-safe evidence")
    try:
        DISPATCH.validate_explicit_token_cap(None, Path("contract.json"), task_binding)
    except DISPATCH.DispatchError:
        pass
    else:
        raise AssertionError("a cap contract without a cap was accepted")


def test_usage_errors_always_retain_an_abort_artifact() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-usage-artifact-") as temporary:
        root = Path(temporary)
        requested_output = root / "already-exists"
        requested_output.mkdir()
        fallback_runs = root / "runs"
        original_runs = DISPATCH.RUNS
        original_argv = sys.argv
        DISPATCH.RUNS = fallback_runs
        sys.argv = [
            str(SCRIPT),
            "run",
            "--request",
            str(root / "request.json"),
            "--cwd",
            str(root),
            "--output-dir",
            str(requested_output),
        ]
        try:
            assert DISPATCH.main() == 2
        finally:
            DISPATCH.RUNS = original_runs
            sys.argv = original_argv
        artifacts = list(fallback_runs.glob("*/aborted.json"))
        assert len(artifacts) == 1
        artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
        assert artifact["status"] == "ABORTED"
        assert artifact["category"] == "USAGE_ERROR"
        assert artifact["requested_output_dir"] == str(requested_output.resolve())


def test_execution_registry_append_is_hash_chained_and_tamper_evident() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-receipt-chain-") as temporary:
        root = Path(temporary)
        with isolated_registry(root) as registry:
            first = DISPATCH.append_execution_receipt(
                retained_execution_payload(root / "first")
            )
            second = DISPATCH.append_execution_receipt(
                retained_execution_payload(root / "second")
            )
            decoded = DISPATCH._decode_receipt_registry(registry.read_bytes())
            assert [event["sequence"] for event in decoded] == [1, 2]
            assert decoded[0]["receipt_sha256"] == first["receipt_sha256"]
            assert decoded[1]["receipt_sha256"] == second["receipt_sha256"]
            assert decoded[1]["previous_receipt_sha256"] == first["receipt_sha256"]
            assert registry.stat().st_mode & 0o777 == 0o600

            tampered = [dict(event) for event in decoded]
            tampered[0]["status"] = "TAMPERED"
            registry.write_text(
                "".join(
                    DISPATCH.canonical_json(event) + "\n" for event in tampered
                ),
                encoding="utf-8",
            )
            try:
                DISPATCH._decode_receipt_registry(registry.read_bytes())
            except DISPATCH.DispatchError as error:
                assert "hash chain" in str(error)
            else:
                raise AssertionError("tampered execution receipt registry was accepted")


def test_execution_registry_recovers_a_complete_interrupted_append() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-receipt-recovery-") as temporary:
        root = Path(temporary)
        with isolated_registry(root) as registry:
            first = DISPATCH.append_execution_receipt(
                retained_execution_payload(root / "first")
            )
            before = registry.read_bytes()
            recovered = {
                "schema_version": 1,
                "receipt_type": "execution",
                "receipt_id": "recovered-receipt",
                "issued_at_epoch": 1234.0,
                **retained_execution_payload(root / "recovered"),
                "sequence": 2,
                "previous_receipt_sha256": first["receipt_sha256"],
            }
            recovered["receipt_sha256"] = DISPATCH.content_hash(recovered)
            after = (
                before
                + DISPATCH.canonical_json(recovered).encode("utf-8")
                + b"\n"
            )
            journal = {
                "schema_version": 1,
                "before_sha256": DISPATCH.sha256_bytes(before),
                "after_sha256": DISPATCH.sha256_bytes(after),
                "target_base64": base64.b64encode(after).decode("ascii"),
                "receipt_id": recovered["receipt_id"],
            }
            journal["journal_sha256"] = DISPATCH.content_hash(journal)
            DISPATCH.EXECUTION_REGISTRY_JOURNAL.write_text(
                DISPATCH.canonical_json(journal) + "\n", encoding="utf-8"
            )

            selected = DISPATCH.registry_receipt("recovered-receipt")
            assert selected["receipt_sha256"] == recovered["receipt_sha256"]
            assert registry.read_bytes() == after
            assert not DISPATCH.EXECUTION_REGISTRY_JOURNAL.exists()


def test_run_phase_registers_only_clean_completed_provenance() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-clean-receipt-") as temporary:
        root = Path(temporary)
        prompt = root / "prompt.txt"
        prompt.write_text("Return done.\n", encoding="utf-8")
        runner = root / "app-server"
        runner.write_text("test runner\n", encoding="utf-8")
        policy = root / "active-policy.json"
        DISPATCH.write_json(policy, {"schema_version": 1})
        request = {
            "workflow_id": "coding",
            "workflow_version": 1,
            "phase_id": "inspect",
            "activity": "inspect",
            "mutation": "none",
            "scope": "local",
            "ambiguity": "none",
            "risk_level": "low",
        }
        resolution = {
            "policy_id": "a" * 64,
            "tier": "T1",
            "provider": "openai",
            "model": REQUESTED["model"],
            "effort": REQUESTED["effort"],
            "service_tier": REQUESTED["service_tier"],
            "profile_sha256": "b" * 64,
        }

        class FakeAppServer:
            def __init__(
                self, unused_runner: Path, transcript: Path, stderr_path: Path
            ) -> None:
                del unused_runner, stderr_path
                self.last_id = 0
                self.values = events()
                self.values[-1]["params"]["turn"]["durationMs"] = 25
                transcript.write_text(
                    "".join(
                        DISPATCH.canonical_json(value) + "\n"
                        for value in self.values
                    ),
                    encoding="utf-8",
                )
                self.pending = iter(self.values)

            def send(self, value: dict) -> None:
                self.last_id = value["id"]

            def wait_for(self, predicate: Any, deadline: float) -> dict:
                del predicate, deadline
                if self.last_id == 1:
                    return {"id": 1, "result": {}}
                if self.last_id == 2:
                    return {"id": 2, "result": {"thread": {"id": THREAD}}}
                if self.last_id == 3:
                    return {"id": 3, "result": {"turn": {"id": TURN}}}
                raise AssertionError(f"unexpected App Server request: {self.last_id}")

            def next_event(self, deadline: float) -> dict:
                del deadline
                return next(self.pending)

            def close(self) -> None:
                return None

        def arguments(output: Path, expected: str) -> argparse.Namespace:
            return argparse.Namespace(
                plan=None,
                phase_key=None,
                request=None,
                prompt_file=prompt,
                cwd=root,
                sandbox="read-only",
                network_access=False,
                mutation_authorized=False,
                token_cap=None,
                token_cap_contract=None,
                context_file=[],
                tool_mode="none",
                wall_time_seconds=60,
                output_dir=output,
                expect_exact_output=expected,
            )

        budget = {
            "mode": "derived",
            "token_cap": 200,
            "estimated_inferences": 1,
            "estimated_context_growth_tokens": 0,
        }
        with isolated_registry(root / "registry") as registry, patched_dispatch(
            POLICY=policy,
            AppServer=FakeAppServer,
            load_route=lambda unused_args: (request, None),
            resolve_phase=lambda unused_request: resolution,
            validate_resolution=lambda unused_resolution: None,
            router_status=lambda: {},
            validate_policy=lambda unused_policy, unused_status, unused_resolution: (
                runner,
                "c" * 64,
                {"developer_instructions": "bounded test worker"},
            ),
            validate_authority=lambda *unused: None,
            derived_token_budget=lambda *unused, **unused_keywords: budget,
            subprocess=SimpleNamespace(
                run=lambda *unused, **unused_keywords: SimpleNamespace(
                    stdout="test-runner\n"
                )
            ),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                assert DISPATCH.run_phase(arguments(root / "clean", "done")) == 0
                assert (
                    DISPATCH.run_phase(arguments(root / "blocked", "different"))
                    == 2
                )
            receipts = DISPATCH._decode_receipt_registry(registry.read_bytes())
            assert len(receipts) == 1
            assert receipts[0]["status"] == "COMPLETED"
            assert receipts[0]["issues"] == []
            assert (root / "clean" / "execution-receipt.json").is_file()
            assert not (root / "blocked" / "execution-receipt.json").exists()
            blocked = json.loads(
                (root / "blocked" / "aborted.json").read_text(encoding="utf-8")
            )
            assert blocked["category"] == "BLOCKED_MODEL_ENFORCEMENT"


def test_checkpoint_restart_uses_fresh_turn_and_only_unfinished_work() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-resume-e2e-") as temporary:
        base = Path(temporary)
        output = base / "run"
        prompt = base / "prompt.txt"
        prompt.write_text("Complete the two bound work units.\n", encoding="utf-8")
        work_identity = {
            "schema_version": 1,
            "contract_name": "adaptive-workflow.resume-work-manifest",
            "contract_version": 1,
            "work_items": [
                {"id": "unit-a", "description": "Complete unit A."},
                {"id": "unit-b", "description": "Complete unit B."},
            ],
        }
        work_manifest = base / "work-manifest.json"
        work_manifest.write_text(
            json.dumps(
                {
                    **work_identity,
                    "manifest_sha256": DISPATCH.content_hash(work_identity),
                }
            ),
            encoding="utf-8",
        )
        plan_path = base / "plan.json"
        plan_path.write_text("{}\n", encoding="utf-8")
        packet_path = base / "packet.json"
        active_policy = base / "active-policy.json"
        DISPATCH.write_json(active_policy, {"schema_version": 1})
        runner = base / "codex"
        runner.write_text("fake runner\n", encoding="utf-8")
        request = {
            "workflow_id": "coding.change",
            "workflow_version": 1,
            "phase_id": "design",
            "activity": "design",
            "mutation": "none",
            "scope": "local",
            "ambiguity": "bounded",
            "risk_level": "moderate",
            "risk_scope": "judgment",
            "risk_tags": [],
            "current_info_required": False,
            "visual_required": False,
            "external_action": False,
        }
        resolution = {
            "policy_id": "a" * 64,
            "mode": "model",
            "tier": "T1",
            "agent": "fast_operator",
            "provider": "openai",
            "service_tier": "default",
            "model": REQUESTED["model"],
            "effort": REQUESTED["effort"],
            "profile_file": "fast-operator.toml",
            "profile_sha256": "b" * 64,
            "execution_identity": "REQUESTED_PENDING_SERVER_METADATA",
            "runtime_evidence_required": True,
            "runtime_attestation_required": False,
            "workflow_id": "coding.change",
            "workflow_version": 1,
            "phase_id": "design",
            "route_facts": {
                key: request[key]
                for key in (
                    "activity",
                    "mutation",
                    "scope",
                    "ambiguity",
                    "risk_level",
                    "risk_scope",
                    "risk_tags",
                    "external_action",
                )
            },
            "web_required": False,
            "visual_required": False,
            "parent_gate_required": False,
            "external_mutation_authorized": False,
        }
        phase = {
            "phase_key": "coding.change:design",
            "workflow_id": "coding.change",
            "workflow_version": 1,
            "phase_id": "design",
            "pattern": "chain",
            "produces": ["implementation_plan"],
            "exit_gate": ["design is explicit"],
            "route_request": request,
            "route_resolution": resolution,
        }
        arguments = argparse.Namespace(
            plan=plan_path,
            phase_key=phase["phase_key"],
            dispatch_packet=packet_path,
            request=None,
            prompt_file=prompt,
            context_file=[],
            cwd=base,
            sandbox="read-only",
            network_access=False,
            tool_mode="none",
            mutation_authorized=False,
            token_cap=None,
            token_cap_contract=None,
            wall_time_seconds=5,
            output_dir=output,
            expect_exact_output=None,
            resumable=True,
            work_manifest=work_manifest,
            max_continuations=1,
            cumulative_wall_time_seconds=10,
            checkpoint_window_seconds=2,
            shutdown_window_seconds=1,
            checkpoint_ttl_seconds=3600,
            resume_checkpoint=None,
        )
        plan_binding = {
            "plan_path": str(plan_path.resolve()),
            "plan_file_sha256": DISPATCH.sha256_bytes(plan_path.read_bytes()),
            "plan_id": "plan-1",
            "planner_contract_version": 1,
            "plan_policy_id": resolution["policy_id"],
            "phase_key": phase["phase_key"],
            "phase": phase,
            "planned_resolution": resolution,
            "dispatch_packet_path": str(packet_path.resolve()),
            "dispatch_packet_file_sha256": None,
            "dispatch_packet": {},
        }
        packet = DISPATCH.expected_dispatch_packet(
            plan_binding,
            prompt_sha256=DISPATCH.sha256_bytes(prompt.read_bytes()),
            context_records=[],
            cwd=base.resolve(),
            args=arguments,
        )
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        plan_binding["dispatch_packet"] = packet
        plan_binding["dispatch_packet_file_sha256"] = DISPATCH.sha256_bytes(
            packet_path.read_bytes()
        )

        class FakeClock:
            def __init__(self) -> None:
                self.value = 100.0

            def monotonic(self) -> float:
                return self.value

            def time(self) -> float:
                return 1_800_000_000.0 + self.value

        clock = FakeClock()
        work_counts = {"unit-a": 0, "unit-b": 0}
        methods: list[str] = []

        class FakeResumeAppServer:
            instances = 0
            reasoning_only = False
            continuation_fault: str | None = None
            crash_after_claim = False

            def __init__(
                self, unused_runner: Path, transcript: Path, stderr_path: Path
            ) -> None:
                del unused_runner
                type(self).instances += 1
                self.instance = type(self).instances
                if self.instance == 2 and type(self).crash_after_claim:
                    raise DISPATCH.DispatchError("synthetic crash after claim")
                self.transcript = transcript
                self.stderr_path = stderr_path
                transcript.write_text("", encoding="utf-8")
                os.chmod(transcript, 0o600)
                self.pending: list[dict] = []

            def emit(self, event: dict) -> None:
                with self.transcript.open("a", encoding="utf-8") as handle:
                    handle.write(DISPATCH.canonical_json(event) + "\n")

            def send(self, value: dict) -> None:
                methods.append(value["method"])
                identifier = value["id"]
                method = value["method"]
                if method == "initialize":
                    self.pending.append({"id": identifier, "result": {}})
                elif method == "thread/start":
                    self.pending.append(
                        {"id": identifier, "result": {"thread": {"id": THREAD}}}
                    )
                elif method == "thread/resume":
                    self.pending.append(
                        {
                            "id": identifier,
                            "result": {
                                "thread": {
                                    "id": THREAD,
                                    "turns": [
                                        {"id": "turn-1", "status": "interrupted"}
                                    ],
                                }
                            },
                        }
                    )
                elif method == "turn/start":
                    turn = "turn-1" if self.instance == 1 else "turn-2"
                    turn_input = value["params"]["input"][0]["text"]
                    if self.instance == 2:
                        assert 'Remaining work IDs: ["unit-b"]' in turn_input
                        assert "Do not repeat any completed work ID" in turn_input
                    self.pending.extend(
                        [
                            {"id": identifier, "result": {"turn": {"id": turn}}},
                            {
                                "method": "thread/settings/updated",
                                "params": {
                                    "threadId": THREAD,
                                    "threadSettings": {
                                        "modelProvider": "openai",
                                        "model": REQUESTED["model"],
                                        "effort": REQUESTED["effort"],
                                        "serviceTier": "default",
                                    },
                                },
                            },
                            {
                                "method": "turn/started",
                                "params": {
                                    "threadId": THREAD,
                                    "turn": {"id": turn, "status": "inProgress"},
                                },
                            },
                        ]
                    )
                    if self.instance == 1:
                        work_counts["unit-a"] += 1
                    else:
                        work_counts["unit-b"] += 1
                        fault = type(self).continuation_fault
                        if fault == "tool":
                            self.pending.append(
                                {
                                    "method": "item/started",
                                    "params": {
                                        "threadId": THREAD,
                                        "turnId": turn,
                                        "item": {
                                            "type": "commandExecution",
                                            "id": "forbidden-tool",
                                        },
                                    },
                                }
                            )
                        elif fault == "reroute":
                            self.pending.append(
                                {
                                    "method": "model/rerouted",
                                    "params": {
                                        "threadId": THREAD,
                                        "turnId": turn,
                                        "toModel": "foreign-model",
                                    },
                                }
                            )
                        elif fault == "safety":
                            self.pending.append(
                                {
                                    "method": "model/safetyBuffering/updated",
                                    "params": {
                                        "threadId": THREAD,
                                        "turnId": turn,
                                        "buffered": True,
                                    },
                                }
                            )
                        self.pending.extend(
                            [
                                {
                                    "method": "item/completed",
                                    "params": {
                                        "threadId": THREAD,
                                        "turnId": turn,
                                        "item": {
                                            "type": "agentMessage",
                                            "id": "message-final",
                                            "phase": "final_answer",
                                            "text": "all work complete",
                                        },
                                    },
                                },
                                {
                                    "method": "thread/tokenUsage/updated",
                                    "params": {
                                        "threadId": THREAD,
                                        "turnId": turn,
                                        "tokenUsage": {
                                            "total": {
                                                "inputTokens": 90,
                                                "outputTokens": 20,
                                                "totalTokens": 110,
                                            }
                                        },
                                    },
                                },
                                {
                                    "method": "turn/completed",
                                    "params": {
                                        "threadId": THREAD,
                                        "turn": {
                                            "id": turn,
                                            "status": "completed",
                                            "durationMs": 20,
                                        },
                                    },
                                },
                            ]
                        )
                elif method == "turn/steer":
                    if type(self).reasoning_only:
                        self.pending.extend(
                            [
                                {"id": identifier, "result": {"turnId": "turn-1"}},
                                {
                                    "method": "item/completed",
                                    "params": {
                                        "threadId": THREAD,
                                        "turnId": "turn-1",
                                        "item": {
                                            "type": "reasoning",
                                            "id": "reasoning-only",
                                            "summary": [],
                                            "content": [],
                                        },
                                    },
                                },
                            ]
                        )
                        return
                    text = value["params"]["input"][0]["text"]
                    match = DISPATCH.re.search(
                        r"checkpoint_nonce is '([0-9a-f]+)'", text
                    )
                    assert match
                    payload = {
                        "checkpoint_nonce": match.group(1),
                        "checkpoint_type": "adaptive-workflow.explicit-checkpoint",
                        "completed_results": {"unit-a": "unit A complete"},
                        "completed_work_ids": ["unit-a"],
                        "remaining_work_ids": ["unit-b"],
                        "schema_version": 1,
                        "summary": "unit A complete; unit B remains",
                    }
                    self.pending.extend(
                        [
                            {"id": identifier, "result": {"turnId": "turn-1"}},
                            {
                                "method": "item/completed",
                                "params": {
                                    "threadId": THREAD,
                                    "turnId": "turn-1",
                                    "item": {
                                        "type": "agentMessage",
                                        "id": "message-checkpoint",
                                        "phase": "commentary",
                                        "text": DISPATCH.CHECKPOINT_PREFIX
                                        + DISPATCH.canonical_json(payload),
                                    },
                                },
                            },
                        ]
                    )
                elif method == "turn/interrupt":
                    self.pending.extend(
                        [
                            {"id": identifier, "result": {}},
                            {
                                "method": "thread/tokenUsage/updated",
                                "params": {
                                    "threadId": THREAD,
                                    "turnId": "turn-1",
                                    "tokenUsage": {
                                        "total": {
                                            "inputTokens": 60,
                                            "outputTokens": 10,
                                            "totalTokens": 70,
                                        }
                                    },
                                },
                            },
                            {
                                "method": "turn/completed",
                                "params": {
                                    "threadId": THREAD,
                                    "turn": {
                                        "id": "turn-1",
                                        "status": "interrupted",
                                        "durationMs": 15,
                                    },
                                },
                            },
                        ]
                    )
                else:
                    raise AssertionError(f"unexpected method {method}")

            def next_event(self, deadline: float) -> dict:
                if not self.pending:
                    clock.value = deadline
                    raise DISPATCH.AppServerDeadline("synthetic deadline")
                event = self.pending.pop(0)
                self.emit(event)
                return event

            def wait_for(self, predicate: Any, deadline: float) -> dict:
                while True:
                    event = self.next_event(deadline)
                    if predicate(event):
                        if "error" in event:
                            raise DISPATCH.DispatchError(str(event["error"]))
                        return event

            def close(self) -> None:
                self.stderr_path.write_text("", encoding="utf-8")

        budget = {
            "mode": "derived",
            "token_cap": 200,
            "estimated_inferences": 1,
            "estimated_context_growth_tokens": 0,
        }
        with isolated_registry(base / "registry"), patched_dispatch(
            POLICY=active_policy,
            AppServer=FakeResumeAppServer,
            time=clock,
            load_route=lambda unused_args: (request, plan_binding),
            resolve_phase=lambda unused_request: resolution,
            validate_resolution=lambda unused_resolution: None,
            router_status=lambda: {},
            validate_policy=lambda unused_policy, unused_status, unused_resolution: (
                runner,
                "c" * 64,
                {"developer_instructions": "bounded synthetic worker"},
            ),
            validate_authority=lambda *unused: None,
            derived_token_budget=lambda *unused, **unused_keywords: budget,
            subprocess=SimpleNamespace(
                run=lambda *unused, **unused_keywords: SimpleNamespace(
                    stdout="test-runner\n"
                )
            ),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                assert DISPATCH.run_phase(arguments) == 0
            assert work_counts == {"unit-a": 1, "unit-b": 1}
            successful_methods = list(methods)
            if os.environ.get("ADAPTIVE_RESUME_CANARY_ONLY") == "1":
                assert "thread/resume" in successful_methods
                assert "turn/resume" not in successful_methods
                assert successful_methods.count("turn/start") == 2
                metadata = json.loads(
                    (output / "execution-metadata.json").read_text(
                        encoding="utf-8"
                    )
                )
                assert (
                    metadata["completion_mode"]
                    == "explicit_checkpoint_restart"
                )
                assert metadata["server"]["total_tokens"] == 110
                assert len(metadata["server"]["transcript_chain"]) == 2
                assert (output / "resume-checkpoint.0001.json").is_file()
                assert (output / "resume-claim.0001.json").is_file()
                checkpoint_value = json.loads(
                    (output / "resume-checkpoint.0001.json").read_text(
                        encoding="utf-8"
                    )
                )
                DISPATCH.validate_checkpoint_runtime_state(
                    checkpoint_value,
                    resume_contract=checkpoint_value["binding"][
                        "resume_contract"
                    ],
                    requested=REQUESTED,
                    token_cap=200,
                )
                return
            FakeResumeAppServer.instances = 0
            FakeResumeAppServer.reasoning_only = True
            clock.value = 200.0
            reasoning_output = base / "reasoning-only-run"
            reasoning_arguments = argparse.Namespace(
                **{
                    **vars(arguments),
                    "output_dir": reasoning_output,
                }
            )
            with contextlib.redirect_stdout(io.StringIO()):
                assert DISPATCH.run_phase(reasoning_arguments) == 2
            FakeResumeAppServer.instances = 0
            FakeResumeAppServer.reasoning_only = False
            clock.value = 300.0
            work_counts.update({"unit-a": 0, "unit-b": 0})
            crash_output = base / "crash-recovery-run"
            crash_arguments = argparse.Namespace(
                **{
                    **vars(arguments),
                    "output_dir": crash_output,
                }
            )
            original_claim = DISPATCH.claim_resume_checkpoint

            def crash_before_claim(*unused: Any, **unused_keywords: Any) -> Any:
                raise KeyboardInterrupt("synthetic crash before claim")

            DISPATCH.claim_resume_checkpoint = crash_before_claim
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    DISPATCH.run_phase(crash_arguments)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("synthetic pre-claim crash did not interrupt")
            finally:
                DISPATCH.claim_resume_checkpoint = original_claim
            crash_checkpoint = crash_output / "resume-checkpoint.0001.json"
            assert crash_checkpoint.is_file()
            assert not (crash_output / "resume-claim.0001.json").exists()
            with contextlib.redirect_stdout(io.StringIO()):
                assert DISPATCH.resume_phase(
                    argparse.Namespace(checkpoint=crash_checkpoint)
                ) == 0
            assert work_counts == {"unit-a": 1, "unit-b": 1}
            fault_outputs: dict[str, Path] = {}
            for fault in ("tool", "reroute", "safety"):
                FakeResumeAppServer.instances = 0
                FakeResumeAppServer.continuation_fault = fault
                clock.value += 100.0
                fault_output = base / f"continuation-{fault}"
                fault_outputs[fault] = fault_output
                fault_arguments = argparse.Namespace(
                    **{**vars(arguments), "output_dir": fault_output}
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    assert DISPATCH.run_phase(fault_arguments) == 2
            FakeResumeAppServer.continuation_fault = None
            FakeResumeAppServer.instances = 0
            FakeResumeAppServer.crash_after_claim = True
            clock.value += 100.0
            claimed_crash_output = base / "claimed-crash"
            claimed_crash_arguments = argparse.Namespace(
                **{**vars(arguments), "output_dir": claimed_crash_output}
            )
            with contextlib.redirect_stdout(io.StringIO()):
                assert DISPATCH.run_phase(claimed_crash_arguments) == 2
            FakeResumeAppServer.crash_after_claim = False
            try:
                DISPATCH.load_resume_checkpoint(
                    claimed_crash_output / "resume-checkpoint.0001.json"
                )
            except DISPATCH.DispatchError as error:
                assert "CLAIMED" in str(error)
            else:
                raise AssertionError("claimed ambiguous checkpoint was replayable")
        assert "thread/resume" in successful_methods
        assert "turn/resume" not in successful_methods
        assert successful_methods.count("turn/start") == 2
        metadata = json.loads(
            (output / "execution-metadata.json").read_text(encoding="utf-8")
        )
        assert metadata["completion_mode"] == "explicit_checkpoint_restart"
        assert metadata["server"]["total_tokens"] == 110
        assert len(metadata["server"]["transcript_chain"]) == 2
        assert (output / "resume-checkpoint.0001.json").is_file()
        assert (output / "resume-claim.0001.json").is_file()
        checkpoint_value = json.loads(
            (output / "resume-checkpoint.0001.json").read_text(encoding="utf-8")
        )
        resume_contract = checkpoint_value["binding"]["resume_contract"]
        DISPATCH.validate_checkpoint_runtime_state(
            checkpoint_value,
            resume_contract=resume_contract,
            requested=REQUESTED,
            token_cap=200,
        )
        forged_usage = json.loads(json.dumps(checkpoint_value))
        forged_usage["usage"] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        forged_usage["elapsed_ms"] = 0
        forged_usage["budgets"]["remaining_token_cap"] = 200
        forged_usage["budgets"]["remaining_wall_time_ms"] = 10_000
        forged_checkpoint = json.loads(json.dumps(checkpoint_value))
        forged_checkpoint["explicit_checkpoint"]["message_id"] = "forged-message"
        for label, forged in (
            ("coordinated usage reset", forged_usage),
            ("checkpoint message", forged_checkpoint),
        ):
            try:
                DISPATCH.validate_checkpoint_runtime_state(
                    forged,
                    resume_contract=resume_contract,
                    requested=REQUESTED,
                    token_cap=200,
                )
            except DISPATCH.DispatchError:
                pass
            else:
                raise AssertionError(f"self-rehashed {label} forgery was accepted")
        reasoning_abort = json.loads(
            (reasoning_output / "aborted.json").read_text(encoding="utf-8")
        )
        assert reasoning_abort["status"] == "ABORTED_NO_RESUMABLE_CHECKPOINT"
        assert (
            reasoning_abort["completion_mode"]
            == "aborted_without_resumable_checkpoint"
        )
        assert not list(reasoning_output.glob("resume-checkpoint.*.json"))
        assert (reasoning_output / "execution-receipt.json").is_file()
        crash_metadata = json.loads(
            (crash_output / "execution-metadata.json").read_text(encoding="utf-8")
        )
        assert crash_metadata["completion_mode"] == "explicit_checkpoint_restart"
        for fault, fault_output in fault_outputs.items():
            fault_abort = json.loads(
                (fault_output / "aborted.json").read_text(encoding="utf-8")
            )
            assert fault_abort["category"] == "BLOCKED_MODEL_ENFORCEMENT", fault
        claimed_crash_abort = json.loads(
            (claimed_crash_output / "aborted.json").read_text(encoding="utf-8")
        )
        assert (
            claimed_crash_abort["category"]
            == "ABORTED_AMBIGUOUS_RESUME_CLAIM"
        )
        assert claimed_crash_abort["completion_mode"] == "aborted"
        assert (claimed_crash_output / "execution-receipt.json").is_file()
        assert (claimed_crash_output / "resume-claim.0001.json").is_file()


def test_deterministic_quality_is_recomputed_before_registration() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-quality-receipt-") as temporary:
        root = Path(temporary)
        with isolated_registry(root) as registry:
            evaluated = DISPATCH.append_execution_receipt(
                retained_execution_payload(root / "evaluated-run")
            )
            grader = DISPATCH.DETERMINISTIC_GRADER.resolve()
            actual = root / "actual.txt"
            actual.write_text("expected bytes\n", encoding="utf-8")
            grader_input_value = {
                "schema_version": 1,
                "case_id": "case-deterministic",
                "evaluated_arm": "challenger",
                "evaluated_execution_receipt_ids": [evaluated["receipt_id"]],
                "grader_identity_sha256": DISPATCH.sha256_bytes(
                    grader.read_bytes()
                ),
                "rubric_sha256": "d" * 64,
                "checks": [
                    {
                        "name": "expected-content",
                        "kind": "file_sha256",
                        "actual_path": str(actual.resolve()),
                        "expected_sha256": DISPATCH.sha256_bytes(
                            actual.read_bytes()
                        ),
                    }
                ],
            }
            quality = {
                "schema_version": 1,
                "case_id": "case-deterministic",
                "evaluated_arm": "challenger",
                "evaluated_execution_receipt_ids": [evaluated["receipt_id"]],
                "grader_kind": "deterministic",
                "grader_identity_sha256": DISPATCH.sha256_bytes(
                    grader.read_bytes()
                ),
                "rubric_sha256": "d" * 64,
                "arms": {
                    "challenger": {
                        "quality_score": 1.0,
                        "objective_gates": {"expected-content": True},
                    }
                },
            }
            quality_path = root / "quality.json"
            grader_input = root / "grader-input.json"
            DISPATCH.write_json(quality_path, quality)
            DISPATCH.write_json(grader_input, grader_input_value)
            args = argparse.Namespace(
                quality_artifact=quality_path,
                grader_executable=grader,
                grader_input=grader_input,
                producer_receipt_id=None,
                grader_arg=[],
                grader_timeout_seconds=10,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                assert DISPATCH.register_quality(args) == 0
            receipts = DISPATCH._decode_receipt_registry(registry.read_bytes())
            assert len(receipts) == 2
            assert receipts[1]["receipt_type"] == "quality"
            assert receipts[1]["producer_kind"] == "pinned-deterministic-grader"
            assert receipts[1]["evaluated_execution_receipt_ids"] == [
                evaluated["receipt_id"]
            ]

            actual.write_text("changed after retained quality\n", encoding="utf-8")
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    DISPATCH.register_quality(args)
            except DISPATCH.DispatchError as error:
                assert "differs" in str(error)
            else:
                raise AssertionError("unrecomputed deterministic quality was registered")
            assert len(DISPATCH._decode_receipt_registry(registry.read_bytes())) == 2


def test_blind_quality_is_bound_to_registered_grader_output() -> None:
    with tempfile.TemporaryDirectory(prefix="workflow-blind-quality-") as temporary:
        root = Path(temporary)
        with isolated_registry(root) as registry:
            observed_identity = dict(REQUESTED)
            profile_sha256 = "b" * 64
            evaluated = DISPATCH.append_execution_receipt(
                retained_execution_payload(root / "evaluated-run")
            )
            grader_identity = DISPATCH.content_hash(
                {
                    "observed_identity": observed_identity,
                    "profile_sha256": profile_sha256,
                }
            )
            quality = {
                "schema_version": 1,
                "grader_kind": "blind_independent",
                "grader_identity_sha256": grader_identity,
                "rubric_sha256": "e" * 64,
                "case_id": "case-blind",
                "evaluated_arm": "challenger",
                "evaluated_execution_receipt_ids": [evaluated["receipt_id"]],
                "blind": True,
                "quality": 0.8,
                "verdict": "PASS",
            }
            output = DISPATCH.canonical_json(quality)
            payload = retained_execution_payload(root / "grader-run", output)
            payload["observed_identity"] = observed_identity
            payload["profile_sha256"] = profile_sha256
            producer = DISPATCH.append_execution_receipt(payload)
            quality_path = root / "quality.json"
            DISPATCH.write_json(quality_path, quality)
            args = argparse.Namespace(
                quality_artifact=quality_path,
                grader_executable=None,
                grader_input=None,
                producer_receipt_id=producer["receipt_id"],
                grader_arg=[],
                grader_timeout_seconds=10,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                assert DISPATCH.register_quality(args) == 0
            receipts = DISPATCH._decode_receipt_registry(registry.read_bytes())
            assert len(receipts) == 3
            assert receipts[2]["receipt_type"] == "quality"
            assert receipts[2]["producer_receipt_id"] == producer["receipt_id"]
            assert receipts[2]["evaluated_execution_receipt_ids"] == [
                evaluated["receipt_id"]
            ]
            assert receipts[2]["previous_receipt_sha256"] == producer[
                "receipt_sha256"
            ]


if __name__ == "__main__":
    tests = (
        test_install_journal_blocks_execution_except_bound_verifier,
        test_exact_metadata_is_accepted,
        test_explicit_checkpoint_envelope_is_strict_and_reasoning_never_qualifies,
        test_resume_checkpoint_is_atomic_private_self_hashed_and_single_use,
        test_resume_checkpoint_rejects_partial_transcript_and_budget_tampering,
        test_resume_checkpoint_binding_tamper_matrix_fails_closed,
        test_route_mismatch_is_rejected,
        test_missing_service_tier_field_is_rejected,
        test_malformed_service_tier_is_rejected,
        test_any_bound_thread_settings_mismatch_is_rejected,
        test_reroute_and_safety_buffering_are_rejected,
        test_wrong_turn_cannot_supply_evidence,
        test_second_turn_on_ephemeral_thread_is_rejected,
        test_duplicate_turn_completion_is_rejected,
        test_token_cap_is_enforced,
        test_smoke_route_facts_resolve_to_requested_tiers,
        test_catalog_staleness_does_not_disable_healthy_routing,
        test_unrestricted_worker_sandbox_is_rejected,
        test_token_budget_is_derived_from_phase_requirements,
        test_bound_context_no_tools_budget_is_single_inference,
        test_no_tools_contract_rejects_tool_items,
        test_historical_mutating_transcripts_require_observed_tool_use,
        test_workspace_write_instructions_identify_local_execution_bridge,
        test_no_tools_contract_rejects_unknown_and_extended_item_types,
        test_no_tools_live_interrupt_ignores_foreign_thread_events,
        test_malformed_event_shapes_become_dispatch_errors,
        test_no_tools_contract_requires_read_only_offline_execution,
        test_planner_dispatch_packet_binds_exact_inputs,
        test_workflow_plan_identity_and_active_phase_are_enforced,
        test_self_rehashed_semantically_invalid_plans_are_rejected,
        test_explicit_token_cap_requires_a_fixed_task_contract,
        test_usage_errors_always_retain_an_abort_artifact,
        test_execution_registry_append_is_hash_chained_and_tamper_evident,
        test_execution_registry_recovers_a_complete_interrupted_append,
        test_run_phase_registers_only_clean_completed_provenance,
        test_checkpoint_restart_uses_fresh_turn_and_only_unfinished_work,
        test_deterministic_quality_is_recomputed_before_registration,
        test_blind_quality_is_bound_to_registered_grader_output,
    )
    if os.environ.get("ADAPTIVE_RESUME_CANARY_ONLY") == "1":
        tests = (test_checkpoint_restart_uses_fresh_turn_and_only_unfinished_work,)
    for test in tests:
        test()
    print("workflow-dispatch deterministic tests passed")
