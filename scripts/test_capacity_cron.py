"""capacity-cron.sh order: queue shaping runs once per org, with that org's token, before any slow check or sweep."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class CronOrder(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        (self.d / "bin").mkdir()
        (self.d / "conf").mkdir()
        shutil.copy(HERE / "capacity-cron.sh", self.d / "capacity-cron.sh")
        log = self.d / "calls.log"
        stub = self.d / "bin" / "python3"
        # Logs which script ran and with which token; reads nothing, writes nothing else.
        stub.write_text('#!/bin/bash\nprintf "%%s %%s\\n" "$(basename "${1:-stdin}")" "${GH_TOKEN:-}" >> "%s"\n' % log)
        stub.chmod(0o755)
        conf = self.d / "conf"
        (conf / "repos").write_text("OrgA/one\nOrgA/two\nOrgB/three\n")
        (conf / "OrgA.token").write_text("tokA")
        (conf / "OrgB.token").write_text("tokB")
        (conf / "apply.env").write_text('ADMIT_REPOS="OrgB/three"\n')
        self.log = log

    def calls(self):
        env = dict(os.environ, PATH="%s:%s" % (self.d / "bin", os.environ["PATH"]),
                   CAPACITY_CONF=str(self.d / "conf"), CAPACITY_STATE=str(self.d / "state"))
        subprocess.run(["bash", str(self.d / "capacity-cron.sh")], env=env, check=True, capture_output=True)
        return [line.split() for line in self.log.read_text().splitlines()]

    def test_queue_shaping_runs_first_once_per_org_with_its_own_token(self):
        calls = self.calls()
        names = [c[0] for c in calls]
        shaping = [c for c in calls if c[0] in ("priority_apply.py", "admit_apply.py")]
        self.assertEqual(shaping, [["priority_apply.py", "tokA"], ["priority_apply.py", "tokB"],
                                   ["admit_apply.py", "tokB"]])
        last_shape = max(i for i, n in enumerate(names) if n in ("priority_apply.py", "admit_apply.py"))
        slow = [i for i, n in enumerate(names) if n in ("steward.py", "capacity_apply.py", "sweep_apply.py")]
        self.assertTrue(slow and min(slow) > last_shape)  # every slow check and the sweep come after
        self.assertEqual(names.count("steward.py"), 4)  # one capacity check and one sweep per org, no more

    def test_an_org_with_no_token_is_skipped_everywhere(self):
        (self.d / "conf" / "OrgB.token").unlink()
        calls = self.calls()
        self.assertFalse([c for c in calls if c[1:] != ["tokA"]])


if __name__ == "__main__":
    unittest.main()
