#!/usr/bin/env python3
"""session_start.py 離線測試（不碰 Anytype：真正的工作用 ANYTYPE_SS_TEST_CMD_* 換成假命令）。
  python3 test_session_start.py
蓋到：快工作等得到（狀態 ok）、FRESH 內不重跑、慢工作 hook 先放行而 worker 跑完、hook 的 stdout 不被 worker 握住、
同時 6 個 hook 只跑 1 次、失敗回 1 且下一個 session 仍回 1、成功後清掉、
經 sync.py／skills_catalog.py 真入口＋socket stdin（Linux 上 Claude Code 給 hook 的 stdin 是 socket）會走背景路徑、
沒有 CLAUDE_ENV_FILE／ANYTYPE_SS_SYNC=1／worker 自己 不走背景路徑。"""
import json, os, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
SS = os.path.join(HERE, "session_start.py")
PY = sys.executable
fails = 0


def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    fails += 0 if cond else 1


def env_for(tmp, job_cmd, **kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith("ANYTYPE_SS_") and k != "CLAUDE_ENV_FILE"}
    e["ANYTYPE_SS_STATE_DIR"] = tmp
    e["ANYTYPE_SS_TEST_CMD_RECONCILE"] = json.dumps(job_cmd)
    e["ANYTYPE_SS_TEST_CMD_CATALOG"] = json.dumps(job_cmd)
    e.update({k: str(v) for k, v in kw.items()})
    return e


def job(tmp, sleep=0.0, rc=0):
    """假工作：往 counter 檔加一行、睡、照 rc 離開。"""
    return [PY, "-c", "import sys,time,os;open(sys.argv[1],'a').write('x\\n');time.sleep(float(sys.argv[2]));sys.exit(int(sys.argv[3]))",
            os.path.join(tmp, "counter"), str(sleep), str(rc)]


def count(tmp):
    try:
        return len(open(os.path.join(tmp, "counter")).read().split())
    except OSError:
        return 0


def status(tmp, j="reconcile"):
    try:
        return json.load(open(os.path.join(tmp, f"session_start.{j}.json")))
    except (OSError, ValueError):
        return None


