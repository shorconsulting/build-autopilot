# Installing the v2 gate in a repo

This is the procedure `/autopilot apply` follows to put the GitHub side of a repo's autopilot settings in place. Steps
marked **(admin)** change repo or org settings and need a person with admin rights; `/autopilot apply` prints the exact
command for those and does not run them itself. Everything else arrives in the repo through one pull request that a
person merges.

What the gate does, and why it is built this way: [v2-report.md](v2-report.md). The settings it enforces:
[policy.md](policy.md).

## What gets installed

| File in the repo | From this plugin | What it is |
|---|---|---|
| `.github/workflows/autopilot.yml` | `templates/github/workflows/autopilot.yml` | The gate: mark, review, decide, and the `/autopilot approve` / `stop` / `resume` comment commands |
| `.github/autopilot/gate.py` | `templates/github/autopilot/gate.py` | The gate's logic (Python 3, standard library plus the Claude SDK in the review job) |
| `.github/autopilot/requirements.txt` | `templates/github/autopilot/requirements.txt` | The Claude SDK and its dependencies, exact versions and hashes |
| `.github/autopilot/skip_duplicate_main.py` | `templates/github/autopilot/skip_duplicate_main.py` | Only used if `tests.skip_duplicate_main` is on |
| `.github/autopilot.json` | the settings page | The settings (v1 already writes this) |

Copy the files byte for byte. Don't edit the workflow per repo except the two places marked below (required check
names live in the ruleset, not the workflow).

## The quick way: one script (admin)

