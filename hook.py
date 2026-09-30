#!/usr/bin/env python3
"""PostToolUse hook: when Claude writes a memory/doc file, mirror it to Anytype.

Reads the hook JSON on stdin, and if the edited file lives in *any* configured
memory dir (a *.md), pushes it to Anytype. Never blocks Claude: failures are
reported on stderr and exit 1 (PostToolUse only blocks on exit 2).
"""
import sys

# 快路徑（2026-10-01）：這支掛在每一次 Write／Edit 之後，但只有 memory 目錄的 .md 才有事做。
# 先看 stdin 原文有沒有 ".md"，沒有就在 import json／subprocess／glob 之前直接退出
# （實測整支 22ms，其中 python 啟動＋import 就佔了大半）。有 ".md" 才走完整判斷。
_RAW = sys.stdin.read()
if ".md" not in _RAW:
    sys.exit(0)

import glob, json, os, subprocess

CFG_PATH = os.path.expanduser("~/.claude/anytype/config.json")
SYNC = os.path.expanduser("~/.claude/anytype/sync.py")
FALLBACK_GLOB = os.path.expanduser("~/.claude/projects/*/memory")

_warned = False


def warn(msg):
    global _warned
    _warned = True
    print("anytype-sync hook: " + msg, file=sys.stderr)


def memory_dirs():
    """Directories whose *.md files mirror to Anytype.

    An unreadable config warns instead of falling back quietly: the old
    fallback was a hard-coded macOS path, so on any other machine it matched
    nothing and every memory write silently stopped syncing.
    """
    try:
        with open(CFG_PATH) as f:
            md = json.load(f).get("memory_dirs")
        if md:
            return [os.path.abspath(os.path.expanduser(p)) for p in md.values()]
        warn("no memory_dirs in " + CFG_PATH + "; falling back to " + FALLBACK_GLOB)
    except Exception as e:
        warn("cannot read " + CFG_PATH + " (" + str(e) + "); falling back to " + FALLBACK_GLOB)
    return [os.path.abspath(p) for p in glob.glob(FALLBACK_GLOB)]


def main():
    try:
        data = json.loads(_RAW)
    except Exception:
        return
    tool = data.get("tool_name", "")
    if tool not in ("Write", "Edit", "MultiEdit"):
        return
    fp = (data.get("tool_input") or {}).get("file_path", "")
    if not fp:
        return
    ap = os.path.abspath(fp)
    if not ap.endswith(".md"):
        return
    if not any(ap.startswith(d + os.sep) for d in memory_dirs()):
        return
    try:
        r = subprocess.run([sys.executable, SYNC, "push", ap],
                           timeout=25, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        warn("push timed out after 25s: " + ap)
        return
    except Exception as e:
        warn("push could not start (" + str(e) + "): " + ap)
        return
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        warn("push exited " + str(r.returncode) + ": " + ap
             + (" | " + detail[-1][:300] if detail else ""))


if __name__ == "__main__":
    main()
    # Non-zero surfaces the warning to the user; PostToolUse only blocks on 2.
    sys.exit(1 if _warned else 0)
