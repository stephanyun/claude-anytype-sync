#!/usr/bin/env python3
"""讀 Anytype 上「給人看的文件」（技術文件／開發專案／Stilla space）。

  doc.py list [space]                 列出所有頁（或某個 space 的頁）
  doc.py toc  <頁名關鍵字>             只列標題（## / ###）與每段字元數，先看再挑段
  doc.py read <頁名關鍵字> [段落關鍵字]  印整頁；給段落關鍵字就只印標題命中的那幾段
  doc.py grep <關鍵字>                 全 space 全文搜尋，印出命中的頁名與該行

大頁（上下文庫很多頁 3 萬～11 萬字元，整頁＝1.5 萬～6 萬 token）一律 toc → read 段落，
別整頁讀；段落關鍵字可給多個（空白分隔，任一命中就印）。
2026-10-29 起 read 不帶段落關鍵字且頁 ≥8KB（UTF-8 位元組，與 landmine R16 同門檻）
時不再整頁倒，改印 toc＋一行提示，exit 0——整頁 ≥8KB 的唯一出口是讀段落。

同 session 快取（2026-10-29）：同一個 claude session 第二次 read 同一頁同一組段落，
且內容沒變，只印一行提示＋各段字數，不重印內容（內容已在這個 session 的 context 裡，
重印只是再燒一次 token）；要重印加 --fresh。快取放 /tmp/doc-cache-<uid>/，2 小時過期。
session 鍵取 CLAUDE_CODE_SESSION_ID，沒有就 CLAUDE_PID，再沒有就沿 PPID 鏈找 claude 程序；
三者都沒有（人在終端機手打）就不快取、每次都印。

頁面內容磁碟快取（2026-11-11）：grep 每次要抓全部頁（60 多頁、共 1.5MB），Anytype 閒的時候 1 秒，
但它正在被寫（別的 session 在 PATCH 大頁、sync.py 在推）或機器滿載時每個請求都排隊，實測一次 grep 60～69 秒。
現在每頁內容存在 ~/.claude/anytype/doccache/<頁 id>.json，鍵是清單 API 回的 last_modified_date：
每次仍打一次清單（1 個請求）拿各頁最新的修改時間，時間沒變的頁直接用快取，變了的才重抓那一頁。
  - 修改時間只到秒，而 PATCH 是「先刪再塞」兩步：抓的時間離修改時間不到 60 秒的那份不信任，下次照樣重抓。
  - doc_patch.py 寫完（不論成敗）會刪掉那一頁的快取；空內容不存。
  - 清單 API 4 秒沒回（Anytype 忙）而本機有上一次的清單時，改用上一次的清單＋快取，並在 stderr 講明是幾秒前的；
    沒有上一次的清單就照舊等到回來為止。
  - --fresh 或環境變數 DOC_CACHE=0：完全不讀快取、不用舊清單（抓回來的照樣寫進快取）。

Anytype 讀回的 markdown 會把底線跳脫成 \\_，本工具已還原。
"""
import json, sys, urllib.request, urllib.error, re, os, time, hashlib, subprocess, calendar

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
c = json.load(open(CFG))
H = {'Authorization': 'Bearer ' + c['api_key'],
     'Anytype-Version': c['api_version'],
     'Content-Type': 'application/json'}
DOC_SPACES = ('上下文庫',)
BIG_BYTES = 8192          # 與 ~/.claude/hooks/landmine-gate.sh R16 同門檻（匯出檔 wc -c ≥ 8192）
CACHE_TTL = 2 * 3600      # 同 session 快取 2 小時
CACHE_DIR = '/tmp/doc-cache-%d' % os.getuid()


DOC_CACHE = os.path.join(os.path.dirname(CFG), 'doccache')   # 頁面內容磁碟快取（頁面可能含個資：目錄 700、檔 600）
PAGES_FILE = '_pages.json'   # 上一次成功拿到的清單（Anytype 忙的時候退回來用）
SETTLE = 60               # 抓的時間離頁面修改時間不到這麼多秒的快取不信任（修改時間只到秒；PATCH 不是原子的）
BUSY_TIMEOUT = 4          # 有舊清單可退時，清單 API 最多等幾秒
LM = {}                   # 頁 id → 這次清單回的 last_modified_date（body() 拿來當快取鍵）
STALE = False             # 這次的清單是不是退回舊的


def api(path, data=None, timeout=120):
    r = urllib.request.Request(c['api_base'] + path, headers=H,
                               data=json.dumps(data).encode() if data else None)
    return json.load(urllib.request.urlopen(r, timeout=timeout))


def spaces(timeout=120):
    return {s.get('name'): s['id'] for s in api('/spaces?limit=50', timeout=timeout)['data'] if s.get('name')}


def cache_on():
    return not FRESH and os.environ.get('DOC_CACHE', '1') != '0'


def _load(name):
    try:
        with open(os.path.join(DOC_CACHE, name), encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _save(name, obj):
    """原子寫入；寫不進去不影響讀。"""
    try:
        os.makedirs(DOC_CACHE, mode=0o700, exist_ok=True)
        tmp = os.path.join(DOC_CACHE, '.%s.%d.tmp' % (name, os.getpid()))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, os.path.join(DOC_CACHE, name))
    except Exception:
        pass


