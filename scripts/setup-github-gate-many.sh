#!/usr/bin/env bash
# setup-github-gate-many.sh -- run setup-github-gate.sh for several repos in one go.
#
# Asks for the Anthropic API key once, then sets up each repo in turn (repos are grouped by org, so each org's
# app is created and installed once, with every repo of that org picked on the same install page). The only
# moments it needs you are the browser steps for each org's app, all near the start; the rest runs by itself.
# A repo that fails doesn't stop the others; the summary at the end says what to re-run.
#
# Usage:
#   scripts/setup-github-gate-many.sh [--dry-run] [--ruleset] OWNER/REPO [OWNER/REPO ...]
#
#   --dry-run   show what would change in every repo; change nothing
#   --ruleset   also add each repo's branch ruleset (only works once the gate is on that repo's default branch,
#               i.e. after /build-autopilot:apply's pull request is merged; repos without it are skipped with a note)
#
# Output is also saved to ~/.build-autopilot/logs/ (no keys are ever printed).

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ONE="$HERE/setup-github-gate.sh"
STATE_DIR="${BUILD_AUTOPILOT_HOME:-$HOME/.build-autopilot}"

say()  { printf '%s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

cleanup() { stty echo 2>/dev/null </dev/tty || true; }
trap cleanup EXIT
trap 'exit 130' INT TERM

DRY=""
RULESET=""
REPOS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY="--dry-run" ;;
    --ruleset) RULESET="--ruleset" ;;
    -h|--help) usage 0 ;;
    -*) die "unknown option: $1" ;;
    */*) REPOS="$REPOS$1
" ;;
    *) die "give repos as OWNER/REPO: $1" ;;
  esac
  shift
done
[ -n "$REPOS" ] || usage 1
[ -x "$ONE" ] || die "can't find $ONE"

# ---------------------------------------------------------------- preflight
for tool in gh jq python3 openssl curl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed"
done
gh auth status >/dev/null 2>&1 || die "gh is not logged in: run gh auth login"

# Group by owner (case-insensitive), keeping the order given within each owner. Bash 3.2: no associative arrays.
SORTED=$(printf '%s' "$REPOS" | awk -F/ 'NF==2 && !seen[tolower($0)]++ {print tolower($1) "\t" NR "\t" $0}' \
           | sort -t "$(printf '\t')" -k1,1 -k2,2n | cut -f3)
COUNT=$(printf '%s\n' "$SORTED" | grep -c .)

mkdir -p "$STATE_DIR/logs"; chmod 700 "$STATE_DIR"
LOG="$STATE_DIR/logs/setup-$(date +%Y%m%d-%H%M%S).log"
( umask 077; : > "$LOG" )

say "Setting up the Build Autopilot gate for $COUNT repos:"
printf '%s\n' "$SORTED" | sed 's/^/  - /'
[ -n "$DRY" ] && say "DRY RUN: nothing will change."
say "Log: $LOG"
say ""

# ---------------------------------------------------------------- the Anthropic key, once
KEY=""
if [ -z "$DRY" ]; then
  say "The review job needs an Anthropic API key. Best: a key from its own Anthropic workspace with a monthly"
  say "spend limit. It is stored only as a locked GitHub secret in each repo; it isn't shown or saved here."
  say "Press Enter without typing to skip repos that already have one (others will then ask you)."
  printf 'Anthropic API key: '
  read -rs KEY </dev/tty || KEY=""
  printf '\n\n'
  case "$KEY" in
    ""|sk-ant-*) ;;
    *) say "WARNING: that doesn't start with sk-ant-; using it anyway." ;;
  esac
fi

# ---------------------------------------------------------------- each repo
RESULTS=""
i=0
while IFS= read -r repo; do
  [ -n "$repo" ] || continue
  i=$((i + 1))
  owner=$(printf '%s' "$repo" | cut -d/ -f1 | tr 'A-Z' 'a-z')
  # The other repos of this org still to come: named on the install page so all are picked in one visit.
  also=$(printf '%s\n' "$SORTED" | awk -F/ -v o="$owner" -v me="$repo" -v n="$i" \
           'NR > n && tolower($1) == o && $0 != me {printf "%s%s", sep, $0; sep=", "}')
  say "################ [$i/$COUNT] $repo"
  status=0
  # The key reaches the child through its environment only (never a command-line argument).
  ANTHROPIC_API_KEY_FOR_GATE="$KEY" GATE_INSTALL_ALSO="$also" \
    "$ONE" "$repo" --yes $DRY $RULESET 2>&1 | tee -a "$LOG" || status=$?
  if [ "$status" -eq 0 ]; then
    RESULTS="${RESULTS}OK      $repo
"
  else
    RESULTS="${RESULTS}FAILED  $repo   (re-run: $ONE $repo $DRY $RULESET)
"
  fi
  say ""
done <<EOF
$SORTED
EOF
KEY=""

# ---------------------------------------------------------------- summary
say "################ Summary"
printf '%s' "$RESULTS" | tee -a "$LOG"
say ""
if [ -z "$RULESET" ]; then
  say "Next, for each repo: run /build-autopilot:apply in a Claude session there and merge the pull request it opens."
  say "Then add the branch rules for all of them at once:"
  say "  $0 --ruleset $(printf '%s' "$SORTED" | tr '\n' ' ')"
fi
case "$RESULTS" in *FAILED*) exit 1 ;; esac
exit 0
