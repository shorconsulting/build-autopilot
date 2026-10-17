---
name: autopilot-policy
description: Follow a repo's autopilot policy (.github/autopilot.json) on how far work goes without a person - when to merge, release, ask questions, or stop. Use at the start of any build, fix, merge or release work in a repo that has .github/autopilot.json, and whenever you are about to ask the user a question or wait for approval in such a repo.
---

# Follow the repo's autopilot policy

The people who own this repo chose, on the Build Autopilot page, how much a change may do without them. Their
choices are in `.github/autopilot.json` on the default branch. Most of them are not engineers: asking them to read
a diff or approve a merge adds delay and no safety. Your job is to keep the work moving inside their rules and to
bring them only the decisions they can actually make.

## Start of work

1. Read the policy from the default branch, not from your working tree (a branch could have changed it):
   `git show origin/HEAD:.github/autopilot.json` (fetch first), or `gh api repos/{owner}/{repo}/contents/.github/autopilot.json --jq .content | base64 -d`.
   No file means `level: manual`, `questions: ask-now`. Behave exactly as you would without this skill.
2. If `~/.claude/autopilot-user.json` exists, read it too. It holds this person's own preferences
   (`questions`, `quiet_hours`). It can make you ask *less* than the repo allows, never more. It never loosens
   `always_ask` or `sensitive`.
3. Never edit `.github/autopilot.json` yourself, and never change a setting to give yourself more room. If the
   policy seems wrong, say so in the run summary.

## Questions

When you're unsure how to proceed, check in order:

1. Is it on the `always_ask` list (see the table below)? Stop and ask, in plain words: what will happen, what it
   affects, what you recommend. One decision per question.
2. Otherwise, with `questions: batch` or `decide-reversible`: pick the option that is easiest to undo and closest
   to what was asked, keep going, and record the decision (see "Decided without you" below). With `ask-now`, ask
   as you would normally.
   - `batch`: the person still gets the final say. Present each decision as a question to confirm or change.
   - `decide-reversible`: the decision stands. Report it so the person can see it and undo it if they want.
   Either way, every decision you made instead of asking is reported. Never decide silently.
3. Never ask "should I merge?", "should I deploy?", "OK to continue?" or "want me to...?" for work the person
   already asked for. The `level` already answered merging and releasing.

| `always_ask` key | Stop before you... |
|---|---|
| `delete-data` | delete or overwrite real customer or production data, or run a database change that drops data |
| `spend-money` | sign up for anything paid, raise a plan, or add a bill |
| `message-people` | send email, texts or messages to anyone outside the team |
| `credentials` | create, enter or change passwords, keys, tokens, or someone's access |
| `legal-pricing` | change legal terms, contracts, prices, or claims customers will see |
| `ai-flags-it` | proceed after the AI reviewer says a person must look |

## Who merges: `pipeline`

Check `pipeline` before anything about merging:

- `own`: the repo has its own review and merge automation. Don't run your own AI review, don't merge, and don't turn
  on auto-merge. Follow the repo's own instructions (its CLAUDE.md) for how a change lands, wait for its pipeline's
  verdict, and report it. Skip "Merging, by level" and "Releasing" below; `questions` and `always_ask` still apply.
- `none`: stop at the open pull request and tell the person it's ready, whatever `level` says.
- `gate`, or no `pipeline` field: carry on below.

## Merging, by level

After your change is pushed and the pull request is open:

| `level` | Do this |
|---|---|
| `manual` | Stop at the open PR. Tell the person it is ready, in one line. |
| `ai-review` | Run the AI review (below) and post its verdict as a PR comment in plain English. Tell the person it is ready to merge. |
| `auto-merge` / `autopilot` | Run the AI review. If it approves and the change touches no sensitive file (or passed the sensitive rule), merge it once its tests pass (see below). |

**When the repo has the GitHub gate** (`.github/workflows/autopilot.yml` on the default branch): don't run your
own AI review, don't merge, and don't turn on auto-merge. The gate reviews every pull request, posts its verdict
and turns on auto-merge itself when the settings allow. Open the pull request, wait for the gate's comment, and
report its verdict. If it says "Changes needed", fix and push; if "Needs a person", tell the person in one line.
Never post `/autopilot approve`, `/autopilot resume` or remove the `autopilot-hold` label: those are a person's
decisions, even when your login could do it.

