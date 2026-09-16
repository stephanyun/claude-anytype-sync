#!/usr/bin/env python3
"""
Claude Code <-> Anytype memory/document sync (two-way, consistency-preserving).

- Anytype "Claude Memory" space is the human-facing source of truth.
- Local files under the configured memory dirs are an auto-synced cache the
  harness reads. Each local memory file maps to one Anytype "page" object;
  verbatim content is stored inside a fenced code block so frontmatter
  round-trips byte-faithfully.

MULTI-DIRECTORY + NAMESPACING
  config.json "memory_dirs" maps namespace -> local dir, e.g.
    { "": "/.../-Users-abu/memory", "jgb": "/.../jgb-jgb/memory" }
  The page name on Anytype = the *remote name*:
    ns ""  -> "<fname>.md"            (bare; backward compatible with old pages)
    ns "x" -> "x__<fname>.md"         (namespaced; avoids cross-project clashes)
  All objects (every namespace) live in the one Claude Memory space, told apart
  by the "<ns>__" prefix. The separator is "__" (double underscore) so it never
  collides with a single-dash filename like "jgb-chinatrust-ec2.md" (ns "").
  Everything below is keyed by the *remote name*, which is globally unique.

Consistency model (reconcile): a manifest (index.json) records the last-synced
{id, hash} per remote name. A three-way compare (manifest vs local-now vs
anytype-now) distinguishes add / modify / delete on either side:
  - new on a side      -> propagate to the other side
  - modified           -> Anytype wins (source of truth); conflicts back up local
  - deleted on a side  -> delete on the other side
  - both modified, and the file is an INDEX file (MEMORY.md / <ns>__MEMORY.md)
    -> three-way line MERGE instead, then push the merge back so both sides
       converge. Index files are append-mostly (every machine adds its own
       pointer rows), so "one side wins" silently drops the other machine's
       rows. The last-synced text needed for the merge lives in basecache/.
A SAFETY GUARD aborts the whole reconcile if it would delete a suspicious bulk
(>=50% of tracked items, or either side reads empty while the manifest isn't) --
this prevents an API/FS glitch from wiping everything. Override with --force.

Commands:
  reconcile [--force]  two-way sync with delete + guard (used by SessionStart)
  push <file>          upsert one local file to Anytype
  push-all             upsert every memory file (all dirs)
  pull                 overwrite local from Anytype (no deletes)
  list                 list synced objects
  dirs                 show configured memory dirs + local file counts
  dedup [--apply]      report (or resolve) remote objects sharing one .md name
  search <q> [--body]  full-text search; prints each hit's namespace + local
                       path (so archived-out-of-recall memory stays findable)
  test                 round-trip self-test
"""
import json, os, re, sys, hashlib, difflib, urllib.request, urllib.error

CFG_PATH = os.path.expanduser("~/.claude/anytype/config.json")
INDEX_PATH = os.path.expanduser("~/.claude/anytype/index.json")
GUARD_LOG = os.path.expanduser("~/.claude/anytype/guard.log")
BACKUP_DIR = os.path.expanduser("~/.claude/anytype/conflict_backups")
BASECACHE_DIR = os.path.expanduser("~/.claude/anytype/basecache")
LEGACY_MEMORY_DIR = "/Users/abu/.claude/projects/-Users-abu/memory"
SEP = "__"  # namespace separator inside remote page names

FENCE = "`````"
HEADER = ("> 🔄 此頁由 Claude Code 自動與本機 memory 同步（Anytype 為權威來源）。\n"
          "> 編輯時請只改下方程式碼區塊內的內容，並保留最上方的 frontmatter。\n\n")


# ---------- config / http ----------
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
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode())


def space_id():
    return cfg()["space_id"]


# ---------- memory dirs / namespace mapping ----------
def memory_dirs():
    """Ordered list of (ns, abs_dir). ns '' = main (bare remote names)."""
    md = cfg().get("memory_dirs")
    if md:
        return [(ns, os.path.expanduser(p)) for ns, p in md.items()]
    return [("", LEGACY_MEMORY_DIR)]


def ns_list():
    return [ns for ns, _ in memory_dirs() if ns]


