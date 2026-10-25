"""LLM 调用层 —— 分镜 / 大纲 / 基础信息抽取 / 提示词润色。

所有提示词来自 config/prompts.yaml，LLM 参数来自同文件的 llm 段。
改提示词不用改代码。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import config as cfgmod
from .config import cfg


class LLMError(RuntimeError):
    pass


def _llm_conf() -> dict:
    return cfg.get("prompts", "llm", {}) or {}


def _secret(name: str, default: str = "") -> str:
    """优先从 config/secrets.env 读，其次环境变量。"""
    s = cfgmod.load_secrets()
    if s.get(name):
        return s[name]
    return os.environ.get(name, default)


def base_url() -> str:
    return _llm_conf().get("base_url", "https://api.icompify.com/v1")


def model() -> str:
    return _llm_conf().get("default_model", "deepseek-v4-flash")


def api_key() -> str:
    return _secret("ICOMPIFY_API_KEY")


# 兼容旧调用方
BASE_URL = property(base_url)
API_KEY = property(api_key)
MODEL = property(model)


_PH = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def _fill(tpl: str, kw: dict) -> str:
    """只替换 {命名占位符}，不碰模板里的 JSON 花括号。

    为什么不用 str.format：模板里内嵌的 JSON 示例（{"title": ...}）会被
    format 当成占位符直接报 KeyError。
    """
    def rep(m: re.Match) -> str:
        k = m.group(1)
        if k in kw and kw[k] is not None:
            return str(kw[k])
        return m.group(0)
    return _PH.sub(rep, tpl or "")


def prompt(name: str, **kw) -> tuple[str, str]:
    """取 prompts.yaml 里的模板，返回 (system, user) 并填充占位符。"""
    sec = cfg.get("prompts", f"prompts.{name}")
    if not sec:
        raise LLMError(f"prompts.yaml 中找不到模板: {name}")
    system = _fill(sec.get("system", "") or "", kw)
    user = _fill(sec.get("user", "") or "", kw)
    # 必填占位符没给值 → 报清楚（否则模型会拿到 {script} 这种原文）
    left = [k for k in kw if kw[k] is not None and "{" + k + "}" in user]
    if left:
        raise LLMError(f"模板 {name} 占位符未填充: {left}")
    return system, user
    return system, user


def _post(path: str, payload: dict, timeout: int = 300) -> dict:
    key = api_key()
    if not key:
        raise LLMError("ICOMPIFY_API_KEY 未配置（config/secrets.env）")
    url = f"{base_url().rstrip('/')}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(f"LLM HTTP {e.code}: {e.read().decode(errors='replace')[:800]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"LLM 连接失败: {e.reason}") from e


def chat(messages: list[dict], *, model_override: str | None = None,
         temperature: float | None = None, max_tokens: int | None = None,
         timeout: int | None = None) -> str:
    c = _llm_conf()
    payload = {
        "model": model_override or c.get("default_model", "deepseek-v4-flash"),
        "messages": messages,
        "temperature": temperature if temperature is not None else c.get("temperature", 0.6),
        "max_tokens": max_tokens or c.get("max_tokens", 16000),
    }
    d = _post("/chat/completions", payload,
              timeout=timeout or c.get("timeout_sec", 600))
    try:
        msg = d["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise LLMError(f"响应结构异常: {json.dumps(d, ensure_ascii=False)[:600]}") from e

    content = msg.get("content")
    # 推理模型（deepseek-v4-pro 等）先花 token 在 reasoning 上，
    # max_tokens 小时 content 会是 None。要报清楚，不能直接 .strip()。
    if content is None:
        reasoning = (msg.get("reasoning") or "").strip()
        usage = d.get("usage") or {}
        r_tok = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if reasoning:
            raise LLMError(
                f"模型只产出 reasoning 没有 content（reasoning_tokens={r_tok}）。"
                f"推理模型需要更大的 max_tokens，或改用非推理模型。"
                f" reasoning 前 200 字: {reasoning[:200]}")
        raise LLMError(f"content 为空: {json.dumps(msg, ensure_ascii=False)[:400]}")
    return content


_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def extract_json(text: str) -> dict:
    """从 LLM 输出里抠 JSON（容忍围栏和前后废话）。"""
    t = (text or "").strip()
    m = _FENCE.search(t)
    if m:
        t = m.group(1).strip()
    if not t.startswith("{"):
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            t = t[i:j + 1]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        t2 = re.sub(r",\s*([}\]])", r"\1", t)   # 尾逗号
        return json.loads(t2)


def chat_json(messages: list[dict], *, temperature: float | None = None,
              max_tokens: int | None = None, timeout: int | None = None) -> dict:
    """要 JSON。失败时自动降级到 fallback_model 重试一次。"""
    c = _llm_conf()
    try:
        return extract_json(chat(messages, temperature=temperature,
                                 max_tokens=max_tokens, timeout=timeout))
    except LLMError as e:
        fb = c.get("fallback_model")
        cur = c.get("default_model")
        if "reasoning" in str(e) and fb and fb != cur:
            return extract_json(chat(messages, model_override=fb,
                                     temperature=temperature,
                                     max_tokens=max_tokens, timeout=timeout))
        raise


def _estimate_tokens(text: str) -> int:
    """粗估 token 数：中文约 1.5 字/token，英文约 4 字符/token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4)


