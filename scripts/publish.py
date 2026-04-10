#!/usr/bin/env python3
"""Publish a new release with mandatory quality gates.

Usage:
    uv run scripts/publish.py              # auto-bump via git-cliff (default)
    uv run scripts/publish.py --patch      # force patch bump
    uv run scripts/publish.py --minor      # force minor bump
    uv run scripts/publish.py --major      # force major bump
    uv run scripts/publish.py --set 2.0.0  # explicit version
    uv run scripts/publish.py --dry-run    # preview — still runs all checks

WORKFLOW (strict order):
    1. Lint / test / validate (gates 0–7, no file modifications)
    2. Compute next version (git-cliff --bumped-version from conventional commits)
    3. Update plugin.json + pyproject.toml
    4. Re-validate with CPV
    5. Regenerate CHANGELOG.md via git-cliff
    6. Commit "chore(release): vX.Y.Z"
    7. Create annotated tag
    8. Push commits + tags
    9. Create GitHub release with notes extracted from CHANGELOG.md

MANDATORY QUALITY GATES (all unskippable, all must return 0 errors):
    0.  Tool availability:    uvx, python3, git, gh, git-cliff, uv all installed
    1.  Pre-push hook:        .git/hooks/pre-push exists and is executable
    2.  Clean working tree:   no uncommitted or staged changes
    3.  Lint (all .py files): ruff check + ruff format --check
    4.  Type check:           mypy on token-reporter.py (0 errors)
    5.  Syntax check:         py_compile all .py files in scripts/
    6.  Test suite:           every test in tests_dev/ passes (0 failures)
    7.  CPV validation:       remote cpv-validate, summary must be all 0s
    8.  Version bump:         plugin.json AND pyproject.toml updated atomically
    9.  CPV re-validation:    post-bump verification
    10. Changelog:            git-cliff regenerates CHANGELOG.md (MANDATORY)
    11. Commit:               chore(release) format, verified afterwards
    12. Tag:                  annotated tag, verified to exist
    13. Push:                 git push --follow-tags (pre-push hook is another gate)
    14. GitHub release:       gh release create with notes from CHANGELOG.md

NO EXCEPTIONS. NO SKIPS. Any failure at ANY step aborts the release.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn


# ─────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────


def die(msg: str, *, hint: str = "") -> NoReturn:
    """Print an error and abort the release. Never returns."""
    print(f"\n\033[91mBLOCKED\033[0m: {msg}", file=sys.stderr)
    if hint:
        print(f"  hint: {hint}", file=sys.stderr)
    sys.exit(1)


def step(n: str, title: str) -> None:
    print(f"\n── {n}. {title} ──")


def ok(msg: str) -> None:
    print(f"  \033[92mOK\033[0m: {msg}")


# ─────────────────────────────────────────────
# Subprocess wrappers
# ─────────────────────────────────────────────


def run_strict(
    cmd: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    """Run a command and abort the release on non-zero exit code.

    Used for commands that MUST succeed. No caller can accidentally ignore
    the return code because we call die() on failure here.
    """
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        die(f"command failed with exit code {result.returncode}: {' '.join(cmd)}")
    return result


def run_probe(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a command for its return code; capture output but do not abort.

    Only used where the return code is explicitly inspected by the caller.
    """
    return subprocess.run(cmd, capture_output=True, text=True)


# ─────────────────────────────────────────────
# Semver helpers
# ─────────────────────────────────────────────


def bump_version(current: str, part: str) -> str:
    parts = current.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        die(f"'{current}' is not valid semver (x.y.z)")
    major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def extract_release_notes(changelog_path: Path, version: str) -> str:
    """Extract the section for a specific version from CHANGELOG.md.

    Matches the section between ``## [version]`` and the next ``## [`` header
    or end of file. The ``version`` argument is the bare semver (no ``v`` prefix)
    because git-cliff strips the prefix in section headers via trim_start_matches.
    """
    if not changelog_path.exists():
        die(f"CHANGELOG.md not found at {changelog_path}")
    content = changelog_path.read_text(encoding="utf-8")
    # Match: ## [1.3.2] - 2026-04-11\n ... (until next ## [ or EOF)
    pattern = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)"
    m = re.search(pattern, content, re.MULTILINE | re.DOTALL)
    if m is None:
        die(
            f"could not find section for version {version} in {changelog_path}",
            hint="cliff.toml template may be broken or version mismatch",
        )
    notes = m.group(1).strip()
    if not notes:
        die(f"section for version {version} is empty in {changelog_path}")
    return notes


