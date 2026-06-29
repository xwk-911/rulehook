import json
import os
import re
import tempfile
import unittest
from unittest import mock

from rulehook.adapters import claude_code, codex, cursor
from rulehook.adapters import native_prompt
from rulehook.adapters import base as adapter_base
from rulehook.adapters.base import emit, parse_stdin, render
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

    def test_target_adapters_delegate_common_payloads(self):
        for adapter, platform in ((claude_code, "claude-code"), (codex, "codex")):
            ev = adapter.parse({"hook_event_name": "PostToolUse", "tool_name": "Bash"})
            self.assertEqual(ev.platform, platform)
            self.assertEqual(ev.event, "post_tool_use")
            obj, code = adapter.render(ev, Verdict())
            self.assertEqual((obj, code), ({}, 0))

    def test_cursor_discovers_event_name_from_payload_key(self):
        ev = cursor.parse({"beforeReadFile": True, "file_path": "a.txt", "content": "hello"})
        self.assertEqual(ev.event, "pre_tool_use")
        self.assertEqual(ev.tool_name, "Read")
        self.assertEqual(ev.tool_input["file_path"], "a.txt")

    def test_cursor_unknown_event_returns_none(self):
        self.assertIsNone(cursor.parse({"hook_event_name": "unknown"}))

    def test_cursor_mcp_payloads_parse_json_and_text(self):
        ev = cursor.parse({
            "hook_event_name": "beforeMCPExecution",
            "tool_name": "mcp.run",
            "tool_input": '{"a": 1}',
            "conversation_id": "c",
            "project_dir": "/repo",
        })
        self.assertEqual(ev.tool_input, {"a": 1})
        self.assertEqual(ev.session_id, "c")
        self.assertEqual(ev.cwd, "/repo")

        ev = cursor.parse({
            "hook_event_name": "afterMCPExecution",
            "tool_name": "mcp.run",
            "tool_input": "not json",
            "result_json": {"ok": True},
        })
        self.assertEqual(ev.event, "post_tool_use")
        self.assertEqual(ev.tool_input, {"input": "not json"})
        self.assertEqual(ev.tool_response, {"ok": True})

    def test_cursor_other_tool_payload_shapes(self):
        shell = cursor.parse({"hook_event_name": "afterShellExecution", "command": "make", "output": "ok"})
        self.assertEqual(shell.tool_response, "ok")
        edit = cursor.parse({"hook_event_name": "afterFileEdit", "file_path": "x.py", "edits": [1]})
        self.assertEqual(edit.tool_name, "Edit")
        fallback = cursor.parse({"hook_event_name": "postToolUseFailure", "tool": "Run", "args": {"x": 1}, "result": "bad"})
        self.assertEqual(fallback.tool_name, "Run")
        self.assertEqual(fallback.tool_input, {"x": 1})
        self.assertEqual(fallback.tool_response, "bad")


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

    def test_render_falls_through_unknown_event(self):
        ev = AgentEvent(platform="claude-code", event="custom")
        obj, code = render(ev, Verdict(violations=[violation("deny")]))
        self.assertEqual((obj, code), ({}, 0))

    def test_emit_prints_json_only_when_present(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(emit(({"ok": True}, 5)), 5)
        self.assertEqual(json.loads(printed.call_args.args[0]), {"ok": True})
        with mock.patch("builtins.print") as printed:
            self.assertEqual(emit(({}, 6)), 6)
        printed.assert_not_called()

    def test_cursor_render_warning_and_fail_closed_paths(self):
        ev = AgentEvent(platform="cursor", event="pre_tool_use", tool_name="Bash")
        obj, _ = cursor.render(ev, Verdict(violations=[violation("warn")], judge_error="slow"))
        self.assertIn("judge unavailable", obj["user_message"])
        self.assertIn("warning", obj["user_message"])

        obj, _ = cursor.render(ev, Verdict(judge_error="down", fail_open=False))
        self.assertEqual(obj["permission"], "deny")

    def test_cursor_post_tool_prompt_remind_stop_guard_and_fallback(self):
        obj, _ = cursor.render(
            AgentEvent(platform="cursor", event="post_tool_use"),
            Verdict(violations=[violation("remind")]),
        )
        self.assertEqual(obj["permission"], "deny")

        obj, _ = cursor.render(
            AgentEvent(platform="cursor", event="user_prompt_submit"),
            Verdict(violations=[violation("remind")]),
        )
        self.assertTrue(obj["continue"])
        self.assertIn("agent_message", obj)

        obj, _ = cursor.render(
            AgentEvent(platform="cursor", event="stop", raw={"loop_count": 1}),
            Verdict(violations=[violation("remind")]),
        )
        self.assertIn("already ran", obj["user_message"])

        obj, _ = cursor.render(
            AgentEvent(platform="cursor", event="custom"),
            Verdict(violations=[violation("remind")]),
        )
        self.assertEqual(obj, {})


class TestInstall(unittest.TestCase):
    def test_claude_install_merges_and_is_idempotent(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
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

    def test_hook_command_quotes_executable_and_rules_path(self):
        rules_path = os.path.join(tempfile.mkdtemp(), "my rules.toml")
        for module, target in (
            (claude_code, "claude-code"),
            (codex, "codex"),
            (cursor, "cursor"),
        ):
            with mock.patch.object(adapter_base, "rulehook_executable", return_value='"/tmp/bin/rule hook"'):
                cmd = module._hook_command(rules_path)
            self.assertTrue(cmd.startswith('"/tmp/bin/rule hook" hook --target ' + target))
            self.assertIn(f'--rules "{os.path.abspath(rules_path)}"', cmd)

    def test_rulehook_executable_prefers_path_then_current_script(self):
        with mock.patch("rulehook.adapters.base.shutil.which", return_value="/tmp/bin/rulehook"):
            self.assertEqual(adapter_base.rulehook_executable(), "/tmp/bin/rulehook")

        script = os.path.join(tempfile.mkdtemp(), "rulehook")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n")
        with mock.patch("rulehook.adapters.base.shutil.which", return_value=None), \
             mock.patch("rulehook.adapters.base.sys.argv", [script]):
            self.assertEqual(adapter_base.rulehook_executable(), script)

        with mock.patch("rulehook.adapters.base.shutil.which", return_value="/tmp/bin/rule hook"):
            self.assertEqual(adapter_base.rulehook_executable(), '"/tmp/bin/rule hook"')

    def test_rulehook_handler_detection_covers_prompt_hooks(self):
        self.assertTrue(claude_code._is_rulehook("python -m rulehook hook --target claude-code"))
        self.assertTrue(codex._is_rulehook("rulehook hook --target codex"))
        self.assertTrue(cursor._is_rulehook_handler({"prompt": "You are rulehook, check this"}))
        self.assertFalse(cursor._is_rulehook_handler({"command": "echo ok"}))

    def test_codex_install_idempotent(self):
        d = tempfile.mkdtemp()
        codex.install(d, rules_path=None)
        codex.install(d, rules_path=None)
        with open(os.path.join(d, ".codex", "hooks.json")) as fh:
            cfg = json.load(fh)
        cmds = [h["command"] for g in cfg["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(len(cmds), 1)

    def test_codex_install_keeps_non_rulehook_hooks_and_refuses_bad_json(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".codex"))
        hooks_path = os.path.join(d, ".codex", "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as fh:
            json.dump({
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "command", "command": "echo mine"}]},
                        {"hooks": [{"type": "command", "command": "rulehook hook --target codex"}]},
                    ]
                }
            }, fh)
        codex.install(d, rules_path=None)
        with open(hooks_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cmds = [h["command"] for g in cfg["hooks"]["PreToolUse"] for h in g["hooks"]]
        self.assertIn("echo mine", cmds)
        self.assertEqual(sum("rulehook hook --target codex" in c for c in cmds), 1)

        with open(hooks_path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        with self.assertRaises(SystemExit):
            codex.install(d, rules_path=None)

    def test_codex_version_warning(self):
        completed = mock.Mock(stdout="codex-cli 0.139.0", stderr="")
        with mock.patch("rulehook.adapters.codex.shutil.which", return_value="/bin/codex"), \
             mock.patch("rulehook.adapters.codex.subprocess.run", return_value=completed):
            self.assertEqual(codex._codex_version(), (0, 139, 0))
            self.assertIn("require codex-cli >= 0.142.0", codex.compatibility_warning())

        completed.stdout = "codex-cli 0.142.3"
        with mock.patch("rulehook.adapters.codex.shutil.which", return_value="/bin/codex"), \
             mock.patch("rulehook.adapters.codex.subprocess.run", return_value=completed):
            self.assertIsNone(codex.compatibility_warning())

        with mock.patch("rulehook.adapters.codex.shutil.which", return_value=None):
            self.assertIn("not found", codex.compatibility_warning())

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

    def test_claude_native_prompt_requires_ruleset_and_cleans_old_hooks(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            claude_code.install(d, rules_path=None, mode="native-prompt")

        os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
        with open(os.path.join(d, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"type": "prompt", "prompt": "You are rulehook, old"}]},
                        {"hooks": [{"type": "command", "command": "echo mine"}]},
                    ],
                    "Stop": [
                        {"hooks": [{"type": "prompt", "prompt": "You are rulehook, old"}]}
                    ],
                }
            }, fh)
        rs = RuleSet(Settings(), [Rule(id="x", rule="Never X.", events=["stop"])], "<test>")
        claude_code.install(d, rules_path=None, mode="native-prompt", ruleset=rs)
        with open(os.path.join(d, ".claude", "settings.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "echo mine")
        self.assertEqual(cfg["hooks"]["Stop"][0]["hooks"][0]["type"], "prompt")

    def test_cursor_install_command_idempotent(self):
        d = tempfile.mkdtemp()
        cursor.install(d, rules_path=None)
        cursor.install(d, rules_path=None)
        with open(os.path.join(d, ".cursor", "hooks.json")) as fh:
            cfg = json.load(fh)
        cmds = [h["command"] for h in cfg["hooks"]["preToolUse"]]
        self.assertEqual(len(cmds), 1)
        self.assertIn("rulehook hook --target cursor", cmds[0])

    def test_cursor_install_merges_keeps_version_and_refuses_bad_json(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".cursor"), exist_ok=True)
        hooks_path = os.path.join(d, ".cursor", "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as fh:
            json.dump({
                "version": 2,
                "hooks": {
                    "preToolUse": [
                        {"command": "echo mine"},
                        {"command": "rulehook hook --target cursor"},
                    ]
                },
            }, fh)
        cursor.install(d, rules_path=None)
        with open(hooks_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["version"], 2)
        cmds = [h["command"] for h in cfg["hooks"]["preToolUse"]]
        self.assertIn("echo mine", cmds)
        self.assertEqual(sum("rulehook hook --target cursor" in c for c in cmds), 1)

        with open(hooks_path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        with self.assertRaises(SystemExit):
            cursor.install(d, rules_path=None)

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

    def test_cursor_native_prompt_requires_ruleset_and_removes_stale_events(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ValueError):
            cursor.install(d, rules_path=None, mode="native-prompt")

        os.makedirs(os.path.join(d, ".cursor"), exist_ok=True)
        hooks_path = os.path.join(d, ".cursor", "hooks.json")
        with open(hooks_path, "w", encoding="utf-8") as fh:
            json.dump({
                "hooks": {
                    "preToolUse": [
                        {"type": "prompt", "prompt": "You are rulehook, old"},
                        {"command": "echo mine"},
                    ],
                    "stop": [
                        {"type": "prompt", "prompt": "You are rulehook, old"}
                    ],
                }
            }, fh)
        rs = RuleSet(Settings(), [Rule(id="x", rule="Never X.", events=["stop"])], "<test>")
        cursor.install(d, rules_path=None, mode="native-prompt", ruleset=rs)
        with open(hooks_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["hooks"]["preToolUse"][0]["command"], "echo mine")
        self.assertEqual(cfg["hooks"]["stop"][0]["type"], "prompt")

    def test_malformed_settings_refused(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, ".claude"))
        with open(os.path.join(d, ".claude", "settings.json"), "w") as fh:
            fh.write("{broken")
        with self.assertRaises(SystemExit):
            claude_code.install(d, rules_path=None)

    def test_native_prompt_helpers_filter_enabled_blocking_rules(self):
        self.assertTrue(native_prompt.supports_native_prompt("claude-code"))
        self.assertTrue(native_prompt.supports_native_prompt("cursor"))
        self.assertFalse(native_prompt.supports_native_prompt("codex"))

        rs = RuleSet(Settings(timeout=3), [
            Rule(id="warn", rule="Soft.", action="warn", events=["pre_tool_use"]),
            Rule(id="off", rule="Off.", enabled=False, events=["pre_tool_use"]),
            Rule(id="deny", rule="Hard.", action="deny", events=["pre_tool_use"]),
        ], "<test>")
        specs = native_prompt.specs_for_ruleset(rs)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].event, "pre_tool_use")
        self.assertEqual(specs[0].timeout, 3)
        self.assertIn('"id": "deny"', specs[0].prompt)
        self.assertNotIn('"id": "warn"', specs[0].prompt)


if __name__ == "__main__":
    unittest.main()
