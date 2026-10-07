#!/usr/bin/env bash
# 每日把 Anytype 上下文庫匯成 markdown 進 claude-memory 私庫（anytype-export/）。
# 只有內容有變才 commit；push 被拒就 pull --rebase 重試。只在 xz-pve-aide 排程（常駐節點）。
set -uo pipefail
REPO="${MEMORY_GIT_REPO:-$HOME/.claude/memory-git}"
LOG="$HOME/.claude/anytype/export.log"
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
[ -d "$REPO/.git" ] || { log "❌ $REPO 不是 git repo"; exit 1; }
cd "$REPO" || exit 1
git pull --rebase --autostash origin main >>"$LOG" 2>&1 || log "⚠️ pull 失敗，繼續"
OUT="$(timeout 600 python3 "$HOME/.claude/anytype/anytype_export.py" "$REPO/anytype-export" 2>&1)" || { log "❌ 匯出失敗：$OUT"; exit 1; }
log "$OUT"
git add -A anytype-export
if git diff --cached --quiet; then log "無變更，不 commit"; exit 0; fi
git commit -q -m "anytype export from $(hostname -s) $(date '+%F %T')" >>"$LOG" 2>&1
for i in 1 2 3; do
  git push origin main >>"$LOG" 2>&1 && { log "✅ push 成功"; exit 0; }
  git pull --rebase origin main >>"$LOG" 2>&1
done
log "❌ push 三次失敗"; exit 1
