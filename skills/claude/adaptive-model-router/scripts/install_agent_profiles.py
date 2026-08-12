#!/usr/bin/env python3
"""Install the Adaptive Model Router custom-agent profiles safely.

Run only on an explicit user request. This installs the bundled baseline
profiles; use `python3 router_lab.py sync-agents` to render the ACTIVE
adaptive policy instead.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent.parent / "assets" / "custom-agents"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install bundled model-routing profiles into CLAUDE_CONFIG_DIR/agents."
    )
    parser.add_argument(
        "--target",
        type=Path,
        help="Override the destination agents directory.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify installed profiles without writing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show writes without changing files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace divergent destination profiles.",
    )
    return parser.parse_args()


def destination_dir(args: argparse.Namespace) -> Path:
    if args.target:
        return args.target.expanduser()
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return claude_home.expanduser() / "agents"


def replace_file(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.adaptive-model-router.tmp")
    temporary.write_bytes(source.read_bytes())
    os.replace(temporary, destination)


def main() -> int:
    args = parse_args()
    target_dir = destination_dir(args)
    sources = sorted(SOURCE_DIR.glob("*.md"))
    if not sources:
        print(f"No bundled profiles found in {SOURCE_DIR}", file=sys.stderr)
        return 2

    if not args.check and not args.dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for source in sources:
        destination = target_dir / source.name
        if destination.is_symlink():
            print(f"REFUSE symlink {destination}")
            failures += 1
            continue

        if not destination.exists():
            if args.check:
                print(f"MISSING {destination}")
                failures += 1
            elif args.dry_run:
                print(f"WOULD_INSTALL {destination}")
            else:
                replace_file(source, destination)
                print(f"INSTALLED {destination}")
            continue

        if destination.read_bytes() == source.read_bytes():
            print(f"CURRENT {destination}")
            continue

        if args.check:
            print(f"STALE {destination}")
            failures += 1
        elif not args.force:
            print(f"CONFLICT {destination}; rerun with --force to replace")
            failures += 1
        elif args.dry_run:
            print(f"WOULD_REPLACE {destination}")
        else:
            replace_file(source, destination)
            print(f"REPLACED {destination}")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