def compute_next_version(current: str, args: argparse.Namespace) -> str:
    """Determine the next release version.

    Priority (highest first):
        1. --set VERSION:   explicit version override
        2. --major/--minor/--patch:  manual bump level
        3. default:  git-cliff --bumped-version (auto-compute from commits)

    Returns a bare semver string (no 'v' prefix).
    """
    if args.set:
        if not re.match(r"^\d+\.\d+\.\d+$", args.set):
            die(f"'{args.set}' is not valid semver (x.y.z)")
        return args.set
    if args.major:
        return bump_version(current, "major")
    if args.minor:
        return bump_version(current, "minor")
    if args.patch:
        return bump_version(current, "patch")
    # Default: delegate to git-cliff based on conventional commits.
    r = run_probe(["git-cliff", "--bumped-version"])
    if r.returncode != 0:
        die(
            "git-cliff --bumped-version failed",
            hint=(
                "check that conventional commit parsers are configured "
                "in cliff.toml, or use --patch/--minor/--major explicitly"
            ),
        )
    bumped = r.stdout.strip()
    if not bumped:
        die(
            "git-cliff returned no bumped version",
            hint=(
                "no releasable changes detected. Either nothing committed "
                "since the last tag, or no commits match a parser rule. "
                "Use --patch/--minor/--major to force a bump."
            ),
        )
    # git-cliff returns tags with the 'v' prefix (matching tag_pattern).
    # Strip it to get the bare semver for plugin.json / pyproject.toml.
    return bumped.lstrip("v")


# ─────────────────────────────────────────────
# GATE 0: tool availability
# ─────────────────────────────────────────────

REQUIRED_TOOLS = ["uvx", "python3", "git", "gh", "git-cliff", "uv"]


def gate_tools() -> None:
    step("0", "Tool availability")
    missing = []
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        die(
            f"missing required tools: {', '.join(missing)}",
            hint=(
                "install them before publishing. "
                "git-cliff: cargo install git-cliff; "
                "gh: brew install gh; "
                "uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
            ),
        )
    ok("all required tools present")


# ─────────────────────────────────────────────
# GATE 1: pre-push hook installed
# ─────────────────────────────────────────────


def gate_pre_push_hook(repo_root: Path) -> None:
    step("1", "Pre-push hook")
    # .git may be a file (worktree) or directory (main). Use git rev-parse.
    result = run_probe(["git", "rev-parse", "--git-path", "hooks/pre-push"])
    if result.returncode != 0:
        die("cannot resolve git hooks path")
    hook_path = (repo_root / result.stdout.strip()).resolve()
    if not hook_path.exists():
        die(
            f"pre-push hook missing at {hook_path}",
            hint=f"run: ln -sf ../../scripts/pre-push {hook_path}",
        )
    # Check executable (symlink target must be executable)
    real = hook_path.resolve()
    if not os.access(real, os.X_OK):
        die(
            f"pre-push hook at {real} is not executable",
            hint=f"run: chmod +x {real}",
        )
    ok(f"pre-push hook active at {hook_path}")


# ─────────────────────────────────────────────
# GATE 2: clean working tree
# ─────────────────────────────────────────────


def gate_clean_tree() -> None:
    step("2", "Clean working tree")
    if run_probe(["git", "diff", "--quiet"]).returncode != 0:
        die("uncommitted changes found", hint="commit or stash first")
    if run_probe(["git", "diff", "--cached", "--quiet"]).returncode != 0:
        die("staged changes found", hint="commit or unstage first")
    ok("working tree clean")


# ─────────────────────────────────────────────
# GATE 3: lint all Python files
# ─────────────────────────────────────────────


def _python_files(scripts_dir: Path) -> list[Path]:
    return sorted(scripts_dir.glob("*.py"))


def gate_lint(scripts_dir: Path) -> None:
    step("3", "Lint (all Python files)")
    py_files = _python_files(scripts_dir)
    if not py_files:
        die(f"no Python files found in {scripts_dir}")
    paths = [str(p) for p in py_files]
    print(f"  checking {len(py_files)} files: {', '.join(p.name for p in py_files)}")
    # ruff check
    r = run_probe(["uvx", "ruff", "check", *paths])
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        die("ruff check reported errors", hint="run: uvx ruff check --fix scripts/")
    # ruff format --check
    r = run_probe(["uvx", "ruff", "format", "--check", *paths])
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        die(
            "ruff format reported unformatted files",
            hint="run: uvx ruff format scripts/",
        )
    ok(f"ruff check + format clean on {len(py_files)} files")


