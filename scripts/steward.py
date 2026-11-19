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
      superseded  an open pull request has this head branch but its head is another commit;
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
            out.append((r["id"], "superseded", r.get("name")))
        elif r.get("event") != "workflow_run" and r.get("name") in pr_ci:
            closed = parse_time(closed_at.get(k))
            made = parse_time(r.get("created_at"))
            if closed and made and made < closed:
                out.append((r["id"], "closed", r.get("name")))
    return out


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


def settle(findings, state, now, cooldown_minutes, max_restarts=3, report_cooldown_minutes=240):
    """Act on a runner only when the finding held on the previous pass too, and not again within the cooldown.
    `state` (kept between passes): {"seen": {key: iso time}, "acted": {runner: iso time}}. Returns the findings to
    act on; updates `state` in place."""
    def names(f):
        if f["kind"] == "idle-capacity-unreachable":
            # Keyed on what is backed up, not on which runners happen to be idle this pass.
            return ";".join(sorted(",".join(b["labels"]) for b in f.get("backed_up") or []))
        idle = [x["runner"] if isinstance(x, dict) else x for x in f.get("idle") or []]
        return f.get("runner") or ",".join(idle) or ",".join(f.get("labels") or [])

    prev = state.get("seen", {})
    acted = state.setdefault("acted", {})
    tries = state.setdefault("restarts", {})
    keep, seen = [], {}
    for f in findings:
        key = "%s|%s" % (f["kind"], names(f))
        seen[key] = prev.get(key) or now.strftime("%Y-%m-%dT%H:%M:%SZ")
        if f["kind"] in ("idle-capacity-unreachable", "no-runner-for-labels"):
            # A reroute PR or a report: once per report cooldown, not every pass.
            last = parse_time(acted.get(key))
            if key not in prev or (last and (now - last).total_seconds() < report_cooldown_minutes * 60):
                continue
            acted[key] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            keep.append(f)
            continue
        if f["kind"] in ("runner-offline", "idle-but-queued", "pool-down"):
            if key not in prev:
                continue  # first sighting: wait for the next pass
            if tries.get(key, 0) >= max_restarts:
                # Restarting hasn't cleared it (wrong runner group, a dead machine): stop churning, tell the person,
                # once per report cooldown.
                rep = "report|" + key
                last = parse_time(acted.get(rep))
                if not last or (now - last).total_seconds() >= report_cooldown_minutes * 60:
                    acted[rep] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                    keep.append(dict(f, escalate=True, restarts=tries[key]))
                continue
            last = parse_time(acted.get(key))
            if last and (now - last).total_seconds() < cooldown_minutes * 60:
                continue
            acted[key] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            tries[key] = tries.get(key, 0) + 1
        keep.append(f)
    # A finding that cleared resets its count.
    for key in list(acted):
        base = key[len("report|"):] if key.startswith("report|") else key
        if base not in seen:
            acted.pop(key)
            tries.pop(base, None)
    state["seen"] = seen
    return keep


def host_pressure(h, rules):
    """'red', 'headroom' or 'ok' for one runner box from the github-throughput dashboard's host health
    (`/api/data` field `hosts`: cpus, load5, MemTotal, MemAvailable, disks). Thresholds: `capacity.host`."""
    t = rules["capacity"]["host"]
    if not h.get("ok") or not h.get("cpus") or not h.get("MemTotal"):
        return "unknown", {}
    load = (h.get("load5") or 0) / h["cpus"]
    mem = 1 - (h.get("MemAvailable") or 0) / h["MemTotal"]
    disk = max([d["used"] / d["size"] for d in h.get("disks") or [] if d.get("size")] or [0])
    m = {"load_per_core": round(load, 2), "mem_pct": round(mem * 100), "disk_pct": round(disk * 100)}
    if load >= t["red_load_per_core"] or mem * 100 >= t["red_mem_pct"] or disk * 100 >= t["red_disk_pct"]:
        return "red", m
    if load <= t["headroom_load_per_core"] and mem * 100 <= t["headroom_mem_pct"] and disk * 100 < t["red_disk_pct"] - 10:
        return "headroom", m
    return "ok", m


