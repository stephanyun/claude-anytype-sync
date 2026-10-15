#!/usr/bin/env python3
"""改 Anytype 上下文庫任一頁（唯一合法寫法；--dump → 改檔 → --patch）：

  doc_patch.py --dump  "<頁名關鍵字>" /tmp/x.md    # 抓下來成可編輯 markdown
  doc_patch.py --patch "<頁名關鍵字>" /tmp/x.md    # 送回去＋自動回讀驗證 H2 集合

兩個參數都要給（頁名加引號；第二個是檔案路徑，不是 stdout）。
dump 檔超過 30KB 別用 Read 整檔（25000 token 上限）：Read 時 limit ≤150 行，或
`grep -n '^## ' x.md` 找段落、`sed -n 'A,Bp' x.md` 只讀那段；改檔用 Edit／python 定位錨點插入。
--patch 回 HTTP 500 時立刻用同一份檔重送一次（大頁可能已被清空），dump 原稿不要先刪。
denorm／ensure_rule_breaks 與 spec-denorm.py 同一套（三個雷的說明見該檔）。
"""
import json, os, re, sys, urllib.request
# realpath 不是 abspath：部署目錄 ~/.claude/anytype/ 的這支是 symlink，
# 用 abspath 會去 symlink 旁邊找 md_clean，那裡沒有（只有 repo 裡有）。
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from md_clean import clean as md_clean

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
    exact = [h for h in hits if h[2] == keyword or h[2].endswith('｜' + keyword)]
    if len(exact) == 1:
        return exact[0]
    if len(hits) > 1:
        sys.exit('✗ 多筆命中，請給更精確的關鍵字：' + '、'.join(h[2] for h in hits))
    return hits[0]


def unescape(md):
    return re.sub(r'\\+([_*`\[\]#])', r'\1', md or '')


def denorm(md):
    """剝掉 Anytype GET 加上的一層渲染修飾。冪等。"""
    out = []
    for line in md.split('\n'):
        # lstrip：巢在清單底下的表格 GET 回來是縮排的（`    | <br>層 |`），
        # 只認行首 `|` 會漏掉它，第一輪留著 `<br>`、第二輪（md_clean 把縮排剝掉後）
        # 才剝，dump 的冪等自檢就會炸（FLEET 那頁 2026-10-01 實際撞到）。
        if line.lstrip().startswith('|') and not re.match(r'^\s*\|[:\-|\s]+\|?$', line):
            line = re.sub(r'(?:\s*<br>)+\s*\|', ' |', line)
            # 表格緊接在清單／段落後面時，GET 會在表頭第一格前塞 `<br>`；
            # 原樣寫回，Anytype 會把整張表攤平成一行文字多出來，每 round-trip 多一行。
            line = re.sub(r'\|\s*(?:<br>\s*)+', '| ', line)
            line = re.sub(r'\s{2,}', ' ', line)
        out.append(line.rstrip())
    md = '\n'.join(out)
    # `|` 的跳脫分兩種命運：表格列裡 `\|` 是**必要**的（拆掉那一格之後的內容會被
    # 吃掉），只把疊加的多層收回一層；表格外（inline code 裡）才還原成裸 `|`。
    md = '\n'.join(
        re.sub(r'\\+\|', r'\\|', l) if l.lstrip().startswith('|') else re.sub(r'\\+\|', '|', l)
        for l in md.split('\n'))
    return re.sub(r'\\([_*])', r'\1', md)


def ensure_rule_breaks(md):
    """`---` 前面若不是空行就補一行，否則它會把前一行吃成 setext H2。
    表格開頭同理：前一行非空且不是表格列就補空行（見 denorm 的 `<br>` 表頭說明）。"""
    out = []
    for line in md.split('\n'):
        if line.startswith('|') and out and out[-1].strip() and not out[-1].lstrip().startswith('|'):
            out.append('')
        if re.match(r'^\s*-{3,}\s*$', line):
            if out and out[-1].strip():
                out.append('')
            out.append(line.strip())
        else:
            out.append(line)
    return '\n'.join(out)


