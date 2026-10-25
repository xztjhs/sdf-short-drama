"""设置 LLM 参数（安全：只改 llm 段里指定的键，保留其余内容与注释）。"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import time

TARGET = {
    "default_model": "deepseek-v4.1-flash",
    "fallback_model": "glm-5.3",
    "temperature": "0.5",
    # 40 镜分镜实测约 12-16K 输出 token，16K 会贴边被截断 → 给 32K 余量
    "max_tokens": "32768",
    "timeout_sec": "600",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/config")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    p = pathlib.Path(a.config) / "prompts.yaml"
    s = p.read_text(encoding="utf-8")
    # llm 段可能在文件末尾（后面没有其它顶层键）→ 用 \Z 兜底
    m = re.search(r"^llm:\s*$([\s\S]*?)(?=^\S|\Z)", s, re.M)
    if not m:
        print("❌ 找不到 llm: 段")
        return 1
    block = m.group(1)
    new = block
    notes = []
    for k, v in TARGET.items():
        pat = rf"^(\s+{k}:\s*).*$"
        if re.search(pat, block, re.M):
            old = re.search(pat, block, re.M).group(2) if False else \
                re.search(rf"^\s+{k}:\s*(.*)$", block, re.M).group(1)
            if old.strip() != v:
                new = re.sub(pat, rf"\g<1>{v}", new, count=1, flags=re.M)
                notes.append(f"~ {k}: {old.strip()} → {v}")
        else:
            new = new.rstrip("\n") + f"\n  {k}: {v}\n"
            notes.append(f"+ {k}: {v}")
    if not notes:
        print("✓ 无需改动")
        return 0
    for n in notes:
        print(n)
    if a.dry_run:
        print("（dry-run，未写入）")
        return 0
    bak = p.with_name(f"{p.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    p.write_text(s[:m.start(1)] + new + s[m.end(1):], encoding="utf-8")
    print(f"备份 {bak.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
