#!/usr/bin/env bash
# token-report — on-demand Claude Code session report
#
# v2.1.91+ plugins can ship executables in bin/ which are added to the
# Bash tool's PATH while the plugin is enabled. Invoke from any Bash
# tool call in Claude Code to print the current session's token usage
# snapshot without waiting for the Stop hook.
#
# Usage:
#   token-report           # print report for current session
#   token-report --help    # show help

set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<'EOF'
token-report — on-demand Claude Code session token report

Prints a snapshot of the current session's token usage, cost, tool
attribution, cache dynamics, and file activity to stdout. Works even
when Claude Code is not running with --debug.

Environment:
  CLAUDE_PLUGIN_ROOT   path to the token-reporter plugin install dir
  CLAUDE_PROJECT_DIR   project root (used to locate the session transcript)
  CLAUDE_SESSION_ID    session id (if not set, uses newest .jsonl in project)
  CLAUDE_CONFIG_DIR    override for ~/.claude

Exit codes:
  0  report printed
  1  no transcript found or session has no messages

Examples:
  # Ask Claude to run it for you
  Please run: token-report

  # Run from a plain shell
  cd /path/to/project && token-report
EOF
  exit 0
fi

# Locate the plugin root. CLAUDE_PLUGIN_ROOT is set by Claude Code; fall
# back to walking two levels up from this script location.
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SCRIPT="${PLUGIN_ROOT}/scripts/token-reporter.py"

if [[ ! -f "$SCRIPT" ]]; then
  echo "[token-report] cannot find token-reporter.py at $SCRIPT" >&2
  exit 1
fi

exec uv run --with tiktoken python3 "$SCRIPT" --on-demand "$@"
