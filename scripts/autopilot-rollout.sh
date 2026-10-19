#!/usr/bin/env bash
# autopilot-rollout.sh -- roll the current Build Autopilot templates out to every repo where autopilot is installed.
#
# "Installed" means the repo's default branch has .github/autopilot.json. The repos searched are the ones gh can see
# under your own login and every org you belong to (archived repos are skipped).
#
# For each such repo this prepares ONE pull request that:
#   - updates the installed gate files that differ from this plugin's templates:
#       .github/workflows/autopilot.yml, .github/autopilot/gate.py,
#       .github/autopilot/requirements.txt, .github/autopilot/skip_duplicate_main.py
#     (only files the repo already has, and only when its pipeline is "gate"; never installs the gate where it is
#     not installed);
#   - adds the new autopilot.json keys, WITH DEFAULTS ONLY (templates/github/new_key_defaults.json). Every value
#     already in the file (level, always_ask, questions, sensitive, phase, ...) is left exactly as it is.
#
# Usage:
#   scripts/autopilot-rollout.sh [--dry-run]            print what would be done (the default; changes nothing)
#   scripts/autopilot-rollout.sh --apply                open or update the pull requests
#   scripts/autopilot-rollout.sh --repo OWNER/REPO ...  limit to these repos (repeatable)
#   scripts/autopilot-rollout.sh --help
#
# Safe by construction: nothing is changed without --apply; a default branch is never pushed to (changes go on the
# branch autopilot-rollout-<plugin version> and a pull request); re-running reuses that branch and its open pull
# request and only adds commits for files that still differ. Nothing is merged: the repo's own gate reviews the
# pull request (.github/** is a sensitive path there).
#
# Needs: gh (logged in), jq, base64, diff. bash 3.2 compatible.

set -euo pipefail
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

APPLY=0
ONLY=""
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TPL="$ROOT/templates/github"
DEFAULTS="$TPL/new_key_defaults.json"
GATE_FILES=".github/workflows/autopilot.yml:workflows/autopilot.yml
.github/autopilot/gate.py:autopilot/gate.py
.github/autopilot/requirements.txt:autopilot/requirements.txt
.github/autopilot/skip_duplicate_main.py:autopilot/skip_duplicate_main.py"
POLICY=".github/autopilot.json"

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }
usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) APPLY=0 ;;
    --apply) APPLY=1 ;;
    --repo) shift; [ $# -gt 0 ] || die "--repo needs OWNER/REPO"; ONLY="$ONLY $1 " ;;
    -h|--help) usage 0 ;;
    *) log "unknown option: $1"; usage 1 ;;
  esac
  shift
done

# ---- preflight
command -v gh >/dev/null 2>&1 || die "gh is not installed (https://cli.github.com)"
command -v jq >/dev/null 2>&1 || die "jq is not installed (brew install jq)"
command -v base64 >/dev/null 2>&1 || die "base64 is not installed"
gh auth status >/dev/null 2>&1 || die "gh is not logged in; run: gh auth login"
[ -f "$DEFAULTS" ] || die "missing $DEFAULTS (run this from a build-autopilot checkout)"
for pair in $(printf '%s\n' "$GATE_FILES" | sed 's/.*://'); do
  [ -f "$TPL/$pair" ] || die "missing template $TPL/$pair"
done
VERSION="$(jq -r '.version' "$ROOT/.claude-plugin/plugin.json")"
[ -n "$VERSION" ] && [ "$VERSION" != "null" ] || die "cannot read the plugin version"
BRANCH="autopilot-rollout-$VERSION"

if [ "$APPLY" -eq 1 ]; then log "mode: APPLY (will open or update pull requests)"; else log "mode: DRY RUN (nothing will be changed; use --apply to change)"; fi
log "plugin version $VERSION, branch $BRANCH"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/autopilot-rollout.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

# raw file from a repo at a ref -> stdout; returns 1 when absent
remote_file() { # repo ref path
  gh api -H "Accept: application/vnd.github.raw" "repos/$1/contents/$3?ref=$2" 2>/dev/null
}
remote_sha() { # repo ref path
  gh api "repos/$1/contents/$3?ref=$2" --jq .sha 2>/dev/null
}

