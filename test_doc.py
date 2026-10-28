#!/usr/bin/env python3
"""doc.py 離線測試（不碰 Anytype：pages／body 用假資料）。
  python3 test_doc.py
蓋到：大頁無段落→toc、大頁有段落→段落、小頁整頁、同 session 重讀→只印提示、--fresh 重印、
不同 session 重印、內容變了重印、TTL 過期重印、toc／list 行為不變、沒 session 鍵不快取。"""
import io, json, os, sys, tempfile, importlib.util, contextlib, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix='doc-test-')
# doc.py 一 import 就讀同目錄 config.json，所以把它 symlink 進暫存目錄配一份假 config
os.symlink(os.path.join(HERE, 'doc.py'), os.path.join(TMP, 'doc.py'))
json.dump({'api_key': 'x', 'api_version': 'x', 'api_base': 'http://127.0.0.1:9'}, open(os.path.join(TMP, 'config.json'), 'w'))
spec = importlib.util.spec_from_file_location('doc', os.path.join(TMP, 'doc.py'))
doc = importlib.util.module_from_spec(spec); spec.loader.exec_module(doc)
doc.CACHE_DIR = os.path.join(TMP, 'cache')

BIG = '# 大頁\n' + ''.join('## %d. 段落%d\n%s\n' % (i, i, ('中文內容' * 200 + '\n') * 3) for i in range(1, 9))
SMALL = '# 小頁\n## 1. 唯一段\n小頁內容\n'
PAGES = {'JGB｜大頁測試': BIG, 'JGB｜小頁測試': SMALL}
doc.pages = lambda only=None: [('上下文庫', 's', t, t) for t in PAGES]
doc.body = lambda sid, oid: PAGES[oid]
assert len(BIG.encode()) >= doc.BIG_BYTES and len(SMALL.encode()) < doc.BIG_BYTES


def run(*argv, env=None):
    old = dict(os.environ)
    for k in ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID'):
        os.environ.pop(k, None)
    os.environ.update(env or {})
    sys.argv = ['doc.py'] + list(argv)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            doc.main()
    finally:
        os.environ.clear(); os.environ.update(old)
    return out.getvalue()


S1 = {'CLAUDE_CODE_SESSION_ID': 'sess-A'}
S2 = {'CLAUDE_CODE_SESSION_ID': 'sess-B'}
n = 0


def ok(cond, msg):
    global n
    n += 1
    assert cond, msg
    print('ok %d - %s' % (n, msg))


# 1 大頁無段落 → toc＋提示，不印內文
o = run('read', 'JGB｜大頁測試', env=S1)
ok('不整頁印' in o and '## 1. 段落1  (' in o and '中文內容' not in o, '大頁無段落：印 toc＋提示，不倒內文')
ok(len(o) < 1500, '大頁無段落：輸出 %d 字（<1500）' % len(o))
# 2 大頁有段落 → 段落內文
o = run('read', 'JGB｜大頁測試', '段落3', env=S1)
ok('## 3. 段落3' in o and '中文內容' in o and '## 4.' not in o, '大頁有段落：只印命中段')
# 3 同 session 重讀 → 只印提示＋字數
o2 = run('read', 'JGB｜大頁測試', '段落3', env=S1)
ok('本 session 已讀過這段' in o2 and '中文內容' not in o2 and '## 3. 段落3  (' in o2, '同 session 重讀：只印提示＋字數')
ok(len(o2) < 200, '同 session 重讀：輸出 %d 字' % len(o2))
# 4 --fresh 重印
o3 = run('read', 'JGB｜大頁測試', '段落3', '--fresh', env=S1)
ok('中文內容' in o3 and '已讀過' not in o3, '--fresh：重印內文')
o3b = run('read', '--fresh', 'JGB｜大頁測試', '段落3', env=S1)
ok(o3b == o3, '--fresh 放任何位置都一樣')
# 5 不同 session → 重印
o4 = run('read', 'JGB｜大頁測試', '段落3', env=S2)
ok('中文內容' in o4 and '已讀過' not in o4, '不同 session：重印')
# 6 段落關鍵字順序／大小寫不同視為同一組
o5 = run('read', 'JGB｜大頁測試', '段落5', '段落3', env=S1)
o6 = run('read', 'JGB｜大頁測試', '段落3', '段落5', env=S1)
ok('中文內容' in o5 and '已讀過' in o6, '多段落關鍵字：順序不同算同一組')
# 7 小頁整頁：第一次印、第二次只印提示
o7 = run('read', 'JGB｜小頁測試', env=S1)
ok('小頁內容' in o7 and '不整頁印' not in o7, '小頁：整頁印')
o8 = run('read', 'JGB｜小頁測試', env=S1)
ok('已讀過' in o8 and '小頁內容' not in o8, '小頁重讀：只印提示')
# 8 內容變了 → 重印
PAGES['JGB｜小頁測試'] = SMALL + '新增一行\n'
o9 = run('read', 'JGB｜小頁測試', env=S1)
ok('新增一行' in o9 and '已讀過' not in o9, '內容變了：重印')
# 9 TTL 過期 → 重印
for f in os.listdir(doc.CACHE_DIR):
    p = os.path.join(doc.CACHE_DIR, f); os.utime(p, (time.time() - 3 * 3600,) * 2)
o10 = run('read', 'JGB｜小頁測試', env=S1)
ok('新增一行' in o10 and '已讀過' not in o10, 'TTL 2 小時過期：重印')
# 10 toc／list 不變
ot = run('toc', 'JGB｜大頁測試', env=S1)
ok(ot.startswith('===== 上下文庫 / JGB｜大頁測試 =====\n## 1. 段落1  (') and '不整頁印' not in ot, 'toc 行為不變')
ol = run('list', env=S1)
ok(ol.splitlines() == ['%-8s %s' % ('上下文庫', 'JGB｜大頁測試'), '%-8s %s' % ('上下文庫', 'JGB｜小頁測試')], 'list 行為不變')
# 11 沒 session 鍵（終端機手打、PPID 鏈也沒 claude）→ 不快取，每次都印
doc.session_key = lambda: None
oa = run('read', 'JGB｜大頁測試', '段落7'); ob = run('read', 'JGB｜大頁測試', '段落7')
ok('中文內容' in oa and oa == ob, '沒 session 鍵：不快取')
# 12 快取目錄權限 0700
ok(oct(os.stat(doc.CACHE_DIR).st_mode & 0o777) == '0o700', '快取目錄 0700')
print('全部 %d 項通過' % n)
