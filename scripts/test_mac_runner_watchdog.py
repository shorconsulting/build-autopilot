"""The Mac runner watchdog, against stub docker and colima commands that read a fake world from files."""
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "mac-runner-watchdog.sh")

DOCKER = r'''#!/bin/bash
W="$WORLD"
echo "docker $*" >> "$W/calls"
case "$1" in
  info) [ -f "$W/up" ] ;;
  ps) cat "$W/ps" ;;
  top)
    # Like the real daemon: ps options that drop the PID column are refused.
    if [ $# -gt 2 ]; then echo "Error response from daemon: Couldn't find PID field in ps output" >&2; exit 1; fi
    n=$(( $(cat "$W/topn.$2" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$W/topn.$2"
    f="$W/top.$2"; [ -f "$f.$n" ] && f="$f.$n"
    [ -f "$f" ] || { echo "Error response from daemon: No such container: $2" >&2; exit 1; }
    cat "$f" ;;
  inspect) echo "2026-11-20T20:00:00Z" ;;
  logs) cat "$W/logs.${@: -1}" 2>/dev/null ;;
  start|restart) exit 0 ;;
esac
'''
COLIMA = '#!/bin/bash\necho "colima $*" >> "$WORLD/calls"; touch "$WORLD/up"\n'


def jobs(*specs):
    """Runner log lines for jobs given as (seconds, result)."""
    out, t = [], 0
    for secs, result in specs:
        out.append("2026-11-20 %s: Running job: j" % hms(t))
        t += secs
        out.append("2026-11-20 %s: Job j completed with result: %s" % (hms(t), result))
        t += 5
    return "\n".join(out) + "\n"


