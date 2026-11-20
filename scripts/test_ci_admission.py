import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_admission  # noqa: E402

NOW = dt.datetime(2026, 11, 20, 16, 0, tzinfo=dt.timezone.utc)
OLD = "2026-11-20T10:00:00Z"
FRESH = "2026-11-20T15:55:00Z"


def pr(n, sha=None, auto=False, created="2026-11-10T00:00:00Z", updated=OLD, draft=False):
    return {"number": n, "head": {"sha": sha or "s%d" % n}, "auto_merge": {"x": 1} if auto else None,
            "created_at": created, "updated_at": updated, "draft": draft}


def run(rid, n, sha=None, status="queued", event="pull_request", name="test"):
    return {"id": rid, "head_sha": sha or "s%d" % n, "status": status, "event": event, "name": name,
            "pull_requests": [{"number": n}]}


def plan(prs, runs, state=None, max_prs=2, running=()):
    return ci_admission.plan(prs, runs, state or {}, max_prs, 15, lambda rid: rid in running, NOW)


class Admission(unittest.TestCase):
    def test_pauses_all_but_the_first_few_auto_merge_first_then_oldest(self):
        prs = [pr(1), pr(2, auto=True, created="2026-11-15T00:00:00Z"), pr(3, created="2026-11-01T00:00:00Z")]
        acts, st = plan(prs, [run(10, 1), run(20, 2), run(30, 3)])
        self.assertEqual(set(st["admitted"]), {"2", "3"})
        self.assertEqual([a[:3] for a in acts], [("pause", 10, 1)])
        self.assertEqual(st["paused"], {"10": {"pr": 1, "sha": "s1"}})

    def test_a_pr_still_pushing_waits_behind_settled_ones(self):
        prs = [pr(1, created="2026-11-01T00:00:00Z", updated=FRESH), pr(2), pr(3)]
        _, st = plan(prs, [run(10, 1), run(20, 2), run(30, 3)])
        self.assertEqual(set(st["admitted"]), {"2", "3"})

    def test_admitted_is_sticky_and_a_finished_pr_frees_its_place_for_a_paused_one(self):
        prs = [pr(1), pr(2), pr(3)]
        state = {"admitted": {"1": "s1", "2": "s2"}, "paused": {"30": {"pr": 3, "sha": "s3"}}}
        acts, st = plan(prs, [run(10, 1, status="in_progress")], state)  # 2 finished
        self.assertEqual(set(st["admitted"]), {"1", "3"})
        self.assertEqual([a[:3] for a in acts], [("resume", 30, 3)])
        self.assertEqual(st["paused"], {})

    def test_a_run_with_a_job_on_a_runner_is_never_paused(self):
        prs = [pr(1), pr(2), pr(3)]
        acts, st = plan(prs, [run(10, 1), run(20, 2), run(30, 3, status="in_progress")], max_prs=2,
                        running={30})
        self.assertIn("3", st["admitted"])
        self.assertNotIn(30, [a[1] for a in acts])

    def test_a_paused_run_on_an_old_head_is_forgotten(self):
        prs = [pr(1, sha="new")]
        acts, st = plan(prs, [], {"paused": {"10": {"pr": 1, "sha": "old"}}})
        self.assertEqual(acts, [])
        self.assertEqual(st["paused"], {})

    def test_only_pull_request_runs_on_an_open_head_are_touched(self):
        prs = [pr(1), pr(2)]
        runs = [run(10, 1), run(11, 1, event="push"), run(12, 1, event="pull_request_target"),
                run(13, 9), run(14, 2, sha="stale")]
        acts, _ = plan(prs, runs, max_prs=0)
        self.assertEqual([a[1] for a in acts], [10])

    def test_a_paused_run_already_running_again_is_not_resumed(self):
        prs = [pr(1)]
        acts, st = plan(prs, [run(10, 1, status="in_progress")], {"paused": {"10": {"pr": 1, "sha": "s1"}}})
        self.assertEqual(acts, [])
        self.assertEqual(st["paused"], {})
        self.assertIn("1", st["admitted"])

    def test_settled_is_judged_by_the_newest_ci_run_not_comments(self):
        prs = [pr(1, created="2026-11-01T00:00:00Z", updated=FRESH), pr(2), pr(3)]
        runs = [dict(run(10, 1), created_at=OLD), dict(run(20, 2), created_at=OLD), dict(run(30, 3), created_at=FRESH)]
        _, st = plan(prs, runs)
        self.assertEqual(set(st["admitted"]), {"1", "2"})


