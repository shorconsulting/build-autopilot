"""Unit tests: recorded/synthetic gh JSON and a fake transcript under tests/fixtures; no network, no real gh."""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
NOW = dt.datetime(2026, 10, 15, 12, 0, tzinfo=dt.timezone.utc)
READ_ONLY_GH = {("api",), ("pr", "list"), ("issue", "list"), ("repo", "list")}


def fx(name):
    return (FIX / name).read_text()


class FakeGh:
    """Stands in for audit.gh. Answers from fixtures, records every call, and fails the test on any write."""

    def __init__(self, repo="acme/widgets", protection=404, rules=None, rollup_fails=False, codeowners=(".github/CODEOWNERS",)):
        self.repo, self.protection, self.rules = repo, protection, rules or []
        self.rollup_fails, self.codeowners = rollup_fails, set(codeowners)
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        verb = tuple(args[:2]) if args[0] != "api" else ("api",)
        assert verb in READ_ONLY_GH, "write or unknown gh call: %r" % (args,)
        for bad in ("-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"):
            assert bad not in args, "gh api write flag %s in %r" % (bad, args)
        if args[0] == "repo":
            return fx("repo_list.json")
        if args[0] == "issue":
            return fx("issues.json")
        if args[0] == "pr":
            if "open" in args:
                return fx("prs_open.json")
            if self.rollup_fails and "statusCheckRollup" in args[args.index("--json") + 1]:
                raise audit.GhError("GraphQL: timeout")
            prs = json.loads(fx("prs_merged.json"))
            if self.rollup_fails:
                for p in prs:
                    p.pop("statusCheckRollup")
            return json.dumps(prs)
        path = [a for a in args if a.startswith("repos/")][0]
        if path == "repos/%s" % self.repo:
            return fx("repo.json")
        if "/actions/runs" in path:
            return fx("runs_page1.json") if "page=1" in path else json.dumps({"workflow_runs": []})
        if path.endswith("/actions/workflows"):
            return fx("workflows.json")
        if "/contents/.github/workflows/test.yml" in path:
            return fx("workflow_test.yml")
        if "/contents/.github/workflows/deploy.yml" in path:
            return fx("workflow_deploy.yml")
        if "/contents/" in path:
            if path.split("/contents/")[1] in self.codeowners:
                return json.dumps({"name": "CODEOWNERS"})
            raise audit.GhError("Not Found (HTTP 404)", 404)
        if "/branches/main/protection" in path:
            if self.protection:
                raise audit.GhError("HTTP %d" % self.protection, self.protection)
            return json.dumps({})
        if "/rules/branches/main" in path:
            return json.dumps(self.rules)
        if "/check-runs" in path:
            return json.dumps({"check_runs": [{"name": "unit", "status": "completed", "conclusion": "success",
                                               "started_at": "2026-10-01T17:05:00Z", "completed_at": "2026-10-01T17:20:00Z"}]})
        raise AssertionError("unexpected gh call %r" % (args,))


class Base(unittest.TestCase):
    def setUp(self):
        self._gh = audit.gh
        self.since, self.days = audit.parse_since("30d", NOW)
        self.tz = audit.local_tz("America/Los_Angeles")
        audit._origin_cache.clear()

    def tearDown(self):
        audit.gh = self._gh

    def audit(self, fake=None, inter=None):
        audit.gh = fake or FakeGh()
        return audit.audit_repo("acme/widgets", self.since, NOW, self.days, inter or {}, self.tz)


