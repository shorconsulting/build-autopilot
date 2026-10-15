#!/usr/bin/env python3
"""Build Autopilot audit: where does a repo's build time go, and what could run without a human click?

    audit.py --repo OWNER/NAME [--repo OWNER/NAME ...] [--org ORG] [--since 30d|YYYY-MM-DD]
             [--transcripts DIR ...] [--checkout OWNER/NAME=PATH ...] --out audit.json

STRICTLY READ-ONLY. It only runs `gh` read calls (GET requests, `pr list`, `issue list`, `repo list`) and read-only
`git remote get-url`. It never writes to GitHub, never edits a repo, never changes a setting. Claude Code transcripts
are only counted: no transcript text is ever written to the output.

Needs python3 (3.9+, standard library only), gh (logged in) and git.
"""
import argparse
import base64
import datetime as dt
import fnmatch
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - python < 3.9
    ZoneInfo = None

MAX_PRS = 200
MAX_RUNS = 500
RUNS_PER_PAGE = 100
DEFAULT_TRANSCRIPT_DIRS = ["~/.claude/projects"]

BOT_WORDS = ("claude", "sling", "coderabbit", "qodo", "copilot")
TEST_FIX_RE = re.compile(r"(flak|intermittent|time of day|clock|fix .*test|test.*fail|CI:)", re.I)
DEPLOY_RE = re.compile(r"deploy|release|publish", re.I)
AI_REVIEW_RE = re.compile(r"anthropic|claude|review", re.I)
SLING_LABELS = ("auto-implement", "sling")


# ---------------------------------------------------------------- gh (the one place that talks to GitHub)

