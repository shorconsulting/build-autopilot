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
  - leftover work: fast-builder/wip/* branches with no pull request;
  - CI health (one line per sweep in ~/.build-autopilot/steward-history/): open PRs by state, main's red hours,
    broken runners, the checks failing most, jobs that passed only on a re-run, merges in the last day and the median
    hours from open to merge. Fixed rules over that history (`improvements`) turn what keeps coming back (a check
    failing on many PRs sweep after sweep, a flaky job, a runner still broken, main red, merges getting slower) into
    a fix for the steward to make, so the same red does not come back every hour.

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

    def flaky(self, repo):
        """Jobs that failed and then passed on a re-run of the same commit: the test or the machine is unreliable."""
        out = {}
        for run in self.recent_runs(repo):
            if (run.get("run_attempt") or 1) < 2 or run.get("conclusion") != "success":
                continue
            try:
                prev = api("repos/%s/actions/runs/%s/attempts/%d/jobs?per_page=100"
                           % (repo, run["id"], run["run_attempt"] - 1)) or {}
            except GhError:
                continue
            for job in prev.get("jobs", []):
                if job.get("conclusion") in FAILED:
                    key = "%s / %s" % (run.get("name"), job.get("name"))
                    out[key] = {"step": self.failed_step(job), "count": out.get(key, {}).get("count", 0) + 1}
        return out

    def merged_hours(self, repo, since_hours=24):
        """Hours from open to merge for pull requests merged in the last day (newest 100 closed)."""
        closed = api("repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100" % repo) or []
        out = []
        for p in closed:
            merged = parse_time(p.get("merged_at"))
            if merged and (self.now - merged).total_seconds() <= since_hours * 3600:
                out.append(hours(merged, parse_time(p["created_at"])))
        return sorted(out)

    def health(self, repo, report):
        """One line of CI health for the history: what the steward compares sweep to sweep."""
        failing = {}
        for pr in report["pull_requests"]:
            for name in pr["failed"]:
                failing[name] = failing.get(name, 0) + 1
        merged = self.merged_hours(repo)
        return {"at": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"), "repo": repo,
                "open_prs": len(report["pull_requests"]), "counts": report["counts"],
                "main_state": report["main"]["state"], "main_red_hours": report["main"]["red_hours"],
                "broken_runners": [r["runner"] for r in report["runners"]],
                "failing_checks": dict(sorted(failing.items(), key=lambda kv: -kv[1])[:15]),
                "flaky": self.flaky(repo), "merged_24h": len(merged),
                "median_hours_to_merge": merged[len(merged) // 2] if merged else None}

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
        report = {"repo": repo, "main": self.main_state(repo, branch), "runners": self.runners(repo),
                  "counts": counts, "pull_requests": prs, "leftover_branches": leftover}
        report["health"] = self.health(repo, report)
        return report


HISTORY_DIR = os.path.expanduser("~/.build-autopilot/steward-history")


def history_path(repo, history_dir):
    return os.path.join(history_dir, repo.replace("/", "__") + ".jsonl")


def read_history(repo, history_dir, keep=200):
    try:
        with open(history_path(repo, history_dir), encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()][-keep:]
    except (OSError, ValueError):
        return []


def append_history(health, history_dir):
    os.makedirs(history_dir, exist_ok=True)
    with open(history_path(health["repo"], history_dir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(health) + "\n")


def improvements(history, rules):
    """Fixed rules over the sweep history: problems that keep coming back become fixes, not more re-runs.

    `history` is oldest first and ends with this sweep."""
    imp = rules["improve"]
    window = history[-imp["sweeps"]:]
    if not window:
        return []
    out, now = [], window[-1]
    n = len(window)
    # A check failing on many PRs, sweep after sweep, is broken in a shared way (a slow test, a bad fixture).
    for name, count in now.get("failing_checks", {}).items():
        seen = sum(1 for h in window if h.get("failing_checks", {}).get(name, 0) >= imp["recurring_check_prs"])
        if count >= imp["recurring_check_prs"] and seen >= min(n, imp["recurring_sweeps"]):
            out.append({"kind": "harden-check", "check": name, "evidence":
                        "fails on %d open PRs now, and on %d+ PRs in %d of the last %d sweeps"
                        % (count, imp["recurring_check_prs"], seen, n)})
    # A job that only passes on a re-run is flaky: fix the cause (timing, shared state, a slow machine).
    flaky = {}
    for h in window:
        for key, f in (h.get("flaky") or {}).items():
            flaky.setdefault(key, {"count": 0, "step": f.get("step")})["count"] += f.get("count", 1)
    for key, f in sorted(flaky.items()):
        if f["count"] >= imp["flaky_reruns"]:
            out.append({"kind": "fix-flaky", "check": key, "step": f["step"], "evidence":
                        "passed only on a re-run %d times in the last %d sweeps" % (f["count"], n)})
    # A runner still broken after a sweep has tried to repair it.
    for r in now.get("broken_runners", []):
        seen = sum(1 for h in window if r in h.get("broken_runners", []))
        if seen >= 2:
            out.append({"kind": "repair-runner", "runner": r, "evidence": "broken in %d of the last %d sweeps"
                        % (seen, n)})
    # Main red across sweeps, or getting slower to merge: make it the steward lane's first item.
    if (now.get("main_red_hours") or 0) > rules["stall"]["main_red_hours"]:
        out.append({"kind": "fix-main-first", "evidence": "main red for %.0f hours" % now["main_red_hours"]})
    meds = [h["median_hours_to_merge"] for h in window[:-1] if h.get("median_hours_to_merge") is not None]
    cur = now.get("median_hours_to_merge")
    if meds and cur is not None:
        base = sorted(meds)[len(meds) // 2]
        if base and cur > base * (1 + imp["slower_merge_pct"] / 100.0):
            out.append({"kind": "merge-slowing", "evidence": "median open-to-merge %.1f h now vs %.1f h over the "
                        "last %d sweeps" % (cur, base, len(meds))})
    return out


def actions_for(report, rules):
    """Turn one repo's sweep into the steward's to-do list, most urgent first."""
    acts = []
    repo = report["repo"]
    for r in report["runners"]:
        acts.append({"do": "fix-runner", "repo": repo, "runner": r["runner"], "needs_person": False,
                     "why": "%d jobs failed at '%s' on this runner and none passed: every PR it picks up goes red. "
                            "Pull it from the pool (remove its custom label), repair it, put it back once a job "
                            "passes, and re-run what it failed."
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
    # Improvements go right after the repairs: they stop the same red from coming back next hour.
    imps = [dict(i, do="improve", repo=repo) for i in report.get("improvements", [])]
    head = [a for a in acts if a["do"] in ("fix-runner", "fix-main")]
    return head + imps + [a for a in acts if a not in head]


def cmd_sweep(args):
    rules = load_rules(args.rules)
    sw = Sweeper(rules)
    repos = args.repo or [current_repo()]
    out = {"generated_at": sw.now.strftime("%Y-%m-%dT%H:%M:%SZ"), "repos": [], "errors": []}
    for repo in repos:
        try:
            rep = sw.sweep_repo(repo)
            if not args.no_history:
                append_history(rep["health"], args.history_dir)
            rep["improvements"] = improvements(read_history(repo, args.history_dir) if not args.no_history
                                               else [rep["health"]], rules)
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
        lines.append("%s: %d open PRs (%s); main %s; %d broken runner(s); %d leftover branch(es); "
                     "%d improvement(s); %d actions" % (
                         rep["repo"], len(rep["pull_requests"]), ", ".join("%s %d" % kv for kv in sorted(c.items())),
                         rep["main"]["state"], len(rep["runners"]), len(rep["leftover_branches"]),
                         len(rep.get("improvements", [])), len(rep["actions"])))
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
    s.add_argument("--history-dir", default=HISTORY_DIR)
    s.add_argument("--no-history", action="store_true", help="don't record this sweep")
    h = sub.add_parser("health", help="the CI health history for a repo, and what it says to improve")
    h.add_argument("--repo", required=True)
    h.add_argument("--history-dir", default=HISTORY_DIR)
    sub.add_parser("rules")
    sub.add_parser("session-note")
    args = ap.parse_args(argv)
    if args.cmd == "sweep":
        return cmd_sweep(args)
    if args.cmd == "health":
        hist = read_history(args.repo, args.history_dir)
        print(json.dumps({"sweeps": len(hist), "latest": hist[-1] if hist else None,
                          "improvements": improvements(hist, load_rules(args.rules))}, indent=2))
        return 0
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
