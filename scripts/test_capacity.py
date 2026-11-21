"""Tests for the steward's capacity check (scripts/steward.py capacity): stale queued runs and runner pools.

No network. Run:
    python3 -m unittest scripts/test_capacity.py -v
"""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import steward  # noqa: E402
import tempfile  # noqa: E402

# Never the real Mac's governor pauses: the suite gives the same results on the Mac mini as anywhere else.
steward.GOVERNOR_PAUSED_DIR = tempfile.mkdtemp()

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
        got = {i: why for i, why, _ in steward.stale_runs(queued, OPEN, closed_at=CLOSED, dedupe={"Auto-merge", "CI"})}
        self.assertEqual(got, {1: "closed", 2: "superseded", 4: "duplicate", 5: "duplicate"})

    def test_an_old_head_run_is_kept_unless_the_same_ci_is_queued_for_the_new_head(self):
        # A label-triggered run of a workflow not in the dedupe list is not started again by the push: keep it.
        queued = [run(1, "Label sync", "a", "a1"), run(2, "Label sync", "a", "a2"),
                  run(3, "CI", "a", "a1")]  # no CI queued for a2 (it may already have finished): keep it too
        self.assertEqual(steward.stale_runs(queued, OPEN, dedupe={"CI"}), [])

    def test_only_listed_workflows_are_deduplicated(self):
        queued = [run(4, "Label sync", "b", "b1"), run(5, "Label sync", "b", "b1"), run(6, "CI", "b", "b1"),
                  run(7, "CI", "b", "b1")]
        self.assertEqual([x[0] for x in steward.stale_runs(queued, OPEN)], [6])

    def test_dedupe_list_can_name_one_repo(self):
        rules = {"capacity": {"dedupe_workflows": ["CI", "o/app:Auto-merge"]}}
        self.assertEqual(steward._dedupe(rules, "o/app"), {"CI", "Auto-merge"})
        self.assertEqual(steward._dedupe(rules, "o/other"), {"CI"})

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
    def test_runs_on_a_bare_custom_label_is_self_hosted(self):
        runners = [runner("hph-ci-1", ["self-hosted", "hph-ci"])]
        _, f = steward.pool_report(runners, {("hph-ci",): 9}, 20, {("hph-ci",): 30})
        self.assertEqual(f[0]["kind"], "idle-but-queued")
        self.assertFalse(steward.self_hosted(["ubuntu-latest"]))
        self.assertTrue(steward.self_hosted(["hph-ci"]))

    def test_idle_capacity_that_no_queued_job_can_reach(self):
        runners = [runner("hph-ci-1", ["self-hosted", "hph-ci"], busy=True),
                   runner("hph-ci-10", ["self-hosted", "hph-ci-light"])]
        pools, f = steward.pool_report(runners, {SH: 150}, 20)
        self.assertEqual(pools[0]["idle"], 0)
        kinds = [x["kind"] for x in f]
        self.assertIn("idle-capacity-unreachable", kinds)
        import datetime as dt
        state, t0 = {}, dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(steward.settle(f, state, t0, 30), [])  # the finding that crashed settle() before
        ready = steward.settle(f, state, t0 + dt.timedelta(minutes=10), 30)
        acts = steward.capacity_actions([], ready, RULES)
        self.assertEqual([a["do"] for a in acts], ["reroute"])
        # A lasting mismatch is one reroute per report cooldown, not a PR every pass, even as idle runners change.
        f = [dict(f[0], idle=[{"runner": "other", "labels": []}])] if f[0]["kind"] == "idle-capacity-unreachable" else f
        self.assertEqual(steward.settle(f, state, t0 + dt.timedelta(minutes=20), 30), [])
        self.assertEqual(len(steward.settle(f, state, t0 + dt.timedelta(minutes=260), 30)), 1)

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
        dos = []
        for i in range(12):
            ready = steward.settle(f, state, t + dt.timedelta(minutes=40 * i), 30, max_restarts=3)
            dos += [a["do"] for a in steward.capacity_actions([], ready, RULES)]
        self.assertEqual(dos.count("restart-runner"), 3)
        self.assertEqual(dos.count("report"), 2)  # once, then again only after the 4-hour report cooldown
        self.assertEqual(dos[3], "report")

    def test_a_finding_missing_for_one_pass_keeps_its_restart_count_and_cooldown(self):
        import datetime as dt
        t = dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        f2 = [{"kind": "runner-offline", "runner": "dind-1", "labels": []}]
        state, acts = {}, 0
        steward.settle(f2, state, t, 30)
        acts += len(steward.settle(f2, state, t + dt.timedelta(minutes=10), 30))  # restart 1
        steward.settle([], state, t + dt.timedelta(minutes=20), 30)  # missing once
        steward.settle(f2, state, t + dt.timedelta(minutes=30), 30)  # first sighting again
        acts += len(steward.settle(f2, state, t + dt.timedelta(minutes=35), 30))  # inside the 30-minute cooldown
        self.assertEqual(acts, 1)
        self.assertEqual(state["restarts"]["r|dind-1"], 1)

    def test_a_runner_is_restarted_once_per_pass_and_capped_across_every_kind(self):
        import datetime as dt
        t = dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        both = [{"kind": "runner-offline", "runner": "dind-1", "labels": []},
                {"kind": "pool-down", "labels": ["hph-ci"], "queued_jobs": 5, "offline": ["dind-1"]}]
        state = {}
        steward.settle(both, state, t, 30)
        ready = steward.settle(both, state, t + dt.timedelta(minutes=10), 30)
        self.assertEqual([f["kind"] for f in ready], ["pool-down"])
        self.assertEqual(state["restarts"], {"r|dind-1": 1})
        hung = [{"kind": "idle-but-queued", "idle": ["dind-1"], "queued_jobs": 5}]
        steward.settle(hung, state, t + dt.timedelta(minutes=50), 30)
        steward.settle(hung, state, t + dt.timedelta(minutes=60), 30)
        steward.settle(hung, state, t + dt.timedelta(minutes=100), 30)
        ready = steward.settle(hung, state, t + dt.timedelta(minutes=140), 30)
        self.assertEqual(state["restarts"]["r|dind-1"], 3)
        self.assertTrue(ready and ready[0].get("escalate"))

    def test_restart_history_is_forgotten_when_the_runner_is_online_or_long_gone(self):
        import datetime as dt
        f = [{"kind": "runner-offline", "runner": "dind-1", "labels": []}]
        t = dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        state = {}
        steward.settle(f, state, t, 30)
        steward.settle(f, state, t + dt.timedelta(minutes=10), 30)
        steward.settle([], state, t + dt.timedelta(minutes=20), 30, online={"dind-1"})
        self.assertNotIn("r|dind-1", state["restarts"])
        state = {}
        steward.settle(f, state, t, 30)
        steward.settle(f, state, t + dt.timedelta(minutes=10), 30)
        steward.settle([], state, t + dt.timedelta(minutes=60), 30)
        self.assertIn("r|dind-1", state["restarts"])
        steward.settle([], state, t + dt.timedelta(minutes=10 + 241), 30)
        self.assertNotIn("r|dind-1", state["restarts"])

    def test_machine_restart_only_when_every_runner_on_the_box_is_idle(self):
        rules = dict(RULES, capacity=dict(RULES["capacity"], host=dict(RULES["capacity"]["host"],
                     runners={"colima": ["davids-mac-mini-dind-"], "mini": ["davids-mac-mini-"]})))
        f = [{"kind": "runner-offline", "runner": "davids-mac-mini-dind-sconsinc-1", "labels": []}]
        runners = [{"name": "davids-mac-mini-dind-sconsinc-1", "status": "offline", "busy": False},
                   {"name": "davids-mac-mini-dind-sconsinc-2", "status": "online", "busy": True},
                   {"name": "davids-mac-mini-hph-1", "status": "online", "busy": True}]
        a = steward.capacity_actions([], f, rules, runners)[0]
        self.assertEqual((a["box"], a["machine_restart_ok"], a["busy"]),
                         ("colima", False, ["davids-mac-mini-dind-sconsinc-2"]))
        runners[1]["busy"] = False
        a = steward.capacity_actions([], f, rules, runners)[0]
        self.assertTrue(a["machine_restart_ok"])  # the busy runner is on the Mac itself, not in the VM
        self.assertFalse(steward.capacity_actions([], f, rules, None)[0]["machine_restart_ok"])

    def test_no_runner_with_the_labels_is_a_report_for_a_person(self):
        _, f = steward.pool_report([], {("self-hosted", "linux-large"): 4}, 20)
        a = steward.capacity_actions([], [dict(f[0], repos=["o/a"])], RULES)
        self.assertEqual((a[0]["do"], a[0]["needs_person"]), ("report", True))

    def test_paged_appends_to_an_existing_query(self):
        seen = []
        old = steward.api
        steward.api = lambda path: seen.append(path) or {"workflow_runs": []}
        try:
            steward.paged("repos/o/r/actions/runs?status=queued", key="workflow_runs")
        finally:
            steward.api = old
        self.assertEqual(seen, ["repos/o/r/actions/runs?status=queued&per_page=100&page=1"])

    def test_cancel_action_is_capped(self):
        stale = [(i, "closed", "CI") for i in range(RULES["capacity"]["max_cancels"] + 10)]
        a = steward.capacity_actions(stale, [], RULES)[0]
        self.assertEqual(len(a["runs"]), RULES["capacity"]["max_cancels"])
        self.assertFalse(a["needs_person"])


