#!/usr/bin/env python3
"""掃「上下文庫」全庫，找 Anytype round-trip 累積出來的 markdown 損壞。

  drift_check.py            # 掃一遍，只印「不在基準線裡的新問題」；有新問題 exit 1
  drift_check.py --all      # 連已接受的誤判一起印出來
  drift_check.py --accept   # 把目前所有命中收進基準線（確認過都是誤判才跑）

為什麼需要它：Anytype 的 PATCH 會把行內標記的位置重算，只要寫回的 markdown
有一點不合它的胃口，`**` 與反引號就會整組往右挪一格，而且**每寫一次挪一次**。
歪掉的行不會報錯、只會愈來愈難讀（`ro`le_email_logs 是`真正的大戶`）。
2026-09-17 全庫盤查時一次修掉 400 多行，這支是為了不要再累積到那個量。

六類檢查（前四類是損壞，後兩類是結構）：
  1. 行內標記漂移 —— inline code／粗體的「內容」頭尾沾到空白、`****` 殘骸、
     `` `**X**` ``（粗體掉進 code span）、`**` 數量是奇數、code span 裡混進中文
  2. 表格 cell 的 `<br>` 疊加（原樣回填會逐輪多一顆）
  3. 多層跳脫 `\\_` `\\*` `\\|`
  4. 表格欄數不一致／缺分隔列
  5. code fence 不成對、亂碼字元、HTML 實體殘骸
  6. 頁面抓得到、而且 denorm+md_clean 是冪等的（doc_patch --dump 的自檢）

基準線 drift_baseline.json 記的是「確認過是誤判」的行（例如 `monday ` 的尾空白
是真的觸發語法、PHP 的 `/**`、Python 的 `**extra`、git status 的 ` M`）。
比對用整行文字，所以那一行一旦被改動就會重新浮出來給人看一次。
"""
import json, os, re, sys

# 兩條路徑都要：doc.py 靠 abspath 找同目錄的 config.json（只有部署目錄
# ~/.claude/anytype/ 有），md_clean／doc_patch 只在 repo 裡。所以本檔要從
# 部署目錄的 symlink 執行：~/.claude/anytype/drift_check.py
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doc
# 用 doc_patch 自己的 unescape：doc.py 那支為了好讀會把 `\|` 也還原成裸 `|`，
# 拿來檢查會讓表格 cell 裡的跳脫直線被誤判成欄位分隔。
from doc_patch import denorm, unescape
from md_clean import clean as md_clean

BASELINE = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'drift_baseline.json')
C_IN_CODE = re.compile(r'`\*\*[^`]*\*\*`')


def mark_flags(line):
    """回傳這一行命中的漂移類型代碼；空字串＝乾淨。"""
    code = line.split('`')
    a = any(p != p.strip() or not p for i, p in enumerate(code) if i % 2 == 1)
    bold = line.split('**')
    e = any(p != p.strip() for i, p in enumerate(bold) if i % 2 == 1 and p)
    # 漂移最常見的形狀是 `ro`le_email_logs 是`真正的大戶` —— 反引號被推進字中間。
    # 兩個很安靜的判準：反引號兩側都是英數字（正常寫法不可能）；
    # 或 code span 內容裡出現全形標點（識別字與指令不會有逗號、括號、引號）。
    f = bool(re.search(r'[A-Za-z0-9]`[A-Za-z0-9]', line)) or any(
        re.search(r'[，。、；：（）「」『』！？]', p)
        for i, p in enumerate(code) if i % 2 == 1)
    return ''.join(t for t, hit in (
        ('A', a),                       # code span 內容頭尾沾空白
        ('B', '****' in line),          # 空的粗體殘骸
        ('C', C_IN_CODE.search(line)),  # 粗體掉進 code span
        ('D', line.count('**') % 2),    # 粗體標記落單
        ('E', e),                       # 粗體內容頭尾沾空白
        ('F', f),                       # code span 裡混進中文
    ) if hit)


def scan_page(title, md):
    """回傳 [(類型, 行號, 整行), ...]。類型 MARK/BR/ESC/TABLE/FENCE。"""
    out, lines, fence = [], md.split('\n'), False
    for i, line in enumerate(lines, 1):
        if line.strip().startswith('```'):
            fence = not fence
            continue
        if fence:
            continue
        f = mark_flags(line)
        if f:
            out.append(('MARK:' + f, i, line))
        if re.search(r'<br>\s*<br>', line):
            out.append(('BR', i, line))
        if re.search(r'\\{2,}[_*|]', line):
            out.append(('ESC', i, line))
    i = 0
    while i < len(lines):
        if lines[i].startswith('|'):
            j = i
            while j < len(lines) and lines[j].startswith('|'):
                j += 1
            rows = lines[i:j]
            cols = {len(re.split(r'(?<!\\)\|', r)) for r in rows}
            if len(cols) > 1:
                out.append(('TABLE', i + 1, '欄數不一 %s：%s' % (sorted(cols), rows[0][:80])))
            if len(rows) > 1 and not re.match(r'^\|[\s:\-|]+\|$', rows[1].strip()):
                out.append(('TABLE', i + 1, '缺分隔列：%s' % rows[0][:80]))
            i = j
        else:
            i += 1
    if md.count('```') % 2:
        out.append(('FENCE', 0, '``` 不成對'))
    if '\ufffd' in md:
        out.append(('FENCE', 0, '有亂碼字元'))
    if re.search(r'&(gt|lt|amp);', md):
        out.append(('FENCE', 0, 'HTML 實體殘骸'))
    return out


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode not in ('', '--all', '--accept'):
        sys.exit(__doc__)
    base = set()
    if os.path.exists(BASELINE) and mode != '--accept':
        base = {tuple(x) for x in json.load(open(BASELINE, encoding='utf-8'))['accepted']}

    pages = doc.pages()
    if not pages:
        sys.exit('✗ 一頁都列不到（Anytype 沒開，或 API token 失效）')

    findings, accepted, broken = [], 0, []
    for _, sid, oid, title in sorted(pages, key=lambda p: p[3]):
        try:
            raw = doc.api('/spaces/%s/objects/%s?format=md' % (sid, oid))['object'].get('markdown', '')
            md = md_clean(denorm(unescape(raw)))
            if md_clean(denorm(md)) != md:      # doc_patch --dump 的同一道冪等自檢
                broken.append('%s：清理不冪等，寫回會壞' % title)
        except Exception as exc:
            broken.append('%s：抓不下來（%s）' % (title, exc))
            continue
        for kind, ln, line in scan_page(title, md):
            key = (title, kind.split(':')[0], line.strip())
            if key in base and mode != '--all':
                accepted += 1
                continue
            findings.append((title, kind, ln, line.strip()))

    if mode == '--accept':
        keys = sorted({(t, k.split(':')[0], l) for t, k, _, l in findings})
        json.dump({'accepted': [list(x) for x in keys]},
                  open(BASELINE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('✓ 基準線已更新：%d 筆（%d 頁）' % (len(keys), len(pages)))
        return

    for title, kind, ln, line in findings:
        print('[%s] %s L%s\n    %s' % (kind, title, ln or '-', line[:160]))
    for b in broken:
        print('[BROKEN] %s' % b)
    print('\n掃 %d 頁：新問題 %d、已接受的誤判 %d、抓不動或不冪等 %d'
          % (len(pages), len(findings), accepted, len(broken)))
    sys.exit(1 if findings or broken else 0)


if __name__ == '__main__':
    main()