def _check_context(msgs: list[dict]) -> None:
    """按配置的 context_length 校验输入，超了直接报清楚，别让服务端截断。"""
    limit = int(cfg.get("prompts", "llm.context_length", 262144) or 262144)
    total = sum(_estimate_tokens(m.get("content") or "") for m in msgs)
    # 留 25% 给输出
    budget = int(limit * 0.75)
    if total > budget:
        raise LLMError(
            f"输入太长：约 {total} tokens，超过上下文预算 {budget}"
            f"（context_length={limit}）。请缩短剧本，或调大 context_length。")


def _ask_json(template: str, *, retries: int = 2, **kw) -> dict:
    """通用：取模板 → 调 LLM → 抠 JSON，失败重试一次（更强约束）。"""
    system, user = prompt(template, **kw)
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]
    _check_context(msgs)
    last = None
    for attempt in range(retries):
        try:
            data = chat_json(msgs, temperature=0.6 if attempt == 0 else 0.3)
            if not isinstance(data, dict):
                raise LLMError("返回的不是 JSON 对象")
            return data
        except Exception as e:
            last = e
            if attempt + 1 < retries:
                msgs.append({"role": "user", "content":
                             f"你上次的输出无法解析（{str(e)[:200]}）。"
                             f"请只输出纯 JSON，不要 markdown 围栏，不要任何解释文字。"})
                time.sleep(1)
    raise LLMError(f"生成失败: {last}")


# ---------------------------------------------------------------- 业务入口

def prompt_language() -> str:
    """提示词语言：zh（默认，v0.4 起全流程中文）/ en（旧版）。"""
    v = str(cfg.get("prompts", "language", "zh") or "zh").lower()
    return "en" if v.startswith("en") else "zh"


def _looks_english(text: str) -> bool:
    """粗判一段文本是不是英文（中文占比 < 10% 且字母多）。"""
    t = (text or "").strip()
    if not t:
        return False
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    latin = sum(1 for c in t if c.isascii() and c.isalpha())
    return cjk == 0 and latin > 8


def pick_style(prj: dict, req_style: str = "") -> str:
    """选进渲染的风格描述。

    v0.5 关键点：中文模式下要保证**进渲染的是中文**。
    旧项目里 style 存的是英文（en），style_zh 是中文；这里优先中文。
    优先级：请求里给的（若是中文）> style_zh > style > 默认中文风格
    """
    req = (req_style or "").strip()
    zh = prompt_language() == "zh"
    if not zh:
        return req or (prj or {}).get("style") or ""
    if req and not _looks_english(req):
        return req
    zh_style = ((prj or {}).get("style_zh") or "").strip()
    if zh_style:
        return zh_style
    base = ((prj or {}).get("style") or "").strip()
    if base and not _looks_english(base):
        return base
    if req and not _looks_english(req):
        return req
    # 旧的全是英文 → 下一层由调用方决定是否翻译；这里先返回空让 LLM 用通用风格
    return base or ""


def _tpl(base: str) -> str:
    """中文模式优先取 `<base>_v2`，缺失则回退旧模板。"""
    if prompt_language() == "zh":
        if cfg.get("prompts", f"prompts.{base}_v2"):
            return f"{base}_v2"
    return base


# 渲染模式 → 中文名（v0.5：只保留 H3）
ENGINE_MODE_ZH = {
    "h3": "全部 MiniMax H3（原生对白 + 音效）",
    "auto": "自动（已统一到 H3）",
}


