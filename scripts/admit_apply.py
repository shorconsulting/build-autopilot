#!/usr/bin/env python3
"""Finish started pull request runs before starting new ones on a self-hosted runner pool (plain code, no AI).

GitHub queues each job when it becomes ready and hands queued jobs to runners oldest first. A run's later jobs
(those with `needs`) are queued only when the jobs they need finish, so with hundreds of runs queued every run gets
one job, then waits behind every other run's next job: everything is partly done and almost nothing finishes
(SConsInc/ranch-rides, 2026-11-21: ~560 runs queued, a PR's first job on Nov 19 and its test shard on Nov 21).

Each pass, per --repo, for queued pull_request runs of workflows with at least --min-jobs jobs:
  * fail fast: a run with a FAILED job has its queued jobs cancelled. The PR cannot pass on that run, so its
    remaining jobs are pure waste. It is never re-run here (a new push, or the steward sweep for an infra failure,
    starts the next one).
  * admit at most --max-active runs at once. Runs that have started (a job done or running) come first, oldest
    first; then runs not started. A run past the limit with no job running is parked: cancelled, and kept in
    --state. Nothing is lost: a started run keeps its successful jobs and resumes with `rerun-failed-jobs`, which
    re-runs only its cancelled jobs; a run not started is re-run whole.
  * each pass first counts the active runs, then admits parked runs, oldest first, into the free places.
A PR with the priority label (default `ci:priority`, see priority_apply.py) is always admitted ahead of the rest. A
run parked for PARK_FOR_S is admitted regardless, so nothing waits forever. Superseded runs (their PR's head moved
on) are cancelled and forgotten. It stops parking when fewer than --min-rate API calls are left, and only reads the
jobs of runs that look started (updated after they were queued), so a pass costs about one call per active run.

  GH_TOKEN=... python3 admit_apply.py --repo o/app --state /var/lib/build-autopilot/admit-o.json --max-active 8
"""
import argparse
import sys
import time

import priority_apply as pa

PARK_FOR_S = 12 * 3600
PR_EVENTS = pa.PR_EVENTS
STARTED_AFTER_S = 60  # a run updated this long after it was queued has had a job change state


def looks_started(run):
    return pa.ts(run["updated_at"]) - pa.queued_at(run) > STARTED_AFTER_S


def classify(jobs):
    """'running' | 'failed' | 'started' | 'new' for one run's latest jobs."""
    st = [(j.get("status"), j.get("conclusion")) for j in jobs]
    if any(s == "in_progress" for s, _ in st):
        return "running"
    if any(c == "failure" for _, c in st):
        return "failed"
    if any(s == "completed" for s, _ in st):
        return "started"
    return "new"


def decide(runs, max_active, prio_prs=()):
    """runs: [{"repo","id","q","kind","prs":[n],"superseded":bool}] -> (keep, park, cancel_failed, cancel_stale).
    Kept: every running run, then the priority PRs' runs, then started, then new, oldest first, up to max_active."""
    stale = [r for r in runs if r["superseded"]]
    live = [r for r in runs if not r["superseded"]]
    failed = [r for r in live if r["kind"] == "failed"]
    rest = [r for r in live if r["kind"] != "failed"]
    prio = set(prio_prs)
    order = {"running": 0, "started": 2, "new": 3}

    def rank(r):
        first = 0 if r["kind"] == "running" else (1 if prio & set(r["prs"]) else order[r["kind"]])
        return (first, r["q"], r["id"])
    rest.sort(key=rank)
    keep, park = [], []
    for r in rest:
        (keep if r["kind"] == "running" or len(keep) < max_active else park).append(r)
    return keep, park, failed, stale


