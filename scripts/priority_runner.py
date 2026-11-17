#!/usr/bin/env python3
"""Priority CI without a reserved runner: lend the next free slot of the self-hosted pool to a labelled pull request.

GitHub hands queued jobs to runners first come, first served, and has no priority. A job can only jump the line by
asking for a runner label the ordinary queue does not use. This watcher makes such a runner appear only while it is
needed: when a job that asks for the priority label is waiting, it swaps one pool runner's label from the pool's to
the priority one. That runner finishes the job it is on, and the only thing it can take next is the priority job.
When no priority job is waiting any more, the label is swapped back. The pool never runs more jobs at once than it
has runners, and nothing sits idle.

    python3 priority_runner.py tick --org ORG [--pool shorconsulting-ci] [--label ci-priority] [--dry-run]
    python3 priority_runner.py add  OWNER/REPO#N [--label ci-priority]

`tick` is one pass; run it every minute (launchd or cron) on a machine whose `gh` login may manage the org's runners.
It keeps no state of its own: a lent runner is one that carries the priority label and not the pool's, so a pass
after a crash or a reboot puts things right.

`add` labels the pull request and updates its branch from its base. The new commit matters: a job's runner is chosen
when its run is created, and a re-run repeats the original event, labels included, so re-running a queued pull
request would leave it in the ordinary queue.

The repo's side is one line per test job (docs/policy.md, "Priority runs"):

    runs-on: ${{ contains(github.event.pull_request.labels.*.name, 'ci-priority') && 'ci-priority' || vars.CI_RUNNER }}

shortcut: lends one runner at a time, and picks it without knowing which job will finish first; lend more, or pick
by job age, if priority pull requests start waiting on each other.
"""
import argparse
import json
import subprocess
import sys


def gh(method, path, **fields):
    cmd = ["gh", "api", "-X", method, path]
    for k, v in fields.items():
        cmd += ["-f", f"{k}={v}"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else None


def plan(runners, waiting, pool, label):
    """What to change: [("lend" | "restore", runner)]. `runners` are the org's runners as the API lists them."""
    names = lambda r: {l["name"] for l in r["labels"]}
    lent = [r for r in runners if label in names(r) and pool not in names(r)]
    if not waiting:
        return [("restore", r) for r in lent]
    if any(r["status"] == "online" for r in lent):
        return []
    free = sorted((r for r in runners if pool in names(r) and r["status"] == "online"),
                  key=lambda r: (r["busy"], r["name"]))   # an idle one first: it can start at once
    return [("lend", free[0])] if free else []


def waiting_jobs(org, label, api=gh):
    """How many queued jobs ask for the priority label, over the org's open pull requests that carry it."""
    found = api("GET", "search/issues", q=f"org:{org} is:pr is:open label:{label}", per_page="100")
    count = 0
    for item in found["items"]:
        repo = item["repository_url"].split("/repos/", 1)[1]
        sha = api("GET", f"repos/{repo}/pulls/{item['number']}")["head"]["sha"]
        for run in api("GET", f"repos/{repo}/actions/runs", head_sha=sha, per_page="100")["workflow_runs"]:
            if run["status"] == "completed":
                continue
            jobs = api("GET", f"repos/{repo}/actions/runs/{run['id']}/jobs", per_page="100")["jobs"]
            count += sum(1 for j in jobs if j["status"] == "queued" and label in j.get("labels", []))
    return count


def tick(org, pool, label, dry_run=False, api=gh):
    waiting = waiting_jobs(org, label, api)
    runners = api("GET", f"orgs/{org}/actions/runners", per_page="100")["runners"]
    for what, r in plan(runners, waiting, pool, label):
        add, drop = (label, pool) if what == "lend" else (pool, label)
        print(f"{what} {r['name']}: +{add} -{drop} ({waiting} priority job(s) waiting)")
        if dry_run:
            continue
        # Add before removing: a runner with both labels is still in the pool, one with neither takes nothing.
        api("POST", f"orgs/{org}/actions/runners/{r['id']}/labels", **{"labels[]": add})
        api("DELETE", f"orgs/{org}/actions/runners/{r['id']}/labels/{drop}")


def add(pr, label, api=gh):
    repo, number = pr.split("#")
    api("POST", f"repos/{repo}/issues/{number}/labels", **{"labels[]": label})
    try:
        api("PUT", f"repos/{repo}/pulls/{number}/update-branch")
        print(f"{pr}: labelled {label}; its branch is being updated, and the new run asks for the priority runner")
    except subprocess.CalledProcessError as e:
        print(f"{pr}: labelled {label}, but its branch could not be updated ({e.stderr.strip()}). "
              "Push a commit to it, or close and reopen it: a re-run stays in the ordinary queue.")
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--org", required=True)
    t.add_argument("--pool", default="shorconsulting-ci")
    t.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("add")
    a.add_argument("pr", help="OWNER/REPO#N")
    for s in (t, a):
        s.add_argument("--label", default="ci-priority")
    args = p.parse_args(argv)
    if args.cmd == "add":
        return add(args.pr, args.label)
    try:
        tick(args.org, args.pool, args.label, args.dry_run)
    except subprocess.CalledProcessError as e:
        # Nothing is changed on an error (a rate limit, GitHub down): the next pass decides again.
        print(f"no change: {e.stderr.strip() or e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
