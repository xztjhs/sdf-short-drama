"""成片层 —— ffmpeg 拼接 / 混音 / 响度归一化 / 字幕。

关键认知（写下来避免重复踩）：
- 单纯拼接（同参数）用 -c copy，秒级完成，不重新编码
- 分辨率/帧率不一致才需要重编码 → 走 NVENC 或限制 -threads 16
- 64 线程对滤镜和 loudnorm 有用，对单流编码本身帮助有限
- 响度按平台 -16 LUFS（抖音/红果），不做会忽大忽小
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def ffmpeg_bin() -> str:
    return os.environ.get("SDF_FFMPEG", "ffmpeg")


def ffprobe_bin() -> str:
    return os.environ.get("SDF_FFPROBE", "ffprobe")


def available() -> bool:
    return shutil.which(ffmpeg_bin()) is not None


def probe(path: str) -> dict:
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(f"ffprobe 失败: {r.stderr[:800]}")
    return json.loads(r.stdout)


def info(path: str) -> dict:
    d = probe(path)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    fps = 0.0
    if v and v.get("r_frame_rate"):
        n, _, dd = v["r_frame_rate"].partition("/")
        fps = float(n) / float(dd or 1)
    return {
        "duration": float(d["format"].get("duration", 0)),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps": round(fps, 3),
        "has_audio": a is not None,
        "audio_channels": int(a["channels"]) if a else 0,
        "vcodec": v["codec_name"] if v else None,
        "size": int(d["format"].get("size", 0)),
    }


@dataclass
class Target:
    width: int = 1080
    height: int = 1920
    fps: int = 24
    vcodec: str = "libx264"       # 或 h264_nvenc
    crf: int = 20
    preset: str = "medium"
    pix_fmt: str = "yuv420p"
    threads: int = 16             # 单流编码 16 线程足够，64 无额外收益
    lufs: float = -16.0


_NVENC_CACHE: dict[str, bool] = {}


def nvenc_available() -> bool:
    """探测 ffmpeg 是否带 h264_nvenc（有则编码快 3-5 倍）。"""
    key = ffmpeg_bin()
    if key in _NVENC_CACHE:
        return _NVENC_CACHE[key]
    ok = False
    try:
        r = subprocess.run([ffmpeg_bin(), "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=15)
        ok = "h264_nvenc" in (r.stdout or "")
    except Exception:
        ok = False
    _NVENC_CACHE[key] = ok
    return ok


def target_from_config() -> "Target":
    """从 config/settings.yaml 的 assemble 段构造 Target。

    v0.5：默认就用 720p 画布（与单镜渲染基准一致），不再默认抬到 1080p。
    要交付分辨率请显式走「超分」（1080P/2K/4K）。
    """
    from .config import cfg
    g = lambda k, d: cfg.get("settings", f"assemble.{k}", d)
    vcodec = str(g("vcodec", "auto") or "auto").lower()
    if vcodec in ("auto", ""):
        vcodec = "h264_nvenc" if nvenc_available() else "libx264"
    orientation = str(g("orientation", "portrait") or "portrait").lower()
    w = int(g("width", 720))
    h = int(g("height", 1280))
    # 朝向与宽高不一致时自动纠正（避免手改成 landscape 但忘了换宽高）
    if orientation.startswith("land") and h > w:
        w, h = h, w
    elif not orientation.startswith("land") and w > h:
        w, h = h, w
    return Target(
        width=w,
        height=h,
        fps=int(g("fps", 24)),
        vcodec=vcodec,
        crf=int(g("crf", 20)),
        preset=str(g("preset", "medium")),
        threads=int(g("threads", 16)),
        lufs=float(g("lufs", -16.0)),
    )


def _scale_mode_from_config() -> str:
    from .config import cfg
    return str(cfg.get("settings", "assemble.scale_mode", "pad") or "pad")


def _level_match_enabled() -> bool:
    """逐镜响度对齐开关（默认开）。

    关掉可跳过这一步（老片重跑成片时省时间），
    但会回到「镜头之间 ±20dB 跳变」的老问题。
    """
    from .config import cfg
    return bool(cfg.get("settings", "assemble.level_match", True))


def _norm_filter(t: Target, scale_mode: str = "pad") -> str:
    """统一画布：等比缩放 + 补黑边（pad），不裁画面。"""
    if scale_mode == "crop":
        return (f"scale={t.width}:{t.height}:force_original_aspect_ratio=increase,"
                f"crop={t.width}:{t.height}")
    return (f"scale={t.width}:{t.height}:force_original_aspect_ratio=decrease,"
            f"pad={t.width}:{t.height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


def normalize_clip(src: str, dst: str, t: Target, *,
                   keep_audio: bool = True, scale_mode: str = "pad") -> str:
    """把任意片段规范成统一的 1080×1920 / 24fps / yuv420p。

    无音轨的补静音，保证后续 concat 时音轨对齐。
    """
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    vf = _norm_filter(t, scale_mode)
    cmd = [ffmpeg_bin(), "-y", "-i", src]

    has_a = info(src)["has_audio"]
    if not has_a:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]

    cmd += ["-vf", vf, "-r", str(t.fps), "-pix_fmt", t.pix_fmt,
            "-c:v", t.vcodec, "-threads", str(t.threads)]
    if t.vcodec == "libx264":
        cmd += ["-crf", str(t.crf), "-preset", t.preset]
    else:
        cmd += ["-rc", "vbr", "-cq", str(t.crf), "-b:v", "0"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-shortest", dst]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(f"规范化失败 {Path(src).name}: {r.stderr[-1500:]}")
    return dst


def concat(parts: list[str], dst: str, workdir: str | None = None) -> str:
    """无损拼接（要求所有片段参数一致，先 normalize_clip）。"""
    if not parts:
        raise FFmpegError("没有可拼接的片段")
    workdir = workdir or os.path.dirname(dst)
    listing = os.path.join(workdir, "_concat.txt")
    with open(listing, "w") as f:
        for p in parts:
            f.write(f"file '{os.path.abspath(p)}'\n")
    cmd = [ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0",
           "-i", listing, "-c", "copy", dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(f"拼接失败: {r.stderr[-1500:]}")
    os.remove(listing)
    return dst


# 超分目标（成片后手动调用）
UPSCALE_PRESETS = {
    "1080p": (1080, 1920),
    "1440p": (1440, 2560),
    "2k":    (1440, 2560),
    "4k":    (2160, 3840),
}


def upscale(src: str, dst: str, target: str = "1080p",
            *, crf: int = 18, preset: str = "slow",
            progress=None) -> dict:
    """把成片超分到目标分辨率（Lanczos 重采样 + 保持比例补边）。

    说明：这是「高质量重采样」而不是 AI 超分。
    AI 超分（Real-ESRGAN/SeedVR2）需要额外模型，当前未装。
    对 720p→1080p 这种 1.5x，Lanczos 效果已够用；
    2K/4K 会有明显涂抹感，建议只在必要时用。
    """
    if target not in UPSCALE_PRESETS:
        raise FFmpegError(f"未知超分档位: {target}（可用: {list(UPSCALE_PRESETS)}）")
    w, h = UPSCALE_PRESETS[target]

    src_info = info(src)
    # 保持原比例：按长边对齐，短边补边
    sw, sh = src_info["width"], src_info["height"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")

    cmd = [ffmpeg_bin(), "-y", "-i", src, "-vf", vf,
           "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
           "-pix_fmt", "yuv420p", "-c:a", "copy", dst]
    if progress:
        progress({"phase": "upscale", "status": "run", "target": target,
                  "source": f"{sw}x{sh}"})
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(f"超分失败: {r.stderr[-1200:]}")
    out = info(dst)
    out["source_resolution"] = f"{sw}x{sh}"
    out["target"] = target
    return out


def loudnorm(src: str, dst: str, t: Target) -> str:
    """两遍 loudnorm 到平台标准响度。第一遍测量，第二遍应用。

    ⚠️ 单遍 loudnorm 只保证「整条」符合目标响度，管不住「镜头之间」的跳变。
    实测《最后一个房间》：整片 -22.8dB 看着正常，但逐镜测：
      镜1 -40.9dB → 镜2 -20.7dB（+20.2dB 突变）→ 镜7 -36.5dB（-17.1dB）
    观众听感就是「一会儿听不见、一会儿吓一跳」，交付级不可接受。
    根因：H3 每镜独立生成音轨，环境声镜和说话镜的电平本来差很多。
    修法：先逐镜做 EBU R128 对齐（只对偏低/偏高的镜头做有限增益修正），
         再整条 loudnorm 收口。见 `level_match`。
    """
    probe_cmd = [ffmpeg_bin(), "-hide_banner", "-i", src, "-af",
                 f"loudnorm=I={t.lufs}:TP=-1.5:LRA=11:print_format=json",
                 "-f", "null", "-"]
    r = subprocess.run(probe_cmd, capture_output=True, text=True)
    stats = {}
    try:
        tail = r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1]
        stats = json.loads(tail)
    except Exception:
        stats = {}

    if stats:
        af = (f"loudnorm=I={t.lufs}:TP=-1.5:LRA=11:"
              f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
              f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
              f"offset={stats['target_offset']}:linear=true")
    else:
        af = f"loudnorm=I={t.lufs}:TP=-1.5:LRA=11"

    # ⚠️ 2026-10-25 修：必须显式指定 -ar 48000。
    #   不指定时 loudnorm 会让 AAC 编码器自行选采样率，
    #   实测《最后一个房间》单镜 32kHz → concat 48kHz → final **96kHz**，
    #   等于凭空重采样两次，高频被反复插值，还让文件无谓变大。
    cmd = [ffmpeg_bin(), "-y", "-i", src, "-af", af,
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-ar", "48000", "-ac", "2", dst]
    r2 = subprocess.run(cmd, capture_output=True, text=True)
    if r2.returncode != 0:
        raise FFmpegError(f"响度归一化失败: {r2.stderr[-1200:]}")
    return dst


def measure_lufs(src: str) -> float | None:
    """测一条音轨的 integrated 响度（LUFS）。失败返回 None。"""
    cmd = [ffmpeg_bin(), "-hide_banner", "-i", src, "-af",
           "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        tail = r.stderr[r.stderr.rfind("{"):r.stderr.rfind("}") + 1]
        return float(json.loads(tail)["input_i"])
    except Exception:
        return None


def level_match(src: str, dst: str, *, target_lufs: float = -18.0,
                max_gain_db: float = 12.0, t: Target | None = None) -> str:
    """把单个镜头的响度朝 target_lufs 靠，但限制增益幅度。

    为什么要限幅（max_gain_db）：纯环境声镜头（如空镜、脚步）
    原始只有 -48LUFS，硬拉到 -18 会把底噪一起放大 30dB，噪声明显。
    所以超过 max_gain_db 就只给 max_gain_db，宁可留一点音量差，
    也不要制造"嘶——"的底噪。

    只做音量调整（volume 滤镜），不动动态、不重采样。
    """
    cur = measure_lufs(src)
    if cur is None or cur <= -70:
        # 测不出来（接近静音）就原样转封装，别硬拉
        shutil.copy2(src, dst)
        return dst
    gain = target_lufs - cur
    gain = max(-max_gain_db, min(max_gain_db, gain))
    if abs(gain) < 0.5:
        shutil.copy2(src, dst)
        return dst
    tt = t or Target()
    cmd = [ffmpeg_bin(), "-y", "-i", src,
           "-af", f"volume={gain:.2f}dB",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-ar", "48000", "-ac", "2", dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(f"音量对齐失败 {Path(src).name}: {r.stderr[-1200:]}")
    return dst


def burn_subtitles(src: str, dst: str, srt_path: str, t: Target,
                   style: str | None = None) -> str:
    """烧录字幕（平台不自动生成时必须烧进画面）。

    字幕样式从 settings.assemble.subtitle 读取（v0.6.4：
    老板要求手动可调位置与大小），默认底部居中 + 小字号。
    """
    from .config import cfg
    if style is None:
        # 字幕样式配置：settings.assemble.subtitle.{font_size,margin_v,alignment,...}
        sub_cfg = cfg.get("settings", "assemble.subtitle", {}) or {}
        if isinstance(sub_cfg, str):
            try:
                import json as _json
                sub_cfg = _json.loads(sub_cfg)
            except Exception:
                sub_cfg = {}
        font_size = int(sub_cfg.get("font_size", 18))
        margin_v = int(sub_cfg.get("margin_v", 60))
        alignment = int(sub_cfg.get("alignment", 2))  # libass numpad: 2=底中 5=居中 8=顶中
        font_name = sub_cfg.get("font_name", "Noto Sans CJK SC")
        primary = sub_cfg.get("primary_colour", "&H00FFFFFF")
        outline_c = sub_cfg.get("outline_colour", "&H00000000")
        outline_w = int(sub_cfg.get("outline", 2))
        shadow = int(sub_cfg.get("shadow", 0))
        style = (f"FontName={font_name},FontSize={font_size},"
                 f"PrimaryColour={primary},OutlineColour={outline_c},"
                 f"BorderStyle=1,Outline={outline_w},Shadow={shadow},"
                 f"Alignment={alignment},MarginV={margin_v}")
    esc = srt_path.replace("\\", "/").replace(":", "\\:")
    vf = f"subtitles='{esc}':force_style='{style}'"
    cmd = [ffmpeg_bin(), "-y", "-i", src, "-vf", vf,
           "-c:v", t.vcodec, "-threads", str(t.threads), "-pix_fmt", t.pix_fmt]
    if t.vcodec == "libx264":
        cmd += ["-crf", str(t.crf), "-preset", t.preset]
    cmd += ["-c:a", "copy", dst]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise FFmpegError(f"字幕烧录失败: {r.stderr[-1500:]}")
    return dst


def extract_last_frame(video_path: str, dst_png: str) -> str:
    """抽视频最后一帧存成 PNG（用于下一镜首帧，保证画面衔接）。"""
    Path(dst_png).parent.mkdir(parents=True, exist_ok=True)
    # 先探时长，再从倒数 0.1 秒取一帧（避免取到黑尾帧）
    try:
        dur = info(video_path)["duration"]
    except Exception:
        dur = 0
    seek = max(0.0, dur - 0.12)
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{seek:.3f}",
           "-i", video_path, "-frames:v", "1", "-q:v", "2", dst_png]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(dst_png):
        raise FFmpegError(f"抽帧失败: {r.stderr[-400:]}")
    return dst_png


def srt_from_shots(shots: list[dict], dst: str) -> str | None:
    """从镜头表生成 SRT（按累计时长排时间轴）。

    没有一句台词的集（纯空镜/动作，常见于 Wan2.2 模式）返回 None，
    调用方要据此跳过烧字幕 —— 0 字节的 srt 会让 libass 直接报
    "Unable to open xxx.srt" 然后整条成片链路失败（2026-10-22 实测）。
    """
    def ts(sec: float) -> str:
        h = int(sec // 3600); m = int(sec % 3600 // 60); s = int(sec % 60)
        ms = int(round((sec - int(sec)) * 1000))
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    t = 0.0
    idx = 1
    for s in shots:
        dur = float(s.get("duration") or 5)
        txt = (s.get("dialogue") or "").strip()
        if txt:
            lines.append(f"{idx}\n{ts(t)} --> {ts(t + dur - 0.05)}\n{txt}\n")
            idx += 1
        t += dur
    if not lines:
        try:
            if Path(dst).exists():
                Path(dst).unlink()
        except OSError:
            pass
        return None
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text("\n".join(lines), encoding="utf-8")
    return dst


def assemble_episode(shot_files: list[dict], dst: str, *,
                     subtitle_srt: str | None = None,
                     target: Target | None = None,
                     workdir: str | None = None,
                     concat_out: str | None = None,
                     progress=None) -> dict:
    """完整成片流程：规范化 → 拼接 → 响度 → (字幕)。

    shot_files: [{"path": ..., "dialogue": ..., "duration": ...}, ...]（按顺序）
    concat_out: 拼接原件保留路径（无字幕、未归一化响度的中间件，便于对比）
    progress:   回调，形如 {"phase": "normalize|concat|loudnorm|subtitle", ...}
    """
    def emit(**ev):
        if progress:
            progress(ev)

    t = target or target_from_config()
    workdir = workdir or str(Path(dst).parent / "_work")
    Path(workdir).mkdir(parents=True, exist_ok=True)

    total = len(shot_files)
    scale_mode = _scale_mode_from_config()
    normed = []
    for i, sf in enumerate(shot_files):
        p = sf["path"]
        if not os.path.exists(p):
            raise FFmpegError(f"片段缺失: {p}")
        out = os.path.join(workdir, f"n_{i:03d}.mp4")
        emit(phase="normalize", index=i + 1, total=total,
             name=Path(p).name, status="start")
        normalize_clip(p, out, t, scale_mode=scale_mode)
        normed.append(out)

    emit(phase="normalize", index=total, total=total, status="done")

    # 逐镜响度对齐（2026-10-25）
    # 单遍 loudnorm 只管整条，管不住镜头之间。实测《最后一个房间》：
    #   镜1 -40.9dB → 镜2 -20.7dB（+20.2dB）→ 镜7 -36.5dB（-17.1dB）
    # 听感是"一会儿听不见、一会儿吓一跳"。先逐镜拉平再整条收口。
    if _level_match_enabled():
        emit(phase="level", index=0, total=total, status="start")
        for i, n in enumerate(normed):
            out2 = os.path.join(workdir, f"lv_{i:03d}.mp4")
            emit(phase="level", index=i + 1, total=total,
                 name=Path(n).name, status="start")
            level_match(n, out2, t=t)
            normed[i] = out2
        emit(phase="level", index=total, total=total, status="done")

    emit(phase="concat", index=total, total=total, status="start")
    joined = os.path.join(workdir, "_joined.mp4")
    concat(normed, joined, workdir)
    if concat_out:
        Path(concat_out).parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(joined, concat_out)
        except (OSError, shutil.Error):
            pass
    emit(phase="concat", status="done")

    emit(phase="loudnorm", status="start")
    lufs = os.path.join(workdir, "_lufs.mp4")
    loudnorm(joined, lufs, t)
    emit(phase="loudnorm", status="done")

    if subtitle_srt and os.path.exists(subtitle_srt) and \
            os.path.getsize(subtitle_srt) > 0:
        emit(phase="subtitle", status="start")
        burn_subtitles(lufs, dst, subtitle_srt, t)
        emit(phase="subtitle", status="done")
    else:
        if subtitle_srt:
            emit(phase="subtitle", status="skip",
                 reason="没有台词，无需烧字幕")
        shutil.copy2(lufs, dst)

    result = info(dst)
    result["sources"] = len(shot_files)
    result["vcodec"] = t.vcodec
    return result
