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
    """Read-only answers: 10 CI pushes and 10 CI pull requests a day for 30 days; each run bills 1 + 2x10 minutes."""

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
            for i in range(20):
                c = day + dt.timedelta(minutes=30 * i)
                runs.append({"id": int(c.timestamp()) * 100 + i, "path": ".github/workflows/ci.yml@main",
                             "event": "push" if i % 2 else "pull_request", "conclusion": "success",
                             "created_at": ts(c), "run_started_at": ts(c), "updated_at": ts(c + dt.timedelta(minutes=90)),
                             "run_attempt": 1})
            return json.dumps({"total_count": len(runs), "workflow_runs": runs})
        if "/jobs?" in path:
            return json.dumps({"jobs": [job("static", 0.5), job("test (1)", 9.2), job("test (2)", 10),
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
        per_run = 1 + 10 + 10  # static rounds up to 1, shards to 10 each; gates skipped, self-hosted free
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

    def test_reads_only_a_sample_of_jobs(self):
        ci_cost.cost_repo("acme/widgets", NOW, samples=3, wf_text={}, default_branch="main")
        job_calls = [a for a in audit.gh.calls if "/jobs?" in a[1]]
        self.assertLessEqual(len(job_calls), 2 * 3 * 3)  # 2 events x 3 buckets x 3 samples

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
