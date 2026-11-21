#!/usr/bin/env python3
"""Act on a `steward.py capacity --out` report without an agent, on an always-on runner box (cron / systemd timer).

Self-healing (build rules, rule 3): cancel the stale queued runs it lists and restart this box's own runner services
it names, and start one more slot when the report says `add-slot` for this box (--host), at the lowest slot number
no runner of this box uses. When the report says `pause-slot` for this box (it is swapping), stop that one idle
slot, and on `resume-slot` (no longer swapping, jobs backed up) start it again; each is recorded with
`steward.py capacity-note` in the report's state file, so pool checks never restart a paused slot. It never deletes a registration, never restarts a VM or reboots the box, and touches only runners whose
names match --slot-regex (runners of other boxes are left to the steward on their own box). Every action is printed
as one line, so the timer's journal is the after-the-fact record.

  python3 capacity_apply.py capacity.json --slot-regex '^hph-ci-(?:light-)?(\\d+)(?:-|$)' \\
      --restart 'systemctl restart hph-runner@{slot}'
"""
import argparse
import json
import re
import shlex
import os
import subprocess
import sys
import time

# A run this box cancelled in the last REMEMBER_S seconds that a later pass still lists as queued: GitHub accepted
# the cancel (202) but never acted on it, which it does for runs stuck before a runner was assigned. Asking again
# changes nothing (HPortHealth/app, 2026-11-20: the same three runs "cancel ok" every 10 minutes for hours), so the
# second time it is force-cancelled.
REMEMBER_S = 6 * 3600


