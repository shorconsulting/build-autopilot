import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import priority_apply as pa  # noqa: E402


def run_(i, created, sha="s%d", status="queued", prs=None, name="CI", event="pull_request", started=None):
    head = sha % i if "%" in sha else sha
    return {"id": i, "created_at": created, "run_started_at": started or created, "head_sha": head,
            "status": status, "name": name, "event": event, "run_attempt": 1,
            "pull_requests": [{"number": 1000 + i, "head": {"sha": head}}] if prs is None else prs}


def waiting(r):
    return dict(r, waiting_since=pa.queued_at(r))


class Plan(unittest.TestCase):
    def test_bumps_only_queued_runs_older_than_the_priority_run_oldest_first(self):
        prio = [("o/a", waiting(run_(10, "2026-11-21T10:00:00Z", sha="P", status="queued")))]
        queued = [("o/a", run_(3, "2026-11-21T09:00:00Z")), ("o/a", run_(1, "2026-11-20T09:00:00Z")),
                  ("o/b", run_(2, "2026-11-21T09:30:00Z")), ("o/a", run_(4, "2026-11-21T11:00:00Z")),
                  ("o/a", prio[0][1])]
        self.assertEqual([(repo, r["id"], rr) for repo, r, rr in pa.plan(prio, queued, 10)],
                         [("o/a", 1, True), ("o/a", 3, True), ("o/b", 2, True)])

    def test_nothing_without_a_priority_run(self):
        self.assertEqual(pa.plan([], [("o/a", run_(1, "2026-11-20T09:00:00Z"))], 10), [])

    def test_never_bumps_the_priority_heads_own_runs_or_a_started_run(self):
        prio = [("o/a", waiting(run_(10, "2026-11-21T10:00:00Z", sha="P", status="in_progress")))]
        queued = [("o/a", run_(11, "2026-11-21T09:00:00Z", sha="P")),
                  ("o/a", run_(12, "2026-11-21T09:00:00Z", status="in_progress"))]
        self.assertEqual(pa.plan(prio, queued, 10), [])

    def test_a_superseded_run_is_cancelled_not_rerun(self):
        prio = [("o/a", waiting(run_(10, "2026-11-21T10:00:00Z", sha="P")))]
        old = run_(1, "2026-11-21T09:00:00Z", sha="old", prs=[{"number": 5, "head": {"sha": "new"}}])
        cur = run_(2, "2026-11-21T09:00:00Z", sha="cur", prs=[{"number": 6, "head": {"sha": "cur"}}])
        self.assertEqual([(r["id"], rr) for _, r, rr in pa.plan(prio, [("o/a", old), ("o/a", cur)], 10)],
                         [(1, False), (2, True)])

    def test_only_pull_request_runs_are_bumped_never_a_push_schedule_or_deploy(self):
        prio = [("o/a", waiting(run_(10, "2026-11-21T10:00:00Z", sha="P")))]
        queued = [("o/a", run_(1, "2026-11-21T09:00:00Z", event="push", prs=[])),
                  ("o/a", run_(2, "2026-11-21T09:00:00Z", event="schedule", prs=[])),
                  ("o/a", run_(3, "2026-11-21T09:00:00Z", event="workflow_dispatch", prs=[])),
                  ("o/a", run_(4, "2026-11-21T09:00:00Z", event="pull_request", prs=[])),
                  ("o/a", run_(5, "2026-11-21T09:00:00Z", event="pull_request_target"))]
        self.assertEqual([r["id"] for _, r, _ in pa.plan(prio, queued, 10)], [5])

    def test_a_rerun_counts_from_when_it_was_queued_again_not_its_created_at(self):
        prio = [("o/a", waiting(run_(10, "2026-11-21T10:00:00Z", sha="P")))]
        rerun = run_(1, "2026-11-21T09:00:00Z", started="2026-11-21T10:05:00Z")
        self.assertEqual(pa.plan(prio, [("o/a", rerun)], 10), [])

    def test_the_cap_holds(self):
        prio = [("o/a", waiting(run_(99, "2026-11-21T10:00:00Z", sha="P")))]
        queued = [("o/a", run_(i, "2026-11-21T0%d:00:00Z" % i)) for i in range(1, 6)]
        self.assertEqual(len(pa.plan(prio, queued, 2)), 2)