class GhError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def gh(args):
    """Run `gh <args>` and return stdout as text. The only function that touches the network; tests replace it."""
    try:
        res = subprocess.run(["gh"] + list(args), capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        raise GhError("gh is not installed")
    except subprocess.TimeoutExpired:
        raise GhError("gh timed out: gh %s" % " ".join(args[:3]))
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        m = re.search(r"HTTP (\d{3})", err)
        status = int(m.group(1)) if m else None
        if status is None and "Could not resolve to a Repository" in err:
            status = 404
        raise GhError(err.splitlines()[-1] if err else "gh failed", status)
    return res.stdout


def gh_json(args):
    out = gh(args)
    return json.loads(out) if out.strip() else None


def gh_api(path):
    return gh_json(["api", path])


def gh_paged(path, key, cap, per_page=RUNS_PER_PAGE):
    """GET a paged list endpoint by hand so a repo with thousands of items stops at `cap`.

    Returns (items, truncated). `key` names the list inside the response object (None when the response is a list)."""
    items, page = [], 1
    sep = "&" if "?" in path else "?"
    while len(items) < cap:
        data = gh_api("%s%sper_page=%d&page=%d" % (path, sep, per_page, page))
        batch = data if key is None else (data or {}).get(key, [])
        if not batch:
            return items, False
        items.extend(batch)
        if len(batch) < per_page:
            return items, False
        page += 1
    return items[:cap], True


# ---------------------------------------------------------------- small helpers

def parse_ts(s):
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_tz(name=None):
    """The time zone "overnight" is judged in: --tz / AUTOPILOT_TZ if given, else this computer's own."""
    name = name or os.environ.get("AUTOPILOT_TZ")
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return dt.datetime.now().astimezone().tzinfo


def parse_since(text, now):
    """'30d', '4w' or 'YYYY-MM-DD' -> (since datetime in UTC, window length in days)."""
    text = (text or "30d").strip()
    m = re.fullmatch(r"(\d+)\s*([dw])", text)
    if m:
        days = int(m.group(1)) * (7 if m.group(2) == "w" else 1)
        since = now - dt.timedelta(days=days)
    else:
        try:
            since = dt.datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            raise SystemExit("--since must look like 30d, 4w or 2026-09-15 (got %r)" % text)
    days = max(1, int(round((now - since).total_seconds() / 86400)))
    return since, days


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def r1(x):
    return None if x is None else round(x, 1)


def med_p90(values):
    return {"median": r1(statistics.median(values)) if values else None, "p90": r1(percentile(values, 0.9))}


def minutes(a, b):
    return (b - a).total_seconds() / 60.0


def login_of(obj):
    a = (obj or {}).get("author") or {}
    return (a.get("login") or "").strip()


def is_bot(login):
    l = login.lower()
    return l.endswith("[bot]") or any(w in l for w in BOT_WORDS)


def spans_overnight(start, end, tz):
    """True when [start, end] overlaps local 00:00-07:00 on any day."""
    s, e = start.astimezone(tz), end.astimezone(tz)
    day = s.date() - dt.timedelta(days=1)
    last = e.date()
    while day <= last:
        w0 = dt.datetime(day.year, day.month, day.day, 0, 0, tzinfo=tz)
        w1 = dt.datetime(day.year, day.month, day.day, 7, 0, tzinfo=tz)
        if s < w1 and e > w0:
            return True
        day += dt.timedelta(days=1)
    return False


def hours_text(h):
    n = round(h, 1)
    n = int(n) if n == int(n) else n
    return "%s hour%s" % (n, "" if n == 1 else "s")


def num(x):
    """120.0 -> '120', 31.4 -> '31.4' (for sentences)."""
    return "%g" % x if x is not None else "n/a"


# ---------------------------------------------------------------- workflow files (light parsing, no yaml library)

def parse_triggers(text):
    """What starts this workflow? -> {'push': None | {'branches': [...] | None, 'tags': bool}, 'pull_request': bool,
    'workflow_dispatch': bool, 'workflow_run': bool, 'release': bool, 'schedule': bool}"""
    out = {"push": None, "pull_request": False, "workflow_dispatch": False, "workflow_run": False,
           "release": False, "schedule": False}
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        m = re.match(r"""^["']?on["']?\s*:\s*(.*?)\s*(#.*)?$""", ln)
        if m:
            start, rest = i, m.group(1)
            break
    if start is None:
        return out
    block = []
    if rest:  # on: push  |  on: [push, pull_request]  |  on: { push: ... }
        names = re.findall(r"[A-Za-z_]+", rest)
        for n in names:
            if n in out:
                out[n] = {"branches": None, "tags": False} if n == "push" else True
        if "branches" in names and out["push"]:
            out["push"]["branches"] = re.findall(r"[\w./*-]+", rest.split("branches", 1)[1].split("]")[0])[1:] or None
        return out
    for ln in lines[start + 1:]:
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            block.append(ln)
            continue
        if not ln.startswith((" ", "\t", "-")):
            break
        block.append(ln)
    indents = [len(l) - len(l.lstrip()) for l in block if l.strip() and not l.lstrip().startswith("#")]
    if not indents:
        return out
    base = min(indents)
    i = 0
    while i < len(block):
        ln = block[i]
        ind = len(ln) - len(ln.lstrip())
        key = ln.strip().lstrip("-").strip().split(":")[0].strip().strip("'\"")
        if ln.strip() and not ln.lstrip().startswith("#") and ind == base and key in out:
            if key == "push":
                sub = []
                j = i + 1
                while j < len(block):
                    l2 = block[j]
                    if l2.strip() and (len(l2) - len(l2.lstrip())) <= base and not l2.lstrip().startswith("#"):
                        break
                    sub.append(l2)
                    j += 1
                out["push"] = {"branches": _list_under(sub, "branches"), "tags": _list_under(sub, "tags") is not None}
            else:
                out[key] = True
        i += 1
    return out


def _list_under(sub, key):
    """Items of `key:` inside a block of lines: `key: [a, b]`, `key: a` or `key:` then `- a` lines."""
    for i, ln in enumerate(sub):
        m = re.match(r"^\s*%s\s*:\s*(.*?)\s*(#.*)?$" % re.escape(key), ln)
        if not m:
            continue
        rest = m.group(1)
        if rest:
            return [x.strip("'\" ") for x in rest.strip("[]").split(",") if x.strip("'\" ")]
        ind = len(ln) - len(ln.lstrip())
        items = []
        for l2 in sub[i + 1:]:
            if not l2.strip() or l2.lstrip().startswith("#"):
                continue
            if (len(l2) - len(l2.lstrip())) <= ind and not l2.lstrip().startswith("-"):
                break
            if l2.lstrip().startswith("-"):
                items.append(l2.lstrip()[1:].split("#")[0].strip().strip("'\""))
        return items
    return None


def push_to_branch(trig, branch):
    p = trig.get("push")
    if not p:
        return False
    if p["branches"] is None:
        return not p["tags"]  # a tags-only filter does not fire for ordinary pushes
    return any(fnmatch.fnmatch(branch, b) for b in p["branches"])


# ---------------------------------------------------------------- the pull-request side

def normalize_checks(rollup):
    """statusCheckRollup entries -> [{'name', 'ok', 'started', 'completed'}] (CheckRuns that finished)."""
    out = []
    for c in rollup or []:
        if c.get("__typename") not in (None, "CheckRun"):
            continue
        done = parse_ts(c.get("completedAt"))
        if not done or c.get("status") not in (None, "COMPLETED"):
            continue
        out.append({"name": c.get("name") or "", "workflow": c.get("workflowName") or "",
                    "ok": (c.get("conclusion") or "").upper() == "SUCCESS",
                    "conclusion": (c.get("conclusion") or "").lower(),
                    "started": parse_ts(c.get("startedAt")), "completed": done})
    return out


def checks_from_api(repo, sha):
    data = gh_api("repos/%s/commits/%s/check-runs?per_page=100" % (repo, sha)) or {}
    out = []
    for c in data.get("check_runs", []):
        if c.get("status") != "completed":
            continue
        out.append({"name": c.get("name") or "", "workflow": "", "ok": c.get("conclusion") == "success",
                    "conclusion": (c.get("conclusion") or "").lower(),
                    "started": parse_ts(c.get("started_at")), "completed": parse_ts(c.get("completed_at"))})
    return [c for c in out if c["completed"]]


def required_filter(checks, required):
    """Keep the checks that are required on the branch; fall back to every check when none can be matched."""
    if not required:
        return checks
    req = {r.lower() for r in required}
    hit = [c for c in checks if c["name"].lower() in req or ("%s / %s" % (c["workflow"], c["name"])).lower() in req]
    return hit or checks


FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}


