---
name: build-rules
description: The global build rules every build follows, in every repo, with no per-repo setting - where questions go and when the person hears about them, that a lane is not done until its pull requests are merged and green, restarting stalled CI and lanes, asking the steward for more work, and the hourly steward sweep for red and unfinished work. Use at the start of any build, fix or lane work, when CI is red or stuck, when a lane runs out of work, and before you finish.
---

# Global build rules

These rules apply to every build, in every repo, for everyone with this plugin. They are not chosen per repo and
there is no setting to turn them off. Their numbers (times, limits) live in one place,
`${CLAUDE_PLUGIN_ROOT}/rules/build-rules.json` (`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/steward.py" rules` prints
it); change them there, in this plugin, and every build follows.

They sit under the repo's own autopilot policy (the `autopilot-policy` skill): that still decides what may merge,
release, or must go to a person (`always_ask`). These rules decide how long work keeps going and where questions go.
Where a repo runs fast-builder, its lanes, lander, fixer and conductor are how these rules are carried out; the
commands below are fast-builder's (`$FB` is its `fb.py`).

## 1. Questions go in the build-questions list, and the person hears once a day

- Every product question goes into the repo's one list, `.claude/build-questions/questions.jsonl`, by the
  build-questions plugin's rules (its `build-questions` skill): AskUserQuestion is captured by its hook; anything else
  is `bq.py record`. Never keep questions in chat, plans, PR bodies or pages of your own.
- Don't stop to wait for an answer that is not an `always_ask` item. With `questions: decide-reversible` or `batch`,
  decide, record it with `--source decided`, list it under "Decided without you", and keep building. A question only
  a person can answer is recorded open (with options and a recommendation), and the lane works on what it doesn't
  block.
- The person hears about open questions once a day, at the time in `questions.digest` (every day at 4:52 pm Central,
  America/Chicago). That is a scheduled cloud routine running `/build-questions:run all-linked --steps
  organize,sync,check,digest`, which ends with `PushNotification` so it reaches their phone through the Claude app,
  and also sends the digest by the build-questions `email_method`. `/build-autopilot:steward --install` sets it up.
  Don't send question notifications at other times; an `always_ask` decision is the only exception.

## 2. A lane is not done until its pull requests are merged and green

Done means `lanes.done_means`: every pull request the lane opened is merged (or deliberately closed, with the reason),
its CI is green, and nothing it started is left on a branch without a pull request. Opening a PR is the middle of the
work, not the end.

- After you push, wait for the checks (`gh run watch`, or `$FB pr-wait <n> --seconds 540`), never by re-reading the PR
  in a loop. Green and allowed to merge by the policy: land it. Red: read the first error lines and fix it on the
  same branch. Behind or in conflict: update or rebase it.
- A failure at checkout, job setup, a download or the runner is the machine's, not the change's: re-run the failed
  jobs (`gh run rerun <id> --failed`), up to `lanes.infra_reruns_per_pr` times, and don't change code for it.
- After `lanes.max_fix_rounds_per_pr` rounds on one PR without green, hand it on rather than stop: in fast-builder,
  `$FB stuck` to the fixer lane; otherwise record a build question saying what is failing and what you tried, and
  move to the next item. A PR is never just left red.
- The plugin's Stop hook (`scripts/finish_guard.py`) holds a session that tries to finish while its branch's PR is
  red or still running, up to that many rounds. Treat its message as the next step, not as an obstacle.

## 3. Stalled CI and stalled lanes are restarted, not waited on

The limits are in `stall`. A check queued over `check_queued_minutes` or running over `check_running_minutes` is
stalled: cancel and re-run it. A lane with no commit, push, heartbeat or check progress for `quiet_minutes` is
stalled: in fast-builder the conductor frees it and a listener restarts it from its handoff; outside it, the steward
sweep picks its work up (rule 5).

- A self-hosted runner whose jobs fail at checkout or setup `broken_runner_failures` times with none passing is
  broken. Every PR it picks up goes red, so re-running onto it only adds red. Report it once as a person's job (take
  it offline or clean its work folder; credentials and machines are theirs), and re-run its failures once it is out.
- The default branch red for over `main_red_hours`: fixing it comes before any other work for the steward lane, and
  the person hears once.

## 4. A lane that runs out of work asks the steward, and the steward starts its next batch

- A lane that clears its list does not finish. It asks the steward for more: in fast-builder, read the plan's
  Overflow, then `$FB send --to conductor --kind request --body "lane <lane> has cleared its list: what next?"` and
  `$FB wait`, still `working`, for up to `refill.steward_answers_within_minutes`.
- The steward answers with a new batch for that lane, in that lane's paths: the oldest unfinished work for it from the
  sweep (red PRs, drafts, leftover branches), then open issues and plan items it owns, then the next items from the
  most loaded lane (moving ownership only by fast-builder's rule, never for paths with an open PR). A lane already
  marked done gets it with `$FB reopen --lane <lane> --body "..."`, so a listener starts it again.
- "Nothing more" is an answer only when the sweep, the issues and the plan hold nothing for that lane.

## 5. The steward sweeps every hour for unfinished work

New builds don't go back for old work on their own, so the steward does. Every hour (`steward.cron`), a routine
runs `/build-autopilot:steward`: `scripts/steward.py sweep` reads every tracked repo and lists, oldest first, red
pull requests (machine failures apart from real ones), stalled checks, conflicts, green PRs nobody is landing,
drafts idle over `steward.idle_hours`, stacked PRs, `fast-builder/wip/*` branches with no PR, broken runners and a
red default branch. The steward then acts on up to `steward.max_resumes_per_sweep` items: re-runs machine failures,
restarts stalled checks, hands red and conflicting PRs back to their lane (the `lane:` label) or the fixer lane,
lands green ones the policy allows, and resumes drafts and leftover branches. It tells the person only about what
needs a person.

## Before you finish

Check rule 2 for every PR you opened, rule 4 if your list is empty, and that every question you raised is in the
build-questions list. Then finish with the autopilot-policy summary.
