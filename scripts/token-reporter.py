#!/usr/bin/env python3
"""
Claude Code Hook: Per-Agent Token Usage Reporter (v3 — Markdown Table)
======================================================================
Reports detailed token usage AND agent identity as a clean markdown table
for individual agents/subagents when they complete.

Install:
  mkdir -p ~/.claude/hooks
  cp token-reporter.py ~/.claude/hooks/token-reporter.py
  chmod +x ~/.claude/hooks/token-reporter.py

Configure in .claude/settings.json (merge with existing hooks):
{
  "hooks": {
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/token-reporter.py"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/hooks/token-reporter.py"
          }
        ]
      }
    ]
  }
}
"""

from __future__ import annotations

import json
import sys
import re
import os
import time
import tempfile
import subprocess
from pathlib import Path
from collections import Counter, defaultdict
from typing import Any


# ─────────────────────────────────────────────
# Environment helpers (v2.1.90+ env vars)
# ─────────────────────────────────────────────


def _claude_config_dir() -> Path:
    """Return the Claude Code config dir, honoring CLAUDE_CONFIG_DIR."""
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    if d:
        return Path(d)
    return Path.home() / ".claude"


def _plugin_data_dir() -> Path:
    """Return the persistent plugin data dir (survives plugin updates).

    Priority:
      1. ${CLAUDE_PLUGIN_DATA} (v2.1.90+)
      2. ${CLAUDE_CONFIG_DIR}/plugins/data/token-reporter
      3. ~/.claude/plugins/data/token-reporter
    """
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    return _claude_config_dir() / "plugins" / "data" / "token-reporter"


# Canonical field -> list of alternate names encountered in Claude Code over time.
# Primary (first) name is what we use internally.
_USAGE_FIELD_ALIASES = {
    "input_tokens": ["input_tokens", "inputTokens", "input"],
    "output_tokens": ["output_tokens", "outputTokens", "output"],
    "cache_creation_input_tokens": [
        "cache_creation_input_tokens",
        "cache_creation_tokens",
        "cacheCreation",
        "cacheCreationInputTokens",
    ],
    "cache_read_input_tokens": [
        "cache_read_input_tokens",
        "cache_read_tokens",
        "cacheRead",
        "cacheReadInputTokens",
    ],
}


def _get_usage_field(u: dict, canonical: str) -> int:
    """Read a usage field accepting OTel event, OTel metric, and legacy spellings.

    v2.1.101 monitoring-usage docs confirm both spellings are canonical:
      - OTel event: cache_read_tokens, input_tokens, ...
      - OTel metric attribute: cacheRead, input, ...
    """
    for alias in _USAGE_FIELD_ALIASES.get(canonical, [canonical]):
        v = u.get(alias)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return 0


def _record_hook_event(hook_event: str, hook_input: dict) -> None:
    """Record a lightweight-only hook event to the plugin data dir.

    Used for hooks that don't produce a report directly (InstructionsLoaded,
    PostCompact, TaskCreated, CwdChanged, FileChanged) but whose data we want
    to surface in the next Stop/SubagentStop report.

    One JSONL file per session: {plugin_data}/events/{session_id[:16]}.jsonl
    """
    sid = hook_input.get("session_id", "") or "no-session"
    sid_short = sid[:16]
    events_dir = _plugin_data_dir() / "events"
    try:
        events_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    path = events_dir / f"{sid_short}.jsonl"
    record = {
        "hook_event": hook_event,
        "timestamp": time.time(),
        "session_id": sid,
    }
    # Copy whitelisted fields from hook_input. We avoid dumping the whole
    # payload because it can be huge (CwdChanged/FileChanged carry diffs).
    for k in (
        "cwd",
        "file_path",
        "memory_type",
        "load_reason",
        "globs",
        "trigger_file_path",
        "parent_file_path",
        "agent_id",
        "agent_type",
        "pre_tokens",
        "preTokens",
        "trigger",
        "compactMetadata",
    ):
        if k in hook_input:
            record[k] = hook_input[k]
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _read_session_events(session_id: str) -> list:
    """Read recorded lightweight hook events for a session."""
    if not session_id:
        return []
    path = _plugin_data_dir() / "events" / f"{session_id[:16]}.jsonl"
    if not path.exists():
        return []
    out = []
    try:
        # errors="replace" — mirror parse_jsonl; an invalid-UTF-8 event line
        # should not crash the reporter.
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _clear_session_events(session_id: str) -> None:
    """Remove the session event log after it has been reported."""
    if not session_id:
        return
    path = _plugin_data_dir() / "events" / f"{session_id[:16]}.jsonl"
    try:
        path.unlink()
    except OSError:
        pass


def _project_slug_from_cwd(cwd: str) -> str:
    """Convert a cwd path to Claude Code's project slug format.

    Claude Code stores transcripts under ~/.claude/projects/<slug>/ where
    the slug is the cwd with "/" replaced by "-". E.g.:
        /Users/foo/bar/baz  →  -Users-foo-bar-baz
    """
    if not cwd:
        return ""
    return cwd.replace("/", "-")


def _find_current_session_transcript(cwd: str) -> str:
    """Find the current session's transcript file (newest JSONL in project dir).

    Used by `--on-demand` invocations (e.g., bin/token-report) that run
    outside of a hook and therefore have no hook input JSON.
    """
    projects_dir = _claude_config_dir() / "projects"
    if not projects_dir.is_dir():
        return ""
    # Walk the cwd upward looking for a matching slug, since the current
    # directory may be a subdir of the session's project root.
    probe = Path(cwd).resolve() if cwd else Path.cwd()
    candidates = []
    while True:
        slug = _project_slug_from_cwd(str(probe))
        pdir = projects_dir / slug
        if pdir.is_dir():
            for f in pdir.glob("*.jsonl"):
                if f.is_file():
                    try:
                        candidates.append((f.stat().st_mtime, str(f)))
                    except OSError:
                        continue
        if probe.parent == probe:
            break
        probe = probe.parent
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


# Regex for inline skill shell execution: !`command` and ```!...``` blocks
# (v2.1.91+). These run as preprocessing — they don't appear in the transcript
# as Bash tool calls, so the plugin's normal tool-attribution misses them.
_INLINE_SHELL_TICK = re.compile(r"!`([^`]+)`")
_INLINE_SHELL_FENCE = re.compile(r"```!\s*\n(.*?)\n```", re.DOTALL)


def _count_inline_shell_tokens(text: str) -> int:
    """Count tokens in inline !`...` shell blocks inside skill/user content.

    Note: this detects the SHELL COMMAND itself, not its output. The output
    is injected into the skill prompt by Claude Code and counted as part of
    the regular usage dict input_tokens.
    """
    if not text or "`" not in text:
        return 0
    total = 0
    for m in _INLINE_SHELL_TICK.finditer(text):
        total += count_tokens(m.group(1))
    for m in _INLINE_SHELL_FENCE.finditer(text):
        total += count_tokens(m.group(1))
    return total


# ─────────────────────────────────────────────
# Debug mode detection — walk process tree
# ─────────────────────────────────────────────


def _is_debug_mode() -> bool:
    """Check if Claude Code is running with --debug by walking the process tree.
    Matches only the actual 'claude' binary (not paths containing '.claude')."""
    pid = os.getppid()
    while pid > 1:
        try:
            result = subprocess.run(
                ["ps", "-o", "args=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            cmdline = result.stdout.strip()
            args = cmdline.split()
            if args:
                # Check the binary is actually 'claude' (not just a path with .claude)
                cmd = os.path.basename(args[0])
                if cmd == "claude" and "--debug" in args:
                    return True
            # Walk up to this process's parent
            result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=2,
            )
            pid = int(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError, OSError):
            break
    return False


# ─────────────────────────────────────────────
# Fast tokenizer — lazy-loaded singleton
# ─────────────────────────────────────────────
_tokenizer = None  # type: ignore
_tokenizer_loaded = False


def count_tokens(text: str) -> int:
    """Count tokens using tiktoken cl100k_base (fast path: encode_ordinary).
    Falls back to len(text)//4 if tiktoken is not installed."""
    global _tokenizer, _tokenizer_loaded
    if not _tokenizer_loaded:
        _tokenizer_loaded = True
        try:
            import tiktoken

            _tokenizer = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            _tokenizer = None
            msg = (
                "[token-reporter] WARNING: tiktoken not found"
                " — token counts will be approximate."
                " Run via 'uv run --with tiktoken'"
                " to get exact counts."
            )
            print(msg, file=sys.stderr)
    if _tokenizer is not None:
        # encode_ordinary skips special token handling — ~30% faster than encode
        return len(_tokenizer.encode_ordinary(text))
    return len(text) // 4


# ─────────────────────────────────────────────
# Pricing
# ─────────────────────────────────────────────
MODEL_PRICING = {
    "claude-opus-4-6": {
        "input": 5.0,
        "output": 25.0,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    "claude-opus-4-5": {
        "input": 5.0,
        "output": 25.0,
        "cache_write": 6.25,
        "cache_read": 0.50,
    },
    "claude-sonnet-4-5": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-6": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 1.0,
        "output": 5.0,
        "cache_write": 1.25,
        "cache_read": 0.10,
    },
    "claude-sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    "claude-opus-4": {
        "input": 15.0,
        "output": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-1": {
        "input": 15.0,
        "output": 75.0,
        "cache_write": 18.75,
        "cache_read": 1.50,
    },
    "claude-haiku-3-5": {
        "input": 0.80,
        "output": 4.0,
        "cache_write": 1.00,
        "cache_read": 0.08,
    },
    "claude-haiku-3": {
        "input": 0.25,
        "output": 1.25,
        "cache_write": 0.30,
        "cache_read": 0.03,
    },
}
DEFAULT_PRICING = {
    "input": 3.0,
    "output": 15.0,
    "cache_write": 3.75,
    "cache_read": 0.30,
}


def get_pricing(model_name: str) -> dict:
    if not model_name:
        return DEFAULT_PRICING
    if model_name in MODEL_PRICING:
        return MODEL_PRICING[model_name]
    # Iterate keys sorted by LENGTH descending so the longest (most specific)
    # prefix wins. Otherwise "claude-sonnet-4" could match before
    # "claude-sonnet-4-5" for a model name like "claude-sonnet-4-5-beta"
    # since Python dict iteration order is insertion order.
    for key in sorted(MODEL_PRICING.keys(), key=len, reverse=True):
        if key in model_name or model_name.startswith(key):
            return MODEL_PRICING[key]
    ml = model_name.lower()
    if "opus" in ml and ("4-6" in ml or "4.6" in ml):
        return MODEL_PRICING["claude-opus-4-6"]
    if "opus" in ml and ("4-5" in ml or "4.5" in ml):
        return MODEL_PRICING["claude-opus-4-5"]
    if "opus" in ml:
        return MODEL_PRICING["claude-opus-4-1"]
    if "sonnet" in ml:
        return MODEL_PRICING["claude-sonnet-4-5"]
    if "haiku" in ml:
        return MODEL_PRICING["claude-haiku-4-5"]
    return DEFAULT_PRICING


def estimate_cost(usage: dict, model: str) -> float:
    p = get_pricing(model)
    return (
        (usage.get("input_tokens", 0) / 1e6) * p["input"]
        + (usage.get("output_tokens", 0) / 1e6) * p["output"]
        + (usage.get("cache_creation_input_tokens", 0) / 1e6) * p["cache_write"]
        + (usage.get("cache_read_input_tokens", 0) / 1e6) * p["cache_read"]
    )


def fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.1f}K"
    return str(n)


def shorten_model(model: str) -> str:
    if not model:
        return "unknown"
    s = re.sub(r"-\d{8}$", "", model)
    s = re.sub(r"^claude-", "", s)
    return s


def fmt_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m = int(seconds) // 60
        s = int(seconds) % 60
        return f"{m}m{s}s" if s else f"{m}m"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}h{m}m" if m else f"{h}h"


