#!/bin/bash
# mac-runner-governor.sh: keep a Mac's containerized GitHub runners inside what the machine can carry, by taking
# runner slots out of service when it is overloaded and putting them back when the load eases. No AI, no server.
# The Mac runner watchdog runs it at the end of every pass (every 2 minutes); it can also be run by hand.
#
# David S, 21 Nov 2026: "I think we need to have as smart a load manager as possible--when things are taking too
# long we can reduce the load and get the backlog cleared better". The night before, typecheck took 76 minutes
# against about 25 seconds normally, with several runners sharing one Mac mini: every job slowed down so far that
# fewer runners would have cleared the queue sooner.
#
# Each pass reads:
#   - the Colima VM the runners live in (colima ssh): memory pressure stall (PSI, the share of time tasks waited on
#     memory), available memory, load per core;
#   - the Mac itself (sysctl): macOS memory pressure level, swap in use and how fast it grows, load per core;
#   - each runner's running job, against how long that job usually takes here (learned from the runners' own logs).
# It then calls the box:
#   red      memory stalls, memory nearly gone, load far above the cores, the Mac swapping hard or in critical
#            memory pressure, or a job running GOV_SLOW_RATIO times its usual time while other jobs share the box
#            and the machine readings are already off calm. Severe (acts on the first pass): VM memory at or under
#            GOV_RED_MEM_AVAIL free, a memory stall (some avg60 or full avg10) at GOV_SEVERE_MEM_PSI, or the Mac in
#            critical pressure.
#   green    all readings well inside the limits, no slow job.
#   (else)   hold: change nothing.
# and acts, one change per GOV_COOLDOWN_S:
#   red two passes in a row (or once, when severe) -> take ONE runner out: an idle one is stopped at once (looked
#            at again right before the stop); with GOV_GH_ORGS, otherwise the busy one closest to done drains: its
#            labels come off at GitHub so it is given no new job, and it is stopped once its job ends and GitHub
#            says it is not busy. A running job is never stopped. Never below GOV_MIN_SLOTS runners in service per
#            runner group (the name without -N).
#   green three passes in a row -> undo ONE: cancel a drain, or start the runner paused most recently.
# Without GOV_GH_ORGS only idle runners are stopped: a busy one could be handed its next job in the moment between
# its job ending and the stop, so it is never drained without its labels off. A draining runner is watched every
# GOV_POLL_S seconds for up to GOV_WATCH_S, and its labels go back when it returns.
#
# Paused runners are listed in $STATE/governor/paused/; the watchdog does not start them again, and the steward's
# capacity check (on this computer) leaves them out of its offline checks. Every action is one line in the log.
#
#   mac-runner-governor.sh              one pass
#   mac-runner-governor.sh --dry-run    say what it would do, change nothing
#   mac-runner-governor.sh --status     the last reading, what is paused or draining, and the learned job times
#   mac-runner-governor.sh --resume-all start every paused runner and cancel every drain (to turn it off: also set
#                                       WATCHDOG_GOVERNOR=0 in the launchd agent, or remove the watchdog)
#
# Settings (environment; defaults in brackets):
#   GOV_MIN_SLOTS [2]  GOV_COOLDOWN_S [360]  GOV_RED_PASSES [2]  GOV_GREEN_PASSES [3]
#   GOV_RED_MEM_PSI [10]   VM memory stall, % of the last minute    GOV_GREEN_MEM_PSI [2]
#   GOV_SEVERE_MEM_PSI [25]                                         (one pass is enough)
#   GOV_RED_MEM_AVAIL [5]  VM available memory, %                   GOV_GREEN_MEM_AVAIL [20]
#   GOV_RED_LOAD [2.0]     load (1 minute) per core, VM or Mac      GOV_GREEN_LOAD [1.0]
#   GOV_RED_SWAP_GROWTH_MB [1024] Mac swap growth since the last pass   GOV_GREEN_SWAP_GROWTH_MB [256]
#   GOV_SLOW_RATIO [3]  GOV_SLOW_MIN_S [600]  GOV_BASE_N [15]  GOV_BASE_MIN [3]
#   GOV_WATCH_S [100]  GOV_POLL_S [2]
#   GOV_MAX_PAUSE_S [86400]  a runner paused this long is started again whatever the readings say
#   GOV_GH_ORGS ['']   e.g. 'sconsinc:SConsInc hporthealth:HPortHealth' (a word of the runner name : its org)
# bash 3.2, ASCII only.
set -uo pipefail
export PATH="${WATCHDOG_TEST_BIN:+$WATCHDOG_TEST_BIN:}/opt/homebrew/bin:/usr/local/bin:$PATH:/usr/sbin:/sbin"
PROFILE="${WATCHDOG_COLIMA_PROFILE:-default}"
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.colima/$PROFILE/docker.sock}"
MATCH="${WATCHDOG_MATCH:-mini-dind-|mini-linux-}"
STATE="${WATCHDOG_STATE:-$HOME/.local/state/mac-runner-watchdog}"
LOG="${WATCHDOG_LOG:-$HOME/Library/Logs/mac-runner-watchdog.log}"
G="$STATE/governor"

