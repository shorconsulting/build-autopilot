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


if __name__ == "__main__":
    unittest.main()