def trunc(text: str, max_len: int = 100) -> str:
    if not text:
        return "—"
    text = text.replace("\n", " ").replace("|", "\\|").strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def shorten_mcp_tool(name: str) -> str:
    """Shorten MCP tool names for display.

    mcp__plugin_serena_serena__find_symbol  -> serena:find_symbol
    mcp__plugin_grepika_grepika__search     -> grepika:search
    mcp__plugin_llm-externalizer_llm-externalizer__chat -> llm-ext:chat
    mcp__chrome-devtools__take_screenshot   -> chrome-dt:take_screenshot
    mcp__claude_ai_Gmail__gmail_search      -> Gmail:gmail_search
    """
    if not name.startswith("mcp__"):
        return name
    rest = name[5:]  # strip "mcp__"
    # Split on double underscore to get [server_parts, tool_name]
    parts = rest.split("__", 1)
    if len(parts) == 2:
        server_part, tool_name = parts
    else:
        return name  # can't parse, return as-is
    # Extract a short server label from the server_part
    # Patterns: "plugin_{name}_{name}" or "claude_ai_{Name}" or "{name}"
    if server_part.startswith("plugin_"):
        segments = server_part[7:].split("_", 1)  # strip "plugin_"
        server_label = segments[0]
    elif server_part.startswith("claude_ai_"):
        server_label = server_part[10:]  # strip "claude_ai_"
    else:
        server_label = server_part
    return f"{server_label}:{tool_name}"


# ─────────────────────────────────────────────
# JSONL helpers
# ─────────────────────────────────────────────


def discover_subagent_transcripts(transcript_path: str) -> list:
    """Find sub-agent transcripts spawned by the agent at transcript_path.

    Worktree agents and orchestrators store sub-agent transcripts at:
        {SESSION_ID}/subagents/agent-*.jsonl
    relative to the parent transcript {SESSION_ID}.jsonl.
    """
    tp = Path(transcript_path)
    subagents_dir = tp.parent / tp.stem / "subagents"
    if not subagents_dir.is_dir():
        return []
    results = []
    for f in sorted(subagents_dir.glob("agent-*.jsonl")):
        meta_path = f.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        agent_id = f.stem[6:] if f.stem.startswith("agent-") else f.stem
        results.append(
            {
                "path": str(f),
                "agent_id": agent_id,
                "agent_type": meta.get("agentType", ""),
                "description": meta.get("description", ""),
            }
        )
    return results


def _merge_usage(base: dict, add: dict):
    """Merge token usage from add into base (in place)."""
    for f in [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "message_count",
    ]:
        base[f] = base.get(f, 0) + add.get(f, 0)
    # Merge models_used
    for model, stats in add.get("models_used", {}).items():
        if model not in base.get("models_used", {}):
            base.setdefault("models_used", {})[model] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "message_count": 0,
            }
        for f in [
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "message_count",
        ]:
            base["models_used"][model][f] += stats.get(f, 0)
    # Merge tools
    for tool, count in add.get("tools_used", {}).items():
        base.setdefault("tools_used", Counter())[tool] += count
    base_tt = base.setdefault(
        "tools_tokens",
        defaultdict(lambda: {"input": 0, "output": 0, "result_tokens": 0}),
    )
    for tool, tok in add.get("tools_tokens", {}).items():
        if tool not in base_tt:
            base_tt[tool] = {"input": 0, "output": 0, "result_tokens": 0}
        for f in ["input", "output", "result_tokens"]:
            base_tt[tool][f] += tok.get(f, 0)
    # Merge skills (v2.1.108+ built-in commands route through Skill tool)
    for skill, count in add.get("skills_used", {}).items():
        base.setdefault("skills_used", Counter())[skill] += count
    base_sk = base.setdefault(
        "skills_tokens",
        defaultdict(
            lambda: {"invocation_count": 0, "result_tokens": 0, "output_tokens": 0}
        ),
    )
    for skill, tok in add.get("skills_tokens", {}).items():
        if skill not in base_sk:
            base_sk[skill] = {
                "invocation_count": 0,
                "result_tokens": 0,
                "output_tokens": 0,
            }
        for f in ["invocation_count", "result_tokens", "output_tokens"]:
            base_sk[skill][f] += tok.get(f, 0)
    # Merge file sets
    for f in ["files_read", "files_written", "files_edited"]:
        existing = base.get(f, [])
        if isinstance(existing, set):
            existing.update(add.get(f, []))
        else:
            combined = set(existing) | set(add.get(f, []))
            base[f] = sorted(combined)
    # Merge lists
    base.setdefault("bash_commands", []).extend(add.get("bash_commands", []))
    base.setdefault("web_fetches", []).extend(add.get("web_fetches", []))
    # Timestamps: widen the range
    t0_add = add.get("first_timestamp", "")
    t1_add = add.get("last_timestamp", "")
    if t0_add and (not base.get("first_timestamp") or t0_add < base["first_timestamp"]):
        base["first_timestamp"] = t0_add
    if t1_add and (not base.get("last_timestamp") or t1_add > base["last_timestamp"]):
        base["last_timestamp"] = t1_add
    # Merge cache events
    base.setdefault("cache_events", []).extend(add.get("cache_events", []))
    # Merge compact events (v2.1.90+)
    base.setdefault("compact_events", []).extend(add.get("compact_events", []))
    # Accumulate spilled tool output bytes (v2.1.90 tool-results/)
    base["spilled_tool_bytes"] = base.get("spilled_tool_bytes", 0) + add.get(
        "spilled_tool_bytes", 0
    )
    # Accumulate skill preprocessing tokens (v2.1.91+ inline !`...`)
    base["skill_preprocessing_tokens"] = base.get(
        "skill_preprocessing_tokens", 0
    ) + add.get("skill_preprocessing_tokens", 0)


def parse_jsonl(filepath: str):
    try:
        # errors="replace" — invalid UTF-8 in transcripts (e.g. bash output
        # from `find`, binary spill, corrupted chunks) would otherwise raise
        # UnicodeDecodeError and crash the hook mid-parse.
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
    except (OSError, IOError):
        return


# ─────────────────────────────────────────────
# Identity extraction from parent transcript
# ─────────────────────────────────────────────


def _build_tool_agent_map(entries: list) -> dict:
    """Build a map of tool_use_id -> agentId from toolUseResult fields.

    Scans user entries for toolUseResult.agentId which provides a direct,
    unambiguous link between a tool_use_id and the agent it spawned.
    Works for both async (background) and sync (foreground) agents,
    including swarms where many agents launch in the same assistant turn.
    """
    # tool_use_id -> agentId
    tuid_to_agent = {}  # type: dict[str, str]
    for entry in entries:
        if entry.get("type") != "user":
            continue
        # Primary source: toolUseResult.agentId (v2.1.76+)
        tur = entry.get("toolUseResult")
        if isinstance(tur, dict) and tur.get("agentId"):
            # Link via the tool_result content block's tool_use_id
            msg = entry.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tuid = block.get("tool_use_id", "")
                        if tuid:
                            tuid_to_agent[tuid] = tur["agentId"]
        # Fallback: parse "agentId: xxx" from tool_result text (v2.1.84 style)
        else:
            msg = entry.get("message", {})
            content = msg.get("content", []) if isinstance(msg, dict) else []
            if isinstance(content, list):
                for block in content:
                    if (
                        not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    tuid = block.get("tool_use_id", "")
                    rc = block.get("content", "")
                    # Extract from text content
                    text = ""
                    if isinstance(rc, str):
                        text = rc
                    elif isinstance(rc, list):
                        text = " ".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in rc
                        )
                    m = re.search(r"agentId:\s*(\S+)", text)
                    if m and tuid:
                        tuid_to_agent[tuid] = m.group(1)
    return tuid_to_agent


def extract_agent_identity(transcript_path: str, agent_id: str) -> dict:
    identity = {
        "task_description": "",
        "task_prompt": "",
        "subagent_type": "",
        "requested_model": "",
        "spawning_skill": "",
        "run_in_background": False,
    }
    if not transcript_path:
        return identity

    entries = list(parse_jsonl(transcript_path))

    # Build tool_use_id -> agentId map from toolUseResult fields.
    # This is the primary matching mechanism — works for swarms, background
    # agents, and nested agents without relying on agentId in JSONL entries.
    tuid_to_agent = _build_tool_agent_map(entries)

    last_user_ctx = ""

    for i, entry in enumerate(entries):
        etype = entry.get("type", "")

        if etype == "user":
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                c = msg.get("content", "")
                if isinstance(c, str):
                    last_user_ctx = c
                elif isinstance(c, list):
                    # Skip tool_result entries — they aren't user prompts
                    has_text = False
                    parts = []
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            parts.append(b.get("text", ""))
                            has_text = True
                    if has_text:
                        last_user_ctx = " ".join(parts)

        if etype != "assistant":
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use" or block.get("name") not in (
                "Task",
                "Agent",
            ):
                continue

            ti = block.get("input", {})
            if not isinstance(ti, dict):
                continue

            tool_use_id = block.get("id", "")

            # Match via toolUseResult.agentId map (primary, works for swarms)
            mapped_agent = tuid_to_agent.get(tool_use_id, "")
            if agent_id and mapped_agent == agent_id:
                _fill_identity(identity, ti, last_user_ctx)
                return identity

            # Fallback: content string search (v2.1.84 compatibility)
            if agent_id and _match_task_legacy(entries, i, tool_use_id, agent_id):
                _fill_identity(identity, ti, last_user_ctx)
                return identity

    # No positive match. Fallback: if only one Task/Agent exists, use it.
    # If multiple, we can't disambiguate — return empty identity.
    task_inputs = []
    task_contexts = []
    for i, entry in enumerate(entries):
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content", []):
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("name") in ("Task", "Agent")
            ):
                ti = block.get("input", {})
                if isinstance(ti, dict):
                    task_inputs.append(ti)
                    task_contexts.append(last_user_ctx)

    if len(task_inputs) == 1:
        _fill_identity(identity, task_inputs[0], task_contexts[0])

    return identity


def _fill_identity(identity: dict, ti: dict, user_ctx: str):
    """Populate identity dict from a Task/Agent tool input."""
    identity["task_description"] = ti.get("description", "")
    identity["task_prompt"] = ti.get("prompt", "")
    identity["subagent_type"] = ti.get("subagent_type", "")
    identity["requested_model"] = ti.get("model", "")
    identity["run_in_background"] = ti.get("run_in_background", False)
    identity["spawning_skill"] = _detect_skill(user_ctx, ti.get("prompt", ""))


def _match_task_legacy(entries, start, tool_use_id, agent_id):
    """Legacy matching: search for agent_id string in tool_result content.

    Used as fallback for transcripts that predate toolUseResult.agentId.
    """
    if not tool_use_id or not agent_id:
        return False
    for j in range(start + 1, min(start + 50, len(entries))):
        e = entries[j]
        for src in [e, e.get("message", {})]:
            c = src.get("content", [])
            if isinstance(c, str) and agent_id in c:
                return True
            if not isinstance(c, list):
                continue
            for rb in c:
                if not isinstance(rb, dict):
                    continue
                if rb.get("tool_use_id") == tool_use_id:
                    if agent_id in str(rb.get("content", "")):
                        return True
                if rb.get("type") == "text" and agent_id in str(rb.get("text", "")):
                    return True
    return False


def _detect_skill(user_ctx: str, prompt: str) -> str:
    combined = (user_ctx + " " + prompt).lower()
    skip = {
        "compact",
        "clear",
        "hooks",
        "model",
        "cost",
        "context",
        "resume",
        "help",
        "config",
        "plan",
        "build",
        "review",
        "status",
        "memory",
        "path",
        "tmp",
        "home",
        "usr",
        "etc",
        "var",
        "bin",
        "dev",
        "mnt",
    }

    for pat in re.findall(r"/([a-z][a-z0-9_-]+)", combined):
        if pat not in skip and len(pat) > 2:
            return f"/{pat}"

    m = re.search(r"(?:using skill|skill[:\s]+)([a-z][a-z0-9_-]+)", combined)
    if m:
        return f"skill:{m.group(1)}"

    m = re.search(r"\.claude/agents/([a-z][a-z0-9_-]+)\.md", combined)
    if m:
        return f"agent:{m.group(1)}"

    m = re.search(r"\.claude/skills/([a-z][a-z0-9_-]+)", combined)
    if m:
        return f"skill:{m.group(1)}"

    return ""


# ─────────────────────────────────────────────
# Agent transcript parsing
# ─────────────────────────────────────────────


