import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import finish_guard  # noqa: E402
import steward  # noqa: E402

NOW = dt.datetime(2026, 11, 15, 21, 0, tzinfo=dt.timezone.utc)
REPO = "acme/app"


def run(id_, name, status="completed", conclusion="success", started="2026-11-15T20:00:00Z"):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion, "started_at": started,
            "details_url": "https://github.com/acme/app/actions/runs/1/job/%d" % id_}


def job(id_, runner, conclusion="success", step=None):
    steps = [{"name": step, "conclusion": "failure"}] if step else [{"name": "Run tests", "conclusion": conclusion}]
    return {"id": id_, "runner_name": runner, "conclusion": conclusion, "steps": steps,
            "completed_at": "2026-11-15T20:10:00Z"}


def pr(n, sha, base="main", head=None, draft=False, created="2026-11-10T00:00:00Z", updated="2026-11-15T20:00:00Z",
       labels=(), auto=False):
    return {"number": n, "title": "PR %d" % n, "draft": draft, "created_at": created, "updated_at": updated,
            "head": {"sha": sha, "ref": head or "feature/%d" % n}, "base": {"ref": base},
            "labels": [{"name": l} for l in labels], "auto_merge": {"x": 1} if auto else None}


class FakeGitHub:
    """Answers the REST paths the steward reads, from dictionaries."""

    def __init__(self, pulls, checks, jobs=None, mergeable=None, main_runs=(), wip=(), runs=()):
        self.pulls, self.checks, self.jobs = pulls, checks, jobs or {}
        self.mergeable, self.main_runs, self.wip, self.runs = mergeable or {}, list(main_runs), list(wip), list(runs)
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        base = path.split("?")[0]
        if base == "repos/%s" % REPO:
            return {"default_branch": "main"}
        if base == "repos/%s/pulls" % REPO:
            return self.pulls if path.endswith("&page=1") else []
        if base.startswith("repos/%s/pulls/" % REPO):
            n = int(base.rsplit("/", 1)[1])
            return {"mergeable_state": self.mergeable.get(n, "clean")}
        if base.endswith("/check-runs"):
            return {"check_runs": self.checks.get(base.split("/")[-2], [])}
        if base.startswith("repos/%s/actions/jobs/" % REPO):
            return self.jobs.get(int(base.rsplit("/", 1)[1]), {})
        if base == "repos/%s/actions/runs" % REPO:
            if "event=push" in path:
                return {"workflow_runs": self.main_runs}
            return {"workflow_runs": [r for r, _ in self.runs]}
        if base.endswith("/jobs"):
            rid = int(base.split("/")[-2])
            return {"jobs": next(j for r, j in self.runs if r["id"] == rid)}
        if "matching-refs" in base:
            return [{"ref": "refs/heads/" + b} for b in self.wip]
        raise AssertionError("unexpected path " + path)


