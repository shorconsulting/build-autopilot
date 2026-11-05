import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402
import ci_cost  # noqa: E402
import lanes  # noqa: E402

NOW = dt.datetime(2026, 10, 29, 12, 0, tzinfo=dt.timezone.utc)

CI = """name: CI
on:
  pull_request:
  push:
    branches: [main]
concurrency:
  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.sha }}
  cancel-in-progress: true
jobs:
  static:
    runs-on: ubuntu-24.04
    steps:
      - run: npm run lint
  test:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        shard: [1, 2]
    steps:
      - run: npm test
  gates:
    needs: [static, test]
    runs-on: ubuntu-24.04
    steps:
      - run: true
"""

GOOD = """name: CI
on:
  pull_request:
  push:
    branches: [main]
concurrency:
  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}
jobs:
  changes:
    runs-on: ubuntu-24.04
    steps:
      - run: git diff --name-only origin/main...HEAD
  static:
    needs: changes
    runs-on: ubuntu-24.04
    steps:
      - run: npm run lint
  test:
    needs: static
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        shard: [1, 2]
    steps:
      - run: npm test
"""

QUEUE = CI.replace("  pull_request:\n", "  pull_request:\n  merge_group:\n")
DEDUPED = QUEUE.replace("jobs:\n", """jobs:
  dedupe:
    if: github.event_name == 'push'
    runs-on: ubuntu-24.04
    steps:
      - run: python3 .github/autopilot/skip_duplicate_main.py ci.yml
""")
INLINE = QUEUE.replace("jobs:\n", """jobs:
  dedupe:
    if: github.event_name == 'push'
    runs-on: ubuntu-24.04
    steps:
      - run: gh api "repos/$R/actions/workflows/ci.yml/runs?event=merge_group&head_sha=$GITHUB_SHA&status=success"
""")

SWEEP = """on:
  schedule:
    - cron: '*/5 * * * *'
jobs:
  sweep:
    runs-on: ubuntu-24.04
    steps:
      - run: true
"""


