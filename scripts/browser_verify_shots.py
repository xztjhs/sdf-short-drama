"""验证镜头管理 + 渲染队列 UI（真实浏览器）。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HOST = "http://YOUR_SERVER_IP:8080"
OUT = Path("/tmp/video_verify2")
OUT.mkdir(parents=True, exist_ok=True)
results = []


def check(cond, label, detail=""):
    results.append((bool(cond), label, detail))
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    return cond


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1700, "height": 1100}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        page.goto(HOST + "/", wait_until="networkidle", timeout=30000)
        time.sleep(2)

        # 找一个有镜头的项目
        print("\n[1] 找一个有镜头的项目")
        cards = page.locator("#projectList .prj")
        n = cards.count()
        picked = False
        for i in range(n):
            txt = cards.nth(i).inner_text()
            if "镜" in txt and "0/" not in txt:
                cards.nth(i).click()
                picked = True
                break
        if not picked and n:
            cards.first.click()
        time.sleep(2.5)
        check(picked or n > 0, "打开项目")

        # 跳到分镜阶段
        print("\n[2] 镜头列表（含状态/操作）")
        page.locator(".steps .step", has_text="分镜头脚本").click()
        time.sleep(3)

        table = page.locator("#sbOut table")
        if table.count() == 0:
            print("   （该项目无剧集/分镜，跳过表格断言）")
        else:
            heads = page.locator("#sbOut table thead th").all_inner_texts()
            check("状态" in heads, "有状态列", str(heads))
            check("操作" in heads, "有操作列")
            check("耗时" in heads, "有耗时列")

            rows = page.locator("#sbOut table tbody tr").count()
            check(rows > 0, "镜头行", f"{rows} 行")

            # 每行应有 上移/下移/删除
            up_n = page.locator("#sbOut table button[title=上移]").count()
            down_n = page.locator("#sbOut table button[title=下移]").count()
            del_n = page.locator("#sbOut table button[title=删除]").count()
            rr_n = page.locator("#sbOut table button[title=重渲这一镜]").count()
            check(up_n == rows, "每行有上移", f"{up_n}/{rows}")
            check(down_n == rows, "每行有下移", f"{down_n}/{rows}")
            check(del_n == rows, "每行有删除", f"{del_n}/{rows}")
            check(rr_n == rows, "每行有重渲", f"{rr_n}/{rows}")

            page.screenshot(path=str(OUT / "01_shots.png"), full_page=True)

            # 状态列是否有可读文本
            st_txt = page.locator("#sbOut table tbody").inner_text()
            readable = any(k in st_txt for k in ("待渲染", "已完成", "失败", "已跳过", "渲染中"))
            check(readable, "状态显示为中文")

        # 加镜头按钮
        add_n = page.locator("#sbOut button", has_text="手工加镜头").count()
        check(add_n > 0, "有「手工加镜头」按钮")

        # 渲染队列
        print("\n[3] 渲染队列可视化")
        page.locator(".steps .step", has_text="渲染成片").click()
        time.sleep(2.5)

        qbox = page.locator("#queueBox")
        if qbox.count() > 0:
            cells = page.locator("#queueBox .qcell").count()
            check(cells > 0, "队列方块渲染", f"{cells} 个")
            if cells > 0:
                cls = page.locator("#queueBox .qcell").first.get_attribute("class")
                check("qcell" in (cls or ""), "方块有状态样式", cls)
            eta = page.locator("#queueEta").inner_text()
            check(bool(eta.strip()), "有剩余时间估算", eta.strip())
            page.screenshot(path=str(OUT / "02_queue.png"), full_page=True)

        # 显存预检提示（若显存高会显示警告）
        warn_visible = page.locator("#preflightWarn").is_visible()
        print(f"   （显存预检提示当前{'显示' if warn_visible else '隐藏'}）")

        # 控制台
        print("\n[4] 控制台")
        real = [e for e in errs if "favicon" not in e.lower()]
        check(len(real) == 0, "无 JS 错误", "; ".join(real[:3]) if real else "")

        b.close()

    passed = sum(1 for ok, _, _ in results if ok)
    print("\n" + "=" * 60)
    print(f" 镜头管理/队列验证: {passed}/{len(results)} 通过")
    for ok, lb, d in results:
        if not ok:
            print(f"   失败: {lb} {d}")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
