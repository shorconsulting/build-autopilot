"""Tests for scripts/lanes.py: phase presets, per-suite path rules, and rewriting a workflow's paths list.

No network. Run:
    python3 -m unittest scripts/test_lanes.py -v
"""
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lanes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "page" / "build-autopilot.html"

MOBILE = {"paths": ["apps/mobile/**", "packages/db/**", "packages/shared/**", "**/migrations/**"], "on_pr": "when-touched"}
POLICY = {"version": 1, "repo": "o/r", "tests": {"lane": "wave", "suites": {"mobile": MOBILE}}}

WORKFLOW = """name: mobile-apps
on:
  pull_request:
    paths:
      - "apps/mobile/**"
      # the workflow itself
      - ".github/workflows/mobile-apps.yml"
  schedule:
    - cron: "17 9 * * *"
  workflow_dispatch:

permissions:
  contents: read
"""


class Phases(unittest.TestCase):
    def test_missing_fields_come_from_the_phase(self):
        for phase, p in lanes.PRESETS.items():
            with self.subTest(phase):
                e = lanes.effective({"phase": phase})
                self.assertEqual(e["tests"]["lane"], p["lane"])
                self.assertEqual(e["level"], p["level"])
                self.assertEqual(e["sensitive"]["rule"], p["sensitive_rule"])

    def test_waves_are_the_default(self):
        self.assertEqual(lanes.DEFAULT_PHASE, "prelaunch")
        self.assertEqual(lanes.effective({})["tests"]["lane"], "wave")
        self.assertEqual(lanes.effective({"phase": "nonsense"})["phase"], "prelaunch")

    def test_a_field_that_is_set_is_never_overridden(self):
        e = lanes.effective({"phase": "customers", "level": "manual", "tests": {"lane": "wave"}, "sensitive": {"rule": "same"}})
        self.assertEqual((e["level"], e["tests"]["lane"], e["sensitive"]["rule"]), ("manual", "wave", "same"))

    def test_input_is_not_mutated(self):
        src = {"phase": "customers"}
        lanes.effective(src)
        self.assertEqual(src, {"phase": "customers"})

    def test_the_page_presets_match_this_table(self):
        html = PAGE.read_text(encoding="utf-8")
        block = re.search(r"const PHASES = \[(.*?)\n\];", html, re.S)
        self.assertIsNotNone(block, "page has no PHASES list")
        for phase, p in lanes.PRESETS.items():
            row = re.search(r'\{ id: "%s".*?preset: (\{[^}]*\})' % re.escape(phase), block.group(1), re.S)
            self.assertIsNotNone(row, f"page has no preset for {phase}")
            js = row.group(1)
            self.assertIn(f'lane: "{p["lane"]}"', js)
            self.assertIn(f'level: "{p["level"]}"', js)
            self.assertIn(f'rule: "{p["sensitive_rule"]}"', js)


class Suites(unittest.TestCase):
    def test_a_mobile_only_change_runs_mobile(self):
        self.assertIn("mobile", lanes.suites_to_run(POLICY, ["apps/mobile/src/App.tsx"]))

    def test_a_web_only_change_skips_mobile(self):
        self.assertEqual(lanes.suites_to_run(POLICY, ["apps/web/src/Home.tsx", "docs/waves/W22.md"]), {})

    def test_a_database_change_runs_mobile(self):
        for f in ("packages/db/migrations/0042_add_zone.sql", "packages/db/src/schema.ts", "services/x/migrations/001.sql"):
            with self.subTest(f):
                self.assertIn("mobile", lanes.suites_to_run(POLICY, ["apps/web/a.ts", f]))

    def test_main_and_the_schedule_run_everything(self):
        for ev in ("push", "schedule"):
            self.assertIn("mobile", lanes.suites_to_run(POLICY, [], event=ev))

    def test_on_pr_always_and_never(self):
        pol = {"tests": {"suites": {"a": {"on_pr": "always"}, "b": {"on_pr": "never", "paths": ["**"]}}}}
        self.assertEqual(set(lanes.suites_to_run(pol, ["x.md"])), {"a"})
        self.assertEqual(set(lanes.suites_to_run(pol, ["x.md"], event="push")), {"a", "b"})

    def test_dotfile_paths_match(self):
        self.assertTrue(lanes.matches(".github/workflows/x.yml", [".github/**"]))
        self.assertTrue(lanes.matches("./.github/x", [".github/**"]))

    def test_glob_rules(self):
        self.assertTrue(lanes.matches("a/b/c.ts", ["**/c.ts"]))
        self.assertTrue(lanes.matches("c.ts", ["**/c.ts"]))
        self.assertFalse(lanes.matches("a/b/c.ts", ["a/*.ts"]))
        self.assertTrue(lanes.matches("a/b/login.ts", ["**/*login*"]))
        self.assertTrue(lanes.matches("a/b/c.env", ["*.env"]), "no slash: matches at any depth")

    def test_validate(self):
        self.assertEqual(lanes.validate(POLICY), [])
        bad = {"tests": {"suites": {"m": {"on_pr": "sometimes"}, "n": {"paths": []}, "o": {"paths": [""]}}}}
        self.assertEqual(len(lanes.validate(bad)), 3)


