#!/usr/bin/env bash
# One self-healing capacity pass on an always-on runner box (run every 10 minutes by a systemd timer or cron).
# Config dir (default /etc/hph-ci/steward):
#   <Owner>.app     preferred: a GitHub App installed on that org, as two lines APP_ID=<id> and KEY=<path to its
#                   private key .pem> (key mode 600). The app has its own API limit, so the timer never spends a
#                   person's. Needs Actions read/write, Contents and Pull requests read, and the org permission
#                   Self-hosted runners read/write.
#   <Owner>.token   otherwise: a fine-grained GitHub token for that org (mode 600). A person's tokens all share
#                   that person's 5,000-calls-an-hour limit.
#   repos           one owner/repo per line (# comments allowed)
#   apply.env       optional: SLOT_REGEX and RESTART (see capacity_apply.py) for this box's own runners, and STOP
#                   (the stop command for a paused slot; default: RESTART with --stop when it is restart-runner-slot),
#                   DRY_RUN=1 to only print what it would do (do one dry pass first),
#                   SWEEP_EVERY_MIN (default 30; 0 = off): how often to also re-run checks that failed before the
#                   code ran and cancel hung ones (steward.py sweep + sweep_apply.py, plain code, no AI);
#                   SWEEP_MAX_PRS (default 40, most recently updated first) and SWEEP_MIN_RATE (default 2500: skip the
#                   sweep when fewer API calls than that are left this hour, since a token's limit is its owner's)
#                   BOX=hph-ci (this box's name in capacity.host.runners): read this box's own load, memory and
#                   disk each pass, so the check can start one more slot (add-slot, capped by capacity.host.max_slots)
#                   when every slot is busy, jobs are backed up and the box has headroom; and stop one idle slot when
#                   the box is swapping (pause-slot), starting it again once swapping stops (resume-slot). Needs the
#                   restart helper from this commit or later (it takes --stop).
#                   PRIORITY_LABEL (default ci:priority; empty = off): an open PR with this label goes to the
#                   front of the line. Every run of that org's repos queued before its unfinished runs is cancelled
#                   and re-run, so it queues behind them (priority_apply.py; re-runs owed survive in $STATE).
# Each org's repos are checked with that org's token; this box's runners are restarted, stale runs cancelled.
set -euo pipefail

CONF="${CAPACITY_CONF:-/etc/hph-ci/steward}"
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="${CAPACITY_STATE:-/var/lib/build-autopilot}"
SLOT_REGEX=""
RESTART=""
STOP=""
DRY_RUN=""
SWEEP_EVERY_MIN=30
SWEEP_MAX_PRS=40
SWEEP_MIN_RATE=2500
BOX=""
PRIORITY_LABEL="ci:priority"
if [ -f "$CONF/apply.env" ]; then
  # shellcheck disable=SC1091
  . "$CONF/apply.env"
fi
# The stop command for pause-slot: STOP in apply.env, or, when RESTART is the restart-runner-slot helper (which
# takes --stop), the same helper with --stop. Any other RESTART and no STOP: slots are never paused.
if [ -z "$STOP" ]; then
  case "$RESTART" in
    *restart-runner-slot\ \{slot\}) STOP="${RESTART% \{slot\}} --stop {slot}" ;;
  esac
fi
mkdir -p -m 700 "$STATE"

# The token for one org: minted from its app when there is one, else its token file. Empty when neither works.
token_for() {
  local app="$CONF/$1.app" APP_ID="" KEY=""
  local minted=""
  if [ -f "$app" ]; then
    APP_ID="$(sed -n 's/^APP_ID=//p' "$app")"
    KEY="$(sed -n 's/^KEY=//p' "$app")"
    # Its errors go to the journal (stderr); the token alone goes to stdout.
    if minted="$("$HERE/app-token.sh" "$APP_ID" "$KEY" "$1")" && [ -n "$minted" ]; then
      printf '%s' "$minted"
      return 0
    fi
    echo "$1: could not mint a token from $app (app id, key or installation); trying $1.token" >&2
  fi
  if [ -s "$CONF/$1.token" ]; then
    cat "$CONF/$1.token"
  fi
}
dry=""
if [ -n "$DRY_RUN" ]; then
  dry="--dry-run"
fi
reports=""
hosts_args=""
if [ -n "$BOX" ] && [ -r /proc/meminfo ]; then
  # This box's own reading, in the dashboard's host format (load5, cpus, MemTotal, MemAvailable, disks), plus
  # swap_pages_per_s: pages swapped in + out per second since the last pass (/proc/vmstat), the signal that pauses an
  # idle slot (capacity.host.pause_on). The first pass has no earlier count, so no swap reading.
  python3 - "$BOX" "$STATE/vmstat.prev" > "$STATE/host.json" <<'PY' && hosts_args="--hosts $STATE/host.json"
