#!/usr/bin/env python3
"""把仓库 config/ 发布到 .253（合并式），保住运行时改过的值。

背景（2026-10-24 事故）：
  sync_deploy.sh 对 config/ 用 rsync --ignore-existing（当年为防止覆盖界面改值），
  结果连 v0.6 的正常发布也被挡住 —— 服务器配置停留在 v0.2 基线，
  11 个风格预设、storyboard_v2 八要素模板、assemble.subtitle 全都没生效。

策略（白名单式合并，不整文件覆盖）：
  · prompts.yaml   —— 按模板名合并：本地有则覆盖，服务器独有的模板保留
  · presets.yaml   —— styles 按 id 合并（本地为准）；platforms 按 id 合并；
                      monitor（监控栏，界面可勾选）保留服务器值
  · models.yaml / routing.yaml / engines.yaml —— 按顶层键合并，本地为准
  · settings.yaml  —— 除 RUNTIME_KEEP 白名单外的键全部以本地为准；
                      RUNTIME_KEEP 是界面里改的运行参数，必须保留服务器值
  · secrets.env    —— 完全不碰

每次写入前在服务器留 .bak.<时间戳>，写后 YAML 自检。

用法：
    python3 deploy/publish_config.py --dry-run     # 只看会改什么
    python3 deploy/publish_config.py               # 发布
    python3 deploy/publish_config.py --rebuild     # 发布并重载 api 配置
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

REMOTE = "YOUR_SERVER_IP"
PASSW = "CHANGE_ME"
DEST = "/mnt/disks/nvme/docker/compose/sdf/config"
SRC = Path(__file__).resolve().parent.parent / "config"

# settings.yaml 里「界面可改、必须保留服务器值」的键（点路径）
RUNTIME_KEEP = {
    "assemble.orientation",
    "assemble.width",
    "assemble.height",
    "assemble.fps",
    "assemble.delivery_width",
    "assemble.delivery_height",
    "assemble.crf",
    "assemble.preset",
    "assemble.lufs",
    "assemble.scale_mode",
    "assemble.burn_subtitles",
    "assemble.subtitle.font_size",
    "assemble.subtitle.margin_v",
    "assemble.subtitle.alignment",
    "assemble.subtitle.font_name",
    "assemble.subtitle.outline",
    "assemble.subtitle.shadow",
    "render.max_concurrent",
    "render.shot_timeout_sec",
    "render.poll_interval_sec",
    "render.cooldown_between_shots_sec",
    "render.max_retries",
    "render.queue_autostart",
    "storage.keep_shot_fragments",
    "logging.level",
    "admin.password",
    "ui.page_size",
    "ui.theme",
}


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ssh(cmd: str) -> str:
    r = sh(["sshpass", "-p", PASSW, "ssh", "-o", "StrictHostKeyChecking=no",
            f"root@{REMOTE}", cmd])
    if r.returncode != 0:
        raise RuntimeError(f"ssh 失败: {r.stderr[:300]}")
    return r.stdout


def pull(remote_name: str, local: Path) -> bool:
    r = sh(["sshpass", "-p", PASSW, "scp", "-o", "StrictHostKeyChecking=no",
            f"root@{REMOTE}:{DEST}/{remote_name}", str(local)])
    return r.returncode == 0


def push(local: Path, remote_name: str) -> None:
    r = sh(["sshpass", "-p", PASSW, "scp", "-o", "StrictHostKeyChecking=no",
            str(local), f"root@{REMOTE}:{DEST}/{remote_name}"])
    if r.returncode != 0:
        raise RuntimeError(f"scp 失败 {remote_name}: {r.stderr[:300]}")


def get_path(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISS
        cur = cur[part]
    return cur


def set_path(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


_MISS = object()


# ---------------------------------------------------------------- 合并器

def merge_prompts(server: dict, local: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    out = dict(server)
    sp, lp = server.get("prompts") or {}, local.get("prompts") or {}
    merged = dict(sp)
    for name, tpl in lp.items():
        if name not in sp:
            notes.append(f"+ prompts.{name}（新增模板）")
        elif sp[name] != tpl:
            notes.append(f"~ prompts.{name}（更新模板）")
        merged[name] = tpl
    for name in sp:
        if name not in lp:
            notes.append(f"= prompts.{name}（本地没有，保留服务器版）")
    out["prompts"] = merged
    # 其它顶层键（language 等）以本地为准
    for k, v in local.items():
        if k != "prompts":
            out[k] = v
    return out, notes


def merge_presets(server: dict, local: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    out = dict(server)

    # styles：按 id 合并，本地为准
    ss = {x.get("id"): x for x in (server.get("styles") or []) if isinstance(x, dict)}
    ls = [x for x in (local.get("styles") or []) if isinstance(x, dict)]
    merged_styles = []
    for item in ls:
        i = item.get("id")
        if i not in ss:
            notes.append(f"+ styles.{i}（新增风格）")
        elif ss[i].get("render") != item.get("render"):
            notes.append(f"~ styles.{i}.render（{len(str(ss[i].get('render','')))}→"
                         f"{len(str(item.get('render','')))} 字）")
        merged_styles.append(item)
    for i in ss:
        if i not in {x.get("id") for x in ls}:
            notes.append(f"= styles.{i}（本地没有，保留）")
            merged_styles.append(ss[i])
    out["styles"] = merged_styles

    # platforms：按 id 合并，本地为准
    spf = {x.get("id"): x for x in (server.get("platforms") or []) if isinstance(x, dict)}
    lpf = [x for x in (local.get("platforms") or []) if isinstance(x, dict)]
    merged_pf = list(lpf)
    for i, v in spf.items():
        if i not in {x.get("id") for x in lpf}:
            notes.append(f"= platforms.{i}（本地没有，保留）")
            merged_pf.append(v)
    if merged_pf != (server.get("platforms") or []):
        notes.append("~ platforms（已合并）")
    out["platforms"] = merged_pf

    # monitor：界面可勾选的监控项 → 保留服务器值
    if server.get("monitor"):
        out["monitor"] = server["monitor"]
        notes.append("= monitor（监控栏配置，保留服务器值）")

    for k, v in local.items():
        if k not in ("styles", "platforms", "monitor"):
            out[k] = v
    return out, notes


def merge_settings(server: dict, local: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    import copy
    out = copy.deepcopy(local)
    for key in sorted(RUNTIME_KEEP):
        sv = get_path(server, key)
        if sv is _MISS:
            continue
        lv = get_path(local, key)
        if lv is _MISS or lv != sv:
            set_path(out, key, sv)
            notes.append(f"= settings.{key} 保留服务器值 {sv!r}"
                         + (f"（本地 {lv!r}）" if lv is not _MISS else ""))
    # 本地没有、服务器有的键，保留（防丢）
    def walk(s, l, prefix=""):
        for k, v in s.items():
            kp = f"{prefix}.{k}" if prefix else k
            if not isinstance(l, dict) or k not in l:
                if isinstance(v, dict):
                    walk(v, {}, kp)
                else:
                    set_path(out, kp, v)
                    notes.append(f"= settings.{kp} 服务器独有，保留")
            elif isinstance(v, dict) and isinstance(l.get(k), dict):
                walk(v, l[k], kp)
    walk(server, local)
    return out, notes


def _fmt_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return "''"
    if any(c in s for c in ":#{}[],&*?|-<>=!%@`\"'") or s.strip() != s:
        return "'" + s.replace("'", "''") + "'"
    return s


def _line_paths(lines: list[str]) -> list[tuple[str, int]]:
    """为每行算出它在 YAML 里的点路径（按缩进栈推）。"""
    out: list[tuple[str, int]] = []
    stack: list[tuple[int, str]] = []      # (indent, key)
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(("", i))
            continue
        indent = len(line) - len(line.lstrip(" "))
        m = re.match(r"^([A-Za-z_][\w.-]*)\s*:", line.lstrip())
        if not m:
            out.append(("", i))
            continue
        key = m.group(1)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = ".".join([k for _, k in stack] + [key])
        stack.append((indent, key))
        out.append((path, i))
    return out


def patch_settings_text(local_text: str, server: dict) -> tuple[str, list[str]]:
    """在保留注释的前提下，把服务器上的运行时值写回本地文本。

    为什么不用 yaml.safe_dump：settings.yaml 里的注释是运维决策记录
    （v0.5 为何回 720p、chain_frames 为何关、Ref2VA 说明），丢掉等于丢文档。
    """
    notes: list[str] = []
    lines = local_text.split("\n")

    try:
        local_d = yaml.safe_load(local_text) or {}
    except Exception:
        local_d = {}
    if not isinstance(local_d, dict):
        local_d = {}

    changes: dict[str, object] = {}
    for key in sorted(RUNTIME_KEEP):
        sv = get_path(server, key)
        if sv is _MISS:
            continue
        lv = get_path(local_d, key)
        if lv is not _MISS and lv == sv:
            continue
        changes[key] = sv

    # 服务器独有、本地没有的键（如 assemble.delivery_width/height）
    extra: dict[str, object] = {}

    def walk(s, l, prefix=""):
        for k, v in s.items():
            kp = f"{prefix}.{k}" if prefix else k
            if not isinstance(l, dict) or k not in l:
                if isinstance(v, dict):
                    walk(v, {}, kp)
                else:
                    extra[kp] = v
            elif isinstance(v, dict) and isinstance(l.get(k), dict):
                walk(v, l[k], kp)

    walk(server, local_d)

    paths = _line_paths(lines)
    for key, val in changes.items():
        hit = None
        for path, idx in paths:
            if path == key:
                hit = idx
        if hit is None:
            continue
        raw = lines[hit].lstrip()
        m = re.match(r"^([A-Za-z_][\w.-]*)\s*:(.*)$", raw)
        if not m:
            continue
        indent = " " * (len(lines[hit]) - len(lines[hit].lstrip(" ")))
        new_line = f"{indent}{m.group(1)}: {_fmt_scalar(val)}"
        if lines[hit] != new_line:
            lines[hit] = new_line
            notes.append(f"= settings.{key} 保留服务器值 {val!r}")

    # 追加服务器独有键到对应父块末尾
    for key, val in extra.items():
        parent = ".".join(key.split(".")[:-1])
        leaf = key.split(".")[-1]
        anchor = None
        for path, idx in paths:
            if path == parent:
                anchor = idx
        if anchor is None:
            notes.append(f"! settings.{key} 找不到父块 {parent}，跳过")
            continue
        # 找父块结束位置（下一个缩进 <= 父块缩进的有效行）
        base_indent = len(lines[anchor]) - len(lines[anchor].lstrip(" "))
        block_end = anchor + 1
        while block_end < len(lines):
            ln = lines[block_end]
            if not ln.strip() or ln.lstrip().startswith("#"):
                block_end += 1
                continue
            if len(ln) - len(ln.lstrip(" ")) <= base_indent:
                break
            block_end += 1
        # 从块尾回退到最后一行有效内容之后（这样能跳过子块的子孙行）
        end = block_end
        probe = block_end - 1
        while probe > anchor:
            ln = lines[probe]
            if ln.strip() and not ln.lstrip().startswith("#"):
                end = probe + 1
                break
            probe -= 1
        note = ("  # 超分默认档（服务器运行时设定）")
        lines.insert(end, f"{' ' * (base_indent + 2)}{leaf}: {_fmt_scalar(val)}{note}")
        notes.append(f"+ settings.{key} = {val!r}（服务器独有，补回）")
        paths = _line_paths(lines)      # 行号变了，重算

    return "\n".join(lines), notes


def merge_flat(server: dict, local: dict, fname: str) -> tuple[dict, list[str]]:
    notes: list[str] = []
    out = dict(server)
    for k, v in local.items():
        if k not in server:
            notes.append(f"+ {fname}.{k}（新增）")
        elif server[k] != v:
            notes.append(f"~ {fname}.{k}（以本地为准）")
        out[k] = v
    for k in server:
        if k not in local:
            notes.append(f"= {fname}.{k}（本地没有，保留）")
    return out, notes


# 文件 → 合并函数
MERGERS = {
    "prompts.yaml": merge_prompts,
    "presets.yaml": merge_presets,
    "settings.yaml": merge_settings,
    "models.yaml": lambda s, l: merge_flat(s, l, "models"),
    "routing.yaml": lambda s, l: merge_flat(s, l, "routing"),
    "engines.yaml": lambda s, l: merge_flat(s, l, "engines"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="发布后重载 api 配置")
    a = ap.parse_args()

    if not SRC.exists():
        print(f"❌ 本地配置目录不存在: {SRC}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="sdfcfg_"))
    total = 0
    changed: list[str] = []

    for fname, merger in MERGERS.items():
        lp = SRC / fname
        if not lp.exists():
            print(f"⚠️ {fname}: 本地缺失，跳过")
            continue
        sp = tmp / f"{fname}.server"
        if not pull(fname, sp):
            print(f"❌ {fname}: 拉取服务器版本失败")
            continue
        try:
            server = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
            local = yaml.safe_load(lp.read_text(encoding="utf-8")) or {}
        except Exception as e:
            print(f"❌ {fname}: YAML 解析失败 {e}")
            continue

        print(f"→ {fname}")
        if fname == "settings.yaml":
            # 走文本补丁：保住运维决策注释（不做 yaml dump）
            local_text = lp.read_text(encoding="utf-8")
            buf, notes = patch_settings_text(local_text, server)
            for n in notes:
                print(f"   {n}")
            if not notes:
                print("   ✓ 无需变更")
                continue
        else:
            merged, notes = merger(server, local)
            for n in notes:
                print(f"   {n}")
            if merged == server:
                print("   ✓ 无需变更")
                continue
            buf = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False,
                                 default_flow_style=False, width=1000)

        # YAML 自检：结果必须能解析，且顶层键不能比服务器少
        try:
            back = yaml.safe_load(buf)
            assert isinstance(back, dict)
            missing = set(server) - set(back)
            assert not missing, f"丢失顶层键 {missing}"
        except Exception as e:
            print(f"   ❌ 自检失败，跳过写入: {e}")
            continue

        total += 1
        changed.append(fname)
        if a.dry_run:
            print(f"   （dry-run 未写入，{len(buf)} 字节）")
            continue

        outp = tmp / f"{fname}.new"
        header = (f"# 由 deploy/publish_config.py 于 "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')} 发布\n")
        if fname == "settings.yaml":
            outp.write_text(header + buf, encoding="utf-8")
        else:
            outp.write_text(
                header + f"# 源: 仓库 config/{fname}（界面改的运行参数已保留）\n" + buf,
                encoding="utf-8")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        ssh(f"cp -a {DEST}/{fname} {DEST}/{fname}.bak.{stamp}")
        push(outp, fname)
        print(f"   ✅ 已发布（服务器备份 {fname}.bak.{stamp}）")

    print()
    if a.dry_run:
        print(f"dry-run：{total} 个文件待发布")
        return 0
    print(f"发布完成：{total} 个文件" + (f"（{', '.join(changed)}）" if changed else ""))

    if a.rebuild and changed:
        print("重载 api 配置…")
        try:
            ssh("cd /mnt/disks/nvme/docker/compose/sdf && docker compose -p sdf restart api")
            print("✅ api 已重启")
        except Exception as e:
            print(f"⚠️ 重启失败，请手动处理: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
