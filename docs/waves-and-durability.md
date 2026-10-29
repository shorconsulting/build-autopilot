# Waves, review caps and durability: lessons from the Wyset v50 overnight build

Source: the Wyset build of 17 to 19 Oct 2026 (about 125 agents in the main run, 16.5M tokens, ten-plus CI runs). Draft
for David S; nothing here is installed anywhere until `scripts/autopilot-rollout.py --apply --yes` is run on his say-so.

## Built in this change (additive, defaults keep today's behavior)

| Setting | Default | Means |
|---|---|---|
| `waves.batches_per_pr` | 1 | How many batches of work one pull request carries. For a repo that is not live, 4 or more means CI runs once per wave instead of once per piece. |
| `waves.sensitive_batches_per_pr` | 1 | The same limit for batches that touch `sensitive.paths`. |
| `waves.integration_pr` | false | `true`: one integration pull request for everything, CI once, then fix in waves. For undeployed repos only. |
| `review.max_rounds` | 3 | Rounds of independent review per batch before the findings are ranked and handed to the owner. |
| `review.batch_rows` | 8 | Batch size cap. Reviews of larger batches never reached "clean" in practice. |
| `review.unresolved` | `owner-accepts` | `owner-accepts`: findings left after the cap are recorded as accepted gaps (security and access first) and the merge goes ahead once CI is green. `block`: they block the merge. |

`scripts/lanes.py effective` fills these in; `validate` rejects unknown or malformed values. `scripts/autopilot-rollout.py`
adds the two blocks to a repo's settings at their defaults and refreshes the installed gate files, one pull request per repo.

## Practice for sessions (not yet enforced by code)

1. **Merge order for stacked PRs**: serialize merges so each lands on a main that already has the previous one.
2. **Model tiering by risk**: the strongest model for access, billing, export, authority and deletion, both building and checking; a cheaper one for ordinary work.
3. **Test-only fast lane**: a PR that only adds tests gets a targeted run, not the full end-to-end suite.
4. **No repeated local gate**: builders run it once; the integrator merges and regenerates; CI is the verdict. Quick safety pushes to the session's own work branch may skip the local pre-push hook (the owner authorized this); never main.
5. **Questions**: with `questions: decide-reversible`, end the run with one ranked page of every reading and question.
6. **Re-runs**: re-run a CI job once only if it died before any test body ran; otherwise root-cause.
7. **Skip the duplicate CI run on main** after a merge when the PR passed on the identical tree (`tests.skip_duplicate_main`, already built).
8. **Durability**: keep run state on a GitHub ledger branch and the workflow scripts and restore script on GitHub, because containers reset.
9. **Concurrency**: read the engine's real agent limit (`min(16, CPUs-2)`) before sizing a plan.
10. **PR bodies must not overclaim**: one check found 27 requirement IDs listed as tested that no test names.

## Rolling out

```
python3 scripts/autopilot-rollout.py                      # dry run over every repo the token can see
python3 scripts/autopilot-rollout.py --repo O/R           # dry run for one repo
python3 scripts/autopilot-rollout.py --apply --yes        # one pull request per repo that needs changes
```

Never pushes to a default branch. Never changes a repo's `level`, `always_ask`, `questions`, `sensitive`, `phase`,
`pipeline` or `tests`. Each pull request touches `.github/**`, a sensitive path, so the repo's own gate reviews it.
