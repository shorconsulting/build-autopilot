#!/usr/bin/env python3
"""Build Autopilot CI cost: what each CI decision costs over 7, 14 and 30 days, and the standard rules that cut it.

    ci_cost.py --repo OWNER/NAME [--repo ...] [--samples 6] [--out cost.json]

STRICTLY READ-ONLY, standard library only, same `gh` seam as audit.py (tests replace audit.gh).

How the numbers are made. GitHub bills each job rounded UP to a whole minute; a skipped job is free. Reading every
job of every run is too many calls for a busy repo (one repo here had 70k runs a month), so:
  1. every run in the last 30 days is listed (one call per 100 runs; a day over 1000 runs is listed hour by hour);
  2. runs are grouped into strata -- (workflow, event, conclusion, age bucket 0-7 / 7-14 / 14-30 days) -- and up to
     `--samples` runs per stratum, evenly spaced, have their jobs read (all attempts, so re-runs count);
  3. each stratum's billed minutes = its run count x its sampled average. Windows are sums of buckets, so a CI
     change two weeks ago shows up as a difference between the 7-day and 30-day numbers instead of being averaged away.
Dollars use the org's billing API price when it answers (needs admin:org), else the list prices in PRICE.

The standard rules (STANDARD_RULES) are checked against the workflow files and priced from the measurement; each
finding says what it costs now and what the fix saves. docs/policy.md, "ci_cost", is the contract.
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

import audit  # gh seam, parse_triggers, push_to_branch, parse_ts, fetch_text

WINDOWS = (7, 14, 30)
BUCKETS = ((0, 7), (7, 14), (14, 30))
PRICE = {"linux": 0.006, "windows": 0.010, "macos": 0.062}  # USD per billed minute, GitHub list price (2026), estimate
SAMPLES = 6
WORKERS = 8
LIST_CAP = 1000  # GitHub returns at most 1000 results per runs query
POLL_MIN = 15  # a schedule more often than this is a poller
HOLD_MIN = 5  # ci-pr-hold-superseded: the hold a pull request run would wait before its expensive jobs
MATRIX_RE = re.compile(r"\s*\(.*\)$")

STANDARD_RULES = {  # id -> (title, the fix). Order is the order findings are shown in.
    "ci-main-batched": (
        "Batch runs on the default branch instead of testing every merge",
        "Key the push concurrency group on the branch, not the commit, and let pushes queue: "
        "`group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}` with "
        "`cancel-in-progress: ${{ github.event_name == 'pull_request' }}`. GitHub keeps one run going and only the "
        "newest push waiting, so each run covers everything merged meanwhile (the `wave` lane)."),
    "ci-queue-then-main": (
        "Do not re-run the suite on the push to the default branch for a commit the merge queue already passed",
        "A merge queue tests the candidate commit, and that exact commit is what lands on the default branch, so the "
        "push run tests nothing new. On `push`, skip the heavy jobs when a successful `merge_group` run of this "
        "workflow exists for `github.sha` (`actions/workflows/<file>/runs?event=merge_group&head_sha=<sha>&status="
        "success`): `templates/github/autopilot/skip_duplicate_main.py` makes that check for a repo with the gate "
        "installed, an inline `gh api` step does it for a `pipeline: own` or `none` repo. Only where the queue run "
        "executes the heavy jobs (a job conditioned on the event not being merge_group makes the queue run a "
        "false pass, and then the push run must stay). Keep the aggregate check reporting "
        "green on a skip, so a release keyed on the push run still goes, and say in the run summary that the suite "
        "was skipped and why."),
    "ci-pr-selection-queue-full": (
        "Run the narrowed selection on a pull request and the full suite in the merge queue",
        "Where a merge queue runs the full suite on every candidate before it lands, the pull request run is not the "
        "last line of defence: let it run the tests its change reaches (a changed-files or import-graph selection, "
        "or the quick suite) and keep `merge_group`, the push to the default branch and the nightly on the full "
        "suite. A selection that falls back to the full suite on any shared path never narrows: measure how often "
        "it does."),
    "ci-pr-cancel-superseded": (
        "Cancel a pull request's run when a newer push replaces it",
        "Add a top-level `concurrency:` keyed on the pull request number with `cancel-in-progress: true`."),
    "ci-fail-fast": (
        "Run the cheap checks first; start the expensive jobs only when they pass",
        "Give each matrix or long job `needs: [<lint/typecheck job>]`, so a red lint or typecheck stops the run "
        "before the shards start. Costs that job's duration in wall-clock time on green runs."),
    "ci-skip-docs-only": (
        "Do not run the full suite for changes that touch only docs or reference files",
        "Add a first `changes` job (`git diff --no-renames --name-only` against the base: without `--no-renames` a "
        "code file renamed to `.md` lists only the `.md`) and `if:` the heavy jobs on its output. Before calling a "
        "`.md` file docs, check whether a test reads it; if one does, that path is code. Wireframes, static HTML "
        "prototypes and reference folders count as docs when nothing builds, imports or reads them: name those paths "
        "rather than matching every `.html` file, and treat anything a gate or test reads (requirements, rules, test "
        "cases) as code. Keep the secret scan and any conflict-marker or naming check running on these changes, since "
        "a prototype can carry a leaked key or an old name. Do not skip a matrix job with "
        "a job-level `if:`: it reports one check under its unexpanded name, so a required `test shard 1/6` never "
        "reports and the pull request blocks. Let those jobs run with `if: ${{ !cancelled() }}`, put the condition on "
        "every step, and make each service image empty when skipping (`image: ${{ <condition> && 'postgres:16' || '' "
        "}}`), since an empty image starts no service. Keep the aggregate required check reporting so the merge is "
        "not blocked. A `paths-ignore` on a workflow that holds a required check would leave the check missing and "
        "block docs-only pull requests."),
    "ci-poll-frequency": (
        "Poll on a schedule no more often than every %d minutes" % POLL_MIN,
        "Every scheduled run is at least one billed minute per job; prefer an event trigger, else `*/%d`." % POLL_MIN),
    "ci-fold-small-jobs": (
        "Fold the jobs that finish in a minute or two into one job",
        "GitHub bills every job at least one minute, and each job pays its own checkout and install, so three or "
        "more jobs that bill a minute or two per run cost more in setup than in work. Make them steps of one job; "
        "keep the required check's name on the job that remains, and the order the steps needed."),
    "ci-pr-hold-superseded": (
        "Hold the expensive jobs for a few minutes on a pull request, so a quick follow-up push cancels the run "
        "before they start",
        "Add a `hold` job (`sleep 300`) on `pull_request` that the matrix and the other long jobs `need`, with "
        "`cancel-in-progress` keyed on the pull request number: a push within the hold cancels a run that has spent "
        "five runner-minutes, not a hundred. The hold is billed on every run, so this is reported only when the "
        "minutes it saves on runs superseded within the hold exceed what every run pays for it."),
    "ci-shard-balance": (
        "Balance the test shards",
        "Shards are billed for their own time, so balance saves wall-clock time, not minutes -- but the slowest "
        "shard names the suites worth speeding up. Split by recorded durations instead of file count."),
    "ci-waste": (
        "Stop paying for runs nobody uses",
        "Minutes on runs that were cancelled or failed (re-run attempts included): see the waste table; the fixes are "
        "ci-pr-cancel-superseded and ci-fail-fast."),
}


# ---------------------------------------------------------------- workflow text (indentation, no YAML library)

def top_block(text, key):
    """Lines under a top-level `key:` (the key line included), or [] when absent."""
    lines, out = text.splitlines(), []
    for i, ln in enumerate(lines):
        if re.match(r"""^["']?%s["']?\s*:""" % re.escape(key), ln):
            out.append(ln)
            for l2 in lines[i + 1:]:
                if l2.strip() and not l2.startswith((" ", "\t", "#", "-")):
                    break
                out.append(l2)
            return out
    return []


def jobs_of(text):
    """{job id: its lines} for the jobs under the top-level `jobs:`."""
    block = top_block(text, "jobs")[1:]
    code = [l for l in block if l.strip() and not l.lstrip().startswith("#")]
    if not code:
        return {}
    base = min(len(l) - len(l.lstrip()) for l in code)
    jobs, cur = {}, None
    for ln in block:
        ind = len(ln) - len(ln.lstrip())
        m = re.match(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(#.*)?$", ln)
        if m and ind == base:
            cur = m.group(1)
            jobs[cur] = []
        elif cur:
            jobs[cur].append(ln)
    return jobs


def _code(lines):
    return "\n".join(l.split(" #")[0] for l in lines if not l.lstrip().startswith("#"))


def crons(text):
    return re.findall(r"""cron\s*:\s*["']([^"']+)["']""", text)


def cron_every_min(expr):
    """Minutes between runs of a cron's minute field when it repeats within the hour, else None."""
    f = expr.split()[0] if expr.split() else ""
    if f == "*":
        return 1
    m = re.match(r"^\*/(\d+)$", f)
    if m:
        return int(m.group(1))
    if "," in f:
        return max(1, 60 // len(f.split(",")))
    return None


def lint(path, text, default_branch):
    """Standard-rule findings for one workflow file: [(rule id, evidence)]."""
    out = []
    trig = audit.parse_triggers(text)
    on_pr = trig.get("pull_request") or trig.get("pull_request_target")
    conc = _code(top_block(text, "concurrency"))
    jobs = jobs_of(text)
    job_conc = " ".join(_code(j) for j in jobs.values() if "concurrency" in _code(j))
    if audit.push_to_branch(trig, default_branch) and len(jobs) >= 2:
        if not conc:
            out.append(("ci-main-batched", "pushes to %s run with no concurrency group: every merge is a full run"
                        % default_branch))
        elif "github.sha" in conc:
            out.append(("ci-main-batched", "the concurrency group falls back to github.sha on push, so each merge to "
                        "%s gets its own group and its own full run" % default_branch))
    queued = re.search(r"\bmerge_group\b", _code(top_block(text, "on")))
    code = _code(text.splitlines())
    deduped = re.search(r"skip_duplicate_main\.py|event=merge_group|--event merge_group", code)
    partial_queue = re.search(r"""event_name\s*!=\s*['"]merge_group['"]""", code)  # the queue run skips jobs
    if queued and audit.push_to_branch(trig, default_branch) and len(jobs) >= 2 and not deduped and not partial_queue:
        out.append(("ci-queue-then-main", "runs on merge_group and again on push to %s: the commit the queue passed "
                    "is tested twice" % default_branch))
    if on_pr and not re.search(r"cancel-in-progress\s*:\s*(true|\$\{\{)", conc + " " + job_conc):
        out.append(("ci-pr-cancel-superseded", "no cancel-in-progress: a new push to a pull request leaves the old "
                    "run going"))
    heavy = [j for j, ls in jobs.items() if re.search(r"^\s*matrix\s*:", "\n".join(ls), re.M)]
    loose = [j for j in heavy if not re.search(r"^\s*needs\s*:", _code(jobs[j]), re.M)]
    if loose and len(jobs) >= 3:
        out.append(("ci-fail-fast", "matrix job%s %s start%s with no `needs:`, in parallel with the cheap checks"
                    % ("s" if len(loose) > 1 else "", ", ".join(loose), "" if len(loose) > 1 else "s")))
    if on_pr and len(jobs) >= 3:
        pr_block = _code(top_block(text, "on"))
        filtered = re.search(r"^\s*paths(-ignore)?\s*:", pr_block, re.M)
        detects = re.search(r"paths-filter|changed-files|diff\b[^\n]*--name-only|needs\.changes\.outputs|dorny/", text)
        if not filtered and not detects:
            out.append(("ci-skip-docs-only", "every pull request runs every job, whatever it changed"))
    for c in crons(text):
        every = cron_every_min(c)
        if every is not None and every < POLL_MIN:
            out.append(("ci-poll-frequency", "schedule `%s` runs every %d min (%d runs a day)"
                        % (c, every, 24 * 60 // every)))
    return [{"rule": r, "workflow": path, "evidence": e} for r, e in out]


# ---------------------------------------------------------------- measurement

def os_of(labels):
    s = " ".join(labels or []).lower()
    if "self-hosted" in s:
        return None  # not billed by GitHub
    return "macos" if "macos" in s else "windows" if "windows" in s else "linux"


def billed_jobs(jobs):
    """[(job name without matrix suffix, leg name, os, billed minutes, raw minutes)] for the jobs GitHub bills."""
    out = []
    for j in jobs:
        a, b = audit.parse_ts(j.get("started_at")), audit.parse_ts(j.get("completed_at"))
        if not a or not b or j.get("conclusion") == "skipped":
            continue
        osn = os_of(j.get("labels"))
        if osn is None:
            continue
        raw = max(0.0, (b - a).total_seconds() / 60.0)
        out.append((MATRIX_RE.sub("", j.get("name") or "?"), j.get("name") or "?", osn, max(1, math.ceil(raw)), raw))
    return out


def list_runs(repo, now, log=lambda m: None):
    """Every run created in the last 30 days:
    [(id, workflow path, event, conclusion, created, wall minutes, attempt, head sha, head branch)]."""
    def page(q, n):
        return audit.gh_api("repos/%s/actions/runs?per_page=100&page=%d&created=%s" % (repo, n, q)) or {}

    def take(q):
        first = page(q, 1)
        total, got, n = first.get("total_count", 0), list(first.get("workflow_runs") or []), 1
        while len(got) < min(total, LIST_CAP) and n < 10:
            n += 1
            more = page(q, n).get("workflow_runs") or []
            if not more:
                break
            got += more
        return total, got

    start = (now - dt.timedelta(days=30)).date()
    days = [start + dt.timedelta(days=d) for d in range(31) if start + dt.timedelta(days=d) <= now.date()]

    def one_day(day):
        total, got = take(day.isoformat())
        if total > LIST_CAP:  # split the day by hour
            got = [r for h in range(24) for r in take("%sT%02d:00:00Z..%sT%02d:59:59Z" % (day, h, day, h))[1]]
        log("runs %s: %d" % (day, len(got)))
        return got

    out = []
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for got in pool.map(one_day, days):
            for r in got:
                c = audit.parse_ts(r.get("created_at"))
                if c and now - c <= dt.timedelta(days=30):
                    out.append((r["id"], audit.wf_key(r), r.get("event") or "?", r.get("conclusion") or "running",
                                c, audit.run_minutes(r), r.get("run_attempt") or 1, r.get("head_sha") or "",
                                r.get("head_branch") or ""))
    return list({r[0]: r for r in out}.values())


def quick_superseded(runs, hold=HOLD_MIN):
    """Ids of the cancelled pull-request runs that a later run of the same workflow on the same branch replaced
    within `hold` minutes: the runs a hold before the expensive jobs would have made cheap."""
    by_branch = {}
    for r in runs:
        if r[2] == "pull_request" and len(r) > 8 and r[8]:
            by_branch.setdefault((r[1], r[8]), []).append(r)
    out = set()
    for rs in by_branch.values():
        rs.sort(key=lambda r: r[4])
        for i, r in enumerate(rs[:-1]):
            if r[3] == "cancelled" and rs[i + 1][4] - r[4] <= dt.timedelta(minutes=hold):
                out.add(r[0])
    return out


def bucket_of(age_days):
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= age_days < hi:
            return i
    return len(BUCKETS) - 1


def evenly(items, k):
    if len(items) <= k:
        return list(items)
    step = len(items) / float(k)
    return [items[int(i * step)] for i in range(k)]


def measure(repo, runs, now, samples=SAMPLES, log=lambda m: None):
    """Billed minutes per stratum, extrapolated. Returns the cost tables for each window."""
    strata = {}
    quick = quick_superseded(runs)
    for r in runs:
        _id, wf, ev, con, created = r[:5]
        if _id in quick:
            con = "cancelled-quick"  # priced apart: these billed far less than a run cancelled late
        age = (now - created).total_seconds() / 86400.0
        strata.setdefault((wf, ev, con, bucket_of(age)), []).append(r)
    picks = {k: evenly(sorted(v, key=lambda r: r[4]), samples) for k, v in strata.items()}
    todo = [(k, r) for k, rs in picks.items() for r in rs if r[3] != "running"]
    log("reading jobs of %d sampled runs (of %d)" % (len(todo), len(runs)))

    def jobs(run_id):
        try:
            data = audit.gh_api("repos/%s/actions/runs/%s/jobs?per_page=100&filter=all" % (repo, run_id)) or {}
            return billed_jobs(data.get("jobs") or [])
        except audit.GhError:
            return None

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        got = list(pool.map(lambda kr: (kr[0], kr[1], jobs(kr[1][0])), todo))
    per = {}  # stratum -> list of sampled job lists
    for k, r, js in got:
        if js is not None:
            per.setdefault(k, []).append(js)

    tables = {w: {"by_workflow": {}, "by_event": {}, "by_job": {}, "by_os": {}, "waste": {}, "runs": 0,
                  "minutes": 0.0, "legs": {}, "by_event_con": {}, "runs_by": {}, "job_runs": {}, "sampled_runs": {}}
              for w in WINDOWS}
    for k, rs in strata.items():
        wf, ev, con, b = k
        samp = per.get(k)
        if not samp:
            continue
        weight = len(rs) / float(len(samp))
        for w in WINDOWS:
            if BUCKETS[b][0] >= w:
                continue
            t = tables[w]
            t["runs"] += len(rs)
            _add(t["runs_by"], "%s %s %s" % (wf, ev, con), len(rs))
            for js in samp:
                _add(t["sampled_runs"], wf, 1)
                per_job = {}
                for name, leg, osn, billed, _raw in js:
                    m = billed * weight
                    t["minutes"] += m
                    _add(t["by_workflow"], wf, m)
                    _add(t["by_event"], "%s %s" % (wf, ev), m)
                    _add(t["by_event_con"], "%s %s %s" % (wf, ev, con), m)
                    _add(t["by_job"], "%s :: %s" % (wf, name), m)
                    _add(t["by_os"], osn, m)
                    _add(per_job, name, billed)
                    if con.split("-")[0] in ("cancelled", "failure", "timed_out"):
                        _add(t["waste"], con.split("-")[0], m)
                    if leg != name:
                        t["legs"].setdefault("%s :: %s" % (wf, name), {}).setdefault(leg, []).append(billed)
                for name, mins in per_job.items():
                    t["job_runs"].setdefault("%s :: %s" % (wf, name), []).append(mins)
    return tables, strata


def batched_runs(times, wall_min):
    """Runs GitHub would start for these push times with one run going and only the newest push waiting."""
    runs, busy, pending = 0, None, False
    for t in times:
        if pending and busy <= t:  # the waiting push started when the last run ended
            runs, busy, pending = runs + 1, busy + dt.timedelta(minutes=wall_min), False
        if busy is None or t >= busy:
            runs, busy = runs + 1, t + dt.timedelta(minutes=wall_min)
        else:
            pending = True
    return runs + (1 if pending else 0)


def _add(d, k, v):
    d[k] = d.get(k, 0.0) + v


# ---------------------------------------------------------------- price, savings, report

def billing_price(owner, now):
    """The org's own Linux price per minute from the billing API, or None (needs admin:org)."""
    for path in ("organizations/%s/settings/billing/usage?year=%d&month=%d" % (owner, now.year, now.month),
                 "users/%s/settings/billing/usage?year=%d&month=%d" % (owner, now.year, now.month)):
        try:
            items = (audit.gh_api(path) or {}).get("usageItems") or []
        except audit.GhError:
            continue
        for it in items:
            if it.get("product") == "actions" and "linux" in (it.get("sku") or "").lower() and it.get("pricePerUnit"):
                return float(it["pricePerUnit"])
    return None


def usd(t, linux_price):
    by_os = t["by_os"]
    total = sum(by_os.values()) or 1.0
    price = sum((linux_price if o == "linux" and linux_price else PRICE[o]) * m for o, m in by_os.items()) / total
    return price


def savings(rule, finding, t, strata, window, now, default_branch="main"):
    """Billed minutes in `window` the rule's fix would save, from the measurement. None = not measurable here."""
    wf = finding["workflow"].split("/")[-1]
    ev = t["by_event"]
    if "_minutes" in finding:  # a measured rule priced itself
        return finding["_minutes"]
    if rule == "ci-queue-then-main":
        # the same runs the window's minutes were built from: strata whose age bucket starts inside the window
        inside = [(k, rs) for k, rs in strata.items() if k[0] == wf and BUCKETS[k[3]][0] < window]
        passed = {r[7] for (_w, e, c, _b), rs in inside if e == "merge_group" and c == "success"
                  for r in rs if len(r) > 7 and r[7]}
        pushes = [r for (_w, e, _c, _b), rs in inside if e == "push" for r in rs]
        if not pushes or not passed:
            return None
        # only a push to the default branch is skipped; the workflow may also run on pushes to the queue's own
        # branches, with the candidate's sha, and those stay
        twice = sum(1 for r in pushes if len(r) > 8 and r[8] == default_branch and r[7] in passed)
        return ev.get("%s push" % wf, 0.0) / float(len(pushes)) * twice
    if rule == "ci-main-batched":
        cut = now - dt.timedelta(days=window)
        pushes = [r for (w, e, _c, _b), rs in strata.items() if w == wf and e == "push" for r in rs if r[4] >= cut]
        mins = ev.get("%s push" % wf, 0.0)
        if not pushes or not mins:
            return None
        walls = sorted(r[5] for r in pushes if r[5] > 0)
        wall = walls[len(walls) // 2] if walls else 30.0
        return mins * max(0.0, 1.0 - batched_runs(sorted(r[4] for r in pushes), wall) / float(len(pushes)))
    if rule == "ci-fail-fast":
        return t["waste"].get("failure", 0.0) * 0.6  # the shards' share of a failed run, roughly
    if rule == "ci-poll-frequency":
        m = ev.get("%s schedule" % wf, 0.0)
        every = min([cron_every_min(c) or 60 for c in re.findall(r"`([^`]+)`", finding["evidence"])] or [60])
        return m * max(0.0, 1.0 - every / float(POLL_MIN))
    if rule == "ci-pr-cancel-superseded":
        return None  # what would have been cancelled is not in the run list
    return None


def per_run(t, wf, ev, con):
    """(average billed minutes of one such run, how many there were) in the window."""
    key = "%s %s %s" % (wf, ev, con)
    n = t["runs_by"].get(key, 0)
    return (t["by_event_con"].get(key, 0.0) / n if n else 0.0), n


def pr_vs_queue(t):
    """ci-pr-selection-queue-full: a passing pull-request run bills about what a merge-queue run bills."""
    out = []
    for wf in t["by_workflow"]:
        pr, npr = per_run(t, wf, "pull_request", "success")
        mq, nmq = per_run(t, wf, "merge_group", "success")
        if npr >= 5 and nmq >= 5 and mq > 0 and pr >= 0.8 * mq:
            out.append({"rule": "ci-pr-selection-queue-full", "workflow": wf,
                        "evidence": "a passing pull-request run bills %.0f min and a merge-queue run %.0f: the queue "
                                    "already runs the full suite and the pull request repeats it" % (pr, mq),
                        "_minutes": t["by_event_con"].get("%s pull_request success" % wf, 0.0) * 0.5})
    return out


FOLD_EXEMPT = {"changes", "dedupe", "hold"}  # the jobs the standard fixes add: they stay separate by design


def fold_exempt(text):
    """Job ids and names in one workflow that ci-fold-small-jobs leaves alone: the standard fixes' helper jobs and
    any aggregate job that exists to `need` three or more others."""
    out = set(FOLD_EXEMPT)
    for job, lines in jobs_of(text).items():
        code = _code(lines)
        name = re.search(r"^[ \t]*name[ \t]*:[ \t]*(.+)$", code, re.M)
        m = re.search(r"^[ \t]*needs[ \t]*:[ \t]*(.*)$", code, re.M)
        if not m:
            continue
        needed = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]*", m.group(1))
        if not m.group(1).strip():  # a list on the following lines
            needed = re.findall(r"^[ \t]*-[ \t]*([A-Za-z0-9_][A-Za-z0-9_-]*)", code[m.end():], re.M)
        if len(needed) >= 3 or job in FOLD_EXEMPT:
            out.add(job)
            if name:
                out.add(name.group(1).strip().strip("'\""))
    return out


def small_jobs(t, exempt=None):
    """ci-fold-small-jobs: three or more jobs of one workflow that bill a minute or two per run, each present in
    at least half the sampled runs; `exempt` maps a workflow to the job names left alone (fold_exempt)."""
    out = []
    for wf, n in t["sampled_runs"].items():
        means = {}
        skip = (exempt or {}).get(wf, set())
        for key, vals in t["job_runs"].items():
            w, name = key.split(" :: ", 1)
            if w == wf and name not in skip and len(vals) >= 0.5 * n and sum(vals) / float(len(vals)) <= 2.0:
                means[name] = sum(vals) / float(len(vals))
        if len(means) >= 3:
            mins = sum(t["by_job"].get("%s :: %s" % (wf, name), 0.0) for name in means)
            out.append({"rule": "ci-fold-small-jobs", "workflow": wf,
                        "evidence": "%d jobs bill a minute or two each per run (%s): each pays its own checkout and "
                                    "install" % (len(means), ", ".join(sorted(means))),
                        "_minutes": mins * (len(means) - 1) / float(len(means))})
    return out


def hold_net(t, hold=HOLD_MIN):
    """ci-pr-hold-superseded: what a hold before the expensive jobs saves on the runs superseded within it (their
    own sampled minutes, the `cancelled-quick` stratum of quick_superseded), net of the hold every pull-request run
    pays. Reported only when it pays."""
    out = []
    for wf in t["by_workflow"]:
        avg, quick = per_run(t, wf, "pull_request", "cancelled-quick")
        _late, late = per_run(t, wf, "pull_request", "cancelled")
        n_pr = sum(n for key, n in t["runs_by"].items() if key.startswith("%s pull_request " % wf))
        net = quick * max(0.0, avg - hold) - hold * (n_pr - quick)
        if quick and net > 0:
            out.append({"rule": "ci-pr-hold-superseded", "workflow": wf,
                        "evidence": "%d of %d cancelled pull-request runs were superseded within %d minutes and had "
                                    "billed %.0f minutes each on average; a %d-minute hold saves about %.0f min net of "
                                    "what every pull-request run pays for it" % (quick, quick + late, hold, avg, hold, net),
                        "_minutes": net})
    return out


def measured_findings(t, exempt=None):
    out = []
    for key, legs in t["legs"].items():
        avgs = {leg: sum(v) / float(len(v)) for leg, v in legs.items() if v}
        if len(avgs) >= 2:
            hi, lo = max(avgs.values()), min(avgs.values())
            if hi > 5 and lo > 0 and hi / lo >= 1.8:
                slow = max(avgs, key=avgs.get)
                out.append({"rule": "ci-shard-balance", "workflow": key.split(" :: ")[0],
                            "evidence": "%s: slowest leg %s averages %.0f min, fastest %.0f" % (key, slow, hi, lo)})
    waste = sum(t["waste"].values())
    if t["minutes"] and waste / t["minutes"] > 0.2:
        out.append({"rule": "ci-waste", "workflow": "*",
                    "evidence": "%.0f%% of billed minutes went to runs that were cancelled or failed" %
                                (100 * waste / t["minutes"])})
    out += pr_vs_queue(t) + small_jobs(t, exempt) + hold_net(t)
    return out


def top(d, n=12):
    return [{"name": k, "minutes": round(v)} for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]


def cost_repo(repo, now=None, samples=SAMPLES, wf_text=None, default_branch=None, log=lambda m: None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if default_branch is None or wf_text is None:
        meta = audit.gh_api("repos/%s" % repo) or {}
        default_branch = meta.get("default_branch") or "main"
        wf_text = {}
        for w in (audit.gh_api("repos/%s/actions/workflows" % repo) or {}).get("workflows", []):
            if w.get("state") == "active" and w.get("path", "").startswith(".github/workflows/"):
                try:
                    wf_text[w["path"]] = audit.fetch_text(repo, w["path"]) or ""
                except audit.GhError:  # listed but gone from the default branch
                    pass
    runs = list_runs(repo, now, log)
    tables, strata = measure(repo, runs, now, samples, log)
    lin = billing_price(repo.split("/")[0], now)
    lints = [f for p, txt in sorted(wf_text.items()) for f in lint(p, txt, default_branch)]
    exempt = {p.split("/")[-1]: fold_exempt(txt) for p, txt in wf_text.items()}
    windows = {}
    for w in WINDOWS:
        t = tables[w]
        price = usd(t, lin)
        findings = []
        floor = max(100.0, 0.05 * t["minutes"])  # a rule on a workflow that costs next to nothing is noise
        keep = [f for f in lints if f["rule"] == "ci-poll-frequency"
                or t["by_workflow"].get(f["workflow"].split("/")[-1], 0.0) >= floor]
        for f in keep + measured_findings(t, exempt):
            s = savings(f["rule"], f, t, strata, w, now, default_branch)
            f = {k: v for k, v in f.items() if k != "_minutes"}
            findings.append(dict(f, title=STANDARD_RULES[f["rule"]][0], fix=STANDARD_RULES[f["rule"]][1],
                                 saves_minutes=None if s is None else round(s),
                                 saves_usd=None if s is None else round(s * price, 2)))
        windows[str(w)] = {
            "runs": t["runs"], "billed_minutes": round(t["minutes"]), "usd": round(t["minutes"] * price, 2),
            "usd_per_month": round(t["minutes"] * price * 30.0 / w, 2), "price_per_minute": round(price, 4),
            "by_workflow": top(t["by_workflow"]), "by_event": top(t["by_event"]), "by_job": top(t["by_job"], 15),
            "by_os": top(t["by_os"]), "waste": top(t["waste"]), "findings": findings}
    return {"repo": repo, "generated_at": audit.iso(now), "samples_per_stratum": samples,
            "price_source": "billing API" if lin else "list price estimate", "runs_listed": len(runs),
            "rules": {k: v[0] for k, v in STANDARD_RULES.items()}, "windows": windows}


def summarize(c):
    lines = ["%s -- CI cost (%s, %d runs listed)" % (c["repo"], c["price_source"], c["runs_listed"])]
    for w in WINDOWS:
        x = c["windows"][str(w)]
        lines.append("  %2dd: %7d billed min  $%9.2f   (= $%.0f / month)" % (w, x["billed_minutes"], x["usd"],
                                                                         x["usd_per_month"]))
    x = c["windows"]["7"]
    lines.append("  last 7 days, top workflows: " + ", ".join("%s %d" % (r["name"], r["minutes"])
                                                               for r in x["by_workflow"][:5]))
    lines.append("  top jobs: " + ", ".join("%s %d" % (r["name"], r["minutes"]) for r in x["by_job"][:5]))
    for f in x["findings"]:
        lines.append("  [%s] %s -- %s%s" % (f["rule"], f["workflow"], f["evidence"],
                                            "" if not f["saves_usd"] else " (saves ~$%.0f in 7 days)" % f["saves_usd"]))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", required=True, metavar="OWNER/NAME")
    ap.add_argument("--samples", type=int, default=SAMPLES, help="runs read per stratum (default %d)" % SAMPLES)
    ap.add_argument("--out", metavar="FILE")
    args = ap.parse_args(argv)
    out = []
    for repo in args.repo:
        c = cost_repo(repo, samples=args.samples, log=lambda m, repo=repo: print("    %s: %s" % (repo, m), file=sys.stderr))
        out.append(c)
        print(summarize(c))
    if args.out:
        Path(args.out).write_text(json.dumps({"repos": out}, indent=2, default=str) + "\n")
        print("\nFull results: %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
