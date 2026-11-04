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
# commit 訊息一律中文、寫清楚改了什麼（abu 紅線；舊範本「anytype export from <host>」是英文，2026-11-04 改）
NS="$(git diff --cached --name-status)"
N_ADD="$(printf '%s\n' "$NS" | grep -c '^A' || true)"
N_MOD="$(printf '%s\n' "$NS" | grep -c '^[MR]' || true)"
N_DEL="$(printf '%s\n' "$NS" | grep -c '^D' || true)"
MSG="Anytype 匯出：$(hostname -s) 新增 ${N_ADD}／修改 ${N_MOD}／刪除 ${N_DEL} 頁（$(date '+%F %T')）"
git commit -q -m "$MSG" -m "anytype_export_cron.sh 每日排程：把 Anytype 上下文庫匯成 markdown 進 anytype-export/，內容有變才 commit。" >>"$LOG" 2>&1
for i in 1 2 3; do
  git push origin main >>"$LOG" 2>&1 && { log "✅ push 成功"; exit 0; }
  git pull --rebase origin main >>"$LOG" 2>&1
done
log "❌ push 三次失敗"; exit 1
