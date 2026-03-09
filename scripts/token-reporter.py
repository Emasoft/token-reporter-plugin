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
import time
import tempfile
from pathlib import Path
from collections import defaultdict, Counter

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
            print("[token-reporter] WARNING: tiktoken not found — token counts will be approximate. Run via 'uv run --with tiktoken' to get exact counts.", file=sys.stderr)
    if _tokenizer is not None:
        # encode_ordinary skips special token handling — ~30% faster than encode
        return len(_tokenizer.encode_ordinary(text))
    return len(text) // 4


# ─────────────────────────────────────────────
# Pricing
# ─────────────────────────────────────────────
MODEL_PRICING = {
    "claude-opus-4-6":   {"input": 5.0,  "output": 25.0, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5.0,  "output": 25.0, "cache_write": 6.25,  "cache_read": 0.50},
    "claude-sonnet-4-5": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_write": 1.25,  "cache_read": 0.10},
    "claude-sonnet-4":   {"input": 3.0,  "output": 15.0, "cache_write": 3.75,  "cache_read": 0.30},
    "claude-opus-4":     {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-opus-4-1":   {"input": 15.0, "output": 75.0, "cache_write": 18.75, "cache_read": 1.50},
    "claude-haiku-3-5":  {"input": 0.80, "output": 4.0,  "cache_write": 1.00,  "cache_read": 0.08},
    "claude-haiku-3":    {"input": 0.25, "output": 1.25, "cache_write": 0.30,  "cache_read": 0.03},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30}


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
    s = re.sub(r'-\d{8}$', '', model)
    s = re.sub(r'^claude-', '', s)
    return s


def trunc(text: str, max_len: int = 100) -> str:
    if not text:
        return "—"
    text = text.replace("\n", " ").replace("|", "\\|").strip()
    return text if len(text) <= max_len else text[:max_len - 1] + "…"


# ─────────────────────────────────────────────
# JSONL helpers
# ─────────────────────────────────────────────

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
                    last_user_ctx = " ".join(
                        b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text"
                    )

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
            if block.get("type") != "tool_use" or block.get("name") != "Task":
                continue

            ti = block.get("input", {})
            if not isinstance(ti, dict):
                continue

            tool_use_id = block.get("id", "")
            matched = _match_task(entries, i, tool_use_id, agent_id)

            if matched or not identity["task_description"]:
                identity["task_description"] = ti.get("description", "")
                identity["task_prompt"] = ti.get("prompt", "")
                identity["subagent_type"] = ti.get("subagent_type", "")
                identity["requested_model"] = ti.get("model", "")
                identity["run_in_background"] = ti.get("run_in_background", False)
                identity["spawning_skill"] = _detect_skill(last_user_ctx, ti.get("prompt", ""))

            if matched:
                return identity

    return identity


def _match_task(entries, start, tool_use_id, agent_id):
    if not agent_id or not tool_use_id:
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
    skip = {"compact", "clear", "hooks", "model", "cost", "context", "resume",
            "help", "config", "plan", "build", "review", "status", "memory",
            "path", "tmp", "home", "usr", "etc", "var", "bin", "dev", "mnt"}

    for pat in re.findall(r'/([a-z][a-z0-9_-]+)', combined):
        if pat not in skip and len(pat) > 2:
            return f"/{pat}"

    m = re.search(r'(?:using skill|skill[:\s]+)([a-z][a-z0-9_-]+)', combined)
    if m:
        return f"skill:{m.group(1)}"

    m = re.search(r'\.claude/agents/([a-z][a-z0-9_-]+)\.md', combined)
    if m:
        return f"agent:{m.group(1)}"

    m = re.search(r'\.claude/skills/([a-z][a-z0-9_-]+)', combined)
    if m:
        return f"skill:{m.group(1)}"

    return ""


# ─────────────────────────────────────────────
# Agent transcript parsing
# ─────────────────────────────────────────────

def parse_agent_transcript(path: str, session_id: str, last_op_only: bool = False) -> dict:
    r = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        "message_count": 0,
        "models_used": defaultdict(lambda: {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "message_count": 0,
        }),
        "tools_used": Counter(),
        "tools_tokens": defaultdict(lambda: {"input": 0, "output": 0, "result_tokens": 0}),
        "files_read": set(), "files_written": set(), "files_edited": set(),
        "bash_commands": [], "web_fetches": [],
    }
    seen = set()

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

        # ── Process user entries: scan tool_result blocks for content size ──
        if etype == "user":
            msg = entry.get("message", {})
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    # Match this tool_result back to its originating tool
                    tuid = block.get("tool_use_id", "")
                    tool_name = tool_use_id_map.get(tuid, "")
                    if not tool_name:
                        continue
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
                for f in ["input_tokens", "output_tokens",
                           "cache_creation_input_tokens", "cache_read_input_tokens"]:
                    v = u.get(f, 0)
                    r[f] += v
                    r["models_used"][model][f] += v
                r["message_count"] += 1
                r["models_used"][model]["message_count"] += 1

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


