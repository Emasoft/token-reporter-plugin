# token-reporter

<!--BADGES-START-->
<!--BADGES-END-->

A Claude Code plugin that displays per-operation token usage when agents and subagents complete. **Only outputs in debug mode** (`claude --debug`). Tracks **Claude Code 2.1.85–2.1.108**.

## What it reports

After each Claude Code response (in debug mode), a compact unicode-bordered report appears in the terminal showing:

- **Token counts** — fresh input + cache-write (what counts toward rate limits) and output
- **Cache breakdown** — cache-write (included in limits) and cache-read (excluded) shown separately
- **Cache efficiency** — percentage of total input that came from cache
- **Cache invalidation detection** — detects and flags cache busts with cause classification:
  - TTL expiry ("hey!" effect — idle >5 min, millions of tokens burned on resume)
  - File change (Edit/Write triggers `loadChangedFiles()`)
  - Bash side effect (linter/formatter modifies files → file watcher invalidates)
  - External change (file watcher detects non-Claude modifications)
  - Penalty cost and batching opportunity analysis
- **Compact boundary markers** — v2.1.90 ground-truth `system/compact_boundary` events with `preTokens` from auto-compaction
- **CLAUDE.md / rule reloads** — v2.1.101 `InstructionsLoaded` events, broken down by `load_reason` (session_start, nested_traversal, path_glob_match, include, **compact**) with red warning for compact-triggered reloads
- **PostCompact / PermissionDenied / TaskCreated** — v2.1.90+ lifecycle events rolled into the next Stop report
- **tool-results/ spillover** — v2.1.90 large tool outputs spilled to `~/.claude/projects/<project>/<session>/tool-results/` are credited to the originating tool
- **Worktree sub-agent breakdown** — dedicated box showing per-agent cache dynamics when skills spawn agent swarms in worktrees
- **Duration** — elapsed time from first to last message in the operation
- **Per-tool attribution** — input, output, and result tokens for each tool used
- **Per-skill attribution** — v2.1.108+ built-in slash commands route through the `Skill` tool. Each skill invocation shows invocation count, result→input bytes (the skill content loaded into context), output bytes, and an estimated cost per skill. Skills are sorted by cost so the biggest drains float to the top
- **Sub-agent aggregation by type** — when the same agent type is spawned multiple times (e.g. `Explore x5`), an aggregated row shows the total count, tokens, and cost per type in addition to the individual per-instance drill-down
- **Standalone Skills box** *(opt-in via `SKILLS_BOX`)* — when enabled, the per-skill breakdown is rendered in its own dedicated unicode box instead of as an inline section in the main report
- **Per-section truncation** *(`MAX_ENTRIES_PER_SECTION`, default 12)* — long lists (skills, bash, web, files, sub-agents) are capped to keep inline output under Claude Code's hook output limit, with a `⋯ +N more — see HTML report` indicator
- **Full HTML report (always on in debug mode)** — every Stop/SubagentStop event writes a self-contained nicely-formatted HTML file to `<project>/reports/token-reporter/<timestamp>-<event>-<session>.html` containing **every** section without truncation. The inline output ends with a `Full report: <path>` footer line so you can open the complete record in a browser. Add `reports/` (or `reports/token-reporter/`) to your project's `.gitignore` so the debug archive doesn't end up in commits.
- **Cost estimate** — based on published Anthropic API pricing, scoped to lifetime (agents) or current operation (session)
- **Agent identity** — agent type/name (via v2.1.101 `agent_id`/`agent_type` hook input fields), model, message count, duration
- **Bash commands** — every shell command executed (full list in HTML, capped inline)
- **Web fetches** — every URL fetched (full list in HTML, capped inline)
- **Files touched** — read, edited, and written files (full list in HTML, capped inline)

