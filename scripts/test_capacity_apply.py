import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capacity_apply  # noqa: E402

RX = r"^hph-ci-(?:light-)?(\d+)(?:-|$)"
REPORT = {
    "repos": [{"repo": "o/a", "actions": [{"do": "cancel-stale", "runs": [1, 2, 3], "why": "stale"}]}],
    "owners": [{"owner": "o", "actions": [
        {"do": "restart-runner", "runner": "hph-ci-7-1795104378", "needs_person": False},
        {"do": "restore-pool", "runner": "hph-ci-7-1795104378, hph-ci-light-10, davids-mac-mini-dind-sconsinc-1",
         "needs_person": False},
        {"do": "report", "runner": "hph-ci-3-1", "needs_person": True},
    ]}],
}


class Plan(unittest.TestCase):
    def test_cancels_stale_and_restarts_only_this_boxs_slots_once(self):
        p = capacity_apply.plan(REPORT, RX, "systemctl restart hph-runner@{slot}", max_cancels=2)
        cancels = [c for k, c, _ in p if k == "cancel"]
        restarts = [c for k, c, _ in p if k == "restart"]
        self.assertEqual(cancels, [["gh", "api", "-X", "POST", "repos/o/a/actions/runs/1/cancel"],
                                   ["gh", "api", "-X", "POST", "repos/o/a/actions/runs/2/cancel"]])
        self.assertEqual(restarts, [["systemctl", "restart", "hph-runner@7"],
                                    ["systemctl", "restart", "hph-runner@10"]])

    def test_without_a_restart_command_it_only_cancels(self):
        p = capacity_apply.plan(REPORT, "", "")
        self.assertEqual({k for k, _, _ in p}, {"cancel"})


class Guards(unittest.TestCase):
    def test_the_cancel_cap_is_per_repo_across_actions(self):
        r = {"repos": [{"repo": "o/a", "actions": [{"do": "cancel-stale", "runs": [1, 2, 3]},
                                                   {"do": "cancel-stale", "runs": [4, 5]}]}]}
        self.assertEqual(len(capacity_apply.plan(r, "", "", max_cancels=4)), 4)

    def test_cancels_marked_for_a_person_are_skipped(self):
        r = {"repos": [{"repo": "o/a", "actions": [{"do": "cancel-stale", "runs": [9], "needs_person": True}]}]}
        self.assertEqual(capacity_apply.plan(r, "", ""), [])

    def test_a_slot_restarts_once_per_pass_across_org_reports(self):
        seen = set()
        a = capacity_apply.plan(REPORT, RX, "systemctl restart hph-runner@{slot}", seen=seen, cancel=False)
        b = capacity_apply.plan(REPORT, RX, "systemctl restart hph-runner@{slot}", seen=seen, cancel=False)
        self.assertEqual(len([x for x in a if x[0] == "restart"]), 2)
        self.assertEqual(b, [])


class AddSlot(unittest.TestCase):
    RX = r"^hph-ci-(?:light-)?([0-9]+)(?:-|$)"

    def rep(self, host="hph-ci", slots=("hph-ci-1-9", "hph-ci-2-9", "hph-ci-light-4-9"), mx=6):
        return {"host_actions": [{"do": "add-slot", "needs_person": False, "host": host, "max": mx,
                                  "slots": list(slots), "why": "backed up"}]}

    def test_starts_the_lowest_free_slot_on_this_box_once(self):
        steps = capacity_apply.plan(self.rep(), self.RX, "sudo -n restart-runner-slot {slot}", host="hph-ci", cancel=False)
        self.assertEqual([(k, a) for k, a, _ in steps], [("add-slot", ["sudo", "-n", "restart-runner-slot", "3"])])

    def test_other_box_no_host_or_at_ceiling_starts_nothing(self):
        for rep, host in ((self.rep(host="colima"), "hph-ci"), (self.rep(), ""), (self.rep(mx=3), "hph-ci")):
            self.assertEqual(capacity_apply.plan(rep, self.RX, "x {slot}", host=host, cancel=False), [])


