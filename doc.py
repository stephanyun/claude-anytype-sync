#!/usr/bin/env python3
"""讀 Anytype 上「給人看的文件」（技術文件／開發專案／Stilla space）。

  doc.py list [space]                 列出所有頁（或某個 space 的頁）
  doc.py toc  <頁名關鍵字>             只列標題（## / ###）與每段字元數，先看再挑段
  doc.py read <頁名關鍵字> [段落關鍵字]  印整頁；給段落關鍵字就只印標題命中的那幾段
  doc.py grep <關鍵字>                 全 space 全文搜尋，印出命中的頁名與該行

大頁（上下文庫很多頁 3 萬～11 萬字元，整頁＝1.5 萬～6 萬 token）一律 toc → read 段落，
別整頁讀；段落關鍵字可給多個（空白分隔，任一命中就印）。

Anytype 讀回的 markdown 會把底線跳脫成 \\_，本工具已還原。
"""
import json, sys, urllib.request, re, os

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
c = json.load(open(CFG))
H = {'Authorization': 'Bearer ' + c['api_key'],
     'Anytype-Version': c['api_version'],
     'Content-Type': 'application/json'}
DOC_SPACES = ('上下文庫',)


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


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    arg = ' '.join(sys.argv[2:])
    if cmd == 'list':
        for nm, sid, oid, title in pages(arg or None):
            print('%-8s %s' % (nm, title))
    elif cmd in ('read', 'toc'):
        # 頁名只吃 argv[2]（請加引號）；argv[3:] 是段落關鍵字。
        key = sys.argv[2] if len(sys.argv) > 2 else ''
        secs = sys.argv[3:]
        hits = [p for p in pages() if key.lower() in p[3].lower()]
        if not hits:
            sys.exit('找不到含「%s」的頁' % key)
        if len(hits) > 1:
            print('# 多筆命中：' + '、'.join(h[3] for h in hits) + '\n', file=sys.stderr)
        nm, sid, oid, title = hits[0]
        md = body(sid, oid)
        print('===== %s / %s =====' % (nm, title))
        if cmd == 'toc':
            for lvl, head, a, b in sections(md):
                print('%s%s  (%d 字)' % ('    ' if lvl == 3 else '', head, len('\n'.join(md.split('\n')[a:b]))))
            return
        if not secs:
            # 2026-10-08 對話挖礦：整頁讀是 token 浪費大戶（單頁 30 天最多 13.7 萬 token），
            # 整頁 >5000 字時在 stdout 最前面印一行提醒（stderr 常被 2>/dev/null 吃掉）。
            nchar = len(md)
            if nchar > 5000:
                print('# ⚠ 整頁 %d 字（約 %d token）；建議先 doc.py toc "%s" 再 doc.py read "%s" <段落關鍵字>'
                      % (nchar, len(md.encode('utf-8')) * 2 // 3, title, title))
            print(md)
            return
        lines = md.split('\n')
        picked = [(h, a, b) for lvl, h, a, b in sections(md)
                  if lvl == 2 and any(k.lower() in h.lower() for k in secs)]
        if not picked:  # H2 沒中再找 H3
            picked = [(h, a, b) for lvl, h, a, b in sections(md)
                      if lvl == 3 and any(k.lower() in h.lower() for k in secs)]
        if not picked:
            sys.exit('沒有標題含「%s」的段落；先 doc.py toc "%s" 看有哪些段' % ('／'.join(secs), title))
        for h, a, b in picked:
            print('\n'.join(lines[a:b]).rstrip() + '\n')
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