```
╭──────────────────────────────────────────────────────────────╮
│ Subagent Explore ae3d16d9 | haiku-4-5 | 3 messages | 12s     │
├──────────────────────────────────────────────────────────────┤
│ Tokens   │ 34.8K input / 573 output                          │
│            L cache-write (included): 2.1K                     │
│            L cache-read (excluded): 12.4K                     │
│            L cache efficiency: 36% of input from cache        │
│ Cost     │ $0.04 (lifetime)                                   │
│ Tools    │ WebFetch x1 / Bash x2                              │
│            L WebFetch x1: 144 out / 85.2K result→input        │
│            L Bash x2: 429 out / 312 result→input              │
│ Bash     │ 2 commands                                         │
│            $ git status                                       │
│            $ ls -la src/                                      │
│ Files    │ 3 read                                             │
│            · README.md                                        │
│            · src/index.ts                                     │
│            · package.json                                     │
╰──────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────────────────────────────────────────────╮
│ Session 2779c422 | opus-4-6 | 15 messages | 2m34s                            │
├──────────────────────────────────────────────────────────────────────────────┤
│ Tokens   │ 367.5K input / 1.1K output                                        │
│            L cache-write (included): 54.8K                                    │
│            L cache-read (excluded): 528.5K                                    │
│            L cache efficiency: 56% of input from cache                        │
│ Cost     │ $2.59 (this op)                                                    │
│ Tools    │ Bash x12 / Edit x3 / Read x2                                      │
│            L Bash x12: 2.0K out / 1.4K result→input                          │
│            L Edit x3: 890 out / 245 result→input                             │
│            L Read x2: 251 out / 6.3K result→input                            │
│ MCP      │ 3 tools / x10 calls                                               │
│            L chrome-devtools:take_screenshot x3: 2.1K result→input            │
│            L chrome-devtools:navigate_page x2: 89 out / 1.2K r→input          │
│            L grepika:search x5: 200 out / 3.1K result→input                   │
│            L total result→input: 97.1K                                        │
│ Bash     │ 12 commands                                                        │
│            $ git status                                                       │
│            $ npm test                                                         │
│            $ ...                                                              │
│ Web      │ 1 fetches                                                          │
│            → https://api.github.com/repos/...                                 │
│ Files    │ 2 read / 1 edited                                                  │
│            · README.md                                                        │
│            · src/index.ts                                                     │
│            * scripts/token-reporter.py                                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

### Per-tool token breakdown explained

- **`in`** — input tokens attributed to the API call where the model invoked this tool
- **`out`** — output tokens the model generated to call this tool (the tool_use JSON block)
- **`result→input`** (or **`r→in`** for MCP tools) — tokens in the tool's result that got fed back as input on the next API turn (tokenized with tiktoken cl100k_base). This is where tools like WebFetch, Read, and Bash consume the most tokens

### MCP tools section

MCP tool names (e.g. `mcp__chrome-devtools__take_screenshot`) are shortened to `server:tool` format (e.g. `chrome-devtools:take_screenshot`) for display:

- **`MCP`** row shows total tool count and call count
- Each tool listed below with shortened name, call count, and token breakdown
- Long lines wrap within the 80-column box with content-aligned continuation

## Prerequisites

- **uv** — the script runs via `uv run --with tiktoken` to manage the tiktoken dependency automatically
- **Python 3.8+** — any Python 3.8+ accessible to uv
- **Claude Code 2.1.85+** — for v2.1.85 JSONL format (agentId removed, toolUseResult.agentId added). StopFailure requires 2.1.78+. TeammateIdle/TaskCompleted require 2.1.69+. PostCompact/TaskCreated/PermissionDenied require 2.1.90+. InstructionsLoaded / `agent_id` / `agent_type` hook input fields require 2.1.101+. Lower versions silently ignore the hook registrations for events they don't support.

Install uv if you don't have it:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Naming

- **Plugin name**: `token-reporter` — this is the name in `plugin.json` and what you use with `claude plugin install`
- **GitHub repo**: [`Emasoft/token-reporter-plugin`](https://github.com/Emasoft/token-reporter-plugin) — where the source code lives

The plugin name and repo name are intentionally different. When installing or referencing the plugin, always use `token-reporter` (the plugin name), not `token-reporter-plugin` (the repo name).

## Installation

### From the emasoft-plugins marketplace (recommended)

```bash
claude plugin install token-reporter@emasoft-plugins
```

If you haven't added the marketplace yet:

```bash
claude plugin marketplace add Emasoft/emasoft-plugins
```

Then install:

```bash
claude plugin install token-reporter@emasoft-plugins
```

Restart Claude Code to activate.

### Alternative: manual settings.json

Add the marketplace and enable the plugin in `~/.claude/settings.json`:

```json
{
  "pluginMarketplaces": [
    "Emasoft/emasoft-plugins"
  ],
  "enabledPlugins": {
    "token-reporter@emasoft-plugins": true
  }
}
```

Restart Claude Code or run `/reload-plugins` to activate.

### Manual installation (development)

```bash
# Clone the plugin repo directly
git clone https://github.com/Emasoft/token-reporter-plugin.git /tmp/token-reporter-plugin

