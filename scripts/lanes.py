#!/usr/bin/env python3
"""Phases, test lanes and per-suite path rules: the part of the policy that decides how much CI a change gets.

Three things, all standard library:

  presets           what each `phase` sets (the page applies the same table; test_lanes.py checks they agree);
  effective(policy) the policy with a missing field filled from its phase, so a hand-trimmed or older file still
                    resolves to something definite;
  suites_to_run     which of the policy's `tests.suites` a set of changed files should run;
  render_paths      rewrite a workflow's `on.pull_request.paths:` list from a suite's `paths`, so the workflow
                    and the policy cannot drift apart. /build-autopilot:apply uses it.

Command line:
    lanes.py effective  .github/autopilot.json
    lanes.py suites     .github/autopilot.json  path/one path/two ...
    lanes.py render     .github/autopilot.json  SUITE  .github/workflows/mobile-apps.yml   (prints the new file)
"""
import json
import re
import sys

DEFAULT_PHASE = "prelaunch"

# What choosing a phase sets. Individual fields can be changed afterwards; `phase` then only records where the
# product is. live-no-customers keeps level auto-merge, not autopilot: the release-with-health-check workflow
# autopilot needs is not built (docs/v2-report.md).
PRESETS = {
    "prelaunch": {"level": "auto-merge", "sensitive_rule": "two-ai", "lane": "wave", "skip_duplicate_main": True},
    "live-no-customers": {"level": "auto-merge", "sensitive_rule": "two-ai", "lane": "fast-lane", "skip_duplicate_main": True},
    "customers": {"level": "auto-merge", "sensitive_rule": "human", "lane": "full", "skip_duplicate_main": True},
}
SUITE_ON_PR = ("when-touched", "always", "never")


def effective(policy):
    """The policy with every missing phase-governed field filled in from its phase. Never changes a field that is set."""
    out = json.loads(json.dumps(policy))
    phase = out.get("phase")
    if phase not in PRESETS:
        phase = DEFAULT_PHASE
    p = PRESETS[phase]
    out["phase"] = phase
    out.setdefault("level", p["level"])
    sens = out.setdefault("sensitive", {})
    sens.setdefault("rule", p["sensitive_rule"])
    tests = out.setdefault("tests", {})
    tests.setdefault("lane", p["lane"])
    tests.setdefault("skip_duplicate_main", p["skip_duplicate_main"])
    tests.setdefault("suites", {})
    return out


def _glob_to_regex(glob):
    """`**` crosses folders, `*` and `?` stay inside one; a pattern with no `/` matches at any depth (like .gitignore)."""
    g = glob.strip()
    anywhere = "/" not in g
    i, n, out = 0, len(g), []
    while i < n:
        c = g[i]
        if c == "*":
            if g[i:i + 2] == "**":
                i += 2
                if g[i:i + 1] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    body = "".join(out)
    return re.compile(("^(?:.*/)?" if anywhere else "^") + body + "$")


def matches(path, globs):
    path = path.removeprefix("./")
    return any(_glob_to_regex(g).match(path) for g in globs)


def suites_to_run(policy, changed_files, event="pull_request"):
    """Names of the suites a change should run, and why. `event` is pull_request, push (the default branch) or schedule.

    A suite with `on_pr: always` runs on every pull request; `never` runs only on the default branch and on the
    schedule; `when-touched` (the default) runs on a pull request only when a changed file matches its `paths`.
    The default branch and the schedule always run every suite: a skipped pull-request run is a saving, never a gap.
    """
    suites = effective(policy)["tests"]["suites"]
    run = {}
    for name, spec in sorted(suites.items()):
        if event != "pull_request":
            run[name] = f"{event}: every suite runs"
            continue
        mode = spec.get("on_pr", "when-touched")
        if mode == "always":
            run[name] = "on_pr: always"
        elif mode == "never":
            continue
        else:
            hit = [f for f in changed_files if matches(f, spec.get("paths", []))]
            if hit:
                run[name] = f"touched {hit[0]}" + (f" and {len(hit) - 1} more" if len(hit) > 1 else "")
    return run


def validate(policy):
    """Problems with the suite rules, as plain sentences; an empty list means fine."""
    problems = []
    suites = (policy.get("tests") or {}).get("suites", {})
    if not isinstance(suites, dict):
        return ["tests.suites must be an object of suite name to rules"]
    for name, spec in suites.items():
        if not isinstance(spec, dict):
            problems.append(f"suite {name!r}: rules must be an object")
            continue
        if spec.get("on_pr", "when-touched") not in SUITE_ON_PR:
            problems.append(f"suite {name!r}: on_pr must be one of {', '.join(SUITE_ON_PR)}")
        paths = spec.get("paths", [])
        if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
            problems.append(f"suite {name!r}: paths must be a list of globs")
        elif spec.get("on_pr", "when-touched") == "when-touched" and not paths:
            problems.append(f"suite {name!r}: when-touched with no paths would never run on a pull request")
    return problems


_ITEM = re.compile(r'^(\s*)-\s+("?)(.+?)\2\s*(#.*)?$')


def render_paths(workflow_text, paths):
    """Replace the `paths:` list under `pull_request:` in a workflow with `paths`; everything else is untouched.

    Raises ValueError when the workflow has no `pull_request.paths` list or uses `paths-ignore` (the policy's `paths`
    is an allow-list, and mixing the two means different things), so a person is told rather than guessed at.
    """
    lines = workflow_text.split("\n")
    in_on = in_pr = False
    start = end = None
    indent = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(line) - len(line.lstrip())
        if lead == 0:
            in_on = stripped.startswith("on:") or stripped.startswith('"on":')
            in_pr = False
            continue
        if in_on and lead == 2:
            in_pr = stripped.startswith("pull_request:")
            continue
        if in_on and in_pr and lead == 4 and stripped.startswith("paths-ignore:"):
            raise ValueError("the workflow uses paths-ignore under pull_request; change it to a paths list first")
        if in_on and in_pr and lead == 4 and stripped.startswith("paths:"):
            start = i + 1
            j = start
            while j < len(lines):
                s = lines[j].strip()
                if s.startswith("#") or not s:
                    j += 1
                    continue
                if _ITEM.match(lines[j]) and len(lines[j]) - len(lines[j].lstrip()) >= 4:
                    indent = _ITEM.match(lines[j]).group(1)
                    end = j + 1
                    j += 1
                    continue
                break
            if end is None:
                raise ValueError("pull_request.paths has no entries to replace")
            break
    if start is None:
        raise ValueError("the workflow has no pull_request.paths list")
    if not paths:
        raise ValueError("a suite with no paths cannot be written into a workflow")
    new = [f'{indent}- "{p}"' for p in paths]
    return "\n".join(lines[:start] + new + lines[end:])


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    if len(argv) >= 3 and argv[1] == "effective":
        print(json.dumps(effective(_load(argv[2])), indent=2))
        return 0
    if len(argv) >= 3 and argv[1] == "suites":
        pol = _load(argv[2])
        bad = validate(pol)
        if bad:
            print("\n".join(bad), file=sys.stderr)
            return 2
        for name, why in suites_to_run(pol, argv[3:]).items():
            print(f"{name}\t{why}")
        return 0
    if len(argv) == 5 and argv[1] == "render":
        pol = effective(_load(argv[2]))
        spec = pol["tests"]["suites"].get(argv[3])
        if spec is None:
            print(f"no suite named {argv[3]!r} in {argv[2]}", file=sys.stderr)
            return 2
        try:
            with open(argv[4], encoding="utf-8") as f:
                sys.stdout.write(render_paths(f.read(), spec.get("paths", [])))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
