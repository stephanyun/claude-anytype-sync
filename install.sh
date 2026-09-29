#!/usr/bin/env bash
# Symlink the canonical engine scripts from this checkout into ~/.claude/anytype/.
# Idempotent. Real files that already exist are backed up to *.bak once.
#
# Usage:  bash install.sh
# Update fleet later:  cd <checkout> && git pull   (symlinks pick it up instantly)
set -euo pipefail

CHECKOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/anytype"
SCRIPTS=(sync.py hook.py skills_catalog.py doc.py doc_patch.py drift_check.py drift_check_cron.sh memory_git_sync.sh fleet_audit.py fleet_probe.py session_probe.sh)

mkdir -p "$DEST"
for f in "${SCRIPTS[@]}"; do
  src="$CHECKOUT/$f"
  dst="$DEST/$f"
  if [ -L "$dst" ]; then
    ln -sfn "$src" "$dst"                       # already a symlink -> repoint
  elif [ -e "$dst" ]; then
    if ! cmp -s "$src" "$dst"; then cp "$dst" "$dst.bak"; echo "backed up $dst -> $dst.bak"; fi
    ln -sfn "$src" "$dst"
  else
    ln -sfn "$src" "$dst"
  fi
  echo "linked $dst -> $src"
done

echo
echo "Done. Next, per machine (NOT from this repo):"
echo "  1) cp config.template.json $DEST/config.json  -> fill api_key/api_base/skills_ns/memory_dirs ; chmod 600"
echo "  2) merge settings.hooks.json into ~/.claude/settings.json"
echo "  3) headless: install anytype-cli + service start + auth + space join (Editor)"
echo "  4) python3 $DEST/sync.py reconcile   &&   python3 $DEST/skills_catalog.py sync"
