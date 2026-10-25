"""把 llm_bench 的多份结果合并成一张对比表（Markdown）。

用法：
    python3 scripts/llm_report.py /tmp/llm_bench.json /tmp/llm_bench_glm.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ORDER = ["deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3", "glm-5.3-flash"]


def load(paths: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in paths:
        try:
            d = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ 跳过 {p}: {e}")
            continue
        for row in d.get("summary") or []:
            out[row["model"]] = row
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    data = load(sys.argv[1:])
    if not data:
        print("没有可用结果")
        return 1
    keys = [m for m in ORDER if m in data] + \
           [m for m in data if m not in ORDER]

    print("| 模型 | JSON 合规 | engine 合规 | 延迟(秒) | 镜头数 | "
          "台词保真 | 中文占比 | 提示词均长 | 输出 token | 推理 token |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for m in keys:
        r = data[m]
        print(f"| {r.get('name') or m} | {r.get('json_ok','-')} | "
              f"{r.get('engine_ok','-')} | {r.get('latency_s','-')} | "
              f"{r.get('shots','-')} | {r.get('dialogue_kept','-')} | "
              f"{r.get('zh_ratio','-')} | {r.get('prompt_len','-')} | "
              f"{r.get('out_tokens','-')} | {r.get('reasoning_tokens','-')} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
