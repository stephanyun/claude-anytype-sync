#!/usr/bin/env python3
"""通用版的 Anytype 文件寫回（上下文庫任一頁）。

  doc_patch.py --dump  <頁名關鍵字> <out.md>   # 抓下來並去渲染成可編輯 markdown
  doc_patch.py --patch <頁名關鍵字> <in.md>    # 送回去並自動回讀驗證

跟 spec-denorm.py 同一套 denorm／ensure_rule_breaks（那三個雷的說明見該檔），
差別只有兩點：任一頁都能用（依頁名關鍵字解析 object id），
以及回讀驗證改成「比對送出的 H2 集合」而不是寫死 `## 一、` 格式。
"""
import json, os, re, sys, urllib.request

CFG = json.load(open(os.path.expanduser('~/.claude/anytype/config.json')))
SPACE_PREF = ('上下文庫', '技術文件')


def api(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        CFG['api_base'] + path, data=data, method=method or ('POST' if body else 'GET'),
        headers={'Authorization': 'Bearer ' + CFG['api_key'],
                 'Anytype-Version': CFG['api_version'],
                 'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req, timeout=120))


def resolve(keyword):
    spaces = [s for s in api('/spaces?limit=100')['data'] if s.get('name')]
    spaces.sort(key=lambda s: SPACE_PREF.index(s['name']) if s['name'] in SPACE_PREF else len(SPACE_PREF))
    hits = []
    for sp in spaces:
        if sp['name'] not in SPACE_PREF:
            continue
        for o in api('/spaces/%s/search?limit=200' % sp['id'], {"query": "", "types": ["page"]})['data']:
            if keyword.lower() in (o.get('name') or '').lower():
                hits.append((sp['id'], o['id'], o.get('name')))
    if not hits:
        sys.exit('✗ 找不到含「%s」的頁' % keyword)
    if len(hits) > 1:
        sys.exit('✗ 多筆命中，請給更精確的關鍵字：' + '、'.join(h[2] for h in hits))
    return hits[0]


def unescape(md):
    return re.sub(r'\\+([_*`\[\]#])', r'\1', md or '')


def denorm(md):
    """剝掉 Anytype GET 加上的一層渲染修飾。冪等。"""
    out = []
    for line in md.split('\n'):
        if line.startswith('|') and not re.match(r'^\|[:\-|\s]+\|?$', line):
            line = re.sub(r'(?:\s*<br>)+\s*\|', ' |', line)
            line = re.sub(r'\s{2,}', ' ', line)
        out.append(line.rstrip())
    md = '\n'.join(out)
    md = md.replace('\\\\|', '|').replace('\\|', '|')
    return re.sub(r'\\([_*])', r'\1', md)


def ensure_rule_breaks(md):
    """`---` 前面若不是空行就補一行，否則它會把前一行吃成 setext H2。"""
    out = []
    for line in md.split('\n'):
        if re.match(r'^\s*-{3,}\s*$', line):
            if out and out[-1].strip():
                out.append('')
            out.append(line.strip())
        else:
            out.append(line)
    return '\n'.join(out)


def fetch(sid, oid):
    o = api('/spaces/%s/objects/%s?format=md' % (sid, oid))
    o = o.get('object', o)
    md = o.get('markdown') or ''
    if not md:
        sys.exit('✗ 頁面抓到了但 markdown 是空的')
    return denorm(unescape(md))


def main():
    mode = sys.argv[1]
    keyword, path = sys.argv[2], sys.argv[3]
    sid, oid, title = resolve(keyword)

    if mode == '--dump':
        base = fetch(sid, oid)
        if denorm(base) != base:
            sys.exit('✗ denorm 不是冪等的')
        open(path, 'w', encoding='utf-8').write(base)
        print('✓ %s → %s（%d bytes）' % (title, path, len(base)))
        return

    if mode != '--patch':
        sys.exit(__doc__)

    new = ensure_rule_breaks(open(path, encoding='utf-8').read())
    api('/spaces/%s/objects/%s' % (sid, oid), {'markdown': new}, method='PATCH')
    back = fetch(sid, oid)
    back_lines = set(l.strip() for l in back.split('\n'))
    lost = [l for l in new.split('\n') if l.strip() and l.strip() not in back_lines
            and not l.lstrip().startswith('|')]
    # Anytype 會把標題裡的 `inline code` 與 **粗體** 標記吃掉，比對前先正規化，
    # 否則每個帶反引號的標題都會被誤報成「被 --- 吃掉的 H2」。
    norm = lambda s: re.sub(r'[`*_]', '', s).strip()
    sent_h2 = set(norm(l) for l in new.split('\n') if l.startswith('## '))
    bad = [l for l in back.split('\n') if l.startswith('## ') and norm(l) not in sent_h2]
    print('✓ %s 已 PATCH：回讀缺行 %d、多出的 H2 %d' % (title, len(lost), len(bad)))
    for l in lost[:5]:
        print('   缺:', l[:90])
    for l in bad[:5]:
        print('   ⚠ 多:', l[:90])
    if bad:
        sys.exit('✗ 有標題被 --- 吃掉，修正後重跑')


main()