class Helpers(Base):
    def test_since_parsing(self):
        s, d = audit.parse_since("30d", NOW)
        self.assertEqual(d, 30)
        self.assertEqual(s, NOW - dt.timedelta(days=30))
        self.assertEqual(audit.parse_since("2w", NOW)[1], 14)
        s, d = audit.parse_since("2026-10-01", NOW)
        self.assertEqual((s.day, d), (1, 14))
        with self.assertRaises(SystemExit):
            audit.parse_since("last week", NOW)

    def test_percentile_and_median(self):
        self.assertEqual(audit.percentile([10, 180, 780], 0.9), 660)
        self.assertIsNone(audit.percentile([], 0.9))
        self.assertEqual(audit.med_p90([]), {"median": None, "p90": None})

    def test_bot_detection(self):
        for login in ("coderabbitai[bot]", "claude", "sling-bot", "Copilot", "qodo-merge-pro", "x[bot]"):
            self.assertTrue(audit.is_bot(login), login)
        for login in ("bob", "alice-smith"):
            self.assertFalse(audit.is_bot(login), login)

    def test_overnight_window(self):
        tz = audit.local_tz("America/Los_Angeles")
        t = lambda s: audit.parse_ts(s)
        self.assertTrue(audit.spans_overnight(t("2026-10-02T02:00:00Z"), t("2026-10-02T15:00:00Z"), tz))
        self.assertFalse(audit.spans_overnight(t("2026-10-03T17:30:00Z"), t("2026-10-03T20:30:00Z"), tz))
        # 06:30 to 09:00 Pacific touches the 00:00-07:00 window
        self.assertTrue(audit.spans_overnight(t("2026-10-03T13:30:00Z"), t("2026-10-03T16:00:00Z"), tz))

    def test_categories_follow_the_documented_order(self):
        self.assertEqual(audit.categorize("Should I push and deploy?"), "merge_or_deploy_ok")
        self.assertEqual(audit.categorize("Which option do you prefer?"), "design_choice")
        self.assertEqual(audit.categorize("What password should it use?"), "credentials_or_access")
        self.assertEqual(audit.categorize("Do you want me to clarify scope?"), "scope_or_clarify")
        self.assertEqual(audit.categorize("Is the sky blue?"), "other")

    def test_trigger_parsing(self):
        t = audit.parse_triggers(fx("workflow_test.yml"))
        self.assertTrue(audit.push_to_branch(t, "main"))
        self.assertFalse(audit.push_to_branch(t, "dev"))
        self.assertTrue(t["pull_request"])
        t = audit.parse_triggers(fx("workflow_deploy.yml"))
        self.assertTrue(t["workflow_dispatch"])
        self.assertFalse(audit.push_to_branch(t, "main"))
        t = audit.parse_triggers("on: [push, pull_request]\njobs: {}\n")
        self.assertTrue(audit.push_to_branch(t, "main") and t["pull_request"])
        t = audit.parse_triggers("on:\n  push:\n    tags:\n      - 'v*'\njobs: {}\n")
        self.assertFalse(audit.push_to_branch(t, "main"))
        t = audit.parse_triggers("on:\n  push:\n    branches:\n      - main\n      - release/*\n  workflow_run:\n    workflows: [test]\n")
        self.assertTrue(audit.push_to_branch(t, "release/1") and t["workflow_run"])
        self.assertFalse(audit.parse_triggers("name: x\njobs: {}\n")["pull_request"])

    def test_ai_review_detection_ignores_a_faked_api_key(self):
        self.assertFalse(audit.looks_like_ai_review({"name": "test", "path": "t.yml"}, fx("workflow_test.yml")))
        self.assertTrue(audit.looks_like_ai_review({"name": "PR check", "path": "r.yml"}, fx("workflow_review.yml")))

    def test_remote_and_slug_helpers(self):
        self.assertEqual(audit.parse_remote("git@github.com:Acme/Widgets.git"), "Acme/Widgets")
        self.assertEqual(audit.parse_remote("https://github.com/Acme/Widgets"), "Acme/Widgets")
        self.assertIsNone(audit.parse_remote("https://example.com/x/y.git"))
        self.assertEqual(audit.encode_path("/Users/me/claude/.claude/x_y"), "-Users-me-claude--claude-x-y")