class Sweep(unittest.TestCase):
    def sweep(self, fake, **rule_overrides):
        rules = steward.load_rules()
        for k, v in rule_overrides.items():
            rules["steward"][k] = v
        with mock.patch.object(steward, "api", fake):
            rep = steward.Sweeper(rules, now=NOW).sweep_repo(REPO)
        return rep, steward.actions_for(rep, rules)

    def states(self, rep):
        return {p["number"]: p["state"] for p in rep["pull_requests"]}

    def test_sorts_each_pr_into_one_state(self):
        fake = FakeGitHub(
            pulls=[pr(1, "a"), pr(2, "b"), pr(3, "c"), pr(4, "d", draft=True, updated="2026-11-01T00:00:00Z"),
                   pr(5, "e", base="feature/1"), pr(6, "f"), pr(7, "g"), pr(8, "h", auto=True),
                   pr(9, "i", draft=True)],
            checks={"a": [run(11, "test", conclusion="failure")],
                    "b": [run(21, "build", conclusion="failure"), run(22, "gates", conclusion="failure")],
                    "c": [run(31, "test", status="queued", conclusion=None, started="2026-11-15T19:00:00Z")],
                    "f": [run(61, "test")], "g": [run(71, "test")], "h": [run(81, "test")]},
            jobs={11: job(11, "hph-ci-1", "failure", step="Run tests"),
                  21: job(21, "runner-2", "failure", step="Checkout"),
                  22: job(22, "hph-ci-1", "failure", step="Every gate green")},
            mergeable={7: "dirty"})
        rep, acts = self.sweep(fake)
        self.assertEqual(self.states(rep), {1: "red", 2: "red-infra", 3: "stalled", 4: "draft", 5: "stacked",
                                            6: "green", 7: "conflict", 8: "pending", 9: "draft-active"})
        self.assertEqual(rep["pull_requests"][0]["failed"], ["test"])
        self.assertEqual([a["do"] for a in acts], ["rerun", "restart-checks", "fix", "rebase", "land", "resume"])

    def test_broken_runner_is_found_and_needs_a_person(self):
        bad = [({"id": 100 + i, "name": "CI"}, [job(200 + i, "mini-2", "failure", step="Checkout"),
                                                 job(300 + i, "hph-ci-%d" % i)]) for i in range(3)]
        fake = FakeGitHub(pulls=[], checks={}, runs=bad)
        rep, acts = self.sweep(fake)
        self.assertEqual([r["runner"] for r in rep["runners"]], ["mini-2"])
        self.assertEqual(acts[0]["do"], "fix-runner")
        self.assertTrue(acts[0]["needs_person"])

    def test_a_runner_that_also_passes_is_not_broken(self):
        runs = [({"id": 1, "name": "CI"}, [job(1, "mini-1", "failure", step="Checkout"), job(2, "mini-1"),
                                           job(3, "mini-1", "failure", step="Checkout"),
                                           job(4, "mini-1", "failure", step="Checkout")])]
        rep, _ = self.sweep(FakeGitHub(pulls=[], checks={}, runs=runs))
        self.assertEqual(rep["runners"], [])

    def test_red_main_since_the_first_failure_after_the_last_green(self):
        main_runs = [{"name": "CI", "conclusion": "failure", "created_at": "2026-11-15T18:00:00Z"},
                     {"name": "Auto-merge", "conclusion": "skipped", "created_at": "2026-11-15T17:00:00Z"},
                     {"name": "CI", "conclusion": "cancelled", "created_at": "2026-11-15T16:00:00Z"},
                     {"name": "CI", "conclusion": "failure", "created_at": "2026-11-15T15:00:00Z"},
                     {"name": "CI", "conclusion": "success", "created_at": "2026-11-15T12:00:00Z"},
                     {"name": "CI", "conclusion": "failure", "created_at": "2026-11-15T10:00:00Z"}]
        rep, acts = self.sweep(FakeGitHub(pulls=[], checks={}, main_runs=main_runs))
        self.assertEqual(rep["main"]["state"], "red")
        self.assertEqual(rep["main"]["red_since"], "2026-11-15T15:00:00Z")
        self.assertEqual(rep["main"]["red_hours"], 6.0)
        self.assertEqual(acts[0]["do"], "fix-main")
        self.assertTrue(acts[0]["needs_person"])  # over main_red_hours

    def test_leftover_wip_branches_without_a_pr(self):
        fake = FakeGitHub(pulls=[pr(1, "a", head="fast-builder/wip/core-1")], checks={"a": [run(1, "t")]},
                          wip=["fast-builder/wip/core-1", "fast-builder/wip/core-2"])
        rep, acts = self.sweep(fake)
        self.assertEqual(rep["leftover_branches"], ["fast-builder/wip/core-2"])
        self.assertEqual(acts[-1], {"do": "resume-branch", "repo": REPO, "branch": "fast-builder/wip/core-2"})

    def test_lane_comes_from_the_label(self):
        fake = FakeGitHub(pulls=[pr(1, "a", labels=["fast-builder", "lane:core"])],
                          checks={"a": [run(11, "t", conclusion="failure")]},
                          jobs={11: job(11, "x", "failure", step="Run tests")})
        rep, acts = self.sweep(fake)
        self.assertEqual(acts[0]["lane"], "core")

    def test_past_the_cap_prs_are_not_classified(self):
        fake = FakeGitHub(pulls=[pr(1, "a"), pr(2, "b", created="2026-11-11T00:00:00Z")],
                          checks={"a": [run(11, "t", conclusion="failure")], "b": [run(21, "t", conclusion="failure")]},
                          jobs={11: job(11, "x", "failure", step="Checkout"), 21: job(21, "x", "failure", step="Checkout")})
        rep, _ = self.sweep(fake, max_prs_classified=1)
        self.assertEqual(self.states(rep), {1: "red-infra", 2: "red"})
        self.assertFalse(any("/actions/jobs/21" in c for c in fake.calls))

    def test_pages_are_fetched_by_hand(self):
        calls = []

        def fake(path):
            calls.append(path)
            return [{"n": i} for i in range(100)] if path.endswith("&page=1") else [{"n": 100}]
        with mock.patch.object(steward, "api", fake):
            self.assertEqual(len(steward.paged("repos/x/pulls?state=open")), 101)
        self.assertEqual(calls, ["repos/x/pulls?state=open&per_page=100&page=1",
                                 "repos/x/pulls?state=open&per_page=100&page=2"])


