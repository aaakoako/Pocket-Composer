"""A09: remember device is explicit, persistable, and revocable."""
from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp import ClientSession
from aiohttp.web import AppRunner, TCPSite

from doubao_typeless.app import V3App
from doubao_typeless.core.bundle import Draft
from doubao_typeless.services.bridge_v3 import V3Bridge
from doubao_typeless.storage.asset_store import AssetStore
from doubao_typeless.storage.credentials import REMEMBER_TTL_S, AuthService
from doubao_typeless.ui.desktop import STYLESHEET


async def _serve(tmp_path, auth=None):
    auth = auth or AuthService(store_path=tmp_path / "trusted_devices.json")
    draft = Draft("d", "e", 0, "phone", "")
    bridge = V3Bridge(port=0, auth=auth, store=AssetStore(tmp_path / "assets"), draft=draft)
    runner = AppRunner(bridge.make_app())
    await runner.setup()
    site = TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return auth, bridge, runner, port


def test_remember_then_resume_without_code(tmp_path):
    auth = AuthService(store_path=tmp_path / "trusted_devices.json")
    code = auth.new_pairing_challenge()
    session = auth.complete_pairing(code, allow_insert=True)
    secret = auth.remember_device(session)
    assert session.remembered is True
    assert REMEMBER_TTL_S >= 29 * 24 * 3600
    later = AuthService(store_path=tmp_path / "trusted_devices.json")
    resumed = later.resume_trusted(session.device_id, secret)
    assert resumed.device_id == session.device_id
    assert resumed.allow_insert is True
    assert resumed.session_id != session.session_id
    with pytest.raises(ValueError, match="mismatch"):
        later.resume_trusted(session.device_id, "wrong-secret")


def test_revoke_blocks_remembered_resume(tmp_path):
    auth = AuthService(store_path=tmp_path / "trusted_devices.json")
    session = auth.complete_pairing(auth.new_pairing_challenge())
    secret = auth.remember_device(session)
    assert auth.revoke(session.session_id) is True
    later = AuthService(store_path=tmp_path / "trusted_devices.json")
    with pytest.raises(ValueError, match="expired"):
        later.resume_trusted(session.device_id, secret)


def test_expired_trusted_device_is_rejected(tmp_path):
    auth = AuthService(store_path=tmp_path / "trusted_devices.json")
    session = auth.complete_pairing(auth.new_pairing_challenge())
    secret = auth.remember_device(session)
    auth.trusted[session.device_id].expires_at = time.time() - 1
    auth._save_trusted()
    later = AuthService(store_path=tmp_path / "trusted_devices.json")
    with pytest.raises(ValueError, match="expired"):
        later.resume_trusted(session.device_id, secret)


def test_app_remember_connected_is_explicit(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    session = app.auth.complete_pairing(app.auth.new_pairing_challenge())
    assert session.remembered is False
    assert app.remember_connected() == 1
    assert session.remembered is True
    assert app.auth.take_device_secret(session)


def test_pair_resume_http_and_secret_retried_until_ack(tmp_path):
    async def run():
        auth, _bridge, runner, port = await _serve(tmp_path)
        try:
            session = auth.complete_pairing(auth.new_pairing_challenge(), allow_insert=True)
            secret = auth.remember_device(session)
            async with ClientSession() as client:
                once = await client.get(
                    f"http://127.0.0.1:{port}/v3/device/secret",
                    headers={"X-DT-Session": session.session_id, "X-DT-Token": session.token},
                )
                body = await once.json()
                assert body["device_secret"] == secret
                again = await client.get(
                    f"http://127.0.0.1:{port}/v3/device/secret",
                    headers={"X-DT-Session": session.session_id, "X-DT-Token": session.token},
                )
                retry = await again.json()
                assert retry["device_secret"] == secret
                headers = {"X-DT-Session": session.session_id, "X-DT-Token": session.token,
                           "Origin": f"http://127.0.0.1:{port}"}
                rejected = await client.post(f"http://127.0.0.1:{port}/v3/device/secret/ack",
                                             json={"device_secret": "wrong"}, headers=headers)
                assert rejected.status == 400
                assert auth.take_device_secret(session) == secret
                for _ in range(2):
                    ack = await client.post(f"http://127.0.0.1:{port}/v3/device/secret/ack",
                                            json={"device_secret": secret}, headers=headers)
                    assert ack.status == 200
                cleared = await client.get(f"http://127.0.0.1:{port}/v3/device/secret", headers=headers)
                assert "device_secret" not in await cleared.json()
                resumed = await client.post(
                    f"http://127.0.0.1:{port}/v3/pair",
                    json={"device_id": session.device_id, "device_secret": secret},
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                data = await resumed.json()
                assert resumed.status == 200
                assert data["device_id"] == session.device_id
                assert data["remembered"] is True
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_resume_preserves_one_device_grants_and_revoke_all_sessions(tmp_path):
    auth = AuthService(store_path=tmp_path / "trusted_devices.json")
    first = auth.complete_pairing(auth.new_pairing_challenge(), allow_insert=True)
    secret = auth.remember_device(first)
    second = auth.resume_trusted(first.device_id, secret)
    third = auth.resume_trusted(first.device_id, secret)
    assert len(auth.public_sessions()) == 1
    assert auth.public_sessions()[0]["session_id"] == third.session_id
    assert not auth.take_device_secret(first)
    auth.set_grants(third.session_id, allow_capture=True)
    assert first.allow_capture and second.allow_capture and third.allow_capture
    assert auth.revoke(second.session_id)
    assert not auth.public_sessions()
    for session in (first, second, third):
        with pytest.raises(ValueError, match="expired"):
            auth.authorize(session.session_id, session.token, "sync")
    with pytest.raises(ValueError, match="expired"):
        auth.resume_trusted(first.device_id, secret)


def test_stylesheet_has_hover_focus_tab_underline():
    assert "QPushButton:hover" in STYLESHEET
    assert "QPushButton:pressed" in STYLESHEET
    assert "QPushButton:disabled" in STYLESHEET
    assert "QPushButton:focus" in STYLESHEET
    assert "QTabBar::tab:selected" in STYLESHEET
    assert "border-bottom: 2px solid" in STYLESHEET
    assert "QListWidget::item:selected" in STYLESHEET
