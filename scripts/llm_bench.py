"""LLM 对比基准 —— 分镜任务真实场景压测。

对 4 个候选模型跑同一个中文分镜任务，比较：
  - 延迟、输出 token、reasoning token
  - JSON 合规率（能否直接解析）
  - shots 数量、engine 字段合规、台词逐字保真
  - 中文提示词质量（启发式：长度、是否含主体/动作/光线/镜头语言）
  - 稳定性（跑 2 轮取最差）

用法（api 容器内，有 /config 与 secrets）：
    docker exec sdf-api python /app/llm_bench.py
    docker exec sdf-api python /app/llm_bench.py --models deepseek-v4.1-flash,glm-5.3
输出：/logs/llm_bench.json + 控制台表格
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_here = Path(__file__).resolve()
for _cand in ("/app", str(_here.parent.parent)):
    if _cand not in sys.path:
        sys.path.insert(0, _cand)

from pipeline import script as llm  # noqa: E402

SCRIPT = """废土荒漠。红发女人赤鸦走进废弃加油站，三个拾荒者围上来。
头目：把水芯片交出来。
赤鸦：你们挡路了。
头目举刀冲过来。
赤鸦抬手一枪，刀飞出去插进沙里。
头目跪在地上，赤鸦低头看他：现在谁才是猎人？"""

MODELS = [
    ("deepseek-v4.1-flash", "DeepSeek 4.1 Flash", 0.5, 16384),
    ("deepseek-v4-pro", "DeepSeek 4 Pro", 0.5, 32768),
    ("glm-5.3", "GLM 5.3", 0.5, 16384),
    ("glm-5.3-flash", "GLM 5.3 Flash", 0.5, 16384),
]

# 中文提示词质量启发式要素
QUALITY_MARKERS = ["镜头", "光", "背影", "特写", "中景", "远景", "近景", "全景",
                   "缓慢", "固定", "推进", "横移", "逆光", "侧光", "浅景深"]


def _zh_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return round(cjk / max(1, len(text)), 3)


def run_one(model: str, temp: float, max_tokens: int) -> dict:
    mode = "auto"
    system, user = llm.prompt(
        llm._tpl("storyboard"),
        script=SCRIPT, style="废土荒漠，电影质感，强烈日光与锈色",
        max_shots=8, target_duration=45, locked_info="（未锁定）",
        engine_mode=mode, engine_mode_zh=llm.ENGINE_MODE_ZH[mode],
        engine_rules=llm.cfg.get("prompts", f"prompts.storyboard_rules.{mode}", ""))
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": user}]

    t0 = time.time()
    raw, usage, err = "", {}, None
    try:
        import urllib.request
        payload = {
            "model": model, "messages": msgs,
            "temperature": temp, "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{llm.base_url().rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {llm.api_key()}"},
            method="POST")
        timeout = int(os.environ.get("BENCH_TIMEOUT", "600"))
        if model.endswith("-pro"):
            timeout = int(os.environ.get("BENCH_TIMEOUT_PRO", "900"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        raw = msg.get("content") or ""
        usage = d.get("usage") or {}
        if not raw:
            err = "content 为空（可能只有 reasoning）"
            raw = msg.get("reasoning") or ""
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
    latency = round(time.time() - t0, 1)

    out = {"model": model, "latency_s": latency, "error": err,
           "usage": usage, "raw_len": len(raw or "")}
    data = None
    try:
        data = llm.extract_json(raw)
        out["json_ok"] = isinstance(data, dict) and bool(data.get("shots"))
    except Exception as e:
        out["json_ok"] = False
        out["json_err"] = str(e)[:120]
    if not out["json_ok"]:
        return out

    shots = data.get("shots") or []
    out["shots"] = len(shots)
    out["engine_field"] = sum(1 for s in shots if s.get("engine") in ("h3", "wan22"))
    out["engine_ok"] = out["engine_field"] == len(shots)
    out["h3"] = sum(1 for s in shots if s.get("engine") == "h3")
    out["wan22"] = sum(1 for s in shots if s.get("engine") == "wan22")
    prompts = [(s.get("prompt") or "") for s in shots]
    out["avg_prompt_len"] = round(sum(len(p) for p in prompts) / max(1, len(prompts)), 1)
    out["avg_zh_ratio"] = round(sum(_zh_ratio(p) for p in prompts) / max(1, len(prompts)), 3)
    out["quality_hits"] = sum(1 for p in prompts
                              if sum(1 for m in QUALITY_MARKERS if m in p) >= 2)
    # 台词保真：剧本里的关键台词是否原样出现
    must = ["把水芯片交出来", "你们挡路了", "现在谁才是猎人"]
    out["dialogue_kept"] = sum(1 for m in must
                               if any(m in (s.get("dialogue") or "") for s in shots))
    out["has_dialogue"] = sum(1 for s in shots if (s.get("dialogue") or "").strip())
    out["chars"] = len(data.get("characters") or [])
    out["appearance_zh"] = round(sum(
        _zh_ratio((c.get("appearance") or ""))
        for c in (data.get("characters") or [])) / max(1, len(data.get("characters") or [])), 3)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--out", default="/logs/llm_bench.json")
    a = ap.parse_args()
    wanted = [m.strip() for m in a.models.split(",") if m.strip()]
    models = [m for m in MODELS if not wanted or m[0] in wanted]

    results = []
    for mid, name, temp, mt in models:
        for rnd in range(a.rounds):
            print(f"→ {name} ({mid}) 第 {rnd+1} 轮 …", flush=True)
            r = run_one(mid, temp, mt)
            r["name"] = name
            r["round"] = rnd + 1
            results.append(r)
            print("   " + json.dumps({k: v for k, v in r.items()
                                      if k not in ("raw",)}, ensure_ascii=False),
                  flush=True)

    # 汇总（取每模型最好一轮，但记录最差延迟）
    summary = {}
    for r in results:
        m = r["model"]
        s = summary.setdefault(m, {"name": r["name"], "runs": 0, "ok": 0,
                                   "latency": [], "shots": [], "engine_ok": 0,
                                   "dialogue_kept": [], "quality": [],
                                   "avg_prompt_len": [], "zh_ratio": [],
                                   "tokens_out": [], "reasoning": []})
        s["runs"] += 1
        s["ok"] += 1 if r.get("json_ok") else 0
        s["latency"].append(r["latency_s"])
        if r.get("json_ok"):
            s["shots"].append(r["shots"])
            s["engine_ok"] += 1 if r.get("engine_ok") else 0
            s["dialogue_kept"].append(r["dialogue_kept"])
            s["quality"].append(r["quality_hits"])
            s["avg_prompt_len"].append(r["avg_prompt_len"])
            s["zh_ratio"].append(r["avg_zh_ratio"])
        u = r.get("usage") or {}
        if u.get("completion_tokens"):
            s["tokens_out"].append(u["completion_tokens"])
        rt = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        if rt:
            s["reasoning"].append(rt)

    def avg(x):
        return round(sum(x) / len(x), 2) if x else None

    table = []
    for m, s in summary.items():
        table.append({
            "model": m, "name": s["name"],
            "json_ok": f"{s['ok']}/{s['runs']}",
            "engine_ok": f"{s['engine_ok']}/{max(1,s['ok'])}",
            "latency_s": f"{avg(s['latency'])}（最慢 {max(s['latency']) if s['latency'] else '-'}）",
            "shots": avg(s["shots"]),
            "dialogue_kept": f"{avg(s['dialogue_kept'])}/3",
            "quality_hits": avg(s["quality"]),
            "prompt_len": avg(s["avg_prompt_len"]),
            "zh_ratio": avg(s["zh_ratio"]),
            "out_tokens": avg(s["tokens_out"]),
            "reasoning_tokens": avg(s["reasoning"]),
        })

    print("\n===== 对比汇总 =====")
    for t in table:
        print(json.dumps(t, ensure_ascii=False))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"summary": table, "raw": results},
                                      ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\n明细写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