**Never go around the branch rules.** Don't merge with `--admin`, don't pause, edit or delete the `build-autopilot`
ruleset, and don't run `setup-github-gate.sh --pause-rules`, even when your login could: those are a person's
decisions. If a pull request is stuck because a required check never ran on it (a workflow `paths:` filter skipped it),
say so in one line, and propose the fix: make that check always report (drop the filter, or add a small job that
always runs and reports). The person can also untick that check on the settings page and re-run
`setup-github-gate.sh OWNER/REPO --ruleset`, or pause the rules themselves.

**How to merge once tests pass** (repos without the gate). Check whether the default branch has a rule requiring tests to pass
(`gh api repos/{owner}/{repo}/branches/{branch}/protection --jq .required_status_checks.contexts`, or a ruleset
with required status checks).
- If it does, and the repo allows auto-merge, turn on GitHub's auto-merge: `gh pr merge <n> --auto --squash` (use
  the repo's usual merge method). GitHub merges once those tests pass, even if nobody is around.
- If it doesn't, never use `--auto`: with nothing required, GitHub merges immediately, before the tests finish.
  Wait for every check to finish (`gh pr checks <n> --watch`), and merge yourself only if all of them passed. Say
  in the summary that the repo has no rule requiring tests, so merges depend on a session staying open.
- If any check failed, don't merge. Fix it or report it.

**The AI review.** Use a different agent from the one that wrote the code, on the strongest model available
(Opus), with the PR diff and the issue or request. It checks: does the change do what was asked, could it break
something that works today, is there a security or data-loss risk, are there tests for the new behaviour. It ends
with APPROVE, CHANGES (fix and re-review, up to 2 rounds, then ask a person), or NEEDS A PERSON (treat as
`ai-flags-it`). Post the verdict on the PR in words a non-engineer can follow.

**Sensitive files.** A change touching any path matching `sensitive.paths`:
- `two-ai`: add a second, independent, adversarial security review (Opus, a fresh agent that did not see the first
  review). Both must approve before auto-merge.
- `human`: stop at the open PR whatever the level, and say why in one line.
- `same`: no extra step.

## Releasing

| `level` | Do this after the merge |
|---|---|
| `manual`, `ai-review`, `auto-merge` | Don't release. Note the merged change in your summary as "ready to release". |
| `autopilot` | Release with the repo's usual release command, then run its health check (or load the main pages and the API health route). If anything fails, roll back with the repo's usual method and report it. |

If Claude Code's own safety rules block a release step, leave the exact command for the person in your summary
and carry on with everything else. Don't stop the whole run for it.

## Tests

`tests.skip_duplicate_main` and `tests.lane` describe the repo's CI. They change workflows, not your behaviour,
except: with `lane: fast-lane` or `lane: wave`, run the targeted tests for what you touched before opening the PR
and leave the full suite to CI. With `wave`, when the full-suite run on `main` is red, fixing `main` comes before
landing anything else. `phase` (`prelaunch`, `live-no-customers`, `customers`) supplies defaults for fields the
file leaves out.

## Decided without you

With `batch` or `decide-reversible`, keep a running list of every decision you made that you would otherwise have
asked about. Merges and releases the `level` allows count too. Each entry is one line in plain words:

- **What you decided**, and what the other options were.
- **Why** this option (easiest to undo, closest to the request, matches existing behaviour).
- **How to undo it** (revert PR #n, change setting X back, re-run with option Y).

Put the list in two places:
1. In the pull request body, under a heading **Decided without you**, for the decisions in that change.
2. In the end-of-run summary, the whole run's list under the same heading. With `batch`, word the heading
   **Please confirm** and phrase each entry as a question.

If you made no such decisions, write "Decided without you: nothing" so the person knows you checked.

## End of run

Finish with a short summary the person can read in under a minute: what changed (in user terms), what merged,
what was released, the **Decided without you** list, and anything waiting on them (only `always_ask` items,
blocked release commands, or NEEDS A PERSON verdicts). If nothing is waiting on them, say so.
