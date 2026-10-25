"""渲染工作器 —— 单卡串行，一次一个镜头。

核心约束：
- A5000 24GB 上 H3 峰值 22GB，并发必 OOM → 全局锁
- ComfyUI 有「坏预算缓存」：同图 OOM 一次就永远 OOM，必须改参数才能恢复
- 重试不无脑重跑，每轮换一个变量（seed / steps / 分辨率）

参数来自 config/settings.yaml 的 render 段与 assemble 段。
"""
from __future__ import annotations

import os
import random
import re
import shutil
import threading
import time
from pathlib import Path

from . import assemble, comfy, config as cfgmod, db, router, workflow
from .config import cfg

LOCK = threading.Lock()

# 重试策略：每次换一个变量，避免坏预算缓存
RETRY_STRATEGY = [
    {"note": "原参数重试（换 seed）", "bump_seed": True},
    {"note": "降步数 -2", "bump_seed": True, "steps_delta": -2},
    {"note": "降分辨率一档", "bump_seed": True, "steps_delta": -2, "downscale": True},
]


def _output_dir() -> str:
    return cfg.get("settings", "paths.output_dir", "/output") or "/output"


def _out_dir(project_id: str, episode_seq: int) -> Path:
    """产物目录：/output/{YYYYMMDD-HHMM_slug}/E01/ 。

    目录名来自 project.out_dir（创建时生成），旧数据回退到 project id。
    """
    name = db.get_project_dir(project_id) or project_id
    p = Path(_output_dir()) / name / f"E{episode_seq:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def character_ref_image(project_id: str, shot: dict) -> str | None:
    """取镜头涉及角色的参考图（用于锁定人物长相）。

    优先级：镜头显式指定的角色 > 镜头 characters 列表里的第一个有图的。
    返回容器内可读的路径；没有就 None。
    """
    names = shot.get("characters") or []
    if isinstance(names, str):
        try:
            import json as _j
            names = _j.loads(names)
        except Exception:
            names = [names]
    if not names:
        return None

    try:
        chars = db.list_characters(project_id)
    except Exception:
        return None
    by_name = {c["name"]: c for c in chars}
    for n in names:
        c = by_name.get(n)
        if not c:
            continue
        ref = (c.get("ref_image") or "").strip()
        if not ref:
            continue
        # 支持 /input/uploads/xxx.png 与相对名两种写法
        cand = Path(ref)
        if not cand.is_absolute():
            cand = Path("/input") / ref.lstrip("/")
        if cand.exists():
            return str(cand)
    return None


def ep_ref2va() -> bool:
    """Ref2VA 是否启用（v0.6）。

    默认开启：有角色参考图就走 Ref2VA。关掉则退回"参考图当首帧"的老行为。
    """
    return bool(cfg.get("settings", "render.ref2va_enabled", True))


def character_ref_images(project_id: str, shot: dict, limit: int = 3) -> list[str]:
    """取镜头涉及角色的参考图列表（v0.6，配合 Ref2VA）。

    与 character_ref_image 的区别：这个返回**多张**——Ref2VA 支持最多 9 张
    参考图，多角色同框时每人给一张，才能同时锁住几张脸。

    顺序即 prompt 里 <Picture i> 的顺序（1-based），所以调用方要把
    图片顺序和提示词里的编号对齐。
    """
    names = shot.get("characters") or []
    if isinstance(names, str):
        try:
            import json as _j
            names = _j.loads(names)
        except Exception:
            names = [names]
    if not names:
        return []
    try:
        chars = db.list_characters(project_id)
    except Exception:
        return []
    by_name = {c["name"]: c for c in chars}

    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        c = by_name.get(n)
        if not c:
            continue
        ref = (c.get("ref_image") or "").strip()
        if not ref:
            continue
        cand = Path(ref)
        if not cand.is_absolute():
            cand = Path("/input") / ref.lstrip("/")
        if not cand.exists():
            continue
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) >= limit:
            break
    return out


