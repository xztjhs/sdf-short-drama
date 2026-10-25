"""v0.3 → v0.4 配置迁移。

为什么需要这个脚本：
  `deploy/sync_deploy.sh` 对 config/ 只用 --ignore-existing（保护老板界面上改过的值），
  `scripts/merge_config.py` 做深合并但**不会改已有的 list**。
  而 v0.4 的关键变更正好有两处是 list / 已有值：

    1. routing.rules 要在最前面插入一条 `storyboard_engine`（尊重分镜指定的引擎）
    2. routing.cost.h3 的耗时基线从 10.6/9.0 改为实测 15.4/13.0（否则 ETA 少算一半）

  这个脚本只做「精确、可回滚」的补丁：
    - 缺什么补什么；已有的用户值一律保留
    - 改 routing.rules 时只插入缺失项，不动其它规则
    - cost 只在仍是旧默认值时替换
    - 每个改动的文件先备份 *.bak.YYYYmmdd_HHMMSS

用法（.253 上）：
    docker exec sdf-api python /app/migrate_v04.py --config /config --dry-run
    docker exec sdf-api python /app/migrate_v04.py --config /config
    # 然后重载配置（或重启 api）
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

import yaml

STORYBOARD_RULE = {
    "id": "storyboard_engine",
    "name": "分镜指定",
    "when": "shot_engine",
    "engine": "{shot_engine}",
    "reason": "分镜脚本指定",
}

OLD_H3_COST = {"768x1344": 10.6, "720x1280": 9.0, "544x960": 4.7}
NEW_H3_COST = {"768x1344": 15.4, "720x1280": 13.0, "544x960": 6.5}


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _save(p: Path, data: dict, dry: bool) -> str | None:
    if dry:
        return None
    bak = p.with_name(f"{p.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    p.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    return bak.name


def _ensure_scalar(text: str, key: str, value: str) -> tuple[str, bool]:
    """文件顶层没有 key: 就追加一行（保持原有注释不动）。"""
    import re
    if re.search(rf"^{re.escape(key)}\s*:", text, re.M):
        return text, False
    # 找到 default_engine 行后插入，保持可读性
    m = re.search(r"^default_engine\s*:.*$", text, re.M)
    ins = f"\n{key}: {value}\n"
    if m:
        return text[:m.end()] + ins + text[m.end():], True
    return text.rstrip() + f"\n\n{key}: {value}\n", True


def patch_routing(path: Path, dry: bool) -> list[str]:
    """文本级补丁：插入规则/开关、修正 cost。

    刻意不用 yaml.safe_dump 重写整个文件 —— 那会把注释全丢掉。
    同时兼容两种写法：`  - id:`（缩进两条）与 `- id:`（顶格，safe_dump 产物）。
    """
    import re
    notes: list[str] = []
    text = path.read_text(encoding="utf-8")

    # 探测 rules 列表项的缩进
    m_item = re.search(r"^( *)- id: ", text, re.M)
    item_indent = m_item.group(1) if m_item else "  "
    # 探测字段缩进（比列表项多两格）
    field_indent = item_indent + "  "

    text, added = _ensure_scalar(text, "respect_shot_engine", "true")
    if added:
        notes.append("+ routing.respect_shot_engine: true")

    if re.search(r"^engine_modes\s*:", text, re.M) is None:
        block = (
            "\n# 渲染模式 → 给 LLM 的分镜规则（对应 prompts.yaml 的 prompts.storyboard_rules.*）\n"
            "engine_modes:\n"
            "  auto:\n"
            "    zh: 自动路由（按镜头内容分配 H3 / Wan2.2）\n"
            "    default: true\n"
            "  h3:\n"
            "    zh: 全部 MiniMax H3（原生对白 + 音效）\n"
            "  wan22:\n"
            "    zh: 全部 Wan2.2（纯画面 + 字幕，最快）\n")
        m = re.search(r"^respect_shot_engine\s*:.*$", text, re.M)
        at = m.end() if m else len(text)
        text = text[:at] + block + text[at:]
        notes.append("+ routing.engine_modes（自动/H3/Wan2.2）")

    if "id: storyboard_engine" not in text:
        block = (
            f"\n{item_indent}# 分镜阶段就定好的引擎（v0.4）：LLM 已按「有对白→H3 / 空镜动作→5B」拆好\n"
            f"{item_indent}- id: storyboard_engine\n"
            f"{field_indent}name: 分镜指定\n"
            f"{field_indent}when: shot_engine\n"
            f'{field_indent}engine: "{{shot_engine}}"\n'
            f"{field_indent}reason: 分镜脚本指定\n")
        if m_item:
            # 找到 force 规则块的结束（下一条列表项之前）
            m_force = re.search(rf"^{re.escape(item_indent)}- id: force\s*$",
                                text, re.M)
            at = None
            if m_force:
                nxt = re.search(rf"^{re.escape(item_indent)}- id: ",
                                text[m_force.end():], re.M)
                at = m_force.end() + nxt.start() if nxt else len(text)
            if at is None:
                nxt = re.search(rf"^{re.escape(item_indent)}- id: ",
                                text[m_item.start() + 1:], re.M)
                at = m_item.start() + 1 + nxt.start() if nxt else len(text)
            text = text[:at] + block + text[at:]
        else:
            m2 = re.search(r"^rules:\s*$", text, re.M)
            at = m2.end() if m2 else len(text)
            text = text[:at] + "\n" + block + text[at:]
        notes.append("+ routing.rules: storyboard_engine（尊重分镜指定的引擎）")

    # cost 修正：只替换仍是旧默认值的行
    cost_h3 = re.search(r"^cost:\s*\n(?:[ \t].*\n)*?[ \t]+h3:\s*\n"
                        r"((?:[ \t]+[^\n]*\n)+)", text, re.M)
    if cost_h3:
        body = cost_h3.group(1)
        new_body = body
        for k, old_v in (("768x1344", 10.6), ("720x1280", 9.0), ("544x960", 4.7)):
            new_v = NEW_H3_COST[k]
            new_body = re.sub(
                rf"(^[ \t]+{re.escape(k)}:[ \t]*){re.escape(str(old_v))}([ \t]*$)",
                rf"\g<1>{new_v}\g<2>", new_body, flags=re.M)
        if new_body != body:
            text = text[:cost_h3.start(1)] + new_body + text[cost_h3.end(1):]
            notes.append("~ routing.cost.h3 修正为实测值（15.4/13.0/6.5 分钟）")

    if notes:
        # 写入前自检：解析不过就拒绝写（绝不把配置写坏）
        try:
            yaml.safe_load(text)
        except Exception as e:
            notes.append(f"!! 生成的 YAML 解析失败，跳过写入: {e}")
            return notes
        if not dry:
            bak = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(path, bak)
            path.write_text(text, encoding="utf-8")
            notes.append(f"备份 {bak.name}")
    return notes


def _find_default_prompts() -> Path | None:
    """找内置默认 prompts.yaml（容器 /opt/sdf-default-config，或仓库 config_defaults/）。"""
    for c in (Path("/opt/sdf-default-config/prompts.yaml"),
              Path(__file__).resolve().parent.parent / "config_defaults" / "prompts.yaml",
              Path(__file__).resolve().parent.parent / "config" / "prompts.yaml"):
        if c.exists():
            return c
    return None


def patch_prompts(path: Path, dry: bool) -> list[str]:
    """补齐 v0.4 中文模板。

    策略：用「文本片段插入」而不是重写 YAML——
    老板在后台改过的提示词、注释都要原样保留。
    段落来源：内置默认 prompts.yaml 里对应的模板块。
    """
    notes: list[str] = []
    text = path.read_text(encoding="utf-8")

    if "language:" not in text:
        import re
        m = re.search(r"^llm:\s*$", text, re.M)
        ins = "\n# 提示词语言：zh（默认，推荐）| en\nlanguage: zh\n"
        at = m.start() if m else 0
        text = text[:at] + ins + text[at:]
        notes.append("+ prompts.language: zh")

    src = _find_default_prompts()
    if src is None:
        notes.append("! 找不到默认 prompts.yaml，无法补齐中文模板")
    else:
        dflt_text = src.read_text(encoding="utf-8")
        for key in ("storyboard_v2", "storyboard_rules", "baseinfo_v2", "enhance_v2"):
            if f"\n  {key}:" in text:
                continue
            block = _extract_block(dflt_text, key)
            if not block:
                notes.append(f"! 默认文件里没有 {key}")
                continue
            anchor = _insert_anchor(text, key)
            text = text[:anchor] + block + text[anchor:]
            notes.append(f"+ prompts.{key}（中文模板）")

    if any(n.startswith("+") for n in notes):
        try:
            yaml.safe_load(text)
        except Exception as e:
            notes.append(f"!! 生成的 YAML 解析失败，跳过写入: {e}")
            return notes
        if not dry:
            bak = path.with_name(f"{path.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
            shutil.copy2(path, bak)
            path.write_text(text, encoding="utf-8")
            notes.append(f"备份 {bak.name}")
    return notes


def _extract_block(text: str, key: str) -> str:
    """从 YAML 文本里抠出 `  <key>:` 这一整块（到下一个同级 key 或文件尾）。"""
    import re
    m = re.search(rf"^  {re.escape(key)}:\s*$", text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^  [A-Za-z_][\w-]*:\s*$", rest, re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    block = text[m.start():end]
    if not block.endswith("\n"):
        block += "\n"
    return "\n" + block


def _insert_anchor(text: str, key: str) -> int:
    """决定新模板插在哪儿：紧跟相关旧模板之后，读起来顺。"""
    import re
    order = {
        "storyboard_v2": ["storyboard"],
        "storyboard_rules": ["storyboard_v2", "storyboard"],
        "baseinfo_v2": ["baseinfo"],
        "enhance_v2": ["enhance"],
    }
    for anchor_key in order.get(key, []):
        m = re.search(rf"^  {re.escape(anchor_key)}:\s*$", text, re.M)
        if not m:
            continue
        rest = text[m.end():]
        nxt = re.search(r"^  [A-Za-z_][\w-]*:\s*$", rest, re.M)
        return m.end() + nxt.start() if nxt else len(text)
    return len(text)


def patch_settings(path: Path, dry: bool) -> list[str]:
    notes: list[str] = []
    d = _load(path)
    render = d.setdefault("render", {})
    if "queue_autostart" not in render:
        render["queue_autostart"] = True
        notes.append("+ settings.render.queue_autostart = true")
    asm = d.setdefault("assemble", {})
    if "vcodec" not in asm:
        asm["vcodec"] = "auto"
        notes.append("+ settings.assemble.vcodec = auto（有 NVENC 就用）")
    if notes:
        bak = _save(path, d, dry)
        if bak:
            notes.append(f"备份 {bak}")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/config")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = Path(a.config)
    if not root.exists():
        print(f"❌ 配置目录不存在: {root}")
        return 1

    total = 0
    for name, fn in (("routing.yaml", patch_routing),
                     ("prompts.yaml", patch_prompts),
                     ("settings.yaml", patch_settings)):
        p = root / name
        if not p.exists():
            print(f"⚠️ {name} 不存在，跳过")
            continue
        try:
            notes = fn(p, a.dry_run)
        except Exception as e:
            print(f"❌ {name}: {type(e).__name__}: {e}")
            continue
        if not notes:
            print(f"✓ {name} 无需改动")
            continue
        total += len([n for n in notes if n[0] in "+~"])
        print(f"→ {name}")
        for n in notes:
            print("   " + n)

    print(("（dry-run，未写入）" if a.dry_run else "") +
          f" 合计 {total} 处变更")
    return 0 if total or a.dry_run else 0


if __name__ == "__main__":
    sys.exit(main())