def pr_metrics(prs, open_count, required_checks, since, runs_by_branch, tz):
    merged = []
    for p in prs:
        m, c = parse_ts(p.get("mergedAt")), parse_ts(p.get("createdAt"))
        if m and c and m >= since:
            merged.append((p, c, m))
    open_to_merge, ci_min, waits, wait_rows = [], [], [], []
    human = bot = unreviewed = test_fix = over2h = overnight = 0
    tested_ok = untested = 0
    fix_rounds = []
    for p, created, mergedat in merged:
        open_to_merge.append(max(0.0, minutes(created, mergedat)))
        if TEST_FIX_RE.search(p.get("title") or ""):
            test_fix += 1
        author = login_of(p)
        reviewed = False
        for item in list(p.get("reviews") or []) + list(p.get("comments") or []):
            who = login_of(item)
            if not who or who == author:
                continue
            if is_bot(who):
                bot += 1
            else:
                human += 1
            reviewed = True
        if not reviewed:
            unreviewed += 1
        shas = runs_by_branch.get(p.get("headRefName"))
        if shas:
            fix_rounds.append(len(shas))
        checks = p.get("_checks")
        if checks is None:
            checks = normalize_checks(p.get("statusCheckRollup"))
        checks = required_filter(checks, required_checks)
        good = [c["completed"] for c in checks if c["ok"]]
        done = [c["completed"] for c in checks if c["completed"]]
        if not checks:
            untested += 1
        elif good and not any(c.get("conclusion") in FAILED for c in checks) and max(done) <= mergedat + dt.timedelta(minutes=1):
            tested_ok += 1  # checks finished before the merge, none failed (skipped ones don't count against it)
        starts = [c["started"] for c in checks if c["started"]]
        if checks and starts:
            ci_min.append(max(0.0, minutes(min(starts), max(c["completed"] for c in checks))))
        if good:
            green = max(max(good), created)  # a PR that was tested before it was opened starts waiting when opened
            wait = max(0.0, minutes(green, mergedat))
            waits.append(wait)
            wait_rows.append((wait, p))
            if wait > 120:
                over2h += 1
            if wait > 240 and spans_overnight(green, mergedat, tz):
                overnight += 1
    wait_rows.sort(key=lambda r: -r[0])
    return {
        "merged": len(merged), "open": open_count,
        "open_to_merge_min": med_p90(open_to_merge),
        "ci_min": med_p90(ci_min),
        "wait_after_green_min": dict(med_p90(waits), total_hours=round(sum(waits) / 60.0, 1)),
        "waited_over_2h": over2h, "waited_overnight": overnight,
        "human_reviews": human, "bot_reviews": bot, "merged_without_review": unreviewed,
        "merged_after_tests_passed": tested_ok, "merged_without_tests": untested,
        "fix_rounds_median": statistics.median(fix_rounds) if fix_rounds else None,
        "test_fix_prs": test_fix,
        "examples_long_wait": [{"number": p["number"], "title": p.get("title"), "wait_hours": round(w / 60.0, 1)}
                               for w, p in wait_rows[:5] if w > 120],
    }, [w for w in waits], merged


# ---------------------------------------------------------------- CI side

def run_minutes(run):
    a = parse_ts(run.get("run_started_at") or run.get("created_at"))
    b = parse_ts(run.get("updated_at"))
    return max(0.0, minutes(a, b)) if a and b else 0.0


def wf_key(run):
    path = (run.get("path") or "").split("@")[0]
    return os.path.basename(path) if path else (run.get("name") or "unknown")


def ci_metrics(runs, merged, default_branch, n_merged):
    by_wf = {}
    for r in runs:
        by_wf.setdefault(wf_key(r), []).append(r)
    workflows = []
    for name, rs in sorted(by_wf.items(), key=lambda kv: -len(kv[1])):
        durs = [run_minutes(r) for r in rs if r.get("conclusion") in ("success", "failure", "timed_out")]
        fin = [r for r in rs if r.get("conclusion") in ("success", "failure", "timed_out")]
        failed = [r for r in fin if r.get("conclusion") != "success"]
        workflows.append({
            "name": name, "runs": len(rs),
            "median_min": r1(statistics.median(durs)) if durs else None,
            "p90_min": r1(percentile(durs, 0.9)),
            "failure_rate": round(len(failed) / len(fin), 3) if fin else 0.0,
            "cancelled": sum(1 for r in rs if r.get("conclusion") == "cancelled")})
    passed_pr = {(r.get("workflow_id"), r.get("head_sha")) for r in runs
                 if r.get("event") == "pull_request" and r.get("conclusion") == "success"}
    pr_by_sha = {}
    for p, _c, _m in merged:
        for sha in ((p.get("mergeCommit") or {}).get("oid"), p.get("headRefOid")):
            if sha:
                pr_by_sha[sha] = p
    dup_runs = dup_min = 0
    red = set()
    for r in runs:
        if r.get("event") != "push" or r.get("head_branch") != default_branch:
            continue
        if r.get("conclusion") in ("failure", "timed_out"):
            red.add(r.get("head_sha"))
        p = pr_by_sha.get(r.get("head_sha"))
        if p and (r.get("workflow_id"), p.get("headRefOid")) in passed_pr:
            dup_runs += 1
            dup_min += run_minutes(r)
    counted = [r for r in runs if r.get("event") in ("pull_request", "push", "pull_request_target", "merge_group")]
    return {
        "workflows": workflows,
        "runs_per_merged_pr": round(len(counted) / n_merged, 1) if n_merged else None,
        "duplicate_main_runs": dup_runs, "duplicate_main_minutes": round(dup_min),
        "red_main_commits": len(red),
    }


def branch_run_shas(runs):
    out = {}
    for r in runs:
        if r.get("event") == "pull_request" and r.get("head_branch") and r.get("head_sha"):
            out.setdefault(r["head_branch"], set()).add(r["head_sha"])
    return out


def deploy_metrics(workflows, wf_text, runs, merged, default_branch):
    cands = [w for w in workflows if w.get("state") == "active"
             and DEPLOY_RE.search((w.get("name") or "") + " " + (w.get("path") or ""))]
    if not cands:
        return {"workflow": None, "auto_on_main": False, "runs": 0, "median_merge_to_deploy_min": None,
                "note": "No deploy workflow found; deploys are manual"}
    cands.sort(key=lambda w: 0 if re.search("deploy", (w.get("name") or "") + (w.get("path") or ""), re.I) else 1)
    w = cands[0]
    fname = os.path.basename(w.get("path") or "")
    trig = parse_triggers(wf_text.get(w.get("path"), ""))
    auto = push_to_branch(trig, default_branch) or trig.get("workflow_run", False)
    druns = [r for r in runs if r.get("workflow_id") == w.get("id")]
    ok = sorted((r for r in druns if r.get("conclusion") == "success"), key=lambda r: r.get("run_started_at") or "")
    merge_times = sorted(parse_ts(p.get("mergedAt")) for p, _c, _m in merged if p.get("mergedAt"))
    lags, prev = [], None
    for r in ok:
        started, done = parse_ts(r.get("run_started_at") or r.get("created_at")), parse_ts(r.get("updated_at"))
        if not started or not done:
            continue
        pending = [m for m in merge_times if m <= started and (prev is None or m > prev)]
        if pending:
            lags.append(minutes(pending[0], done))
        prev = started
    if auto:
        note = "Deploys start by themselves when changes reach %s (%s; %d run%s in this window)." % (
            default_branch, fname, len(druns), "" if len(druns) == 1 else "s")
    else:
        note = ("A deploy workflow exists (%s) but it only runs when someone starts it, so every release waits "
                "for a person (%d run%s in this window)." % (fname, len(druns), "" if len(druns) == 1 else "s"))
    return {"workflow": fname, "auto_on_main": bool(auto), "runs": len(druns),
            "median_merge_to_deploy_min": r1(statistics.median(lags)) if lags else None, "note": note}


