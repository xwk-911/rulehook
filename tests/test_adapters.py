import json
import os
import re
import tempfile
import unittest

from rulehook.adapters import claude_code, codex, cursor
from rulehook.adapters.base import parse_stdin, render
from rulehook.engine import AgentEvent, Verdict, Violation
from rulehook.rules import Rule, RuleSet, Settings


def violation(action="deny", rule_id="r1"):
    return Violation(rule_id=rule_id, rule_text="Never do X.", action=action, reason="why")


class TestParsing(unittest.TestCase):
    def test_pre_tool_use_parsed(self):
        ev = parse_stdin("claude-code", {
            "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": "ls"}, "session_id": "s", "cwd": "/p",
        })
        self.assertEqual(ev.event, "pre_tool_use")
        self.assertIn("ls", ev.action_text())

    def test_unsupported_event_returns_none(self):
        self.assertIsNone(parse_stdin("codex", {"hook_event_name": "SessionStart"}))

    def test_subagent_stop_maps_to_stop(self):
        ev = parse_stdin("claude-code", {"hook_event_name": "SubagentStop"})
        self.assertEqual(ev.event, "stop")

    def test_cursor_shell_event_parsed(self):
        ev = cursor.parse({
            "hook_event_name": "beforeShellExecution",
            "command": "npm test",
            "cwd": "/p",
        })
        self.assertEqual(ev.event, "pre_tool_use")
        self.assertEqual(ev.tool_name, "Bash")
        self.assertIn("npm test", ev.action_text())

    def test_cursor_stop_event_parsed(self):
        ev = cursor.parse({"hook_event_name": "stop", "status": "completed"})
        self.assertEqual(ev.event, "stop")


