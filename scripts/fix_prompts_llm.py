"""修复 prompts.yaml 丢失的 llm / language 段（v0.5.1）。

背景：后台「提示词管理」保存时用 cfg.write_section("prompts", req)，
如果前端只回传 prompts 子键，整个文件会被重写成只剩 prompts，
llm（base_url / 模型 / temperature / max_tokens）与 language 全部丢失 →
系统悄悄回落到代码里的默认模型，改配置完全不生效。

本脚本：
  1. 保留现有 prompts.* 模板（用户可能改过）
  2. 补齐 llm 段（用实测推荐值）与 language: zh
  3. 写前 YAML 校验，自动备份
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import time

import yaml

LLM = {
    "base_url": "https://api.icompify.com/v1",
    # 实测：27 秒/次，质量与 4 Pro 打平（见 docs/10-LLM选型对比.md）
    "default_model": "deepseek-v4.1-flash",
    # 降级：GLM 5.3 更细但慢（约 4.6 分钟/次），只在主力失败时用
    "fallback_model": "glm-5.3",
    "temperature": 0.5,
    # 40 镜分镜实测输出 12-16K token，16K 会贴边，给 32K 余量
    "max_tokens": 32768,
    "context_length": 262144,
    "timeout_sec": 600,
}

HEADER = """# ============================================================
# SDF 栈 · 提示词模板 + LLM 参数
#
# ⚠️ 本文件是「模板 + 参数」二合一：
#   llm:      LLM 连接与采样参数（改完点「重载配置」即生效）
#   language: 提示词语言，zh（默认）/ en
#   prompts:  各阶段提示词模板
#
# 注意：后台「提示词管理」保存时只写 prompts 段，
#       llm / language 由 cfg.write_section 的深合并保护。
# ============================================================

"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/config")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    p = pathlib.Path(a.config) / "prompts.yaml"
    if not p.exists():
        print(f"❌ 不存在: {p}")
        return 1
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    notes = []

    if not isinstance(d.get("llm"), dict):
        d["llm"] = dict(LLM)
        notes.append("+ 恢复 llm 段（base_url/模型/temperature/max_tokens）")
    else:
        for k, v in LLM.items():
            if k not in d["llm"]:
                d["llm"][k] = v
                notes.append(f"+ llm.{k}: {v}")

    if not d.get("language"):
        d["language"] = "zh"
        notes.append("+ language: zh")

    if not notes:
        print("✓ 无需修复")
        return 0
    for n in notes:
        print(n)
    if a.dry_run:
        print("（dry-run，未写入）")
        return 0

    text = HEADER + yaml.safe_dump(d, allow_unicode=True, sort_keys=False)
    yaml.safe_load(text)  # 自检
    bak = p.with_name(f"{p.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    p.write_text(text, encoding="utf-8")
    print(f"备份 {bak.name}")
    print(f"现有 prompts 键: {sorted((d.get('prompts') or {}).keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