class RepoAudit(Base):
    def test_pull_request_numbers(self):
        p = self.audit()["prs"]
        self.assertEqual((p["merged"], p["open"]), (4, 1))
        self.assertEqual(p["open_to_merge_min"], {"median": 120.0, "p90": 651.0})
        self.assertEqual(p["ci_min"]["median"], 25.0)
        w = p["wait_after_green_min"]
        self.assertEqual((w["median"], w["p90"], w["total_hours"]), (180.0, 660.0, 16.2))
        self.assertEqual((p["waited_over_2h"], p["waited_overnight"]), (2, 1))
        self.assertEqual((p["human_reviews"], p["bot_reviews"], p["merged_without_review"]), (1, 1, 3))
        self.assertEqual((p["merged_after_tests_passed"], p["merged_without_tests"]), (3, 1))
        self.assertEqual(p["fix_rounds_median"], 1)
        self.assertEqual(p["test_fix_prs"], 1)
        self.assertEqual([e["number"] for e in p["examples_long_wait"]], [2, 3])
        self.assertEqual(p["examples_long_wait"][0]["wait_hours"], 13.0)

    def test_a_pr_tested_before_it_was_opened_waits_from_when_it_opened(self):
        prs = json.loads(fx("prs_merged.json"))[:1]
        prs[0]["createdAt"] = "2026-10-01T17:25:00Z"  # checks finished at 17:20, merged 17:30
        stats, waits, _ = audit.pr_metrics(prs, 0, [], self.since, {}, self.tz)
        self.assertEqual(waits, [5.0])

    def test_required_checks_decide_what_counts_as_green(self):
        prs = json.loads(fx("prs_merged.json"))[:1]  # unit done 17:20, lint done 17:08
        _, waits, _ = audit.pr_metrics(prs, 0, ["lint"], self.since, {}, self.tz)
        self.assertEqual(waits, [22.0])
        _, waits, _ = audit.pr_metrics(prs, 0, ["no-such-check"], self.since, {}, self.tz)  # falls back to every check
        self.assertEqual(waits, [10.0])

    def test_ci_numbers(self):
        ci = self.audit()["ci"]
        wf = ci["workflows"][0]
        self.assertEqual((wf["name"], wf["runs"], wf["median_min"], wf["cancelled"]), ("test.yml", 8, 20.0, 1))
        self.assertAlmostEqual(wf["failure_rate"], 0.286, places=3)
        self.assertEqual((ci["duplicate_main_runs"], ci["duplicate_main_minutes"], ci["red_main_commits"]), (2, 35, 1))
        self.assertEqual(ci["runs_per_merged_pr"], 2.0)

    def test_deploy_is_manual_when_only_dispatch(self):
        d = self.audit()["deploy"]
        self.assertEqual((d["workflow"], d["auto_on_main"], d["runs"]), ("deploy.yml", False, 1))
        self.assertEqual(d["median_merge_to_deploy_min"], 3160.0)
        self.assertIn("only runs when someone starts it", d["note"])

    def test_deploy_auto_and_none(self):
        wfs = [{"id": 22, "name": "Release", "path": ".github/workflows/release.yml", "state": "active"}]
        d = audit.deploy_metrics(wfs, {".github/workflows/release.yml": "on:\n  push:\n    branches: [main]\n"}, [], [], "main")
        self.assertTrue(d["auto_on_main"])
        d = audit.deploy_metrics([{"id": 1, "name": "test", "path": "t.yml", "state": "active"}], {}, [], [], "main")
        self.assertEqual(d["note"], "No deploy workflow found; deploys are manual")
        self.assertIsNone(d["workflow"])

    def test_settings(self):
        a = self.audit()["automation"]
        self.assertEqual((a["allow_auto_merge"], a["delete_branch_on_merge"]), (False, True))
        self.assertEqual(a["branch_protection"]["required_checks"], [])
        self.assertEqual(a["branch_protection"]["status"], "none")
        self.assertTrue(a["codeowners"])
        self.assertFalse(a["ai_review_workflow"])
        self.assertTrue(a["sling_labels_used"])

    def test_rulesets_count_as_protection(self):
        rules = [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}},
                 {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "unit"}]}}]
        bp = self.audit(FakeGh(rules=rules))["automation"]["branch_protection"]
        self.assertEqual((bp["required_reviews"], bp["required_checks"]), (1, ["unit"]))

    def test_protection_403_is_reported_unknown_not_none(self):
        r = self.audit(FakeGh(protection=403))
        self.assertEqual(r["automation"]["branch_protection"]["status"], "unknown")
        self.assertIsNone(r["automation"]["branch_protection"]["required_reviews"])
        self.assertTrue(any("403" in n for n in r["notes"]))

    def test_checks_fall_back_to_the_checks_api(self):
        fake = FakeGh(rollup_fails=True)
        p = self.audit(fake)["prs"]
        self.assertEqual(p["merged"], 4)
        self.assertTrue(any("check-runs" in " ".join(c) for c in fake.calls))
        self.assertIsNotNone(p["wait_after_green_min"]["median"])

    def test_unreadable_repo_becomes_an_error_record(self):
        def boom(args):
            raise audit.GhError("Not Found (HTTP 404)", 404)
        audit.gh = boom
        r = audit.audit_repo("acme/secret", self.since, NOW, self.days, {}, self.tz)
        self.assertEqual(set(r), {"repo", "error"})
        self.assertIn("Cannot read", r["error"])

    def test_a_failing_side_lookup_does_not_sink_the_repo(self):
        fake = FakeGh()
        orig = fake.__call__

        def flaky(args):
            if args[0] == "issue":
                raise audit.GhError("rate limited")
            return orig(args)
        audit.gh = flaky
        r = audit.audit_repo("acme/widgets", self.since, NOW, self.days, {}, self.tz)
        self.assertNotIn("error", r)
        self.assertFalse(r["automation"]["sling_labels_used"])
        self.assertTrue(any("Issue labels" in n for n in r["notes"]))

    def test_every_gh_call_is_read_only(self):
        fake = FakeGh()
        self.audit(fake)
        self.assertGreater(len(fake.calls), 8)  # FakeGh asserts on each call; this proves it saw them