def anchor_text(project_id: str, shot: dict) -> str:
    """该镜头涉及角色的**外貌锚点原文**（v0.6 方案 A1）。

    为什么要单独拼：分镜 LLM 每镜都会用自己的话重写人物，
    细节必然漂移（E02 实测 8 种写法）。这里把角色库里锁定的锚点
    逐字拼在画面描述**之前**，给模型一个强约束的"定妆说明"。

    ⚠️ 2026-10-25 重大修正（有 A/B 实测支撑）：
      当该角色**有参考图且 Ref2VA 开启**时，**不再输出外貌文字描述**，
      改为「长相严格参考 <Picture N>」。

      原因（《最后一个房间》9 镜实测）：
        文字锚点写「圆脸、黑框眼镜、深橄榄绿夹克」这类概括词，
        模型会朝"通用中年圆脸男"收敛，反而盖过参考图本人的真实长相。
        三联对比（同一参考图、同一镜头、同 seed、1344x768）：
          A 长锚点 + match → 显老、脸更宽、眼镜变成细框 ← 当前生产
          C 短指令 + max   → 脸型/下颌线/眼镜框明显接近本人
        且 C 只用 14.2 分钟，比生产的 18-27 分钟更快。

      文字描述只在**没有参考图**时才用（那时文字是唯一身份线索）。
    """
    names = shot.get("characters") or []
    if isinstance(names, str):
        try:
            import json as _j
            names = _j.loads(names)
        except Exception:
            names = [names]
    if not names:
        return ""
    try:
        chars = {c["name"]: c for c in db.list_characters(project_id)}
    except Exception:
        return ""
    lines = []
    # 参考图是否参与本镜（决定走「图」还是走「字」）
    try:
        ref_paths = character_ref_images(project_id, shot) if ep_ref2va() else []
    except Exception:
        ref_paths = []
    ref_names: list[str] = []
    if ref_paths:
        # 与 character_ref_images 同序：每个有图的角色占一个 <Picture i>
        seq = 0
        for n in names:
            c = chars.get(n) or {}
            ref = (c.get("ref_image") or "").strip()
            if not ref:
                continue
            cand = Path(ref)
            if not cand.is_absolute():
                cand = Path("/input") / ref.lstrip("/")
            if cand.exists():
                seq += 1
                ref_names.append(f"{n}=<Picture {seq}>")
    for n in names:
        c = chars.get(n)
        if not c:
            continue
        # 有参考图 → 只给「参考第几张图」，绝不写外貌形容词
        if ref_names:
            tag = next((x.split("=")[1] for x in ref_names
                        if x.split("=")[0] == n), None)
            if tag:
                lines.append(f"{n}的长相必须严格参考 {tag}，是同一个人，"
                             f"不要改变脸型、五官、眼镜与发型")
                continue
        app = (c.get("appearance") or "").strip()
        zh = (c.get("appearance_zh") or "").strip()
        # 中文模式下优先中文锚点
        from .script import prompt_language
        text = zh if (prompt_language() == "zh" and zh) else (app or zh)
        # 清掉历史遗留的前缀（"English anchor:" / "英文锚点："），
        # 否则会原样进提示词，白白浪费 token 还干扰模型理解
        for pre in ("英文锚点：", "英文锚点:", "English anchor:", "English anchor："):
            if text.startswith(pre):
                text = text[len(pre):].strip()
        # ⚠️ 锚点自己常以名字开头（「示例人物B，45岁中国男性…」），
        #    外面又拼一次 f"{n}：{text}" → 「示例人物B：示例人物B，45岁…」
        #    重复的名字对模型是噪声，还占 token（2026-10-23 实测）。
        for _sep in ("，", "、", ",", "：", ":", " "):
            pref = n + _sep
            if text.startswith(pref):
                text = text[len(pref):].lstrip()
                break
        if text:
            lines.append(f"{n}：{text}")
    if not lines:
        return ""
    from .script import prompt_language
    if prompt_language() == "zh":
        return "【人物定妆（必须严格保持）】" + "；".join(lines) + "。"
    return "【Character sheet (must match exactly)】" + "; ".join(lines) + "."


def scene_text(episode_id: str, shot: dict, max_len: int = 110) -> str:
    """该镜头所属场次的场景卡（v0.6 方案 A3）。

    只提供「空间长相 + 光源」两项，**不提供**连续性清单（那属于分镜规则，
    不该进渲染提示词：实测把 notes 也塞进去会让模型去拍别的镜头的内容）。

    ⚠️ 必须截断：一个 300 字的空间描述会压过本镜的景别，
    实测镜6（孩子近景特写）被场景卡带成了全景（2026-10-22）。
    """
    sc = db.scene_card(episode_id, shot.get("scene_id"))
    if not sc:
        return ""
    from .script import prompt_language
    zh = prompt_language() == "zh"
    bits = []
    for key, label_zh, label_en in (("desc", "空间", "Space"),
                                    ("light", "光源", "Light")):
        v = (sc.get(key) or "").strip()
        if not v:
            continue
        if len(v) > max_len:
            v = v[:max_len].rstrip("，,；;。. ") + "…"
        bits.append(f"{label_zh}：{v}" if zh else f"{label_en}: {v}")
    if not bits:
        return ""
    head = ("【场景一致性（只用于保持空间与光线相同，"
            "不得改变下面本镜的景别与构图）】" if zh else
            "【Scene consistency (space and light only; "
            "do NOT change the shot framing below)】")
    return head + "；".join(bits) + "。"