def parse_agent_transcript(
    path: str, session_id: str, last_op_only: bool = False
) -> dict:
    models_used: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "message_count": 0,
        }
    )
    tools_tokens: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {"input": 0, "output": 0, "result_tokens": 0}
    )
    # v2.1.108: built-in slash commands are now routed through the Skill tool.
    # Every `tool_use` block with name=="Skill" has `input.skill` = the skill
    # identifier (e.g. "commit", "code-auditor-agent:caa-pr-review-skill",
    # or a built-in like "init"/"review"/"security-review"). We track these
    # separately so the report can show which skills cost what.
    skills_tokens: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {
            "invocation_count": 0,
            "result_tokens": 0,  # bytes of skill content loaded into context
            "output_tokens": 0,  # bytes the model spent issuing the Skill call
        }
    )
    r: dict[str, Any] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "message_count": 0,
        "models_used": models_used,
        "tools_used": Counter(),
        "tools_tokens": tools_tokens,
        "skills_used": Counter(),  # skill_name -> invocation count
        "skills_tokens": skills_tokens,  # skill_name -> per-skill totals
        "files_read": set(),
        "files_written": set(),
        "files_edited": set(),
        "bash_commands": [],
        "web_fetches": [],
        "first_timestamp": "",
        "last_timestamp": "",
        "cache_events": [],  # detected invalidation/TTL expiry events
        "compact_events": [],  # v2.1.90+ compact_boundary markers
        "spilled_tool_bytes": 0,  # v2.1.90 tool-results/ spillover files
        "skill_preprocessing_tokens": 0,  # v2.1.91+ !`...` inline shell
        "entries_with_usage": 0,  # total JSONL rows that carried a usage block
    }
    seen = set()
    # Stream-chunk tracking: Claude Code writes one JSONL entry per streaming
    # delta. Entries with the same message id share input/cw/cr (frozen after
    # the first chunk) but have growing output_tokens. The `seen` set guards
    # against double-counting input/cw/cr. `mid_output_seen` lets us keep the
    # FINAL (highest) output_tokens value from the last streaming chunk,
    # instead of the stub value from the first chunk. Without this fix,
    # output tokens are under-counted by ~20-25% (verified empirically on
    # 2026-04-15 against the llm-externalizer session: first-wins gave
    # 1,169,734 output, last-wins gave 1,456,634 — a $7.17 gap at Opus).
    mid_output_seen: dict[str, int] = {}
    entries_with_usage = 0  # total JSONL rows that carried a usage block
    # State for cache invalidation detection
    _prev_ts = ""  # timestamp of previous assistant message
    _prev_cr = 0  # previous cache_read
    # tool names that modify files since last assistant msg
    _recent_writes: list[str] = []

    # Load all entries so we can optionally filter to last operation
    all_entries = list(parse_jsonl(path))

    # When last_op_only is True, find the last real user prompt and only process
    # entries after it. We must skip tool_result entries which also have type=user
    # but are not actual user prompts — they're responses from tool calls.
    start_index = 0
    if last_op_only:
        last_user_idx = -1
        for idx, entry in enumerate(all_entries):
            if entry.get("type") != "user":
                continue
            # tool_result entries have type=user but contain tool_result content blocks
            msg = entry.get("message", {})
            content = msg.get("content", [])
            is_tool_result = False
            if isinstance(content, list):
                is_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
            elif isinstance(content, str) and content == "":
                is_tool_result = False
            if not is_tool_result:
                last_user_idx = idx
        if last_user_idx >= 0:
            start_index = last_user_idx + 1

    # Map tool_use_id -> tool_name so we can attribute tool_result costs
    tool_use_id_map = {}  # type: dict[str, str]
    # Map tool_use_id -> skill_name for Skill tool invocations. This lets us
    # credit a Skill call's tool_result (the skill content loaded into
    # context) to the specific skill name rather than to the generic "Skill"
    # tool bucket.
    skill_use_id_map = {}  # type: dict[str, str]

    # v2.1.90: large tool outputs are spilled to
    #   {transcript_parent}/{session_id}/tool-results/*.json
    # If present, compute the aggregate spill size so we can credit the
    # corresponding tool's result_tokens at the end of the parse.
    transcript_parent = Path(path).parent
    transcript_stem = Path(path).stem
    spill_dir = transcript_parent / transcript_stem / "tool-results"
    spill_files: dict[str, int] = {}  # tool_use_id prefix -> size bytes
    if spill_dir.is_dir():
        try:
            for sf in spill_dir.iterdir():
                if sf.is_file():
                    # Files are typically named "<tool_use_id>.<ext>"
                    key = sf.stem
                    try:
                        spill_files[key] = sf.stat().st_size
                    except OSError:
                        continue
        except OSError:
            pass

    for entry in all_entries[start_index:]:
        etype = entry.get("type", "")

        # Track timestamps for duration calculation
        ts = entry.get("timestamp", "")
        if ts:
            if not r["first_timestamp"]:
                r["first_timestamp"] = ts
            r["last_timestamp"] = ts

        # ── v2.1.90+ compact_boundary system event ──
        # Ground-truth marker emitted by auto-compaction. Use the preTokens
        # value as a historical compaction record instead of inferring from
        # usage-dict drops. Subagent transcripts persist independently so
        # these can appear outside the Stop event window.
        if etype == "system" and entry.get("subtype") == "compact_boundary":
            cm = entry.get("compactMetadata", {})
            try:
                pre = int(cm.get("preTokens", 0) or 0)
            except (TypeError, ValueError):
                pre = 0
            r["compact_events"].append(
                {
                    "trigger": cm.get("trigger", ""),
                    "pre_tokens": pre,
                    "timestamp": ts,
                }
            )
            continue

        # ── Process user entries: scan tool_result blocks for content size ──
        if etype == "user":
            matched_tuids = set()  # track which tool_use_ids we already counted
            msg = entry.get("message", {})
            content = msg.get("content", [])
            # v2.1.91+ inline shell detection: scan string content for !`...`
            # and ```!...``` blocks. Skill content enters the conversation as
            # a user message; if the skill's source happens to surface here
            # verbatim, we credit the blocks to skill preprocessing bytes.
            if isinstance(content, str) and content:
                r["skill_preprocessing_tokens"] += _count_inline_shell_tokens(content)
            if isinstance(content, list):
                for block in content:
                    if (
                        not isinstance(block, dict)
                        or block.get("type") != "tool_result"
                    ):
                        continue
                    # Match this tool_result back to its originating tool
                    tuid = block.get("tool_use_id", "")
                    tool_name = tool_use_id_map.get(tuid, "")
                    if not tool_name:
                        continue
                    matched_tuids.add(tuid)
                    # Tokenize the tool result — this is what gets fed as input tokens
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        # Content can be list of blocks (text, image, etc.)
                        text = " ".join(
                            b.get("text", "") if isinstance(b, dict) else str(b)
                            for b in result_content
                        )
                    elif isinstance(result_content, str):
                        text = result_content
                    else:
                        text = str(result_content)
                    rt = count_tokens(text)
                    r["tools_tokens"][tool_name]["result_tokens"] += rt
                    # If this tool_result came from a Skill invocation, also
                    # credit the result_tokens (the skill content bytes loaded
                    # into context) to the specific skill so per-skill reports
                    # show the actual cost of loading each skill.
                    skill_name = skill_use_id_map.get(tuid, "")
                    if skill_name:
                        r["skills_tokens"][skill_name]["result_tokens"] += rt

            # v2.1.85+: toolUseResult at entry level carries tool result data.
            # Use sourceToolAssistantUUID to trace back to the assistant turn,
            # and tokenize toolUseResult content if not already counted above.
            tool_use_result = entry.get("toolUseResult")
            if isinstance(tool_use_result, dict):
                tuid = tool_use_result.get("tool_use_id", "")
                if tuid and tuid not in matched_tuids:
                    tool_name = tool_use_id_map.get(tuid, "")
                    if tool_name:
                        result_content = tool_use_result.get("content", "")
                        if isinstance(result_content, list):
                            text = " ".join(
                                b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in result_content
                            )
                        elif isinstance(result_content, str):
                            text = result_content
                        else:
                            text = str(result_content)
                        rt_fb = count_tokens(text)
                        r["tools_tokens"][tool_name]["result_tokens"] += rt_fb
                        # Mirror the skill crediting done in the inline
                        # tool_result path above, so Skill invocations that
                        # surface via the v2.1.85 fallback path still credit
                        # their result bytes to the specific skill.
                        skill_name_fb = skill_use_id_map.get(tuid, "")
                        if skill_name_fb:
                            r["skills_tokens"][skill_name_fb]["result_tokens"] += rt_fb
            continue

        # ── Process assistant entries ──
        if etype != "assistant" or "message" not in entry:
            continue
        if session_id:
            sid = entry.get("sessionId", entry.get("session_id", ""))
            if sid and sid != session_id:
                continue

        msg = entry["message"]
        mid = msg.get("id", "")
        u = msg.get("usage", {}) if isinstance(msg, dict) else {}
        if u:
            entries_with_usage += 1

        if mid and mid not in seen:
            seen.add(mid)
            if u:
                model = msg.get("model", "unknown")
                for f in [
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ]:
                    # Accept both OTel event form (snake_case with _tokens)
                    # and OTel metric form (camelCase) — see _USAGE_FIELD_ALIASES
                    v = _get_usage_field(u, f)
                    r[f] += v
                    r["models_used"][model][f] += v
                r["message_count"] += 1
                r["models_used"][model]["message_count"] += 1
                # Record the first-chunk output value so subsequent streaming
                # chunks can grow it to its final value.
                mid_output_seen[mid] = _get_usage_field(u, "output_tokens")

                # ── Cache invalidation detection ──
                # A spike in cache_creation with a drop in cache_read signals
                # that the cache was invalidated and the full context had to be
                # re-sent. Two main causes:
                #   1. TTL expiry (>5 min idle → "hey!" effect)
                #   2. File change (Edit/Write → loadChangedFiles() invalidates)
                cw = _get_usage_field(u, "cache_creation_input_tokens")
                cr = _get_usage_field(u, "cache_read_input_tokens")
                inp = _get_usage_field(u, "input_tokens")
                total_cache = cw + cr
                # Detect: cache_creation dominates (>80%) AND is substantial (>50K)
                is_spike = cw > 50_000 and total_cache > 0 and (cw / total_cache) > 0.80
                if is_spike:
                    from datetime import datetime

                    event = {
                        "cache_write_tokens": cw,
                        "cache_read_tokens": cr,
                        "input_tokens": inp,
                        "timestamp": ts,
                        "message_index": r["message_count"],
                        "preceding_tools": list(_recent_writes),
                        "cause": "unknown",
                        "idle_seconds": 0,
                    }
                    # Classify cause
                    idle_secs: float = 0
                    if _prev_ts and ts:
                        try:
                            dt_prev = datetime.fromisoformat(
                                _prev_ts.replace("Z", "+00:00")
                            )
                            dt_now = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            idle_secs = (dt_now - dt_prev).total_seconds()
                        except (ValueError, TypeError):
                            pass
                    event["idle_seconds"] = idle_secs
                    # Classify cause — most specific first
                    file_tools = [
                        t
                        for t in _recent_writes
                        if t in ("Edit", "MultiEdit", "Write", "NotebookEdit")
                    ]
                    bash_tools = [t for t in _recent_writes if t == "Bash"]
                    if idle_secs > 300:
                        event["cause"] = "ttl_expiry"
                    elif file_tools:
                        # Direct file modification by Claude's own tools
                        event["cause"] = "file_change"
                    elif bash_tools:
                        # Bash ran something that modified files on disk;
                        # file watcher detected the change → cache invalidated
                        event["cause"] = "bash_side_effect"
                    elif idle_secs > 10 and not _recent_writes:
                        # No tool caused it, short idle — file watcher detected
                        # an external change (another terminal, build system, etc.)
                        event["cause"] = "external_change"
                    elif _prev_cr > 0 and cr < _prev_cr * 0.1:
                        event["cause"] = "context_change"
                    else:
                        event["cause"] = "cache_miss"

                    # Cost of this event: what was paid at cache_write rate
                    # vs what would have been paid at cache_read rate
                    p = get_pricing(model)
                    event["cost"] = (cw / 1e6) * p["cache_write"]
                    event["saved_if_cached"] = (cw / 1e6) * (
                        p["cache_write"] - p["cache_read"]
                    )
                    r["cache_events"].append(event)

                _prev_ts = ts
                _prev_cr = cr
                _recent_writes = []  # reset after processing
        elif mid and u:
            # Subsequent streaming chunk for an already-seen mid.
            # input/cw/cr are frozen after the first chunk, but output_tokens
            # grows until the final chunk. Upgrade r["output_tokens"] to the
            # new value whenever the streaming delta grows.
            new_out = _get_usage_field(u, "output_tokens")
            prev_out = mid_output_seen.get(mid, 0)
            if new_out > prev_out:
                delta = new_out - prev_out
                r["output_tokens"] += delta
                model = msg.get("model", "unknown")
                if model in r["models_used"]:
                    r["models_used"][model]["output_tokens"] += delta
                mid_output_seen[mid] = new_out

        content = msg.get("content", [])
        if not isinstance(content, list):
            continue

        # Collect tool names in this message for token attribution
        msg_tools = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tn = block.get("name", "unknown")
            msg_tools.append(tn)
            r["tools_used"][tn] += 1
            # Record tool_use_id -> tool_name for matching tool_results
            tuid = block.get("id", "")
            if tuid:
                tool_use_id_map[tuid] = tn
            ti = block.get("input", {})
            if not isinstance(ti, dict):
                continue
            # v2.1.108: built-in slash commands routed through Skill tool.
            # Every Skill tool_use carries input.skill = the skill identifier
            # (e.g. "commit", "code-auditor-agent:caa-pr-review-skill"). Track
            # these separately so the report shows which skills cost what.
            if tn == "Skill":
                skill_name = ti.get("skill", "") or "unknown"
                r["skills_used"][skill_name] += 1
                r["skills_tokens"][skill_name]["invocation_count"] += 1
                if tuid:
                    skill_use_id_map[tuid] = skill_name
            fp = ti.get("file_path", "")
            if tn == "Read" and fp:
                r["files_read"].add(fp)
            elif tn == "Write" and fp:
                r["files_written"].add(fp)
            elif tn in ("Edit", "MultiEdit") and fp:
                r["files_edited"].add(fp)
            elif tn == "Bash" and ti.get("command"):
                r["bash_commands"].append(trunc(ti["command"], 80))
            elif tn in ("WebFetch", "WebSearch"):
                q = ti.get("url", ti.get("query", ""))
                if q:
                    r["web_fetches"].append(trunc(q, 80))
            # Track tools that can modify files (triggers loadChangedFiles
            # via file watchers, causing cache invalidation). Bash is included
            # because linters/formatters invoked via Bash modify files on disk
            # and the file watcher detects the change.
            if tn in ("Edit", "MultiEdit", "Write", "NotebookEdit", "Bash"):
                _recent_writes.append(tn)

        # Attribute this message's tokens to the tools it invoked
        u = msg.get("usage", {})
        if msg_tools and u:
            per_tool_out = u.get("output_tokens", 0) // len(msg_tools)
            per_tool_inp = u.get("input_tokens", 0) // len(msg_tools)
            for tn in msg_tools:
                r["tools_tokens"][tn]["output"] += per_tool_out
                r["tools_tokens"][tn]["input"] += per_tool_inp
            # Also attribute output bytes to each skill the message spawned.
            # A single message can issue multiple Skill invocations; credit each
            # Skill's share of the assistant output (the bytes the model spent
            # writing the Skill tool-use block itself, NOT the skill's loaded
            # content — that is credited via tool_result below).
            # Must match the detection-path input validation exactly: a Skill
            # block with a non-dict input was already skipped for invocation
            # counting, so we skip it here too. Otherwise output_tokens could
            # be attributed to a skill that has no invocation_count.
            for sb in content:
                if not (
                    isinstance(sb, dict)
                    and sb.get("type") == "tool_use"
                    and sb.get("name") == "Skill"
                ):
                    continue
                si = sb.get("input", {})
                if not isinstance(si, dict):
                    continue
                sname = si.get("skill", "") or "unknown"
                r["skills_tokens"][sname]["output_tokens"] += per_tool_out

    r["files_read"] = sorted(r["files_read"])
    r["files_written"] = sorted(r["files_written"])
    r["files_edited"] = sorted(r["files_edited"])
    r["entries_with_usage"] = entries_with_usage

    # v2.1.90 tool-results/ spillover: credit any spilled files to the tool
    # that produced them. Filename stem is typically the tool_use_id.
    # Rough token approximation: bytes/4 (same as tiktoken fallback).
    if spill_files:
        total_spill = 0
        for key, size in spill_files.items():
            total_spill += size
            # Try exact match first, then extension-boundary match.
            # Previous implementation used `key.startswith(tuid) or
            # tuid.startswith(key)` which double-credits when two tool ids
            # share a common prefix (e.g. "toolu_01" and "toolu_012"). The
            # boundary must be an extension separator '.' so only
            # "toolu_01.txt" matches tool "toolu_01", never "toolu_012".
            tool_name = tool_use_id_map.get(key, "")
            matched_key = key if tool_name else ""
            if not tool_name:
                for tuid, tn in tool_use_id_map.items():
                    if key == tuid or key.startswith(tuid + "."):
                        tool_name = tn
                        matched_key = tuid
                        break
            if tool_name:
                sp_tok = size // 4
                r["tools_tokens"][tool_name]["result_tokens"] += sp_tok
                # If the spilled payload came from a Skill invocation, also
                # credit it to the specific skill (same contract as the inline
                # and toolUseResult paths above).
                sk_sp = skill_use_id_map.get(matched_key, "")
                if sk_sp:
                    r["skills_tokens"][sk_sp]["result_tokens"] += sp_tok
        r["spilled_tool_bytes"] = total_spill

    return r


