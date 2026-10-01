"""Opus 5.5 审查修复：生产 web/dist 页面 + 一次性本机 V3App 的网页流程。

系统投递是明确的平台替身（platform()），不碰真实 Codex/Cursor 输入框，也不是 Android 真机证据。
"""
from __future__ import annotations

import asyncio

from tests.test_v3_assistant_delivery import platform
from tests.test_v3_editor_transactions import editing, images_received, photo, product, synced
from tests.test_v3_frontend_prod import READ_DRAFT


async def until(predicate, timeout=10):
    async def wait():
        while not predicate():
            await asyncio.sleep(.025)
    await asyncio.wait_for(wait(), timeout)


def observe(app):
    outcomes = []
    original = app._on_intent
    def wrapped(intent, bundle):
        result = original(intent, bundle)
        outcomes.append(result)
        return result
    app._on_intent = wrapped
    return outcomes


def grant(app):
    session = next(iter(app.auth.sessions.values()))
    app.auth.set_grants(session.session_id, allow_insert=True)
    return session


# ---- O-P1-1：正文里有 PowerShell 等技术词，回执照常到达手机，插入闭环与普通正文一致 ----

def test_technical_words_complete_the_same_insert_loop(tmp_path):
    async def run():
        for index, text in enumerate(("请检查这个运行报错", "请检查这个 PowerShell 报错，cmd.exe 也打不开")):
            async with product(tmp_path / str(index)) as (page, app, _):
                written = platform(app); grant(app); outcomes = observe(app)
                await page.fill("#text", text); await synced(page)
                await page.click("#sendBtn"); await until(lambda: bool(outcomes))
                assert written == [text]
                # 回执没被手机过滤：新段落开始，上一段可在「最近」恢复。
                await page.wait_for_function("document.querySelector('#text').value === ''")
                assert "forbidden" not in await page.locator("body").inner_text()
    asyncio.run(run())


# ---- O-P1-2：NO_STEPS 后用户再点一次可重试，且只投递一次 ----

