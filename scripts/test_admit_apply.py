import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import admit_apply as aa  # noqa: E402


def run_(i, q, started=False, wid=1, event="pull_request", pr=None, head=None):
    upd = "2026-11-21T%s" % ("23:00:00Z" if started else q[11:])
    sha = head or "s%d" % i
    return {"id": i, "workflow_id": wid, "name": "test", "event": event, "status": "queued", "head_sha": sha,
            "created_at": q, "run_started_at": q, "updated_at": q if not started else upd,
            "pull_requests": [{"number": pr or 100 + i, "head": {"sha": sha}}]}


def jobs(*states):
    return [{"status": s, "conclusion": c} for s, c in states]


NEW = jobs(("queued", None), ("queued", None), ("queued", None))
STARTED = jobs(("completed", "success"), ("queued", None), ("queued", None))
RUNNING = jobs(("completed", "success"), ("in_progress", None), ("queued", None))
FAILED = jobs(("completed", "failure"), ("queued", None), ("queued", None))


class Api:
    def __init__(self, runs, jobs_by_id, prio=None, role="admin"):
        self.runs = {r["id"]: dict(r) for r in runs}
        self.jobs = jobs_by_id
        self.prio = prio or []
        self.role = role
        self.calls = []

    def __call__(self, method, path):
        self.calls.append((method, path))
        if path == "rate_limit":
            return True, {"resources": {"core": {"remaining": 5000}}}, ""
        if "/issues?" in path:
            return True, [{"number": n, "pull_request": {}} for n in self.prio], ""
        if "/events?" in path:
            return True, [{"event": "labeled", "label": {"name": "ci:priority"}, "actor": {"login": "d"}}], ""
        if "/collaborators/" in path:
            return True, {"role_name": self.role}, ""
        if "runs?status=in_progress" in path:
            return True, {"workflow_runs": [r for r in self.runs.values() if r["status"] == "in_progress"]}, ""
        if "runs?status=queued" in path:
            return True, {"workflow_runs": [r for r in self.runs.values() if r["status"] == "queued"]}, ""
        rid = int(path.split("/actions/runs/")[1].split("/")[0].split("?")[0])
        if path.endswith("/jobs") or "/jobs?" in path:
            return True, {"jobs": self.jobs.get(rid, NEW)}, ""
        if method == "POST" and path.endswith("/cancel"):
            self.runs[rid]["status"] = "completed"
            return True, None, ""
        if method == "POST" and ("/rerun" in path):
            self.runs[rid].update(status="queued", run_started_at="2026-11-21T23:30:00Z", updated_at="2026-11-21T23:30:00Z")
            return True, None, ""
        return True, self.runs[rid], ""

    def posts(self):
        return [p for m, p in self.calls if m == "POST"]


class Decide(unittest.TestCase):
    def item(self, i, kind, q, prs=None, sup=False, parked=False, overdue=False):
        return {"id": i, "kind": kind, "q": q, "prs": prs or [i], "superseded": sup, "parked": parked,
                "overdue": overdue}

    def test_a_parked_started_run_takes_a_place_ahead_of_a_new_queued_run(self):
        keep, park, _, _ = aa.decide([self.item(1, "new", 0), self.item(2, "started", 5, parked=True)], 1)
        self.assertEqual(([r["id"] for r in keep], [r["id"] for r in park]), ([2], [1]))

    def test_overdue_runs_trickle_back_two_at_a_time(self):
        backlog = [self.item(i, "new", i, parked=True, overdue=True) for i in range(10)]
        fresh = [self.item(100 + i, "started", 50 + i) for i in range(5)]
        keep, _, _, _ = aa.decide(backlog + fresh, 4)
        self.assertEqual([r["id"] for r in keep], [0, 1, 100, 101])

    def test_running_then_started_then_new_oldest_first(self):
        runs = [self.item(1, "new", 1), self.item(2, "started", 5), self.item(3, "started", 2),
                self.item(4, "running", 9), self.item(5, "new", 0)]
        keep, park, _, _ = aa.decide(runs, 3)
        self.assertEqual([r["id"] for r in keep], [4, 3, 2])
        self.assertEqual([r["id"] for r in park], [5, 1])

    def test_a_running_run_is_never_parked_even_past_the_limit(self):
        keep, park, _, _ = aa.decide([self.item(i, "running", i) for i in range(5)], 2)
        self.assertEqual((len(keep), park), (5, []))

    def test_a_priority_pr_goes_ahead_of_older_started_runs(self):
        keep, _, _, _ = aa.decide([self.item(1, "started", 1), self.item(2, "new", 9, prs=[42])], 1, {42})
        self.assertEqual([r["id"] for r in keep], [2])

    def test_failed_and_superseded_are_split_out(self):
        _, _, failed, stale = aa.decide([self.item(1, "failed", 1), self.item(2, "started", 1, sup=True)], 5)
        self.assertEqual(([r["id"] for r in failed], [r["id"] for r in stale]), ([1], [2]))


