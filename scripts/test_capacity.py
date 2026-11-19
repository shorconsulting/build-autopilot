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


def run(i, name, branch, sha, event="pull_request", repo="o/r", created="2026-11-19T10:00:00Z", wid=None, prs=()):
    return {"id": i, "name": name, "head_branch": branch, "head_sha": sha, "event": event, "created_at": created,
            "workflow_id": wid or name, "pull_requests": [{"number": n} for n in prs],
            "head_repository": {"full_name": repo}}


CLOSED = {("o/r", "gone"): "2026-11-19T12:00:00Z", ("o/r", "gone2"): "2026-11-19T12:00:00Z"}


def runner(name, labels, status="online", busy=False, rid=1):
    return {"id": rid, "name": name, "status": status, "busy": busy, "labels": [{"name": l} for l in labels]}


def pr(ref, sha, repo="o/r"):
    return {"head": {"ref": ref, "sha": sha, "repo": {"full_name": repo}}}


OPEN = [pr("a", "a2"), pr("b", "b1")]
SH = ("hph-ci", "self-hosted")


class StaleRuns(unittest.TestCase):
    def test_closed_superseded_and_duplicates(self):
        queued = [run(1, "CI", "gone", "x"), run(2, "CI", "a", "a1"), run(3, "CI", "a", "a2"),
                  run(4, "Auto-merge", "b", "b1"), run(5, "Auto-merge", "b", "b1"), run(6, "Auto-merge", "b", "b1")]
        got = {i: why for i, why, _ in steward.stale_runs(queued, OPEN, closed_at=CLOSED)}
        self.assertEqual(got, {1: "closed", 2: "superseded", 4: "duplicate", 5: "duplicate"})

    def test_a_run_the_close_itself_started_is_kept(self):
        queued = [run(1, "CI", "gone", "x", created="2026-11-19T12:00:05Z"), run(2, "CI", "b", "b1")]
        self.assertEqual(steward.stale_runs(queued, OPEN, closed_at=CLOSED), [])

    def test_a_closed_run_with_no_known_close_time_is_kept(self):
        queued = [run(1, "CI", "gone", "x"), run(2, "CI", "b", "b1")]
        self.assertEqual(steward.stale_runs(queued, OPEN), [])

    def test_two_workflow_files_with_the_same_name_are_not_duplicates(self):
        queued = [run(1, "CI", "b", "b1", wid=11), run(2, "CI", "b", "b1", wid=22)]
        self.assertEqual(steward.stale_runs(queued, OPEN), [])

    def test_stacked_prs_on_one_head_are_not_duplicates(self):
        queued = [run(1, "CI", "b", "b1", prs=(5,)), run(2, "CI", "b", "b1", prs=(6,))]
        self.assertEqual(steward.stale_runs(queued, OPEN), [])

    def test_newest_duplicate_is_kept_whatever_the_order(self):
        queued = [run(9, "CI", "b", "b1"), run(7, "CI", "b", "b1")]
        self.assertEqual([s[0] for s in steward.stale_runs(queued, OPEN)], [7])

    def test_runs_from_different_events_are_not_duplicates(self):
        queued = [run(1, "Auto-merge", "b", "b1", "workflow_run"), run(2, "Auto-merge", "b", "b1", "pull_request")]
        self.assertEqual(steward.stale_runs(queued, OPEN), [])

    def test_never_cancels_work_that_must_run_after_a_merge(self):
        queued = [run(1, "Mark issues done on merge", "gone", "x"), run(2, "deploy", "gone", "x", "workflow_run"),
                  run(3, "test", "main", "m", "push"), run(4, "Nightly", "main", "m", "schedule")]
        self.assertEqual(steward.stale_runs(queued, OPEN), [])

    def test_a_close_only_workflow_is_never_cancelled_whatever_its_name(self):
        # "Tidy up" runs only when a PR closes, so it never has a run for an open PR: it is not PR CI.
        queued = [run(1, "Tidy up", "gone", "x"), run(2, "Cleanup preview", "gone2", "y"), run(3, "CI", "b", "b1")]
        self.assertEqual(steward.stale_runs(queued, OPEN), [])

    def test_fork_branches_with_the_same_name_do_not_collide(self):
        open_prs = [pr("main", "f1", "fork1/r"), pr("main", "f2", "fork2/r")]
        queued = [run(1, "CI", "main", "f1", repo="fork1/r"), run(2, "CI", "main", "f2", repo="fork2/r")]
        self.assertEqual(steward.stale_runs(queued, open_prs), [])

    def test_keep_list_is_configurable(self):
        queued = [run(1, "CI", "gone", "x"), run(2, "CI", "b", "b1")]
        keep = steward._keep({"capacity": {"keep_after_close": ["^CI$"]}})
        self.assertEqual(steward.stale_runs(queued, OPEN, keep, CLOSED), [])


