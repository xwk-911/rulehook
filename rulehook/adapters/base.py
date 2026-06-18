"""Shared parsing/rendering for Claude Code and Codex hooks.

Both CLIs use the same overall contract: one JSON object on stdin, an optional
JSON object on stdout, and exit code 2 + stderr as a blocking fallback. Codex
adopted a schema compatible with Claude Code's, so most logic lives here and
the per-platform modules only express their documented differences.
"""

from __future__ import annotations

import json
from typing import Optional, Tuple

from ..engine import AgentEvent, Verdict

EVENT_MAP = {
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "UserPromptSubmit": "user_prompt_submit",
    "Stop": "stop",
    "SubagentStop": "stop",
}


def parse_stdin(platform: str, payload: dict) -> Optional[AgentEvent]:
    """Map a raw hook payload to an AgentEvent. Returns None for unsupported events."""
    name = payload.get("hook_event_name", "")
    event = EVENT_MAP.get(name)
    if event is None:
        return None
    return AgentEvent(
        platform=platform,
        event=event,
        tool_name=payload.get("tool_name"),
        tool_input=payload.get("tool_input"),
        tool_response=payload.get("tool_response"),
        prompt=payload.get("prompt"),
        last_message=payload.get("last_assistant_message"),
        session_id=payload.get("session_id"),
        cwd=payload.get("cwd"),
        raw=payload,
    )


def render(event: AgentEvent, verdict: Verdict) -> Tuple[dict, int]:
    """Translate a Verdict into (stdout JSON object, exit code).

    Strategy per event type (identical wire format on both platforms):
      pre_tool_use        deny/remind -> permissionDecision "deny" with the rule
                          reminder as the reason; the model reads it, corrects
                          itself, and retries.
      post_tool_use       deny/remind -> decision "block": the tool result is
                          replaced by our feedback and the model continues.
      user_prompt_submit  deny -> block the prompt; remind -> inject the rules
                          as additionalContext without blocking.
      stop                deny/remind -> decision "block": the run continues
                          with the reminder as a new prompt. Guarded by
                          stop_hook_active to prevent infinite loops.
      warn (any event)    -> systemMessage only; never blocks.
    """
    out: dict = {}

    # Judge failure handling: fail-open (default) lets the action through with a
    # visible warning; fail-closed blocks pre_tool_use as a precaution.
    if verdict.judge_error:
        msg = f"[rulehook] judge unavailable: {verdict.judge_error}"
        if not verdict.fail_open and event.event == "pre_tool_use":
            return _pre_tool_deny(msg + " (fail_closed=true, blocking)"), 0
        out["systemMessage"] = msg

    if verdict.warnings:
        warn_text = "; ".join(f"{v.rule_id}: {v.rule_text}" for v in verdict.warnings)
        out["systemMessage"] = (out.get("systemMessage", "") + " " if out.get("systemMessage") else "") \
            + f"[rulehook] warning: {warn_text}"

    blocking = verdict.blocking
    if not blocking:
        return out, 0

    reminder = verdict.reminder_text()

    if event.event == "pre_tool_use":
        deny = _pre_tool_deny(reminder)
        deny.update({k: v for k, v in out.items() if k == "systemMessage"})
        return deny, 0

    if event.event == "post_tool_use":
        out.update({
            "decision": "block",
            "reason": reminder,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": reminder,
            },
        })
        return out, 0

    if event.event == "user_prompt_submit":
        if any(v.action == "deny" for v in blocking):
            out.update({"decision": "block", "reason": reminder})
        else:  # remind: let the prompt through but inject the rules as context
            out["hookSpecificOutput"] = {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": reminder,
            }
        return out, 0

    if event.event == "stop":
        if event.raw.get("stop_hook_active"):
            # Already continued once by a stop hook; do not loop forever.
            out["systemMessage"] = (
                "[rulehook] rule still violated at stop, but a stop-hook continuation "
                "already ran; not blocking again. " + reminder
            )
            return out, 0
        out.update({"decision": "block", "reason": reminder})
        return out, 0

    return out, 0


def _pre_tool_deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def emit(result: Tuple[dict, int]) -> int:
    obj, code = result
    if obj:
        print(json.dumps(obj, ensure_ascii=False))
    return code