def test_no_steps_can_be_retried_by_explicit_click(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            written = platform(app); valid_focus = app._read_focus
            app._read_focus = lambda: ("QtWindow", "Pocket Composer", 11)
            grant(app); outcomes = observe(app)
            await page.fill("#text", "目标修好后应该能重试"); await synced(page)
            await page.click("#sendBtn"); await until(lambda: len(outcomes) == 1)
            assert outcomes[0]["result"] == "NO_STEPS" and written == []
            await page.wait_for_function("!document.querySelector('#sendBtn').disabled")
            app._read_focus = valid_focus
            await page.click("#sendBtn"); await until(lambda: len(outcomes) == 2)
            assert not outcomes[1].get("duplicate") and outcomes[1]["result"] != "NO_STEPS"
            assert written == ["目标修好后应该能重试"]
            await asyncio.sleep(.3)
            assert written == ["目标修好后应该能重试"] and len(outcomes) == 2
    asyncio.run(run())


# ---- O-P1-3：失去凭据后重新配对 = 新身份；旧图用手机本地原图重传，不靠旧 device_id 夺权 ----

def test_repair_after_restart_reuploads_local_images_under_new_identity(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill("#text", "带图的旧稿")
            await page.set_input_files("#file", photo()); await editing(page)
            await page.click("#done"); await images_received(page, app)
            old_device = next(iter(app.auth.sessions.values())).device_id
            old_asset = app.draft.assets[0]["asset_id"]
            app.auth.sessions.clear()  # 电脑重启：临时会话全部失效，素材和草稿保留
            await page.goto(page.url.split("?")[0] + "?pair=" + app.auth.new_pairing_challenge())

            async def reowned():
                while True:
                    if await page.locator("#useLocal").is_visible():
                        await page.click("#useLocal")
                    sessions = list(app.auth.sessions.values())
                    assets = app.draft.assets
                    if sessions and assets and all(a.get("asset_id") and a.get("status") == "ready" for a in assets):
                        row = app.db.asset_by_id(assets[0]["asset_id"]) or {}
                        if row.get("owner_session_id") == "device:" + sessions[0].device_id:
                            return sessions[0]
                    await asyncio.sleep(.05)
            session = await asyncio.wait_for(reowned(), 20)
            assert session.device_id != old_device
            assert app.draft.assets[0]["asset_id"] != old_asset
            assert app.draft.editor_device_id == session.device_id and app.draft.text == "带图的旧稿"
            assert app.db.asset_by_id(old_asset)["owner_session_id"] == "device:" + old_device
            await images_received(page, app)
            assert await page.locator(".attach-card").count() == 1
    asyncio.run(run())


# ---- O-P1-3 + V2：同一手机新标签页扫码，凭有效旧会话延续身份；旧页面暂停、可接回 ----

def test_second_tab_keeps_identity_pauses_old_tab_and_can_hand_back(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill("#text", "第一页写的"); await synced(page)
            first = next(iter(app.auth.sessions.values()))
            app.auth.set_grants(first.session_id, allow_insert=True)
            other = await page.context.new_page()
            await other.goto(page.url.split("?")[0] + "?pair=" + app.auth.new_pairing_challenge())
            await synced(other)
            devices = {s.device_id for s in app.auth.sessions.values()}
            assert len(app.auth.sessions) == 2 and devices == {first.device_id}
            assert all(s.allow_insert for s in app.auth.sessions.values())
            assert await other.locator("#text").input_value() == "第一页写的"
            await page.wait_for_selector("#tabBanner", state="visible")
            assert await page.locator("#text").is_disabled()
            await other.fill("#text", "第二页接着写"); await synced(other)
            assert app.draft.text == "第二页接着写"
            # 电脑向这台手机取稿：暂停页不挡路，当前页回复。
            await app.bridge.prepare_phone(first.device_id)
            assert app.draft.text == "第二页接着写"
            # 旧页面被接回：读入最新主稿后才可写，另一页随之暂停。
            await page.click("#takeTab")
            await page.wait_for_selector("#tabBanner", state="hidden")
            assert await page.locator("#text").input_value() == "第二页接着写"
            await other.wait_for_selector("#tabBanner", state="visible")
            await synced(page)
            await page.fill("#text", "第一页接回后写的"); await synced(page)
            assert app.draft.text == "第一页接回后写的"
            stored = await page.evaluate(READ_DRAFT)
            assert stored["text"] == "第一页接回后写的"
            await app.bridge.prepare_phone(first.device_id)
            await other.close()
    asyncio.run(run())


def test_stale_tab_cannot_resume_without_reading_latest(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill("#text", "A"); await synced(page)
            other = await page.context.new_page()
            await other.goto(page.url.split("?")[0] + "?pair=" + app.auth.new_pairing_challenge())
            await synced(other)
            await page.wait_for_selector("#tabBanner", state="visible")
            await other.fill("#text", "B 页最新"); await synced(other)
            # 旧页面回到前台（visibilitychange）：先读最新主稿，再接着写；不会把 "A" 写回。
            await page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
            await page.wait_for_selector("#tabBanner", state="hidden")
            assert await page.locator("#text").input_value() == "B 页最新"
            await synced(page)
            assert app.draft.text == "B 页最新"
            assert (await page.evaluate(READ_DRAFT))["text"] == "B 页最新"
            await other.close()
    asyncio.run(run())


# ---- V1：读失败不等于没有旧稿 ----

FAULT = """
(() => {
  const original = IDBObjectStore.prototype.get;
  IDBObjectStore.prototype.get = function(key) {
    const request = original.call(this, key);
    const mode = localStorage.getItem('dt.test.fault');
    if (this.name === 'drafts' && key === 'current' && (mode === 'always' || mode === 'once')) {
      if (mode === 'once') localStorage.removeItem('dt.test.fault');
      this.transaction.abort();
    }
    return request;
  };
})();
"""


SIDE_HAS = """text => new Promise(resolve => {
  const r = indexedDB.open('doubao-typeless-v3-drafts', 1);
  r.onsuccess = () => {
    const tx = r.result.transaction('drafts', 'readonly');
    const keys = tx.objectStore('drafts').getAllKeys(), values = tx.objectStore('drafts').getAll();
    tx.oncomplete = () => {
      r.result.close();
      resolve(keys.result.some((k, i) => String(k).startsWith('side-new-') && values.result[i].text === text));
    };
  };
  r.onerror = () => resolve(false);
})"""


async def unsynced_local(page, app, text):
    app.bridge.paused = True  # 只存手机，不让电脑接收：电脑上没有这一份
    await page.fill("#text", text)
    for _ in range(200):
        before = await page.evaluate(READ_DRAFT)
        if before and before["text"] == text:
            break
        await asyncio.sleep(.03)
    assert before["text"] == text and app.draft.text != text


def test_single_read_abort_is_retried_and_keeps_local_draft(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            text = "还没有同步到电脑的手机草稿"
            await unsynced_local(page, app, text)
            await page.add_init_script(FAULT)
            await page.evaluate("localStorage.setItem('dt.test.fault','once')")
            await page.reload()
            await page.wait_for_function("!document.querySelector('#text').disabled")
            assert await page.locator("#text").input_value() == text
            assert (await page.evaluate(READ_DRAFT))["text"] == text
    asyncio.run(run())


def test_persistent_read_failure_never_overwrites_and_offers_retry_or_side_draft(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            text = "读不出时也不能被空稿覆盖"
            await unsynced_local(page, app, text)
            await page.add_init_script(FAULT)
            await page.evaluate("localStorage.setItem('dt.test.fault','always')")
            await page.reload()
            await page.wait_for_selector("#readFailBanner", state="visible", timeout=10000)
            assert await page.locator("#text").is_disabled()
            assert "已保存" not in await page.locator("#localSave").inner_text()
            # 重试：故障消失后读出原稿
            await page.evaluate("localStorage.removeItem('dt.test.fault')")
            await page.click("#retryRead")
            await page.wait_for_selector("#readFailBanner", state="hidden")
            await page.wait_for_function("!document.querySelector('#text').disabled")
            assert await page.locator("#text").input_value() == text

            # 再次持续读失败，这次选「先写新稿」：只写独立记录，原稿不动
            await page.evaluate("localStorage.setItem('dt.test.fault','always')")
            await page.reload()
            await page.wait_for_selector("#readFailBanner", state="visible", timeout=10000)
            await page.click("#sideDraft")
            await page.wait_for_function("!document.querySelector('#text').disabled")
            await page.fill("#text", "读不出时先写的新稿")
            await page.wait_for_function("document.querySelector('#localSave').textContent.includes('旧稿未覆盖')")
            for _ in range(200):
                if await page.evaluate(SIDE_HAS, "读不出时先写的新稿"):
                    break
                await asyncio.sleep(.03)
            assert await page.evaluate(SIDE_HAS, "读不出时先写的新稿")
            await page.evaluate("localStorage.removeItem('dt.test.fault')")
            assert (await page.evaluate(READ_DRAFT))["text"] == text
            await page.reload()
            await page.wait_for_function("!document.querySelector('#text').disabled")
            assert await page.locator("#text").input_value() == text
            await page.click("#historyBtn")
            await page.wait_for_function("document.querySelector('#sheet').textContent.includes('读不出旧稿时另写')")
            assert "读不出时先写的新稿" in await page.locator("#sheet").inner_text()
    asyncio.run(run())


# ---- P2-4：冲突时「采用电脑这份」可用、鉴权，且不共享另一台手机的图 ----

def test_adopt_desktop_copy_takes_over_text_without_foreign_images(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill("#text", "A 手机的稿")
            await page.set_input_files("#file", photo()); await editing(page)
            await page.click("#done"); await images_received(page, app)
            a_device = app.draft.editor_device_id
            browser = page.context.browser
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            phone_b = await context.new_page()
            await phone_b.goto(page.url.split("?")[0] + "?pair=" + app.auth.new_pairing_challenge())
            await phone_b.wait_for_function("!document.querySelector('#text').disabled")
            await phone_b.fill("#text", "B 手机自己的")
            await phone_b.wait_for_selector("#useServer", state="visible", timeout=10000)
            assert app.draft.text == "A 手机的稿"
            await phone_b.click("#useServer")
            await phone_b.wait_for_function("document.querySelector('#text').value === 'A 手机的稿'")
            b_device = next(s.device_id for s in app.auth.sessions.values() if s.device_id != a_device)
            await until(lambda: app.draft.editor_device_id == b_device)
            assert app.draft.text == "A 手机的稿" and app.draft.assets == []
            assert await phone_b.locator(".attach-card").count() == 0
            assert "未带过来" in await phone_b.locator("body").inner_text()
            # B 手机原稿留在「最近」
            await phone_b.click("#historyBtn")
            await phone_b.wait_for_function("document.querySelector('#sheet').textContent.includes('B 手机自己的')")
            await context.close()
    asyncio.run(run())
