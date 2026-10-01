"""正式手机页面 + 实际桥接：编辑提交与取消的事务边界，不调用页面内部函数。"""
import asyncio
from contextlib import asynccontextmanager
import io
from pathlib import Path
import pytest
from aiohttp import web
from PIL import Image
from tests.test_v3_frontend_prod import READ_DRAFT
from doubao_typeless.app import V3App


def photo():
    stream=io.BytesIO();Image.new('RGB',(120,80),(25,65,150)).save(stream,format='PNG')
    return {'name':'saved.png','mimeType':'image/png','buffer':stream.getvalue()}


@asynccontextmanager
async def product(tmp_path, *, touch=False):
    from playwright.async_api import async_playwright
    app=V3App(data_dir=tmp_path/'pc',port=0)
    app.hud.show_receiving=lambda *args,**kw:None
    runner=web.AppRunner(app.bridge.make_app());await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    errors=[];uploads=[]
    try:
        async with async_playwright() as pw:
            browser=await pw.chromium.launch()
            context=await browser.new_context(viewport={'width':390,'height':844},has_touch=touch,is_mobile=touch)
            page=await context.new_page()
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda req:uploads.append(req.url) if req.method=='POST' and req.url.endswith('/v3/assets/init') else None)
            await page.goto(f'http://127.0.0.1:{port}/?pair={app.auth.new_pairing_challenge()}')
            await synced(page)
            try:yield page,app,uploads
            finally:await browser.close()
    finally:await runner.cleanup();await app.stop()
    assert not errors,errors


async def synced(page):
    await page.wait_for_function("document.querySelector('#transferStatus').dataset.transport === 'synced'")


async def images_received(page,app):
    # Draft metadata acknowledgement may precede the image upload. Capture the
    # saved baseline only once the UI confirms the actual image was received.
    await page.wait_for_function("document.querySelectorAll('.attach-card').length>0 && [...document.querySelectorAll('.attach-card label')].every(e=>e.textContent.includes('电脑已收到'))")
    async def remote_ready():
        while not app.draft.assets or not all(a.get('asset_id') and a.get('status')=='ready' for a in app.draft.assets):
            await asyncio.sleep(.01)
    await asyncio.wait_for(remote_ready(),5)


async def editing(page):
    await page.wait_for_function("document.querySelector('#editor').classList.contains('show') && document.querySelector('#stage').dataset.ready==='1' && !document.querySelector('#done').disabled")


async def stroke(page):
    await editing(page);box=await page.locator('#stage').bounding_box()
    await page.mouse.move(box['x']+box['width']*.3,box['y']+box['height']*.4)
    await page.mouse.down();await page.mouse.move(box['x']+box['width']*.65,box['y']+box['height']*.5,steps=5);await page.mouse.up()


def test_new_unsaved_boards_cancel_without_upload(tmp_path):
    async def run():
        async with product(tmp_path) as (page,app,uploads):
            await page.fill('#text','本来要说的话');await synced(page)
            await page.click('#boardBtn');await editing(page);await page.click('#back')
            await page.wait_for_selector('#editor.show',state='hidden');await synced(page)
            assert await page.locator('.attach-card').count()==0 and app.draft.assets==[]
            assert await page.locator('#text').input_value()=='本来要说的话'
            await page.click('#boardBtn');await stroke(page);await page.click('#back')
            await page.wait_for_selector('#discardEditing');await page.click('#keepEditing')
            assert await page.locator('#editor').is_visible()
            await page.click('#back');await page.click('#discardEditing')
            await page.wait_for_selector('#editor.show',state='hidden');await synced(page)
            assert await page.locator('.attach-card').count()==0 and app.draft.assets==[]
            assert uploads==[],uploads
    asyncio.run(run())


def test_cancel_reedit_restores_saved_image_caption_and_asset_identity(tmp_path):
    async def run():
        async with product(tmp_path) as (page,app,uploads):
            await page.set_input_files('#file',photo());await editing(page)
            await page.click('#captionToggle');await page.fill('#captionInput','原图说明')
            await page.click('#done');await page.wait_for_selector('#editor.show',state='hidden');await synced(page)
            await images_received(page,app)
            first=await page.evaluate(READ_DRAFT);previous=first['assets'][0]
            original_id=app.draft.assets[0]['asset_id'];original_uploads=len(uploads)
            assert original_id and original_uploads==1,uploads
            await page.locator('.attach-card img').click();await stroke(page)
            await page.click('#captionToggle');await page.fill('#captionInput','不应该提交的修改')
            await page.click('#back');await page.click('#discardEditing')
            await page.wait_for_selector('#editor.show',state='hidden');await synced(page)
            saved=await page.evaluate(READ_DRAFT);current=saved['assets'][0]
            for key in ('asset_id','scene','caption','preview','render_revision'):
                assert current.get(key)==previous.get(key),(key,current.get(key),previous.get(key))
            assert app.draft.assets[0]['asset_id']==original_id and app.draft.assets[0]['caption']=='原图说明'
            assert len(uploads)==original_uploads
    asyncio.run(run())


def test_selected_photo_queue_opens_next_only_after_save_and_can_cancel_it(tmp_path):
    async def run():
        async with product(tmp_path) as (page,app,uploads):
            a=photo();b={**photo(),'name':'second.png'}
            await page.set_input_files('#file',[a,b]);await editing(page)
            await page.click('#done')
            # 同一个画板节点继续使用，但这一次必须已经轮到第二张，不是第一张未退出。
            await page.wait_for_function("document.querySelector('#editor').classList.contains('show') && !document.querySelector('#done').disabled && document.querySelectorAll('.attach-card').length===2")
            await page.click('#back')
            # 无修改第二张直接取消，而不是保存第二张。
            await page.wait_for_selector('#editor.show',state='hidden');await synced(page)
            await images_received(page,app)
            assert await page.locator('.attach-card').count()==1
            assert len(app.draft.assets)==1 and app.draft.assets[0]['status']=='ready'
    asyncio.run(run())


def test_local_storage_failure_keeps_original_and_does_not_upload(tmp_path):
    async def run():
        async with product(tmp_path) as (page,app,uploads):
            await page.set_input_files('#file',photo());await editing(page);await page.click('#done')
            await page.wait_for_selector('#editor.show',state='hidden');await synced(page)
            await images_received(page,app)
            original=app.draft.assets[0]['asset_id'];count=len(uploads)
            assert original and count==1,uploads
            await page.locator('.attach-card img').click();await stroke(page)
            # 明确模拟浏览器容量错误；不是修改生产保存函数或放宽验证。
            await page.evaluate("() => {window.realPut=IDBObjectStore.prototype.put;IDBObjectStore.prototype.put=function(){throw new DOMException('test quota','QuotaExceededError')};}")
            await page.click('#done')
            await page.wait_for_function("document.querySelector('#editorStatus').textContent.includes('保存失败')")
            assert await page.locator('#editor').is_visible()
            assert len(uploads)==count
            await page.evaluate("() => {IDBObjectStore.prototype.put=window.realPut;}")
            await page.click('#back');await page.click('#discardEditing')
            await page.wait_for_selector('#editor.show',state='hidden');await synced(page)
            assert app.draft.assets[0]['asset_id']==original
    asyncio.run(run())