def host(name, load5, cpus=32, mem_avail=100, mem_total=128, disk=0.05):
    g = 1 << 30
    return {"host": name, "ok": True, "cpus": cpus, "load5": load5, "MemTotal": mem_total * g,
            "MemAvailable": mem_avail * g, "disks": [{"mount": "/", "size": 1000, "used": int(disk * 1000)}]}


BACKED = [{"labels": ["hph-ci"], "queued_jobs": 100, "idle": 0}]
SLOTS = [runner("hph-ci-1", ["hph-ci"], busy=True), runner("hph-ci-2", ["hph-ci"]), runner("hph-ci-3", ["hph-ci"]),
         runner("davids-mac-mini-dind-sconsinc-1", ["shorconsulting-ci"])]


class Hosts(unittest.TestCase):
    def setUp(self):
        import datetime as dt
        self.t = dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)
        self.dt = dt

    def test_levels(self):
        self.assertEqual(steward.host_pressure(host("h", 52), RULES)[0], "red")       # 1.6 per core
        self.assertEqual(steward.host_pressure(host("h", 20, mem_avail=4), RULES)[0], "red")  # memory 97%
        self.assertEqual(steward.host_pressure(host("h", 20, disk=0.92), RULES)[0], "red")
        self.assertEqual(steward.host_pressure(host("h", 30), RULES)[0], "ok")         # 0.94 per core
        self.assertEqual(steward.host_pressure(host("h", 16), RULES)[0], "headroom")
        self.assertEqual(steward.host_pressure({"host": "h", "ok": False}, RULES)[0], "unknown")
        partial = dict(host("h", 16))
        partial.pop("MemAvailable")
        self.assertEqual(steward.host_pressure(partial, RULES)[0], "unknown")  # not "100% memory used"

    def test_disk_and_no_slot_reports_use_the_report_cooldown_and_never_delay_a_resume(self):
        state, T = {}, lambda m: self.t + self.dt.timedelta(minutes=m)
        dos = []
        for i in range(10):  # 100 minutes of a full disk
            dos += [x["do"] for x in steward.host_actions([host("hph-ci", 16, disk=0.95)], BACKED, RULES, state, T(10 * i))]
        self.assertEqual(dos, ["report"])
        self.assertNotIn("changed", state["hosts"]["hph-ci"])

    def test_read_hosts_ignores_json_that_is_not_a_host_list(self):
        import tempfile
        for body in ("null", "3", '"x"', '{"ci": "x"}', '{"hosts": 4}'):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
                fh.write(body)
            self.assertEqual(steward.read_hosts(fh.name), [])
            os.unlink(fh.name)

    def test_a_paused_slot_seen_online_is_unpaused_and_a_long_pause_is_reported(self):
        T = lambda m: self.t + self.dt.timedelta(minutes=m)
        stamp = T(0).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = {"hosts": {"hph-ci": {"paused": ["hph-ci-3", "hph-ci-4"],
                                      "paused_at": {"hph-ci-3": stamp, "hph-ci-4": stamp}}}}
        slots = [runner("hph-ci-3", ["hph-ci"], status="online"), runner("hph-ci-4", ["hph-ci"], status="offline")]
        steward.host_actions([host("hph-ci", 30)], [], RULES, state, T(10), slots)
        self.assertEqual(state["hosts"]["hph-ci"]["paused"], ["hph-ci-4"])  # 3 is running again
        a = steward.host_actions([host("hph-ci", 30)], [], RULES, state, T(13 * 60), slots)
        self.assertEqual([(x["do"], x["needs_person"], x["runners"]) for x in a], [("report", True, ["hph-ci-4"])])
        self.assertEqual(steward.host_actions([host("hph-ci", 30)], [], RULES, state, T(13 * 60 + 10), slots), [])

    def test_a_pause_not_yet_confirmed_still_protects_the_slot_from_a_restart(self):
        state, T = {}, lambda m: self.t + self.dt.timedelta(minutes=m)
        steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(0))
        steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(10), SLOTS)
        self.assertEqual(steward.paused_runners(state, T(20)), ["hph-ci-3"])
        self.assertEqual(steward.paused_runners(state, T(10 + 60 * steward.PENDING_PAUSE_HOURS)), [])
        steward.host_actions([host("hph-ci", 16)], [], RULES, state, T(10 + 60 * steward.PENDING_PAUSE_HOURS))
        self.assertEqual(state["hosts"]["hph-ci"].get("pending_pause"), {})  # expired entries are pruned

    def test_pause_needs_two_red_passes_then_resume_with_headroom(self):
        state, T = {}, lambda m: self.t + self.dt.timedelta(minutes=m)
        self.assertEqual(steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(0)), [])  # first red
        a = steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(10), SLOTS)
        self.assertEqual([(x["do"], x["runner"]) for x in a], [("pause-slot", "hph-ci-3")])
        self.assertEqual(steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(15), SLOTS), [])
        # Nothing recorded as paused (the steward didn't confirm it): headroom has nothing to resume.
        self.assertEqual(steward.host_actions([host("hph-ci", 16)], BACKED, RULES, state, T(40)), [])
        state["hosts"]["hph-ci"]["paused"].append("hph-ci-7")
        a = steward.host_actions([host("hph-ci", 16)], BACKED, RULES, state, T(70))
        self.assertEqual([(x["do"], x["runner"]) for x in a], [("resume-slot", "hph-ci-7")])

    def test_pause_never_takes_a_busy_slot_or_goes_below_the_minimum(self):
        T = lambda m: self.t + self.dt.timedelta(minutes=m)
        busy = [runner("hph-ci-%d" % i, ["hph-ci"], busy=True) for i in range(1, 5)]
        state = {}
        steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(0), busy)
        a = steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(10), busy)
        self.assertEqual([x["do"] for x in a], ["report"])
        two = [runner("hph-ci-1", ["hph-ci"], busy=True), runner("hph-ci-2", ["hph-ci"])]
        state = {}
        steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(0), two)
        a = steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(10), two)
        self.assertEqual([x["do"] for x in a], ["report"])  # 2 online, min 2: pausing one would go below
        state = {}
        steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(0), None)
        a = steward.host_actions([host("hph-ci", 52)], BACKED, RULES, state, T(10), None)
        self.assertEqual([x["do"] for x in a], ["report"])  # runners unknown: never a blind pause

    def test_a_disk_only_red_is_a_report_not_a_pause(self):
        state = {}
        for m in (0, 10):
            a = steward.host_actions([host("mini", 4, cpus=10, disk=0.93)], BACKED, RULES, state,
                                     self.t + self.dt.timedelta(minutes=m))
        self.assertNotIn("pause-slot", [x["do"] for x in a])
        a = steward.host_actions([host("mini", 4, cpus=10, disk=0.93)], BACKED, RULES, state,
                                 self.t + self.dt.timedelta(minutes=240))
        self.assertEqual([x["do"] for x in a], ["report"])  # again only after the report cooldown

    def test_colima_runners_belong_to_the_vm_not_the_mac(self):
        boxes = {"mac": ["davids-mac-mini-"], "colima": ["davids-mac-mini-dind-"]}
        self.assertEqual(steward.box_of("davids-mac-mini-dind-sconsinc-1", boxes), "colima")
        self.assertEqual(steward.box_of("davids-mac-mini-1", boxes), "mac")
        self.assertIsNone(steward.box_of("hph-ci-1", boxes))

    def test_resume_only_when_the_backlog_fits_the_paused_slot(self):
        state = {"hosts": {"hph-ci": {"paused": ["hph-ci-light-1"]}}}
        slots = [runner("hph-ci-light-1", ["self-hosted", "hph-ci-light"], status="offline")]
        self.assertEqual(steward.host_actions([host("hph-ci", 16)], BACKED, RULES, state, self.t, slots), [])
        slots = [runner("hph-ci-light-1", ["self-hosted", "hph-ci"], status="offline")]
        a = steward.host_actions([host("hph-ci", 16)], BACKED, RULES, state, self.t, slots)
        self.assertEqual([x["do"] for x in a], ["resume-slot"])

    def test_no_resume_without_a_backlog(self):
        state = {"hosts": {"hph-ci": {"paused": ["hph-ci-7"]}}}
        self.assertEqual(steward.host_actions([host("hph-ci", 16)], [], RULES, state, self.t), [])

    def test_a_paused_slot_is_never_restarted_by_the_pool_check(self):
        state = {"hosts": {"hph-ci": {"paused": ["hph-ci-7"]}}}
        runners = [runner("hph-ci-7", ["self-hosted", "hph-ci"], status="offline"),
                   runner("hph-ci-1", ["self-hosted", "hph-ci"], busy=True)]
        dos = []
        for m in (0, 10, 20):
            _, f = steward.pool_report(runners, {SH: 50}, 20, {SH: 60}, 10, steward.paused_runners(state))
            ready = steward.settle(f, state.setdefault("HPortHealth", {}), self.t + self.dt.timedelta(minutes=m), 30)
            dos += [a["do"] for a in steward.capacity_actions([], ready, RULES)]
        self.assertNotIn("restart-runner", dos)
        self.assertNotIn("report", dos)

    def test_capacity_note_records_and_clears_a_paused_slot(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            steward.main(["capacity-note", "--host", "hph-ci", "--paused", "hph-ci-7", "--state", path])
            self.assertEqual(steward.paused_runners(steward._read_state(path)), ["hph-ci-7"])
            steward.main(["capacity-note", "--host", "hph-ci", "--resumed", "hph-ci-7", "--state", path])
            self.assertEqual(steward.paused_runners(steward._read_state(path)), [])


class Owners(unittest.TestCase):
    def setUp(self):
        import datetime as dt
        self.dt, self.t = dt, dt.datetime(2026, 11, 19, 14, 0, tzinfo=dt.timezone.utc)

    def agg(self, labels=None, runners=(), oldest=60):
        labels = {SH: 30} if labels is None else labels
        return {"labels": labels, "oldest": {k: oldest for k in labels}, "runners": list(runners)}

    def test_a_lean_pass_keeps_restart_counts(self):
        org = [runner("dind-1", ["self-hosted", "hph-ci"], status="offline")]
        state, dos = {}, []
        for i in range(10):
            lean = i % 2 == 1  # every other pass is lean: it must not reset anything
            res = steward.owner_pools("o", org, {"o/r": self.agg()}, state, RULES,
                                      self.t + self.dt.timedelta(minutes=40 * i), lean)
            if res:
                dos += [a["do"] for a in steward.capacity_actions([], res[2], RULES)]
        self.assertEqual(dos.count("restore-pool"), 3)  # its pool is down: one restart kind, counted per runner
        self.assertNotIn("restart-runner", dos)
        self.assertIn("report", dos)

    def test_one_shared_offline_runner_is_one_restart_for_all_repos(self):
        org = [runner("dind-1", ["self-hosted", "hph-ci"], status="offline", rid=7)]
        aggs = {"o/a": self.agg(), "o/b": self.agg(), "o/c": self.agg()}
        state, dos = {}, []
        for i in range(12):
            res = steward.owner_pools("o", org, aggs, state, RULES, self.t + self.dt.timedelta(minutes=10 * i), False)
            dos += [a["do"] for a in steward.capacity_actions([], res[2], RULES)]
        # The pool is down, so the runner is restarted as restore-pool only, never also as runner-offline: three
        # restarts in all across every repo and kind, then one escalation.
        self.assertEqual(dos.count("restore-pool"), 3)
        self.assertEqual(dos.count("restart-runner"), 0)
        self.assertEqual(dos.count("report"), 1)

    def test_idle_but_queued_needs_runner_groups_and_respects_them(self):
        org = [runner("r1", ["self-hosted", "hph-ci"], rid=1)]
        aggs = {"o/a": self.agg()}
        _, f, _ = steward.owner_pools("o", org, aggs, {}, RULES, self.t, False, groups=None)
        self.assertNotIn("idle-but-queued", [x["kind"] for x in f])  # can't tell if o/a may use r1
        _, f, _ = steward.owner_pools("o", org, aggs, {}, RULES, self.t, False, groups={1: {"o/other"}})
        self.assertNotIn("idle-but-queued", [x["kind"] for x in f])  # r1 isn't allowed for o/a
        _, f, _ = steward.owner_pools("o", org, aggs, {}, RULES, self.t, False, groups={1: None})
        self.assertIn("idle-but-queued", [x["kind"] for x in f])

    def test_a_malformed_host_reading_never_crashes(self):
        bad = [{"ok": True}, {"host": "x", "ok": True, "cpus": 4, "MemTotal": 8, "disks": [{"size": 10}]}, "junk"]
        steward.host_actions(bad, [], RULES, {}, self.t)

    def test_a_repo_level_runner_only_serves_its_repo(self):
        mine = runner("repo-b-runner", ["self-hosted", "hph-ci"])  # idle, belongs to o/b
        aggs = {"o/a": self.agg(), "o/b": self.agg(labels={})}
        aggs["o/b"]["runners"] = [mine]
        org = [runner("hph-ci-1", ["self-hosted", "hph-ci"], busy=True)]
        _, f, _ = steward.owner_pools("o", org, aggs, {}, RULES, self.t, False)
        self.assertNotIn("idle-but-queued", [x["kind"] for x in f])  # o/a's jobs can't use o/b's runner

    def test_unreadable_org_runners_never_read_as_a_dead_pool(self):
        res = steward.owner_pools("o", None, {"o/a": self.agg()}, {}, RULES, self.t, False)
        self.assertEqual(res[1], [])
        with_own = {"o/a": self.agg(runners=[runner("own", ["self-hosted", "hph-ci"], status="offline")])}
        _, f, _ = steward.owner_pools("o", None, with_own, {}, RULES, self.t, False)
        self.assertEqual([x["kind"] for x in f], ["runner-offline"])

    def test_unknown_host_and_missing_readings_report_once_per_cooldown(self):
        state, T = {}, lambda m: self.t + self.dt.timedelta(minutes=m)
        down = [{"host": "mini", "ok": False, "error": "timeout"}]
        self.assertEqual(len(steward.host_actions(down, [], RULES, state, T(0))), 1)
        self.assertEqual(steward.host_actions(down, [], RULES, state, T(10)), [])
        state["hosts"]["hph-ci"] = {"paused": ["hph-ci-7"]}
        a = steward.hosts_missing("http://127.0.0.1:4517/api/data", [], state, RULES, T(0))
        self.assertTrue(a[0]["needs_person"])
        self.assertEqual(steward.hosts_missing("http://127.0.0.1:4517/api/data", [], state, RULES, T(10)), [])


class CapacityRepo(unittest.TestCase):
    def test_closed_lookups_are_cached_encoded_and_skipped_when_lean(self):
        calls = []
        queued = [run(1, "CI", "fix#1", "x", created="2026-11-19T09:00:00Z"), run(2, "CI", "b", "b1")]

        def fake_paged(path, key=None, cap=1000, per_page=100):
            if "actions/runs" in path:
                return queued
            if "/pulls" in path:
                return OPEN
            return []

        def fake_api(path):
            calls.append(path)
            if "state=closed" in path:
                return [{"closed_at": "2026-11-19T12:00:00Z"}]
            return {"jobs": []}
        old = steward.paged, steward.api
        steward.paged, steward.api = fake_paged, fake_api
        try:
            cache = {}
            rep = steward.capacity_repo("o/r", RULES, 5, cache)
            self.assertEqual([x["run"] for x in rep["stale"]], [1])
            self.assertTrue(any("head=o%3Afix%231" in c for c in calls))
            n = len(calls)
            steward.capacity_repo("o/r", RULES, 5, cache)
            self.assertFalse(any("state=closed" in c for c in calls[n:]))  # cached
            calls.clear()
            steward.capacity_repo("o/r", RULES, 5, {}, lean=True)
            self.assertEqual([c for c in calls if "state=closed" in c or "/jobs" in c], [])
        finally:
            steward.paged, steward.api = old


class Sampling(unittest.TestCase):
    def test_sampled_job_counts_are_scaled_to_the_whole_queue(self):
        queued = [run(i, "CI", "b", "b1", wid=i, created="2026-11-19T%02d:00:00Z" % i) for i in range(1, 11)]
        old = steward.paged, steward.api
        steward.paged = lambda path, key=None, cap=1000, per_page=100: queued if "actions/runs" in path else (
            OPEN if "/pulls" in path else [])
        steward.api = lambda path: {"jobs": [{"status": "queued", "labels": ["hph-ci"],
                                              "created_at": "2026-11-19T01:00:00Z"}]}
        try:
            rep = steward.capacity_repo("o/r", RULES, 2, {})
        finally:
            steward.paged, steward.api = old
        self.assertEqual(rep["_labels"], {("hph-ci",): 10})  # 2 sampled runs of 10, one job each


class Rules(unittest.TestCase):
    def test_a_custom_rules_file_without_capacity_gets_the_defaults(self):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"capacity": {"max_cancels": 5, "host": {"min_slots": 4}}}, fh)
        r = steward.load_rules(fh.name)
        os.unlink(fh.name)
        self.assertEqual((r["capacity"]["max_cancels"], r["capacity"]["host"]["min_slots"]), (5, 4))
        self.assertEqual(r["capacity"]["sample_runs"], RULES["capacity"]["sample_runs"])
        self.assertEqual(r["capacity"]["host"]["red_mem_pct"], RULES["capacity"]["host"]["red_mem_pct"])


