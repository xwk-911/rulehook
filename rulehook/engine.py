"""Core engine: normalized agent events in, verdicts out.

Pipeline per event:
  1. Select rules applicable to (event type, tool name).
  2. pattern_only rules: regex match alone decides (no LLM call).
  3. Remaining rules whose `pattern` (if any) matches are sent to the LLM judge
     in a single batched call.
  4. Violations are merged into a Verdict; adapters translate it into each
     platform's hook output protocol.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .judge import JudgeError, judge
from .rules import Rule, RuleSet


@dataclass
class AgentEvent:
    """Platform-neutral view of a hook event."""
    platform: str                       # "claude-code" | "codex" | "cursor"
    event: str                          # pre_tool_use | post_tool_use | user_prompt_submit | stop
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    tool_response: Any = None
    prompt: Optional[str] = None
    last_message: Optional[str] = None
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def action_text(self) -> str:
        """Human/LLM-readable description of what the agent is doing."""
        lines = [f"event: {self.event}"]
        if self.tool_name:
            lines.append(f"tool: {self.tool_name}")
        if self.tool_input is not None:
            lines.append("tool_input: " + json.dumps(self.tool_input, ensure_ascii=False))
        if self.tool_response is not None:
            resp = self.tool_response
            if not isinstance(resp, str):
                resp = json.dumps(resp, ensure_ascii=False)
            lines.append("tool_output: " + resp)
        if self.prompt:
            lines.append("user_prompt: " + self.prompt)
        if self.last_message:
            lines.append("assistant_final_message: " + self.last_message)
        return "\n".join(lines)


@dataclass
class Violation:
    rule_id: str
    rule_text: str
    action: str       # deny | remind | warn
    reason: str
    deterministic: bool = False


@dataclass
class Verdict:
    violations: list[Violation] = field(default_factory=list)
    judge_error: Optional[str] = None
    fail_open: bool = True

    @property
    def blocking(self) -> list[Violation]:
        return [v for v in self.violations if v.action in ("deny", "remind")]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.action == "warn"]

    def reminder_text(self) -> str:
        """The message fed back to the model: restate each violated rule."""
        parts = []
        for v in self.blocking:
            parts.append(
                f"[rulehook] MANDATORY RULE VIOLATED (id={v.rule_id}): {v.rule_text}"
                + (f" | judge: {v.reason}" if v.reason else "")
            )
        parts.append(
            "[rulehook] Re-read the rule(s) above, adjust your approach so the rule is "
            "satisfied, and then continue with the task."
        )
        return "\n".join(parts)


def evaluate(event: AgentEvent, ruleset: RuleSet) -> Verdict:
    applicable = ruleset.for_event(event.event, event.tool_name)
    if not applicable:
        return Verdict(fail_open=ruleset.settings.fail_open)

    text = event.action_text()
    verdict = Verdict(fail_open=ruleset.settings.fail_open)
    llm_rules: list[Rule] = []

    for r in applicable:
        if r.pattern:
            hit = re.search(r.pattern, text)
            if r.pattern_only:
                if hit:
                    verdict.violations.append(Violation(
                        rule_id=r.id, rule_text=r.rule, action=r.action,
                        reason=f"pattern '{r.pattern}' matched", deterministic=True,
                    ))
                continue
            if not hit:
                continue  # pattern acts as a cheap scoping pre-filter
        llm_rules.append(r)

    if llm_rules:
        try:
            results = judge(llm_rules, text, ruleset.settings)
            by_id = {r.id: r for r in llm_rules}
            for res in results:
                if res.violated and res.rule_id in by_id:
                    rule = by_id[res.rule_id]
                    verdict.violations.append(Violation(
                        rule_id=rule.id, rule_text=rule.rule,
                        action=rule.action, reason=res.reason,
                    ))
        except JudgeError as exc:
            verdict.judge_error = str(exc)

    _audit_log(event, verdict, ruleset)
    return verdict


def _audit_log(event: AgentEvent, verdict: Verdict, ruleset: RuleSet) -> None:
    path = ruleset.settings.log_file
    if not path:
        return
    try:
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "platform": event.platform,
            "event": event.event,
            "tool": event.tool_name,
            "session": event.session_id,
            "violations": [vars(v) for v in verdict.violations],
            "judge_error": verdict.judge_error,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