# ─────────────────────────────────────────────
# Report builder
# ─────────────────────────────────────────────


def _rel_path(filepath: str, project_dir: str) -> str:
    """Convert absolute path to project-relative path."""
    if not project_dir or not filepath:
        return filepath
    try:
        return str(Path(filepath).relative_to(project_dir))
    except ValueError:
        # Path is outside project — try home-relative
        try:
            return "~/" + str(Path(filepath).relative_to(Path.home()))
        except ValueError:
            return filepath


def build_report(
    hook_event: str,
    hook_input: dict,
    usage: dict,
    identity: dict,
    sub_usage_list: list[Any] | None = None,
    session_events: list | None = None,
    subagent_fallback: bool = False,
    events_log_path: str = "",
) -> str:
    """Build a compact unicode-bordered report for terminal display.

    Args:
        subagent_fallback: True when a SubagentStop event hit the fallback
            path of parsing the main session transcript instead of the
            subagent's own transcript. The reported totals then reflect
            the entire session, not just the subagent.
        events_log_path: Optional filesystem path where all cache events
            were persisted, so users can inspect events beyond the top-5
            shown inline.
    """
    is_sub = hook_event in ("SubagentStop", "TeammateIdle", "TaskCompleted")
    # Show the hook event type as the label for teammate/task events
    label_map = {
        "SubagentStop": "Subagent",
        "TeammateIdle": "Teammate",
        "TaskCompleted": "Task",
        "StopFailure": "FAILED",
    }
    label = label_map.get(hook_event, "Session")
    # For subagent events, display the agent_id; for Stop (main session), session_id
    if is_sub:
        display_id = hook_input.get("agent_id", "")
        # v2.1.85 fallback: extract from agent_transcript_path filename
        if not display_id:
            atp = hook_input.get("agent_transcript_path", "")
            stem = Path(atp).stem if atp else ""
            if stem.startswith("agent-"):
                display_id = stem[6:]
    else:
        display_id = hook_input.get("session_id", "")
    short_id = display_id[:8] if display_id else "?"
    project_dir = hook_input.get("cwd", "")

    models_used = usage.get("models_used", {})
    real_models = {m: s for m, s in models_used.items() if m != "<synthetic>"}
    # Exclude "<synthetic>" placeholder usage from cost: estimate_cost on a
    # synthetic model falls back to DEFAULT_PRICING and inflates the total
    # with a fake cost that never hit the real API.
    total_cost = sum(estimate_cost(s, m) for m, s in real_models.items())
    model_names = "/".join(shorten_model(m) for m in real_models.keys()) or "unknown"

    # Token breakdown
    inp = usage["input_tokens"]
    out = usage["output_tokens"]
    cw = usage["cache_creation_input_tokens"]
    cr = usage["cache_read_input_tokens"]
    msgs = usage["message_count"]

    # Tools — separate regular tools from MCP tools (long names break box width)
    tools = usage.get("tools_used", {})
    all_top_tools = (
        tools.most_common()
        if hasattr(tools, "most_common")
        else sorted(tools.items(), key=lambda x: -x[1])
    )
    regular_tools = [(t, c) for t, c in all_top_tools if not t.startswith("mcp__")]
    mcp_tools_list = [(t, c) for t, c in all_top_tools if t.startswith("mcp__")]

    # Files — relative to project
    fw = [_rel_path(f, project_dir) for f in usage.get("files_written", [])]
    fe = [_rel_path(f, project_dir) for f in usage.get("files_edited", [])]
    fr = [_rel_path(f, project_dir) for f in usage.get("files_read", [])]

    # ── Build rows ──
    rows: list[str | tuple[str, str]] = []

    # ANSI color palette for dark terminals
    # Principle: ONE color for all static/non-changing text (same as border),
    # bright colors ONLY for dynamic/changing values that pop out
    S = "\033[94m"  # static - bright blue (border, labels, all non-changing text)
    Y = "\033[93m"  # bright yellow - ALL token values (unified)
    C = "\033[92m"  # bright green - cost values
    G = "\033[95m"  # bright magenta - tool counts
    H = "\033[96m"  # bright cyan - session hash
    W = "\033[97m"  # bright white - model, msg count, tool names
    R = "\033[0m"  # reset

    # Duration from timestamps
    duration_str = ""
    t0 = usage.get("first_timestamp", "")
    t1 = usage.get("last_timestamp", "")
    if t0 and t1 and t0 != t1:
        from datetime import datetime

        try:
            dt0 = datetime.fromisoformat(t0.replace("Z", "+00:00"))
            dt1 = datetime.fromisoformat(t1.replace("Z", "+00:00"))
            elapsed = (dt1 - dt0).total_seconds()
            if elapsed > 0:
                duration_str = f" {S}|{R} {W}{fmt_duration(elapsed)}{R}"
        except (ValueError, TypeError):
            pass

    # Header: static text same as border, dynamic values bright
    # For SubagentStop, show the agent type (e.g. "Subagent Explore a2c30deb")
    # For StopFailure, show label in red to flag the error
    sub_type = identity.get("subagent_type", "") if is_sub else ""
    type_part = f" {W}{sub_type}{R}" if sub_type else ""
    label_color = "\033[91m" if hook_event == "StopFailure" else S
    # Streaming overhead indicator: Claude Code writes 1 JSONL entry per
    # streaming delta. When entries >> messages, the transcript is split
    # across many chunks per message (normal for long outputs).
    entries = usage.get("entries_with_usage", 0)
    entries_hint = ""
    if entries and entries > msgs:
        entries_hint = f" {S}({entries} entries){R}"
    header_line = (
        f"{label_color}{label}{R}{type_part}"
        f" {H}{short_id}{R}"
        f" {S}|{R} {W}{model_names}{R}"
        f" {S}|{R} {W}{msgs}{R}"
        f" {S}messages{R}{entries_hint}{duration_str}"
    )
    rows.append(header_line)

    # Subagent-fallback warning: SubagentStop fired but the subagent's own
    # transcript was not available, so we parsed the whole main session. The
    # numbers then reflect the ENTIRE session, not just this subagent.
    if subagent_fallback:
        RED = "\033[91m"
        rows.append(
            (
                "",
                f"  {RED}⚠ SUBAGENT TRANSCRIPT MISSING — numbers below "
                f"reflect the ENTIRE main session, not just this subagent{R}",
            )
        )

    # Implausible-size warning: even without fallback, a "subagent" with
    # thousands of messages or hundreds of hours is suspicious and might
    # actually be a long-running background agent OR the fallback case.
    if is_sub and not subagent_fallback:
        dur_hours = 0.0
        t0 = usage.get("first_timestamp", "")
        t1 = usage.get("last_timestamp", "")
        if t0 and t1:
            from datetime import datetime as _dt

            try:
                dt0 = _dt.fromisoformat(t0.replace("Z", "+00:00"))
                dt1 = _dt.fromisoformat(t1.replace("Z", "+00:00"))
                dur_hours = (dt1 - dt0).total_seconds() / 3600
            except (ValueError, TypeError):
                pass
        if msgs > 5000 or dur_hours > 24:
            RED = "\033[91m"
            rows.append(
                (
                    "",
                    f"  {RED}⚠ UNUSUALLY LARGE SUBAGENT"
                    f" ({msgs} messages, {dur_hours:.0f}h)"
                    f" — verify the numbers match your Anthropic dashboard{R}",
                )
            )

    # Primary tokens — show each Anthropic billing category separately so
    # users don't confuse cache_write with user-typed input. Headline covers
    # new input, cache_write (rebuild cost), and output. Cache_read is on the
    # next line because it's excluded from rate limits.
    tok_val = (
        f"{Y}{fmt_tok(inp)}{R} {S}new in{R} {S}/{R}"
        f" {Y}{fmt_tok(cw)}{R} {S}cached{R} {S}/{R}"
        f" {Y}{fmt_tok(out)}{R} {S}out{R}"
    )
    rows.append(("Tokens", tok_val))

    # Explain the three billed-input categories for users who see millions
    # of "cached" tokens and wonder where they came from.
    if cw > 0:
        rows.append(
            (
                "",
                f"{S}  L cached (billed 1.25x, included in limits):{R}"
                f" {Y}{fmt_tok(cw)}{R}",
            )
        )

    # Cache read — NOT counted toward rate limits, billed 0.1x
    if cr > 0:
        rows.append(
            (
                "",
                f"{S}  L cache-read (billed 0.1x, excluded from limits):{R}"
                f" {Y}{fmt_tok(cr)}{R}",
            )
        )

    # Cache efficiency — how much of total input came from cache
    total_input_all = inp + cw + cr
    if cr > 0 and total_input_all > 0:
        cache_pct = (cr / total_input_all) * 100
        rows.append(
            (
                "",
                f"{S}  L cache efficiency:{R}"
                f" {Y}{cache_pct:.0f}%{R}"
                f" {S}of input from cache{R}",
            )
        )

    # ── Cache invalidation events (prominent, red) ──
    RED = "\033[91m"
    cache_events = usage.get("cache_events", [])
    if cache_events:
        cause_labels = {
            "ttl_expiry": "TTL expired (idle >5m)",
            "file_change": "file changed → context resent",
            "bash_side_effect": "Bash modified files → file watcher invalidated",
            "external_change": "external file change → file watcher invalidated",
            "context_change": "context changed",
            "cache_miss": "cache miss",
        }
        rows.append(
            (
                "",
                f"  {RED}⚠ {len(cache_events)}"
                f" cache invalidation"
                f"{'s' if len(cache_events) > 1 else ''}"
                f" detected{R}",
            )
        )
        total_penalty = sum(e.get("saved_if_cached", 0) for e in cache_events)
        if total_penalty > 0.001:
            rows.append(
                (
                    "",
                    f"  {RED}  penalty:"
                    f" ${total_penalty:.4f}"
                    f" wasted on context resend{R}",
                )
            )
        for ei, ev in enumerate(cache_events[:5]):  # cap at 5 to avoid box explosion
            cause = cause_labels.get(ev["cause"], ev["cause"])
            cw_tok = ev["cache_write_tokens"]
            idle = ev.get("idle_seconds", 0)
            idle_str = ""
            if idle > 0:
                idle_str = f" after {fmt_duration(idle)} idle"
            tools_str = ""
            if ev.get("preceding_tools"):
                tools_str = f" after {'/'.join(ev['preceding_tools'])}"
            rows.append(
                (
                    "",
                    f"  {RED}  #{ei + 1}"
                    f" {fmt_tok(cw_tok)} resent{R}"
                    f" {S}— {cause}"
                    f"{idle_str}{tools_str}{R}",
                )
            )
        # Show N total when capped — users need to know they're looking
        # at a truncated slice of the full event list.
        if len(cache_events) > 5:
            rows.append(
                (
                    "",
                    f"  {S}  (showing top 5 of {len(cache_events)} events — "
                    f"see log for the rest){R}",
                )
            )
        if events_log_path:
            rows.append(
                (
                    "",
                    f"  {S}  full event log: {events_log_path}{R}",
                )
            )
        # Batching opportunity: count invalidations
        # within 60s of each other
        # that could have been a single reprocess if modifications were grouped
        if len(cache_events) >= 2:
            from datetime import datetime as _dt

            clustered = 0
            for i_ce in range(1, len(cache_events)):
                t_prev = cache_events[i_ce - 1].get("timestamp", "")
                t_curr = cache_events[i_ce].get("timestamp", "")
                if t_prev and t_curr:
                    try:
                        gap = abs(
                            (
                                _dt.fromisoformat(t_curr.replace("Z", "+00:00"))
                                - _dt.fromisoformat(t_prev.replace("Z", "+00:00"))
                            ).total_seconds()
                        )
                        if gap < 60:
                            clustered += 1
                    except (ValueError, TypeError):
                        pass
            if clustered > 0:
                saved = clustered  # each could have been avoided
                rows.append(
                    ("", f"  {Y}⟳ {saved} could be avoided by batching file changes{R}")
                )

    # ── v2.1.90+ compact_boundary events (authoritative compaction markers) ──
    compact_events = usage.get("compact_events", [])
    if compact_events:
        rows.append(
            (
                "Compact",
                f"{W}{len(compact_events)}{R}"
                f" {S}auto-compaction boundar"
                f"{'ies' if len(compact_events) != 1 else 'y'}{R}",
            )
        )
        for ce in compact_events[:3]:
            pre = ce.get("pre_tokens", 0)
            trig = ce.get("trigger", "auto")
            rows.append(
                (
                    "",
                    f"  {S}·{R} {W}{fmt_tok(pre)}{R} {S}pre-compact ({trig}){R}",
                )
            )

    # ── v2.1.101+ InstructionsLoaded + other lightweight session events ──
    if session_events:
        instr_events = [
            e for e in session_events if e.get("hook_event") == "InstructionsLoaded"
        ]
        compact_hooks = [
            e for e in session_events if e.get("hook_event") == "PostCompact"
        ]
        fc_events = [e for e in session_events if e.get("hook_event") == "FileChanged"]
        cwd_events = [e for e in session_events if e.get("hook_event") == "CwdChanged"]
        pd_events = [
            e for e in session_events if e.get("hook_event") == "PermissionDenied"
        ]
        task_created = [
            e for e in session_events if e.get("hook_event") == "TaskCreated"
        ]

        if instr_events:
            reasons = Counter(e.get("load_reason", "unknown") for e in instr_events)
            reason_str = ", ".join(f"{k} x{v}" for k, v in reasons.most_common())
            rows.append(
                (
                    "Rules",
                    f"{W}{len(instr_events)}{R} {S}CLAUDE.md / rules loads{R}",
                )
            )
            rows.append(("", f"  {S}·{R} {W}{reason_str}{R}"))
            # Highlight compact-triggered reloads (cache invalidation)
            compact_reloads = reasons.get("compact", 0)
            if compact_reloads:
                rows.append(
                    (
                        "",
                        f"  {RED}⚠{R} {W}{compact_reloads}{R}"
                        f" {S}rule reload(s) triggered by compact{R}",
                    )
                )
        if compact_hooks:
            rows.append(
                ("", f"  {S}·{R} {W}{len(compact_hooks)}{R} {S}PostCompact event(s){R}")
            )
        if fc_events:
            rows.append(
                (
                    "FileChanged",
                    f"{W}{len(fc_events)}{R} {S}external file event(s){R}",
                )
            )
        if cwd_events:
            rows.append(
                ("CwdChanged", f"{W}{len(cwd_events)}{R} {S}directory transitions{R}")
            )
        if pd_events:
            rows.append(
                (
                    "Denied",
                    f"{RED}{len(pd_events)}{R} {S}permission denial(s){R}",
                )
            )
        if task_created:
            rows.append(("Tasks", f"{W}{len(task_created)}{R} {S}agents spawned{R}"))

    # Cost — label reflects scope: "(lifetime)" for completed
    # agents, "(this op)" for session
    cost_scope = "(lifetime)" if is_sub else "(this op)"
    rows.append(("Cost", f"{C}${total_cost:.2f}{R} {S}{cost_scope}{R}"))

    # Per-model breakdown (only if multiple real models)
    if len(real_models) > 1:
        for model, stats in real_models.items():
            c = estimate_cost(stats, model)
            mt = sum(
                stats[f]
                for f in [
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ]
            )
            rows.append(
                (
                    f"{S}  L {shorten_model(model)}{R}",
                    f"{Y}{fmt_tok(mt)}{R} {S}tokens /{R} {C}${c:.2f}{R}",
                )
            )

    # Regular tools with per-tool token attribution
    tools_tokens = usage.get("tools_tokens", {})
    if regular_tools:
        tool_str = f" {S}/{R} ".join(f"{W}{t}{R} {G}x{c}{R}" for t, c in regular_tools)
        rows.append(("Tools", tool_str))
        # Per-tool token breakdown for regular tools
        for t, c in regular_tools:
            tt = tools_tokens.get(t, {})
            t_inp = tt.get("input", 0)
            t_out = tt.get("output", 0)
            result_toks = tt.get("result_tokens", 0)
            parts = []
            if t_inp > 0:
                parts.append(f"{Y}{fmt_tok(t_inp)}{R} {S}in{R}")
            if t_out > 0:
                parts.append(f"{Y}{fmt_tok(t_out)}{R} {S}out{R}")
            if result_toks > 0:
                parts.append(f"{Y}{fmt_tok(result_toks)}{R} {S}result→input{R}")
            if parts:
                detail = f" {S}/{R} ".join(parts)
                rows.append(("", f"{S}  L{R} {W}{t}{R} {G}x{c}{R}{S}:{R} {detail}"))

    # MCP tools — listed vertically (one per line) to avoid breaking box width
    if mcp_tools_list:
        total_mcp_calls = sum(c for _, c in mcp_tools_list)
        rows.append(
            (
                "MCP",
                f"{W}{len(mcp_tools_list)}{R}"
                f" {S}tools{R} {S}/{R}"
                f" {G}x{total_mcp_calls}{R}"
                f" {S}calls{R}",
            )
        )
        # Each MCP tool on its own line with shortened name and token breakdown
        for t, c in mcp_tools_list:
            short_t = shorten_mcp_tool(t)
            tt = tools_tokens.get(t, {})
            t_inp = tt.get("input", 0)
            t_out = tt.get("output", 0)
            result_toks = tt.get("result_tokens", 0)
            parts = []
            if t_inp > 0:
                parts.append(f"{Y}{fmt_tok(t_inp)}{R} {S}in{R}")
            if t_out > 0:
                parts.append(f"{Y}{fmt_tok(t_out)}{R} {S}out{R}")
            if result_toks > 0:
                parts.append(f"{Y}{fmt_tok(result_toks)}{R} {S}result→input{R}")
            detail = f" {S}/{R} ".join(parts) if parts else ""
            detail_str = f"{S}:{R} {detail}" if detail else ""
            rows.append(("", f"{S}  L{R} {W}{short_t}{R} {G}x{c}{R}{detail_str}"))

    # Total result→input across all tools — how much tools fed back as input
    total_result_in = sum(tt.get("result_tokens", 0) for tt in tools_tokens.values())
    if total_result_in > 0:
        rows.append(
            ("", f"{S}  L total result→input:{R} {Y}{fmt_tok(total_result_in)}{R}")
        )

    # Skills (v2.1.108+: built-in slash commands are routed through Skill tool).
    # Each skill invocation credits: invocation_count, result_tokens (the skill
    # content loaded into context), and output_tokens (the model's share of
    # output spent writing the Skill tool-use block). We cost result_tokens at
    # the input rate and output_tokens at the output rate using the session's
    # most-used model as the reference.
    skills_used_ctr = usage.get("skills_used", Counter())
    skills_tokens_d = usage.get("skills_tokens", {})
    if skills_used_ctr:
        ref_model = ""
        ref_tok = -1
        for m_name, m_stats in models_used.items():
            if m_name == "<synthetic>":
                continue
            t = m_stats.get("input_tokens", 0) + m_stats.get("output_tokens", 0)
            if t > ref_tok:
                ref_tok = t
                ref_model = m_name
        sk_pricing = get_pricing(ref_model)
        skill_rows: list[tuple[str, int, int, int, float]] = []
        for sk_name, sk_count in skills_used_ctr.items():
            st = skills_tokens_d.get(sk_name, {})
            sk_res = st.get("result_tokens", 0)
            sk_out = st.get("output_tokens", 0)
            sk_cost = (sk_res / 1e6) * sk_pricing["input"] + (
                sk_out / 1e6
            ) * sk_pricing["output"]
            skill_rows.append((sk_name, sk_count, sk_res, sk_out, sk_cost))
        skill_rows.sort(key=lambda x: x[4], reverse=True)
        total_sk_calls = sum(sk_count for _, sk_count, _, _, _ in skill_rows)
        rows.append(
            (
                "Skills",
                f"{W}{len(skill_rows)}{R} {S}skills{R} {S}/{R} "
                f"{G}x{total_sk_calls}{R} {S}calls{R}",
            )
        )
        for sk_name, sk_count, sk_res, sk_out, sk_cost in skill_rows:
            short_sk = sk_name if len(sk_name) <= 34 else sk_name[:31] + "..."
            parts = []
            if sk_res > 0:
                parts.append(f"{Y}{fmt_tok(sk_res)}{R} {S}result→input{R}")
            if sk_out > 0:
                parts.append(f"{Y}{fmt_tok(sk_out)}{R} {S}out{R}")
            if sk_cost > 0:
                parts.append(f"{C}${sk_cost:.3f}{R}")
            detail = f" {S}/{R} ".join(parts) if parts else ""
            detail_str = f"{S}:{R} {detail}" if detail else ""
            rows.append(
                (
                    "",
                    f"{S}  L{R} {W}{short_sk}{R} {G}x{sk_count}{R}{detail_str}",
                )
            )

    # Bash commands executed
    bash_cmds = usage.get("bash_commands", [])
    if bash_cmds:
        rows.append(("Bash", f"{W}{len(bash_cmds)}{R} {S}commands{R}"))
        for cmd in bash_cmds:
            rows.append(("", f"  {S}${R} {W}{cmd}{R}"))

    # Web fetches
    web_fetches = usage.get("web_fetches", [])
    if web_fetches:
        rows.append(("Web", f"{W}{len(web_fetches)}{R} {S}fetches{R}"))
        for url in web_fetches:
            rows.append(("", f"  {S}→{R} {W}{url}{R}"))

    # Files summary (counts bright, labels static)
    file_parts = []
    if fr:
        file_parts.append(f"{W}{len(fr)}{R} {S}read{R}")
    if fe:
        file_parts.append(f"{W}{len(fe)}{R} {S}edited{R}")
    if fw:
        file_parts.append(f"{W}{len(fw)}{R} {S}written{R}")
    if file_parts:
        rows.append(("Files", f" {S}/{R} ".join(file_parts)))

    # List all edited files
    for f in fe:
        rows.append(("", f"  {S}*{R} {W}{f}{R}"))

    # List all written files
    for f in fw:
        rows.append(("", f"  {S}+{R} {W}{f}{R}"))

    # List all read files
    for f in fr:
        rows.append(("", f"  {S}·{R} {W}{f}{R}"))

    # Subagent task description — no truncation
    if is_sub:
        task = identity.get("task_description", "")
        if task:
            rows.append(("Task", f"{W}{task}{R}"))

    # Sub-agent breakdown — shows tokens consumed by agents spawned by this agent
    # (e.g., a worktree skill that launches a swarm of sub-agents)
    if sub_usage_list:
        n_subs = len(sub_usage_list)
        sub_total_inp = sum(
            u["input_tokens"] + u["cache_creation_input_tokens"]
            for _, u in sub_usage_list
        )
        sub_total_out = sum(u["output_tokens"] for _, u in sub_usage_list)
        rows.append(
            (
                "Sub-agents",
                f"{W}{n_subs}{R} {S}spawned:{R}"
                f" {Y}{fmt_tok(sub_total_inp)}{R}"
                f" {S}in{R} {S}/{R}"
                f" {Y}{fmt_tok(sub_total_out)}{R}"
                f" {S}out{R}",
            )
        )

        # Aggregate by agent_type so users can see which agent TYPE is the most
        # expensive across all invocations, not just which individual instance.
        # This is critical when the same agent type (e.g. "Explore") is spawned
        # many times — the individual rows show small per-instance cost but the
        # type-level aggregate reveals the total drain.
        type_agg: dict[str, dict[str, float]] = {}
        for sa_info, sa_usage in sub_usage_list:
            sa_type = sa_info.get("agent_type", "") or "untyped"
            if sa_type not in type_agg:
                type_agg[sa_type] = {"count": 0.0, "inp": 0.0, "out": 0.0, "cost": 0.0}
            type_agg[sa_type]["count"] += 1
            type_agg[sa_type]["inp"] += (
                sa_usage["input_tokens"] + sa_usage["cache_creation_input_tokens"]
            )
            type_agg[sa_type]["out"] += sa_usage["output_tokens"]
            type_agg[sa_type]["cost"] += sum(
                estimate_cost(s, m) for m, s in sa_usage.get("models_used", {}).items()
            )
        if len(type_agg) > 1 or (
            len(type_agg) == 1 and next(iter(type_agg.values()))["count"] > 1
        ):
            type_rows = sorted(
                type_agg.items(), key=lambda kv: kv[1]["cost"], reverse=True
            )
            rows.append(("", f"  {S}by type:{R}"))
            for tname, ts in type_rows:
                tcnt = int(ts["count"])
                tinp = int(ts["inp"])
                tout = int(ts["out"])
                tcost = ts["cost"]
                cost_str = f" {C}${tcost:.4f}{R}" if tcost > 0 else ""
                rows.append(
                    (
                        "",
                        f"    {S}*{R} {W}{trunc(tname, 28)}{R}"
                        f" {G}x{tcnt}{R}"
                        f" {Y}{fmt_tok(tinp)}{R}"
                        f"{S}/{R}"
                        f"{Y}{fmt_tok(tout)}{R}"
                        f"{cost_str}",
                    )
                )

        for sa_info, sa_usage in sub_usage_list:
            sa_type = sa_info.get("agent_type", "")
            sa_desc = sa_info.get("description", "")
            sa_label = sa_type or sa_desc or sa_info["agent_id"][:12]
            sa_inp = sa_usage["input_tokens"] + sa_usage["cache_creation_input_tokens"]
            sa_out = sa_usage["output_tokens"]
            sa_cost = sum(
                estimate_cost(s, m) for m, s in sa_usage.get("models_used", {}).items()
            )
            cost_str = f" {C}${sa_cost:.4f}{R}" if sa_cost > 0 else ""
            rows.append(
                (
                    "",
                    f"  {S}>{R}"
                    f" {W}{trunc(sa_label, 30)}{R}"
                    f" {Y}{fmt_tok(sa_inp)}{R}"
                    f"{S}/{R}"
                    f"{Y}{fmt_tok(sa_out)}{R}"
                    f"{cost_str}",
                )
            )

    return _render_box(rows)


