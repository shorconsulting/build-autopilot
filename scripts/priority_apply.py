#!/usr/bin/env python3
"""Put a labelled pull request's CI at the front of the line on a self-hosted runner pool (plain code, no AI).

GitHub hands queued jobs to self-hosted runners in arrival order and has no priority setting. So when an open PR in
one of --repo carries --label (default `ci:priority`), applied by someone with admin or maintain access, and its
head still has a job waiting for a runner, every pull request run of those repos that is QUEUED (no job started, so
nothing is lost) and was queued before it is bumped: it is cancelled, and once GitHub reports it completed it is
re-run, which queues it again behind the priority PR. "Queued" is when the current attempt was queued
(run_started_at), so a run bumped once is never bumped again for the same priority PR. Only pull_request and
pull_request_target runs with a pull request are bumped: a push, schedule, release or deploy run is never touched.
A bumped run that is already superseded (its pull request's head moved on) is only cancelled, never re-run. List
only repos whose CI runs on the box's own pool in --repo: a run waiting for a GitHub-hosted runner gains nothing.

Re-runs owed are kept in --state, so a run cancelled in one pass and not yet completed is re-run by a later pass
(each pass first pays what earlier passes owe, with or without a priority PR). A re-run still owed after
OWE_FOR_S is dropped with a line in the journal. It never touches a run that has started, a run of the priority
PR itself, or a repo not named with --repo, and it stops bumping when fewer than --min-rate API calls are left.

  GH_TOKEN=... python3 priority_apply.py --repo o/app --state /var/lib/build-autopilot/priority-o.json
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import quote

LABEL = "ci:priority"
OWE_FOR_S = 6 * 3600
MAX_QUEUED_PAGES = 30  # 3,000 queued runs per repo


def gh(method, path):
    """(ok, parsed JSON or None, error text) for one `gh api` call."""
    res = subprocess.run(["gh", "api", "-X", method, path], capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        return False, None, (res.stderr or res.stdout).strip()[:200]
    try:
        return True, json.loads(res.stdout) if res.stdout.strip() else None, ""
    except ValueError:
        return True, None, ""


def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").timestamp()


PR_EVENTS = ("pull_request", "pull_request_target")
MAINTAINERS = ("admin", "maintain")


def queued_at(run):
    """When the run's current attempt was queued: run_started_at (a re-run keeps its created_at), else created_at."""
    return ts(run.get("run_started_at") or run["created_at"])


def superseded(run):
    """True when every pull request the run is for has a different head now: a re-run would test old code."""
    prs = run.get("pull_requests") or []
    return bool(prs) and all((p.get("head") or {}).get("sha") not in (None, run.get("head_sha")) for p in prs)


def plan(priority_runs, queued, max_bumps):
    """[(repo, run, rerun?)] to bump: queued runs created before the oldest unfinished priority run.
    priority_runs: [(repo, run)] of runs on priority heads with a job waiting, each with "waiting_since" (epoch);
    queued: [(repo, run)] of queued runs."""
    if not priority_runs:
        return []
    first = min(r["waiting_since"] for _, r in priority_runs)
    mine = {(repo, r["id"]) for repo, r in priority_runs}
    heads = {(repo, r["head_sha"]) for repo, r in priority_runs}
    older = [(repo, r) for repo, r in queued
             if (repo, r["id"]) not in mine and (repo, r.get("head_sha")) not in heads
             and r.get("status") == "queued" and r.get("event") in PR_EVENTS and r.get("pull_requests")
             and queued_at(r) < first]
    older.sort(key=lambda x: (queued_at(x[1]), x[1]["id"]))
    return [(repo, r, not superseded(r)) for repo, r in older[:max(max_bumps, 0)]]


