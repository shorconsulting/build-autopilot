#!/usr/bin/env python3
"""Keep only a few pull requests of a repo in CI at once, so each one finishes (build rules, rule 3).

Why: when every open pull request has CI queued on a small runner pool, GitHub hands jobs out oldest first across
all of them. A run with stages (changes -> lint/e2e -> test shards) goes to the back of a queue thousands of jobs long
at every stage, a new push cancels it before it reaches a runner, and nothing merges (SConsInc/ranch-rides,
2026-11-20: 186 open pull requests, 397 queued runs, 0 merges in 40 hours on 6 runners).

Each pass, per repo:
  admitted  at most --max-prs pull requests keep their CI. A pull request stays admitted while its head is the
            one it was admitted on and it still has CI queued or running (sticky, so a pass never pauses work it
            let start). Free places go, in order, to pull requests with auto-merge on, a head that has not
            changed for --settle-minutes (a lane still pushing would cancel its own run), then the oldest.
  paused    queued or running `pull_request` runs of every other pull request are cancelled and remembered.
            A run with a job running right now is never paused: its pull request counts as admitted instead.
  resumed   when a paused pull request is admitted, each of its paused runs on its current head gets
            `rerun-failed-jobs`, which re-runs the cancelled jobs and keeps the ones that already passed. A paused
            run whose pull request has a newer head is forgotten (the push started fresh runs).
Only `pull_request` runs are touched: pushes to the default branch, schedules and `pull_request_target` bookkeeping
(auto-merge) are left alone. Every action is printed as one line; --dry-run prints them without acting.

  python3 ci_admission.py --repo SConsInc/ranch-rides --repo SConsInc/tidyhaus --max-prs 3 \\
      --state /var/lib/build-autopilot/admission.json
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import steward  # noqa: E402

ACTIVE = ("queued", "in_progress", "waiting", "requested", "pending")
GHOST = "has not been queued yet"


def _now():
    return dt.datetime.now(dt.timezone.utc)


def pr_of(run, by_sha):
    """The open pull request a run belongs to: by its listed pull requests, else by head sha."""
    for p in run.get("pull_requests") or []:
        if p.get("number") in by_sha.get("_numbers", {}):
            return p["number"]
    return by_sha.get(run.get("head_sha"))


def plan(open_prs, runs, state, max_prs, settle_minutes=15, running_job=None, now=None):
    """Decide one pass for one repo. Returns (actions, new_state).
    open_prs: GitHub pulls (open). runs: the repo's active workflow runs (any event). state: this repo's
    {"admitted": {pr: sha}, "paused": {run_id: {"pr": n, "sha": s}}}. running_job(run_id) -> True when a job of
    that run is running right now (only asked for runs that would be paused). actions: [(kind, run_id, pr, why)]
    with kind "pause" or "resume"."""
    now = now or _now()
    running_job = running_job or (lambda _id: False)
    head = {p["number"]: (p.get("head") or {}).get("sha") for p in open_prs}
    by_sha = {sha: n for n, sha in head.items()}
    by_sha["_numbers"] = head
    prs = {p["number"]: p for p in open_prs}

    ci = {}  # pr -> [run] of active pull_request runs on its current head
    for r in runs:
        if r.get("event") != "pull_request" or r.get("status") not in ACTIVE:
            continue
        n = pr_of(r, by_sha)
        if n is None or head.get(n) != r.get("head_sha"):
            continue  # closed or superseded: the capacity check's stale-run rules own those
        ci.setdefault(n, []).append(r)

    paused = {}
    for rid, p in (state.get("paused") or {}).items():
        if head.get(p.get("pr")) == p.get("sha"):
            paused[str(rid)] = p  # still the pull request's head: worth resuming
    waiting = {}  # pr -> [run_id] paused on its current head
    for rid, p in paused.items():
        waiting.setdefault(p["pr"], []).append(rid)

    admitted = {}
    for n, sha in (state.get("admitted") or {}).items():
        n = int(n)
        if head.get(n) == sha and (n in ci or n in waiting):
            admitted[n] = sha
    # A run with a job on a runner right now is never cancelled: its pull request is admitted, even past the cap.
    for n, rs in ci.items():
        if n not in admitted and any(r.get("status") == "in_progress" and running_job(r["id"]) for r in rs):
            admitted[n] = head[n]

    def settled(n):
        p = prs[n]
        t = steward.parse_time(p.get("updated_at") or p.get("created_at"))
        # updated_at moves on a push (and on comments); a head pushed minutes ago is likely to move again.
        return t is None or (now - t).total_seconds() >= settle_minutes * 60

    def rank(n):
        p = prs[n]
        return (0 if p.get("auto_merge") else 1, 0 if settled(n) else 1, 1 if p.get("draft") else 0,
                p.get("created_at") or "", n)

    wants = sorted((set(ci) | set(waiting)) - set(admitted), key=rank)
    for n in wants:
        if len(admitted) >= max_prs:
            break
        admitted[n] = head[n]

    actions = []
    for n in sorted(admitted):
        for rid in sorted(waiting.get(n, [])):
            actions.append(("resume", int(rid), n, "admitted: re-run its paused jobs"))
            paused.pop(rid, None)
    for n in sorted(ci):
        if n in admitted:
            continue
        for r in ci[n]:
            actions.append(("pause", r["id"], n, "%s: %d pull requests already in CI" % (r.get("name"), max_prs)))
            paused[str(r["id"])] = {"pr": n, "sha": head[n]}
    return actions, {"admitted": {str(n): s for n, s in admitted.items()}, "paused": paused}


def _running_job(repo):
    def ask(run_id):
        try:
            jobs = steward.paged("repos/%s/actions/runs/%d/jobs" % (repo, run_id), "jobs", cap=300)
        except steward.GhError:
            return True  # unknown: never cancel what might be running
        return any(j.get("status") == "in_progress" for j in jobs)
    return ask


def active_runs(repo):
    runs, seen = [], set()
    for status in ("queued", "in_progress", "waiting", "pending", "requested"):
        try:
            batch = steward.paged("repos/%s/actions/runs?status=%s" % (repo, status), "workflow_runs", cap=3000)
        except steward.GhError:
            if status in ("queued", "in_progress"):
                raise
            continue
        for r in batch:
            if r["id"] not in seen:
                seen.add(r["id"])
                runs.append(r)
    return runs


def run_repo(repo, state, max_prs, settle_minutes, dry_run, max_actions):
    open_prs = steward.paged("repos/%s/pulls?state=open" % repo, cap=3000)
    runs = active_runs(repo)
    actions, new = plan(open_prs, runs, state, max_prs, settle_minutes, _running_job(repo))
    failed = 0
    for kind, rid, n, why in actions[:max_actions]:
        if dry_run:
            print("would %s: %s#%d run %d: %s" % (kind, repo, n, rid, why))
            continue
        path = "repos/%s/actions/runs/%d/%s" % (repo, rid, "cancel" if kind == "pause" else "rerun-failed-jobs")
        try:
            try:
                steward.gh(["api", "-X", "POST", path])
            except steward.GhError:
                if kind == "pause":
                    raise
                # A run cancelled before any job was made has no failed job to re-run: run it again whole.
                steward.gh(["api", "-X", "POST", "repos/%s/actions/runs/%d/rerun" % (repo, rid)])
            print("%s ok: %s#%d run %d: %s" % (kind, repo, n, rid, why))
        except steward.GhError as exc:
            if kind == "pause" and GHOST in str(exc):
                print("pause skipped: %s#%d run %d (a ghost run GitHub cannot cancel)" % (repo, n, rid))
                new["paused"].pop(str(rid), None)
                continue
            failed += 1
            print("%s FAILED: %s#%d run %d: %s" % (kind, repo, n, rid, str(exc)[:200]))
            if kind == "pause":
                new["paused"].pop(str(rid), None)
            else:
                new["paused"][str(rid)] = {"pr": n, "sha": new["admitted"].get(str(n))}
    for kind, rid, n, _ in actions[max_actions:]:  # over this pass's cap: next pass decides again
        if kind == "pause":
            new["paused"].pop(str(rid), None)
        else:
            new["paused"][str(rid)] = {"pr": n, "sha": new["admitted"].get(str(n))}
    print("%s: %d in CI (max %d: %s), %d paused" % (repo, len(new["admitted"]), max_prs,
                                                   " ".join("#" + n for n in sorted(new["admitted"], key=int)),
                                                   len(new["paused"])))
    return new, failed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", action="append", required=True, help="owner/name, repeatable")
    ap.add_argument("--max-prs", type=int, default=3, help="pull requests in CI at once, per repo")
    ap.add_argument("--settle-minutes", type=int, default=15)
    ap.add_argument("--max-actions", type=int, default=400, help="cancels plus re-runs per repo per pass")
    ap.add_argument("--state", required=True, help="JSON file this pass reads and writes")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        with open(a.state, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    failed = 0
    for repo in a.repo:
        try:
            new, f = run_repo(repo, state.get(repo) or {}, a.max_prs, a.settle_minutes, a.dry_run,
                              a.max_actions)
        except steward.GhError as exc:
            print("%s: admission check failed: %s" % (repo, exc))
            failed += 1
            continue
        failed += f
        if not a.dry_run:
            state[repo] = new
    if not a.dry_run:
        tmp = a.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, a.state)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