def _last_modified(o):
    for p in o.get('properties') or []:
        if p.get('key') == 'last_modified_date':
            return p.get('date') or ''
    return ''


def _epoch(iso):
    try:
        return calendar.timegm(time.strptime(iso[:19], '%Y-%m-%dT%H:%M:%S'))
    except Exception:
        return None


def _fetch_pages(timeout):
    out = []
    for nm, sid in spaces(timeout).items():
        if nm not in DOC_SPACES:
            continue
        for o in api('/spaces/%s/search?limit=200' % sid, {"query": "", "types": ["page"]}, timeout=timeout)['data']:
            out.append([nm, sid, o['id'], o.get('name', ''), _last_modified(o)])
    return out


def pages(only=None):
    global STALE
    old = _load(PAGES_FILE) if cache_on() else None
    try:
        rows = _fetch_pages(BUSY_TIMEOUT if old else 120)
        STALE = False
        _save(PAGES_FILE, {'at': time.time(), 'pages': rows})
        # 已不在清單上的頁（封存／刪除）把快取清掉，免得越積越多
        try:
            keep = set(r[2] + '.json' for r in rows) | {PAGES_FILE}
            for f in os.listdir(DOC_CACHE):
                if f.endswith('.json') and f not in keep:
                    os.unlink(os.path.join(DOC_CACHE, f))
        except OSError:
            pass
    except OSError as exc:      # 逾時、連線被拒（URLError／TimeoutError 都是 OSError）
        if not old or isinstance(exc, urllib.error.HTTPError):
            raise
        rows = old['pages']
        STALE = True
        print('# ⚠ Anytype %d 秒內沒回應（%s），改用 %d 秒前的清單與快取；要等到最新的加 --fresh'
              % (BUSY_TIMEOUT, type(exc).__name__, time.time() - old.get('at', 0)), file=sys.stderr)
    LM.clear()
    LM.update((r[2], r[4]) for r in rows)
    return [tuple(r[:4]) for r in rows if not only or r[0] == only]


def unescape(md):
    # Anytype 存的是**雙**反斜線（`include\\_signature`），原本只吃一層、
    # 會留下 `include\_signature`，於是所有含底線的字（欄位名、函式名、檔名）
    # 用 grep 一律搜不到。2026-09-14 改成 `\\+` 一次吃光所有層。
    return re.sub(r'\\+([_*`\[\]#|])', r'\1', md or '')


def fetch_body(sid, oid):
    return unescape(api('/spaces/%s/objects/%s?format=md' % (sid, oid))['object'].get('markdown', ''))


def body(sid, oid):
    """頁面內容；清單回的修改時間跟快取那份一樣就不打 API（規則見檔頭「頁面內容磁碟快取」）。"""
    lm = LM.get(oid) or ''
    if lm and cache_on():
        d = _load(oid + '.json')
        if d and d.get('lm') == lm and d.get('md'):
            t = _epoch(lm)
            if STALE or (t is not None and d.get('at', 0) - t >= SETTLE):
                return d['md']
    at = time.time()            # 抓之前記時間：抓的途中被改，下次會因為離修改時間太近而重抓
    md = fetch_body(sid, oid)
    if lm and md and not STALE:
        _save(oid + '.json', {'lm': lm, 'at': at, 'md': md})
    return md


def sections(md):
    """切成 (層級, 標題行, 起始行, 結束行)；H2 段含底下的 H3，H3 段到下一個 H2/H3 為止。"""
    lines = md.split('\n')
    heads = [(2 if l.startswith('## ') else 3, i, l.strip())
             for i, l in enumerate(lines) if l.startswith('## ') or l.startswith('### ')]
    out = []
    for n, (lvl, i, h) in enumerate(heads):
        end = len(lines)
        for lvl2, j, _ in heads[n + 1:]:
            if lvl2 <= lvl:
                end = j
                break
        out.append((lvl, h, i, end))
    return out


def print_toc(md):
    for lvl, head, a, b in sections(md):
        print('%s%s  (%d 字)' % ('    ' if lvl == 3 else '', head, len('\n'.join(md.split('\n')[a:b]))))