`scripts/setup-github-gate.sh` does the admin steps below for one repo: it creates the org's app (your browser opens
once; you click "Create GitHub App"), installs it on the repo, locks the two environments, stores the secrets and
variables (you type the Anthropic key; it isn't shown), sets the repo settings and, with `--ruleset`, the branch
ruleset. Re-running is safe; finished steps are skipped. Try `--dry-run` first: it changes nothing.

```bash
scripts/setup-github-gate.sh Velocity-Org/chasing-shade --dry-run
```

```bash
scripts/setup-github-gate.sh Velocity-Org/chasing-shade --ruleset
```

The app's key stays on the computer that created it (`~/.build-autopilot/keys/<org>.pem`, readable only by you); the
next repo in the same org reuses it. Then run `/autopilot apply` for the install pull request (step 2) and do the
checks in step 5. The manual steps follow, for reference.

## Once per GitHub organization: the Build Autopilot app (admin)

The gate acts as a dedicated GitHub App, not as the workflow's built-in token, for three reasons: a merge made with the
built-in token doesn't start the repo's other workflows (so nothing would deploy after an auto-merge); the required
`autopilot/gate` check can be tied to the app so no other automation can fake it; and its key can be locked to the
default branch.

1. Organization settings → Developer settings → GitHub Apps → New GitHub App.
   - Name: `Build Autopilot (<org>)`. Homepage: any URL. Webhook: **off**.
   - Repository permissions: Contents **read and write**, Pull requests **read and write**, Checks **read and
     write**, Issues **read and write**, Metadata read. Nothing else. (Checks, not commit statuses: the gate's result
     is a check run, which only this app can write or change.)
   - Where can it be installed: only this account.
2. Note the **App ID** (a number) and the **Client ID**. Generate a **private key** (a `.pem` file).
3. Install the app on the repos that will use the gate (not "all repositories").

One app per org means one key in every repo's `autopilot` environment: a repo whose environment or default branch is
weaker than the rest can mint tokens for the others. Where repos differ in trust (a client's repo beside internal
ones), give each group its own app.

Keep the `.pem` out of every repo and chat. It goes only into the environment secret below.

## Per repo

### 1. Two locked environments (admin)

| Environment | Holds | Used by |
|---|---|---|
| `autopilot-review` | secret `ANTHROPIC_API_KEY` | the review job (talks to Claude, can't write to GitHub) |
| `autopilot` | secret `AUTOPILOT_APP_PRIVATE_KEY`, variables `AUTOPILOT_APP_ID`, `AUTOPILOT_APP_CLIENT_ID` | the decide and approve jobs |

Both must allow **only the default branch** to deploy. That lock is what stops a workflow added by a pull request from
reading the secrets: pull-request workflows run on `refs/pull/N/merge`, while the gate's `pull_request_target` and
`issue_comment` runs count as the default branch. Don't put these secrets at repo or org level.

```bash
gh api -X PUT repos/OWNER/REPO/environments/autopilot-review --input - <<'EOF'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
EOF
```

```bash
gh api -X POST repos/OWNER/REPO/environments/autopilot-review/deployment-branch-policies -f name=main -f type=branch
```

Repeat both for `autopilot`, using the repo's default branch name in place of `main`. Then:

```bash
gh secret set ANTHROPIC_API_KEY --env autopilot-review --repo OWNER/REPO
```

```bash
gh secret set AUTOPILOT_APP_PRIVATE_KEY --env autopilot --repo OWNER/REPO < build-autopilot.private-key.pem
```

```bash
gh variable set AUTOPILOT_APP_ID --env autopilot --repo OWNER/REPO --body 1234567
```

```bash
gh variable set AUTOPILOT_APP_CLIENT_ID --env autopilot --repo OWNER/REPO --body Iv23liXXXXXXXXXXXX
```

### 2. The install pull request

One pull request adds the files above (and `.github/autopilot.json` if it isn't there yet). A person reviews and
merges it. It is never auto-merged: on a fresh repo there is no gate yet, and once the gate exists it treats any change
to these files as "needs a person".

### 3. Repo settings (admin)

- Allow auto-merge: on (`gh api -X PATCH repos/OWNER/REPO -F allow_auto_merge=true`).
- Settings → Actions → General → Workflow permissions: **Read repository contents**; "Allow GitHub Actions to create
  and approve pull requests": **off**.

### 4. The branch ruleset, only for level `auto-merge` or `autopilot` (admin)

Without this ruleset the gate still reviews and comments, but it will not turn on auto-merge (it checks the rules first
and says what is missing in its comment). With level `manual` or `ai-review`, skip this step.

The ruleset requires a pull request, the gate's check run `autopilot/gate` **from the app** (`integration_id` = the
App ID), and the repo's test checks. Other apps and workflows *can* create a check run named `autopilot/gate` on any
commit; it is the ruleset's `integration_id` pin that makes theirs not count. None of them can change the app's own
run: a check run can only be updated by the app that created it. List every test job a change must pass; those are the names shown on a pull request's checks. For
Velocity-Org/chasing-shade they are `API · unit + integration + CRM + platform` and `3D-asset-tools · pytest`, from
GitHub Actions (integration 15368).

Know the limit of that pin: every workflow in the repo reports as GitHub Actions. Anyone who can push can add a
workflow on some branch (or in a pull request) that asks for `checks: write` and posts a passing check under those
names on any commit, including another pull request's head. The gate's own check can't be faked this way (it is
pinned to the app); the test checks can. Options, cheapest first: accept it as residual risk (the default, since
whoever can push can already run code in CI); add a ruleset that stops non-admins from pushing branches that
change `.github/workflows/` (rulesets' file-path restrictions); or have the tests report through a dedicated app whose
key lives in a default-branch-only environment, and pin the required test checks to that app.

```bash
gh api -X POST repos/OWNER/REPO/rulesets --input - <<'EOF'
{
  "name": "build-autopilot",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false, "require_last_push_approval": false,
      "required_review_thread_resolution": false}},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": false,
      "required_status_checks": [
        {"context": "autopilot/gate", "integration_id": 1234567},
        {"context": "API · unit + integration + CRM + platform", "integration_id": 15368},
        {"context": "3D-asset-tools · pytest", "integration_id": 15368}]}}
  ]
}
EOF
```

Replace `1234567` with the App ID and the test names with the repo's own. Keep `bypass_actors` empty unless David
decides otherwise (see the report): anyone on that list, including a Claude session using their login, can merge
around the gate.

### 5. Check it works

1. A pull request that only edits a README: within a few minutes, a "Build Autopilot: Approved" comment, a green
   `autopilot/gate` check, and (at `auto-merge`) "Auto-merge is on"; it merges when the tests pass.
2. A pull request that edits anything under `.github/workflows/`: "Needs a person", red `autopilot/gate`, no
   auto-merge. As an admin who didn't open it or write its commits, read the review (the comment, or the Details link
   on the `autopilot/gate` check, which nobody can edit), then comment `/autopilot approve <the full 40-character
   commit id it names>`: the check turns green; a person merges.
3. Comment `/autopilot stop` on an approved pull request: auto-merge goes off, the check turns red ("On hold"), the
   `autopilot-hold` label appears. A builder removing the label doesn't lift the hold (the gate puts it back); an
   admin's `/autopilot resume` does, and the review runs again. Every gate comment ends with a "Decided without you" section
   (or "Decided without you: nothing").
4. Spoof test: a pull request that adds a workflow creating an `autopilot/gate` check run (conclusion success) with
   the built-in token, and posting an `autopilot/gate` commit status too. The pull request must stay blocked (the
   ruleset only accepts the check run from the app), and the gate must answer "Needs a person" because it changes a
   workflow. Close it unmerged.

### 6. Optional: `tests.skip_duplicate_main`

Add the `run-name` line, the `dedupe` job and the two `needs`/`if` lines to the repo's test workflow, as in
`drafts/chasing-shade/test.yml.patch` (the run name records which commit a pull request's tests actually ran on), and set `"tests": {"skip_duplicate_main": true}` on the settings page. After a
merge, the tests on the default branch are skipped only when the pull request already passed the same workflow on
exactly the same files; any doubt runs them. Each skip is reported under "Decided without you" in the run summary and
as a comment on the merged pull request, with how to run the tests anyway.

## Updating the gate

Re-run `/autopilot apply`: it opens one pull request with the new template files, which a person merges (the gate
marks it "needs a person", as it does for any change to itself).

To move to a newer Claude SDK, regenerate the hash-pinned requirements on any machine with Python 3.12:

```bash
python3 -m venv /tmp/ap && /tmp/ap/bin/pip install -q pip-tools && /tmp/ap/bin/pip-compile --generate-hashes --allow-unsafe -o requirements.txt requirements.in
```

with `requirements.in` containing the single line `anthropic==<version>` (pick a release at least two weeks old), then
run `python3 -m unittest scripts/test_gate.py` here and ship it through the update pull request.

## Removing it

Delete the ruleset (admin), then a pull request removing the workflow and `.github/autopilot/`. Delete the two
environments.
