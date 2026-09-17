#!/bin/bash
# 每週掃一次 Anytype 上下文庫的 markdown 損壞。cron 進入點。
# 每次都寫一行結果（所以「log 一直沒長」＝排程死了，跟「掃過沒事」分得出來），
# 有新問題才附完整清單。確認過是誤判就跑 `drift_check.py --accept` 收進基準線。
set -u
LOG="$HOME/.claude/anytype/drift_check.log"
OUT=$(timeout 600 python3 "$HOME/.claude/anytype/drift_check.py" 2>&1)
RC=$?
STAMP=$(date '+%F %H:%M')
TAIL=$(printf '%s\n' "$OUT" | tail -1)
if [ "$RC" -eq 0 ]; then
    printf '%s  OK  %s\n' "$STAMP" "$TAIL" >> "$LOG"
else
    printf '%s  有新問題（rc=%s）\n%s\n\n' "$STAMP" "$RC" "$OUT" >> "$LOG"
fi