def load_cancelled(path):
    """{"<repo> <run>": epoch seconds of our last cancel}, recent ones only. Missing or unreadable: empty."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - REMEMBER_S
    return {k: v for k, v in data.items() if isinstance(v, (int, float)) and v >= cutoff} if isinstance(data, dict) else {}


def save_cancelled(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def force(cmd):
    return [c.replace("/cancel", "/force-cancel") if c.endswith("/cancel") else c for c in cmd]

RESTART_ACTIONS = ("restart-runner", "restore-pool")
HERE = os.path.dirname(os.path.abspath(__file__))


def plan(report, slot_regex, restart_template, max_cancels=100, seen=None, cancel=True, host="", stop_template=""):
    """[(kind, argv, why)] for this box: cancels for every repo's cancel-stale, restarts for this box's slots.
    `seen`: slots already planned this pass (shared across reports, so a slot restarts at most once per pass).
    Slots are one numbered service per box: on hph-ci, hph-ci-10 and hph-ci-light-10 are both hph-runner@10."""
    out = []
    for rep in (report.get("repos") or []) if cancel else []:
        left = max_cancels  # per repo, across all of its cancel-stale actions
        for act in rep.get("actions") or []:
            if act.get("do") != "cancel-stale" or act.get("needs_person"):
                continue
            runs = (act.get("runs") or [])[:max(left, 0)]
            left -= len(runs)
            for run in runs:
                out.append(("cancel", ["gh", "api", "-X", "POST",
                                       "repos/%s/actions/runs/%d/cancel" % (rep["repo"], int(run))],
                            "%s run %s: %s" % (rep["repo"], run, act.get("why", "stale"))))
    rx = re.compile(slot_regex) if slot_regex else None
    seen = set() if seen is None else seen
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
    for act in report.get("host_actions") or []:
        if (act.get("do") in ("pause-slot", "resume-slot") and not act.get("needs_person") and host
                and act.get("host") == host and rx):
            # A pause runs only with an explicit stop command (--stop-cmd), never one guessed from the restart one.
            tmpl = stop_template if act["do"] == "pause-slot" else restart_template
            m = rx.search(act.get("runner") or "")
            # One change per box per pass, whichever org's report asks (the 20-minute cooldown is per org's state).
            if not tmpl or not m or m.group(1) in seen or "host-change" in seen:
                continue
            slot = m.group(1)
            seen.update({slot, "host-change"})
            note = [sys.executable, os.path.join(HERE, "steward.py"), "capacity-note", "--host", host,
                    "--paused" if act["do"] == "pause-slot" else "--resumed", act["runner"]]
            if report.get("state"):
                note += ["--state", report["state"]]
            out.append((act["do"], shlex.split(tmpl.format(slot=slot)), "%s (%s): %s" % (
                act["runner"], host, act.get("why", "")), note))
            continue
        if act.get("do") != "add-slot" or act.get("needs_person") or not host or act.get("host") != host:
            continue
        if not rx or not restart_template or "add-slot" in seen or "host-change" in seen:
            continue
        used = {int(m.group(1)) for m in (rx.search(n or "") for n in act.get("slots") or []) if m}
        slot = next(i for i in range(1, len(used) + 2) if i not in used)
        if len(used) >= int(act.get("max") or 0) or str(slot) in seen:
            continue
        seen.update({"add-slot", "host-change", str(slot)})
        out.append(("add-slot", shlex.split(restart_template.format(slot=slot)),
                    "%s slot %d: %s" % (host, slot, act.get("why", ""))))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("report", nargs="+", help="one or more capacity reports (one per org)")
    ap.add_argument("--slot-regex", default="", help="regex whose group 1 is this box's slot number")
    ap.add_argument("--restart", default="", help="command template to restart a slot, with {slot}")
    ap.add_argument("--max-cancels", type=int, default=100)
    ap.add_argument("--host", default="", help="this box's name in capacity.host.runners, for add-slot")
    ap.add_argument("--stop-cmd", default="",
                    help="command template to stop a slot, with {slot}, for pause-slot (none: pauses are not done)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-cancel", action="store_true", help="restarts only (cancels need that org's token)")
    ap.add_argument("--no-restart", action="store_true", help="cancels only")
    ap.add_argument("--cancel-state", default="",
                    help="JSON file remembering this box's recent cancels, so a run that ignored one is force-cancelled")
    a = ap.parse_args(argv)
    steps, seen = [], set()
    for path in a.report:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
        steps += plan(report, a.slot_regex, "" if a.no_restart else a.restart, a.max_cancels, seen,
                      cancel=not a.no_cancel, host=a.host, stop_template="" if a.no_restart else a.stop_cmd)
    failed = 0
    cancelled = load_cancelled(a.cancel_state) if a.cancel_state else {}
    for step in steps:
        kind, cmd, why = step[:3]
        note = step[3] if len(step) > 3 else None
        key = cmd[-1].rsplit("/actions/runs/", 1)
        key = "%s %s" % (key[0][len("repos/"):], key[1].split("/")[0]) if kind == "cancel" and len(key) == 2 else ""
        repeat = bool(key) and key in cancelled
        if a.dry_run:
            print("would %s%s: %s" % (kind, " (forced: an earlier cancel was ignored)" if repeat else "", why))
            continue
        if repeat:
            res = subprocess.run(force(cmd), capture_output=True, text=True, timeout=300)
            if res.returncode == 0:
                print("cancel ok (forced: an earlier cancel was ignored): %s" % why)
            else:
                print("cancel skipped: %s (still queued after a cancel, and GitHub refuses a force-cancel: %s)"
                      % (why, (res.stderr or res.stdout).strip()[:120]))
            continue
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if kind == "cancel" and key and res.returncode == 0:
            cancelled[key] = time.time()
        ok = res.returncode == 0
        err = "" if ok else (res.stderr or res.stdout).strip()
        if not ok and kind == "cancel" and "HTTP 409" in err:
            # A run GitHub never really queued (old, stuck in requested/waiting) refuses a plain cancel. Force-cancel
            # stops it; if that is refused too, it is GitHub's to clear, not a failure of this pass.
            forced = subprocess.run(force(cmd), capture_output=True, text=True, timeout=300)
            if forced.returncode == 0:
                print("cancel ok (forced): %s" % why)
            else:
                print("cancel skipped: %s (GitHub refuses to cancel it: %s)" % (why, err[:120]))
            continue
        failed += 0 if ok else 1
        print("%s %s: %s%s" % (kind, "ok" if ok else "FAILED", why, "" if ok else " (%s)" % err[:200]))
        if ok and note:
            rec = subprocess.run(note, capture_output=True, text=True, timeout=600)
            if rec.returncode != 0:
                failed += 1
                print("%s done but not recorded: %s" % (kind, (rec.stderr or rec.stdout).strip()[:200]))
    if a.cancel_state and not a.dry_run:
        try:
            save_cancelled(a.cancel_state, cancelled)
        except OSError as e:
            print("could not save %s: %s" % (a.cancel_state, e))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