class Run(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        Path(self.state).write_text("{}")

    def go(self, api, **kw):
        return aa.run(["o/a"], self.state, api=api, now=kw.pop("now", lambda: 1000.0), **kw)

    def test_parks_past_the_limit_then_admits_into_free_places_with_the_right_rerun(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True),
                run_(3, "2026-11-21T03:00:00Z")]
        api = Api(runs, {1: STARTED, 2: STARTED, 3: NEW})
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/2/cancel", "repos/o/a/actions/runs/3/cancel"])
        st = json.loads(Path(self.state).read_text())
        self.assertEqual(sorted(st), ["o/a 2", "o/a 3"])
        # Run 1 finishes: one place frees, the oldest parked run (2, started) resumes with only its cancelled jobs.
        api.runs[1]["status"] = "completed"
        api.calls.clear()
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/2/rerun-failed-jobs"])
        api.runs[2]["status"] = "completed"
        api.calls.clear()
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/3/rerun"])
        self.assertEqual(json.loads(Path(self.state).read_text()), {})

    def test_a_second_pass_with_nothing_new_changes_nothing(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z")]
        api = Api(runs, {1: STARTED})
        self.go(api, max_active=1)
        n = len(api.posts())
        self.go(api, max_active=1)
        self.assertEqual(len(api.posts()), n)

    def test_fail_fast_cancels_a_run_with_a_failed_job_and_never_reruns_it(self):
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True)], {1: FAILED})
        self.go(api, max_active=5)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/cancel"])
        self.assertEqual(json.loads(Path(self.state).read_text()), {})

    def test_a_light_workflow_is_never_shaped(self):
        merge_bot = run_(1, "2026-11-21T01:00:00Z", wid=9, event="pull_request_target")
        api = Api([merge_bot, run_(2, "2026-11-21T02:00:00Z", wid=9)], {1: jobs(("queued", None)), 2: jobs(("queued", None))})
        self.go(api, max_active=0)
        self.assertEqual(api.posts(), [])

    def test_a_light_size_from_a_new_run_does_not_hide_a_big_workflow(self):
        # The new run lists only its first job; the started run of the same workflow shows its real size.
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z")]
        api = Api(runs, {1: STARTED, 2: jobs(("queued", None))})
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/2/cancel"])

    def test_runs_github_reports_in_progress_hold_places(self):
        runs = [dict(run_(1, "2026-11-21T00:00:00Z", started=True), status="in_progress"),
                run_(2, "2026-11-21T01:00:00Z", started=True), run_(3, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {2: STARTED, 3: STARTED})
        self.go(api, max_active=2)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/3/cancel"])

    def test_an_in_progress_run_of_a_workflow_with_nothing_queued_still_holds_a_place(self):
        other = dict(run_(1, "2026-11-21T00:00:00Z", wid=5), status="in_progress")
        runs = [other, run_(2, "2026-11-21T01:00:00Z", started=True), run_(3, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: RUNNING, 2: STARTED, 3: STARTED})
        self.go(api, max_active=2)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/3/cancel"])

    def test_push_and_schedule_runs_are_never_touched(self):
        r = run_(1, "2026-11-21T01:00:00Z")
        r.update(event="push", pull_requests=[])
        api = Api([r], {})
        self.go(api, max_active=0)
        self.assertEqual(api.posts(), [])

    def test_a_run_parked_too_long_goes_ahead_of_started_runs(self):
        Path(self.state).write_text('{"o/a 7": {"t": 0, "q": 99999999999, "kind": "new", "prs": [107], "wid": 1}}')
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True), dict(run_(7, "2026-11-21T00:00:00Z"), status="completed")],
                  {1: STARTED})
        self.go(api, max_active=1, now=lambda: aa.PARK_FOR_S + 10.0)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/cancel", "repos/o/a/actions/runs/7/rerun"])

    def test_steady_new_arrivals_never_starve_a_parked_started_run(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: STARTED, 2: STARTED})
        self.go(api, max_active=1)  # 2 is parked
        api.runs[1]["status"] = "completed"
        api.runs[50] = run_(50, "2026-11-21T05:00:00Z")  # a fresh push arrives
        api.calls.clear()
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/50/cancel", "repos/o/a/actions/runs/2/rerun-failed-jobs"])

    def test_a_resumed_started_run_parked_again_comes_back_with_rerun_failed_jobs(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: STARTED, 2: STARTED})
        self.go(api, max_active=1)  # 2 parked
        api.runs[1]["status"] = "completed"
        self.go(api, max_active=1)  # 2 resumed: a re-run attempt, so its queue time looks new
        api.runs[2]["run_attempt"] = 2
        api.runs[3] = dict(run_(3, "2026-11-21T00:30:00Z", started=True), run_attempt=1)
        api.jobs[3] = RUNNING  # something running holds the only place, so 2 is parked again
        api.calls.clear()
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/2/cancel"])
        self.assertEqual(json.loads(Path(self.state).read_text())["o/a 2"]["kind"], "started")

    def test_a_held_run_is_never_touched(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: STARTED, 2: STARTED})
        aa.run(["o/a"], self.state, max_active=1, api=api, now=lambda: 1000.0, hold={"o/a 2"})
        self.assertEqual(api.posts(), [])

    def test_a_superseded_parked_run_is_forgotten_not_rerun(self):
        Path(self.state).write_text('{"o/a 7": {"t": 900, "q": 1, "kind": "started", "prs": [107]}}')
        r7 = dict(run_(7, "2026-11-21T00:00:00Z"), status="completed")
        r7["pull_requests"] = [{"number": 107, "head": {"sha": "newer"}}]
        api = Api([r7], {})
        self.go(api, max_active=3)
        self.assertEqual(api.posts(), [])
        self.assertEqual(json.loads(Path(self.state).read_text()), {})

    def test_dry_run_posts_nothing_and_saves_nothing(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z")]
        api = Api(runs, {1: STARTED})
        aa.run(["o/a"], self.state, max_active=1, dry=True, api=api)
        self.assertEqual(api.posts(), [])
        self.assertEqual(Path(self.state).read_text(), "{}")


if __name__ == "__main__":
    unittest.main()
