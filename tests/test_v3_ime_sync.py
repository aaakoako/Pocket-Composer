"""生产页面、真实 WebSocket/存储，模拟输入法遗漏 input；按键投递为替身。"""
import asyncio

import pytest

from tests.test_v3_editor_transactions import product, synced
from tests.test_v3_frontend_prod import READ_DRAFT
from tests.test_v3_assistant_delivery import platform
from doubao_typeless.core.bundle import freeze_bundle


async def received(page, text):
    await page.wait_for_function("text => document.querySelector('#text').value === text && document.querySelector('#charCount').textContent === `${[...text].length} 字` && document.querySelector('#transferStatus').dataset.transport === 'synced'", arg=text, timeout=5000)


@pytest.mark.parametrize('event', [None, 'compositionend', 'change', 'blur'])
def test_ime_tail_syncs_without_insert_click(tmp_path, event):
    async def run():
        async with product(tmp_path, touch=True) as (page, app, _):
            await page.fill('#text', '只有开头'); await synced(page)
            full = '只有开头，接着语音输入完整的后半段。\n第二行🙂'
            await page.evaluate("({full,event}) => {const t=document.querySelector('#text');t.value=full;if(event)t.dispatchEvent(new Event(event,{bubbles:true}));}", {'full':full, 'event':event})
            await received(page, full)
            assert app.draft.text == full
            assert (await page.evaluate(READ_DRAFT))['text'] == full
    asyncio.run(run())


def test_phone_insert_copies_full_text_rotates_and_recovers(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            copied = []
            platform(app)
            app.delivery._set_text = copied.append
            for session in app.auth.sessions.values(): session.allow_insert = True
            await page.fill('#text', '头部'); await synced(page)
            full = '头部和输入法刚提交的完整内容🙂'
            await page.evaluate("text=>{document.querySelector('#text').value=text;document.querySelector('#sendBtn').click()}", full)
            await page.wait_for_function("document.querySelector('#text').value==='' && document.querySelector('#sendBtn').disabled", timeout=7000)
            await synced(page)
            assert copied == [full] and app.draft.text == ''
            assert app.history.last_bundle()['text'] == full
            await page.fill('#text', '下一段'); await received(page, '下一段')
            await page.click('#historyBtn')
            await page.get_by_role('button', name='恢复手机上次图文（可离线）').click()
            await page.get_by_role('button', name='保留当前，恢复图文').click()
            await received(page, full)
            assert copied == [full], '恢复不会再次插入'
    asyncio.run(run())


def test_old_receipt_does_not_clear_unreported_ime_tail(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '已插入的旧稿'); await synced(page)
            event = app._phone_rotate_event(freeze_bundle(app.draft, bundle_id='old'), True, 'UNKNOWN')
            full = '已插入的旧稿，还有刚说的新内容'
            await page.evaluate("text=>document.querySelector('#text').value=text", full)
            await app.bridge.publish_phone_event(event)
            await received(page, full)
            assert app.draft.text == full
    asyncio.run(run())


def test_desktop_prepare_reads_silent_ime_commit(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '头'); await synced(page)
            full = '头和刚提交的语音尾部'
            await page.evaluate("text=>document.querySelector('#text').value=text", full)
            result = await app.bridge.prepare_phone(app.draft.editor_device_id)
            assert not result.get('error'), result
            await received(page, full)
            assert app.draft.text == full
    asyncio.run(run())


def test_failed_read_allows_explicit_phone_retry_without_editing(tmp_path):
    async def run():
        from doubao_typeless.platform.windows.clipboard import ClipboardReadError
        async with product(tmp_path) as (page, app, _):
            pastes = []; clipboard = []
            platform(app, paste=lambda: pastes.append('paste'))
            app.delivery._set_text = clipboard.append
            app.delivery._read_clipboard_text = lambda: (_ for _ in ()).throw(ClipboardReadError())
            for session in app.auth.sessions.values(): session.allow_insert = True
            full = '读失败后不改字也能再次插入'
            await page.fill('#text', full); await synced(page)
            await page.click('#sendBtn')
            await page.wait_for_function("document.querySelector('#deliveryStatus').textContent.includes('剪贴板暂时读不出') && !document.querySelector('#sendBtn').disabled", timeout=7000)
            assert pastes == [] and app.draft.text == full
            app.delivery._read_clipboard_text = lambda: clipboard[-1]
            await page.click('#sendBtn')
            await page.wait_for_function("document.querySelector('#text').value===''")
            await synced(page)
            assert pastes == ['paste']
    asyncio.run(run())