class Pools(unittest.TestCase):
    def test_idle_capacity_that_no_queued_job_can_reach(self):
        runners = [runner("hph-ci-1", ["self-hosted", "hph-ci"], busy=True),
                   runner("hph-ci-10", ["self-hosted", "hph-ci-light"])]
        pools, f = steward.pool_report(runners, {SH: 150}, 20)
        self.assertEqual(pools[0]["idle"], 0)
        kinds = [x["kind"] for x in f]
        self.assertIn("idle-capacity-unreachable", kinds)
        import datetime as dt
        state, t0 = {}, dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        ready = steward.settle(f, state, t0, 30)  # the finding that crashed settle() before
        acts = steward.capacity_actions([], ready, RULES)
        self.assertEqual([a["do"] for a in acts], ["reroute"])

    def test_offline_runner_is_restarted_and_a_dead_pool_restored(self):
        runners = [runner("dind-1", ["self-hosted", "shorconsulting-ci"], status="offline")]
        _, f = steward.pool_report(runners, {("self-hosted", "shorconsulting-ci"): 5}, 20)
        dos = [a["do"] for a in steward.capacity_actions([], f, RULES)]
        self.assertEqual(dos, ["restore-pool", "restart-runner"])

    def test_labels_match_like_github(self):
        self.assertTrue(steward.fits(["self-hosted", "HPH-CI"], ["self-hosted", "hph-ci", "linux"]))
        self.assertFalse(steward.fits(["self-hosted", "hph-ci"], ["self-hosted", "hph-ci-light"]))

    def test_idle_while_jobs_fit_means_hung_only_after_they_waited(self):
        runners = [runner("r1", ["self-hosted", "x"])]
        _, f = steward.pool_report(runners, {("self-hosted", "x"): 3}, 20, {("self-hosted", "x"): 2})
        self.assertEqual(f, [])  # a runner between two jobs looks idle for a moment
        _, f = steward.pool_report(runners, {("self-hosted", "x"): 3}, 20, {("self-hosted", "x"): 25})
        self.assertEqual(f[0]["kind"], "idle-but-queued")

    def test_github_hosted_jobs_are_not_a_pool(self):
        _, f = steward.pool_report([], {("ubuntu-latest",): 4, ("macos-26",): 1}, 20)
        self.assertEqual(f, [])

    def test_a_runner_is_restarted_only_after_two_passes_and_not_again_within_the_cooldown(self):
        import datetime as dt
        f = [{"kind": "runner-offline", "runner": "dind-1", "labels": []}]
        state, t0 = {}, dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(steward.settle(f, state, t0, 30), [])
        self.assertEqual(len(steward.settle(f, state, t0 + dt.timedelta(minutes=10), 30)), 1)
        self.assertEqual(steward.settle(f, state, t0 + dt.timedelta(minutes=20), 30), [])
        self.assertEqual(len(steward.settle(f, state, t0 + dt.timedelta(minutes=50), 30)), 1)
        steward.settle([], state, t0 + dt.timedelta(minutes=60), 30)  # it came back
        self.assertEqual(steward.settle(f, state, t0 + dt.timedelta(minutes=70), 30), [])

    def test_restarts_stop_and_escalate_when_they_do_not_help(self):
        import datetime as dt
        f = [{"kind": "runner-offline", "runner": "dind-5", "labels": []}]
        state, t = {}, dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        restarts = 0
        for i in range(12):
            ready = steward.settle(f, state, t + dt.timedelta(minutes=40 * i), 30, max_restarts=3)
            acts = steward.capacity_actions([], ready, RULES)
            restarts += sum(1 for a in acts if a["do"] == "restart-runner")
        self.assertEqual(restarts, 3)
        self.assertEqual(acts[0]["do"], "report")
        self.assertTrue(acts[0]["needs_person"])

    def test_cancel_action_is_capped(self):
        stale = [(i, "closed", "CI") for i in range(RULES["capacity"]["max_cancels"] + 10)]
        a = steward.capacity_actions(stale, [], RULES)[0]
        self.assertEqual(len(a["runs"]), RULES["capacity"]["max_cancels"])
        self.assertFalse(a["needs_person"])


if __name__ == "__main__":
    unittest.main()
