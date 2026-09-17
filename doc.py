#!/usr/bin/env python3
"""讀 Anytype 上「給人看的文件」（技術文件／開發專案／Stilla space）。

  doc.py list [space]        列出所有頁（或某個 space 的頁）
  doc.py read <頁名關鍵字>    印出整頁 markdown
  doc.py grep <關鍵字>        全 space 全文搜尋，印出命中的頁名與該行

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


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    arg = ' '.join(sys.argv[2:])
    if cmd == 'list':
        for nm, sid, oid, title in pages(arg or None):
            print('%-8s %s' % (nm, title))
    elif cmd == 'read':
        hits = [p for p in pages() if arg.lower() in p[3].lower()]
        if not hits:
            sys.exit('找不到含「%s」的頁' % arg)
        if len(hits) > 1:
            print('# 多筆命中：' + '、'.join(h[3] for h in hits) + '\n', file=sys.stderr)
        nm, sid, oid, title = hits[0]
        print('===== %s / %s =====' % (nm, title))
        print(body(sid, oid))
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