class FakeApi:
    def __init__(self, runs, label_prs, rate=5000, labeller="david", role="admin", clock=None):
        self.runs = {r["id"]: dict(r) for r in runs}
        self.label_prs = label_prs  # {number: head sha}
        self.rate = rate
        self.labeller, self.role = labeller, role
        self.clock = clock or (lambda: "2026-11-21T12:00:00Z")
        self.jobs = {}  # run id: [job]
        self.calls = []

    def __call__(self, method, path):
        self.calls.append((method, path))
        if "/events?" in path:
            return True, [{"event": "labeled", "label": {"name": "ci:priority"}, "actor": {"login": self.labeller}}], ""
        if "/collaborators/" in path:
            return True, {"permission": "write", "role_name": self.role}, ""
        if "/jobs?" in path:
            return True, {"jobs": self.jobs.get(int(path.split("/actions/runs/")[1].split("/")[0]), [])}, ""
        if path.startswith("repos/o/a/issues"):
            return True, [{"number": n, "pull_request": {}} for n in self.label_prs], ""
        if path.startswith("repos/o/a/pulls/"):
            return True, {"head": {"sha": self.label_prs[int(path.rsplit("/", 1)[1])]}}, ""
        if path == "rate_limit":
            return True, {"resources": {"core": {"remaining": self.rate}}}, ""
        if "runs?head_sha=" in path:
            sha = path.split("head_sha=")[1].split("&")[0]
            return True, {"workflow_runs": [r for r in self.runs.values() if r["head_sha"] == sha]}, ""
        if "runs?status=queued" in path:
            return True, {"workflow_runs": [r for r in self.runs.values() if r["status"] == "queued"]}, ""
        rid = int(path.split("/actions/runs/")[1].split("/")[0])
        if method == "POST" and path.endswith("/cancel"):
            self.runs[rid]["status"] = "completed"
            return True, None, ""
        if method == "POST" and path.endswith("/rerun"):
            self.runs[rid].update(status="queued", run_started_at=self.clock(), run_attempt=2)
            return True, None, ""
        return True, self.runs[rid], ""