# ─────────────────────────────────────────────
# GATE 4: type check (mypy)
# ─────────────────────────────────────────────


def gate_type_check(scripts_dir: Path) -> None:
    step("4", "Type check (mypy)")
    target = scripts_dir / "token-reporter.py"
    if not target.exists():
        die(f"{target} not found")
    r = run_probe(
        [
            "uvx",
            "--with",
            "tiktoken",
            "mypy",
            "--ignore-missing-imports",
            str(target),
        ]
    )
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        die("mypy reported type errors", hint="fix all type errors before publishing")
    ok("mypy: 0 errors")


# ─────────────────────────────────────────────
# GATE 5: syntax check (all Python files)
# ─────────────────────────────────────────────


def gate_syntax(scripts_dir: Path) -> None:
    step("5", "Syntax check (all Python files)")
    py_files = _python_files(scripts_dir)
    for f in py_files:
        r = run_probe(["python3", "-m", "py_compile", str(f)])
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            die(f"syntax error in {f.name}")
    ok(f"syntax valid in {len(py_files)} files")


# ─────────────────────────────────────────────
# GATE 6: test suite
# ─────────────────────────────────────────────


def gate_tests(repo_root: Path) -> None:
    step("6", "Test suite")
    # Tests live in tests_dev/ at project root (one level above plugin repo root)
    # This is gitignored in the plugin repo but present in the development tree.
    test_candidates = [
        repo_root / "tests_dev" / "test_v2185_parse.py",
        repo_root.parent / "tests_dev" / "test_v2185_parse.py",
    ]
    test_file = next((p for p in test_candidates if p.exists()), None)
    if test_file is None:
        die(
            "test file not found in any known location",
            hint=(
                "expected at one of: "
                + "; ".join(str(p) for p in test_candidates)
                + ". Tests are mandatory — do not delete them."
            ),
        )
    print(f"  running {test_file}")
    # Use uv run with tiktoken so tokenization matches production
    r = run_probe(["uv", "run", "--with", "tiktoken", "python3", str(test_file)])
    # Stream test output (it has the unicode table)
    if r.stdout:
        # Show the summary line at minimum
        for line in r.stdout.splitlines()[-5:]:
            print(f"  {line}")
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        die("test suite has failures", hint="all tests must pass before publishing")
    # Parse "N passed, M failed / T total"
    match_tests = re.search(r"(\d+)\s*passed.*?(\d+)\s*failed", r.stdout, re.DOTALL)
    if match_tests is None:
        die(
            "cannot parse test summary",
            hint="test output format changed — update publish.py",
        )
    passed = int(match_tests.group(1))
    failed = int(match_tests.group(2))
    if failed > 0:
        die(f"{failed} tests failed (only {passed} passed)")
    if passed == 0:
        die("0 tests ran — test file may be empty or broken")
    ok(f"all {passed} tests passed")


# ─────────────────────────────────────────────
# GATE 7: CPV validation (strict)
# ─────────────────────────────────────────────

CPV_REPO = "git+https://github.com/Emasoft/claude-plugins-validation"


def _issue_path(line: str) -> str:
    """Extract the trailing (path[:line]) marker from a CPV issue line.

    CPV issue lines look like:
        [CRITICAL] message here (path/to/file.py):42
        [WARNING] message here (~/.claude/settings.local.json)
    Returns the raw path string, or "" if no path marker is found.
    """
    # Last parenthesised group on the line is the path marker.
    m = re.search(r"\(([^()]+)\)(?::\d+)?\s*$", line)
    return m.group(1).strip() if m else ""


def _path_inside(path_str: str, repo_root: Path) -> bool:
    """True if path_str refers to a file inside repo_root.

    CPV uses relative paths for plugin files and ~-prefixed or /-prefixed
    absolute paths for global issues. Only files inside the plugin count
    against the release gate.
    """
    if not path_str:
        # No path at all — structural issue, counts as inside the plugin.
        return True
    # Absolute or home-relative paths are outside the plugin tree.
    if path_str.startswith(("~", "/")):
        try:
            p = Path(path_str).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        try:
            p.relative_to(repo_root.resolve())
            return True
        except ValueError:
            return False
    # Relative path: assume inside plugin (CPV reports them from repo_root).
    return True