def engine_modes() -> list[dict]:
    """可用渲染模式（v0.5 只剩 H3，字段保留给前端兼容）。"""
    conf = cfg.get("routing", "engine_modes", {}) or {}
    out = []
    for k, v in conf.items():
        if k not in ENGINE_MODE_ZH:
            continue
        out.append({"id": k, "zh": (v or {}).get("zh") or ENGINE_MODE_ZH[k]})
    if not out:
        out = [{"id": "h3", "zh": ENGINE_MODE_ZH["h3"]}]
    order = {"h3": 0, "auto": 1}
    out.sort(key=lambda x: order.get(x["id"], 9))
    return out


def outline(story: str, *, episode_count: int = 3, style: str = "") -> dict:
    """第 2 步：故事 → 剧集大纲。"""
    data = _ask_json("outline", story=story, episode_count=episode_count,
                     style=style or "现代都市写实，电影感")
    eps = data.get("episodes") or []
    for i, e in enumerate(eps, 1):
        e.setdefault("seq", i)
    eps.sort(key=lambda x: x["seq"])
    data["episodes"] = eps
    return data


def baseinfo(script: str) -> dict:
    """第 3 步：从剧本抽取锁定项（角色/场景/道具/风格/禁用项）。"""
    data = _ask_json(_tpl("baseinfo"), script=script)
    data.setdefault("characters", [])
    data.setdefault("locations", [])
    data.setdefault("props", [])
    data.setdefault("forbidden", [])
    return data


def storyboard(script: str, *, style: str = "", max_shots: int = 60,
               target_duration: float = 0, locked_info: str = "",
               engine_mode: str = "h3") -> dict:
    """第 4 步：剧本 → 分镜头脚本。

    v0.5：只有 H3 一个引擎，engine_mode 固定 h3（保留参数兼容旧调用）。

    中文模式下用 storyboard_v2 模板，并把 H3 规则（storyboard_rules.h3）
    插进 system prompt，让 LLM 一次就按 H3 的能力拆镜：
      · 有台词必填 dialogue（H3 会照着念，口型对齐）
      · 人物发声写进 sfx
      · 机位要能看到嘴部/侧脸
      · 同一场景的连续对白尽量合并，减少 H3 镜头数
    """
    mode = "h3"
    tpl = _tpl("storyboard")
    rules = cfg.get("prompts", f"prompts.storyboard_rules.{mode}", "") or ""
    data = _ask_json(tpl, script=script, style=style or "",
                     max_shots=max_shots,
                     target_duration=target_duration or "90-120",
                     locked_info=locked_info or "（未锁定）",
                     engine_mode=mode,
                     engine_mode_zh=ENGINE_MODE_ZH[mode],
                     engine_rules=rules)
    if not data.get("shots"):
        raise LLMError("返回无 shots 字段")
    data = normalize_storyboard(data)
    for s in data["shots"]:
        s["engine"] = "h3"
    data["scenes"] = normalize_scenes(data.get("scenes"))
    # 保底：LLM 没给场次时按 1 个场次处理，衔接逻辑就不会乱跨场
    if not data["scenes"]:
        data["scenes"] = [{"id": 1, "name": "全场", "desc": "", "light": "", "notes": ""}]
    for s in data["shots"]:
        if not s.get("scene_id"):
            s["scene_id"] = 1
    data["engine_mode"] = mode
    return data


def _as_int(v) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_scenes(raw) -> list[dict]:
    """场次卡规整（v0.6）。容忍 LLM 输出 dict/list 两种形态。"""
    out: list[dict] = []
    if isinstance(raw, dict):
        raw = [{"id": k, **(v or {})} if isinstance(v, dict) else {"id": k}
               for k, v in raw.items()]
    if not isinstance(raw, list):
        return out
    for i, sc in enumerate(raw, 1):
        if not isinstance(sc, dict):
            continue
        sid = _as_int(sc.get("id")) or i
        out.append({
            "id": sid,
            "name": (sc.get("name") or f"场次{sid}").strip(),
            "desc": (sc.get("desc") or "").strip(),
            "light": (sc.get("light") or "").strip(),
            "notes": (sc.get("notes") or "").strip(),
        })
    out.sort(key=lambda x: x["id"])
    return out


