---
description: Measure where builds wait on people, and apply each repo's automation settings from the Build Autopilot page
argument-hint: "setup | install [owner/repo] | audit [owner/repo ... | --org ORG] [--since 30d] | apply [owner/repo ...] | apply --me | settings"
---

# /autopilot

Arguments: `$ARGUMENTS`

The plugin folder is `${CLAUDE_PLUGIN_ROOT}`. Each person (or team) has their own Build Autopilot page. Its link is
`artifact` in `~/.build-autopilot/config.json` on this computer. If that file is missing or has no `artifact`, run
`setup` first, then carry on with the subcommand that was asked for.

The page keeps its data in its own store, which you read and write with the `ArtifactData` tool (load it with
ToolSearch if it is deferred):

| Path | Holds | Written by |
|---|---|---|
| `audits/<owner>__<name>` | One repo's latest audit (the repo object from `audit.py`, plus `generated_at`, `window_days`) | You, on `audit` |
| `settings/<owner>__<name>` | That repo's chosen settings (see `docs/policy.md`), plus `updated_at`, `updated_by`, `applied_at` | The page; you set `applied_at` on `apply` |
| `settings/_defaults` | Settings for repos with no settings document | The page |
| `builders/<user id>` | One person's own preferences: `github`, `questions`, `quiet_hours` | That person, on the page |
| `changes/<id>` | Who changed which setting, when | The page |
| `config/page` | `hidden`: repos the page's list keeps under "Hidden"; `plugin_path`: this plugin's folder, so the page can show full commands | The page; you set `plugin_path` on `setup` |
| `requests/<owner>__<name>__<rec id>` | A one-time fix someone queued from a recommendation: `repo`, `rec_id`, `title`, `why`, `status` (`queued` or `filed`), `issue_number`, `issue_url` | The page queues; you file |

## Every run: file queued fixes first

Before the subcommand, `ArtifactData` `query` on `requests` for `status == queued`. For each one:

1. If an open issue in that repo already has the same title, link it instead of filing a new one.
2. Otherwise file a GitHub issue in the repo (`gh issue create --body-file`), titled with the request's `title`,
   and a body in four sections: **Problem** (the `why`, with the audit's numbers from `audits/<owner>__<name>`),
   **What to build**, **How we'll verify**, **Out of scope**. For `fix-clock-tests`: make the tests that fail by time
   of day or date use one fixed, injected clock; list the recent PRs that fixed such tests as evidence. For
   `permission-allowlist`: add read-only commands only (`git status/log/diff`, `gh pr view/list`, `gh run view/list`,
   test and lint commands) to the repo's committed `.claude/settings.json`; never commands that push, deploy,
   delete or touch production.
3. If the repo's CLAUDE.md names Sling trigger labels, add them so Sling builds it.
4. `ArtifactData` `update` the request with `status: "filed"`, `issue_number`, `issue_url`.

Mention the filed issues in one line each in your reply.

Choose the subcommand from the first word of the arguments. With no arguments, run `audit` for the current repo.

## setup

Connects this computer to a Build Autopilot page and chooses which repos it tracks. Safe to run again.

1. **The page.** Ask once: "Do you already have a Build Autopilot page (yours, or one a teammate shared with you as
   an Editor)? Paste its link, or say no." With a link, read it with the Artifact tool (`action: "read"`) to check
   it is a Build Autopilot page and that this person can edit it; save it. With no link, publish a new page: copy
   `${CLAUDE_PLUGIN_ROOT}/page/build-autopilot.html` into your scratchpad, publish it with the Artifact tool with
   `icon: "gauge"`, a one-line `description`, and exactly these `capabilities`:

   ```json
   {"db": {"rules": [{"path": "", "read": "view", "write": "admin"},
                     {"path": "builders", "read": "view", "write": "owner"},
                     {"path": "builders/{self}", "write": "interact"}]},
    "user": {"scopes": ["profile"]}}
   ```

   Write `{"artifact": "<link>"}` to `~/.build-autopilot/config.json`. Tell them the page is private until they
   share it from its Share menu, and that teammates who should change settings need Editor access.
2. **The repos.** List what this person can see and has worked on lately:
   `gh api user/repos --paginate -q '.[] | select(.archived|not) | [.full_name, .pushed_at] | @tsv'`, keep repos
   pushed in the last 60 days, and group them by owner. Show them as a numbered list (owner as a heading; one line
   per repo with its last push date), and say which are already on the page (`audits` documents). Ask which to
   track: numbers, owner names ("all of homeporthealth"), or "all". Don't guess; this is their choice.
3. Run `audit` for the chosen repos (one run with several `--repo`). Repos can be added later with
   `/autopilot audit owner/repo`, and hidden on the page without deleting anything.
