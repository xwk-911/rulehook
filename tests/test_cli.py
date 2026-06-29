import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from rulehook import cli
from rulehook.judge import JudgeResult


RULES_TEXT = """
[settings]
provider = "anthropic"
fail_open = true

[[rules]]
id = "det"
rule = "Never remove recursively."
events = ["pre_tool_use"]
tools = "Bash"
action = "deny"
pattern = "rm -rf"
pattern_only = true

[[rules]]
id = "scoped"
rule = "Never read secrets."
events = ["pre_tool_use"]
action = "deny"
pattern = "secret"

[[rules]]
id = "done"
rule = "Run tests before done."
events = ["stop"]
action = "remind"
"""


class CliCase(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()
        os.environ["RULEHOOK_MOCK"] = "1"
        os.environ.pop("RULEHOOK_RULES", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def run_cli(self, argv, stdin_text=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        stdin = io.StringIO(stdin_text) if stdin_text is not None else io.StringIO("")
        with redirect_stdout(stdout), redirect_stderr(stderr), mock.patch("sys.stdin", stdin):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def write_rules(self, directory=None, text=RULES_TEXT):
        directory = directory or tempfile.mkdtemp()
        path = os.path.join(directory, "rulehook.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_init_writes_starter_and_refuses_overwrite(self):
        d = tempfile.mkdtemp()
        code, out, err = self.run_cli(["init", "--dir", d])
        self.assertEqual(code, 0)
        self.assertIn("starter rules", out)
        self.assertEqual(err, "")
        self.assertTrue(os.path.isfile(os.path.join(d, ".rulehook", "rulehook.toml")))

        code, out, err = self.run_cli(["init", "--dir", d])
        self.assertEqual(code, 1)
        self.assertIn("Already exists", err)

        code, out, err = self.run_cli(["init", "--dir", d, "--force", "--preset", "security"])
        self.assertEqual(code, 0)
        self.assertIn("preset 'security'", out)

    def test_init_unknown_preset_lists_available_presets(self):
        code, out, err = self.run_cli(["init", "--dir", tempfile.mkdtemp(), "--preset", "missing"])
        self.assertEqual(code, 1)
        self.assertIn("Unknown preset 'missing'", err)
        self.assertIn("security", err)

    def test_check_prints_rule_modes(self):
        path = self.write_rules()
        code, out, err = self.run_cli(["check", "--rules", path])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("3 rule(s)", out)
        self.assertIn("(deterministic)", out)
        self.assertIn("(pattern-scoped)", out)
        self.assertIn("(llm)", out)

    def test_install_requires_rules_file_when_not_initialized(self):
        code, out, err = self.run_cli(["install", "--dir", tempfile.mkdtemp(), "--target", "all"])
        self.assertEqual(code, 1)
        self.assertIn("No rules file found", err)

    def test_install_all_auto_covers_claude_codex_and_cursor(self):
        d = tempfile.mkdtemp()
        rules_path = self.write_rules(d)
        code, out, err = self.run_cli([
            "install",
            "--target", "all",
            "--hook-mode", "auto",
            "--dir", d,
            "--rules", rules_path,
        ])
        self.assertEqual(code, 0)
        self.assertIn("[claude-code] hooks registered", out)
        self.assertIn("[codex] native prompt hooks are not supported", out)
        self.assertIn("Codex post-install checklist", out)
        self.assertIn("[cursor] hooks registered", out)

        with open(os.path.join(d, ".claude", "settings.json"), encoding="utf-8") as fh:
            claude_cfg = json.load(fh)
        with open(os.path.join(d, ".codex", "hooks.json"), encoding="utf-8") as fh:
            codex_cfg = json.load(fh)
        with open(os.path.join(d, ".cursor", "hooks.json"), encoding="utf-8") as fh:
            cursor_cfg = json.load(fh)

        self.assertEqual(claude_cfg["hooks"]["PreToolUse"][0]["hooks"][0]["type"], "prompt")
        self.assertEqual(codex_cfg["hooks"]["PreToolUse"][0]["hooks"][0]["type"], "command")
        self.assertEqual(cursor_cfg["hooks"]["preToolUse"][0]["type"], "prompt")

    def test_install_prints_codex_compatibility_warning(self):
        d = tempfile.mkdtemp()
        rules_path = self.write_rules(d)
        with mock.patch("rulehook.adapters.codex.compatibility_warning", return_value="upgrade codex"):
            code, out, err = self.run_cli([
                "install",
                "--target", "codex",
                "--dir", d,
                "--rules", rules_path,
            ])
        self.assertEqual(code, 0)
        self.assertIn("[codex] hooks registered", out)
        self.assertIn("[codex] warning: upgrade codex", err)

    def test_hook_short_circuits_inside_nested_judge(self):
        os.environ["RULEHOOK_IN_JUDGE"] = "1"
        code, out, err = self.run_cli(["hook", "--target", "codex"], "{}")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_hook_invalid_json_and_unsupported_events_fail_open(self):
        code, out, err = self.run_cli(["hook", "--target", "codex"], "{bad")
        self.assertEqual(code, 0)
        self.assertIn("could not parse", err)

        payload = json.dumps({"hook_event_name": "SessionStart"})
        code, out, err = self.run_cli(["hook", "--target", "codex"], payload)
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err, "")

    def test_hook_missing_rules_and_internal_errors_fail_open(self):
        payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": tempfile.mkdtemp()})
        code, out, err = self.run_cli(["hook", "--target", "codex"], payload)
        self.assertEqual(code, 0)
        self.assertIn("No rules file found", err)

        with mock.patch.dict(cli.ADAPTERS, {
            "codex": mock.Mock(parse=mock.Mock(side_effect=RuntimeError("boom")))
        }):
            code, out, err = self.run_cli(["hook", "--target", "codex"], "{}")
        self.assertEqual(code, 0)
        self.assertIn("internal error", err)

    def test_hook_success_emits_target_json(self):
        path = self.write_rules()
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/build"},
        })
        code, out, err = self.run_cli(["hook", "--target", "claude-code", "--rules", path], payload)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        rendered = json.loads(out)
        self.assertEqual(rendered["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("det", rendered["hookSpecificOutput"]["permissionDecisionReason"])

    def test_codex_hook_blocks_semantic_rule_with_judge(self):
        path = self.write_rules(text="""
[settings]
provider = "codex-cli"
fail_open = false

[[rules]]
id = "semantic"
rule = "Never echo the forbidden marker."
events = ["pre_tool_use"]
tools = "Bash"
action = "deny"
""")
        payload = json.dumps({
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo FORBIDDEN"},
        })
        os.environ.pop("RULEHOOK_MOCK", None)
        with mock.patch("rulehook.engine.judge", return_value=[
            JudgeResult("semantic", True, "the command echoes the forbidden marker")
        ]):
            code, out, err = self.run_cli(["hook", "--target", "codex", "--rules", path], payload)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        rendered = json.loads(out)
        self.assertEqual(rendered["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("semantic", rendered["hookSpecificOutput"]["permissionDecisionReason"])

    def test_test_command_prints_verdict_and_hook_output(self):
        os.environ["RULEHOOK_MOCK_VIOLATE"] = "done"
        path = self.write_rules()
        code, out, err = self.run_cli([
            "test",
            "--target", "cursor",
            "--event", "Stop",
            "--last-message", "all done",
            "--rules", path,
        ])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertIn("VIOLATION [remind] done", out)
        self.assertIn("followup_message", out)

        os.environ.pop("RULEHOOK_MOCK_VIOLATE")
        code, out, err = self.run_cli([
            "test",
            "--target", "claude-code",
            "--event", "PreToolUse",
            "--tool", "Bash",
            "--command", "echo hi",
            "--input-json", '{"command": "echo bye"}',
            "--prompt", "hello",
            "--rules", path,
        ])
        self.assertEqual(code, 0)
        self.assertIn("no violations", out)


if __name__ == "__main__":
    unittest.main()