def build_h3_prompt(shot: dict, *, project_id: str = "",
                    episode_id: str = "") -> str:
    """给 H3 组装提示词：画面 + 对白 + 音效。

    关键：H3 是音视频同生模型，对白必须写进 prompt 它才会照着说。
    之前只传 shot["prompt"]（纯画面描述），H3 只能自己瞎编人声，
    导致「声音听不懂、和台词对不上」。

    v0.4：中文优先。Wan2.2 与 H3 都原生支持中文提示词，
          中文写人物/服装/情绪更准；对白本来就是中文，不必再翻译。
    """
    from .script import prompt_language
    zh = prompt_language() == "zh"
    base = (shot.get("prompt") or "").strip()
    dialogue = (shot.get("dialogue") or "").strip()
    speaker = (shot.get("speaker") or "").strip()
    sfx = (shot.get("sfx") or "").strip()

    parts: list[str] = []
    # ① 人物定妆（逐字锚点）—— 身份权重最高，必须最前
    if project_id:
        a = anchor_text(project_id, shot)
        if a:
            parts.append(a)
        # ①b 有参考图时，把镜头自带的长外貌描述剥掉（2026-10-25）
        #     分镜 LLM 会在每镜 prompt 开头抄一遍「圆脸/黑框眼镜/绿夹克…」，
        #     这段文字和参考图在打架：文字把人往"通用中年圆脸男"拉，
        #     实测这就是脸不像本人的主因之一（A/C 对照：A 有这段→显老变宽）。
        #     有图时只留名字，让 <Picture N> 单独决定长相。
        base = _strip_appearance_when_ref(base, project_id, shot)
    # ② 本镜画面 + 景别锁定
    #    ⚠️ 必须在场景卡之前：场景卡描述的是"整个空间"，
    #    放前面会把近景特写带成全景（2026-10-22 镜6 实测踩到）。
    if base:
        size = (shot.get("shot_size") or "").strip()
        cam = (shot.get("camera") or "").strip()
        lock = []
        if zh:
            if size:
                lock.append(f"本镜景别：{size}")
            if cam:
                lock.append(f"镜头运动：{cam}")
            lock.append("画面构图以此描述为准，不要把镜头拉远")
            # ⚠️ 参考图的构图会污染本镜景别（2026-10-23 实测）：
            #    定妆照是「正面半身」，模型连构图一起照搬，
            #    第1镜本应全景（人走进画面），5s 变成正面大特写。
            #    必须明确告诉它：参考图只管长相，不管构图。
            lock.append("参考图只用于锁定人物长相，不要照搬参考图的构图与景别")
            # ⚠️ 实测还会“中途切镜”（同一镜头里从全景硬切到特写）。
            #    H3 是单镜生成，必须明确要求一镜到底。
            lock.append("一镜到底，中途不切镜，景别与构图全程保持不变")
        else:
            if size:
                lock.append(f"Shot size: {size}")
            if cam:
                lock.append(f"Camera: {cam}")
            lock.append("this framing is authoritative; do not pull back")
        parts.append(f"【本镜】{'，'.join(lock)}：{base}" if zh else
                     f"[Shot] {', '.join(lock)}: {base}")
    # ③ 场景一致性（瘦身版，明确不得改景别）
    if episode_id:
        sc = scene_text(episode_id, shot)
        if sc:
            parts.append(sc)

    # ③b 单人镜头人物约束
    #     实测单人镜头会莫名多出一个模糊路人（2026-10-23 第1镜 6s 帧）。
    chars_now = shot.get("characters") or []
    if isinstance(chars_now, str):
        chars_now = [chars_now]
    if len(chars_now) == 1:
        only = chars_now[0]
        parts.append(
            f"画面中只有{only}一个人，不要出现其他人物，不要有路人经过。"
            if zh else
            f"Only {only} is in frame; no other people, no passers-by.")

    # ④ 台词：必须点名说话人，否则多人同框时 H3 会猜错
    if dialogue:
        who = speaker or (shot.get("characters") or [""])[0] or "画面中的人物"
        # 【画外音】（2026-10-24）：说话人不在本镜画面里（对讲机/电话/旁白）时，
        # 绝不能写成「{who}的嘴唇开合说出台词」—— 画面里根本没这个人，
        # 与 ③b「画面中只有 X 一个人」直接冲突，H3 会生成幽灵人声或糊嘴。
        # 实测来源：示例人物A项目 37 镜失败里就有这类矛盾指令。
        present = shot.get("characters") or []
        if isinstance(present, str):
            try:
                import json as _json
                present = _json.loads(present)
            except Exception:
                present = [present]
        off_screen = bool(speaker) and speaker not in present
        # ⚠️ 必须先判断这句是「唱」还是「说」（2026-10-23）。
        #    示例人物A项目实测：画面描述写「正在唱出这句歌词」，
        #    这里却无条件写「用普通话逐字说出这句台词」，
        #    两个矛盾指令一起进 H3 → 半说半唱、含糊听不懂。
        singing = any(k in base for k in ("唱", "歌词", "旋律", "演唱", "歌声"))
        # 台词长度必须撑得起镜头时长，否则模型会把一句话反复念来填时间
        #（实测：7 字歌词配 8 秒镜头 → 同一句循环好几遍）。
        per_sec = 1.6 if singing else 2.6
        need = max(1, round(float(shot.get("duration") or 5) * per_sec))
        pace = ("语速放慢、一句到底，不要重复歌词" if len(dialogue) >= need
                else "按正常语速一次说完，说完就停，不要重复这句话")
        # 【语言硬约束】（2026-10-24 老板指令）
        # 人声只允许中文：普通话为主，允许粤语、四川话等地方语言。
        # 严禁英语、泰语等任何外语 —— 实测模型会把没听懂的人声
        # 自由生成成外语，听越成「杂乱声音」。
        lang_lock = ("全程只说中文（普通话，允许带自然口音），"
                     "严禁说英语、泰语或任何外语，"
                     "严禁哼唱以外的含糊乱语。")
        if zh:
            if off_screen:
                # 画外音：声源来自设备，画面中人闭着嘴听
                listeners = present[0] if present else "画面中的人物"
                parts.append(
                    f'声音来自画外（{who}通过对讲机/电话说话，人不在画面中）：'
                    f'只能听到{who}的声音说：“{dialogue}”，声音带设备底噪。'
                    f'{listeners}闭着嘴不说话，没有口型动作，只是听着。'
                    f'{pace}。{lang_lock}')
            elif singing:
                parts.append(
                    f'{who}用普通话把这句歌词唱出来：“{dialogue}”。'
                    f'只唱这一句，唱完就停，不要重复歌词、不要加唱别的句子。'
                    f'口型与歌声精确对应，原生人声音轨，发音清晰，不要含糊或乱语。'
                    f'{lang_lock}')
            else:
                parts.append(
                    f'{who}的嘴唇开合，用普通话逐字说出这句台词：“{dialogue}”。'
                    f'只有 {who} 在说话，其他人物都闭着嘴，不说话、没有口型动作。'
                    f'{pace}。口型必须与台词精确对应，原生对白音轨，'
                    f'发音清晰，不要含糊或乱语。{lang_lock}')
        else:
            verb = "sings" if singing else "speaks"
            parts.append(
                f'{who} {verb} this exact line in Mandarin Chinese: "{dialogue}". '
                f'Say it once and stop; do not repeat the line. '
                f'Only {who} is vocal; everyone else keeps their mouth closed. '
                f'Lip movement must match precisely. '
                f'Native voice audio, clear pronunciation, no gibberish, '
                f'no English or any foreign language, Mandarin Chinese only.')
    if sfx and sfx not in ("无", "-", "none", "None"):
        # 已经指定了唱/说，就不要再让 sfx 重复描述人声，否则两处人声指令打架
        vocal_words = ("人声", "歌", "唱", "说", "喊", "叫", "对白")
        if dialogue and any(w in sfx for w in vocal_words):
            sfx = "环境底噪"
        parts.append(f"背景声：{sfx}。" if zh else f"Background sound: {sfx}.")
    if not dialogue and not sfx:
        parts.append("只有环境底噪，没有人声，没有人说话，没有任何人声杂音。" if zh
                     else "Ambient room tone only. No speech, no voice, no vocal noise.")
    # ⑤ 全局语言约束：即使镜头没有台词，模型也可能自发生成人声
    #    （英语/泰语/含糊哼唱 → 老板听到的「杂乱声音」）。
    #    全部镜头统一追加：人声只允许中文，方言可，外语禁止。
    parts.append(
        "【人声语言约束】本镜头所有人声必须使用中文：普通话为主，"
        "允许粤语、四川话等地方语言；严禁英语、泰语等外语，"
        "严禁无意义的哼唱乱语或嘈杂人声。没有台词时保持环境声，"
        "不要自发生成任何人声。"
        if zh else
        "[Voice language] All human voices must be Chinese (Mandarin; regional "
        "Chinese dialects such as Cantonese or Sichuanese are acceptable). "
        "English, Thai, or any other foreign language is forbidden. No gibberish "
        "or meaningless vocal noise. With no dialogue, keep ambient sound only.")
    return " ".join(p for p in parts if p)


