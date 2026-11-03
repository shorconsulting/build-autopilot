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
MATRIX_RE = re.compile(r"\s*\(.*\)$")

STANDARD_RULES = {  # id -> (title, the fix). Order is the order findings are shown in.
    "ci-main-batched": (
        "Batch runs on the default branch instead of testing every merge",
        "Key the push concurrency group on the branch, not the commit, and let pushes queue: "
        "`group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}` with "
        "`cancel-in-progress: ${{ github.event_name == 'pull_request' }}`. GitHub keeps one run going and only the "
        "newest push waiting, so each run covers everything merged meanwhile (the `wave` lane)."),
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
    """Every run created in the last 30 days: [(id, workflow path, event, conclusion, created, wall minutes)]."""
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
                                c, audit.run_minutes(r), r.get("run_attempt") or 1))
    return list({r[0]: r for r in out}.values())


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
    for r in runs:
        _id, wf, ev, con, created, _wall, _att = r
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
                  "minutes": 0.0, "legs": {}} for w in WINDOWS}
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
            for js in samp:
                for name, leg, osn, billed, _raw in js:
                    m = billed * weight
                    t["minutes"] += m
                    _add(t["by_workflow"], wf, m)
                    _add(t["by_event"], "%s %s" % (wf, ev), m)
                    _add(t["by_job"], "%s :: %s" % (wf, name), m)
                    _add(t["by_os"], osn, m)
                    if con in ("cancelled", "failure", "timed_out"):
                        _add(t["waste"], con, m)
                    if leg != name:
                        t["legs"].setdefault("%s :: %s" % (wf, name), {}).setdefault(leg, []).append(billed)
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


def savings(rule, finding, t, strata, window, now):
    """Billed minutes in `window` the rule's fix would save, from the measurement. None = not measurable here."""
    wf = finding["workflow"].split("/")[-1]
    ev = t["by_event"]
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


def measured_findings(t):
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
    windows = {}
    for w in WINDOWS:
        t = tables[w]
        price = usd(t, lin)
        findings = []
        floor = max(100.0, 0.05 * t["minutes"])  # a rule on a workflow that costs next to nothing is noise
        keep = [f for f in lints if f["rule"] == "ci-poll-frequency"
                or t["by_workflow"].get(f["workflow"].split("/")[-1], 0.0) >= floor]
        for f in keep + measured_findings(t):
            s = savings(f["rule"], f, t, strata, w, now)
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
