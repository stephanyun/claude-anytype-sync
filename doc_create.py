#!/usr/bin/env python3
"""在「上下文庫」新建一頁（給人看的文件）。之後的修改一律走 doc_patch.py。

  doc_create.py "<頁名>" <in.md>

- 頁名要照命名規約 `<範圍>｜<主題>`（例：CORE｜開發流程健檢）。
- 同名頁已存在就拒絕（避免同名物件互相覆蓋，見 sync.py 的三類靜默損壞）。
- 送出前先過 md_clean（astral emoji → BMP、表格整列重寫），跟 doc_patch 同一套；
  建好之後回讀比對 H2 數，不一致就印出來給人看。
"""
import json, os, sys, urllib.request
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from md_clean import clean as md_clean
from doc_patch import ensure_rule_breaks

CFG = json.load(open(os.path.expanduser('~/.claude/anytype/config.json')))
SPACE = '上下文庫'


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        CFG['api_base'] + path, data=data, method=method or ('POST' if body else 'GET'),
        headers={'Authorization': 'Bearer ' + CFG['api_key'],
                 'Anytype-Version': CFG['api_version'],
                 'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=60))


def h2s(md):
    return [l for l in md.split('\n') if l.startswith('## ')]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    name, path = sys.argv[1], sys.argv[2]
    sid = next((s['id'] for s in api('/spaces?limit=100')['data'] if s.get('name') == SPACE), None)
    if not sid:
        sys.exit('✗ 找不到 space「%s」' % SPACE)
    for o in api('/spaces/%s/search?limit=200' % sid, {"query": "", "types": ["page"]})['data']:
        if (o.get('name') or '') == name:
            sys.exit('✗ 同名頁已存在（%s），改用 doc_patch.py' % o['id'])
    md = ensure_rule_breaks(md_clean(open(path, encoding='utf-8').read()))
    res = api('/spaces/%s/objects' % sid, {"type_key": "page", "name": name, "body": md})
    oid = res.get('object', res).get('id')
    back = api('/spaces/%s/objects/%s?format=md' % (sid, oid)).get('object', {}).get('markdown', '')
    a, b = h2s(md), h2s(back)
    print('✓ 建好 %s（%s）：送出 %d 行／%d 個 H2，回讀 %d 行／%d 個 H2' % (name, oid, md.count('\n') + 1, len(a), back.count('\n') + 1, len(b)))
    if len(a) != len(b):
        print('⚠ H2 數不一致，用 doc.py read 檢查')


if __name__ == '__main__':
    main()