def build_worktree_report(
    worktree_path: str,
    orchestrator_usage: dict,
    sub_usage_list: list,
) -> str:
    """Build a detailed worktree breakdown box showing per-agent cache dynamics.

    Cache invalidation in worktrees is expensive: when an agent modifies a file,
    loadChangedFiles() invalidates the cache, forcing all subsequent messages to
    re-send context as cache_creation tokens (1.25x price) instead of cache_read
    (0.1x price). Agents that share context with the parent can reuse the cache.
    This report makes those dynamics visible.
    """
    # ANSI palette (same as build_report)
    S = "\033[94m"
    Y = "\033[93m"
    C = "\033[92m"
    G = "\033[95m"
    H = "\033[96m"
    W = "\033[97m"
    R = "\033[0m"
    RED = "\033[91m"

    rows: list[str | tuple[str, str]] = []

    # ── Header ──
    n_agents = len(sub_usage_list)
    # Compute total tokens across orchestrator + all sub-agents
    all_usages = [orchestrator_usage] + [u for _, u in sub_usage_list]
    total_inp = sum(u.get("input_tokens", 0) for u in all_usages)
    total_out = sum(u.get("output_tokens", 0) for u in all_usages)
    total_cw = sum(u.get("cache_creation_input_tokens", 0) for u in all_usages)
    total_cr = sum(u.get("cache_read_input_tokens", 0) for u in all_usages)
    total_msgs = sum(u.get("message_count", 0) for u in all_usages)
    total_all_input = total_inp + total_cw + total_cr

    # Compute total cost
    total_cost = 0.0
    for u in all_usages:
        for m, s in u.get("models_used", {}).items():
            total_cost += estimate_cost(s, m)

    wt_label = worktree_path
    # Shorten path if possible
    try:
        wt_label = "~/" + str(Path(worktree_path).relative_to(Path.home()))
    except (ValueError, TypeError):
        pass
    # Trim to just the meaningful tail
    if "/worktrees/" in wt_label:
        wt_label = wt_label[wt_label.index("/worktrees/") :]
    elif len(wt_label) > 50:
        wt_label = "..." + wt_label[-47:]

    rows.append(
        f"{S}Worktree{R} {H}{wt_label}{R} {S}|{R} "
        f"{W}{n_agents + 1}{R} {S}agents{R} {S}|{R} "
        f"{W}{total_msgs}{R} {S}msgs{R} {S}|{R} "
        f"{C}${total_cost:.2f}{R}"
    )

    # ── Totals ──
    rows.append(
        (
            "Tokens",
            f"{Y}{fmt_tok(total_inp + total_cw)}{R}"
            f" {S}input{R} {S}/{R}"
            f" {Y}{fmt_tok(total_out)}{R}"
            f" {S}output{R}",
        )
    )
    if total_cw > 0:
        rows.append(
            (
                "",
                f"{S}  L cache-write:{R}"
                f" {Y}{fmt_tok(total_cw)}{R}"
                f" {S}(billed at 1.25x){R}",
            )
        )
    if total_cr > 0:
        rows.append(
            (
                "",
                f"{S}  L cache-read:{R}"
                f" {Y}{fmt_tok(total_cr)}{R}"
                f" {S}(billed at 0.1x){R}",
            )
        )

    # ── Cache efficiency overview ──
    if total_all_input > 0:
        cache_eff = (total_cr / total_all_input) * 100
        # Color the efficiency: green if good (>50%),
        # yellow if ok (20-50%), red if bad (<20%)
        if cache_eff >= 50:
            eff_color = C
        elif cache_eff >= 20:
            eff_color = Y
        else:
            eff_color = RED
        rows.append(
            (
                "Cache",
                f"{eff_color}{cache_eff:.0f}%{R} {S}of all input served from cache{R}",
            )
        )

        # Cache invalidation penalty: cache_write tokens that COULD have been
        # cache_read if the cache wasn't invalidated. The cost delta is:
        # (cache_write_price - cache_read_price) per million tokens
        if total_cw > 0 and total_cr > 0:
            # Estimate: if all cache_write had been cache_read instead
            avg_cw_price = 3.75  # default sonnet cache_write $/MTok
            avg_cr_price = 0.30  # default sonnet cache_read $/MTok
            # Try to get actual prices from the most-used model
            for u in all_usages:
                for m_name in u.get("models_used", {}):
                    p = get_pricing(m_name)
                    avg_cw_price = p["cache_write"]
                    avg_cr_price = p["cache_read"]
                    break
                break
            penalty_cost = (total_cw / 1e6) * (avg_cw_price - avg_cr_price)
            if penalty_cost > 0.001:
                rows.append(
                    (
                        "",
                        f"{S}  L invalidation penalty:{R}"
                        f" {RED}${penalty_cost:.4f}{R}"
                        f" {S}(cache-write that could"
                        f" be cache-read){R}",
                    )
                )

    # ── Orchestrator row ──
    o = orchestrator_usage
    o_inp = o.get("input_tokens", 0) + o.get("cache_creation_input_tokens", 0)
    o_out = o.get("output_tokens", 0)
    o_cr = o.get("cache_read_input_tokens", 0)
    o_all = o_inp + o_cr
    o_eff = (
        f" {S}cache:{R} {Y}{(o_cr / o_all * 100):.0f}%{R}"
        if o_all > 0 and o_cr > 0
        else ""
    )
    o_cost = sum(estimate_cost(s, m) for m, s in o.get("models_used", {}).items())
    rows.append(
        (
            "Orchestrator",
            f"{Y}{fmt_tok(o_inp)}{R}"
            f" {S}in{R} {S}/{R}"
            f" {Y}{fmt_tok(o_out)}{R}"
            f" {S}out{R}{o_eff}"
            f" {C}${o_cost:.4f}{R}",
        )
    )

    # ── Per-agent breakdown ──
    if sub_usage_list:
        rows.append(("Agents", f"{W}{n_agents}{R} {S}spawned in worktree{R}"))
        for sa_info, sa_usage in sub_usage_list:
            sa_type = sa_info.get("agent_type", "")
            sa_desc = sa_info.get("description", "")
            sa_label = sa_type or sa_desc or sa_info["agent_id"][:12]
            sa_inp = sa_usage["input_tokens"] + sa_usage["cache_creation_input_tokens"]
            sa_out = sa_usage["output_tokens"]
            sa_cw = sa_usage["cache_creation_input_tokens"]
            sa_cr = sa_usage["cache_read_input_tokens"]
            sa_all_input = sa_inp + sa_cr
            sa_msgs = sa_usage["message_count"]
            sa_cost = sum(
                estimate_cost(s, m) for m, s in sa_usage.get("models_used", {}).items()
            )

            # Cache efficiency per agent
            cache_str = ""
            if sa_all_input > 0:
                sa_eff = (sa_cr / sa_all_input) * 100
                if sa_eff >= 50:
                    ec = C
                elif sa_eff >= 20:
                    ec = Y
                else:
                    ec = RED
                cache_str = f" {S}cache:{R} {ec}{sa_eff:.0f}%{R}"
                # Flag likely cache invalidation: high cache_write + low cache_read
                if sa_cw > 0 and sa_eff < 15:
                    cache_str += f" {RED}invalidated{R}"

            rows.append(("", f"  {S}>{R} {W}{sa_label}{R} {S}({sa_msgs} msgs){R}"))
            rows.append(
                (
                    "",
                    f"    {Y}{fmt_tok(sa_inp)}{R}"
                    f" {S}in{R} {S}/{R}"
                    f" {Y}{fmt_tok(sa_out)}{R}"
                    f" {S}out{R}{cache_str}"
                    f" {C}${sa_cost:.4f}{R}",
                )
            )
            # Show cache write/read breakdown if significant
            if sa_cw > 0 or sa_cr > 0:
                parts = []
                if sa_cw > 0:
                    parts.append(f"{RED}{fmt_tok(sa_cw)}{R} {S}written{R}")
                if sa_cr > 0:
                    parts.append(f"{C}{fmt_tok(sa_cr)}{R} {S}read{R}")
                rows.append(("", f"    {S}L cache:{R} {f' {S}/{R} '.join(parts)}"))

    # ── Cache invalidation events across all worktree agents ──
    all_cache_events = []
    for u in all_usages:
        all_cache_events.extend(u.get("cache_events", []))
    if all_cache_events:
        # Sort by timestamp
        all_cache_events.sort(key=lambda e: e.get("timestamp", ""))
        cause_labels = {
            "ttl_expiry": "TTL expired (idle >5m)",
            "file_change": "file changed → context resent",
            "bash_side_effect": "Bash modified files → file watcher invalidated",
            "external_change": "external file change → file watcher invalidated",
            "context_change": "context changed",
            "cache_miss": "cache miss",
        }
        total_penalty = sum(e.get("saved_if_cached", 0) for e in all_cache_events)
        rows.append(
            (
                "",
                f"  {RED}⚠ {len(all_cache_events)}"
                f" cache invalidation"
                f"{'s' if len(all_cache_events) > 1 else ''}{R}"
                + (
                    f" {RED}— ${total_penalty:.4f} penalty{R}"
                    if total_penalty > 0.001
                    else ""
                ),
            )
        )
        for ei, ev in enumerate(all_cache_events[:8]):
            cause = cause_labels.get(ev["cause"], ev["cause"])
            cw_tok = ev["cache_write_tokens"]
            idle = ev.get("idle_seconds", 0)
            idle_str = f" after {fmt_duration(idle)} idle" if idle > 0 else ""
            tools_str = ""
            if ev.get("preceding_tools"):
                tools_str = f" after {'/'.join(ev['preceding_tools'])}"
            rows.append(
                (
                    "",
                    f"  {RED}  #{ei + 1}"
                    f" {fmt_tok(cw_tok)} resent{R}"
                    f" {S}— {cause}"
                    f"{idle_str}{tools_str}{R}",
                )
            )
        # Batching opportunity in worktree
        if len(all_cache_events) >= 2:
            from datetime import datetime as _dt

            clustered = 0
            for i_ce in range(1, len(all_cache_events)):
                t_p = all_cache_events[i_ce - 1].get("timestamp", "")
                t_c = all_cache_events[i_ce].get("timestamp", "")
                if t_p and t_c:
                    try:
                        gap = abs(
                            (
                                _dt.fromisoformat(t_c.replace("Z", "+00:00"))
                                - _dt.fromisoformat(t_p.replace("Z", "+00:00"))
                            ).total_seconds()
                        )
                        if gap < 60:
                            clustered += 1
                    except (ValueError, TypeError):
                        pass
            if clustered > 0:
                rows.append(
                    (
                        "",
                        f"  {Y}⟳ {clustered}"
                        f" could be avoided by"
                        f" batching file changes{R}",
                    )
                )

    # ── Tool usage across all worktree agents ──
    combined_tools: Counter[str] = Counter()
    for u in all_usages:
        for tool, count in u.get("tools_used", {}).items():
            combined_tools[tool] += count
    if combined_tools:
        regular = [
            (t, c) for t, c in combined_tools.most_common() if not t.startswith("mcp__")
        ]
        mcp = [(t, c) for t, c in combined_tools.most_common() if t.startswith("mcp__")]
        if regular:
            tool_str = f" {S}/{R} ".join(
                f"{W}{t}{R} {G}x{c}{R}" for t, c in regular[:8]
            )
            rows.append(("Tools", tool_str))
        if mcp:
            for t, c in mcp[:5]:
                rows.append(("", f"  {S}L{R} {W}{shorten_mcp_tool(t)}{R} {G}x{c}{R}"))

    # ── Skills across all worktree agents (v2.1.108+) ──
    combined_skills: Counter[str] = Counter()
    combined_skill_tokens: dict[str, dict[str, int]] = {}
    for u in all_usages:
        for sk, count in u.get("skills_used", {}).items():
            combined_skills[sk] += count
        for sk, tok in u.get("skills_tokens", {}).items():
            if sk not in combined_skill_tokens:
                combined_skill_tokens[sk] = {
                    "invocation_count": 0,
                    "result_tokens": 0,
                    "output_tokens": 0,
                }
            for f in ("invocation_count", "result_tokens", "output_tokens"):
                combined_skill_tokens[sk][f] += tok.get(f, 0)
    if combined_skills:
        ref_model_wt = ""
        ref_tok_wt = -1
        for u in all_usages:
            for m_name, m_stats in u.get("models_used", {}).items():
                if m_name == "<synthetic>":
                    continue
                t_wt = m_stats.get("input_tokens", 0) + m_stats.get("output_tokens", 0)
                if t_wt > ref_tok_wt:
                    ref_tok_wt = t_wt
                    ref_model_wt = m_name
        sk_p_wt = get_pricing(ref_model_wt)
        sk_rows_wt: list[tuple[str, int, float]] = []
        for sk_name, sk_count in combined_skills.items():
            st = combined_skill_tokens.get(sk_name, {})
            sk_res = st.get("result_tokens", 0)
            sk_out = st.get("output_tokens", 0)
            sk_cost = (sk_res / 1e6) * sk_p_wt["input"] + (sk_out / 1e6) * sk_p_wt[
                "output"
            ]
            sk_rows_wt.append((sk_name, sk_count, sk_cost))
        sk_rows_wt.sort(key=lambda x: x[2], reverse=True)
        total_sk_calls_wt = sum(c for _, c, _ in sk_rows_wt)
        rows.append(
            (
                "Skills",
                f"{W}{len(sk_rows_wt)}{R} {S}skills{R} {S}/{R}"
                f" {G}x{total_sk_calls_wt}{R} {S}calls{R}",
            )
        )
        for sk_name, sk_count, sk_cost in sk_rows_wt[:6]:
            short_sk = sk_name if len(sk_name) <= 34 else sk_name[:31] + "..."
            cost_str = f" {C}${sk_cost:.4f}{R}" if sk_cost > 0 else ""
            rows.append(("", f"  {S}L{R} {W}{short_sk}{R} {G}x{sk_count}{R}{cost_str}"))

    return _render_box(rows)