# ---------------------------------------------------------------- repo settings

def protection_info(repo, branch):
    """Returns (branch_protection dict, note or None)."""
    unknown = {"status": "unknown", "required_reviews": None, "required_checks": None, "enforce_admins": None}
    none = {"status": "none", "required_reviews": 0, "required_checks": [], "enforce_admins": False}
    try:
        d = gh_api("repos/%s/branches/%s/protection" % (repo, branch))
    except GhError as e:
        if e.status == 403:
            return unknown, "Branch protection could not be read (403: the token needs admin access)."
        if e.status != 404:
            return unknown, "Branch protection could not be read: %s" % e
        d = None
    info = dict(none)
    if d:
        rsc = d.get("required_status_checks") or {}
        checks = [c.get("context") if isinstance(c, dict) else c for c in (rsc.get("checks") or [])] \
            or list(rsc.get("contexts") or [])
        info = {"status": "set",
                "required_reviews": (d.get("required_pull_request_reviews") or {}).get("required_approving_review_count", 0)
                if d.get("required_pull_request_reviews") else 0,
                "required_checks": [c for c in checks if c],
                "enforce_admins": bool((d.get("enforce_admins") or {}).get("enabled"))}
    try:  # newer repos use rulesets instead of classic protection
        rules = gh_api("repos/%s/rules/branches/%s" % (repo, branch)) or []
    except GhError:
        rules = []
    for rule in rules:
        t, params = rule.get("type"), rule.get("parameters") or {}
        if t == "pull_request":
            info["required_reviews"] = max(info["required_reviews"] or 0, params.get("required_approving_review_count", 0))
            info["status"] = "set"
        elif t == "required_status_checks":
            info["required_checks"] = sorted(set(info["required_checks"]) |
                                             {c.get("context") for c in params.get("required_status_checks", []) if c.get("context")})
            info["status"] = "set"
    return info, None


def has_codeowners(repo):
    for p in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        try:
            gh_api("repos/%s/contents/%s" % (repo, p))
            return True
        except GhError:
            continue  # 404 = not there; anything else = cannot tell, treated as not there
    return False


def looks_like_ai_review(workflow, text):
    """A workflow that runs on pull requests and is a review: named like one, or uses a review/AI action.
    (A mere mention of Anthropic or Claude, for example a faked API key in a test job, does not count.)"""
    if not text or not parse_triggers(text)["pull_request"]:
        return False
    if AI_REVIEW_RE.search((workflow.get("name") or "") + " " + (workflow.get("path") or "")):
        return True
    for ln in text.splitlines():
        code = ln.split(" #")[0]
        if code.lstrip().startswith("#"):
            continue
        if re.match(r"\s*-?\s*uses:", code) and re.search(r"claude|anthropic|review|coderabbit|qodo|copilot", code, re.I):
            return True
        if re.match(r"\s*-?\s*name:", code) and re.search(r"review", code, re.I):
            return True
    return False


def fetch_text(repo, path):
    out = gh(["api", "-H", "Accept: application/vnd.github.raw+json", "repos/%s/contents/%s" % (repo, path)])
    return out


def sling_labels_used(repo, since):
    issues = gh_json(["issue", "list", "--repo", repo, "--state", "all", "--search", "created:>=%s" % since.date().isoformat(),
                      "--json", "labels", "--limit", "200"]) or []
    return any((lb.get("name") or "").lower() in SLING_LABELS for i in issues for lb in i.get("labels") or [])


# ---------------------------------------------------------------- Claude Code transcripts

APPROVAL_RE = re.compile(r"^(yes|yep|yeah|ok|okay|sure|go|go ahead|merge|deploy|approve|approved|do it|proceed|"
                         r"continue|lgtm|ship it)\b", re.I)
DENIAL_RE = re.compile(r"permission (for|to use)[^.]{0,80}(denied|not granted|blocked)|doesn.t want to proceed|"
                       r"denied by|user denied|requires (user )?approval|not allowed to use", re.I)
CATEGORIES = [  # first match wins, in this order
    ("merge_or_deploy_ok", re.compile(r"\b(merge|deploy|ship|push|release|go ahead)", re.I)),
    ("permission", re.compile(r"\b(permission|allow|approve the command)", re.I)),
    ("credentials_or_access", re.compile(r"\b(password|keys?|token|login|access|secret)", re.I)),
    ("design_choice", re.compile(r"\b(which|prefer|option|should i|approach)", re.I)),
    ("scope_or_clarify", re.compile(r"\b(scope|clarify|did you mean|include)", re.I)),
]
CATEGORY_NAMES = [c for c, _ in CATEGORIES] + ["other"]


def categorize(text):
    for name, rx in CATEGORIES:
        if rx.search(text or ""):
            return name
    return "other"


def new_interruptions():
    return {"sessions": set(), "days": set(), "questions": 0, "turns_ending_on_user": 0, "permission_denials": 0,
            "short_approvals": 0, "by_category": {c: 0 for c in CATEGORY_NAMES}}


def user_text(msg):
    """The words a human typed in a user event, or None for tool results and system injections."""
    c = (msg or {}).get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        text = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    else:
        return None
    text = text.strip()
    return None if not text or text.startswith("<") else text


