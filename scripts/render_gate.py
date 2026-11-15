#!/usr/bin/env python3
"""Write the gate's workflow for one repo: the template, with every job on the repo's chosen runner.

The template (templates/github/workflows/autopilot.yml) runs each job on GitHub's `ubuntu-24.04`. A repo whose
GitHub-hosted Actions is off (billing) or that keeps its own runners sets `runner` in .github/autopilot.json, and
`apply` writes the workflow through this script instead of copying the template byte for byte. Nothing else in the
template changes.

Which runner, first that is set:
  1. `runner` in the policy (from the page): a label, or a list of labels such as ["self-hosted", "my-ci"].
  2. With --existing: the `runs-on` the repo's current gate workflow already uses, when every gate job agrees and it
     is not the template's. So an apply never moves a repo back to GitHub's runners by accident.
  3. The template's own `ubuntu-24.04`.

Usage:
    render_gate.py TEMPLATE POLICY_JSON [--existing CURRENT_WORKFLOW]   # prints the workflow
    render_gate.py --which POLICY_JSON [--existing CURRENT_WORKFLOW]    # prints the runner chosen, as JSON
"""
import argparse
import json
import re
import sys

DEFAULT = "ubuntu-24.04"
RUNS_ON = re.compile(r"^(?P<ind>[ \t]+)runs-on:[ \t]*(?P<val>[^\n#]*?)[ \t]*(?P<cmt>#[^\n]*)?$", re.M)


def normalize(value):
    """A runner as a list of labels, or None when it is missing or empty."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
        else:
            value = [v.strip() for v in value.split(",")]
    if not isinstance(value, list):
        raise ValueError("runner must be a label or a list of labels, not %r" % (value,))
    labels = [str(v).strip() for v in value if str(v).strip()]
    for label in labels:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", label):
            raise ValueError("not a runner label: %r" % label)
    return labels or None


def existing_runner(workflow_text):
    """The runner every job of a current gate workflow uses, when they all agree; None otherwise."""
    if not workflow_text:
        return None
    found = {tuple(normalize(m.group("val")) or ()) for m in RUNS_ON.finditer(workflow_text)}
    if len(found) != 1:
        return None
    labels = list(found.pop())
    return None if not labels or labels == [DEFAULT] else labels


def choose(policy, existing_text=None):
    return normalize(policy.get("runner")) or existing_runner(existing_text) or [DEFAULT]


def as_yaml(labels):
    return labels[0] if len(labels) == 1 else "[" + ", ".join(labels) + "]"


def render(template_text, labels):
    value = as_yaml(labels)
    out, n = RUNS_ON.subn(lambda m: "%sruns-on: %s%s" % (m.group("ind"), value,
                                                          (" " + m.group("cmt")) if m.group("cmt") else ""),
                          template_text)
    if n == 0:
        raise ValueError("the template has no runs-on line")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("template", nargs="?")
    ap.add_argument("policy")
    ap.add_argument("--existing")
    ap.add_argument("--which", action="store_true")
    a = ap.parse_args(argv)
    with open(a.policy, encoding="utf-8") as fh:
        policy = json.load(fh)
    existing = None
    if a.existing:
        try:
            with open(a.existing, encoding="utf-8") as fh:
                existing = fh.read()
        except FileNotFoundError:
            existing = None
    labels = choose(policy, existing)
    if a.which:
        print(json.dumps(labels))
        return 0
    if not a.template:
        ap.error("TEMPLATE is required unless --which")
    with open(a.template, encoding="utf-8") as fh:
        sys.stdout.write(render(fh.read(), labels))
    return 0


if __name__ == "__main__":
    sys.exit(main())
