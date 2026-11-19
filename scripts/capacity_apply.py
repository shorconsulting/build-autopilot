#!/usr/bin/env python3
"""Act on a `steward.py capacity --out` report without an agent, on an always-on runner box (cron / systemd timer).

Self-healing (build rules, rule 3): cancel the stale queued runs it lists and restart this box's own runner services
it names. It never deletes a registration, never restarts a VM or reboots the box, and touches only runners whose
names match --slot-regex (runners of other boxes are left to the steward on their own box). Every action is printed
as one line, so the timer's journal is the after-the-fact record.

  python3 capacity_apply.py capacity.json --slot-regex '^hph-ci-(?:light-)?(\\d+)(?:-|$)' \\
      --restart 'systemctl restart hph-runner@{slot}'
"""
import argparse
import json
import re
import shlex
import subprocess
import sys

RESTART_ACTIONS = ("restart-runner", "restore-pool")


def plan(report, slot_regex, restart_template, max_cancels=100):
    """[(kind, argv, why)] for this box: cancels for every repo's cancel-stale, restarts for this box's slots."""
    out = []
    for rep in report.get("repos") or []:
        for act in rep.get("actions") or []:
            if act.get("do") != "cancel-stale":
                continue
            for run in (act.get("runs") or [])[:max_cancels]:
                out.append(("cancel", ["gh", "api", "-X", "POST",
                                       "repos/%s/actions/runs/%d/cancel" % (rep["repo"], int(run))],
                            "%s run %s: %s" % (rep["repo"], run, act.get("why", "stale"))))
    rx = re.compile(slot_regex) if slot_regex else None
    seen = set()
    for owner in report.get("owners") or []:
        for act in owner.get("actions") or []:
            if act.get("do") not in RESTART_ACTIONS or act.get("needs_person") or not rx or not restart_template:
                continue
            names = [n.strip() for n in str(act.get("runner") or "").split(",") if n.strip()]
            for name in names:
                m = rx.search(name)
                if not m:
                    continue
                slot = m.group(1)
                if slot in seen:
                    continue
                seen.add(slot)
                argv = shlex.split(restart_template.format(slot=slot))
                out.append(("restart", argv, "%s (%s): %s" % (name, owner.get("owner"), act.get("why", ""))))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("report")
    ap.add_argument("--slot-regex", default="", help="regex whose group 1 is this box's slot number")
    ap.add_argument("--restart", default="", help="command template to restart a slot, with {slot}")
    ap.add_argument("--max-cancels", type=int, default=100)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    with open(a.report, encoding="utf-8") as fh:
        report = json.load(fh)
    failed = 0
    for kind, cmd, why in plan(report, a.slot_regex, a.restart, a.max_cancels):
        if a.dry_run:
            print("would %s: %s" % (kind, why))
            continue
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        ok = res.returncode == 0
        failed += 0 if ok else 1
        print("%s %s: %s%s" % (kind, "ok" if ok else "FAILED", why,
                               "" if ok else " (%s)" % (res.stderr or res.stdout).strip()[:200]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
