#!/usr/bin/env python3
"""The always-on steward's sweep: find unfinished build work and say what to do with each piece.

Read-only. It never re-runs, merges, pushes or comments; the steward command acts on its output.

    python3 steward.py sweep --repo owner/name [--repo ...] [--out sweep.json]
    python3 steward.py capacity --repo owner/name [--out capacity.json]   # idle/offline runners, stale queued runs
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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(os.path.dirname(HERE), "rules", "build-rules.json")
FAILED = ("failure", "timed_out", "startup_failure", "action_required")
# A job that failed in one of these steps never ran the code under test: the machine, not the change, is at fault.
INFRA_STEP = re.compile(r"checkout|set up job|setup|set-up|download|cache|runner|clone|fetch", re.I)
# An unnamed script step is called "Run <first line of the script>", and that line is often a comment about the
# machine ("# Self-hosted since #2654: six Linux runner containers ..."), so the words above say nothing about it.
# Such a step is the machine's only when it runs an action that prepares the job or a plain git fetch or clone.
INFRA_RUN_STEP = re.compile(r"^Run (actions/(checkout|cache(/\w+)?|download-artifact)@|[\w.-]+/(setup-[\w-]+|[\w-]+-setup|action-setup|setup)@|git (fetch|clone)\b)", re.I)


def infra_step(step):
    """True when a failed step's name says it never got to the code under test (checkout, setup, a download)."""
    if not step:
        return False
    if step.startswith("Run "):
        return bool(INFRA_RUN_STEP.search(step))
    return bool(INFRA_STEP.search(step))


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
    """The rules file; a custom one without a `capacity` block (or part of it) gets the built-in defaults."""
    with open(path, encoding="utf-8") as fh:
        rules = json.load(fh)
    if os.path.abspath(path) != os.path.abspath(RULES_PATH):
        with open(RULES_PATH, encoding="utf-8") as fh:
            base = json.load(fh).get("capacity") or {}
        cap = dict(base, **(rules.get("capacity") or {}))
        cap["host"] = dict(base.get("host") or {}, **((rules.get("capacity") or {}).get("host") or {}))
        rules["capacity"] = cap
    return rules


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def hours(a, b):
    return round((a - b).total_seconds() / 3600, 1) if a and b else None


def job_id(run):
    """A GitHub Actions check run's id is its job id; other apps' check runs have no job."""
    return run.get("id") if "/actions/" in (run.get("details_url") or "") else None


