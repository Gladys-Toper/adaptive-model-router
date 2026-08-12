#!/usr/bin/env python3
"""Emit a quality artifact from closed, reproducible file assertions on stdin."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def regular_file(value: Any) -> Path:
    if not isinstance(value, str):
        raise ValueError("check path must be an absolute string")
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("check path must be an absolute retained regular file")
    return path.resolve()


def evaluate(check: dict[str, Any]) -> bool:
    if not isinstance(check, dict) or set(check) not in (
        {"name", "kind", "actual_path", "expected_sha256"},
        {"name", "kind", "actual_path", "expected_path"},
    ):
        raise ValueError("deterministic check has an unsupported closed schema")
    if not isinstance(check["name"], str) or not check["name"]:
        raise ValueError("deterministic check name is required")
    actual = regular_file(check["actual_path"])
    if check["kind"] == "file_sha256":
        expected = check["expected_sha256"]
        if not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("file_sha256 check requires an expected SHA-256")
        return sha256_bytes(actual.read_bytes()) == expected
    if check["kind"] == "exact_bytes":
        expected_path = regular_file(check["expected_path"])
        return actual.read_bytes() == expected_path.read_bytes()
    raise ValueError("unsupported deterministic check kind")


def main() -> int:
    try:
        request = json.load(sys.stdin)
        allowed = {
            "schema_version",
            "case_id",
            "evaluated_arm",
            "evaluated_execution_receipt_ids",
            "grader_identity_sha256",
            "rubric_sha256",
            "checks",
        }
        if not isinstance(request, dict) or set(request) != allowed:
            raise ValueError("grader input has an unsupported closed schema")
        if request["schema_version"] != 1:
            raise ValueError("unsupported grader-input schema")
        checks = request["checks"]
        if not isinstance(checks, list) or not checks:
            raise ValueError("at least one deterministic check is required")
        results = {check["name"]: evaluate(check) for check in checks}
        score = sum(1.0 for passed in results.values() if passed) / len(results)
        if not math.isfinite(score):
            raise ValueError("quality score is not finite")
        artifact = {
            "schema_version": 1,
            "case_id": request["case_id"],
            "evaluated_arm": request["evaluated_arm"],
            "evaluated_execution_receipt_ids": request[
                "evaluated_execution_receipt_ids"
            ],
            "grader_kind": "deterministic",
            "grader_identity_sha256": request["grader_identity_sha256"],
            "rubric_sha256": request["rubric_sha256"],
            "arms": {
                request["evaluated_arm"]: {
                    "quality_score": score,
                    "objective_gates": results,
                }
            },
        }
        json.dump(artifact, sys.stdout, sort_keys=True, separators=(",", ":"))
        sys.stdout.write("\n")
        return 0
    except Exception as error:
        print(f"deterministic_quality_grader: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
