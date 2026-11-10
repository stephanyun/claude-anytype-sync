#!/usr/bin/env python3
"""SessionStart 的 reconcile／skills catalog sync 背景化（2026-11-11）。

為什麼：兩支 SessionStart hook 原本在前景同步跑完才放行。14 天全機隊 transcript 的 hook_cancelled：
  xz-pve-aide reconcile 90 次、skills sync 106 次（timeout 15s），都發生在同一秒連開 3～7 個 session 的時候
  （check-routing 的 headless 批次、布想同時開對話；11-04 那天 load 40）。sync.py／skills_catalog.py 的
  索引鎖是阻塞式 flock，同時開場的 session 排隊等前一個做完，每一個還各打一輪 Anytype API（reconcile 是
  每則 memory 一個 GET）；單跑 0.3s／0.17s，6 個同時跑最後一個 2.7s，機器再忙一點就超過 hook timeout。
  被 timeout 砍掉的那次還可能死在 reconcile 套用到一半。

做法（settings.json 的 hook 命令一個字都不用改）：
  sync.py reconcile／skills_catalog.py sync 發現自己是被 SessionStart hook 叫起來的
  （環境有 CLAUDE_ENV_FILE——Claude Code 只給 SessionStart 這類 hook，Bash 工具、cron、ssh 手動跑都沒有），
  就改走 run_hook()：
    1. 上次成功在 FRESH 秒內 → 直接回 0，不跑（連開一批 session 只有第一個真的同步）。
    2. 把真正的工作丟給脫離 session 的背景 worker（setsid、stdio 全關），自己最多等 WAIT 秒。
       平常 0.3s 就做完，等得到 → 行為跟以前一樣：本 session 開場時 memory 已對齊、失敗當場警告。
       等不到 → 先放行，worker 繼續跑完，結果寫狀態檔。
    3. 回傳值看狀態檔：最近一次結果是失敗 → exit 1（settings.json 那條 `|| echo ⚠️…` 照舊印警告）。
       所以慢的那次若失敗，警告會晚一個 session 出現。
  worker：非阻塞 flock，拿不到＝同機已有另一個 worker 在跑同一件事 → 直接結束，不排隊。
    拿到鎖才用原本的命令列跑 sync.py reconcile／skills_catalog.py sync（帶 ANYTYPE_SS_WORKER=1 走同步路徑），
    所以 reconcile 的三方比對、刪除語意、防呆全部原樣，這支只決定「什麼時候跑、誰跑」。

手動跑（終端機、Bash 工具、cron、memory_git_sync、fleet_probe 的 ssh）沒有 CLAUDE_ENV_FILE，一律照舊同步跑完。
逃生口：ANYTYPE_SS_SYNC=1 → hook 也照舊在前景同步跑。
可調：ANYTYPE_SS_WAIT（預設 3 秒）、ANYTYPE_SS_FRESH（預設 30 秒）、ANYTYPE_SS_JOB_TIMEOUT（預設 300 秒）。
狀態：~/.claude/anytype/session_start.<job>.json（{"ts","ok","rc","secs"}）、.lock、session_start.log（worker 輸出）。
測試：python3 test_session_start.py
"""
import json, os, subprocess, sys, time

STATE_DIR = os.environ.get("ANYTYPE_SS_STATE_DIR") or os.path.expanduser("~/.claude/anytype")
HERE = os.path.dirname(os.path.realpath(__file__))
JOBS = {
    "reconcile": [os.path.join(HERE, "sync.py"), "reconcile"],
    "catalog": [os.path.join(HERE, "skills_catalog.py"), "sync"],
}
LOG_MAX = 200 * 1024


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def _status_path(job):
    return os.path.join(STATE_DIR, f"session_start.{job}.json")


def read_status(job):
    try:
        with open(_status_path(job)) as f:
            st = json.load(f)
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def write_status(job, st):
    p = _status_path(job)
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, p)


def _fresh(st):
    return bool(st and st.get("ok") and 0 <= time.time() - float(st.get("ts", 0)) < _f("ANYTYPE_SS_FRESH", 30))


def from_session_start_hook():
    """是不是被 SessionStart hook 叫起來的（而且沒被要求照舊同步跑）。"""
    e = os.environ
    return bool(e.get("CLAUDE_ENV_FILE")) and e.get("ANYTYPE_SS_WORKER") != "1" and e.get("ANYTYPE_SS_SYNC") != "1"


def _job_cmd(job):
    # 測試用：ANYTYPE_SS_TEST_CMD_<JOB> 給一個 JSON 陣列，取代真正要跑的命令
    o = os.environ.get(f"ANYTYPE_SS_TEST_CMD_{job.upper()}")
    return json.loads(o) if o else [sys.executable] + JOBS[job]


def run_hook(job):
    """hook 前景：回傳 exit code（0＝沒事或還在跑；1＝最近一次結果是失敗）。"""
    if _fresh(read_status(job)):
        return 0
    env = dict(os.environ)
    env["ANYTYPE_SS_WORKER"] = "1"
    try:
        # stdio 全部接 /dev/null：worker 若握著 hook 的 stdout，Claude Code 會等到它結束才算 hook 跑完
        p = subprocess.Popen([sys.executable, os.path.realpath(__file__), "--worker", job],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True, env=env)
    except OSError:
        return 0   # 起不了背景就放行；下一個 session 再試
    try:
        p.wait(timeout=_f("ANYTYPE_SS_WAIT", 3))
    except subprocess.TimeoutExpired:
        pass
    st = read_status(job)
    return 1 if (st and not st.get("ok")) else 0


def worker(job):
    import fcntl
    os.makedirs(STATE_DIR, exist_ok=True)
    lock = open(os.path.join(STATE_DIR, f"session_start.{job}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0   # 同機另一個 worker 正在跑同一件事：跳過，不排隊
    if _fresh(read_status(job)):
        return 0   # 等鎖的空檔別人剛做完
    log_path = os.path.join(STATE_DIR, "session_start.log")
    try:
        if os.path.getsize(log_path) > LOG_MAX:
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass
    t0 = time.time()
    with open(log_path, "a") as log:
        log.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {job} pid={os.getpid()}\n")
        log.flush()
        try:
            rc = subprocess.run(_job_cmd(job), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                timeout=_f("ANYTYPE_SS_JOB_TIMEOUT", 300)).returncode
        except subprocess.TimeoutExpired:
            rc = 124
        except OSError as e:
            log.write(f"起不來：{e}\n")
            rc = 127
        log.write(f"--- rc={rc} {time.time() - t0:.1f}s\n")
    write_status(job, {"ts": time.time(), "ok": rc == 0, "rc": rc, "secs": round(time.time() - t0, 2)})
    return 0


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "--worker" and a[1] in JOBS:
        sys.exit(worker(a[1]))
    if len(a) == 1 and a[0] in JOBS:
        sys.exit(run_hook(a[0]))
    print(__doc__)
    sys.exit(2)
