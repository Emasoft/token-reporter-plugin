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

import json
import sys
import re
import os
import time
import tempfile
import subprocess
from pathlib import Path
from collections import defaultdict, Counter


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
            print(
                "[token-reporter] WARNING: tiktoken not found — token counts will be approximate. Run via 'uv run --with tiktoken' to get exact counts.",
                file=sys.stderr,
            )
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
    for key, pricing in MODEL_PRICING.items():
        if key in model_name or model_name.startswith(key):
            return pricing
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
        results.append({
            "path": str(f),
            "agent_id": agent_id,
            "agent_type": meta.get("agentType", ""),
            "description": meta.get("description", ""),
        })
    return results


def _merge_usage(base: dict, add: dict):
    """Merge token usage from add into base (in place)."""
    for f in ["input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "message_count"]:
        base[f] = base.get(f, 0) + add.get(f, 0)
    # Merge models_used
    for model, stats in add.get("models_used", {}).items():
        if model not in base.get("models_used", {}):
            base.setdefault("models_used", {})[model] = {
                "input_tokens": 0, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                "message_count": 0,
            }
        for f in ["input_tokens", "output_tokens", "cache_creation_input_tokens",
                   "cache_read_input_tokens", "message_count"]:
            base["models_used"][model][f] += stats.get(f, 0)
    # Merge tools
    for tool, count in add.get("tools_used", {}).items():
        base.setdefault("tools_used", Counter())[tool] += count
    for tool, tok in add.get("tools_tokens", {}).items():
        if tool not in base.get("tools_tokens", {}):
            base.setdefault("tools_tokens", defaultdict(
                lambda: {"input": 0, "output": 0, "result_tokens": 0}
            ))
        for f in ["input", "output", "result_tokens"]:
            base["tools_tokens"][tool][f] += tok.get(f, 0)
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


def parse_jsonl(filepath: str):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
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
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
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
            if block.get("type") != "tool_use" or block.get("name") not in ("Task", "Agent"):
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
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") in ("Task", "Agent")):
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
    r = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "message_count": 0,
        "models_used": defaultdict(
            lambda: {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "message_count": 0,
            }
        ),
        "tools_used": Counter(),
        "tools_tokens": defaultdict(
            lambda: {"input": 0, "output": 0, "result_tokens": 0}
        ),
        "files_read": set(),
        "files_written": set(),
        "files_edited": set(),
        "bash_commands": [],
        "web_fetches": [],
        "first_timestamp": "",
        "last_timestamp": "",
        "cache_events": [],  # detected invalidation/TTL expiry events
    }
    seen = set()
    # State for cache invalidation detection
    _prev_ts = ""        # timestamp of previous assistant message
    _prev_cw = 0         # previous cache_creation
    _prev_cr = 0         # previous cache_read
    _recent_writes = []  # tool names that modify files since last assistant msg

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

    for entry in all_entries[start_index:]:
        etype = entry.get("type", "")

        # Track timestamps for duration calculation
        ts = entry.get("timestamp", "")
        if ts:
            if not r["first_timestamp"]:
                r["first_timestamp"] = ts
            r["last_timestamp"] = ts

        # ── Process user entries: scan tool_result blocks for content size ──
        if etype == "user":
            matched_tuids = set()  # track which tool_use_ids we already counted
            msg = entry.get("message", {})
            content = msg.get("content", [])
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
                    r["tools_tokens"][tool_name]["result_tokens"] += count_tokens(text)

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
                        r["tools_tokens"][tool_name]["result_tokens"] += count_tokens(text)
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

        if mid and mid not in seen:
            seen.add(mid)
            u = msg.get("usage", {})
            if u:
                model = msg.get("model", "unknown")
                for f in [
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ]:
                    v = u.get(f, 0)
                    r[f] += v
                    r["models_used"][model][f] += v
                r["message_count"] += 1
                r["models_used"][model]["message_count"] += 1

                # ── Cache invalidation detection ──
                # A spike in cache_creation with a drop in cache_read signals
                # that the cache was invalidated and the full context had to be
                # re-sent. Two main causes:
                #   1. TTL expiry (>5 min idle → "hey!" effect)
                #   2. File change (Edit/Write → loadChangedFiles() invalidates)
                cw = u.get("cache_creation_input_tokens", 0)
                cr = u.get("cache_read_input_tokens", 0)
                inp = u.get("input_tokens", 0)
                total_cache = cw + cr
                # Detect: cache_creation dominates (>80%) AND is substantial (>50K)
                is_spike = (
                    cw > 50_000
                    and total_cache > 0
                    and (cw / total_cache) > 0.80
                )
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
                    idle_secs = 0
                    if _prev_ts and ts:
                        try:
                            dt_prev = datetime.fromisoformat(
                                _prev_ts.replace("Z", "+00:00")
                            )
                            dt_now = datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            )
                            idle_secs = (dt_now - dt_prev).total_seconds()
                        except (ValueError, TypeError):
                            pass
                    event["idle_seconds"] = idle_secs
                    if idle_secs > 300:
                        event["cause"] = "ttl_expiry"
                    elif _recent_writes:
                        event["cause"] = "file_change"
                    elif _prev_cr > 0 and cr < _prev_cr * 0.1:
                        # Cache read collapsed vs previous message
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
                _prev_cw = cw
                _prev_cr = cr
                _recent_writes = []  # reset after processing

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
            # Track file-modifying tools for cache invalidation detection
            if tn in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                _recent_writes.append(tn)

        # Attribute this message's tokens to the tools it invoked
        u = msg.get("usage", {})
        if msg_tools and u:
            per_tool_out = u.get("output_tokens", 0) // len(msg_tools)
            per_tool_inp = u.get("input_tokens", 0) // len(msg_tools)
            for tn in msg_tools:
                r["tools_tokens"][tn]["output"] += per_tool_out
                r["tools_tokens"][tn]["input"] += per_tool_inp

    r["files_read"] = sorted(r["files_read"])
    r["files_written"] = sorted(r["files_written"])
    r["files_edited"] = sorted(r["files_edited"])
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


