#!/usr/bin/env python3
"""Check that a pull request body does not overclaim: every requirement ID it lists as tested must be named by at
least one test file in the checkout.

Why: a check on a real PR found 27 requirement IDs listed as tested that no test named. The body was written from
the plan, not from the tests.

A requirement ID is letters, a dash, digits (for example REQ-12, AC-104, UI-01a). The body's claim is read from lines
under a heading or label containing "tested" or "test coverage" (up to the next heading); IDs elsewhere in the body
are not claims. Test files are those scripts/lanes.py calls test files.

    check_pr_claims.py BODY.md [CHECKOUT_DIR]      exit 0 clean, 1 when claimed IDs have no test, 2 usage

Standard library only.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lanes  # noqa: E402

ID_RE = re.compile(r"\b[A-Z]{2,6}-\d{1,4}[a-z]?\b")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6}\s|\*\*[^*]+\*\*\s*:?\s*$)")
CLAIM_RE = re.compile(r"\btested\b|\btest coverage\b", re.I)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "vendor"}


def claimed_ids(body):
    """IDs listed in sections whose heading (or label line) says they are tested."""
    ids, active = [], False
    for line in body.splitlines():
        if HEADING_RE.match(line) or re.match(r"^\s*[A-Za-z ]{3,40}:\s*$", line):
            active = bool(CLAIM_RE.search(line))
            continue
        if re.match(r"^\s*(?:[-*]\s*)?(?:Tested|Test coverage)\b.*:", line, re.I):
            active = True
        if active:
            for i in ID_RE.findall(line):
                if i not in ids:
                    ids.append(i)
    return ids


def test_text(checkout):
    chunks = []
    for root, dirs, files in os.walk(checkout):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), checkout).replace(os.sep, "/")
            if lanes.is_test_only([rel]):
                try:
                    with open(os.path.join(root, f), encoding="utf-8", errors="ignore") as fh:
                        chunks.append(fh.read())
                except OSError:
                    pass
    return "\n".join(chunks)


def untested(body, checkout):
    ids = claimed_ids(body)
    text = test_text(checkout)
    return [i for i in ids if not re.search(r"(?<![A-Za-z0-9-])%s(?![A-Za-z0-9])" % re.escape(i), text)]


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as f:
        body = f.read()
    missing = untested(body, argv[2] if len(argv) > 2 else ".")
    if missing:
        print("Listed as tested, but no test file names them (%d): %s" % (len(missing), ", ".join(missing)))
        print("Remove them from the tested list, or add the tests.")
        return 1
    print("Every requirement ID listed as tested is named by a test.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