class Sweeper:
    def __init__(self, rules, now=None, lite=False):
        self.rules = rules
        # lite: repairs only (re-run machine failures, restart hung checks), for an unattended timer that shares the
        # token owner's 5,000-calls-an-hour limit. Skips the runner, flaky and merge-time scans and the per-PR detail
        # read, and reads only the most recently updated PRs.
        self.lite = lite
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
        return infra_step(step)

    def checks(self, repo, sha, classify=True):
        runs = (api("repos/%s/commits/%s/check-runs?per_page=100" % (repo, sha)) or {}).get("check_runs", [])
        stall = self.rules["stall"]
        out = {"total": len(runs), "failed": [], "infra": [], "stalled": [], "pending": 0}
        # A check cancelled because a later run of it took over (a duplicate trigger, a concurrency group) is not a
        # failure: the later one decides. Only a cancel nothing of that name started after is a machine failure.
        later = {}
        for r in runs:
            if not (r.get("status") == "completed" and r.get("conclusion") == "cancelled"):
                later[r.get("name")] = max(later.get(r.get("name")) or "", r.get("started_at") or "")

        def superseded(r):
            return r.get("name") in later and later[r["name"]] > (r.get("started_at") or "")
        for r in runs:
            if r.get("status") != "completed":
                started = parse_time(r.get("started_at"))
                age_min = (self.now - started).total_seconds() / 60 if started else None
                limit = stall["check_queued_minutes"] if r.get("status") == "queued" else stall["check_running_minutes"]
                if age_min is not None and age_min > limit:
                    entry = {"name": r.get("name"), "status": r.get("status"), "minutes": int(age_min),
                             "job": job_id(r)}
                    if classify and entry["job"] and r.get("status") == "in_progress":
                        j = self.job(repo, entry["job"])
                        entry.update(run_id=j.get("run_id"), attempt=j.get("run_attempt"))
                    out["stalled"].append(entry)
                else:
                    out["pending"] += 1
                continue
            if r.get("conclusion") == "cancelled" and superseded(r):
                continue
            if r.get("conclusion") in FAILED or r.get("conclusion") == "cancelled":
                entry = {"name": r.get("name"), "conclusion": r.get("conclusion"), "job": job_id(r)}
                if classify and self.is_infra(repo, r):
                    j = self.job(repo, entry["job"]) if entry["job"] else {}
                    entry.update(run_id=j.get("run_id"), attempt=j.get("run_attempt"))
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
            elif job.get("conclusion") in FAILED and infra_step(self.failed_step(job)):
                tally["infra_failures"] += 1
                tally["sample_step"] = self.failed_step(job)
            tally["last_seen"] = max(filter(None, [tally["last_seen"], job.get("completed_at")]), default=None)
        need = self.rules["stall"]["broken_runner_failures"]
        return [dict(runner=n, **t) for n, t in sorted(per.items())
                if t["infra_failures"] >= need and t["successes"] == 0]

    def main_state(self, repo, branch):
        """The default branch is red when the latest push run of any workflow on it failed. Check runs on main's
        commit are not used: review and retry bots attach their own results there.

        Only workflows that still exist and are enabled count. A workflow file that was deleted, disabled or never
        parsed (GitHub records a nameless "startup_failure" run for a broken file, then marks the workflow deleted)
        never runs again, so its last failure would otherwise keep main red forever."""
        runs = (api("repos/%s/actions/runs?branch=%s&event=push&status=completed&per_page=100" % (repo, branch))
                or {}).get("workflow_runs", [])
        workflows = (api("repos/%s/actions/workflows?per_page=100" % repo) or {}).get("workflows") or []
        active = {w.get("id") for w in workflows if w.get("state") == "active"}
        latest, history = {}, {}
        for run in runs:  # newest first
            if run.get("conclusion") in ("cancelled", "skipped"):
                continue
            if active and run.get("workflow_id") is not None and run["workflow_id"] not in active:
                continue  # its workflow is gone or switched off: it cannot go green again
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
        merged = [] if self.lite else self.merged_hours(repo)
        return {"at": self.now.strftime("%Y-%m-%dT%H:%M:%SZ"), "repo": repo,
                "open_prs": len(report["pull_requests"]), "counts": report["counts"],
                "main_state": report["main"]["state"], "main_red_hours": report["main"]["red_hours"],
                "broken_runners": [r["runner"] for r in report["runners"]],
                "failing_checks": dict(sorted(failing.items(), key=lambda kv: -kv[1])[:15]),
                "flaky": {} if self.lite else self.flaky(repo), "merged_24h": len(merged),
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
        order = sorted(pulls, key=lambda p: p["created_at"])
        if self.lite:  # the PRs being worked on now, and nothing past the cap
            order = sorted(pulls, key=lambda p: p.get("updated_at") or "", reverse=True)[:cap]
        for i, p in enumerate(order):
            detail = api("repos/%s/pulls/%s" % (repo, p["number"])) if i < cap and not self.lite else p
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
                        "infra_runs": [{k: f.get(k) for k in ("name", "run_id", "attempt")} for f in chk["infra"]],
                        "stalled": chk["stalled"], "classified": i < cap})
        try:
            wip = api("repos/%s/git/matching-refs/heads/fast-builder/wip/" % repo) or []
        except GhError:
            wip = []
        wip_heads = [w["ref"][len("refs/heads/"):] for w in wip]
        leftover = [b for b in wip_heads if b not in open_heads]
        report = {"repo": repo, "main": self.main_state(repo, branch),
                  "runners": [] if self.lite else self.runners(repo),
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


# ---------------------------------------------------------------- capacity (every few minutes, not hourly)
# The hourly sweep looks at pull requests. It misses the commonest way throughput dies: runners sitting idle while
# hundreds of jobs queue, because the queued jobs ask for labels the idle runners don't have, the runners for those
# labels are offline, or the queue is full of runs nobody needs (closed pull requests, superseded heads, duplicates).
# GitHub never moves a job to a runner whose labels don't match, so nothing reroutes by itself.
# `capacity` finds all three. Cancelling unneeded runs is safe and reversible (a run can be re-run); the rest are
# actions for the steward to take through the person's computer.

PR_EVENTS = ("pull_request", "pull_request_target", "pull_request_review", "workflow_run")
# Runs that must still happen after their pull request closed: never cancelled as stale. Extend with
# `capacity.keep_after_close` in build-rules.json.
KEEP_AFTER_CLOSE = r"mark.*done|release|deploy|publish|cleanup|clean-up|teardown|backport|changelog|notify|preview"
# GitHub-hosted labels: queued jobs asking for these are GitHub's to start, never a self-hosted pool's.
HOSTED = re.compile(r"^(ubuntu|windows|macos)-[\w.-]+$", re.I)


def _keep(rules):
    extra = (rules or {}).get("capacity", {}).get("keep_after_close") or []
    return re.compile("|".join([KEEP_AFTER_CLOSE] + list(extra)), re.I)


def _head_key(repo_full, ref):
    return ((repo_full or "").lower(), ref)


def _dedupe(rules, repo):
    names = set()
    for w in (rules or {}).get("capacity", {}).get("dedupe_workflows") or []:
        if ":" in w:
            r, n = w.split(":", 1)
            if r.lower() == (repo or "").lower():
                names.add(n)
        else:
            names.add(w)
    return names


def stale_runs(queued, open_prs, keep=None, closed_at=None, dedupe=None):
    """Queued runs nobody needs. Returns [(run_id, reason, workflow)]:
      closed      no open pull request has this head (repo and branch), the workflow is pull-request CI (it also has
                  queued runs for an open pull request), AND the run was created before its pull request closed
                  (`closed_at`: {(head repo, branch): ISO time}). A run the close itself started (post-merge
                  bookkeeping on `closed`) is created after it, so it is kept; with no close time known it is kept.
      superseded  an open pull request has this head branch but its head is another commit, the workflow is in
                  `dedupe` (CI that does the same thing whatever started it) and a run of that same workflow is
                  already queued for the new head. A run a label, a review or another workflow started, of any
                  other workflow, is not started again by a push, so it is kept;
      duplicate   an older queued run of the same workflow file (workflow_id), event, head and pull request, only for
                  workflows in `dedupe` (`capacity.dedupe_workflows`): ones known to do the same thing whatever
                  action or label started them (CI over the head). Others may branch on the event, so never.
    `queued` is GitHub's workflow_runs; `open_prs` the open pulls."""
    closed_at = closed_at or {}
    dedupe = dedupe if dedupe is not None else {"CI", "test"}
    keep = keep or _keep(None)
    heads, open_shas = {}, set()
    for p in open_prs:
        h = p.get("head") or {}
        heads[_head_key((h.get("repo") or {}).get("full_name"), h.get("ref"))] = h.get("sha")
        open_shas.add(h.get("sha"))

    def key(r):
        return _head_key((r.get("head_repository") or {}).get("full_name"), r.get("head_branch"))

    def pr_run(r):
        return r.get("event") in PR_EVENTS and not keep.search(r.get("name") or "")

    pr_ci = {r.get("name") for r in queued if pr_run(r) and r.get("head_sha") in open_shas}
    on_head = {(r.get("workflow_id") or r.get("name"), r.get("head_sha")) for r in queued
               if pr_run(r) and r.get("head_sha") in open_shas}
    out, newest = [], {}
    for r in queued:
        if not pr_run(r):
            continue
        sha, k = r.get("head_sha"), key(r)
        if sha in open_shas:
            if r.get("name") not in dedupe:
                continue
            dkey = (r.get("workflow_id") or r.get("name"), r.get("event"), sha,
                    tuple(sorted(p.get("number") for p in r.get("pull_requests") or [])))
            prev = newest.get(dkey)
            if prev is None or r["id"] > prev:
                if prev is not None:
                    out.append((prev, "duplicate", r.get("name")))
                newest[dkey] = r["id"]
            else:
                out.append((r["id"], "duplicate", r.get("name")))
        elif k in heads:
            if (r.get("event") != "workflow_run" and r.get("name") in dedupe
                    and (r.get("workflow_id") or r.get("name"), heads[k]) in on_head):
                out.append((r["id"], "superseded", r.get("name")))
        elif r.get("event") != "workflow_run" and r.get("name") in pr_ci:
            closed = parse_time(closed_at.get(k))
            made = parse_time(r.get("created_at"))
            if closed and made and made < closed:
                out.append((r["id"], "closed", r.get("name")))
    return out


def stacked_workflows(queued, open_prs, min_runs=50, per_pr=2.0):
    """Workflows piling up queued runs for the same open pull requests: more than `per_pr` queued runs per pull
    request on average, and at least `min_runs` in all. That is a workflow with no per-PR concurrency group whose
    jobs start on every label, review or push (HPortHealth/app's Auto-merge `disarm`, 2026-11-20: ~2,500 queued,
    PRs stopped merging). They can't be cancelled safely here, because each run may act on its own event, so
    they become an `improve` item: give the workflow a per-PR concurrency group or start its jobs only on the
    events they act on. Returns [{"workflow", "queued", "prs", "per_pr"}], most queued first."""
    open_shas = {sha for p in open_prs if (sha := (p.get("head") or {}).get("sha"))}
    runs, prs = {}, {}
    for r in queued:
        if r.get("event") not in PR_EVENTS or r.get("head_sha") not in open_shas:
            continue
        name = r.get("name") or str(r.get("workflow_id"))
        runs[name] = runs.get(name, 0) + 1
        prs.setdefault(name, set()).add(r.get("head_sha"))
    out = []
    for name, n in runs.items():
        k = len(prs[name])
        if n >= min_runs and k and n / k > per_pr:
            out.append({"workflow": name, "queued": n, "prs": k, "per_pr": round(n / k, 1)})
    return sorted(out, key=lambda x: -x["queued"])


def fits(job_labels, runner_labels):
    """GitHub's rule: a runner can take a job only if it has every label the job asks for."""
    have = {l.lower() for l in runner_labels}
    return all(l.lower() in have for l in job_labels)


def self_hosted(labels):
    """A label set is a self-hosted pool unless every label is a GitHub-hosted image (`runs-on: hph-ci` with no
    `self-hosted` is still self-hosted)."""
    return bool(labels) and not all(HOSTED.match(l) for l in labels)


def pool_report(runners, queued_labels, min_queue, oldest_minutes=None, settle_minutes=10, paused=()):
    """Per self-hosted label set asked for by queued jobs: how many runners could take it (online, idle,
    offline), and the findings. `runners`: GitHub's runner objects (org and repo level); `queued_labels`:
    {tuple(labels): count of queued jobs}; `oldest_minutes`: {tuple(labels): minutes the oldest such job has
    queued}. A momentary state is not a finding: idle-but-queued needs a job queued over `settle_minutes`
    (a runner that just finished a job looks idle for a moment), and offline runners are only candidates
    (`settle` in cmd_capacity requires two passes in a row before acting)."""
    oldest_minutes = oldest_minutes or {}
    # Slots this check paused for host pressure are offline on purpose: never "offline", "pool down" or restarted.
    paused = {n.lower() for n in paused}
    runners = [r for r in runners if (r.get("name") or "").lower() not in paused]

    def labels(r):
        return [l["name"] for l in r.get("labels") or []]
    pools, findings = [], []
    used = set()
    for want, n in sorted(queued_labels.items(), key=lambda kv: -kv[1]):
        if not self_hosted(want):
            continue  # GitHub-hosted: not ours to restore
        able = [r for r in runners if fits(want, labels(r))]
        online = [r for r in able if r.get("status") == "online"]
        idle = [r for r in online if not r.get("busy")]
        offline = [r for r in able if r.get("status") != "online"]
        used.update(r["name"] for r in able)
        waited = oldest_minutes.get(want)
        pools.append({"labels": list(want), "queued_jobs": n, "runners": len(able), "online": len(online),
                      "idle": len(idle), "offline": [r["name"] for r in offline], "oldest_minutes": waited})
        if n and not able:
            findings.append({"kind": "no-runner-for-labels", "labels": list(want), "queued_jobs": n})
        elif n and not online:
            findings.append({"kind": "pool-down", "labels": list(want), "queued_jobs": n,
                             "offline": [r["name"] for r in offline]})
        elif n and idle and waited is not None and waited >= settle_minutes:
            # Jobs queued for minutes while matching runners are idle: GitHub hands work out within seconds, so the
            # runners are hung or deregistered behind a stale "online".
            findings.append({"kind": "idle-but-queued", "labels": list(want), "queued_jobs": n,
                             "idle": [r["name"] for r in idle], "oldest_minutes": waited})
        for r in offline:
            findings.append({"kind": "runner-offline", "runner": r["name"], "id": r.get("id"),
                             "labels": labels(r)})
    # Online runners no queued job can use, while a pool is backed up: capacity the workflows can't reach.
    backed_up = [p for p in pools if p["queued_jobs"] >= min_queue and p["idle"] == 0]
    spare = [r for r in runners if r.get("status") == "online" and not r.get("busy") and r["name"] not in used]
    if backed_up and spare:
        findings.append({"kind": "idle-capacity-unreachable",
                         "idle": [{"runner": r["name"], "labels": labels(r)} for r in spare],
                         "backed_up": [{"labels": p["labels"], "queued_jobs": p["queued_jobs"]} for p in backed_up]})
    seen, uniq = set(), []
    for f in findings:  # a runner offline in two pools is one finding
        k = json.dumps(f, sort_keys=True)
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    return pools, uniq


RESTART_KINDS = ("runner-offline", "idle-but-queued", "pool-down")


def restart_targets(f):
    """The runner names a restart finding would restart."""
    if f["kind"] == "runner-offline":
        return [f["runner"]]
    if f["kind"] == "pool-down":
        return list(f.get("offline") or [])
    return [x["runner"] if isinstance(x, dict) else x for x in f.get("idle") or []]


def settle(findings, state, now, cooldown_minutes, max_restarts=3, report_cooldown_minutes=240, online=None):
    """Act on a runner only when the finding held on the previous pass too, and not again within the cooldown.
    Restarts are counted PER RUNNER across every finding kind, so `max_restarts` caps a runner's restarts in all,
    and a runner a `pool-down` finding covers is not also restarted as `runner-offline` in the same pass.
    `state` (kept between passes): seen {finding key: first seen}, acted {key: last acted}, restarts
    {"r|runner": count}, last_seen {key: last seen}. Returns the findings to act on; updates `state` in place.
    `online`: names of runners seen online and not hung this pass, whose restart history is cleared at once."""
    def names(f):
        if f["kind"] == "idle-capacity-unreachable":
            # Keyed on what is backed up, not on which runners happen to be idle this pass.
            return ";".join(sorted(",".join(b["labels"]) for b in f.get("backed_up") or []))
        if f["kind"] in RESTART_KINDS:
            return ",".join(sorted(restart_targets(f))) or ",".join(f.get("labels") or [])
        return f.get("runner") or ",".join(f.get("labels") or [])

    covered = {n for f in findings if f["kind"] == "pool-down" for n in restart_targets(f)}
    findings = [f for f in findings if not (f["kind"] == "runner-offline" and f["runner"] in covered)]
    prev = state.get("seen", {})
    acted = state.setdefault("acted", {})
    tries = state.setdefault("restarts", {})
    last_seen = state.setdefault("last_seen", {})
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    keep, seen, done = [], {}, set()
    for f in findings:
        key = "%s|%s" % (f["kind"], names(f))
        seen[key] = prev.get(key) or stamp
        last_seen[key] = stamp
        if f["kind"] in ("idle-capacity-unreachable", "no-runner-for-labels"):
            # A reroute PR or a report: once per report cooldown, not every pass.
            last = parse_time(acted.get(key))
            if key not in prev or (last and (now - last).total_seconds() < report_cooldown_minutes * 60):
                continue
            acted[key] = stamp
            keep.append(f)
            continue
        if f["kind"] in RESTART_KINDS:
            targets = [n for n in restart_targets(f) if n not in done]
            rkeys = ["r|" + n for n in targets]
            for rk in rkeys:
                last_seen[rk] = stamp
            if key not in prev or (restart_targets(f) and not targets):
                continue  # first sighting (wait for the next pass), or already handled this pass
            count = max([tries.get(rk, 0) for rk in rkeys] or [0])
            if count >= max_restarts:
                # Restarting hasn't cleared it (wrong runner group, a dead machine): stop churning, tell the person,
                # once per report cooldown.
                rep = "report|" + key
                last = parse_time(acted.get(rep))
                if not last or (now - last).total_seconds() >= report_cooldown_minutes * 60:
                    acted[rep] = stamp
                    keep.append(dict(f, escalate=True, restarts=count))
                done.update(targets)
                continue
            last = max([parse_time(acted.get(k)) for k in [key] + rkeys if parse_time(acted.get(k))] or [None],
                       key=lambda x: x or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
            if last and (now - last).total_seconds() < cooldown_minutes * 60:
                continue
            for k in [key] + rkeys:
                acted[k] = stamp
            for rk in rkeys:
                tries[rk] = tries.get(rk, 0) + 1
            done.update(targets)
        keep.append(f)
    # A finding that vanishes for a pass or two (a sampled queue, a flapping runner, a skipped repo) keeps its restart
    # count and cooldown; only one absent for `forget` minutes, or a runner seen online and fine, is forgotten.
    forget = max(report_cooldown_minutes, cooldown_minutes * 3) * 60
    healthy = {"r|" + n for n in (online or ())}
    for key in set(acted) | set(tries) | set(last_seen):
        base = key[len("report|"):] if key.startswith("report|") else key
        if base in seen or (base.startswith("r|") and last_seen.get(base) == stamp):
            continue
        gone = parse_time(last_seen.get(base))
        if base in healthy or not gone or (now - gone).total_seconds() >= forget:
            acted.pop(key, None)
            tries.pop(base, None)
            last_seen.pop(base, None)
    state["seen"] = seen
    return keep


def host_pressure(h, rules):
    """'red', 'headroom' or 'ok' for one runner box from its host health (cpus, load5, MemTotal, MemAvailable, disks,
    and from a box-local reading `swap_pages_per_s`: pages swapped in + out per second since the last pass).
    Thresholds: `capacity.host`. `pause_on` picks which of load, memory and swap make a box red (disk always does,
    as a report); a signal left out of it never makes a box red and never blocks headroom."""
    t = rules["capacity"]["host"]
    if (not h.get("ok") or not h.get("cpus") or not h.get("MemTotal") or h.get("MemAvailable") is None
            or h.get("load5") is None):
        return "unknown", {}  # a partial reading must not look like 100% memory or zero load
    on = set(t.get("pause_on") or ("load", "memory", "swap"))
    load = h["load5"] / h["cpus"]
    mem = 1 - h["MemAvailable"] / h["MemTotal"]
    disk = max([(d.get("used") or 0) / d["size"] for d in h.get("disks") or [] if d.get("size")] or [0])
    swap = h.get("swap_pages_per_s")
    swap = float(swap) if isinstance(swap, (int, float)) else None
    m = {"load_per_core": round(load, 2), "mem_pct": round(mem * 100), "disk_pct": round(disk * 100)}
    if swap is not None:
        m["swap_pages_per_s"] = round(swap, 1)
    if hot_reading(m, rules) or disk * 100 >= t["red_disk_pct"]:
        return "red", m
    if (("load" not in on or load <= t["headroom_load_per_core"])
            and ("memory" not in on or mem * 100 <= t["headroom_mem_pct"])
            and ("swap" not in on or swap is None or swap < t.get("calm_swap_pages_per_s", 1))
            and disk * 100 < t["red_disk_pct"] - 10):
        return "headroom", m
    return "ok", m


def hot_reading(m, rules):
    """True when a reading crosses a `pause_on` limit: the kind of red that pausing a slot can fix."""
    t = rules["capacity"]["host"]
    on = set(t.get("pause_on") or ("load", "memory", "swap"))
    return bool(("load" in on and m.get("load_per_core", 0) >= t["red_load_per_core"])
                or ("memory" in on and m.get("mem_pct", 0) >= t["red_mem_pct"])
                or ("swap" in on and m.get("swap_pages_per_s") is not None
                    and m["swap_pages_per_s"] >= t.get("red_swap_pages_per_s", 100)))


def _pressure_text(m):
    return "load %.2f per core, memory %d%%%s" % (
        m["load_per_core"], m["mem_pct"],
        ", swapping %.0f pages/s" % m["swap_pages_per_s"] if m.get("swap_pages_per_s") is not None else "")


def host_actions(hosts, pools, rules, state, now, runners=None):
    """Keep each runner box inside its CPU and memory limits, one slot at a time.
    red (load or memory, two passes in a row) -> pause-slot: stop one IDLE runner slot on that box (its service or
                 container; never a busy one, never below `min_slots`). The steward records which slot with
                 `steward.py capacity-note --paused NAME --host BOX` (or that none could be paused), so the
                 paused list stays true and pool checks don't restart it.
    red on disk only -> report: pausing runners frees no disk.
    headroom while a pool is backed up -> resume-slot: start the slot this check paused most recently.
    headroom while a pool it serves is backed up, nothing paused, every slot on it busy, and fewer online than
                 `capacity.host.max_slots[box]` -> add-slot: start ONE more slot (2026-11-20: 12 busy slots against
                 ~2,000 queued runs, and nothing could grow the pool). Off for a box with no max_slots entry.
    One change per box per `capacity.host.cooldown_minutes`. `state["hosts"][box]`: {"paused": [names],
    "red_seen": iso, "changed": iso}."""
    t = rules["capacity"]["host"]
    backed_up = any(p["queued_jobs"] >= rules["capacity"]["backed_up_jobs"] and p["idle"] == 0 for p in pools)
    hs = state.setdefault("hosts", {})
    acts = []
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for h in hosts:
        if not isinstance(h, dict) or not h.get("host"):
            continue
        try:
            level, m = host_pressure(h, rules)
        except (TypeError, ValueError, ZeroDivisionError, KeyError):
            level, m = "unknown", {}
        st = hs.setdefault(h["host"], {})
        if not isinstance(st.get("paused"), list):
            st["paused"] = []
        last = parse_time(st.get("changed"))
        cooling = last and (now - last).total_seconds() < t["cooldown_minutes"] * 60
        hot = level == "red" and hot_reading(m, rules)
        online_names = {r.get("name") for r in runners or [] if r.get("status") == "online"}
        for n in [n for n in st["paused"] if n in online_names]:
            # Running again (a resume whose note was never recorded, or a pause that never happened): not paused.
            st["paused"].remove(n)
            (st.get("paused_at") or {}).pop(n, None)
        long_paused = [n for n in st["paused"] if parse_time((st.get("paused_at") or {}).get(n))
                       and (now - parse_time(st["paused_at"][n])).total_seconds()
                       >= t.get("paused_report_hours", 12) * 3600]
        lp_last = parse_time(st.get("long_paused_reported"))
        if long_paused and (not lp_last or (now - lp_last).total_seconds()
                            >= rules["capacity"]["report_cooldown_minutes"] * 60):
            st["long_paused_reported"] = stamp
            acts.append({"do": "report", "needs_person": True, "host": h["host"], "runners": long_paused,
                         "why": "%s paused on %s for over %d hours: that capacity is parked; check the box and "
                                "resume them" % (", ".join(long_paused), h["host"], t.get("paused_report_hours", 12))})
        rep_last = parse_time(st.get("pressure_reported"))
        may_report = not rep_last or (now - rep_last).total_seconds() >= rules["capacity"]["report_cooldown_minutes"] * 60
        for n, when in list((st.get("pending_pause") or {}).items()):
            t0 = parse_time(when)
            if not t0 or (now - t0).total_seconds() >= PENDING_PAUSE_HOURS * 3600:
                st["pending_pause"].pop(n)
        if level == "red" and not hot:
            # A report is not a change on the box: it uses the report cooldown and never delays a resume.
            if may_report:
                st["pressure_reported"] = stamp
                acts.append({"do": "report", "needs_person": False, "host": h["host"], "metrics": m,
                             "why": "%s disk at %d%%: free space (docker builder/image prune, Colima disk); pausing "
                                    "runners won't help" % (h["host"], m["disk_pct"])})
            st.pop("red_seen", None)
            continue
        if hot:
            first = st.get("red_seen")
            st["red_seen"] = first or stamp
            if not first or cooling:
                continue  # first red reading: confirm on the next pass
            pick, online = pick_idle_slot(h["host"], runners, rules, st["paused"])
            if pick is None:
                if not may_report:
                    continue
                st["pressure_reported"] = stamp
                acts.append({"do": "report", "needs_person": False, "host": h["host"], "metrics": m,
                             "why": "%s is over its limit (%s) but no slot can be paused "
                                    "(%s)" % (h["host"], _pressure_text(m), online)})
                continue
            acts.append({"do": "pause-slot", "needs_person": False, "host": h["host"], "runner": pick, "metrics": m,
                         "why": "%s is over its limit (%s): stop idle runner %s (%d online "
                                "on it), then record it with capacity-note --paused %s"
                                % (h["host"], _pressure_text(m), pick, online, pick)})
            st["changed"] = stamp
            st.setdefault("pending_pause", {})[pick] = stamp
            continue
        st.pop("red_seen", None)
        slot_labels = {}
        for r in runners or []:
            slot_labels[r.get("name")] = [l["name"] for l in r.get("labels") or []]
        wanted = [p for p in pools if p["queued_jobs"] >= rules["capacity"]["backed_up_jobs"] and p["idle"] == 0]
        fits_backlog = bool(st["paused"]) and (
            st["paused"][-1] not in slot_labels  # labels unknown: any backlog counts
            or any(fits(p["labels"], slot_labels[st["paused"][-1]]) for p in wanted))
        if level == "headroom" and backed_up and fits_backlog and not cooling:
            acts.append({"do": "resume-slot", "needs_person": False, "host": h["host"], "runner": st["paused"][-1],
                         "metrics": m,
                         "why": "%s has headroom (%s) and jobs are backed up: start %s "
                                "again, then record it with capacity-note --resumed"
                                % (h["host"], _pressure_text(m), st["paused"][-1])})
            st["changed"] = stamp
        elif (level == "headroom" and backed_up and not st["paused"] and not cooling
              and m["load_per_core"] <= t["headroom_load_per_core"]):
            # Growing a box always needs CPU headroom, even when load no longer pauses or blocks a resume.
            add = add_slot(h["host"], runners, rules, wanted)
            if add:
                acts.append(dict({"do": "add-slot", "needs_person": False, "host": h["host"], "metrics": m,
                                  "why": "%s has headroom (load %.2f per core, memory %d%%), all %d of its slots are "
                                         "busy and %d jobs it fits are queued: start one more slot (ceiling %d); "
                                         "the next pass counts it from GitHub, nothing to record"
                                         % (h["host"], m["load_per_core"], m["mem_pct"], add["online"],
                                            add["queued_jobs"], add["max"])}, **add))
                st["changed"] = stamp
        elif level == "unknown" and h.get("host"):
            last_r = parse_time(st.get("reported"))
            if not last_r or (now - last_r).total_seconds() >= rules["capacity"]["report_cooldown_minutes"] * 60:
                st["reported"] = stamp
                acts.append({"do": "report", "needs_person": False, "host": h["host"],
                             "why": "no health reading from %s: %s" % (h["host"], h.get("error") or "unreachable")})
    return acts


def hosts_missing(spec, hosts, state, rules, now):
    """`--hosts` was given but returned nothing (the dashboard is down): one report per report cooldown, which says
    so plainly when slots are still paused, since nothing can resume them until the readings come back."""
    if not spec or hosts:
        return []
    last = parse_time(state.get("hosts_missing_reported"))
    if last and (now - last).total_seconds() < rules["capacity"]["report_cooldown_minutes"] * 60:
        return []
    state["hosts_missing_reported"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    paused = paused_runners(state)
    return [{"do": "report", "needs_person": bool(paused), "host": spec,
             "why": "no host readings from %s%s" % (spec, "; still paused: %s" % ", ".join(paused) if paused else "")}]


def box_of(name, boxes):
    """The one box a runner belongs to: the box with the LONGEST matching name prefix, so `davids-mac-mini-dind-`
    (the Colima VM) wins over `davids-mac-mini-` and no runner counts under two boxes."""
    name, best, size = (name or "").lower(), None, -1
    for box, prefixes in boxes.items():
        for p in prefixes:
            if name.startswith(p.lower()) and len(p) > size:
                best, size = box, len(p)
    return best


def pick_idle_slot(host, runners, rules, paused):
    """The one runner to pause on `host`: online, not busy, not already paused, and only if at least
    `capacity.host.min_slots` would stay online on that box. Runners belong to a box by name prefix
    (`capacity.host.runners`: {box: [prefix, ...]}). Returns (name or None, online count or a reason)."""
    t = rules["capacity"]["host"]
    prefixes = [x.lower() for x in (t.get("runners") or {}).get(host, [])]
    if runners is None:
        return None, "runners not readable from here"
    if not prefixes:
        return None, "no runner prefixes for this box in capacity.host.runners"
    mine = [r for r in runners if box_of(r.get("name"), t.get("runners") or {}) == host]
    online = [r for r in mine if r.get("status") == "online"]
    idle = [r for r in online if not r.get("busy") and r.get("name") not in paused]
    if len(online) - 1 < t["min_slots"]:
        return None, "only %d online, minimum %d" % (len(online), t["min_slots"])
    if not idle:
        return None, "every online slot is busy"
    return sorted(r["name"] for r in idle)[-1], len(online)


def add_slot(host, runners, rules, wanted):
    """Whether `host` may start one more runner slot: it has a `capacity.host.max_slots` ceiling, runners are
    readable, every online slot on it is busy, fewer than the ceiling are online, and a backed-up pool's labels fit
    its slots. Returns {"online", "max", "queued_jobs", "labels"} or None."""
    t = rules["capacity"]["host"]
    ceiling = (t.get("max_slots") or {}).get(host)
    if not ceiling or runners is None:
        return None
    mine = [r for r in runners if box_of(r.get("name"), t.get("runners") or {}) == host]
    online = [r for r in mine if r.get("status") == "online"]
    if not online or len(online) >= ceiling or any(not r.get("busy") for r in online):
        return None
    have = [[l["name"] if isinstance(l, dict) else l for l in r.get("labels") or []] for r in online]
    fit = [p for p in wanted if any(fits(p["labels"], h) for h in have)]
    if not fit:
        return None
    top = max(fit, key=lambda p: p["queued_jobs"])
    return {"online": len(online), "max": ceiling, "queued_jobs": top["queued_jobs"], "labels": top["labels"],
            "slots": sorted(r.get("name") for r in mine)}


PENDING_PAUSE_HOURS = 4


def paused_runners(state, now=None):
    """Slots paused on purpose: those recorded with capacity-note --paused, plus any a pause-slot action named in
    the last PENDING_PAUSE_HOURS that was never confirmed (the session stopped, or the note couldn't get the
    lock), so a slot the steward meant to stop is never restarted as 'offline'."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for st in (state.get("hosts") or {}).values():
        if isinstance(st.get("paused"), list):
            out.extend(st["paused"])
        for n, when in (st.get("pending_pause") or {}).items():
            t = parse_time(when)
            if t and (now - t).total_seconds() < PENDING_PAUSE_HOURS * 3600 and n not in out:
                out.append(n)
    return out


# The Mac load governor (mac-runner-governor.sh, run by the Mac runner watchdog) stops runner containers while the
# Mac is overloaded and lists them here, one file per runner name. They are offline on purpose, like the slots
# above: never "offline", "pool down" or restarted. Only seen when the capacity check runs on that Mac.
GOVERNOR_PAUSED_DIR = os.environ.get("BA_GOVERNOR_PAUSED_DIR") or os.path.expanduser(
    "~/.local/state/mac-runner-watchdog/governor/paused")


GOVERNOR_PAUSED_MAX_HOURS = 24


def governor_paused(path=None, now=None):
    """Runner names the Mac load governor has paused, or [] when it isn't installed here. A pause older than
    GOVERNOR_PAUSED_MAX_HOURS is not counted (the governor turned off, or stuck), so the runner is reported
    offline and restarted like any other instead of staying parked unseen."""
    path = path or GOVERNOR_PAUSED_DIR
    now = now or time.time()
    out = []
    try:
        names = os.listdir(path)
    except OSError:
        return []
    for f in names:
        if f.endswith((".labels", ".id", ".org")) or f.startswith("."):
            continue
        try:
            if now - os.path.getmtime(os.path.join(path, f)) < GOVERNOR_PAUSED_MAX_HOURS * 3600:
                out.append(f)
        except OSError:
            pass
    return sorted(out)


def cmd_capacity_note(args):
    """Record what the steward did on a box, so the next pass knows which slots are paused on purpose. Waits up to
    as long as the stale-lock window for a running capacity pass, so neither overwrites the other's state."""
    lock, deadline = None, time.time() + LOCK_STALE_MINUTES * 60 + 30
    while lock is None:
        lock = _take_lock(args.state)
        if lock is None:
            if time.time() >= deadline:
                print("capacity state is locked (%s.lock); note not recorded, run it again" % args.state,
                      file=sys.stderr)
                return 1
            time.sleep(2)
    try:
        state = _read_state(args.state)
        st = state.setdefault("hosts", {}).setdefault(args.host, {})
        if not isinstance(st.get("paused"), list):
            st["paused"] = []
        if args.paused and args.paused not in st["paused"]:
            st["paused"].append(args.paused)
            st.setdefault("paused_at", {})[args.paused] = dt.datetime.now(dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        if args.resumed and args.resumed in st["paused"]:
            st["paused"].remove(args.resumed)
            (st.get("paused_at") or {}).pop(args.resumed, None)
        for n in (args.paused, args.resumed):
            (st.get("pending_pause") or {}).pop(n, None)
        _write_state(args.state, state)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass
    print(json.dumps({"host": args.host, "paused": st["paused"]}))
    return 0


def read_hosts(spec):
    """Host health from the dashboard (`http://127.0.0.1:4517/api/data`) or a saved JSON file; [] when neither."""
    if not spec:
        return []
    try:
        if spec.startswith("http"):
            import urllib.request
            with urllib.request.urlopen(spec, timeout=10) as r:
                data = json.load(r)
        else:
            with open(spec, encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError):
        return []
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    ci = data.get("ci") if isinstance(data.get("ci"), dict) else {}
    found = ci.get("hosts") or data.get("hosts") or []
    return found if isinstance(found, list) else []


def machine_restart(names, runners, rules):
    """Whether the VM the named runners run on can be restarted (by the steward or a person) without killing a job:
    every
    runner on that box (by `capacity.host.runners` prefix) is offline or idle. When true, the steward may restart
    the runner VM (Colima) after a service restart failed; it never reboots a physical server. Returns {"box", "machine_restart_ok", "busy"}; busy lists
    the runners a machine restart would kill."""
    boxes = rules["capacity"].get("host", {}).get("runners") or {}
    found = {box_of(n, boxes) for n in names} - {None}
    if len(found) != 1 or runners is None:
        return {"box": ",".join(sorted(found)) or None, "machine_restart_ok": False, "busy": None}
    box = found.pop()
    busy = sorted(r.get("name") for r in runners if box_of(r.get("name"), boxes) == box and r.get("busy"))
    return {"box": box, "machine_restart_ok": not busy, "busy": busy}


def capacity_actions(stale, findings, rules, runners=None):
    """`runners`: the owner's runners this pass, to decide whether a restart may go as far as the machine."""
    cap = rules["capacity"]
    acts = []
    if stale:
        acts.append({"do": "cancel-stale", "needs_person": False, "runs": [s[0] for s in stale][:cap["max_cancels"]],
                     "why": "%d queued runs nobody needs (%s); cancelling them is safe and any can be re-run"
                            % (len(stale), ", ".join("%s %d" % kv for kv in sorted(_count(s[1] for s in stale).items())))})
    for f in findings:
        k = f["kind"]
        if f.get("escalate"):
            acts.append({"do": "report", "needs_person": True, "finding": k,
                         "runner": f.get("runner") or f.get("idle") or f.get("labels"),
                         "why": "%s is still there after %d restarts: a restart can't fix it (runner group, labels, "
                                "or a dead machine). Stopped restarting; it needs a look." % (k, f["restarts"])})
            continue
        if k == "runner-offline":
            acts.append(dict({"do": "restart-runner", "needs_person": False, "runner": f["runner"],
                              "why": "offline with jobs queued for its labels: restart its service or container on "
                                     "its machine (never delete the registration)"},
                             **machine_restart([f["runner"]], runners, rules)))
        elif k == "idle-but-queued":
            idle = [x if isinstance(x, str) else x.get("runner") for x in f["idle"]]
            acts.append(dict({"do": "restart-runner", "needs_person": False, "runner": ", ".join(idle),
                              "why": "shown idle while %d jobs it fits are queued: hung; restart it"
                                     % f["queued_jobs"]}, **machine_restart(idle, runners, rules)))
        elif k == "pool-down":
            acts.append(dict({"do": "restore-pool", "needs_person": False, "labels": f["labels"],
                              "runner": ", ".join(f.get("offline") or []),
                              "why": "%d jobs queued and every runner that fits %s is offline: restart them"
                                     % (f["queued_jobs"], f["labels"])},
                             **machine_restart(f.get("offline") or [], runners, rules)))
        elif k == "no-runner-for-labels":
            # Nothing to restart: a mistyped runs-on, a larger-runner label, or a runner never registered.
            acts.append({"do": "report", "needs_person": True, "labels": f["labels"], "repos": f.get("repos"),
                         "why": "%d jobs in %s ask for %s and no runner has those labels: fix runs-on or register a "
                                "runner" % (f["queued_jobs"], ", ".join(f.get("repos") or []) or "these repos",
                                            f["labels"])})
        elif k == "idle-capacity-unreachable":
            acts.append({"do": "reroute", "needs_person": False, "idle": f["idle"], "backed_up": f["backed_up"],
                         "why": "runners idle while other pools are backed up: move the light jobs (auto-merge, "
                                "sweeps, labelling, mark-done) to the idle label in the workflows, or give the idle "
                                "runners the busy pool's label on their machine"})
    return acts


def _count(xs):
    c = {}
    for x in xs:
        c[x] = c.get(x, 0) + 1
    return c


def rate_remaining():
    try:
        return ((api("rate_limit") or {}).get("resources", {}).get("core", {}) or {}).get("remaining")
    except GhError:
        return None


def capacity_repo(repo, rules, sample_runs, cache=None, lean=False):
    """`cache`: {(head repo, branch): closed_at} kept between passes, so each closed branch is looked up once.
    `lean`: the API budget is low, so skip close lookups and job sampling (stale superseded runs still found)."""
    from urllib.parse import quote
    cache = cache if cache is not None else {}
    now = dt.datetime.now(dt.timezone.utc)
    queued = paged("repos/%s/actions/runs?status=queued" % repo, key="workflow_runs", cap=2000)
    open_prs = paged("repos/%s/pulls?state=open" % repo, cap=2000)
    open_keys = {_head_key((p["head"].get("repo") or {}).get("full_name"), p["head"].get("ref")) for p in open_prs}
    closed_at = {}
    candidates = {(r.get("head_repository") or {}).get("full_name") and
                  _head_key(r["head_repository"]["full_name"], r.get("head_branch"))
                  for r in queued if r.get("event") in PR_EVENTS}
    todo = sorted(k for k in candidates if k and k not in open_keys)
    lookups = 0
    for key in todo:
        ck = "%s|%s" % key
        if ck in cache:
            hit = cache[ck]
            closed_at[key] = hit["t"] if isinstance(hit, dict) else hit
            cache[ck] = {"t": closed_at[key], "seen": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
            continue
        if lean or lookups >= rules["capacity"]["max_close_lookups"]:
            continue
        lookups += 1
        full, branch = key
        try:
            closed = api("repos/%s/pulls?state=closed&head=%s&per_page=5"
                         % (repo, quote("%s:%s" % (full.split("/")[0], branch), safe=""))) or []
        except GhError:
            continue
        times = [p.get("closed_at") for p in closed if p.get("closed_at")]
        if times:
            closed_at[key] = max(times)
            cache[ck] = {"t": closed_at[key], "seen": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    # Forget branches no queued run has needed for a few days (a reused name then gets a fresh lookup).
    for ck in list(cache):
        seen = parse_time(cache[ck].get("seen")) if isinstance(cache[ck], dict) else None
        if not seen or (now - seen).total_seconds() > 3 * 86400:
            cache.pop(ck)
    stale = stale_runs(queued, open_prs, _keep(rules), closed_at, _dedupe(rules, repo))
    stale_ids = {s[0] for s in stale}
    labels, oldest = {}, {}
    live = [r for r in queued if r["id"] not in stale_ids]
    oldest_first = sorted(live, key=lambda r: r.get("created_at") or "")
    half = max(1, sample_runs // 2)
    sample = oldest_first[:half] + [r for r in oldest_first[-half:] if r not in oldest_first[:half]]
    sample = [] if lean else sample
    for r in sample:
        try:
            jobs = (api("repos/%s/actions/runs/%s/jobs?per_page=100" % (repo, r["id"])) or {}).get("jobs", [])
        except GhError:
            continue
        for j in jobs:
            if j.get("status") == "queued" and j.get("labels"):
                k = tuple(sorted(j["labels"]))
                labels[k] = labels.get(k, 0) + 1
                waited = (now - parse_time(j.get("created_at"))).total_seconds() / 60 if j.get("created_at") else None
                if waited is not None:
                    oldest[k] = max(oldest.get(k, 0), round(waited))
    # The jobs were read from a sample of the queued runs: scale the counts to the whole queue, so they compare
    # with `backed_up_jobs` (a count of queued jobs in the whole queue).
    if sample and len(live) > len(sample):
        f = len(live) / len(sample)
        labels = {k: int(round(v * f)) for k, v in labels.items()}
    first = min((parse_time(r.get("created_at")) for r in live), default=None)
    try:  # repo-level runners also serve these jobs; the org list doesn't show them
        repo_runners = paged("repos/%s/actions/runners" % repo, key="runners")
    except GhError:
        repo_runners = []
    return {"_oldest": oldest, "_runners": repo_runners, "repo": repo, "queued_runs": len(queued), "stale": [{"run": i, "reason": why, "workflow": w}
                                                                   for i, why, w in stale],
            "queued_job_labels": {",".join(k): v for k, v in labels.items()}, "_labels": labels,
            "stacked": stacked_workflows(queued, open_prs, rules["capacity"].get("stack_min_runs", 50),
                                         rules["capacity"].get("stack_per_pr", 2.0)),
            "oldest_queued_hours": hours(now, first)}


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_state(path, state):
    """Write to a temp file beside it and rename, so a crash mid-write never leaves half a state file."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


LOCK_STALE_MINUTES = 60


def _take_lock(path):
    """One capacity pass per state file at a time. A lock older than LOCK_STALE_MINUTES is from a run that died
    and is taken over. Returns the lock path, or None when another pass holds it."""
    lock = path + ".lock"
    os.makedirs(os.path.dirname(lock) or ".", exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock)
            except OSError:
                continue
            if age < LOCK_STALE_MINUTES * 60:
                return None
            try:
                os.unlink(lock)
            except OSError:
                return None
    return None


def runner_groups(owner):
    """{runner id: set of repo full names it may serve, or None for every repo}, from the org's runner groups;
    None when they can't be read."""
    try:
        groups = paged("orgs/%s/actions/runner-groups" % owner, key="runner_groups")
        allowed = {}
        for g in groups:
            repos = None
            if g.get("visibility") == "selected":
                repos = {r["full_name"].lower() for r in
                         paged("orgs/%s/actions/runner-groups/%s/repositories" % (owner, g["id"]), key="repositories")}
            for r in paged("orgs/%s/actions/runner-groups/%s/runners" % (owner, g["id"]), key="runners"):
                allowed[r["id"]] = repos
        return allowed
    except GhError:
        return None


def _finding_key(f):
    if f["kind"] in ("runner-offline",):
        return (f["kind"], f["runner"])
    if f["kind"] == "idle-but-queued":
        return (f["kind"], tuple(sorted(f["idle"])))
    return (f["kind"], tuple(f.get("labels") or []))


def owner_pools(owner, org_runners, aggs, state, rules, now, lean, groups=None):
    """Pool findings for one owner, settled against what earlier passes saw. `aggs`: {repo: {"labels", "oldest",
    "runners"}} (that repo's sampled queued-job labels and its own repo-level runners). `org_runners`: the org's
    runners, or None when they can't be read. `groups`: runner_groups(), or None when unreadable.

    Each repo is checked against the org runners its runner groups allow plus ITS OWN repo-level runners. Findings
    are then merged across repos (one runner offline for five repos is one finding) and settled ONCE under the
    owner's state, so the cooldown and the restart cap count per runner, not per repo. Without runner groups, a
    runner that looks idle while jobs queue may simply not be allowed to take them, so idle-but-queued is not
    acted on. When the org's runners can't be read, only label sets a readable repo-level runner fits are checked,
    and never as pool-down or no-runner-for-labels. A pass that sampled no job labels (lean, nothing queued)
    leaves the state as it was. Returns (pools, findings, ready) or None."""
    cap = rules["capacity"]
    if lean or not any(a["labels"] for a in aggs.values()):
        return None
    paused = paused_runners(state, now) + governor_paused()
    pools, merged = [], {}
    for repo, a in sorted(aggs.items()):
        if not a["labels"] or (org_runners is None and not a["runners"]):
            continue
        usable = [r for r in (org_runners or [])
                  if groups is None or groups.get(r.get("id"), None) is None
                  or repo.lower() in groups.get(r.get("id"))]
        labels = a["labels"]
        if org_runners is None:
            labels = {k: v for k, v in labels.items()
                      if any(fits(k, [l["name"] for l in r.get("labels") or []]) for r in a["runners"])}
        p, f = pool_report(usable + a["runners"], labels, 10 ** 9, a["oldest"], cap["settle_minutes"], paused)
        for x in p:
            x["repo"] = repo
        pools.extend(p)
        for x in f:
            if org_runners is None and x["kind"] in ("pool-down", "no-runner-for-labels"):
                continue
            if groups is None and x["kind"] == "idle-but-queued":
                continue
            k = _finding_key(x)
            if k in merged:
                merged[k].setdefault("repos", []).append(repo)
                if "queued_jobs" in x:
                    merged[k]["queued_jobs"] = merged[k].get("queued_jobs", 0) + x["queued_jobs"]
            else:
                merged[k] = dict(x, repos=[repo])
    findings = list(merged.values())
    if org_runners is not None:
        allm, oldest = {}, {}
        for a in aggs.values():
            for k, v in a["labels"].items():
                allm[k] = allm.get(k, 0) + v
            for k, v in a["oldest"].items():
                oldest[k] = max(oldest.get(k, 0), v)
        every = org_runners + [r for a in aggs.values() for r in a["runners"]]
        _, f = pool_report(every, allm, cap["backed_up_jobs"], oldest, cap["settle_minutes"], paused)
        findings.extend(x for x in f if x["kind"] == "idle-capacity-unreachable")
    online = None
    if org_runners is not None:
        hung = {x if isinstance(x, str) else x.get("runner") for f in findings if f["kind"] == "idle-but-queued"
                for x in f.get("idle") or []}
        online = {r.get("name") for r in org_runners if r.get("status") == "online" and r.get("name") not in hung}
    ready = settle(findings, state.setdefault(owner, {}), now, cap["restart_cooldown_minutes"],
                   cap["max_restarts"], cap["report_cooldown_minutes"], online)
    return pools, findings, ready


def cmd_capacity(args):
    lock = _take_lock(args.state)
    if lock is None:
        print("another capacity pass holds %s.lock; skipping this one" % args.state, file=sys.stderr)
        return 0
    try:
        return _capacity_pass(args)
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def _capacity_pass(args):
    rules = load_rules(args.rules)
    repos = args.repo or [current_repo()]
    out = {"generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "owners": [],
           "repos": [], "errors": []}
    by_owner = {}
    state = _read_state(args.state)
    cap = rules["capacity"]
    left = rate_remaining()
    lean = left is not None and left < cap["min_rate_remaining"]
    out["rate_remaining"], out["lean"] = left, lean
    for repo in repos:
        try:
            rep = capacity_repo(repo, rules, cap["sample_runs"], state.setdefault("closed_at", {}), lean)
        except GhError as exc:
            out["errors"].append({"repo": repo, "error": str(exc)})
            continue
        owner = repo.split("/")[0]
        by_owner.setdefault(owner, {})[repo] = {"labels": rep.pop("_labels"), "oldest": rep.pop("_oldest"),
                                                "runners": rep.pop("_runners")}
        stale = [(s["run"], s["reason"], s["workflow"]) for s in rep["stale"]]
        rep["actions"] = capacity_actions(stale, [], rules)
        for w in rep.get("stacked") or []:
            rep["actions"].append({"do": "improve", "kind": "stacked-workflow", "needs_person": False,
                                   "workflow": w["workflow"], "evidence": w,
                                   "why": "%s has %d queued runs for %d open pull requests (%.1f each): every event "
                                          "queues another job on the pool. File `Steward: stacked-workflow %s` and "
                                          "fix it with a PR: a per-PR concurrency group, or start its jobs only on "
                                          "the events they act on" % (w["workflow"], w["queued"], w["prs"],
                                                                      w["per_pr"], w["workflow"])})
        out["repos"].append(rep)
    now = dt.datetime.now(dt.timezone.utc)
    all_pools, seen_runners = [], []
    for owner, aggs in by_owner.items():
        try:
            org_runners = paged("orgs/%s/actions/runners" % owner, key="runners")
        except GhError as exc:
            # Listing an org's runners needs org admin, and cloud sessions are refused it: runner and host actions
            # only work from the person's computer. Stale runs are still found and cancelled.
            out["errors"].append({"owner": owner, "error": "org runners not readable: %s" % exc})
            org_runners = None
        groups = runner_groups(owner) if org_runners is not None else None
        if org_runners is not None:
            seen_runners.extend(org_runners)
        # Repo-level runners count for a box's min_slots and idle checks too.
        seen_runners.extend(r for a in aggs.values() for r in a["runners"]
                            if r.get("id") not in {x.get("id") for x in seen_runners})
        res = owner_pools(owner, org_runners, aggs, state, rules, now, lean, groups)
        if res is None:
            continue
        pools, findings, ready = res
        all_pools.extend(pools)
        out["owners"].append({"owner": owner, "pools": pools, "findings": findings,
                              "actions": capacity_actions([], ready, rules, org_runners)})
    hosts = read_hosts(args.hosts)
    out["hosts"] = []
    for h in hosts:
        try:
            out["hosts"].append(dict(host=h.get("host"), **dict(zip(("level", "metrics"), host_pressure(h, rules)))))
        except (AttributeError, TypeError, ValueError, ZeroDivisionError, KeyError):
            out["hosts"].append({"host": str(h)[:60], "level": "unknown", "metrics": {}})
    out["state"] = args.state  # the box-local apply records its pauses here (capacity-note)
    out["host_actions"] = host_actions(hosts, all_pools, rules, state, now, seen_runners or None) + hosts_missing(args.hosts, hosts, state,
                                                                                        rules, now)
    _write_state(args.state, state)
    text = json.dumps(out, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        for r in out["repos"]:
            print("%s: %d queued, oldest %sh, %d stale" % (r["repo"], r["queued_runs"], r["oldest_queued_hours"],
                                                          len(r["stale"])))
        for o in out["owners"]:
            print("%s: %s" % (o["owner"], "; ".join("%s %s" % (a["do"], a.get("runner") or a.get("labels") or "")
                                                     for a in o["actions"]) or "pools fine"))
        for a in out["host_actions"]:
            print("%s: %s" % (a["host"], a["do"]))
        for e in out["errors"]:
            print("skipped: %s" % json.dumps(e))
    else:
        print(text)
    return 0


def cmd_sweep(args):
    rules = load_rules(args.rules)
    if args.max_prs is not None:
        rules["steward"]["max_prs_classified"] = args.max_prs
    if args.min_rate:
        try:
            left = (api("rate_limit") or {}).get("resources", {}).get("core", {}).get("remaining")
        except GhError:
            left = None
        if left is not None and left < args.min_rate:
            # The limit is the token owner's, shared with every other tool they run: never spend the last of it.
            print("sweep skipped: %d API calls left this hour, under --min-rate %d" % (left, args.min_rate))
            if args.out:  # never leave the last pass's report for an apply step to act on again
                with open(args.out, "w", encoding="utf-8") as fh:
                    json.dump({"repos": [], "errors": [], "skipped": "rate floor"}, fh)
                    fh.write("\n")
            return 0
    sw = Sweeper(rules, lite=args.lite)
    if args.lite:
        args.no_history = True  # a capped, scan-free sweep would skew the health history sweep to sweep
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
    s.add_argument("--lite", action="store_true", help="repairs only: the cheap reads an unattended timer needs")
    s.add_argument("--max-prs", type=int, help="classify at most this many PRs (default steward.max_prs_classified)")
    s.add_argument("--min-rate", type=int, default=0, help="skip the sweep when fewer API calls than this are left")
    h = sub.add_parser("health", help="the CI health history for a repo, and what it says to improve")
    h.add_argument("--repo", required=True)
    h.add_argument("--history-dir", default=HISTORY_DIR)
    c = sub.add_parser("capacity", help="idle runners, offline runners and stale queued runs (every few minutes)")
    c.add_argument("--repo", action="append")
    c.add_argument("--out")
    c.add_argument("--state", default=os.path.join(os.path.expanduser("~"), ".build-autopilot", "capacity-state.json"),
                   help="findings seen on the last pass and runners restarted, so one snapshot never acts")
    c.add_argument("--hosts", default=os.environ.get("CAPACITY_HOSTS", ""),
                   help="runner box health: the github-throughput dashboard (http://127.0.0.1:4517/api/data) or a JSON file")
    n = sub.add_parser("capacity-note", help="record a slot the steward paused or resumed on a box")
    n.add_argument("--host", required=True)
    n.add_argument("--paused")
    n.add_argument("--resumed")
    n.add_argument("--state", default=os.path.join(os.path.expanduser("~"), ".build-autopilot", "capacity-state.json"))
    sub.add_parser("rules")
    sub.add_parser("session-note")
    args = ap.parse_args(argv)
    if args.cmd == "sweep":
        return cmd_sweep(args)
    if args.cmd == "capacity":
        return cmd_capacity(args)
    if args.cmd == "capacity-note":
        return cmd_capacity_note(args)
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