def _strip_appearance_when_ref(base: str, project_id: str, shot: dict) -> str:
    """有参考图的角色，从镜头描述里剥掉外貌形容词，只留名字。

    只处理**第一段**（第一个「；」之前）—— 那一段是分镜 LLM 写的人物定妆，
    后面是动作/场景，剥掉会丢信息。

    示例：
      输入 「三十多岁中国男性示例人物A，圆脸，黑色短发略硬，黑框半框眼镜，
             穿深橄榄绿连帽夹克内搭浅蓝细条纹衬衫，体型中等偏壮，表情克制；
             他停下脚步…」
      输出 「示例人物A；他停下脚步…」

    保守条件（任一不满足就原样返回，绝不误伤）：
      · 该镜有参考图且 Ref2VA 开启
      · 第一段里确实出现该角色名
      · 第一段至少有 2 个逗号（说明真是一串形容词）
    """
    if not base or not project_id:
        return base
    try:
        refs = character_ref_images(project_id, shot) if ep_ref2va() else []
    except Exception:
        refs = []
    if not refs:
        return base
    names = shot.get("characters") or []
    if isinstance(names, str):
        try:
            import json as _j
            names = _j.loads(names)
        except Exception:
            names = [names]
    if not names:
        return base
    try:
        chars = {c["name"]: c for c in db.list_characters(project_id)}
    except Exception:
        return base
    # 只对「真的带参考图」的角色下手
    with_ref = []
    for n in names:
        ref = ((chars.get(n) or {}).get("ref_image") or "").strip()
        if not ref:
            continue
        cand = Path(ref)
        if not cand.is_absolute():
            cand = Path("/input") / ref.lstrip("/")
        if cand.exists():
            with_ref.append(n)
    if not with_ref:
        return base

    if "；" in base:
        head, rest = base.split("；", 1)
        rest = "；" + rest
    else:
        head, rest = base, ""
    if head.count("，") < 2:
        return base
    hit = next((n for n in with_ref if n in head), None)
    if not hit:
        return base
    return f"{hit}{rest}"


