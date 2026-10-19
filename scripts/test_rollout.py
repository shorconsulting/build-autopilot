"""Tests for scripts/autopilot-rollout.py with a fake GitHub client. No network.

    python3 -m unittest scripts/test_rollout.py -v
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("rollout", HERE / "autopilot-rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)

POLICY = {"version": 1, "repo": "o/r", "level": "manual", "questions": "ask-now", "always_ask": ["spend-money"],
          "sensitive": {"rule": "human", "paths": ["**/auth/**"]}}


class Fake:
    def __init__(self, repos):
        self.repos_ = repos  # repo -> {path: text}
        self.opened = []

    def default_branch(self, repo):
        return "main"

    def read(self, repo, path, ref):
        return self.repos_[repo].get(path)

    def open_pr(self, repo, base, files, title, body):
        self.opened.append((repo, base, files, title, body))
        return f"https://example/{repo}/pull/1"


def installed(policy):
    files = {d: s.read_text(encoding="utf-8") for d, s in rollout.FILES.items()}
    files[rollout.SETTINGS] = json.dumps(policy)
    return files


class Rollout(unittest.TestCase):
    def test_repo_without_settings_is_ignored(self):
        self.assertIsNone(rollout.plan_repo(Fake({"o/none": {}}), "o/none"))

    def test_defaults_added_and_choices_untouched(self):
        gh = Fake({"o/r": installed(POLICY)})
        plan = rollout.plan_repo(gh, "o/r")
        new = json.loads(plan["files"][rollout.SETTINGS])
        for k in ("level", "questions", "always_ask", "sensitive"):
            self.assertEqual(new[k], POLICY[k])
        self.assertEqual(new["waves"], {"batches_per_pr": 1, "sensitive_batches_per_pr": 1, "integration_pr": False})
        self.assertNotIn(".github/autopilot/gate.py", plan["files"], "identical gate files are left alone")

    def test_stale_gate_file_is_updated_and_second_run_is_clean(self):
        files = installed(dict(POLICY, waves={}, review={}))
        files[".github/autopilot/gate.py"] = "old"
        plan = rollout.plan_repo(Fake({"o/r": files}), "o/r")
        self.assertEqual(list(plan["files"]), [".github/autopilot/gate.py"])
        files[".github/autopilot/gate.py"] = plan["files"][".github/autopilot/gate.py"]
        self.assertEqual(rollout.plan_repo(Fake({"o/r": files}), "o/r")["files"], {})

    def test_existing_waves_value_is_kept(self):
        gh = Fake({"o/r": installed(dict(POLICY, waves={"batches_per_pr": 4}))})
        new = json.loads(rollout.plan_repo(gh, "o/r")["files"][rollout.SETTINGS])
        self.assertEqual(new["waves"], {"batches_per_pr": 4})

    def test_pipeline_own_gets_no_gate_files(self):
        gh = Fake({"o/r": {rollout.SETTINGS: json.dumps(dict(POLICY, pipeline="own"))}})
        self.assertEqual(rollout.plan_repo(gh, "o/r")["files"], {})

    def test_dry_run_opens_nothing_and_apply_opens_one_pr_per_repo(self):
        gh = Fake({"o/a": installed(POLICY), "o/b": installed(POLICY), "o/c": installed(dict(POLICY, waves={}, review={}))})
        rollout.run(gh, list(gh.repos_), apply=False, out=lambda *_: None)
        self.assertEqual(gh.opened, [])
        rollout.run(gh, list(gh.repos_), apply=True, out=lambda *_: None)
        self.assertEqual([o[0] for o in gh.opened], ["o/a", "o/b"])
        self.assertTrue(all(o[4].endswith("https://claude.ai/code/session_01FUoyGmC3grehZmd2BbgNuN") or "Claude Code" in o[4] for o in gh.opened))

    def test_apply_without_yes_is_refused(self):
        self.assertEqual(rollout.main(["--apply"]), 2)

    def test_invalid_settings_json_is_skipped_not_fatal(self):
        gh = Fake({"o/r": {rollout.SETTINGS: "{nope"}})
        self.assertEqual(rollout.plan_repo(gh, "o/r")["files"], {})


if __name__ == "__main__":
    unittest.main()
