# Contributing to rulehook

Thanks for your interest! rulehook is intentionally small and zero-dependency.
Please keep contributions in that spirit.

## Ground rules

- **Zero runtime dependencies.** The hook entrypoint must run on a bare
  Python 3.11+ install. Test/dev tooling may use dependencies; runtime may not.
- **Never break the agent.** The `hook` command must exit 0 on any internal
  failure (fail-open) unless the user configured `fail_open = false`.
- **One adapter per platform.** Platform quirks live in `rulehook/adapters/`,
  not in the engine.

## Dev setup

```bash
git clone <your-fork>
cd rulehook
PYTHONPATH=. python -m unittest discover -s tests -v
```

Mock mode lets you exercise the full pipeline offline:

```bash
RULEHOOK_MOCK=1 PYTHONPATH=. python -m rulehook.cli test \
  --event PreToolUse --tool Bash --command "cat .env" \
  --rules rulehook/presets/security.toml
```

## Adding a platform adapter

1. Create `rulehook/adapters/<platform>.py` with `parse(payload) -> AgentEvent|None`,
   `render(event, verdict) -> (dict, int)`, and `install(project_dir, rules_path, scope)`.
2. Register it in `rulehook/adapters/__init__.py`.
3. Add rendering + install tests in `tests/test_adapters.py`.
4. Document the platform's documented limits in the module docstring (be honest
   about what the hook can and cannot intercept).

## Adding a preset

Presets are plain rules files in `rulehook/presets/<name>.toml`. They must pass
`rulehook check`, prefer `pattern_only` deterministic rules as the floor with
semantic rules on top, and include a comment header explaining the pack's intent.

## Pull requests

- Include tests for behavior changes.
- Update README/CHANGELOG when user-facing behavior changes.
- CI must be green (unit tests on 3.11–3.13, preset validation, e2e smoke).
