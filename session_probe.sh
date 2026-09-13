#!/bin/bash
# 新 session 端的 SOP 測試：驗 SessionStart hook、autoMemoryDirectory 回想、PostToolUse 自動 push。
# 用法：bash session_probe.sh   （約 1 分鐘，會自動清掉探針檔）
set -u
TS=$(date +%Y%m%d-%H%M%S); PROBE="zz-session-probe-$TS"
MEM="$HOME/.claude/memory"; IDX="$HOME/.claude/anytype/index.json"
WORKDIR=$(mktemp -d)   # 故意不在 $HOME、也不是 git repo root
ok=0; fail=0
chk(){ if [ "$1" = 1 ]; then echo "✓ $2"; ok=$((ok+1)); else echo "✗ $2 — $3"; fail=$((fail+1)); fi; }

BEFORE=$(stat -c %Y "$IDX" 2>/dev/null || stat -f %m "$IDX")
# ① SessionStart hook：開一個新 session，reconcile 應該碰過 manifest
ANS=$(cd "$WORKDIR" && claude -p "不要用任何工具，直接回答：碰到 JGB 合約或帳單，要先讀哪一個 memory 索引檔？只回檔名。" 2>&1 | tail -3)
AFTER=$(stat -c %Y "$IDX" 2>/dev/null || stat -f %m "$IDX")
[ "$AFTER" -ge "$BEFORE" ] && A=1 || A=0
chk "$A" "SessionStart hook 有跑 reconcile（index.json mtime $BEFORE → $AFTER）" "manifest 沒被碰過"
echo "$ANS" | grep -q "jgb__MEMORY.md" && B=1 || B=0
chk "$B" "在 $WORKDIR 這種非 \$HOME 目錄也回想得到 memory" "回答是：$ANS"

# ② PostToolUse hook：新 session 用 Write 寫一則 memory，應該自動推上 Anytype
cd "$WORKDIR" && claude -p "用 Write 工具建立檔案 $MEM/$PROBE.md，內容就是這段 frontmatter 加一行本文：
---
name: $PROBE
description: 新 session 的 PostToolUse 探針，跑完會刪
metadata:
  type: reference
---

SESSION-HOOK-MARKER $TS
寫完就結束，不要做別的事。" --permission-mode bypassPermissions --allowedTools Write >/dev/null 2>&1
sleep 3
cat > "$WORKDIR/check.py" <<'PY'
import json,sys,os,urllib.request
cfg=json.load(open(os.path.expanduser('~/.claude/anytype/config.json')))
H={'Authorization':'Bearer '+cfg['api_key'],'Anytype-Version':cfg['api_version'],'Content-Type':'application/json'}
r=urllib.request.Request(f"{cfg['api_base']}/spaces/{cfg['space_id']}/search?limit=100",method='POST',
    headers=H,data=json.dumps({"query":sys.argv[1]}).encode())
d=json.load(urllib.request.urlopen(r,timeout=60))
print('FOUND' if any(o.get('name')==sys.argv[1]+'.md' for o in d.get('data',[])) else 'MISSING')
PY
[ "$(python3 "$WORKDIR/check.py" "$PROBE")" = FOUND ] && C=1 || C=0
chk "$C" "新 session 用 Write 寫 memory → PostToolUse hook 自動推上 Anytype" "Anytype 找不到 $PROBE"

# 收尾：刪探針並把刪除傳播出去
rm -f "$MEM/$PROBE.md"; python3 "$HOME/.claude/anytype/sync.py" reconcile
rm -rf "$WORKDIR"
echo "----- ok=$ok fail=$fail"
[ "$fail" -eq 0 ] && echo "PASS：新 session 這一層符合 SOP" || echo "FAIL：見上面的 ✗"
