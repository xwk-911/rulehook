"""Cursor adapter.

Cursor hooks use lower camelCase event names and a Cursor-specific response
schema. Command hooks receive JSON on stdin and return JSON on stdout. Prompt
hooks are supported locally by Cursor; cloud agents run command hooks only.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional, Tuple

from ..engine import AgentEvent, Verdict
from ..rules import RuleSet
from . import base
from .native_prompt import specs_for_ruleset

PLATFORM = "cursor"

EVENT_MAP = {
    "PreToolUse": "pre_tool_use",
    "preToolUse": "pre_tool_use",
    "beforeShellExecution": "pre_tool_use",
    "beforeMCPExecution": "pre_tool_use",
    "beforeReadFile": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "postToolUse": "post_tool_use",
    "postToolUseFailure": "post_tool_use",
    "afterShellExecution": "post_tool_use",
    "afterMCPExecution": "post_tool_use",
    "afterFileEdit": "post_tool_use",
    "UserPromptSubmit": "user_prompt_submit",
    "beforeSubmitPrompt": "user_prompt_submit",
    "Stop": "stop",
    "stop": "stop",
    "SubagentStop": "stop",
    "subagentStop": "stop",
}

EVENT_BY_RULEHOOK = {
    "pre_tool_use": "preToolUse",
    "post_tool_use": "postToolUse",
    "user_prompt_submit": "beforeSubmitPrompt",
    "stop": "stop",
}

COMMAND_EVENTS = ["preToolUse", "postToolUse", "beforeSubmitPrompt", "stop"]


def parse(payload: dict) -> Optional[AgentEvent]:
    name = payload.get("hook_event_name") or payload.get("event") or payload.get("hook")
    if not name:
        for candidate in EVENT_MAP:
            if candidate in payload:
                name = candidate
                break
    event = EVENT_MAP.get(str(name))
    if event is None:
        return None

    tool_name, tool_input, tool_response = _tool_fields(name, payload)
    return AgentEvent(
        platform=PLATFORM,
        event=event,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_response=tool_response,
        prompt=payload.get("prompt"),
        last_message=payload.get("text") or payload.get("summary"),
        session_id=payload.get("session_id") or payload.get("conversation_id"),
        cwd=payload.get("cwd") or payload.get("project_dir"),
        raw=payload,
    )


def _tool_fields(name: Any, payload: dict) -> tuple[Optional[str], Optional[dict], Any]:
    if name == "beforeShellExecution":
        return "Bash", {"command": payload.get("command", "")}, None
    if name == "afterShellExecution":
        return "Bash", {"command": payload.get("command", "")}, payload.get("output")
    if name in ("beforeMCPExecution", "afterMCPExecution"):
        tool_input = payload.get("tool_input")
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError:
                tool_input = {"input": tool_input}
        return payload.get("tool_name"), tool_input, payload.get("result_json")
    if name == "beforeReadFile":
        return "Read", {"file_path": payload.get("file_path"), "content": payload.get("content")}, None
    if name == "afterFileEdit":
        return "Edit", {"file_path": payload.get("file_path"), "edits": payload.get("edits")}, None
    if "tool_name" in payload or "tool_input" in payload:
        return payload.get("tool_name"), payload.get("tool_input"), payload.get("tool_response")
    return payload.get("tool") or payload.get("tool_name"), payload.get("args") or payload.get("tool_input"), payload.get("result")


def render(event: AgentEvent, verdict: Verdict) -> Tuple[dict, int]:
    out: dict = {}
    if verdict.judge_error:
        msg = f"[rulehook] judge unavailable: {verdict.judge_error}"
        if not verdict.fail_open and event.event == "pre_tool_use":
            return _deny(msg + " (fail_closed=true, blocking)"), 0
        out["user_message"] = msg

    if verdict.warnings:
        warn_text = "; ".join(f"{v.rule_id}: {v.rule_text}" for v in verdict.warnings)
        out["user_message"] = (out.get("user_message", "") + " " if out.get("user_message") else "") \
            + f"[rulehook] warning: {warn_text}"

    blocking = verdict.blocking
    if not blocking:
        return out, 0

    reminder = verdict.reminder_text()
    if event.event == "pre_tool_use":
        deny = _deny(reminder)
        deny.update({k: v for k, v in out.items() if k == "user_message"})
        return deny, 0

    if event.event == "post_tool_use":
        out.update({
            "continue": True,
            "permission": "deny",
            "agent_message": reminder,
            "user_message": reminder,
        })
        return out, 0

    if event.event == "user_prompt_submit":
        if any(v.action == "deny" for v in blocking):
            out.update({"continue": False, "user_message": reminder})
        else:
            out.update({"continue": True, "agent_message": reminder})
        return out, 0

    if event.event == "stop":
        if int(event.raw.get("loop_count") or 0) > 0:
            out["user_message"] = (
                "[rulehook] rule still violated at stop, but a stop-hook continuation "
                "already ran; not continuing again. " + reminder
            )
            return out, 0
        return {"followup_message": reminder}, 0

    return out, 0


def _deny(reason: str) -> dict:
    return {
        "continue": True,
        "permission": "deny",
        "agent_message": reason,
        "user_message": reason,
    }


def _hook_command(rules_path: Optional[str]) -> str:
    cmd = f'{base.rulehook_executable()} hook --target cursor'
    if rules_path:
        cmd += f' --rules "{os.path.abspath(rules_path)}"'
    return cmd


def _is_rulehook_handler(handler: dict) -> bool:
    command = handler.get("command", "")
    if re.search(r"rulehook[\'\"]?\s+hook\b", command):
        return True
    return bool((handler.get("prompt") or "").startswith("You are rulehook,"))


def install(
    project_dir: str,
    rules_path: Optional[str],
    scope: str = "project",
    mode: str = "command",
    ruleset: Optional[RuleSet] = None,
) -> str:
    """Write/merge <project>/.cursor/hooks.json (or ~/.cursor/hooks.json)."""
    base_dir = os.path.expanduser("~/.cursor") if scope == "user" \
        else os.path.join(project_dir, ".cursor")
    os.makedirs(base_dir, exist_ok=True)
    hooks_path = os.path.join(base_dir, "hooks.json")

    doc: dict = {"version": 1, "hooks": {}}
    if os.path.isfile(hooks_path):
        with open(hooks_path, "r", encoding="utf-8") as fh:
            try:
                doc = json.load(fh)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Refusing to edit malformed JSON at {hooks_path}: {exc}")
    doc.setdefault("version", 1)
    hooks = doc.setdefault("hooks", {})

    if mode in ("native-prompt", "auto"):
        if ruleset is None:
            raise ValueError("ruleset is required for native-prompt hook installation")
        _install_prompt_hooks(hooks, ruleset)
    else:
        _install_command_hooks(hooks, _hook_command(rules_path))

    with open(hooks_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return hooks_path


def _install_command_hooks(hooks: dict, command: str) -> None:
    for event in COMMAND_EVENTS:
        entries = hooks.setdefault(event, [])
        entries[:] = [h for h in entries if not _is_rulehook_handler(h)]
        entries.append({
            "command": command,
            "timeout": 60,
            "matcher": "*",
        })


def _install_prompt_hooks(hooks: dict, ruleset: RuleSet) -> None:
    for event in COMMAND_EVENTS:
        entries = hooks.setdefault(event, [])
        entries[:] = [h for h in entries if not _is_rulehook_handler(h)]
        if not entries:
            hooks.pop(event, None)

    for spec in specs_for_ruleset(ruleset):
        event = EVENT_BY_RULEHOOK.get(spec.event)
        if not event:
            continue
        hooks.setdefault(event, []).append({
            "type": "prompt",
            "prompt": spec.prompt,
            "timeout": spec.timeout,
        })