class Apply409(unittest.TestCase):
    def run_main(self, results):
        import json
        import subprocess
        import tempfile
        from unittest import mock
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"repos": [{"repo": "o/a", "actions": [{"do": "cancel-stale", "runs": [9], "why": "stale"}]}]},
                      fh)
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd[-1])
            code, err = results.pop(0)
            return subprocess.CompletedProcess(cmd, code, "", err)
        with mock.patch.object(capacity_apply.subprocess, "run", fake), mock.patch("builtins.print"):
            rc = capacity_apply.main([fh.name, "--no-restart"])
        return rc, calls

    def test_a_run_github_never_queued_is_force_cancelled(self):
        rc, calls = self.run_main([(1, "Cannot cancel a workflow run that has not been queued yet. (HTTP 409)"),
                                   (0, "")])
        self.assertEqual((rc, calls), (0, ["repos/o/a/actions/runs/9/cancel", "repos/o/a/actions/runs/9/force-cancel"]))

    def test_a_refused_force_cancel_is_skipped_not_failed(self):
        rc, _ = self.run_main([(1, "(HTTP 409)"), (1, "(HTTP 409)")])
        self.assertEqual(rc, 0)

    def test_other_cancel_errors_still_fail(self):
        rc, calls = self.run_main([(1, "(HTTP 500)")])
        self.assertEqual((rc, len(calls)), (1, 1))


class PauseResume(unittest.TestCase):
    RX = r"^hph-ci-(?:light-)?([0-9]+)(?:-|$)"
    T = "sudo -n /usr/local/sbin/restart-runner-slot {slot}"
    S = "sudo -n /usr/local/sbin/restart-runner-slot --stop {slot}"

    def rep(self, do, host="hph-ci", runner="hph-ci-3-1795"):
        return {"state": "/var/lib/build-autopilot/capacity-state-o.json",
                "host_actions": [{"do": do, "needs_person": False, "host": host, "runner": runner, "why": "swapping"}]}

    def test_pause_stops_the_slot_and_records_it(self):
        (kind, argv, _, note), = capacity_apply.plan(self.rep("pause-slot"), self.RX, self.T, host="hph-ci",
                                                     cancel=False, stop_template=self.S)
        self.assertEqual((kind, argv), ("pause-slot", ["sudo", "-n", "/usr/local/sbin/restart-runner-slot", "--stop",
                                                       "3"]))
        self.assertEqual(note[2:], ["capacity-note", "--host", "hph-ci", "--paused", "hph-ci-3-1795",
                                    "--state", "/var/lib/build-autopilot/capacity-state-o.json"])

    def test_resume_starts_it_and_records_it(self):
        (kind, argv, _, note), = capacity_apply.plan(self.rep("resume-slot"), self.RX, self.T, host="hph-ci",
                                                     cancel=False)
        self.assertEqual(argv[-1], "3")
        self.assertNotIn("--stop", argv)
        self.assertIn("--resumed", note)

    def test_other_box_or_no_restart_command_does_nothing(self):
        self.assertEqual(capacity_apply.plan(self.rep("pause-slot", host="colima"), self.RX, self.T, host="hph-ci",
                                             cancel=False, stop_template=self.S), [])
        self.assertEqual(capacity_apply.plan(self.rep("pause-slot", runner="davids-mac-mini-dind-1"), self.RX, self.T,
                                             host="hph-ci", cancel=False, stop_template=self.S), [])

    def test_no_pause_without_an_explicit_stop_command(self):
        # A systemctl-style restart template must never be turned into a "stop" by text editing.
        self.assertEqual(capacity_apply.plan(self.rep("pause-slot"), self.RX, "systemctl restart hph-runner@{slot}",
                                             host="hph-ci", cancel=False), [])
        (k, argv, _, _), = capacity_apply.plan(self.rep("pause-slot"), self.RX, "systemctl restart hph-runner@{slot}",
                                               host="hph-ci", cancel=False,
                                               stop_template="systemctl stop hph-runner@{slot}")
        self.assertEqual(argv, ["systemctl", "stop", "hph-runner@3"])

    def test_one_box_change_per_pass_across_org_reports(self):
        seen = set()
        a = capacity_apply.plan(self.rep("pause-slot"), self.RX, self.T, seen=seen, host="hph-ci", cancel=False,
                                stop_template=self.S)
        b = capacity_apply.plan(self.rep("pause-slot", runner="hph-ci-5-1"), self.RX, self.T, seen=seen,
                                host="hph-ci", cancel=False, stop_template=self.S)
        c = capacity_apply.plan({"host_actions": [{"do": "add-slot", "host": "hph-ci", "max": 9, "slots": ["hph-ci-1-1"]}]},
                                self.RX, self.T, seen=seen, host="hph-ci", cancel=False)
        self.assertEqual((len(a), b, c), (1, [], []))

    def test_a_pause_that_is_not_recorded_counts_as_a_failure(self):
        import json
        import subprocess
        import tempfile
        from unittest import mock
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(self.rep("pause-slot"), fh)
        results = [(0, ""), (1, "locked")]

        def fake(cmd, **kw):
            code, err = results.pop(0)
            return subprocess.CompletedProcess(cmd, code, "", err)
        with mock.patch.object(capacity_apply.subprocess, "run", fake), mock.patch("builtins.print"):
            rc = capacity_apply.main([fh.name, "--no-cancel", "--slot-regex", self.RX, "--restart", self.T,
                                      "--host", "hph-ci", "--stop-cmd", self.S])
        self.assertEqual((rc, results), (1, []))


