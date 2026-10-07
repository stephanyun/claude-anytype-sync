#!/usr/bin/env bash
# PostToolUse（Write／Edit／MultiEdit）的 bash 外殼：hook.py 只有 memory 目錄的 .md 才有事做，
# 但 python3 光啟動就 10ms（pve）～60ms（Mac），每一次 Write／Edit 都在付。
# 這層先用 bash 看 stdin 前 4KB 的 tool_input.file_path 是不是「…/memory/….md」，
# 不是就直接 exit 0；是才把完整 stdin 原文餵給 hook.py（同步邏輯全在 .py，這裡不碰）。
#
# 注意：
# - macOS bash 3.2 相容：regex 放變數再 =~，不用 read -N、不用 ${x#*…}
#   （${x#*…} 在 UTF-8 大字串上是 O(n²)，Write 整篇長文會卡）。
# - 只看前 4KB 判斷（file_path 在 tool_input 開頭），但轉交要給完整內容，
#   所以先把整個 stdin 收進變數，再 printf 給 python。
# - 判斷用 /memory/ 而不是 /.claude/memory/：hook.py 沒 config 時會退回 ~/.claude/projects/*/memory。
#   誤判成「要處理」只是多跑一次 python（hook.py 自己會再嚴格判斷），不會漏同步。
LC_ALL=C   # 不 export，只改 bash 自己的 locale（python 那邊維持原 locale）。字串切片與 regex 都照位元組算：UTF-8 下 ${x:0:4096} 會先走完整個字串算字數，600KB 要多 10ms
x=$(cat)
h=${x:0:4096}
re='"file_path"[[:space:]]*:[[:space:]]*"[^"]*/memory/[^"]*\.md"'
[[ $h =~ $re ]] || exit 0
printf '%s' "$x" | exec python3 "$HOME/.claude/anytype/hook.py"
