"""生产前端（web/dist）双标签页接管与共享凭据：真实 Chromium IndexedDB/localStorage，服务为本机测试实例。"""
import asyncio
import json

from tests.test_v3_editor_transactions import product, synced
from tests.test_v3_frontend_prod import READ_DRAFT

ALL_RECORDS = """() => new Promise(resolve=>{const r=indexedDB.open('doubao-typeless-v3-drafts',1);
r.onsuccess=()=>{const tx=r.result.transaction('drafts','readonly'),q=tx.objectStore('drafts').getAllKeys(),v=tx.objectStore('drafts').getAll();
tx.oncomplete=()=>{resolve(q.result.map((k,i)=>[String(k),v.result[i]&&v.result[i].text]));r.result.close();};};})"""
SLOW_DEBOUNCE = """() => {const original=window.setTimeout;
window.setTimeout=(fn,ms,...args)=>original(fn,ms===120?60000:ms,...args);}"""
PENDING = '接管前刚说完、还在 120ms 防抖里的最后一句'


async def _until(predicate, timeout=5.0):
    async def loop():
        while not await predicate():
            await asyncio.sleep(.05)
    await asyncio.wait_for(loop(), timeout)


async def _take_over(page, app):
    other = await page.context.new_page()
    await other.goto(page.url.split('?')[0] + '?pair=' + app.auth.new_pairing_challenge())
    await page.wait_for_selector('#tabBanner', state='visible')
    return other


def test_takeover_back_restores_pending_text_when_other_tab_did_not_edit(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '已保存'); await synced(page)
            await page.evaluate(SLOW_DEBOUNCE)
            app.bridge.paused = True
            await page.fill('#text', PENDING)
            other = await _take_over(page, app)
            await page.click('#takeTab')
            await page.wait_for_selector('#tabBanner', state='hidden')
            assert await page.input_value('#text') == PENDING
            await _until(lambda: _current_is(page, PENDING))
            # 另存的那份已重新成为主稿后清掉，不在「最近」里留重复项。
            await _until(lambda: _no_side_with(page, PENDING))
            await other.wait_for_selector('#tabBanner', state='visible')
            await other.close()
    asyncio.run(run())


def test_takeover_back_keeps_newer_current_and_side_copies_pending_text(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '已保存'); await synced(page)
            await page.evaluate(SLOW_DEBOUNCE)
            app.bridge.paused = True
            await page.fill('#text', PENDING)
            other = await _take_over(page, app)
            await other.wait_for_selector('#text:not([disabled])')
            await other.fill('#text', '另一页接着写的新稿')
            await _until(lambda: _current_is(other, '另一页接着写的新稿'))
            await page.click('#takeTab')
            await page.wait_for_selector('#tabBanner', state='hidden')
            assert await page.input_value('#text') == '另一页接着写的新稿'
            draft = await page.evaluate(READ_DRAFT)
            assert draft['text'] == '另一页接着写的新稿'
            records = await page.evaluate(ALL_RECORDS)
            assert any(key.startswith('side-superseded-') and text == PENDING for key, text in records)
            await other.close()
    asyncio.run(run())


async def _current_is(page, text):
    draft = await page.evaluate(READ_DRAFT)
    return bool(draft) and draft.get('text') == text


async def _no_side_with(page, text):
    records = await page.evaluate(ALL_RECORDS)
    return not any(key.startswith('side-') and value == text for key, value in records)


def test_late_resume_success_for_replaced_credential_is_not_adopted(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            stale = {'device_id': 'stale-id', 'device_secret': 'stale-secret'}
            current = {'device_id': 'new-id', 'device_secret': 'new-secret'}
            newer_proof = {'session_id': 'newer-tab-session', 'token': 'newer-tab-token'}
            await page.evaluate('(v)=>{sessionStorage.clear();localStorage.removeItem("dt.v3.proof");'
                                'localStorage.setItem("dt.v3.device",JSON.stringify(v));}', stale)
            requested, release = asyncio.Event(), asyncio.Event()

            async def delayed(route):
                if (route.request.post_data_json or {}).get('device_id') == 'stale-id':
                    requested.set(); await release.wait()
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps(
                        {'session_id': 'stale-session', 'token': 'stale-token', 'device_id': 'stale-id'}))
                else:
                    await route.continue_()
            await page.route('**/v3/pair', delayed)
            await page.reload(); await asyncio.wait_for(requested.wait(), 8)
            await page.evaluate('([d,p])=>{localStorage.setItem("dt.v3.device",JSON.stringify(d));'
                                'localStorage.setItem("dt.v3.proof",JSON.stringify(p));}', [current, newer_proof])
            release.set(); await asyncio.sleep(.5)
            stored = await page.evaluate('[localStorage.getItem("dt.v3.device"),localStorage.getItem("dt.v3.proof"),'
                                         'sessionStorage.getItem("dt.v3.session")]')
            assert json.loads(stored[0]) == current
            assert json.loads(stored[1]) == newer_proof
            assert stored[2] is None
    asyncio.run(run())


def test_revoked_session_only_forgets_its_own_proof(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            session = json.loads(await page.evaluate('sessionStorage.getItem("dt.v3.session")'))
            newer = {'session_id': 'newer-tab-session', 'token': 'newer-tab-token'}
            await page.evaluate('(p)=>localStorage.setItem("dt.v3.proof",JSON.stringify(p))', newer)
            assert await app.bridge.revoke_session(session['session_id'])
            await page.wait_for_function('sessionStorage.getItem("dt.v3.session")===null')
            assert json.loads(await page.evaluate('localStorage.getItem("dt.v3.proof")')) == newer
    asyncio.run(run())


def test_revoked_session_still_forgets_its_own_proof(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            session = json.loads(await page.evaluate('sessionStorage.getItem("dt.v3.session")'))
            proof = json.loads(await page.evaluate('localStorage.getItem("dt.v3.proof")'))
            assert proof['session_id'] == session['session_id']
            assert await app.bridge.revoke_session(session['session_id'])
            await page.wait_for_function('sessionStorage.getItem("dt.v3.session")===null')
            assert await page.evaluate('localStorage.getItem("dt.v3.proof")') is None
    asyncio.run(run())