def load(path):
    """{"<repo> <run>": {"t": epoch of the bump, "q": epoch it was queued before it}}."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and len(k.split(" ")) == 2}


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def pay_owed(api, owed, now, dry):
    """Re-run every owed run GitHub now reports completed. Returns the count of failures."""
    failed = 0
    # Oldest first, by when each run was queued before its bump (numeric, never the text of the key).
    for key in sorted(owed, key=lambda k: (owed[k].get("q", 0), int(k.split(" ")[1]))):
        repo, run = key.split(" ")
        if now - owed[key].get("t", 0) > OWE_FOR_S:
            print("re-run dropped: %s run %s (still not completed %d hours after the bump)" % (repo, run, OWE_FOR_S // 3600))
            del owed[key]
            continue
        ok, data, err = api("GET", "repos/%s/actions/runs/%s" % (repo, run))
        if not ok:
            print("re-run waits: %s run %s (%s)" % (repo, run, err))
            continue
        if (data or {}).get("status") != "completed":
            continue
        if dry:
            print("would re-run: %s run %s" % (repo, run))
            continue
        ok, _, err = api("POST", "repos/%s/actions/runs/%s/rerun" % (repo, run))
        if ok:
            print("re-run ok: %s run %s (behind the priority PR)" % (repo, run))
            del owed[key]
        else:
            failed += 1
            print("re-run FAILED: %s run %s (%s)" % (repo, run, err))
    return failed


def labeller(api, repo, number, label):
    """(login, may) of whoever last applied the label: may is True only for admin or maintain access."""
    who = None
    for page in range(1, 11):
        ok, events, err = api("GET", "repos/%s/issues/%d/events?per_page=100&page=%d" % (repo, number, page))
        if not ok:
            print("%s #%d: could not read who applied %s (%s)" % (repo, number, label, err))
            return None, False
        for e in events or []:
            if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label:
                who = (e.get("actor") or {}).get("login")
        if len(events or []) < 100:
            break
    if not who:
        return None, False
    ok, perm, err = api("GET", "repos/%s/collaborators/%s/permission" % (repo, who))
    role = (perm or {}).get("role_name") or (perm or {}).get("permission") if ok else None
    return who, role in MAINTAINERS


def waiting_since(api, repo, run):
    """Epoch the run's earliest job still waiting for a runner was queued; None when no job waits."""
    if run.get("status") == "queued":
        return queued_at(run)
    ok, data, err = api("GET", "repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (repo, run["id"]))
    if not ok:
        print("%s run %d: could not list its jobs (%s)" % (repo, run["id"], err))
        return None
    waits = [ts(j["created_at"]) for j in (data or {}).get("jobs") or [] if j.get("status") == "queued"]
    return min(waits) if waits else None


def priority_runs_for(api, repo, label):
    """[(repo, run)] of the runs on the heads of open PRs carrying the label that have a job waiting for a runner."""
    ok, issues, err = api("GET", "repos/%s/issues?state=open&labels=%s&per_page=100" % (repo, quote(label, safe="")))
    if not ok:
        print("%s: could not list %s PRs (%s)" % (repo, label, err))
        return []
    out = []
    for issue in issues or []:
        if "pull_request" not in issue:
            continue
        ok, pr, err = api("GET", "repos/%s/pulls/%d" % (repo, issue["number"]))
        if not ok or not pr:
            print("%s #%s: could not read the PR (%s)" % (repo, issue["number"], err))
            continue
        who, may = labeller(api, repo, issue["number"], label)
        if not may:
            print("%s #%s has %s from %s, who has no admin or maintain access: ignored" % (
                repo, issue["number"], label, who or "an unknown account"))
            continue
        ok, runs, err = api("GET", "repos/%s/actions/runs?head_sha=%s&per_page=100" % (repo, pr["head"]["sha"]))
        if not ok:
            print("%s #%s: could not list the runs on its head (%s)" % (repo, issue["number"], err))
            continue
        n = 0
        for r in (runs or {}).get("workflow_runs") or []:
            since = waiting_since(api, repo, r) if r.get("status") != "completed" else None
            if since is not None:
                out.append((repo, dict(r, waiting_since=since)))
                n += 1
        print("%s #%s has %s (applied by %s): %d run(s) on its head waiting for a runner" % (
            repo, issue["number"], label, who, n))
    return out


def queued_for(api, repo):
    out = []
    for page in range(1, MAX_QUEUED_PAGES + 1):
        ok, data, err = api("GET", "repos/%s/actions/runs?status=queued&per_page=100&page=%d" % (repo, page))
        if not ok:
            print("%s: could not list queued runs (%s)" % (repo, err))
            break
        runs = (data or {}).get("workflow_runs") or []
        out += [(repo, r) for r in runs]
        if len(runs) < 100:
            break
    return out


def run(repos, state_path, label=LABEL, max_bumps=500, min_rate=1500, settle_s=30, dry=False, api=gh,
        sleep=time.sleep, now=time.time):
    owed = load(state_path) if state_path else {}
    failed = pay_owed(api, owed, now(), dry)
    prio = [x for repo in repos for x in priority_runs_for(api, repo, label)]
    if prio:
        ok, rate, _ = api("GET", "rate_limit")
        left = ((rate or {}).get("resources", {}).get("core", {}).get("remaining")) if ok else None
        if left is not None and left < min_rate:
            print("priority: not bumping, %d API calls left this hour (under %d)" % (left, min_rate))
            prio = []
    prio_names = ", ".join(sorted({"%s@%s" % (repo, r["head_sha"][:7]) for repo, r in prio}))
    bumps = plan(prio, [x for repo in repos for x in queued_for(api, repo)], max_bumps) if prio else []
    for repo, r, rerun in bumps:
        why = "%s run %s (%s, queued %s) for priority %s" % (
            repo, r["id"], r.get("name", ""), r.get("run_started_at") or r["created_at"], prio_names)
        if dry:
            print("would bump: %s%s" % (why, "" if rerun else ", superseded: cancel only"))
            continue
        ok, _, err = api("POST", "repos/%s/actions/runs/%d/cancel" % (repo, r["id"]))
        if not ok and "409" in err:
            ok, _, err = api("POST", "repos/%s/actions/runs/%d/force-cancel" % (repo, r["id"]))
        if not ok:
            failed += 1
            print("bump FAILED: %s (%s)" % (why, err))
            continue
        if rerun:
            owed.setdefault("%s %d" % (repo, r["id"]), {"t": now(), "q": queued_at(r)})
        print("bumped: %s%s" % (why, ", re-run owed" if rerun else ", superseded: cancelled, not re-run"))
    if bumps and not dry:
        if state_path:
            save(state_path, owed)  # before the wait, so a killed pass still owes these re-runs
        sleep(settle_s)
        failed += pay_owed(api, owed, now(), dry)
    if state_path and not dry:
        save(state_path, owed)
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", action="append", required=True, help="owner/repo whose runs share the pool")
    ap.add_argument("--state", default="", help="JSON file of re-runs owed (without it a bump is never re-run)")
    ap.add_argument("--label", default=LABEL)
    ap.add_argument("--max-bumps", type=int, default=500)
    ap.add_argument("--min-rate", type=int, default=1500)
    ap.add_argument("--settle", type=int, default=30, help="seconds to wait after cancelling, before re-running")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.state and not a.dry_run:
        ap.error("--state is required (a bumped run must be re-run)")
    return run(a.repo, a.state, a.label, a.max_bumps, a.min_rate, a.settle, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
