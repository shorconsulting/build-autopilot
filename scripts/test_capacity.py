"""Tests for the steward's capacity check (scripts/steward.py capacity): stale queued runs and runner pools.

No network. Run:
    python3 -m unittest scripts/test_capacity.py -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import steward  # noqa: E402

RULES = steward.load_rules()


def run(i, name, branch, sha, event="pull_request"):
    return {"id": i, "name": name, "head_branch": branch, "head_sha": sha, "event": event}


def runner(name, labels, status="online", busy=False, rid=1):
    return {"id": rid, "name": name, "status": status, "busy": busy, "labels": [{"name": l} for l in labels]}


OPEN = [{"head": {"ref": "a", "sha": "a2"}}, {"head": {"ref": "b", "sha": "b1"}}]


class StaleRuns(unittest.TestCase):
    def test_closed_superseded_and_duplicates(self):
        queued = [run(1, "CI", "gone", "x"), run(2, "CI", "a", "a1"), run(3, "CI", "a", "a2"),
                  run(4, "Auto-merge", "b", "b1"), run(5, "Auto-merge", "b", "b1"), run(6, "Auto-merge", "b", "b1")]
        got = {i: why for i, why, _ in steward.stale_runs(queued, OPEN)}
        self.assertEqual(got, {1: "closed", 2: "superseded", 4: "duplicate", 5: "duplicate"})

    def test_newest_duplicate_is_kept_whatever_the_order(self):
        queued = [run(9, "CI", "b", "b1"), run(7, "CI", "b", "b1")]
        self.assertEqual([s[0] for s in steward.stale_runs(queued, OPEN)], [7])

    def test_never_cancels_work_that_must_run_after_a_merge(self):
        queued = [run(1, "Mark issues done on merge", "gone", "x"), run(2, "deploy", "gone", "x", "workflow_run"),
                  run(3, "test", "main", "m", "push"), run(4, "Nightly", "main", "m", "schedule")]
        self.assertEqual(steward.stale_runs(queued, OPEN), [])

    def test_workflow_run_on_a_non_pr_branch_is_left_alone(self):
        self.assertEqual(steward.stale_runs([run(1, "Auto-merge", "main", "m", "workflow_run")], OPEN), [])


class Pools(unittest.TestCase):
    def test_idle_capacity_that_no_queued_job_can_reach(self):
        runners = [runner("hph-ci-1", ["self-hosted", "hph-ci"], busy=True),
                   runner("hph-ci-10", ["self-hosted", "hph-ci-light"])]
        pools, f = steward.pool_report(runners, {("hph-ci", "self-hosted"): 150}, 20)
        self.assertEqual(pools[0]["idle"], 0)
        kinds = [x["kind"] for x in f]
        self.assertIn("idle-capacity-unreachable", kinds)
        acts = steward.capacity_actions([], f, RULES)
        self.assertEqual([a["do"] for a in acts], ["reroute"])

    def test_offline_runner_is_restarted_and_a_dead_pool_restored(self):
        runners = [runner("dind-1", ["self-hosted", "shorconsulting-ci"], status="offline")]
        _, f = steward.pool_report(runners, {("self-hosted", "shorconsulting-ci"): 5}, 20)
        dos = [a["do"] for a in steward.capacity_actions([], f, RULES)]
        self.assertEqual(dos, ["restore-pool", "restart-runner"])

    def test_labels_match_like_github(self):
        self.assertTrue(steward.fits(["self-hosted", "HPH-CI"], ["self-hosted", "hph-ci", "linux"]))
        self.assertFalse(steward.fits(["self-hosted", "hph-ci"], ["self-hosted", "hph-ci-light"]))

    def test_idle_while_jobs_fit_means_hung(self):
        runners = [runner("r1", ["self-hosted", "x"])]
        _, f = steward.pool_report(runners, {("self-hosted", "x"): 3}, 20)
        self.assertEqual(f[0]["kind"], "idle-but-queued")

    def test_cancel_action_is_capped(self):
        stale = [(i, "closed", "CI") for i in range(RULES["capacity"]["max_cancels"] + 10)]
        a = steward.capacity_actions(stale, [], RULES)[0]
        self.assertEqual(len(a["runs"]), RULES["capacity"]["max_cancels"])
        self.assertFalse(a["needs_person"])


if __name__ == "__main__":
    unittest.main()
