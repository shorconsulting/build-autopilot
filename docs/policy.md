# The autopilot policy

Each repo's automation settings live in one file, `.github/autopilot.json`, on the repo's default branch. The
Build Autopilot page is where people choose them; `/build-autopilot:apply` writes the page's choices into the repo as
a pull request. Claude sessions (and, from v2, the GitHub workflow) read the file. Nobody edits it by hand.

A repo without the file behaves as `manual` with questions `ask-now`: exactly as today.

## Shape

```json
{
  "version": 1,
  "repo": "your-org/your-repo",
  "phase": "prelaunch",
  "pipeline": "gate",
  "required_checks": ["API · unit + integration", "lint"],
  "level": "auto-merge",
  "sensitive": {
    "rule": "two-ai",
    "paths": ["**/auth/**", "**/*login*", "**/payments/**", "**/billing/**", "**/phi/**",
              "**/migrations/**", ".github/**", "**/.env*", "**/secrets*"]
  },
  "questions": "batch",
  "tests": {
    "lane": "wave",
    "skip_duplicate_main": true,
    "suites": {
      "mobile": { "on_pr": "when-touched", "paths": ["apps/mobile/**", "packages/db/**", "**/migrations/**"] }
    }
  },
  "ci_cost": { "rules": "standard", "off": [], "budget_usd_month": 300 },
  "always_ask": ["delete-data", "spend-money", "message-people", "credentials", "legal-pricing", "ai-flags-it"],
  "source": "https://claude.ai/artifact/...",
  "applied_at": "2026-10-15T21:00:00Z"
}
```

## Fields

### `pipeline`: who reviews and merges

| Value | Means | What Build Autopilot does in the repo |
|---|---|---|
| `gate` (default) | Build Autopilot's GitHub gate reviews every pull request and merges what the settings allow. | `/build-autopilot:apply` installs the gate; `level`, `sensitive`, `tests` and `required_checks` all apply. |
| `own` | The repo already has its own review and merge automation, and keeps it. | Writes only this file. Doesn't install the gate or branch rules, and never turns on auto-merge itself: the repo's own pipeline decides. `questions` and `always_ask` still apply to Claude sessions. `level`, `sensitive` and `tests` are ignored. |
| `none` | Nobody merges automatically; a person merges every pull request. | Writes only this file. Claude sessions stop at the open pull request. `questions` and `always_ask` still apply. |

