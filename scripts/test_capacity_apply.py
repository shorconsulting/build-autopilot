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


if __name__ == "__main__":
    unittest.main()