def host_actions(hosts, pools, rules, state, now):
    """Keep each runner box inside its CPU and memory limits, one slot at a time.
    red (load or memory, two passes in a row) -> pause-slot: stop one IDLE runner slot on that box (its service or
                 container; never a busy one, never below `min_slots`). The steward records which slot with
                 `steward.py capacity-note --paused NAME --host BOX` (or that none could be paused), so the
                 paused list stays true and pool checks don't restart it.
    red on disk only -> report: pausing runners frees no disk.
    headroom while a pool is backed up -> resume-slot: start the slot this check paused most recently.
    One change per box per `capacity.host.cooldown_minutes`. `state["hosts"][box]`: {"paused": [names],
    "red_seen": iso, "changed": iso}."""
    t = rules["capacity"]["host"]
    backed_up = any(p["queued_jobs"] >= rules["capacity"]["backed_up_jobs"] and p["idle"] == 0 for p in pools)
    hs = state.setdefault("hosts", {})
    acts = []
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for h in hosts:
        level, m = host_pressure(h, rules)
        st = hs.setdefault(h.get("host", "?"), {})
        if not isinstance(st.get("paused"), list):
            st["paused"] = []
        last = parse_time(st.get("changed"))
        cooling = last and (now - last).total_seconds() < t["cooldown_minutes"] * 60
        hot = level == "red" and (m["load_per_core"] >= t["red_load_per_core"] or m["mem_pct"] >= t["red_mem_pct"])
        if level == "red" and not hot:
            if not cooling:
                st["changed"] = stamp
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
            acts.append({"do": "pause-slot", "needs_person": False, "host": h["host"], "metrics": m,
                         "min_slots": t["min_slots"],
                         "why": "%s is over its limit (load %.2f per core, memory %d%%): stop one idle runner slot on "
                                "it, never a busy one and never below %d running, then record it with capacity-note"
                                % (h["host"], m["load_per_core"], m["mem_pct"], t["min_slots"])})
            st["changed"] = stamp
            continue
        st.pop("red_seen", None)
        if level == "headroom" and backed_up and st["paused"] and not cooling:
            acts.append({"do": "resume-slot", "needs_person": False, "host": h["host"], "runner": st["paused"][-1],
                         "metrics": m,
                         "why": "%s has headroom (load %.2f per core, memory %d%%) and jobs are backed up: start %s "
                                "again, then record it with capacity-note --resumed"
                                % (h["host"], m["load_per_core"], m["mem_pct"], st["paused"][-1])})
            st["changed"] = stamp
        elif level == "unknown" and h.get("host"):
            acts.append({"do": "report", "needs_person": False, "host": h["host"],
                         "why": "no health reading from %s: %s" % (h["host"], h.get("error") or "unreachable")})
    return acts


def paused_runners(state):
    return [n for st in (state.get("hosts") or {}).values() for n in (st.get("paused") or [])
            if isinstance(st.get("paused"), list)]


def cmd_capacity_note(args):
    """Record what the steward did on a box, so the next pass knows which slots are paused on purpose."""
    state = _read_state(args.state)
    st = state.setdefault("hosts", {}).setdefault(args.host, {})
    if not isinstance(st.get("paused"), list):
        st["paused"] = []
    if args.paused and args.paused not in st["paused"]:
        st["paused"].append(args.paused)
    if args.resumed and args.resumed in st["paused"]:
        st["paused"].remove(args.resumed)
    _write_state(args.state, state)
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
    return (data.get("ci") or {}).get("hosts") or data.get("hosts") or []


