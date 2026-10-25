"""v0.4 浏览器回归：分镜模式 / 渲染阶段 / 队列 / 角色库 / 文件预览下载。

运行（开发机）：
    uv run --with playwright python scripts/browser_verify_v04.py
或已有 playwright 环境：
    python3 scripts/browser_verify_v04.py
"""
from __future__ import annotations

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HOST = "http://YOUR_SERVER_IP:8080"
OUT = Path("/tmp/sdf_verify_v04")
OUT.mkdir(parents=True, exist_ok=True)

results: list[tuple[bool, str, str]] = []


def check(cond, label, detail=""):
    results.append((bool(cond), label, detail))
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    return cond


def main() -> int:
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--no-sandbox"])
        pg = b.new_page(viewport={"width": 1500, "height": 950})
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        print("== 前台 ==")
        pg.goto(HOST, wait_until="networkidle", timeout=30000)
        check("SDF" in pg.title(), "首页可打开", pg.title())

        # 顶栏新增按钮
        check(pg.locator('button[title*="全局角色库"]').count() == 1,
              "顶栏有角色库入口")
        check(pg.locator('button[title*="渲染队列"]').count() == 1,
              "顶栏有队列入口")

        # 打开「西部荒漠」（按标题精确定位，避免点到新建的验证项目）
        pg.wait_for_timeout(800)
        prj = pg.locator('aside').get_by_text("西部荒漠", exact=True).first
        if not prj.count():
            prj = pg.locator('aside').get_by_text("西部荒漠", exact=False).first
        if prj.count():
            prj.click()
            pg.wait_for_timeout(2000)
        # 等到进入项目（出现步骤条）
        try:
            pg.wait_for_selector("#main .card", timeout=8000)
        except Exception:
            pass
        body0 = pg.inner_text("main")
        check("西部荒漠" in pg.inner_text("aside") + body0, "已选中「西部荒漠」")
        pg.screenshot(path=str(OUT / "01_project.png"))

        # 进第 4 步（分镜头脚本）
        # ⚠️ 必须限定在步骤条里点：侧边栏项目卡片也会显示"分镜头脚本"状态，
        #    .first 会命中它 → 切到别的项目（2026-10-22 实测踩到）
        pg.locator("#main .steps").get_by_text("分镜头脚本", exact=False).first.click()
        pg.wait_for_timeout(2000)
        body4 = pg.inner_text("main")
        check("MiniMax H3（唯一引擎）" in body4, "④ 显示 H3 单引擎")
        check("Wan2.2" not in body4 and "5B" not in body4, "④ 无 wan2.2 残留")
        check('button:has-text("本集转中文")' in pg.content() or
              pg.locator('button:has-text("本集转中文")').count()==1,
              "④ 有「本集转中文」")
        check(pg.locator('button:has-text("全剧批量分镜")').count() == 1,
              "④ 有「全剧批量分镜」")
        check(pg.locator('button:has-text("加入渲染队列")').count() >= 1,
              "④ 有「加入渲染队列」")
        # v0.6 A+B
        body4b = pg.inner_text("main")
        check("场次卡" in body4b or "场" in body4b, "④ 有场次列/场次卡")
        check(pg.locator('button:has-text("定妆照")').count() >= 1,
              "④ 有定妆照按钮")
        pg.screenshot(path=str(OUT / "02_storyboard.png"))

        # 进第 5 步（渲染成片）
        pg.locator("#main .steps").get_by_text("渲染成片", exact=False).first.click()
        pg.wait_for_timeout(2500)
        body = pg.inner_text("body")
        check("拼接" in body and "响度" in body and "超分" in body,
              "⑤ 展示渲染→拼接→响度/字幕→超分 链路")
        check(pg.locator("#upSel").count() == 1, "⑤ 有「成片后超分」选择")
        check(pg.locator("#qSel").count() == 1, "⑤ 有 H3 质量档选择")
        check(pg.locator("#modeSel").count() == 0, "⑤ 已无旧「渲染模式」下拉")
        body5 = pg.inner_text("main")
        check("720×1280" in body5, "⑤ 画布显示 720×1280", body5[:0])
        check("H3" in body5 and "5B" not in body5, "⑤ 无 wan2.2 残留")
        check(pg.locator('button:has-text("预览")').count() > 0, "⑤ 产物有预览按钮")
        check("成片与超分产物" in body, "⑤ 有「成片与超分产物」区块")
        check(pg.locator("text=E01_final").count() >= 1
              or pg.locator("video").count() >= 1,
              "⑤ 有成片产物/预览")
        check(pg.locator('button:has-text("全部集入队")').count() >= 1,
              "⑤ 有「全部集入队」")
        pg.screenshot(path=str(OUT / "03_render.png"), full_page=True)

        # 全局队列弹窗
        pg.locator('button[title*="渲染队列"]').click()
        pg.wait_for_timeout(1500)
        check("渲染队列" in pg.inner_text(".modal"), "队列弹窗可打开")
        check(pg.locator("text=单卡串行").count() >= 1, "队列说明单卡串行")
        pg.screenshot(path=str(OUT / "04_queue.png"))
        pg.locator('.modal button:has-text("关闭")').last.click()
        pg.wait_for_timeout(500)

        # 角色库弹窗
        pg.locator('button[title*="全局角色库"]').click()
        pg.wait_for_timeout(1200)
        mtext = pg.inner_text(".modal")
        check("全局角色库" in mtext, "角色库独立管理界面可打开")
        check("新增角色" in mtext, "角色库可新增")
        pg.screenshot(path=str(OUT / "05_charlib.png"))
        pg.keyboard.press("Escape")
        pg.locator('.modal button:has-text("关闭")').last.click()
        pg.wait_for_timeout(400)

        print("== 后台 ==")
        pg.goto(HOST + "/admin", wait_until="networkidle", timeout=30000)
        pg.wait_for_timeout(1000)
        pg.get_by_text("文件管理", exact=False).first.click()
        pg.wait_for_timeout(2000)
        body = pg.inner_text("main")
        check("预览" in body and "下载" in body, "后台文件管理有预览/下载")
        check(pg.locator('a:has-text("下载")').count() > 0, "下载是真实链接")
        pg.screenshot(path=str(OUT / "06_files.png"), full_page=True)

        # 点第一个预
        prev = pg.locator('button:has-text("预览")').first
        if prev.count():
            prev.click()
            pg.wait_for_timeout(2500)
            mt = pg.inner_text(".modal") if pg.locator(".modal").count() else ""
            check("下载" in mt, "预览弹窗可打开（含下载）")
            pg.screenshot(path=str(OUT / "07_preview.png"))
            pg.locator('.modal button:has-text("关闭")').last.click()

        # 任务页看队列
        pg.get_by_text("任务日志", exact=False).first.click()
        pg.wait_for_timeout(2000)
        mt = pg.inner_text("main")
        check("渲染队列" in mt, "后台任务页有队列监控")
        pg.screenshot(path=str(OUT / "08_tasks.png"), full_page=True)

        real_errs = [e for e in errs if "favicon" not in e.lower()]
        check(not real_errs, "无 JS 报错", " ; ".join(real_errs[:2])[:200])

        b.close()

    ok = sum(1 for c, _, _ in results if c)
    print(f"\n结果：{ok}/{len(results)} 通过")
    for c, label, d in results:
        if not c:
            print(f"  ✗ {label} {d}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
