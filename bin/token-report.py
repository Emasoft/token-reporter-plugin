#!/usr/bin/env python3
"""token-report — on-demand Claude Code session token report.

v2.1.91+ plugins can ship executables in bin/ which are added to the
Bash tool's PATH while the plugin is enabled. Invoke from any Bash tool
call in Claude Code to print a snapshot of the current session's token
usage, cost, tool attribution, cache dynamics, and file activity.

Cross-platform: runs on macOS, Linux, and Windows as long as `uv` is
available on PATH. The implementation is a thin wrapper that execs the
main token-reporter.py script with the --on-demand flag.

Usage:
  token-report.py           # print report for current session
  token-report.py --help    # show help

Environment:
  CLAUDE_PLUGIN_ROOT   path to the token-reporter plugin install dir
  CLAUDE_PROJECT_DIR   project root (used to locate the session transcript)
  CLAUDE_SESSION_ID    session id (if not set, uses newest .jsonl in project)
  CLAUDE_CONFIG_DIR    override for ~/.claude

Exit codes:
  0   report printed
  1   no transcript found or main script missing
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


HELP = """\
token-report.py — on-demand Claude Code session token report

Prints a snapshot of the current session's token usage, cost, tool
attribution, cache dynamics, and file activity to stdout. Works even
when Claude Code is not running with --debug.

This is a thin wrapper around scripts/token-reporter.py --on-demand.

Environment:
  CLAUDE_PLUGIN_ROOT   path to the token-reporter plugin install dir
  CLAUDE_PROJECT_DIR   project root (used to locate the session transcript)
  CLAUDE_SESSION_ID    session id (if not set, uses newest .jsonl in project)
  CLAUDE_CONFIG_DIR    override for ~/.claude

Exit codes:
  0  report printed
  1  no transcript found or session has no messages
"""


def _resolve_plugin_root() -> Path:
    """Resolve the plugin install dir from env or relative to this file."""
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        return Path(env_root)
    # Fall back to ../  (this file lives in <plugin_root>/bin/)
    return Path(__file__).resolve().parent.parent


def main() -> int:
    if any(arg in ("--help", "-h") for arg in sys.argv[1:]):
        print(HELP)
        return 0

    plugin_root = _resolve_plugin_root()
    script = plugin_root / "scripts" / "token-reporter.py"
    if not script.is_file():
        print(
            f"[token-report] cannot find token-reporter.py at {script}",
            file=sys.stderr,
        )
        return 1

    # Forward all user args. Only prepend --on-demand if the user didn't
    # already pass it — otherwise the flag would appear twice.
    user_args = list(sys.argv[1:])
    if "--on-demand" not in user_args:
        user_args = ["--on-demand", *user_args]

    cmd = [
        "uv",
        "run",
        "--with",
        "tiktoken",
        sys.executable,
        str(script),
        *user_args,
    ]

    # subprocess.run is cross-platform (os.execvp is POSIX-only and would
    # raise AttributeError on Windows). We forward the child's return code
    # as our own exit code so shell callers see the real result.
    try:
        result = subprocess.run(cmd)
    except OSError:
        print(
            "[token-report] `uv` is not on PATH. Install from https://astral.sh/uv",
            file=sys.stderr,
        )
        return 1
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