# Install from local path
claude plugin install /tmp/token-reporter-plugin/token-reporter
```

Or copy to a local marketplace:

```bash
mkdir -p ~/.claude/plugins/marketplaces/local-marketplace/plugins/
cp -r /tmp/token-reporter-plugin/token-reporter ~/.claude/plugins/marketplaces/local-marketplace/plugins/token-reporter
```

Then enable in `~/.claude/settings.json`:

```json
{
  "enabledPlugins": {
    "token-reporter@local-marketplace": true
  }
}
```

Restart Claude Code to activate.

### Plugin directory structure

```
token-reporter/
  .claude-plugin/
    plugin.json            # Plugin manifest (userConfig: OUTPUT_LIMIT_CHARS)
  .github/
    workflows/
      notify-marketplace.yml  # Auto-notify emasoft-plugins on version bump
  bin/
    token-report.py        # v2.1.91+ bin/ helper — on-demand report (cross-platform Python wrapper)
  hooks/
    hooks.json             # Hook event → command mapping (9 events)
  scripts/
    token-reporter.py      # Main hook script (~2400 LOC)
    bump_version.py        # Semver bumper for plugin.json
    publish.py             # Release pipeline (lint, CPV validation, bump, tag, push, gh release)
    pre-push               # Git pre-push quality gate (symlinked to .git/hooks/pre-push)
  pyproject.toml           # Python project metadata and tool config
  cliff.toml               # Changelog generation config (git-cliff)