4. Set `plugin_path` on `config/page` to `${CLAUDE_PLUGIN_ROOT}` (`ArtifactData` `update`, or `set` keeping `hidden` if the
   document doesn't exist yet), so the page's "Next step" panel shows full script paths.
5. Reply with the page link and one line on what to do there: pick a repo from the list, choose its settings, and
   use "Copy these settings to other repos" for the rest.

## audit

Read-only. Never change a repo, GitHub setting or workflow during an audit.

1. Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py` with the repos or `--org` from the arguments (default: the
   current checkout's `owner/repo`), `--since` (default `30d`), and `--out` a file in your scratchpad. It can take
   several minutes for an org; run it in the background and wait for it.
2. For each repo in the output without an `error`, write `audits/<owner>__<name>` with `ArtifactData` `set` (use one
   `batch` for several repos): the repo object plus `generated_at` and `window_days`. Write numbers only. The script
   never includes transcript text; don't add any.
3. Reply with the stdout summary, the page link, and, for each repo, the one recommendation that saves the most
   time. Don't list everything; the page does that.

## install

Takes one repo (default: the current checkout's) from chosen settings to working, as far as it can without the
person, and says exactly what's left. Work through these in order, skipping what's already done; check the real
state on GitHub each time rather than trusting the page.

1. **Settings.** Read `settings/<owner>__<name>` (or `_defaults`). If there are none, give the page link, say "choose
   this repo's settings there", and stop.
2. **`pipeline: own` or `none`.** Run `apply` (below) and stop: that small pull request is all such a repo needs.
3. **The GitHub side** (`pipeline: gate`). If the `autopilot` and `autopilot-review` environments aren't locked or
   their secrets are missing, you can't do this part: it needs the person's browser and their Anthropic key. Give
   them the exact command, with the full path, and stop:
   `"${CLAUDE_PLUGIN_ROOT}/scripts/setup-github-gate.sh" OWNER/REPO`
4. **The install pull request.** Run `apply`, which adds the gate's files. Open the pull request in the person's
   browser (`open <url>` on macOS, `xdg-open` elsewhere), and say in one line that it needs their merge (it changes the
   automation itself, so it's never merged automatically), with the link. Then stop.
5. **Branch rules** (once the gate's workflow is on the default branch and no `build-autopilot` ruleset exists).
   This changes the repo's settings: after it, nothing reaches the default branch except through a pull request that
   passes the gate and the chosen checks, and nobody can push straight to it. Ask the person once, naming the
   checks it will require. On a clear yes, run
   `"${CLAUDE_PLUGIN_ROOT}/scripts/setup-github-gate.sh" OWNER/REPO --ruleset --yes` and report what it printed. If it
   stops on something (for example a check that never runs on pull requests), pass that on; don't work around it.
6. **Done.** Point the person to the four live checks in `docs/v2-install.md`, step 5. Then run `audit` for the repo
   so the page's "Next step" panel catches up.

## apply

Turns the page's choices into the repo's `.github/autopilot.json`.

1. For each repo (default: the current checkout's), read `settings/<owner>__<name>`, falling back to
   `settings/_defaults`. If neither exists, say so and stop for that repo.
2. Build the policy file exactly as `docs/policy.md` describes: `version: 1`, `repo`, `pipeline` (default `gate`),
   `required_checks`, `level`, `sensitive`, `questions`, `tests`, `always_ask`, `source` (the page link) and
   `applied_at` (now, UTC). Drop page-only fields (`dismissed`, `updated_by`, `updated_at`).
   - `pipeline: gate`: the pull request also adds the gate's files (`docs/v2-install.md`, "What gets installed").
     Afterwards, tell the person to run `scripts/setup-github-gate.sh OWNER/REPO --ruleset` once it's merged.
   - `pipeline: own` or `none`: the pull request changes only `.github/autopilot.json` and the CLAUDE.md section.
     Never add the gate's files, a ruleset or auto-merge to such a repo. If the gate's files are already there
     from an earlier choice, the pull request removes them (`.github/workflows/autopilot.yml`, `.github/autopilot/`),
     and you tell the person to delete the `build-autopilot` ruleset in the repo's settings.
3. If the file on the default branch already matches (ignoring `applied_at`), report "already applied" and stop for
   that repo.
4. Otherwise open a pull request on a branch `autopilot/policy-<date>` that changes only
   `.github/autopilot.json` and, if it lacks one, adds this section to the repo's `CLAUDE.md` (so Sling and sessions
   without the plugin follow it too):

   ```
   ## Autopilot policy

   This repo's automation settings are in `.github/autopilot.json` (chosen on the Build Autopilot page; never edit
   it by hand). Before asking the user a question or waiting for an approval, read it and follow it: `always_ask`
   lists the only decisions that must go to a person; `level` says whether to stop at the PR, merge after an AI
   review, or merge and release; `questions` says whether to ask now or decide and keep going. When you decide
   instead of asking, list every such decision under "Decided without you" in the pull request and in your
   end-of-run summary (what you chose, why, how to undo it). Never decide silently.
   ```

   Open it in the person's browser (`open <url>` on macOS, `xdg-open` elsewhere) and give the link.
   Title it "Autopilot policy: <level>, questions <questions>". In the body, list each setting in plain words and
   what changed from the previous file.
5. This pull request changes the automation rules themselves, so never auto-merge it: it is a sensitive change
   under any policy. Tell the person it is ready to merge.
6. After it merges (or right away if they ask you to merge it), set `applied_at` on the settings document with
   `ArtifactData` `update`.
7. If `level` is `auto-merge` or `autopilot` and the repo's `allow_auto_merge` is false, or `main` has no rule
   requiring the tests to pass, say that GitHub's auto-merge needs those two repo settings, and ask before changing
   them. Changing a repo's settings is the person's call.

## apply --me

1. Get the GitHub login (`gh api user --jq .login`).
2. `ArtifactData` `query` on `builders` for `github == <login>`. If none, say how to add it on the page and stop.
3. Write `~/.claude/autopilot-user.json` with `questions` and `quiet_hours` from that document.

## settings

Open the page with the Artifact tool (`action: "open"`, `url` from `~/.build-autopilot/config.json`).
