#!/usr/bin/env python3
"""全機隊稽核：比對每台機器與《Claude memory／skills 多機 Anytype 同步機制》的結構規定。

用法：python3 fleet_audit.py [--hosts "a b c"]
從 controller（任一台有全機 ssh 的機器）跑；唯讀，不改任何東西。
任何一項 FAIL → exit 1。
"""
import json, subprocess, sys, os, urllib.request, socket
from collections import Counter

HOSTS = os.environ.get("FLEET_HOSTS",
    "m1-16 m2-13 cc-abu-c xz-pve-aide m2-mini i5-13 xz-stilla").split()
if "--hosts" in sys.argv:
    HOSTS = sys.argv[sys.argv.index("--hosts") + 1].split()

CFG = json.load(open(os.path.expanduser("~/.claude/anytype/config.json")))
H = {"Authorization": "Bearer " + CFG["api_key"], "Anytype-Version": CFG["api_version"],
     "Content-Type": "application/json"}

def anytype_all(space_id):
    out, off = [], 0
    while True:
        r = urllib.request.Request(f'{CFG["api_base"]}/spaces/{space_id}/search?limit=100&offset={off}',
                                   method="POST", headers=H, data=json.dumps({"query": ""}).encode())
        d = json.load(urllib.request.urlopen(r, timeout=60))
        out += d.get("data", [])
        if len(d.get("data", [])) < 100:
            return out
        off += 100

REMOTE = r'''
import json, os, glob, hashlib, subprocess
h = os.path.expanduser
C = h("~/.claude")
def j(p):
    try: return json.load(open(p))
    except Exception: return {}
cfg = j(C + "/anytype/config.json")
o = {"cfg_ok": bool(cfg), "api_base": cfg.get("api_base"), "skills_ns": cfg.get("skills_ns"),
     "memory_dirs": cfg.get("memory_dirs"), "space_id": cfg.get("space_id"),
     "skills_space_id": cfg.get("skills_space_id")}
try: o["cfg_mode"] = oct(os.stat(C + "/anytype/config.json").st_mode & 0o777)[2:]
except Exception: o["cfg_mode"] = None
amd, hk = None, {"reconcile": 0, "skills": 0, "post": 0}
for f in ("settings.json", "settings.local.json"):
    d = j(C + "/" + f)
    if isinstance(d, dict):
        amd = d.get("autoMemoryDirectory") or amd
        s = json.dumps(d.get("hooks", {}), ensure_ascii=False)
        hk["reconcile"] |= int("sync.py reconcile" in s)
        hk["skills"] |= int("skills_catalog.py sync" in s)
        hk["post"] |= int("hook.py" in s and "Write|Edit|MultiEdit" in s)
o["auto_memory_dir"], o["hooks"] = amd, hk
m = h("~/.claude/memory")
names = [os.path.basename(x) for x in glob.glob(m + "/*.md")] if os.path.isdir(m) else []
o["mem_total"] = len(names)
for p in ("jgb__", "stilla__", "arch__"):
    o["mem_" + p.strip("_")] = len([x for x in names if x.startswith(p)])
o["index_json"] = os.path.exists(C + "/anytype/index.json")
o["idx"] = {i: (os.path.getsize(m + "/" + i) if os.path.exists(m + "/" + i) else None)
            for i in ("MEMORY.md", "jgb__MEMORY.md", "stilla__MEMORY.md")}
stale = []
for d in glob.glob(h("~/.claude/projects/*/memory")):
    try:
        if os.listdir(d): stale.append(os.path.basename(os.path.dirname(d)))
    except OSError: stale.append("BROKEN:" + os.path.basename(os.path.dirname(d)))
o["stale_dirs"] = stale
o["mirror_dirs"] = len(glob.glob(h("~/.claude/mirror-*")))
o["md5"] = {}
for s in ("sync.py", "hook.py", "skills_catalog.py"):
    p = C + "/anytype/" + s
    o["md5"][s] = hashlib.md5(open(p, "rb").read()).hexdigest()[:8] if os.path.exists(p) else None
port = (cfg.get("api_base") or "::0/").split(":")[2].split("/")[0]
try:
    o["api_http"] = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "8",
        "http://127.0.0.1:%s/v1/spaces" % port], capture_output=True, text=True).stdout.strip()
except Exception as e: o["api_http"] = "ERR"
def sh(c):
    try: return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    except Exception: return ""
o["headless"] = bool(sh("command -v loginctl"))
if o["headless"]:
    o["linger"] = sh('loginctl show-user "$(whoami)" -p Linger').split("=")[-1]
    o["svc"] = sh("systemctl --user is-active anytype")
    o["svc_enabled"] = sh("systemctl --user is-enabled anytype")
    o["healthcheck"] = sh("systemctl --user is-active anytype-healthcheck.timer")
o["hostname"] = sh("hostname -s")
print("###JSON###" + json.dumps(o))
'''

def collect(host):
    local = host == socket.gethostname().split(".")[0]
    cmd = ["python3", "-c", REMOTE] if local else \
          ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, "python3 -c " + json_quote(REMOTE)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    for line in p.stdout.splitlines():
        if line.startswith("###JSON###"):
            return json.loads(line[len("###JSON###"):])
    return {"error": (p.stderr or p.stdout)[-200:]}