```

### 4. Verify

The plugin **only outputs reports in debug mode**. Start Claude Code with:

```bash
claude --debug
```

Run any command. When the response completes, you should see the token report box in the terminal. Look for lines prefixed with `[token-reporter]` in stderr output (visible in `~/.claude/debug/`).

Without `--debug`, the hook exits immediately with no output.

## How it works

The plugin registers nine hook events in `hooks/hooks.json`. The first five produce reports; the remaining four are lightweight event-loggers that fold into the next Stop/SubagentStop report (so they don't spam the terminal).

**Report-emitting hooks:**

| Hook Event | When it fires | What the script does | Cost label |
|---|---|---|---|
| **Stop** | Main session response complete | Parses session transcript (since last user prompt), collects any saved subagent reports, displays all together | `(this op)` |
| **StopFailure** | Turn ends due to API error (rate limit, auth) | Same as Stop — tokens are still consumed before the error | `(this op)` |
| **SubagentStop** | Subagent (Explore, Plan, etc.) finished | Parses agent's full lifetime transcript | `(lifetime)` |
| **TeammateIdle** | Teammate agent paused/waiting | Same as SubagentStop | `(lifetime)` |
| **TaskCompleted** | Background task finished | Same as SubagentStop | `(lifetime)` |

**Lightweight event-log hooks** (v2.1.90+):

| Hook Event | Claude Code | Recorded to `${CLAUDE_PLUGIN_DATA}/events/` and surfaced next Stop |
|---|---|---|
| **InstructionsLoaded** | 2.1.101+ | `file_path`, `memory_type`, `load_reason` (incl. `compact`), `globs`, `trigger_file_path`, `parent_file_path` |
| **PostCompact** | 2.1.90+ | `preTokens`, `trigger`, `compactMetadata` |
| **TaskCreated** | 2.1.90+ | `agent_id`, `agent_type` for agents spawned in the session |
| **PermissionDenied** | 2.1.90+ | Permission-denial count surfaces as a red row in the report |

**Debug gate**: The hook first walks the process tree (`getppid()` → `ps -o args=`) checking for a parent `claude` process with `--debug` flag. If not found, the hook exits immediately with no output or processing. **Lightweight hooks still log events even when --debug is off**, so the next debug-mode Stop hook report includes the full history.

## On-demand report (v2.1.91+ bin/ helper)

The plugin ships `bin/token-report.py`, a Python wrapper that prints a report on demand without waiting for the Stop hook. Because v2.1.91+ adds `bin/` executables to the Bash tool's PATH while the plugin is enabled, you can ask Claude Code to run it directly:

```
Please run: token-report.py
```

Or from any shell inside the project:

```bash
cd /path/to/project && /path/to/plugin/bin/token-report.py
```

The helper reads the current session transcript (found via `${CLAUDE_PROJECT_DIR}` / newest JSONL under `~/.claude/projects/<slug>/`) and prints the same report format as the Stop hook, but to stdout as plain text instead of as a `systemMessage`. Useful for mid-session snapshots.

Python was chosen over Bash so the helper is cross-platform (macOS, Linux, Windows) as long as `uv` is on PATH. CPV (claude-plugins-validation) flags extensionless executables and `.sh` files as platform-specific; `.py` is recognized as cross-platform.

## User config (v2.1.90+)

The plugin exposes three `userConfig` entries in `plugin.json`. Each one is also overridable via a plain env var (prefix `TOKEN_REPORTER_`) for local development or older Claude Code versions that lack `userConfig` support.

| Key | Type | Default | Purpose |
|---|---|---|---|
| `OUTPUT_LIMIT_CHARS` | number | `10000` | Max characters injected into the transcript. The v2.1.101 hooks reference states 10,000; the v2.1.90 changelog says 50,000. Default is conservative. Raise via `CLAUDE_PLUGIN_OPTION_OUTPUT_LIMIT_CHARS` if your version supports more. |
| `SKILLS_BOX` | boolean | `false` | When `true`, the per-skill cost breakdown is rendered in its own dedicated unicode box instead of as an inline section in the main report. Useful for sessions with many skill invocations where the inline section would crowd the main box. |
| `MAX_ENTRIES_PER_SECTION` | number | `12` | Caps the number of entries shown per inline list section (skills, bash commands, web fetches, files, sub-agents). Lists exceeding this length show a `⋯ +N more — see HTML report` indicator. The full untruncated data is always available in the HTML report. Set to `0` to disable truncation entirely. |

Env var overrides (dev/local): `TOKEN_REPORTER_OUTPUT_LIMIT_CHARS`, `TOKEN_REPORTER_SKILLS_BOX`, `TOKEN_REPORTER_MAX_ENTRIES_PER_SECTION`. Boolean values accept `1/true/yes/on` and `0/false/no/off` (case-insensitive).

### HTML debug archive (always on)

When the hook fires (which it only does in `claude --debug` mode), it always writes a full HTML report containing **every** section without truncation to:

```
<project-cwd>/reports/token-reporter/<timestamp>-<event>-<session>.html
```

The path is appended to the inline output as a `Full report: <path>` footer line. The HTML uses an inline dark-theme CSS, summary cards, and per-section tables — open it in any browser, no external assets required.

**Add `reports/` (or `reports/token-reporter/`) to your project's `.gitignore`** so this debug archive doesn't end up in commits. The convention is shared across all of this author's plugins: every plugin saves its reports under `<project>/reports/<plugin-name>/`.

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

The plugin **requires** debug mode to produce any output. Start Claude Code with:

```bash
claude --debug
```

Detailed stderr logs (visible in `~/.claude/debug/`):

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

Without `--debug`, the hook detects the absence via process tree inspection and exits immediately (no transcript parsing, no output).

## Publishing

```bash
# Bump patch version, tag, push, create GitHub release
uv run scripts/publish.py

# Or specify bump level
uv run scripts/publish.py --minor
uv run scripts/publish.py --major
uv run scripts/publish.py --set 2.0.0

# Preview without changes
uv run scripts/publish.py --dry-run
```

The pre-push hook runs ruff lint and syntax checks before allowing pushes to main. Install it with:

```bash
ln -sf ../../scripts/pre-push .git/hooks/pre-push
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

## Links

- **Marketplace**: [Emasoft/emasoft-plugins](https://github.com/Emasoft/emasoft-plugins)
- **Repository**: [Emasoft/token-reporter-plugin](https://github.com/Emasoft/token-reporter-plugin)

## License

MIT
