"""Production-module counterexamples from independent review 8326bb4.

These import the live package, not the review-zip archive. They do not
operate a real screen, Cursor, or Android device.
"""
from __future__ import annotations

import asyncio
import types
from types import SimpleNamespace as N

from aiohttp import ClientSession

from doubao_typeless.app import V3App
from doubao_typeless.core.attempt import Attempt
from doubao_typeless.core.bundle import apply_draft_update
from doubao_typeless.services.delivery import DeliveryService
from doubao_typeless.storage.secret_store import put_secret
from tests.test_v3_s1_commands import _stub_delivery
from tests.test_v3_s4_conflict import _pair


def _attempt() -> Attempt:
    return Attempt("a", "i", "b", "adapter")


def test_core_still_rejects_stale_revision(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    app.draft.text = "新稿B"
    app.draft.revision = 10
    apply_draft_update(app.draft, {"text": "旧稿A", "revision": 2, "asset_refs": []})
    assert app.draft.text == "新稿B"
    assert app.draft.revision == 10


def test_app_must_not_upgrade_stale_update_into_latest(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    app.draft.text = "新稿B"
    app.draft.revision = 10
    app.draft.epoch = "epoch-new"
    ack = app.apply_phone_update({"text": "旧稿A", "revision": 2, "epoch": "epoch-old", "asset_refs": []})
    assert app.draft.text == "新稿B"
    assert app.draft.revision == 10
    assert ack.get("durable") is False


def test_client_supplied_asset_objects_must_not_bypass_resolution(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    app.draft.text = "hello"
    app.draft.revision = 10
    accepted = False
    try:
        app.apply_phone_update(
            {
                "text": "hello",
                "revision": 11,
                "asset_refs": ["not-owned"],
                "assets": [{"asset_id": "not-owned", "bytes": 1}],
            }
        )
        accepted = True
    except ValueError:
        pass
    assert not accepted
    assert app.draft.assets == []


def test_focus_change_during_modifier_wait_must_stop_paste():
    focus, pasted = [("ComposerPane", "chatinput")], []

    def wait():
        focus[0] = ("Scintilla", "code")
        return True

    svc = DeliveryService(
        paste=lambda: pasted.append(focus[0]),
        set_clipboard_image=lambda _: None,
        set_clipboard_text=lambda _: None,
        read_focus=lambda: focus[0],
        observe_image=lambda: "observed",
        observe_text=lambda: "unknown",
        wait_modifiers=wait,
    )
    result = svc.run(_attempt(), {"text": "must not land in code", "assets": []})
    assert pasted == []
    assert result.error_code == "TARGET_CHANGED"


def test_partial_delivery_must_not_be_reported_as_zero_steps():
    focus, pasted = [("ComposerPane", "chatinput")], []
    sequence = iter([True, False])
    svc = DeliveryService(
        paste=lambda: pasted.append("paste"),
        set_clipboard_image=lambda _: None,
        set_clipboard_text=lambda _: None,
        read_focus=lambda: focus[0],
        observe_image=lambda: "observed",
        observe_text=lambda: "unknown",
        wait_modifiers=lambda: next(sequence),
    )
    result = svc.run(
        _attempt(),
        {
            "text": "later",
            "assets": [
                {"asset_id": "image-a", "bytes_data": b"a"},
                {"asset_id": "image-b", "bytes_data": b"b"},
            ],
        },
    )
    assert len(pasted) == 1
    assert result.result != "NO_STEPS"
    assert result.steps


def test_native_region_route_must_pass_granted_session(tmp_path, monkeypatch):
    region = types.ModuleType("doubao_typeless.ui.region")
    region.select_region = lambda: (0, 0, 100, 100)
    monkeypatch.setitem(__import__("sys").modules, region.__name__, region)
    app = V3App(data_dir=tmp_path / "data", port=0)
    # 截图只发给在线、未过期、有截图权限的手机；桩会话需具备这些属性。
    app.auth.sessions["s"] = N(allow_capture=True, session_id="s", device_id="d", expires_at=__import__("time").time() + 60)
    monkeypatch.setattr(app.bridge, "online_device_ids", lambda: {"d"})
    seen = {}

    def capture(scope, request_id="", session=None):
        seen["session"] = session
        if session is None:
            raise ValueError("no session")
        return {"width": 100, "height": 100}

    app._on_capture = capture
    app.capture_region()
    assert seen.get("session") is not None


def test_recall_hotkey_must_execute_or_offer_recovery(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    app.draft.text = "新稿B"
    app.history.record({"bundle_id": "A", "text": "上次A", "assets": []}, attempt_result="UNKNOWN")
    app._last_attempt = _attempt()
    app._last_attempt.result = "UNKNOWN"
    actions = []
    app._notify_ui = lambda name, **kw: actions.append(name)
    app.recall_last()
    assert "recovery_ask" in actions
    assert app.draft.text == "新稿B"
    assert app.bridge.last_bundle["text"] == "上次A"


def test_clipboard_interference_must_not_be_overwritten_in_finalizer(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    clipboard = ["user freshly copied something else"]
    app._set_text = lambda text: clipboard.__setitem__(0, text)
    app._after_insert(
        {"text": "old attempted text"},
        {"result": "UNKNOWN", "error_code": "CLIPBOARD_INTERFERENCE", "steps": []},
    )
    assert clipboard[0] == "user freshly copied something else"


def test_credential_failure_must_not_fall_back_to_plaintext(tmp_path, monkeypatch):
    monkeypatch.delenv("DT_V3_SECRET_FILE", raising=False)
    cred = types.ModuleType("win32cred")
    cred.CRED_TYPE_GENERIC = 1

    def fail(*_a, **_k):
        raise OSError("simulated OS vault unavailable")

    cred.CredWrite = fail
    monkeypatch.setitem(__import__("sys").modules, "win32cred", cred)
    import doubao_typeless.storage.secret_store as secrets

    monkeypatch.setattr(secrets.sys, "platform", "win32")
    result = secrets.put_secret(tmp_path, "byok_api_key", "DUMMY-TEST-KEY-NOT-A-SECRET")
    path = tmp_path / "secrets" / "byok_api_key.txt"
    assert result == "memory"
    assert not path.exists()


def test_same_origin_lan_get_must_reach_session_authorization(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    called = []

    async def handler(_req):
        called.append(True)
        from aiohttp import web

        return web.Response(status=200)

    request = N(
        remote="192.168.50.7",
        path="/v3/assets/image-a",
        method="GET",
        headers={
            "Host": "192.168.50.2:8766",
            "X-DT-Session": "controlled-session",
            "X-DT-Token": "test-token",
        },
    )
    response = asyncio.run(app.bridge._origin_host_gate(request, handler))
    assert called
    assert response.status == 200


def test_phone_insert_rotates_matching_draft_and_keeps_later_b(tmp_path):
    app = V3App(data_dir=tmp_path / "data", port=0)
    pasted: list[str] = []
    _stub_delivery(app, pasted)
    app.start_background(start_hud=False)
    try:

        async def run():
            async with ClientSession() as session:
                creds = await _pair(session, app.port, app.auth)
                async with session.ws_connect(f"http://127.0.0.1:{app.port}/ws") as ws:
                    await ws.send_json(
                        {
                            "type": "session.hello",
                            "session_id": creds["session_id"],
                            "token": creds["token"],
                        }
                    )
                    ready = await ws.receive_json()
                    assert ready["type"] == "session.ready"
                    await ws.send_json(
                        {
                            "type": "draft.update",
                            "text": "段落A",
                            "revision": 1,
                            "asset_refs": [],
                            "draft_id": ready["draft_id"],
                            "epoch": ready["epoch"],
                        }
                    )
                    assert (await ws.receive_json())["type"] == "draft.ack"
                    nonce = await (
                        await session.post(
                            f"http://127.0.0.1:{app.port}/v3/nonce",
                            json=creds,
                        )
                    ).json()
                    await ws.send_json(
                        {
                            "type": "insert.intent",
                            "session_id": creds["session_id"],
                            "token": creds["token"],
                            "nonce": nonce["nonce"],
                            "intent_id": "phone-a",
                        }
                    )
                    status = await ws.receive_json()
                    assert status["type"] == "attempt.status"
                    rotated = await ws.receive_json()
                    assert rotated["type"] == "draft.rotated"
                    assert rotated["archived"]["text"] == "段落A"
                    assert app.draft.text == ""
                    await ws.send_json(
                        {
                            "type": "draft.update",
                            "text": "段落B",
                            "revision": app.draft.revision + 1,
                            "asset_refs": [],
                            "draft_id": app.draft.draft_id,
                            "epoch": app.draft.epoch,
                        }
                    )
                    assert (await ws.receive_json())["type"] == "draft.ack"
                    assert app.draft.text == "段落B"

        asyncio.run(run())
    finally:
        asyncio.run_coroutine_threadsafe(app.stop(), app._loop).result(5)


def test_cross_host_save_does_not_reuse_old_key(tmp_path):
    from doubao_typeless.storage.settings_store import load_settings, save_settings

    save_settings(tmp_path, {"byok_endpoint": "https://old.example/v1", "byok_api_key": "sk-old"})
    save_settings(tmp_path, {"byok_endpoint": "https://new.example/v1", "byok_api_key": "sk-old"})
    assert load_settings(tmp_path)["byok_api_key"] == ""


def test_byok_default_prompt_is_a_real_task():
    from doubao_typeless.services.byok import DEFAULT_POLISH_PROMPT, ByokService

    seen = {}

    def post(_url, body, _headers):
        seen["body"] = body
        return {"choices": [{"message": {"content": "整理后"}}]}

    svc = ByokService(endpoint="https://example.invalid/v1", api_key="sk", model="m", post=post)
    out = svc.polish("口误", draft_id="d", revision=1, current_draft_id="d", current_revision=1)
    assert seen["body"]["messages"][0]["content"] == DEFAULT_POLISH_PROMPT
    assert out["text"] == "整理后"
