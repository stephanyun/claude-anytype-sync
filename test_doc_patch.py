#!/usr/bin/env python3
"""doc_patch.py 離線測試（不碰 Anytype：api() 換成記憶體假伺服器）。
  python3 test_doc_patch.py
蓋到：lint 每條規則、lint 只掃改動的行、dump 寫指紋、正常 patch 回讀乾淨、
dump 後他人改過→拒絕／--force 照送、寫回被弄歪→verify 非 0／--no-verify 放行、
--strict 擋下不送、同頁鎖序列化＋鎖等不到就退出、回讀正規化（H1／fence 語言／表格分隔列／清單符號）、
單頁 drift 只算新問題、drift_check --page 參數解析。
線上部分（真 Anytype 測試頁）2026-10-29 已跑過：正常、壞寫法、併發兩個、dump 後他人改過，見 commit 訊息。"""
import contextlib, importlib.util, io, json, os, re, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix='doc-patch-test-')
# doc_patch 一 import 就讀 ~/.claude/anytype/config.json：把 HOME 指到暫存目錄配一份假 config
os.environ['HOME'] = TMP
os.makedirs(os.path.join(TMP, '.claude', 'anytype'))
json.dump({'api_key': 'x', 'api_version': 'x', 'api_base': 'http://127.0.0.1:9'},
          open(os.path.join(TMP, '.claude', 'anytype', 'config.json'), 'w'))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location('doc_patch', os.path.join(HERE, 'doc_patch.py'))
dp = importlib.util.module_from_spec(spec); spec.loader.exec_module(dp)
sys.modules['doc_patch'] = dp          # drift_check 要 from doc_patch import …，給同一個實例
import drift_check
dp.LOCK_DIR = TMP

# ── 假 Anytype ────────────────────────────────────────────────────────────────
SID, OID, TITLE = 'space1', 'page1', '測試｜doc_patch 自驗'
STORE = {}
STATE = {'patch_sleep': 0.0, 'inflight': 0, 'max_inflight': 0, 'mangle': False}


def render(md):
    """模擬 GET ?format=md：第一行頁名不帶 #、底線跳脫、表格 cell 尾端塞 <br>、fence 語言丟掉。"""
    out = []
    for i, line in enumerate(md.split('\n')):
        if i == 0 and line.startswith('# '):
            line = line[2:]
        if line.startswith('|') and not re.match(r'^\|[:\-|\s]+\|$', line):
            cells = [c.strip() for c in line.strip().strip('|').split('|')]
            line = '|' + '|'.join('   %s   <br> ' % c for c in cells) + '|'
        if line.startswith('```'):
            line = '```'
        if not line.startswith('```'):
            line = re.sub(r'(`[^`]*`)|_', lambda m: m.group(1) or r'\_', line)
        out.append(line)
    return '\n'.join(out)


def mangle(md):
    """模擬 Anytype 把「粗體包 code」那種行推歪：**粗體包 `code` 在裡面** → 粗體包 **`code` 在裡面**"""
    return re.sub(r'\*\*([^*`]+)(`[^`]+`)([^*]*)\*\*', r'\1**\2\3**', md)


def fake_api(path, body=None, method=None):
    if path.startswith('/spaces?'):
        return {'data': [{'name': '上下文庫', 'id': SID}]}
    if path.endswith('/search?limit=200'):
        return {'data': [{'id': OID, 'name': TITLE}]}
    if method == 'PATCH':
        STATE['inflight'] += 1
        STATE['max_inflight'] = max(STATE['max_inflight'], STATE['inflight'])
        time.sleep(STATE['patch_sleep'])
        STORE[OID] = mangle(body['markdown']) if STATE['mangle'] else body['markdown']
        STATE['inflight'] -= 1
        return {'object': {'id': OID}}
    if path.startswith('/spaces/%s/objects/%s' % (SID, OID)):
        return {'object': {'markdown': render(STORE[OID])}}
    raise AssertionError('unexpected api call ' + path)


