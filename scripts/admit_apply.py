#!/usr/bin/env python3
"""Finish started pull request runs before starting new ones on a self-hosted runner pool (plain code, no AI).

GitHub queues each job when it becomes ready and hands queued jobs to runners oldest first. A run's later jobs
(those with `needs`) are queued only when the jobs they need finish, so with hundreds of runs queued every run gets
one job, then waits behind every other run's next job: everything is partly done and almost nothing finishes
(SConsInc/ranch-rides, 2026-11-21: ~560 runs queued, a PR's first job on Nov 19 and its test shard on Nov 21).

Each pass, per --repo, for queued pull_request runs of workflows with at least --min-jobs jobs:
  * fail fast: a run with a FAILED job has its queued jobs cancelled. The PR cannot pass on that run, so its
    remaining jobs are pure waste. It is never re-run here. A new push starts the next one; when the failure was a
    machine failure (checkout, setup), the steward sweep re-runs the failed and cancelled jobs as before.
  * admit at most --max-active runs at once. Runs that have started (a job done or running) come first, oldest
    first; then runs not started. A run past the limit with no job running is parked: cancelled, and kept in
    --state. Nothing is lost: a started run keeps its successful jobs and resumes with `rerun-failed-jobs`, which
    re-runs only its cancelled jobs; a run not started is re-run whole.
  * queued and parked runs compete in ONE ordering each pass, so a parked started run takes a place ahead of a new
    run (which is parked instead). Places = --max-active minus the runs GitHub reports in progress.
A PR with the priority label (default `ci:priority`, see priority_apply.py) goes ahead of the rest. Up to two runs
per repo parked longer than PARK_FOR_S go next each pass (oldest first), so nothing waits forever and a backlog parked
in one pass trickles back instead of returning at once. Runs listed in a --hold file (priority_apply's owed re-runs)
are never touched; the steward sweep is given this script's state file so it does not re-run a parked run. Superseded runs (their PR's head moved
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


def decide(runs, places, prio_prs=(), overdue_cap=2):
    """One ordering over queued AND parked runs -> (keep, park, cancel_failed, cancel_stale).
    runs: [{"id","q","kind","prs":[n],"superseded":bool,"parked":bool,"overdue":bool}].
    Running runs always stay (they hold places but are never parked). The other places go, in order, to: priority
    PRs' runs; up to overdue_cap runs parked too long (oldest first, so a backlog parked in one pass trickles back
    instead of returning at once); started runs; new runs; oldest first within each. keep = the runs that get a
    place (a parked one there is admitted); park = queued runs that do not (a parked one there just keeps waiting)."""
    stale = [r for r in runs if r["superseded"]]
    live = [r for r in runs if not r["superseded"]]
    failed = [r for r in live if r["kind"] == "failed" and not r.get("parked")]
    rest = [r for r in live if r["kind"] != "failed" or r.get("parked")]
    prio = set(prio_prs)
    boosted = {r["id"] for r in sorted((r for r in rest if r.get("overdue")), key=lambda r: (r["q"], r["id"]))[:overdue_cap]}
    order = {"started": 3, "new": 4, "failed": 4}

    def rank(r):
        if r["kind"] == "running":
            first = 0
        elif prio & set(r["prs"]):
            first = 1
        elif r["id"] in boosted:
            first = 2
        else:
            first = order[r["kind"]]
        return (first, r["q"], r["id"])
    rest.sort(key=rank)
    keep, park = [], []
    for r in rest:
        (keep if r["kind"] == "running" or len(keep) < places else park).append(r)
    return keep, park, failed, stale


def remaining(api):
    ok, rate, _ = api("GET", "rate_limit")
    return ((rate or {}).get("resources", {}).get("core", {}).get("remaining")) if ok else None


def run(repos, state_path, max_active=8, min_jobs=3, label=pa.LABEL, min_rate=1500, dry=False, api=pa.gh,
        now=time.time, hold=None, overdue_cap=2):
    """hold: {"<repo> <run>"} owned by another script this pass (priority_apply's owed re-runs): never touched."""
    parked = pa.load(state_path) if state_path else {}
    hold = hold or set()
    failed_calls = 0
    for repo in repos:
        left = remaining(api)
        may_park = left is None or left >= min_rate
        prio_prs = set()
        okp, issues, _ = api("GET", "repos/%s/issues?state=open&labels=%s&per_page=100" % (
            repo, pa.quote(label, safe="")))
        for i in (issues or []) if okp else []:
            if "pull_request" in i and pa.labeller(api, repo, i["number"], label)[1]:
                prio_prs.add(i["number"])
        runs, sizes = [], {}  # sizes: workflow id -> job count (a merge bot's 1-job run is never shaped)
        for _, r in pa.queued_for(api, repo):
            if r.get("event") not in PR_EVENTS or not r.get("pull_requests"):
                continue
            key = "%s %d" % (repo, r["id"])
            if key in hold:
                continue
            wid = r.get("workflow_id")
            item = {"wid": wid, "repo": repo, "id": r["id"], "q": pa.queued_at(r),
                    "prs": [p["number"] for p in r["pull_requests"]], "superseded": pa.superseded(r),
                    "name": r.get("name", ""), "kind": "new", "parked": False, "overdue": False}
            # A re-run attempt resets its queue time, so it never "looks started"; read its jobs to see what it kept.
            if looks_started(r) or item["superseded"] or int(r.get("run_attempt") or 1) > 1:
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
        shaped = {w for w, n in sizes.items() if n >= min_jobs} | {v.get("wid") for k, v in parked.items()
                                                                    if k.split(" ")[0] == repo}
        # Runs GitHub already reports in_progress hold places too (only shaped workflows count).
        busy = 0
        for page in range(1, 6):
            okb, data, _ = api("GET", "repos/%s/actions/runs?status=in_progress&per_page=100&page=%d" % (repo, page))
            got = ((data or {}).get("workflow_runs") or []) if okb else []
            busy += sum(1 for r in got if r.get("workflow_id") in shaped and r.get("event") in PR_EVENTS
                        and "%s %d" % (repo, r["id"]) not in hold)
            if len(got) < 100:
                break
        # Parked runs (cancelled, so not in the queue) compete in the same ordering as the queued ones.
        queued_ids = {r["id"] for r in runs}
        for key, v in parked.items():
            rid = int(key.split(" ")[1])
            if key.split(" ")[0] != repo or rid in queued_ids or key in hold:
                continue
            runs.append({"wid": v.get("wid"), "repo": repo, "id": rid, "q": v.get("q", 0), "prs": v.get("prs") or [],
                         "superseded": False, "name": v.get("name", ""), "kind": v.get("kind", "new"),
                         "parked": True, "overdue": now() - v.get("t", 0) > PARK_FOR_S})
        keep, park, failed, stale = decide(runs, max(max_active - busy, 0), prio_prs, overdue_cap)
        for r in failed:
            failed_calls += act(api, dry, repo, r, "cancel", "fail fast: a job already failed, the PR cannot pass on this run")
        for r in stale:
            failed_calls += act(api, dry, repo, r, "cancel", "superseded: its PR has a newer head")
            parked.pop("%s %d" % (repo, r["id"]), None)
        n_parked = 0
        for r in park:
            if r["parked"]:
                continue  # still waiting its turn
            if not may_park:
                continue
            if act(api, dry, repo, r, "cancel", "parked: %d runs ahead are active" % max_active) == 0:
                n_parked += 1
                if not dry:
                    parked["%s %d" % (repo, r["id"])] = {"t": now(), "q": r["q"], "kind": r["kind"], "prs": r["prs"],
                                                         "wid": r["wid"], "name": r["name"]}
        admitted = 0
        for r in keep:
            if not r["parked"]:
                continue
            key = "%s %d" % (repo, r["id"])
            okr, cur, err = api("GET", "repos/%s/actions/runs/%d" % (repo, r["id"]))
            if not okr:
                print("admit waits: %s run %d (%s)" % (repo, r["id"], err))
                continue
            if (cur or {}).get("status") != "completed":
                continue  # the cancel has not landed yet
            if pa.superseded(cur or {}):
                print("forgotten: %s run %d (its PR has a newer head)" % (repo, r["id"]))
                parked.pop(key, None)
                continue
            # A started run keeps its successful jobs and re-runs only the cancelled ones.
            path = "rerun-failed-jobs" if r["kind"] in ("started", "running") else "rerun"
            if dry:
                print("would admit: %s run %d (%s)" % (repo, r["id"], path))
                continue
            okr, _, err = api("POST", "repos/%s/actions/runs/%d/%s" % (repo, r["id"], path))
            if okr:
                admitted += 1
                print("admitted: %s run %d (%s%s)" % (repo, r["id"], path, ", parked too long" if r["overdue"] else ""))
                parked.pop(key, None)
            else:
                failed_calls += 1
                print("admit FAILED: %s run %d (%s)" % (repo, r["id"], err))
        print("%s: %d in progress, %d places, %d kept, %d newly parked, %d admitted, %d waiting parked, "
              "%d failed-fast, %d superseded%s" % (
                  repo, busy, max(max_active - busy, 0), len(keep), n_parked, admitted,
                  sum(1 for k in parked if k.split(" ")[0] == repo), len(failed), len(stale),
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
    ap.add_argument("--hold", action="append", default=[],
                    help="a state file of another script (priority_apply's owed re-runs): its runs are never touched")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.state and not a.dry_run:
        ap.error("--state is required (a parked run must be admitted again)")
    hold = set()
    for path in a.hold:
        hold |= set(pa.load(path))
    return run(a.repo, a.state, a.max_active, a.min_jobs, a.label, a.min_rate, a.dry_run, hold=hold)


if __name__ == "__main__":
    sys.exit(main())
