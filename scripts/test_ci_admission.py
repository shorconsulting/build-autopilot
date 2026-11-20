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


if __name__ == "__main__":
    unittest.main()
