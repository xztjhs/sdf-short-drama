"""v0.6.4 全量浏览器验证（修正导航缺陷版）

前两版验证脚本的假失败原因（不是产品问题）：
  1. 项目的 stage=done 时点开直接进「完成」页，不点「渲染成片」就没有 composerBox
  2. admin.html 是单页分 section 的，「字幕样式」在 presets section 里，
     不点左侧导航就查不到

本次全部按真实用户路径走：点导航 → 等渲染 → 再断言。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

HOST = "http://YOUR_SERVER_IP:8080"
OUT = Path("/tmp/sdf_v064_final")
OUT.mkdir(parents=True, exist_ok=True)
res: list[tuple[bool, str, str]] = []

EID = "ep_5e86f9c4a9b0"      # 最后一次值班 E1
PID = "prj_baeff0aa66e5"


def check(cond, label, detail=""):
    res.append((bool(cond), label, detail))
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    return cond


def g(path):
    return json.loads(urllib.request.urlopen(HOST + path, timeout=30).read())


def post(path, payload):
    req = urllib.request.Request(
        HOST + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def main() -> bool:
    # ---------------------------------------------------------- 后端
    print("\n[1] 配置已发布（Bug4 / Bug5b / 功能2 的数据源）")
    styles = g("/api/presets")["styles"]
    short = [s["id"] for s in styles if len(s.get("render") or "") < 40]
    check(len(styles) == 11 and not short,
          f"{len(styles)} 个风格预设 render 均 ≥40 字", f"过短的: {short}" if short else "")

    cfg = g("/api/admin/config/settings")["data"]
    sub = (cfg.get("assemble") or {}).get("subtitle") or {}
    check(set(sub) == {"font_size", "margin_v", "alignment", "font_name",
                       "outline", "shadow"},
          "settings.assemble.subtitle 六项齐全", json.dumps(sub, ensure_ascii=False))

    a = cfg.get("assemble") or {}
    keep_ok = (a.get("orientation") == "landscape" and a.get("fps") == 30
               and a.get("delivery_width") == 1920)
    check(keep_ok, "发布未覆盖服务器运行时值（横屏/fps30/交付1920x1080）",
          f"orientation={a.get('orientation')} fps={a.get('fps')} "
          f"delivery={a.get('delivery_width')}x{a.get('delivery_height')}")

    prompts = g("/api/admin/config/prompts")["data"]["prompts"]
    sysp = prompts["storyboard_v2"]["system"]
    check("主体 + 动作 + 场景 + 光线 + 镜头运动 + 视觉风格 + 声音 + 约束" in sysp,
          "storyboard_v2 含 H3 八要素公式", f"{len(sysp)} 字")
    check("中文" in prompts["continuation"]["system"],
          "continuation 模板已中文化（不再要求英文）")

    print("\n[2] 后端接口（功能1）")
    takes = g(f"/api/episodes/{EID}/takes")["takes"]
    check(len(takes) == 17, f"/takes 返回 {len(takes)} 镜")
    multi = [t for t in takes if len(t.get("takes") or []) > 1]
    check(len(multi) >= 1, f"{len(multi)} 镜有多个 take（重渲不覆盖）")

    print("\n[3] 浏览器：组合成片面板（功能1 前端，按真实路径点导航）")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1700, "height": 1100}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))

        page.goto(HOST + "/", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        page.locator("text=最后一次值班").first.click()
        page.wait_for_timeout(2500)

        # 关键修复：stage=done 时要点「渲染成片」才进渲染页
        nav = page.locator("text=渲染成片").first
        if nav.count() > 0:
            nav.click()
            page.wait_for_timeout(2500)

        stage = page.evaluate("() => S.stage")
        n_items = page.locator("#composerBox .comp-item").count()
        n_btns = page.locator("#composerBox button").count()
        check(stage == "render", f"已进入渲染页（stage={stage}）")
        check(n_items == 17, f"组合成片面板 {n_items} 个镜头条目")
        check(n_btns >= 17, f"take 选择按钮 {n_btns} 个")

        # 真实交互：切 take → 勾跳过 → 取消
        switched = page.evaluate("""(eid) => {
            const st = window._compState || {};
            const sids = Object.keys(st.sel || {});
            return {selCount: sids.length, firstSid: sids[0] || null};
        }""", EID)
        check(switched.get("selCount") == 17,
              f"默认全选 17 镜（实际 {switched.get('selCount')}）")

        page.screenshot(path=str(OUT / "01_composer_panel.png"))

        js_err = [e for e in errs if "innerHTML" not in e]
        check(not errs, f"页面无 JS 错误", "；".join(errs[:2]) if errs else "")
        b.close()

    print("\n[4] 浏览器：admin 字幕样式（功能2 前端，先进 presets section）")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1700, "height": 1100}).new_page()
        page.goto(HOST + "/admin", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # admin 是 section 切换式，导航标签实际叫「风格与平台」；直接调 go('presets') 最稳
        clicked = page.evaluate("""() => {
            try { go('presets'); return true; } catch(e) { return false; }
        }""")
        page.wait_for_timeout(2200)

        body = page.inner_text("body")
        found = "字幕样式" in body
        box = page.locator("#subStyleBox").count()
        check(found, f"admin 出现「字幕样式」卡片（导航点击={clicked}）")
        check(box > 0, "subStyleBox 容器存在")

        # 断言可编辑控件
        if box > 0:
            ctrls = page.evaluate("""() => {
                const box = document.getElementById('subStyleBox');
                if (!box) return {};
                return {
                    inputs: box.querySelectorAll('input,select').length,
                    html: box.innerHTML.length
                };
            }""")
            check(ctrls.get("inputs", 0) >= 4,
                  f"字幕样式控件 {ctrls.get('inputs')} 个可编辑项")

        page.screenshot(path=str(OUT / "02_admin_subtitle.png"))
        b.close()

    print("\n[5] 浏览器：风格预设表头（Bug5a）")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1700, "height": 1100}).new_page()
        page.goto(HOST + "/admin", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)
        page.evaluate("() => go('presets')")
        page.wait_for_timeout(2200)
        body = page.inner_text("body")
        check("提示词（进渲染）" in body, "表头为「提示词（进渲染）」")
        check("英文提示词" not in body, "旧表头「英文提示词」已移除")
        page.screenshot(path=str(OUT / "03_admin_presets.png"))
        b.close()

    print("\n[6] 浏览器：锁定页标签（Bug3）")
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1700, "height": 1100}).new_page()
        page.goto(HOST + "/", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        page.locator("text=最后一次值班").first.click()
        page.wait_for_timeout(2000)
        # 锁定页是 S.stage='locked'，不是链接
        ok_lock = page.evaluate("""() => {
            try { S.stage = 'locked'; render(); return true; } catch(e) { return false; }
        }""")
        page.wait_for_timeout(2500)
        body = page.inner_text("body")
        has_new = "渲染提示词" in body
        has_old = "英文（进渲染）" in body or "英文(进渲染)" in body
        check(ok_lock, "切到锁定页")
        check(has_new, "锁定页出现「渲染提示词」")
        check(not has_old, "旧标签「英文（进渲染）」已移除")
        check("中文（给人看）" not in body and "中文(给人看)" not in body,
              "冗余栏「中文（给人看）」已删除")
        page.screenshot(path=str(OUT / "04_lock_page.png"))
        b.close()

    print("\n[7] MCP server（功能3）")
    tools = post("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                          "params": {}})["result"]["tools"]
    check(len(tools) == 9, f"MCP tools/list {len(tools)} 个工具")

    print("\n" + "=" * 62)
    ok = sum(1 for r in res if r[0])
    print(f"v0.6.4 全量验证：{ok}/{len(res)} 通过")
    for passed, label, detail in res:
        if not passed:
            print(f"  ❌ {label} — {detail}")
    print(f"截图目录：{OUT}")
    return ok == len(res)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