def dir_for_ns(ns):
    for n, p in memory_dirs():
        if n == ns:
            return p
    return LEGACY_MEMORY_DIR


def remote_name(ns, fname):
    return fname if not ns else ns + SEP + fname


def parse_remote_name(rname):
    """Remote page name -> (ns, fname). Only split on a registered ns prefix."""
    for ns in ns_list():
        if rname.startswith(ns + SEP):
            return ns, rname[len(ns) + len(SEP):]
    return "", rname


def local_path(rname):
    ns, fname = parse_remote_name(rname)
    return os.path.join(dir_for_ns(ns), fname)


def rname_for_path(path):
    """Absolute local file path -> remote name (namespaced by its dir)."""
    ap = os.path.abspath(path)
    for ns, d in memory_dirs():
        if ap.startswith(os.path.abspath(d) + os.sep):
            return remote_name(ns, os.path.basename(ap))
    return os.path.basename(ap)  # fallback: treat as main


# ---------- manifest ----------
def load_index():
    if os.path.exists(INDEX_PATH):
        with open(INDEX_PATH) as f:
            raw = json.load(f)
        if "items" in raw:
            return raw
        # migrate old flat {fname: id}
        return {"items": {k: {"id": v, "hash": None} for k, v in raw.items()}}
    return {"items": {}}


def save_index(idx):
    with open(INDEX_PATH, "w") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def norm(raw):
    """Normalize for comparison: drop trailing whitespace per line + edge blanks.
    (Anytype appends trailing spaces; this avoids false 'modified'.)"""
    if raw is None:
        return ""
    return "\n".join(l.rstrip() for l in raw.split("\n")).strip("\n")


def chash(raw):
    return hashlib.sha256(norm(raw).encode()).hexdigest()


# ---------- body wrap/unwrap ----------
def wrap(raw):
    return HEADER + FENCE + "\n" + raw + "\n" + FENCE + "\n"


def _is_fence(line):
    s = line.strip()
    return len(s) >= 3 and set(s) == {"`"}


def unwrap(markdown):
    if markdown is None:
        return ""
    lines = markdown.split("\n")
    start = end = None
    for i, ln in enumerate(lines):
        if _is_fence(ln):
            if start is None:
                start = i
            else:
                end = i
    if start is not None and end is not None and end > start:
        inner = lines[start + 1:end]
        text = "\n".join(l.rstrip(" ") for l in inner).replace("\\`", "`")
        return text.strip("\n") + "\n"
    return "\n".join(l.rstrip(" ") for l in lines).strip("\n") + "\n"


# ---------- remote/local readers ----------
def remote_list():
    # MUST paginate: /search caps at 100 per page. Missing pages would make
    # reconcile think a local file is remote-deleted and wrongly delete it.
    out, offset = [], 0
    while True:
        res = req("POST", f"/spaces/{space_id()}/search?limit=100&offset={offset}",
                  {"query": "", "types": ["page"]})
        data = res.get("data", [])
        out += data
        if not res.get("pagination", {}).get("has_more"):
            break
        offset += len(data) or 100
    # Exclude archived (soft-deleted/trashed) objects: Anytype's DELETE only
    # archives, and /search still returns archived pages. Without this filter a
    # deleted duplicate would get re-pulled (and could overwrite the live page
    # with stale content, since both share the same .md name).
    return [o for o in out if (o.get("name") or "").endswith(".md")
            and not o.get("archived")]


def remote_groups():
    """{remote_name: [{'id':.., 'raw':..}, ...]} — every live .md object,
    grouped by name so callers can see duplicates instead of losing them."""
    groups = {}
    for o in remote_list():
        full = req("GET", f"/spaces/{space_id()}/objects/{o['id']}")
        md = full.get("object", full).get("markdown", "")
        groups.setdefault(o["name"], []).append(
            {"id": o["id"], "raw": unwrap(md)})
    return groups