class Run(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        Path(self.state).write_text("{}")

    def test_cancels_then_reruns_older_runs_so_they_queue_behind_the_priority_pr(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P"), run_(1, "2026-11-21T09:00:00Z"),
                       run_(2, "2026-11-21T11:00:00Z")], {5533: "P"})
        self.assertEqual(pa.run(["o/a"], self.state, api=api, sleep=lambda s: None), 0)
        posts = [p for m, p in api.calls if m == "POST"]
        self.assertEqual(posts, ["repos/o/a/actions/runs/1/cancel", "repos/o/a/actions/runs/1/rerun"])
        self.assertEqual(Path(self.state).read_text(), "{}")

    def test_a_rerun_not_yet_possible_is_owed_and_paid_by_a_later_pass(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P"), run_(1, "2026-11-21T09:00:00Z")], {5533: "P"})
        real = api.__call__

        def slow(method, path):  # the cancel is accepted but GitHub has not completed the run yet
            ok, data, err = real(method, path)
            if path.endswith("/1/cancel"):
                api.runs[1]["status"] = "queued"
            return ok, data, err
        pa.run(["o/a"], self.state, api=slow, sleep=lambda s: None)
        self.assertIn("o/a 1", Path(self.state).read_text())
        api.runs[1]["status"] = "completed"
        api.label_prs = {}  # the priority PR merged; the debt is still paid
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertIn(("POST", "repos/o/a/actions/runs/1/rerun"), api.calls)
        self.assertEqual(Path(self.state).read_text(), "{}")

    def test_a_second_pass_bumps_nothing_again(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P"), run_(1, "2026-11-21T09:00:00Z")], {5533: "P"})
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        before = len([c for c in api.calls if c[0] == "POST"])
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertEqual(before, 2)
        self.assertEqual(len([c for c in api.calls if c[0] == "POST"]), before)

    def test_a_label_from_someone_without_maintain_access_is_ignored(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P"), run_(1, "2026-11-21T09:00:00Z")], {5533: "P"},
                      labeller="drive-by", role="triage")
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertEqual([p for m, p in api.calls if m == "POST"], [])

    def test_a_started_priority_run_counts_only_while_one_of_its_jobs_waits(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P", status="in_progress"),
                       run_(1, "2026-11-21T09:00:00Z")], {5533: "P"})
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertEqual([p for m, p in api.calls if m == "POST"], [])
        api.jobs[10] = [{"status": "completed", "created_at": "2026-11-21T10:00:00Z"},
                        {"status": "queued", "created_at": "2026-11-21T10:00:00Z"}]
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertIn(("POST", "repos/o/a/actions/runs/1/cancel"), api.calls)

    def test_owed_reruns_are_paid_oldest_first_by_number_not_text(self):
        Path(self.state).write_text('{"o/a 10": {"t": 100, "q": 2}, "o/a 9": {"t": 100, "q": 1}}')
        api = FakeApi([run_(9, "2026-11-21T09:00:00Z", status="completed"),
                       run_(10, "2026-11-21T09:30:00Z", status="completed")], {})
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None, now=lambda: 200)
        self.assertEqual([p for m, p in api.calls if m == "POST"],
                         ["repos/o/a/actions/runs/9/rerun", "repos/o/a/actions/runs/10/rerun"])

    def test_an_old_debt_is_dropped(self):
        Path(self.state).write_text('{"o/a 1": {"t": 0, "q": 0}}')
        api = FakeApi([run_(1, "2026-11-21T09:00:00Z", status="completed")], {})
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None, now=lambda: pa.OWE_FOR_S + 10)
        self.assertNotIn("POST", [m for m, _ in api.calls])
        self.assertEqual(Path(self.state).read_text(), "{}")

    def test_no_bumps_when_the_rate_limit_is_low(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P"), run_(1, "2026-11-21T09:00:00Z")], {1: "P"}, rate=100)
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertEqual([p for m, p in api.calls if m == "POST"], [])

    def test_a_finished_priority_pr_bumps_nothing(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P", status="completed"),
                       run_(1, "2026-11-21T09:00:00Z")], {1: "P"})
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertEqual([p for m, p in api.calls if m == "POST"], [])

    def test_the_label_is_url_encoded(self):
        api = FakeApi([], {})
        pa.run(["o/a"], self.state, api=api, sleep=lambda s: None)
        self.assertIn(("GET", "repos/o/a/issues?state=open&labels=ci%3Apriority&per_page=100"), api.calls)

    def test_pay_only_pays_owed_reruns_of_any_repo_and_bumps_nothing(self):
        Path(self.state).write_text('{"o/shaped 1": {"t": 100, "q": 1}}')
        api = FakeApi([run_(1, "2026-11-21T09:00:00Z", status="completed")], {})
        real = api.__call__

        def anyrepo(method, path):
            return real(method, path.replace("repos/o/shaped/", "repos/o/a/"))
        pa.run([], self.state, api=anyrepo, sleep=lambda s: None, now=lambda: 200)
        self.assertEqual([p for m, p in api.calls if m == "POST"], ["repos/o/a/actions/runs/1/rerun"])
        self.assertFalse(any("issues" in p or "status=queued" in p for _, p in api.calls))

    def test_dry_run_posts_nothing(self):
        api = FakeApi([run_(10, "2026-11-21T10:00:00Z", sha="P"), run_(1, "2026-11-21T09:00:00Z")], {1: "P"})
        pa.run(["o/a"], self.state, dry=True, api=api, sleep=lambda s: None)
        self.assertEqual([p for m, p in api.calls if m == "POST"], [])


if __name__ == "__main__":
    unittest.main()