def build_wan22_prompt(shot: dict) -> str:
    """Wan2.2 提示词：只有画面，绝不提“说话”（它没有音轨、不能对口型）。

    v0.4 新增。以前直接传 shot["prompt"]，若分镜里写了“开口说话”
    会让模型去凑口型，出嘴部抽搯、画面崩坏；这里显式否掉语音。
    """
    from .script import prompt_language
    zh = prompt_language() == "zh"
    base = (shot.get("prompt") or "").strip()
    if not base:
        return base
    suffix = ("画面中人物不说话，没有口型动作，无水印无字幕，"
              "电影质感，画面稳定清晰。" if zh else
              "No speaking, no lip movement. No watermark, no subtitles. "
              "Cinematic, stable and sharp.")
    return f"{base} {suffix}"


def _find_output(history: dict) -> str | None:
    for kind in ("images", "gifs", "videos"):
        for item in comfy.outputs_of(history, kind):
            fn = item.get("filename")
            if fn:
                sub = item.get("subfolder") or ""
                return f"{sub}/{fn}" if sub else fn
    return None


def _pull_output(filename: str, local_path: str) -> bool:
    """把 ComfyUI 产物拿到本地。

    A) api 与 comfyui 共享 output 卷 → 直接 move
    B) 只有 api 看得到 → HTTP /view 下载
    """
    out_root = Path(_output_dir())
    src = out_root / filename
    if src.exists() and src.is_file():
        dst = Path(local_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(dst))
            return True
        except (OSError, shutil.Error):
            pass

    import urllib.parse
    import urllib.request
    parts = filename.rsplit("/", 1)
    sub, fn = (parts[0], parts[1]) if len(parts) == 2 else ("", filename)
    url = (f"{comfy.base_url()}/view?filename={urllib.parse.quote(fn)}"
           f"&type=output&subfolder={urllib.parse.quote(sub)}")
    try:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(url, local_path)
        return os.path.getsize(local_path) > 0
    except Exception:
        return False


