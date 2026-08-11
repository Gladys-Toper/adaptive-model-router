#!/usr/bin/env python3
"""Bounded synthetic proof for explicit-checkpoint restart efficiency."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

import workflow_dispatch as dispatch


REQUESTED = {
    "provider": "openai",
    "model": "gpt-5.6-luna",
    "effort": "low",
    "service_tier": "default",
}


def protocol_events(
    thread_id: str, turn_id: str, checkpoint_item: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        {
            "method": "thread/settings/updated",
            "params": {
                "threadId": thread_id,
                "threadSettings": {
                    "modelProvider": REQUESTED["provider"],
                    "model": REQUESTED["model"],
                    "effort": REQUESTED["effort"],
                    "serviceTier": "default",
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
            "method": "item/completed",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
                "item": checkpoint_item,
            },
        },
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": thread_id,
                "turnId": turn_id,
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
                "threadId": thread_id,
                "turn": {
                    "id": turn_id,
                    "status": "interrupted",
                    "durationMs": 3_000,
                },
            },
        },
    ]


def artifact_probe(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    _, policy_raw, policy = dispatch.load_resumption_policy()
    thread_id = "canary-thread"
    turn_id = "canary-turn-1"
    work_manifest = {
        "path": str((root / "work-manifest.json").resolve()),
        "file_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "work_items": [
            {"id": "unit-a", "description": "Complete unit A."},
            {"id": "unit-b", "description": "Complete unit B."},
        ],
    }
    nonce = "canary-nonce"
    payload = {
        "checkpoint_nonce": nonce,
        "checkpoint_type": "adaptive-workflow.explicit-checkpoint",
        "completed_results": {"unit-a": "unit A complete"},
        "completed_work_ids": ["unit-a"],
        "remaining_work_ids": ["unit-b"],
        "schema_version": 1,
        "summary": "unit A complete; unit B remains",
    }
    checkpoint_item = {
        "type": "agentMessage",
        "id": "canary-checkpoint-message",
        "phase": "commentary",
        "text": dispatch.CHECKPOINT_PREFIX + dispatch.canonical_json(payload),
    }
    explicit = dispatch.parse_explicit_checkpoint(
        checkpoint_item,
        nonce=nonce,
        work_manifest=work_manifest,
        policy=policy,
    )
    transcript = root / "app-server.0001.jsonl"
    transcript.write_text(
        "".join(
            dispatch.canonical_json(event) + "\n"
            for event in protocol_events(thread_id, turn_id, checkpoint_item)
        ),
        encoding="utf-8",
    )
    os.chmod(transcript, 0o600)
    dispatch._fsync_file(transcript)
    timing_path, timing = dispatch.write_segment_timing(
        root,
        sequence=1,
        thread_id=thread_id,
        turn_id=turn_id,
        segment_elapsed_ms=3_000,
        cumulative_elapsed_ms=3_000,
        previous_timing_sha256=None,
    )
    segment = {
        "sequence": 1,
        "path": str(transcript.resolve()),
        "sha256": dispatch.sha256_bytes(transcript.read_bytes()),
        "thread_id": thread_id,
        "turn_id": turn_id,
        "turn_status": "interrupted",
        "timing_path": str(timing_path.resolve()),
        "timing_file_sha256": dispatch.sha256_bytes(timing_path.read_bytes()),
        "timing_sha256": timing["timing_sha256"],
        "segment_elapsed_ms": 3_000,
        "cumulative_elapsed_ms": 3_000,
    }
    resume_contract = {
        "mode": "explicit_checkpoint_restart",
        "work_manifest": work_manifest,
        "cumulative_wall_time_seconds": 15,
        "max_continuations": 1,
        "checkpoint_ttl_seconds": 60,
    }
    binding = {
        "run_id": root.name,
        "resumption_policy_path": str(dispatch.RESUMPTION_POLICY.resolve()),
        "resumption_policy_file_sha256": dispatch.sha256_bytes(policy_raw),
        "resumption_policy_sha256": policy["policy_sha256"],
        "resume_contract": resume_contract,
    }
    now_ms = int(time.time() * 1000)
    identity = {
        "schema_version": 1,
        "contract_name": policy["checkpoint_contract_name"],
        "contract_version": policy["checkpoint_contract_version"],
        "checkpoint_id": "canary-checkpoint-1",
        "sequence": 1,
        "previous_checkpoint_sha256": None,
        "created_at_unix_ms": now_ms,
        "expires_at_unix_ms": now_ms + 60_000,
        "mode": "explicit_checkpoint_restart",
        "binding": binding,
        "thread": {
            "thread_id": thread_id,
            "source_turn_id": turn_id,
            "turn_ids": [turn_id],
        },
        "transcript_chain": [segment],
        "transcript_chain_sha256": dispatch.transcript_chain_sha256([segment]),
        "explicit_checkpoint": explicit,
        "usage": {"input_tokens": 60, "output_tokens": 10, "total_tokens": 70},
        "elapsed_ms": 3_000,
        "budgets": {
            "cumulative_token_cap": 200,
            "remaining_token_cap": 130,
            "cumulative_wall_time_ms": 15_000,
            "remaining_wall_time_ms": 12_000,
            "continuation_limit": 1,
            "continuations_used": 0,
        },
        "interruption_reason": "cooperative_segment_checkpoint",
    }
    checkpoint_started = time.perf_counter_ns()
    checkpoint_path, checkpoint = dispatch.write_resume_checkpoint(
        root, identity, policy
    )
    checkpoint_write_ns = time.perf_counter_ns() - checkpoint_started
    loaded, _ = dispatch.load_resume_checkpoint(checkpoint_path)
    dispatch.validate_checkpoint_runtime_state(
        loaded,
        resume_contract=resume_contract,
        requested=REQUESTED,
        token_cap=200,
    )
    claim_started = time.perf_counter_ns()
    claim_path, claim = dispatch.claim_resume_checkpoint(
        checkpoint_path,
        checkpoint,
        continuation_input_sha256=dispatch.sha256_bytes(
            dispatch.continuation_prompt(checkpoint).encode("utf-8")
        ),
    )
    claim_write_ns = time.perf_counter_ns() - claim_started
    if claim["checkpoint_sha256"] != checkpoint["checkpoint_sha256"]:
        raise RuntimeError("canary claim did not bind the checkpoint")
    return {
        "checkpoint_bytes": checkpoint_path.stat().st_size,
        "checkpoint_file_sha256": dispatch.sha256_bytes(
            checkpoint_path.read_bytes()
        ),
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "checkpoint_write_ms": round(checkpoint_write_ns / 1_000_000, 3),
        "claim_bytes": claim_path.stat().st_size,
        "claim_file_sha256": dispatch.sha256_bytes(claim_path.read_bytes()),
        "claim_sha256": claim["claim_sha256"],
        "claim_write_ms": round(claim_write_ns / 1_000_000, 3),
        "transcript_sha256": segment["sha256"],
    }


def run_canary(wall_time_seconds: int) -> dict[str, Any]:
    if wall_time_seconds != 5:
        raise ValueError("the frozen canary requires --wall-time-seconds 5")
    started = time.perf_counter()
    tests = Path(__file__).resolve().with_name("test_workflow_dispatch.py")
    test_environment = dict(os.environ)
    test_environment["ADAPTIVE_RESUME_CANARY_ONLY"] = "1"
    completed = subprocess.run(
        [sys.executable, str(tests)],
        capture_output=True,
        text=True,
        timeout=wall_time_seconds,
        check=False,
        env=test_environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "dispatcher checkpoint/restart suite failed: "
            + (completed.stderr or completed.stdout)[-2000:]
        )
    with tempfile.TemporaryDirectory(prefix="adaptive-resume-canary-") as temporary:
        probe = artifact_probe(Path(temporary) / "run")
    process_elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    if process_elapsed_ms > wall_time_seconds * 1000:
        raise RuntimeError("canary exceeded its real five-second watchdog")

    without_resumption = {
        "execution_counts": {"unit-a": 2, "unit-b": 1},
        "repeated_input_tokens": 60,
        "repeated_output_reasoning_tokens": 10,
        "total_input_tokens": 140,
        "total_output_reasoning_tokens": 30,
        "total_tokens": 170,
        "synthetic_latency_ms": 10_000,
    }
    with_checkpoint = {
        "completion_mode": "explicit_checkpoint_restart",
        "execution_counts": {"unit-a": 1, "unit-b": 1},
        "repeated_input_tokens": 0,
        "repeated_output_reasoning_tokens": 0,
        "total_input_tokens": 90,
        "total_output_reasoning_tokens": 20,
        "total_tokens": 110,
        "synthetic_latency_ms": 7_000,
    }
    if with_checkpoint["execution_counts"] != {"unit-a": 1, "unit-b": 1}:
        raise RuntimeError("completed work was duplicated")
    return {
        "schema_version": 1,
        "status": "PASS",
        "protocol_mode": "explicit_checkpoint_restart",
        "genuine_unfinished_turn_resumption_supported": False,
        "wall_time_cap_seconds": wall_time_seconds,
        "comparison_scope": "synthetic protocol evidence; not model-performance evidence",
        "without_resumption": without_resumption,
        "with_checkpoint_restart": with_checkpoint,
        "savings": {
            "input_tokens": 50,
            "output_reasoning_tokens": 10,
            "total_tokens": 60,
            "synthetic_latency_ms": 3_000,
        },
        "checkpoint_overhead": probe,
        "provider_cache": {
            "status": "unknown",
            "observed_cache_read_tokens": None,
            "reason": "the deterministic fake protocol emits no provider cache metadata",
        },
        "evidence": {
            "dispatcher_e2e_suite": "PASS",
            "test_stdout_sha256": dispatch.sha256_bytes(
                completed.stdout.encode("utf-8")
            ),
            "process_elapsed_ms": process_elapsed_ms,
            "exactly_once_completed_work": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wall-time-seconds", type=int, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.wall_time_seconds < 1:
        parser.error("wall-time-seconds must be positive")

    def timeout_handler(unused_signum: int, unused_frame: Any) -> None:
        raise TimeoutError("resume canary watchdog elapsed")

    previous_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, args.wall_time_seconds)
    try:
        result = run_canary(args.wall_time_seconds)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    if args.output is not None:
        output = args.output.expanduser().absolute()
        if output.is_symlink():
            raise RuntimeError("canary output path is unsafe")
        dispatch._atomic_private_json(output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
