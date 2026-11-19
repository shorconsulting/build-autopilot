#!/usr/bin/env bash
# One self-healing capacity pass on an always-on runner box (run every 10 minutes by a systemd timer or cron).
# Config dir (default /etc/hph-ci/steward):
#   <Owner>.token   a fine-grained GitHub token for that org (mode 600), one per org
#   repos           one owner/repo per line (# comments allowed)
#   apply.env       optional: SLOT_REGEX and RESTART (see capacity_apply.py) for this box's own runners
# Each org's repos are checked with that org's token; this box's runners are restarted, stale runs cancelled.
set -euo pipefail

CONF="${CAPACITY_CONF:-/etc/hph-ci/steward}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${CAPACITY_STATE:-/var/lib/build-autopilot}"
SLOT_REGEX=""
RESTART=""
if [ -f "$CONF/apply.env" ]; then
  # shellcheck disable=SC1091
  . "$CONF/apply.env"
fi
mkdir -p -m 700 "$STATE"

owners="$(grep -v '^[[:space:]]*#' "$CONF/repos" | grep '/' | cut -d/ -f1 | sort -u)"
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
    GH_TOKEN="$(cat "$tok")" python3 "$HERE/capacity_apply.py" "$out" \
      --slot-regex "$SLOT_REGEX" --restart "$RESTART" || echo "$owner: some actions failed (see above)"
  else
    echo "$owner: capacity check failed"
  fi
done
