import unittest

import priority_runner as pr

POOL, LABEL = "shorconsulting-ci", "ci-priority"
LENT = (LABEL, LABEL + "-lent")   # what a lent runner carries


def runner(name, *labels, busy=True, status="online"):
    return {"id": abs(hash(name)) % 1000, "name": name, "busy": busy, "status": status,
            "labels": [{"name": l} for l in ("self-hosted",) + labels]}


class Plan(unittest.TestCase):
    def plan(self, runners, waiting):
        return [(what, r["name"]) for what, r in pr.plan(runners, waiting, POOL, LABEL)]

    def test_nothing_waiting_nothing_lent(self):
        self.assertEqual(self.plan([runner("a", POOL), runner("b", POOL)], 0), [])

    def test_lends_one_pool_runner_idle_first(self):
        rs = [runner("a", POOL), runner("b", POOL, busy=False), runner("gate", "autopilot", busy=False)]
        self.assertEqual(self.plan(rs, 3), [("lend", "b")])

    def test_already_lent_lends_no_second(self):
        self.assertEqual(self.plan([runner("a", *LENT), runner("b", POOL)], 2), [])

    def test_restores_every_lent_runner_when_nothing_waits(self):
        rs = [runner("a", *LENT), runner("b", *LENT, status="offline"), runner("c", POOL)]
        self.assertEqual(self.plan(rs, 0), [("restore", "a"), ("restore", "b")])

    def test_offline_runners_are_not_lent_and_do_not_count_as_lent(self):
        rs = [runner("a", *LENT, status="offline"), runner("b", POOL, status="offline"), runner("c", POOL)]
        self.assertEqual(self.plan(rs, 1), [("lend", "c")])

    def test_a_half_lent_runner_is_finished_or_given_back(self):
        # a pass that died after adding the labels and before removing the pool's
        half = [runner("a", POOL), runner("b", POOL, *LENT)]
        self.assertEqual(self.plan(half, 1), [("lend", "b")])
        self.assertEqual(self.plan(half, 0), [("restore", "b")])

    def test_a_runner_labelled_by_hand_is_never_pulled_into_the_pool(self):
        rs = [runner("dedicated", LABEL), runner("gate", "autopilot", LABEL), runner("c", POOL)]
        self.assertEqual(self.plan(rs, 0), [])

    def test_no_pool_runner_online(self):
        self.assertEqual(self.plan([runner("a", POOL, status="offline")], 1), [])


class Tick(unittest.TestCase):
    def api(self, jobs, runners):
        calls = []

        def api(method, path, **fields):
            calls.append((method, path, fields))
            if path == "search/issues":
                return {"items": [{"number": 7, "repository_url": "https://api.github.com/repos/o/r"}]}
            if path == "repos/o/r/pulls/7":
                return {"head": {"sha": "abc"}}
            if path == "repos/o/r/actions/runs":
                return {"workflow_runs": [{"id": 1, "status": "queued"}, {"id": 2, "status": "completed"}]}
            if path == "repos/o/r/actions/runs/1/jobs":
                return {"jobs": jobs}
            if path == "orgs/o/actions/runners":
                return {"runners": runners}
            return None
        return api, calls

    def test_counts_only_queued_jobs_that_ask_for_the_label(self):
        jobs = [{"status": "queued", "labels": [LABEL]}, {"status": "queued", "labels": [POOL]},
                {"status": "in_progress", "labels": [LABEL]}]
        api, _ = self.api(jobs, [])
        self.assertEqual(pr.waiting_jobs("o", LABEL, api), 1)

    def test_lend_adds_the_label_before_removing_the_pool(self):
        api, calls = self.api([{"status": "queued", "labels": [LABEL]}], [runner("a", POOL)])
        pr.tick("o", POOL, LABEL, api=api)
        writes = [(m, p.rsplit("/", 1)[-1]) for m, p, _ in calls if m != "GET"]
        self.assertEqual(writes, [("POST", "labels"), ("POST", "labels"), ("DELETE", POOL)])

    def test_restore_adds_the_pool_first_and_drops_the_marker_last(self):
        api, calls = self.api([], [runner("a", *LENT)])
        pr.tick("o", POOL, LABEL, api=api)
        writes = [(m, p.rsplit("/", 1)[-1]) for m, p, _ in calls if m != "GET"]
        self.assertEqual(writes, [("POST", "labels"), ("DELETE", LABEL), ("DELETE", LABEL + "-lent")])

    def test_not_knowing_what_waits_gives_lent_runners_back(self):
        import subprocess
        inner, calls = self.api([], [runner("a", *LENT)])

        def api(method, path, **fields):
            if path == "search/issues":
                raise subprocess.CalledProcessError(1, "gh", stderr="rate limit")
            return inner(method, path, **fields)
        pr.tick("o", POOL, LABEL, api=api)
        self.assertTrue(any(m == "DELETE" and p.endswith("/" + LABEL) for m, p, _ in calls))

    def test_dry_run_changes_nothing(self):
        api, calls = self.api([{"status": "queued", "labels": [LABEL]}], [runner("a", POOL)])
        pr.tick("o", POOL, LABEL, dry_run=True, api=api)
        self.assertEqual([c for c in calls if c[0] != "GET"], [])


if __name__ == "__main__":
    unittest.main()
