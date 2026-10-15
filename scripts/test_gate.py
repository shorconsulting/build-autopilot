"""Tests for the v2 gate (templates/github/autopilot/gate.py, skip_duplicate_main.py, workflows/autopilot.yml).

Real git repos in temp folders, a fake GitHub and a fake Claude; no network. Run:
    python3 -m unittest scripts/test_gate.py -v
"""
import base64
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_harness as h  # noqa: E402
from gate_harness import gate  # noqa: E402
import skip_duplicate_main as dedupe  # noqa: E402  (on sys.path via gate_harness)

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / "templates" / "github" / "workflows" / "autopilot.yml"


class Scenarios(unittest.TestCase):
    """Every row of the dry-run table (scripts/gate_dryrun.py) is a test."""

    def test_each_rule(self):
        for sc in h.SCENARIOS:
            with self.subTest(sc["name"]):
                self.assertEqual(h.observed(h.run_scenario(sc)), sc["expect"])

    def find(self, name):
        return next(s for s in h.SCENARIOS if s["name"] == name)

    def test_automerge_is_pinned_to_the_reviewed_commit(self):
        out = h.run_scenario(self.find("auto-merge, approved"))
        call = [c for c in out["gh"].graphql_calls if "enablePullRequestAutoMerge" in c["query"]][0]
        self.assertEqual(call["variables"]["oid"], out["head"])
        self.assertEqual(call["variables"]["method"], "SQUASH")
        self.assertIn("Auto-merge is on", out["comment"])

    def test_direct_merge_is_pinned_to_the_reviewed_commit(self):
        out = h.run_scenario(self.find("auto-merge, checks already green"))
        self.assertEqual(out["gh"].merges, [{"sha": out["head"], "merge_method": "squash"}])

    def test_needs_a_person_comment_says_how_to_approve(self):
        out = h.run_scenario(self.find("Changes autopilot.json"))
        self.assertIn("/autopilot approve %s" % out["head"][:7], out["comment"])
        self.assertIn("never merge on their own", out["comment"])
        self.assertTrue(out["comment"].startswith(gate.MARKER))

    def test_no_settings_posts_nothing_and_spends_nothing(self):
        out = h.run_scenario(self.find("No settings file"))
        self.assertEqual(out["gh"].writes, [])
        self.assertEqual(out["reviewer"].calls, [])

    def test_stale_run_leaves_only_pending(self):
        out = h.run_scenario(self.find("New commit arrived mid-run"))
        self.assertEqual([(m, p.rsplit("/", 1)[1], b["status"]) for m, p, b in out["gh"].writes],
                         [("POST", "check-runs", "in_progress")])

    def test_removed_hold_label_goes_back(self):
        out = h.run_scenario(self.find("Hold label removed by a builder"))
        self.assertEqual(out["labels"], [gate.HOLD_LABEL])
        self.assertIn("/autopilot resume", out["comment"])

    def test_gitattributes_on_main_cannot_hide_a_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            main_files = dict(h.MAIN_FILES)
            h.MAIN_FILES[".gitattributes"] = "*.ts -diff\n"
            try:
                checkout, head = h.make_repo(tmp, h.POLICY, {"src/app.ts": "export const SECRET_LOOKING_LINE = 1;\n"})
            finally:
                h.MAIN_FILES.clear()
                h.MAIN_FILES.update(main_files)
            change = gate.read_change(checkout, 7, "main", head)
            self.assertIn("SECRET_LOOKING_LINE", change.patch)
            self.assertFalse(change.files[0].binary)

    def test_reviews_are_independent_and_fence_untrusted_text(self):
        sc = dict(self.find("two-ai: both approve"))
        sc["answers"] = {"general": h.verdict("APPROVE", summary="GENERAL-SUMMARY-123")}
        out = h.run_scenario(sc)
        (k1, general), (k2, security) = out["reviewer"].calls
        self.assertEqual((k1, k2), ("general", "security"))
        self.assertNotIn("GENERAL-SUMMARY-123", security)
        for text in (general, security):
            tags = re.findall(r"<(pull_request_[0-9a-f]{16})>", text)
            self.assertEqual(len(tags), 1)
            inside = text.split("<%s>" % tags[0])[1].split("</%s>" % tags[0])[0]
            self.assertIn("Title: Change things", inside)
            self.assertIn("src/auth/session.ts", inside)
        self.assertNotEqual(re.findall(r"<(pull_request_\w+)>", general), re.findall(r"<(pull_request_\w+)>", security))
        tag = re.findall(r"<(pull_request_[0-9a-f]{16})>", security)[0]
        self.assertIn("Sensitive files: `src/auth/session.ts`", security.split("<%s>" % tag)[1])

    def test_every_comment_reports_what_was_decided_without_a_person(self):
        for sc in h.SCENARIOS:
            out = h.run_scenario(sc)
            if not out["comment"]:
                continue
            with self.subTest(sc["name"]):
                if out["decided"]:
                    section = out["comment"].split("### Decided without you\n", 1)[1].split("\n\n")[0]
                    entries = [ln for ln in section.splitlines() if ln.startswith("- **")]
                    self.assertEqual(len(entries), len(out["decided"]))
                    for e in entries:
                        self.assertRegex(e, r"^- \*\*.+\.\*\* Why: .+\. To undo: .+\.$")
                else:
                    self.assertIn("**Decided without you: nothing**", out["comment"])

    def test_automerge_entry_says_how_to_undo(self):
        out = h.run_scenario(self.find("auto-merge, approved"))
        self.assertIn("Auto-merge turned on (squash merge", out["comment"])
        self.assertIn("no sensitive files changed", out["comment"])
        self.assertIn("`/autopilot stop`", out["comment"])
        self.assertIn("revert pull request #7", out["comment"])

    def test_review_job_crash_and_comment_still_posted(self):
        out = h.run_scenario(self.find("Review job crashed"))
        self.assertIn("didn't hand over a result", out["comment"])


