"""正式手机页面：记住凭据、断线续接和撤销。测试使用隔离数据与无头浏览器。"""
import asyncio

from doubao_typeless.storage.credentials import AuthService
from tests.test_v3_editor_transactions import product, synced


async def remember(page, app):
    session = next(iter(app.auth.sessions.values()))
    app.auth.set_grants(session.session_id, allow_insert=True, allow_capture=True)
    app.auth.remember_device(session)
    await app.bridge.send_to_session(session.session_id, {"type": "device.remembered"})
    await page.wait_for_function("!!localStorage.getItem('dt.v3.device')")
    async def acknowledged():
        while app.auth.take_device_secret(session):
            await asyncio.sleep(.02)
    await asyncio.wait_for(acknowledged(), 10)
    return session.device_id


def test_lost_secret_response_is_retried_and_offline_reload_keeps_identity(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '断线后仍是我的草稿'); await synced(page)
            attempts = 0

            async def lose_first_response(route):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    await route.fetch()  # 电脑已生成响应，但手机没有收到。
                    await route.abort()
                else:
                    await route.continue_()

            await page.route('**/v3/device/secret', lose_first_response)
            identity = await remember(page, app)
            assert attempts >= 2
            await page.context.set_offline(True)
            await page.wait_for_function("document.querySelector('#connText').textContent.includes('离线')")
            await page.fill('#text', '断网期间也能继续写')
            await page.context.set_offline(False)
            await synced(page)
            await page.reload(); await synced(page)
            assert app.draft.editor_device_id == identity
            assert app.draft.text == '断网期间也能继续写'
            assert len(app.auth.public_sessions()) == 1
            assert not await page.locator('#sheet').is_visible()
    asyncio.run(run())


def test_restart_auth_and_temporary_resume_failure_retry_without_pairing(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '电脑恢复后接着写'); await synced(page)
            identity = await remember(page, app)
            # 重新从磁盘创建认证服务，等同进程重启后：没有内存会话，只有持久凭据。
            app.auth = AuthService(store_path=app.auth.store_path)
            app.bridge.auth = app.auth
            attempts = 0
            release = asyncio.Event()

            async def unavailable(route):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    await release.wait()
                    await route.fulfill(status=503, content_type='application/json', body='{"error":"starting"}')
                else:
                    await route.continue_()

            await page.route('**/v3/pair', unavailable)
            await page.reload()
            async def requested():
                while not attempts:
                    await asyncio.sleep(.02)
            await asyncio.wait_for(requested(), 8)
            # 唤醒和点击重试不能并发创建多份续接会话。
            for _ in range(3):
                await page.dispatch_event('#connectBtn', 'click')
                await page.evaluate("window.dispatchEvent(new Event('online'))")
            assert attempts == 1
            assert '自动续接中' in await page.locator('#connText').inner_text()
            assert not await page.locator('#sheet').is_visible()
            assert await page.locator('#text').input_value() == '电脑恢复后接着写'
            release.set()
            await synced(page)
            assert attempts == 2
            assert app.draft.editor_device_id == identity
            device = app.auth.public_sessions()
            assert len(device) == 1 and device[0]['device_id'] == identity
            assert device[0]['allow_insert'] and device[0]['allow_capture']
            assert not await page.locator('#sheet').is_visible()
    asyncio.run(run())


def test_rescan_in_new_tab_resumes_existing_device_and_revoke_requires_pairing(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            await page.fill('#text', '同一手机的新标签页'); await synced(page)
            identity = await remember(page, app)
            code = app.auth.new_pairing_challenge()
            other = await page.context.new_page()
            await other.goto(page.url.split('?')[0] + '?pair=' + code)
            await synced(other)
            assert app.auth.current_pairing_challenge() == code
            assert app.draft.editor_device_id == identity
            assert len(app.auth.public_sessions()) == 1
            assert await other.locator('#text').input_value() == '同一手机的新标签页'
            assert 'pair=' not in other.url
            app.auth.forget_device(identity)
            await other.reload()
            await other.wait_for_selector('#pairStatus', state='visible')
            assert await other.evaluate("localStorage.getItem('dt.v3.device')") is None
            assert not app.auth.sessions
            assert await other.locator('#text').input_value() == '同一手机的新标签页'
            await other.close()
    asyncio.run(run())


def test_storage_denied_does_not_ack_or_claim_saved_and_can_retry(tmp_path):
    async def run():
        async with product(tmp_path) as (page, app, _):
            session = next(iter(app.auth.sessions.values()))
            await page.evaluate("""() => {
                window.originalSetItem = Storage.prototype.setItem;
                Storage.prototype.setItem = function(k,v) {
                    if(k === 'dt.v3.device') throw new DOMException('test quota','QuotaExceededError');
                    return window.originalSetItem.call(this,k,v);
                };
            }""")
            app.auth.remember_device(session)
            await app.bridge.send_to_session(session.session_id, {"type": "device.remembered"})
            await page.wait_for_function("document.body.textContent.includes('浏览器未能保存配对')")
            assert app.auth.take_device_secret(session)
            assert app.auth.public_sessions()[0]['remember_pending']
            assert await page.evaluate("localStorage.getItem('dt.v3.device')") is None
            await page.reload(); await synced(page)
            await page.wait_for_function("!!localStorage.getItem('dt.v3.device')")
            async def acknowledged():
                while session.device_secret_once:
                    await asyncio.sleep(.02)
            await asyncio.wait_for(acknowledged(), 5)
            assert not app.auth.public_sessions()[0]['remember_pending']
    asyncio.run(run())
