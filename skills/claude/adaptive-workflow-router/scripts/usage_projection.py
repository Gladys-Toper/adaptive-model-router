#!/usr/bin/env python3
"""Usage projection: normalize execution receipts from both platforms into
comparable UsageRows and produce aggregated reports.

Platform-agnostic: locates both the Codex and Claude registries and reads each
with a per-platform adapter.  No imports from workflow_dispatch or
agent_governance — this is a read-only leaf module.

Stdlib only, Python 3.12+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------

def codex_registry_path() -> Path:
    """Return the path to the Codex execution registry (may not exist)."""
    home = os.environ.get("CODEX_HOME") or (str(Path.home() / ".codex"))
    return Path(home) / "adaptive-workflow-router" / "execution-registry.jsonl"


def claude_registry_path() -> Path:
    """Return the path to the Claude execution registry (may not exist)."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    home = Path(raw).expanduser() if raw else (Path.home() / ".claude")
    return home / "adaptive-workflow-router" / "execution-registry.jsonl"


# ---------------------------------------------------------------------------
# UsageRow
# ---------------------------------------------------------------------------

@dataclass
class UsageRow:
    platform: str                    # "codex" | "claude"
    receipt_sha256: str | None
    receipt_sequence: int | None
    issued_at_epoch: float | None
    status: str | None
    route_tier: str | None
    requested_model: str | None
    requested_effort: str | None
    observed_model: str | None
    phase_key: str | None
    phase_id: str | None
    activity: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    total_tokens: int | None
    model_cycles: int | None
    tool_cycles: int | None
    wall_ms: int | None
    evidence_grade: str | None
    source_commit: str | None


# ---------------------------------------------------------------------------
# Per-platform adapters
# ---------------------------------------------------------------------------