class Round4(unittest.TestCase):
    def find(self, name):
        return next(s for s in h.SCENARIOS if s["name"] == name)

    def run_with(self, name, **gh_attrs):
        sc = dict(self.find(name))
        sc["gh_attrs"] = gh_attrs
        return h.run_scenario(sc)

    def test_hold_arriving_before_green_wins(self):
        out = self.run_with("auto-merge, approved", hold_after_reads=1)
        self.assertEqual(out["verdict"], "STALE")
        self.assertNotIn("success", [st["state"] for st in out["gh"].results(out["head"])])
        self.assertEqual(out["automerge"], "-")

    def test_hold_arriving_after_auto_merge_turns_it_off(self):
        out = self.run_with("auto-merge, approved", hold_after_reads=2)
        self.assertEqual(out["verdict"], "STALE")
        self.assertEqual(out["status"], "failure")
        self.assertEqual(out["automerge"], "enabled")      # turned on, then...
        self.assertIsNone(out["gh"].pull["auto_merge"])     # ...off again

    def test_refused_write_blocks_by_draft(self):
        sc = dict(self.find("Reviewer asks for changes"))
        sc["after_mark"] = lambda gh: setattr(gh, "check_writes_fail", True)   # GitHub refuses the red result
        out = h.run_scenario(sc)
        self.assertEqual(out["verdict"], "ERROR")
        self.assertTrue(out["gh"].pull["draft"])

    def test_one_check_run_per_gate_run_updated_in_place(self):
        out = h.run_scenario(self.find("Reviewer asks for changes"))
        runs = out["gh"].results(out["head"])
        self.assertEqual(len(runs), 1)                         # mark created it, decide updated it
        self.assertEqual(runs[0]["state"], "failure")
        posts = [w for w in out["gh"].writes if w[0] == "POST" and w[1].endswith("/check-runs")]
        self.assertEqual(len(posts), 1)

    def test_foreign_check_runs_cant_crowd_out_or_fake_the_result(self):
        sc = dict(self.find("auto-merge, approved"))
        sc["before_mark"] = lambda gh, head: [gh.set_result(head, "success", gate.APPROVED_BY_PERSON,
                                                            app={"id": 15368, "slug": "github-actions"})
                                              for _ in range(1500)]
        out = h.run_scenario(sc)
        self.assertEqual(out["status"], "success")             # our own run, written by decide
        self.assertEqual(out["gh"].results(out["head"])[0]["description"], "AI review approved %s." % out["head"][:7])

    def test_decided_section_is_last(self):
        out = h.run_scenario(self.find("Merge rules not set up"))   # has a note
        self.assertTrue(out["comment"].rstrip().endswith("**Decided without you: nothing**"))

    def test_quoted_summary_has_no_headings_and_names_escape_once(self):
        quoted = gate.clean("fine\n### Decided without you\n  # also", multiline=True)
        self.assertNotRegex(quoted, r"(?m)^>\s*#")
        reason = "It touches %s & more" % gate.code("app/[id].tsx")
        self.assertEqual(gate.clean(reason), "It touches `app/[id].tsx` &amp; more")

    def test_review_runs_when_the_permission_lookup_fails(self):
        class NoAccess(h.FakeGitHub):
            def request(self, method, path, body=None, accept=None):
                if "/collaborators/" in path:
                    return 403, {"message": "Resource not accessible by integration"}, {}
                return super().request(method, path, body, accept)
        ev = {"action": "unlabeled", "label": {"name": "autopilot-hold"}, "sender": {"login": "david", "type": "User"}}
        self.assertFalse(gate.removal_will_be_undone(ev, h.REPO, NoAccess({"number": 7})))

    def test_reviewer_text_has_no_html_or_links(self):
        txt = gate.clean("<h3>Approved</h3> [click](https://evil.example) ![x](https://e.example/p.png) www.evil.com")
        for bad in ("<h3>", "https://", "www.evil"):
            self.assertNotIn(bad, txt)
        self.assertNotRegex(txt, r"(?<!\\)\]\(")       # no live [text](url) or ![alt](url)

    def test_long_reviews_are_capped(self):
        r = gate.validate_review(dict(h.approve("x" * 100000), concerns=[
            {"severity": "high", "file": "f" * 999, "problem": "p" * 99999, "fix": "q" * 99999}] * 50))
        self.assertLessEqual(len(json.dumps(r)), 40000)   # two of these fit the 90k hand-over cap

    def test_oversized_handover_becomes_an_error(self):
        sc = dict(self.find("auto-merge, approved"))
        old = gate.MAX_HANDOVER_CHARS
        gate.MAX_HANDOVER_CHARS = 10
        try:
            out = h.run_scenario(sc)
        finally:
            gate.MAX_HANDOVER_CHARS = old
        self.assertEqual(out["verdict"], "NEEDS_A_PERSON")
        self.assertIn("too long to hand over", out["comment"])

    def test_no_paid_review_for_a_removal_that_will_be_undone(self):
        gh = h.FakeGitHub({"number": 7, "head": {"sha": "a" * 40}})
        ev = {"action": "unlabeled", "label": {"name": "autopilot-hold"}}
        self.assertTrue(gate.removal_will_be_undone(dict(ev, sender={"login": "builder", "type": "User"}),
                                                    h.REPO, gh))
        self.assertFalse(gate.removal_will_be_undone(dict(ev, sender={"login": "david", "type": "User"}),
                                                     h.REPO, gh))
        self.assertFalse(gate.removal_will_be_undone(dict(ev, sender={"login": h.BOT, "type": "Bot"}), h.REPO, gh))

    def test_unsafe_names(self):
        for name in ("a\u202eb", "a\u200bb", "a\x85b", "a\u2028b", "a\u2029b", "a\x07b"):
            self.assertTrue(gate.unsafe_name(name), repr(name))
        self.assertFalse(gate.unsafe_name("src/caf\u00e9.ts"))


