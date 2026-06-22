#!/usr/bin/env python3
"""PostToolUse hook: when Claude writes a memory/doc file, mirror it to Anytype.

Reads the hook JSON on stdin, and if the edited file lives in *any* configured
memory dir (a *.md), pushes it to Anytype. Fails silent (never blocks Claude).
"""
import json, os, sys, subprocess

CFG_PATH = os.path.expanduser("~/.claude/anytype/config.json")
SYNC = os.path.expanduser("~/.claude/anytype/sync.py")
LEGACY_MEMORY_DIR = "/Users/abu/.claude/projects/-Users-abu/memory"


def memory_dirs():
    try:
        with open(CFG_PATH) as f:
            md = json.load(f).get("memory_dirs")
        if md:
            return [os.path.abspath(os.path.expanduser(p)) for p in md.values()]
    except Exception:
        pass
    return [os.path.abspath(LEGACY_MEMORY_DIR)]


def main():
    try:
        data = json.load(sys.stdin)
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
        subprocess.run([sys.executable, SYNC, "push", ap],
                       timeout=25, capture_output=True)
    except Exception:
        pass


if __name__ == "__main__":
    main()