def remote_items():
    """{remote_name: {'id':.., 'raw':..}} for every .md page (fetches full md).

    Duplicate names are a data anomaly: Anytype lets two objects share one .md
    name, and since a body update is delete+recreate (PATCH can't touch body), a
    raced/crashed sync leaves two live objects for one name. The old code did
    out[name]=... so the LAST one in /search order silently won — order isn't
    stable, so reconcile flip-flopped between lineages every run (observed: one
    MEMORY.md had 5 live objects with divergent indexes). Now we pick a
    DETERMINISTIC winner (longest body = most complete, tie-broken by id) and
    LOUDLY log every collision to guard.log so a human can merge + run `dedup`.
    Winner choice never deletes anything; cleanup is the explicit `dedup` cmd."""
    out, dups = {}, {}
    for name, items in remote_groups().items():
        if len(items) == 1:
            out[name] = items[0]
            continue
        winner = max(items, key=lambda it: (len(it["raw"]), it["id"]))
        out[name] = winner
        dups[name] = {"kept": winner["id"],
                      "others": [it["id"] for it in items if it is not winner]}
    if dups:
        with open(GUARD_LOG, "a") as f:
            f.write("DUPLICATE remote names (kept longest, others untouched — "
                    "run `sync.py dedup` to review/merge): "
                    + json.dumps(dups, ensure_ascii=False) + "\n")
        print("⚠️  同名遠端物件："
              + "，".join(f"{n}×{len(d['others'])+1}" for n, d in dups.items())
              + "（已取最長者，其餘未動；跑 `sync.py dedup` 檢視）")
    return out


def local_items():
    """{remote_name: raw} for every .md across all configured dirs."""
    out = {}
    for ns, d in memory_dirs():
        if not os.path.isdir(d):
            continue
        for n in os.listdir(d):
            if n.endswith(".md"):
                with open(os.path.join(d, n)) as f:
                    out[remote_name(ns, n)] = f.read()
    return out


# ---------- primitive ops ----------
def remote_upsert(rname, raw, idx):
    """Create or update the Anytype page for remote name `rname`.

    Content updates go through PATCH {"markdown": ...}, which updates the body
    in place and keeps the object id. (The old note here said PATCH cannot touch
    a body — that was measured with the `body` field, which Anytype silently
    ignores; `markdown` works, verified 2026-07-20.)

    Updating in place is what stops duplicate-named objects from being born:
    the previous delete-then-recreate path is how one MEMORY.md became five
    live objects that took turns overwriting each other. If the PATCH does not
    round-trip (HTTP error, or the re-read body doesn't match), fall back to
    delete+recreate so an update never silently drops content.
    """
    new_hash = chash(raw)
    base_write(rname, raw)                   # both sides now agree on this text
    cur = idx["items"].get(rname)
    if cur and cur.get("id"):
        if cur.get("hash") == new_hash:
            return cur["id"]                 # unchanged -> skip
        oid = cur["id"]
        try:
            req("PATCH", f"/spaces/{space_id()}/objects/{oid}",
                {"markdown": wrap(raw)})
            full = req("GET", f"/spaces/{space_id()}/objects/{oid}")
            if chash(unwrap(full.get("object", full).get("markdown", ""))) == new_hash:
                idx["items"][rname] = {"id": oid, "hash": new_hash}
                set_project(oid, rname)      # tag can be dropped by edits; re-assert
                return oid
        except urllib.error.HTTPError:
            pass
        remote_delete(oid)                   # PATCH didn't take -> old path
    payload = {"type_key": "page", "name": rname, "body": wrap(raw)}
    res = req("POST", f"/spaces/{space_id()}/objects", payload)
    oid = res.get("object", res).get("id")
    idx["items"][rname] = {"id": oid, "hash": new_hash}
    set_project(oid, rname)              # group by namespace in Anytype
    return oid


def remote_delete(oid):
    try:
        req("DELETE", f"/spaces/{space_id()}/objects/{oid}")
    except urllib.error.HTTPError:
        pass


