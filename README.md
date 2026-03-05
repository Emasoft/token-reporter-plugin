# token-reporter

A Claude Code plugin that displays per-operation token usage when agents and subagents complete.

## What it does

After each Claude Code response (Stop), subagent completion (SubagentStop), teammate pause (TeammateIdle), or task completion (TaskCompleted), a compact unicode-bordered report is shown in the terminal:

```
╭────────────────────────────────────────────────────────────╮
│ Subagent Explore ae3d16d9 | haiku-4-5 | 1 messages         │
├────────────────────────────────────────────────────────────┤
│ Tokens       │ 34.8K input / 114 output                    │
│ Cost         │ $0.04 (this op)                              │
│ Tools        │ Bash x1                                      │
│                L Bash x1: 114 output                        │
╰────────────────────────────────────────────────────────────╯
╭────────────────────────────────────────────────────────────╮
│ Session 2779c422 | opus-4-6 | 15 messages                  │
├────────────────────────────────────────────────────────────┤
│ Tokens       │ 367.5K input / 1.1K output                  │
│                L cache-read: 528.5K                         │
│ Cost         │ $2.59 (this op)                              │
│ Tools        │ Bash x12 / ToolSearch x1 / Agent x1         │
│                L Bash x12: 2.0K output                      │
│                L ToolSearch x1: 251 output                  │
│                L Agent x1: 143 output                       │
│ Files        │ 1 read / 1 edited                            │
│                * scripts/token-reporter.py                  │
╰────────────────────────────────────────────────────────────╯
```

## Features

- **Per-operation tokens** -- reports only the tokens consumed since the last user prompt (not cumulative session totals)
- **Actual consumption** -- shows `fresh_input + cache_write` as primary input (the tokens that count against rate limits), with cache-read shown separately
- **Subagent reports** -- each subagent, teammate, or task gets its own report box with agent type/name (e.g., "Subagent Explore", "Teammate", "Task"), model used, tools invoked, and files touched
- **Per-tool token attribution** -- how many output tokens each tool consumed
- **Cost estimates** -- based on published Anthropic API pricing (useful for comparing model efficiency, even on Pro Max plans where billing is flat-rate)
- **Color-coded terminal output** -- bright yellow for token values, green for cost, magenta for tool counts, cyan for session hash, blue for all static text and borders
- **Multi-model breakdown** -- if multiple models were used in one operation, shows per-model token/cost split

## Supported models

- Claude Opus 4.6, 4.5, 4.1, 4
- Claude Sonnet 4.6, 4.5, 4
- Claude Haiku 4.5, 3.5

## Installation

Install as a Claude Code plugin via the local marketplace:

```bash
# Clone into your plugins directory
mkdir -p ~/.claude/plugins/marketplaces/local-marketplace/plugins/
cp -r token-reporter ~/.claude/plugins/marketplaces/local-marketplace/plugins/token-reporter
```

Then enable it in Claude Code settings (`enabledPlugins`). Use `/reload-plugins` to activate changes without restarting.

## Plugin structure

```
token-reporter/
  .claude-plugin/
    plugin.json          # Plugin manifest
  hooks/
    hooks.json           # Hook configuration (Stop, SubagentStop, TeammateIdle, TaskCompleted)
  scripts/
    token-reporter.py    # Main hook script
```

## How it works

1. **SubagentStop / TeammateIdle / TaskCompleted** hooks fire when a child agent completes or pauses -- the script parses the agent's transcript, builds a report, and saves it to a temp file (`/tmp/token-reporter/{session}/`)
2. **Stop** hook fires when the main session responds -- the script collects any saved child agent reports, parses the main session transcript (only entries since the last user prompt), and displays all reports together via `systemMessage`

This two-phase approach is needed because Claude Code only renders `systemMessage` to the terminal for Stop events, not child agent events.

## Configuration

The plugin uses four hooks defined in `hooks/hooks.json`:

- **Stop** -- main session response complete
- **SubagentStop** -- subagent (Explore, Plan, etc.) finished
- **TeammateIdle** -- teammate agent paused/waiting
- **TaskCompleted** -- background task finished

### Debug mode

Enable Claude Code debug mode to see detailed stderr logs from the hook:

```
[token-reporter] hook invoked
[token-reporter] hook_event=Stop session=2779c422
[token-reporter] waiting 3s for transcript flush...
[token-reporter] parsing session transcript: ... last_op_only=True
[token-reporter] messages=15 inp=367500 out=1100 cw=0 cr=528500
[token-reporter] collected 1 subagent reports
[token-reporter] report built, length=1234
```

## Requirements

- Python 3.8+
- Claude Code 2.1.69+ (for TeammateIdle/TaskCompleted support)

## License

MIT