def scan_transcript(path, since, acc, tz):
    seen_tools = set()
    session_fallback = os.path.basename(str(path))
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = parse_ts(e.get("timestamp"))
            kind = e.get("type")
            if not ts or ts < since or kind not in ("user", "assistant") or e.get("isSidechain"):
                continue
            acc["sessions"].add(e.get("sessionId") or session_fallback)
            acc["days"].add(ts.astimezone(tz).date())
            msg = e.get("message") or {}
            content = msg.get("content")
            if kind == "assistant" and isinstance(content, list):
                for it in content:
                    if not isinstance(it, dict) or it.get("type") != "tool_use":
                        continue
                    tid = it.get("id")
                    if tid in seen_tools:
                        continue
                    seen_tools.add(tid)
                    inp = it.get("input") or {}
                    if it.get("name") == "AskUserQuestion":
                        qs = inp.get("questions")
                        texts = [q.get("question", "") for q in qs if isinstance(q, dict)] if isinstance(qs, list) \
                            else [inp.get("question", "")]
                        for t in texts or [""]:
                            acc["questions"] += 1
                            acc["by_category"][categorize(t)] += 1
                    elif it.get("name") == "mcp__ccd_turn__wrap_up" and str(inp.get("ask") or "").strip():
                        acc["turns_ending_on_user"] += 1
                        acc["by_category"][categorize(str(inp.get("ask")))] += 1
            elif kind == "user":
                if isinstance(content, list):
                    for it in content:
                        if isinstance(it, dict) and it.get("type") == "tool_result" and it.get("is_error"):
                            tid = it.get("tool_use_id")
                            body = it.get("content")
                            if isinstance(body, list):
                                body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                            if ("denied:" + str(tid)) not in seen_tools and DENIAL_RE.search(str(body or "")[:400]):
                                seen_tools.add("denied:" + str(tid))
                                acc["permission_denials"] += 1
                                acc["by_category"]["permission"] += 1
                origin = (e.get("origin") or {}).get("kind")
                if e.get("isMeta") or (origin and origin != "human"):
                    continue
                text = user_text(msg)
                if text and len(text.split()) <= 6 and APPROVAL_RE.match(text.strip().lower()):
                    acc["short_approvals"] += 1


def interruption_summary(acc):
    days = len(acc["days"])
    total = acc["questions"] + acc["turns_ending_on_user"] + acc["permission_denials"]
    return {"sessions": len(acc["sessions"]), "days_active": days, "questions": acc["questions"],
            "turns_ending_on_user": acc["turns_ending_on_user"], "permission_denials": acc["permission_denials"],
            "short_approvals": acc["short_approvals"], "by_category": dict(acc["by_category"]),
            "per_active_day": round(total / days, 1) if days else 0.0}


def encode_path(p):
    """Claude Code's project-folder name for a path: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", p)


def parse_remote(url):
    m = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", (url or "").strip())
    return "%s/%s" % (m.group(1), m.group(2)) if m else None


_origin_cache = {}


def origin_repo(path):
    """OWNER/NAME of the checkout's `origin`, or None. Read-only."""
    if path in _origin_cache:
        return _origin_cache[path]
    repo = None
    try:
        res = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True, timeout=20)
        if res.returncode == 0:
            repo = parse_remote(res.stdout)
    except Exception:
        pass
    _origin_cache[path] = repo
    return repo


WORKTREE_MARKERS = ("--claude-worktrees-", "-worktrees-")


def slug_to_path(slug):
    """Walk the file system to find the folder a project slug was made from. Returns (path, leftover) where leftover
    is the unmatched tail of the slug ('' when the folder exists)."""
    best = ("/", slug[1:] if slug.startswith("-") else slug)

    def walk(cur, rem):
        nonlocal best
        if len(rem) < len(best[1]) or (len(rem) == len(best[1]) and len(cur) > len(best[0])):
            best = (cur, rem)
        if not rem:
            return True
        try:
            names = sorted(os.listdir(cur))
        except OSError:
            return False
        for n in names:
            enc = encode_path(n)
            if enc == rem or rem.startswith(enc + "-"):
                nxt = os.path.join(cur, n)
                if os.path.isdir(nxt) and walk(nxt, rem[len(enc) + 1:]):
                    return True
        return False

    if not slug.startswith("-"):
        return None, slug
    walk("/", slug[1:])
    return best


def slug_repo(slug, repos, checkouts):
    """Which requested repo does this transcript folder belong to? (explicit --checkout, then the checkout's origin
    remote, then the repo name in the folder name). Returns the repo as spelled in `repos`, or None."""
    wanted = {r.lower(): r for r in repos}
    for repo, path in checkouts.items():
        enc = encode_path(os.path.realpath(os.path.expanduser(path)))
        if slug == enc or any(slug.startswith(enc + m) for m in WORKTREE_MARKERS):
            return wanted.get(repo.lower())
    base = slug.split("--claude-worktrees-")[0]  # a removed worktree still belongs to the repo it hung off
    path, leftover = slug_to_path(slug)
    if not (path and not leftover):
        path, leftover = slug_to_path(base) if base != slug else (None, slug)
    if path and not leftover and os.path.isdir(path):
        o = origin_repo(path)
        if o and o.lower() in wanted:
            return wanted[o.lower()]
        if o:  # the folder belongs to some other repo
            return None
    hits = []
    for low, orig in wanted.items():
        owner, name = (encode_path(x).lower() for x in low.split("/", 1))
        s = slug.lower()
        tail = r"(?:$|--claude-worktrees-|-worktrees-)"
        if re.search(r"(?:^|-)%s-%s%s" % (re.escape(owner), re.escape(name), tail), s):
            hits.append((2, orig))
        elif re.search(r"(?:^|-)%s%s" % (re.escape(name), tail), s):
            hits.append((1, orig))
    if hits:
        best_score = max(h[0] for h in hits)
        top = [h[1] for h in hits if h[0] == best_score]
        if len(top) == 1:
            return top[0]
    return None