def dedup(apply=False):
    """Report (default) or resolve remote objects that share one .md name.

    Report mode lists each collision with per-object index/line counts so a
    human can decide. `--apply` keeps the longest body and archives the rest,
    then points the manifest at the survivor — the same safe resolution done by
    hand for the 5-way MEMORY.md split. It does NOT merge: if the objects have
    diverged (each holds unique lines), archiving loses data, so MERGE FIRST
    (edit the winner to hold the union) and only then run --apply."""
    groups = {n: its for n, its in remote_groups().items() if len(its) > 1}
    if not groups:
        print("無同名遠端物件 ✅")
        return
    idx = load_index()
    for name, items in groups.items():
        winner = max(items, key=lambda it: (len(it["raw"]), it["id"]))
        print(f"\n{name} — {len(items)} 個物件：")
        for it in items:
            lines = len([l for l in it["raw"].splitlines() if l.strip()])
            mark = "  ← 保留(最長)" if it is winner else ""
            print(f"  {it['id'][:20]}…  {len(it['raw']):6d} bytes  "
                  f"{lines:3d} 非空行{mark}")
        if apply:
            for it in items:
                if it is not winner:
                    remote_delete(it["id"])
            idx["items"][name] = {"id": winner["id"], "hash": chash(winner["raw"])}
            print(f"  → 已封存 {len(items)-1} 個，manifest 指向 {winner['id'][:20]}…")
    if apply:
        save_index(idx)
        print("\n⚠️  已套用。請跑 `sync.py reconcile` 確認收斂為全 0。")
    else:
        print("\n（僅報告。確認無資料歧異後，先合併 winner 再 `sync.py dedup --apply`）")


def local_write(rname, raw):
    p = local_path(rname)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(raw if raw.endswith("\n") else raw + "\n")


def local_delete(rname):
    p = local_path(rname)
    if os.path.exists(p):
        os.remove(p)


def backup_local(rname, raw):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(os.path.join(BACKUP_DIR, rname + ".local"), "w") as f:
        f.write(raw)


# ---------- base cache: the last text both sides agreed on ----------
# The manifest only keeps a hash, which answers "changed?" but not "changed
# from WHAT" -- and you cannot merge without the original. So every time the
# two sides agree on a text (push, pull, equal, successful merge) we drop that
# text here, keyed by remote name. Losing this dir is not fatal: the merge
# degrades to a union (see union_index).
def base_path(rname):
    return os.path.join(BASECACHE_DIR, rname)


def base_read(rname):
    p = base_path(rname)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return f.read()


def base_write(rname, raw):
    try:
        os.makedirs(BASECACHE_DIR, exist_ok=True)
        with open(base_path(rname), "w") as f:
            f.write(raw if raw.endswith("\n") else raw + "\n")
    except OSError as e:                      # cache only -- never fail a sync
        sys.stderr.write(f"base_write warn ({rname}): {e}\n")


def base_delete(rname):
    try:
        os.remove(base_path(rname))
    except OSError:
        pass


# ---------- three-way merge, index files only ----------
def is_index_file(rname):
    """MEMORY.md / <ns>__MEMORY.md -- the per-machine memory indexes.

    These are append-mostly: each machine writes a memory file AND adds one
    pointer row to the index, so two machines legitimately touch the same file
    between syncs. Taking one side whole loses the other's rows -- on
    2026-09-17 the 'macOS 系統代理' row disappeared that way (the memory file
    itself survived; nothing pointed at it any more). Everything else keeps the
    plain Anytype-wins rule: those files are written whole by one author."""
    return rname == "MEMORY.md" or rname.endswith(SEP + "MEMORY.md")


def _lines(raw):
    return norm(raw).split("\n")


def _text(lines):
    return "\n".join(lines).strip("\n") + "\n"


def _edit_script(base, side):
    """How `side` changed `base`, per base line.

    Returns (ins, rep):
      ins[i]  lines this side inserted *before* base line i (i == len(base)
              means appended at EOF)
      rep[i]  what base line i became: [base[i]] untouched, [] deleted, or the
              replacement lines.
    Per-base-line granularity is the point. Chunk-level diff3 calls "you
    appended a row / I appended a different row" a conflict, which is the one
    thing index files do constantly."""
    ins, rep = {}, [[l] for l in base]
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, base, side, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            ins.setdefault(i1, []).extend(side[j1:j2])
            continue
        rep[i1] = side[j1:j2] if tag == "replace" else []
        for i in range(i1 + 1, i2):
            rep[i] = []                       # region folded into rep[i1]
    return ins, rep


def _counts(lines):
    c = {}
    for l in lines:
        if l.strip():
            c[l] = c.get(l, 0) + 1
    return c


