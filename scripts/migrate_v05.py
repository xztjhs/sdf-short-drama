"""v0.4 → v0.5 配置迁移。

v0.5 变更：
  1. 只保留 MiniMax H3：models.yaml 删除 wan2.2 三条、routing 收敛到 h3
  2. 成片画布回归 720p（settings.assemble.width/height），新增 orientation
  3. 平台预设支持横竖屏（presets.platforms[].orientation / base）
  4. 风格预设改中文渲染提示词（presets.styles[].render）

用法（容器内）：
    docker exec sdf-api python /app/migrate_v05.py --config /config --dry-run
    docker exec sdf-api python /app/migrate_v05.py --config /config
    curl -X POST localhost:8080/api/admin/config/reload
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path

import yaml

DEFAULTS_CANDIDATES = (
    Path("/opt/sdf-default-config"),
    Path(__file__).resolve().parent.parent / "config_defaults",
    Path(__file__).resolve().parent.parent / "config",
)

WAN22_MODEL_IDS = {"wan22-ti2v-5b", "wan22-clip", "wan22-vae"}


def _defaults_dir() -> Path | None:
    for c in DEFAULTS_CANDIDATES:
        if (c / "presets.yaml").exists():
            return c
    return None


def _backup(p: Path) -> str:
    bak = p.with_name(f"{p.name}.bak.{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    return bak.name


def _write(p: Path, text: str, dry: bool, notes: list[str]) -> None:
    try:
        yaml.safe_load(text)
    except Exception as e:
        notes.append(f"!! YAML 校验失败，未写入: {e}")
        return
    if dry:
        return
    notes.append(f"备份 {_backup(p)}")
    p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 1. settings
def patch_settings(path: Path, dry: bool) -> list[str]:
    notes: list[str] = []
    text = path.read_text(encoding="utf-8")

    # 定位 assemble 块
    m = re.search(r"^assemble:\s*$", text, re.M)
    if not m:
        notes.append("! settings.yaml 没有 assemble 块，跳过")
        return notes
    nxt = re.search(r"^[A-Za-z_]", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    block = text[m.end():end]
    new_block = block

    # 画布 720p（只在仍是旧默认 1080/1920 时改，避免覆盖手调值）
    if re.search(r"^\s+width:\s*1080\s*$", new_block, re.M) and \
       re.search(r"^\s+height:\s*1920\s*$", new_block, re.M):
        new_block = re.sub(r"^(\s+)width:\s*1080\s*$", r"\g<1>width: 720",
                           new_block, flags=re.M)
        new_block = re.sub(r"^(\s+)height:\s*1920\s*$", r"\g<1>height: 1280",
                           new_block, flags=re.M)
        notes.append("~ assemble 画布 1080×1920 → 720×1280（不再隐式放大）")

    if not re.search(r"^\s+orientation:\s*\S+", new_block, re.M):
        new_block = re.sub(r"^(assemble:\s*)$", r"\1", new_block)
        new_block = new_block.replace("\n", "\n  orientation: portrait\n", 1) \
            if new_block.startswith("\n") else "\n  orientation: portrait" + new_block
        notes.append("+ assemble.orientation: portrait")

    if not re.search(r"^\s+delivery_width:", new_block, re.M):
        new_block = new_block.rstrip("\n") + \
            "\n  delivery_width: 1080\n  delivery_height: 1920\n"
        notes.append("+ assemble.delivery_width/height（超分默认档）")

    if new_block != block:
        _write(path, text[:m.end()] + new_block + text[end:], dry, notes)
    return notes


# ---------------------------------------------------------------- 2. models
def patch_models(path: Path, dry: bool) -> list[str]:
    notes: list[str] = []
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    models = d.get("models") or []
    kept, removed = [], []
    for m in models:
        if m.get("id") in WAN22_MODEL_IDS:
            removed.append(m.get("id"))
        else:
            kept.append(m)
    if not removed:
        return ["✓ models.yaml 已无 wan2.2 条目"]
    d["models"] = kept
    notes.append(f"- 删除 wan2.2 模型条目: {', '.join(removed)}")
    text = yaml.safe_dump(d, allow_unicode=True, sort_keys=False)
    header = ("# SDF 模型注册表（v0.5：只保留 MiniMax H3）\n"
              "# wan2.2 三条已归档：权重移到 models_backup/wan22-20261022/，\n"
              "# 清单见 Gitea admin/sdf-model-archive\n\n")
    _write(path, header + text, dry, notes)
    return notes


# ---------------------------------------------------------------- 3. routing
def patch_routing(path: Path, dry: bool, dflt: Path | None) -> list[str]:
    notes: list[str] = []
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    changed = False
    if d.get("default_engine") != "h3":
        d["default_engine"] = "h3"
        notes.append("~ default_engine → h3")
        changed = True
    if d.get("allowed_engines") != ["h3"]:
        d["allowed_engines"] = ["h3"]
        notes.append("+ allowed_engines: [h3]（旧分镜残留 wan22 会被纠正）")
        changed = True
    modes = d.get("engine_modes") or {}
    if set(modes.keys()) - {"h3"}:
        d["engine_modes"] = {"h3": {"zh": "全部 MiniMax H3（原生对白 + 音效）",
                                    "default": True}}
        notes.append("~ engine_modes 收敛为 h3")
        changed = True

    # 规则：去掉指向 wan22 的规则，保留 force / storyboard_engine / fallback
    rules = d.get("rules") or []
    new_rules = []
    for r in rules:
        eng = str(r.get("engine") or "")
        if eng == "wan22":
            continue
        new_rules.append(r)
    if not any(r.get("id") == "fallback" for r in new_rules):
        new_rules.append({"id": "fallback", "name": "兜底", "when": "always",
                          "engine": "h3", "reason": "唯一引擎 H3"})
    if len(new_rules) != len(rules):
        d["rules"] = new_rules
        notes.append(f"~ 移除 {len(rules) - len(new_rules)} 条路由到 wan2.2 的规则")
        changed = True

    if d.get("resolutions", {}).get("wan22"):
        d["resolutions"].pop("wan22", None)
        notes.append("~ 删除 resolutions.wan22")
        changed = True
    if d.get("cost", {}).get("wan22"):
        d["cost"].pop("wan22", None)
        notes.append("~ 删除 cost.wan22")
        changed = True

    if changed:
        text = yaml.safe_dump(d, allow_unicode=True, sort_keys=False)
        header = ("# SDF 路由（v0.5：只保留 MiniMax H3，Wan2.2 已下线）\n"
                  "# 详细说明见仓库 config/routing.yaml\n\n")
        _write(path, header + text, dry, notes)
    return notes or ["✓ routing.yaml 已是 h3-only"]


# ---------------------------------------------------------------- 4. presets
def patch_presets(path: Path, dry: bool, dflt: Path | None) -> list[str]:
    notes: list[str] = []
    cur = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if dflt is None:
        return ["! 找不到默认 presets.yaml，跳过"]
    ref = yaml.safe_load((dflt / "presets.yaml").read_text(encoding="utf-8")) or {}

    # styles：给每个风格补中文 render（按 id 匹配，保留用户自定义项）
    ref_styles = {s.get("id"): s for s in (ref.get("styles") or [])}
    styles = cur.get("styles") or []
    added = 0
    for s in styles:
        if s.get("render"):
            continue
        r = ref_styles.get(s.get("id")) or {}
        if r.get("render"):
            s["render"] = r["render"]
            added += 1
    # 默认里有、当前没有的风格 → 补进去
    have = {s.get("id") for s in styles}
    for sid, s in ref_styles.items():
        if sid not in have:
            styles.append(s)
    if styles:
        cur["styles"] = styles
    if added:
        notes.append(f"~ {added} 个风格预设补中文渲染提示词")

    # platforms：补 orientation/base（横竖屏切换依赖它）
    ref_plats = {p.get("id"): p for p in (ref.get("platforms") or [])}
    plats = cur.get("platforms") or []
    n_orient = 0
    for p in plats:
        r = ref_plats.get(p.get("id")) or {}
        if not p.get("orientation"):
            if r.get("orientation"):
                p["orientation"] = r["orientation"]
            else:
                p["orientation"] = ("landscape"
                                    if (p.get("width") or 0) > (p.get("height") or 0)
                                    else "portrait")
            n_orient += 1
        if not p.get("base"):
            if r.get("base"):
                p["base"] = r["base"]
            else:
                portrait = p["orientation"] == "portrait"
                p["base"] = {"width": 720, "height": 1280} if portrait \
                    else {"width": 1280, "height": 720}
            n_orient += 1
    have_p = {p.get("id") for p in plats}
    for pid, p in ref_plats.items():
        if pid not in have_p:
            plats.append(p)
    if plats:
        cur["platforms"] = plats
    if n_orient:
        notes.append(f"~ {n_orient} 处平台预设补 orientation/base（支持横竖屏）")

    if notes:
        text = yaml.safe_dump(cur, allow_unicode=True, sort_keys=False)
        header = ("# SDF 预设库（v0.5：中文风格 + 横竖屏平台规格）\n"
                  "# 详细说明见仓库 config/presets.yaml\n\n")
        _write(path, header + text, dry, notes)
    return notes or ["✓ presets.yaml 已是最新结构"]



# ---------------------------------------------------------------- prompts
def _extract_block(text: str, key: str) -> str:
    m = re.search(rf"^  {re.escape(key)}:\s*$", text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^  [A-Za-z_][\w-]*:\s*$", rest, re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    block = text[m.start():end]
    return "\n" + block if block.endswith("\n") else "\n" + block + "\n"


def patch_prompts(path: Path, dry: bool, dflt: Path | None) -> list[str]:
    """补 v0.5 新增模板（localize_v2：旧资产中文化）。"""
    notes: list[str] = []
    if dflt is None:
        return ["! 找不到默认 prompts.yaml"]
    text = path.read_text(encoding="utf-8")
    src = (dflt / "prompts.yaml").read_text(encoding="utf-8")
    added = 0
    for key in ("localize_v2",):
        if f"\n  {key}:" in text:
            continue
        block = _extract_block(src, key)
        if not block:
            notes.append(f"! 默认里没有 {key}")
            continue
        m = re.search(r"^  translate:\s*$", text, re.M)
        at = m.start() if m else len(text)
        text = text[:at] + block.lstrip("\n") + "\n" + text[at:]
        added += 1
        notes.append(f"+ prompts.{key}（旧资产批量中文化模板）")
    if added:
        _write(path, text, dry, notes)
    return notes or ["✓ prompts.yaml 无需改动"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/config")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = Path(a.config)
    if not root.exists():
        print(f"❌ 配置目录不存在: {root}")
        return 1
    dflt = _defaults_dir()
    total = 0
    for name, fn in (("settings.yaml", lambda p, d: patch_settings(p, d)),
                     ("models.yaml", lambda p, d: patch_models(p, d)),
                     ("routing.yaml", lambda p, d: patch_routing(p, d, dflt)),
                     ("presets.yaml", lambda p, d: patch_presets(p, d, dflt)),
                     ("prompts.yaml", lambda p, d: patch_prompts(p, d, dflt))):
        p = root / name
        if not p.exists():
            print(f"⚠️ {name} 不存在，跳过")
            continue
        try:
            notes = fn(p, a.dry_run)
        except Exception as e:
            print(f"❌ {name}: {type(e).__name__}: {e}")
            continue
        if notes:
            print(f"→ {name}")
            for n in notes:
                print("   " + n)
                if n[0] in "+~-":
                    total += 1
    print(("（dry-run，未写入）" if a.dry_run else "") + f" 合计 {total} 处变更")
    return 0


if __name__ == "__main__":
    sys.exit(main())