def normalize_storyboard(data: dict) -> dict:
    shots = data.get("shots") or []
    default_neg_zh = "模糊，变形，水印，低质量，多手多脚，面部扭曲"
    default_neg_en = "blurry, distorted, watermark, low quality, extra limbs"
    zh = prompt_language() == "zh"
    clean = []
    for i, s in enumerate(shots, 1):
        eng = (s.get("engine") or "").strip().lower()
        clean.append({
            "seq": int(s.get("seq") or i),
            "duration": float(s.get("duration") or 5.0),
            "shot_size": s.get("shot_size") or "",
            "camera": s.get("camera") or "",
            "prompt": s.get("prompt") or "",
            "negative": s.get("negative") or
                        (default_neg_zh if zh else default_neg_en),
            "dialogue": s.get("dialogue"),
            "speaker": (s.get("speaker") or "").strip() or None,
            "scene_id": _as_int(s.get("scene_id")),
            "sfx": s.get("sfx"),
            "characters": s.get("characters") or [],
            # v0.5 只有 H3，统一写死，避免旧分镜的 wan22 残留
            "engine": "h3",
        })
    clean.sort(key=lambda x: x["seq"])
    clean = dedupe_dialogue(clean)
    data["shots"] = clean
    data.setdefault("characters", [])
    return data