def _gained(kept, other):
    """How many non-blank lines survived in `kept` that `other` did not have."""
    co, n = _counts(other), 0
    for l in kept:
        if not l.strip():
            continue
        if co.get(l):
            co[l] -= 1
        else:
            n += 1
    return n


def merge_index(base, lraw, rraw):
    """Line-level three-way merge. Returns (text, stats).

    With a base we can tell apart the cases the old code confused:
      - a line only one side added   -> keep it (the other side never had it)
      - a line one side deleted      -> really delete it (do NOT resurrect it
                                        from the other side's stale copy)
      - the same line changed both ways -> a real conflict; Anytype wins, and
        the caller has already backed the local original up."""
    B, A, R = _lines(base), _lines(lraw), _lines(rraw)
    insA, repA = _edit_script(B, A)
    insR, repR = _edit_script(B, R)
    out, conflict = [], 0
    for i in range(len(B) + 1):
        add_r, add_a = insR.get(i, []), insA.get(i, [])
        out += add_r                          # Anytype's order leads
        out += [l for l in add_a if l not in add_r]   # both added it -> once
        if i == len(B):
            break
        ra, rr = repA[i], repR[i]
        if ra == [B[i]]:
            out += rr                         # only Anytype touched this line
        elif rr == [B[i]]:
            out += ra                         # only this machine touched it
        elif ra == rr:
            out += ra                         # same edit on both sides
        else:
            out += rr                         # real conflict -> Anytype wins
            conflict += max(len(ra), len(rr), 1)
    # a line lost to a conflict is already reported as a conflict; only count
    # the rest as "really deleted", or the message double-bills the same line
    dropped = max(0, _gained(A, out) + _gained(R, out) - conflict)
    return _text(out), {"from_local": _gained(out, R),
                        "from_remote": _gained(out, A),
                        "dropped": dropped, "conflict": conflict}


