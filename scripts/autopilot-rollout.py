#!/usr/bin/env python3
"""Roll the current Build Autopilot files out to every repo that already uses it. Standard library only.

For each repo that has `.github/autopilot.json` on its default branch, work out:
  * which installed gate files (`.github/autopilot/*.py`, `.github/workflows/autopilot.yml`) differ from this plugin's
    templates, and
  * which new keys the settings file is missing (`waves`, `review`), added with their DEFAULTS only.
A repo's own `level`, `always_ask`, `questions`, `sensitive`, `phase`, `pipeline` and `tests` are never touched.

    autopilot-rollout.py                         dry run over every repo the token can see (the default)
    autopilot-rollout.py --repo O/R --repo O/R2  dry run over named repos
    autopilot-rollout.py --apply --yes           open ONE pull request per repo that needs changes

`--apply` needs `--yes` and a person's say-so. It never pushes to a default branch: it pushes a branch and opens a
pull request, which the repo's own gate reviews (`.github/**` is a sensitive path). Reads and writes go through `gh`.
"""
import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lanes  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates" / "github"
BRANCH = "build-autopilot/rollout"
SETTINGS = ".github/autopilot.json"
# Installed path -> template path. The settings file is handled separately because it is merged, not copied.
FILES = {
    ".github/workflows/autopilot.yml": TEMPLATES / "workflows" / "autopilot.yml",
    ".github/autopilot/gate.py": TEMPLATES / "autopilot" / "gate.py",
    ".github/autopilot/skip_duplicate_main.py": TEMPLATES / "autopilot" / "skip_duplicate_main.py",
    ".github/autopilot/requirements.txt": TEMPLATES / "autopilot" / "requirements.txt",
}
ADDITIVE_KEYS = ("waves", "review")


class GH:
    """The few GitHub calls the script needs, through the `gh` CLI. Tests substitute a fake with the same methods."""

    def api(self, method, path, body=None):
        cmd = ["gh", "api", "-X", method, path]
        if body is not None:
            cmd += ["--input", "-"]
        r = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"{method} {path}: {r.stderr.strip()[:300]}")
        return json.loads(r.stdout) if r.stdout.strip() else None

    def repos(self):
        out, page = [], 1
        while True:
            chunk = self.api("GET", f"/user/repos?per_page=100&page={page}&affiliation=owner,collaborator,organization_member")
            out += [r["full_name"] for r in chunk if not r.get("archived")]
            if len(chunk) < 100:
                return out
            page += 1

    def default_branch(self, repo):
        return self.api("GET", f"/repos/{repo}")["default_branch"]

    def read(self, repo, path, ref):
        try:
            d = self.api("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        except RuntimeError:
            return None
        return base64.b64decode(d["content"]).decode("utf-8")

    def open_pr(self, repo, base, files, title, body):
        sha = self.api("GET", f"/repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
        try:
            self.api("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": sha})
        except RuntimeError:
            self.api("PATCH", f"/repos/{repo}/git/refs/heads/{BRANCH}", {"sha": sha, "force": True})
        for path, text in files.items():
            cur = self.api("GET", f"/repos/{repo}/contents/{path}?ref={BRANCH}") if self.read(repo, path, BRANCH) is not None else None
            payload = {"message": f"build-autopilot rollout: {path}", "branch": BRANCH,
                       "content": base64.b64encode(text.encode()).decode()}
            if cur:
                payload["sha"] = cur["sha"]
            self.api("PUT", f"/repos/{repo}/contents/{path}", payload)
        return self.api("POST", f"/repos/{repo}/pulls", {"title": title, "head": BRANCH, "base": base, "body": body})["html_url"]


def merge_settings(text):
    """The settings file with the missing additive keys added at their defaults; (new_text, keys_added)."""
    policy = json.loads(text)
    eff = lanes.effective(policy)
    added = [k for k in ADDITIVE_KEYS if k not in policy]
    for k in added:
        policy[k] = eff[k]
    if not added:
        return text, []
    return json.dumps(policy, indent=2) + "\n", added


def plan_repo(gh, repo):
    """What would change in `repo`: {'repo', 'base', 'files': {path: text}, 'notes': [...]} or None if not a user."""
    base = gh.default_branch(repo)
    settings = gh.read(repo, SETTINGS, base)
    if settings is None:
        return None
    try:
        policy = json.loads(settings)
    except ValueError:
        return {"repo": repo, "base": base, "files": {}, "notes": ["settings file is not valid JSON: skipped"]}
    if policy.get("pipeline", "gate") != "gate":
        return {"repo": repo, "base": base, "files": {}, "notes": [f"pipeline is {policy['pipeline']!r}: no gate files to update"],
                "settings_only": True, "policy": policy}
    files, notes = {}, []
    for dest, src in FILES.items():
        want = src.read_text(encoding="utf-8")
        have = gh.read(repo, dest, base)
        if have is None:
            if dest.endswith("skip_duplicate_main.py") and not (policy.get("tests") or {}).get("skip_duplicate_main", True):
                continue
            notes.append(f"missing {dest}")
            files[dest] = want
        elif have != want:
            notes.append(f"differs {dest}")
            files[dest] = want
    new_settings, added = merge_settings(settings)
    if added:
        files[SETTINGS] = new_settings
        notes.append("add defaults: " + ", ".join(added))
    return {"repo": repo, "base": base, "files": files, "notes": notes}


def pr_body(plan):
    lines = ["Brings this repo's Build Autopilot files up to date with the plugin.", "", "Changes:"]
    lines += [f"- {n}" for n in plan["notes"]]
    lines += ["", f"Your chosen `level`, `always_ask`, `questions`, `sensitive`, `phase`, `pipeline` and `tests` in `{SETTINGS}` are unchanged;",
              "new keys are added at their defaults, which keep today's behavior.",
              "", "🤖 Generated with [Claude Code](https://claude.com/claude-code)"]
    return "\n".join(lines)


def run(gh, repos, apply, out=print):
    plans = []
    for repo in repos:
        try:
            plan = plan_repo(gh, repo)
        except RuntimeError as e:
            out(f"{repo}: error {e}")
            continue
        if plan is None:
            continue
        plans.append(plan)
        if not plan["files"]:
            out(f"{repo}: up to date" + (f" ({'; '.join(plan['notes'])})" if plan["notes"] else ""))
            continue
        out(f"{repo}: {len(plan['files'])} file(s) to change")
        for n in plan["notes"]:
            out(f"    {n}")
        if apply:
            url = gh.open_pr(repo, plan["base"], plan["files"], "Update Build Autopilot files", pr_body(plan))
            out(f"    opened {url}")
    return plans


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", action="append", help="OWNER/REPO (repeatable); default: every repo the token can see")
    ap.add_argument("--apply", action="store_true", help="open one pull request per repo that needs changes")
    ap.add_argument("--yes", action="store_true", help="required with --apply: confirms a person approved the rollout")
    a = ap.parse_args(argv)
    if a.apply and not a.yes:
        print("--apply needs --yes (a person's say-so). Nothing was changed.", file=sys.stderr)
        return 2
    gh = GH()
    run(gh, a.repo or gh.repos(), a.apply)
    if not a.apply:
        print("\nDry run: nothing was changed. Re-run with --apply --yes to open the pull requests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
