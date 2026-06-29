import os
import tempfile
import unittest
from unittest import mock

from rulehook.rules import Rule, load_rules, find_rules_file

VALID = """
[settings]
provider = "anthropic"
fail_open = false

[[rules]]
id = "a"
rule = "Never do X."
events = ["pre_tool_use", "stop"]
tools = "Bash"
action = "deny"
pattern = "x"

[[rules]]
id = "b"
rule = "Always do Y."
events = ["stop"]
action = "remind"
"""


class TestRulesLoading(unittest.TestCase):
    def _write(self, text: str, name: str = "rulehook.toml") -> str:
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_load_valid(self):
        rs = load_rules(self._write(VALID))
        self.assertEqual(len(rs.rules), 2)
        self.assertFalse(rs.settings.fail_open)
        self.assertEqual(rs.rules[0].action, "deny")

    def test_for_event_filters_by_event_and_tool(self):
        rs = load_rules(self._write(VALID))
        self.assertEqual([r.id for r in rs.for_event("stop", None)], ["a", "b"])
        self.assertEqual([r.id for r in rs.for_event("pre_tool_use", "Bash")], ["a"])
        self.assertEqual([r.id for r in rs.for_event("pre_tool_use", "Edit")], [])
        self.assertEqual([r.id for r in rs.for_event("post_tool_use", "Bash")], [])

    def test_invalid_event_rejected(self):
        with self.assertRaises(ValueError):
            Rule(id="x", rule="r", events=["before_tool"]).validate()

    def test_invalid_action_rejected(self):
        with self.assertRaises(ValueError):
            Rule(id="x", rule="r", action="explode").validate()

    def test_invalid_regex_rejected(self):
        with self.assertRaises(ValueError):
            Rule(id="x", rule="r", pattern="[").validate()

    def test_pattern_only_requires_pattern(self):
        with self.assertRaises(ValueError):
            Rule(id="x", rule="r", pattern_only=True).validate()

    def test_duplicate_ids_rejected(self):
        text = VALID + '\n[[rules]]\nid = "a"\nrule = "dup"\n'
        with self.assertRaises(ValueError):
            load_rules(self._write(text))

    def test_env_var_discovery(self):
        path = self._write(VALID)
        os.environ["RULEHOOK_RULES"] = path
        try:
            self.assertEqual(find_rules_file(), path)
        finally:
            del os.environ["RULEHOOK_RULES"]

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_rules_file(explicit="/nonexistent/rules.toml")

    def test_env_var_missing_file_raises(self):
        os.environ["RULEHOOK_RULES"] = "/nonexistent/from-env.toml"
        try:
            with self.assertRaises(FileNotFoundError):
                find_rules_file()
        finally:
            del os.environ["RULEHOOK_RULES"]

    def test_discovers_rules_in_project_directories(self):
        base = tempfile.mkdtemp()
        os.makedirs(os.path.join(base, ".rulehook"))
        path = os.path.join(base, ".rulehook", "rules.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(VALID)
        self.assertEqual(find_rules_file(cwd=base), path)

    def test_json_rules_supported(self):
        path = self._write(
            '{"settings": {}, "rules": [{"id": "j", "rule": "Never Z.", "events": ["stop"]}]}',
            name="rules.json",
        )
        rs = load_rules(path)
        self.assertEqual(rs.rules[0].id, "j")

    def test_json_content_with_toml_extension_falls_back_to_json(self):
        path = self._write(
            '{"settings": {"timeout": "8", "cache": false, "max_chars": "99"}, '
            '"rules": [{"id": "j", "rule": "Never Z.", "events": ["stop"], "enabled": false}]}',
        )
        rs = load_rules(path)
        self.assertEqual(rs.settings.timeout, 8)
        self.assertFalse(rs.settings.cache)
        self.assertEqual(rs.settings.max_chars, 99)
        self.assertFalse(rs.rules[0].enabled)

    def test_empty_required_fields_rejected(self):
        with self.assertRaises(ValueError):
            Rule(id="", rule="r").validate()
        with self.assertRaises(ValueError):
            Rule(id="x", rule="").validate()

    def test_for_event_tool_regex_applies_only_when_tool_known(self):
        path = self._write(VALID)
        rs = load_rules(path)
        self.assertEqual([r.id for r in rs.for_event("pre_tool_use", None)], ["a"])

    def test_missing_default_rules_reports_search_locations(self):
        with mock.patch("os.getcwd", return_value=tempfile.mkdtemp()):
            with self.assertRaisesRegex(FileNotFoundError, "Searched:"):
                find_rules_file()


if __name__ == "__main__":
    unittest.main()
