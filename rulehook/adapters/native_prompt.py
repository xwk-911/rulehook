"""Helpers for compiling rulehook rules into native prompt hooks.

Claude Code and Cursor can run LLM-evaluated hooks directly. Codex currently
cannot, so its adapter falls back to command hooks that call rulehook's judge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..rules import Rule, RuleSet


@dataclass(frozen=True)
class NativePromptSpec:
    event: str
    prompt: str
    timeout: int


def supports_native_prompt(platform: str) -> bool:
    return platform in {"claude-code", "cursor"}


def build_prompt(event: str, rules: list[Rule]) -> str:
    rules_payload = [
        {
            "id": r.id,
            "rule": r.rule,
            "action": r.action,
            "tools": r.tools,
            "pattern": r.pattern,
            "pattern_only": r.pattern_only,
        }
        for r in rules
    ]
    rules_json = json.dumps(rules_payload, indent=2, ensure_ascii=False)
    return (
        "You are rulehook, a strict compliance judge for an AI coding agent.\n"
        f"Hook event: {event}\n\n"
        "RULES:\n"
        f"{rules_json}\n\n"
        "HOOK INPUT:\n"
        "$ARGUMENTS\n\n"
        "For each rule, decide whether the hook input clearly violates the rule's "
        "intent. Respect each rule's optional tools regex and pattern prefilter "
        "when the relevant fields are present in the hook input. Judge "
        "conservatively: do not block actions that are merely related to a rule.\n\n"
        "Respond only with JSON in this exact shape:\n"
        "{\"ok\": true}\n"
        "or\n"
        "{\"ok\": false, \"reason\": \"[rulehook] MANDATORY RULE VIOLATED "
        "(id=<id>): <rule text>. <short reason>. Re-read the rule, adjust your "
        "approach so it is satisfied, and continue.\"}\n"
    )


def specs_for_ruleset(ruleset: RuleSet) -> list[NativePromptSpec]:
    specs: list[NativePromptSpec] = []
    for event in sorted({event for rule in ruleset.rules for event in rule.events}):
        rules = [
            r for r in ruleset.rules
            if r.enabled and event in r.events and r.action in ("deny", "remind")
        ]
        if not rules:
            continue
        specs.append(
            NativePromptSpec(
                event=event,
                prompt=build_prompt(event, rules),
                timeout=ruleset.settings.timeout,
            )
        )
    return specs