# ── 同 session 快取 ──────────────────────────────────────────────────────────
def session_key():
    """哪個 claude session 在讀。環境變數優先（claude 給 Bash 子程序帶 CLAUDE_CODE_SESSION_ID／CLAUDE_PID），
    都沒有就沿 PPID 鏈往上找 comm 是 claude 的程序；找不到回 None（不快取）。
    subagent 有自己的 CLAUDE_CODE_SESSION_ID，所以 subagent 跟母 session 各算各的——它的 context 是空的，理應重印。"""
    for k in ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID'):
        v = os.environ.get(k, '').strip()
        if v:
            return k[0] + ':' + v
    pid = os.getppid()
    for _ in range(12):
        if pid <= 1:
            break
        try:
            out = subprocess.run(['ps', '-o', 'ppid=,comm=', '-p', str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.split()
        except Exception:
            break
        if len(out) < 2:
            break
        ppid, comm = out[0], out[1]
        if os.path.basename(comm) == 'claude':
            return 'ppid:' + str(pid)
        pid = int(ppid)
    return None


def cache_path(sess, title, secs):
    raw = '\0'.join([sess, title] + sorted(s.lower() for s in secs))
    return os.path.join(CACHE_DIR, hashlib.sha1(raw.encode('utf-8')).hexdigest() + '.json')


def cache_get(path, digest):
    """同 session、2 小時內、內容 sha 相同才算命中；內容變了就當沒讀過（要重印新內容）。"""
    try:
        st = os.stat(path)
        if time.time() - st.st_mtime > CACHE_TTL:
            return None
        d = json.load(open(path))
        return d if d.get('sha') == digest else None
    except Exception:
        return None


def cache_put(path, title, secs, digest, nchar):
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        tmp = path + '.tmp'
        json.dump({'title': title, 'secs': secs, 'sha': digest, 'nchar': nchar, 'ts': time.time()},
                  open(tmp, 'w'), ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass  # 快取寫不進去不影響讀


def emit(title, secs, chunks):
    """印 chunks（[(標題, 內文)]）；同 session 讀過同一組且內容沒變就只印提示＋字數。"""
    text = '\n'.join(t for _, t in chunks)
    digest = hashlib.sha1(text.encode('utf-8')).hexdigest()
    sess = None if FRESH else session_key()
    path = cache_path(sess, title, secs) if sess else None
    if path and cache_get(path, digest):
        print('（本 session 已讀過這段，內容同上次；要強制重抓加 --fresh）')
        for h, t in chunks:
            print('%s  (%d 字)' % (h, len(t)))
        return
    for _, t in chunks:
        print(t)
    if sess:
        cache_put(cache_path(sess, title, secs), title, secs, digest, len(text))


FRESH = False


def main():
    global FRESH
    argv = [a for a in sys.argv[1:] if a != '--fresh']
    FRESH = len(argv) != len(sys.argv) - 1
    cmd = argv[0] if argv else 'list'
    arg = ' '.join(argv[1:])
    if cmd == 'list':
        for nm, sid, oid, title in pages(arg or None):
            print('%-8s %s' % (nm, title))
    elif cmd in ('read', 'toc'):
        # 頁名只吃 argv[1]（請加引號）；argv[2:] 是段落關鍵字。
        key = argv[1] if len(argv) > 1 else ''
        secs = argv[2:]
        hits = [p for p in pages() if key.lower() in p[3].lower()]
        if not hits:
            sys.exit('找不到含「%s」的頁' % key)
        if len(hits) > 1:
            print('# 多筆命中：' + '、'.join(h[3] for h in hits) + '\n', file=sys.stderr)
        nm, sid, oid, title = hits[0]
        md = body(sid, oid)
        print('===== %s / %s =====' % (nm, title))
        if cmd == 'toc':
            print_toc(md)
            return
        if not secs:
            # 2026-10-08 對話挖礦：整頁讀是 token 浪費大戶（單頁 30 天最多 13.7 萬 token），
            # 整頁 >5000 字時在 stdout 最前面印一行提醒（stderr 常被 2>/dev/null 吃掉）。
            # 2026-10-29 再挖：提醒沒用（R16 30 天仍擋 22 次＝先撞牆才改），≥8KB 直接改印 toc。
            nchar = len(md)
            nbytes = len(md.encode('utf-8'))
            if nbytes >= BIG_BYTES:
                print('# 整頁 %d 字（約 %d token），不整頁印；下面是段落目錄，用 doc.py read "%s" <段落關鍵字>（可給多個）讀要的段'
                      % (nchar, nbytes * 2 // 3, title))
                print_toc(md)
                return
            if nchar > 5000:
                print('# ⚠ 整頁 %d 字（約 %d token）；建議先 doc.py toc "%s" 再 doc.py read "%s" <段落關鍵字>'
                      % (nchar, nbytes * 2 // 3, title, title))
            emit(title, [], [('(整頁)', md)])
            return
        lines = md.split('\n')
        picked = [(h, a, b) for lvl, h, a, b in sections(md)
                  if lvl == 2 and any(k.lower() in h.lower() for k in secs)]
        if not picked:  # H2 沒中再找 H3
            picked = [(h, a, b) for lvl, h, a, b in sections(md)
                      if lvl == 3 and any(k.lower() in h.lower() for k in secs)]
        if not picked:
            sys.exit('沒有標題含「%s」的段落；先 doc.py toc "%s" 看有哪些段' % ('／'.join(secs), title))
        emit(title, secs, [(h, '\n'.join(lines[a:b]).rstrip() + '\n') for h, a, b in picked])
    elif cmd == 'grep':
        for nm, sid, oid, title in pages():
            md = body(sid, oid)
            for i, line in enumerate(md.split('\n')):
                if arg.lower() in line.lower():
                    print('%s / %s:%d: %s' % (nm, title, i + 1, line.strip()[:160]))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