def ts(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def job(name, minutes, labels=("ubuntu-24.04",), conclusion="success"):
    a = NOW - dt.timedelta(days=1)
    return {"name": name, "started_at": ts(a), "completed_at": ts(a + dt.timedelta(minutes=minutes)),
            "conclusion": conclusion, "labels": list(labels)}


class FakeGh:
    """Read-only answers, 30 days of CI: every 20 minutes a pull request, a push to main, then the merge-queue run
    that passed the pushed commit (same sha; the last queue run of the day passed another commit, so one push a day
    has no twin); two pushes a day on the queue's own branch with a candidate's sha; six cancelled pull-request runs
    a day each superseded two minutes later on its branch, and one superseded two minutes later on ANOTHER branch.
    Each run bills 1 + 2x10 + 1 + 1 minutes (static, two shards, contract, build)."""

    def __init__(self):
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        assert args[0] == "api" and "-X" not in args, args
        path = args[1]
        if "/runs?" in path:
            q = path.split("created=")[1]
            if "T" in q or int(path.split("&page=")[1].split("&")[0]) > 1:
                return json.dumps({"total_count": 0, "workflow_runs": []})
            day = dt.datetime.strptime(q, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc) + dt.timedelta(hours=1)
            runs = []

            def run(c, i, event, conclusion, sha, branch):
                return {"id": int(c.timestamp()) * 100 + i, "path": ".github/workflows/ci.yml@main", "event": event,
                        "conclusion": conclusion, "created_at": ts(c), "run_started_at": ts(c),
                        "updated_at": ts(c + dt.timedelta(minutes=90)), "run_attempt": 1, "head_sha": sha,
                        "head_branch": branch}
            def sha_of(i):
                return "%040x" % (int(day.timestamp()) + i // 3)  # the push and the queue run share the commit
            for i in range(30):
                c = day + dt.timedelta(minutes=20 * i)
                ev = ("pull_request", "push", "merge_group")[i % 3]
                sha = sha_of(i) if i != 29 else "%040x" % 999999  # the day's last queue run: another commit
                runs.append(run(c, i, ev, "success", sha, "main" if ev != "pull_request" else "feature-%d" % i))
            for i in range(2):  # the workflow also runs on pushes to the queue's branch, with the candidate's sha
                c = day + dt.timedelta(hours=11, minutes=5 * i)
                runs.append(run(c, 40 + i, "push", "success", sha_of(3 * i), "gh-readonly-queue/main/pr-%d" % i))
            for i in range(6):
                c = day + dt.timedelta(hours=12, minutes=10 * i)
                runs.append(run(c, 50 + i, "pull_request", "cancelled", "%040x" % i, "quick-%d" % i))
                runs.append(run(c + dt.timedelta(minutes=2), 60 + i, "pull_request", "success", "%040x" % (10 + i),
                                "quick-%d" % i))
            c = day + dt.timedelta(hours=14)
            runs.append(run(c, 70, "pull_request", "cancelled", "%040x" % 70, "lonely"))
            runs.append(run(c + dt.timedelta(minutes=2), 71, "pull_request", "success", "%040x" % 71, "other"))
            return json.dumps({"total_count": len(runs), "workflow_runs": runs})
        if "/jobs?" in path:
            return json.dumps({"jobs": [job("static", 0.5), job("test (1)", 9.2), job("test (2)", 10),
                                        job("contract", 0.4), job("build", 0.3),
                                        job("gates", 0, conclusion="skipped"),
                                        job("mac", 3, labels=("self-hosted", "macOS"))]})
        if "billing" in path:
            raise audit.GhError("needs admin:org", 404)
        raise AssertionError(path)


class CiCostTest(unittest.TestCase):
    def setUp(self):
        self.real = audit.gh
        audit.gh = FakeGh()

    def tearDown(self):
        audit.gh = self.real

    def test_lint_flags_each_standard_rule(self):
        rules = {f["rule"] for f in ci_cost.lint(".github/workflows/ci.yml", CI, "main")}
        self.assertEqual(rules, {"ci-main-batched", "ci-fail-fast", "ci-skip-docs-only"})
        self.assertEqual([f["rule"] for f in ci_cost.lint("s.yml", SWEEP, "main")], ["ci-poll-frequency"])
        self.assertEqual(ci_cost.lint(".github/workflows/ci.yml", GOOD, "main"), [])

    def test_queue_then_main_is_flagged_until_the_dedupe_is_made(self):
        def rules(text):
            return {f["rule"] for f in ci_cost.lint(".github/workflows/ci.yml", text, "main")}
        self.assertIn("ci-queue-then-main", rules(QUEUE))
        self.assertNotIn("ci-queue-then-main", rules(CI))  # no merge queue: the push run is the only full run
        self.assertNotIn("ci-queue-then-main", rules(DEDUPED))  # the gate's script
        self.assertNotIn("ci-queue-then-main", rules(INLINE))  # the inline check of a pipeline: own repo
        self.assertNotIn("ci-queue-then-main", rules(INLINE.replace(
            'gh api "repos/$R/actions/workflows/ci.yml/runs?event=merge_group&head_sha=$GITHUB_SHA&status=success"',
            "gh run list --event merge_group --commit $GITHUB_SHA")))
        self.assertIn("ci-queue-then-main", rules(QUEUE.replace("jobs:\n", "# TODO: skip_duplicate_main.py\njobs:\n")))
        # a job the queue run skips: the queue run is no full suite, so the push run must stay
        self.assertNotIn("ci-queue-then-main", rules(QUEUE.replace(
            "  test:\n", "  test:\n    if: github.event_name != 'merge_group'\n")))

    def test_docs_only_counts_the_fix_as_made(self):
        def rules(text):
            return {f["rule"] for f in ci_cost.lint(".github/workflows/ci.yml", text, "main")}
        plain = "git diff --name-only origin/main...HEAD"
        self.assertIn(plain, GOOD)
        self.assertNotIn("ci-skip-docs-only", rules(GOOD.replace(plain, plain.replace("diff", "diff --no-renames"))))
        script = GOOD.replace(plain, "bash scripts/ci-docs-only.sh").replace(
            "    needs: static\n", "    needs: static\n    if: needs.changes.outputs.code != 'false'\n")
        self.assertNotIn("ci-skip-docs-only", rules(script))  # the diff lives in a script the workflow only calls
        self.assertIn("ci-skip-docs-only", rules(GOOD.replace(plain, "bash scripts/ci-docs-only.sh")))

    def test_batching_runs_the_newest_waiting_push_only(self):
        t = [NOW + dt.timedelta(minutes=m) for m in (0, 5, 10, 15, 100, 200)]
        self.assertEqual(ci_cost.batched_runs(t, 30), 4)  # 0 runs; 5,10,15 collapse into one at 30; 100; 200
        self.assertEqual(ci_cost.batched_runs(t[:1], 30), 1)

    def test_pr_without_cancel_is_flagged(self):
        text = CI.replace("  cancel-in-progress: true\n", "")
        self.assertIn("ci-pr-cancel-superseded", {f["rule"] for f in ci_cost.lint("ci.yml", text, "main")})

    def test_cron_frequency(self):
        self.assertEqual(ci_cost.cron_every_min("*/5 * * * *"), 5)
        self.assertEqual(ci_cost.cron_every_min("* * * * *"), 1)
        self.assertEqual(ci_cost.cron_every_min("0,30 * * * *"), 30)
        self.assertIsNone(ci_cost.cron_every_min("0 2 * * *"))

    def test_billing_rounds_up_and_skips_free_jobs(self):
        got = ci_cost.billed_jobs([job("a", 0.2), job("b", 2.5), job("c", 4, conclusion="skipped"),
                                   job("d", 4, labels=("self-hosted",)), job("e", 1, labels=("macos-14",))])
        self.assertEqual([(g[0], g[2], g[3]) for g in got], [("a", "linux", 1), ("b", "linux", 3), ("e", "macos", 1)])

    def test_windows_extrapolate_from_samples(self):
        c = ci_cost.cost_repo("acme/widgets", NOW, samples=3, wf_text={".github/workflows/ci.yml": CI},
                              default_branch="main")
        w7, w30 = c["windows"]["7"], c["windows"]["30"]
        per_run = 1 + 10 + 10 + 1 + 1  # static, contract and build round up to 1, shards to 10; gates skipped, mac free
        self.assertEqual(w7["billed_minutes"], w7["runs"] * per_run)
        self.assertEqual(w30["billed_minutes"], w30["runs"] * per_run)
        self.assertGreater(w30["runs"], w7["runs"] * 3)
        self.assertAlmostEqual(w7["usd"], w7["billed_minutes"] * ci_cost.PRICE["linux"], places=1)
        self.assertEqual(c["price_source"], "list price estimate")
        rules = {f["rule"]: f for f in w7["findings"]}
        self.assertIn("ci-main-batched", rules)
        self.assertGreater(rules["ci-main-batched"]["saves_minutes"], 0)  # a push every hour, 90-minute runs
        jobs = {r["name"]: r["minutes"] for r in w7["by_job"]}
        self.assertEqual(jobs["ci.yml :: test"], w7["runs"] * 20)

    def cost(self, text=None):
        return ci_cost.cost_repo("acme/widgets", NOW, samples=3, default_branch="main",
                                 wf_text={".github/workflows/ci.yml": text or QUEUE})["windows"]["7"]

    def test_queue_then_main_is_priced_by_the_pushes_the_queue_already_tested(self):
        w7 = self.cost()
        f = {x["rule"]: x for x in w7["findings"]}["ci-queue-then-main"]
        pushes = {r["name"]: r["minutes"] for r in w7["by_event"]}["ci.yml push"]
        self.assertGreater(pushes, 0)
        # of 12 pushes a day, 9 are pushes to main whose commit a queue run had passed; the two on the queue's own
        # branch and the one main push with no twin are not skipped
        self.assertAlmostEqual(f["saves_minutes"], pushes * 9 / 12.0, delta=1)
        self.assertNotIn("ci-queue-then-main", {x["rule"] for x in self.cost(DEDUPED)["findings"]})

    def test_pull_request_as_costly_as_the_queue_run_is_flagged(self):
        w7 = self.cost()
        f = {x["rule"]: x for x in w7["findings"]}["ci-pr-selection-queue-full"]
        self.assertIn("bills 23 min and a merge-queue run 23", f["evidence"])
        self.assertGreater(f["saves_minutes"], 0)
        t = {"by_workflow": {"ci.yml": 1}, "by_event_con": {"ci.yml pull_request success": 100.0,
                                                           "ci.yml merge_group success": 500.0},
             "runs_by": {"ci.yml pull_request success": 10, "ci.yml merge_group success": 10}}
        self.assertEqual(ci_cost.pr_vs_queue(t), [])  # the pull request already runs a fifth of the queue's suite
        t["by_event_con"]["ci.yml pull_request success"] = 500.0
        self.assertEqual(len(ci_cost.pr_vs_queue(t)), 1)
        t["runs_by"]["ci.yml merge_group success"] = 3  # too few queue runs to compare against
        self.assertEqual(ci_cost.pr_vs_queue(t), [])

    def test_small_jobs_are_folded(self):
        w7 = self.cost()
        f = {x["rule"]: x for x in w7["findings"]}["ci-fold-small-jobs"]
        self.assertIn("(build, contract, static)", f["evidence"])
        self.assertAlmostEqual(f["saves_minutes"], 2 * w7["runs"], delta=1)  # three 1-minute jobs become one
        t = {"sampled_runs": {"ci.yml": 10}, "by_job": {"ci.yml :: a": 10, "ci.yml :: b": 10, "ci.yml :: c": 10},
             "job_runs": {"ci.yml :: a": [1] * 10, "ci.yml :: b": [1] * 10, "ci.yml :: c": [1] * 10}}
        self.assertEqual(len(ci_cost.small_jobs(t)), 1)
        t["job_runs"]["ci.yml :: c"] = [1]  # a job that ran once in ten runs is not a job to fold
        self.assertEqual(ci_cost.small_jobs(t), [])
        t["job_runs"]["ci.yml :: c"] = [1] * 10
        self.assertEqual(ci_cost.small_jobs(t, {"ci.yml": {"c"}}), [])  # an exempt job (fold_exempt)
        text = "jobs:\n  changes:\n    steps: []\n  gates:\n    name: every gate\n    needs: [a, b, c]\n  x:\n    needs:\n      - a\n      - b\n      - c\n  y:\n    needs: [a]\n"
        self.assertEqual(ci_cost.fold_exempt(text), {"changes", "dedupe", "hold", "gates", "every gate", "x"})

    def test_hold_pays_only_when_the_superseding_push_is_quick(self):
        w7 = self.cost()
        f = {x["rule"]: x for x in w7["findings"]}["ci-pr-hold-superseded"]
        self.assertIn("superseded within 5 minutes", f["evidence"])
        self.assertGreater(f["saves_minutes"], 0)
        # which cancelled runs a hold would have made cheap: superseded on the SAME branch within the hold
        mk = lambda i, m, con, branch: (i, "ci.yml", "pull_request", con, NOW - dt.timedelta(minutes=m), 0, 1, "", branch)
        runs = [mk(1, 60, "cancelled", "b"), mk(2, 58, "success", "b"),      # quick
                mk(3, 50, "cancelled", "c"), mk(4, 48, "success", "d"),      # another branch: not a supersede
                mk(5, 40, "cancelled", "e"), mk(6, 10, "success", "e"),      # 30 minutes later: the shards had run
                mk(7, 30, "cancelled", ""), mk(8, 28, "success", "")]        # no branch known
        self.assertEqual(ci_cost.quick_superseded(runs), {1})
        # priced from the quick runs' own sample, net of the hold every pull-request run pays
        t = {"by_workflow": {"ci.yml": 1},
             "by_event_con": {"ci.yml pull_request cancelled-quick": 4.0, "ci.yml pull_request cancelled": 900.0,
                              "ci.yml pull_request success": 23.0},
             "runs_by": {"ci.yml pull_request cancelled-quick": 1, "ci.yml pull_request cancelled": 9,
                         "ci.yml pull_request success": 1}}
        self.assertEqual(ci_cost.hold_net(t), [])  # one quick run that billed 4; nine late ones at 100 do not count
        t["by_event_con"]["ci.yml pull_request cancelled-quick"] = 5 * 23.0
        t["runs_by"]["ci.yml pull_request cancelled-quick"] = 5
        self.assertEqual(ci_cost.hold_net(t)[0]["_minutes"], 5 * (23 - 5) - 5 * (1 + 9))

    def test_reads_only_a_sample_of_jobs(self):
        ci_cost.cost_repo("acme/widgets", NOW, samples=3, wf_text={}, default_branch="main")
        job_calls = [a for a in audit.gh.calls if "/jobs?" in a[1]]
        # 5 strata (pull_request success / cancelled-quick / cancelled, push, merge_group) x 3 buckets x 3 samples
        self.assertLessEqual(len(job_calls), 5 * 3 * 3)

    def test_policy_default_turns_the_rules_on(self):
        self.assertEqual(lanes.effective({})["ci_cost"], {"rules": "standard", "off": []})
        self.assertEqual(lanes.effective({"ci_cost": {"off": ["ci-fail-fast"]}})["ci_cost"]["off"], ["ci-fail-fast"])

    def test_cost_regression_proposes_the_standard_rules(self):
        cur = {"metrics": {"cost.windows.7.usd_per_month": 3000}, "settings": {"ci_cost": {"off": ["ci-fail-fast"]}}}
        prev = {"metrics": {"cost.windows.7.usd_per_month": 1000}}
        got = audit.rule_ci_cost(cur, [prev])
        self.assertEqual(got[0]["to"], {"rules": "standard", "off": []})
        cur["settings"] = {}
        self.assertEqual(audit.rule_ci_cost(cur, [prev]), [])  # already all on: findings, not a proposal


if __name__ == "__main__":
    unittest.main()
