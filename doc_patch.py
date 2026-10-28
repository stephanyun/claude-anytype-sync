#!/usr/bin/env python3
"""改 Anytype 上下文庫任一頁（唯一合法寫法；--dump → 改檔 → --patch）：

  doc_patch.py --dump  "<頁名關鍵字>" /tmp/x.md    # 抓下來成可編輯 markdown（旁邊寫 x.md.fp 版本指紋）
  doc_patch.py --patch "<頁名關鍵字>" /tmp/x.md    # lint → 鎖頁 → 比對指紋 → PATCH → 回讀逐行比對＋單頁 drift_check
  doc_patch.py --lint  /tmp/x.md                   # 只做離線靜態檢查，不碰 Anytype

--patch 的旗標：
  --no-verify   跳過回讀驗證（逃生口；只在確定要跳過時用）
  --strict      lint 有警告就不送（預設只印警告照送，但送完回讀仍會逐行比對）
  --force       指紋不符（dump 之後頁面被別人改過）仍照送；HTTP 500 重送同一份檔也用這個

兩個參數都要給（頁名加引號；第二個是檔案路徑，不是 stdout）。
dump 檔超過 30KB 別用 Read 整檔（25000 token 上限）：Read 時 limit ≤150 行，或
`grep -n '^## ' x.md` 找段落、`sed -n 'A,Bp' x.md` 只讀那段；改檔用 Edit／python 定位錨點插入。
--patch 回 HTTP 500 時立刻用同一份檔加 --force 重送一次（大頁可能已被清空），dump 原稿不要先刪。
denorm／ensure_rule_breaks 與 spec-denorm.py 同一套（三個雷的說明見該檔）。

2026-10-29 起 --patch 自己保證正確，不再靠人記得跑 drift_check：
  1. 送出前 lint 新增／改動的行（已知會壞的寫法：粗體包 inline code、三反引號圍籬字面、
     巢在清單下的表格、astral emoji 後接行內標記、兩段 code 夾波浪號、裸角括號、
     收尾 ** 緊接中文、drift_check 的 MARK 六類）。
  2. 同頁 flock（/tmp/doc_patch-<頁id>.lock，等最多 60 秒）＋ dump 時記內容 sha，
     patch 前再抓一次比對，不同就拒絕（兩個 agent 同時改同頁會互蓋）。
  3. 送完回讀：與送出的 markdown 逐行比對（缺行／多行）、只對這頁跑 drift_check 的檢查，
     跟 patch 前比有「新」問題就 exit 1 並印差異前 20 行。
"""
import difflib, fcntl, hashlib, json, os, re, sys, time, urllib.error, urllib.request
# realpath 不是 abspath：部署目錄 ~/.claude/anytype/ 的這支是 symlink，
# 用 abspath 會去 symlink 旁邊找 md_clean，那裡沒有（只有 repo 裡有）。
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from md_clean import clean as md_clean

CFG = json.load(open(os.path.expanduser('~/.claude/anytype/config.json')))
SPACE_PREF = ('上下文庫', '技術文件')
LOCK_WAIT = 60          # 同頁鎖最多等幾秒
LOCK_DIR = '/tmp'


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


# ── 1. 靜態檢查（lint）────────────────────────────────────────────────────────
# 規則來源：Anytype《CORE｜memory 與 skills 同步機制》第 15 節「七個雷」與
# drift_baseline.json 收過的誤判形狀。只掃「這次新增／改動的行」（patch 時與頁面現況比），
# 既有的行不重複吵；--lint 離線模式沒有現況可比，就全掃。
CJK = r'\u3400-\u9fff\uf900-\ufaff'
CODE_SPAN = re.compile(r'(`+)(.+?)\1')


def _strip_code(line):
    """把 code span 連同反引號換成等長空白（保留座標），檢查粗體／角括號／波浪號時用。"""
    return CODE_SPAN.sub(lambda m: ' ' * len(m.group(0)), line)


