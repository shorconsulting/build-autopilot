"""The Mac runner load governor, against stub docker, colima, sysctl and gh commands that read a fake world from
files. Each test sets the machine's readings and the runners' processes and logs, runs passes, and checks what was
stopped, started or relabelled."""
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GOVERNOR = os.path.join(HERE, "mac-runner-governor.sh")
WATCHDOG = os.path.join(HERE, "mac-runner-watchdog.sh")

DOCKER = r'''#!/bin/bash
W="$WORLD"
echo "docker $*" >> "$W/calls"
case "$1" in
  info) exit 0 ;;
  ps) cat "$W/ps" ;;
  top) f="$W/top.$2"; [ -f "$f" ] || exit 1; cat "$f" ;;
  inspect) echo "2026-11-20T20:00:00Z" ;;
  logs) cat "$W/logs.${@: -1}" 2>/dev/null ;;
  stop) sed -i.bak "s/^$2|running$/$2|exited/" "$W/ps" ;;
  start|restart) sed -i.bak "s/^$2|exited$/$2|running/" "$W/ps" ;;
esac
'''
COLIMA = r'''#!/bin/bash
echo "colima $*" >> "$WORLD/calls"
[ "$1" = ssh ] && { [ -f "$WORLD/vm" ] || exit 1; cat "$WORLD/vm"; }
exit 0
'''
SYSCTL = r'''#!/bin/bash
[ -f "$WORLD/mac" ] || exit 1
. "$WORLD/mac"
case "$2" in
  hw.ncpu) echo "$NCPU" ;;
  vm.loadavg) echo "{ $LOAD 1.00 1.00 }" ;;
  kern.memorystatus_vm_pressure_level) echo "$LEVEL" ;;
  vm.swapusage) echo "total = 4096.00M  used = ${SWAP}.00M  free = 100.00M  (encrypted)" ;;
esac
'''
GH = r'''#!/bin/bash
echo "gh $*" >> "$WORLD/calls"
case "$*" in
  *"actions/runners --paginate"*) echo 42 ;;
  "api orgs/SConsInc/actions/runners/42/labels --jq"*) echo shorconsulting-ci ;;
esac
exit 0
'''

BASE = 22 * 3600  # the runners' log clock starts at 22:00:00Z in these tests


