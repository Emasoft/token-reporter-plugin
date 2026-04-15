# Changelog

All notable changes to this project will be documented in this file.

## [1.9.0] - 2026-04-15


### Features

- Standalone Skills box, per-section truncation, HTML report


### Miscellaneous

- Sync uv.lock to 1.8.1

## [1.8.1] - 2026-04-15


### Bug Fixes

- Harden against scan-identified bugs
- Cross-platform launcher and bumper hardening
- Tighten release pipeline against latent failures


### Miscellaneous

- Tighten hooks.json, README, and CI workflow
- Sync uv.lock to 1.8.0

## [1.8.0] - 2026-04-15


### Bug Fixes

- Credit skills on all three result paths


### Features

- Track per-skill and per-agent-type token costs


### Miscellaneous

- Sync uv.lock

## [1.7.2] - 2026-04-15


### Bug Fixes

- Correct output_tokens under-count from streaming chunks

## [1.7.1] - 2026-04-13


### Bug Fixes

- Add required userConfig.title field to plugin.json
- Track uv.lock for reproducible builds (CPV requirement)

## [1.7.0] - 2026-04-12


### Bug Fixes

- Rename bin/token-report to bin/token-report.sh for CPV compliance
- Use Python wrapper for bin/ helper (CPV cross-platform rule)


### Features

- Support Claude Code 2.1.90-2.1.101 events and on-demand report

## [1.6.0] - 2026-04-10


### Features

- Pre-push hook uses process ancestry (not env vars) for publish gate

## [1.5.0] - 2026-04-10


### Features

- Pre-push hook refuses direct pushes to main (publish.py gate)

## [1.4.0] - 2026-04-10


### Changes

- Harden publish.py: all quality gates mandatory and unskippable
- Add uv.lock to .gitignore


### Features

- Use git-cliff --bumped-version for auto version and canonical changelog

## [1.3.1] - 2026-04-02


### Changes

- Adapt JSONL parser for Claude Code v2.1.85 format changes
- Add cache invalidation event detection with per-event reporting
- Detect file watcher and Bash side-effect cache invalidations
- Add StopFailure hook support from v2.1.78 changelog
- Release v1.3.0
- Remove unused variable o_cw flagged by ruff
- Format token-reporter.py with ruff
- Add CPV remote validation gate to publish.py
- Fix CPV gate to block only on CRITICAL/MAJOR (MINOR passes)
- Fix all CPV validation issues — 0 errors across all 190+ rules
- Guard against v2.1.89 hook output >50K limit, fix _merge_usage
- Clean up repo structure and update README for v1.3.0
- Release v1.3.1

## [1.2.3] - 2026-03-25


### Changes

- Clarify plugin name vs repo name in README
- Add Serena project config (track all files, no internal .gitignore)
- Update .gitignore with TLDR, Claude worktrees, rechecker, LLM externalizer
- Release v1.2.3

## [1.2.2] - 2026-03-15


### Changes

- Fix notify-marketplace to send plugin name from plugin.json
- Release v1.2.2

## [1.2.1] - 2026-03-15


### Changes

- Release v1.2.1

## [1.2.0] - 2026-03-15


### Changes

- Initial commit: token-reporter plugin
- Show per-operation token usage with ANSI yellow highlight
- Fix Stop hook timing: sleep 3s for transcript flush + per-tool token tracking
- Skip tool_result entries when finding last user prompt
- Fix box alignment: replace ambiguous-width chars with ASCII
- Remove all emoji from report - use plain ASCII labels
- Fix box alignment: box drawing chars (U+2500-U+259F) are 1-wide not 2
- Add bright ANSI colors for dark terminals, separate tool name from count
- Color hierarchy: bright=dynamic values, dim=labels/constant text
- Unified color scheme: all static text matches border (dark gray)
- Change static text color from dark gray to blue
- Unified blue for border+labels, bright green for cost
- Unify all token values to bright yellow, session hash to cyan
- Tool counts to bright magenta, session hash keeps cyan
- Add agent_type to SubagentStop header, collect subagent reports in Stop
- Add TeammateIdle/TaskCompleted hooks, update for Claude Code 2.1.69
- Fix Haiku 3.5 pricing (was using Haiku 3.0 prices), add Haiku 3.0
- Fix transcript flush race: retry loop instead of fixed 3s sleep
- Add tool result→input attribution and cache-write/read labels
- Show all tool result→input sizes, no minimum threshold
- Use tiktoken cl100k_base for accurate tool result token counts
- Use uv run --with tiktoken instead of global pip install
- Warn on stderr if tiktoken is not available
- Clarify tiktoken warning: advise running via uv
- Rewrite README with full configuration and usage documentation
- Update README: emasoft-plugins marketplace as primary install method
- Add rate limit bars (5h/7d) with delta to Stop report
- Shared usage cache between statusline and token-reporter (120s TTL)
- Make token-reporter fully independent: own usage cache
- Bump usage cache TTL to 300s (5min) — /api/oauth/usage is heavily rate-limited
- Remove rate limit bars (OAuth API too unreliable for plugin use)
- Separate MCP tools into columnar section to fix box width
- MCP tools: show full names vertically, one per line
- List read files when no edited/written files exist
- Incremental reporting via watermarks for subagent events
- Full lifetime reporting for completed agents, no list truncation
- Enrich token report with duration, cache %, bash/web, task text
- Only show token reports when claude --debug is active
- Add marketplace integration workflow and update plugin metadata
- Add publish pipeline and pre-push quality gate
- Fix ruff E701 lint errors in token-reporter.py
- Update README with all v1.2.0 features
- Release v1.2.0


