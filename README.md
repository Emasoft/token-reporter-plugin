# token-reporter

A Claude Code plugin that displays per-operation token usage when agents and subagents complete.

## What it reports

After each Claude Code response, a compact unicode-bordered report appears in the terminal showing:

- **Token counts** — fresh input + cache-write (what counts toward rate limits) and output
- **Cache breakdown** — cache-write (included in limits) and cache-read (excluded) shown separately
- **Per-tool attribution** — input, output, and result tokens for each tool used
- **Cost estimate** — based on published Anthropic API pricing
- **Agent identity** — agent type/name, model, message count
- **Files touched** — read, edited, written

```
╭────────────────────────────────────────────────────────────╮
│ Subagent Explore ae3d16d9 | haiku-4-5 | 3 messages         │
├────────────────────────────────────────────────────────────┤
│ Tokens       │ 34.8K input / 573 output                    │
│                L cache-write (included): 2.1K               │
│                L cache-read (excluded): 12.4K               │
│ Cost         │ $0.04 (this op)                              │
│ Tools        │ WebFetch x1 / Bash x2                        │
│                L WebFetch x1: 144 out / 85.2K result→input  │
│                L Bash x2: 429 out / 312 result→input        │
╰────────────────────────────────────────────────────────────╯
╭────────────────────────────────────────────────────────────╮
│ Session 2779c422 | opus-4-6 | 15 messages                  │
├────────────────────────────────────────────────────────────┤
│ Tokens       │ 367.5K input / 1.1K output                  │
│                L cache-write (included): 54.8K              │
│                L cache-read (excluded): 528.5K              │
│ Cost         │ $2.59 (this op)                              │
│ Tools        │ Bash x12 / Edit x3 / Read x2                │
│                L Bash x12: 2.0K out / 1.4K result→input    │
│                L Edit x3: 890 out / 245 result→input       │
│                L Read x2: 251 out / 6.3K result→input      │
│ Files        │ 2 read / 1 edited                            │
│                * scripts/token-reporter.py                  │
╰────────────────────────────────────────────────────────────╯
```

### Per-tool token breakdown explained

- **`in`** — input tokens attributed to the API call where the model invoked this tool
- **`out`** — output tokens the model generated to call this tool (the tool_use JSON block)
- **`result→input`** — tokens in the tool's result that got fed back as input on the next API turn (tokenized with tiktoken cl100k_base). This is where tools like WebFetch, Read, and Bash consume the most tokens

## Prerequisites

- **uv** — the script runs via `uv run --with tiktoken` to manage the tiktoken dependency automatically
- **Python 3.8+** — any Python 3.8+ accessible to uv
- **Claude Code 2.1.69+** — for TeammateIdle/TaskCompleted hook support (Stop and SubagentStop work on older versions)

Install uv if you don't have it:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Installation

### From the emasoft-plugins marketplace (recommended)

Add the marketplace to `~/.claude/settings.json` if you haven't already:

```json
{
  "pluginMarketplaces": [
    "Emasoft/emasoft-plugins"
  ]
}
```

Then enable the plugin:

```json
{
  "enabledPlugins": {
    "token-reporter@emasoft-plugins": true
  }
}
```

Restart Claude Code or run `/reload-plugins` to activate.

### Manual installation (development)

```bash
# Clone the repo
git clone https://github.com/Emasoft/emasoft-plugins.git /tmp/emasoft-plugins

# Copy the plugin to a local marketplace
mkdir -p ~/.claude/plugins/marketplaces/local-marketplace/plugins/
cp -r /tmp/emasoft-plugins/plugins/token-reporter ~/.claude/plugins/marketplaces/local-marketplace/plugins/token-reporter
```

Then enable in `~/.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "token-reporter@local-marketplace": true
  }
}
```

Restart Claude Code or run `/reload-plugins` to activate.

### Plugin directory structure

```
token-reporter/
  .claude-plugin/
    plugin.json          # Plugin manifest
  hooks/
    hooks.json           # Hook event → command mapping
  scripts/
    token-reporter.py    # Main hook script
```

### 4. Verify

Run any command in Claude Code. When the response completes, you should see the token report box in the terminal. Enable debug mode to see detailed logs:

```bash
claude --debug
```