def utf16_compensate(md):
    """PATCH 前把 astral 字元（U+10000 以上，如 🔊，UTF-16 佔 2 單位）之後的行內標記
    （`code`、**粗體**）預先左移「該點之前的 astral 字元數」。
    2026-10-15 實測（API 2025-11-08）：Anytype 解析 markdown 時行內標記的位移是用
    UTF-16 code unit 算、套用時卻當 code point，所以同一行每個標記都右移「其前 astral 數」
    格（`cd` → c`d `、三個 emoji 後的 `op` → op `qq`），每次 dump→patch 再挪一次。
    md_clean 的 ASTRAL 對照表只救得了表上有的字，這裡是通用解。
    左移後若會跨過另一個 astral 字、反引號、星號或 `|`（表格格線）就放棄不動，維持舊行為。
    code fence 內不動。"""
    def astral_before(text, i):
        return sum(1 for c in text[:i] if ord(c) > 0xFFFF)
    def fix_line(line):
        if not any(ord(c) > 0xFFFF for c in line):
            return line
        # 拆成純文字 + 標記：(from, to, open, close)，座標是純文字的 code point 索引
        pat = re.compile(r'(`+)(.+?)\1|\*\*(.+?)\*\*')
        text, marks, pos = [], [], 0
        cur = 0
        for m in pat.finditer(line):
            text.append(line[pos:m.start()]); cur += m.start() - pos
            if m.group(1):
                inner, op = m.group(2), m.group(1)
            else:
                inner, op = m.group(3), '**'
            marks.append([cur, cur + len(inner), op, op])
            text.append(inner); cur += len(inner)
            pos = m.end()
        text.append(line[pos:])
        text = ''.join(text)
        if not marks:
            return line
        ins = []  # (純文字位置, 標記字串)；同一位置先關後開
        for f, t, op, cl in marks:
            nf, nt = f - astral_before(text, f), t - astral_before(text, t)
            ok = (nf + astral_before(text, nf) == f and nt + astral_before(text, nt) == t
                  and not re.search(r'[`*|]', text[nf:f] + text[nt:t]))
            # 粗體左移後若開頭接空白／結尾前是空白，CommonMark 不認它是強調（實測變成
            # 字面 \*\*），這種放棄不動；code span 沒這限制。
            if ok and op == '**' and (text[nf:nf + 1].isspace() or text[nt - 1:nt].isspace()):
                ok = False
            if not ok:
                nf, nt = f, t
            ins.append((nf, 1, op)); ins.append((nt, 0, cl))
        ins.sort(key=lambda x: (x[0], x[1]))
        out, pos = [], 0
        for at, _, s in ins:
            out.append(text[pos:at]); out.append(s); pos = at
        out.append(text[pos:])
        return ''.join(out)
    out, fence = [], False
    for line in md.split('\n'):
        if line.strip().startswith('```'):
            fence = not fence
            out.append(line); continue
        out.append(line if fence else fix_line(line))
    return '\n'.join(out)


def fetch(sid, oid):
    o = api('/spaces/%s/objects/%s?format=md' % (sid, oid))
    o = o.get('object', o)
    md = o.get('markdown') or ''
    if not md:
        sys.exit('✗ 頁面抓到了但 markdown 是空的')
    # denorm 只剝渲染修飾；md_clean 還做兩件 denorm 不做、但不做就會累積損壞的事：
    #   (a) 表格整列重寫（原樣回填頂層表格會整張散掉）
    #   (b) astral-plane emoji → BMP（🛑 這種 4-byte 字會讓同段的 ` 與 ** 每 round-trip 右移一格）
    # 見 memory anytype-api-doc-write-corrupts / anytype-patch-markdown-updates-body。
    return md_clean(denorm(unescape(md)))


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    mode = sys.argv[1]
    keyword, path = sys.argv[2], sys.argv[3]
    sid, oid, title = resolve(keyword)

    if mode == '--dump':
        base = fetch(sid, oid)
        if md_clean(denorm(base)) != base:
            sys.exit('✗ 清理不是冪等的')
        open(path, 'w', encoding='utf-8').write(base)
        nb = len(base.encode('utf-8'))
        print('✓ %s → %s（%dKB、%d 行）' % (title, path, nb // 1024, base.count('\n') + 1))
        if nb > 30000:
            print("   ⚠ 超過 Read 單次上限：Read 時 limit ≤150 行，或 grep -n '^## ' 找段再 sed -n 'A,Bp'")
        return

    if mode != '--patch':
        sys.exit(__doc__)

    new = ensure_rule_breaks(open(path, encoding='utf-8').read())
    # 送出的是補償過的版本，回讀比對仍用 new（補償只動標記位置，純文字不變）
    api('/spaces/%s/objects/%s' % (sid, oid), {'markdown': utf16_compensate(new)}, method='PATCH')
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


# 掛 __main__ 守衛，drift_check.py 才 import 得到 denorm／fetch 而不會跟著跑起來
if __name__ == '__main__':
    main()