def storyboard_quality(shots: list[dict]) -> list[str]:
    """分镜质量体检（R1 代码层防线，2026-10-23）。

    光靠 prompt 里写规则不够 —— LLM 照样能把 600 秒拆成 75 个「同一个人
    在同一走廊后退」（示例人物A项目实际发生）。这里做确定性检查，
    让上层能拿结果要求重拆，而不是盲目开渲。

    返回问题列表，空列表 = 合格。
    """
    problems: list[str] = []
    if not shots:
        return ["没有镜头"]

    # ① 场次数：整集只有一个场次 = 注定重复
    scenes = {s.get("scene_id") for s in shots}
    total = sum(float(s.get("duration") or 0) for s in shots)
    need_scenes = max(2, int(total // 40) + 1) if total else 2
    if len(scenes) < need_scenes:
        problems.append(
            f"场次太少：只有 {len(scenes)} 个（{total:.0f} 秒至少需 {need_scenes} 个），"
            f"单一场景会导致大量重复镜头")

    # ② 同一句台词不许重复
    seen: dict[str, int] = {}
    for s in shots:
        d = (s.get("dialogue") or "").strip()
        if d:
            k = "".join(d.split())
            seen[k] = seen.get(k, 0) + 1
    dup = {k: v for k, v in seen.items() if v > 1}
    if dup:
        problems.append(
            "台词重复：" + "；".join(f"「{k}」x{v}" for k, v in dup.items()))

    # ③ 连续同景别 / 机位过度集中
    sizes = [s.get("shot_size") or "" for s in shots]
    run = 1
    for a, b in zip(sizes, sizes[1:]):
        run = run + 1 if (a and a == b) else 1
        if run >= 4:
            problems.append(f"连续 {run} 个镜头同为「{b}」景别，太单调")
            break
    cams = [s.get("camera") or "" for s in shots if s.get("camera")]
    if len(cams) >= 8:
        top = max(set(cams), key=cams.count)
        if cams.count(top) / len(cams) > 0.6:
            problems.append(
                f"机位「{top}」占 {cams.count(top)}/{len(cams)}，过度集中")

    # ④ 动作重复：前 12 字相同的镜头太多
    import re as _re
    acts: list[str] = []
    for s in shots:
        t = _re.sub(r"^[^：]*：", "", (s.get("prompt") or ""))
        t = _re.sub(r"[（(][^）)]*[）)]", "", t)
        acts.append(t.strip()[:12])
    hits = {a: acts.count(a) for a in set(acts) if a and acts.count(a) > 1}
    if hits:
        worst = max(hits.items(), key=lambda x: x[1])
        if worst[1] >= 3:
            problems.append(
                f"动作重复：{worst[1]} 个镜头开头相同「{worst[0]}」")
    return problems


def dedupe_dialogue(shots: list[dict], max_repeat: int = 1) -> list[dict]:
    """同一个台词不允许跨多个镜头重复（2026-10-23）。

    示例人物A项目实测：LLM 把「今天是个好日子」这一句塞进了 11 个镜头
    （镜35/39/43/49/52/55/58/61/64/67/70），成片里同一句反复唱了 11 遍，
    听起来又嗧嗥又重复。

    规则：同一句台词全片最多出现 max_repeat 次（默认 1 次），
    多出来的清空 dialogue，并在 sfx 里明确写成环境声，
    避免模型得不到台词指令后自己瞎编人声。
    """
    seen: dict[str, int] = {}
    for s in shots:
        d = (s.get("dialogue") or "").strip()
        if not d:
            continue
        key = "".join(d.split())
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > max_repeat:
            s["dialogue"] = None
            s["speaker"] = None
            if not (s.get("sfx") or "").strip():
                s["sfx"] = "环境底噪"
    return shots


def retrofit(shots: list[dict]) -> dict:
    """给旧分镜补全场次/说话人/场次卡（v0.6 方案 A 的存量迁移）。

    v0.4 之前生成的分镜没有 scene_id / speaker / scenes，
    直接重渲的话 A 方案（场次卡、说话人点名）完全不会生效。
    这里只补结构，不改画面提示词、不改台词、不重排镜头。

    shots: [{"seq":1,"prompt":"...","dialogue":"...","characters":[...],
             "shot_size":"..."}]
    返回: {"scenes":[...], "shots":[{"seq","scene_id","speaker"}]}
    """
    payload = [{
        "seq": s.get("seq"),
        "prompt": (s.get("prompt") or "")[:600],
        "dialogue": s.get("dialogue"),
        "characters": s.get("characters") or [],
        "shot_size": s.get("shot_size") or "",
    } for s in shots]
    system, user = prompt(_tpl("retrofit"), items="", shots=json.dumps(
        payload, ensure_ascii=False, indent=1))
    data = chat_json([{"role": "system", "content": system},
                      {"role": "user", "content": user}],
                     temperature=0.2, max_tokens=16384, timeout=900)
    scenes = normalize_scenes(data.get("scenes"))
    mapping: dict[int, dict] = {}
    for it in data.get("shots") or []:
        try:
            seq = int(it.get("seq"))
        except (TypeError, ValueError):
            continue
        mapping[seq] = {
            "scene_id": _as_int(it.get("scene_id")) or 1,
            "speaker": (it.get("speaker") or "").strip() or None,
        }
    return {"scenes": scenes, "shots": mapping}


def localize(items: list[dict]) -> dict[str, str]:
    """批量把英文资产翻成中文（v0.5）。

    items: [{"id": "s1", "text": "Extreme wide shot..."}, ...]
    返回: {"s1": "中文...", ...}

    一次调用搞定一集（40 个镜头约 5K 字符），比逐条翻快且风格统一。
    """
    items = [x for x in items if (x.get("text") or "").strip()]
    if not items:
        return {}
    system, user = prompt(_tpl("localize"), items=json.dumps(
        [{"id": x["id"], "text": x["text"][:2000]} for x in items],
        ensure_ascii=False, indent=1))
    data = chat_json([{"role": "system", "content": system},
                      {"role": "user", "content": user}],
                     temperature=0.2, max_tokens=32768, timeout=900)
    out: dict[str, str] = {}
    for it in data.get("items") or []:
        i = str(it.get("id") or "")
        zh = (it.get("zh") or "").strip()
        if i and zh:
            out[i] = zh
    return out


def translate(text: str, kind: str = "text") -> str:
    """中英互译。

    kind=slug  → 生成路径安全的英文短名（小写、连字符、无中文）
    kind=style → 视觉风格描述
    kind=text  → 通用互译（中文→英文；英文→中文）
    """
    try:
        system, user = prompt("translate", text=text, kind=kind)
    except LLMError:
        # 模板缺失时的兜底
        if kind == "slug":
            system = ("Convert the given title into a short English slug for a folder name. "
                      "Output ONLY lowercase ascii letters, digits and hyphens. "
                      "Use pinyin for Chinese names. Max 4 words. No quotes, no explanation.")
            user = text
        else:
            system = ("Translate between Chinese and English. If input is Chinese, "
                      "output concise English. If English, output concise Chinese. "
                      "Output only the translation, no explanation.")
            user = text
    out = chat([{"role": "system", "content": system},
                {"role": "user", "content": user}],
               max_tokens=600, timeout=180, temperature=0.2).strip()
    # 清掉模型常带的引号/围栏
    out = out.strip().strip('"').strip("'").strip("`").strip()
    if kind == "slug":
        import re
        out = re.sub(r"[^a-zA-Z0-9]+", "-", out).strip("-").lower()
        out = out[:48] or "project"
    return out


def enhance_prompt(text: str, style: str = "") -> str:
    system, user = prompt(_tpl("enhance"), prompt=text, style=style)
    return chat([{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                max_tokens=800, timeout=180).strip()


def health() -> dict:
    """测 LLM 连通性。"""
    key = api_key()
    out = {"configured": bool(key), "base_url": base_url(),
           "model": model(), "ok": False, "latency_ms": None, "error": None}
    if not key:
        out["error"] = "未配置 key"
        return out
    t0 = time.time()
    try:
        chat([{"role": "user", "content": "只回复两个字：收到"}],
             max_tokens=64, timeout=90)
        out["ok"] = True
        out["latency_ms"] = int((time.time() - t0) * 1000)
    except Exception as e:
        out["error"] = str(e)[:300]
    return out
