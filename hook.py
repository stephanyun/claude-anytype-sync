#!/usr/bin/env python3
"""PostToolUse hook: when Claude writes a memory/doc file, mirror it to Anytype.

Reads the hook JSON on stdin, and if the edited file lives in *any* configured
memory dir (a *.md), pushes it to Anytype. Never blocks Claude: failures are
reported on stderr and exit 1 (PostToolUse only blocks on exit 2).
"""
import glob, json, os, sys, subprocess

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