MIN_SLOTS="${GOV_MIN_SLOTS:-2}"
COOLDOWN="${GOV_COOLDOWN_S:-360}"
RED_PASSES="${GOV_RED_PASSES:-2}"
GREEN_PASSES="${GOV_GREEN_PASSES:-3}"
RED_MEM_PSI="${GOV_RED_MEM_PSI:-10}"
GREEN_MEM_PSI="${GOV_GREEN_MEM_PSI:-2}"
SEVERE_MEM_PSI="${GOV_SEVERE_MEM_PSI:-25}"
RED_MEM_AVAIL="${GOV_RED_MEM_AVAIL:-5}"
GREEN_MEM_AVAIL="${GOV_GREEN_MEM_AVAIL:-20}"
RED_LOAD="${GOV_RED_LOAD:-2.0}"
GREEN_LOAD="${GOV_GREEN_LOAD:-1.0}"
RED_SWAP="${GOV_RED_SWAP_GROWTH_MB:-1024}"
GREEN_SWAP="${GOV_GREEN_SWAP_GROWTH_MB:-256}"
SLOW_RATIO="${GOV_SLOW_RATIO:-3}"
SLOW_MIN="${GOV_SLOW_MIN_S:-600}"
BASE_N="${GOV_BASE_N:-15}"
BASE_MIN="${GOV_BASE_MIN:-3}"
WATCH_S="${GOV_WATCH_S:-100}"
POLL_S="${GOV_POLL_S:-2}"
GH_ORGS="${GOV_GH_ORGS:-}"
MAX_PAUSE="${GOV_MAX_PAUSE_S:-86400}"

DRY=""
MODE="${1:-}"
[ "$MODE" = "--dry-run" ] && DRY=1
mkdir -p "$G/paused" "$G/draining" "$(dirname "$LOG")"

say() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') governor: $*"
  echo "$line"
  echo "$line" >> "$LOG"
}

now() { date +%s; }
# Seconds since midnight UTC, the clock the runners' log lines use (GOV_NOW_SOD in tests).
now_sod() {
  if [ -n "${GOV_NOW_SOD:-}" ]; then echo "$GOV_NOW_SOD"; return; fi
  date -u '+%H %M %S' | awk '{ print $1 * 3600 + $2 * 60 + $3 }'
}
ge() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 >= b + 0) }'; }  # a >= b, for decimals
lt() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 < b + 0) }'; }
group_of() { echo "$1" | sed -E 's/-[0-9]+$//'; }
getf() { cat "$G/$1" 2>/dev/null || echo "${2:-0}"; }
setf() { [ -n "$DRY" ] || echo "$2" > "$G/$1"; }

containers() {
  docker ps -a --format '{{.Names}}|{{.State}}' 2>/dev/null | grep -E "^[^|]*($MATCH)[^|]*\|" || true
}
processes() { docker top "$1" 2>/dev/null </dev/null; }

# The runner's job lines since its container started: "<sod> R <name>" (Running job) or "<sod> C <name>" (completed).
job_events() {
  local since
  since="$(docker inspect -f '{{.State.StartedAt}}' "$1" 2>/dev/null)"
  docker logs ${since:+--since "$since"} "$1" 2>&1 | awk '
    function secs(t,  a) { split(t, a, ":"); return a[1] * 3600 + a[2] * 60 + a[3] }
    / Running job: / { n = $0; sub(/.* Running job: /, "", n); print secs(substr($2, 1, 8)), "R", n; next }
    / completed with result: / { n = $0; sub(/.*Z: Job /, "", n); sub(/ completed with result: .*/, "", n)
                                 print secs(substr($2, 1, 8)), "C", n }'
}