def collect_interruptions(dirs, repos, checkouts, since, tz):
    """-> ({repo: summary}, meta). Only counts; never keeps transcript text."""
    accs = {r: new_interruptions() for r in repos}
    meta = {"dirs_scanned": [], "dirs_unreadable": [], "project_folders": 0, "mapped": 0, "unmapped": 0,
            "files_unreadable": 0}
    seen_dirs = set()
    for d in dirs:
        d = os.path.expanduser(d)
        real = os.path.realpath(d)
        if real in seen_dirs:
            continue
        seen_dirs.add(real)
        if not os.path.isdir(d) or not os.access(d, os.R_OK | os.X_OK):
            meta["dirs_unreadable"].append(d)
            continue
        meta["dirs_scanned"].append(d)
        try:
            slugs = sorted(os.listdir(d))
        except OSError:
            meta["dirs_unreadable"].append(d)
            continue
        for slug in slugs:
            sdir = os.path.join(d, slug)
            if not os.path.isdir(sdir):
                continue
            meta["project_folders"] += 1
            repo = slug_repo(slug, repos, checkouts)
            if not repo:
                meta["unmapped"] += 1
                continue
            meta["mapped"] += 1
            try:
                files = sorted(os.listdir(sdir))
            except OSError:
                meta["files_unreadable"] += 1
                continue
            for f in files:
                if not f.endswith(".jsonl"):
                    continue
                fp = os.path.join(sdir, f)
                try:
                    if dt.datetime.fromtimestamp(os.path.getmtime(fp), dt.timezone.utc) < since:
                        continue
                    scan_transcript(fp, since, accs[repo], tz)
                except OSError:
                    meta["files_unreadable"] += 1
    return {r: interruption_summary(a) for r, a in accs.items()}, meta


# ---------------------------------------------------------------- recommendations

def recommendations(p, ci, dep, auto, inter, waits, window_days, default_branch):
    weeks = window_days / 7.0
    out = []
    merged = p["merged"]
    wag = p["wait_after_green_min"]

    def rec(id_, title, why, saves, pros, cons, risk, key, value):
        out.append({"id": id_, "title": title, "why": why, "saves_hours_per_week": round(max(0.0, saves), 1),
                    "pros": pros, "cons": cons, "risk": risk, "setting": {"key": key, "value": value}})

    if merged and (wag["total_hours"] > 2 or p["waited_over_2h"] >= 2):
        # Count each PR's wait up to 8 hours, less 10 minutes that a human-free merge would still take.
        saved = sum(max(0.0, min(w, 480.0) - 10.0) for w in waits) / 60.0 / weeks
        rec("auto-merge", "Merge automatically when tests pass and the AI review approves",
            "%d of %d changes waited over 2 hours after the tests passed; %s of waiting in total. Estimate: each wait "
            "counted up to 8 hours, minus 10 minutes, spread over %.1f weeks." % (
                p["waited_over_2h"], merged, hours_text(wag["total_hours"]), weeks),
            saved,
            ["Finished work reaches the main branch within minutes instead of sitting overnight",
             "Nobody has to remember to click Merge"],
            ["A change that passes tests but is still wrong goes in without a person looking",
             "Sensitive areas (sign-in, payments, health data, and the rules that run the tests) stay stricter and "
             "still need a person"],
            "medium", "merge", "auto")
    if merged and p["human_reviews"] == 0 and not auto["ai_review_workflow"]:
        rec("ai-review", "Have an AI reviewer read every change before it merges",
            "Nobody reviews these changes today: %d of %d merged without any review. An AI reviewer is better than "
            "none, and better than a non-engineer clicking approve. (Saves no waiting time by itself; it is what makes "
            "automatic merging safer.)" % (p["merged_without_review"], merged),
            0.0,
            ["Catches obvious mistakes, missing tests and risky changes", "Leaves a written record of what was checked"],
            ["Adds a few minutes and a small cost per change", "Can raise false alarms that someone has to dismiss"],
            "low", "review", "ai")
    if ci["duplicate_main_minutes"] > 60:
        hrs = ci["duplicate_main_minutes"] / 60.0
        rec("skip-duplicate-main-ci", "Do not re-run tests on main for changes that already passed",
            "%d test runs on the main branch repeated a run that had just passed on the same change: %s of test "
            "time over %.1f weeks. Machine time, counted as is." % (ci["duplicate_main_runs"], hours_text(hrs), weeks),
            hrs / weeks * 0.8,
            ["Frees the test machines, so the next change's tests start sooner", "Lowers the CI bill"],
            ["A bad merge combination is caught later (or only by the next change)"],
            "low", "tests", "no-duplicate")
    med_ci = p["ci_min"]["median"]
    if med_ci and med_ci > 10:
        per_week = merged / weeks
        rec("fast-lane-tests", "Merge after a quick set of tests; run the slow ones after the merge",
            "Tests take a median of %.0f minutes before a change can merge. Estimate: if the quick set takes about "
            "5 minutes, half of the difference is saved on each of %.1f merged changes per week." % (med_ci, per_week),
            per_week * (med_ci - 5.0) / 60.0 * 0.5,
            ["Changes merge in minutes", "Builders stay in flow"],
            ["Some breakages only show up after the merge, so a failing full run needs an automatic revert"],
            "medium", "tests", "fast-lane")
    if p["test_fix_prs"] >= 2:
        rec("fix-clock-tests", "Fix the flaky, time-of-day-sensitive tests once",
            "%d of %d merged changes were about fixing flaky or failing tests. Estimate: about 1 hour each, spread "
            "over %.1f weeks." % (p["test_fix_prs"], merged, weeks),
            p["test_fix_prs"] * 1.0 / weeks,
            ["Fewer red builds and fewer reruns", "Tests can be trusted enough to automate merging"],
            ["Takes a focused piece of work up front"],
            "low", "none", None)
    if merged and not dep["auto_on_main"]:
        per_week = merged / weeks
        deploys = min(per_week, 5.0)
        rec("auto-deploy", "Deploy to production automatically after a change merges",
            "%s Estimate: about 15 minutes of a person's attention per deploy, at most 5 deploys a week "
            "(%.1f changes merge per week)." % (dep["note"], per_week),
            deploys * 0.25,
            ["New work reaches users as soon as it merges", "One less thing a person has to remember or do"],
            ["Production changes without a person watching",
             "Needs a health check and an automatic rollback before it is safe"],
            "high", "deploy", "auto")
    if inter["days_active"] and (inter["per_active_day"] > 3 or inter["short_approvals"] >= 10):
        mins = (inter["short_approvals"] + inter["questions"]) * 3.0
        rec("fewer-questions", "Let Claude decide small, reversible things without asking",
            "Claude stopped to ask %d times and you answered with a one-word approval %d times; %d of the questions "
            "were about merging or deploying. That is %.1f interruptions per active day over %d days. Estimate: 3 "
            "minutes per question or approval." % (
                inter["questions"] + inter["turns_ending_on_user"], inter["short_approvals"],
                inter["by_category"].get("merge_or_deploy_ok", 0), inter["per_active_day"], inter["days_active"]),
            mins / 60.0 / weeks,
            ["Claude keeps working while you are away", "Fewer approvals to click through"],
            ["Occasionally Claude picks a different option than you would have; that is why only reversible choices qualify"],
            "medium", "questions", "decide-reversible")
    if inter["permission_denials"] >= 5:
        rec("permission-allowlist", "Pre-approve the safe commands Claude keeps being blocked on",
            "%d commands were blocked for permission in this window. Estimate: 2 minutes lost each." % inter["permission_denials"],
            inter["permission_denials"] * 2.0 / 60.0 / weeks,
            ["Claude stops stalling on routine, harmless commands"],
            ["The list needs a one-time review so nothing risky slips onto it"],
            "low", "none", None)
    return out