def run(repos, state_path, max_active=8, min_jobs=3, label=pa.LABEL, min_rate=1500, dry=False, api=pa.gh,
        now=time.time):
    parked = pa.load(state_path) if state_path else {}
    failed_calls = 0
    ok, rate, _ = api("GET", "rate_limit")
    left = ((rate or {}).get("resources", {}).get("core", {}).get("remaining")) if ok else None
    may_park = left is None or left >= min_rate
    for repo in repos:
        prio_prs = set()
        okp, issues, _ = api("GET", "repos/%s/issues?state=open&labels=%s&per_page=100" % (
            repo, pa.quote(label, safe="")))
        for i in (issues or []) if okp else []:
            if "pull_request" in i and pa.labeller(api, repo, i["number"], label)[1]:
                prio_prs.add(i["number"])
        runs, sizes = [], {}  # sizes: workflow id -> job count of one of its runs (a merge bot's 1-job run is never parked)
        for _, r in pa.queued_for(api, repo):
            if r.get("event") not in PR_EVENTS or not r.get("pull_requests"):
                continue
            wid = r.get("workflow_id")
            item = {"wid": wid, "repo": repo, "id": r["id"], "q": pa.queued_at(r), "prs": [p["number"] for p in r["pull_requests"]],
                    "superseded": pa.superseded(r), "name": r.get("name", ""), "kind": "new"}
            if looks_started(r) or item["superseded"]:
                okj, data, err = api("GET", "repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (repo, r["id"]))
                if not okj:
                    print("%s run %d: could not read its jobs, left alone (%s)" % (repo, r["id"], err))
                    continue
                jobs = (data or {}).get("jobs") or []
                sizes[wid] = max(sizes.get(wid, 0), len(jobs))
                item["kind"] = classify(jobs)
            runs.append(item)
        for wid in {r["wid"] for r in runs} - set(sizes):
            # No started run of this workflow to size it by: read one of its runs (a run not started may list only
            # the jobs that need nothing, so a started run's count wins whenever there is one).
            sample = next(r for r in runs if r["wid"] == wid)
            okj, data, _ = api("GET", "repos/%s/actions/runs/%d/jobs?filter=latest&per_page=100" % (repo, sample["id"]))
            sizes[wid] = len((data or {}).get("jobs") or []) if okj else 0
        # A light workflow (merge bot, labeler, review) or one whose size could not be read is never shaped.
        runs = [r for r in runs if sizes.get(r["wid"], 0) >= min_jobs]
        # Runs GitHub already reports in_progress hold places too (only shaped workflows count).
        shaped = {w for w, n in sizes.items() if n >= min_jobs}
        busy = 0
        for page in range(1, 6):
            okb, data, _ = api("GET", "repos/%s/actions/runs?status=in_progress&per_page=100&page=%d" % (repo, page))
            got = (data or {}).get("workflow_runs") or [] if okb else []
            busy += sum(1 for r in got if r.get("workflow_id") in shaped and r.get("event") in PR_EVENTS)
            if len(got) < 100:
                break
        # Parked runs are cancelled, so they are not in the queue: they wait in the state file.
        mine = {k: v for k, v in parked.items() if k.split(" ")[0] == repo}
        keep, park, failed, stale = decide(runs, max(max_active - busy, 0), prio_prs)
        free = max(max_active - busy - len(keep), 0)
        for r in failed:
            failed_calls += act(api, dry, repo, r, "cancel", "fail fast: a job already failed, the PR cannot pass on this run")
        for r in stale:
            failed_calls += act(api, dry, repo, r, "cancel", "superseded: its PR has a newer head")
        if may_park:
            for r in park:
                if act(api, dry, repo, r, "cancel", "parked: %d runs ahead are active" % max_active) == 0 and not dry:
                    parked["%s %d" % (repo, r["id"])] = {"t": now(), "q": r["q"], "kind": r["kind"], "prs": r["prs"]}
        # Admit: priority PRs first, then oldest queued; anything parked too long goes regardless of room.
        waiting = sorted(mine.items(), key=lambda kv: (0 if prio_prs & set(kv[1].get("prs") or []) else 1,
                                                       kv[1].get("q", 0), int(kv[0].split(" ")[1])))
        for key, v in waiting:
            overdue = now() - v.get("t", 0) > PARK_FOR_S
            if free <= 0 and not overdue:
                continue
            rid = int(key.split(" ")[1])
            okr, cur, err = api("GET", "repos/%s/actions/runs/%d" % (repo, rid))
            if not okr:
                print("admit waits: %s run %d (%s)" % (repo, rid, err))
                continue
            if (cur or {}).get("status") != "completed":
                continue  # the cancel has not landed yet
            if pa.superseded(cur or {}):
                print("forgotten: %s run %d (its PR has a newer head)" % (repo, rid))
                parked.pop(key, None)
                continue
            path = "rerun-failed-jobs" if v.get("kind") == "started" else "rerun"
            if dry:
                print("would admit: %s run %d (%s)" % (repo, rid, path))
                continue
            okr, _, err = api("POST", "repos/%s/actions/runs/%d/%s" % (repo, rid, path))
            if okr:
                print("admitted: %s run %d (%s%s)" % (repo, rid, path, ", parked too long" if overdue else ""))
                parked.pop(key, None)
                free -= 1
            else:
                failed_calls += 1
                print("admit FAILED: %s run %d (%s)" % (repo, rid, err))
        print("%s: %d active (%d in progress + %d queued; limit %d), %d parked, %d failed-fast, %d superseded%s" % (
            repo, busy + len(keep), busy, len(keep), max_active, sum(1 for k in parked if k.split(" ")[0] == repo), len(failed), len(stale),
            "" if may_park else "; not parking, %s API calls left" % left))
        if state_path and not dry:
            pa.save(state_path, parked)  # after each repo, so a killed pass still remembers what it parked
    return 1 if failed_calls else 0


def act(api, dry, repo, r, what, why):
    line = "%s run %d (%s, PR %s): %s" % (repo, r["id"], r.get("name", ""), ",".join("#%d" % n for n in r["prs"]), why)
    if dry:
        print("would %s: %s" % (what, line))
        return 0
    ok, _, err = api("POST", "repos/%s/actions/runs/%d/cancel" % (repo, r["id"]))
    if not ok and "409" in err:
        ok, _, err = api("POST", "repos/%s/actions/runs/%d/force-cancel" % (repo, r["id"]))
    print("%s %s: %s%s" % (what, "ok" if ok else "FAILED", line, "" if ok else " (%s)" % err))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", action="append", required=True)
    ap.add_argument("--state", default="", help="JSON file of parked runs (required unless --dry-run)")
    ap.add_argument("--max-active", type=int, default=8, help="runs admitted at once per repo")
    ap.add_argument("--min-jobs", type=int, default=3, help="only runs with at least this many jobs are shaped")
    ap.add_argument("--label", default=pa.LABEL)
    ap.add_argument("--min-rate", type=int, default=1500)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.state and not a.dry_run:
        ap.error("--state is required (a parked run must be admitted again)")
    return run(a.repo, a.state, a.max_active, a.min_jobs, a.label, a.min_rate, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
