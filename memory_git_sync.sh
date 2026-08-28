#!/usr/bin/env bash
# Claude memory ←→ 私有 git repo 同步
#   1) 先跑 anytype reconcile（Anytype 是即時同步匯流排）
#   2) 把 config.json 裡每個 namespace 的目錄快照進 repo
#   3) commit + push（有變更才 commit；push 被拒就 pull --rebase 重試）
# 權威正本＝這個 repo；任何一台都能跑，重複跑無害。
set -uo pipefail

REPO="${MEMORY_GIT_REPO:-$HOME/.claude/memory-git}"
CFG="$HOME/.claude/anytype/config.json"
SYNC="$HOME/.claude/anytype/sync.py"
LOG="$HOME/.claude/memory-git-sync.log"
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname)"

log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

[ -d "$REPO/.git" ] || { log "❌ $REPO 不是 git repo，請先 clone"; exit 1; }
[ -f "$CFG" ]       || { log "❌ 找不到 $CFG"; exit 1; }

# --- 1) Anytype reconcile ---------------------------------------------------
if [ -f "$SYNC" ]; then
  OUT="$(cd "$HOME" && timeout 600 python3 "$SYNC" reconcile 2>&1 | tail -1)"
  log "reconcile: $OUT"
  CONFLICT="$(echo "$OUT" | grep -oE 'conflict [0-9]+' | grep -oE '[0-9]+' || echo 0)"
  if [ "${CONFLICT:-0}" -gt 0 ]; then
    log "🛑 reconcile 有 $CONFLICT 個衝突，不做 git 快照（避免把半同步狀態寫成正本）"
    exit 2
  fi
else
  log "⚠️ 找不到 sync.py，跳過 reconcile（只做 git 快照）"
fi

# --- 2) 先 pull，再快照 ------------------------------------------------------
cd "$REPO" || exit 1
git pull --rebase --autostash origin main >>"$LOG" 2>&1 || log "⚠️ pull 失敗（可能是首次 push 前），繼續"

# 從 config.json 讀出所有 ns → 目錄
# 用 while-read 而非 mapfile：macOS 內建 bash 是 3.2，沒有 mapfile
PAIRS_FILE="$(mktemp)"
python3 - "$CFG" > "$PAIRS_FILE" <<'PYNS'
import json,sys,os
c=json.load(open(sys.argv[1]))
for ns,d in c.get("memory_dirs",{}).items():
    name = ns if ns else "main"
    d=os.path.expanduser(d)
    if os.path.isdir(d):
        print("%s\t%s" % (name,d))
PYNS

while IFS=$'\t' read -r ns dir; do
  [ -n "$ns" ] || continue
  mkdir -p "$REPO/$ns"
  # --delete：本機刪除也要反映到 repo，靠 git 歷史當還原網
  rsync -a --delete --include='*.md' --include='*/' --exclude='*' "$dir/" "$REPO/$ns/" 2>>"$LOG"
  log "快照 ns=$ns ← $dir （$(ls "$REPO/$ns"/*.md 2>/dev/null | wc -l | tr -d ' ') 檔）"
done < "$PAIRS_FILE"
rm -f "$PAIRS_FILE"

# --- 2b) 正規化快照 ---------------------------------------------------------
# Anytype 的 markdown round-trip 會在 YAML frontmatter 行尾留下不固定的空白
# （實測 178/179 個檔在兩台之間只差 "metadata:" 後面那個空格）。
# 不正規化的話，兩台的排程會每小時互相翻案，repo 全是雜訊 commit。
# 只清 frontmatter 區塊的行尾空白 —— 正文不動，因為 markdown 的行尾兩空格是換行語意。
python3 - "$REPO" <<'PYNORM'
import os,sys,io
root=sys.argv[1]; n=0
for dirpath,dirnames,filenames in os.walk(root):
    if ".git" in dirpath.split(os.sep): continue
    for fn in filenames:
        if not fn.endswith(".md"): continue
        fp=os.path.join(dirpath,fn)
        try: t=io.open(fp,encoding="utf-8").read()
        except Exception: continue
        lines=t.split("\n"); out=[]; infm=False; seen=0; changed=False
        for i,l in enumerate(lines):
            if l.strip()=="---" and seen<2 and (i==0 or infm):
                seen+=1; infm=(seen==1); out.append(l); continue
            if infm:
                st=l.rstrip()
                if st!=l: changed=True
                out.append(st)
            else:
                out.append(l)
        t2="\n".join(out)
        if not t2.endswith("\n"): t2+="\n"; changed=True
        while t2.endswith("\n\n"): t2=t2[:-1]; changed=True
        if changed:
            io.open(fp,"w",encoding="utf-8").write(t2); n+=1
print("正規化 %d 檔" % n)
PYNORM

# --- 3) commit + push --------------------------------------------------------
git add -A
if git diff --cached --quiet; then
  log "無變更，不 commit"
  exit 0
fi
STAT="$(git diff --cached --shortstat)"
git -c user.name="${GIT_AUTHOR_NAME:-$(git config user.name || echo claude)}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-$(git config user.email || echo claude@localhost)}" \
    commit -q -m "memory snapshot from ${HOSTNAME_SHORT} $(date '+%F %T')" -m "$STAT"
log "commit: $STAT"

if ! git push -q origin main 2>>"$LOG"; then
  log "⚠️ push 被拒，pull --rebase 後重試"
  git pull --rebase --autostash origin main >>"$LOG" 2>&1
  git push -q origin main 2>>"$LOG" && log "✅ 重試 push 成功" || { log "❌ push 仍失敗"; exit 3; }
else
  log "✅ push 成功"
fi
