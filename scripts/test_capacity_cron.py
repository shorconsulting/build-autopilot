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


class SelfUpdate(unittest.TestCase):
    """The pass moves its checkout forward to origin/main, then runs the new code."""

    def git(self, cwd, *a):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                       capture_output=True)

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)
        origin, self.box = self.d / "origin", self.d / "box"
        (origin / "scripts").mkdir(parents=True)
        shutil.copy(HERE / "capacity-cron.sh", origin / "scripts" / "capacity-cron.sh")
        self.git(origin, "init", "-q", "-b", "main")
        self.git(origin, "add", ".")
        self.git(origin, "commit", "-q", "-m", "one")
        self.git(self.d, "clone", "-q", str(origin), str(self.box))
        # origin moves on: the new version marks that it ran.
        cron = origin / "scripts" / "capacity-cron.sh"
        cron.write_text(cron.read_text().replace("set -euo pipefail\n", "set -euo pipefail\necho NEW-CODE-RAN\n", 1))
        self.git(origin, "commit", "-q", "-am", "two")
        self.new = subprocess.run(["git", "rev-parse", "HEAD"], cwd=origin, capture_output=True, text=True).stdout.strip()
        conf = self.d / "conf"
        conf.mkdir()
        (conf / "repos").write_text("")
        self.env = dict(os.environ, CAPACITY_CONF=str(conf), CAPACITY_STATE=str(self.d / "state"))

    def pass_(self, **env):
        return subprocess.run(["bash", str(self.box / "scripts" / "capacity-cron.sh")], env=dict(self.env, **env),
                              capture_output=True, text=True, check=True).stdout

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.box, capture_output=True, text=True).stdout.strip()

    def test_a_pass_updates_to_main_and_runs_the_new_code(self):
        out = self.pass_()
        self.assertIn("self-update:", out)
        self.assertIn("NEW-CODE-RAN", out)
        self.assertEqual(self.head(), self.new)
        self.assertNotIn("self-update:", self.pass_())  # up to date: nothing to say

    def test_off_with_auto_update_0(self):
        (self.d / "conf" / "apply.env").write_text("AUTO_UPDATE=0\n")
        out = self.pass_()
        self.assertNotIn("NEW-CODE-RAN", out)
        self.assertNotEqual(self.head(), self.new)

    def test_a_local_commit_is_never_overwritten(self):
        self.git(self.box, "commit", "-q", "--allow-empty", "-m", "local")
        mine = self.head()
        out = self.pass_()
        self.assertIn("not behind origin/main", out)
        self.assertEqual(self.head(), mine)

    def test_a_main_that_does_not_parse_is_rolled_back(self):
        origin = self.d / "origin"
        cron = origin / "scripts" / "capacity-cron.sh"
        cron.write_text(cron.read_text() + "\nif then\n")
        self.git(origin, "commit", "-q", "-am", "broken")
        before = self.head()
        out = self.pass_()
        self.assertIn("does not parse", out)
        self.assertEqual(self.head(), before)

    def test_a_local_edit_or_unreachable_origin_keeps_the_old_code(self):
        cron = self.box / "scripts" / "capacity-cron.sh"
        cron.write_text(cron.read_text().replace("set -euo pipefail\n", "set -euo pipefail\necho LOCAL-EDIT\n", 1))
        out = self.pass_()
        self.assertIn("could not check out", out)
        self.assertIn("LOCAL-EDIT", out)
        self.git(self.box, "checkout", "-q", "--", ".")
        self.git(self.box, "remote", "set-url", "origin", str(self.d / "nowhere"))
        self.assertIn("could not fetch", self.pass_())

    def test_a_dry_run_never_updates(self):
        before = self.head()
        (self.d / "conf" / "apply.env").write_text("DRY_RUN=1\n")
        self.pass_()
        self.assertEqual(self.head(), before)


if __name__ == "__main__":
    unittest.main()
