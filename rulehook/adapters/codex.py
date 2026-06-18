"""OpenAI Codex CLI adapter.

Codex hook contract:
  - Hooks are discovered in ~/.codex/hooks.json and <repo>/.codex/hooks.json
    (project hooks load only when the project is trusted).
  - Events: PreToolUse, PostToolUse, UserPromptSubmit, Stop, SessionStart...
  - KNOWN LIMITS (documented by OpenAI):
      * Codex currently runs command hooks. Prompt/agent hook handlers are not
        equivalent to Claude Code or Cursor native prompt hooks, so rulehook
        provides semantic checks by invoking its own judge from a command hook.
      * Treat hooks as guardrails, not a complete security boundary.
      * Stop hooks MUST emit JSON on stdout when exiting 0 (plain text invalid).
      * Hooks are disabled on Windows.
  - Output schema mirrors Claude Code's (permissionDecision / decision /
    additionalContext / systemMessage; exit 2 + stderr as fallback).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from typing import Optional, Tuple

from ..engine import AgentEvent, Verdict
from ..rules import RuleSet
from . import base

PLATFORM = "codex"
EVENTS = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]


def parse(payload: dict) -> Optional[AgentEvent]:
    return base.parse_stdin(PLATFORM, payload)


def render(event: AgentEvent, verdict: Verdict) -> Tuple[dict, int]:
    obj, code = base.render(event, verdict)
    # Codex: Stop hooks must print JSON when exiting 0; plain/empty is invalid.
    if event.event == "stop" and not obj:
        obj = {"continue": True}
    return obj, code


def _hook_command(rules_path: Optional[str]) -> str:
    exe = shutil.which("rulehook") or "rulehook"
    if " " in exe:
        exe = f'"{exe}"'
    cmd = f'{exe} hook --target codex'
    if rules_path:
        cmd += f' --rules "{os.path.abspath(rules_path)}"'
    return cmd


def _is_rulehook(command: str) -> bool:
    return bool(re.search(r"rulehook[\'\"]?\s+hook\b", command))


def install(
    project_dir: str,
    rules_path: Optional[str],
    scope: str = "project",
    mode: str = "command",
    ruleset: Optional[RuleSet] = None,
) -> str:
    """Write/merge <project>/.codex/hooks.json (or ~/.codex/hooks.json)."""
    del mode, ruleset  # Codex currently runs command hooks; native prompts fall back here.
    base_dir = os.path.expanduser("~/.codex") if scope == "user" \
        else os.path.join(project_dir, ".codex")
    os.makedirs(base_dir, exist_ok=True)
    hooks_path = os.path.join(base_dir, "hooks.json")

    doc: dict = {"hooks": {}}
    if os.path.isfile(hooks_path):
        with open(hooks_path, "r", encoding="utf-8") as fh:
            try:
                doc = json.load(fh)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Refusing to edit malformed JSON at {hooks_path}: {exc}")
    hooks = doc.setdefault("hooks", {})

    command = _hook_command(rules_path)
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        for group in groups:
            group["hooks"] = [
                h for h in group.get("hooks", [])
                if not _is_rulehook(h.get("command", ""))
            ]
        groups[:] = [g for g in groups if g.get("hooks")]
        entry = {
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": 60,
                "statusMessage": "rulehook: checking rules",
            }]
        }
        groups.append(entry)

    with open(hooks_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return hooks_path


POST_INSTALL_NOTES = """\
Codex post-install checklist:
  1. Project-local hooks only load when the project is trusted
     (run `codex` in the repo and trust it, or use --trust).
  2. Non-managed command hooks must be reviewed/trusted on first run.
  3. Native prompt hooks are not supported; rulehook installs command hooks
     and calls its own judge.
"""