def render_shot(shot: dict, *, project_id: str, episode_seq: int,
                quality_first: bool = False,
                force_engine: str | None = None,
                tier: str = "") -> dict:
    """渲染单个镜头。返回更新后的 shot dict。"""
    sid = shot["id"]
    decision = router.route(shot, quality_first=quality_first,
                            forced_engine=force_engine, tier=tier)
    # v0.5：只用 H3。router 已做白名单收敛，这里再兜一次，
    # 防止旧分镜的 engine 字段把流程带到已归档的 Wan2.2 权重上。
    if decision.engine != "h3":
        decision.engine = "h3"
        decision.resolution = router._resolution("h3", quality_first, tier)

    outdir = _out_dir(project_id, episode_seq)
    base = f"{sid}_{decision.engine}"
    # v0.6.4：重渲不覆盖旧产物（老板要求多轮 take 并存）。
    # 此前 local = {base}_v{attempts}，而 attempts 每次调用都从 1 起，
    # 重渲直接覆盖旧 v1（2026-10-24 镶14 重渲实测旧文件丢失）。
    # 现在扫描已有 {base}_v*.mp4，从「已存在最大版本+1」起算。
    v_off = 0
    try:
        import glob as _glob
        for fp in _glob.glob(str(outdir / f"{base}_v*.mp4")):
            m = re.search(r"_v(\d+)\.mp4$", os.path.basename(fp))
            if m:
                v_off = max(v_off, int(m.group(1)))
    except Exception:
        v_off = 0

    db.update_shot(sid, status="running", engine=decision.engine,
                   resolution=decision.resolution, steps=decision.steps,
                   error=None)
    job_id = db.create_job(sid, decision.engine)
    t0 = time.time()

    # 超时/轮询/显存释放全部走 comfy 段配置（可线下改 settings.yaml）
    timeout = int(cfg.get("settings", "comfy.wait_timeout_sec",
                          cfg.get("settings", "render.shot_timeout_sec", 7200)) or 7200)
    free_before = bool(cfg.get("settings", "comfy.free_vram_before_shot",
                               cfg.get("settings", "render.free_vram_before_shot", True)))
    poll = float(cfg.get("settings", "comfy.poll_interval_sec",
                         cfg.get("settings", "render.poll_interval_sec", 3)) or 3)
    max_retries = int(cfg.get("settings", "render.max_retries", 3) or 3)
    strategy = RETRY_STRATEGY[:max(1, min(max_retries, len(RETRY_STRATEGY)))]

    attempts = 0
    last_err = ""
    cur_pid: str | None = None
    for attempt, strat in enumerate(strategy):
        attempts = attempt + 1
        steps = max(4, decision.steps + strat.get("steps_delta", 0))
        res = decision.resolution
        if strat.get("downscale"):
            w, h = (int(x) for x in res.split("x"))
            res = f"{max(384, int(w * 0.75) // 32 * 32)}x{max(384, int(h * 0.75) // 32 * 32)}"
        seed = shot.get("seed")
        if seed is None:
            seed = random.randint(0, 2**31 - 1)
        if strat.get("bump_seed"):
            seed = (seed + 1) % (2**31 - 1)

        try:
            if free_before:
                comfy.free_vram()
                time.sleep(1)

            first_frame = shot.get("first_frame")
            if first_frame and not os.path.exists(first_frame):
                first_frame = None

            # ---- v0.6 方案 A+B ----
            # B：有角色参考图 → 走 Ref2VA（参考图参与每一步采样，锁脸）
            #    没有参考图 → 退回 FL2VA 首帧模式（原来的行为）
            ref_paths = character_ref_images(project_id, shot) \
                if ep_ref2va() else []
            use_ref = bool(ref_paths) and not first_frame

            eng_prompt = build_h3_prompt(shot, project_id=project_id,
                                         episode_id=shot.get("episode_id") or "")
            # 参考图要在提示词里按 <Picture i> 编号引用，模型才知道谁是第几张
            if use_ref:
                from .script import prompt_language
                tags = "、".join(f"<Picture {i+1}>" for i in range(len(ref_paths)))
                if prompt_language() == "zh":
                    eng_prompt = (f"人物长相严格参考 {tags}（按顺序对应"
                                  f"{'、'.join((shot.get('characters') or [])[:len(ref_paths)])}）。"
                                  + eng_prompt)
                else:
                    eng_prompt = (f"Character identity strictly follows {tags}. "
                                  + eng_prompt)

            if use_ref:
                uploaded = []
                for rp in ref_paths:
                    up = comfy.upload_image(rp)
                    if up:
                        uploaded.append(up)
                if not uploaded:
                    use_ref = False
                else:
                    ref_paths = uploaded
            elif not first_frame:
                # 没有参考图也没有首帧 → 拿第一张角色图当首帧
                ff = character_ref_image(project_id, shot)
                if ff:
                    first_frame = comfy.upload_image(ff)

            # ⚠️ 关键：只要走 FL2VA 且带首帧，**必须先上传**再进图。
            # ComfyUI 的 LoadImage 只认自己 input 目录下的文件名，
            # 传 /output/... 这种绝对路径会报 "Invalid image file"。
            #
            # v0.6 重构时把这一步漏在了 use_ref 分支里，导致：
            #   镜1 无首帧 → Ref2VA 成功
            #   镜2 拿到镜1尾帧 → use_ref=False → 但没上传 → LoadImage 失败
            #   镜2 失败后 prev_last_frame 清空 → 镜3 又走 Ref2VA 成功
            # 于是 100% 奇数成功 / 偶数失败（示例人物A项目实测 37 镜失败）。
            # 只要还是绝对路径（/output/... 尾帧 或 /input/... 上传路径），
            # 都必须先上传换成 ComfyUI input 目录下的文件名。
            if not use_ref and first_frame and os.path.isabs(first_frame):
                first_frame = comfy.upload_image(first_frame)

            if use_ref:
                wf = workflow.build(
                    "h3ref",
                    prompt=eng_prompt,
                    resolution=res,
                    duration=float(shot.get("duration") or 5),
                    steps=steps,
                    seed=seed,
                    ref_images=ref_paths,
                    ref_image_size=str(
                        cfg.get("settings", "render.ref2va_image_size", "match") or "match"),
                    filename_prefix=base,
                )
            else:
                wf = workflow.build(
                    decision.engine,
                    prompt=eng_prompt,
                    negative=shot.get("negative") or "",
                    resolution=res,
                    duration=float(shot.get("duration") or 5),
                    steps=steps,
                    seed=seed,
                    first_frame=first_frame,
                    filename_prefix=base,
                )
            pid = comfy.submit(wf)
            cur_pid = pid
            db.update_job(job_id, prompt_id=pid, status="running",
                          started_at=time.time())

            h = comfy.wait(pid, timeout=timeout, poll=poll)
            fn = _find_output(h)
            if not fn:
                raise RuntimeError("产物为空（history 无 output）")

            ext = os.path.splitext(fn)[1] or ".mp4"
            local = str(outdir / f"{base}_v{attempts + v_off}{ext}")
            if not _pull_output(fn, local):
                raise RuntimeError(f"产物拉取失败: {fn}")

            dur_ms = int((time.time() - t0) * 1000)
            db.update_shot(sid, status="done", output_path=local,
                           seed=seed, steps=steps, resolution=res,
                           duration_ms=dur_ms, retries=attempts - 1, error=None)
            db.update_job(job_id, status="done", finished_at=time.time())
            cur_pid = None
            return {**db.get_shot(sid), "_route": decision.__dict__}

        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            # 关键：把已提交但没人接收的 prompt 撤掉。
            # 不撤的话 ComfyUI 会继续把它跑完，占着 GPU 空转，
            # 后面所有镜头都排在它后面（实测残留 72 个任务空转 8 小时）。
            if cur_pid:
                try:
                    comfy.cancel_prompt(cur_pid)
                except Exception:
                    pass
                cur_pid = None
            db.update_job(job_id, status="failed", error=last_err[:1000],
                          finished_at=time.time())
            comfy.free_vram()
            if attempt + 1 < len(strategy):
                time.sleep(3)
                continue

    dur_ms = int((time.time() - t0) * 1000)
    if cur_pid:
        try:
            comfy.cancel_prompt(cur_pid)
        except Exception:
            pass
    db.update_shot(sid, status="failed", error=last_err[:1000],
                   retries=attempts - 1, duration_ms=dur_ms)
    return {**db.get_shot(sid), "_route": decision.__dict__}


