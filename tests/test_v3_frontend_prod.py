"""Production Vite frontend against a real V3Bridge. Do not inject extra protocol fields."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from aiohttp.web import AppRunner, TCPSite

from doubao_typeless.core.bundle import Draft
from doubao_typeless.services.bridge_v3 import V3Bridge
from doubao_typeless.storage.asset_store import AssetStore
from doubao_typeless.storage.credentials import AuthService

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "web" / "dist" / "index.html"


def test_production_frontend_syncs_text_without_extra_fields(tmp_path):
    assert DIST.is_file(), "web/dist/index.html missing; CI/local must run npm run build first"
    playwright = pytest.importorskip("playwright.async_api")

    async def run():
        auth = AuthService()
        draft = Draft(str(uuid.uuid4()), str(uuid.uuid4()), 0, "phone", "")
        bridge = V3Bridge(port=0, auth=auth, store=AssetStore(tmp_path / "assets"), draft=draft, data_dir=tmp_path)
        runner = AppRunner(bridge.make_app())
        await runner.setup()
        site = TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        code = auth.new_pairing_challenge()
        posts: list[str] = []
        try:
            async with playwright.async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                def on_request(request):
                    if request.method == "POST" and request.url.endswith("/v3/pair"):
                        posts.append(request.post_data or "")
                page.on("request", on_request)
                await page.goto(f"http://127.0.0.1:{port}/")
                await page.locator("#sheetCard summary").click()
                await page.wait_for_selector("#pairCode", state="visible")
                await page.fill("#pairCode", code)
                async with page.expect_response(lambda r: r.url.endswith("/v3/pair") and r.request.method == "POST") as resp_info:
                    await page.click("#pairGo")
                pair_resp = await resp_info.value
                assert pair_resp.ok, await pair_resp.text()
                await page.wait_for_function("() => !document.getElementById('sheet').classList.contains('show')")
                await page.wait_for_function("() => document.getElementById('connText').textContent.indexOf('已连接') >= 0")
                await page.fill("#text", "production frontend text")
                await page.locator("#text").dispatch_event("input")
                for _ in range(40):
                    if "production frontend text" in bridge.draft.text:
                        break
                    await asyncio.sleep(0.05)
                await browser.close()
        finally:
            await runner.cleanup()
        assert posts, "production page must POST /v3/pair"
        body = json.loads(posts[0])
        assert set(body) == {"code"}
        assert "allow_insert" not in body
        assert "assets" not in body
        assert "production frontend text" in bridge.draft.text
    asyncio.run(run())


READ_DRAFT = """() => new Promise(resolve => {
  const r=indexedDB.open('doubao-typeless-v3-drafts');
  r.onsuccess=()=>{
    const db=r.result;
    if(!db.objectStoreNames.contains('drafts')){db.close();resolve(null);return;}
    const tx=db.transaction('drafts','readonly');
    const store=tx.objectStore('drafts');
    const q=store.get('current');
    // 大字段另存为 payload-* 记录：同一读事务内换回内容，断言的是“已落盘的内容”而不是存储格式。
    let value=null;
    const swap=(node,map)=>{
      if(node&&typeof node==='object'&&!Array.isArray(node)&&typeof node.$payload==='string'&&Object.keys(node).length===1)return map.get(node.$payload);
      if(Array.isArray(node))return node.map(x=>swap(x,map));
      if(node&&typeof node==='object'&&Object.getPrototypeOf(node)===Object.prototype){const o={};for(const[k,v]of Object.entries(node)){const n=swap(v,map);if(n!==undefined)o[k]=n;}return o;}
      return node;};
    q.onsuccess=()=>{
      value=q.result||null;if(!value||!Array.isArray(value.assets))return;
      const refs=new Set();const walk=n=>{if(n&&typeof n==='object'){if(typeof n.$payload==='string')refs.add(n.$payload);else Object.values(n).forEach(walk);}};
      walk(value.assets);const map=new Map();let left=refs.size;
      for(const k of refs){const g=store.get(k);g.onsuccess=()=>{map.set(k,g.result);if(--left===0)value={...value,assets:swap(value.assets,map)};};}
    };
    tx.oncomplete=()=>{db.close();resolve(value);};
    tx.onabort=()=>{db.close();resolve(null);};
  };
  r.onerror=()=>resolve(null);
})"""


def test_production_page_reconnect_uses_phone_master_and_preserves_old_pc_draft(tmp_path):
    assert DIST.is_file(), "web/dist/index.html missing; CI/local must run npm run build first"
    playwright = pytest.importorskip("playwright.async_api")
    async def run():
        auth = AuthService()
        draft = Draft(str(uuid.uuid4()), "E-SERVER", 3, "pc", "服务器新稿")
        bridge = V3Bridge(port=0, auth=auth, store=AssetStore(tmp_path / "assets"), draft=draft, data_dir=tmp_path)
        runner = AppRunner(bridge.make_app())
        await runner.setup()
        site = TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        frames=[];errors=[];before=None
        try:
            async with playwright.async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page()
                def remember(payload, direction):
                    msg=json.loads(payload)
                    frames.append({"direction":direction,**{k:msg[k] for k in
                        ("type","text","draft_id","epoch","revision","error") if k in msg}})
                def socket_seen(socket):
                    socket.on("framereceived",lambda payload:remember(payload,"received"))
                    socket.on("framesent",lambda payload:remember(payload,"sent"))
                page.on("websocket",socket_seen)
                page.on("pageerror",lambda err:errors.append(str(err)))
                await page.goto(f"http://127.0.0.1:{port}/")
                await page.locator("#sheetCard summary").click()
                await page.wait_for_selector("#pairCode", state="visible")
                code = auth.new_pairing_challenge()
                await page.fill("#pairCode", code)
                await page.click("#pairGo")
                await page.wait_for_function("() => document.getElementById('connText').textContent.indexOf('已连接') >= 0")
                await page.fill("#text", "已同步A")
                for _ in range(100):
                    if bridge.draft.text == "已同步A": break
                    await asyncio.sleep(.05)
                assert bridge.draft.text == "已同步A"
                await page.context.set_offline(True)
                for socket in list(bridge._clients):
                    await socket.close()
                await page.wait_for_function("() => document.getElementById('connText').textContent.indexOf('已连接') < 0")
                await page.fill("#text", "离线新稿B")
                # await evaluate真的等待事务完成；不能把Promise的truthiness当成保存成功。
                for _ in range(100):
                    before=await page.evaluate(READ_DRAFT)
                    if before and before.get("text")=="离线新稿B":
                        break
                    await asyncio.sleep(.05)
                assert before and before["text"] == "离线新稿B", (before,errors)
                # 断线期间服务器只能拥有已同步镜像A，不能给手机稿制造新epoch。
                assert bridge.draft.text == "已同步A"
                await page.context.set_offline(False)
                await page.reload()
                await page.wait_for_function("() => document.getElementById('connText').textContent.indexOf('已连接') >= 0")
                try:
                    await page.wait_for_function("() => document.getElementById('transferStatus').textContent.includes('电脑已收到当前版本')",timeout=8000)
                except Exception as exc:
                    details={"before":before,"after":await page.evaluate(READ_DRAFT),
                        "local_text":await page.locator("#text").input_value(),"frames":frames,
                        "page_errors":errors,"server":{"text":bridge.draft.text,"epoch":bridge.draft.epoch}}
                    raise AssertionError(json.dumps(details,ensure_ascii=False)) from exc
                local = await page.locator("#text").input_value()
                await browser.close()
        finally:
            await runner.cleanup()
        assert bridge.draft.text == "离线新稿B"
        assert bridge.draft.epoch == before["epoch"]
        assert local == "离线新稿B"
        recovered=[json.loads(p.read_text(encoding='utf-8')) for p in (tmp_path/"recovery").glob("*.json")]
        assert any(item["text"]=="服务器新稿" for item in recovered)
        assert not any(f.get("type")=="insert.intent" for f in frames)
    asyncio.run(run())


def test_production_offline_photo_is_local_then_wakes_pc_with_rendered_version(tmp_path):
    """真实生产页离线选图/完成/续传；HUD平台显示由明确观察函数代替。"""
    import io
    from PIL import Image
    from doubao_typeless.app import V3App
    from doubao_typeless.core.bundle import freeze_bundle
    playwright=pytest.importorskip('playwright.async_api')
    image=Image.new('RGB',(128,128),(200,50,30));raw=io.BytesIO();image.save(raw,format='PNG')
    async def run():
        app=V3App(data_dir=tmp_path/'pc',port=0)
        observations=[]
        app.hud.show_receiving=lambda text,count,**kw:observations.append({'text':text,'count':count,**kw})
        runner=AppRunner(app.bridge.make_app());await runner.setup();site=TCPSite(runner,'127.0.0.1',0);await site.start()
        port=site._server.sockets[0].getsockname()[1]
        frames=[];errors=[]
        try:
            async with playwright.async_playwright() as pw:
                browser=await pw.chromium.launch();page=await browser.new_page(viewport={'width':430,'height':880})
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('websocket',lambda ws:ws.on('framesent',lambda text:frames.append(json.loads(text))))
                code=app.auth.new_pairing_challenge()
                await page.goto(f'http://127.0.0.1:{port}/?pair={code}')
                await page.wait_for_function("document.getElementById('transferStatus').textContent.includes('电脑已收到当前版本')")
                await page.context.set_offline(True)
                for ws in list(app.bridge._clients):await ws.close()
                await page.wait_for_function("document.getElementById('connText').textContent.includes('离线')")
                await page.fill('#text','离线画好再同步')
                await page.set_input_files('#file',{'name':'test.png','mimeType':'image/png','buffer':raw.getvalue()})
                await page.wait_for_selector('#editor.show')
                await page.click('#captionToggle')
                await page.fill('#captionInput','按钮往右')
                await page.click('#done')
                await page.wait_for_selector('#editor.show',state='hidden')
                saved=None
                for _ in range(80):
                    saved=await page.evaluate(READ_DRAFT)
                    if saved and saved.get('assets') and saved['assets'][0].get('status')=='queued':break
                    await asyncio.sleep(.05)
                assert saved and saved['text']=='离线画好再同步'
                assert await page.evaluate('''async()=>{const r=indexedDB.open('doubao-typeless-v3-drafts');return await new Promise(resolve=>{r.onsuccess=()=>{const db=r.result,t=db.transaction('drafts'),q=t.objectStore('drafts').get('current');t.oncomplete=()=>{resolve(q.result.assets[0].pending_png instanceof Blob);db.close()}}})}''')
                assert not app.draft.assets
                await page.context.set_offline(False)
                for _ in range(300):
                    if app.draft.assets and app.draft.assets[0].get('status')=='ready':break
                    await asyncio.sleep(.05)
                assert app.draft.assets and app.draft.assets[0].get('status')=='ready', (app.draft,errors,frames[-8:])
                assert app.draft.text=='离线画好再同步'
                bundle=freeze_bundle(app.draft,bundle_id='test-no-injection')
                assert bundle['text']=='离线画好再同步\n\n图1：按钮往右'
                assert observations[-1]['assets'][0]['status']=='ready'
                assert Path(observations[-1]['assets'][0]['path']).is_file()
                assert any(o['assets'] and o['assets'][0]['status']!='ready' for o in observations)
                # 已完成照片重新打开依然有原图像素，编辑状态立即通知电脑，而非维持旧成品。
                await page.locator('#attachments .attach-card img').first.click()
                await page.wait_for_selector('#editor.show')
                for _ in range(80):
                    if app.draft.assets[0].get('status')=='editing':break
                    await asyncio.sleep(.05)
                assert app.draft.assets[0]['status']=='editing'
                with pytest.raises(ValueError,match='IMAGE_EDITING'):freeze_bundle(app.draft,bundle_id='no-old-image')
                await browser.close()
        finally:
            await runner.cleanup();await app.stop()
        assert not any(f.get('type')=='insert.intent' for f in frames)
        assert not errors,errors
    asyncio.run(run())