# add missing keys (recursively) from the defaults; existing values are never touched
FILL='def fill($d): . as $o | reduce ($d | keys_unsorted[]) as $k ($o;
        if has($k) | not then .[$k] = $d[$k]
        elif (.[$k] | type) == "object" and ($d[$k] | type) == "object" then .[$k] |= fill($d[$k])
        else . end);
      fill($d[0])'

# ---- discover repos
list_repos() {
  local me owners o
  me="$(gh api user --jq .login)"
  owners="$me $(gh api user/orgs --paginate --jq '.[].login' 2>/dev/null | tr '\n' ' ')"
  for o in $owners; do
    gh repo list "$o" --limit 1000 --json nameWithOwner,isArchived,defaultBranchRef \
      --jq '.[] | select(.isArchived | not) | select(.defaultBranchRef != null) | .nameWithOwner + " " + .defaultBranchRef.name' \
      2>/dev/null || log "warning: could not list repos for $o"
  done | sort -u
}

body_for() { # repo changedlist -> pr body file
  local out="$TMP/pr-body.md"
  cat > "$out" <<'BODY_EOF'
Rolls out the current Build Autopilot templates (the v50 learnings).

What this changes:
BODY_EOF
  printf '%s\n' "$2" >> "$out"
  cat >> "$out" <<'BODY_EOF'

What it does not change: this repo's chosen settings. New `autopilot.json` keys are added with defaults only;
`level`, `always_ask`, `questions`, `sensitive`, `phase` and every other value already in the file are untouched.

`.github/**` is a sensitive path, so this repo's own autopilot gate reviews this pull request. It is not merged
automatically by the rollout script.
BODY_EOF
  printf '\nOpened by scripts/autopilot-rollout.sh from build-autopilot %s.\n' "$VERSION" >> "$out"
  printf '%s\n' "$out"
}

put_file() { # repo branch path localfile message
  local sha args
  sha="$(remote_sha "$1" "$2" "$3" || true)"
  if [ -n "$sha" ]; then
    gh api -X PUT "repos/$1/contents/$3" -f message="$5" -f branch="$2" -f sha="$sha" \
      -f content="$(base64 < "$4" | tr -d '\n')" >/dev/null
  else
    gh api -X PUT "repos/$1/contents/$3" -f message="$5" -f branch="$2" \
      -f content="$(base64 < "$4" | tr -d '\n')" >/dev/null
  fi
}

N_FOUND=0; N_CHANGE=0; N_CURRENT=0; N_SKIP=0