class StateFile(unittest.TestCase):
    def test_state_is_written_atomically_and_one_pass_holds_the_lock(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "state.json")
        steward._write_state(path, {"a": 1})
        self.assertEqual(steward._read_state(path), {"a": 1})
        self.assertEqual([n for n in os.listdir(d) if n.endswith(".tmp")], [])
        lock = steward._take_lock(path)
        self.assertIsNotNone(lock)
        self.assertIsNone(steward._take_lock(path))
        old = time.time() - (steward.LOCK_STALE_MINUTES + 1) * 60
        os.utime(lock, (old, old))
        self.assertIsNotNone(steward._take_lock(path))  # a lock from a run that died is taken over
        os.unlink(lock)


class StackedWorkflowTest(unittest.TestCase):
    """A workflow queueing several runs per open pull request (app's Auto-merge disarm, 2026-11-20) is flagged."""

    def prs(self, n):
        return [{"number": i, "head": {"sha": "s%d" % i, "ref": "b%d" % i, "repo": {"full_name": "o/r"}}}
                for i in range(n)]

    def test_flags_a_workflow_stacking_runs_per_pr(self):
        queued = [run(i, "Auto-merge", "b%d" % (i % 20), "s%d" % (i % 20)) for i in range(80)]
        queued += [run(1000 + i, "CI", "b%d" % i, "s%d" % i) for i in range(20)]
        out = steward.stacked_workflows(queued, self.prs(20), min_runs=50, per_pr=2.0)
        self.assertEqual([w["workflow"] for w in out], ["Auto-merge"])
        self.assertEqual((out[0]["queued"], out[0]["prs"], out[0]["per_pr"]), (80, 20, 4.0))

    def test_one_run_per_pr_is_demand_not_stacking(self):
        queued = [run(i, "CI", "b%d" % i, "s%d" % i) for i in range(200)]
        self.assertEqual(steward.stacked_workflows(queued, self.prs(200)), [])

    def test_small_queues_and_closed_heads_are_ignored(self):
        few = [run(i, "Auto-merge", "b0", "s0") for i in range(10)]
        self.assertEqual(steward.stacked_workflows(few, self.prs(1), min_runs=50), [])
        gone = [run(i, "Auto-merge", "x", "old") for i in range(100)]
        self.assertEqual(steward.stacked_workflows(gone, self.prs(1)), [])


