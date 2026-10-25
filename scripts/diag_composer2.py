"""诊断组合成片面板：打开项目 → 渲染页 → 逐项检查。"""
import json
from playwright.sync_api import sync_playwright

HOST = "http://YOUR_SERVER_IP:8080"

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    page = b.new_context(viewport={"width": 1700, "height": 1100}).new_page()

    # 收集所有 JS 错误（带堆栈）
    errors = []
    page.on("pageerror", lambda e: errors.append({
        "msg": str(e),
        "stack": getattr(e, 'stack', '') or 'no stack'
    }))

    page.goto(HOST + "/", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)

    # 点击项目卡片（用 force=True 绕过可见性检查）
    target = page.locator("text=最后一次值班").first
    try:
        target.click(force=True, timeout=5000)
        page.wait_for_timeout(3000)
        print("✅ 项目已打开")
    except Exception as e:
        print(f"❌ 点击失败: {str(e)[:100]}")
        # 尝试 JS 点击
        page.evaluate("() => document.querySelector('text=最后一次值班')?.click()")
        page.wait_for_timeout(3000)

    # 检查当前 stage
    stage_info = page.evaluate("""() => {
        try {
            return {
                stage: S.stage,
                episodeId: S.episode ? S.episode.id : null,
                episodeSeq: S.episode ? S.episode.seq : null,
                projectStage: S.project ? S.project.stage : null
            };
        } catch(e) {
            return {error: String(e)};
        }
    }""")
    print("\n=== 当前状态 ===")
    for k, v in stage_info.items():
        print(f"  {k}: {v}")

    # 手动切换到 render stage（如果不在）
    cur_stage = stage_info.get("stage")
    if cur_stage != "render":
        # 点击「渲染成片」导航
        nav = page.locator("text=渲染成片").first
        if nav.count() > 0:
            nav.click()
            page.wait_for_timeout(2000)
            print(f"\n切换到渲染页 (之前: {cur_stage})")

    # 检查 composerBox 内容
    comp_info = page.evaluate("""() => {
        const box = document.getElementById('composerBox');
        const info = {
            boxExists: !!box,
            boxChildren: box ? box.children.length : -1,
            boxHTMLLength: box ? box.innerHTML.length : -1,
            boxHTMLPreview: box ? box.innerHTML.slice(0, 500) : 'N/A',
            compTakes: window._compTakes ? window._compTakes.length : 'undefined',
            compState: window._compState ?
                JSON.stringify({epId: window._compState.epId, selCount: Object.keys(window._compState.sel||{}).length})
                : 'undefined',
            fnRenderComposer: typeof renderComposer,
            fnRenderCompList: typeof renderCompList,
            fnPickTake: typeof window.pickTake,
            fnAssembleFromSelection: typeof window.assembleFromSelection
        };
        return info;
    }""")
    print("\n=== composerBox 状态 ===")
    for k, v in comp_info.items():
        print(f"  {k}: {v}")

    # 手动调用 renderComposer 并检查结果
    manual = page.evaluate("""async () => {
        try {
            const ep = S.episode;
            if (!ep) return {error: 'S.episode 为空'};
            console.log('renderComposer 调用, ep.id =', ep.id);
            await renderComposer(ep);
            const box = document.getElementById('composerBox');
            return {
                ok: true,
                boxChildren: box ? box.children.length : -1,
                boxHTMLPreview: box ? box.innerHTML.slice(0, 300) : 'N/A'
            };
        } catch(e) {
            return {error: String(e), stack: (e.stack || '').slice(0, 500)};
        }
    }""")
    print("\n=== 手动调用 renderComposer ===")
    for k, v in manual.items():
        print(f"  {k}: {v}")

    # 检查 takes API 在当前 episode 下的返回
    api_check = page.evaluate("""async () => {
        try {
            const ep = S.episode;
            if (!ep) return {error: 'no episode'};
            const r = await api('/api/episodes/' + ep.id + '/takes');
            return {
                status: 'ok',
                takesCount: (r.takes || []).length,
                firstTakeShotId: r.takes && r.takes[0] ? r.takes[0].shot_id : null,
                firstTakeTakesLen: r.takes && r.takes[0] && r.takes[0].takes ? r.takes[0].takes.length : -1
            };
        } catch(e) {
            return {error: String(e)};
        }
    }""")
    print("\n=== takes API（当前 episode）===")
    for k, v in api_check.items():
        print(f"  {k}: {v}")

    # 页面错误
    print(f"\n=== 页面 JS 错误（{len(errors)} 个）===")
    for e in errors:
        print(f"  错误: {e['msg'][:200]}")
        print(f"  堆栈: {e['stack'][:300]}")
        print()

    page.screenshot(path="/tmp/sdf_composer_diag.png", full_page=True)
    print("截图: /tmp/sdf_composer_diag.png")
    b.close()
