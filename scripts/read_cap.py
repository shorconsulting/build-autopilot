#!/usr/bin/env python3
"""PreToolUse hook: deny a Read with no `limit` on a file over 300 lines, when the repo's .github/autopilot.json has
"read_cap": true. Allows everything else; any error allows (never fails closed)."""
import json
import os
import sys

MAX_LINES = 300
NOT_TEXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb")  # Read renders these; line count is meaningless


def over_cap(data):
    if data.get("tool_name") != "Read":
        return False
    inp = data.get("tool_input") or {}
    path = inp.get("file_path")
    if not path or inp.get("limit") or path.lower().endswith(NOT_TEXT):
        return False
    with open(os.path.join(data.get("cwd") or ".", ".github", "autopilot.json"), encoding="utf-8") as fh:
        if json.load(fh).get("read_cap") is not True:
            return False
    with open(path, "rb") as fh:
        return sum(1 for _ in zip(fh, range(MAX_LINES + 1))) > MAX_LINES


def main():
    try:
        deny = over_cap(json.load(sys.stdin))
    except Exception:  # ponytail: no file, no policy, bad JSON, anything: allow
        return
    if deny:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "File is over %d lines: grep it first, or Read with offset and limit." % MAX_LINES}}))


if __name__ == "__main__":
    main()