def build_report(hook_event: str, hook_input: dict, usage: dict, identity: dict) -> str:
    """Build a compact unicode-bordered report for terminal display."""
    is_sub = hook_event in ("SubagentStop", "TeammateIdle", "TaskCompleted")
    # Show the hook event type as the label for teammate/task events
    label_map = {"SubagentStop": "Subagent", "TeammateIdle": "Teammate", "TaskCompleted": "Task"}
    label = label_map.get(hook_event, "Session")
    agent_id = hook_input.get("agent_id", hook_input.get("session_id", ""))
    short_id = agent_id[:8] if agent_id else "?"
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
    all_top_tools = (tools.most_common(20) if hasattr(tools, 'most_common')
                     else sorted(tools.items(), key=lambda x: -x[1])[:20])
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
    S = "\033[94m"          # static - bright blue (border, labels, all non-changing text)
    Y = "\033[93m"          # bright yellow - ALL token values (unified)
    C = "\033[92m"          # bright green - cost values
    G = "\033[95m"          # bright magenta - tool counts
    H = "\033[96m"          # bright cyan - session hash
    W = "\033[97m"          # bright white - model, msg count, tool names
    R = "\033[0m"           # reset

    # Header: static text same as border, dynamic values bright
    # For SubagentStop, show the agent type (e.g. "Subagent Explore a2c30deb")
    sub_type = identity.get("subagent_type", "") if is_sub else ""
    type_part = f" {W}{sub_type}{R}" if sub_type else ""
    rows.append(f"{S}{label}{R}{type_part} {H}{short_id}{R} {S}|{R} {W}{model_names}{R} {S}|{R} {W}{msgs}{R} {S}messages{R}")

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

    # Cost (bright cyan value, static label)
    rows.append(("Cost", f"{C}${total_cost:.2f}{R} {S}(this op){R}"))

    # Per-model breakdown (only if multiple real models)
    if len(real_models) > 1:
        for model, stats in real_models.items():
            c = estimate_cost(stats, model)
            mt = sum(stats[f] for f in ["input_tokens", "output_tokens",
                     "cache_creation_input_tokens", "cache_read_input_tokens"])
            rows.append((f"{S}  L {shorten_model(model)}{R}", f"{Y}{fmt_tok(mt)}{R} {S}tokens /{R} {C}${c:.2f}{R}"))

    # Regular tools with per-tool token attribution
    tools_tokens = usage.get("tools_tokens", {})
    if regular_tools:
        tool_str = f" {S}/{R} ".join(f"{W}{t}{R} {G}x{c}{R}" for t, c in regular_tools[:6])
        if len(regular_tools) > 6:
            tool_str += f" {S}+{len(regular_tools) - 6} more{R}"
        rows.append(("Tools", tool_str))
        # Per-tool token breakdown for regular tools
        for t, c in regular_tools[:6]:
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
        rows.append(("MCP", f"{W}{len(mcp_tools_list)}{R} {S}tools{R} {S}/{R} {G}x{total_mcp_calls}{R} {S}calls{R}"))
        # Each MCP tool on its own line with full name and token breakdown
        for t, c in mcp_tools_list:
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
            rows.append(("", f"{S}  L{R} {W}{t}{R} {G}x{c}{R}{detail_str}"))

    # Files summary (counts bright, labels static)
    file_parts = []
    if fr: file_parts.append(f"{W}{len(fr)}{R} {S}read{R}")
    if fe: file_parts.append(f"{W}{len(fe)}{R} {S}edited{R}")
    if fw: file_parts.append(f"{W}{len(fw)}{R} {S}written{R}")
    if file_parts:
        rows.append(("Files", f" {S}/{R} ".join(file_parts)))

    # List edited files
    for f in fe[:5]:
        rows.append(("", f"  {S}*{R} {W}{f}{R}"))
    if len(fe) > 5:
        rows.append(("", f"  {S}+{len(fe) - 5} more{R}"))

    # List written files
    for f in fw[:3]:
        rows.append(("", f"  {S}+{R} {W}{f}{R}"))
    if len(fw) > 3:
        rows.append(("", f"  {S}+{len(fw) - 3} more{R}"))

    # List read files (only if no edited/written files are listed, to avoid clutter)
    if not fe and not fw:
        for f in fr[:5]:
            rows.append(("", f"  {S}·{R} {W}{f}{R}"))
        if len(fr) > 5:
            rows.append(("", f"  {S}+{len(fr) - 5} more{R}"))

    # Subagent task
    if is_sub:
        task = identity.get("task_description", "")
        if task:
            rows.append(("Task", trunc(task, 60)))

    # ── Render unicode table ──
    # Emoji and CJK characters are 2 columns wide in terminals, but len() counts
    # them as 1 (or more for compound emoji like ✏️ which is 2 codepoints).
    # We need display-width-aware padding.

    import unicodedata

    def _char_width(c: str) -> int:
        """Terminal display width of a single character."""
        cp = ord(c)
        # Box drawing (U+2500-U+257F) and block elements (U+2580-U+259F)
        # are always 1 column wide despite being category So
        if 0x2500 <= cp <= 0x259F:
            return 1
        cat = unicodedata.category(c)
        if cat.startswith('M') or cat == 'Cf':  # Mark or Format
            return 0
        ea = unicodedata.east_asian_width(c)
        if ea in ('F', 'W'):  # Fullwidth or Wide
            return 2
        # Most emoji not caught by east_asian_width: check Unicode category
        # Emoji modifiers, symbols, pictographs are typically 2-wide
        if cat.startswith('So'):  # Symbol, Other (covers most emoji)
            return 2
        return 1

    def _strip_ansi(s: str) -> str:
        """Remove ANSI escape sequences before measuring display width."""
        return re.sub(r'\033\[[0-9;]*m', '', s)

    def dw(s: str) -> int:
        """Display width of a string in terminal columns."""
        return sum(_char_width(c) for c in _strip_ansi(s))

    def pad(s: str, width: int) -> str:
        """Left-align string s to exactly `width` terminal columns."""
        cur = dw(s)
        if cur >= width:
            return s
        return s + ' ' * (width - cur)

    header = rows[0]  # first element is the header string
    data_rows = rows[1:]  # rest are (label, value) tuples

    # Calculate column widths using display width
    max_label = max((dw(r[0]) for r in data_rows if isinstance(r, tuple) and r[0]), default=12)
    max_val = max((dw(r[1]) for r in data_rows if isinstance(r, tuple)), default=20)
    max_label = max(max_label, 12)
    max_val = max(max_val, 20)
    inner_w = max_label + 3 + max_val  # 3 = " │ " separator
    inner_w = max(inner_w, dw(header))

    # Use S for all border and label chrome (must match S defined above)
    # (S and R already defined above in the color palette)

    lines = []
    lines.append("")  # newline to avoid box top being broken by preceding text
    lines.append(f"{S}╭{'─' * (inner_w + 2)}╮{R}")
    lines.append(f"{S}│{R} {pad(header, inner_w)} {S}│{R}")
    lines.append(f"{S}├{'─' * (inner_w + 2)}┤{R}")

    for row in data_rows:
        if isinstance(row, tuple):
            lbl, val = row
            if lbl:
                line = f"{S}{pad(lbl, max_label)}{R} {S}│{R} {val}"
            else:
                line = f"{pad('', max_label)}   {val}"
            lines.append(f"{S}│{R} {pad(line, inner_w)} {S}│{R}")
        else:
            lines.append(f"{S}│{R} {pad(str(row), inner_w)} {S}│{R}")

    lines.append(f"{S}╰{'─' * (inner_w + 2)}╯{R}")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def dbg(msg: str):
    """Debug log to stderr — visible in ~/.claude/debug/ when debug mode is on."""
    print(f"[token-reporter] {msg}", file=sys.stderr)


