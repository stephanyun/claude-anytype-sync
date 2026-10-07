#!/usr/bin/env bash
# Symlink the canonical engine scripts from this checkout into ~/.claude/anytype/.
# Idempotent. Real files that already exist are backed up to *.bak once.
#
# Usage:  bash install.sh
# Update fleet later:  cd <checkout> && git pull   (symlinks pick it up instantly)
set -euo pipefail

CHECKOUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/anytype"
SCRIPTS=(sync.py hook.py hook.sh skills_catalog.py doc.py doc_patch.py doc_create.py md_clean.py drift_check.py drift_check_cron.sh memory_git_sync.sh fleet_audit.py fleet_probe.py session_probe.sh anytype_export.py anytype_export_cron.sh)

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

# settings.json 外科式升級（2026-10-08）：PostToolUse 的 anytype hook 命令從直接起 python3
# 換成 bash 外殼 hook.sh（非 memory 檔不起 python3）。只替換「逐字等於舊命令」的那一條，
# 沒註冊的不補（註冊仍照下面 2) 手動 merge）、其他鍵一概不碰、改之前備份到 backups/。
SETTINGS="$HOME/.claude/settings.json"
if [ -f "$SETTINGS" ] && command -v python3 >/dev/null 2>&1; then
  python3 - "$SETTINGS" <<'PY'
import json, os, shutil, sys, time
p = sys.argv[1]
OLD = "python3 ~/.claude/anytype/hook.py"
NEW = "bash ~/.claude/anytype/hook.sh"
try:
    data = json.load(open(p))
    changed = False
    for g in (data.get("hooks") or {}).get("PostToolUse", []):
        for h in g.get("hooks", []):
            if h.get("command", "").strip() == OLD:
                h["command"] = NEW; changed = True
    if changed:
        bk = os.path.join(os.path.dirname(p), "backups"); os.makedirs(bk, exist_ok=True)
        shutil.copy(p, os.path.join(bk, "settings.json." + time.strftime("%Y%m%d")))
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2); f.write("\n")
        os.chmod(tmp, os.stat(p).st_mode & 0o777); os.replace(tmp, p)
        print("settings.json: PostToolUse anytype hook -> " + NEW)
    else:
        print("settings.json: anytype hook 已是外殼或尚未註冊（未動）")
except Exception as e:
    print("settings.json 升級跳過: %s" % e)
PY
fi

echo
echo "Done. Next, per machine (NOT from this repo):"
echo "  1) cp config.template.json $DEST/config.json  -> fill api_key/api_base/skills_ns/memory_dirs ; chmod 600"
echo "  2) merge settings.hooks.json into ~/.claude/settings.json"
echo "  3) headless: install anytype-cli + service start + auth + space join (Editor)"
echo "  4) python3 $DEST/sync.py reconcile   &&   python3 $DEST/skills_catalog.py sync"
