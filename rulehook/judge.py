"""LLM judge: decide whether an agent action violates natural-language rules.

Zero third-party dependencies. Two families of judge backend:

  * API providers ("anthropic" / "openai") — urllib calls that need an API key.
  * CLI providers ("claude-cli" / "codex-cli") — shell out to a locally
    installed `claude`/`codex` binary, which authenticates with the user's
    existing subscription login. No API key required.

Supports a mock mode (RULEHOOK_MOCK=1) for tests and offline dry runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .rules import Rule, Settings

DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "claude-cli": "claude-haiku-4-5",
    "codex-cli": "",  # empty = let codex use its own configured default
}

API_PROVIDERS = ("anthropic", "openai")
CLI_PROVIDERS = ("claude-cli", "codex-cli")

JUDGE_SYSTEM = """You are a strict compliance judge for an AI coding agent. \
You receive (1) a numbered list of mandatory rules written in natural language and \
(2) a description of the action the agent is about to take or has just taken. \
For EACH rule, decide whether this action VIOLATES it. Judge conservatively: \
only report a violation when the action clearly conflicts with the rule's intent; \
do not flag actions that are merely related to the rule's topic. \
Respond with ONLY a JSON array, no prose, no markdown fences. One object per rule, \
in the same order, with this exact shape: \
[{"id": "<rule id>", "violated": true|false, "reason": "<one short sentence>"}]"""


@dataclass
class JudgeResult:
    rule_id: str
    violated: bool
    reason: str


class JudgeError(Exception):
    pass


def _cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    d = os.path.join(base, "rulehook")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "verdicts.json")


def _cache_load() -> dict:
    try:
        with open(_cache_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _cache_store(cache: dict) -> None:
    try:
        # Keep the cache bounded.
        if len(cache) > 2000:
            cache = dict(list(cache.items())[-1000:])
        with open(_cache_path(), "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass


def _http_json(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")[:500]
        except Exception:
            pass
        raise JudgeError(f"Judge API HTTP {exc.code}: {body}") from exc
    except Exception as exc:  # timeouts, DNS, etc.
        raise JudgeError(f"Judge API call failed: {exc}") from exc


def _call_anthropic(model: str, user_prompt: str, timeout: int) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise JudgeError("ANTHROPIC_API_KEY is not set.")
    out = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        {
            "model": model,
            "max_tokens": 1024,
            "system": JUDGE_SYSTEM,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout,
    )
    parts = out.get("content", [])
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _call_openai(model: str, user_prompt: str, timeout: int) -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise JudgeError("OPENAI_API_KEY is not set.")
    out = _http_json(
        "https://api.openai.com/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout,
    )
    choices = out.get("choices", [])
    if not choices:
        raise JudgeError("Empty response from OpenAI API.")
    return choices[0]["message"]["content"] or ""


def _run_cli(cmd: list[str], timeout: int) -> tuple[str, str, int]:
    """Run a judge CLI in a subprocess. RULEHOOK_IN_JUDGE guards against the
    nested agent re-triggering rulehook's own hooks (see cli.cmd_hook)."""
    env = {**os.environ, "RULEHOOK_IN_JUDGE": "1"}
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=env,
        )
    except FileNotFoundError as exc:
        raise JudgeError(f"CLI judge binary not found: {cmd[0]!r}. Is it installed and on PATH?") from exc
    except subprocess.TimeoutExpired as exc:
        raise JudgeError(f"CLI judge '{cmd[0]}' timed out after {timeout}s.") from exc
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    return out, err, proc.returncode


def _call_claude_cli(model: str, user_prompt: str, timeout: int) -> str:
    """Judge via the Claude Code CLI in headless mode (uses subscription login).

    --system-prompt replaces the coding-agent persona with the judge persona;
    --disallowed-tools keeps the nested call from doing any tool work; JSON
    output mode gives a stable envelope to parse the verdict text out of."""
    cmd = [
        "claude", "-p", user_prompt,
        "--system-prompt", JUDGE_SYSTEM,
        "--output-format", "json",
        "--no-session-persistence",
        "--disallowed-tools", "Bash Edit Write Read Glob Grep WebFetch WebSearch",
    ]
    if model:
        cmd += ["--model", model]
    out, err, code = _run_cli(cmd, timeout)
    if code != 0:
        raise JudgeError(f"claude CLI exited {code}: {(err or out)[:500]}")
    try:
        env = json.loads(out)
        if isinstance(env, dict):
            if env.get("is_error"):
                raise JudgeError(f"claude CLI reported an error: {str(env.get('result'))[:300]}")
            return str(env.get("result", ""))
    except json.JSONDecodeError:
        pass
    return out


