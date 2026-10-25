"""SDF 后台优化验证：菜单顺序 / LLM 配置 / 模型下载 / 密码迁移 / 改名。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HOST = "http://YOUR_SERVER_IP:8080"
OUT = Path("/tmp/sdf_verify")
OUT.mkdir(parents=True, exist_ok=True)
res = []


def check(cond, label, detail=""):
    res.append((bool(cond), label, detail))
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {detail}" if detail else ""))
    return cond


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        page = b.new_context(viewport={"width": 1700, "height": 1100}).new_page()
        errs = []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

        print("\n[1] 品牌改名")
        page.goto(HOST + "/", wait_until="networkidle", timeout=30000)
        time.sleep(2)
        title = page.title()
        check("SDF" in title or "短剧工厂" in title, "前台标题", title)
        body = page.inner_text("header h1")
        check("短剧工厂" in body, "页头品牌", body.replace("\n", " "))

        print("\n[2] 后台菜单顺序")
        page.goto(HOST + "/admin", wait_until="networkidle", timeout=30000)
        time.sleep(2)
        nav = [x.inner_text().strip() for x in page.locator("nav .item").all()]
        check(len(nav) >= 12, "菜单项数", f"{len(nav)}")
        # 业务顺序：总览 → ComfyUI → 模型 → LLM/提示词
        order_ok = (nav[0].endswith("总览") and "ComfyUI" in nav[1]
                    and "模型" in nav[2] and "LLM" in nav[3])
        check(order_ok, "按业务流排序", " > ".join(nav[:4]))
        page.screenshot(path=str(OUT / "01_nav.png"))

        print("\n[3] LLM 与提示词页")
        page.locator("nav .item", has_text="LLM 与提示词").click()
        time.sleep(2.5)
        check(page.locator("#llm_url").count() > 0, "base_url 输入框")
        check(page.locator("#llm_key").count() > 0, "API KEY 输入框")
        maxtok = page.input_value("#llm_maxtok")
        ctx = page.input_value("#llm_ctx")
        check(maxtok == "65536", "max_tokens 默认 64K", maxtok)
        # context_length 是用户可改项：默认 256K，但界面显示的是当前生效值
        check(ctx.isdigit() and int(ctx) >= 65536,
              "context_length 已设置（默认 256K）",
              f"{int(ctx)//1024}K")
        check(page.locator("button", has_text="获取模型列表").count() > 0, "有获取模型按钮")
        check(page.locator("button", has_text="测试连通").count() > 0, "有连通测试按钮")
        page.screenshot(path=str(OUT / "02_llm.png"), full_page=True)

        # 实际拉模型列表
        page.locator("button", has_text="获取模型列表").click()
        time.sleep(4)
        msg = page.inner_text("#llmMsg")
        check("取到" in msg or "✓" in msg, "模型列表拉取", msg.strip()[:50])
        # 下拉是否有选项
        opts = page.locator("#llm_def option").count()
        check(opts > 1, "主模型下拉有选项", f"{opts} 个")
        page.screenshot(path=str(OUT / "03_llm_models.png"), full_page=True)

        # 连通测试
        page.locator("button", has_text="测试连通").click()
        time.sleep(5)
        msg2 = page.inner_text("#llmMsg")
        check("连通" in msg2 or "✓" in msg2, "连通测试通过", msg2.strip()[:50])

        print("\n[4] 模型接入：下载功能")
        page.locator("nav .item", has_text="模型接入").click()
        time.sleep(2.5)
        check(page.locator("button", has_text="下载模型").count() > 0, "有下载模型按钮")
        page.locator("button", has_text="下载模型").click()
        time.sleep(1)
        check(page.locator("#dl_src").count() > 0, "下载弹窗打开")
        src_opts = page.locator("#dl_src option").count()
        check(src_opts >= 4, "下载源种类", f"{src_opts} 个")
        src_txt = page.inner_text("#dl_src")
        check("ModelScope" in src_txt and "Hugging Face" in src_txt, "含 ModelScope/HF")
        page.screenshot(path=str(OUT / "04_download.png"))
        page.locator(".modal button", has_text="关闭").click()
        time.sleep(0.6)

        print("\n[5] API KEY 页：ADMIN_PASSWORD 已移除")
        page.locator("nav .item", has_text="API KEY").click()
        time.sleep(2)
        stxt = page.inner_text("#main")
        check("ADMIN_PASSWORD" not in stxt, "API KEY 页无 ADMIN_PASSWORD")
        check("设置项 → 后台安全" in stxt or "后台密码" in stxt, "有迁移提示")

        print("\n[6] 设置项：无 ComfyUI 分组 + 有后台安全")
        page.locator("nav .item", has_text="设置项").click()
        time.sleep(2.5)
        tabs = [x.inner_text().strip() for x in page.locator("#stab .tab").all()]
        check(all("ComfyUI" not in t for t in tabs), "设置项无 ComfyUI 分组", str(tabs))
        check(any("后台安全" in t for t in tabs), "有后台安全分组", str(tabs))
        # 点开后台安全
        adm = page.locator("#stab .tab", has_text="后台安全")
        if adm.count():
            adm.click(); time.sleep(1)
            t2 = page.inner_text("#sform")
            check("后台密码" in t2, "后台密码字段存在", t2[:40].replace("\n", " "))
        page.screenshot(path=str(OUT / "05_settings.png"), full_page=True)

        print("\n[7] 控制台")
        real = [e for e in errs if "favicon" not in e.lower()]
        check(len(real) == 0, "无 JS 错误", "; ".join(real[:2]) if real else "")

        b.close()

    passed = sum(1 for ok, _, _ in res if ok)
    print("\n" + "=" * 62)
    print(f" SDF 后台验证: {passed}/{len(res)} 通过")
    for ok, lb, d in res:
        if not ok:
            print(f"   失败: {lb} {d}")
    print("=" * 62)
    return 0 if passed == len(res) else 1


if __name__ == "__main__":
    sys.exit(main())
