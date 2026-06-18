# Changelog

## 0.2.0 (unreleased)

- Added Cursor support via `.cursor/hooks.json`, including command hooks and
  native prompt hook installation.
- Added a unified hook install mode:
  `command`, `native-prompt`, and `auto`.
- Added native prompt hook compilation for Claude Code and Cursor. Codex falls
  back to command hooks because it does not support the same prompt hook model.
- Updated package metadata and README for the cross-platform prompt-hook
  positioning.

## 0.1.0

Initial release.

- Natural-language rules in TOML/JSON, enforced via lifecycle hooks.
- Adapters: Claude Code (full tool coverage) and OpenAI Codex CLI
  (experimental hooks; Bash-only Pre/PostToolUse per upstream limits).
- Hybrid checking: deterministic `pattern_only` rules + batched LLM-as-judge
  with regex scoping and verdict caching. Judge backends: API providers
  (`anthropic`, `openai`) and subscription CLI providers (`claude-cli`,
  `codex-cli`) that shell out to a local `claude`/`codex` login — no API key
  required, with a re-entrancy guard against recursive hook triggering.
- Violation feedback loop: deny with rule restatement (pre_tool_use), replace
  tool result (post_tool_use), force continuation (stop, loop-guarded),
  context injection or block (user_prompt_submit).
- `init` (with bundled presets: security, git-discipline, done-discipline),
  `check`, `install` (idempotent merge), `hook`, `test` (incl. offline mock mode).
- JSONL audit log, fail-open/fail-closed modes, zero runtime dependencies.