class Transcripts(Base):
    def collect(self, dirs, checkouts=None, repos=("acme/widgets",)):
        return audit.collect_interruptions(dirs, list(repos), checkouts or {}, self.since, self.tz)

    def test_counts_by_name_and_worktree_folder(self):
        inter, meta = self.collect([str(FIX / "transcripts")])
        i = inter["acme/widgets"]
        self.assertEqual((i["sessions"], i["days_active"]), (2, 3))
        self.assertEqual((i["questions"], i["turns_ending_on_user"], i["permission_denials"], i["short_approvals"]), (3, 1, 1, 4))
        self.assertEqual(i["by_category"], {"merge_or_deploy_ok": 2, "permission": 1, "credentials_or_access": 1,
                                            "design_choice": 1, "scope_or_clarify": 0, "other": 0})
        self.assertEqual(i["per_active_day"], 1.7)
        self.assertEqual((meta["project_folders"], meta["mapped"], meta["unmapped"]), (3, 2, 1))

    def test_no_transcript_text_reaches_the_output(self):
        inter, meta = self.collect([str(FIX / "transcripts")])
        self.assertNotIn("SECRETPHRASE", json.dumps([inter, meta]))
        audit.gh = FakeGh()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "a.json")
            import io
            from contextlib import redirect_stdout, redirect_stderr
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                audit.main(["--repo", "acme/widgets", "--transcripts", str(FIX / "transcripts"), "--out", out], now=NOW)
            self.assertNotIn("SECRETPHRASE", Path(out).read_text())

    def test_checkout_override_and_git_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            proj = Path(tmp) / "projects"
            work = Path(tmp) / "work" / "totally-different-name"
            work.mkdir(parents=True)
            subprocess.run(["git", "-C", str(work), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(work), "remote", "add", "origin", "git@github.com:acme/widgets.git"], check=True)
            slug = audit.encode_path(str(work))
            for s in (slug, slug + "--claude-worktrees-foo-1"):  # the second folder no longer exists on disk
                (proj / s).mkdir(parents=True)
                shutil.copy(FIX / "transcripts/-Users-nobody-src-acme-widgets/session1.jsonl", proj / s / "x.jsonl")
            inter, meta = self.collect([str(proj)])  # origin remote, no --checkout
            self.assertEqual(meta["mapped"], 2)
            self.assertEqual(inter["acme/widgets"]["sessions"], 1)  # same sessionId in both copies
            audit._origin_cache.clear()
            inter, meta = self.collect([str(proj)], checkouts={"acme/widgets": str(work)})  # explicit override
            self.assertEqual(meta["mapped"], 2)

    def test_a_checkout_of_another_repo_is_not_claimed_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            work = Path(tmp) / "acme-widgets"  # looks like ours, but its origin says otherwise
            work.mkdir()
            subprocess.run(["git", "-C", str(work), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(work), "remote", "add", "origin", "https://github.com/someone/else.git"], check=True)
            proj = Path(tmp) / "projects"
            (proj / audit.encode_path(str(work))).mkdir(parents=True)
            self.assertEqual(self.collect([str(proj)])[1]["mapped"], 0)

    def test_unreadable_or_missing_dirs_are_reported_not_fatal(self):
        inter, meta = self.collect(["/definitely/not/here", str(FIX / "transcripts")])
        self.assertEqual(meta["dirs_unreadable"], ["/definitely/not/here"])
        self.assertEqual(inter["acme/widgets"]["sessions"], 2)