def hook(env, j="reconcile"):
    t = time.time()
    p = subprocess.run([PY, SS, j], env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    return p.returncode, time.time() - t


def wait_for(fn, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# 1 快工作：等得到
tmp = tempfile.mkdtemp(prefix="ss-test-")
e = env_for(tmp, job(tmp, 0.1))
rc, dt = hook(e)
st = status(tmp)
check("快工作：hook 回 0、狀態 ok、跑了 1 次", rc == 0 and st and st["ok"] and count(tmp) == 1, f"rc={rc} {dt:.2f}s st={st}")
# 2 FRESH 內不重跑
rc, dt = hook(e)
check("FRESH 內第二個 session 不重跑", rc == 0 and count(tmp) == 1, f"count={count(tmp)}")
# 2b FRESH=0 會重跑
rc, dt = hook(dict(e, ANYTYPE_SS_FRESH="0"))
check("過了 FRESH 會重跑", wait_for(lambda: count(tmp) == 2), f"count={count(tmp)}")

# 3 慢工作：hook 先放行、worker 跑完
tmp = tempfile.mkdtemp(prefix="ss-test-")
e = env_for(tmp, job(tmp, 2.0), ANYTYPE_SS_WAIT="0.3")
rc, dt = hook(e)
check("慢工作：hook 在 WAIT 後就回 0（不等工作做完）", rc == 0 and dt < 1.5 and status(tmp) is None, f"rc={rc} {dt:.2f}s")
check("慢工作：背景 worker 自己跑完並寫狀態", wait_for(lambda: (status(tmp) or {}).get("ok") is True, 10), f"st={status(tmp)}")

# 4 hook 的 stdout 是 pipe 時，worker 不握著它（握著的話 Claude Code 會等到 worker 結束）
tmp = tempfile.mkdtemp(prefix="ss-test-")
e = env_for(tmp, job(tmp, 3.0), ANYTYPE_SS_WAIT="0.2")
t = time.time()
p = subprocess.Popen([PY, SS, "reconcile"], env=e, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
p.communicate(timeout=30)
dt = time.time() - t
check("worker 不握 hook 的 stdout／stderr（pipe 立刻 EOF）", dt < 1.5, f"{dt:.2f}s")

# 5 同時 6 個 hook：只有一個 worker 真的跑
tmp = tempfile.mkdtemp(prefix="ss-test-")
e = env_for(tmp, job(tmp, 1.0), ANYTYPE_SS_WAIT="0.2", ANYTYPE_SS_FRESH="0")
t = time.time()
ps = [subprocess.Popen([PY, SS, "reconcile"], env=e, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(6)]
rcs = [p.wait(timeout=30) for p in ps]
dt = time.time() - t
wait_for(lambda: status(tmp) is not None, 10)
time.sleep(0.3)
check("同時 6 個 hook：全部回 0、都沒等工作、工作只跑 1 次", rcs == [0] * 6 and dt < 2.5 and count(tmp) == 1, f"rcs={rcs} {dt:.2f}s count={count(tmp)}")

# 6 失敗：回 1；下一個 session 再試仍失敗仍回 1；之後成功就回 0
tmp = tempfile.mkdtemp(prefix="ss-test-")
e = env_for(tmp, job(tmp, 0.0, rc=1))
rc, _ = hook(e)
check("工作失敗：hook 回 1（settings.json 的 || echo 會印警告）", rc == 1 and status(tmp)["ok"] is False, f"rc={rc} st={status(tmp)}")
rc, _ = hook(e)
check("失敗不算 FRESH：下一個 session 會重試", rc == 1 and count(tmp) == 2, f"rc={rc} count={count(tmp)}")
rc, _ = hook(env_for(tmp, job(tmp, 0.0, rc=0)))
check("之後成功：回 0", rc == 0 and status(tmp)["ok"] is True)
# 6b 慢而且上次失敗：先放行時照上次結果回 1
hook(env_for(tmp, job(tmp, 0.0, rc=1), ANYTYPE_SS_FRESH="0"))
rc, dt = hook(env_for(tmp, job(tmp, 2.0), ANYTYPE_SS_WAIT="0.2"))
check("上次失敗、這次還在跑：照上次結果回 1", rc == 1 and dt < 1.5, f"rc={rc} {dt:.2f}s")
wait_for(lambda: (status(tmp) or {}).get("ok") is True, 10)

# 7 真入口＋socket stdin：sync.py reconcile／skills_catalog.py sync 帶 CLAUDE_ENV_FILE → 走背景路徑（假命令被叫到）
for script, arg, j in (("sync.py", "reconcile", "reconcile"), ("skills_catalog.py", "sync", "catalog")):
    tmp = tempfile.mkdtemp(prefix="ss-test-")
    e = env_for(tmp, job(tmp, 0.1), CLAUDE_ENV_FILE=os.path.join(tmp, "envfile"))
    a, b = socket.socketpair()
    b.sendall(json.dumps({"session_id": "t", "hook_event_name": "SessionStart", "source": "startup"}).encode() + b"\n")
    p = subprocess.run([PY, os.path.join(HERE, script), arg], env=e, stdin=a.fileno(), capture_output=True, timeout=30)
    a.close(); b.close()
    st = status(tmp, j)
    check(f"{script} {arg}：SessionStart（socket stdin＋CLAUDE_ENV_FILE）走背景路徑", p.returncode == 0 and count(tmp) == 1 and st and st["ok"],
          f"rc={p.returncode} count={count(tmp)} st={st} err={p.stderr[-120:]!r}")

# 8 判斷函式：手動／worker／逃生口都不走背景
sys.path.insert(0, HERE)
import session_start as ss
saved = dict(os.environ)
def judge(**kw):
    for k in ("CLAUDE_ENV_FILE", "ANYTYPE_SS_WORKER", "ANYTYPE_SS_SYNC"):
        os.environ.pop(k, None)
    os.environ.update(kw)
    return ss.from_session_start_hook()
check("沒有 CLAUDE_ENV_FILE（手動／cron／ssh）→ 同步跑", judge() is False)
check("有 CLAUDE_ENV_FILE → 背景", judge(CLAUDE_ENV_FILE="/x") is True)
check("worker 自己（ANYTYPE_SS_WORKER=1）→ 同步跑", judge(CLAUDE_ENV_FILE="/x", ANYTYPE_SS_WORKER="1") is False)
check("逃生口 ANYTYPE_SS_SYNC=1 → 同步跑", judge(CLAUDE_ENV_FILE="/x", ANYTYPE_SS_SYNC="1") is False)
os.environ.clear(); os.environ.update(saved)

print(f"\n{'全部通過' if not fails else f'{fails} 項失敗'}")
sys.exit(1 if fails else 0)
