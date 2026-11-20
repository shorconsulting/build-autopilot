#!/bin/bash
# mac-runner-watchdog.sh: keep a Mac's containerized GitHub runners running, with no AI, no server and no GitHub API.
# Run by launchd every 2 minutes (scripts/install-mac-runner-watchdog.sh). Each pass:
#   1. Colima (the Docker VM) is not running: start it.
#   2. A runner container has exited: start it.
#   3. A runner container is up but its runner process (Runner.Listener) is gone for two passes in a row: restart it.
#      A container running a job (Runner.Worker) is never touched.
#   4. A runner whose last WATCHDOG_FAST_FAILS (3) jobs since it started all failed within WATCHDOG_FAST_FAIL_S (90)
#      seconds is broken in a way every job hits (checkout, setup, a full disk): restart it, which gives it a clean
#      work folder. Real test failures take longer than that, so a runner that just picked up red PRs is left alone.
# A container is restarted at most WATCHDOG_MAX_PER_HOUR (3) times an hour; past that it is left alone and logged,
# because something is wrong that a restart won't fix. Every action is one line in the log.
#
#   mac-runner-watchdog.sh            one pass
#   mac-runner-watchdog.sh --dry-run  say what it would do, change nothing
#   mac-runner-watchdog.sh --status   each runner container's state, and the last pass
#
# Containers are found by name: WATCHDOG_MATCH (an extended regex, default 'dind|hporthealth|runner').
# bash 3.2, ASCII only.
set -uo pipefail
export PATH="${WATCHDOG_TEST_BIN:+$WATCHDOG_TEST_BIN:}/opt/homebrew/bin:/usr/local/bin:$PATH"
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.colima/default/docker.sock}"

MATCH="${WATCHDOG_MATCH:-dind|hporthealth|runner}"
STATE="${WATCHDOG_STATE:-$HOME/.local/state/mac-runner-watchdog}"
LOG="${WATCHDOG_LOG:-$HOME/Library/Logs/mac-runner-watchdog.log}"
MAX_PER_HOUR="${WATCHDOG_MAX_PER_HOUR:-3}"
FAST_FAILS="${WATCHDOG_FAST_FAILS:-3}"
FAST_FAIL_S="${WATCHDOG_FAST_FAIL_S:-90}"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1
mkdir -p "$STATE" "$(dirname "$LOG")"

# Keep the log small: past about 1 MB, keep the newest half.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1000000 ]; then
  tail -c 500000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

say() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') $*"
  echo "$line"
  echo "$line" >> "$LOG"
}

act() { # act <what> <command...>: run it, or say it would
  local what="$1"
  shift
  if [ -n "$DRY" ]; then
    say "would $what"
    return 0
  fi
  if "$@" >/dev/null 2>&1; then
    say "$what: ok"
  else
    say "$what: FAILED"
    return 1
  fi
}

containers() {
  docker ps -a --format '{{.Names}}|{{.State}}' 2>/dev/null | grep -E "^[^|]*($MATCH)[^|]*\|" || true
}

if [ "${1:-}" = "--status" ]; then
  containers | while IFS='|' read -r name st; do
    procs="$(docker top "$name" -eo args 2>/dev/null | grep -oE 'Runner\.(Listener|Worker)' | sort -u | tr '\n' ' ')"
    echo "$name $st ${procs:-no-runner-process}"
  done
  echo "last pass: $(tail -1 "$LOG" 2>/dev/null)"
  exit 0
fi

# Restarts this hour for one container; records one more when called with "add".
budget() {
  local f="$STATE/$1.restarts" now cutoff
  now="$(date +%s)"
  cutoff=$((now - 3600))
  touch "$f"
  awk -v c="$cutoff" '$1 >= c' "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  [ "${2:-}" = "add" ] && echo "$now" >> "$f"
  wc -l < "$f" | tr -d ' '
}