def lint_line(line):
    """回傳這一行命中的 [(代碼, 說明)]；空＝乾淨。"""
    hits = []
    s = line.strip()
    if '```' in s and (not s.startswith('```') or s.count('```') > 1):
        hits.append(('FENCE_LITERAL', '三反引號出現在行中（非圍籬開頭）：Anytype 會當 code fence 開關，整頁後面全進 code block'))
    if re.match(r'^\s+\|', line):
        hits.append(('NESTED_TABLE', '表格列有縮排（巢在清單底下）：GET 回來會被攤平成頂層表格'))
    if re.search(r'\*\*[^*\n]*`[^`\n]+`[^*\n]*\*\*', line):
        hits.append(('BOLD_WRAPS_CODE', '粗體包住 inline code：Anytype 會讓整組標記右移，改成粗體不跨反引號'))
    if any(ord(c) > 0xFFFF for c in line) and re.search(r'`|\*\*', line):
        hits.append(('ASTRAL_MARK', '同一行有 astral emoji（U+10000 以上）又有 `／**：位移補償可能放棄，換成 BMP 字元（md_clean.ASTRAL）最穩'))
    nc = _strip_code(line)
    # 波浪號：兩段 code 中間夾一顆、或同一行 code 外有兩顆（兩個裸 ~ 路徑）都會被當刪除線，中間的值無聲消失。
    # 只數 code span 外的：全庫實測兩個 `~/...` 各自包在 code span 裡的行 round-trip 沒事（CORE 頁 7 行）。
    if nc.count('~') >= 2 or re.search(r'`\s*~\s*`', line):
        hits.append(('TILDE_STRIKE', '同一行兩個波浪號（兩段 code 夾波浪號／兩個 ~ 路徑）會被當刪除線，中間的值消失；寫成「到」或放進同一個 code span'))
    if re.search(r'<[A-Za-z/][^<>]*>', nc.replace('<br>', '')):
        hits.append(('ANGLE_HTML', '裸角括號會被當 HTML 砍掉：放進 inline code 或改寫'))
    # 收尾 ** 緊接中文（沒留空白）
    parts = nc.split('**')
    if len(parts) >= 3:
        for i in range(2, len(parts), 2):
            if parts[i][:1] and re.match('[%s]' % CJK, parts[i][:1]):
                hits.append(('BOLD_CLOSE_CJK', '收尾 ** 後面要留空白再接中文，否則 CommonMark 不認它是強調'))
                break
    # drift_check 的 MARK 六類（A 空白沾邊、B ****、C 粗體掉進 code、D ** 落單、E 粗體沾空白、F code 混標點）
    try:
        from drift_check import mark_flags
        f = mark_flags(line)
    except Exception:
        f = ''
    if f:
        hits.append(('MARK:' + f, 'drift_check 會抓的形狀（A 空白沾 code 邊、B ****、C 粗體掉進 code、D ** 落單、E 粗體沾空白、F code 裡混中文標點）'))
    return hits


def lint(md, only_lines=None):
    """回傳 [(行號, 代碼, 說明, 整行)]。only_lines＝只檢查這些（strip 後的）行，None＝全掃。code fence 內不檢查。"""
    out, fence = [], False
    for i, line in enumerate(md.split('\n'), 1):
        if line.strip().startswith('```') and line.strip().count('```') == 1:
            fence = not fence
            continue
        if fence or not line.strip():
            continue
        if only_lines is not None and line.strip() in only_lines:
            continue
        for code, why in lint_line(line):
            out.append((i, code, why, line))
    if md.count('```') % 2:
        out.append((0, 'FENCE_UNPAIRED', '``` 不成對', ''))
    return out


def print_lint(hits):
    for ln, code, why, line in hits:
        print('   ⚠ L%s [%s] %s' % (ln or '-', code, why))
        if line:
            print('       %s' % line.strip()[:120])


# ── 2. 鎖與指紋 ───────────────────────────────────────────────────────────────
def fingerprint(md):
    return hashlib.sha256(md.encode('utf-8')).hexdigest()


def fp_path(path):
    return path + '.fp'


def write_fp(path, oid, title, md):
    json.dump({'oid': oid, 'title': title, 'sha': fingerprint(md), 'at': time.strftime('%F %T')},
              open(fp_path(path), 'w', encoding='utf-8'), ensure_ascii=False)


def read_fp(path):
    try:
        return json.load(open(fp_path(path), encoding='utf-8'))
    except (OSError, ValueError):
        return None


