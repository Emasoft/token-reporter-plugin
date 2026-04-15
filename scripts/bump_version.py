#!/usr/bin/env python3
"""Bump version in plugin.json.

Usage:
    uv run scripts/bump_version.py --patch   # 1.1.0 -> 1.1.1
    uv run scripts/bump_version.py --minor   # 1.1.0 -> 1.2.0
    uv run scripts/bump_version.py --major   # 1.1.0 -> 2.0.0
    uv run scripts/bump_version.py --set 2.0.0  # explicit version
"""

import argparse
import json
import re
import sys
from pathlib import Path


def bump_version(current: str, part: str) -> str:
    """Bump a semver string by the specified part.

    Raises ValueError when *current* is not a valid x.y.z semver string.
    Raising (instead of sys.exit) keeps the function testable — callers
    decide how to surface the error.
    """
    parts = current.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError(f"Invalid semver: {current}")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if part == "major":
        return f"{major + 1}.0.0"
    elif part == "minor":
        return f"{major}.{minor + 1}.0"
    else:
        return f"{major}.{minor}.{patch + 1}"


def main():
    parser = argparse.ArgumentParser(description="Bump plugin version")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--patch", action="store_true", help="Bump patch version")
    group.add_argument("--minor", action="store_true", help="Bump minor version")
    group.add_argument("--major", action="store_true", help="Bump major version")
    group.add_argument("--set", type=str, help="Set explicit version (x.y.z)")
    args = parser.parse_args()

    # Resolve plugin root from script location
    script_dir = Path(__file__).resolve().parent
    plugin_root = script_dir.parent
    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"

    if not plugin_json.exists():
        print(f"ERROR: {plugin_json} not found", file=sys.stderr)
        sys.exit(1)

    # Read current version. Catch malformed JSON so users get a clean
    # error message instead of a raw JSONDecodeError traceback.
    try:
        manifest = json.loads(plugin_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: {plugin_json} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    # Coerce to str — manifest.get could return a non-string if the file
    # was hand-edited to e.g. a number; bump_version requires a string.
    current = str(manifest.get("version", "0.0.0"))

    # Compute new version. bump_version raises ValueError on invalid
    # semver — we catch it here and translate to a clean exit(1).
    try:
        if args.set:
            if not re.match(r"^\d+\.\d+\.\d+$", args.set):
                print(
                    f"ERROR: '{args.set}' is not valid semver (x.y.z)", file=sys.stderr
                )
                sys.exit(1)
            new_version = args.set
        elif args.major:
            new_version = bump_version(current, "major")
        elif args.minor:
            new_version = bump_version(current, "minor")
        else:
            new_version = bump_version(current, "patch")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if new_version == current:
        print(f"Version unchanged: {current}")
        return

    # Update plugin.json. Ensure the parent dir exists so first-time
    # writes on a clean checkout don't crash with FileNotFoundError.
    manifest["version"] = new_version
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  {plugin_json.relative_to(plugin_root)}: {current} -> {new_version}")
    print(f"\nVersion bumped: {current} -> {new_version}")


if __name__ == "__main__":
    main()
