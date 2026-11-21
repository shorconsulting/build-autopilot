# Build Autopilot commands: the shared guide

Each `/build-autopilot:<name>` command reads this file: the opening part (the page, its store, and "Every run: file
queued fixes first"), then its own section below.

The plugin folder is `${CLAUDE_PLUGIN_ROOT}`. Each person (or team) has their own Build Autopilot page. Its link is
`artifact` in `~/.build-autopilot/config.json` on this computer. If that file is missing or has no `artifact`, run
`setup` first, then carry on with the command that was asked for.

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

Before the command's own steps, `ArtifactData` `query` on `requests` for `status == queued`. For each one:

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

Commands that take a repo default to the current checkout's `owner/repo` when none is given.

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
   `/build-autopilot:audit owner/repo`, and hidden on the page without deleting anything.
4. Set `plugin_path` on `config/page` to `${CLAUDE_PLUGIN_ROOT}` (`ArtifactData` `update`, or `set` keeping `hidden` if the
   document doesn't exist yet), so the page's "Next step" panel shows full script paths.
5. Reply with the page link and one line on what to do there: pick a repo from the list, choose its settings, and
   use "Copy these settings to other repos" for the rest.

## audit

Read-only. Never change a repo, GitHub setting or workflow during an audit.

1. Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py` with the repos or `--org` from the arguments (default: the
   current checkout's `owner/repo`), `--since` (default `30d`), `--cost`, and `--out` a file in your scratchpad.
   `--cost` lists every run of the last 30 days; leave it out only when the person says `--no-cost`. It can take
   several minutes for an org; run it in the background and wait for it.
2. For each repo in the output without an `error`, write `audits/<owner>__<name>` with `ArtifactData` `set` (use one
   `batch` for several repos): the repo object plus `generated_at` and `window_days`. Write numbers only. The script
   never includes transcript text; don't add any. The script itself appends the run to
   `~/.build-autopilot/history/` and adds `since_last` and `proposals` to each repo; write them as they are.
3. Reply with the stdout summary, the page link, and, for each repo, the one recommendation that saves the most
   time. Don't list everything; the page does that.

## cost

Read-only: what CI costs and which standard rules would cut it (`docs/policy.md`, "ci_cost").

1. Run `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/ci_cost.py --repo OWNER/NAME [--repo ...] --out <scratchpad>/cost.json`
   (default: the current checkout's repo) in the background; a busy repo takes several minutes. If `gh auth status`
   lacks `admin:org`, say once that prices are list prices and that `gh auth refresh -h github.com -s admin:org` gives
   the org's own.
2. Write each repo's result into its audit document: `ArtifactData` `update` on `audits/<owner>__<name>` with
   `{"cost": <the repo object>}`.
3. Reply with, per repo: the 7 / 14 / 30-day dollars (and the 7-day number per month), the three most expensive
   workflow-and-trigger rows, and the findings with the largest `saves_usd`. Offer `apply` for the findings.

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
2. Build the policy file exactly as `docs/policy.md` describes: `version: 1`, `repo`, `phase` (leave it out when the page has none: the repo's settings were saved before phases existed), `pipeline` (default `gate`),
   `required_checks`, `level`, `sensitive`, `questions`, `tests` (with `suites` when the page has any), `always_ask`, `source` (the page link) and
   `applied_at` (now, UTC). Drop page-only fields (`dismissed`, `updated_by`, `updated_at`, `claude_md`).
   - `pipeline: gate`: the pull request also adds the gate's files (`docs/v2-install.md`, "What gets installed").
     Afterwards, tell the person to run `scripts/setup-github-gate.sh OWNER/REPO --ruleset` once it's merged.
     The gate's jobs run on `vars.AUTOPILOT_RUNNER`; if the repo can't see that variable yet, say in the pull request
     body that it must not merge until `scripts/setup-github-gate.sh OWNER/REPO` (step 5b) has shared the org's
     variable with it (private repo) or set a GitHub-hosted repo variable (public repo; `docs/v2-install.md`, 3b).
     When updating an existing gate, keep `runs-on: ${{ vars.AUTOPILOT_RUNNER }}` on every job.
   - `pipeline: own` or `none`: the pull request changes only `.github/autopilot.json`, the CLAUDE.md section, and
     the workflow edits for `tests.suites` and `ci_cost` below.
     Never add the gate's files, a ruleset or auto-merge to such a repo. If the gate's files are already there
     from an earlier choice, the pull request removes them (`.github/workflows/autopilot.yml`, `.github/autopilot/`),
     and you tell the person to delete the `build-autopilot` ruleset in the repo's settings.
   - `tests.suites` (any `pipeline`): for each suite with `on_pr: when-touched`, run
     `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/lanes.py render .github/autopilot.json <suite> <workflow>` against the
     workflow that runs it (the suite's name, or the one the person names) and put the result in the same pull request,
     so the workflow's `on.pull_request.paths` is the suite's `paths`. For `always`, remove the workflow's `paths:`
     filter; for `never`, remove its `pull_request:` trigger and leave `schedule` and `push`. If `render` says it
     cannot (a `paths-ignore`, or no `pull_request.paths`), say so in the pull request body and leave that workflow alone.
     Run `lanes.py suites` over the last merged pull request's files for one example in the body: which suites it would have run.
   - Change types (any `pipeline`, `own` included; `docs/policy.md`, "Change types"): copy
     `templates/github/autopilot/lanes.py` to `.github/autopilot/lanes.py` byte for byte (for `own` and `none` this is
     the one file kept under `.github/autopilot/`). In every workflow that runs tests on `pull_request`, do this
     rather than describe it:
     - add `types: [opened, reopened, synchronize, ready_for_review]` to `pull_request` (keep any `paths`/`branches`);
     - replace the repo's own changed-files or docs-only job with the `changes` job from
       `templates/github/workflows/test-lanes.yml`, keeping the repo's runner and pinned action versions;
     - give each test job `needs: changes` and the template's `if:`; a job that runs only one suite adds
       `&& (needs.changes.outputs.class != 'light' || contains(needs.changes.outputs.suites, '"<suite>"'))`. The
       repo's general tests keep running on `light`: only `policy`, `ci` and a parked draft skip them. The `ci-skip-docs-only` notes below on required
       matrix jobs apply to every such `if:`.
     Where the repo already has a changed-files step that other jobs read (an output such as `code`, or an
     aggregate job that checks it), change that step, not the jobs: ask the classifier first, write the step's
     existing output from the answer, and keep the old rule as the branch that runs when the classifier cannot
     answer. Nothing that reads the output changes, and an open pull request whose base predates the rollout behaves
     as it did. `SConsInc/tidyhaus#1588` and `SConsInc/ranch-rides#2690` are worked examples; `SConsInc/knock#66`
     replaces the job outright. Give every job that can now be skipped a runner the repo's jobs actually start on.
     If the settings have no `suites` and the repo's own job selected by path, turn those paths into `suites` (and its
     docs patterns into `tests.no_code_paths`) on the page first, listing each workflow file in its own suite's
     `paths`, so nothing that was selective becomes run-everything. Say in the body what each type runs after the change.
   - `tests.lane: wave`: if the repo has no workflow that runs its tests on pull requests and on pushes to the
     default branch, add `templates/github/workflows/test-lanes.yml` (set its two commands from the repo's `package.json`
     scripts and ask nothing); if it has one, list in the body the two changes that make it the wave lane (a quick-tests
     job on `pull_request`, the full suite on `push` to the default branch with a concurrency group that has
     `cancel-in-progress: false`) and make them. Workflow files are sensitive paths: this is part of the same
     pull request, which is never auto-merged.
   - `ci_cost` (any `pipeline`, `own` included): write the field (default `{"rules": "standard", "off": []}`). Unless
     `rules` is `none`, read the latest audit's `cost.windows.7.findings` (run `ci_cost.py` if the audit has no `cost`)
     and, for each finding whose rule is not in `off`, make its `fix` in the named workflow, in this pull request. Keep
     every required check reporting (a skipped heavy job still lets the aggregate check pass; never a workflow-level
     `paths-ignore` on a workflow that holds a required check). For `ci-skip-docs-only`, check four things (the first three have a
     worked example in `ShorConsultingInc/ranch-rides#1570`):
     - A matrix job skipped by a job-level `if:` reports one check under its unexpanded name, so a required
       `test shard 1/6` never reports and the pull request blocks. Such a job runs with `if: ${{ !cancelled() }}` and
       skips step by step, with an empty service image when skipping, since an empty image starts no service.
     - The `changes` job diffs with `--no-renames`, or a code file renamed to `.md` counts as docs.
     - Before calling a `.md` file docs, check whether a test reads it. If one does, that path is code.
     - Wireframes, static HTML prototypes and reference folders count as docs when nothing builds, imports or reads
       them. Name those paths rather than matching every `.html` file, and treat anything a gate or test reads
       (requirements, rules, test cases) as code. Keep the secret scan and any conflict-marker or naming check running
       on these changes, since a prototype can carry a leaked key or an old name.

     For `ci-queue-then-main`: with `pipeline: gate`, add the `dedupe` job from `drafts/chasing-shade/test.yml`
     (`python3 .github/autopilot/skip_duplicate_main.py <workflow file>`; the gate install ships the script, and it
     skips on a passing `merge_group` run of the same workflow for the pushed commit as well as on identical files);
     with `pipeline: own` or `none`, a `dedupe` job of one `gh api` step on
     `actions/workflows/<file>/runs?event=merge_group&head_sha=$GITHUB_SHA&status=success` that writes `skip` only
     for a run with conclusion success, that sha, that event and that workflow path (any error: `false`), and never
     the gate's files. Only where the queue run executes the heavy jobs: a job conditioned on the event not being
     `merge_group` makes the queue run a false pass, and then the push run must stay (the rule is not reported
     there). Either way the heavy jobs `need` it and skip when `skip == 'true'`, the aggregate check still
     reports green on a skip (a release keyed on the push run must still go), and the run summary says what was
     skipped, why and how to undo it. Where the repo's own policy pins its workflow files (a workflow-policy gate),
     make the change through that policy in the same pull request rather than around it.
     For `ci-pr-selection-queue-full`: the fix is the repo's own selection. Narrow what a `pull_request` runs (its
     changed-files or import-graph selection, or its quick suite) and leave `merge_group`, the push to the default
     branch and the schedule on the full suite; never by a job-level `if:` on a required matrix job (the
     `ci-skip-docs-only` notes apply). A selection that falls back to the full suite on any shared path has not been
     narrowed: say in the body how often it narrows after the change.
     For `ci-fold-small-jobs`: make the named jobs steps of one job, keeping the required check's name on the job
     that remains and the order the steps needed.
     For `ci-pr-hold-superseded` (reported only when it pays): a `hold` job (`sleep 300`) on `pull_request` that the
     matrix and the other long jobs `need`, with `cancel-in-progress` keyed on the pull request number.

     Leave anything the repo's own rules protect (a CODEOWNERS or "gate" path that needs its own review) to that
     review: say so in the body. List each finding with its evidence and `saves_usd` in the body, and the total.
     `ci-shard-balance` and `ci-waste` are reported, not edited.
   - `claude_md` (accepted proposals, a list of lines): append each line the repo's `CLAUDE.md` does not already
     contain, under a `## Context habits` heading, in the same pull request. List each accepted proposal and its
     evidence (from the audit's `proposals`) in the body.
3. If the file on the default branch already matches (ignoring `applied_at`) and every accepted `claude_md` line is in `CLAUDE.md`, report "already applied" and stop for
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
7. Once it has merged, bring the open pull requests under the new rules; they still carry results from the old
   ones. For each open, non-draft pull request into the default branch: get its files
   (`gh api repos/OWNER/REPO/pulls/N/files --paginate --jq '.[].filename'`), run them through
   `lanes.py classify .github/autopilot.json`, and update its branch from the default branch
   (`gh api -X PUT repos/OWNER/REPO/pulls/N/update-branch`) so its checks run again under the merged workflows. Skip
   one that conflicts or is held, and say so. Report a table: pull request, type, suites it now runs. Do them
   oldest first, at most 10 at a time, so the runners are not flooded. Leave drafts alone: they run when marked ready.
8. If `level` is `auto-merge` or `autopilot` and the repo's `allow_auto_merge` is false, or `main` has no rule
   requiring the tests to pass, say that GitHub's auto-merge needs those two repo settings, and ask before changing
   them. Changing a repo's settings is the person's call.

## apply --me

1. Get the GitHub login (`gh api user --jq .login`).
2. `ArtifactData` `query` on `builders` for `github == <login>`. If none, say how to add it on the page and stop.
3. Write `~/.claude/autopilot-user.json` with `questions` and `quiet_hours` from that document.

## settings

Open the page with the Artifact tool (`action: "open"`, `url` from `~/.build-autopilot/config.json`).

## steward

The always-on steward of the global build rules (`skills/build-rules/SKILL.md`, values in `rules/build-rules.json`).
It finds unfinished build work in every tracked repo and gets it moving again. It runs unattended: no questions to
the person, nothing on the `always_ask` list, no merging or releasing beyond what each repo's
`.github/autopilot.json` allows, and never around branch rules. Arguments: repos (`owner/repo ...`; default: every
repo with an `audits` document on the page, else the current checkout), `--dry-run` (sweep and report, act on
nothing), or `--install`.

**`--install`** sets up the two schedules the rules need, once per person (not per repo), in a cloud environment
with this plugin and build-questions installed and GitHub access to the tracked repos:

1. The hourly sweep: `create_trigger` with `create_new_session_on_fire: true`, the cron in `steward.cron`, and the
   prompt `/build-autopilot:steward`. Replace an existing steward trigger with `update_trigger` instead of adding a
   second one.
2. The daily questions digest, from `questions.digest`: a trigger with that cron (it carries its own `CRON_TZ`) and
   the prompt `/build-questions:run all-linked --steps organize,sync,check,digest`. If `bq.py schedule show` names
   an older schedule, replace that task (`update_trigger`, or for a desktop task say it can be turned off in the
   desktop app's scheduled tasks), then `bq.py schedule set` with the new cron, `--tz`, `--label`, `--repos
   all-linked`, `--steps`, `--task-id` and `--where cloud`. The run ends with `PushNotification`, which reaches the
   person's phone through the Claude app; say in one line that notifications must be allowed for the Claude app on
   the phone, and that the digest also goes by the build-questions `email_method` if one is set.
3. The capacity check, in two parts. In the cloud, a trigger with the prompt `/build-autopilot:steward --capacity`,
   hourly (cloud scheduled tasks run at most hourly): it can only find and cancel stale runs, because cloud sessions
   can't read runners or reach the boxes and don't keep `~/.build-autopilot/capacity-state.json`. Runner and host
   actions (restarts, pause and resume, reroutes) only work from the person's computer, where the state file
   persists and the dashboard's box health is at `http://127.0.0.1:4517/api/data`: run it there every
   `capacity.cron` (10 minutes) as a scheduled task on that computer, with `--hosts` set. Replace an existing one
   instead of adding a second.
4. Reply with the schedules in the person's local time and how to stop them (`delete_trigger`).

**`--capacity`** (every 10 minutes; build rule 3, "a stalled check is restarted, not waited on", applied to the
runners). GitHub gives a job only to a runner in the same org that has every label the job asks for. It never
reroutes, so idle runners with other labels stay idle while a pool queues for hours.

1. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/steward.py" capacity --repo A --repo B ... --out <scratchpad>/capacity.json`.
   It is read-only. Per repo it lists the queued runs nobody needs: head superseded, an older duplicate of the same
   workflow file, event, head and pull request, or a closed pull request's run of a workflow that is pull-request CI
   (it also has runs for open PRs) that was created before the PR closed (a run the close started is kept). A workflow that only runs when a PR closes is never cancelled, nor anything matching mark-done,
   release, deploy, publish, cleanup, backport, notify or preview, or `capacity.keep_after_close`. GitHub-hosted
   label sets are never a pool. A runner finding (offline, hung, pool down) only becomes an action when it held on
   the previous pass too (state in `~/.build-autopilot/capacity-state.json`), and one runner is restarted at most
   once per `capacity.restart_cooldown_minutes` and `capacity.max_restarts` times in all, counted per runner across
   offline, hung and pool-down (a runner a pool-down covers is restarted once, as `restore-pool`), before it becomes a
   `report` for the person; idle-while-queued also needs a matching job queued over
   `capacity.settle_minutes`. Reroute and report findings repeat at most once per `capacity.report_cooldown_minutes`.
   Duplicates are only cancelled for `capacity.dedupe_workflows` (workflows that do the same work whatever started
   them). Below `capacity.min_rate_remaining` API calls left it runs lean (no close-time lookups or job sampling);
   close times are cached in the state file, so each closed branch is looked up once. Per org, when
   runners can be read, it lists the label pools the queued jobs ask for and these findings: `runner-offline`,
   `pool-down`, `no-runner-for-labels`, `idle-but-queued` (hung), and `idle-capacity-unreachable` (online idle
   runners that no queued job can use while a pool is backed up).
2. Act without asking:
   - `cancel-stale`: `gh api -X POST repos/<repo>/actions/runs/<run>/cancel` for each listed run (at most
     `capacity.max_cancels` per repo per pass). Any run can be re-run, so this needs nobody.
   - `restart-runner` and `restore-pool`: through Remote Control on the person's computer (runner admin and the
     machines are only reachable from there). Restart only that runner's own service or container. Never delete a
     registration. If the service restart doesn't bring the runner back and the action says `machine_restart_ok: true`
     (no GitHub runner on that `box` is busy), restart the runner VM (`colima restart`) without asking, as Build
     Autopilot is self-healing, but only after `docker ps` in that VM (and `colima status`: no k3s) shows nothing running except
     runner containers (names matching that box's `capacity.host.runners` prefixes); the github-throughput dashboard
     runs on the person's computer, not in the VM. Anything else running there makes it a `report` with what is
     running. Drain one runner at a time: read it from the GitHub API right before stopping it, and `docker stop` it
     only if it is not busy; if one is busy, start the ones already stopped again and make it a `report`. Once all are
     stopped (a stopped runner takes no new job), restart the VM. If a job was interrupted anyway, re-run it once. When `machine_restart_ok` is false, wait for the `busy` runners and try again next pass;
     the restart cap (`capacity.max_restarts`) still turns a runner that never comes back into a `report`. Every VM
     restart and every cancel batch goes in the run's summary afterwards (what, where, why), so the person can see
     what was done. Never reboot a physical server. A box you can't reach is one `report`, naming the machine and
     why.
   - `pause-slot` and `resume-slot` (from `--hosts`, the github-throughput dashboard's box health; run on the
     person's computer with `--hosts http://127.0.0.1:4517/api/data`): keep every runner box (Hetzner, the Mac mini
     and its Colima VM) inside its limits in `capacity.host`. Each box is read on its own: the Mac's reading can pause only
     the Mac's own slots, and the Colima VM's slots need the VM's own reading (`colima` in the dashboard's hosts). On `pause-slot`, stop ONE idle runner slot on that box
     the action names (its service or container). The check itself picks only an idle, online slot on that box
     (`capacity.host.runners` maps each box to its runner name prefixes) and never one that would leave fewer than
     `min_slots` online; if none qualifies it is a `report`. Check the runner is still idle just before stopping it.
     Then record it: `steward.py capacity-note --host <box> --paused <runner>` (record nothing if none could be
     paused). Paused slots are left out of the offline and pool checks, so they are never restarted by mistake. On
     `resume-slot`, start the named runner, then `capacity-note --host <box> --resumed <runner>`. Pausing needs red
     load or memory on two passes in a row; red disk alone is a `report` (prune Docker or the Colima disk). One
     change per box per `cooldown_minutes`.
   - `improve` (`stacked-workflow`): a workflow has over `capacity.stack_per_pr` queued runs per open pull request
     (and `capacity.stack_min_runs` in all), so every label, review or push queues another job on the pool. Its
     runs aren't cancelled (each may act on its own event); file `Steward: stacked-workflow <name>` once (link an
     open one) and fix it with a pull request by the repo's policy: a per-PR concurrency group, or start its jobs
     only on the events they act on (HPortHealth/app#5327).
   - `add-slot` (from `--hosts`): every online slot on a box is busy, a backed-up pool fits its slots, the box has
     headroom and fewer slots are online than `capacity.host.max_slots[box]`: start ONE more slot there (one change
     per box per `cooldown_minutes`; a box with no ceiling never grows). On Hetzner `capacity-cron.sh` does it itself
     when `apply.env` sets `BOX=hph-ci`: it reads the box's own load, memory and disk and starts the lowest unused
     slot number through the same restart wrapper. Elsewhere, start that box's next runner service or container.
   - `reroute`: before opening a pull request, look for an open one from the steward for the same labels and
     update it instead. Open a pull request in the repo that moves its light jobs (auto-merge, sweeps, labelling, mark-done,
     cancel-on-close) to the idle runners' label, or file it as the steward lane's next item. A CI-machinery or ADR
     change follows the repo's own landing rules.
3. Say nothing when nothing was found. Otherwise one line per repo: runs cancelled, runners restarted, PRs opened.

**A sweep:**

1. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/steward.py" sweep --repo A --repo B ... --out <scratchpad>/sweep.json`.
   It is read-only and prints one line per repo; the file holds each repo's `main`, `runners`, `pull_requests` and
   `actions` (most urgent first). Read each repo's `.github/autopilot.json` from its default branch.
2. Work the `actions`, at most `steward.max_resumes_per_sweep` across all repos, skipping any a lane already holds
   (a fast-builder claim on the bus, or a PR updated in the last `stall.quiet_minutes`):
   - `fix-runner`: the steward's own job. Runner admin is refused in cloud sessions, so do it through a Remote
     Control session on the person's computer (it has their `gh` login and reaches their runner machines):
     remove the runner's custom label (`gh api -X DELETE .../actions/runners/<id>/labels/<label>`) so it takes no
     jobs, never delete its registration; then on its machine compare it with a healthy sibling, clean its work
     folder, fix the mount or ownership, and restart it; add the label back only after a re-run passes on it, then
     re-run its failures. Tell the person only if the machine can't be reached or the fix needs a credential.
   - `fix-main`: the steward lane's first job. In fast-builder, check `main.json` and that the steward lane is
     working on it (`$FB send --to <steward lane> --kind request` if not); otherwise start the fix (a session on a
     new branch, by the repo's policy).
   - `rerun`: `gh api -X POST repos/<repo>/actions/jobs/<job>/rerun` for each failed machine job (or `gh run rerun
     <run> --failed`), at most `lanes.infra_reruns_per_pr` times per PR (count earlier attempts from the run's
     `run_attempt`).
   - `restart-checks`: cancel the stalled run (`gh api -X POST repos/<repo>/actions/runs/<run>/cancel`) and re-run it.
   - `fix` and `rebase`: hand it to its lane. In fast-builder, `$FB send --to <lane> --kind request --body "PR #n is
     red: <checks>"`, or `$FB reopen --lane <lane> --body ...` if the lane is done; a PR labelled `fb:stuck` goes to
     the fixer lane. Without a lane, start a session for it (Remote Control on the person's computer if that is
     where the repo's builds run, else a cloud session) with the PR, its failing checks and the build rules.
   - `land`: green and still open. Follow the repo's policy: `pipeline: own` or the GitHub gate land it themselves,
     so check why they haven't (a required check that never ran, a hold) and report that; otherwise merge as the
     autopilot-policy skill allows.
   - `improve` (build rule 6): something that keeps coming back. File it once as an issue titled
     `Steward: <kind> <check or runner>` (link an open one with that title instead), with the `evidence` and the
     last few sweeps' numbers, then build the fix like `fix-main`: the steward lane's next item in fast-builder,
     otherwise a session on a new branch by the repo's policy. Never skip, quarantine or delete a test to clear it.
   - `resume` (an idle draft) and `resume-branch` (a `fast-builder/wip/*` branch with no PR): hand it to the lane
     whose paths it touches, as for `fix`, with its handoff if there is one.
3. A lane that asked for more work (fast-builder `requests_for_conductor`, or a lane done while its repo still has
   items in the sweep) gets the next batch by build rule 4.
4. Reply (or `PushNotification` in a scheduled run) with one line per repo: open PRs by state, what was re-run,
   handed back and landed, the improvements started, how the health line moved since the day before (open PRs, red
   PRs, main red hours, median hours to merge), and only the items that need a person. Say nothing more when nothing needs one.

### Keeping a Mac's runners inside its load (no agent)

The capacity check's `pause-slot` needs an agent on the person's computer and the throughput dashboard, and moves
one slot per 20 minutes. On the Mac mini the runners share one Colima VM, and when too many heavy jobs run at once
every job slows down (21 Nov 2026: typecheck took 76 minutes against about 25 seconds), so fewer runners clear the
queue sooner. `scripts/mac-runner-governor.sh` does this on the Mac itself, every 2 minutes, as the last step of the
Mac runner watchdog (`scripts/install-mac-runner-watchdog.sh` installs both as one launchd agent):

- It reads the VM's memory stall (PSI), free memory and load per core, the Mac's memory pressure level, swap growth
  and load per core, and each runner's running job against the median time that job took on this Mac (learned from
  the runners' own logs; no estimate until it has seen a job three times).
- Overloaded two passes in a row (once, when memory is nearly gone or the Mac is in critical pressure), or a job
  running three times its usual time (and at least 10 minutes) while other jobs share the box: it takes ONE runner
  out of service. An idle one is stopped at once, after one more look right before the stop. With `GOV_GH_ORGS`,
  when none is idle, the busy one closest to done drains (below). A running job is never stopped, and each runner
  group keeps at least `GOV_MIN_SLOTS` (2).
- Calm three passes in a row: it puts ONE back (cancels a drain, or starts the runner paused most recently).
- One change per `GOV_COOLDOWN_S` (6 minutes). Paused runners are listed in
  `~/.local/state/mac-runner-watchdog/governor/paused/`; while the governor is on the watchdog doesn't restart them,
  and a capacity check run on that Mac leaves them out of its offline and pool checks for up to a day (an older
  pause is reported and restarted like any offline runner). The governor itself starts any runner paused longer
  than `GOV_MAX_PAUSE_S` (a day) whatever the readings say. Installing with `WATCHDOG_GOVERNOR=0` starts them all.
  Settings are taken from the installer's environment each time, so give them again on every re-run.
- Draining needs `GOV_GH_ORGS` (e.g. `sconsinc:SConsInc`) and a `gh` login that can administer the org's runners:
  the runner's custom labels come off at GitHub so it is given no new job, it is stopped once its job ends and
  GitHub says it is not busy, and its labels go back when it returns. Without it, only idle runners are stopped
  (under a backlog a runner takes its next job within a second of the last, so stopping it "between jobs" could
  kill a job just handed to it). An idle runner is looked at again right before it is stopped; a job handed to it
  in the fraction of a second between that look and the stop would still be cut off, which is unlikely when the
  runner was idle (no matching job was waiting).

```
bash scripts/install-mac-runner-watchdog.sh                               # install or update (governor on)
GOV_GH_ORGS='sconsinc:SConsInc' bash scripts/install-mac-runner-watchdog.sh  # settings are written into the agent
~/.local/bin/mac-runner-watchdog.sh --status    # runners, the last load reading, paused, draining, usual job times
~/.local/bin/mac-runner-governor.sh --dry-run   # what it would do now
~/.local/bin/mac-runner-governor.sh --resume-all
WATCHDOG_GOVERNOR=0 bash scripts/install-mac-runner-watchdog.sh           # watchdog only
```

Thresholds are at the top of the script (`GOV_*`), and every action is a line in
`~/Library/Logs/mac-runner-watchdog.log`.

### Running the capacity check on a runner box (no agent)

On an always-on box (the Hetzner hph-ci server), `scripts/capacity-cron.sh` runs one pass: `steward.py capacity` per
org with that org's fine-grained token, cancels each org's stale runs with that token, then restarts this box's own
runner services at most once per slot per pass (`scripts/capacity_apply.py`). It never deletes a registration,
restarts a VM or reboots. It runs as a dedicated non-root user from a pinned, reviewed commit, so nothing merged later
runs on the box until a person moves the pin. Setup (as root, once):

```
useradd --system --home /var/lib/build-autopilot --create-home ba-steward
git clone https://github.com/shorconsulting/build-autopilot /opt/build-autopilot
git -C /opt/build-autopilot checkout --detach <reviewed commit>   # moving the pin is a person's step
chown -R root:root /opt/build-autopilot                           # the service user can't change the code
install -o root -g root -m 755 /opt/build-autopilot/scripts/restart-runner-slot.sh /usr/local/sbin/restart-runner-slot
echo 'ba-steward ALL=(root) NOPASSWD: /usr/local/sbin/restart-runner-slot' > /etc/sudoers.d/ba-steward
# the wrapper accepts one plain slot number and restarts only hph-runner@<n>
chown root:root /etc/hph-ci/steward && chmod 755 /etc/hph-ci/steward   # root-owned: ba-steward can't edit apply.env
# /etc/hph-ci/steward/HPortHealth.token, SConsInc.token: owner ba-steward, mode 600
#   (fine-grained: Actions read/write, Contents and Pull requests read, Self-hosted runners read/write)
# /etc/hph-ci/steward/repos: one owner/repo per line
# /etc/hph-ci/steward/apply.env (root:root, mode 644):
#   SLOT_REGEX='^hph-ci-(?:light-)?([0-9]+)(?:-|$)'   # hph-ci-10 and hph-ci-light-10 are both hph-runner@10
#   RESTART='sudo -n /usr/local/sbin/restart-runner-slot {slot}'
#   DRY_RUN=1    # keep until a person has read the first journal output, then remove it
#   BOX=hph-ci   # lets the pass read this box's load and start one more slot (add-slot) up to capacity.host.max_slots
# systemd: build-autopilot-capacity.service (User=ba-steward, Type=oneshot, ExecStart=.../capacity-cron.sh)
#          build-autopilot-capacity.timer (OnCalendar=*:0/10)
```

Each pass prints one line per action, so `journalctl -u build-autopilot-capacity` is the record of what it did.

Every `SWEEP_EVERY_MIN` minutes (default 30, set it in `apply.env`; 0 turns it off) the same pass also runs
`steward.py sweep` and `scripts/sweep_apply.py`, plain code with no AI: a PR whose checks failed before the code ran
(checkout, setup, a vanished runner, a cancel with nothing else of that name) gets its failed jobs re-run, at most
`lanes.infra_reruns_per_pr` times by the run's attempt number; a check *running* past `stall.check_running_minutes`
is cancelled and re-run on the next pass. A check stalled while *queued* is left to capacity (cancelling it only
re-queues it at the back). PRs labelled parked, do-not-merge, hold or blocked are skipped, at most 20 actions per org
per pass. Red code, conflicts and drafts stay with the lanes. The timer's sweep is `--lite`: it reads only the `SWEEP_MAX_PRS`
(default 40) most recently updated PRs, skips the runner, flaky and merge-time scans, and steps aside when fewer than
`SWEEP_MIN_RATE` (default 2500) API calls are left this hour. A token's limit is its owner's (5,000 an hour, shared by
every token and tool they run), so the box must never spend the last of it.