class PageLock:
    """同一頁同一時間只准一個 patch；等不到就 exit 3。鎖檔在 /tmp，以頁 id 為鍵，跨 session 有效。"""
    def __init__(self, oid, wait=LOCK_WAIT, lock_dir=None):
        self.path = os.path.join(lock_dir or LOCK_DIR, 'doc_patch-%s.lock' % oid)
        self.wait = wait
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, 'a+')
        try:
            os.chmod(self.path, 0o666)   # 同機不同帳號也能等同一把鎖
        except OSError:
            pass
        deadline = time.time() + self.wait
        waited = False
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    self.fh.close(); self.fh = None
                    sys.exit('✗ 這頁正被另一個 patch 鎖著（%s），等了 %d 秒；稍後重跑' % (self.path, self.wait))
                if not waited:
                    print('… 這頁正被另一個 patch 改，等它完成（最多 %d 秒）' % self.wait)
                    waited = True
                time.sleep(0.5)
        self.fh.seek(0); self.fh.truncate()
        self.fh.write('%d %s\n' % (os.getpid(), time.strftime('%F %T'))); self.fh.flush()
        return self

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            self.fh = None


# ── 3. 回讀驗證 ───────────────────────────────────────────────────────────────
def _norm_line(line):
    """逐行比對前的正規化：Anytype 會吃掉標題裡的 `code`／**粗體** 標記、重排表格空白、
    把 `*`／`+` 清單符號改成 `-`，這些不是損壞。"""
    s = line.strip()
    if s.startswith('#'):
        s = re.sub(r'[`*_]', '', s)
        s = re.sub(r'^#\s+', '', s)      # H1 是頁名，回讀時不帶 `# `
    elif s.startswith('|'):
        s = re.sub(r'\s*\|\s*', '|', s)
        if re.fullmatch(r'\|[:\-|]+\|', s):
            s = '|---|'
    else:
        s = re.sub(r'^[*+] ', '- ', s)
    return re.sub(r'\s+', ' ', s)


def _norm_lines(md):
    out, fence = [], False
    for line in md.split('\n'):
        if line.strip().startswith('```'):
            fence = not fence
            out.append('```'); continue
        if fence:
            out.append(line.rstrip()); continue
        n = _norm_line(line)
        if n:
            out.append(n)
    return out


def line_diff(sent, back):
    """回傳 (缺行, 多行, unified diff 前 N 行)。sent／back 都先過 md_clean（送出的檔沒過 md_clean，
    但 Anytype 回來的一定是 md_clean 後的樣子：astral emoji 已換 BMP、表格已重排）。"""
    a, b = _norm_lines(md_clean(sent)), _norm_lines(back)
    lost = [l for l in a if l not in set(b)]
    extra = [l for l in b if l not in set(a)]
    diff = list(difflib.unified_diff(a, b, 'sent', 'readback', lineterm='', n=0))
    return lost, extra, [d for d in diff if not d.startswith(('---', '+++', '@@'))]


def page_drift(title, md):
    """只對這頁跑 drift_check 的檢查，回傳 {(類型, 整行)}（去掉基準線裡已接受的誤判）。"""
    from drift_check import scan_page, load_baseline
    base = load_baseline()
    return {(kind.split(':')[0], line.strip()) for kind, _, line in scan_page(title, md)
            if (title, kind.split(':')[0], line.strip()) not in base}


def verify(title, sent, before, back):
    """回讀驗證。回傳 True＝乾淨；印出所有差異。
    drift 只算「patch 前沒有、patch 後有」的，頁面本來就歪的行不算這次的帳（但會印數量）。"""
    ok = True
    lost, extra, diff = line_diff(sent, back)
    print('✓ %s 已 PATCH：回讀缺行 %d、多行 %d' % (title, len(lost), len(extra)))
    if diff:
        ok = False
        print('   逐行差異（前 20 行；- 送出有／+ 回讀多）：')
        for d in diff[:20]:
            print('   %s' % d[:140])
    norm = lambda s: re.sub(r'[`*_]', '', s).strip()
    sent_h2 = set(norm(l) for l in sent.split('\n') if l.startswith('## '))
    bad = [l for l in back.split('\n') if l.startswith('## ') and norm(l) not in sent_h2]
    if bad:
        ok = False
        print('   ⚠ 多出的 H2 %d（多半是孤立 --- 把前一行吃成標題）：' % len(bad))
        for l in bad[:5]:
            print('      %s' % l[:90])
    try:
        d_before = page_drift(title, before) if before else set()
        d_after = page_drift(title, back)
    except Exception as exc:
        print('   ⚠ drift_check 跑不起來（%s），這段略過' % exc)
        d_before, d_after = set(), set()
    new = sorted(d_after - d_before)
    old = len(d_after & d_before)
    print('   drift_check（只這頁）：新問題 %d、本來就有 %d' % (len(new), old))
    if new:
        ok = False
        for kind, line in new[:20]:
            print('   [%s] %s' % (kind, line[:140]))
    return ok


