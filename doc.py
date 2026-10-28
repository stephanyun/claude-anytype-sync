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

Anytype 讀回的 markdown 會把底線跳脫成 \\_，本工具已還原。
"""
import json, sys, urllib.request, re, os, time, hashlib, subprocess

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
c = json.load(open(CFG))
H = {'Authorization': 'Bearer ' + c['api_key'],
     'Anytype-Version': c['api_version'],
     'Content-Type': 'application/json'}
DOC_SPACES = ('上下文庫',)
BIG_BYTES = 8192          # 與 ~/.claude/hooks/landmine-gate.sh R16 同門檻（匯出檔 wc -c ≥ 8192）
CACHE_TTL = 2 * 3600      # 同 session 快取 2 小時
CACHE_DIR = '/tmp/doc-cache-%d' % os.getuid()


def api(path, data=None):
    r = urllib.request.Request(c['api_base'] + path, headers=H,
                               data=json.dumps(data).encode() if data else None)
    return json.load(urllib.request.urlopen(r, timeout=120))


def spaces():
    return {s.get('name'): s['id'] for s in api('/spaces?limit=50')['data'] if s.get('name')}


def pages(only=None):
    out = []
    for nm, sid in spaces().items():
        if nm not in DOC_SPACES or (only and nm != only):
            continue
        for o in api('/spaces/%s/search?limit=200' % sid, {"query": "", "types": ["page"]})['data']:
            out.append((nm, sid, o['id'], o.get('name', '')))
    return out


def unescape(md):
    # Anytype 存的是**雙**反斜線（`include\\_signature`），原本只吃一層、
    # 會留下 `include\_signature`，於是所有含底線的字（欄位名、函式名、檔名）
    # 用 grep 一律搜不到。2026-09-14 改成 `\\+` 一次吃光所有層。
    return re.sub(r'\\+([_*`\[\]#|])', r'\1', md or '')


def body(sid, oid):
    return unescape(api('/spaces/%s/objects/%s?format=md' % (sid, oid))['object'].get('markdown', ''))


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
