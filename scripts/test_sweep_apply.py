import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import steward  # noqa: E402
import sweep_apply  # noqa: E402
from test_steward import NOW, REPO, FakeGitHub, job, pr, run  # noqa: E402


def report(*prs):
    return {"repos": [{"repo": "o/r", "pull_requests": list(prs)}]}


def p(n, state, labels=(), infra=(), stalled=()):
    return {"number": n, "state": state, "labels": list(labels), "infra_runs": list(infra), "stalled": list(stalled)}


class Plan(unittest.TestCase):
    def test_reruns_infra_failures_once_per_run(self):
        acts = sweep_apply.plan(report(p(1, "red-infra", infra=[{"name": "a", "run_id": 7, "attempt": 1},
                                                                {"name": "b", "run_id": 7, "attempt": 1}])))
        self.assertEqual([(k, a[-1]) for k, a, _ in acts], [("rerun", "repos/o/r/actions/runs/7/rerun-failed-jobs")])

    def test_stops_at_the_attempt_cap(self):
        acts = sweep_apply.plan(report(p(1, "red-infra", infra=[{"name": "a", "run_id": 7, "attempt": 4}])),
                                max_attempts=3)
        self.assertEqual(acts, [])

    def test_cancels_only_hung_running_checks(self):
        acts = sweep_apply.plan(report(p(1, "stalled", stalled=[
            {"name": "a", "status": "in_progress", "run_id": 8, "minutes": 120},
            {"name": "b", "status": "queued", "run_id": 9, "minutes": 300}])))
        self.assertEqual([(k, a[-1]) for k, a, _ in acts], [("cancel", "repos/o/r/actions/runs/8/cancel")])

    def test_leaves_parked_red_code_and_conflicts_alone(self):
        acts = sweep_apply.plan(report(
            p(1, "red-infra", labels=["parked"], infra=[{"name": "a", "run_id": 7, "attempt": 1}]),
            p(2, "red"), p(3, "conflict"), p(4, "green")))
        self.assertEqual(acts, [])

    def test_caps_actions_per_pass(self):
        prs = [p(i, "red-infra", infra=[{"name": "a", "run_id": 100 + i, "attempt": 1}]) for i in range(30)]
        self.assertEqual(len(sweep_apply.plan(report(*prs), max_actions=5)), 5)

    def test_dry_run_changes_nothing(self):
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(report(p(1, "red-infra", infra=[{"name": "a", "run_id": 7, "attempt": 1}])), fh)
        with mock.patch.object(sweep_apply.subprocess, "run") as sp:
            self.assertEqual(sweep_apply.main([fh.name, "--dry-run"]), 0)
            sp.assert_not_called()
        os.unlink(fh.name)


class SweepCarriesRunIds(unittest.TestCase):
    def sweep(self, fake):
        with mock.patch.object(steward, "api", fake):
            return steward.Sweeper(steward.load_rules(), now=NOW).sweep_repo(REPO)

    def test_infra_and_hung_checks_carry_their_run(self):
        j21 = dict(job(21, "r", "failure", step="Checkout"), run_id=500, run_attempt=2)
        j31 = dict(job(31, "r"), run_id=600, run_attempt=1)
        fake = FakeGitHub(pulls=[pr(1, "a"), pr(2, "b")],
                          checks={"a": [run(21, "build", conclusion="failure")],
                                  "b": [run(31, "test", status="in_progress", conclusion=None,
                                            started="2026-11-15T18:00:00Z")]},
                          jobs={21: j21, 31: j31})
        rep = self.sweep(fake)
        a, b = rep["pull_requests"]
        self.assertEqual(a["infra_runs"], [{"name": "build", "run_id": 500, "attempt": 2}])
        self.assertEqual((b["state"], b["stalled"][0]["run_id"]), ("stalled", 600))
        acts = sweep_apply.plan({"repos": [rep]})
        self.assertEqual([a[-1] for _, a, _ in acts], ["repos/%s/actions/runs/500/rerun-failed-jobs" % REPO,
                                                       "repos/%s/actions/runs/600/cancel" % REPO])

    def test_a_cancel_superseded_by_a_live_run_is_not_a_failure(self):
        fake = FakeGitHub(pulls=[pr(1, "a")],
                          checks={"a": [run(21, "build", conclusion="cancelled"), run(22, "build", started="2026-11-15T20:30:00Z")]})
        self.assertEqual(self.sweep(fake)["pull_requests"][0]["state"], "green")

    def test_a_cancel_beside_an_earlier_finished_check_of_that_name_is_still_infra(self):
        fake = FakeGitHub(pulls=[pr(1, "a")],
                          checks={"a": [run(21, "build", conclusion="cancelled", started="2026-11-15T20:30:00Z"),
                                        run(22, "build", started="2026-11-15T20:00:00Z")]})
        self.assertEqual(self.sweep(fake)["pull_requests"][0]["state"], "red-infra")


class Lite(unittest.TestCase):
    def test_lite_reads_only_recent_prs_and_skips_the_scans(self):
        pulls = [pr(i, "s%d" % i, updated="2026-11-15T%02d:00:00Z" % i) for i in range(1, 6)]
        fake = FakeGitHub(pulls=pulls, checks={})
        rules = steward.load_rules()
        rules["steward"]["max_prs_classified"] = 2
        with mock.patch.object(steward, "api", fake):
            rep = steward.Sweeper(rules, now=NOW, lite=True).sweep_repo(REPO)
        self.assertEqual([p["number"] for p in rep["pull_requests"]], [5, 4])
        expensive = [c for c in fake.calls if "event=push" not in c and (
            "/pulls/" in c or "/jobs" in c or "state=closed" in c or "status=completed" in c)]
        self.assertEqual(expensive, [])

    def test_sweep_steps_aside_when_the_hour_is_nearly_spent(self):
        import argparse
        import json
        import tempfile
        out = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        out.write(json.dumps(report(p(1, "red-infra", infra=[{"name": "a", "run_id": 7, "attempt": 1}]))))
        out.close()
        args = argparse.Namespace(rules=steward.RULES_PATH, repo=[REPO], out=out.name, no_history=True, history_dir="/nonexistent",
                                  lite=True, max_prs=10, min_rate=2500)
        calls = []

        def fake(path):
            calls.append(path)
            return {"resources": {"core": {"remaining": 900}}}
        with mock.patch.object(steward, "api", fake):
            self.assertEqual(steward.cmd_sweep(args), 0)
        self.assertEqual(calls, ["rate_limit"])
        with open(out.name) as fh:  # the previous pass's report is gone: nothing to apply again
            self.assertEqual(sweep_apply.plan(json.load(fh)), [])
        os.unlink(out.name)

    def test_lite_sweeps_leave_the_health_history_alone(self):
        import argparse
        import tempfile
        hist = tempfile.mkdtemp()
        args = argparse.Namespace(rules=steward.RULES_PATH, repo=[REPO], out=None, no_history=False,
                                  history_dir=hist, lite=True, max_prs=5, min_rate=0)
        with mock.patch.object(steward, "api", FakeGitHub(pulls=[], checks={})), \
                mock.patch("sys.stdout"):
            steward.cmd_sweep(args)
        self.assertEqual(os.listdir(hist), [])


if __name__ == "__main__":
    unittest.main()
