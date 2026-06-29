"""Claude Code adapter.

Claude Code hook contract (settings.json -> hooks):
  - Events: PreToolUse, PostToolUse, UserPromptSubmit, Stop, ...
  - Input: one JSON object on stdin (session_id, hook_event_name, tool_name,
    tool_input, tool_response, prompt, stop_hook_active, ...).
  - Output: JSON on stdout (permissionDecision / decision / additionalContext /
    systemMessage), or exit code 2 with the reason on stderr.

rulehook registers one command per event:  rulehook hook --target claude-code
The event type is read from stdin's hook_event_name, so a single command serves
all events.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional, Tuple

from ..engine import AgentEvent, Verdict
from ..rules import RuleSet
from . import base
from .native_prompt import specs_for_ruleset

PLATFORM = "claude-code"
EVENTS = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"]
EVENT_BY_RULEHOOK = {
    "pre_tool_use": "PreToolUse",
    "post_tool_use": "PostToolUse",
    "user_prompt_submit": "UserPromptSubmit",
    "stop": "Stop",
}


def parse(payload: dict) -> Optional[AgentEvent]:
    return base.parse_stdin(PLATFORM, payload)


def render(event: AgentEvent, verdict: Verdict) -> Tuple[dict, int]:
    return base.render(event, verdict)


def _hook_command(rules_path: Optional[str]) -> str:
    cmd = f'{base.rulehook_executable()} hook --target claude-code'
    if rules_path:
        cmd += f' --rules "{os.path.abspath(rules_path)}"'
    return cmd


def _is_rulehook(command: str) -> bool:
    return bool(re.search(r"rulehook[\'\"]?\s+hook\b", command))


def _is_rulehook_handler(handler: dict) -> bool:
    if _is_rulehook(handler.get("command", "")):
        return True
    return bool((handler.get("prompt") or "").startswith("You are rulehook,"))


def _install_command_hooks(hooks: dict, command: str) -> None:
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        for group in groups:
            group["hooks"] = [
                h for h in group.get("hooks", [])
                if not _is_rulehook_handler(h)
            ]
        groups[:] = [g for g in groups if g.get("hooks")]
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 60}]})


def _install_prompt_hooks(hooks: dict, ruleset: RuleSet) -> None:
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        for group in groups:
            group["hooks"] = [
                h for h in group.get("hooks", [])
                if not _is_rulehook_handler(h)
            ]
        groups[:] = [g for g in groups if g.get("hooks")]
        if not groups:
            hooks.pop(event, None)

    for spec in specs_for_ruleset(ruleset):
        event = EVENT_BY_RULEHOOK.get(spec.event)
        if not event:
            continue
        hooks.setdefault(event, []).append({
            "hooks": [{
                "type": "prompt",
                "prompt": spec.prompt,
                "timeout": spec.timeout,
                "continueOnBlock": True,
            }]
        })


def install(
    project_dir: str,
    rules_path: Optional[str],
    scope: str = "project",
    mode: str = "command",
    ruleset: Optional[RuleSet] = None,
) -> str:
    """Merge rulehook hooks into Claude Code settings without clobbering others.

    scope: "project" -> <project>/.claude/settings.json
           "user"    -> ~/.claude/settings.json
    """
    if scope == "user":
        settings_path = os.path.expanduser("~/.claude/settings.json")
    else:
        settings_path = os.path.join(project_dir, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    settings: dict = {}
    if os.path.isfile(settings_path):
        with open(settings_path, "r", encoding="utf-8") as fh:
            try:
                settings = json.load(fh)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Refusing to edit malformed JSON at {settings_path}: {exc}")

    hooks = settings.setdefault("hooks", {})
    if mode in ("native-prompt", "auto"):
        if ruleset is None:
            raise ValueError("ruleset is required for native-prompt hook installation")
        _install_prompt_hooks(hooks, ruleset)
    else:
        _install_command_hooks(hooks, _hook_command(rules_path))

    with open(settings_path, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return settings_path
