#!/usr/bin/env bash
# One self-healing capacity pass on an always-on runner box (run every 10 minutes by a systemd timer or cron).
# Config dir (default /etc/hph-ci/steward):
#   <Owner>.token   a fine-grained GitHub token for that org (mode 600), one per org
#   repos           one owner/repo per line (# comments allowed)
#   apply.env       optional: SLOT_REGEX and RESTART (see capacity_apply.py) for this box's own runners,
#                   DRY_RUN=1 to only print what it would do (do one dry pass first)
#                   BOX=hph-ci (this box's name in capacity.host.runners): read this box's own load, memory and
#                   disk each pass, so the check can start one more slot (add-slot, capped by capacity.host.max_slots)
#                   when every slot is busy, jobs are backed up and the box has headroom
# Each org's repos are checked with that org's token; this box's runners are restarted, stale runs cancelled.
set -euo pipefail

CONF="${CAPACITY_CONF:-/etc/hph-ci/steward}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${CAPACITY_STATE:-/var/lib/build-autopilot}"
SLOT_REGEX=""
RESTART=""
DRY_RUN=""
BOX=""
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
hosts_args=""
if [ -n "$BOX" ] && [ -r /proc/meminfo ]; then
  # This box's own reading, in the dashboard's host format (load5, cpus, MemTotal, MemAvailable, disks).
  python3 - "$BOX" > "$STATE/host.json" <<'PY' && hosts_args="--hosts $STATE/host.json"
import json, os, shutil, sys
mem = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo") if l.split()[1:2]}
du = shutil.disk_usage("/")
print(json.dumps([{"host": sys.argv[1], "ok": True, "cpus": os.cpu_count(), "load5": os.getloadavg()[1],
                   "MemTotal": mem["MemTotal"], "MemAvailable": mem["MemAvailable"],
                   "disks": [{"mount": "/", "size": du.total, "used": du.used}]}]))
PY
fi

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
  if GH_TOKEN="$(cat "$tok")" python3 "$HERE/steward.py" capacity $args $hosts_args --out "$out" \
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
  python3 "$HERE/capacity_apply.py" $reports --no-cancel --slot-regex "$SLOT_REGEX" --restart "$RESTART" --host "$BOX" $dry \
    || echo "some restarts failed (see above)"
fi
