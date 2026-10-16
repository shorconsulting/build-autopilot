# The autopilot policy

Each repo's automation settings live in one file, `.github/autopilot.json`, on the repo's default branch. The
Build Autopilot page is where people choose them; `/autopilot apply` writes the page's choices into the repo as
a pull request. Claude sessions (and, from v2, the GitHub workflow) read the file. Nobody edits it by hand.

A repo without the file behaves as `manual` with questions `ask-now`: exactly as today.

## Shape

```json
{
  "version": 1,
  "repo": "your-org/your-repo",
  "pipeline": "gate",
  "required_checks": ["API · unit + integration", "lint"],
  "level": "auto-merge",
  "sensitive": {
    "rule": "two-ai",
    "paths": ["**/auth/**", "**/*login*", "**/payments/**", "**/billing/**", "**/phi/**",
              "**/migrations/**", ".github/**", "**/.env*", "**/secrets*"]
  },
  "questions": "batch",
  "tests": { "lane": "full", "skip_duplicate_main": true },
  "always_ask": ["delete-data", "spend-money", "message-people", "credentials", "legal-pricing", "ai-flags-it"],
  "source": "https://claude.ai/artifact/...",
  "applied_at": "2026-10-15T21:00:00Z"
}
```

## Fields

### `pipeline`: who reviews and merges

| Value | Means | What Build Autopilot does in the repo |
|---|---|---|
| `gate` (default) | Build Autopilot's GitHub gate reviews every pull request and merges what the settings allow. | `/autopilot apply` installs the gate; `level`, `sensitive`, `tests` and `required_checks` all apply. |
| `own` | The repo already has its own review and merge automation, and keeps it. | Writes only this file. Doesn't install the gate or branch rules, and never turns on auto-merge itself: the repo's own pipeline decides. `questions` and `always_ask` still apply to Claude sessions. `level`, `sensitive` and `tests` are ignored. |
| `none` | Nobody merges automatically; a person merges every pull request. | Writes only this file. Claude sessions stop at the open pull request. `questions` and `always_ask` still apply. |

Running two merge systems in one repo makes them fight (each turns the other's auto-merge off), so a repo picks one.

Moving a repo from `gate` to `own` or `none`: `/autopilot apply` removes the gate's files, and
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

A change to `.github/autopilot.json` itself is only ever made by `/autopilot apply` from the page.

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

### `tests`

| Field | Values |
|---|---|
| `lane` | `full`: every test runs before merging. `fast-lane`: quick tests gate the merge; the full suite runs right after and the change is undone automatically if it fails. |
| `skip_duplicate_main` | `true` skips re-running the same tests on `main` after a merge when the pull request already passed them on the same code. |

## What enforces it

| Setting | v1 (now) | v2 |
|---|---|---|
| `questions`, `always_ask` | Every Claude session with the plugin (the `autopilot-policy` skill), and Sling through the repo's CLAUDE.md section | Same |
| `level` up to `auto-merge` | Claude sessions turn on GitHub's own auto-merge for the pull request once the AI review approves, so it merges even if nobody is around | A GitHub workflow runs the AI review on every pull request and turns on auto-merge itself |
| `level` = `autopilot` | Claude sessions release after merging, where they can | A release workflow with a health check and automatic undo |
| `tests` | Recorded only | Applied to the repo's test workflow by a reviewed pull request |
| `sensitive` | Claude sessions run the second, adversarial review | The GitHub workflow requires both reviews |

The v2 column is built (not yet installed anywhere): `templates/github/`, installed per [v2-install.md](v2-install.md), design and threat model in [v2-report.md](v2-report.md). Release-with-health-check for `autopilot` and the `fast-lane` test lane are not built yet.

Claude Code's own safety rules still apply on top of this file. Where they block a step (for example copying files
to a production server), the session leaves the exact command for a person and carries on with everything else.
