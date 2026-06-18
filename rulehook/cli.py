"""rulehook CLI.

Subcommands:
  init      Write a starter rules file (.rulehook/rulehook.toml).
  install   Register rulehook into Claude Code, Codex, and/or Cursor hook configs.
  hook      Hook entrypoint (reads the event JSON on stdin). Used by the CLIs.
  check     Validate the rules file.
  test      Simulate an event from the command line (great with RULEHOOK_MOCK=1).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .adapters import ADAPTERS
from .engine import AgentEvent, evaluate
from .rules import load_rules

STARTER_RULES = '''# rulehook rules file - natural-language rules enforced via agent hooks.
# Docs: see README. Validate with `rulehook check`.

[settings]
provider = "anthropic"        # anthropic | openai  (env: RULEHOOK_PROVIDER)
# model = "claude-haiku-4-5"  # judge model (env: RULEHOOK_MODEL)
fail_open = true              # if the judge API fails: true=allow, false=block pre_tool_use
timeout = 25
cache = true
# log_file = ".rulehook/audit.jsonl"

# --- Deterministic rule: regex alone decides, zero LLM cost -----------------
[[rules]]
id = "no-destructive-rm"
rule = "Never run destructive recursive deletion commands such as `rm -rf` on broad paths."
events = ["pre_tool_use"]
tools = "Bash"
action = "deny"
pattern = "rm\\\\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\\\\s"
pattern_only = true

# --- Semantic rule: LLM judge decides, pattern scopes when it runs ----------
[[rules]]
id = "no-secret-access"
rule = "Never read, print, copy, or exfiltrate secrets: .env files, private keys, API tokens, or credential stores."
events = ["pre_tool_use"]
tools = "Bash|Read|Grep|Glob"
action = "deny"
pattern = "\\\\.env|secret|credential|id_rsa|token|\\\\.pem"

# --- Style/process rule checked when the agent thinks it is done ------------
[[rules]]
id = "tests-before-done"
rule = "Before declaring a coding task complete, the agent must have run the project's test suite in this session."
events = ["stop"]
action = "remind"

# --- Soft rule: warn the human, never block ----------------------------------
[[rules]]
id = "prefer-uv"
rule = "Python dependencies should be installed with `uv` rather than bare pip."
events = ["pre_tool_use"]
tools = "Bash"
action = "warn"
pattern = "pip3?\\\\s+install"
'''


def _presets_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "presets")


def _available_presets() -> list[str]:
    d = _presets_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".toml"))


def cmd_init(args: argparse.Namespace) -> int:
    target_dir = os.path.join(args.dir, ".rulehook")
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, "rulehook.toml")
    if os.path.exists(path) and not args.force:
        print(f"Already exists: {path} (use --force to overwrite)", file=sys.stderr)
        return 1
    if args.preset:
        preset_path = os.path.join(_presets_dir(), f"{args.preset}.toml")
        if not os.path.isfile(preset_path):
            print(f"Unknown preset '{args.preset}'. Available: "
                  + ", ".join(_available_presets()), file=sys.stderr)
            return 1
        with open(preset_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    else:
        content = STARTER_RULES
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    label = f"preset '{args.preset}'" if args.preset else "starter rules"
    print(f"Wrote {label} to {path}")
    print("Next: edit your rules, then run `rulehook install --target all`.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    ruleset = load_rules(args.rules)
    print(f"OK: {len(ruleset.rules)} rule(s) loaded from {ruleset.source_path}")
    for r in ruleset.rules:
        flags = []
        if r.pattern_only:
            flags.append("deterministic")
        elif r.pattern:
            flags.append("pattern-scoped")
        else:
            flags.append("llm")
        print(f"  - [{r.action:^6}] {r.id}: events={','.join(r.events)}"
              f" tools={r.tools or '*'} ({'/'.join(flags)})")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    targets = ["claude-code", "codex", "cursor"] if args.target == "all" else [args.target]
    rules_path = args.rules
    ruleset = None
    if rules_path is None:
        try:
            ruleset = load_rules(None, args.dir)
            rules_path = ruleset.source_path
        except FileNotFoundError:
            print("No rules file found; run `rulehook init` first.", file=sys.stderr)
            return 1
    if ruleset is None:
        ruleset = load_rules(rules_path, args.dir)
    for target in targets:
        adapter = ADAPTERS[target]
        written = adapter.install(
            args.dir,
            rules_path,
            scope=args.scope,
            mode=args.hook_mode,
            ruleset=ruleset,
        )
        print(f"[{target}] hooks registered in {written}")
        if args.hook_mode in ("auto", "native-prompt") and target == "codex":
            print("[codex] native prompt hooks are not supported; installed command hooks instead.")
        notes = getattr(adapter, "POST_INSTALL_NOTES", None)
        if notes:
            print(notes)
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    # A CLI judge (provider=claude-cli/codex-cli) spawns a nested agent; that
    # agent must not re-trigger our own hooks. The judge sets this env var.
    if os.environ.get("RULEHOOK_IN_JUDGE"):
        return 0
    adapter = ADAPTERS[args.target]
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:
        print(f"[rulehook] could not parse hook stdin: {exc}", file=sys.stderr)
        return 0  # never break the agent on our own bugs
    try:
        event = adapter.parse(payload)
        if event is None:
            # Unsupported event; Codex Stop quirk handled inside adapters.
            return 0
        ruleset = load_rules(args.rules, event.cwd or None)
        verdict = evaluate(event, ruleset)
        from .adapters import base as _base
        return _base.emit(adapter.render(event, verdict))
    except FileNotFoundError as exc:
        print(f"[rulehook] {exc}", file=sys.stderr)
        return 0
    except Exception as exc:  # fail-open on internal errors, but say so
        print(f"[rulehook] internal error (failing open): {exc}", file=sys.stderr)
        return 0


def cmd_test(args: argparse.Namespace) -> int:
    payload = {
        "session_id": "test",
        "cwd": os.getcwd(),
        "hook_event_name": args.event,
    }
    if args.tool:
        payload["tool_name"] = args.tool
    if args.command:
        payload["tool_input"] = {"command": args.command}
    if args.input_json:
        payload["tool_input"] = json.loads(args.input_json)
    if args.prompt:
        payload["prompt"] = args.prompt
    if args.last_message:
        payload["last_assistant_message"] = args.last_message

    adapter = ADAPTERS[args.target]
    event = adapter.parse(payload)
    if event is None:
        print(f"Unsupported event: {args.event}", file=sys.stderr)
        return 1
    ruleset = load_rules(args.rules)
    verdict = evaluate(event, ruleset)
    obj, code = adapter.render(event, verdict)
    print("--- verdict ---------------------------------------------")
    for v in verdict.violations:
        kind = "deterministic" if v.deterministic else "llm"
        print(f"  VIOLATION [{v.action}] {v.rule_id} ({kind}): {v.reason}")
    if not verdict.violations:
        print("  no violations")
    if verdict.judge_error:
        print(f"  judge_error: {verdict.judge_error}")
    print("--- hook stdout -----------------------------------------")
    print(json.dumps(obj, indent=2, ensure_ascii=False) if obj else "(empty)")
    print(f"--- exit code: {code}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rulehook",
        description="Enforce natural-language rules on coding agents via lifecycle hooks.",
    )
    parser.add_argument("--version", action="version", version=f"rulehook {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="write a starter rules file")
    p.add_argument("--dir", default=".", help="project directory (default: .)")
    p.add_argument("--preset", default=None,
                   help="start from a bundled preset (run with an invalid name to list)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("check", help="validate the rules file")
    p.add_argument("--rules", default=None)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("install", help="register hooks into the target CLI(s)")
    p.add_argument("--target", choices=["claude-code", "codex", "cursor", "all"], default="all")
    p.add_argument("--scope", choices=["project", "user"], default="project")
    p.add_argument("--hook-mode", choices=["command", "native-prompt", "auto"], default="command",
                   help="command=portable rulehook judge; native-prompt=Claude/Cursor native prompt hooks; auto=native where supported, command fallback")
    p.add_argument("--dir", default=".", help="project directory (default: .)")
    p.add_argument("--rules", default=None, help="explicit rules file to pin in the hook command")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("hook", help="hook entrypoint (reads event JSON on stdin)")
    p.add_argument("--target", choices=["claude-code", "codex", "cursor"], required=True)
    p.add_argument("--rules", default=None)
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("test", help="simulate a hook event")
    p.add_argument("--target", choices=["claude-code", "codex", "cursor"], default="claude-code")
    p.add_argument("--event", default="PreToolUse",
                   choices=["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"])
    p.add_argument("--tool", default=None)
    p.add_argument("--command", default=None, help="shorthand for tool_input.command")
    p.add_argument("--input-json", default=None, help="raw tool_input JSON")
    p.add_argument("--prompt", default=None)
    p.add_argument("--last-message", default=None)
    p.add_argument("--rules", default=None)
    p.set_defaults(func=cmd_test)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
