#!/usr/bin/env python3
"""The always-on steward's sweep: find unfinished build work and say what to do with each piece.

Read-only. It never re-runs, merges, pushes or comments; the steward command acts on its output.

    python3 steward.py sweep --repo owner/name [--repo ...] [--out sweep.json]
    python3 steward.py rules                 # the global build rules (rules/build-rules.json), as JSON
    python3 steward.py session-note          # SessionStart hook: a short reminder of the global rules

What a sweep finds, per repo:
  - main: the default branch's checks (green / red / pending) and, if red, since when;
  - broken runners: a self-hosted runner whose jobs fail at checkout or setup again and again, with none passing;
  - every open pull request, sorted into one state:
        red-infra   failed only at checkout / setup / cancelled: re-run it, don't fix code
        red         a real check failed: hand it back to its lane (or the fixer lane) to fix
        stalled     a check queued or running far past normal: cancel and re-run
        conflict    can't merge into its base: rebase it
        green       every check passed but it is still open with nothing set to merge it
        pending     checks still running normally
        draft       a draft nobody has touched for `idle_hours`
        stacked     based on another open pull request's branch (lands after its base)
  - leftover work: fast-builder/wip/* branches with no pull request.

GitHub is read through REST only (`gh api`, pages fetched by hand), so it works in cloud sessions, which refuse GraphQL.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(os.path.dirname(HERE), "rules", "build-rules.json")
FAILED = ("failure", "timed_out", "startup_failure", "action_required")
# A job that failed in one of these steps never ran the code under test: the machine, not the change, is at fault.
INFRA_STEP = re.compile(r"checkout|set up job|setup|set-up|download|cache|runner|clone|fetch", re.I)


class GhError(RuntimeError):
    pass


def gh(args):
    """Run `gh <args>`; the only function that touches the network (tests replace it)."""
    try:
        res = subprocess.run(["gh"] + list(args), capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise GhError(str(exc))
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "gh failed").strip().splitlines()
        raise GhError(err[-1] if err else "gh failed")
    return res.stdout


def api(path):
    out = gh(["api", path])
    return json.loads(out) if out.strip() else None


def paged(path, key=None, cap=1000, per_page=100):
    """GET a list endpoint page by page (the proxy refuses gh's --paginate links)."""
    items, page = [], 1
    sep = "&" if "?" in path else "?"
    while len(items) < cap:
        data = api("%s%sper_page=%d&page=%d" % (path, sep, per_page, page))
        batch = (data or {}).get(key, []) if key else (data or [])
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items[:cap]


def load_rules(path=RULES_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def hours(a, b):
    return round((a - b).total_seconds() / 3600, 1) if a and b else None


def job_id(run):
    """A GitHub Actions check run's id is its job id; other apps' check runs have no job."""
    return run.get("id") if "/actions/" in (run.get("details_url") or "") else None


class Sweeper:
    def __init__(self, rules, now=None):
        self.rules = rules
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.jobs = {}  # job id -> job (cached; a sweep reads each job once)
        self._runs = {}  # repo -> recent workflow runs

    def job(self, repo, jid):
        if jid not in self.jobs:
            try:
                self.jobs[jid] = api("repos/%s/actions/jobs/%s" % (repo, jid)) or {}
            except GhError:
                self.jobs[jid] = {}
        return self.jobs[jid]

    def failed_step(self, job):
        for step in job.get("steps") or []:
            if step.get("conclusion") in FAILED:
                return step.get("name") or ""
        return ""

    def is_infra(self, repo, run):
        """True when a failed check never got to the code: a checkout/setup step failed, or it was cancelled."""
        if run.get("conclusion") == "cancelled":
            return True
        jid = job_id(run)
        if not jid:
            return False
        job = self.job(repo, jid)
        step = self.failed_step(job)
        return bool(step) and bool(INFRA_STEP.search(step))

    def checks(self, repo, sha, classify=True):
        runs = (api("repos/%s/commits/%s/check-runs?per_page=100" % (repo, sha)) or {}).get("check_runs", [])
        stall = self.rules["stall"]
        out = {"total": len(runs), "failed": [], "infra": [], "stalled": [], "pending": 0}
        for r in runs:
            if r.get("status") != "completed":
                started = parse_time(r.get("started_at"))
                age_min = (self.now - started).total_seconds() / 60 if started else None
                limit = stall["check_queued_minutes"] if r.get("status") == "queued" else stall["check_running_minutes"]
                if age_min is not None and age_min > limit:
                    out["stalled"].append({"name": r.get("name"), "status": r.get("status"), "minutes": int(age_min)})
                else:
                    out["pending"] += 1
                continue
            if r.get("conclusion") in FAILED or r.get("conclusion") == "cancelled":
                entry = {"name": r.get("name"), "conclusion": r.get("conclusion"), "job": job_id(r)}
                if classify and self.is_infra(repo, r):
                    out["infra"].append(entry)
                elif r.get("conclusion") != "cancelled":
                    out["failed"].append(entry)
        return out

    def pr_state(self, pr, chk, open_heads):
        if pr.get("draft"):
            return "draft"
        if pr["base"]["ref"] in open_heads:
            return "stacked"
        if chk["infra"]:
            return "red-infra"  # re-run the machine failures first: they often take the downstream failures with them
        if chk["failed"]:
            return "red"
        if chk["stalled"]:
            return "stalled"
        if pr.get("mergeable_state") == "dirty":
            return "conflict"
        if chk["pending"]:
            return "pending"
        if pr.get("auto_merge"):
            return "pending"  # green with auto-merge on: GitHub lands it
        return "green"

    def recent_runs(self, repo, per_workflow=3):
        """The latest few completed runs of each workflow (one busy workflow must not hide the others)."""
        if repo not in self._runs:
            runs = (api("repos/%s/actions/runs?status=completed&per_page=100" % repo) or {}).get("workflow_runs", [])
            seen, keep = {}, []
            for run in runs:
                n = seen[run.get("name")] = seen.get(run.get("name"), 0) + 1
                if n <= per_workflow:
                    keep.append(run)
            self._runs[repo] = keep
        return self._runs[repo]

    def runners(self, repo):
        """Self-hosted runners failing at checkout/setup with no successes, from recent runs and the PR jobs read."""
        for run in self.recent_runs(repo):
            for job in (api("repos/%s/actions/runs/%s/jobs?per_page=100" % (repo, run["id"])) or {}).get("jobs", []):
                self.jobs.setdefault(job.get("id"), job)
        per = {}
        for job in self.jobs.values():
            name = job.get("runner_name")
            if not name:
                continue
            tally = per.setdefault(name, {"infra_failures": 0, "successes": 0, "last_seen": None})
            if job.get("conclusion") == "success":
                tally["successes"] += 1
            elif job.get("conclusion") in FAILED and INFRA_STEP.search(self.failed_step(job)):
                tally["infra_failures"] += 1
                tally["sample_step"] = self.failed_step(job)
            tally["last_seen"] = max(filter(None, [tally["last_seen"], job.get("completed_at")]), default=None)
        need = self.rules["stall"]["broken_runner_failures"]
        return [dict(runner=n, **t) for n, t in sorted(per.items())
                if t["infra_failures"] >= need and t["successes"] == 0]

    def main_state(self, repo, branch):
        """The default branch is red when the latest push run of any workflow on it failed. Check runs on main's
        commit are not used: review and retry bots attach their own results there."""
        runs = (api("repos/%s/actions/runs?branch=%s&event=push&status=completed&per_page=100" % (repo, branch))
                or {}).get("workflow_runs", [])
        latest, history = {}, {}
        for run in runs:  # newest first
            if run.get("conclusion") in ("cancelled", "skipped"):
                continue
            latest.setdefault(run.get("name"), run)
            history.setdefault(run.get("name"), []).append(run)
        failed = sorted(n for n, r in latest.items() if r.get("conclusion") in FAILED)
        red_since = None
        for name in failed:  # the oldest failure in each red workflow's newest unbroken run of failures
            for run in history[name]:
                if run.get("conclusion") == "success":
                    break
                if red_since is None or run["created_at"] < red_since:
                    red_since = run["created_at"]
        state = "red" if failed else ("green" if latest else "unknown")
        return {"branch": branch, "state": state, "failed": failed, "red_since": red_since,
                "red_hours": hours(self.now, parse_time(red_since))}

    def sweep_repo(self, repo):
        info = api("repos/%s" % repo) or {}
        branch = info.get("default_branch") or "main"
        pulls = paged("repos/%s/pulls?state=open" % repo)
        open_heads = {p["head"]["ref"] for p in pulls}
        cap = self.rules["steward"]["max_prs_classified"]
        idle_limit = self.rules["steward"]["idle_hours"]
        prs, counts = [], {}
        # Oldest first: the work that has waited longest is the work most likely to be forgotten.
        for i, p in enumerate(sorted(pulls, key=lambda p: p["created_at"])):
            detail = api("repos/%s/pulls/%s" % (repo, p["number"])) if i < cap else p
            chk = self.checks(repo, p["head"]["sha"], classify=i < cap)
            state = self.pr_state(dict(p, mergeable_state=(detail or {}).get("mergeable_state")), chk, open_heads)
            idle = hours(self.now, parse_time(p.get("updated_at")))
            if state == "draft" and (idle or 0) < idle_limit:
                state = "draft-active"
            counts[state] = counts.get(state, 0) + 1
            prs.append({"number": p["number"], "title": p.get("title"), "branch": p["head"]["ref"],
                        "base": p["base"]["ref"], "state": state, "idle_hours": idle,
                        "labels": [l["name"] for l in p.get("labels") or []],
                        "lane": next((l["name"][5:] for l in p.get("labels") or [] if l["name"].startswith("lane:")),
                                     None),
                        "failed": [f["name"] for f in chk["failed"]], "infra": [f["name"] for f in chk["infra"]],
                        "stalled": chk["stalled"], "classified": i < cap})
        try:
            wip = api("repos/%s/git/matching-refs/heads/fast-builder/wip/" % repo) or []
        except GhError:
            wip = []
        wip_heads = [w["ref"][len("refs/heads/"):] for w in wip]
        leftover = [b for b in wip_heads if b not in open_heads]
        return {"repo": repo, "main": self.main_state(repo, branch), "runners": self.runners(repo),
                "counts": counts, "pull_requests": prs, "leftover_branches": leftover}


def actions_for(report, rules):
    """Turn one repo's sweep into the steward's to-do list, most urgent first."""
    acts = []
    repo = report["repo"]
    for r in report["runners"]:
        acts.append({"do": "fix-runner", "repo": repo, "runner": r["runner"], "needs_person": True,
                     "why": "%d jobs failed at '%s' on this runner and none passed: every PR it picks up goes red. "
                            "Take it offline (or wipe its work folder) and re-run what it failed."
                            % (r["infra_failures"], r.get("sample_step", "setup"))})
    main = report["main"]
    if main["state"] == "red":
        acts.append({"do": "fix-main", "repo": repo, "lane": "steward",
                     "needs_person": (main["red_hours"] or 0) > rules["stall"]["main_red_hours"],
                     "why": "main is red (%s)%s; nothing else lands until it is green"
                            % (", ".join(main["failed"]) or "checks failed",
                               " for %.0f hours" % main["red_hours"] if main["red_hours"] else "")})
    verbs = {"red-infra": "rerun", "stalled": "restart-checks", "red": "fix", "conflict": "rebase",
             "green": "land", "draft": "resume"}
    order = ["red-infra", "stalled", "red", "conflict", "green", "draft"]
    for state in order:
        for pr in report["pull_requests"]:
            if pr["state"] == state:
                acts.append({"do": verbs[state], "repo": repo, "pr": pr["number"], "lane": pr["lane"],
                             "checks": pr["failed"] or pr["infra"] or [s["name"] for s in pr["stalled"]]})
    for b in report["leftover_branches"]:
        acts.append({"do": "resume-branch", "repo": repo, "branch": b})
    return acts


def cmd_sweep(args):
    rules = load_rules(args.rules)
    sw = Sweeper(rules)
    repos = args.repo or [current_repo()]
    out = {"generated_at": sw.now.strftime("%Y-%m-%dT%H:%M:%SZ"), "repos": [], "errors": []}
    for repo in repos:
        try:
            rep = sw.sweep_repo(repo)
            rep["actions"] = actions_for(rep, rules)
            out["repos"].append(rep)
        except GhError as exc:
            out["errors"].append({"repo": repo, "error": str(exc)})
    text = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(summary(out))
    else:
        print(text)
    return 0


def summary(out):
    lines = []
    for rep in out["repos"]:
        c = rep["counts"]
        lines.append("%s: %d open PRs (%s); main %s; %d broken runner(s); %d leftover branch(es); %d actions" % (
            rep["repo"], len(rep["pull_requests"]), ", ".join("%s %d" % kv for kv in sorted(c.items())),
            rep["main"]["state"], len(rep["runners"]), len(rep["leftover_branches"]), len(rep["actions"])))
    for e in out["errors"]:
        lines.append("%s: skipped (%s)" % (e["repo"], e["error"]))
    return "\n".join(lines)


def current_repo():
    url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    if not m:
        sys.exit("not in a GitHub checkout: pass --repo owner/name")
    return m.group(1)


def session_note(rules):
    d = rules["questions"]["digest"]
    return ("Build Autopilot global build rules (every build, every repo; the build-rules skill has the detail): "
            "1) product questions go in the build-questions list, never to chat; the person hears about open ones "
            "%s. 2) a lane is not done until every PR it opened is merged and green: keep driving CI, don't stop "
            "after the first attempt. 3) a stalled check or lane is restarted, not waited on. 4) a lane that runs "
            "out of work asks the steward for the next batch before it finishes." % d["label"].lower())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rules", default=RULES_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sweep")
    s.add_argument("--repo", action="append")
    s.add_argument("--out")
    sub.add_parser("rules")
    sub.add_parser("session-note")
    args = ap.parse_args(argv)
    if args.cmd == "sweep":
        return cmd_sweep(args)
    if args.cmd == "rules":
        print(json.dumps(load_rules(args.rules), indent=2))
        return 0
    try:  # a hook: never fail the session over a reminder
        print(session_note(load_rules(args.rules)))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        pass
