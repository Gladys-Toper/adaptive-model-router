#!/usr/bin/env python3
"""Tests for usage_projection.py.

Uses inline fixtures built from the shapes in .dispatch-context/receipt-shapes.md.
Does NOT read live registries.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import usage_projection as up


# ---------------------------------------------------------------------------
# Inline fixtures from receipt-shapes.md
# ---------------------------------------------------------------------------

CODEX_RECEIPT: dict = {
    "schema_version": 1,
    "receipt_type": "execution",
    "status": "COMPLETED",
    "issued_at_epoch": 1786985536.798992,
    "dispatch_packet_sha256": None,
    "requested_identity": {
        "effort": "low",
        "model": "gpt-5.6-luna",
        "provider": "openai",
        "service_tier": "default",
    },
    "observed_identity": {
        "effort": "low",
        "model": "gpt-5.6-luna",
        "provider": "openai",
        "service_tier": "default",
    },
    "accepted_usage": {
        "activity": "inspect",
        "duration_ms": 4414,
        "effort": "low",
        "model": "gpt-5.6-luna",
        "model_api_call_updates": 1,
        "phase_id": "t1_observed_identity",
        "tool_cycles": 0,
        "tool_mode": "none",
        "total_tokens": 14512,
        "turn_cycles": 1,
        "workflow_id": "adaptive-workflow.live-smoke",
        "workflow_version": 1,
    },
    "usage": {
        "input_tokens": 14505,
        "model_api_call_updates": 1,
        "model_cycles": 1,
        "output_tokens": 7,
        "tool_cycles": 0,
        "total_tokens": 14512,
        "turn_cycles": 1,
    },
    "duration_ms": 4414,
    "source_commit": "0000000000000000000000000000000000000000",
    "receipt_sha256": "63acbe93f5ea16236e8d5f67cd0197462f13aa97c9b79a4a92bca413840d6a5f",
}

CLAUDE_RECEIPT: dict = {
    "schema_version": 1,
    "receipt_type": "execution",
    "issued_at_epoch": 1786985950.254451,
    "dispatch_packet_sha256": "0a882e7d381f9a5ff53c25f8e3b745f2bff9c0089582be9b08be97c8b91754ec",
    "tier": "T2",
    "requested_model": "sonnet",
    "requested_effort": "medium",
    "observed_models": ["claude-sonnet-4-6"],
    "execution_identity": "REQUESTED_PENDING_RUNTIME_METADATA",
    "phase_key": "coding.change:implement",
    "workflow_id": "coding.change",
    "runtime_metadata": {
        "duration_api_ms": 399509,
        "duration_ms": 400770,
        "evidence_grade": "declared",
        "evidence_source": "claude-code-headless-metadata",
        "is_error": False,
        "model_usage": {
            "claude-sonnet-4-6": {
                "cacheCreationInputTokens": 73029,
                "cacheReadInputTokens": 1854463,
                "contextWindow": 200000,
                "costUSD": 1.88924275,
                "inputTokens": 41,
                "maxOutputTokens": 32000,
                "outputTokens": 20215,
                "webSearchRequests": 0,
            }
        },
        "num_turns": 47,
        "session_id": "f4344dc9-5aa1-468b-9fd6-a92064ed58d1",
        "subtype": "success",
        "total_cost_usd": 1.88924275,
        "usage": {
            "cache_creation": {
                "ephemeral_1h_input_tokens": 73029,
                "ephemeral_5m_input_tokens": 0,
            },
            "cache_creation_input_tokens": 73029,
            "cache_read_input_tokens": 1854463,
            "inference_geo": "",
            "input_tokens": 41,
            "iterations": [],
            "output_tokens": 20215,
            "server_tool_use": {"web_fetch_requests": 0, "web_search_requests": 0},
            "service_tier": "standard",
            "speed": "standard",
        },
    },
    "elapsed_ms": 403856,
    "source_commit": "c7b097a5e5b97270ecffdc50473c1aaebe478b3f",
    "receipt_sha256": "0951fe7edab28c0ace59ee5d21bc00b789aa72765c7210210cf4ab05260af11e",
}

# A second model in model_usage for multi-model sum tests.
CLAUDE_RECEIPT_TWO_MODELS: dict = {
    **CLAUDE_RECEIPT,
    "issued_at_epoch": 1786985960.0,
    "receipt_sha256": "aaaa",
    "runtime_metadata": {
        **CLAUDE_RECEIPT["runtime_metadata"],
        "model_usage": {
            "claude-sonnet-4-6": {
                "cacheCreationInputTokens": 1000,
                "cacheReadInputTokens": 2000,
                "inputTokens": 100,
                "outputTokens": 200,
            },
            "claude-haiku-4-5": {
                "cacheCreationInputTokens": 500,
                "cacheReadInputTokens": 300,
                "inputTokens": 50,
                "outputTokens": 80,
            },
        },
    },
}

CLAUDE_RECEIPT_ABORTED: dict = {
    **CLAUDE_RECEIPT,
    "issued_at_epoch": 1786985970.0,
    "receipt_sha256": "bbbb",
    "status": "ABORTED_WALL_TIME",
}

# Non-execution receipts that should be skipped.
CODEX_NON_EXECUTION: dict = {**CODEX_RECEIPT, "receipt_type": "dispatch-plan"}
CLAUDE_NON_EXECUTION: dict = {**CLAUDE_RECEIPT, "receipt_type": "workflow-open"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_codex_projection_fields() -> None:
    row = up.project_codex_receipt(CODEX_RECEIPT)
    assert row is not None, "Expected a UsageRow, got None"
    assert row.platform == "codex"
    assert row.status == "COMPLETED"
    assert row.issued_at_epoch == 1786985536.798992
    assert row.requested_model == "gpt-5.6-luna"
    assert row.observed_model == "gpt-5.6-luna"
    assert row.requested_effort == "low"
    assert row.phase_id == "t1_observed_identity"
    assert row.activity == "inspect"
    assert row.input_tokens == 14505
    assert row.output_tokens == 7
    assert row.total_tokens == 14512
    assert row.model_cycles == 1
    assert row.tool_cycles == 0
    assert row.wall_ms == 4414
    assert row.source_commit == "0000000000000000000000000000000000000000"
    assert row.receipt_sha256 == "63acbe93f5ea16236e8d5f67cd0197462f13aa97c9b79a4a92bca413840d6a5f"
    print("  test_codex_projection_fields: PASS")


def test_claude_projection_sums_model_usage_and_cache() -> None:
    # Single model
    row = up.project_claude_receipt(CLAUDE_RECEIPT)
    assert row is not None
    assert row.platform == "claude"
    assert row.route_tier == "T2"
    assert row.requested_model == "sonnet"
    assert row.requested_effort == "medium"
    assert row.observed_model == "claude-sonnet-4-6"
    assert row.phase_key == "coding.change:implement"
    assert row.input_tokens == 41
    assert row.output_tokens == 20215
    assert row.cache_read_tokens == 1854463
    assert row.cache_write_tokens == 73029
    assert row.total_tokens == 41 + 20215 + 1854463 + 73029
    assert row.model_cycles == 47
    assert row.wall_ms == 403856
    assert row.evidence_grade == "declared"
    assert row.status == "COMPLETED"  # no status field → default

    # Two models: sums must add up.
    row2 = up.project_claude_receipt(CLAUDE_RECEIPT_TWO_MODELS)
    assert row2 is not None
    assert row2.input_tokens == 100 + 50
    assert row2.output_tokens == 200 + 80
    assert row2.cache_read_tokens == 2000 + 300
    assert row2.cache_write_tokens == 1000 + 500
    assert row2.total_tokens == (100 + 50) + (200 + 80) + (2000 + 300) + (1000 + 500)

    # ABORTED status is preserved.
    row3 = up.project_claude_receipt(CLAUDE_RECEIPT_ABORTED)
    assert row3 is not None
    assert row3.status == "ABORTED_WALL_TIME"

    print("  test_claude_projection_sums_model_usage_and_cache: PASS")


def test_non_execution_receipts_are_skipped() -> None:
    assert up.project_codex_receipt(CODEX_NON_EXECUTION) is None
    assert up.project_claude_receipt(CLAUDE_NON_EXECUTION) is None
    print("  test_non_execution_receipts_are_skipped: PASS")


def test_load_rows_window_and_malformed_lines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Codex registry: two valid receipts + blank + malformed.
        codex_home = tmp_path / "codex-home"
        codex_reg = codex_home / "adaptive-workflow-router" / "execution-registry.jsonl"
        codex_reg.parent.mkdir(parents=True)

        # Epoch 1786985536 for first, 9999999999 for second (outside window).
        receipt_early = {**CODEX_RECEIPT, "issued_at_epoch": 1786985536.0, "receipt_sha256": "c1"}
        receipt_late = {**CODEX_RECEIPT, "issued_at_epoch": 9999999999.0, "receipt_sha256": "c2"}
        codex_reg.write_text(
            json.dumps(receipt_early) + "\n"
            + "\n"  # blank line
            + "NOT JSON\n"  # malformed
            + json.dumps(receipt_late) + "\n",
            encoding="utf-8",
        )

        # Claude registry: one valid receipt.
        claude_home = tmp_path / "claude-home"
        claude_reg = claude_home / "adaptive-workflow-router" / "execution-registry.jsonl"
        claude_reg.parent.mkdir(parents=True)
        receipt_claude = {**CLAUDE_RECEIPT, "issued_at_epoch": 1786985950.0, "receipt_sha256": "cl1"}
        claude_reg.write_text(json.dumps(receipt_claude) + "\n", encoding="utf-8")

        old_codex = os.environ.get("CODEX_HOME")
        old_claude = os.environ.get("CLAUDE_CONFIG_DIR")
        try:
            os.environ["CODEX_HOME"] = str(codex_home)
            os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)

            # Window that includes only the early codex receipt and the claude receipt.
            rows, skipped = up.load_rows(
                platforms=("codex", "claude"),
                since_epoch=1786985500.0,
                until_epoch=1786986000.0,
            )
        finally:
            if old_codex is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old_codex
            if old_claude is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old_claude

    assert skipped == 2, f"Expected 2 skipped (blank + malformed), got {skipped}"
    assert len(rows) == 2, f"Expected 2 rows (early codex + claude), got {len(rows)}"
    platforms = {r.platform for r in rows}
    assert platforms == {"codex", "claude"}
    print("  test_load_rows_window_and_malformed_lines: PASS")


def test_summarize_routed_share() -> None:
    rows = [
        up.project_codex_receipt(CODEX_RECEIPT),
        up.project_claude_receipt(CLAUDE_RECEIPT),
    ]
    rows = [r for r in rows if r is not None]
    summary = up.summarize(rows)

    # Shares must sum to 1.0 (±1e-9).
    total_share = sum(summary["routed_output_share_by_tier"].values())
    assert abs(total_share - 1.0) < 1e-9 or total_share == 0.0, (
        f"Tier shares sum to {total_share}, expected 1.0"
    )

    # by_platform totals must equal overall totals.
    by_plat_rows = sum(v["rows"] for v in summary["by_platform"].values())
    assert by_plat_rows == summary["totals"]["rows"], (
        f"by_platform rows {by_plat_rows} != totals rows {summary['totals']['rows']}"
    )
    by_plat_out = sum(v["output_tokens"] for v in summary["by_platform"].values())
    assert by_plat_out == summary["totals"]["output_tokens"], (
        f"by_platform output_tokens {by_plat_out} != totals {summary['totals']['output_tokens']}"
    )

    print("  test_summarize_routed_share: PASS")


def test_cli_report_json_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        codex_home = tmp_path / "codex-home"
        codex_reg = codex_home / "adaptive-workflow-router" / "execution-registry.jsonl"
        codex_reg.parent.mkdir(parents=True)
        codex_reg.write_text(json.dumps(CODEX_RECEIPT) + "\n", encoding="utf-8")

        claude_home = tmp_path / "claude-home"
        claude_reg = claude_home / "adaptive-workflow-router" / "execution-registry.jsonl"
        claude_reg.parent.mkdir(parents=True)
        claude_reg.write_text(json.dumps(CLAUDE_RECEIPT) + "\n", encoding="utf-8")

        env = os.environ.copy()
        env["CODEX_HOME"] = str(codex_home)
        env["CLAUDE_CONFIG_DIR"] = str(claude_home)

        # Locate this script's sibling usage_projection.py.
        script = Path(__file__).parent / "usage_projection.py"
        result = subprocess.run(
            [sys.executable, str(script), "report", "--platform", "all", "--json"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, f"CLI exited {result.returncode}: {result.stderr}"

        data = json.loads(result.stdout)
        assert "rows" in data, "Missing 'rows' key"
        assert "summary" in data, "Missing 'summary' key"
        assert "skipped_lines" in data, "Missing 'skipped_lines' key"
        assert len(data["rows"]) == 2, f"Expected 2 rows, got {len(data['rows'])}"
        summary = data["summary"]
        assert "totals" in summary
        assert "by_platform" in summary
        assert "by_tier" in summary
        assert "by_model" in summary
        assert "by_status" in summary
        assert "routed_output_share_by_tier" in summary

    print("  test_cli_report_json_roundtrip: PASS")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Running usage-projection tests...")
    test_codex_projection_fields()
    test_claude_projection_sums_model_usage_and_cache()
    test_non_execution_receipts_are_skipped()
    test_load_rows_window_and_malformed_lines()
    test_summarize_routed_share()
    test_cli_report_json_roundtrip()
    print("usage-projection tests passed")


if __name__ == "__main__":
    main()