process_repo() { # repo default_branch
  local repo="$1" db="$2" w="$TMP/work" raw pipeline newjson pair path tpl dest cur changed
  rm -rf "$w"; mkdir -p "$w"
  raw="$w/policy.json"
  remote_file "$repo" "$db" "$POLICY" > "$raw" || return 0   # no settings file: autopilot is not installed here
  N_FOUND=$((N_FOUND + 1))
  if ! jq -e 'type == "object"' "$raw" >/dev/null 2>&1; then
    log "SKIP  $repo: $POLICY is not a JSON object"; N_SKIP=$((N_SKIP + 1)); return 0
  fi
  pipeline="$(jq -r '.pipeline // "gate"' "$raw")"
  changed=""

  # 1. new keys with defaults only
  jq --slurpfile d "$DEFAULTS" "$FILL" "$raw" > "$w/policy.new.json"
  if [ "$(jq -S . "$raw")" != "$(jq -S . "$w/policy.new.json")" ]; then
    changed="$changed$POLICY|$w/policy.new.json
"
  fi

  # 2. gate files, only where this repo has the gate installed
  if [ "$pipeline" = "gate" ]; then
    for pair in $(printf '%s\n' "$GATE_FILES" ); do
      path="$(printf '%s' "$pair" | sed 's/:.*//')"
      tpl="$TPL/$(printf '%s' "$pair" | sed 's/.*://')"
      cur="$w/cur.$(printf '%s' "$path" | tr '/' '_')"
      if remote_file "$repo" "$db" "$path" > "$cur"; then
        if ! cmp -s "$cur" "$tpl"; then changed="$changed$path|$tpl
"; fi
      fi
    done
  fi

  if [ -z "$changed" ]; then
    log "OK    $repo ($db): already current"; N_CURRENT=$((N_CURRENT + 1)); return 0
  fi
  N_CHANGE=$((N_CHANGE + 1))
  local summary="" line existing="" pr_url=""
  existing="$(gh pr list --repo "$repo" --head "$BRANCH" --state open --json url --jq '.[0].url // empty' 2>/dev/null || true)"
  printf '%s' "$changed" > "$w/changed.list"
  while IFS='|' read -r path dest; do
    [ -n "$path" ] || continue
    if [ "$path" = "$POLICY" ]; then
      line="- \`$POLICY\`: new keys added with defaults: $(jq -rn --slurpfile a "$raw" --slurpfile b "$dest" '[$b[0] | paths | select(length <= 2)] as $pb | [$a[0] | paths | select(length <= 2)] as $pa | ($pb - $pa) as $new | ([$new[] | select(length == 1) | .[0]]) as $tops | [$new[] | select(length == 1 or ((.[0] | IN($tops[])) | not)) | map(tostring) | join(".")] | join(", ")')"
    else
      if remote_file "$repo" "$db" "$path" > "$w/old" 2>/dev/null; then
        line="- \`$path\`: updated ($(diff "$w/old" "$dest" | grep -c '^[<>]' || true) changed lines)"
      else
        line="- \`$path\`: updated"
      fi
    fi
    summary="$summary$line
"
  done < "$w/changed.list"

  if [ "$APPLY" -ne 1 ]; then
    log "PLAN  $repo ($db): one pull request from $BRANCH"
    printf '%s' "$summary" | sed 's/^/        /' | sed 's/\\`/`/g' >&2
    if [ -n "$existing" ]; then log "        an open rollout pull request already exists and would be reused: $existing"; fi
    return 0
  fi

  # ---- apply: never touches the default branch
  [ "$BRANCH" != "$db" ] || die "refusing to write to the default branch of $repo"
  if ! gh api "repos/$repo/git/ref/heads/$BRANCH" >/dev/null 2>&1; then
    gh api -X POST "repos/$repo/git/refs" -f ref="refs/heads/$BRANCH" \
      -f sha="$(gh api "repos/$repo/git/ref/heads/$db" --jq .object.sha)" >/dev/null
  fi
  while IFS='|' read -r path dest; do
    [ -n "$path" ] || continue
    if remote_file "$repo" "$BRANCH" "$path" > "$w/onbranch" 2>/dev/null && cmp -s "$w/onbranch" "$dest"; then
      continue   # already on the rollout branch
    fi
    put_file "$repo" "$BRANCH" "$path" "$dest" "Autopilot rollout $VERSION: $path"
  done < "$w/changed.list"
  if [ -n "$existing" ]; then
    log "DONE  $repo: updated the existing pull request $existing"
  else
    pr_url="$(gh pr create --repo "$repo" --base "$db" --head "$BRANCH" \
      --title "Build Autopilot rollout $VERSION" --body-file "$(body_for "$repo" "$summary")")"
    log "DONE  $repo: opened $pr_url"
  fi
}

REPOS="$(list_repos)"
[ -n "$REPOS" ] || die "gh can see no repos"
while read -r repo db; do
  [ -n "$repo" ] || continue
  if [ -n "$ONLY" ]; then
    case "$ONLY" in *" $repo "*) ;; *) continue ;; esac
  fi
  process_repo "$repo" "$db"
done <<EOF2
$REPOS
EOF2

log ""
log "autopilot installed in $N_FOUND repo(s): $N_CHANGE need an update, $N_CURRENT current, $N_SKIP skipped"
if [ "$APPLY" -ne 1 ] && [ "$N_CHANGE" -gt 0 ]; then log "dry run only. Nothing was changed. Re-run with --apply to open the pull requests."; fi