Running two merge systems in one repo makes them fight (each turns the other's auto-merge off), so a repo picks one.

Moving a repo from `gate` to `own` or `none`: `/build-autopilot:apply` removes the gate's files, and
`scripts/setup-github-gate.sh OWNER/REPO` offers to delete the `build-autopilot` ruleset. Do both. The ruleset requires
the gate's check, which nothing posts any more, so until it's gone no pull request can merge (see "Removing it" in
`v2-install.md`).

### `required_checks`: what must pass before a merge

The names of the CI checks (as shown on a pull request) that must pass before anything merges. With `pipeline: gate`
they become the required test checks in the repo's branch ruleset, beside the gate's own `autopilot/gate`. With
`own` or `none` they are recorded only; the repo's existing rules decide. An empty list means "the checks GitHub Actions
reported on the default branch's latest commit", which the setup script shows before using them.

Turning a test suite off entirely is a change to the repo's workflow files, made through a pull request, not a
setting here.

### `level`: how far a change goes without a person

| Value | What happens | What still needs you |
|---|---|---|
| `manual` | AI builds and opens the pull request. | Merging and releasing. |
| `ai-review` | An AI reviewer on the strongest model reads every change and posts a plain-English verdict. | One click to merge; releasing. |
| `auto-merge` | When tests pass and the AI reviewer approves, the change merges itself. | Releasing to the live site (one click from the daily summary). |
| `autopilot` | Changes merge and go live on their own once tests pass and the AI approves. A health check runs after each release and undoes it if it fails. | Nothing routine. You get a daily summary. |

Each level includes everything in the levels above it.

### `sensitive`: stricter handling for risky areas

Files matching `paths` are the sensitive areas: sign-in, payments, health data, secrets, database changes, and the
automation rules themselves (`.github/**`, which includes this file).

| `rule` | Effect on a change that touches a sensitive file |
|---|---|
| `two-ai` (default) | Two independent AI reviews, one of them an adversarial security review on the strongest model. Both must approve; then the repo's `level` applies. |
| `human` | The change waits for a person to merge, whatever the `level`. |
| `same` | No extra step. Not recommended. |

A change to `.github/autopilot.json` itself is only ever made by `/build-autopilot:apply` from the page.

### `questions`: what a Claude session does when it's unsure

| Value | Behaviour |
|---|---|
| `ask-now` | Stops and asks (today's behaviour). |
| `batch` | Picks the option that's easiest to undo, keeps going, and ends the run with one **Please confirm** list of those choices for a person to confirm or change. Stops only for the `always_ask` list. |
| `decide-reversible` | Decides anything that can be undone and keeps going. Every decision made without a person is listed under **Decided without you** in the pull request and in the end-of-run summary, with what was chosen, why, and how to undo it. Stops only for the `always_ask` list. |

With either setting, nothing is decided silently: if a session made no decisions on a person's behalf, its summary
says "Decided without you: nothing".

Merging and releasing are never "questions": the `level` already answered them.

### `always_ask`: decisions that always go to a person

These are the decisions a non-engineer can and should make. They stop the work whatever `level` and `questions`
say.

| Key | Means |
|---|---|
| `delete-data` | Deleting or overwriting real customer or production data, including database changes that drop data. |
| `spend-money` | Signing up for a paid service, raising a plan, or anything that adds a bill. |
| `message-people` | Sending email, texts or messages to customers, partners or anyone outside the team. |
| `credentials` | Creating, entering or changing passwords, API keys or access for people. |
| `legal-pricing` | Legal terms, contracts, prices or claims shown to customers. |
| `ai-flags-it` | The AI reviewer says a change needs a person. |

### `phase`: where the product is

One setting that picks the defaults below; any field set explicitly overrides its phase default. Moving phase is
one edit to this file.

| `phase` | Aim | `tests.lane` | AI review | Merge and release |
|---|---|---|---|---|
| `prelaunch` | Finish the build fast; nothing is live | `wave` | Sensitive paths only, or on request | Auto-merge on quick tests; nothing to release |
| `live-no-customers` | Prove it runs for real | `fast-lane` | Sensitive paths, `two-ai` | Auto-merge, then auto-release with a health check that rolls back |
| `customers` | Protect users and their data | `full` | Every PR, `two-ai`; `human` for sensitive paths | Auto-merge, release with one click |

On the settings page, choosing a phase sets `level`, `sensitive.rule`, `tests.lane` and `tests.skip_duplicate_main` to
the phase's values; each can be changed afterwards, and `phase` then records where the product is. A file that leaves a
field out gets the phase's value (`scripts/lanes.py effective`). A file with no `phase` is `prelaunch`: **waves are the
default**. A repo with `pipeline: own` maps the phase onto its own switches.

`live-no-customers` sets `level` to `auto-merge`, not `autopilot`: the release workflow with a health check and rollback
that `autopilot` needs is not built (`docs/v2-report.md`), so nothing here pretends to release for you.

### `tests`

| Field | Values |
|---|---|
| `lane` | `full`: every test runs before merging. `fast-lane`: quick tests gate the merge; the full suite runs right after and the change is undone automatically if it fails. `wave`: quick tests gate the merge; the full suite runs on `main` in batches (one run going, the newest push waiting, so each run covers whatever merged meanwhile) and on demand at the end of a wave; no automatic undo -- a red run means fix `main` before landing more. No batch-size setting: the batch sizes itself to how long the suite takes. `templates/github/workflows/test-lanes.yml` is the `wave` lane as a workflow. |
| `skip_duplicate_main` | `true` skips re-running the same tests on `main` after a merge when the pull request already passed them on the same code. |
| `suites` | Optional. Per-suite rules for a suite that is expensive or only matters to part of the repo, such as the mobile app's build and emulator tests (macOS minutes cost ten times Linux). A map of suite name to `{ "on_pr", "paths" }`. |

`suites.<name>.on_pr`:

| Value | A pull request runs the suite |
|---|---|
| `when-touched` (default) | only when a changed file matches one of `paths`. |
| `always` | every time. |
| `never` | never; it runs on `main` and on the schedule only. |

`paths` are globs (`**` crosses folders, `*` stays in one, a pattern with no `/` matches at any depth). List what the suite
actually depends on, not just its own folder: a suite that exercises the database should list the migrations and schema
too, so a database change runs it even when none of its own files changed. `main` and the schedule always run every
suite, so skipping on a pull request is a saving, never a gap. `scripts/lanes.py suites POLICY FILE...` says which suites a
set of files runs; `scripts/lanes.py render POLICY SUITE WORKFLOW` rewrites the workflow's `on.pull_request.paths` list
from the suite's `paths`, so the two cannot disagree (`apply` does this).

### `ci_cost`: the standard CI cost rules

On for every repo, every phase and every `pipeline` (including `own`): a file without `ci_cost` gets
`{"rules": "standard", "off": []}` (`scripts/lanes.py effective`). They exist because one prelaunch repo spent about
$3k of Actions minutes in a month, 90% of it one CI workflow, while its policy already said `tests.lane: wave` -- but
`pipeline: own` meant nothing applied it.

| Field | Values |
|---|---|
| `rules` | `standard` (default): every rule below is on. `none`: measure and report, change nothing. |
| `off` | Rule ids to leave alone in this repo, e.g. `["ci-skip-docs-only"]`. Say why in the pull request that sets it. |
| `budget_usd_month` | Optional. A 7-day cost (per month) over it raises a proposal and is shown in red on the page. |

| Rule | Checked in the workflow files | Measured |
|---|---|---|
| `ci-main-batched` | A workflow with 2+ jobs that runs on push to the default branch with no concurrency group, or one that falls back to `github.sha` | Push minutes, and what GitHub's one-running-one-waiting queue would have run instead, replayed over the real push times |
| `ci-pr-cancel-superseded` | A pull-request workflow with no `cancel-in-progress` | Cancelled minutes |
| `ci-fail-fast` | A matrix job with no `needs:` in a workflow of 3+ jobs | ~60% of failed-run minutes (the shards a red lint would have stopped) |
| `ci-skip-docs-only` | A pull-request workflow of 3+ jobs with no `paths` filter and no changed-files job | Not priced |
| `ci-poll-frequency` | A `schedule` more often than every 15 minutes | That schedule's minutes above a 15-minute cadence |
| `ci-shard-balance` | -- | Matrix legs whose average differs by 1.8x or more (the slowest is above 5 min) |
| `ci-waste` | -- | Over 20% of minutes on cancelled or failed runs |

A lint finding is reported only for a workflow that costs at least 5% of the repo's minutes (and at least 100) in the
window, so a cheap workflow never produces noise; `ci-poll-frequency` is always reported. Every finding carries the
fix (`STANDARD_RULES` in `scripts/ci_cost.py`) and, where measurable, the minutes and dollars it saves in that window.

**Cost of each CI decision.** `audit.py --cost` (or `ci_cost.py` alone) prices the last 7, 14 and 30 days: billed
minutes and dollars by workflow, by workflow and trigger (`ci.yml push` is the decision to test every merge), by job
(each job is a decision to have it), by runner OS, and the cancelled and failed minutes. Each job is billed rounded up
to the minute, as GitHub bills it. The method -- every run listed, a stratified sample of their jobs read -- is in
`ci_cost.py`'s docstring; the price is the billing API's when it answers (needs the `admin:org` scope), else list price.
The 7-day number per month next to the 30-day one shows whether a change is working.

### `read_cap`: stop whole-file Reads of long files

`true` makes the plugin's `PreToolUse` hook (`scripts/read_cap.py`) refuse a `Read` that has no `limit` on a file over 300
lines, with a one-line reason telling Claude to grep first or pass `offset` and `limit`. Absent or `false` (the default)
allows every Read. Images, PDFs and notebooks are never refused, and the hook allows on any error. It reads the file
from the session's working directory. Off by default; turn it on per repo.

## Proposals

Each audit is recorded (one line per run in `~/.build-autopilot/history/`), compared with the run before it (`since_last` on the page), and run through five fixed rules. A rule that fires adds a proposal to the repo's page with its evidence; nothing is tuned by a model and nothing adapts.

1. **Revert a loosening that made things worse.** A setting (`level`, `questions`, `sensitive.rule`, `tests.lane`, `read_cap`) was made less strict between two audits, and reverts or hotfixes, failed runs on `main` after a merge, or CHANGES verdicts are higher now than in the audit before the loosening: propose putting that one setting back.
2. **Loosen a quiet level.** `level` is `manual` or `ai-review` (stricter than `auto-merge`) for three audits in a row with no reverts, no failed runs on `main` and no CHANGES verdicts: propose the next level up (`manual` → `ai-review` → `auto-merge` → `autopilot`).
3. **Protect the directory that keeps breaking.** Three or more reverted, hotfixed or main-breaking changes touched one top-level directory in the window and `sensitive.paths` does not cover it: propose adding `<dir>/**`.
4. **Keep Claude's results small.** More than 25% of Read results over 20k characters, or more than 100 Bash results over 20k, in two audits in a row: propose a line for the repo's `CLAUDE.md` (grep first and Read with offset/limit; pipe Bash output through head, tail or grep).

5. **Turn the CI cost rules back on when cost climbs.** The 7-day CI cost (per month) is 25% above the last audit's, or over `ci_cost.budget_usd_month`, while `ci_cost.rules` is `none` or `off` lists a rule: propose `{"rules": "standard", "off": []}`. Needs `--cost`.

Turning a proposal on only saves it as a choice on the page. It reaches the repo when you run `/build-autopilot:apply`, which puts settings in `.github/autopilot.json` and any `CLAUDE.md` line in the same pull request. **Nothing changes a repo without a merged pull request**, and that pull request is never auto-merged.

## What enforces it

| Setting | v1 (now) | v2 |
|---|---|---|
| `questions`, `always_ask` | Every Claude session with the plugin (the `autopilot-policy` skill), and Sling through the repo's CLAUDE.md section | Same |
| `level` up to `auto-merge` | Claude sessions turn on GitHub's own auto-merge for the pull request once the AI review approves, so it merges even if nobody is around | A GitHub workflow runs the AI review on every pull request and turns on auto-merge itself |
| `level` = `autopilot` | Claude sessions release after merging, where they can | A release workflow with a health check and automatic undo |
| `phase` | Sets the other fields when chosen on the page; read by `scripts/lanes.py` | Same |
| `tests.suites` | `apply` writes each suite's `paths` into its workflow's `on.pull_request.paths` in the same reviewed pull request | Same |
| `tests.lane: wave` | `apply` adds `templates/github/workflows/test-lanes.yml` to a repo with no test workflow of its own, or tells you which two blocks to merge into the one it has | Same |
| `tests.skip_duplicate_main` | Recorded only; `drafts/` has a reviewed example for one repo | Applied to the repo's test workflow by a reviewed pull request |
| `ci_cost` | `apply` makes each finding's fix in the repo's workflow files, in the same reviewed pull request, for every `pipeline`; `audit --cost` measures it | Same |
| `read_cap` | The `PreToolUse` hook in `hooks/hooks.json` (`scripts/read_cap.py`) | Same |
| `sensitive` | Claude sessions run the second, adversarial review | The GitHub workflow requires both reviews |

The v2 column is built (not yet installed anywhere): `templates/github/`, installed per [v2-install.md](v2-install.md), design and threat model in [v2-report.md](v2-report.md). Release-with-health-check for `autopilot` and the `fast-lane` test lane are not built yet; the `wave` lane and `tests.suites` are.

Claude Code's own safety rules still apply on top of this file. Where they block a step (for example copying files
to a production server), the session leaves the exact command for a person and carries on with everything else.
