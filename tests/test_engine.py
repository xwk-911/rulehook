import json
import os
import tempfile
import unittest
from unittest import mock

from rulehook.engine import AgentEvent, Violation, evaluate
from rulehook.judge import JudgeResult
from rulehook.rules import Rule, RuleSet, Settings


def ruleset(rules, **settings_kwargs):
    return RuleSet(settings=Settings(**settings_kwargs), rules=rules, source_path="<test>")


def event(event_name="pre_tool_use", tool="Bash", command="ls", **kw):
    return AgentEvent(
        platform="claude-code", event=event_name, tool_name=tool,
        tool_input={"command": command} if command else None, **kw,
    )


class TestEngine(unittest.TestCase):
    def setUp(self):
        os.environ["RULEHOOK_MOCK"] = "1"
        os.environ.pop("RULEHOOK_MOCK_VIOLATE", None)

    def tearDown(self):
        os.environ.pop("RULEHOOK_MOCK", None)
        os.environ.pop("RULEHOOK_MOCK_VIOLATE", None)

    def test_pattern_only_violation_is_deterministic(self):
        rs = ruleset([Rule(id="rmrf", rule="no rm -rf", events=["pre_tool_use"],
                           action="deny", pattern=r"rm\s+-rf", pattern_only=True)])
        v = evaluate(event(command="rm -rf /tmp"), rs)
        self.assertEqual(len(v.violations), 1)
        self.assertTrue(v.violations[0].deterministic)
        self.assertEqual(v.violations[0].action, "deny")

    def test_pattern_only_no_match_passes_without_llm(self):
        rs = ruleset([Rule(id="rmrf", rule="no rm -rf", events=["pre_tool_use"],
                           action="deny", pattern=r"rm\s+-rf", pattern_only=True)])
        with mock.patch("rulehook.engine.judge", side_effect=AssertionError("LLM must not be called")):
            v = evaluate(event(command="ls"), rs)
        self.assertEqual(v.violations, [])

    def test_pattern_scopes_llm_call(self):
        rs = ruleset([Rule(id="env", rule="no .env", events=["pre_tool_use"],
                           action="deny", pattern=r"\.env")])
        with mock.patch("rulehook.engine.judge", side_effect=AssertionError("must be scoped out")):
            v = evaluate(event(command="echo hello"), rs)
        self.assertEqual(v.violations, [])
        # mock judge marks violated because rule's own pattern matches action text
        v = evaluate(event(command="cat .env"), rs)
        self.assertEqual([x.rule_id for x in v.violations], ["env"])

    def test_mock_violate_env_forces_violation(self):
        os.environ["RULEHOOK_MOCK_VIOLATE"] = "done-check"
        rs = ruleset([Rule(id="done-check", rule="run tests first", events=["stop"], action="remind")])
        v = evaluate(event(event_name="stop", tool=None, command=None,
                           last_message="all done"), rs)
        self.assertEqual([x.action for x in v.violations], ["remind"])

    def test_judge_error_captured(self):
        del os.environ["RULEHOOK_MOCK"]  # force real judge path
        rs = ruleset([Rule(id="sem", rule="semantic", events=["pre_tool_use"])])
        from rulehook.judge import JudgeError
        with mock.patch("rulehook.engine.judge", side_effect=JudgeError("boom")):
            v = evaluate(event(), rs)
        self.assertEqual(v.violations, [])
        self.assertIn("boom", v.judge_error)

    def test_warn_and_deny_separated(self):
        os.environ["RULEHOOK_MOCK_VIOLATE"] = "w,d"
        rs = ruleset([
            Rule(id="w", rule="soft", events=["pre_tool_use"], action="warn"),
            Rule(id="d", rule="hard", events=["pre_tool_use"], action="deny"),
        ])
        v = evaluate(event(), rs)
        self.assertEqual([x.rule_id for x in v.warnings], ["w"])
        self.assertEqual([x.rule_id for x in v.blocking], ["d"])

    def test_reminder_text_restates_rule(self):
        os.environ["RULEHOOK_MOCK_VIOLATE"] = "d"
        rs = ruleset([Rule(id="d", rule="Never touch prod.", events=["pre_tool_use"], action="deny")])
        v = evaluate(event(), rs)
        text = v.reminder_text()
        self.assertIn("Never touch prod.", text)
        self.assertIn("continue with the task", text)

    def test_audit_log_written(self):
        log = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        rs = ruleset([Rule(id="rmrf", rule="no rm -rf", events=["pre_tool_use"],
                           action="deny", pattern=r"rm\s+-rf", pattern_only=True)],
                     log_file=log)
        evaluate(event(command="rm -rf /"), rs)
        with open(log, encoding="utf-8") as fh:
            record = json.loads(fh.readline())
        self.assertEqual(record["violations"][0]["rule_id"], "rmrf")

    def test_action_text_includes_outputs_prompts_and_final_messages(self):
        ev = AgentEvent(
            platform="codex",
            event="post_tool_use",
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            tool_response={"stdout": "hi"},
            prompt="please run it",
            last_message="done",
        )
        text = ev.action_text()
        self.assertIn('"command": "echo hi"', text)
        self.assertIn('"stdout": "hi"', text)
        self.assertIn("user_prompt: please run it", text)
        self.assertIn("assistant_final_message: done", text)

    def test_no_applicable_rules_returns_fail_open_setting(self):
        rs = ruleset([Rule(id="x", rule="Never X.", events=["stop"])], fail_open=False)
        v = evaluate(event(event_name="pre_tool_use"), rs)
        self.assertEqual(v.violations, [])
        self.assertFalse(v.fail_open)

    def test_unknown_judge_result_ids_are_ignored(self):
        rs = ruleset([Rule(id="known", rule="Never X.", events=["pre_tool_use"])])
        with mock.patch("rulehook.engine.judge", return_value=[
            JudgeResult("unknown", True, "ignored"),
            JudgeResult("known", True, "matched"),
        ]):
            v = evaluate(event(), rs)
        self.assertEqual([x.rule_id for x in v.violations], ["known"])
        self.assertEqual(v.violations[0].reason, "matched")

    def test_audit_log_failures_are_ignored(self):
        rs = ruleset([Rule(id="x", rule="Never X.", events=["pre_tool_use"],
                           pattern="ls", pattern_only=True)], log_file="/no/such/dir/audit.jsonl")
        v = evaluate(event(command="ls"), rs)
        self.assertEqual([x.rule_id for x in v.violations], ["x"])

    def test_verdict_helpers_only_classify_matching_actions(self):
        from rulehook.engine import Verdict

        v = Verdict(violations=[
            Violation("d", "D", "deny", ""),
            Violation("r", "R", "remind", ""),
            Violation("w", "W", "warn", ""),
        ])
        self.assertEqual([x.rule_id for x in v.blocking], ["d", "r"])
        self.assertEqual([x.rule_id for x in v.warnings], ["w"])


if __name__ == "__main__":
    unittest.main()