def json_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"

def main():
    mem = anytype_all(CFG["space_id"])
    skills = anytype_all(CFG["skills_space_id"])
    mem_names = [o.get("name", "") for o in mem]
    truth = {"total": len(mem_names),
             "jgb": len([n for n in mem_names if n.startswith("jgb__")]),
             "stilla": len([n for n in mem_names if n.startswith("stilla__")]),
             "arch": len([n for n in mem_names if n.startswith("arch__")])}
    ns_hist = Counter(o.get("name", "").split("__")[0] for o in skills)
    print(f'Anytype 權威：memory {truth["total"]} 則（jgb {truth["jgb"]}／stilla {truth["stilla"]}'
          f'／arch {truth["arch"]}）、skills {len(skills)} 頁 / {len(ns_hist)} 個 ns')

    data = {h: collect(h) for h in HOSTS}
    md5_votes = Counter(json.dumps(d.get("md5")) for d in data.values() if d.get("md5"))
    canon_md5 = json.loads(md5_votes.most_common(1)[0][0]) if md5_votes else {}
    idx_votes = Counter(json.dumps(d.get("idx")) for d in data.values() if d.get("idx"))
    canon_idx = json.loads(idx_votes.most_common(1)[0][0]) if idx_votes else {}
    ns_seen, fails = Counter(), []

    for host, d in data.items():
        errs = []
        if d.get("error"):
            print(f"✗ {host}: 連不上 / 取不到資料 — {d['error']}"); fails.append(host); continue
        ns = d.get("skills_ns")
        ns_seen[ns] += 1
        if not ns: errs.append("skills_ns 未設")
        elif ns not in ns_hist: errs.append(f"skills_ns {ns} 在 Anytype 沒有任何頁")
        if d.get("cfg_mode") != "600": errs.append(f"config.json 權限 {d.get('cfg_mode')} 不是 600")
        if d.get("space_id") != CFG["space_id"]: errs.append("space_id 不符")
        if d.get("skills_space_id") != CFG["skills_space_id"]: errs.append("skills_space_id 不符")
        md = d.get("memory_dirs") or {}
        if list(md.keys()) != [""] or not str(list(md.values())[0]).rstrip("/").endswith(".claude/memory"):
            errs.append(f"memory_dirs 不是單一 ~/.claude/memory：{md}")
        if not d.get("auto_memory_dir"): errs.append("settings 缺 autoMemoryDirectory")
        for k, v in (d.get("hooks") or {}).items():
            if not v: errs.append(f"hook 缺 {k}")
        if d.get("mem_total") != truth["total"]:
            errs.append(f'memory {d.get("mem_total")} 則 ≠ Anytype {truth["total"]} 則')
        if d.get("mem_jgb") != truth["jgb"] or d.get("mem_stilla") != truth["stilla"]:
            errs.append("jgb__／stilla__ 檔數與 Anytype 不符")
        if d.get("mem_arch"): errs.append(f'arch__ 殘留 {d["mem_arch"]} 則')
        if d.get("idx") != canon_idx: errs.append(f'索引檔大小與多數機不同：{d.get("idx")}')
        if d.get("stale_dirs"): errs.append("projects 殘留 memory：" + ",".join(d["stale_dirs"][:3]))
        if d.get("mirror_dirs"): errs.append("還有 mirror-* 目錄")
        if not d.get("index_json"): errs.append("缺 index.json manifest")
        if d.get("md5") != canon_md5: errs.append(f'腳本 md5 與多數機不同：{d.get("md5")}')
        if d.get("api_http") in ("000", "ERR", ""): errs.append(f'Anytype API 不通（{d.get("api_http")}）')
        if d.get("headless"):
            if d.get("linger") != "yes": errs.append("linger 沒開（重開機後不自啟）")
            if d.get("svc") != "active" or d.get("svc_enabled") != "enabled": errs.append("anytype service 非 enabled+active")
            if d.get("healthcheck") != "active": errs.append("缺 anytype-healthcheck.timer")
        if errs:
            fails.append(host)
            print(f"✗ {host}（{d.get('hostname')}）")
            for e in errs: print("    -", e)
        else:
            print(f'✓ {host}（{d.get("hostname")}）ns={ns} mem={d.get("mem_total")} api={d.get("api_http")}')

    dup = [n for n, c in ns_seen.items() if c > 1 and n]
    if dup:
        print("✗ skills_ns 重複：" + ",".join(dup) + "（會互刪對方的 skill 頁）"); fails.append("ns")
    orphan = set(ns_hist) - set(ns_seen)
    if orphan: print("⚠ Anytype skills 有孤兒 ns（機器已不在名單）：" + ",".join(sorted(orphan)))
    if truth["arch"]: print(f'⚠ Anytype 仍有 {truth["arch"]} 個 arch__ 物件（v5 起應為 0）')
    print(("FAIL：" + ",".join(sorted(set(fails)))) if fails else f"PASS：{len(HOSTS)} 台全數符合 SOP")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
