#!/usr/bin/env bash
# Restart, or stop, exactly one runner slot service. Installed root-owned and named in sudoers, so the capacity user
# can restart or stop hph-runner@<number> and nothing else: the argument must be a plain slot number.
#   restart-runner-slot.sh <slot>          restart (or start) hph-runner@<slot>
#   restart-runner-slot.sh --stop <slot>   stop it (a slot paused because the box is swapping)
set -euo pipefail
verb=restart
if [ "${1:-}" = "--stop" ]; then
  verb=stop
  shift
fi
slot="${1:-}"
case "$slot" in
  ''|*[!0-9]*) echo "usage: restart-runner-slot.sh [--stop] <slot number>" >&2; exit 2 ;;
esac
if [ "$#" -ne 1 ]; then
  echo "restart-runner-slot.sh takes exactly one slot number" >&2
  exit 2
fi
exec /usr/bin/systemctl "$verb" "hph-runner@${slot}.service"
