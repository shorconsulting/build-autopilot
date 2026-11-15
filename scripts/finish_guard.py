#!/usr/bin/env python3
"""Stop hook: a builder does not stop while the pull request for its branch is still red or still running.

Global build rule 2 (rules/build-rules.json): a lane is done when its pull requests are merged and green, not after
the first attempt. When the session is about to stop and the current branch has an open pull request whose checks
failed or are still running, this blocks the stop and says what to do next. It allows the stop:
  - when nothing is open for this branch, or its checks are green;
  - after `lanes.max_fix_rounds_per_pr` blocks for the same pull request in this session (only a person can help then,
    and the lane should record that as a build question);
  - when BUILD_AUTOPILOT_FINISH_GUARD=0, or on any error (it never fails closed).
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import steward  # noqa: E402

STATE_DIR = os.path.expanduser("~/.build-autopilot/finish-guard")


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True, timeout=20).stdout.strip()


def open_pr(cwd):
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", git(cwd, "remote", "get-url", "origin"))
    if not branch or branch == "HEAD" or not m:
        return None, None
    repo = "%s/%s" % m.groups()
    prs = steward.api("repos/%s/pulls?state=open&head=%s:%s&per_page=5" % (repo, m.group(1), branch)) or []
    return repo, (prs[0] if prs else None)


def verdict(data, rules):
    if os.environ.get("BUILD_AUTOPILOT_FINISH_GUARD") == "0":
        return None
    cwd = data.get("cwd") or os.getcwd()
    repo, pr = open_pr(cwd)
    if not pr:
        return None
    chk = steward.Sweeper(rules).checks(repo, pr["head"]["sha"], classify=False)
    if not (chk["failed"] or chk["pending"] or chk["stalled"] or chk["infra"]):
        return None
    key = "%s#%s" % (repo, pr["number"])
    path = os.path.join(STATE_DIR, "%s.json" % re.sub(r"[^\w.-]", "_", data.get("session_id") or "session"))
    try:
        with open(path, encoding="utf-8") as fh:
            seen = json.load(fh)
    except (OSError, ValueError):
        seen = {}
    if seen.get(key, 0) >= rules["lanes"]["max_fix_rounds_per_pr"]:
        return None
    seen[key] = seen.get(key, 0) + 1
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(seen, fh)
    if chk["failed"]:
        what = "failed checks: %s. Read the first error lines, fix it on this branch and push; if the failure is " \
               "the machine's (checkout, setup, runner), re-run the failed jobs instead" % ", ".join(
                   sorted({f["name"] for f in chk["failed"]}))
    elif chk["stalled"]:
        what = "checks stuck: %s. Cancel and re-run them" % ", ".join(s["name"] for s in chk["stalled"])
    else:
        what = "checks still running (%d). Wait for them (`gh run watch`, or fast-builder's `pr-wait`), then land " \
               "it or fix what fails" % chk["pending"]
    return ("Build rule: this lane is not done. PR #%s (%s) has %s. If only a person can unblock it, record that as a "
            "build question (bq.py record) and say so; otherwise keep going until it is green and merged. "
            "(Round %d of %d.)" % (pr["number"], pr.get("html_url", key), what, seen[key],
                                   rules["lanes"]["max_fix_rounds_per_pr"]))


def main():
    try:
        reason = verdict(json.load(sys.stdin), steward.load_rules())
    except Exception:  # no gh, no network, not a repo, anything: allow the stop
        return
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    main()