Look for lines prefixed with `[token-reporter]` in stderr output (visible in `~/.claude/debug/`).

## How it works

The plugin registers four hook events in `hooks/hooks.json`:

| Hook Event | When it fires | What the script does |
|---|---|---|
| **Stop** | Main session response complete | Parses session transcript (since last user prompt), collects any saved subagent reports, displays all together |
| **SubagentStop** | Subagent (Explore, Plan, etc.) finished | Parses agent's transcript, saves report to `/tmp/token-reporter/{session}/` |
| **TeammateIdle** | Teammate agent paused/waiting | Same as SubagentStop |
| **TaskCompleted** | Background task finished | Same as SubagentStop |

**Why the temp file pattern?** Claude Code only renders `systemMessage` output to the terminal for Stop events. SubagentStop/TeammateIdle/TaskCompleted output is consumed as system context but not displayed. So the script saves child agent reports to temp files, and the Stop hook collects and displays them all together.

**Why the retry loop?** The Stop hook fires before the current response is fully written to the JSONL transcript file. The script retries up to 6 times with exponential backoff (1s → 5s) until assistant messages appear.

### Token attribution model

The script reads Claude Code's JSONL transcript files and tracks:

1. **Per-message usage** — `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` from each assistant message's `usage` field
2. **Per-tool output** — output tokens divided among tools in each assistant message
3. **Per-tool result→input** — the script matches `tool_use_id` from assistant messages to `tool_result` blocks in the following user messages, tokenizes the result content with tiktoken, and attributes those tokens back to the originating tool

### Rate limit accounting

- **Input counted toward limits**: `input_tokens` + `cache_creation_input_tokens`
- **NOT counted toward limits**: `cache_read_input_tokens`

The report labels these as `(included)` and `(excluded)` respectively.

## Hook command

Each hook runs:
```
uv run --with tiktoken python3 ${CLAUDE_PLUGIN_ROOT}/scripts/token-reporter.py
```

- `uv run --with tiktoken` provides the tiktoken dependency in a cached virtual environment (first run ~3s, subsequent runs ~3ms overhead)
- `${CLAUDE_PLUGIN_ROOT}` is expanded by Claude Code to the plugin's install directory
- The script reads hook input from stdin (JSON) and writes `{"systemMessage": "..."}` to stdout

If tiktoken is not available (e.g., running the script directly without uv), token counts fall back to a chars/4 estimate and a warning is printed to stderr.

## Supported models and pricing

| Model | Input $/M | Output $/M | Cache Write $/M | Cache Read $/M |
|---|---|---|---|---|
| Claude Opus 4.6 / 4.5 | $5.00 | $25.00 | $6.25 | $0.50 |
| Claude Opus 4.1 / 4 | $15.00 | $75.00 | $18.75 | $1.50 |
| Claude Sonnet 4.6 / 4.5 / 4 | $3.00 | $15.00 | $3.75 | $0.30 |
| Claude Haiku 4.5 | $1.00 | $5.00 | $1.25 | $0.10 |
| Claude Haiku 3.5 | $0.80 | $4.00 | $1.00 | $0.08 |
| Claude Haiku 3 | $0.25 | $1.25 | $0.30 | $0.03 |

Unknown models default to Sonnet pricing.

## Debug mode

Enable Claude Code debug mode to see detailed stderr logs:

```
[token-reporter] hook invoked
[token-reporter] hook_event=Stop session=2779c422
[token-reporter] retry 1/5, waiting 1.0s for transcript flush...
[token-reporter] parsing session transcript: ... last_op_only=True
[token-reporter] messages=15 inp=367500 out=1100 cw=0 cr=528500
[token-reporter] tools={'Bash': 12, 'Edit': 3, 'Read': 2}
[token-reporter] tools_tokens={'Bash': {'input': ..., 'output': ..., 'result_tokens': ...}, ...}
[token-reporter] collected 1 subagent reports
[token-reporter] report built, length=1234
```

## Color scheme

Designed for dark terminal backgrounds:

| Color | Used for |
|---|---|
| Bright blue | Borders, labels, all static text |
| Bright yellow | Token values |
| Bright green | Cost values |
| Bright magenta | Tool counts |
| Bright cyan | Session/agent hash |
| Bright white | Model names, tool names, file names |

## License

MIT
