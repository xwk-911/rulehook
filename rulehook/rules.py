"""Rule definitions and loading.

Rules live in a single TOML (or JSON) file. Each rule is a natural-language
statement plus optional deterministic pre-filters. Example:

    [settings]
    provider = "anthropic"          # anthropic | openai
    model = "claude-haiku-4-5"
    fail_open = true                # on judge error: allow (true) or block (false)
    timeout = 25                    # seconds for the judge API call

    [[rules]]
    id = "no-secrets"
    rule = "Never read, print, or exfiltrate .env files, private keys, or credentials."
    events = ["pre_tool_use"]       # pre_tool_use | post_tool_use | user_prompt_submit | stop
    tools = "Bash|Read|Grep"        # regex over tool name; omit = all tools
    action = "deny"                 # deny | remind | warn
    pattern = "\\.env|id_rsa"       # optional regex pre-filter over the action text
    pattern_only = false            # true = regex match alone is a violation (no LLM call)
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

VALID_EVENTS = {"pre_tool_use", "post_tool_use", "user_prompt_submit", "stop"}
VALID_ACTIONS = {"deny", "remind", "warn"}

DEFAULT_RULES_FILENAMES = ("rulehook.toml", "rules.toml")
DEFAULT_SEARCH_DIRS = (".rulehook", ".", os.path.expanduser("~/.rulehook"))


@dataclass
class Rule:
    id: str
    rule: str
    events: list[str] = field(default_factory=lambda: ["pre_tool_use"])
    tools: Optional[str] = None          # regex over tool name
    action: str = "remind"               # deny | remind | warn
    pattern: Optional[str] = None        # regex pre-filter over serialized action
    pattern_only: bool = False
    enabled: bool = True

    def validate(self) -> None:
        if not self.id or not self.rule:
            raise ValueError("Each rule needs non-empty 'id' and 'rule'.")
        bad = set(self.events) - VALID_EVENTS
        if bad:
            raise ValueError(f"Rule '{self.id}': unknown events {sorted(bad)}. "
                             f"Valid: {sorted(VALID_EVENTS)}")
        if self.action not in VALID_ACTIONS:
            raise ValueError(f"Rule '{self.id}': action must be one of {sorted(VALID_ACTIONS)}.")
        for attr in ("tools", "pattern"):
            value = getattr(self, attr)
            if value is not None:
                try:
                    re.compile(value)
                except re.error as exc:
                    raise ValueError(f"Rule '{self.id}': invalid regex in '{attr}': {exc}") from exc
        if self.pattern_only and not self.pattern:
            raise ValueError(f"Rule '{self.id}': pattern_only=true requires 'pattern'.")


@dataclass
class Settings:
    # anthropic | openai (API key) | claude-cli | codex-cli (subscription CLI login)
    provider: str = "anthropic"
    model: Optional[str] = None          # default chosen per provider in judge.py
    fail_open: bool = True
    timeout: int = 25
    cache: bool = True
    max_chars: int = 6000                # truncate action text sent to the judge
    log_file: Optional[str] = None       # optional JSONL audit log


@dataclass
class RuleSet:
    settings: Settings
    rules: list[Rule]
    source_path: str

    def for_event(self, event: str, tool_name: Optional[str]) -> list[Rule]:
        out = []
        for r in self.rules:
            if not r.enabled or event not in r.events:
                continue
            if r.tools and tool_name is not None and not re.search(r.tools, tool_name):
                continue
            out.append(r)
        return out


def _parse_toml(text: str) -> dict:
    import tomllib  # Python 3.11+
    return tomllib.loads(text)


def _load_raw(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        return _parse_toml(text)
    except Exception:
        # Last resort: maybe it is JSON with a .toml name.
        return json.loads(text)


def find_rules_file(explicit: Optional[str] = None, cwd: Optional[str] = None) -> str:
    """Resolve the rules file: explicit path > $RULEHOOK_RULES > project > home."""
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise FileNotFoundError(f"Rules file not found: {explicit}")
    env = os.environ.get("RULEHOOK_RULES")
    if env:
        if os.path.isfile(env):
            return env
        raise FileNotFoundError(f"$RULEHOOK_RULES points to a missing file: {env}")
    base = cwd or os.getcwd()
    candidates = []
    for d in (os.path.join(base, ".rulehook"), base, os.path.expanduser("~/.rulehook")):
        for name in DEFAULT_RULES_FILENAMES:
            candidates.append(os.path.join(d, name))
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError(
        "No rules file found. Run 'rulehook init' or set $RULEHOOK_RULES. Searched:\n  "
        + "\n  ".join(candidates)
    )


def load_rules(explicit: Optional[str] = None, cwd: Optional[str] = None) -> RuleSet:
    path = find_rules_file(explicit, cwd)
    raw = _load_raw(path)
    s = raw.get("settings", {}) or {}
    settings = Settings(
        provider=s.get("provider", "anthropic"),
        model=s.get("model"),
        fail_open=bool(s.get("fail_open", True)),
        timeout=int(s.get("timeout", 25)),
        cache=bool(s.get("cache", True)),
        max_chars=int(s.get("max_chars", 6000)),
        log_file=s.get("log_file"),
    )
    rules: list[Rule] = []
    for item in raw.get("rules", []) or []:
        rule = Rule(
            id=str(item.get("id", "")),
            rule=str(item.get("rule", "")),
            events=list(item.get("events", ["pre_tool_use"])),
            tools=item.get("tools"),
            action=str(item.get("action", "remind")),
            pattern=item.get("pattern"),
            pattern_only=bool(item.get("pattern_only", False)),
            enabled=bool(item.get("enabled", True)),
        )
        rule.validate()
        rules.append(rule)
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate rule ids in rules file.")
    return RuleSet(settings=settings, rules=rules, source_path=path)