def run_episode(episode_id: str, *, quality_first: bool = False,
                force_engine: str | None = None,
                max_shots: int | None = None,
                upscale: str = "",
                assemble_after: bool = True,
                tier: str = "",
                progress=None) -> dict:
    """跑完整集：逐镜串行 → 拼接 → 响度/字幕 → (超分)。"""
    # v0.5：只有 H3，渲染模式字段保留不用
    ep = db.get_episode(episode_id)
    if not ep:
        raise ValueError(f"剧集不存在: {episode_id}")
    shots = db.list_shots(episode_id)
    if not shots:
        return {"ok": False, "error": "该集没有镜头"}
    force_engine = "h3"

    db.set_episode_status(episode_id, "rendering")
    db.set_stage(ep["project_id"], "rendering")

    todo = [s for s in shots if s["status"] in ("pending", "failed")]
    if max_shots:
        todo = todo[:max_shots]

    cooldown = int(cfg.get("settings", "render.cooldown_between_shots_sec", 0) or 0)

    results = []
    assemble_error: str | None = None
    with LOCK:
        # 镜头衔接：把上一镜的尾帧作为下一镜首帧，减少画面跳变。
        # 实测：不接的话每个镜头独立生成，人物/环境会突然换样。
        #
        # ⚠️ v0.6 关键修正：**只在同一场次内接帧**。
        # E02 实测：镜3（靴子踩柏油路，外景）尾帧喂给镜4（加油站正面），
        # 镜4 尾帧又喂给镜5（内景），模型被迫在两个完全不同的空间之间做形变，
        # 于是出现"场景溶解/变色"。跨场次不该接帧，应该让模型重新建场。
        chain = bool(cfg.get("settings", "render.chain_frames", True))
        same_scene_only = bool(
            cfg.get("settings", "render.chain_same_scene_only", True))
        prev_last_frame: str | None = None
        prev_scene = None

        for i, s in enumerate(todo, 1):
            scene = s.get("scene_id")
            # 用户没指定首帧、且开了衔接 → 用上一镜尾帧
            allow_chain = chain and prev_last_frame and not s.get("first_frame")
            if allow_chain and same_scene_only and prev_scene is not None \
                    and scene != prev_scene:
                # 跨场次：不接帧，并清掉尾帧避免误用
                allow_chain = False
                if progress:
                    progress({"phase": "chain", "shot": s["seq"],
                              "status": "skip", "reason": "跨场次不接帧",
                              "from_scene": prev_scene, "to_scene": scene})
            if allow_chain:
                s = {**s, "first_frame": prev_last_frame}
            prev_scene = scene
            if progress:
                progress({"phase": "render", "index": i, "total": len(todo),
                          "shot": s["seq"], "status": "start",
                          "engine": s.get("engine")})
            r = render_shot(s, project_id=ep["project_id"],
                            episode_seq=ep["seq"],
                            quality_first=quality_first,
                            force_engine=force_engine,
                            tier=tier)

            # 成功后抽尾帧，供下一镜使用
            if chain and r.get("status") == "done" and r.get("output_path"):
                try:
                    outdir = _out_dir(ep["project_id"], ep["seq"])
                    lf = outdir / f"{r['id']}_lastframe.png"
                    assemble.extract_last_frame(r["output_path"], str(lf))
                    prev_last_frame = str(lf)
                    if progress:
                        progress({"phase": "chain", "shot": s["seq"],
                                  "status": "ok"})
                except Exception as e:
                    prev_last_frame = None
                    if progress:
                        progress({"phase": "chain", "shot": s["seq"],
                                  "status": "failed", "error": str(e)[:120]})
            else:
                prev_last_frame = None
            results.append(r)
            if progress:
                progress({"phase": "render", "index": i, "total": len(todo),
                          "shot": s["seq"], "status": r["status"],
                          "engine": r.get("engine"),
                          "error": r.get("error")})
            if cooldown and i < len(todo):
                if progress:
                    progress({"phase": "cooldown", "seconds": cooldown,
                              "status": "wait"})
                time.sleep(cooldown)

        # ---- 成片：拼接 → 响度/字幕 → (超分) ----
        done = [x for x in db.list_shots(episode_id) if x["status"] == "done"]
        if done and not max_shots and assemble_after:
            if progress:
                progress({"phase": "assemble", "status": "start",
                          "sources": len(done)})
            try:
                art = _assemble(ep, done, progress=progress, upscale=upscale)
                db.set_episode_status(episode_id, "done")
                db.set_stage(ep["project_id"], "done")
                if progress:
                    progress({"phase": "assemble", "status": "done",
                              "output": art.get("summary")})
            except Exception as e:
                db.set_episode_status(episode_id, "rendering")
                assemble_error = f"{type(e).__name__}: {e}"
                if progress:
                    progress({"phase": "assemble", "status": "failed",
                              "error": assemble_error[:400]})

    st = db.stats(ep["project_id"])
    final = db.latest_artifact(episode_id, "final")
    return {"ok": assemble_error is None and bool(final),
            "rendered": len(results),
            "done": sum(1 for r in results if r["status"] == "done"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "final": (final or {}).get("path"),
            "assemble_error": assemble_error,
            "stats": st}


def assemble_episode_only(episode_id: str, *, upscale: str = "",
                          progress=None) -> dict:
    """只重新成片（不重渲镜头）：拼接 → 响度/字幕 → (超分)。

    供「只重新成片」与队列使用：已完成镜头的渲染产物都还在，
    改字幕/改画幅/加超分时不必重跑 GPU。
    """
    ep = db.get_episode(episode_id)
    if not ep:
        raise ValueError(f"剧集不存在: {episode_id}")
    done = [s for s in db.list_shots(episode_id) if s["status"] == "done"]
    if not done:
        raise ValueError("该集没有已完成的镜头")
    with LOCK:
        art = _assemble(ep, done, progress=progress, upscale=upscale)
    db.set_episode_status(episode_id, "done")
    db.set_stage(ep["project_id"], "done")
    return art


def _assemble(ep: dict, done_shots: list[dict], *,
              progress=None, upscale: str = "") -> dict:
    """渲染成片的完整链路（v0.4）：

        ① 逐镜渲染产物（已就绪）
        ② 统一规格 + 拼接          → concat 原件（无字幕）
        ③ 响度归一化 + 烧字幕        → 1080p 成片
        ④ 可选超分（Lanczos）      → 2K/4K

    所有产物写入 artifact 表，界面可列出/预览/下载。
    返回 {"final": {...}, "concat": {...}, "upscale": {...}|None}
    """
    outdir = _out_dir(ep["project_id"], ep["seq"])
    tag = f"E{ep['seq']:02d}"
    files = [{"path": s["output_path"], "dialogue": s.get("dialogue"),
              "duration": s.get("duration")}
             for s in done_shots if s.get("output_path")]

    srt = outdir / "subtitle.srt"
    srt_path = assemble.srt_from_shots(done_shots, str(srt))
    burn = bool(cfg.get("settings", "assemble.burn_subtitles", True))

    concat_out = str(outdir / f"{tag}_concat.mp4")
    dst = str(outdir / f"{tag}_final.mp4")
    r = assemble.assemble_episode(
        files, dst,
        subtitle_srt=srt_path if burn else None,
        target=assemble.target_from_config(),
        concat_out=concat_out,
        progress=progress)

    # ---- 登记产物 ----
    art: dict = {}
    try:
        if os.path.exists(concat_out):
            ci = assemble.info(concat_out)
            aid = db.add_artifact(
                ep["id"], "concat", concat_out,
                label=f"{tag} 拼接原件（{ci['width']}x{ci['height']}，无字幕）",
                width=ci["width"], height=ci["height"],
                duration=ci["duration"], size=ci["size"],
                meta={"sources": len(files), "vcodec": r.get("vcodec")})
            art["concat"] = db.list_artifacts(ep["id"], "concat")[0] \
                if db.list_artifacts(ep["id"], "concat") else {"id": aid}
        fi = assemble.info(dst)
        with_sub = bool(srt_path and os.path.getsize(srt_path) > 0)
        label = (f"{tag} 成片 {fi['width']}x{fi['height']} "
                 f"{fi['duration']:.1f}s{'+字幕' if with_sub else '（无台词，无字幕）'}")
        db.add_artifact(ep["id"], "final", dst, label=label,
                        width=fi["width"], height=fi["height"],
                        duration=fi["duration"], size=fi["size"],
                        meta={"sources": len(files), "vcodec": r.get("vcodec"),
                              "burn_subtitles": with_sub})
        art["final"] = db.list_artifacts(ep["id"], "final")[0]
        if srt_path and os.path.exists(srt_path) and os.path.getsize(srt_path) > 0:
            db.add_artifact(ep["id"], "srt", srt_path,
                            label=f"{tag} 字幕 SRT",
                            size=os.path.getsize(srt_path))
        db.prune_artifacts(ep["id"], "final", keep=5)
        db.prune_artifacts(ep["id"], "concat", keep=3)
    except Exception as e:
        # 产物登记失败不能影响成片本身
        if progress:
            progress({"phase": "artifact", "status": "warn",
                      "error": f"产物登记失败: {e}"})

    # ---- 超分 ----
    if upscale:
        if progress:
            progress({"phase": "upscale", "status": "start",
                      "target": upscale})
        up = dst.replace(".mp4", f"_{upscale}.mp4")
        ui = assemble.upscale(dst, up, upscale, progress=progress)
        try:
            db.add_artifact(
                ep["id"], "upscale", up,
                label=f"{tag} 超分 {upscale.upper()} "
                      f"{ui['width']}x{ui['height']}",
                width=ui["width"], height=ui["height"],
                duration=ui["duration"], size=ui["size"],
                target=upscale,
                meta={"source_resolution": ui.get("source_resolution")})
            art["upscale"] = db.list_artifacts(ep["id"], "upscale")[0]
            db.prune_artifacts(ep["id"], "upscale", keep=5)
        except Exception as e:
            if progress:
                progress({"phase": "artifact", "status": "warn",
                          "error": f"超分登记失败: {e}"})
        if progress:
            progress({"phase": "upscale", "status": "done",
                      "target": upscale,
                      "resolution": f"{ui['width']}x{ui['height']}",
                      "path": up})
        art["upscale_path"] = up

    art["summary"] = (f"{dst} ({r['width']}x{r['height']}, "
                      f"{r['duration']:.2f}s)")
    art["final_path"] = dst
    return art