# Durations in seconds of the jobs a runner finished since its container started, with their result, oldest first:
# "<seconds> <Succeeded|Failed|Canceled>". From the runner's own lines:
#   2026-11-20 22:36:17Z: Running job: enable
#   2026-11-20 22:36:34Z: Job enable completed with result: Succeeded
job_results() {
  local since
  since="$(docker inspect -f '{{.State.StartedAt}}' "$1" 2>/dev/null)"
  docker logs ${since:+--since "$since"} "$1" 2>&1 | awk '
    function secs(t,  a) { split(t, a, ":"); return a[1] * 3600 + a[2] * 60 + a[3] }
    /: Running job: / { start = secs(substr($2, 1, 8)) }
    / completed with result: / {
      if (start != "") { d = secs(substr($2, 1, 8)) - start; if (d < 0) d += 86400; print d, $NF } ; start = "" }'
}

fast_failing() { # true when the last FAST_FAILS jobs all failed fast
  local tail_n
  tail_n="$(job_results "$1" | tail -n "$FAST_FAILS")"
  [ "$(printf '%s\n' "$tail_n" | grep -c .)" -ge "$FAST_FAILS" ] || return 1
  printf '%s\n' "$tail_n" | awk -v max="$FAST_FAIL_S" '$2 != "Failed" || $1 > max { bad = 1 } END { exit bad }'
}

# 1. Colima
if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null 2>&1; then
    act "colima start (docker not answering)" colima start || exit 1
  else
    say "docker not answering and colima not installed: nothing to do"
    exit 1
  fi
fi

seen=0
while IFS='|' read -r name st; do
  [ -n "$name" ] || continue
  seen=$((seen + 1))
  miss="$STATE/$name.missing"
  if [ "$st" != "running" ]; then
    rm -f "$miss"
    if [ "$(budget "$name")" -ge "$MAX_PER_HOUR" ]; then
      say "$name: $st, but already restarted $MAX_PER_HOUR times this hour; leaving it for a person"
      continue
    fi
    [ -z "$DRY" ] && budget "$name" add >/dev/null
    act "$name: was $st, docker start" docker start "$name"
    continue
  fi
  procs="$(docker top "$name" -eo args 2>/dev/null)"
  if echo "$procs" | grep -q 'Runner\.Worker'; then
    rm -f "$miss"  # running a job: healthy, never touched
    continue
  fi
  if echo "$procs" | grep -q 'Runner\.Listener'; then
    rm -f "$miss"
    if fast_failing "$name"; then
      if [ "$(budget "$name")" -ge "$MAX_PER_HOUR" ]; then
        say "$name: every recent job fails within ${FAST_FAIL_S}s, but already restarted $MAX_PER_HOUR times this hour; leaving it for a person"
      else
        [ -z "$DRY" ] && budget "$name" add >/dev/null
        act "$name: last $FAST_FAILS jobs all failed within ${FAST_FAIL_S}s (checkout/setup), docker restart" \
          docker restart "$name"
      fi
    fi
    continue
  fi
  # Up, but no runner process. Once can be the moment between jobs (an ephemeral runner re-registering): wait a pass.
  if [ ! -f "$miss" ]; then
    [ -z "$DRY" ] && touch "$miss"
    say "$name: up but no runner process; checking again next pass"
    continue
  fi
  if [ "$(budget "$name")" -ge "$MAX_PER_HOUR" ]; then
    say "$name: no runner process, but already restarted $MAX_PER_HOUR times this hour; leaving it for a person"
    continue
  fi
  [ -z "$DRY" ] && budget "$name" add >/dev/null && rm -f "$miss"
  act "$name: no runner process for two passes, docker restart" docker restart "$name"
done <<EOF
$(containers)
EOF

[ "$seen" -gt 0 ] || say "no runner containers match '$MATCH'"
[ -z "$DRY" ] && date +%s > "$STATE/last-pass"
exit 0
