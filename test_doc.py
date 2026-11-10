#!/usr/bin/env python3
"""doc.py 離線測試（不碰 Anytype：pages／body 用假資料）。
  python3 test_doc.py
蓋到：大頁無段落→toc、大頁有段落→段落、小頁整頁、同 session 重讀→只印提示、--fresh 重印、
不同 session 重印、內容變了重印、TTL 過期重印、toc／list 行為不變、沒 session 鍵不快取；
頁面內容磁碟快取（假 api）：修改時間沒變不重抓、變了只重抓那頁、剛改過的不信任、--fresh／DOC_CACHE=0 不讀快取、
Anytype 逾時退回舊清單、下架的頁清快取、檔案權限。"""
import io, json, os, sys, tempfile, importlib.util, contextlib, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix='doc-test-')
# doc.py 一 import 就讀同目錄 config.json，所以把它 symlink 進暫存目錄配一份假 config
os.symlink(os.path.join(HERE, 'doc.py'), os.path.join(TMP, 'doc.py'))
json.dump({'api_key': 'x', 'api_version': 'x', 'api_base': 'http://127.0.0.1:9'}, open(os.path.join(TMP, 'config.json'), 'w'))
spec = importlib.util.spec_from_file_location('doc', os.path.join(TMP, 'doc.py'))
doc = importlib.util.module_from_spec(spec); spec.loader.exec_module(doc)
doc.CACHE_DIR = os.path.join(TMP, 'cache')
doc.DOC_CACHE = os.path.join(TMP, 'doccache')
REAL_PAGES, REAL_BODY = doc.pages, doc.body   # 下面換成假的；最後一段測磁碟快取要用真的

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

# ── 頁面內容磁碟快取（真的 pages／body，假的 api）────────────────────────────
doc.pages, doc.body = REAL_PAGES, REAL_BODY
OLD = '2026-01-01T00:00:00Z'
REMOTE = {'p1': ['頁一', OLD, 'alpha 一\n'], 'p2': ['頁二', OLD, 'alpha 二\n']}
CALLS = []
DOWN = [None]


def fake_api(path, data=None, timeout=120):
    CALLS.append(path)
    if DOWN[0]:
        raise DOWN[0]
    if path.startswith('/spaces?'):
        return {'data': [{'name': '上下文庫', 'id': 's1'}, {'name': 'Claude Memory', 'id': 's2'}]}
    if path.endswith('/search?limit=200'):
        return {'data': [{'id': k, 'name': v[0], 'properties': [{'key': 'last_modified_date', 'date': v[1]}]}
                         for k, v in REMOTE.items()]}
    return {'object': {'markdown': REMOTE[path.split('/objects/')[1].split('?')[0]][2]}}


doc.api = fake_api


def bodies():
    return [x for x in CALLS if '/objects/' in x]


def grep(*a, env=None):
    del CALLS[:]
    return run('grep', *a, env=env)


g1 = grep('alpha')
ok(g1.splitlines() == ['上下文庫 / 頁一:1: alpha 一', '上下文庫 / 頁二:1: alpha 二'] and len(bodies()) == 2, '磁碟快取：第一次全抓')
g2 = grep('alpha')
ok(g2 == g1 and bodies() == [] and len(CALLS) == 2, '磁碟快取：修改時間沒變，只打清單不抓內容')
REMOTE['p2'][1:] = ['2026-01-02T00:00:00Z', 'alpha 二改\n']
g3 = grep('alpha')
ok('alpha 二改' in g3 and len(bodies()) == 1 and 'p2' in bodies()[0], '磁碟快取：改過的頁只重抓那一頁')
REMOTE['p2'][2] = 'alpha 偷改\n'      # 修改時間沒動但內容變了（同一秒內兩次寫入）
ok('偷改' not in grep('alpha'), '磁碟快取：修改時間沒變就用快取（對照組）')
ok('偷改' in grep('alpha', '--fresh') and len(bodies()) == 2, '--fresh：不讀快取全抓')
REMOTE['p2'][2] = 'alpha 再改\n'
ok('再改' in grep('alpha', env={'DOC_CACHE': '0'}) and len(bodies()) == 2, 'DOC_CACHE=0：不讀快取全抓')
ok('再改' in grep('alpha') and bodies() == [], '--fresh／DOC_CACHE=0 抓回來的有寫進快取')
# 剛改過（抓的時間離修改時間不到 SETTLE）的不信任：每次都重抓
REMOTE['p1'][1:] = [time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'alpha 剛改\n']
grep('alpha'); grep('alpha')
ok(len(bodies()) == 1 and 'p1' in bodies()[0], '磁碟快取：剛改過的頁不信任快取，照樣重抓')
REMOTE['p1'][1:] = ['2026-01-03T00:00:00Z', 'alpha 一\n']; grep('alpha')
# 模擬 doc_patch 寫完刪快取
os.unlink(os.path.join(doc.DOC_CACHE, 'p1.json')); grep('alpha')
ok(len(bodies()) == 1 and 'p1' in bodies()[0], '磁碟快取：快取檔被刪（doc_patch 寫完）就重抓')
# Anytype 逾時 → 退回舊清單＋快取
DOWN[0] = TimeoutError('timed out')
err = io.StringIO()
with contextlib.redirect_stderr(err):
    g4 = grep('alpha')
ok(g4.splitlines() == ['上下文庫 / 頁一:1: alpha 一', '上下文庫 / 頁二:1: alpha 再改'] and '秒前的清單' in err.getvalue(),
   'Anytype 逾時：退回舊清單與快取，stderr 講明')
try:
    grep('alpha', '--fresh'); raised = False
except OSError:
    raised = True
ok(raised, 'Anytype 逾時＋--fresh：不退回，照舊報錯')
DOWN[0] = None
# 下架的頁清快取；空內容不存；權限
del REMOTE['p2']; REMOTE['p3'] = ['頁三', OLD, '']
grep('alpha')
fs = sorted(os.listdir(doc.DOC_CACHE))
ok(fs == ['_pages.json', 'p1.json'], '磁碟快取：不在清單上的頁清掉、空內容不存（%s）' % fs)
ok(oct(os.stat(doc.DOC_CACHE).st_mode & 0o777) == '0o700'
   and oct(os.stat(os.path.join(doc.DOC_CACHE, 'p1.json')).st_mode & 0o777) == '0o600', '磁碟快取：目錄 0700、檔 0600')
ok(run('list').splitlines() == ['%-8s %s' % ('上下文庫', '頁一'), '%-8s %s' % ('上下文庫', '頁三')], 'list（真 pages）：只列文件 space')
print('全部 %d 項通過' % n)