def project_codex_receipt(receipt: dict) -> UsageRow | None:
    """Project a Codex receipt dict into a UsageRow.

    Returns None for non-'execution' receipt types.
    """
    if receipt.get("receipt_type") != "execution":
        return None

    au = receipt.get("accepted_usage") or {}
    usage = receipt.get("usage") or {}
    req = receipt.get("requested_identity") or {}
    obs = receipt.get("observed_identity") or {}

    # Tier: Codex receipts encode tier inside accepted_usage or identity blocks;
    # the sample does not expose a direct "tier" key — use None when absent.
    route_tier: str | None = (
        au.get("tier")
        or req.get("tier")
        or obs.get("tier")
        or None
    )

    # Model/effort: accepted_usage is authoritative; fall back to identities.
    requested_model: str | None = req.get("model") or au.get("model") or None
    observed_model: str | None = obs.get("model") or au.get("model") or None
    requested_effort: str | None = req.get("effort") or au.get("effort") or None

    # Token counts: usage block is primary (snake_case).
    input_tokens: int | None = usage.get("input_tokens")
    output_tokens: int | None = usage.get("output_tokens")
    total_tokens: int | None = (
        usage.get("total_tokens") or au.get("total_tokens") or None
    )
    # Codex receipts in the sample carry no cache fields.
    cache_read_tokens: int | None = usage.get("cache_read_tokens")
    cache_write_tokens: int | None = usage.get("cache_write_tokens")

    model_cycles: int | None = usage.get("model_cycles")
    tool_cycles: int | None = usage.get("tool_cycles") if usage.get("tool_cycles") is not None else au.get("tool_cycles")

    wall_ms: int | None = (
        receipt.get("duration_ms")
        or au.get("duration_ms")
        or None
    )

    return UsageRow(
        platform="codex",
        receipt_sha256=receipt.get("receipt_sha256"),
        receipt_sequence=receipt.get("receipt_sequence"),
        issued_at_epoch=receipt.get("issued_at_epoch"),
        status=receipt.get("status"),
        route_tier=route_tier,
        requested_model=requested_model,
        requested_effort=requested_effort,
        observed_model=observed_model,
        phase_key=au.get("phase_key"),
        phase_id=au.get("phase_id"),
        activity=au.get("activity"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        total_tokens=total_tokens,
        model_cycles=model_cycles,
        tool_cycles=tool_cycles,
        wall_ms=wall_ms,
        evidence_grade=receipt.get("evidence_grade"),
        source_commit=receipt.get("source_commit"),
    )


def project_claude_receipt(receipt: dict) -> UsageRow | None:
    """Project a Claude receipt dict into a UsageRow.

    Returns None for non-'execution' receipt types.
    """
    if receipt.get("receipt_type") != "execution":
        return None

    rm = receipt.get("runtime_metadata") or {}
    model_usage: dict[str, dict] = rm.get("model_usage") or {}

    # Sum token counts across all models.
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    for mu in model_usage.values():
        input_tokens += mu.get("inputTokens", 0)
        output_tokens += mu.get("outputTokens", 0)
        cache_read_tokens += mu.get("cacheReadInputTokens", 0)
        cache_write_tokens += mu.get("cacheCreationInputTokens", 0)

    total_tokens = input_tokens + output_tokens + cache_read_tokens + cache_write_tokens

    observed_models: list = receipt.get("observed_models") or []
    observed_model: str | None = observed_models[0] if observed_models else None

    # Status: use receipt-level status if present, else "COMPLETED".
    status: str = receipt.get("status") or "COMPLETED"

    # model_cycles: num_turns from runtime_metadata when present.
    model_cycles: int | None = rm.get("num_turns")

    return UsageRow(
        platform="claude",
        receipt_sha256=receipt.get("receipt_sha256"),
        receipt_sequence=receipt.get("receipt_sequence"),
        issued_at_epoch=receipt.get("issued_at_epoch"),
        status=status,
        route_tier=receipt.get("tier"),
        requested_model=receipt.get("requested_model"),
        requested_effort=receipt.get("requested_effort"),
        observed_model=observed_model,
        phase_key=receipt.get("phase_key"),
        phase_id=receipt.get("phase_id"),
        activity=receipt.get("activity"),
        input_tokens=input_tokens if model_usage else None,
        output_tokens=output_tokens if model_usage else None,
        cache_read_tokens=cache_read_tokens if model_usage else None,
        cache_write_tokens=cache_write_tokens if model_usage else None,
        total_tokens=total_tokens if model_usage else None,
        model_cycles=model_cycles,
        tool_cycles=receipt.get("tool_cycles"),
        wall_ms=receipt.get("elapsed_ms") or rm.get("duration_ms") or None,
        evidence_grade=rm.get("evidence_grade"),
        source_commit=receipt.get("source_commit"),
    )


# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------

_ADAPTERS = {
    "codex": project_codex_receipt,
    "claude": project_claude_receipt,
}


def load_rows(
    platforms: Sequence[str] = ("codex", "claude"),
    since_epoch: float | None = None,
    until_epoch: float | None = None,
) -> tuple[list[UsageRow], int]:
    """Load UsageRows from each platform registry.

    Returns (rows, skipped_lines) where skipped_lines counts blank or
    malformed registry lines.  Missing registry files produce zero rows, not
    an error.
    """
    rows: list[UsageRow] = []
    skipped = 0

    path_fns = {
        "codex": codex_registry_path,
        "claude": claude_registry_path,
    }

    for platform in platforms:
        if platform not in _ADAPTERS:
            continue
        path = path_fns[platform]()
        if not path.exists():
            continue
        adapter = _ADAPTERS[platform]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                skipped += 1
                continue
            try:
                receipt = json.loads(stripped)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(receipt, dict):
                skipped += 1
                continue
            try:
                row = adapter(receipt)
            except Exception:
                skipped += 1
                continue
            if row is None:
                # non-execution receipt type — not an error, but skip
                continue
            # Apply time window filter.
            epoch = row.issued_at_epoch
            if since_epoch is not None and (epoch is None or epoch < since_epoch):
                continue
            if until_epoch is not None and (epoch is None or epoch > until_epoch):
                continue
            rows.append(row)

    return rows, skipped


# ---------------------------------------------------------------------------
# Summarizer
# ---------------------------------------------------------------------------

def _zero_agg() -> dict:
    return {
        "rows": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "wall_ms": 0,
    }


def _add_row(agg: dict, row: UsageRow) -> None:
    agg["rows"] += 1
    agg["input_tokens"] += row.input_tokens or 0
    agg["output_tokens"] += row.output_tokens or 0
    agg["cache_read_tokens"] += row.cache_read_tokens or 0
    agg["cache_write_tokens"] += row.cache_write_tokens or 0
    agg["total_tokens"] += row.total_tokens or 0
    agg["wall_ms"] += row.wall_ms or 0


def summarize(rows: list[UsageRow]) -> dict:
    """Return nested aggregates from a list of UsageRows."""
    totals = _zero_agg()
    by_platform: dict[str, dict] = {}
    by_tier: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_status: dict[str, dict] = {}

    for row in rows:
        _add_row(totals, row)

        p = row.platform or "unknown"
        by_platform.setdefault(p, _zero_agg())
        _add_row(by_platform[p], row)

        t = row.route_tier or "unknown"
        by_tier.setdefault(t, _zero_agg())
        _add_row(by_tier[t], row)

        m = row.requested_model or "unknown"
        by_model.setdefault(m, _zero_agg())
        _add_row(by_model[m], row)

        s = row.status or "unknown"
        by_status.setdefault(s, _zero_agg())
        _add_row(by_status[s], row)

    total_output = totals["output_tokens"]
    routed_output_share_by_tier: dict[str, float] = {}
    for tier, agg in by_tier.items():
        if total_output > 0:
            routed_output_share_by_tier[tier] = agg["output_tokens"] / total_output
        else:
            routed_output_share_by_tier[tier] = 0.0

    return {
        "totals": totals,
        "by_platform": by_platform,
        "by_tier": by_tier,
        "by_model": by_model,
        "by_status": by_status,
        "routed_output_share_by_tier": routed_output_share_by_tier,
    }


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _parse_epoch(value: str) -> float:
    """Parse ISO8601 string or numeric epoch string into a float epoch."""
    try:
        return float(value)
    except ValueError:
        pass
    # Try ISO8601 with or without Z suffix.
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Cannot parse date/epoch: {value!r}")


def _fmt_int(v: int) -> str:
    return f"{v:,}"


def _fmt_min(ms: int) -> str:
    minutes = ms / 60_000
    return f"{minutes:.1f}"


def _print_table(rows_list: list[UsageRow], summary: dict) -> None:
    """Print a compact human-readable table."""
    header = (
        f"{'platform':<10} {'tier':<8} {'model':<30} {'rows':>6} "
        f"{'out-tok':>10} {'in-tok':>10} {'cache-rd':>10} {'wall-min':>9}"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)

    # Group by (platform, tier, model)
    groups: dict[tuple[str, str, str], dict] = {}
    for row in rows_list:
        key = (row.platform or "?", row.route_tier or "?", row.requested_model or "?")
        if key not in groups:
            groups[key] = _zero_agg()
        _add_row(groups[key], row)

    for (plat, tier, model), agg in sorted(groups.items()):
        print(
            f"{plat:<10} {tier:<8} {model:<30} {agg['rows']:>6} "
            f"{_fmt_int(agg['output_tokens']):>10} "
            f"{_fmt_int(agg['input_tokens']):>10} "
            f"{_fmt_int(agg['cache_read_tokens']):>10} "
            f"{_fmt_min(agg['wall_ms']):>9}"
        )

    print(sep)
    t = summary["totals"]
    print(
        f"{'TOTAL':<10} {'':<8} {'':<30} {t['rows']:>6} "
        f"{_fmt_int(t['output_tokens']):>10} "
        f"{_fmt_int(t['input_tokens']):>10} "
        f"{_fmt_int(t['cache_read_tokens']):>10} "
        f"{_fmt_min(t['wall_ms']):>9}"
    )
    print()
    print("Routed output share by tier:")
    for tier, share in sorted(summary["routed_output_share_by_tier"].items()):
        print(f"  {tier}: {share:.1%}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="usage_projection",
        description="Project and report adaptive-workflow-router usage.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # report subcommand
    rep = sub.add_parser("report", help="Summarize usage from live registries.")
    rep.add_argument(
        "--platform",
        choices=("codex", "claude", "all"),
        default="all",
        help="Which platform registry to read (default: all).",
    )
    rep.add_argument("--since", metavar="ISO8601|EPOCH", help="Start of time window.")
    rep.add_argument("--until", metavar="ISO8601|EPOCH", help="End of time window.")
    rep.add_argument("--json", action="store_true", help="Output JSON instead of table.")

    # project subcommand
    proj = sub.add_parser("project", help="Project a single receipt file.")
    proj.add_argument(
        "--platform",
        choices=("codex", "claude"),
        required=True,
        help="Which adapter to use.",
    )
    proj.add_argument(
        "--receipt-file",
        required=True,
        metavar="FILE",
        help="Path to a JSON receipt file.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "project":
        receipt = json.loads(Path(args.receipt_file).read_text(encoding="utf-8"))
        adapter = _ADAPTERS[args.platform]
        row = adapter(receipt)
        if row is None:
            print("Receipt type is not 'execution' — no projection.")
        else:
            print(json.dumps(row.__dict__, indent=2, default=str))
        return 0

    # report
    platforms: tuple[str, ...]
    if args.platform == "all":
        platforms = ("codex", "claude")
    else:
        platforms = (args.platform,)

    since = _parse_epoch(args.since) if args.since else None
    until = _parse_epoch(args.until) if args.until else None

    rows_list, skipped = load_rows(platforms, since_epoch=since, until_epoch=until)
    summary = summarize(rows_list)

    if not rows_list:
        if args.json:
            print(json.dumps({"rows": [], "summary": summary, "skipped_lines": skipped}))
        else:
            print("no receipts in window")
            if skipped:
                print(f"(skipped {skipped} malformed/blank lines)")
        return 0

    if args.json:
        output = {
            "rows": [r.__dict__ for r in rows_list],
            "summary": summary,
            "skipped_lines": skipped,
        }
        print(json.dumps(output, default=str))
    else:
        _print_table(rows_list, summary)
        if skipped:
            print(f"\n(skipped {skipped} malformed/blank lines)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