dp.api = fake_api
BASE = """# 測試｜doc_patch 自驗
說明段。

## 1. 清單
- 用 `doc_patch.py --dump` 抓，**不要** 自刻 PATCH。
- 路徑 `~/.claude/anytype/doc_patch.py` 與 `snake_case_name`。
* 星號清單

## 2. 表格
| 欄位 | 說明 |
|:---|:---|
| `api_base` | API 位址 |

## 3. 程式碼
```bash
grep -n '^## ' /tmp/t.md
```
---
## 4. 尾
- 收尾 `**` 後面要留空白再接中文。
"""
STORE[OID] = BASE
n = 0


def ok(cond, msg):
    global n
    n += 1
    assert cond, msg
    print('ok %d - %s' % (n, msg))


def run(fn, *a, **kw):
    """跑一段，回 (stdout, exit_code)；sys.exit 的字串訊息也收進 stdout。"""
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out):
        try:
            fn(*a, **kw)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str):
                print(e.code)
    return out.getvalue(), code


def path(name):
    return os.path.join(TMP, name)


def dump(name):
    o, c = run(dp.do_dump, SID, OID, TITLE, path(name))
    assert c == 0, o
    return o


# ── lint ──────────────────────────────────────────────────────────────────────
codes = lambda md: {c.split(':')[0] for _, c, _, _ in dp.lint(md)}
ok('BOLD_WRAPS_CODE' in codes('- **粗體包 `code` 在裡面**'), 'lint：粗體包 inline code')
ok('FENCE_LITERAL' in codes('- 文字中 ``` 字面'), 'lint：三反引號字面')
ok('FENCE_UNPAIRED' in codes('```\nx\n'), 'lint：``` 不成對')
ok('NESTED_TABLE' in codes('- 清單\n  | a | b |\n  | --- | --- |'), 'lint：巢在清單下的表格')
ok('ASTRAL_MARK' in codes('- 🔊 之後 `code`'), 'lint：astral emoji 後接 code span')
ok('TILDE_STRIKE' in codes('- 值 `01` ~ `05`') and 'TILDE_STRIKE' in codes('- ~/a 與 ~/b'), 'lint：波浪號刪除線（code 夾一顆／裸兩顆）')
ok('TILDE_STRIKE' not in codes('- `~/.claude/a` 與 `~/.claude/b`'), 'lint：兩個 ~ 路徑各自在 code span 裡不報')
ok('ANGLE_HTML' in codes('- 用 <img> 標籤') and 'ANGLE_HTML' not in codes('- `<img>` 與 <範圍>｜<主題> 與 <br>'), 'lint：裸角括號（code 內／中文佔位／<br> 不報）')
ok('BOLD_CLOSE_CJK' in codes('- **粗體**緊接中文') and 'BOLD_CLOSE_CJK' not in codes('- **粗體** 留空白'), 'lint：收尾 ** 緊接中文')
ok('MARK' in codes('- `ro`le 推進字中') and 'MARK' in codes('- 落單 `**` 一顆'), 'lint：drift_check 的 MARK 形狀')
ok(not dp.lint('# a\n- 乾淨 `x` **y** z\n```\n**`在 fence 裡不管`**\n```\n'), 'lint：乾淨頁＋fence 內不檢查')
ok(not dp.lint('- **壞 `x`**\n- 新的乾淨行', only_lines={'- **壞 `x`**'}), 'lint：only_lines 只掃改動的行')

