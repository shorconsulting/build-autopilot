#!/usr/bin/env python3
"""Dry run of the v2 gate: every rule, against real git repos, a fake GitHub and a fake Claude. Prints what the gate
did in each case. No network, no API key.

    python3 scripts/gate_dryrun.py            # the table
    python3 scripts/gate_dryrun.py --comment "Changes autopilot.json"   # also print that case's PR comment
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_harness as h  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--comment", help="print the pull request comment for the scenario with this name")
    args = ap.parse_args()
    rows, failed = [], 0
    for sc in h.SCENARIOS:
        out = h.run_scenario(sc)
        obs = h.observed(out)
        ok = obs == sc["expect"]
        failed += not ok
        rows.append((sc["name"], obs["verdict"], obs["status"], obs["automerge"], "+".join(obs["reviews"]) or "-",
                     ", ".join(obs["decided"]) or "nothing",
                     "ok" if ok else "UNEXPECTED (wanted %s)" % sc["expect"], sc["rule"]))
        if args.comment == sc["name"]:
            print(out["comment"])
    head = ("Scenario", "Verdict", "Status", "Auto-merge", "AI reviews", "Decided without you", "Check", "Rule shown")
    widths = [max(len(str(r[i])) for r in rows + [head]) for i in range(len(head) - 1)]
    fmt = "  ".join("%%-%ds" % w for w in widths) + "  %s"
    print(fmt % head)
    print(fmt % tuple("-" * w for w in widths + [10]))
    for r in rows:
        print(fmt % r)
    print("\n%d scenarios, %d unexpected" % (len(rows), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