class Recommendations(Base):
    def recs(self, inter=None):
        r = self.audit(inter={"acme/widgets": inter} if inter else None)
        return {x["id"]: x for x in r["recommendations"]}

    def test_rules_fire_on_the_fixture(self):
        recs = self.recs()
        self.assertEqual(set(recs), {"auto-merge", "fast-lane-tests", "auto-deploy"})
        am = recs["auto-merge"]
        self.assertEqual(am["setting"], {"key": "merge", "value": "auto"})
        self.assertIn("2 of 4", am["why"])
        self.assertIn("16.2 hours", am["why"])
        self.assertTrue(any("sign-in" in c for c in am["cons"]))
        self.assertGreater(am["saves_hours_per_week"], 0)
        self.assertEqual(recs["auto-deploy"]["risk"], "high")
        self.assertEqual(recs["fast-lane-tests"]["setting"], {"key": "tests", "value": "fast-lane"})

    def test_each_recommendation_has_the_documented_shape(self):
        for rec in self.audit()["recommendations"]:
            self.assertEqual(set(rec), {"id", "title", "why", "saves_hours_per_week", "pros", "cons", "risk", "setting"})
            self.assertIn(rec["risk"], ("low", "medium", "high"))
            self.assertEqual(set(rec["setting"]), {"key", "value"})

    def test_interruption_rules(self):
        inter = audit.interruption_summary(audit.new_interruptions())
        inter.update(days_active=5, questions=10, turns_ending_on_user=20, permission_denials=6, short_approvals=12, per_active_day=7.2)
        inter["by_category"]["merge_or_deploy_ok"] = 9
        recs = self.recs(inter)
        self.assertIn("fewer-questions", recs)
        self.assertIn("12", recs["fewer-questions"]["why"])
        self.assertIn("9", recs["fewer-questions"]["why"])
        self.assertEqual(recs["permission-allowlist"]["setting"], {"key": "none", "value": None})

    def test_ai_review_and_duplicate_ci_and_clock_fixes(self):
        prs = json.loads(fx("prs_merged.json"))
        for p in prs[:3]:
            p["title"] = "Fix flaky clock test"
        stats, waits, merged = audit.pr_metrics(prs, 0, [], self.since, {}, self.tz)
        stats["human_reviews"] = 0
        ci = {"duplicate_main_runs": 22, "duplicate_main_minutes": 340, "red_main_commits": 0}
        dep = {"auto_on_main": True, "note": ""}
        auto = {"ai_review_workflow": False}
        inter = audit.interruption_summary(audit.new_interruptions())
        ids = {r["id"] for r in audit.recommendations(stats, ci, dep, auto, inter, waits, 30, "main")}
        self.assertTrue({"ai-review", "skip-duplicate-main-ci", "fix-clock-tests", "auto-merge"} <= ids)
        self.assertNotIn("auto-deploy", ids)
        auto["ai_review_workflow"] = True
        ids = {r["id"] for r in audit.recommendations(stats, ci, dep, auto, inter, waits, 30, "main")}
        self.assertNotIn("ai-review", ids)

    def test_nothing_to_recommend_without_data(self):
        stats, waits, _ = audit.pr_metrics([], 0, [], self.since, {}, self.tz)
        ci = {"duplicate_main_runs": 0, "duplicate_main_minutes": 0, "red_main_commits": 0}
        recs = audit.recommendations(stats, ci, {"auto_on_main": False, "note": ""}, {"ai_review_workflow": False},
                                     audit.interruption_summary(audit.new_interruptions()), waits, 30, "main")
        self.assertEqual(recs, [])


class EndToEnd(Base):
    def test_main_writes_json_and_prints_a_summary(self):
        audit.gh = FakeGh()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "sub", "audit.json")
            import io
            from contextlib import redirect_stdout, redirect_stderr
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                audit.main(["--repo", "acme/widgets", "--repo", "acme/widgets", "--since", "30d",
                            "--transcripts", str(FIX / "transcripts"), "--out", out], now=NOW)
            data = json.loads(Path(out).read_text())
            self.assertEqual(data["window_days"], 30)
            self.assertEqual(data["generated_at"], "2026-10-15T12:00:00Z")
            self.assertEqual(len(data["repos"]), 1)
            repo = data["repos"][0]
            for key in ("prs", "ci", "deploy", "automation", "interruptions", "recommendations"):
                self.assertIn(key, repo)
            self.assertEqual(repo["interruptions"]["sessions"], 2)
            self.assertIn("waited over 2 hours", buf.getvalue())
            self.assertIn("acme/widgets", buf.getvalue())

    def test_org_adds_only_active_unarchived_repos(self):
        fake = FakeGh()
        audit.gh = fake

        def routed(args):
            if args[0] == "api" and "repos/acme/gadgets" in args[-1]:
                raise audit.GhError("Not Found (HTTP 404)", 404)
            return fake(args)
        audit.gh = routed
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "audit.json")
            import io
            from contextlib import redirect_stdout, redirect_stderr
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                audit.main(["--org", "acme", "--transcripts", str(FIX / "transcripts"), "--out", out], now=NOW)
            names = [r["repo"] for r in json.loads(Path(out).read_text())["repos"]]
            self.assertEqual(names, ["acme/widgets", "acme/gadgets"])
            gadgets = json.loads(Path(out).read_text())["repos"][1]
            self.assertIn("error", gadgets)  # unreadable repo is a record, not a crash


if __name__ == "__main__":
    unittest.main()