class RestartHelper(unittest.TestCase):
    def run_helper(self, *args):
        import os
        import subprocess
        import tempfile
        d = tempfile.mkdtemp()
        script = open(os.path.join(os.path.dirname(os.path.abspath(capacity_apply.__file__)),
                                   "restart-runner-slot.sh")).read().replace("/usr/bin/systemctl", "echo")
        path = os.path.join(d, "h.sh")
        open(path, "w").write(script)
        return subprocess.run(["bash", path] + list(args), capture_output=True, text=True)

    def test_stop_and_restart_one_numbered_slot_only(self):
        self.assertEqual(self.run_helper("4").stdout.strip(), "restart hph-runner@4.service")
        self.assertEqual(self.run_helper("--stop", "4").stdout.strip(), "stop hph-runner@4.service")
        for bad in (["--stop"], ["x"], ["--stop", "4", "5"], ["--start", "4"], ["4;reboot"]):
            self.assertEqual(self.run_helper(*bad).returncode, 2, bad)


class RepeatCancel(unittest.TestCase):
    """A run GitHub accepted a cancel for (202) but left queued is force-cancelled on the next pass."""

    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp()
        self.state = self.d + "/cancelled.json"
        self.report = self.d + "/cap.json"
        import json
        with open(self.report, "w") as fh:
            json.dump({"repos": [{"repo": "o/a", "actions": [{"do": "cancel-stale", "runs": [9], "why": "stale"}]}]}, fh)

    def run_pass(self, code=0, extra=()):
        import subprocess
        from unittest import mock
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd[-1])
            return subprocess.CompletedProcess(cmd, code, "", "" if code == 0 else "(HTTP 500)")
        with mock.patch.object(capacity_apply.subprocess, "run", fake), mock.patch("builtins.print"):
            rc = capacity_apply.main([self.report, "--no-restart", "--cancel-state", self.state] + list(extra))
        return rc, calls

    def test_first_pass_cancels_second_pass_forces(self):
        self.assertEqual(self.run_pass(), (0, ["repos/o/a/actions/runs/9/cancel"]))
        self.assertEqual(self.run_pass(), (0, ["repos/o/a/actions/runs/9/force-cancel"]))

    def test_a_failed_cancel_is_not_remembered(self):
        self.run_pass(code=1)
        self.assertEqual(self.run_pass()[1], ["repos/o/a/actions/runs/9/cancel"])

    def test_old_cancels_are_forgotten(self):
        import json
        import time
        with open(self.state, "w") as fh:
            json.dump({"o/a 9": time.time() - capacity_apply.REMEMBER_S - 60}, fh)
        self.assertEqual(self.run_pass()[1], ["repos/o/a/actions/runs/9/cancel"])

    def test_a_refused_force_cancel_is_skipped_not_failed(self):
        self.run_pass()
        self.assertEqual(self.run_pass(code=1), (0, ["repos/o/a/actions/runs/9/force-cancel"]))

    def test_unreadable_state_starts_empty_and_dry_run_writes_nothing(self):
        import os
        with open(self.state, "w") as fh:
            fh.write("not json")
        self.assertEqual(self.run_pass(extra=["--dry-run"])[1], [])
        with open(self.state) as fh:
            self.assertEqual(fh.read(), "not json")
        os.remove(self.state)
        self.assertEqual(self.run_pass()[1], ["repos/o/a/actions/runs/9/cancel"])


if __name__ == "__main__":
    unittest.main()