# --- readings -------------------------------------------------------------------------------------------------

# The Colima VM: "ncpu load1 memavail_pct mem_psi_some60 mem_psi_full10", or nothing when it can't be read.
read_vm() {
  colima ssh --profile "$PROFILE" -- sh -c 'nproc; cat /proc/loadavg; grep -E "^(MemTotal|MemAvailable):" /proc/meminfo;
    echo PSI_MEM; cat /proc/pressure/memory 2>/dev/null' 2>/dev/null </dev/null | awk '
    NR == 1 { cpus = $1; next }
    NR == 2 { load = $1; next }
    /^MemTotal:/ { tot = $2 } /^MemAvailable:/ { av = $2 }
    /^PSI_MEM/ { psi = 1; next }
    psi && /^some/ { for (i = 2; i <= NF; i++) if ($i ~ /^avg60=/) { sub(/avg60=/, "", $i); s60 = $i } }
    psi && /^full/ { for (i = 2; i <= NF; i++) if ($i ~ /^avg10=/) { sub(/avg10=/, "", $i); f10 = $i } }
    END { if (cpus > 0 && tot > 0 && av != "" && load != "")
            printf "%d %s %.0f %s %s\n", cpus, load, av * 100 / tot, (s60 == "" ? 0 : s60), (f10 == "" ? 0 : f10) }'
}

# The Mac: "ncpu load1 pressure_level swap_used_mb", or nothing. Pressure level: 1 normal, 2 warn, 4 critical.
read_mac() {
  local cpus load lvl swap
  cpus="$(sysctl -n hw.ncpu 2>/dev/null)" || return 0
  load="$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}' | awk '{ print $1 }')"
  lvl="$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)"
  swap="$(sysctl -n vm.swapusage 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "used") { v = $(i + 2); sub(/M$/, "", v); print int(v) } }')"
  [ -n "$cpus" ] && [ -n "$load" ] && echo "$cpus $load ${lvl:-1} ${swap:-0}"
}

# One line per runner container: "name group state elapsed_s job_key 0" where state is idle|busy|starting|unknown|
# stopped, elapsed and job only for a busy runner (mark_slow fills in the last field). Also records finished jobs.
scan_runners() {
  local name st procs ev last sod group
  sod="$(now_sod)"
  while IFS='|' read -r name st <&4; do
    [ -n "$name" ] || continue
    group="$(group_of "$name")"
    if [ "$st" != "running" ]; then
      echo "$name $group stopped 0 - 0"
      continue
    fi
    ev="$(job_events "$name")"
    record_durations "$group" "$name" "$ev"
    if ! procs="$(processes "$name")"; then
      echo "$name $group unknown 0 - 0"
      continue
    fi
    last="$(printf '%s\n' "$ev" | grep . | tail -1)"
    if echo "$procs" | grep -q 'Runner\.Worker'; then
      if [ "$(echo "$last" | awk '{ print $2 }')" = "R" ]; then
        local start el
        start="$(echo "$last" | awk '{ print $1 }')"
        el=$((sod - start))
        [ "$el" -lt 0 ] && el=$((el + 86400))
        echo "$name $group busy $el $(key "$(echo "$last" | cut -d' ' -f3-)") 0"
      else
        echo "$name $group busy 0 - 0"
      fi
    elif echo "$procs" | grep -q 'Runner\.Listener'; then
      # No job process. If the last thing it logged is a job it took, the job is starting: never stop it.
      if [ "$(echo "$last" | awk '{ print $2 }')" = "R" ]; then
        echo "$name $group starting 0 - 0"
      else
        echo "$name $group idle 0 - 0"
      fi
    else
      echo "$name $group unknown 0 - 0"
    fi
  done 4<<EOF
$(containers)
EOF
}

# Mark each busy runner whose job has run GOV_SLOW_RATIO times its usual time (and at least GOV_SLOW_MIN_S), once
# every runner's finished jobs are recorded.
mark_slow() {
  local n g s e k sl base
  while read -r n g s e k sl; do
    [ -n "$n" ] || continue
    sl=0
    if [ "$s" = busy ] && [ "$k" != "-" ] && [ "$e" -ge "$SLOW_MIN" ]; then
      base="$(baseline "$g" "$k")"
      if [ -n "$base" ] && ge "$e" "$(awk -v b="$base" -v r="$SLOW_RATIO" 'BEGIN { print b * r }')"; then
        sl=1
      fi
    fi
    echo "$n $g $s $e $k $sl"
  done
}

