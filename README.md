# build-autopilot

A Claude Code plugin that finds where your builds wait on a person, and lets each repo choose how far a change may
go without one.

Most of the delay in an AI-built project isn't the build. It's the gap after the tests pass, while a person gets
around to merging, releasing, or answering a question. The people running these projects often aren't engineers,
so a human "review" step is a click with no real check behind it. Build Autopilot replaces that click with an AI
review and keeps people for the decisions they can actually make: spending money, deleting real data, messaging
customers, credentials, legal and pricing text.

Works for anyone with GitHub repos: each person gets their own page on first run and chooses which repos it tracks.
Nothing about any particular person's repos is built in.

## The parts

| Part | What it does |
|---|---|
| `scripts/audit.py` | Read-only. For each repo: how long changes wait after tests pass, how long tests take, repeat test runs, how releases happen, which automation GitHub has turned on, and how often Claude sessions stopped to ask (counts only; no transcript text leaves your computer). Ends with recommendations, each with pros, cons, risk and hours saved. |
| The Build Autopilot page | Shows each repo's audit, lets you turn recommendations on, and holds each repo's settings, plus each builder's own interruption preferences. One page per person or team; its link is kept in `~/.build-autopilot/config.json`. Pick a repo from the list; copy its settings to other repos in one step. |
| `/build-autopilot:setup`, `:install`, `:audit`, `:apply`, `:settings` | `setup` makes or connects your page and picks your repos; `install` takes a repo through every remaining step; `audit` fills the page; `apply` writes a repo's settings into `.github/autopilot.json` with one pull request; `apply --me` saves your own preferences on this computer. |
| `autopilot-policy` skill | Makes every Claude session in a repo follow that repo's settings. |
| `scripts/lanes.py` | Phases and test lanes: what each phase sets, which `tests.suites` a set of changed files runs (so the mobile suite runs when the app or the database changes, and not otherwise), and rewriting a workflow's `paths:` list from the policy. Waves (`phase: prelaunch`, the `wave` test lane) are the default. |
| `docs/policy.md` | The settings, field by field, and what enforces each one today. |
| `templates/github/` (v2) | The GitHub gate: a workflow that reviews every pull request with Claude, adds a second security review for sensitive files, and turns on auto-merge when the settings allow. Installed by `/build-autopilot:apply` following `docs/v2-install.md`; design and threat model in `docs/v2-report.md`. |

## Install (once per Mac login)

```bash
claude plugin marketplace add shorconsulting/build-autopilot
```

```bash
claude plugin install build-autopilot@build-autopilot
```

Updates: `claude plugin marketplace update build-autopilot`, then restart Claude.

Needs `python3` (3.9+), `git`, and `gh` logged in with read access to the repos you audit. Then run `/build-autopilot:setup`:
it asks whether you already have a page (for example one a teammate shared with you as an Editor) or makes you a new
one, then lists the repos you've pushed to lately and asks which to track.

## Use

```
/build-autopilot:setup                         # first run: your page and which repos it tracks
/build-autopilot:install                       # take the current repo through every remaining step
/build-autopilot:audit                         # the current repo, last 30 days
/build-autopilot:audit owner/repo              # add or refresh one repo
/build-autopilot:audit --org my-org            # every repo in an org pushed to in the window
/build-autopilot:settings                      # open the page
/build-autopilot:apply                         # put the page's choices into the current repo
/build-autopilot:apply --me                    # your own preferences, on this computer
```

### Without Claude

```bash
python3 scripts/audit.py --repo owner/repo --since 30d --out audit.json
python3 scripts/audit.py --org my-org --tz America/New_York --out audit.json
```

## Safe by default

- A repo with no settings behaves exactly as it does today.
- The audit never changes anything.
- Settings reach a repo only through a pull request that a person merges. That pull request is never auto-merged.
- Claude sessions never edit `.github/autopilot.json`.
- Sign-in, payments, health data, database changes and the automation rules themselves get two independent AI
  reviews by default (or wait for a person, if you choose).
- Claude Code's own safety rules still apply on top of the settings.