# ── 主流程 ────────────────────────────────────────────────────────────────────
def do_dump(sid, oid, title, path):
    base = fetch(sid, oid)
    if md_clean(denorm(base)) != base:
        sys.exit('✗ 清理不是冪等的')
    open(path, 'w', encoding='utf-8').write(base)
    write_fp(path, oid, title, base)
    nb = len(base.encode('utf-8'))
    print('✓ %s → %s（%dKB、%d 行；指紋 %s）' % (title, path, nb // 1024, base.count('\n') + 1, fp_path(path)))
    if nb > 30000:
        print("   ⚠ 超過 Read 單次上限：Read 時 limit ≤150 行，或 grep -n '^## ' 找段再 sed -n 'A,Bp'")


def do_patch(sid, oid, title, path, strict=False, force=False, no_verify=False, lock_wait=LOCK_WAIT):
    new = ensure_rule_breaks(open(path, encoding='utf-8').read())
    with PageLock(oid, wait=lock_wait):
        before = fetch(sid, oid)
        # 指紋：dump 時的內容 sha 要跟頁面現況一致，否則就是 dump 之後別人改過，照送會蓋掉他的
        fp = read_fp(path)
        if fp and fp.get('oid') == oid:
            if fp['sha'] != fingerprint(before):
                if force:
                    print('⚠ 指紋不符（dump 之後這頁被改過），--force 照送')
                else:
                    sys.exit('✗ dump 之後這頁被改過（指紋不符）：重新 --dump 再改一次；確定要蓋掉就加 --force\n'
                             '   dump 時 %s / %s' % (fp.get('at'), fp['sha'][:12]))
        elif fp:
            print('⚠ %s 指的是另一頁（%s），指紋不比' % (fp_path(path), fp.get('title')))
        else:
            print('⚠ 沒有 %s（不是用 --dump 抓的？），跳過指紋比對' % fp_path(path))
        # lint 只掃與頁面現況不同的行
        existing = set(l.strip() for l in before.split('\n'))
        hits = lint(new, only_lines=existing)
        if hits:
            print('⚠ lint：%d 個已知會壞的寫法%s' % (len(hits), '，--strict 擋下不送' if strict else '（照送，回讀會比對）'))
            print_lint(hits)
            if strict:
                sys.exit(4)
        # 送出的是補償過的版本，回讀比對仍用 new（補償只動標記位置，純文字不變）
        try:
            api('/spaces/%s/objects/%s' % (sid, oid), {'markdown': utf16_compensate(new)}, method='PATCH')
        except urllib.error.HTTPError as exc:
            sys.exit('✗ PATCH 失敗 HTTP %s：頁面可能已被清空，立刻用同一份檔加 --force 重送' % exc.code)
        back = fetch(sid, oid)
        write_fp(path, oid, title, back)   # 同一份檔可以再 patch（例如改一點再送），不必重新 dump
        if no_verify:
            print('✓ %s 已 PATCH（--no-verify，未驗證）' % title)
            return
        if not verify(title, new, before, back):
            sys.exit('✗ 回讀與送出不一致或有新 drift：照上面差異修檔後重跑（確定是誤判才 --no-verify）')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--') or a in ('--dump', '--patch', '--lint')]
    flags = set(a for a in sys.argv[1:] if a.startswith('--')) - {'--dump', '--patch', '--lint'}
    unknown = flags - {'--no-verify', '--strict', '--force'}
    if unknown:
        sys.exit('✗ 不認得的旗標：%s\n%s' % (' '.join(sorted(unknown)), __doc__))
    if not args:
        sys.exit(__doc__)
    mode = args[0]
    if mode == '--lint':
        if len(args) != 2:
            sys.exit(__doc__)
        hits = lint(open(args[1], encoding='utf-8').read())
        if not hits:
            print('✓ lint 乾淨'); return
        print('⚠ lint：%d 個已知會壞的寫法' % len(hits)); print_lint(hits)
        sys.exit(4)
    if mode not in ('--dump', '--patch') or len(args) != 3:
        sys.exit(__doc__)
    keyword, path = args[1], args[2]
    sid, oid, title = resolve(keyword)
    if mode == '--dump':
        do_dump(sid, oid, title, path)
    else:
        do_patch(sid, oid, title, path, strict='--strict' in flags, force='--force' in flags,
                 no_verify='--no-verify' in flags)


# 掛 __main__ 守衛，drift_check.py 才 import 得到 denorm／fetch 而不會跟著跑起來
if __name__ == '__main__':
    main()
