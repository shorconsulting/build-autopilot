#!/usr/bin/env python3
"""Act on a `steward.py sweep --out` report without an agent, on an always-on runner box (cron / systemd timer).

Self-healing (build rules, rule 3: a stalled check is restarted, not waited on). Plain code, no AI, two mechanical
repairs only:

  rerun           a PR whose checks failed before reaching the code (checkout, setup, a runner that vanished, a
                  cancel with nothing else of that name): re-run the failed jobs of that workflow run, at most
                  `lanes.infra_reruns_per_pr` times (counted by the run's attempt number), so a real break is not
                  re-run forever.
  restart-checks  a check RUNNING far past `stall.check_running_minutes`: cancel its run. The next pass sees it
                  cancelled, with nothing else of that name, and re-runs it under the rule above.

A check stalled while QUEUED is left alone: it is waiting for a runner, and cancelling it only sends it to the back
of the queue. Capacity (`steward.py capacity`) deals with queues. A PR labelled parked / do-not-merge / hold is
skipped. Red code (`fix`), conflicts (`rebase`) and the rest stay with the lanes: they need judgement.

Every action is printed as one line, so the timer's journal is the record.

  python3 sweep_apply.py sweep.json [--dry-run] [--max-actions 20] [--max-attempts 3]
"""
import argparse
import json
import os
import subprocess
import sys

SKIP_LABELS = {"parked", "do-not-merge", "hold", "blocked"}


def load_cap(rules_path=None):
    path = rules_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rules", "build-rules.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return int(json.load(fh)["lanes"]["infra_reruns_per_pr"])
    except (OSError, ValueError, KeyError, TypeError):
        return 3


def load_hold(paths):
    """{"<repo lowercase> <run>"} from state files of the capacity pass (admit_apply's parked runs): cancelled on
    purpose, so never re-run as a machine failure here; the capacity pass admits them in turn."""
    held = set()
    for path in paths or []:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            held |= {k.lower() for k in data if isinstance(k, str)}
    return held


def plan(report, max_attempts=3, max_actions=20, hold=frozenset()):
    """[(kind, argv, why)]: re-runs for machine failures, cancels for hung running checks. Each run once per pass."""
    out, seen = [], set()
    for rep in report.get("repos") or []:
        repo = rep["repo"]
        for pr in rep.get("pull_requests") or []:
            if SKIP_LABELS & set(pr.get("labels") or []):
                continue
            if pr.get("state") == "red-infra":
                for f in pr.get("infra_runs") or []:
                    rid, attempt = f.get("run_id"), f.get("attempt") or 1
                    if not rid or (repo, rid) in seen:
                        continue
                    seen.add((repo, rid))
                    if ("%s %s" % (repo, rid)).lower() in hold:
                        print("leave %s#%s run %s (%s): parked by the capacity pass, which admits it in turn"
                              % (repo, pr["number"], rid, f.get("name")))
                        continue
                    if attempt > max_attempts:
                        print("leave %s#%s run %s (%s): already on attempt %d, past the %d re-run cap; a lane or "
                              "person looks at it" % (repo, pr["number"], rid, f.get("name"), attempt, max_attempts))
                        continue
                    out.append(("rerun", ["gh", "api", "-X", "POST",
                                          "repos/%s/actions/runs/%d/rerun-failed-jobs" % (repo, int(rid))],
                                "%s#%s %s failed before the code ran (attempt %d)"
                                % (repo, pr["number"], f.get("name"), attempt)))
            elif pr.get("state") == "stalled":
                for s in pr.get("stalled") or []:
                    rid = s.get("run_id")
                    if s.get("status") != "in_progress" or not rid or (repo, rid) in seen:
                        continue
                    seen.add((repo, rid))
                    out.append(("cancel", ["gh", "api", "-X", "POST",
                                           "repos/%s/actions/runs/%d/cancel" % (repo, int(rid))],
                                "%s#%s %s running %d min, hung: cancel, the next pass re-runs it"
                                % (repo, pr["number"], s.get("name"), s.get("minutes") or 0)))
    return out[:max_actions]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("report", nargs="+", help="one or more sweep reports")
    ap.add_argument("--dry-run", action="store_true", help="print what it would do and change nothing")
    ap.add_argument("--max-actions", type=int, default=20, help="cap per report per pass (default 20)")
    ap.add_argument("--max-attempts", type=int, default=None,
                    help="re-run a run only while its attempt is at most this (default lanes.infra_reruns_per_pr)")
    ap.add_argument("--rules", help="build-rules.json (default: the plugin's)")
    ap.add_argument("--hold", action="append", default=[],
                    help="a capacity-pass state file (admit-<owner>.json): its runs are never re-run here")
    a = ap.parse_args(argv)
    hold = load_hold(a.hold)
    cap = a.max_attempts if a.max_attempts is not None else load_cap(a.rules)
    failed = 0
    for path in a.report:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
        for kind, argv_, why in plan(report, cap, a.max_actions, hold):
            if a.dry_run:
                print("would %s: %s" % (kind, why))
                continue
            r = subprocess.run(argv_, capture_output=True, text=True)
            if r.returncode == 0:
                print("%s: %s" % (kind, why))
            else:
                failed += 1
                print("%s failed: %s (%s)" % (kind, why, (r.stderr or r.stdout).strip()[:200]))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