class Render(unittest.TestCase):
    def test_replaces_only_the_paths_list(self):
        out = lanes.render_paths(WORKFLOW, MOBILE["paths"])
        self.assertIn('      - "packages/db/**"', out)
        self.assertIn('      - "**/migrations/**"', out)
        self.assertNotIn(".github/workflows/mobile-apps.yml", out)
        self.assertIn('- cron: "17 9 * * *"', out)
        self.assertIn("permissions:\n  contents: read", out)
        self.assertEqual(out.count("paths:"), 1)

    def test_is_stable(self):
        once = lanes.render_paths(WORKFLOW, MOBILE["paths"])
        self.assertEqual(lanes.render_paths(once, MOBILE["paths"]), once)

    def test_refuses_what_it_cannot_do_safely(self):
        with self.assertRaises(ValueError):
            lanes.render_paths("on:\n  pull_request:\n    paths-ignore:\n      - docs/**\n", ["a/**"])
        with self.assertRaises(ValueError):
            lanes.render_paths("on:\n  push:\n    branches: [main]\n", ["a/**"])
        with self.assertRaises(ValueError):
            lanes.render_paths(WORKFLOW, [])

    def test_a_paths_list_under_push_is_left_alone(self):
        wf = "on:\n  push:\n    paths:\n      - x/**\n  pull_request:\n    paths:\n      - y/**\n"
        out = lanes.render_paths(wf, ["z/**"])
        self.assertIn("- x/**", out)
        self.assertIn('- "z/**"', out)
        self.assertNotIn("y/**", out)


class Templates(unittest.TestCase):
    def test_the_wave_workflow_template_keeps_one_run_and_one_waiting(self):
        t = (ROOT / "templates" / "github" / "workflows" / "test-lanes.yml").read_text(encoding="utf-8")
        self.assertIn("cancel-in-progress: false", t)
        self.assertIn("workflow_dispatch", t)
        self.assertRegex(t, r"(?m)^\s+branches: \[main\]")

    def test_the_policy_example_in_the_docs_is_valid_json(self):
        doc = (ROOT / "docs" / "policy.md").read_text(encoding="utf-8")
        block = re.search(r"```json\n(\{.*?\n\})\n```", doc, re.S)
        pol = json.loads(block.group(1))
        self.assertEqual(lanes.validate(pol), [])


if __name__ == "__main__":
    unittest.main()


class NewKeys(unittest.TestCase):
    def test_missing_new_keys_get_defaults_and_set_values_win(self):
        pol = {"version": 1, "level": "ai-review", "waves": {"batches_per_pr": 9}}
        eff = lanes.effective(pol)
        self.assertEqual(eff["waves"]["batches_per_pr"], 9)
        self.assertEqual(eff["waves"]["sensitive_batches_per_pr"], 2)
        self.assertEqual(eff["level"], "ai-review")
        self.assertEqual(eff["review"]["batch_rows"], 8)
        self.assertEqual(eff["models"]["risky"], "opus")
        self.assertTrue(eff["tests"]["test_only_fast_lane"])
        self.assertNotIn("review", pol)   # the input is not mutated

    def test_defaults_file_is_valid(self):
        self.assertEqual(lanes.validate_new_keys(lanes.new_defaults()), [])

    def test_bad_values_are_reported(self):
        bad = {"waves": {"mode": "x", "batches_per_pr": 0}, "review": {"max_rounds": "2"}, "models": []}
        self.assertEqual(len(lanes.validate_new_keys(bad)), 4)

    def test_test_only(self):
        self.assertTrue(lanes.is_test_only(["apps/api/tests/a.test.ts", "e2e/login.spec.ts"]))
        self.assertFalse(lanes.is_test_only(["apps/api/tests/a.test.ts", "apps/api/src/a.ts"]))
        self.assertFalse(lanes.is_test_only([]))
        self.assertFalse(lanes.is_test_only([".github/workflows/ci.yml"]))