# ---------------------------------------------------------------- one repo

def audit_repo(repo, since, now, window_days, inter_by_repo, tz, log=lambda m: None):
    notes = []
    try:
        meta = gh_api("repos/%s" % repo)
    except GhError as e:
        return {"repo": repo, "error": "Cannot read this repository: %s" % e}
    if not isinstance(meta, dict) or "default_branch" not in meta:
        return {"repo": repo, "error": "Unexpected answer from GitHub for this repository"}
    branch = meta.get("default_branch") or "main"
    date = since.date().isoformat()
    result = {"repo": repo}

    def step(label, fn, default):
        try:
            return fn()
        except Exception as e:  # one failed lookup must not sink the repo
            notes.append("%s unavailable: %s" % (label, e))
            return default

    # branch protection first: the required check names decide which checks count as "tests passed"
    log("protection")
    bp, bp_note = step("Branch protection", lambda: protection_info(repo, branch),
                       ({"status": "unknown", "required_reviews": None, "required_checks": None, "enforce_admins": None}, None))
    if bp_note:
        notes.append(bp_note)

    log("pull requests")
    fields = "number,title,createdAt,mergedAt,headRefOid,headRefName,mergeCommit,author,reviews,comments"
    search = "merged:>=%s" % date
    base = ["pr", "list", "--repo", repo, "--state", "merged", "--search", search, "--limit", str(MAX_PRS)]
    prs = None
    try:
        prs = gh_json(base + ["--json", fields + ",statusCheckRollup"]) or []
    except GhError as e:
        notes.append("Check results came with the PR list failed (%s); fetching them one PR at a time." % e)
        prs = step("Merged pull requests", lambda: gh_json(base + ["--json", fields]) or [], [])
        for p in prs:
            p["_checks"] = step("Checks for #%s" % p.get("number"), lambda p=p: checks_from_api(repo, p["headRefOid"]), [])
    if len(prs) >= MAX_PRS:
        notes.append("Only the %d most recent merged pull requests were read." % MAX_PRS)
    open_prs = step("Open pull requests", lambda: gh_json(["pr", "list", "--repo", repo, "--state", "open", "--json", "number",
                                                          "--limit", "200"]) or [], [])

    log("workflow runs")
    runs, trunc = step("Workflow runs", lambda: gh_paged("repos/%s/actions/runs?created=%%3E%%3D%s" % (repo, date),
                                                         "workflow_runs", MAX_RUNS), ([], False))
    if trunc:
        notes.append("Only the %d most recent workflow runs were read." % MAX_RUNS)
    runs = [r for r in runs if (parse_ts(r.get("created_at")) or since) >= since]

    wf_list = step("Workflow list", lambda: (gh_api("repos/%s/actions/workflows" % repo) or {}).get("workflows", []), [])
    log("workflow files")
    wf_text = {}
    for w in wf_list:
        if w.get("state") == "active" and w.get("path") and not w["path"].startswith("dynamic/"):  # GitHub-run, no file
            wf_text[w["path"]] = step("Workflow file %s" % w["path"], lambda w=w: fetch_text(repo, w["path"]), "")

    pr_stats, waits, merged = pr_metrics(prs, len(open_prs), bp.get("required_checks") or [], since,
                                         branch_run_shas(runs), tz)
    result["prs"] = pr_stats
    result["ci"] = ci_metrics(runs, merged, branch, len(merged))
    result["deploy"] = deploy_metrics(wf_list, wf_text, runs, merged, branch)

    log("settings")
    ai_review = any(looks_like_ai_review(w, wf_text.get(w.get("path"), "")) for w in wf_list if w.get("state") == "active")
    codeowners = step("CODEOWNERS check", lambda: has_codeowners(repo), False)
    sling = step("Issue labels", lambda: sling_labels_used(repo, since), False)
    auto = {"allow_auto_merge": bool(meta.get("allow_auto_merge")),
            "delete_branch_on_merge": bool(meta.get("delete_branch_on_merge")),
            "branch_protection": {"required_reviews": bp["required_reviews"], "required_checks": bp["required_checks"],
                                  "enforce_admins": bp["enforce_admins"], "status": bp["status"]},
            "codeowners": codeowners, "ai_review_workflow": ai_review, "sling_labels_used": sling}
    result["automation"] = auto
    inter = inter_by_repo.get(repo) or interruption_summary(new_interruptions())
    result["interruptions"] = inter
    result["recommendations"] = recommendations(pr_stats, result["ci"], result["deploy"], auto, inter, waits,
                                                window_days, branch)
    result["notes"] = notes
    return result


