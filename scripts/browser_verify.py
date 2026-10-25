"""用真实 Chromium 点击验证前台与后台。

在开发机 uv/python 环境运行（需 playwright + chromium）：
    python3 scripts/browser_verify.py

会截图到 /tmp/video_verify/
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HOST = "http://YOUR_SERVER_IP:8080"
OUT = Path("/tmp/video_verify")
OUT.mkdir(parents=True, exist_ok=True)

results: list[tuple[bool, str, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label, detail))
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    return cond


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
                if m.type == "error" else None)

        # ---------- 前台 ----------
        print("\n[1] 前台首页")
        page.goto(HOST + "/", wait_until="networkidle", timeout=30000)
        check(page.title().startswith("Video"), "页面加载", page.title())
        page.screenshot(path=str(OUT / "01_index.png"))

        # 项目列表是否渲染
        page.wait_for_selector("#projectList", timeout=10000)
        time.sleep(1.5)
        cnt_js = page.evaluate("() => document.querySelectorAll('#projectList .prj').length")
        check(cnt_js > 0, "项目列表已渲染", f"{cnt_js} 个项目")

        # 搜索框
        has_search = page.locator("#prjSearch").count() > 0
        check(has_search, "搜索框存在")
        if has_search:
            page.fill("#prjSearch", "zzz_不存在的项目_zzz")
            time.sleep(0.6)
            n = page.evaluate("() => document.querySelectorAll('#projectList .prj').length")
            check(n == 0, "搜索能过滤", f"过滤后 {n} 项")
            page.fill("#prjSearch", "")
            time.sleep(0.6)

        # 归档切换按钮
        check(page.locator("#archBtn").count() > 0, "归档切换按钮存在")

        # 打开第一个项目
        print("\n[2] 打开项目")
        page.locator("#projectList .prj").first.click()
        time.sleep(2.0)
        check(page.locator(".steps").count() > 0, "五段步骤条显示")
        step_n = page.locator(".steps .step").count()
        check(step_n == 5, "步骤数为 5", str(step_n))
        page.screenshot(path=str(OUT / "02_project.png"))

        # 更多菜单
        print("\n[3] 项目菜单")
        page.locator("#projectList .prj .icon-btn").first.click()
        time.sleep(0.6)
        menu_n = page.locator(".ctx-menu > div").count()
        check(menu_n >= 5, "右键菜单项", f"{menu_n} 项")
        page.screenshot(path=str(OUT / "03_menu.png"))
        # 关掉菜单
        page.mouse.click(800, 700)
        time.sleep(0.4)
        check(page.locator(".ctx-menu").count() == 0, "点击外部可关闭菜单")

        # 批量选择
        print("\n[4] 批量选择")
        cb = page.locator("#projectList .prj input[type=checkbox]").first
        if cb.count() > 0:
            cb.check()
            time.sleep(0.6)
            vis = page.locator("#batchBar").is_visible()
            check(vis, "批量栏出现")
            page.screenshot(path=str(OUT / "04_batch.png"))
            # 清除
            btns = page.locator("#batchBar button")
            page.locator("#batchBar button", has_text="清除").click()
            time.sleep(0.5)
            check(not page.locator("#batchBar").is_visible(), "清除后批量栏隐藏")

        # ---------- 后台 ----------
        print("\n[5] 后台首页")
        page.goto(HOST + "/admin", wait_until="networkidle", timeout=30000)
        time.sleep(1.5)
        check(page.locator(".back-link").count() > 0, "「返回前台」按钮存在")
        back_href = page.locator(".back-link").get_attribute("href")
        check(back_href == "/", "返回按钮指向 /", str(back_href))
        page.screenshot(path=str(OUT / "05_admin.png"))

        nav_n = page.locator("nav .item").count()
        check(nav_n >= 10, "侧边栏项数", f"{nav_n} 项")

        # ComfyUI 对接页
        print("\n[6] ComfyUI 对接面板")
        page.locator("nav .item", has_text="ComfyUI 对接").click()
        time.sleep(2.5)
        check(page.locator("#cf_url").count() > 0, "地址输入框存在")
        url_val = page.input_value("#cf_url")
        check("comfyui" in url_val or "8188" in url_val, "地址已回填", url_val)
        # 在线状态
        body = page.inner_text("#main")
        check("在线" in body or "ComfyUI" in body, "显示实时状态")
        page.screenshot(path=str(OUT / "06_comfy.png"), full_page=True)

        # 测试连通
        page.locator("button", has_text="测连通").first.click()
        time.sleep(3.0)
        out = page.inner_text("#cf_out")
        check("✓" in out or "连通" in out, "连通测试返回成功", out[:60].replace("\n", " "))

        # 模型页
        print("\n[7] 模型管理")
        page.locator("nav .item", has_text="模型接入").click()
        time.sleep(2.0)
        rows = page.locator("#main table tbody tr").count()
        check(rows >= 8, "模型行数", f"{rows} 行")
        edit_n = page.locator("#main table button", has_text="✏️").count()
        check(edit_n >= 8, "每行有编辑按钮", f"{edit_n} 个")
        page.screenshot(path=str(OUT / "07_models.png"), full_page=True)

        # 打开编辑弹窗
        page.locator("#main table button", has_text="✏️").first.click()
        time.sleep(0.8)
        check(page.locator(".modal-mask .modal").count() > 0, "编辑弹窗打开")
        check(page.locator("#m_name").count() > 0, "弹窗字段存在")
        name_val = page.input_value("#m_name")
        check(bool(name_val), "名称已回填", name_val[:30])
        page.screenshot(path=str(OUT / "08_model_edit.png"))

        # 取消
        page.locator(".modal button", has_text="取消").click()
        time.sleep(0.5)
        check(page.locator(".modal-mask").count() == 0, "取消能关闭弹窗")

        # 设置项
        print("\n[8] 设置项")
        page.locator("nav .item", has_text="设置项").click()
        time.sleep(2.0)
        groups = page.locator("#stab .tab").count()
        check(groups >= 5, "设置分组数", f"{groups} 组")
        page.screenshot(path=str(OUT / "09_settings.png"), full_page=True)

        # 密钥页
        print("\n[9] API KEY")
        page.locator("nav .item", has_text="API KEY").click()
        time.sleep(1.8)
        txt = page.inner_text("#main")
        check("ICOMPIFY" in txt or "未设置" in txt, "密钥列表显示")

        # 回前台
        print("\n[10] 返回前台")
        page.locator(".back-link").click()
        time.sleep(2.0)
        check("短剧工厂" in page.title() or page.url.rstrip("/") == HOST,
              "成功返回前台", page.url)

        # ---------- 控制台错误 ----------
        print("\n[11] 控制台")
        real_errors = [e for e in errors if "favicon" not in e.lower()]
        check(len(real_errors) == 0, "无 JS 运行时错误",
              "; ".join(real_errors[:3]) if real_errors else "")

        browser.close()

    # ---------- 汇总 ----------
    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    print("\n" + "=" * 66)
    print(f" 浏览器验证: {passed}/{total} 通过")
    if passed != total:
        print(" 失败项:")
        for ok, label, detail in results:
            if not ok:
                print(f"   - {label}  {detail}")
    print(f" 截图目录: {OUT}")
    print("=" * 66)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
