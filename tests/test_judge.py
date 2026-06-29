import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from unittest import mock

from rulehook import judge as judge_mod
from rulehook.judge import JudgeError, JudgeResult, judge
from rulehook.rules import Rule, Settings


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TestJudgeBackends(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()
        os.environ.pop("RULEHOOK_MOCK", None)
        os.environ.pop("RULEHOOK_PROVIDER", None)
        os.environ.pop("RULEHOOK_MODEL", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_empty_rules_returns_empty_without_provider(self):
        self.assertEqual(judge([], "anything", Settings(provider="missing")), [])

    def test_unknown_provider_rejected(self):
        with self.assertRaisesRegex(JudgeError, "Unknown provider"):
            judge([Rule(id="r", rule="Never X.")], "x", Settings(provider="bogus", cache=False))

    def test_extract_json_array_accepts_plain_fenced_and_embedded_json(self):
        self.assertEqual(judge_mod._extract_json_array('[{"id":"r"}]'), [{"id": "r"}])
        self.assertEqual(judge_mod._extract_json_array('```json\n[{"id":"r"}]\n```'), [{"id": "r"}])
        self.assertEqual(judge_mod._extract_json_array('prefix [{"id":"r"}] suffix'), [{"id": "r"}])
        with self.assertRaisesRegex(JudgeError, "non-JSON"):
            judge_mod._extract_json_array("no verdict here")

    def test_http_json_success_and_errors(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse({"ok": True})) as urlopen:
            out = judge_mod._http_json("https://example.test", {"h": "v"}, {"p": 1}, 3)
        self.assertEqual(out, {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.method, "POST")

        err = urllib.error.HTTPError("u", 429, "slow", {}, None)
        err.read = mock.Mock(return_value=b"rate limited")
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(JudgeError, "HTTP 429: rate limited"):
                judge_mod._http_json("https://example.test", {}, {}, 3)

        with mock.patch("urllib.request.urlopen", side_effect=OSError("dns")):
            with self.assertRaisesRegex(JudgeError, "call failed"):
                judge_mod._http_json("https://example.test", {}, {}, 3)

    def test_anthropic_and_openai_calls_build_expected_requests(self):
        os.environ["ANTHROPIC_API_KEY"] = "anthropic-key"
        with mock.patch("rulehook.judge._http_json", return_value={
            "content": [
                {"type": "text", "text": "[1]"},
                {"type": "tool_use", "text": "ignored"},
                {"type": "text", "text": "[2]"},
            ]
        }) as http:
            self.assertEqual(judge_mod._call_anthropic("claude-test", "prompt", 4), "[1][2]")
        payload = http.call_args.args[2]
        headers = http.call_args.args[1]
        self.assertEqual(payload["model"], "claude-test")
        self.assertEqual(headers["x-api-key"], "anthropic-key")

        os.environ.pop("ANTHROPIC_API_KEY")
        with self.assertRaisesRegex(JudgeError, "ANTHROPIC_API_KEY"):
            judge_mod._call_anthropic("m", "p", 1)

        os.environ["OPENAI_API_KEY"] = "openai-key"
        with mock.patch("rulehook.judge._http_json", return_value={
            "choices": [{"message": {"content": "[3]"}}]
        }) as http:
            self.assertEqual(judge_mod._call_openai("gpt-test", "prompt", 5), "[3]")
        payload = http.call_args.args[2]
        headers = http.call_args.args[1]
        self.assertEqual(payload["model"], "gpt-test")
        self.assertEqual(headers["Authorization"], "Bearer openai-key")

        with mock.patch("rulehook.judge._http_json", return_value={"choices": []}):
            with self.assertRaisesRegex(JudgeError, "Empty response"):
                judge_mod._call_openai("m", "p", 1)
        os.environ.pop("OPENAI_API_KEY")
        with self.assertRaisesRegex(JudgeError, "OPENAI_API_KEY"):
            judge_mod._call_openai("m", "p", 1)

    def test_run_cli_success_and_failures(self):
        completed = subprocess.CompletedProcess(
            ["tool"], 0, stdout="out".encode("utf-8"), stderr="err".encode("utf-8")
        )

        def fake_run(cmd, **kwargs):
            self.assertEqual(cmd, ["tool"])
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["env"]["RULEHOOK_IN_JUDGE"], "1")
            return completed

        with mock.patch("subprocess.run", side_effect=fake_run):
            self.assertEqual(judge_mod._run_cli(["tool"], 6), ("out", "err", 0))

        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            with self.assertRaisesRegex(JudgeError, "binary not found"):
                judge_mod._run_cli(["missing"], 1)

        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["slow"], 2)):
            with self.assertRaisesRegex(JudgeError, "timed out"):
                judge_mod._run_cli(["slow"], 2)

    def test_claude_cli_parses_envelope_and_reports_errors(self):
        with mock.patch("rulehook.judge._run_cli", return_value=(
            json.dumps({"result": "[{\"id\":\"r\",\"violated\":true}]", "is_error": False}),
            "",
            0,
        )) as run:
            out = judge_mod._call_claude_cli("claude-test", "prompt", 3)
        self.assertIn('"violated":true', out)
        self.assertIn("--model", run.call_args.args[0])
        self.assertIn("--disallowed-tools", run.call_args.args[0])

        with mock.patch("rulehook.judge._run_cli", return_value=(
            json.dumps({"result": "bad", "is_error": True}),
            "",
            0,
        )):
            with self.assertRaisesRegex(JudgeError, "reported an error"):
                judge_mod._call_claude_cli("", "prompt", 3)

        with mock.patch("rulehook.judge._run_cli", return_value=("raw text", "", 0)):
            self.assertEqual(judge_mod._call_claude_cli("", "prompt", 3), "raw text")

        with mock.patch("rulehook.judge._run_cli", return_value=("", "no auth", 7)):
            with self.assertRaisesRegex(JudgeError, "exited 7"):
                judge_mod._call_claude_cli("", "prompt", 3)

    def test_codex_cli_builds_read_only_command(self):
        with mock.patch("rulehook.judge._run_cli", return_value=("[{}]", "", 0)) as run:
            self.assertEqual(judge_mod._call_codex_cli("gpt-test", "prompt", 3), "[{}]")
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:5], ["codex", "exec", "--skip-git-repo-check", "-s", "read-only"])
        self.assertIn("-m", cmd)
        self.assertIn(judge_mod.JUDGE_SYSTEM, cmd[-1])

        with mock.patch("rulehook.judge._run_cli", return_value=("", "bad", 4)):
            with self.assertRaisesRegex(JudgeError, "exited 4"):
                judge_mod._call_codex_cli("", "prompt", 3)

    def test_judge_maps_results_truncates_and_caches(self):
        cache_dir = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = cache_dir
        rules = [
            Rule(id="hit", rule="Never hit."),
            Rule(id="miss", rule="Never miss."),
        ]
        raw = json.dumps([
            {"id": "hit", "violated": True, "reason": "x" * 400},
            {"id": "unknown", "violated": True, "reason": "ignored"},
        ])
        with mock.patch("rulehook.judge._call_openai", return_value=raw) as call:
            results = judge(rules, "abcdef", Settings(provider="openai", model="m", max_chars=3))
            cached = judge(rules, "abcdef", Settings(provider="openai", model="m", max_chars=3))
        self.assertEqual(call.call_count, 1)
        self.assertEqual([r.rule_id for r in results], ["hit", "miss"])
        self.assertEqual([r.violated for r in results], [True, False])
        self.assertEqual(len(results[0].reason), 300)
        self.assertEqual([r.violated for r in cached], [True, False])
        self.assertIn("AGENT ACTION:\nabc\n", call.call_args.args[1])

    def test_cache_helpers_tolerate_bad_files_and_store_is_bounded(self):
        cache_dir = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = cache_dir
        path = judge_mod._cache_path()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{bad")
        self.assertEqual(judge_mod._cache_load(), {})

        big = {str(i): [vars(JudgeResult("r", False, ""))] for i in range(2005)}
        judge_mod._cache_store(big)
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
        self.assertEqual(len(stored), 1000)


if __name__ == "__main__":
    unittest.main()
