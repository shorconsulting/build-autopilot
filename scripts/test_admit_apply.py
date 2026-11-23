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
TIMED_OUT = jobs(("completed", "success"), ("completed", "cancelled"), ("queued", None))  # queued 24 h: cancelled
DEAD_RUNNING = jobs(("completed", "cancelled"), ("in_progress", None), ("queued", None))
FAILED_RUNNING = jobs(("completed", "failure"), ("in_progress", None), ("queued", None))


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
        if method == "POST" and (path.endswith("/cancel") or path.endswith("/force-cancel")):
            self.runs[rid].update(status="completed", conclusion="cancelled")
            return True, None, ""
        if method == "POST" and ("/rerun" in path):
            self.runs[rid].update(status="queued", conclusion=None, run_started_at="2026-11-21T23:30:00Z",
                                  updated_at="2026-11-21T23:30:00Z")
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

    def test_a_run_with_a_timed_out_job_is_parked_first_even_with_free_places(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: TIMED_OUT, 2: STARTED})
        self.go(api, max_active=5)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/force-cancel"])
        self.assertEqual(json.loads(Path(self.state).read_text())["o/a 1"]["kind"], "started")
        # It comes back with rerun-failed-jobs, which keeps the job that passed.
        api.calls.clear()
        self.go(api, max_active=5)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/rerun-failed-jobs"])

    def test_a_dead_run_still_running_a_job_is_stopped_too(self):
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True)], {1: DEAD_RUNNING})
        self.go(api, max_active=5)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/force-cancel"])

    def test_fail_fast_frees_the_running_jobs_of_a_failed_run(self):
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True)], {1: FAILED_RUNNING})
        self.go(api, max_active=5)
        self.assertEqual(api.posts(), [])  # off: a continue-on-error job may still let it pass
        self.go(api, max_active=5, fail_fast=True)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/force-cancel"])
        self.assertNotIn("o/a 1", json.loads(Path(self.state).read_text()))  # failed: not parked for re-run

    def test_a_real_failure_with_cancelled_matrix_siblings_is_never_parked_for_rerun(self):
        dead = jobs(("completed", "failure"), ("completed", "cancelled"), ("queued", None))
        hung = jobs(("completed", "timed_out"), ("completed", "success"), ("queued", None))
        for js in (dead, hung):
            self.assertEqual(aa.classify(js), "failed")
            api = Api([run_(1, "2026-11-21T01:00:00Z", started=True)], {1: js})
            Path(self.state).write_text("{}")
            self.go(api, max_active=5)
            self.assertEqual(api.posts(), [])  # fail fast off: left alone
            self.go(api, max_active=5, fail_fast=True)
            self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/force-cancel"])
            self.assertEqual(json.loads(Path(self.state).read_text()), {})  # never queued to re-run

    def test_a_run_that_keeps_stalling_stops_being_re_run(self):
        self.assertEqual(aa.classify(TIMED_OUT, attempt=2), "stalled")
        self.assertEqual(aa.classify(TIMED_OUT, attempt=aa.MAX_STALL_ATTEMPTS), "failed")
        r = dict(run_(1, "2026-11-21T01:00:00Z", started=True), run_attempt=aa.MAX_STALL_ATTEMPTS)
        api = Api([r], {1: TIMED_OUT})
        self.go(api, max_active=5)
        self.assertEqual(api.posts(), [])
        self.assertEqual(json.loads(Path(self.state).read_text()), {})

    def test_a_park_an_always_job_outlived_is_force_cancelled_on_the_next_pass(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: RUNNING, 2: STARTED})
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/2/cancel"])
        # The roll-up job survived the cancel: the run is queued again, its shards cancelled.
        api.runs[2].update(status="queued", conclusion=None)
        api.jobs[2] = TIMED_OUT
        api.calls.clear()
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/2/force-cancel"])
        self.assertIn("o/a 2", json.loads(Path(self.state).read_text()))

    def test_a_parked_run_re_run_by_hand_is_never_force_cancelled(self):
        Path(self.state).write_text('{"o/a 7": {"t": 0, "q": 1, "kind": "started", "prs": [107], "wid": 1}}')
        r = dict(run_(7, "2026-11-21T00:00:00Z", started=True), run_attempt=2)
        api = Api([r], {7: RUNNING})  # a fresh attempt: no cancelled job
        self.go(api, max_active=5, now=lambda: 1000.0)
        self.assertFalse([p for p in api.posts() if "force-cancel" in p])

    def test_out_of_writes_no_force_cancel_is_sent(self):
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True)], {1: TIMED_OUT})
        self.go(api, max_active=5, max_writes=0)
        self.assertEqual(api.posts(), [])

    def test_a_second_pass_with_nothing_new_changes_nothing(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z")]
        api = Api(runs, {1: STARTED})
        self.go(api, max_active=1)
        n = len(api.posts())
        self.go(api, max_active=1)
        self.assertEqual(len(api.posts()), n)

    def test_fail_fast_cancels_a_run_with_a_failed_job_and_never_reruns_it(self):
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True)], {1: FAILED})
        self.go(api, max_active=5, fail_fast=True)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/1/force-cancel"])
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

    def test_without_fail_fast_a_run_with_a_failed_job_is_left_alone(self):
        # A continue-on-error or if: failure() job can fail while the run still passes.
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: FAILED, 2: STARTED})
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), [])

    def test_writes_are_capped_per_pass_and_the_backlog_parks_over_several_passes(self):
        runs = [run_(i, "2026-11-21T%02d:00:00Z" % i, started=True) for i in range(1, 12)]
        api = Api(runs, {i: STARTED for i in range(1, 12)})
        self.go(api, max_active=1, max_writes=4)
        self.assertEqual(len(api.posts()), 4)
        api.calls.clear()
        self.go(api, max_active=1, max_writes=4)
        self.assertEqual(len(api.posts()), 4)
        self.assertEqual(len(json.loads(Path(self.state).read_text())), 8)

    def test_a_rate_limit_refusal_stops_every_write_for_the_pass(self):
        runs = [run_(i, "2026-11-21T%02d:00:00Z" % i, started=True) for i in range(1, 8)]
        api = Api(runs, {i: STARTED for i in range(1, 8)})
        real = api.__call__

        def limited(method, path):
            if method == "POST":
                api.calls.append((method, path))
                return False, None, "gh: You have exceeded a secondary rate limit (HTTP 403)"
            return real(method, path)
        aa.run(["o/a"], self.state, max_active=1, api=limited, now=lambda: 1000.0)
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), 1)

    def test_a_parked_run_whose_pr_closed_is_forgotten(self):
        Path(self.state).write_text('{"o/a 7": {"t": 900, "q": 1, "kind": "started", "prs": [107], "wid": 1}}')
        r7 = dict(run_(7, "2026-11-21T00:00:00Z"), status="completed", conclusion="cancelled", pull_requests=[])
        api = Api([r7], {})
        self.go(api, max_active=3)
        self.assertEqual(api.posts(), [])
        self.assertEqual(json.loads(Path(self.state).read_text()), {})

    def test_a_parked_run_rerun_by_hand_is_dropped_and_never_rerun_again(self):
        Path(self.state).write_text('{"o/a 7": {"t": 900, "q": 1, "kind": "new", "prs": [107], "wid": 1}}')
        api = Api([run_(7, "2026-11-21T00:00:00Z", started=True)], {7: RUNNING})  # someone clicked Re-run
        self.go(api, max_active=3)
        self.assertEqual(json.loads(Path(self.state).read_text()), {})
        api.runs[7].update(status="completed", conclusion="success")
        api.calls.clear()
        self.go(api, max_active=3)
        self.assertEqual(api.posts(), [])

    def test_a_parked_run_that_finished_other_than_cancelled_is_forgotten(self):
        Path(self.state).write_text('{"o/a 7": {"t": 900, "q": 1, "kind": "started", "prs": [107], "wid": 1}}')
        api = Api([dict(run_(7, "2026-11-21T00:00:00Z"), status="completed", conclusion="success")], {})
        self.go(api, max_active=3)
        self.assertEqual(api.posts(), [])
        self.assertEqual(json.loads(Path(self.state).read_text()), {})

    def test_a_plain_403_fails_one_run_and_does_not_stop_the_pass(self):
        b = aa.Budget(5)
        b.spend("gh: Resource not accessible by integration (HTTP 403)")
        self.assertTrue(b.ok())
        b.spend("gh: You have exceeded a secondary rate limit (HTTP 403)")
        self.assertFalse(b.ok())

    def test_out_of_time_it_stops_and_keeps_what_it_parked(self):
        runs = [run_(i, "2026-11-21T%02d:00:00Z" % i, started=True) for i in range(1, 6)]
        api = Api(runs, {i: STARTED for i in range(1, 6)})
        t = [0.0]

        def slow(method, path):  # every call takes a second
            t[0] += 1.0
            return api(method, path)
        aa.run(["o/a", "o/b"], self.state, max_active=1, api=slow, now=lambda: 1000.0, max_seconds=12,
               clock=lambda: t[0])
        st = json.loads(Path(self.state).read_text())
        cancels = [p for p in api.posts() if p.endswith("/cancel")]
        self.assertEqual(len(st), len(cancels))  # every cancel it made is recorded
        self.assertFalse(any("repos/o/b/" in p for _, p in api.calls))  # the second repo waits for the next pass

    def test_a_read_cut_short_by_time_keeps_every_parked_run(self):
        Path(self.state).write_text('{"o/a 7": {"t": 900, "q": 1, "kind": "started", "prs": [107], "wid": 1}}')
        runs = [run_(i, "2026-11-21T%02d:00:00Z" % i, started=True) for i in range(1, 6)]
        api = Api(runs, {i: STARTED for i in range(1, 6)})
        t = [0.0]

        def slow(method, path):
            t[0] += 1.0
            return api(method, path)
        aa.run(["o/a"], self.state, max_active=1, api=slow, now=lambda: 1000.0, max_seconds=4, clock=lambda: t[0])
        self.assertIn("o/a 7", json.loads(Path(self.state).read_text()))  # not read this pass, still parked
        self.assertEqual(api.posts(), [])  # out of time: nothing written on a partial read

    def test_an_empty_label_turns_priority_off(self):
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True)], {1: STARTED}, prio=[101])
        aa.run(["o/a"], self.state, max_active=1, label="", api=api, now=lambda: 1000.0)
        self.assertFalse(any("/issues?" in p for _, p in api.calls))

    def test_push_and_schedule_runs_are_never_touched(self):
        r = run_(1, "2026-11-21T01:00:00Z")
        r.update(event="push", pull_requests=[])
        api = Api([r], {})
        self.go(api, max_active=0)
        self.assertEqual(api.posts(), [])

    def test_a_run_parked_too_long_goes_ahead_of_started_runs(self):
        Path(self.state).write_text('{"o/a 7": {"t": 0, "q": 99999999999, "kind": "new", "prs": [107], "wid": 1}}')
        api = Api([run_(1, "2026-11-21T01:00:00Z", started=True), dict(run_(7, "2026-11-21T00:00:00Z"), status="completed", conclusion="cancelled")],
                  {1: STARTED})
        self.go(api, max_active=1, now=lambda: aa.PARK_FOR_S + 10.0)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/7/rerun", "repos/o/a/actions/runs/1/cancel"])

    def test_steady_new_arrivals_never_starve_a_parked_started_run(self):
        runs = [run_(1, "2026-11-21T01:00:00Z", started=True), run_(2, "2026-11-21T02:00:00Z", started=True)]
        api = Api(runs, {1: STARTED, 2: STARTED})
        self.go(api, max_active=1)  # 2 is parked
        api.runs[1]["status"] = "completed"
        api.runs[50] = run_(50, "2026-11-21T05:00:00Z")  # a fresh push arrives
        api.calls.clear()
        self.go(api, max_active=1)
        self.assertEqual(api.posts(), ["repos/o/a/actions/runs/2/rerun-failed-jobs", "repos/o/a/actions/runs/50/cancel"])

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
        r7 = dict(run_(7, "2026-11-21T00:00:00Z"), status="completed", conclusion="cancelled")
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
