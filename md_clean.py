"""把 Anytype 匯出的 markdown 清成「可以安全寫回去」的乾淨 markdown。

依據 memory anytype-patch-markdown-updates-body 的五個雷：
  1. 每個表格儲存格尾端的 "   <br>" 是匯出產物，不剝掉每輪會多疊一層
  2. inline code 裡的 _ 被 escape 成 \\_，同理
  3. 分隔線匯出成 " --- "（前後有空白），行首錨定的 regex 抓不到
  4. 表格若原封不動送回去會整個散掉 —— 要自己重寫成乾淨的 pipe table
  5. 段落含 astral-plane emoji 時，該段的 ** 與反引號每 round-trip 位移一格
     → 根治＝換成 BMP 範圍的字元
code fence 內一律不動。
"""
import re, sys

# 全部都是 BMP（< U+10000），語意盡量貼近原本那個
ASTRAL = {
    "🛑": "⛔", "🚩": "⚑", "🍎": "⌘", "🔗": "➜", "🔑": "★", "🔬": "⚗",
    "📱": "▣", "💡": "☀", "🎯": "◎", "🧪": "⚗", "🚀": "▲", "📦": "▤",
    "🔴": "●", "🟠": "◐", "🟢": "◉", "🔵": "◈", "🟡": "◌", "🔥": "‼", "📌": "▸",
    # 2026-09-17 補：盤查全庫發現這 18 種還在 astral 範圍，帶著它們的段落
    # 每寫回一次 ** 與反引號就右移一格，兩份 JGB 大文件已經被推歪上百行。
    "📍": "⌖", "🔒": "⚿", "🔐": "⚿", "🔧": "⚒", "🔔": "❢", "🔁": "↻",
    "🕐": "◷", "🕳": "○", "🚫": "⊘", "🪞": "▢", "📎": "❏", "📖": "▦",
    "🏠": "⌂", "🏢": "▥", "🖥": "⌨", "🖧": "⚯", "💻": "▭", "🤖": "☻",
    "🎁": "❖", "🍷": "☕",
}

def clean(md: str) -> str:
    out, fence = [], False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            # ⚠️ 關閉 fence 前先把尾端空行收掉：Anytype 每 round-trip 會在
            #    code block 結尾自己多塞一行，不收的話每 PATCH 一次就多一行。
            if fence:
                while out and not out[-1].strip():
                    out.pop()
            fence = not fence
            out.append(line.rstrip())
            continue
        if fence:
            out.append(line)
            continue

        s = line.rstrip()
        # 分隔線：一律拉齊成 ---（用 fullmatch，因為匯出是 " --- "）
        if re.fullmatch(r"\s*-{3,}\s*", s):
            out.append("---")
            continue
        # 表格：整列重寫，每格 strip、剝掉尾端 <br>
        if s.lstrip().startswith("|"):
            # ⚠️ 只在「沒被跳脫」的 | 上切格。cell 內容裡的 `ps -ef \| grep` 是
            #    合法的表格跳脫，照 | 硬切會把那一格之後的內容整個吃掉
            #    （2026-09-17 實際弄丟兩格才發現）。
            body = re.sub(r"^\s*\||\|\s*$", "", s.strip())
            cells = re.split(r"(?<!\\)\|", body)
            cells = [re.sub(r"\s*<br>\s*$", "", c).strip() for c in cells]
            # ⚠️ escape 也要在這裡收：表格分支在下面 continue 掉了，
            #    只在普通行收的話，版本紀錄那張表裡的 \_ 會一輪比一輪多。
            cells = [c.replace("\\_", "_").replace("\\*", "*") for c in cells]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                cells = [":---" for _ in cells]
            out.append("| " + " | ".join(cells) + " |")
            continue
        # escape 收回來
        s = s.replace("\\_", "_").replace("\\*", "*")
        out.append(s)
    txt = "\n".join(out)
    for a, b in ASTRAL.items():
        txt = txt.replace(a, b)
    # 尾端多餘空行收掉
    return txt.rstrip("\n") + "\n"

if __name__ == "__main__":
    sys.stdout.write(clean(open(sys.argv[1], encoding="utf-8").read()))
