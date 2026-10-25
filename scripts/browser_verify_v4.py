"""v0.4 新功能验证：监控栏/主题/翻译/角色库/预设/目录命名。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HOST = "http://YOUR_SERVER_IP:8080"
OUT = Path("/tmp/video_verify4")
OUT.mkdir(parents=True, exist_ok=True)
res = []


def check(cond, label, detail=""):
    res.append((bool(cond), label, detail))
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    return cond


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1700, "height": 1100})
        page = ctx.new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        print("\n[1] 监控栏")
        page.goto(HOST + "/", wait_until="networkidle", timeout=30000)
        time.sleep(2.5)
        bar = page.inner_text("#sysbar")
        check(len(bar.strip()) > 0, "监控栏有内容", bar.replace("\n", " ")[:70])

        # 打开设置
        page.locator("button[title='监控栏设置']").click()
        time.sleep(0.8)
        check(page.locator(".modal-mask").count() > 0, "监控设置弹窗打开")
        n_items = page.locator(".monchk input").count()
        check(n_items >= 10, "监控可选项数量", f"{n_items} 个")
        page.screenshot(path=str(OUT / "01_monitor.png"))

        # 勾上 CPU + swap + 网络
        for kv in ("cpu", "swap", "net", "disk_io"):
            cb = page.locator(f".monchk input[value='{kv}']")
            if cb.count() > 0 and not cb.is_checked():
                cb.check()
        page.fill("#monR", "10")
        page.locator(".modal button", has_text="保存").click()
        time.sleep(2.5)
        bar2 = page.inner_text("#sysbar")
        check("CPU" in bar2, "CPU 已显示", bar2.replace("\n", " ")[:80])
        page.screenshot(path=str(OUT / "02_monitor_cpu.png"))

        print("\n[2] 主题切换")
        # 先强制回暗色，保证起点确定
        page.evaluate("() => { localStorage.setItem('video.theme','dark'); }")
        page.reload(wait_until="networkidle")
        time.sleep(1.5)
        dark_bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        page.locator("#themeBtn").click()
        time.sleep(1.2)
        html_after = page.get_attribute("html", "data-theme")
        light_bg = page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        check(html_after == "light", "切换后为浅色", html_after)
        check(dark_bg != light_bg, f"背景色已变 {dark_bg} → {light_bg}")
        page.screenshot(path=str(OUT / "03_light_theme.png"))
        page.locator("#themeBtn").click()   # 切回暗色
        time.sleep(0.8)

        print("\n[3] 输入故事页：英文名 + 风格预设 + 集数")
        cards = page.locator("#projectList .prj")
        if cards.count():
            cards.first.click()
            time.sleep(2.5)
            page.locator(".steps .step", has_text="输入故事").click()
            time.sleep(2)
            check(page.locator("#slugBox").count() > 0, "英文名输入框存在")
            check(page.locator("#stylePreset").count() > 0, "风格预设下拉存在")
            opts = page.locator("#stylePreset option").count()
            check(opts >= 11, "风格选项数", f"{opts}（含1个自定义）")
            ep_input = page.locator("#epCount")
            maxattr = ep_input.get_attribute("max")
            check(maxattr is None, "集数无上限属性", f"max={maxattr}")
            page.screenshot(path=str(OUT / "04_story.png"), full_page=True)

        print("\n[4] 后台：预设页 + 中文字段")
        page.goto(HOST + "/admin", wait_until="networkidle", timeout=30000)
        time.sleep(2)
        nav = page.locator("nav .item", has_text="风格与平台预设")
        check(nav.count() > 0, "预设页入口存在")
        nav.click()
        time.sleep(2)
        txt = page.inner_text("#main")
        check("都市写实" in txt or "风格预设" in txt, "预设页内容渲染")
        page.screenshot(path=str(OUT / "05_presets.png"), full_page=True)

        # 设置项中文化
        page.locator("nav .item", has_text="设置项").click()
        time.sleep(2)
        stxt = page.inner_text("#main")
        ok_zh = any(k in stxt for k in ("应用信息","渲染","路径","名称","版本","成片规格"))
        check(ok_zh, "设置项有中文标签", stxt[:50].replace("\n", " "))
        # 切到 assemble 组看字段中文
        asm = page.locator("#stab .tab", has_text="成片规格")
        if asm.count():
            asm.click(); time.sleep(1)
            t2 = page.inner_text("#sform")
            check("宽度" in t2 and "帧率" in t2, "组内字段有中文", t2[:40].replace("\n"," "))

        print("\n[5] 控制台")
        real = [e for e in errs if "favicon" not in e.lower()]
        check(len(real) == 0, "无 JS 错误", "; ".join(real[:2]) if real else "")

        b.close()

    passed = sum(1 for ok, _, _ in res if ok)
    print("\n" + "=" * 60)
    print(f" v0.4 验证: {passed}/{len(res)} 通过")
    for ok, lb, d in res:
        if not ok:
            print(f"   失败: {lb} {d}")
    print("=" * 60)
    return 0 if passed == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
