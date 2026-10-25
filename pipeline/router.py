"""镜头路由 —— 决定每个镜头由哪个引擎渲染。

规则全部来自 config/routing.yaml，不再硬编码。
改配置 → 重启 api（或管理界面点重载）即生效。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import cfg

H3 = "h3"
WAN22 = "wan22"


@dataclass
class RouteDecision:
    engine: str
    reason: str
    resolution: str
    steps: int
    priority: int = 5


def _kw(name: str) -> re.Pattern | None:
    pat = cfg.get("routing", f"keywords.{name}", "")
    if not pat:
        return None
    try:
        return re.compile(pat, re.I)
    except re.error:
        return None


def _match(name: str, text: str) -> bool:
    p = _kw(name)
    return bool(p and p.search(text or ""))


def _orientation() -> str:
    """成片朝向：portrait（竖屏，默认）| landscape（横屏）。"""
    v = str(cfg.get("settings", "assemble.orientation", "portrait") or "portrait").lower()
    return "landscape" if v.startswith("land") else "portrait"


def _swap_for_orientation(res: str) -> str:
    """routing 里的分辨率按竖屏写；横屏时交换宽高。

    为什么在这里做：一套 routing 配置同时支持横竖屏，换画幅只改
    settings.assemble.orientation，不用重写所有分辨率矩阵。
    """
    if _orientation() != "landscape":
        return res
    try:
        w, h = (int(x) for x in res.lower().split("x"))
        return f"{h}x{w}"
    except Exception:
        return res


def _resolution(engine: str, quality_first: bool, tier: str = "") -> str:
    tier = (tier or "").strip().lower()
    if tier not in ("quality", "normal", "draft"):
        tier = "quality" if quality_first else "normal"
    res = cfg.get("routing", f"resolutions.{engine}.{tier}")
    if res:
        return _swap_for_orientation(res)
    # 兜底
    return _swap_for_orientation("768x1344")


def _steps(engine: str, quality_first: bool, has_dialogue: bool) -> int:
    base = cfg.get("engines", f"engines.{engine}.steps", 8) or 8
    if quality_first or (engine == H3 and has_dialogue):
        q = cfg.get("engines", f"engines.{engine}.steps_quality")
        if q:
            return int(q)
    return int(base)


def route(shot: dict, *, quality_first: bool = False,
          forced_engine: str | None = None,
          tier: str = "") -> RouteDecision:
    """按 routing.yaml 的 rules 顺序匹配，命中即停。"""
    dialogue = (shot.get("dialogue") or "").strip()
    sfx = (shot.get("sfx") or "").strip()
    prompt = shot.get("prompt") or ""
    shot_size = (shot.get("shot_size") or "").strip()
    resolution_field = (shot.get("resolution") or "").strip()

    quality_sizes = cfg.get("routing", "quality_shot_sizes", []) or []
    vocal_words = cfg.get("routing", "vocal_sfx", []) or []

    # 分镜脚本指定的引擎（v0.4）：LLM 已按「有对白→H3 / 空镜动作→5B」拆好
    shot_engine = (shot.get("engine") or "").strip().lower()
    if shot_engine not in (H3, WAN22):
        shot_engine = ""
    respect_shot = bool(cfg.get("routing", "respect_shot_engine", True))

    def when(cond: str) -> bool:
        if cond == "forced_engine":
            return forced_engine in (H3, WAN22)
        if cond == "shot_engine":
            return bool(shot_engine) and respect_shot
        if cond == "has_dialogue":
            return bool(dialogue)
        if cond == "has_sfx":
            # 已被 vocal_sfx 取代，保留向后兼容
            return bool(sfx) and sfx not in ("无", "-", "none", "None")
        if cond == "matches_vocal_sfx":
            # 只有人物发声才走 H3；风声/雨声/脚步声等环境音后期 ffmpeg 配
            if not sfx:
                return False
            low = sfx.lower()
            return any(w.lower() in low for w in vocal_words)
        if cond == "matches_action":
            return _match("action", prompt)
        if cond == "matches_empty":
            return _match("empty", prompt)
        if cond == "matches_lowlight":
            return _match("lowlight", prompt)
        if cond == "quality_closeup":
            return bool(quality_first) and any(s in shot_size for s in quality_sizes)
        if cond == "always":
            return True
        return False

    rules = cfg.get("routing", "rules", []) or []
    engine = cfg.get("routing", "default_engine", H3)
    reason = "默认引擎"

    for rule in rules:
        cond = rule.get("when", "")
        if not when(cond):
            continue
        target = rule.get("engine", engine)
        if target == "{forced_engine}" and forced_engine:
            target = forced_engine
        if target == "{shot_engine}" and shot_engine:
            target = shot_engine
        if target not in (H3, WAN22):
            continue
        engine = target
        reason = rule.get("reason", rule.get("name", cond))
        break

    # v0.5：引擎已收敛到 allowed_engines（默认只有 h3）。
    # 旧分镜里残留 engine=wan22 时，这里强制改回 h3，避免去加载已归档的权重。
    allowed = [str(x).lower() for x in
               (cfg.get("routing", "allowed_engines", ["h3"]) or ["h3"])]
    if engine not in allowed:
        if "h3" in allowed:
            reason = f"{reason}（原定 {engine} 已下线 → 改用 H3）"
            engine = H3
        else:
            engine = allowed[0] if allowed else H3

    # 显式指定分辨率优先
    if resolution_field and resolution_field != "auto":
        res = _swap_for_orientation(resolution_field)
    else:
        res = _resolution(engine, quality_first, tier)

    steps = _steps(engine, quality_first, bool(dialogue))

    p_with = cfg.get("routing", "priority.with_dialogue", 3)
    p_def = cfg.get("routing", "priority.default", 6)
    prio = int(p_with) if dialogue else int(p_def)

    return RouteDecision(engine=engine, reason=reason, resolution=res,
                         steps=steps, priority=prio)


def plan_episode(shots: list[dict], **kw) -> tuple[list[dict], dict]:
    """给每个镜头附上路由决策，并汇总产能估算。

    注意：**不按 priority 重排**。渲染是严格按 seq 串行跑的，
    若展示顺序被重排，界面上镜头号会乱跳，误导判断。
    priority 仅作为字段保留（供未来调度的优先队）。
    """
    out = []
    for s in shots:
        d = route(s, **kw)
        out.append({**s, "_route": {
            "engine": d.engine, "reason": d.reason,
            "resolution": d.resolution, "steps": d.steps, "priority": d.priority,
        }})
    out.sort(key=lambda x: x.get("seq", 0))

    summary = {
        "total": len(out),
        "h3": sum(1 for x in out if x["_route"]["engine"] == H3),
        "wan22": sum(1 for x in out if x["_route"]["engine"] == WAN22),
        "total_duration": round(sum(float(x.get("duration") or 5) for x in out), 1),
        "estimated_minutes": estimate(out),
    }
    return out, summary


def cost_of(engine: str, resolution: str) -> float:
    """单镜基线耗时（分钟），来自 routing.yaml 的 cost 表。"""
    v = cfg.get("routing", f"cost.{engine}.{resolution}")
    if v is not None:
        return float(v)
    # 兜底：按引擎缺省
    return 13.0 if engine == H3 else 2.6


def estimate(shots: list[dict]) -> float:
    total = 0.0
    for s in shots:
        r = s.get("_route") or {}
        base = cost_of(r.get("engine", H3), r.get("resolution", "720x1280"))
        dur = float(s.get("duration") or 5)
        total += base * (dur / 5.0)
    return round(total, 1)


def reload() -> None:
    cfg.reload()