class RunRepo(unittest.TestCase):
    """run_repo with GitHub replaced: what it calls and what state it keeps."""

    def go(self, prs, runs, state, answers, max_prs=1, max_actions=400):
        from unittest import mock
        calls = []

        def gh(args):
            calls.append(args[-1])
            ans = answers.get(args[-1])
            if isinstance(ans, Exception):
                raise ans
            return ""

        def paged(path, key=None, cap=1000, per_page=100):
            if "/pulls" in path:
                return prs
            if "/jobs" in path:
                return []
            return runs if "status=queued" in path else []

        with mock.patch.object(ci_admission.steward, "gh", gh), \
                mock.patch.object(ci_admission.steward, "paged", paged), \
                mock.patch.object(ci_admission, "_now", lambda: NOW):
            new, failed = ci_admission.run_repo("o/r", state, max_prs, 15, False, max_actions)
        return calls, new, failed

    E = ci_admission.steward.GhError

    def test_pause_ok_ghost_and_failure(self):
        prs = [pr(1), pr(2), pr(3), pr(4)]
        runs = [run(10, 1), run(20, 2), run(30, 3), run(40, 4)]
        answers = {"repos/o/r/actions/runs/30/cancel": self.E("Cannot cancel ... has not been queued yet. (HTTP 409)"),
                   "repos/o/r/actions/runs/40/cancel": self.E("HTTP 500")}
        calls, new, failed = self.go(prs, runs, {}, answers)
        self.assertEqual(calls, ["repos/o/r/actions/runs/%d/cancel" % i for i in (20, 30, 40)])
        self.assertEqual(set(new["paused"]), {"20"})
        self.assertEqual(failed, 1)

    def test_resume_falls_back_to_a_full_rerun(self):
        state = {"paused": {"10": {"pr": 1, "sha": "s1"}}}
        answers = {"repos/o/r/actions/runs/10/rerun-failed-jobs": self.E("no failed jobs")}
        calls, new, failed = self.go([pr(1)], [], state, answers)
        self.assertEqual(calls, ["repos/o/r/actions/runs/10/rerun-failed-jobs", "repos/o/r/actions/runs/10/rerun"])
        self.assertEqual((new["paused"], failed), ({}, 0))

    def test_a_resume_that_keeps_failing_frees_the_place(self):
        state = {"paused": {"10": {"pr": 1, "sha": "s1"}}}
        bad = {"repos/o/r/actions/runs/10/rerun-failed-jobs": self.E("too old"),
               "repos/o/r/actions/runs/10/rerun": self.E("too old")}
        for tries in range(1, ci_admission.RESUME_TRIES):
            _, state, _ = self.go([pr(1)], [], state, bad)
            self.assertEqual(state["paused"]["10"]["tries"], tries)
        _, state, _ = self.go([pr(1)], [], state, bad)
        self.assertEqual(state["paused"], {})
        _, state, _ = self.go([pr(1)], [], state, bad)
        self.assertEqual(state["admitted"], {})

    def test_actions_over_the_cap_wait_for_the_next_pass(self):
        prs = [pr(1), pr(2), pr(3)]
        calls, new, _ = self.go(prs, [run(10, 1), run(20, 2), run(30, 3)], {}, {}, max_actions=1)
        self.assertEqual(calls, ["repos/o/r/actions/runs/20/cancel"])
        self.assertEqual(set(new["paused"]), {"20"})


if __name__ == "__main__":
    unittest.main()
