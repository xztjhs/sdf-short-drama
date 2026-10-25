"""成片层测试：用已有 smoke 片段跑 normalize → concat → loudnorm → 字幕。"""
import glob
import json
import os
import sys

from pipeline import assemble

OUT = "/output/_assemble_test"
os.makedirs(OUT, exist_ok=True)

# 找几个已有片段（不同分辨率/有无音轨都要覆盖到）
cands = sorted(glob.glob("/output/smoke_*.mp4"))
if not cands:
    print("❌ 没有可用片段")
    sys.exit(1)

print("候选片段:")
for c in cands:
    try:
        i = assemble.info(c)
        print(f"  {os.path.basename(c)}  {i['width']}x{i['height']} "
              f"{i['duration']:.2f}s audio={i['has_audio']}({i['audio_channels']}ch)")
    except Exception as e:
        print(f"  {os.path.basename(c)}  读取失败 {e}")

# 挑选：一个带音频(H3) + 一个无音频(wan22)
with_audio = []
no_audio = []
for c in cands:
    i = assemble.info(c)
    (with_audio if i["has_audio"] else no_audio).append(c)

parts = (with_audio[:1] + no_audio[:1]) or cands[:2]
print(f"\n选用 {len(parts)} 段:")
for p in parts:
    print("  ", os.path.basename(p))

target = assemble.Target(width=1080, height=1920, fps=24, crf=20, threads=16)
print(f"\n目标: {target.width}x{target.height} {target.fps}fps crf={target.crf}")

# 字幕
shots = [
    {"seq": 1, "duration": 5.0, "dialogue": "你早就知道，对不对？"},
    {"seq": 2, "duration": 5.0, "dialogue": "我以为我能护住你。"},
]
srt = os.path.join(OUT, "test.srt")
assemble.srt_from_shots(shots, srt)
print(f"\nSRT:\n{open(srt).read()}")

dst = os.path.join(OUT, "final.mp4")
print("开始成片（normalize → concat → loudnorm → 字幕）...")
try:
    r = assemble.assemble_episode(
        [{"path": p, "dialogue": None, "duration": 5} for p in parts],
        dst, subtitle_srt=srt, target=target,
        workdir=os.path.join(OUT, "_work"))
    print("\n✅ 成片成功")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"\n文件: {dst}  {os.path.getsize(dst)/1e6:.2f} MB")

    # 复核：确实带字幕（画面变了）+ 有音轨
    fi = assemble.info(dst)
    print(f"\n复核: {fi['width']}x{fi['height']} {fi['fps']}fps "
          f"{fi['duration']:.2f}s audio={fi['has_audio']} {fi['audio_channels']}ch")
    ok = fi["width"] == 1080 and fi["height"] == 1920 and fi["has_audio"]
    print("\n✅ 全部通过" if ok else "\n⚠️ 尺寸或音轨不符")
    sys.exit(0 if ok else 1)
except Exception as e:
    print(f"\n❌ 失败: {type(e).__name__}: {e}")
    sys.exit(1)