def _call_codex_cli(model: str, user_prompt: str, timeout: int) -> str:
    """Judge via the Codex CLI in non-interactive mode (uses subscription login).

    Codex has no system-prompt flag, so the judge instructions ride at the top
    of the prompt; a read-only sandbox keeps the nested run from touching files.
    The JSON array is recovered from stdout by _extract_json_array."""
    prompt = JUDGE_SYSTEM + "\n\n" + user_prompt
    cmd = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only"]
    if model:
        cmd += ["-m", model]
    cmd += [prompt]
    out, err, code = _run_cli(cmd, timeout)
    if code != 0:
        raise JudgeError(f"codex CLI exited {code}: {(err or out)[:500]}")
    return out


def _extract_json_array(text: str) -> list:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    match = re.search(r"\[.*\]", text, flags=re.S)
    if match:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, list):
            return parsed
    raise JudgeError(f"Judge returned non-JSON output: {text[:200]!r}")


def _mock_results(rules: list[Rule], action_text: str) -> list[JudgeResult]:
    """Offline heuristic: a rule is 'violated' if RULEHOOK_MOCK_VIOLATE lists its id,
    or if the rule's own pattern matches. Used for tests and dry runs."""
    forced = set(filter(None, os.environ.get("RULEHOOK_MOCK_VIOLATE", "").split(",")))
    out = []
    for r in rules:
        hit = r.id in forced or bool(r.pattern and re.search(r.pattern, action_text))
        out.append(JudgeResult(r.id, hit, "mock verdict"))
    return out


def judge(rules: list[Rule], action_text: str, settings: Settings) -> list[JudgeResult]:
    """Ask the judge model which of `rules` the action violates."""
    if not rules:
        return []
    if os.environ.get("RULEHOOK_MOCK"):
        return _mock_results(rules, action_text)

    provider = os.environ.get("RULEHOOK_PROVIDER", settings.provider)
    model = os.environ.get("RULEHOOK_MODEL", settings.model or DEFAULT_MODELS.get(provider, ""))
    callers = {
        "anthropic": _call_anthropic,
        "openai": _call_openai,
        "claude-cli": _call_claude_cli,
        "codex-cli": _call_codex_cli,
    }
    if provider not in callers:
        raise JudgeError(
            f"Unknown provider '{provider}'. Use one of: {', '.join(callers)}."
        )

    action_text = action_text[: settings.max_chars]
    rules_block = "\n".join(f'{i + 1}. (id={r.id}) {r.rule}' for i, r in enumerate(rules))
    user_prompt = (
        f"RULES:\n{rules_block}\n\nAGENT ACTION:\n{action_text}\n\n"
        "Return the JSON array now."
    )

    cache_key = None
    cache: dict = {}
    if settings.cache:
        cache_key = hashlib.sha256(
            (provider + model + rules_block + action_text).encode("utf-8")
        ).hexdigest()
        cache = _cache_load()
        if cache_key in cache:
            return [JudgeResult(**item) for item in cache[cache_key]]

    raw = callers[provider](model, user_prompt, settings.timeout)
    parsed = _extract_json_array(raw)

    by_id = {str(item.get("id", "")): item for item in parsed if isinstance(item, dict)}
    results = []
    for r in rules:
        item = by_id.get(r.id, {})
        results.append(
            JudgeResult(
                rule_id=r.id,
                violated=bool(item.get("violated", False)),
                reason=str(item.get("reason", ""))[:300],
            )
        )
    if settings.cache and cache_key:
        cache[cache_key] = [vars(x) for x in results]
        _cache_store(cache)
    return results
