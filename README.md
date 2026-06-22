# claude-anytype-sync

Canonical home of the **3 engine scripts** that sync Claude Code's memory & skills
to Anytype across machines. Before this repo they were copied by hand and drifted
(twice). Now: **edit here → commit → push → each machine `git pull`.**

| script | role |
|---|---|
| `sync.py` | two-way memory ⇄ Anytype "Claude Memory" (push/pull/reconcile/guard) |
| `hook.py` | PostToolUse: auto-push a memory `.md` right after it's written |
| `skills_catalog.py` | one-way mirror of local `SKILL.md` → Anytype "Claude Skills" (namespaced) |

These three **must be byte-identical on every node.** This repo is the source of truth.

## What is NOT in this repo (and why)
- `config.json` — per-machine, holds the secret `api_key`. Use `config.template.json`. Gitignored.
- `~/.claude/settings.json` — merge the `hooks` block from `settings.hooks.json` (don't overwrite).
- `index.json`, `skills_index.json`, `guard.log`, `conflict_backups/`, mirrored `.md`, `mirror-*/` sinks — auto-generated runtime state. Gitignored.
- The anytype-cli binary — installed per machine via the official `anyproto/anytype-cli` install.sh, not vendored.

## Deploy to a machine
```bash
git clone <repo-url> ~/.claude/anytype-sync
cd ~/.claude/anytype-sync && bash install.sh   # symlinks the 3 scripts into ~/.claude/anytype/
# then per-machine: config.json (from template), merge hooks, anytype access, first reconcile
```

## Update the whole fleet after editing a script
```bash
# on the machine you edited:
cd ~/.claude/anytype-sync && git add -A && git commit -m "..." && git push
# on every other machine:
cd ~/.claude/anytype-sync && git pull          # symlinks make it live immediately
```

## Namespaces (recap)
- **memory** `memory_dirs`: `""`(shared base, required everywhere) + per-machine ns (`jgb`, `xz`, …). A ns pointed at a non-project "sink" dir is downloaded but never recalled.
- **skills** `skills_ns`: unique per machine (`mac`, `ccabuc`, `xzabu`, …). Prune is scoped to your own ns so machines never delete each other's skills.

Full mechanism / troubleshooting: Anytype 技術文件 «Claude memory／skills 多機 Anytype 同步機制».
