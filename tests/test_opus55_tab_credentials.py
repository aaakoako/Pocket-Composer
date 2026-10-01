"""生产前端（web/dist）双标签页接管与共享凭据：真实 Chromium IndexedDB/localStorage，服务为本机测试实例。"""
import asyncio
import json

import pytest

from tests.test_v3_editor_transactions import product, synced
from tests.test_v3_frontend_prod import READ_DRAFT

ALL_RECORDS = """() => new Promise(resolve=>{const r=indexedDB.open('doubao-typeless-v3-drafts',1);
r.onsuccess=()=>{const tx=r.result.transaction('drafts','readonly'),q=tx.objectStore('drafts').getAllKeys(),v=tx.objectStore('drafts').getAll();
tx.oncomplete=()=>{resolve(q.result.map((k,i)=>[String(k),v.result[i]&&v.result[i].text]));r.result.close();};};})"""
# 让最后一句在接管时确实只在内存里：本地写盘防抖（120ms）与同步外发防抖（100ms，外发前会写盘）
# 都要拖住。只拖 120ms 时，接管若晚于 100ms，这句已正常写进主稿并显示在新页面，场景就不再是“未保存”。
SLOW_DEBOUNCE = """() => {const original=window.setTimeout;
window.setTimeout=(fn,ms,...args)=>original(fn,(ms===120||ms===100)?60000:ms,...args);}"""
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
            await asyncio.sleep(.4)
            assert (await page.evaluate(READ_DRAFT))['text'] == '已保存'
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


@pytest.mark.parametrize('takeover_delay', [0, .4], ids=['prompt-takeover', 'slow-takeover'])
def test_takeover_back_keeps_newer_current_and_side_copies_pending_text(tmp_path, takeover_delay):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '已保存'); await synced(page)
            await page.evaluate(SLOW_DEBOUNCE)
            app.bridge.paused = True
            await page.fill('#text', PENDING)
            await asyncio.sleep(takeover_delay)
            assert (await page.evaluate(READ_DRAFT))['text'] == '已保存'
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


# 只让本页另存副本（side-superseded-*）的写入失败，模拟手机存储已满；同时记下本页实际落下的 put/delete。
FAIL_SIDE_SAVES = """() => {window.sideFull=true; window.idbLog=[];
const put=IDBObjectStore.prototype.put, del=IDBObjectStore.prototype.delete;
IDBObjectStore.prototype.put=function(value,key){
  if(window.sideFull && String(key).startsWith('side-superseded-'))throw new DOMException('存储已满（模拟）','QuotaExceededError');
  const request=put.call(this,value,key);
  this.transaction.addEventListener('complete',()=>window.idbLog.push(['put',String(key),value&&value.text]));
  return request;};
IDBObjectStore.prototype.delete=function(key){window.idbLog.push(['delete',String(key)]);return del.call(this,key);};}"""


def test_failed_side_save_keeps_page_paused_and_retry_preserves_before_claiming(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '已保存'); await synced(page)
            await page.evaluate(SLOW_DEBOUNCE)
            await page.evaluate(FAIL_SIDE_SAVES)
            app.bridge.paused = True
            await page.fill('#text', PENDING)
            other = await _take_over(page, app)
            await other.wait_for_selector('#text:not([disabled])')
            await other.fill('#text', '另一页接着写的新稿')
            await _until(lambda: _current_is(other, '另一页接着写的新稿'))

            for _ in range(2):  # 接管时另存失败；存储仍满时再点也一样失败
                await page.click('#takeTab')
                await page.wait_for_function("document.getElementById('tabBanner').hidden||"
                                             "document.getElementById('takeTab').textContent==='重试保存并继续'")
                assert await page.input_value('#text') == PENDING
                assert await page.locator('#tabBanner').is_visible()
                log = await page.evaluate('window.idbLog')
                assert not any(op[0] == 'delete' or op[1] in ('writer', 'current') for op in log), log
                assert (await other.evaluate(READ_DRAFT))['text'] == '另一页接着写的新稿'
                assert await _no_side_with(page, PENDING)
                assert await page.evaluate("(()=>{const t=document.getElementById('text');return !t.disabled&&t.readOnly;})()")
                assert '没能另存' in await page.locator('#tabBanner p').inner_text()

            await page.evaluate('window.sideFull=false')
            await page.click('#takeTab')
            await page.wait_for_selector('#tabBanner', state='hidden')
            assert await page.input_value('#text') == '另一页接着写的新稿'
            assert (await page.evaluate(READ_DRAFT))['text'] == '另一页接着写的新稿'
            assert '放进「最近」' in await page.locator('#sync').inner_text()
            records = await page.evaluate(ALL_RECORDS)
            assert any(key.startswith('side-superseded-') and text == PENDING for key, text in records)
            log = await page.evaluate('window.idbLog')
            keys = [op[1] for op in log]
            side = next(i for i, op in enumerate(log) if op[1].startswith('side-superseded-') and op[2] == PENDING)
            # 副本先落盘，之后才登记为写入者；本页没有删除任何记录，也没写过主稿。
            assert side < keys.index('writer')
            assert not any(op[0] == 'delete' for op in log) and 'current' not in keys, log
            await other.wait_for_selector('#tabBanner', state='visible')
            await other.close()
    asyncio.run(run())


def test_takeover_after_failed_main_save_still_side_copies_the_unsaved_text(tmp_path):
    # 没有在途防抖、但上一次写主稿已真实失败：这段内容同样只在内存里，接管时也要另存，不能当成“已落盘”。
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '已保存'); await synced(page)
            await page.evaluate("""() => {window.currentFull=true; const put=IDBObjectStore.prototype.put;
              IDBObjectStore.prototype.put=function(value,key){
                if(window.currentFull && key==='current')throw new DOMException('存储已满（模拟）','QuotaExceededError');
                return put.call(this,value,key);};}""")
            app.bridge.paused = True
            await page.fill('#text', PENDING)
            await page.wait_for_function("document.getElementById('localSave').textContent.includes('手机存储不可用')")
            await page.evaluate('window.currentFull=false')
            assert (await page.evaluate(READ_DRAFT))['text'] == '已保存'
            other = await _take_over(page, app)
            await other.wait_for_selector('#text:not([disabled])')
            await other.fill('#text', '另一页接着写的新稿')
            await _until(lambda: _current_is(other, '另一页接着写的新稿'))
            await page.click('#takeTab')
            await page.wait_for_selector('#tabBanner', state='hidden')
            assert await page.input_value('#text') == '另一页接着写的新稿'
            records = await page.evaluate(ALL_RECORDS)
            assert any(key.startswith('side-superseded-') and text == PENDING for key, text in records), records
            await other.close()
    asyncio.run(run())


def test_text_saved_before_takeover_is_what_the_new_tab_shows(tmp_path):
    # 另一种真实时序：防抖已写盘后才被接管。这句已是主稿，新页面直接显示它，不需要也不另存副本。
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '已保存'); await synced(page)
            app.bridge.paused = True
            await page.fill('#text', PENDING)
            await _until(lambda: _current_is(page, PENDING))
            other = await _take_over(page, app)
            await other.wait_for_selector('#text:not([disabled])')
            assert await other.input_value('#text') == PENDING
            records = await other.evaluate(ALL_RECORDS)
            assert not any(key.startswith('side-') and text == PENDING for key, text in records)
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
