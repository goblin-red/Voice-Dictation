#!/bin/bash
# Keep the original quick-install URL working after the move to utility/.
set -euo pipefail

installer_source="${BASH_SOURCE[0]:-}"
if [ -n "$installer_source" ] && [ -f "$installer_source" ]; then
  installer_root="$(cd "$(dirname "$installer_source")" && pwd)"
  if [ -f "$installer_root/utility/scripts/install.sh" ]; then
    exec bash "$installer_root/utility/scripts/install.sh" "$@"
  fi
fi

curl -fsSL https://raw.githubusercontent.com/goblin-red/Orca-Voice/main/utility/scripts/install.sh |
  bash -s -- "$@"
