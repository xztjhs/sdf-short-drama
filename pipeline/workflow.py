"""ComfyUI 工作流构造 —— 程序化拼装 API 格式 JSON。

节点签名已对照 ComfyUI 主线源码核实（2026-10-20）：
   comfy_extras/nodes_minimax_h3.py / nodes_wan.py /
   nodes_custom_sampler.py / nodes_video.py / nodes_audio.py / nodes.py

关键事实（都在这里，避免重复踩）：
  1. MiniMaxH3ImageToVideo 没有 negative / cfg / turbo_mode 参数。
     输入 clip / vae / prompt / width / height / length / first_frame? / last_frame?
     输出 (Conditioning positive, Latent) → 必须配 BasicGuider，不能用 KSampler。
  2. turbo 不是节点开关，而是「加载 Turbo LoRA + 用 8 步」。
  3. H3 latent 是嵌套张量 {video, audio}；VAEDecode 取 [0]，VAEDecodeAudio 取 [-1]。
  4. 帧数必须对齐：H3 是 17k+5，Wan 是 4n+1。
  5. CLIPLoader 的 type 对 H3 是 "minimax"（不是 minimax_h3）。

模型文件名/参数全部来自 config/models.yaml 与 config/engines.yaml。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .config import cfg


@dataclass
class Node:
    type: str
    inputs: dict[str, Any] = field(default_factory=dict)
    _id: str | None = None
    title: str | None = None


class Graph:
    def __init__(self) -> None:
        self.nodes: list[Node] = []
        self._n = 0

    def add(self, type_: str, title: str | None = None, **inputs) -> Node:
        self._n += 1
        n = Node(type=type_, inputs=inputs, _id=str(self._n), title=title)
        self.nodes.append(n)
        return n

    @staticmethod
    def _val(v: Any) -> Any:
        """把输入值转成 API 格式。

        需要处理三种情况：
          1. (Node, slot)        → ["节点id", slot]
          2. dict（Autogrow 等动态输入） → 逐项递归转换
          3. 普通字面量           → 原样
        """
        if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], Node):
            return [v[0]._id, v[1]]
        if isinstance(v, dict):
            return {k: Graph._val(x) for k, x in v.items() if x is not None}
        if isinstance(v, list):
            return [Graph._val(x) for x in v]
        return v

    def to_api(self) -> dict:
        out = {}
        for n in self.nodes:
            item: dict[str, Any] = {"class_type": n.type, "inputs": {}}
            for k, v in n.inputs.items():
                if v is None:
                    continue
                item["inputs"][k] = self._val(v)
            if n.title:
                item["_meta"] = {"title": n.title}
            out[n._id] = item
        return out


# ---------------------------------------------------------------- 模型解析
# 两个用途不同，不能混：
#   model_file()     → 完整相对路径，用于磁盘存在性/大小检查
#   model_basename() → 纯文件名，用于喂给 ComfyUI 加载节点
#
# ⚠️ 2026-10-21 踩坑：ComfyUI 的 UNETLoader/CLIPLoader/VAELoader 只接受
#   folder_paths.get_filename_list() 返回的名字——即相对其模型子目录的**纯文件名**，
#   传 "text_encoders/xxx.safetensors" 会报 value_not_in_list。

def model_file(model_id: str) -> str:
    """把 models.yaml 里的 id 解析成完整相对路径（用于文件检查）。"""
    for m in cfg.get("models", "models", []) or []:
        if m.get("id") == model_id:
            if not m.get("enabled", True):
                raise ValueError(f"模型 {model_id} 已在配置中禁用")
            return m["file"]
    raise ValueError(f"models.yaml 中找不到模型 id: {model_id}")


def model_basename(model_id: str) -> str:
    """返回纯文件名，供 ComfyUI 加载节点使用。"""
    return model_file(model_id).replace("\\", "/").rsplit("/", 1)[-1]


def _engine_conf(engine: str) -> dict:
    conf = cfg.get("engines", f"engines.{engine}")
    if not conf:
        raise ValueError(f"engines.yaml 中找不到引擎: {engine}")
    return conf


def _align(v: int, step: int) -> int:
    return max(step, round(v / step) * step)


def _wh(res: str, align: int = 32) -> tuple[int, int]:
    w, h = (int(x) for x in res.lower().replace("*", "x").split("x"))
    return _align(w, align), _align(h, align)


def _frames(engine: str, duration_s: float) -> int:
    """按 engines.yaml 的 frames 规则对齐帧数。"""
    f = cfg.get("engines", f"engines.{engine}.frames", {}) or {}
    grid = int(f.get("grid", 17))
    offset = int(f.get("offset", 5))
    fps = int(f.get("fps", 24))
    fmin = int(f.get("min", 5))
    fmax = f.get("max")

    want = max(fmin, round(duration_s * fps))
    k = max(1, round((want - offset) / grid))
    n = k * grid + offset
    n = max(fmin, n)
    if fmax:
        n = min(int(fmax), n)
    return n


def build_h3(*, prompt: str, negative: str = "", resolution: str = "768x1344",
             duration: float = 5.0, steps: int | None = None, seed: int = 0,
             first_frame: str | None = None, last_frame: str | None = None,
             turbo: bool = True, filename_prefix: str = "h3") -> dict:
    """H3 FL2VA：文生视频 / 首尾帧图生视频，一次出画面 + 原生立体声。

    注：H3 无 negative 分支（BasicGuider 只吃一条 conditioning），
        negative 参数保留仅为接口一致，不参与出图。
    """
    e = _engine_conf("h3")
    w, h = _wh(resolution, int(e.get("align", 32)))
    nf = _frames("h3", duration)
    steps = int(steps if steps is not None else e.get("steps", 8))

    g = Graph()
    unet = g.add("UNETLoader", unet_name=model_basename(e["model_id"]),
                 weight_dtype="default")
    clip = g.add("CLIPLoader", clip_name=model_basename(e["clip_id"]),
                 type=e.get("clip_type", "minimax"))
    vae_v = g.add("VAELoader", vae_name=model_basename(e["vae_video_id"]))
    vae_a = g.add("VAELoader", vae_name=model_basename(e["vae_audio_id"]))

    model_ref: Any = (unet, 0)
    lora_id = e.get("lora_id")
    if turbo and lora_id:
        try:
            lora = g.add("LoraLoaderModelOnly", lora_name=model_basename(lora_id),
                         strength_model=1.0, model=model_ref)
            model_ref = (lora, 0)
        except ValueError:
            pass  # LoRA 未配置/文件缺失时退化为 20 步基线

    ff_arg = None
    if first_frame:
        ff = g.add("LoadImage", image=first_frame)
        ff_arg = (ff, 0)
    lf_arg = None
    if last_frame:
        lf = g.add("LoadImage", image=last_frame)
        lf_arg = (lf, 0)

    h3 = g.add("MiniMaxH3ImageToVideo", title="H3 FL2VA",
               clip=(clip, 0), vae=(vae_v, 0), prompt=prompt,
               width=w, height=h, length=nf,
               first_frame=ff_arg, last_frame=lf_arg)

    guider = g.add("BasicGuider", model=model_ref, conditioning=(h3, 0))
    sampler = g.add("KSamplerSelect", sampler_name=e.get("sampler", "euler"))
    sched = g.add("BasicScheduler", model=model_ref,
                  scheduler=e.get("scheduler", "simple"), steps=steps, denoise=1.0)
    noise = g.add("RandomNoise", noise_seed=seed)
    custom = g.add("SamplerCustomAdvanced",
                   noise=(noise, 0), guider=(guider, 0),
                   sampler=(sampler, 0), sigmas=(sched, 0),
                   latent_image=(h3, 1))
    dec_v = g.add("VAEDecode", samples=(custom, 0), vae=(vae_v, 0))
    dec_a = g.add("VAEDecodeAudio", samples=(custom, 0), vae=(vae_a, 0))
    video = g.add("CreateVideo", images=(dec_v, 0), audio=(dec_a, 0),
                  fps=float(e.get("frames", {}).get("fps", 24)))
    g.add("SaveVideo", video=(video, 0), filename_prefix=filename_prefix,
          format="mp4", codec="h264")
    return g.to_api()


def build_wan22(*, prompt: str, negative: str = "", resolution: str = "768x1344",
                duration: float = 5.0, steps: int | None = None, seed: int = 0,
                first_frame: str | None = None,
                filename_prefix: str = "wan22") -> dict:
    """Wan2.2 TI2V-5B：空镜/动作镜头。无音轨，后期配。"""
    e = _engine_conf("wan22")
    w, h = _wh(resolution, int(e.get("align", 16)))
    nf = _frames("wan22", duration)
    steps = int(steps if steps is not None else e.get("steps", 6))

    g = Graph()
    unet = g.add("UNETLoader", unet_name=model_basename(e["model_id"]),
                 weight_dtype="default")
    clip = g.add("CLIPLoader", clip_name=model_basename(e["clip_id"]),
                 type=e.get("clip_type", "wan"))
    vae = g.add("VAELoader", vae_name=model_basename(e["vae_id"]))

    pos = g.add("CLIPTextEncode", text=prompt, clip=(clip, 0))
    neg = g.add("CLIPTextEncode", text=negative or "", clip=(clip, 0))

    start = None
    if first_frame:
        img = g.add("LoadImage", image=first_frame)
        start = (img, 0)

    lat = g.add("Wan22ImageToVideoLatent", vae=(vae, 0), width=w, height=h,
                length=nf, batch_size=1, start_image=start)
    ks = g.add("KSampler", seed=seed, steps=steps, cfg=float(e.get("cfg", 3.5)),
               sampler_name=e.get("sampler", "euler"),
               scheduler=e.get("scheduler", "simple"), denoise=1.0,
               model=(unet, 0), positive=(pos, 0), negative=(neg, 0),
               latent_image=(lat, 0))
    dec = g.add("VAEDecode", samples=(ks, 0), vae=(vae, 0))
    video = g.add("CreateVideo", images=(dec, 0),
                  fps=float(e.get("frames", {}).get("fps", 24)))
    g.add("SaveVideo", video=(video, 0), filename_prefix=filename_prefix,
          format="mp4", codec="h264")
    return g.to_api()


def build_h3_ref2va(*, prompt: str, resolution: str = "720x1280",
                    duration: float = 5.0, steps: int | None = None,
                    seed: int = 0, ref_images: list[str] | None = None,
                    ref_image_size: str = "match", turbo: bool = True,
                    filename_prefix: str = "h3ref") -> dict:
    """H3 Ref2VA：参考图驱动的生成（v0.6，用于锁人物长相）。

    与 FL2VA 的区别（节点签名已核对 ComfyUI 源码）：
      MiniMaxH3ReferenceToVideo 的输入是
        clip / vae(可选) / audio_vae(可选) / prompt / width / height / length
        / ref_image_size("match"|"max") / ref_images(Autogrow, 前缀 ref_image_,
          最多 9 张) / ref_videos / ref_audios
      输出 (positive conditioning, LATENT)，同样配 BasicGuider。

    关键点：
      1. 参考图**不是首帧**，它作为 identity token 参与每一步采样，
         所以能在整段视频里保持长相（首帧锚定只管开头几秒）。
      2. Autogrow 输入在 API 格式里是**扁平键**：ref_image_0、ref_image_1…
         （见 comfy_api/latest/_io.py::_expand_schema_for_dynamic，前缀 + 序号）
      3. prompt 里用 <Picture 1> / <Picture 2> 指代第几张参考图（1-based）。
      4. ref_image_size="match" 按生成面积缩放（默认，快）；
         "max" 用 2048 短边，身份保真最好但慢数倍。
    """
    e = _engine_conf("h3")
    w, h = _wh(resolution, int(e.get("align", 32)))
    nf = _frames("h3", duration)
    steps = int(steps if steps is not None else e.get("steps", 8))
    ref_model_id = e.get("ref_model_id") or e["model_id"]

    g = Graph()
    unet = g.add("UNETLoader", unet_name=model_basename(ref_model_id),
                 weight_dtype="default")
    clip = g.add("CLIPLoader", clip_name=model_basename(e["clip_id"]),
                 type=e.get("clip_type", "minimax"))
    vae_v = g.add("VAELoader", vae_name=model_basename(e["vae_video_id"]))
    vae_a = g.add("VAELoader", vae_name=model_basename(e["vae_audio_id"]))

    model_ref: Any = (unet, 0)
    lora_id = e.get("lora_id")
    if turbo and lora_id:
        try:
            lora = g.add("LoraLoaderModelOnly", lora_name=model_basename(lora_id),
                         strength_model=1.0, model=model_ref)
            model_ref = (lora, 0)
        except ValueError:
            pass

    refs = [x for x in (ref_images or []) if x][:9]
    # ⚠️ Autogrow 输入在 API 格式里必须是**带前缀的点号键**：
    #       "ref_images.ref_image_0": [node_id, 0]
    #    不是嵌套 dict，也不是扁平的 ref_image_0。
    #
    #    2026-10-23 实测（这是 Ref2VA 参考图长期失效的真正原因）：
    #      写法                          结果
    #      ref_images={"ref_image_0":..}  execute() 收到未知 kwarg 直接报错
    #      ref_images.ref_image_0 点号键  ✅ 正确
    #      嵌套 dict ref_images={...}     静默丢弃 → 参考图完全不参与计算！
    #
    #    证据：传 0 张 / 1 张整图 / 1 张脸部特写，抽帧 md5 完全相同
    #    （6be2e144c07f...）；改成点号键后 md5 立刻变化（88694a8e...），
    #    生成人物才与参考照片一致。
    #    依据：ComfyUI tests-unit/execution_test/dynamic_inputs_test.py
    #      Autogrow 用例即 {"rows.item0": 0.5}
    img_nodes: list[tuple[str, Any]] = []
    for i, path in enumerate(refs):
        img = g.add("LoadImage", title=f"参考图{i+1}", image=path)
        img_nodes.append((f"ref_images.ref_image_{i}", (img, 0)))

    h3_inputs: dict[str, Any] = dict(
        clip=(clip, 0), vae=(vae_v, 0), audio_vae=(vae_a, 0),
        prompt=prompt, width=w, height=h, length=nf,
        ref_image_size=ref_image_size)
    for key, ref in img_nodes:
        h3_inputs[key] = ref
    h3 = g.add("MiniMaxH3ReferenceToVideo", title="H3 Ref2VA", **h3_inputs)

    guider = g.add("BasicGuider", model=model_ref, conditioning=(h3, 0))
    sampler = g.add("KSamplerSelect", sampler_name=e.get("sampler", "euler"))
    sched = g.add("BasicScheduler", model=model_ref,
                  scheduler=e.get("scheduler", "simple"), steps=steps, denoise=1.0)
    noise = g.add("RandomNoise", noise_seed=seed)
    custom = g.add("SamplerCustomAdvanced",
                   noise=(noise, 0), guider=(guider, 0),
                   sampler=(sampler, 0), sigmas=(sched, 0),
                   latent_image=(h3, 1))
    dec_v = g.add("VAEDecode", samples=(custom, 0), vae=(vae_v, 0))
    dec_a = g.add("VAEDecodeAudio", samples=(custom, 0), vae=(vae_a, 0))
    video = g.add("CreateVideo", images=(dec_v, 0), audio=(dec_a, 0),
                  fps=float(e.get("frames", {}).get("fps", 24)))
    g.add("SaveVideo", video=(video, 0), filename_prefix=filename_prefix,
          format="mp4", codec="h264")
    return g.to_api()


BUILDERS = {"h3": build_h3, "h3ref": build_h3_ref2va, "wan22": build_wan22}


def build(engine: str, **kw) -> dict:
    if engine not in BUILDERS:
        raise ValueError(f"未知引擎 {engine}，可用: {list(BUILDERS)}")
    return BUILDERS[engine](**kw)


def validate(wf: dict) -> list[str]:
    """静态自检，不连 ComfyUI 也能发现明显错误。"""
    problems: list[str] = []
    types = {n["class_type"] for n in wf.values()}
    ids = set(wf)

    for nid, node in wf.items():
        for k, v in node["inputs"].items():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                if v[0] not in ids:
                    problems.append(
                        f"节点 {nid}({node['class_type']}) 的 {k} 指向不存在的节点 {v[0]}")

    if "MiniMaxH3ImageToVideo" in types:
        if "BasicGuider" not in types:
            problems.append("H3 需要 BasicGuider（该节点无 negative 输出，不能用 KSampler）")
        if "VAEDecodeAudio" not in types:
            problems.append("H3 有音频，缺 VAEDecodeAudio 会丢声音")
        h3 = next(n for n in wf.values() if n["class_type"] == "MiniMaxH3ImageToVideo")
        nf = h3["inputs"].get("length")
        if isinstance(nf, int) and (nf - 5) % 17 != 0:
            problems.append(f"H3 帧数 {nf} 未对齐 17k+5")
    return problems


if __name__ == "__main__":
    import sys

    if "--verify" in sys.argv:
        from . import comfy
        if not comfy.alive():
            print("❌ ComfyUI 未运行")
            raise SystemExit(1)
        info = comfy.system_stats()
        print("✅ ComfyUI:", json.dumps(info.get("system", {}), ensure_ascii=False)[:200])
        import urllib.request
        with urllib.request.urlopen(f"{comfy.base_url()}/object_info", timeout=60) as r:
            obj = json.load(r)
        need = ["UNETLoader", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly",
                "MiniMaxH3ImageToVideo", "BasicGuider", "KSamplerSelect",
                "BasicScheduler", "RandomNoise", "SamplerCustomAdvanced",
                "VAEDecode", "VAEDecodeAudio", "CreateVideo", "SaveVideo",
                "Wan22ImageToVideoLatent", "KSampler", "CLIPTextEncode", "LoadImage"]
        missing = [n for n in need if n not in obj]
        for n in need:
            print(("  ✅ " if n in obj else "  ❌ ") + n)
        if missing:
            print(f"\n⚠️ 缺失 {len(missing)} 个节点：{missing}")
            raise SystemExit(2)
        print("\n✅ 全部节点就绪")
    else:
        wf = build_h3(prompt="test", resolution="544x960", duration=5)
        print(f"H3 节点数: {len(wf)}  自检: {validate(wf) or '通过'}")
        wf2 = build_wan22(prompt="test", resolution="544x960", duration=5)
        print(f"Wan22 节点数: {len(wf2)}  自检: {validate(wf2) or '通过'}")