key() { echo "$*" | tr -c 'A-Za-z0-9._-' '_' | sed 's/_*$//'; }

# Finished jobs' durations, one line each in $G/durations: "<group>|<job key> <container>@<end sod> <seconds>".
record_durations() {
  local group="$1" name="$2" ev="$3"
  [ -n "$DRY" ] && return 0
  touch "$G/durations"
  printf '%s\n' "$ev" | awk -v g="$group" -v c="$name" '
    $2 == "R" { s = $1; n = $0; sub(/^[0-9]+ R /, "", n); next }
    $2 == "C" && s != "" { d = $1 - s; if (d < 0) d += 86400; k = n; gsub(/[^A-Za-z0-9._-]/, "_", k); sub(/_+$/, "", k)
                           print g "|" k, c "@" $1, d; s = "" }' > "$G/durations.new"
  # Keep the newest 3000, each finished job once.
  cat "$G/durations" "$G/durations.new" | awk '!seen[$1 " " $2]++' | tail -n 3000 > "$G/durations.tmp" \
    && mv "$G/durations.tmp" "$G/durations"
  rm -f "$G/durations.new"
}

# The median of the last GOV_BASE_N runs of a job in a runner group, or nothing with fewer than GOV_BASE_MIN.
baseline() {
  local k
  k="$1|$(key "$2")"
  [ -f "$G/durations" ] || return 0
  awk -v k="$k" '$1 == k { print $3 }' "$G/durations" | tail -n "$BASE_N" | sort -n | awk -v min="$BASE_MIN" '
    { v[NR] = $1 } END { if (NR >= min) print (NR % 2 ? v[(NR + 1) / 2] : (v[NR / 2] + v[NR / 2 + 1]) / 2) }'
}

# --- GitHub labels (optional) ---------------------------------------------------------------------------------

org_of() {
  local pair
  for pair in $GH_ORGS; do
    case "$1" in *"${pair%%:*}"*) echo "${pair#*:}"; return ;; esac
  done
}

runner_id() { gh api "orgs/$1/actions/runners" --paginate --jq ".runners[] | select(.name == \"$2\") | .id" 2>/dev/null | head -1; }

# Take a busy runner's custom labels off at GitHub, keeping them in $G/draining/<name>.labels, so GitHub can give it
# no new job. True only when they came off: a busy runner is drained only then, because without it the runner can
# be handed its next job in the moment between "its job ended" and the stop.
unlabel() {
  local name="$1" org id labels
  org="$(org_of "$name")"
  [ -n "$org" ] || return 1
  [ -n "$DRY" ] && { say "would take $name's labels off at GitHub ($org)"; return 0; }
  id="$(runner_id "$org" "$name")"
  [ -n "$id" ] || { say "$name: not found at GitHub ($org); not drained"; return 1; }
  labels="$(gh api "orgs/$org/actions/runners/$id/labels" --jq '.labels[] | select(.type == "custom") | .name' 2>/dev/null)"
  [ -n "$labels" ] || { say "$name: could not read its labels at GitHub ($org); not drained"; return 1; }
  if gh api -X DELETE "orgs/$org/actions/runners/$id/labels" >/dev/null 2>&1; then
    printf '%s\n' "$labels" > "$G/draining/$name.labels"
    echo "$org" > "$G/draining/$name.org"  # put back to this org whatever GOV_GH_ORGS says later
    echo "$id" > "$G/draining/$name.id"
    say "$name: labels taken off at GitHub ($(echo $labels)), so it takes no new job"
    return 0
  fi
  say "$name: could not take its labels off at GitHub ($org); not drained"
  return 1
}

# For a runner whose labels the governor took off: true when GitHub says it is not running a job.
github_idle() {
  local name="$1" org id
  [ -f "$G/draining/$name.id" ] || return 0
  org="$(cat "$G/draining/$name.org" 2>/dev/null || org_of "$name")"
  id="$(cat "$G/draining/$name.id")"
  [ "$(gh api "orgs/$org/actions/runners/$id" --jq .busy 2>/dev/null)" = "false" ]
}

