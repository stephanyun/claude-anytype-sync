#!/usr/bin/env python3
"""把 Anytype「上下文庫」所有頁匯成 markdown（每頁一檔），當作災難還原備份。

  anytype_export.py [輸出目錄]        預設 ~/.claude/memory-git/anytype-export/

- 檔名安全化：頁名去掉 / \\ : * ? " < > | 與控制字元，前後空白修掉；同名加 ~2。
- 每檔開頭 YAML frontmatter：title／id／tags／created／modified／exported。
- 遮蔽：長串 token、password=／密碼： 後的值、AKIA…、sk-…、ghp_…、github_pat_…、xox?-…
  一律換成 [REDACTED]。有命中的頁名印在 stdout（只印頁名，不印值）。
- 輸出目錄裡「不在本次清單」的 .md 會刪掉（頁被改名或刪除時舊檔不留）；git 歷史是還原網。
- 只讀 Anytype，不寫任何物件。memory 類物件不在此匯（memory_git_sync.sh 已把 ~/.claude/memory 快照進同一個 repo）。
"""
import json, os, re, sys, urllib.request

CFG = os.path.expanduser('~/.claude/anytype/config.json')
c = json.load(open(CFG))
H = {'Authorization': 'Bearer ' + c['api_key'],
     'Anytype-Version': c['api_version'],
     'Content-Type': 'application/json'}
DOC_SPACES = ('上下文庫',)
OUT = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else '~/.claude/memory-git/anytype-export')

# 遮蔽規則：(名稱, 正則, 替換)。值只認 ASCII 串（≥8 字、不是 $變數／<佔位>／[REDACTED]），
# 碰到空白、反引號、引號、括號、中文標點就停，避免把整行吃掉或誤傷中文正文。
# 2026-10-08：排除路徑（/ ~ 開頭）、全大寫底線的變數名（NOCAPTCHA_BYPASS_TOKEN、SLACK_PM_HANDLES）——這些是指引不是值，
# 第一次掃描 5 頁全是這類誤判。
VAL = r'(?![\$<\[/~])(?!(?-i:[A-Z0-9]*_[A-Z0-9_]*)(?<=[A-Z0-9_]{8})(?![A-Za-z0-9_\-\.\{\}:/\?=%@!#+~]))[A-Za-z0-9_\-\.\{\}:/\?=%@!#+~]{8,}'
REDACT = [
    ('aws-key',   re.compile(r'AKIA[0-9A-Z]{16}'), '[REDACTED]'),
    ('openai',    re.compile(r'\bsk-[A-Za-z0-9_\-]{16,}'), '[REDACTED]'),
    ('github',    re.compile(r'\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}'), '[REDACTED]'),
    ('github-pat', re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}'), '[REDACTED]'),
    ('slack',     re.compile(r'\bxox[abpr]-[A-Za-z0-9\-]{10,}'), '[REDACTED]'),
    ('password=', re.compile(r'((?:password|passwd|pwd|secret|token|api[_\-]?key|apikey)\s*[=:：]\s*[`\'"]?)(' + VAL + ')', re.I),
     r'\1[REDACTED]'),
    ('密碼：',     re.compile(r'((?:密碼|密码|口令)\s*[=:：]\s*[`\'"]?)(' + VAL + ')'), r'\1[REDACTED]'),
    # 長串：32 字以上、同時含英文與數字；排除 Anytype 物件 id（bafy…）、git sha（純 hex）、
    # 全小寫含連字號的 slug（memory 檔名、URL 路徑段）
    ('long-token', re.compile(r'(?<![A-Za-z0-9_/\-\.])(?!bafy)(?=[A-Za-z0-9_\-]*[A-Za-z])(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{32,}(?![A-Za-z0-9_/\-\.])'),
     '[REDACTED]'),
]
HEX = re.compile(r'^[0-9a-f]{40}$|^[0-9a-f]{64}$')


def api(path, data=None):
    r = urllib.request.Request(c['api_base'] + path, headers=H,
                               data=json.dumps(data).encode() if data else None)
    return json.load(urllib.request.urlopen(r, timeout=120))


def unescape(md):
    return re.sub(r'\\+([_*`\[\]#|])', r'\1', md or '')


def safe_name(title):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', title).strip().strip('.')
    return s or 'untitled'


def redact(md):
    hits = []
    for name, rx, rep in REDACT:
        def _sub(m, name=name, rep=rep, rx=rx):
            whole = m.group(0)
            if name == 'long-token' and (HEX.match(whole) or ('-' in whole and whole == whole.lower())):
                return whole
            hits.append(name)
            return m.expand(rep) if rep.startswith('\\') else rep
        md = rx.sub(_sub, md)
    return md, sorted(set(hits))


def props(obj):
    out = {'tags': [], 'created': '', 'modified': ''}
    for p in obj.get('properties') or []:
        k = p.get('key')
        if k == 'tag':
            out['tags'] = [t.get('name', '') for t in (p.get('multi_select') or [])]
        elif k == 'created_date':
            out['created'] = p.get('date', '')
        elif k == 'last_modified_date':
            out['modified'] = p.get('date', '')
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    spaces = {s.get('name'): s['id'] for s in api('/spaces?limit=50')['data'] if s.get('name')}
    written, redacted_pages, used = [], [], set()
    for nm in DOC_SPACES:
        sid = spaces.get(nm)
        if not sid:
            sys.exit('找不到 space「%s」' % nm)
        res = api('/spaces/%s/search?limit=200' % sid, {"query": "", "types": ["page"]})
        objs = res['data']
        if res.get('pagination', {}).get('has_more'):
            sys.exit('超過 200 頁，要加分頁')
        for o in objs:
            if o.get('archived'):
                continue
            full = api('/spaces/%s/objects/%s?format=md' % (sid, o['id']))['object']
            title = full.get('name') or o.get('name') or o['id']
            md = unescape(full.get('markdown', ''))
            md, hits = redact(md)
            if hits:
                redacted_pages.append((title, hits))
            fname = safe_name(title)
            base, n = fname, 2
            while fname in used:
                fname = '%s~%d' % (base, n); n += 1
            used.add(fname)
            pr = props(full)
            fm = ['---',
                  'title: %s' % json.dumps(title, ensure_ascii=False),
                  'id: %s' % o['id'],
                  'space: %s' % nm,
                  'tags: %s' % json.dumps(pr['tags'], ensure_ascii=False),
                  'created: %s' % pr['created'],
                  'modified: %s' % pr['modified'],
                  '---', '']
            path = os.path.join(OUT, fname + '.md')
            body = '\n'.join(fm) + md.rstrip('\n') + '\n'
            old = open(path, encoding='utf-8').read() if os.path.exists(path) else None
            if old != body:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(body)
            written.append(fname + '.md')
    # 清掉不在清單的舊檔
    removed = []
    for f in os.listdir(OUT):
        if f.endswith('.md') and f not in written:
            os.remove(os.path.join(OUT, f)); removed.append(f)
    with open(os.path.join(OUT, 'INDEX.json'), 'w', encoding='utf-8') as f:
        # 不寫時間戳：內容沒變就不該產生 commit
        json.dump({'pages': sorted(written)}, f, ensure_ascii=False, indent=1)
    print('匯出 %d 頁 → %s；移除舊檔 %d' % (len(written), OUT, len(removed)))
    for t, hits in redacted_pages:
        print('遮蔽：%s（%s）' % (t, '、'.join(hits)))


if __name__ == '__main__':
    main()