class AddSlot(unittest.TestCase):
    """add-slot (2026-11-20): every slot busy, headroom, a backlog it fits, under the box's ceiling."""

    def setUp(self):
        import copy
        import datetime as dt
        self.t = dt.datetime(2026, 11, 20, 20, 0, tzinfo=dt.timezone.utc)
        self.rules = copy.deepcopy(RULES)
        self.rules["capacity"]["host"]["max_slots"] = {"hph-ci": 4}

    def slots(self, n, busy=True):
        return [runner("hph-ci-%d" % i, ["self-hosted", "hph-ci"], status="online", busy=busy) for i in range(1, n + 1)]

    def test_adds_one_slot_when_all_busy_with_headroom_and_a_fitting_backlog(self):
        a = steward.host_actions([host("hph-ci", 16)], BACKED, self.rules, {}, self.t, self.slots(3))
        self.assertEqual([x["do"] for x in a], ["add-slot"])
        self.assertEqual((a[0]["online"], a[0]["max"]), (3, 4))

    def test_never_past_the_ceiling_nor_with_an_idle_slot_nor_without_a_ceiling(self):
        self.assertEqual(steward.host_actions([host("hph-ci", 16)], BACKED, self.rules, {}, self.t, self.slots(4)), [])
        idle = self.slots(2) + [runner("hph-ci-3", ["self-hosted", "hph-ci"], status="online", busy=False)]
        self.assertEqual(steward.host_actions([host("hph-ci", 16)], BACKED, self.rules, {}, self.t, idle), [])
        self.assertEqual(steward.host_actions([host("hph-ci", 16)], BACKED, RULES, {}, self.t, self.slots(3)), [])

    def test_no_add_under_load_and_one_change_per_cooldown(self):
        self.assertNotIn("add-slot", [x["do"] for x in steward.host_actions(
            [host("hph-ci", 30)], BACKED, self.rules, {}, self.t, self.slots(3))])  # ok, not headroom
        state = {}
        steward.host_actions([host("hph-ci", 16)], BACKED, self.rules, state, self.t, self.slots(3))
        later = self.t + __import__("datetime").timedelta(minutes=5)
        self.assertEqual(steward.host_actions([host("hph-ci", 16)], BACKED, self.rules, state, later, self.slots(3)), [])