# Put back labels unlabel() took off, to the org saved beside them (GOV_GH_ORGS may have changed or been dropped
# since). The saved labels are only removed once GitHub has them back; otherwise they are kept and tried again
# every pass, and said loudly, because a runner without its labels is online but never given a job.
relabel() {
  local name="$1" f d org id args l
  for f in "$G/draining/$name.labels" "$G/paused/$name.labels"; do
    [ -f "$f" ] || continue
    d="$(dirname "$f")"
    org="$(cat "$d/$name.org" 2>/dev/null || org_of "$name")"
    [ -n "$DRY" ] && { say "would put $name's labels back at GitHub"; return 0; }
    args=""
    for l in $(cat "$f"); do args="$args -f labels[]=$l"; done
    id=""
    [ -n "$org" ] && id="$(runner_id "$org" "$name")"
    # shellcheck disable=SC2086
    if [ -n "$id" ] && gh api -X POST "orgs/$org/actions/runners/$id/labels" $args >/dev/null 2>&1; then
      say "$name: labels put back at GitHub ($(echo $(cat "$f")))"
      rm -f "$f" "$d/$name.org"
    else
      say "$name: COULD NOT put its labels back at GitHub (${org:-org unknown}: $(echo $(cat "$f"))); it takes no jobs until they are back; trying again next pass"
      return 1
    fi
  done
  return 0
}

# --- actions --------------------------------------------------------------------------------------------------

stop_paused() { # stop an idle runner and record it as paused by the governor
  local name="$1" why="$2"
  if [ -n "$DRY" ]; then
    say "would stop $name ($why)"
    return 0
  fi
  # Looked at again right before the stop (the scan or the last poll may be seconds old): never stop a runner that
  # has taken a job meanwhile, nor one GitHub still counts as busy.
  if ! idle_now "$name" || ! github_idle "$name"; then
    say "$name: took a job before it could be stopped; left running"
    return 1
  fi
  if docker stop "$name" >/dev/null 2>&1; then
    now > "$G/paused/$name"
    [ -f "$G/draining/$name.labels" ] && mv "$G/draining/$name.labels" "$G/paused/$name.labels"
    [ -f "$G/draining/$name.org" ] && mv "$G/draining/$name.org" "$G/paused/$name.org"
    rm -f "$G/draining/$name" "$G/draining/$name.id"
    say "$name: stopped ($why)"
  else
    say "$name: docker stop FAILED ($why)"
  fi
}

resume_one() {
  local name="$1" why="$2"
  if [ -n "$DRY" ]; then
    say "would start $name ($why)"
    return 0
  fi
  if docker start "$name" >/dev/null 2>&1; then
    relabel "$name"
    rm -f "$G/paused/$name"
    say "$name: started again ($why)"
  else
    say "$name: docker start FAILED ($why)"
  fi
}

cancel_drain() {
  local name="$1" why="$2"
  if [ -n "$DRY" ]; then
    say "would stop draining $name ($why)"
    return 0
  fi
  relabel "$name"
  rm -f "$G/draining/$name" "$G/draining/$name.id"
  say "$name: no longer draining ($why)"
}

# Is this runner between jobs right now (job process gone, and the last thing it logged is a finished job)?
idle_now() {
  local procs last
  procs="$(processes "$1")" || return 1
  echo "$procs" | grep -q 'Runner\.Worker' && return 1
  echo "$procs" | grep -q 'Runner\.Listener' || return 1
  last="$(job_events "$1" | grep . | tail -1)"
  [ "$(echo "$last" | awk '{ print $2 }')" != "R" ]
}

# Stop each draining runner the moment its job ends: every GOV_POLL_S seconds for up to GOV_WATCH_S.
watch_drains() {
  local deadline name left
  deadline=$(( $(now) + WATCH_S ))
  while :; do
    left=0
    for f in "$G/draining/"*; do
      [ -f "$f" ] || continue
      case "$f" in *.labels|*.id|*.org) continue ;; esac
      name="$(basename "$f")"
      left=1
      if idle_now "$name"; then
        stop_paused "$name" "drained: its job ended" || true
      fi
    done
    [ "$left" = 1 ] && [ "$(now)" -lt "$deadline" ] || break
    sleep "$POLL_S"
  done
}

paused_list() { for f in "$G/paused/"*; do [ -f "$f" ] && case "$f" in *.labels|*.id|*.org) ;; *) basename "$f" ;; esac; done; }
draining_list() { for f in "$G/draining/"*; do [ -f "$f" ] && case "$f" in *.labels|*.id|*.org) ;; *) basename "$f" ;; esac; done; }