def _render_box(rows: list[str | tuple[str, str]]) -> str:
    """Render rows into a unicode-bordered box with word-wrap."""
    import unicodedata

    S = "\033[94m"
    R = "\033[0m"

    def _char_width(c: str) -> int:
        cp = ord(c)
        if 0x2500 <= cp <= 0x259F:
            return 1
        cat = unicodedata.category(c)
        if cat.startswith("M") or cat == "Cf":
            return 0
        ea = unicodedata.east_asian_width(c)
        if ea in ("F", "W"):
            return 2
        if cat.startswith("So"):
            return 2
        return 1

    def _strip_ansi(s: str) -> str:
        return re.sub(r"\033\[[0-9;]*m", "", s)

    def dw(s: str) -> int:
        return sum(_char_width(c) for c in _strip_ansi(s))

    def pad(s: str, width: int) -> str:
        cur = dw(s)
        if cur >= width:
            return s
        return s + " " * (width - cur)

    header_raw = rows[0]
    # The first row is always a plain string (the header line)
    header: str = header_raw if isinstance(header_raw, str) else header_raw[1]
    data_rows = rows[1:]

    TARGET_INNER = 76
    max_label = max(
        (dw(r[0]) for r in data_rows if isinstance(r, tuple) and r[0]), default=12
    )
    max_label = max(max_label, 12)
    val_budget = TARGET_INNER - max_label - 3
    inner_w = max(TARGET_INNER, dw(header))

    _ansi_re = re.compile(r"\033\[[0-9;]*m")

    def _content_indent(text: str) -> int:
        stripped = _strip_ansi(text)
        m_prefix = re.match(r"^(\s+\S\s)", stripped)
        if m_prefix:
            return dw(m_prefix.group(1))
        return 0

    def _wrap_ansi(text: str, budget: int, cont_indent: int = 0) -> list:
        if dw(text) <= budget:
            return [text]
        cont_budget = budget - cont_indent
        if cont_budget < 20:
            cont_budget = 20
        result_lines = []
        current_line = ""
        current_width = 0
        active_codes = ""
        line_budget = budget
        i = 0
        raw = text
        while i < len(raw):
            m = _ansi_re.match(raw, i)
            if m:
                code = m.group()
                current_line += code
                if code == "\033[0m":
                    active_codes = ""
                else:
                    active_codes += code
                i = m.end()
                continue
            ch = raw[i]
            ch_w = _char_width(ch)
            if current_width + ch_w > line_budget and current_width > 0:
                result_lines.append(current_line + "\033[0m")
                line_budget = cont_budget
                current_line = (" " * cont_indent) + active_codes
                current_width = cont_indent
            current_line += ch
            current_width += ch_w
            i += 1
        if current_line:
            result_lines.append(current_line)
        return result_lines if result_lines else [text]

    out = []
    out.append("")
    out.append(f"{S}╭{'─' * (inner_w + 2)}╮{R}")
    out.append(f"{S}│{R} {pad(header, inner_w)} {S}│{R}")
    out.append(f"{S}├{'─' * (inner_w + 2)}┤{R}")

    for row in data_rows:
        if isinstance(row, tuple):
            lbl, val = row
            val_width = dw(val)
            if val_width <= val_budget:
                if lbl:
                    line = f"{S}{pad(lbl, max_label)}{R} {S}│{R} {val}"
                else:
                    line = f"{pad('', max_label)}   {val}"
                out.append(f"{S}│{R} {pad(line, inner_w)} {S}│{R}")
            else:
                ci = _content_indent(val)
                wrapped = _wrap_ansi(val, val_budget, ci)
                for wi, wline in enumerate(wrapped):
                    if wi == 0 and lbl:
                        prefix = f"{S}{pad(lbl, max_label)}{R} {S}│{R} "
                    else:
                        prefix = f"{pad('', max_label)}   "
                    full = f"{prefix}{wline}"
                    out.append(f"{S}│{R} {pad(full, inner_w)} {S}│{R}")
        else:
            out.append(f"{S}│{R} {pad(str(row), inner_w)} {S}│{R}")

    out.append(f"{S}╰{'─' * (inner_w + 2)}╯{R}")
    return "\n".join(out)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────