# ── dump／正常 patch ──────────────────────────────────────────────────────────
o = dump('t1.md')
fp = json.load(open(path('t1.md.fp')))
ok(fp['oid'] == OID and fp['sha'] == dp.fingerprint(open(path('t1.md')).read()), 'dump：寫指紋 .fp（oid＋內容 sha）')
body = open(path('t1.md')).read()
ok(dp.md_clean(dp.denorm(body)) == body and '\\_' not in body and '<br>' not in body, 'dump：清理冪等、跳脫與 <br> 已剝')
open(path('t1.md'), 'a').write('\n## 5. 新段\n- 新增 `code_span` 與 **粗體**。\n')
o, c = run(dp.do_patch, SID, OID, TITLE, path('t1.md'))
ok(c == 0 and '缺行 0、多行 0' in o and '新問題 0' in o, '正常 patch：回讀逐行一致、drift 無新問題')
ok(json.load(open(path('t1.md.fp')))['sha'] == dp.fingerprint(dp.fetch(SID, OID)), 'patch 後指紋更新成頁面現況（同一份檔可再 patch）')
o, c = run(dp.do_patch, SID, OID, TITLE, path('t1.md'))
ok(c == 0, '同一份檔不重新 dump 再 patch 一次：不被指紋擋')

# ── dump 後他人改過 ───────────────────────────────────────────────────────────
dump('t2.md'); dump('t3.md')
open(path('t2.md'), 'a').write('\n## 6. 甲加的\n')
open(path('t3.md'), 'a').write('\n## 6. 乙加的\n')
o, c = run(dp.do_patch, SID, OID, TITLE, path('t2.md')); ok(c == 0, '甲先 patch 成功')
o, c = run(dp.do_patch, SID, OID, TITLE, path('t3.md'))
ok(c != 0 and '指紋不符' in o and '乙加的' not in STORE[OID], '乙用舊 dump patch：拒絕，頁面沒被蓋')
o, c = run(dp.do_patch, SID, OID, TITLE, path('t3.md'), force=True)
ok(c == 0 and '--force 照送' in o and '乙加的' in STORE[OID], '--force：照送')
os.remove(path('t3.md.fp'))
o, c = run(dp.do_patch, SID, OID, TITLE, path('t3.md'))
ok(c == 0 and '跳過指紋比對' in o, '沒有 .fp（非 dump 來的檔）：警告後照送')

# ── 寫回被弄歪 ────────────────────────────────────────────────────────────────
dump('t4.md')
open(path('t4.md'), 'a').write('\n## 7. 壞\n- **粗體包 `code` 在裡面** 會右移\n')
o, c = run(dp.do_patch, SID, OID, TITLE, path('t4.md'), strict=True)
ok(c == 4 and 'BOLD_WRAPS_CODE' in o and '粗體包' not in STORE[OID], '--strict：lint 命中就擋下不送')
STATE['mangle'] = True
o, c = run(dp.do_patch, SID, OID, TITLE, path('t4.md'))
ok(c == 1 and '缺行 1、多行 1' in o and '-- **粗體包' in o and '+- 粗體包 **' in o, '寫回被推歪：verify 非 0、印出 -/+ 差異')
o, c = run(dp.do_patch, SID, OID, TITLE, path('t4.md'), no_verify=True)
ok(c == 0 and '未驗證' in o, '--no-verify：逃生口放行')
STATE['mangle'] = False

# ── 回讀正規化 ────────────────────────────────────────────────────────────────
lost, extra, diff = dp.line_diff('# 頁名\n```bash\nx\n```\n|:---|:--|\n* 星\n', '頁名\n```\nx\n```\n| :--- | :-- |\n- 星\n')
ok(not diff, '正規化：H1 不帶 #、fence 語言、表格分隔列、* 清單符號都不算差異')
lost, extra, diff = dp.line_diff('- a\n- b\n', '- a\n- c\n')
ok(lost == ['- b'] and extra == ['- c'], '正規化：真差異照報缺行／多行')