class Cli(unittest.TestCase):
    def test_rules_and_session_note(self):
        out = subprocess.run([sys.executable, os.path.join(HERE, "steward.py"), "rules"], capture_output=True,
                             text=True).stdout
        self.assertEqual(json.loads(out)["questions"]["digest"]["tz"], "America/Chicago")
        note = subprocess.run([sys.executable, os.path.join(HERE, "steward.py"), "session-note"],
                              capture_output=True, text=True).stdout
        self.assertIn("4:52 pm central", note)

    def test_session_note_never_fails(self):
        res = subprocess.run([sys.executable, os.path.join(HERE, "steward.py"), "--rules", "/nonexistent",
                              "session-note"], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0)

    def test_sweep_reports_a_repo_it_cannot_read(self):
        def boom(path):
            raise steward.GhError("HTTP 404")
        with mock.patch.object(steward, "api", boom), tempfile.TemporaryDirectory() as tmp, \
                mock.patch("sys.stdout", new=io.StringIO()) as out:
            steward.main(["sweep", "--repo", REPO, "--out", os.path.join(tmp, "s.json")])
            with open(os.path.join(tmp, "s.json")) as fh:
                data = json.load(fh)
        self.assertEqual(data["errors"], [{"repo": REPO, "error": "HTTP 404"}])
        self.assertIn("skipped", out.getvalue())


class FinishGuard(unittest.TestCase):
    def guard(self, checks, pulls=None, env=None, session="s1"):
        fake = FakeGitHub(pulls=[], checks={"abc": checks})
        fake_pulls = [pr(7, "abc")] if pulls is None else pulls

        def api(path):
            return fake_pulls if path.startswith("repos/%s/pulls?" % REPO) else fake(path)
        with tempfile.TemporaryDirectory() as home, mock.patch.object(steward, "api", api), \
                mock.patch.object(finish_guard, "STATE_DIR", home), \
                mock.patch.object(finish_guard, "git", lambda cwd, *a: "feature/7" if a[0] == "rev-parse"
                                  else "git@github.com:acme/app.git"), \
                mock.patch.dict(os.environ, env or {}, clear=False):
            rules = steward.load_rules()
            return [finish_guard.verdict({"cwd": "/x", "session_id": session}, rules)
                    for _ in range(rules["lanes"]["max_fix_rounds_per_pr"] + 1)]

    def test_red_pr_blocks_the_stop_then_lets_go_after_the_rounds(self):
        out = self.guard([run(1, "test", conclusion="failure")])
        self.assertIn("failed checks: test", out[0])
        self.assertIn("Round 1 of 5", out[0])
        self.assertTrue(all(out[:-1]))
        self.assertIsNone(out[-1])

    def test_running_checks_block(self):
        self.assertIn("still running", self.guard([run(1, "test", status="in_progress", conclusion=None,
                                                       started="2099-01-01T00:00:00Z")])[0])

    def test_green_or_no_pr_or_switched_off_allows(self):
        self.assertIsNone(self.guard([run(1, "test")])[0])
        self.assertIsNone(self.guard([run(1, "t", conclusion="failure")], pulls=[])[0])
        self.assertIsNone(self.guard([run(1, "t", conclusion="failure")],
                                     env={"BUILD_AUTOPILOT_FINISH_GUARD": "0"})[0])

    def test_hook_allows_on_any_error(self):
        res = subprocess.run([sys.executable, os.path.join(HERE, "finish_guard.py")], input="not json",
                             capture_output=True, text=True)
        self.assertEqual((res.returncode, res.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
