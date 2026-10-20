#!/usr/bin/env bash
#
# One command to bring a frame fully up to date from GitHub: the core app
# and, if they were ever set up on this Pi, the three companion services
# (calendar/weather, motion, video) too -- no separate `git pull` first, no
# remembering which installer handles which piece.
#
#   cd ~/digital-picture-frame-pi-2026   # wherever this tree was cloned
#   bash packaging/update.sh
#
# Safe to re-run any time -- install.sh and install_companions.sh both are,
# and both keep every answer already given rather than asking again.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.."

echo "Pulling the latest from GitHub…"
git pull --ff-only

bash packaging/install.sh

# install_companions.sh needs this source tree (it is not the kind of
# script a curl-piped install.sh can run on its own), and only makes sense
# on a Pi that actually has the companions set up -- the answers file is
# the honest test for that, the same one install_companions.sh itself uses.
if [ -f "$HOME/.config/picframe3/companions.env" ]; then
  echo
  bash packaging/install_companions.sh
fi