def build_report(hook_event: str, hook_input: dict, usage: dict, identity: dict,
                  sub_usage_list: list = None) -> str:
    """Build a compact unicode-bordered report for terminal display."""
    is_sub = hook_event in ("SubagentStop", "TeammateIdle", "TaskCompleted")
    # Show the hook event type as the label for teammate/task events
    label_map = {
        "SubagentStop": "Subagent",
        "TeammateIdle": "Teammate",
        "TaskCompleted": "Task",
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
    total_cost = sum(estimate_cost(s, m) for m, s in models_used.items())
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
    rows = []

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
    sub_type = identity.get("subagent_type", "") if is_sub else ""
    type_part = f" {W}{sub_type}{R}" if sub_type else ""
    rows.append(
        f"{S}{label}{R}{type_part} {H}{short_id}{R} {S}|{R} {W}{model_names}{R} {S}|{R} {W}{msgs}{R} {S}messages{R}{duration_str}"
    )

    # Primary tokens (bright yellow values, static labels)
    primary_input = inp + cw
    tok_val = f"{Y}{fmt_tok(primary_input)}{R} {S}input{R} {S}/{R} {Y}{fmt_tok(out)}{R} {S}output{R}"
    rows.append(("Tokens", tok_val))

    # Cache write — counted toward rate limits
    if cw > 0:
        rows.append(("", f"{S}  L cache-write (included):{R} {Y}{fmt_tok(cw)}{R}"))

    # Cache read — NOT counted toward rate limits
    if cr > 0:
        rows.append(("", f"{S}  L cache-read (excluded):{R} {Y}{fmt_tok(cr)}{R}"))

    # Cache efficiency — how much of total input came from cache
    total_input_all = inp + cw + cr
    if cr > 0 and total_input_all > 0:
        cache_pct = (cr / total_input_all) * 100
        rows.append(
            (
                "",
                f"{S}  L cache efficiency:{R} {Y}{cache_pct:.0f}%{R} {S}of input from cache{R}",
            )
        )

    # ── Cache invalidation events (prominent, red) ──
    RED = "\033[91m"
    cache_events = usage.get("cache_events", [])
    if cache_events:
        cause_labels = {
            "ttl_expiry": "TTL expired (idle >5m)",
            "file_change": "file changed → context resent",
            "context_change": "context changed",
            "cache_miss": "cache miss",
        }
        rows.append(
            ("", f"  {RED}⚠ {len(cache_events)} cache invalidation{'s' if len(cache_events) > 1 else ''} detected{R}")
        )
        total_penalty = sum(e.get("saved_if_cached", 0) for e in cache_events)
        if total_penalty > 0.001:
            rows.append(
                ("", f"  {RED}  penalty: ${total_penalty:.4f} wasted on context resend{R}")
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
                ("", f"  {RED}  #{ei+1} {fmt_tok(cw_tok)} resent{R} {S}— {cause}{idle_str}{tools_str}{R}")
            )

    # Cost — label reflects scope: "(lifetime)" for completed agents, "(this op)" for session
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
                f"{W}{len(mcp_tools_list)}{R} {S}tools{R} {S}/{R} {G}x{total_mcp_calls}{R} {S}calls{R}",
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
        sub_total_inp = sum(u["input_tokens"] + u["cache_creation_input_tokens"] for _, u in sub_usage_list)
        sub_total_out = sum(u["output_tokens"] for _, u in sub_usage_list)
        rows.append(
            ("Sub-agents",
             f"{W}{n_subs}{R} {S}spawned:{R} {Y}{fmt_tok(sub_total_inp)}{R} {S}in{R} {S}/{R} {Y}{fmt_tok(sub_total_out)}{R} {S}out{R}")
        )
        for sa_info, sa_usage in sub_usage_list:
            sa_type = sa_info.get("agent_type", "")
            sa_desc = sa_info.get("description", "")
            sa_label = sa_type or sa_desc or sa_info["agent_id"][:12]
            sa_inp = sa_usage["input_tokens"] + sa_usage["cache_creation_input_tokens"]
            sa_out = sa_usage["output_tokens"]
            sa_cost = sum(estimate_cost(s, m) for m, s in sa_usage.get("models_used", {}).items())
            cost_str = f" {C}${sa_cost:.4f}{R}" if sa_cost > 0 else ""
            rows.append(("", f"  {S}>{R} {W}{trunc(sa_label, 30)}{R} {Y}{fmt_tok(sa_inp)}{R}{S}/{R}{Y}{fmt_tok(sa_out)}{R}{cost_str}"))

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

    rows = []

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
        wt_label = wt_label[wt_label.index("/worktrees/"):]
    elif len(wt_label) > 50:
        wt_label = "..." + wt_label[-47:]

    rows.append(
        f"{S}Worktree{R} {H}{wt_label}{R} {S}|{R} "
        f"{W}{n_agents + 1}{R} {S}agents{R} {S}|{R} "
        f"{W}{total_msgs}{R} {S}msgs{R} {S}|{R} "
        f"{C}${total_cost:.2f}{R}"
    )

    # ── Totals ──
    rows.append(("Tokens", f"{Y}{fmt_tok(total_inp + total_cw)}{R} {S}input{R} {S}/{R} {Y}{fmt_tok(total_out)}{R} {S}output{R}"))
    if total_cw > 0:
        rows.append(("", f"{S}  L cache-write:{R} {Y}{fmt_tok(total_cw)}{R} {S}(billed at 1.25x){R}"))
    if total_cr > 0:
        rows.append(("", f"{S}  L cache-read:{R} {Y}{fmt_tok(total_cr)}{R} {S}(billed at 0.1x){R}"))

    # ── Cache efficiency overview ──
    if total_all_input > 0:
        cache_eff = (total_cr / total_all_input) * 100
        # Color the efficiency: green if good (>50%), yellow if ok (20-50%), red if bad (<20%)
        if cache_eff >= 50:
            eff_color = C
        elif cache_eff >= 20:
            eff_color = Y
        else:
            eff_color = RED
        rows.append(("Cache", f"{eff_color}{cache_eff:.0f}%{R} {S}of all input served from cache{R}"))

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
                rows.append(("", f"{S}  L invalidation penalty:{R} {RED}${penalty_cost:.4f}{R} {S}(cache-write that could be cache-read){R}"))

    # ── Orchestrator row ──
    o = orchestrator_usage
    o_inp = o.get("input_tokens", 0) + o.get("cache_creation_input_tokens", 0)
    o_out = o.get("output_tokens", 0)
    o_cw = o.get("cache_creation_input_tokens", 0)
    o_cr = o.get("cache_read_input_tokens", 0)
    o_all = o_inp + o_cr
    o_eff = f" {S}cache:{R} {Y}{(o_cr / o_all * 100):.0f}%{R}" if o_all > 0 and o_cr > 0 else ""
    o_cost = sum(estimate_cost(s, m) for m, s in o.get("models_used", {}).items())
    rows.append(("Orchestrator",
                 f"{Y}{fmt_tok(o_inp)}{R} {S}in{R} {S}/{R} {Y}{fmt_tok(o_out)}{R} {S}out{R}{o_eff} {C}${o_cost:.4f}{R}"))

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
            sa_cost = sum(estimate_cost(s, m) for m, s in sa_usage.get("models_used", {}).items())

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
            rows.append(("", f"    {Y}{fmt_tok(sa_inp)}{R} {S}in{R} {S}/{R} {Y}{fmt_tok(sa_out)}{R} {S}out{R}{cache_str} {C}${sa_cost:.4f}{R}"))
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
            "context_change": "context changed",
            "cache_miss": "cache miss",
        }
        total_penalty = sum(e.get("saved_if_cached", 0) for e in all_cache_events)
        rows.append(
            ("",
             f"  {RED}⚠ {len(all_cache_events)} cache invalidation{'s' if len(all_cache_events) > 1 else ''}{R}"
             + (f" {RED}— ${total_penalty:.4f} penalty{R}" if total_penalty > 0.001 else ""))
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
                ("", f"  {RED}  #{ei+1} {fmt_tok(cw_tok)} resent{R} {S}— {cause}{idle_str}{tools_str}{R}")
            )

    # ── Tool usage across all worktree agents ──
    combined_tools = Counter()
    for u in all_usages:
        for tool, count in u.get("tools_used", {}).items():
            combined_tools[tool] += count
    if combined_tools:
        regular = [(t, c) for t, c in combined_tools.most_common() if not t.startswith("mcp__")]
        mcp = [(t, c) for t, c in combined_tools.most_common() if t.startswith("mcp__")]
        if regular:
            tool_str = f" {S}/{R} ".join(f"{W}{t}{R} {G}x{c}{R}" for t, c in regular[:8])
            rows.append(("Tools", tool_str))
        if mcp:
            for t, c in mcp[:5]:
                rows.append(("", f"  {S}L{R} {W}{shorten_mcp_tool(t)}{R} {G}x{c}{R}"))

    return _render_box(rows)