class Round6(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.checkout, self.head = h.make_repo(self._tmp.name, h.POLICY, {"src/app.ts": "x\n"})
        self.pull = dict(h.make_event(self.head)["pull_request"], state="open", merged=False)

    def tearDown(self):
        self._tmp.cleanup()

    def test_refused_update_starts_a_new_failing_run(self):
        gh = h.FakeGitHub(self.pull)
        gh.set_result(self.head, "success", "AI review approved.", pushers=("alice",))
        gh.check_patch_fails = True        # GitHub won't change the completed green run
        pr = gate.live_pull_request(gh, h.REPO, 7, "main")
        gate.write_blocking_result(gh, h.REPO, pr, "failure", "On hold: a person decides.", h.RUN_URL)
        newest = gh.results(self.head)[0]
        self.assertEqual((newest["state"], newest["pushers"]), ("failure", {"alice"}))
        self.assertFalse(gh.pull.get("draft"))   # the new red run is enough; no draft needed

    def test_stop_turns_red_when_the_green_run_cant_be_changed(self):
        gh = h.FakeGitHub(self.pull)
        gh.set_result(self.head, "success", "AI review approved.")
        gh.check_patch_fails = True
        ev = {"repository": {"full_name": h.REPO, "default_branch": "main"},
              "issue": {"number": 7, "pull_request": {"url": "x"}}, "comment": gh.command("/autopilot stop", "builder")}
        self.assertEqual(gate.cmd_approve(self.checkout, ev, gh, h.APP_SLUG, h.RUN_URL), "held by builder")
        self.assertEqual(gh.status_for(self.head), "failure")

    def test_missing_app_id_is_refused(self):
        gh = h.FakeGitHub(self.pull)
        gh.app_id = ""                             # without the filter, anyone's same-named runs would be read
        with self.assertRaises(RuntimeError):
            gate.gate_results(gh, h.REPO, self.head, h.APP_SLUG)
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"AUTOPILOT_APP_ID": "", "AUTOPILOT_APP_SLUG": ""}), \
                mock.patch.object(gate, "load_event", return_value={}), mock.patch.object(sys, "argv", ["gate", "mark"]):
            self.assertEqual(gate.main(["mark"]), 1)

    def test_newest_run_is_read_and_written_beyond_one_page(self):
        gh = h.FakeGitHub(self.pull)
        for k in range(250):                       # label toggles: one run per gate run
            gh.set_result(self.head, "failure" if k < 249 else "success", "run %d" % k)
        self.assertEqual(gh.results(self.head)[0]["description"], "run 249")
        gate.write_result(gh, h.REPO, self.head, "failure", "On hold: a person decides.", h.RUN_URL)
        self.assertEqual(len(gh.results(self.head)), 250)         # updated in place, no new run
        self.assertEqual(gh.status_for(self.head), "failure")

    def test_too_many_runs_fails_closed(self):
        gh = h.FakeGitHub(self.pull)
        old = gate.MAX_CHECK_PAGES
        gate.MAX_CHECK_PAGES = 2
        try:
            for k in range(250):
                gh.set_result(self.head, "success", "run %d" % k)
            with self.assertRaises(gate.GateError):
                gh.results(self.head)
        finally:
            gate.MAX_CHECK_PAGES = old

    def test_draft_pushes_are_recorded_and_gathered_when_ready(self):
        gh = h.FakeGitHub(self.pull)
        draft_sha, head = "d" * 40, self.head
        draft_push = h.make_event(draft_sha)
        draft_push["pull_request"].update(draft=True)
        draft_push.update(sender={"login": "carol", "type": "User"})
        self.assertEqual(gate.cmd_mark(self.checkout, draft_push, gh, h.APP_SLUG, h.RUN_URL), "pending")
        gh.pr_commits, gh.commit_authors = [draft_sha, head], ["builder"]
        ready = dict(h.make_event(head), action="ready_for_review", sender={"login": "dave", "type": "User"})
        gate.cmd_mark(self.checkout, ready, gh, h.APP_SLUG, h.RUN_URL)
        self.assertEqual(gh.results(head)[0]["pushers"], {"carol", "dave"})

    def test_mark_runs_for_draft_pushes(self):
        wf = (ROOT / "templates" / "github" / "workflows" / "autopilot.yml").read_text()
        self.assertIn("""contains(fromJSON('["closed", "synchronize"]'), github.event.action) ||""", wf)

    def test_closed_turns_red_when_the_green_run_cant_be_changed(self):
        gh = h.FakeGitHub(self.pull)
        gh.set_result(self.head, "success", "AI review approved.")
        gh.check_patch_fails = True
        closed = dict(h.make_event(self.head), action="closed")
        closed["pull_request"] = dict(closed["pull_request"], merged=False)
        self.assertEqual(gate.cmd_mark(self.checkout, closed, gh, h.APP_SLUG, h.RUN_URL), "closed")
        self.assertEqual(gh.status_for(self.head), "failure")