def main():
    dbg("hook invoked")
    try:
        hook_input = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        dbg(f"JSON parse error: {e}")
        sys.exit(1)

    dbg(f"hook_event={hook_input.get('hook_event_name')} session={hook_input.get('session_id','')[:8]}")

    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")
    hook_event = hook_input.get("hook_event_name", "unknown")
    agent_id = hook_input.get("agent_id", "")
    agent_type = hook_input.get("agent_type", "")
    agent_transcript_path = hook_input.get("agent_transcript_path", "")

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
    usage = {}  # type: dict
    max_retries = 6 if hook_event == "Stop" else 1
    retry_delay = 1.0  # seconds between retries for Stop events

    for attempt in range(max_retries):
        if attempt > 0:
            dbg(f"retry {attempt}/{max_retries-1}, waiting {retry_delay}s for transcript flush...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 5.0)  # backoff: 1s, 1.5s, 2.25s, 3.4s, 5s

        if agent_transcript_path and Path(agent_transcript_path).exists():
            dbg(f"parsing agent transcript: {agent_transcript_path}")
            usage = parse_agent_transcript(agent_transcript_path, session_id="")
        elif transcript_path:
            is_stop = hook_event == "Stop"
            dbg(f"parsing session transcript: {transcript_path} last_op_only={is_stop}")
            usage = parse_agent_transcript(transcript_path, session_id=session_id, last_op_only=is_stop)
        else:
            dbg("exit: no transcript path found")
            sys.exit(0)

        if usage["message_count"] > 0:
            break  # found messages, proceed
        dbg(f"attempt {attempt}: message_count=0")

    dbg(f"messages={usage['message_count']} inp={usage['input_tokens']} out={usage['output_tokens']} cw={usage['cache_creation_input_tokens']} cr={usage['cache_read_input_tokens']}")
    dbg(f"tools={dict(usage.get('tools_used', {}))}")
    dbg(f"tools_tokens={dict(usage.get('tools_tokens', {}))}")

    if usage["message_count"] == 0:
        dbg("exit: message_count=0")
        sys.exit(0)

    # Step 3: Build report
    report = build_report(hook_event, hook_input, usage, identity)
    dbg(f"report built, length={len(report)}")

    # SubagentStop systemMessage is not rendered to the terminal by Claude Code,
    # so we save subagent reports to temp files. The Stop hook then collects
    # and displays them all together with the main session report.
    report_dir = Path(tempfile.gettempdir()) / "token-reporter" / session_id[:16]

    if is_subagent_event:
        # Save this subagent/teammate/task report to a temp file for later collection
        report_dir.mkdir(parents=True, exist_ok=True)
        aid = agent_id[:8] if agent_id else "unknown"
        report_file = report_dir / f"subagent-{aid}-{int(time.time()*1000)}.txt"
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