# ---------------------------------------------------------------- output

def summarize(report):
    lines = ["Build Autopilot audit: %d day%s ending %s" % (report["window_days"], "" if report["window_days"] == 1 else "s",
                                                           report["generated_at"][:10]), ""]
    for r in report["repos"]:
        if "error" in r:
            lines.append("%s: could not be read (%s)" % (r["repo"], r["error"]))
            lines.append("")
            continue
        p, ci, dep, it = r["prs"], r["ci"], r["deploy"], r["interruptions"]
        lines.append(r["repo"])
        if not p["merged"]:
            lines.append("  No pull requests merged in this window.")
        else:
            w = p["wait_after_green_min"]
            lines.append("  %d changes merged (%d still open). Typical change: %s minutes from opening to merge." % (
                p["merged"], p["open"], num(p["open_to_merge_min"]["median"])))
            if w["median"] is None:
                lines.append("  No test results were found to measure waiting after the tests passed.")
            else:
                lines.append("  After the tests passed, a change waited a median of %s minutes for someone to merge it; "
                             "%d waited over 2 hours (%s in total), %d overnight." % (
                                 num(w["median"]), p["waited_over_2h"], hours_text(w["total_hours"]), p["waited_overnight"]))
            lines.append("  Reviews: %d human, %d AI; %d of %d merged with no review at all." % (
                p["human_reviews"], p["bot_reviews"], p["merged_without_review"], p["merged"]))
        lines.append("  Tests: %d duplicate runs on main (%d minutes), %d red commit(s) on main. Deploy: %s" % (
            ci["duplicate_main_runs"], ci["duplicate_main_minutes"], ci["red_main_commits"], dep["note"]))
        if it["sessions"]:
            lines.append("  Claude sessions: %d over %d active day(s); %d question(s), %d turn(s) that ended waiting on you, "
                         "%d blocked command(s), %d one-word approval(s)." % (
                             it["sessions"], it["days_active"], it["questions"], it["turns_ending_on_user"],
                             it["permission_denials"], it["short_approvals"]))
        else:
            lines.append("  Claude sessions: none found for this repo in the transcripts that could be read.")
        if r["recommendations"]:
            lines.append("  Suggestions:")
            for rec in r["recommendations"]:
                lines.append("    - %s (about %s hours/week, %s risk)" % (rec["title"], rec["saves_hours_per_week"], rec["risk"]))
        for n in r.get("notes", []):
            lines.append("  Note: " + n)
        lines.append("")
    t = report.get("transcripts") or {}
    if t:
        lines.append("Transcripts: %d project folders found, %d matched to these repos, %d not matched; unreadable folders: %s." % (
            t.get("project_folders", 0), t.get("mapped", 0), t.get("unmapped", 0), ", ".join(t.get("dirs_unreadable", [])) or "none"))
    return "\n".join(lines)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME")
    ap.add_argument("--org", action="append", default=[], metavar="ORG",
                    help="add every non-archived repo in the org with a push inside the window")
    ap.add_argument("--since", default="30d", help="30d, 4w or YYYY-MM-DD (default 30d)")
    ap.add_argument("--transcripts", action="append", default=[], metavar="DIR",
                    help="Claude Code projects folder; repeat for several (default: ~/.claude/projects)")
    ap.add_argument("--tz", metavar="ZONE", help="time zone for overnight waits, e.g. America/New_York (default: this computer's)")
    ap.add_argument("--checkout", action="append", default=[], metavar="OWNER/NAME=PATH")
    ap.add_argument("--out", required=True, metavar="FILE")
    return ap.parse_args(argv)


def main(argv=None, now=None):
    args = parse_args(argv)
    now = now or dt.datetime.now(dt.timezone.utc)
    since, window_days = parse_since(args.since, now)
    tz = local_tz(args.tz)
    repos = list(dict.fromkeys(args.repo))
    for org in args.org:
        try:
            listing = gh_json(["repo", "list", org, "--json", "nameWithOwner,pushedAt,isArchived", "--limit", "200"]) or []
        except GhError as e:
            print("Could not list repositories for %s: %s" % (org, e), file=sys.stderr)
            continue
        for r in listing:
            pushed = parse_ts(r.get("pushedAt"))
            if not r.get("isArchived") and pushed and pushed >= since and r["nameWithOwner"] not in repos:
                repos.append(r["nameWithOwner"])
    if not repos:
        raise SystemExit("Nothing to audit: pass --repo OWNER/NAME and/or --org ORG")
    checkouts = {}
    for c in args.checkout:
        if "=" not in c:
            raise SystemExit("--checkout must look like OWNER/NAME=PATH (got %r)" % c)
        k, v = c.split("=", 1)
        checkouts[k.strip()] = v.strip()
    dirs = args.transcripts or DEFAULT_TRANSCRIPT_DIRS
    inter, tmeta = collect_interruptions(dirs, repos, checkouts, since, tz)
    out = []
    for i, repo in enumerate(repos, 1):
        print("[%d/%d] %s" % (i, len(repos), repo), file=sys.stderr)
        try:
            out.append(audit_repo(repo, since, now, window_days, inter, tz,
                                  log=lambda m, repo=repo: print("    %s: %s" % (repo, m), file=sys.stderr)))
        except Exception as e:  # never crash the whole audit for one repo
            out.append({"repo": repo, "error": "%s: %s" % (type(e).__name__, e)})
    report = {"generated_at": iso(now), "since": iso(since), "window_days": window_days, "repos": out,
              "transcripts": tmeta}
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(summarize(report))
    print("\nFull results: %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