class Patterns(unittest.TestCase):
    CASES = [
        ("**/auth/**", "src/auth/session.ts", True),
        ("**/auth/**", "auth/x.py", True),
        ("**/auth/**", "src/Auth/x.py", True),
        ("**/auth/**", "src/author.ts", False),
        ("**/*login*", "web/LoginPage.tsx", True),
        ("**/*login*", "web/login/form.tsx", True),     # a matching folder covers its files
        ("**/migrations/**", "db/migrations/001.sql", True),
        (".github/**", ".github/autopilot.json", True),
        (".github/**", "docs/.github/x", False),
        ("**/.env*", "api/.env.production", True),
        ("**/secrets*", "config/secrets.yml", True),
        ("*.pem", "deploy/keys/server.pem", True),       # no slash: any folder
        ("docs/", "docs/a/b.md", True),
        (".github/workflows/**", ".github/workflows/test.yml", True),
        (".github/workflows/**", ".github/workflow/test.yml", False),
    ]

    def test_cases(self):
        for pattern, path, want in self.CASES:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(gate.path_matches(path, [pattern]), want)

    def test_local_actions_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            wf = Path(tmp) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "a.yml").write_text("steps:\n  - uses: ./tools/setup\n  - uses: './x/y/'\n  - uses: ../../evil\n"
                                      "  - uses: actions/checkout@abc\n")
            self.assertEqual(gate.local_action_paths(tmp), ("tools/setup", "tools/setup/**", "x/y", "x/y/**"))


class Settings(unittest.TestCase):
    def parse(self, obj):
        return gate.parse_policy(json.dumps(obj) if not isinstance(obj, str) else obj, h.REPO)

    def test_valid(self):
        p = self.parse(h.POLICY)
        self.assertEqual((p.level, p.rule, p.error), ("auto-merge", "two-ai", ""))

    def test_defaults(self):
        p = self.parse({"version": 1})
        self.assertEqual((p.level, p.rule, p.sensitive_paths), ("manual", "two-ai", gate.DEFAULT_SENSITIVE))

    def test_fail_closed(self):
        for bad in ("{nope", "[]", {"version": 2}, {"version": 1, "level": "yolo"},
                    {"version": 1, "sensitive": {"rule": "none"}}, {"version": 1, "sensitive": {"paths": "x"}},
                    {"version": 1, "repo": "other/repo"}, {"version": 1, "sensitive": []}):
            with self.subTest(bad=bad):
                self.assertTrue(self.parse(bad).error)

    def test_symlinked_settings_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".github").mkdir()
            (Path(tmp) / "elsewhere.json").write_text(json.dumps(h.POLICY))
            (Path(tmp) / ".github" / "autopilot.json").symlink_to(Path(tmp) / "elsewhere.json")
            self.assertTrue(gate.load_policy(tmp, h.REPO).error)


class Decide(unittest.TestCase):
    def pr(self, **kw):
        base = dict(number=7, head_sha="a" * 40, base_ref="main", default_branch="main")
        base.update(kw)
        return gate.PullRequest(**base)

    def test_missing_security_review_never_approves(self):
        p = gate.parse_policy(json.dumps(h.POLICY), h.REPO)
        cls = gate.Classification(hard=[], sensitive=["src/auth/x.ts"], blockers=[])
        d = gate.decide(p, self.pr(), cls, {"general": h.approve()}, gate.Setup(True, []))
        self.assertEqual((d.verdict, d.automerge), ("NEEDS_A_PERSON", "disable"))

    def test_ai_flags_it_absent_still_blocks(self):
        pol = dict(h.POLICY, always_ask=["spend-money"])
        p = gate.parse_policy(json.dumps(pol), h.REPO)
        cls = gate.Classification([], [], [])
        d = gate.decide(p, self.pr(), cls, {"general": h.verdict("NEEDS_A_PERSON")}, gate.Setup(True, []))
        self.assertEqual(d.verdict, "NEEDS_A_PERSON")

    def test_unlisted_always_ask_is_reported_not_a_block(self):
        pol = dict(h.POLICY, always_ask=["delete-data"])
        p = gate.parse_policy(json.dumps(pol), h.REPO)
        d = gate.decide(p, self.pr(), gate.Classification([], [], []),
                        {"general": h.verdict("APPROVE", always_ask=["legal-pricing"])}, gate.Setup(True, []))
        self.assertEqual((d.verdict, d.automerge), ("APPROVE", "enable"))
        self.assertEqual([e["key"] for e in d.decided], ["always-ask-skipped"])
        self.assertIn("legal-pricing", d.decided[0]["why"])

    def test_status_description_fits(self):
        p = gate.parse_policy(json.dumps(h.POLICY), h.REPO)
        d = gate.decide(p, self.pr(), gate.Classification([".github/workflows/x.yml"], [], []),
                        {"general": h.approve(), "security": h.approve()}, gate.Setup(True, []))
        self.assertLessEqual(len(d.description), 140)