def gate_cpv(repo_root: Path, *, label: str = "CPV validation") -> None:
    step("7", label)
    # cpv-remote-validate is the environment isolation launcher that wraps
    # the individual validators to prevent the target's local config files
    # (ruff.toml, pyproject.toml, etc.) from interfering with the validator's
    # own tools. This is the REQUIRED entry point when running CPV via uvx.
    r = run_probe(
        [
            "uvx",
            "--from",
            CPV_REPO,
            "--with",
            "pyyaml",
            "cpv-remote-validate",
            "plugin",
            str(repo_root),
        ]
    )
    output = (r.stdout or "") + "\n" + (r.stderr or "")
    # Show last 15 lines of output for context
    for line in output.strip().splitlines()[-15:]:
        print(f"  {line}")
    if r.returncode != 0:
        die(f"cpv-validate exited with code {r.returncode}")

    # Count issues that refer to files inside the plugin.
    # Issues about global config files (e.g. ~/.claude/settings.local.json)
    # are reported but do not block the release — they are environment
    # problems, not plugin defects.
    counts = {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "NIT": 0, "WARNING": 0}
    ignored = 0
    issue_re = re.compile(r"^\s*\[(CRITICAL|MAJOR|MINOR|NIT|WARNING)\]")
    for line in output.splitlines():
        m = issue_re.match(line)
        if not m:
            continue
        severity = m.group(1)
        path_str = _issue_path(line)
        if _path_inside(path_str, repo_root):
            counts[severity] += 1
        else:
            ignored += 1

    total = sum(counts.values())
    if ignored:
        print(f"  (ignored {ignored} issue(s) about files outside the plugin)")
    if total > 0:
        breakdown = " ".join(f"{k.lower()}={v}" for k, v in counts.items())
        die(
            f"CPV found {total} plugin issues ({breakdown})",
            hint="fix every single plugin issue before publishing — NO EXCEPTIONS",
        )
    ok("CPV: 0 plugin issues across all severity levels")


# ─────────────────────────────────────────────
# Version management
# ─────────────────────────────────────────────


def read_plugin_version(plugin_json: Path) -> str:
    if not plugin_json.exists():
        die(f"{plugin_json} not found — is this a claude-plugin repo?")
    return json.loads(plugin_json.read_text(encoding="utf-8")).get("version", "0.0.0")


def write_plugin_version(plugin_json: Path, new_version: str) -> None:
    manifest = json.loads(plugin_json.read_text(encoding="utf-8"))
    manifest["version"] = new_version
    plugin_json.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_pyproject_version(pyproject: Path, new_version: str) -> None:
    if not pyproject.exists():
        die(f"{pyproject} not found — pyproject.toml is required")
    content = pyproject.read_text(encoding="utf-8")
    new_content, n = re.subn(
        r'^version\s*=\s*"[^"]*"',
        f'version = "{new_version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        die(
            f"could not find 'version = ...' line in {pyproject}",
            hint="pyproject.toml must have a top-level [project] version field",
        )
    pyproject.write_text(new_content, encoding="utf-8")