def _render_box(rows: list) -> str:
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

    header = rows[0]
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

    # Only produce output when Claude Code is running with --debug
    if not _is_debug_mode():
        dbg("not in debug mode, skipping")
        sys.exit(0)

    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        dbg(f"JSON parse error: {e}")
        sys.exit(1)

    dbg(
        f"hook_event={hook_input.get('hook_event_name')} session={hook_input.get('session_id', '')[:8]}"
    )

    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")
    hook_event = hook_input.get("hook_event_name", "unknown")
    agent_type = hook_input.get("agent_type", "")
    agent_transcript_path = hook_input.get("agent_transcript_path", "")
    # v2.1.85: agentId removed from JSONL entries. Hook input may still
    # provide agent_id; if not, extract from agent_transcript_path filename
    # (pattern: agent-{agentId}.jsonl). Do NOT fall back to session_id —
    # multiple agents can share a session.
    agent_id = hook_input.get("agent_id", "")
    if not agent_id and agent_transcript_path:
        stem = Path(agent_transcript_path).stem  # "agent-{id}"
        if stem.startswith("agent-"):
            agent_id = stem[6:]

    if not session_id and not agent_transcript_path:
        dbg("exit: no session_id and no agent_transcript_path")
        sys.exit(0)

    # Subagent-type events: SubagentStop, TeammateIdle, TaskCompleted
    # All report on a child agent's lifecycle, not the main session
    is_subagent_event = hook_event in ("SubagentStop", "TeammateIdle", "TaskCompleted")

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
    max_retries = 6 if hook_event == "Stop" else 1
    retry_delay = 1.0  # seconds between retries for Stop events

    for attempt in range(max_retries):
        if attempt > 0:
            dbg(
                f"retry {attempt}/{max_retries - 1}, waiting {retry_delay}s for transcript flush..."
            )
            time.sleep(retry_delay)
            retry_delay = min(
                retry_delay * 1.5, 5.0
            )  # backoff: 1s, 1.5s, 2.25s, 3.4s, 5s

        if agent_transcript_path and Path(agent_transcript_path).exists():
            # Full lifetime parse — report everything this agent did from start to finish
            dbg(f"parsing agent transcript: {agent_transcript_path}")
            usage = parse_agent_transcript(agent_transcript_path, session_id="")
        elif transcript_path:
            is_stop = hook_event == "Stop"
            dbg(f"parsing session transcript: {transcript_path} last_op_only={is_stop}")
            usage = parse_agent_transcript(
                transcript_path, session_id=session_id, last_op_only=is_stop
            )
        else:
            dbg("exit: no transcript path found")
            sys.exit(0)

        if usage["message_count"] > 0:
            break  # found messages, proceed
        dbg(f"attempt {attempt}: message_count=0")

    dbg(
        f"messages={usage['message_count']} inp={usage['input_tokens']} out={usage['output_tokens']} cw={usage['cache_creation_input_tokens']} cr={usage['cache_read_input_tokens']}"
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

    # Step 3: Build report (pass sub_usage_list for summary in main box)
    report = build_report(hook_event, hook_input, usage, identity, sub_usage_list)
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

    # Temp dir for saving subagent reports (collected by Stop hook)
    report_dir = Path(tempfile.gettempdir()) / "token-reporter" / session_id[:16]

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

    output = {"systemMessage": combined}
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
