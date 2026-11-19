#!/usr/bin/env bash
# One self-healing capacity pass on an always-on runner box (run every 10 minutes by a systemd timer or cron).
# Config dir (default /etc/hph-ci/steward):
#   <Owner>.token   a fine-grained GitHub token for that org (mode 600), one per org
#   repos           one owner/repo per line (# comments allowed)
#   apply.env       optional: SLOT_REGEX and RESTART (see capacity_apply.py) for this box's own runners,
#                   DRY_RUN=1 to only print what it would do (do one dry pass first)
# Each org's repos are checked with that org's token; this box's runners are restarted, stale runs cancelled.
set -euo pipefail

CONF="${CAPACITY_CONF:-/etc/hph-ci/steward}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${CAPACITY_STATE:-/var/lib/build-autopilot}"
SLOT_REGEX=""
RESTART=""
DRY_RUN=""
if [ -f "$CONF/apply.env" ]; then
  # shellcheck disable=SC1091
  . "$CONF/apply.env"
fi
mkdir -p -m 700 "$STATE"
dry=""
if [ -n "$DRY_RUN" ]; then
  dry="--dry-run"
fi
reports=""

owners="$(grep -v '^[[:space:]]*#' "$CONF/repos" | grep '/' | cut -d/ -f1 | sort -u || true)"
if [ -z "$owners" ]; then
  echo "no owner/repo lines in $CONF/repos: nothing to check"
  exit 0
fi
for owner in $owners; do
  tok="$CONF/$owner.token"
  if [ ! -s "$tok" ]; then
    echo "skip $owner: no token at $tok"
    continue
  fi
  args=""
  for r in $(grep -v '^[[:space:]]*#' "$CONF/repos" | grep "^$owner/"); do
    args="$args --repo $r"
  done
  out="$STATE/capacity-$owner.json"
  # shellcheck disable=SC2086
  if GH_TOKEN="$(cat "$tok")" python3 "$HERE/steward.py" capacity $args --out "$out" \
      --state "$STATE/capacity-state-$owner.json"; then
    # Cancels with this org's token; restarts once per pass over every org's report, below.
    GH_TOKEN="$(cat "$tok")" python3 "$HERE/capacity_apply.py" "$out" --no-restart $dry \
      || echo "$owner: some cancels failed (see above)"
    reports="$reports $out"
  else
    echo "$owner: capacity check failed"
  fi
done
if [ -n "$reports" ] && [ -n "$RESTART" ]; then
  # shellcheck disable=SC2086
  python3 "$HERE/capacity_apply.py" $reports --no-cancel --slot-regex "$SLOT_REGEX" --restart "$RESTART" $dry \
    || echo "some restarts failed (see above)"
fi