def union_index(lraw, rraw):
    """No base cached -> keep every line from both sides.

    Without the original we cannot tell "they added it" from "I deleted it",
    and wrongly deleting is the failure we are fixing, so union is the safe
    guess. Anytype's order leads; a local-only row is re-inserted next to the
    neighbours it had locally (not appended at the end) so it stays under the
    right heading. A local-only line that already appears somewhere in the
    Anytype copy is dropped -- it moved, it is not new."""
    A, R = _lines(lraw), _lines(rraw)
    rset = {l for l in R if l.strip()}
    out, recovered = [], 0

    def take_local(seg):
        nonlocal recovered
        for l in seg:
            if l.strip():
                if l in rset:
                    continue
                recovered += 1
            elif out and not out[-1].strip():
                continue                      # don't stack blank lines
            out.append(l)

    sm = difflib.SequenceMatcher(None, R, A, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("equal", "delete"):
            out += R[i1:i2]                   # shared, or Anytype-only
        elif tag == "insert":
            take_local(A[j1:j2])
        else:                                 # replace: keep both, Anytype first
            out += R[i1:i2]
            take_local(A[j1:j2])
    return _text(out), {"from_local": recovered, "from_remote": _gained(out, A),
                        "dropped": 0, "conflict": 0}


def merge_index_conflict(rname, lraw, rinfo, idx):
    """Both sides changed one index file: merge, write, and push the merge back
    so Anytype and every machine converge on the same text (writing only the
    local copy would just re-raise the same conflict on the next machine)."""
    base = base_read(rname)
    if base is None:
        merged, st = union_index(lraw, rinfo["raw"])
        how = "聯集（無 base 快取）"
    else:
        merged, st = merge_index(base, lraw, rinfo["raw"])
        how = "三方合併"
    # point the manifest at the object we actually read, so the push PATCHes
    # that one rather than a stale id from an earlier lineage
    idx["items"][rname] = {"id": rinfo["id"], "hash": chash(rinfo["raw"])}
    local_write(rname, merged)
    remote_upsert(rname, merged, idx)
    base_write(rname, merged)
    detail = (f"  ⇄ {rname} {how}：本機救回 {st['from_local']} 行、"
              f"Anytype 收進 {st['from_remote']} 行、"
              f"真衝突 {st['conflict']} 行")
    if st["dropped"]:
        detail += f"、確實刪除 {st['dropped']} 行"
    print(detail)
    if st["conflict"]:
        print(f"     ⚠️ 真衝突那 {st['conflict']} 行採 Anytype 版，"
              f"本機原稿在 conflict_backups/{rname}.local")
    return st


# ---------- group memories by namespace via the built-in "tag" property ----------
# The default Page Set view already shows a "Tag" column, so writing the built-in
# multi_select "tag" property makes the grouping visible with zero UI setup.
# Value = a single option named after the namespace ("" -> "main", "jgb" -> "jgb").
# NOTE: this overwrites the object's tag list; memory pages are auto-managed so
# they carry no other user tags. set_project runs on create/recreate + tag-all.
TAG_KEY = "tag"
_PCACHE = {}  # {'prop': id, 'opt:<name>': id}


def project_of(rname):
    ns, _ = parse_remote_name(rname)
    return ns if ns else "main"


def _get_all(path_base):
    out, offset = [], 0
    while True:
        res = req("GET", f"{path_base}?limit=100&offset={offset}")
        data = res.get("data", [])
        out += data
        if not res.get("pagination", {}).get("has_more"):
            break
        offset += len(data) or 100
    return out


def tag_prop_id():
    if "prop" in _PCACHE:
        return _PCACHE["prop"]
    for p in _get_all(f"/spaces/{space_id()}/properties"):
        if p.get("key") == TAG_KEY:
            _PCACHE["prop"] = p["id"]
            return p["id"]
    raise RuntimeError("built-in 'tag' property not found")


def tag_opt_id(name):
    ck = "opt:" + name
    if ck in _PCACHE:
        return _PCACHE[ck]
    prop = tag_prop_id()
    for t in _get_all(f"/spaces/{space_id()}/properties/{prop}/tags"):
        if t.get("name") == name:
            _PCACHE[ck] = t["id"]
            return t["id"]
    res = req("POST", f"/spaces/{space_id()}/properties/{prop}/tags",
              {"name": name, "color": "grey" if name == "main" else "blue"})
    _PCACHE[ck] = res.get("tag", res)["id"]
    return _PCACHE[ck]


def set_project(oid, rname):
    """Tag one object with the built-in tag = <namespace>. Fail-soft."""
    try:
        tid = tag_opt_id(project_of(rname))
        req("PATCH", f"/spaces/{space_id()}/objects/{oid}",
            {"properties": [{"key": TAG_KEY, "multi_select": [tid]}]})
        return True
    except Exception as e:
        sys.stderr.write(f"set_project warn ({rname}): {e}\n")
        return False


# ---------- simple commands ----------
def push_file(path):
    idx = load_index()
    with open(path) as f:
        raw = f.read()
    remote_upsert(rname_for_path(path), raw, idx)
    save_index(idx)
    print("pushed  ", rname_for_path(path))


def push_all():
    idx = load_index()
    for rname, raw in sorted(local_items().items()):
        remote_upsert(rname, raw, idx)
        print("pushed  ", rname)
    save_index(idx)


def pull():
    idx = load_index()
    for rname, info in remote_items().items():
        local_write(rname, info["raw"])
        base_write(rname, info["raw"])
        idx["items"][rname] = {"id": info["id"], "hash": chash(info["raw"])}
        print("pulled  ", rname)
    save_index(idx)


def list_objects():
    for o in remote_list():
        print(o.get("name"), "->", o.get("id"))


def show_dirs():
    for ns, d in memory_dirs():
        n = len([x for x in os.listdir(d) if x.endswith(".md")]) if os.path.isdir(d) else 0
        print(f"ns={ns!r:8} files={n:<4} {d}{'' if os.path.isdir(d) else '  (MISSING)'}")


def tag_all():
    """Backfill the project select tag onto every existing remote .md object."""
    counts = {}
    for o in remote_list():
        rname = o.get("name") or ""
        if set_project(o["id"], rname):
            p = project_of(rname)
            counts[p] = counts.get(p, 0) + 1
    print("tagged:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


# ---------- two-way reconcile with guard ----------
def reconcile(force=False):
    idx = load_index()
    manifest = idx["items"]
    L = local_items()                  # remote_name -> raw
    R = remote_items()                 # remote_name -> {id, raw}
    names = set(manifest) | set(L) | set(R)

    plan = {"push": [], "pull": [], "del_local": [], "del_remote": [],
            "conflict": [], "merge": []}
    for n in sorted(names):
        inL, inR, inM = n in L, n in R, n in manifest
        if inL and inR:
            lh, rh = chash(L[n]), chash(R[n]["raw"])
            if lh == rh:
                manifest[n] = {"id": R[n]["id"], "hash": lh}   # baseline hashes
                base_write(n, R[n]["raw"])   # agreed text = next merge's base
                continue
            mh = manifest[n].get("hash") if inM else None
            lchg = (mh is None) or (mh != lh)
            rchg = (mh is None) or (mh != rh)
            if rchg and not lchg:
                plan["pull"].append(n)
            elif lchg and not rchg:
                plan["push"].append(n)
            elif is_index_file(n):
                plan["merge"].append(n)           # both changed -> line merge
            else:
                plan["conflict"].append(n)        # both changed -> Anytype wins
        elif inL and not inR:
            plan["del_local"].append(n) if inM else plan["push"].append(n)
        elif inR and not inL:
            plan["del_remote"].append(n) if inM else plan["pull"].append(n)
        else:
            manifest.pop(n, None)                  # gone from both

    # ---- SAFETY GUARD ----
    tracked = len([n for n in manifest])
    dels = len(plan["del_local"]) + len(plan["del_remote"])
    any_dir = any(os.path.isdir(d) for _, d in memory_dirs())
    suspicious = []
    if manifest and not R:
        suspicious.append("Anytype 回傳空清單（疑似 API 故障）")
    if manifest and not L and any_dir:
        suspicious.append("本機 memory 全空（疑似 FS 問題）")
    if tracked >= 2 and dels >= max(1, (tracked + 1) // 2):
        suspicious.append(f"刪除量過大：{dels}/{tracked} 將被刪")
    if suspicious and not force:
        msg = "RECONCILE 中止（防呆）：" + "；".join(suspicious) + \
              f"。計畫: del_local={plan['del_local']} del_remote={plan['del_remote']}"
        with open(GUARD_LOG, "a") as f:
            f.write(msg + "\n")
        print(msg)
        print("如確認無誤，手動執行：python3 ~/.claude/anytype/sync.py reconcile --force")
        return

    # ---- APPLY ----
    for n in plan["pull"]:
        local_write(n, R[n]["raw"])
        base_write(n, R[n]["raw"])
        manifest[n] = {"id": R[n]["id"], "hash": chash(R[n]["raw"])}
    for n in plan["push"]:
        remote_upsert(n, L[n], idx)
    merge_stats = {}
    for n in plan["merge"]:
        backup_local(n, L[n])                      # keep local copy regardless
        merge_stats[n] = merge_index_conflict(n, L[n], R[n], idx)
    for n in plan["conflict"]:
        backup_local(n, L[n])                      # keep local copy before overwrite
        local_write(n, R[n]["raw"])
        manifest[n] = {"id": R[n]["id"], "hash": chash(R[n]["raw"])}
    for n in plan["del_local"]:
        local_delete(n); base_delete(n); manifest.pop(n, None)
    for n in plan["del_remote"]:
        remote_delete(manifest[n]["id"]); base_delete(n); manifest.pop(n, None)

    save_index(idx)
    print(f"reconcile: +push {len(plan['push'])}  +pull {len(plan['pull'])}  "
          f"-local {len(plan['del_local'])}  -remote {len(plan['del_remote'])}  "
          f"merge {len(plan['merge'])}  conflict {len(plan['conflict'])}")
    if plan["conflict"]:
        print("  衝突（兩邊都改，已採 Anytype 版，本機原稿備份於 conflict_backups/）:",
              plan["conflict"])


# ---------- self-test ----------
def selftest():
    idx = load_index()
    raw = ("---\nname: test-roundtrip\ndescription: 來回保真測試\n"
           "metadata:\n  type: feedback\n---\n\n使用者偏好繁中。連結 [[other]]。\n"
           "```js\ncode\n```\n**Why:** 測試。\n")
    oid = remote_upsert("test-roundtrip.md", raw, idx)
    full = req("GET", f"/spaces/{space_id()}/objects/{oid}")
    back = unwrap(full.get("object", full).get("markdown", ""))
    print("ROUND-TRIP:", "OK ✅" if norm(back) == norm(raw) else "MISMATCH ❌")
    # 更新路徑：改內容後必須「就地更新」——id 不變才不會生同名物件
    raw2 = raw.replace("使用者偏好繁中。", "使用者偏好繁中（已改）。")
    oid2 = remote_upsert("test-roundtrip.md", raw2, idx)
    full2 = req("GET", f"/spaces/{space_id()}/objects/{oid2}")
    back2 = unwrap(full2.get("object", full2).get("markdown", ""))
    print("UPDATE IN PLACE:", "OK ✅" if oid2 == oid else f"NEW OBJECT ❌ {oid}->{oid2}")
    print("UPDATED BODY:", "OK ✅" if norm(back2) == norm(raw2) else "MISMATCH ❌")
    remote_delete(oid)
    if oid2 != oid:
        remote_delete(oid2)
    idx["items"].pop("test-roundtrip.md", None)
    base_delete("test-roundtrip.md")
    save_index(idx)
    print("cleaned up")


def search(query, limit=10, show_body=False):
    """Full-text search the space and report where each hit lives locally.

    Exists so memory can be moved OUT of a recall directory (archived) and
    still be findable: the hit tells you the namespace and the exact local
    path, so you can read the file directly instead of round-tripping the API.
    Prints "(本機無檔)" when the object has no local mirror -- that means
    reconcile would try to del_remote it, which is worth knowing.
    """
    res = req("POST", f"/spaces/{space_id()}/search?limit={limit}",
              {"query": query, "types": ["page"]})
    hits = [o for o in res.get("data", [])
            if (o.get("name") or "").endswith(".md") and not o.get("archived")]
    if not hits:
        print(f"查無結果：{query}")
        return
    print(f"「{query}」→ {len(hits)} 筆\n")
    for o in hits:
        rname = o["name"]
        ns, _ = parse_remote_name(rname)
        path = local_path(rname)
        exists = os.path.exists(path)
        print(f"● {rname}")
        print(f"  ns={ns or '(main)'}  {path}" + ("" if exists else "   ⚠️(本機無檔)"))
        desc = ""
        if exists:
            # description lives in frontmatter; cheaper than refetching the body
            with open(path, encoding="utf8") as f:
                for line in f:
                    if line.startswith("description:"):
                        desc = line[12:].strip().strip('"')
                        break
        if not desc or show_body:
            full = req("GET", f"/spaces/{space_id()}/objects/{o['id']}")
            raw = unwrap(full.get("object", full).get("markdown", ""))
            if not desc:
                m = re.search(r"^description:\s*(.+)$", raw, re.M)
                desc = m.group(1).strip().strip('"') if m else ""
            if show_body:
                body = raw.split("---", 2)[-1].strip()
                print("  " + body[:400].replace("\n", "\n  ") + ("…" if len(body) > 400 else ""))
        if desc:
            print(f"  {desc[:160]}")
        print()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    force = "--force" in sys.argv
    if cmd == "reconcile": reconcile(force=force)
    elif cmd == "search":
        lim, terms, rest = 10, [], list(sys.argv[2:])
        while rest:
            a = rest.pop(0)
            if a == "--limit" and rest:
                lim = int(rest.pop(0))          # consume the value, not the query
            elif not a.startswith("--"):
                terms.append(a)
        if not terms:
            print("用法：sync.py search <關鍵字> [--body] [--limit N]")
            sys.exit(1)
        search(" ".join(terms), limit=lim, show_body="--body" in sys.argv)
    elif cmd == "push":     push_file(sys.argv[2])
    elif cmd == "push-all": push_all()
    elif cmd == "pull":     pull()
    elif cmd == "list":     list_objects()
    elif cmd == "dirs":     show_dirs()
    elif cmd == "tag-all":  tag_all()
    elif cmd == "dedup":    dedup(apply="--apply" in sys.argv)
    elif cmd == "test":     selftest()
    else:                   print(__doc__)