def verify_versions_match(plugin_json: Path, pyproject: Path, expected: str) -> None:
    """Verify plugin.json and pyproject.toml both contain the expected version."""
    pj_version = json.loads(plugin_json.read_text(encoding="utf-8")).get("version")
    if pj_version != expected:
        die(f"plugin.json version {pj_version!r} != expected {expected!r}")
    py_content = pyproject.read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', py_content, re.MULTILINE)
    if not m or m.group(1) != expected:
        actual = m.group(1) if m else "<not found>"
        die(f"pyproject.toml version {actual!r} != expected {expected!r}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a new release (all quality gates mandatory)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--patch",
        action="store_true",
        help="Force patch bump (override git-cliff auto)",
    )
    group.add_argument(
        "--minor",
        action="store_true",
        help="Force minor bump (override git-cliff auto)",
    )
    group.add_argument(
        "--major",
        action="store_true",
        help="Force major bump (override git-cliff auto)",
    )
    group.add_argument(
        "--set",
        type=str,
        metavar="VERSION",
        help="Set explicit version (x.y.z)",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Run ALL checks but skip commit/tag/push/release",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    scripts_dir = repo_root / "scripts"
    plugin_json = repo_root / ".claude-plugin" / "plugin.json"
    pyproject = repo_root / "pyproject.toml"
    changelog = repo_root / "CHANGELOG.md"

    print("\n" + "=" * 64)
    print("  token-reporter release pipeline")
    print("  ALL QUALITY GATES ARE MANDATORY — NO SKIPS, NO EXCEPTIONS")
    print("=" * 64)

    # ── MANDATORY QUALITY GATES ──
    gate_tools()
    gate_pre_push_hook(repo_root)
    gate_clean_tree()
    gate_lint(scripts_dir)
    gate_type_check(scripts_dir)
    gate_syntax(scripts_dir)
    gate_tests(repo_root)
    gate_cpv(repo_root, label="CPV validation (pre-bump)")

    # All pre-bump gates passed. Compute new version.
    step("8", "Version bump")
    current = read_plugin_version(plugin_json)
    new_version = compute_next_version(current, args)

    if new_version == current:
        die(
            f"version unchanged ({current}) — nothing to publish",
            hint=(
                "no releasable changes detected by git-cliff. "
                "Either nothing committed since last tag, or force with "
                "--patch/--minor/--major/--set"
            ),
        )

    tag = f"v{new_version}"
    tag_check = run_probe(["git", "tag", "--list", tag])
    if tag_check.stdout.strip() == tag:
        die(f"tag {tag} already exists")

    print(f"  {current} -> {new_version} (tag: {tag})")

    if args.dry_run:
        print(f"\n[DRY RUN] All quality gates passed. Would release {tag}.")
        return

    # Update plugin.json AND pyproject.toml atomically
    write_plugin_version(plugin_json, new_version)
    write_pyproject_version(pyproject, new_version)
    verify_versions_match(plugin_json, pyproject, new_version)
    ok(f"plugin.json and pyproject.toml updated to {new_version}")

    # ── GATE 9: re-validate after version bump ──
    gate_cpv(repo_root, label="CPV validation (post-bump)")

    # ── GATE 10: changelog (mandatory) ──
    # Use git-cliff to regenerate CHANGELOG.md with the new release section.
    # The --tag flag stamps unreleased commits with the target version, and
    # our cliff.toml template renders a ``## [version] - date`` header plus
    # grouped entries (Features, Bug Fixes, etc.) for each release.
    step("10", "Changelog regeneration (git-cliff)")
    run_strict(["git-cliff", "--tag", tag, "--output", str(changelog)])
    if not changelog.exists():
        die("git-cliff did not produce CHANGELOG.md")
    # Verify the new section is actually present in the output
    new_section_header = f"## [{new_version}]"
    if new_section_header not in changelog.read_text(encoding="utf-8"):
        die(
            f"CHANGELOG.md does not contain expected header {new_section_header!r}",
            hint="check cliff.toml template — version header may be missing",
        )
    ok(f"CHANGELOG.md regenerated with {new_section_header}")

    # ── STEP 11: commit ──
    # Use conventional-commit format so the cliff.toml parser SKIPS this
    # commit in future changelogs (see {message = "^chore\\(release\\)",
    # skip = true}). Otherwise every release would pollute the next one.
    step("11", "Commit")
    commit_msg = f"chore(release): {tag}"
    run_strict(["git", "add", str(plugin_json), str(pyproject), str(changelog)])
    run_strict(["git", "commit", "-m", commit_msg])
    # Verify the commit exists (HEAD message matches)
    head_msg = run_probe(["git", "log", "-1", "--pretty=%s"]).stdout.strip()
    if head_msg != commit_msg:
        die(f"commit verification failed: HEAD message is {head_msg!r}")
    ok("commit created and verified")

    # ── STEP 12: tag ──
    step("12", "Annotated tag")
    run_strict(["git", "tag", "-a", tag, "-m", f"Release {tag}"])
    tag_verify = run_probe(["git", "tag", "--list", tag])
    if tag_verify.stdout.strip() != tag:
        die(f"tag {tag} was not created")
    ok(f"tag {tag} created and verified")

    # ── STEP 13: push ──
    step("13", "Push (pre-push hook runs as another gate)")
    # Never use --no-verify. The pre-push hook is an additional enforcement layer.
    run_strict(["git", "push", "--follow-tags"])
    # Verify the remote tag exists
    ls_remote = run_probe(["git", "ls-remote", "--tags", "origin", tag])
    if tag not in ls_remote.stdout:
        die(f"tag {tag} was not pushed to origin")
    ok(f"commit and tag {tag} pushed to origin")

    # ── STEP 14: GitHub release ──
    step("14", "GitHub release")
    notes = extract_release_notes(changelog, new_version)
    run_strict(
        ["gh", "release", "create", tag, "--title", tag, "--notes", notes],
    )
    # Verify the release exists
    gh_view = run_probe(["gh", "release", "view", tag])
    if gh_view.returncode != 0:
        die(f"GitHub release {tag} was not created")
    ok(f"GitHub release {tag} created and verified")

    print(f"\n\033[92m✓ Published {tag}\033[0m")


if __name__ == "__main__":
    main()
