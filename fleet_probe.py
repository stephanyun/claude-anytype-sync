#!/usr/bin/env python3
"""端到端傳播探針：證明 memory 真的在跑雙向同步，而不只是設定長得對。

在本機寫一則探針 memory → push → 驗 Anytype 有物件 → 指定 peer 跑 reconcile 後檔案要出現
→ 改內容再驗一次 → 刪除並驗兩邊都消失。全程只動這一則以 zz-probe- 開頭的檔案。

用法：python3 fleet_probe.py [--peers "m2-13 cc-abu-c"]
"""
import json, os, subprocess, sys, time, urllib.request

PEERS = os.environ.get("PROBE_PEERS", "m2-13 cc-abu-c").split()
if "--peers" in sys.argv:
    PEERS = sys.argv[sys.argv.index("--peers") + 1].split()

CFG = json.load(open(os.path.expanduser("~/.claude/anytype/config.json")))
H = {"Authorization": "Bearer " + CFG["api_key"], "Anytype-Version": CFG["api_version"],
     "Content-Type": "application/json"}
MEM = os.path.expanduser("~/.claude/memory")
SYNC = os.path.expanduser("~/.claude/anytype/sync.py")
stamp = time.strftime("%Y%m%d-%H%M%S")
NAME = f"zz-probe-{stamp}"
FILE = f"{MEM}/{NAME}.md"
results = []

def step(ok, label, detail=""):
    results.append(ok)
    print(("✓ " if ok else "✗ ") + label + (f" — {detail}" if detail else ""))

def remote_objects():
    out, off = [], 0
    while True:
        r = urllib.request.Request(f'{CFG["api_base"]}/spaces/{CFG["space_id"]}/search?limit=100&offset={off}',
                                   method="POST", headers=H, data=json.dumps({"query": ""}).encode())
        d = json.load(urllib.request.urlopen(r, timeout=60))
        out += d.get("data", [])
        if len(d.get("data", [])) < 100:
            return out
        off += 100

def remote_has(name):
    return any(o.get("name") == name + ".md" for o in remote_objects())

def local(*args):
    return subprocess.run(["python3", SYNC, *args], capture_output=True, text=True, timeout=600).stdout.strip()

def peer(host, cmd):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, cmd],
                          capture_output=True, text=True, timeout=600).stdout.strip()

body = f"""---
name: {NAME}
description: 同步機制端到端探針，跑完會自動刪除
metadata:
  type: reference
---

探針時間 {stamp}。MARKER-CREATE
"""
try:
    open(FILE, "w").write(body)
    print(local("push", FILE) or "(push done)")
    step(remote_has(NAME), "本機寫入 → Anytype 出現物件")

    for h in PEERS:
        peer(h, "python3 ~/.claude/anytype/sync.py reconcile")
        got = peer(h, f'grep -c MARKER-CREATE ~/.claude/memory/{NAME}.md 2>/dev/null || echo 0')
        step(got.strip() not in ("", "0"), f"傳播到 {h}", f"grep 命中 {got}")

    open(FILE, "a").write("\nMARKER-UPDATE 第二輪\n")
    local("push", FILE)
    time.sleep(2)
    for h in PEERS:
        peer(h, "python3 ~/.claude/anytype/sync.py reconcile")
        got = peer(h, f'grep -c MARKER-UPDATE ~/.claude/memory/{NAME}.md 2>/dev/null || echo 0')
        step(got.strip() not in ("", "0"), f"內容更新傳播到 {h}", f"grep 命中 {got}")
finally:
    if os.path.exists(FILE):
        os.remove(FILE)
    out = local("reconcile")
    print(out)
    step(not remote_has(NAME), "本機刪除 → Anytype 物件消失")
    for h in PEERS:
        peer(h, "python3 ~/.claude/anytype/sync.py reconcile")
        gone = peer(h, f'test -e ~/.claude/memory/{NAME}.md && echo STILL || echo GONE')
        step(gone.strip() == "GONE", f"刪除傳播到 {h}", gone)

print("PASS：端到端同步正常" if all(results) else "FAIL：見上面的 ✗")
sys.exit(0 if all(results) else 1)
