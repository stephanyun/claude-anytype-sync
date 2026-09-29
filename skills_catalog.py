#!/usr/bin/env python3
"""One-way, read-only mirror of Claude Code skills -> Anytype "Claude Skills".

Skills are plugin-managed (overwritten on update), so local stays authoritative.
This only PUSHES each SKILL.md into Anytype as a browsable catalog. It never
pulls back. Run on demand / at SessionStart to refresh the catalog.

MULTI-MACHINE NAMESPACING
  Multiple machines share ONE "Claude Skills" space. Each machine tags its
  catalog objects with config "skills_ns" (e.g. "mac", "ccabuc"):
    title = "<skills_ns>__<plugin>__<skill>.SKILL.md"
  Prune is SCOPED to the machine's own namespace, so machines never delete each
  other's skills (without this, two machines with different skill sets would
  ping-pong-delete each other's unique skills). If skills_ns is unset, behaves
  legacy (no prefix, prunes every .SKILL.md) -- single-machine mode only.

  python3 skills_catalog.py sync     # push/refresh this machine's skill docs
  python3 skills_catalog.py list     # list catalog objects in Anytype
"""
import json, os, sys, glob, urllib.request, urllib.error

CFG_PATH = os.path.expanduser("~/.claude/anytype/config.json")
INDEX_PATH = os.path.expanduser("~/.claude/anytype/skills_index.json")
# 只鏡像自己的 skill（私人／外包／公司三個 repo 接進 ~/.claude/skills 的 symlink）。
# 第三方（gstack、官方 plugin 快取）不進目錄：內容在上游，鏡像七份只是噪音（abu 決定 2026-09-29）。
SEARCH_ROOTS = [os.path.expanduser("~/.claude/skills")]
THIRD_PARTY_MARKERS = ("/gstack/", "/.gstack/", "/plugins/")
OWN_MARKERS = ("/skills-personal/", "/jgb-skills/", "/claude-skills")
FENCE = "`````"


def cfg():
    with open(CFG_PATH) as f:
        return json.load(f)


def req(method, path, body=None):
    c = cfg()
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(c["api_base"] + path, data=data, method=method)
    r.add_header("Authorization", "Bearer " + c["api_key"])
    r.add_header("Anytype-Version", c["api_version"])
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP {e.code} {method} {path}: {e.read().decode()[:200]}\n")
        raise


def space():
    return cfg()["skills_space_id"]


def skills_ns():
    return cfg().get("skills_ns", "") or ""


def title_for(name):
    ns = skills_ns()
    return (f"{ns}__{name}.SKILL.md") if ns else (f"{name}.SKILL.md")


def in_my_ns(title):
    """True if this catalog object belongs to THIS machine's namespace."""
    if not title.endswith(".SKILL.md"):
        return False
    ns = skills_ns()
    return title.startswith(ns + "__") if ns else True  # legacy: own everything


def load_index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as f:
            return json.load(f)
    return {}


def save_index(idx):
    with open(INDEX_PATH, "w") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def skill_files():
    """Return list of (display_name, abspath) for every SKILL.md found."""
    out = {}
    for root in SEARCH_ROOTS:
        for p in glob.glob(os.path.join(root, "**", "SKILL.md"), recursive=True):
            real = os.path.realpath(p)
            if any(m in real for m in THIRD_PARTY_MARKERS) or any(m in p for m in THIRD_PARTY_MARKERS):
                continue
            # 自己的 skill 一律是 install.sh 從三個 repo 接進來的 symlink；
            # 直接躺在 ~/.claude/skills 底下的實體目錄都是第三方（gstack 子指令等）。
            if real == os.path.abspath(p) and not any(m in real for m in OWN_MARKERS):
                continue
            # Name as <plugin>__<skill>: plugin = dir right above the "skills/" dir.
            parts = p.split(os.sep)
            skill_dir = parts[-2] if len(parts) >= 2 else "skill"
            plugin = ""
            if "skills" in parts:
                i = parts.index("skills")
                if i > 0:
                    plugin = parts[i - 1]
            if plugin in ("", ".claude"):
                plugin = "user"
            name = f"{plugin}__{skill_dir}"
            out[name] = p  # unique per (plugin, skill)
    return sorted(out.items())


def _hash(s):
    import hashlib
    norm = "\n".join(l.rstrip() for l in s.split("\n")).strip("\n")
    return hashlib.sha256(norm.encode()).hexdigest()


def push(name, path):
    with open(path) as f:
        raw = f.read()
    rel = path.replace(os.path.expanduser("~"), "~")
    ns = skills_ns()
    machine = f"（機器：{ns}）" if ns else ""
    header = (f"> 📖 唯讀鏡像：Claude Code skill 目錄{machine}。權威來源在本機 plugin，請勿在此編輯。\n"
              f"> 本機路徑：`{rel}`\n\n")
    body = header + FENCE + "\n" + raw + "\n" + FENCE + "\n"
    idx = load_index()
    title = title_for(name)
    h = _hash(raw)
    cur = idx.get(title)
    # idx values may be legacy str (id) or new dict {id, hash}.
    cur_id = cur.get("id") if isinstance(cur, dict) else cur
    cur_hash = cur.get("hash") if isinstance(cur, dict) else None
    if cur_id and cur_hash == h:
        return "skipped", title                 # unchanged
    # Anytype PATCH can't update body -> delete + recreate to refresh content.
    if cur_id:
        try:
            req("DELETE", f"/spaces/{space()}/objects/{cur_id}")
        except urllib.error.HTTPError:
            pass
    payload = {"type_key": "page", "name": title, "body": body}
    res = req("POST", f"/spaces/{space()}/objects", payload)
    idx[title] = {"id": res.get("object", res).get("id"), "hash": h}
    save_index(idx)
    return ("updated" if cur_id else "created"), title


def all_objects():
    # Paginate: /search caps at 100/page. With multiple machines the shared
    # catalog exceeds 100, so a single page would miss objects (and a scoped
    # prune could then fail to remove this machine's stale entries).
    out, offset = [], 0
    while True:
        res = req("POST", f"/spaces/{space()}/search?limit=100&offset={offset}",
                  {"query": "", "types": ["page"]})
        data = res.get("data", [])
        out += data
        if not res.get("pagination", {}).get("has_more"):
            break
        offset += len(data) or 100
    # DELETE only archives objects; filter so archived pages aren't reprocessed.
    return [o for o in out if not o.get("archived")]


def sync():
    """Push/refresh this machine's skills, then prune ONLY this machine's
    namespace objects whose skill is gone. Other machines' namespaces untouched.
    """
    print(f"skills_ns = {skills_ns()!r}")
    files = skill_files()
    print(f"found {len(files)} skill(s)")
    current = set()
    for name, path in files:
        action, title = push(name, path)
        current.add(title)
        if action != "skipped":
            print(f"{action:8} {title}")

    # Prune: delete catalog objects in MY namespace not backed by a local skill.
    idx = load_index()
    pruned = 0
    for o in all_objects():
        title = o.get("name", "")
        if in_my_ns(title) and title not in current:
            try:
                req("DELETE", f"/spaces/{space()}/objects/{o['id']}")
                idx.pop(title, None)
                pruned += 1
                print(f"deleted  {title}")
            except urllib.error.HTTPError:
                pass
    save_index(idx)
    print(f"done: {len(current)} synced, {pruned} pruned (ns={skills_ns()!r} consistent)")


def list_objects():
    for o in all_objects():
        print(o.get("name"), "->", o.get("id"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    if cmd == "sync":   sync()
    elif cmd == "list": list_objects()
    else:               print(__doc__)