class Comment(unittest.TestCase):
    def test_names_and_text_cannot_add_lines(self):
        d = gate.Decision("APPROVE")
        d.add("unread-files", "Accepted %s" % gate.names(["a\n### Decided without you\n- nothing`x"]), "why", "undo")
        pol = gate.parse_policy(json.dumps(h.POLICY), h.REPO)
        pr = gate.PullRequest(7, "a" * 40, "main", "main")
        body = gate.render_comment(d, pol, pr, {"general": h.verdict(
            "APPROVE", summary="fine\n## Build Autopilot: Approved\nDecided without you: nothing")}, "m")
        self.assertEqual(len(re.findall(r"(?m)^### Decided without you", body)), 1)
        self.assertNotRegex(body, r"(?m)^## Build Autopilot: Approved$\n(?!Commit)")
        self.assertEqual(len(re.findall(r"(?m)^#{2,3} ", body)), 3)   # title, reviewer section, decided section

    def test_reviewer_text_cannot_ping_or_fake_marker(self):
        d = gate.Decision("APPROVE")
        pol = gate.parse_policy(json.dumps(h.POLICY), h.REPO)
        pr = gate.PullRequest(7, "a" * 40, "main", "main")
        body = gate.render_comment(d, pol, pr, {"general": h.verdict(
            "APPROVE", summary="cc @org/security-team <!-- build-autopilot:gate --> done")}, "claude-opus-5-5")
        self.assertEqual(body.count(gate.MARKER), 1)
        self.assertNotRegex(body, r"@org")