# ── 單頁 drift 只算新問題 ─────────────────────────────────────────────────────
drift_check.BASELINE = path('baseline.json')
json.dump({'accepted': [[TITLE, 'MARK', '- 收尾 `**` 後面要留空白再接中文。']]}, open(drift_check.BASELINE, 'w'))
ok(dp.page_drift(TITLE, BASE) == set(), 'page_drift：基準線裡的誤判不算')
ok(dp.page_drift(TITLE, BASE + '- `ro`le 歪了\n') == {('MARK', '- `ro`le 歪了')}, 'page_drift：新歪行抓到')
o, c = run(dp.verify, TITLE, BASE + '- `ro`le 本來就歪\n', BASE + '- `ro`le 本來就歪\n', dp.md_clean(BASE + '- `ro`le 本來就歪\n'))
ok('新問題 0、本來就有 1' in o, 'verify：patch 前就歪的行不算這次的帳')

# ── 鎖 ───────────────────────────────────────────────────────────────────────
held = dp.PageLock(OID, lock_dir=TMP).__enter__()
t0 = time.time(); o, c = run(dp.do_patch, SID, OID, TITLE, path('t1.md'), lock_wait=1)
ok(c != 0 and '鎖著' in o and 1 <= time.time() - t0 < 5, '鎖被別人拿著：等到上限就退出')
held.__exit__(None, None, None)
dump('c1.md'); dump('c2.md')
open(path('c1.md'), 'a').write('\n## 8. 併發 A\n'); open(path('c2.md'), 'a').write('\n## 8. 併發 B\n')
STATE['patch_sleep'] = 0.4; STATE['max_inflight'] = 0
res = {}
class ThreadStdout:
    """redirect_stdout 跨執行緒會互相還原，改成依執行緒分流的 stdout 代理。"""
    def __init__(self, real): self.real, self.bufs = real, {}
    def write(self, s): self.bufs.setdefault(threading.get_ident(), io.StringIO()).write(s)
    def flush(self): pass
def worker(k):
    code = 0
    try:
        dp.do_patch(SID, OID, TITLE, path(k))
    except SystemExit as e:
        code = 1
        print(e.code)
    res[k] = (sys.stdout.bufs[threading.get_ident()].getvalue(), code)
real_stdout, sys.stdout = sys.stdout, ThreadStdout(sys.stdout)
ts = [threading.Thread(target=worker, args=(k,)) for k in ('c1.md', 'c2.md')]
[t.start() for t in ts]; [t.join() for t in ts]
sys.stdout = real_stdout
STATE['patch_sleep'] = 0
rcs = sorted(v[1] for v in res.values())
ok(STATE['max_inflight'] == 1 and rcs == [0, 1] and sum('指紋不符' in v[0] for v in res.values()) == 1,
   '併發兩個 patch：序列化，後者被指紋擋住不互蓋')
ok(STORE[OID].count('## 8. 併發') == 1, '併發後頁面只有一個人的改動')

# ── drift_check --page 參數解析 ──────────────────────────────────────────────
seen = {}
class FakeDoc:
    pages = staticmethod(lambda: [('上下文庫', SID, OID, TITLE), ('上下文庫', SID, 'p2', 'JGB｜別頁')])
    api = staticmethod(lambda p: {'object': {'markdown': render(STORE[OID])}})
sys.modules['doc'] = FakeDoc
sys.argv = ['drift_check.py', '--page', '自驗']
o, c = run(drift_check.main)
ok('掃 1 頁' in o, 'drift_check --page：只掃標題命中的頁')
sys.argv = ['drift_check.py', '--page', '沒有這頁']
o, c = run(drift_check.main)
ok(c != 0 and '沒有標題含' in o, 'drift_check --page：沒命中就退出')
sys.argv = ['drift_check.py', '--accept', '--page', '自驗']
o, c = run(drift_check.main)
ok(c != 0 and '不能配' in o, 'drift_check：--accept 不能配 --page')

# ── CLI 旗標 ─────────────────────────────────────────────────────────────────
sys.argv = ['doc_patch.py', '--patch', '--bogus', 'x', 'y']
o, c = run(dp.main)
ok(c != 0 and '不認得的旗標' in o, 'CLI：不認得的旗標拒絕')
print('全部 %d 項通過' % n)