def hms(t):
    t += BASE
    return "%02d:%02d:%02dZ" % (t // 3600 % 24, t // 60 % 60, t % 60)


def jobs(*specs, running=None, at=0):
    """Runner log lines for finished jobs (name, seconds), then optionally one still running."""
    out, t = [], at
    for name, secs in specs:
        out.append("2026-11-20 %s: Running job: %s" % (hms(t), name))
        t += secs
        out.append("2026-11-20 %s: Job %s completed with result: Succeeded" % (hms(t), name))
        t += 5
    if running:
        out.append("2026-11-20 %s: Running job: %s" % (hms(t), running))
    return "\n".join(out) + "\n"


def vm(cpus=8, load=2.0, avail_pct=50, psi_some60=0.0, psi_full10=0.0):
    total = 16000000
    return ("%d\n%.2f 1.00 1.00 3/400 999\nMemTotal: %d kB\nMemAvailable: %d kB\nPSI_MEM\n"
            "some avg10=0.00 avg60=%.2f avg300=0.00 total=1\nfull avg10=%.2f avg60=0.00 avg300=0.00 total=1\n"
            % (cpus, load, total, total * avail_pct // 100, psi_some60, psi_full10))


IDLE = "Runner.Listener run"
BUSY = "Runner.Listener run\nRunner.Worker spawnclient"
R = "davids-mac-mini-dind-sconsinc-"


class Governor(unittest.TestCase):
    def setUp(self):
        self.w = tempfile.mkdtemp()
        self.bin = os.path.join(self.w, "bin")
        os.mkdir(self.bin)
        for name, body in (("docker", DOCKER), ("colima", COLIMA), ("sysctl", SYSCTL), ("gh", GH)):
            path = os.path.join(self.bin, name)
            with open(path, "w") as fh:
                fh.write(body)
            os.chmod(path, 0o755)
        self.state = os.path.join(self.w, "st")
        self.mac(load=2.0)
        self.env = {"GOV_COOLDOWN_S": "0", "GOV_WATCH_S": "0", "GOV_POLL_S": "0", "GOV_NOW_SOD": str(BASE + 3600)}

    # --- the fake world ---
    def runners(self, spec):
        """spec: {suffix: (state, processes or None, logs or '')}"""
        with open(os.path.join(self.w, "ps"), "w") as fh:
            for n, (st, _, _) in sorted(spec.items()):
                fh.write("%s%s|%s\n" % (R, n, st))
        for n, (_, procs, logs) in spec.items():
            self.top(n, procs)
            with open(os.path.join(self.w, "logs.%s%s" % (R, n)), "w") as fh:
                fh.write(logs or "")

    def top(self, n, procs):
        path = os.path.join(self.w, "top.%s%s" % (R, n))
        if procs is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w") as fh:
            fh.write("UID PID PPID C STIME TTY TIME CMD\n"
                     + "".join("1001 %d 1 0 17:30 ? 00:00:00 %s\n" % (100 + i, c)
                               for i, c in enumerate(procs.splitlines())))

    def logs(self, n, text):
        with open(os.path.join(self.w, "logs.%s%s" % (R, n)), "w") as fh:
            fh.write(text)

    def vm(self, text):
        with open(os.path.join(self.w, "vm"), "w") as fh:
            fh.write(text)

    def mac(self, ncpu=8, load=2.0, level=1, swap=500):
        with open(os.path.join(self.w, "mac"), "w") as fh:
            fh.write("NCPU=%d\nLOAD=%.2f\nLEVEL=%d\nSWAP=%d\n" % (ncpu, load, level, swap))

    def go(self, *args, script=GOVERNOR, **env):
        e = dict(os.environ, WORLD=self.w, WATCHDOG_TEST_BIN=self.bin, WATCHDOG_STATE=self.state,
                 WATCHDOG_LOG=os.path.join(self.w, "log"), **self.env)
        e.update(env)
        out = subprocess.run(["bash", script] + list(args), env=e, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr + out.stdout)
        return out.stdout

    def calls(self, verb):
        try:
            with open(os.path.join(self.w, "calls")) as fh:
                return [l.split()[2].replace(R, "") for l in fh if l.startswith("docker %s " % verb)]
        except OSError:
            return []

    def gh_calls(self):
        try:
            with open(os.path.join(self.w, "calls")) as fh:
                return [l.strip() for l in fh if l.startswith("gh ")]
        except OSError:
            return []

    def paused(self):
        d = os.path.join(self.state, "governor", "paused")
        return sorted(f.replace(R, "") for f in os.listdir(d) if not f.endswith(".labels")) if os.path.isdir(d) else []

    def draining(self):
        d = os.path.join(self.state, "governor", "draining")
        return sorted(f.replace(R, "") for f in os.listdir(d) if not f.endswith(".labels")) if os.path.isdir(d) else []

    def four_busy_one_idle(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", BUSY, jobs(running="build", at=600)),
                      "3": ("running", BUSY, jobs(running="lint", at=1200)),
                      "4": ("running", IDLE, jobs(("build", 300))),
                      "5": ("running", BUSY, jobs(running="build", at=1800))})

    # --- overload ---
    def test_memory_stall_two_passes_in_a_row_stops_an_idle_runner(self):
        self.four_busy_one_idle()
        self.vm(vm(psi_some60=15))
        self.go()
        self.assertEqual(self.calls("stop"), [], "one red pass is not enough")
        self.go()
        self.assertEqual(self.calls("stop"), ["4"])
        self.assertEqual(self.paused(), ["4"])
        with open(os.path.join(self.w, "log")) as fh:
            self.assertIn("VM memory stall 15.00%", fh.read())

    def test_a_red_pass_then_a_calm_one_changes_nothing(self):
        self.four_busy_one_idle()
        self.vm(vm(psi_some60=15))
        self.go()
        self.vm(vm(psi_some60=4))
        self.go()
        self.vm(vm(psi_some60=15))
        self.go()
        self.assertEqual(self.calls("stop"), [])

    def test_severe_memory_shortage_acts_on_the_first_pass(self):
        self.four_busy_one_idle()
        self.vm(vm(avail_pct=3))
        self.go()
        self.assertEqual(self.calls("stop"), ["4"])

    def test_mac_critical_memory_pressure_or_heavy_swapping_is_overload(self):
        self.four_busy_one_idle()
        self.vm(vm())
        self.mac(level=4)
        self.go()
        self.assertEqual(self.calls("stop"), ["4"])

    def test_mac_swap_growing_fast_is_overload(self):
        self.four_busy_one_idle()
        self.vm(vm())
        self.mac(swap=500)
        self.go()
        self.mac(swap=2000)
        self.go()
        self.mac(swap=3500)
        self.go()
        self.assertEqual(self.calls("stop"), ["4"])

    def test_with_every_runner_busy_it_drains_the_longest_running_and_stops_it_when_its_job_ends(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", BUSY, jobs(running="build", at=600)),
                      "3": ("running", BUSY, jobs(running="lint", at=1200))})
        self.vm(vm(load=24))  # 3 per core
        self.go()
        self.go()
        self.assertEqual(self.calls("stop"), [], "a running job is never stopped")
        self.assertEqual(self.draining(), ["1"])
        self.go()
        self.assertEqual(self.draining(), ["1"], "one change at a time: still draining the same one")
        # Its job ends: the next look stops it before it can take another.
        self.logs("1", jobs(("build", 3000)))
        self.top("1", IDLE)
        self.go()
        self.assertEqual(self.calls("stop"), ["1"])
        self.assertEqual(self.paused(), ["1"])
        self.assertEqual(self.draining(), [])

    def test_a_runner_that_just_took_a_job_is_never_stopped(self):
        # Listener only, but the last line it logged is a job it took: the job process is about to start.
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", IDLE, jobs(("build", 60), running="build", at=600)),
                      "3": ("running", BUSY, jobs(running="lint", at=1200))})
        self.vm(vm(avail_pct=2))
        self.go()
        self.assertNotIn("2", self.calls("stop"))
        self.assertEqual(self.draining(), ["1"])

    def test_a_draining_runner_that_takes_a_new_job_is_not_stopped(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", BUSY, jobs(running="build", at=600)),
                      "3": ("running", BUSY, jobs(running="lint", at=1200))})
        self.vm(vm(avail_pct=2))
        self.go()
        self.assertEqual(self.draining(), ["1"])
        self.logs("1", jobs(("build", 3000), running="migrate", at=0))
        self.top("1", IDLE)  # took the next job, job process not up yet
        self.go(GOV_COOLDOWN_S="9999")
        self.assertEqual(self.calls("stop"), [])

    def test_never_below_the_minimum_per_runner_group(self):
        self.runners({"1": ("running", IDLE, ""), "2": ("running", IDLE, "")})
        self.vm(vm(avail_pct=2))
        for _ in range(3):
            self.go()
        self.assertEqual(self.calls("stop"), [])
        with open(os.path.join(self.w, "log")) as fh:
            self.assertIn("at its minimum of 2", fh.read())

    def test_one_change_per_cooldown(self):
        self.four_busy_one_idle()
        self.vm(vm(avail_pct=2))
        self.go(GOV_COOLDOWN_S="3600")
        self.go(GOV_COOLDOWN_S="3600")
        self.go(GOV_COOLDOWN_S="3600")
        self.assertEqual(self.calls("stop"), ["4"])
        self.assertEqual(self.draining(), [])

    def test_an_unreadable_machine_is_never_read_as_calm(self):
        self.runners({"1": ("running", IDLE, ""), "2": ("running", IDLE, ""), "3": ("running", "Runner.Listener run", "")})
        os.makedirs(os.path.join(self.state, "governor", "paused"))
        with open(os.path.join(self.state, "governor", "paused", R + "3"), "w") as fh:
            fh.write("1\n")
        with open(os.path.join(self.w, "ps"), "a") as fh:
            pass
        self.vm(vm(load=1))
        os.remove(os.path.join(self.w, "vm"))
        for _ in range(4):
            self.go()
        self.assertEqual(self.calls("start"), [])

    # --- slow jobs ---
    def learned(self):
        """Runner 9 has finished 'build' three times in about two minutes each."""
        return jobs(("build", 120), ("build", 130), ("build", 110))

    def test_a_job_running_far_past_its_usual_time_with_others_on_the_box_is_overload(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),       # 60 minutes, usual 2
                      "2": ("running", BUSY, jobs(running="lint", at=3000)),
                      "3": ("running", IDLE, ""),
                      "9": ("running", IDLE, self.learned())})
        self.vm(vm(load=4))  # CPU and memory look fine: only the job times say the box is struggling
        self.go()
        self.go()
        self.assertEqual(len(self.calls("stop")), 1)
        with open(os.path.join(self.w, "log")) as fh:
            self.assertIn("slow: %s1 (build, 3600s)" % R, fh.read())

    def test_a_slow_job_alone_on_the_box_is_not_overload(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "3": ("running", IDLE, ""),
                      "9": ("running", IDLE, self.learned())})
        self.vm(vm(load=4))
        self.go()
        self.go()
        self.assertEqual(self.calls("stop"), [])

    def test_a_job_with_no_learned_time_is_never_slow(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", BUSY, jobs(running="lint", at=3000)),
                      "3": ("running", IDLE, "")})
        self.vm(vm(load=4))
        self.go()
        self.go()
        self.assertEqual(self.calls("stop"), [])

    def test_status_lists_the_learned_job_times(self):
        self.runners({"9": ("running", IDLE, self.learned())})
        self.vm(vm())
        self.go()
        out = self.go("--status")
        self.assertIn("%ssconsinc|build 120 3" % R[:-len("sconsinc-")], out)

    # --- recovery ---
    def test_calm_three_passes_in_a_row_starts_a_paused_runner_and_the_watchdog_leaves_it_alone_until_then(self):
        self.four_busy_one_idle()
        self.vm(vm(avail_pct=2))
        self.go()
        self.assertEqual(self.paused(), ["4"])
        self.vm(vm(load=2, avail_pct=60))
        self.mac(load=1)
        # The watchdog runs the governor itself; it must not start the paused runner as "exited".
        self.go(script=WATCHDOG)
        self.go(script=WATCHDOG)
        self.assertEqual(self.calls("start"), [])
        self.go(script=WATCHDOG)
        self.assertEqual(self.calls("start"), ["4"])
        self.assertEqual(self.paused(), [])

    def test_calm_cancels_a_drain_before_starting_anything(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", BUSY, jobs(running="build", at=600)),
                      "3": ("running", BUSY, jobs(running="lint", at=1200))})
        self.vm(vm(avail_pct=2))
        self.go()
        self.assertEqual(self.draining(), ["1"])
        self.vm(vm(load=2, avail_pct=60))
        self.mac(load=1)
        for _ in range(3):
            self.go()
        self.assertEqual(self.draining(), [])
        self.assertEqual(self.calls("stop"), [])

    def test_a_runner_started_by_hand_is_no_longer_paused(self):
        self.four_busy_one_idle()
        self.vm(vm(avail_pct=2))
        self.go()
        self.assertEqual(self.paused(), ["4"])
        subprocess.run(["sed", "-i.bak", "s/%s4|exited/%s4|running/" % (R, R), os.path.join(self.w, "ps")])
        self.vm(vm(psi_some60=4))
        self.go()
        self.assertEqual(self.paused(), [])

    def test_resume_all_starts_every_paused_runner_and_ends_every_drain(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", BUSY, jobs(running="build", at=600)),
                      "3": ("running", IDLE, ""),
                      "4": ("running", IDLE, "")})
        self.vm(vm(avail_pct=2))
        self.go()
        self.go()
        self.assertEqual(len(self.paused()) + len(self.draining()), 2)
        self.go("--resume-all")
        self.assertEqual(self.paused(), [])
        self.assertEqual(self.draining(), [])

    # --- GitHub labels ---
    def test_with_github_orgs_a_draining_runner_loses_its_labels_and_gets_them_back(self):
        self.runners({"1": ("running", BUSY, jobs(running="build", at=0)),
                      "2": ("running", BUSY, jobs(running="build", at=600)),
                      "3": ("running", BUSY, jobs(running="lint", at=1200))})
        self.vm(vm(avail_pct=2))
        self.go(GOV_GH_ORGS="sconsinc:SConsInc")
        self.assertIn("gh api -X DELETE orgs/SConsInc/actions/runners/42/labels", self.gh_calls())
        self.logs("1", jobs(("build", 3000)))
        self.top("1", IDLE)
        self.go(GOV_GH_ORGS="sconsinc:SConsInc", GOV_COOLDOWN_S="9999")
        self.assertEqual(self.paused(), ["1"])
        self.vm(vm(load=2, avail_pct=60))
        self.mac(load=1)
        for _ in range(3):
            self.go(GOV_GH_ORGS="sconsinc:SConsInc")
        self.assertEqual(self.calls("start"), ["1"])
        self.assertIn("gh api -X POST orgs/SConsInc/actions/runners/42/labels -f labels[]=shorconsulting-ci",
                      self.gh_calls())

    def test_without_github_orgs_github_is_never_called(self):
        self.four_busy_one_idle()
        self.vm(vm(avail_pct=2))
        self.go()
        self.assertEqual(self.gh_calls(), [])

    def test_dry_run_changes_nothing(self):
        self.four_busy_one_idle()
        self.vm(vm(avail_pct=2))
        out = self.go("--dry-run")
        self.assertIn("would stop %s4" % R, out)
        self.assertEqual(self.calls("stop"), [])
        self.assertEqual(self.paused(), [])

    def test_watchdog_can_turn_the_governor_off(self):
        self.four_busy_one_idle()
        self.vm(vm(avail_pct=2))
        self.go(script=WATCHDOG, WATCHDOG_GOVERNOR="0")
        self.assertEqual(self.calls("stop"), [])


if __name__ == "__main__":
    unittest.main()