# --- modes ----------------------------------------------------------------------------------------------------

if [ "$MODE" = "--status" ]; then
  echo "last reading: $(getf last-reading none)"
  echo "streak: red $(getf red 0), green $(getf green 0); last change $(getf changed-at never)"
  echo "paused:   $(echo $(paused_list))"
  echo "draining: $(echo $(draining_list))"
  if [ -f "$G/durations" ]; then
    echo "usual job times (median seconds, runs):"
    awk '{ print $1 }' "$G/durations" | sort -u | while read -r k; do
      g="${k%%|*}"; j="${k#*|}"
      echo "  $k $(baseline "$g" "$j") $(awk -v k="$k" '$1 == k' "$G/durations" | wc -l | tr -d ' ')"
    done
  fi
  exit 0
fi

if [ "$MODE" = "--resume-all" ]; then
  for n in $(draining_list); do cancel_drain "$n" "resume-all"; done
  for n in $(paused_list); do resume_one "$n" "resume-all"; done
  exit 0
fi

docker info >/dev/null 2>&1 || { say "docker not answering; nothing read"; exit 0; }

# Runners a person or the watchdog started again are no longer paused.
for n in $(paused_list); do
  if containers | grep -q "^$n|running$"; then
    [ -z "$DRY" ] && { relabel "$n"; rm -f "$G/paused/$n"; }
    say "$n: running again (started outside the governor); no longer paused"
  fi
done

# A pause never outlives GOV_MAX_PAUSE_S: if the readings never come back calm (the VM can't be read, or load sits
# between the limits), the runner is started again rather than left parked.
for n in $(paused_list); do
  if [ $(( $(now) - $(cat "$G/paused/$n" 2>/dev/null || echo 0) )) -ge "$MAX_PAUSE" ]; then
    resume_one "$n" "paused longer than ${MAX_PAUSE}s"
  fi
done

runners="$(scan_runners)"
runners="$(echo "$runners" | mark_slow)"  # after every runner's finished jobs are recorded
vm="$(read_vm)"
mac="$(read_mac)"
busy="$(echo "$runners" | awk '$3 == "busy"' | grep -c . || true)"
slow="$(echo "$runners" | awk '$6 == 1 { printf "%s (%s, %ds) ", $1, $5, $4 }')"

level=green
severe=""
why=""
red() { level=red; why="$why$*; "; }
hold() { [ "$level" = green ] && level=hold; }

if [ -n "$vm" ]; then
  set -- $vm
  vload="$(awk -v l="$2" -v c="$1" 'BEGIN { printf "%.2f", l / c }')"
  ge "$4" "$RED_MEM_PSI" && red "VM memory stall ${4}%"
  ge "$4" "$SEVERE_MEM_PSI" && severe=1
  ge "$5" "$SEVERE_MEM_PSI" && { red "VM memory full stall ${5}%"; severe=1; }
  ge "$RED_MEM_AVAIL" "$3" && { red "VM memory ${3}% free"; severe=1; }
  ge "$vload" "$RED_LOAD" && red "VM load ${vload} per core"
  { ge "$4" "$GREEN_MEM_PSI" || lt "$3" "$GREEN_MEM_AVAIL" || ! lt "$vload" "$GREEN_LOAD"; } && hold
  vmtxt="vm load/core $vload, mem free ${3}%, mem stall ${4}%"
else
  hold
  vmtxt="vm unreadable"
fi

if [ -n "$mac" ]; then
  set -- $mac
  mload="$(awk -v l="$2" -v c="$1" 'BEGIN { printf "%.2f", l / c }')"
  prev_swap="$(getf mac-swap "$4")"
  growth=$(( $4 - prev_swap ))
  setf mac-swap "$4"
  [ "$3" -ge 4 ] && { red "Mac in critical memory pressure"; severe=1; }
  [ "$growth" -ge "$RED_SWAP" ] && red "Mac swap grew ${growth} MB"
  ge "$mload" "$RED_LOAD" && red "Mac load ${mload} per core"
  { [ "$3" -ge 2 ] || [ "$growth" -ge "$GREEN_SWAP" ] || ! lt "$mload" "$GREEN_LOAD"; } && hold
  mactxt="mac load/core $mload, pressure $3, swap ${4} MB (+$growth)"
else
  mactxt="mac unreadable"
fi