def dbg(msg: str):
    """Debug log to stderr — visible in ~/.claude/debug/ when debug mode is on."""
    print(f"[token-reporter] {msg}", file=sys.stderr)


def main():
    dbg("hook invoked")

    # ── --on-demand mode (bin/token-report invocation, not a hook) ──
    # No hook input, no --debug requirement. Print the current session's
    # report to stdout as plain text. Synthesizes a minimal hook_input
    # payload from env vars + current working directory.
    if "--on-demand" in sys.argv:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        transcript_path = _find_current_session_transcript(cwd)
        if not transcript_path:
            print(
                "[token-reporter] no session transcript found for cwd={}".format(cwd),
                file=sys.stderr,
            )
            sys.exit(1)
        if not session_id:
            session_id = Path(transcript_path).stem
        synthetic_input = {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "cwd": cwd,
            "hook_event_name": "OnDemand",
        }
        usage = parse_agent_transcript(
            transcript_path, session_id="", last_op_only=False
        )
        if usage.get("message_count", 0) == 0:
            print(
                "[token-reporter] transcript {} has no parseable messages".format(
                    transcript_path
                ),
                file=sys.stderr,
            )
            sys.exit(1)
        session_events = _read_session_events(session_id)
        report = build_report(
            "OnDemand",
            synthetic_input,
            usage,
            identity={},
            sub_usage_list=None,
            session_events=session_events,
        )
        # Plain text (no JSON wrapper) — user reads it directly from terminal
        print(report)
        sys.exit(0)

    # Only produce output when Claude Code is running with --debug
    if not _is_debug_mode():
        dbg("not in debug mode, skipping")
        sys.exit(0)

    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        dbg(f"JSON parse error: {e}")
        sys.exit(1)

    he = hook_input.get("hook_event_name")
    sid_short = hook_input.get("session_id", "")[:8]
    dbg(f"hook_event={he} session={sid_short}")

    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")
    hook_event = hook_input.get("hook_event_name", "unknown")
    agent_type = hook_input.get("agent_type", "")
    agent_transcript_path = hook_input.get("agent_transcript_path", "")
    # v2.1.85: agentId removed from JSONL entries. Hook input may still
    # provide agent_id (v2.1.101 added it as a common subagent-hook field).
    # If absent, extract from agent_transcript_path filename
    # (pattern: agent-{agentId}.jsonl). Do NOT fall back to session_id —
    # multiple agents can share a session.
    agent_id = hook_input.get("agent_id", "")
    if not agent_id and agent_transcript_path:
        stem = Path(agent_transcript_path).stem  # "agent-{id}"
        if stem.startswith("agent-"):
            agent_id = stem[6:]

    # v2.1.90+ lightweight hooks: no transcript parsing, just record the
    # event to the session event log so the next Stop/SubagentStop report
    # can include the count/details. These fire too often to emit reports.
    LIGHTWEIGHT_HOOKS = {
        "InstructionsLoaded",  # v2.1.101: CLAUDE.md / .claude/rules/* load
        "PostCompact",  # v2.1.90: compaction boundary
        "TaskCreated",  # v2.1.90: agent spawn
        "CwdChanged",  # v2.1.97/98: working dir changed
        "FileChanged",  # v2.1.97/98: external file change
        "PermissionDenied",  # v2.1.90: permission denial
    }
    if hook_event in LIGHTWEIGHT_HOOKS:
        _record_hook_event(hook_event, hook_input)
        dbg(f"recorded lightweight event {hook_event}")
        sys.exit(0)

    if not session_id and not agent_transcript_path:
        dbg("exit: no session_id and no agent_transcript_path")
        sys.exit(0)

    # Subagent-type events: SubagentStop, TeammateIdle, TaskCompleted
    # All report on a child agent's lifecycle, not the main session
    # StopFailure (v2.1.78): fires when the turn ends due to an API error
    # (rate limit, auth failure, etc.). Still consumed tokens before failing.
    is_subagent_event = hook_event in ("SubagentStop", "TeammateIdle", "TaskCompleted")
    is_stop_event = hook_event in ("Stop", "StopFailure")

    # Step 1: Identity from parent transcript
    identity = {}
    if is_subagent_event and transcript_path:
        identity = extract_agent_identity(transcript_path, agent_id)
    if not identity.get("subagent_type") and agent_type:
        identity["subagent_type"] = agent_type

    # Step 2: Token usage from agent's own transcript
    # For Stop events, the hook fires before the current response is written to
    # the JSONL file. We retry with backoff until new assistant messages appear.
    # For subagent events (SubagentStop/TaskCompleted), parse the FULL agent
    # transcript — report the complete lifetime cost when the agent finishes.
    usage = {}  # type: dict
    max_retries = 6 if is_stop_event else 1
    retry_delay = 1.0  # seconds between retries for Stop events
    # Track whether we hit the SubagentStop fallback path (subagent transcript
    # missing — we end up parsing the whole main session which will look
    # absurdly large for a single "subagent"). Reported in the box so users
    # don't misread a session-sized bill as one subagent's cost.
    subagent_fallback = False

    for attempt in range(max_retries):
        if attempt > 0:
            dbg(
                f"retry {attempt}/{max_retries - 1},"
                f" waiting {retry_delay}s"
                f" for transcript flush..."
            )
            time.sleep(retry_delay)
            retry_delay = min(
                retry_delay * 1.5, 5.0
            )  # backoff: 1s, 1.5s, 2.25s, 3.4s, 5s

        if agent_transcript_path and Path(agent_transcript_path).exists():
            # Full lifetime parse — report everything
            # this agent did from start to finish
            dbg(f"parsing agent transcript: {agent_transcript_path}")
            usage = parse_agent_transcript(agent_transcript_path, session_id="")
        elif transcript_path:
            is_stop = is_stop_event
            dbg(f"parsing session transcript: {transcript_path} last_op_only={is_stop}")
            usage = parse_agent_transcript(
                transcript_path, session_id=session_id, last_op_only=is_stop
            )
            # For subagent-style events, falling back to the main transcript
            # means we're about to report the ENTIRE main session as if it
            # were one subagent. Flag it so build_report can warn the user.
            if is_subagent_event:
                subagent_fallback = True
        else:
            dbg("exit: no transcript path found")
            sys.exit(0)

        if usage["message_count"] > 0:
            break  # found messages, proceed
        dbg(f"attempt {attempt}: message_count=0")

    dbg(
        f"messages={usage['message_count']}"
        f" inp={usage['input_tokens']}"
        f" out={usage['output_tokens']}"
        f" cw={usage['cache_creation_input_tokens']}"
        f" cr={usage['cache_read_input_tokens']}"
    )
    dbg(f"tools={dict(usage.get('tools_used', {}))}")
    dbg(f"tools_tokens={dict(usage.get('tools_tokens', {}))}")

    if usage["message_count"] == 0:
        dbg("exit: message_count=0")
        sys.exit(0)

    # Step 2b: Discover and aggregate sub-agent tokens.
    # Worktree agents and orchestrators store sub-agent transcripts at
    # {transcript_stem}/subagents/agent-*.jsonl next to the parent transcript.
    # This captures the full cost of skills that spawn swarms in worktrees.
    active_transcript = agent_transcript_path or transcript_path
    sub_agents = discover_subagent_transcripts(active_transcript)
    sub_usage_list = []  # (info_dict, usage_dict) for report breakdown
    if sub_agents:
        dbg(f"found {len(sub_agents)} sub-agent transcripts under {active_transcript}")
        for sa in sub_agents:
            sa_usage = parse_agent_transcript(sa["path"], session_id="")
            if sa_usage["message_count"] > 0:
                sub_usage_list.append((sa, sa_usage))
                _merge_usage(usage, sa_usage)
                dbg(
                    f"  sub-agent {sa['agent_id'][:8]}: "
                    f"inp={sa_usage['input_tokens']} out={sa_usage['output_tokens']} "
                    f"msgs={sa_usage['message_count']}"
                )
        dbg(
            f"after merge: messages={usage['message_count']} "
            f"inp={usage['input_tokens']} out={usage['output_tokens']}"
        )

    # Step 3: Read lightweight session events recorded by other hooks.
    # These are events like InstructionsLoaded, PostCompact, TaskCreated,
    # FileChanged, CwdChanged, PermissionDenied — they fire too often to
    # emit reports directly, so we log them to disk and fold them into
    # the next Stop/SubagentStop report.
    session_events = _read_session_events(session_id)
    dbg(f"read {len(session_events)} session events")

    # Step 3b: Persist the full cache-events list to disk. The report box
    # can only fit top-5 by resent size, but users debugging cost leaks
    # need ALL N events with timestamps and causes. Writes to:
    #   ${CLAUDE_PLUGIN_DATA}/cache-events/<session_short>-<timestamp>.jsonl
    # One JSONL line per event, safe to diff / grep / analyse offline.
    cache_events = usage.get("cache_events", [])
    events_log_path = None
    if cache_events:
        try:
            events_dir = _plugin_data_dir() / "cache-events"
            events_dir.mkdir(parents=True, exist_ok=True)
            sid_label = session_id[:16] or (agent_id[:16] or "unknown")
            events_log_path = (
                events_dir / f"{sid_label}-{int(time.time())}-{hook_event}.jsonl"
            )
            with events_log_path.open("w", encoding="utf-8") as f:
                for ev in cache_events:
                    f.write(json.dumps(ev, default=str) + "\n")
            dbg(f"persisted {len(cache_events)} cache events to {events_log_path}")
        except OSError as e:
            dbg(f"failed to persist cache events: {e}")
            events_log_path = None

    # Step 4: Build report (pass sub_usage_list for summary in main box)
    report = build_report(
        hook_event,
        hook_input,
        usage,
        identity,
        sub_usage_list,
        session_events=session_events,
        subagent_fallback=subagent_fallback,
        events_log_path=str(events_log_path) if events_log_path else "",
    )
    dbg(f"report built, length={len(report)}")

    # Step 3b: Build dedicated worktree box if sub-agents were found.
    # Box 1 (above) shows the total. Box 2 shows the detailed per-agent
    # cache dynamics — cache invalidation, efficiency per agent, penalty cost.
    if sub_usage_list:
        # Orchestrator's own usage = total minus all sub-agents
        orch_usage = parse_agent_transcript(active_transcript, session_id="")
        wt_path = hook_input.get("cwd", active_transcript)
        wt_report = build_worktree_report(wt_path, orch_usage, sub_usage_list)
        report = report + "\n" + wt_report
        dbg(f"worktree report appended, total length={len(report)}")

    # Temp dir for saving subagent reports (collected by Stop hook).
    # session_id comes from hook_input and is untrusted: sanitize before
    # using it as a path component so values like "../../etc" or "a/b"
    # cannot escape the temp dir.
    safe_session_id = re.sub(r"[^A-Za-z0-9_-]", "_", session_id[:16])
    report_dir = Path(tempfile.gettempdir()) / "token-reporter" / safe_session_id

    if is_subagent_event:
        # Save this subagent/teammate/task report to a temp file for later collection
        report_dir.mkdir(parents=True, exist_ok=True)
        aid = agent_id[:8] if agent_id else "unknown"
        report_file = report_dir / f"subagent-{aid}-{int(time.time() * 1000)}.txt"
        report_file.write_text(report, encoding="utf-8")
        dbg(f"saved subagent report to {report_file}")
        # Still return systemMessage (it becomes a system context message for the AI)
        output = {"systemMessage": report}
        print(json.dumps(output))
        sys.exit(0)

    # Stop event: collect any saved subagent reports, prepend them, then show all
    subagent_reports = []
    if report_dir.exists():
        for f in sorted(report_dir.glob("subagent-*.txt")):
            try:
                subagent_reports.append(f.read_text(encoding="utf-8"))
                f.unlink()  # clean up after reading
            except OSError:
                pass
        # Remove the dir if empty
        try:
            report_dir.rmdir()
        except OSError:
            pass
    dbg(f"collected {len(subagent_reports)} subagent reports")

    # Combine: subagent reports first, then main session report
    all_reports = subagent_reports + [report]
    combined = "\n".join(all_reports)

    # Hook output character cap. Claude Code docs disagree:
    #   - Hooks reference (v2.1.101): "Output is capped at 10,000 characters"
    #   - Changelog v2.1.90: "Hook output >50K characters saved to disk"
    # We default to 10K (safest) but allow override via env var or the
    # CLAUDE_PLUGIN_OPTION_OUTPUT_LIMIT_CHARS user config value.
    def _resolve_output_limit() -> int:
        raw = (
            os.environ.get("TOKEN_REPORTER_OUTPUT_LIMIT_CHARS")
            or os.environ.get("CLAUDE_PLUGIN_OPTION_OUTPUT_LIMIT_CHARS")
            or ""
        )
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
        return 10_000

    MAX_HOOK_OUTPUT = _resolve_output_limit()
    if len(combined) > MAX_HOOK_OUTPUT:
        dbg(
            f"WARNING: output {len(combined)} chars exceeds {MAX_HOOK_OUTPUT}, "
            f"truncating to fit hook output limit"
        )
        # Keep the main session report (last entry), drop oldest subagent reports
        while len(combined) > MAX_HOOK_OUTPUT and len(all_reports) > 1:
            dropped = all_reports.pop(0)
            dbg(f"  dropped subagent report ({len(dropped)} chars)")
            combined = "\n".join(all_reports)
        # If still too large, hard-truncate
        if len(combined) > MAX_HOOK_OUTPUT:
            combined = combined[:MAX_HOOK_OUTPUT] + "\n[...truncated]"

    # Stop event successfully reported — clear the session event log so
    # the next session starts fresh.
    _clear_session_events(session_id)

    output = {"systemMessage": combined}
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
