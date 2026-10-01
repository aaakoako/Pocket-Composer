"""Opus 5.5 审查修复：本机一次性 V3Bridge 上的 HTTP 边界（真实 aiohttp 路由，不连外网、无真实凭据）。"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace as N

from aiohttp import ClientSession
from aiohttp.web import AppRunner, TCPSite
from yarl import URL

from doubao_typeless.core.bundle import Draft
from doubao_typeless.services.bridge_v3 import V3Bridge
from doubao_typeless.storage import draft_snapshot
from doubao_typeless.storage.asset_store import AssetStore
from doubao_typeless.storage.credentials import PAIR_SOURCE_LIMIT, AuthService


@asynccontextmanager
async def served(tmp_path):
    auth = AuthService(store_path=tmp_path / "trusted.json")
    draft = Draft(str(uuid.uuid4()), str(uuid.uuid4()), 0, "phone", "")
    bridge = V3Bridge(port=0, auth=auth, store=AssetStore(tmp_path / "assets"), draft=draft, data_dir=tmp_path)
    bridge.byok = N(endpoint="http://127.0.0.1:9/original", api_key="")
    runner = AppRunner(bridge.make_app())
    await runner.setup()
    site = TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as http:
            yield bridge, http, f"http://127.0.0.1:{port}", port
    finally:
        await runner.cleanup()


def test_asset_ids_cannot_escape_asset_directory(tmp_path):
    async def run():
        async with served(tmp_path) as (bridge, http, base, _port):
            canary = tmp_path / "review-canary.bin"
            canary.write_bytes(b"owned-review-canary")
            session = bridge.auth.complete_pairing(bridge.auth.new_pairing_challenge())
            headers = {"X-DT-Session": session.session_id, "X-DT-Token": session.token}
            for raw in ("..%5Creview-canary", "..%2Freview-canary", "%2E%2E%5Creview-canary"):
                async with http.get(URL(base + "/v3/assets/" + raw, encoded=True), headers=headers) as res:
                    body = await res.read()
                    assert res.status == 404 and b"owned-review-canary" not in body
    asyncio.run(run())


def test_admin_routes_require_same_origin_loopback_json(tmp_path):
    async def run():
        async with served(tmp_path) as (bridge, http, base, port):
            changed = {"endpoint": "http://127.0.0.1:9/review-canary", "api_key": ""}
            async with http.post(base + "/v3/byok", headers={"Origin": "http://192.168.200.200",
                                 "Content-Type": "text/plain"}, data='{"endpoint":"x"}') as res:
                assert res.status == 403
            async with http.post(base + "/v3/byok", headers={"Origin": f"http://localhost:{port + 1}"}, json=changed) as res:
                assert res.status == 403
            async with http.post(base + "/v3/byok", headers={"Origin": base, "Content-Type": "text/plain"},
                                 data='{"endpoint":"x"}') as res:
                assert res.status == 415
            async with http.get(base + "/v3/sessions", headers={"Host": f"rebind.example:{port}"}) as res:
                assert res.status == 403
            assert bridge.byok.endpoint == "http://127.0.0.1:9/original"
            # 真正的电脑连接页（同源）和无 Origin 的本机程序照常可用。
            async with http.post(base + "/v3/byok", headers={"Origin": base}, json=changed) as res:
                assert res.status == 200
            assert bridge.byok.endpoint == changed["endpoint"]
            async with http.get(base + "/v3/sessions") as res:
                assert res.status == 200
    asyncio.run(run())


def test_lan_phone_pairing_still_works_with_private_origin(tmp_path):
    async def run():
        async with served(tmp_path) as (bridge, http, base, port):
            lan = f"192.168.31.20:{port}"
            code = bridge.auth.new_pairing_challenge()
            async with http.post(base + "/v3/pair", headers={"Host": lan, "Origin": f"http://{lan}"},
                                 json={"code": code}) as res:
                assert res.status == 200
                paired = await res.json()
            assert paired["device_id"] and paired["token"]
            async with http.get(base + "/v3/sessions", headers={"Host": lan, "Origin": f"http://{lan}"}) as res:
                assert res.status == 403
    asyncio.run(run())


def test_wrong_pair_codes_are_rate_limited_even_with_challenge_refresh(tmp_path):
    async def run():
        async with served(tmp_path) as (bridge, http, base, _port):
            statuses, retry = [], None
            for _ in range(30):
                bridge.auth.new_pairing_challenge()
                wrong = "0000" if bridge.auth.current_short_code() != "0000" else "0001"
                async with http.post(base + "/v3/pair", headers={"Origin": base}, json={"code": wrong}) as res:
                    statuses.append(res.status)
                    if res.status == 429:
                        retry = res.headers.get("Retry-After")
                        assert (await res.json())["error_code"] == "PAIRING_LOCKED"
            assert statuses[:PAIR_SOURCE_LIMIT] == [400] * PAIR_SOURCE_LIMIT
            assert set(statuses[PAIR_SOURCE_LIMIT:]) == {429} and retry and int(retry) > 0
            # 猜错没有让当前二维码作废；锁定到期后同一二维码仍能完成配对。
            code = bridge.auth.current_pairing_challenge()
            assert code
            bridge.auth._sources.clear()
            async with http.post(base + "/v3/pair", json={"code": code}) as res:
                assert res.status == 200
            async with http.get(base + "/v3/pair") as res:
                assert res.status == 403  # 网页不能自行生成新挑战
    asyncio.run(run())


def test_trust_store_write_failures_return_507_and_keep_state(tmp_path, monkeypatch):
    async def run():
        async with served(tmp_path) as (bridge, http, base, _port):
            session = bridge.auth.complete_pairing(bridge.auth.new_pairing_challenge())
            headers = {"X-DT-Session": session.session_id, "X-DT-Token": session.token}
            original = draft_snapshot.write_json_atomic
            def fail(*args, **kwargs):
                raise OSError("disk full")
            monkeypatch.setattr(draft_snapshot, "write_json_atomic", fail)
            async with http.post(base + "/v3/device/remember", headers=headers, json={}) as res:
                assert res.status == 507
                body = await res.json()
                assert body["remembered"] is False and "device_secret" not in body
            assert not session.remembered and not session.device_secret_once
            monkeypatch.setattr(draft_snapshot, "write_json_atomic", original)
            async with http.post(base + "/v3/device/remember", headers=headers, json={}) as res:
                assert res.status == 200 and (await res.json())["device_secret"]
            monkeypatch.setattr(draft_snapshot, "write_json_atomic", fail)
            async with http.post(base + "/v3/grants", json={"session_id": session.session_id, "allow_insert": True}) as res:
                assert res.status == 507
            assert not session.allow_insert
            async with http.post(base + "/v3/sessions/revoke", json={"session_id": session.session_id}) as res:
                assert res.status == 507
            assert session.session_id in bridge.auth.sessions and session.device_id in bridge.auth.trusted
    asyncio.run(run())