import json, os, shutil, sys, time
mem = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in open("/proc/meminfo") if l.split()[1:2]}
du = shutil.disk_usage("/")
h = {"host": sys.argv[1], "ok": True, "cpus": os.cpu_count(), "load5": os.getloadavg()[1],
     "MemTotal": mem["MemTotal"], "MemAvailable": mem["MemAvailable"],
     "SwapTotal": mem.get("SwapTotal", 0), "SwapFree": mem.get("SwapFree", 0),
     "disks": [{"mount": "/", "size": du.total, "used": du.used}]}
try:
    vm = {l.split()[0]: int(l.split()[1]) for l in open("/proc/vmstat") if l.startswith(("pswpin ", "pswpout "))}
    now = {"t": time.time(), "pages": vm.get("pswpin", 0) + vm.get("pswpout", 0)}
    try:
        prev = json.load(open(sys.argv[2]))
        dt = now["t"] - prev["t"]
        if 0 < dt < 3 * 3600 and now["pages"] >= prev["pages"]:
            h["swap_pages_per_s"] = round((now["pages"] - prev["pages"]) / dt, 1)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    with open(sys.argv[2], "w") as fh:
        json.dump(now, fh)
except OSError:
    pass
print(json.dumps([h]))
PY
fi

owners="$(grep -v '^[[:space:]]*#' "$CONF/repos" | grep '/' | cut -d/ -f1 | sort -u || true)"
if [ -z "$owners" ]; then
  echo "no owner/repo lines in $CONF/repos: nothing to check"
  exit 0
fi
for owner in $owners; do
  tok="$(token_for "$owner")"
  if [ -z "$tok" ]; then
    echo "skip $owner: no working $CONF/$owner.app or $owner.token"
    continue
  fi
  args=""
  for r in $(grep -v '^[[:space:]]*#' "$CONF/repos" | grep "^$owner/"); do
    args="$args --repo $r"
  done
  out="$STATE/capacity-$owner.json"
  # shellcheck disable=SC2086
  if GH_TOKEN="$tok" python3 "$HERE/steward.py" capacity $args $hosts_args --out "$out" \
      --state "$STATE/capacity-state-$owner.json"; then
    # Cancels with this org's token; restarts once per pass over every org's report, below.
    GH_TOKEN="$tok" python3 "$HERE/capacity_apply.py" "$out" --no-restart $dry \
      --cancel-state "$STATE/cancelled-$owner.json" \
      || echo "$owner: some cancels failed (see above)"
    reports="$reports $out"
  else
    echo "$owner: capacity check failed"
  fi
  if [ -n "$PRIORITY_LABEL" ]; then
    # shellcheck disable=SC2086
    GH_TOKEN="$tok" python3 "$HERE/priority_apply.py" $args --label "$PRIORITY_LABEL" $dry \
      --state "$STATE/priority-$owner.json" \
      || echo "$owner: some priority bumps or re-runs failed (see above)"
  fi
done
if [ -n "$reports" ] && [ -n "$RESTART" ]; then
  # shellcheck disable=SC2086
  python3 "$HERE/capacity_apply.py" $reports --no-cancel --slot-regex "$SLOT_REGEX" --restart "$RESTART" --host "$BOX" $dry \
    --stop-cmd "$STOP" \
    || echo "some restarts failed (see above)"
fi

# The PR sweep reads every open PR's checks, so it runs less often than the capacity pass.
if [ "${SWEEP_EVERY_MIN:-0}" -gt 0 ]; then
  stamp="$STATE/sweep.stamp"
  if [ -f "$stamp" ] && [ -n "$(find "$stamp" -mmin -"$SWEEP_EVERY_MIN" 2>/dev/null)" ]; then
    exit 0
  fi
  touch "$stamp"
  for owner in $owners; do
    tok="$(token_for "$owner")"
    [ -n "$tok" ] || continue
    args=""
    for r in $(grep -v '^[[:space:]]*#' "$CONF/repos" | grep "^$owner/"); do
      args="$args --repo $r"
    done
    out="$STATE/sweep-$owner.json"
    # shellcheck disable=SC2086
    if GH_TOKEN="$tok" python3 "$HERE/steward.py" sweep $args --out "$out" \
        --history-dir "$STATE/steward-history" --lite --max-prs "$SWEEP_MAX_PRS" --min-rate "$SWEEP_MIN_RATE"; then
      GH_TOKEN="$tok" python3 "$HERE/sweep_apply.py" "$out" $dry \
        || echo "$owner: some re-runs or cancels failed (see above)"
    else
      echo "$owner: sweep failed"
    fi
  done
fi