class TestRendering(unittest.TestCase):
    def test_pre_tool_deny(self):
        ev = AgentEvent(platform="claude-code", event="pre_tool_use", tool_name="Bash")
        obj, code = render(ev, Verdict(violations=[violation()]))
        self.assertEqual(code, 0)
        self.assertEqual(obj["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Never do X.", obj["hookSpecificOutput"]["permissionDecisionReason"])

    def test_post_tool_block_carries_context(self):
        ev = AgentEvent(platform="codex", event="post_tool_use", tool_name="Bash")
        obj, _ = render(ev, Verdict(violations=[violation("remind")]))
        self.assertEqual(obj["decision"], "block")
        self.assertIn("Never do X.", obj["hookSpecificOutput"]["additionalContext"])

    def test_stop_blocks_to_force_continuation(self):
        ev = AgentEvent(platform="claude-code", event="stop", raw={})
        obj, _ = render(ev, Verdict(violations=[violation("remind")]))
        self.assertEqual(obj["decision"], "block")

    def test_stop_loop_guard(self):
        ev = AgentEvent(platform="claude-code", event="stop", raw={"stop_hook_active": True})
        obj, _ = render(ev, Verdict(violations=[violation("remind")]))
        self.assertNotIn("decision", obj)
        self.assertIn("systemMessage", obj)

    def test_prompt_remind_injects_context_without_block(self):
        ev = AgentEvent(platform="claude-code", event="user_prompt_submit", prompt="hi")
        obj, _ = render(ev, Verdict(violations=[violation("remind")]))
        self.assertNotIn("decision", obj)
        self.assertIn("additionalContext", obj["hookSpecificOutput"])

    def test_prompt_deny_blocks(self):
        ev = AgentEvent(platform="codex", event="user_prompt_submit", prompt="hi")
        obj, _ = render(ev, Verdict(violations=[violation("deny")]))
        self.assertEqual(obj["decision"], "block")

    def test_warn_never_blocks(self):
        ev = AgentEvent(platform="claude-code", event="pre_tool_use", tool_name="Bash")
        obj, code = render(ev, Verdict(violations=[violation("warn")]))
        self.assertEqual(code, 0)
        self.assertNotIn("hookSpecificOutput", obj)
        self.assertIn("warning", obj["systemMessage"])

    def test_fail_closed_blocks_pre_tool_on_judge_error(self):
        ev = AgentEvent(platform="claude-code", event="pre_tool_use", tool_name="Bash")
        obj, _ = render(ev, Verdict(judge_error="down", fail_open=False))
        self.assertEqual(obj["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_fail_open_allows_with_message(self):
        ev = AgentEvent(platform="claude-code", event="pre_tool_use", tool_name="Bash")
        obj, _ = render(ev, Verdict(judge_error="down", fail_open=True))
        self.assertNotIn("hookSpecificOutput", obj)
        self.assertIn("judge unavailable", obj["systemMessage"])

    def test_codex_stop_always_emits_json(self):
        ev = AgentEvent(platform="codex", event="stop", raw={})
        obj, _ = codex.render(ev, Verdict())
        self.assertEqual(obj, {"continue": True})

    def test_cursor_pre_tool_deny(self):
        ev = AgentEvent(platform="cursor", event="pre_tool_use", tool_name="Bash")
        obj, code = cursor.render(ev, Verdict(violations=[violation()]))
        self.assertEqual(code, 0)
        self.assertEqual(obj["permission"], "deny")
        self.assertIn("Never do X.", obj["agent_message"])

    def test_cursor_stop_followup(self):
        ev = AgentEvent(platform="cursor", event="stop", raw={"loop_count": 0})
        obj, _ = cursor.render(ev, Verdict(violations=[violation("remind")]))
        self.assertIn("followup_message", obj)
        self.assertIn("Never do X.", obj["followup_message"])

    def test_cursor_prompt_deny_blocks_submit(self):
        ev = AgentEvent(platform="cursor", event="user_prompt_submit", prompt="hi")
        obj, _ = cursor.render(ev, Verdict(violations=[violation("deny")]))
        self.assertFalse(obj["continue"])


class TestInstall(unittest.TestCase):
    def test_claude_install_merges_and_is_idempotent(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "settings.json"), "w") as fh:
            json.dump({"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}
            ]}}, fh)
        claude_code.install(d, rules_path=None)
        claude_code.install(d, rules_path=None)
        with open(os.path.join(d, ".claude", "settings.json")) as fh:
            cfg = json.load(fh)
        cmds = [h["command"] for g in cfg["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertEqual(sum("echo mine" in c for c in cmds), 1)
        self.assertEqual(sum(bool(re.search(r'rulehook[\'"]?\s+hook', c)) for c in cmds), 1)
        for ev in ("PostToolUse", "UserPromptSubmit", "Stop"):
            self.assertIn(ev, cfg["hooks"])

    def test_codex_install_idempotent(self):
        d = tempfile.mkdtemp()
        codex.install(d, rules_path=None)
        codex.install(d, rules_path=None)
        with open(os.path.join(d, ".codex", "hooks.json")) as fh:
            cfg = json.load(fh)
        cmds = [h["command"] for g in cfg["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(len(cmds), 1)

    def test_codex_native_prompt_mode_falls_back_to_command(self):
        d = tempfile.mkdtemp()
        rs = RuleSet(Settings(), [Rule(id="x", rule="Never X.")], "<test>")
        codex.install(d, rules_path=None, mode="native-prompt", ruleset=rs)
        with open(os.path.join(d, ".codex", "hooks.json")) as fh:
            cfg = json.load(fh)
        hook = cfg["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook["type"], "command")

    def test_claude_native_prompt_install(self):
        d = tempfile.mkdtemp()
        rs = RuleSet(Settings(timeout=7), [Rule(id="x", rule="Never X.")], "<test>")
        claude_code.install(d, rules_path=None, mode="native-prompt", ruleset=rs)
        with open(os.path.join(d, ".claude", "settings.json")) as fh:
            cfg = json.load(fh)
        hook = cfg["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertEqual(hook["type"], "prompt")
        self.assertIn("Never X.", hook["prompt"])
        self.assertEqual(hook["timeout"], 7)

    def test_cursor_install_command_idempotent(self):
        d = tempfile.mkdtemp()
        cursor.install(d, rules_path=None)
        cursor.install(d, rules_path=None)
        with open(os.path.join(d, ".cursor", "hooks.json")) as fh:
            cfg = json.load(fh)
        cmds = [h["command"] for h in cfg["hooks"]["preToolUse"]]
        self.assertEqual(len(cmds), 1)
        self.assertIn("rulehook hook --target cursor", cmds[0])

    def test_cursor_native_prompt_install(self):
        d = tempfile.mkdtemp()
        rs = RuleSet(Settings(timeout=9), [Rule(id="x", rule="Never X.", events=["stop"])], "<test>")
        cursor.install(d, rules_path=None, mode="native-prompt", ruleset=rs)
        with open(os.path.join(d, ".cursor", "hooks.json")) as fh:
            cfg = json.load(fh)
        hook = cfg["hooks"]["stop"][0]
        self.assertEqual(hook["type"], "prompt")
        self.assertIn("Never X.", hook["prompt"])
        self.assertEqual(hook["timeout"], 9)

    def test_malformed_settings_refused(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "settings.json"), "w") as fh:
            fh.write("{broken")
        with self.assertRaises(SystemExit):
            claude_code.install(d, rules_path=None)


if __name__ == "__main__":
    unittest.main()
