#!/usr/bin/env bash
# SessionStart 前置：Mac 上 Anytype 桌面 App 沒開（API 埠連不上）就在背景開起來，等 API 起來再放行。
# 2026-10-29 加：m2-13 重開機後 Anytype 登入項目被開起來又被關掉（關主視窗＝整個 App 退出），
# 每次開 claude 都跳「Anytype memory sync 失敗」。Linux／headless（anytype-cli）不碰，直接放行。
# 兩支 SessionStart hook 會並行各叫一次；重複 open -g 無害。永遠 exit 0，成敗交給後面的 sync 判斷。

[ "$(uname)" = Darwin ] || exit 0
[ -d /Applications/Anytype.app ] || exit 0

cfg="$HOME/.claude/anytype/config.json"
port=$(grep -o '"api_base"[^,}]*' "$cfg" 2>/dev/null | grep -oE ':[0-9]+' | tail -1 | tr -d :)
port=${port:-31009}

nc -z -G1 localhost "$port" >/dev/null 2>&1 && exit 0

open -g -a Anytype >/dev/null 2>&1 || exit 0
for _ in $(seq 1 50); do
  nc -z -G1 localhost "$port" >/dev/null 2>&1 && { sleep 2; exit 0; }   # 埠剛開時 API 可能還沒就緒，多等 2 秒
  sleep 0.5
done
exit 0