class GovernorPaused(unittest.TestCase):
    def test_runners_the_mac_load_governor_paused_are_never_offline_or_restarted(self):
        import tempfile
        d = tempfile.mkdtemp()
        for f in ("davids-mac-mini-dind-sconsinc-4", "davids-mac-mini-dind-sconsinc-4.labels"):
            open(os.path.join(d, f), "w").close()
        self.assertEqual(steward.governor_paused(d), ["davids-mac-mini-dind-sconsinc-4"])
        self.assertEqual(steward.governor_paused(os.path.join(d, "missing")), [])
        runners = [{"name": "davids-mac-mini-dind-sconsinc-4", "status": "offline", "busy": False,
                    "labels": [{"name": "shorconsulting-ci"}]},
                   {"name": "davids-mac-mini-dind-sconsinc-5", "status": "online", "busy": True,
                    "labels": [{"name": "shorconsulting-ci"}]}]
        _, f = steward.pool_report(runners, {("shorconsulting-ci",): 50}, 20, {("shorconsulting-ci",): 60}, 10,
                                   steward.governor_paused(d))
        self.assertEqual([x for x in f if x.get("runner") == "davids-mac-mini-dind-sconsinc-4"], [])

    def test_a_governor_pause_older_than_a_day_is_not_counted(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "davids-mac-mini-dind-sconsinc-4")
        open(path, "w").close()
        old = time.time() - (steward.GOVERNOR_PAUSED_MAX_HOURS + 1) * 3600
        os.utime(path, (old, old))
        self.assertEqual(steward.governor_paused(d), [])

if __name__ == "__main__":
    unittest.main()
