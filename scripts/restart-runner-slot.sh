#!/usr/bin/env bash
# Restart exactly one runner slot service. Installed root-owned and named in sudoers, so the capacity user can
# restart hph-runner@<number> and nothing else: the argument must be a plain slot number.
set -euo pipefail
slot="${1:-}"
case "$slot" in
  ''|*[!0-9]*) echo "usage: restart-runner-slot.sh <slot number>" >&2; exit 2 ;;
esac
if [ "$#" -ne 1 ]; then
  echo "restart-runner-slot.sh takes exactly one slot number" >&2
  exit 2
fi
exec /usr/bin/systemctl restart "hph-runner@${slot}.service"