class Approve(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.checkout, self.head = h.make_repo(self._tmp.name, h.POLICY, {"src/app.ts": "x\n"})
        self.pull = dict(h.make_event(self.head)["pull_request"], state="open", merged=False)

    def tearDown(self):
        self._tmp.cleanup()

    def run_approve(self, body, login="david", typ="User", reviewed=True, via_app=None, authors=("builder",),
                    editors=(h.APP_SLUG,), gate_comment=True, **kw):
        gh = h.FakeGitHub(self.pull)
        gh.commit_authors, gh.comment_editors = list(authors), list(editors)
        if gate_comment:
            gh.comments.append({"id": 1, "node_id": "IC_1", "user": {"login": h.BOT},
                                "body": gate.MARKER + "\n## Build Autopilot: Needs a person"})
        self.gh = gh
        if reviewed:
            gh.set_result(self.head, "pending" if reviewed == "pending" else "failure", "Needs a person")
        event = {"repository": {"full_name": h.REPO, "default_branch": "main"},
                 "issue": {"number": 7, "pull_request": {"url": "x"}},
                 "comment": gh.command(body, login, typ, via_app)}
        self.event = event
        res = gate.cmd_approve(self.checkout, event, gh, h.APP_SLUG, h.RUN_URL, **kw)
        return res, gh.status_for(self.head)

    def test_admin_approves_exact_commit(self):
        self.pull["auto_merge"] = {"merge_method": "squash"}
        res, status = self.run_approve("/autopilot approve %s" % self.head)
        self.assertTrue(res.startswith("approved"))
        self.assertEqual(status, "success")
        self.assertEqual(self.gh.automerge_action(), "disabled")   # approval means a person merges
        reply = self.gh.comments[-1]["body"]
        self.assertIn("### Decided without you\n- **Auto-merge turned off.**", reply)

    def test_replays_and_stale_comments_dont_count(self):
        full = "/autopilot approve %s" % self.head
        res, status = self.run_approve(full, run_attempt="2")
        self.assertIn("re-runs", res)
        self.assertNotEqual(status, "success")
        self.assertIn("Decided without you: nothing", self.gh.comments[-1]["body"])
        for change, why in ((lambda gh, c: gh.command_comments.pop(c["id"]), "no longer exists"),
                            (lambda gh, c: gh.command_comments[c["id"]].update(updated_at="2026-10-16T00:00:00Z"),
                             "edited"),
                            (lambda gh, c: gh.label_event("unlabeled", gate.HOLD_LABEL, h.BOT_ACTOR), "after this")):
            with self.subTest(why):
                gh = h.FakeGitHub(self.pull)
                gh.comments.append({"id": 1, "node_id": "IC_1", "user": {"login": h.BOT}, "body": gate.MARKER})
                gh.set_result(self.head, "failure", "Needs a person")
                event = {"repository": {"full_name": h.REPO, "default_branch": "main"},
                         "issue": {"number": 7, "pull_request": {"url": "x"}}, "comment": gh.command(full)}
                change(gh, event["comment"])
                res = gate.cmd_approve(self.checkout, event, gh, h.APP_SLUG, h.RUN_URL)
                self.assertIn(why, res)
                self.assertNotEqual(gh.status_for(self.head), "success")

    def test_approve_refused_while_held(self):
        self.pull["labels"] = [{"name": gate.HOLD_LABEL}]
        res, status = self.run_approve("/autopilot approve %s" % self.head)
        self.assertIn("on hold", res)
        self.assertNotEqual(status, "success")

    def test_pusher_recorded_by_the_app_cannot_approve(self):
        gh = h.FakeGitHub(self.pull)
        event = dict(h.make_event(self.head), sender={"login": "david", "type": "User"})
        gate.cmd_mark(self.checkout, event, gh, h.APP_SLUG, h.RUN_URL)
        self.assertEqual(gh.results(self.head)[0]["pushers"], {"david"})
        # decide writes its result into the same run; the pusher record stays
        gate.write_result(gh, h.REPO, self.head, "failure", "Needs a person", h.RUN_URL)
        self.assertEqual(gh.results(self.head)[0]["pushers"], {"david"})
        gh.comments.append({"id": 1, "node_id": "IC_1", "user": {"login": h.BOT}, "body": gate.MARKER})
        ev = {"repository": {"full_name": h.REPO, "default_branch": "main"},
              "issue": {"number": 7, "pull_request": {"url": "x"}},
              "comment": gh.command("/autopilot approve %s" % self.head)}
        res = gate.cmd_approve(self.checkout, ev, gh, h.APP_SLUG, h.RUN_URL)
        self.assertIn("pushed to", res)

    def test_refusals(self):
        full = self.head
        cases = [
            (dict(body="/autopilot approve %s" % ("1" * 40)), "now, not"),
            (dict(body="/autopilot approve %s" % full[:7]), "full 40-character"),
            (dict(body="/autopilot approve %s" % full, login="builder"), "admin"),
            (dict(body="/autopilot approve %s" % full, login="stranger"), "admin"),
            (dict(body="/autopilot approve %s" % full, typ="Bot"), "person"),
            (dict(body="/autopilot approve %s" % full, via_app={"slug": "sling"}), "person"),
            (dict(body="/autopilot approve %s" % full, reviewed=False), "hasn't finished"),
            (dict(body="/autopilot approve %s" % full, reviewed="pending"), "hasn't finished"),
            (dict(body="/autopilot approve %s" % full, authors=("builder", "david")), "wrote commits"),
            (dict(body="/autopilot approve %s" % full, login="builder", roles=("admin", "maintain", "write")),
             "opened, pushed to or wrote"),
            (dict(body="/autopilot approve %s" % full, editors=(h.APP_SLUG, "builder")), "edited"),
            (dict(body="/autopilot approve %s" % full, gate_comment=False), "missing"),
        ]
        for kw, why in cases:
            with self.subTest(kw=kw):
                res, status = self.run_approve(**kw)
                self.assertTrue(res.startswith("refused"), res)
                self.assertIn(why, res)
                self.assertNotEqual(status, "success")

    def comment_event(self, gh, body, login, typ="User"):
        return {"repository": {"full_name": h.REPO, "default_branch": "main"},
                "issue": {"number": 7, "pull_request": {"url": "x"}},
                "comment": gh.command(body, login, typ)}

    def test_stop_then_resume(self):
        self.pull["auto_merge"] = {"merge_method": "squash"}
        gh = h.FakeGitHub(self.pull)
        held = lambda: gate.hold_active(gh, h.REPO, 7, h.APP_SLUG, gate.has_hold_label(gh.pull))
        run = lambda body, who: gate.cmd_approve(self.checkout, self.comment_event(gh, body, who), gh, h.APP_SLUG,
                                                 h.RUN_URL, ("admin", "maintain"))
        self.assertEqual(run("/autopilot stop", "builder"), "held by builder")
        self.assertEqual(gh.pull["labels"], [{"name": gate.HOLD_LABEL}])
        self.assertEqual(gh.automerge_action(), "disabled")
        self.assertEqual(gh.status_for(self.head), "failure")
        self.assertTrue(held())
        self.assertTrue(run("/autopilot stop", "stranger").startswith("ignored"))
        gh.person_removes_label("builder")          # a builder pulls the label off: the hold stands
        self.assertTrue(held())
        self.assertTrue(run("/autopilot resume", "builder").startswith("ignored"))   # write can't lift a hold
        self.assertEqual(run("/autopilot resume", "david"), "resumed by david")
        self.assertFalse(held())
        self.assertEqual(gh.pull["labels"], [])

    def test_resume_replay_refused(self):
        gh = h.FakeGitHub(self.pull)
        old_resume = self.comment_event(gh, "/autopilot resume", "david")
        gate.cmd_approve(self.checkout, self.comment_event(gh, "/autopilot stop", "builder"), gh, h.APP_SLUG,
                         h.RUN_URL)
        res = gate.cmd_approve(self.checkout, old_resume, gh, h.APP_SLUG, h.RUN_URL)   # older than the hold
        self.assertIn("after this comment", res)
        self.assertTrue(gate.hold_active(gh, h.REPO, 7, h.APP_SLUG, gate.has_hold_label(gh.pull)))
        fresh = self.comment_event(gh, "/autopilot resume", "david")
        self.assertIn("re-runs", gate.cmd_approve(self.checkout, fresh, gh, h.APP_SLUG, h.RUN_URL,
                                                  run_attempt="2"))

    def test_stop_with_refused_check_write_still_holds(self):
        self.pull["auto_merge"] = {"merge_method": "squash"}
        gh = h.FakeGitHub(self.pull)
        gh.set_result(self.head, "success", "AI review approved.")
        gh.check_writes_fail = True  # GitHub refuses the write
        with self.assertRaises(gate.GateError):
            gate.cmd_approve(self.checkout, self.comment_event(gh, "/autopilot stop", "builder"), gh, h.APP_SLUG,
                             h.RUN_URL)
        self.assertEqual(gh.pull["labels"], [{"name": gate.HOLD_LABEL}])
        self.assertEqual(gh.automerge_action(), "disabled")
        self.assertTrue(gh.pull["draft"])

    def test_stop_reply_reports_decisions(self):
        self.pull["auto_merge"] = {"merge_method": "squash"}
        gh = h.FakeGitHub(self.pull)
        gate.cmd_approve(self.checkout, self.comment_event(gh, "/autopilot stop", "builder"), gh, h.APP_SLUG,
                         h.RUN_URL)
        self.assertIn("### Decided without you\n- **Auto-merge turned off.**", gh.comments[-1]["body"])

    def test_hold_survives_comment_deletion(self):
        gh = h.FakeGitHub(self.pull)
        gate.cmd_approve(self.checkout, self.comment_event(gh, "/autopilot stop", "builder"), gh, h.APP_SLUG, h.RUN_URL)
        gh.comments.clear()                          # write users can delete any comment
        gh.person_removes_label("builder")
        self.assertTrue(gate.hold_active(gh, h.REPO, 7, h.APP_SLUG, False))

    def test_mark(self):
        gh = h.FakeGitHub(self.pull)
        event = h.make_event(self.head)
        self.assertEqual(gate.cmd_mark(self.checkout, dict(event, action="synchronize"), gh, h.APP_SLUG, h.RUN_URL),
                         "pending")
        gate.write_result(gh, h.REPO, self.head, "success", "AI review approved.", h.RUN_URL)
        closed = dict(event, action="closed")
        closed["pull_request"] = dict(event["pull_request"], merged=False)
        self.assertEqual(gate.cmd_mark(self.checkout, closed, gh, h.APP_SLUG, h.RUN_URL), "closed")
        self.assertEqual(gh.status_for(self.head), "failure")
        # A new pull request with the same head never inherits an old green: mark starts a fresh pending run.
        gate.write_result(gh, h.REPO, self.head, "success", "AI review approved.", h.RUN_URL)
        self.assertEqual(gate.cmd_mark(self.checkout, dict(event, action="opened"), gh, h.APP_SLUG, h.RUN_URL),
                         "pending")
        self.assertEqual(gh.status_for(self.head), "pending")

    def test_pushers_follow_the_pull_request_across_pushes(self):
        gh = h.FakeGitHub(self.pull)
        old = "b" * 40
        gh.set_result(old, "failure", "Needs a person", pushers=("alice",))
        event = dict(h.make_event(self.head), before=old, sender={"login": "bob", "type": "User"})
        gate.cmd_mark(self.checkout, event, gh, h.APP_SLUG, h.RUN_URL)
        self.assertEqual(gh.results(self.head)[0]["pushers"], {"alice", "bob"})

    def test_other_comments_ignored(self):
        for body in ("looks good", "/autopilot approve", "please /autopilot approve abcdef1", "/autopilot approve xyz"):
            res, _ = self.run_approve(body)
            self.assertTrue(res.startswith("ignored"), body)

    def test_other_base_refused(self):
        self.pull["base"]["ref"] = "portal-default"
        res, status = self.run_approve("/autopilot approve %s" % self.head)
        self.assertIn("only apply", res)


class SkipDuplicateMain(unittest.TestCase):
    SHA, BEFORE, HEAD, TREE = "c" * 40, "b" * 40, "d" * 40, "t" * 40

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        (Path(self._tmp.name) / ".github").mkdir()
        self.write_policy(True)

    def tearDown(self):
        self._tmp.cleanup()

    def write_policy(self, on):
        pol = dict(h.POLICY, tests={"lane": "full", "skip_duplicate_main": on})
        (Path(self._tmp.name) / ".github" / "autopilot.json").write_text(json.dumps(pol))

    TESTED = "e" * 40

    def gh(self, tested_tree=None, title=None, path=".github/workflows/test.yml", event="pull_request",
           github_files=False, merged=1):
        gh = h.FakeGitHub({"number": 7})
        r = h.REPO
        pulls = [{"number": 7 + i, "merged_at": "2026-10-15T00:00:00Z", "merge_commit_sha": self.SHA,
                  "base": {"ref": "main"}, "head": {"sha": self.HEAD}} for i in range(merged)]
        gh.extra.update({
            "/repos/%s/commits/%s/pulls" % (r, self.SHA): pulls,
            "/repos/%s/pulls/7/files" % r: [{"filename": "src/app.ts"}] +
            ([{"filename": "x.ts", "previous_filename": ".github/workflows/test.yml"}] if github_files else []),
            "/repos/%s/git/commits/%s" % (r, self.SHA): {"tree": {"sha": self.TREE}},
            "/repos/%s/git/commits/%s" % (r, self.TESTED): {"tree": {"sha": tested_tree or self.TREE}},
            "/repos/%s/actions/workflows/test.yml/runs?event=pull_request&head_sha=%s&status=success"
            % (r, self.HEAD): {"workflow_runs": [{
                "conclusion": "success", "head_sha": self.HEAD, "event": event, "path": path,
                "display_title": title if title is not None else "[tested %s] Change things" % self.TESTED}]},
        })
        return gh

    def event(self, **kw):
        e = {"ref": "refs/heads/main", "before": self.BEFORE, "after": self.SHA, "forced": False,
             "repository": {"default_branch": "main"}}
        e.update(kw)
        return e

    def check(self, gh=None, event=None):
        return dedupe.check(gh or self.gh(), h.REPO, event or self.event(), "test.yml", self._tmp.name)

    def test_skips_identical_tested_code(self):
        self.assertEqual(self.check()[0], True)

    def test_runs_otherwise(self):
        cases = [
            ("different files tested", dict(gh=self.gh(tested_tree="z" * 40))),
            ("no tested commit in the title", dict(gh=self.gh(title="Change things [tested %s]" % self.TESTED))),
            ("another workflow's run", dict(gh=self.gh(path=".github/workflows/evil.yml"))),
            ("not a pull_request run", dict(gh=self.gh(event="push"))),
            ("PR touched .github", dict(gh=self.gh(github_files=True))),
            ("two prs", dict(gh=self.gh(merged=2))),
            ("other branch", dict(event=self.event(ref="refs/heads/portal-default"))),
            ("new branch", dict(event=self.event(before="0" * 40))),
            ("force push", dict(event=self.event(forced=True))),
        ]
        for name, kw in cases:
            with self.subTest(name):
                self.assertEqual(self.check(**kw)[0], False)

    def test_skip_is_reported_with_undo(self):
        skip, reason, number = self.check()
        self.assertEqual(number, 7)
        text = dedupe.decided_text(reason, "test.yml", "https://github.com", h.REPO)
        self.assertIn("### Decided without you", text)
        self.assertIn("Skipped the test.yml tests", text)
        self.assertIn("To undo: run them anyway", text)
        gh = self.gh()
        dedupe.report(gh, h.REPO, number, text)
        self.assertEqual(gh.comments[-1]["body"].split("\n", 1)[1], text)

    def test_off_in_settings(self):
        self.write_policy(False)
        self.assertEqual(self.check()[0], False)


class WorkflowFile(unittest.TestCase):
    """Static guards on the workflow template: the properties the threat model depends on."""

    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_triggers_and_default_branch_checkout(self):
        self.assertIn("pull_request_target:", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s+pull_request:\s*$")
        refs = re.findall(r"ref: (.+)", self.text)
        self.assertEqual(set(refs), {"${{ github.event.repository.default_branch }}"})
        self.assertNotIn("github.head_ref", self.text)
        self.assertNotIn("pull_request.head", self.text)

    def test_no_untrusted_text_in_shell(self):
        for bad in ("pull_request.title", "pull_request.body", "comment.body }}", "issue.title", "head.ref"):
            self.assertNotIn(bad, re.sub(r"startsWith\(github\.event\.comment\.body", "", self.text))

    def test_secrets_only_in_environment_jobs(self):
        jobs = re.split(r"(?m)^  (?=\w+:$)", self.text.split("\njobs:\n", 1)[1])
        for job in jobs:
            if "secrets." in job:
                self.assertRegex(job, r"environment: autopilot(-review)?\n", job.splitlines()[0])
        self.assertIn("permissions: {}", self.text)

    def test_actions_pinned_by_sha(self):
        for use in re.findall(r"(?m)^\s*(?:- )?uses: (\S+)", self.text):
            self.assertRegex(use, r"@[0-9a-f]{40}$")

    def test_install_is_hash_pinned(self):
        self.assertIn("--require-hashes", self.text)
        req = (ROOT / "templates" / "github" / "autopilot" / "requirements.txt").read_text()
        blocks = re.split(r"(?m)^(?=[A-Za-z])", req)
        pins = [b for b in blocks if "==" in b]
        self.assertIn("anthropic==1.11.0", req)
        for b in pins:
            self.assertRegex(b, r"^[A-Za-z0-9_.-]+==[\w.]+ \\\n\s+--hash=sha256:[0-9a-f]{64}", b.splitlines()[0])

    def test_model_is_opus(self):
        self.assertEqual(set(re.findall(r"AUTOPILOT_MODEL: (\S+)", self.text)), {"claude-opus-5-5"})
        self.assertEqual(gate.DEFAULT_MODEL, "claude-opus-5-5")


class History(unittest.TestCase):
    """GitHub lists issue events oldest first; the gate must find the newest hold change even in a long history."""

    def fake(self, pages):
        g = gate.GitHub("t", "https://api.test")

        def request(method, url, body=None, accept=None):
            m = re.search(r"[?&]page=(\d+)", url)
            page = int(m.group(1)) if m else 1
            link = '<https://api.test/x?per_page=100&page=%d>; rel="last"' % len(pages)
            return 200, pages[page - 1], {"Link": link}
        g.request = request
        return g

    def test_newest_match_wins_across_pages(self):
        hold = lambda kind, who: {"event": kind, "label": {"name": "autopilot-hold"}, "actor": {"login": who}}
        noise = {"event": "labeled", "label": {"name": "other"}}
        pages = [[hold("labeled", "david")], [noise] * 100, [hold("unlabeled", "builder")] + [noise] * 50]
        match = lambda e: (e.get("label") or {}).get("name") == "autopilot-hold"
        self.assertEqual(self.fake(pages).newest("/x", match)["actor"]["login"], "builder")

    def test_too_long_history_fails_closed(self):
        pages = [[{"event": "labeled", "label": {"name": "other"}}]] * 120
        with self.assertRaises(gate.GateError):
            self.fake(pages).newest("/x", lambda e: False)


class HandOver(unittest.TestCase):
    def test_garbage_handover(self):
        for text in ("", "!!!", base64.b64encode(b"[1]").decode()):
            reviews, problem = gate.read_handover(text, "a" * 40, "d")
            self.assertEqual(reviews, {})
            self.assertTrue(problem)


if __name__ == "__main__":
    unittest.main()