def hms(t):
    t += 22 * 3600
    return "%02d:%02d:%02dZ" % (t // 3600, t // 60 % 60, t % 60)


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.w = tempfile.mkdtemp()
        self.bin = os.path.join(self.w, "bin")
        os.mkdir(self.bin)
        for name, body in (("docker", DOCKER), ("colima", COLIMA)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        open(os.path.join(self.w, "up"), "w").close()

    def world(self, ps, top=None, logs=None):
        with open(os.path.join(self.w, "ps"), "w") as fh:
            fh.write("".join("%s|%s\n" % kv for kv in ps))
        for name, procs in (top or {}).items():
            with open(os.path.join(self.w, "top." + name), "w") as fh:
                fh.write("UID PID PPID C STIME TTY TIME CMD\n"
                         + "".join("1001 %d 1 0 17:30 ? 00:00:00 %s\n" % (100 + i, c)
                                   for i, c in enumerate(procs.splitlines())))
        for name, text in (logs or {}).items():
            with open(os.path.join(self.w, "logs." + name), "w") as fh:
                fh.write(text)

    def run_pass(self, *args):
        env = dict(os.environ, WORLD=self.w, WATCHDOG_TEST_BIN=self.bin, WATCHDOG_STATE=os.path.join(self.w, "st"),
                   WATCHDOG_LOG=os.path.join(self.w, "log"))
        out = subprocess.run(["bash", SCRIPT] + list(args), env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        return out.stdout

    def calls(self, verb):
        try:
            with open(os.path.join(self.w, "calls")) as fh:
                return [l.split()[2] for l in fh if l.startswith("docker %s " % verb)]
        except OSError:
            return []

    def test_starts_an_exited_runner_and_ignores_other_containers(self):
        self.world([("davids-mac-mini-dind-sconsinc-4", "exited"), ("postgres", "exited"), ("dind-helper-runner", "exited")])
        self.run_pass()
        self.assertEqual(self.calls("start"), ["davids-mac-mini-dind-sconsinc-4"])

    def test_starts_colima_when_docker_is_down(self):
        os.remove(os.path.join(self.w, "up"))
        self.world([("davids-mac-mini-dind-sconsinc-1", "running")], top={"davids-mac-mini-dind-sconsinc-1": "Runner.Listener run"})
        self.run_pass()
        with open(os.path.join(self.w, "calls")) as fh:
            self.assertIn("colima start --profile default\n", fh.read())

    def test_restarts_a_runner_with_no_process_only_on_the_second_pass(self):
        self.world([("davids-mac-mini-dind-sconsinc-2", "running")], top={"davids-mac-mini-dind-sconsinc-2": "/bin/sh entrypoint"})
        self.run_pass()
        self.assertEqual(self.calls("restart"), [])
        self.run_pass()
        self.assertEqual(self.calls("restart"), ["davids-mac-mini-dind-sconsinc-2"])

    def test_a_busy_runner_is_never_touched(self):
        self.world([("davids-mac-mini-dind-sconsinc-3", "running")],
                   top={"davids-mac-mini-dind-sconsinc-3": "Runner.Listener run\nRunner.Worker spawnclient"},
                   logs={"davids-mac-mini-dind-sconsinc-3": jobs((5, "Failed"), (5, "Failed"), (5, "Failed"))})
        self.run_pass()
        self.run_pass()
        self.assertEqual(self.calls("restart") + self.calls("start"), [])

    def test_restarts_a_runner_whose_jobs_all_fail_fast(self):
        self.world([("davids-mac-mini-linux-hporthealth-2", "running")], top={"davids-mac-mini-linux-hporthealth-2": "Runner.Listener run"},
                   logs={"davids-mac-mini-linux-hporthealth-2": jobs((300, "Succeeded"), (8, "Failed"), (6, "Failed"), (9, "Failed"))})
        self.run_pass()
        self.assertEqual(self.calls("restart"), ["davids-mac-mini-linux-hporthealth-2"])

    def test_leaves_a_runner_that_picked_up_real_test_failures(self):
        self.world([("davids-mac-mini-linux-hporthealth-1", "running")], top={"davids-mac-mini-linux-hporthealth-1": "Runner.Listener run"},
                   logs={"davids-mac-mini-linux-hporthealth-1": jobs((400, "Failed"), (8, "Failed"), (500, "Failed"))})
        self.run_pass()
        self.assertEqual(self.calls("restart"), [])

    def test_a_runner_whose_processes_cannot_be_read_is_left_alone(self):
        self.world([("davids-mac-mini-dind-sconsinc-1", "running")])  # docker top fails for it
        for _ in range(3):
            self.run_pass()
        self.assertEqual(self.calls("restart"), [])
        with open(os.path.join(self.w, "log")) as fh:
            self.assertIn("could not list its processes", fh.read())

    def test_status_reads_the_real_docker_top_format(self):
        self.world([("davids-mac-mini-dind-sconsinc-1", "running")],
                   top={"davids-mac-mini-dind-sconsinc-1": "runuser -u runner -- ./run.sh\n/home/runner/bin/Runner.Listener run"})
        out = self.run_pass("--status")
        self.assertIn("davids-mac-mini-dind-sconsinc-1 running Runner.Listener", out)

    def test_never_restarts_a_fast_failing_runner_that_just_took_a_job(self):
        name = "davids-mac-mini-linux-hporthealth-2"
        self.world([(name, "running")], top={name: "Runner.Listener run"},
                   logs={name: jobs((8, "Failed"), (6, "Failed"), (9, "Failed"))})
        with open(os.path.join(self.w, "top.%s.2" % name), "w") as fh:
            fh.write("UID PID PPID C STIME TTY TIME CMD\n1001 1 0 0 17:30 ? 00:00:00 Runner.Listener run\n"
                     "1001 2 1 0 17:30 ? 00:00:00 Runner.Worker spawnclient\n")
        self.run_pass()
        self.assertEqual(self.calls("restart"), [])

    def test_gives_up_after_the_hourly_cap(self):
        self.world([("davids-mac-mini-dind-sconsinc-5", "exited")])
        for _ in range(5):
            self.run_pass()
        self.assertEqual(len(self.calls("start")), 3)
        with open(os.path.join(self.w, "log")) as fh:
            self.assertIn("leaving it for a person", fh.read())

    def test_dry_run_changes_nothing(self):
        self.world([("davids-mac-mini-dind-sconsinc-4", "exited")])
        out = self.run_pass("--dry-run")
        self.assertIn("would davids-mac-mini-dind-sconsinc-4", out)
        self.assertEqual(self.calls("start"), [])


if __name__ == "__main__":
    unittest.main()