# A slow job counts only beside a machine reading that is already off calm: job names (build, test) repeat across
# repos on org-wide runners, so a heavy repo's build can look slow next to a light one's on a box that is fine.
if [ -n "$slow" ]; then
  if [ "$busy" -ge 2 ] && [ "$level" != green ]; then red "slow: $slow"; else hold; fi
fi

reading="$(date '+%Y-%m-%d %H:%M:%S') $level; $vmtxt; $mactxt; busy $busy${slow:+; slow $slow}"
setf last-reading "$reading"

if [ "$level" = red ]; then setf red $(( $(getf red 0) + 1 )); setf green 0
elif [ "$level" = green ]; then setf green $(( $(getf green 0) + 1 )); setf red 0
else setf red 0; setf green 0; fi
reds="$(getf red 0)"; greens="$(getf green 0)"
[ -n "$DRY" ] && { [ "$level" = red ] && reds=$((reds + 1)); [ "$level" = green ] && greens=$((greens + 1)); }

since=$(( $(now) - $(getf changed-at 0) ))
cooling=""
[ "$since" -lt "$COOLDOWN" ] && cooling=1

if [ "$level" = red ] && { [ "$reds" -ge "$RED_PASSES" ] || [ -n "$severe" ]; } && [ -z "$cooling" ]; then
  # In service per group: running and not draining. Pick from the group with the most, idle first, else the busy
  # runner whose job has run longest (soonest done); never one starting a job, and a busy one only where its
  # labels can be taken off at GitHub (GOV_GH_ORGS).
  pick="$(echo "$runners" | while read -r n g s e k sl; do
      [ -f "$G/draining/$n" ] && continue
      dr=0; [ -n "$(org_of "$n")" ] && dr=1
      case "$s" in idle|busy|starting|unknown) echo "$n $g $s $e $dr" ;; esac
    done | awk -v min="$MIN_SLOTS" '
      { n[NR] = $1; g[NR] = $2; s[NR] = $3; e[NR] = $4; dr[NR] = $5; c[$2]++ }
      END { best = ""; for (i = 1; i <= NR; i++) {
              if (c[g[i]] <= min || (s[i] != "idle" && !(s[i] == "busy" && dr[i]))) continue
              score = c[g[i]] * 1000000 + (s[i] == "idle" ? 500000 : 0) + e[i]
              if (best == "" || score > bs) { best = i; bs = score } }
            if (best != "") print n[best], s[best] }')"
  if [ -z "$pick" ]; then
    if [ -z "$GH_ORGS" ]; then
      say "overloaded ($why) but no runner is idle or above its group's minimum of $MIN_SLOTS; a busy runner is only drained with GOV_GH_ORGS set"
    else
      say "overloaded ($why) but every runner group is at its minimum of $MIN_SLOTS; nothing to take out"
    fi
  else
    set -- $pick
    say "overloaded: $why$vmtxt; $mactxt"
    acted=""
    if [ "$2" = idle ]; then
      stop_paused "$1" "overloaded, idle" && acted=1
    elif unlabel "$1"; then
      [ -z "$DRY" ] && now > "$G/draining/$1"
      say "$1: draining (overloaded); it finishes its job and then stops"
      acted=1
    fi
    if [ -n "$acted" ]; then
      setf changed-at "$(now)"
      setf red 0
    fi
  fi
elif [ "$level" = green ] && [ "$greens" -ge "$GREEN_PASSES" ] && [ -z "$cooling" ]; then
  d="$(draining_list | tail -1)"
  p="$(for n in $(paused_list); do echo "$(cat "$G/paused/$n") $n"; done | sort -n | tail -1 | awk '{ print $2 }')"
  if [ -n "$d" ]; then
    cancel_drain "$d" "load eased: $vmtxt; $mactxt"
    setf changed-at "$(now)"; setf green 0
  elif [ -n "$p" ]; then
    resume_one "$p" "load eased: $vmtxt; $mactxt"
    setf changed-at "$(now)"; setf green 0
  fi
fi

# Labels that could not be put back last time (the runner is back in service, its labels file is left over).
for f in "$G/paused/"*.labels "$G/draining/"*.labels; do
  [ -f "$f" ] || continue
  n="$(basename "$f" .labels)"
  [ -f "$G/paused/$n" ] || [ -f "$G/draining/$n" ] || relabel "$n"
done

[ -z "$DRY" ] && [ -n "$(draining_list)" ] && watch_drains
exit 0