def capacity_actions(stale, findings, rules):
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
            acts.append({"do": "restart-runner", "needs_person": False, "runner": f["runner"],
                         "why": "offline with jobs queued for its labels: restart its service or container on its "
                                "machine (never delete the registration)"})
        elif k == "idle-but-queued":
            acts.append({"do": "restart-runner", "needs_person": False, "runner": ", ".join(f["idle"]),
                         "why": "shown idle while %d jobs it fits are queued: hung; restart it" % f["queued_jobs"]})
        elif k in ("pool-down", "no-runner-for-labels"):
            acts.append({"do": "restore-pool", "needs_person": False, "labels": f["labels"],
                         "why": "%d jobs queued and no runner online that fits %s" % (f["queued_jobs"], f["labels"])})
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
            closed_at[key] = cache[ck]
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
            closed_at[key] = cache[ck] = max(times)
    stale = stale_runs(queued, open_prs, _keep(rules), closed_at, _dedupe(rules, repo))
    stale_ids = {s[0] for s in stale}
    labels, oldest = {}, {}
    now = dt.datetime.now(dt.timezone.utc)
    live = [r for r in queued if r["id"] not in stale_ids]
    oldest_first = sorted(live, key=lambda r: r.get("created_at") or "")
    half = max(1, sample_runs // 2)
    sample = oldest_first[:half] + [r for r in oldest_first[-half:] if r not in oldest_first[:half]]
    for r in ([] if lean else sample):
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
    first = min((parse_time(r.get("created_at")) for r in live), default=None)
    try:  # repo-level runners also serve these jobs; the org list doesn't show them
        repo_runners = paged("repos/%s/actions/runners" % repo, key="runners")
    except GhError:
        repo_runners = []
    return {"_oldest": oldest, "_runners": repo_runners, "repo": repo, "queued_runs": len(queued), "stale": [{"run": i, "reason": why, "workflow": w}
                                                                   for i, why, w in stale],
            "queued_job_labels": {",".join(k): v for k, v in labels.items()}, "_labels": labels,
            "oldest_queued_hours": hours(now, first)}


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _write_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
    except OSError:
        pass


def cmd_capacity(args):
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
        agg = by_owner.setdefault(owner, {"labels": {}, "oldest": {}, "runners": []})
        for k, v in rep.pop("_labels").items():
            agg["labels"][k] = agg["labels"].get(k, 0) + v
        for k, v in rep.pop("_oldest").items():
            agg["oldest"][k] = max(agg["oldest"].get(k, 0), v)
        agg["runners"].extend(rep.pop("_runners"))
        stale = [(s["run"], s["reason"], s["workflow"]) for s in rep["stale"]]
        rep["actions"] = capacity_actions(stale, [], rules)
        out["repos"].append(rep)
    now = dt.datetime.now(dt.timezone.utc)
    all_pools = []
    for owner, agg in by_owner.items():
        try:
            runners = paged("orgs/%s/actions/runners" % owner, key="runners") + agg["runners"]
        except GhError as exc:
            # Listing an org's runners needs org admin, and cloud sessions are refused it: run from the person's
            # computer (Remote Control) to see runners. Stale runs are still found and cancelled. Repo-level
            # runners (a personal account's) are still checked.
            out["errors"].append({"owner": owner, "error": "org runners not readable: %s" % exc})
            runners = agg["runners"]
            if not runners:
                continue
        pools, findings = pool_report(runners, agg["labels"], cap["backed_up_jobs"], agg["oldest"],
                                      cap["settle_minutes"], paused_runners(state))
        ready = settle(findings, state.setdefault(owner, {}), now, cap["restart_cooldown_minutes"],
                       cap["max_restarts"], cap["report_cooldown_minutes"])
        all_pools.extend(pools)
        out["owners"].append({"owner": owner, "pools": pools, "findings": findings,
                              "actions": capacity_actions([], ready, rules)})
    hosts = read_hosts(args.hosts)
    out["hosts"] = [dict(host=h.get("host"), **dict(zip(("level", "metrics"), host_pressure(h, rules))))
                    for h in hosts]
    out["host_actions"] = host_actions(hosts, all_pools, rules, state, now)
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
