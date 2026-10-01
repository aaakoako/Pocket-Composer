"""Opus 5.5 审查修复：服务端/桌面纯逻辑回归（单元级，不开浏览器、不注册热键、不发键）。"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from doubao_typeless.core.intent import IntentLedger
from doubao_typeless.services import bridge_v3
from doubao_typeless.services.command_queue import CommandQueue
from doubao_typeless.services.v3_update import prune_update_staging
from doubao_typeless.storage import draft_snapshot
from doubao_typeless.storage.asset_store import AssetStore
from doubao_typeless.storage.credentials import (
    PAIR_SOURCE_LIMIT, SHORT_CODE_CHALLENGE_LIMIT, AuthService, PairingError, TrustStoreError,
    validate_client_message)
from doubao_typeless.storage.upgrade import prepare_upgrade


# ---- O-P1-1：按协议结构拒绝按键脚本，正文/图注/回执里的命令名是普通内容 ----

@pytest.mark.parametrize("text", ["请检查这个 PowerShell 报错", "cmd.exe /c dir", "SendInput 和 keybd_event 的区别", "shell 脚本"])
def test_technical_words_in_text_and_captions_are_ordinary_content(text):
    update = {"type": "draft.update", "protocol": 3, "text": text, "revision": 2, "draft_id": "d", "epoch": "e",
              "update_id": "u1", "captions": [text], "asset_refs": ["a1"],
              "asset_documents": [{"id": "l1", "asset_id": "a1", "status": "ready", "render_revision": 1, "caption": text}]}
    assert validate_client_message(update) is None
    intent = {"type": "insert.intent", "text": text, "intent_id": "i", "nonce": "n", "session_id": "s", "token": "t"}
    assert validate_client_message(intent) is None


@pytest.mark.parametrize("payload, code", [
    ({"type": "draft.update", "keys": ["ctrl", "v"]}, "forbidden payload"),
    ({"type": "insert.intent", "vk": 13}, "forbidden payload"),
    ({"type": "draft.update", "x": 1, "y": 2}, "forbidden payload"),
    ({"type": "keyboard.send", "text": "a"}, "UNSUPPORTED_MESSAGE"),
    ({"type": "draft.update", "text": "a", "script": "x"}, "UNEXPECTED_FIELD"),
    ({"type": "draft.update", "text": 3}, "INVALID_MESSAGE"),
    ({"type": "draft.update", "asset_documents": [{"id": "a", "keys": "x"}]}, "INVALID_MESSAGE"),
    ({"type": "draft.update", "takeover": {"draft_id": "d", "vk": 1}}, "INVALID_MESSAGE"),
    ([], "INVALID_MESSAGE"),
])
def test_structural_key_scripts_and_unknown_shapes_still_rejected(payload, code):
    assert validate_client_message(payload) == code


def test_error_reply_echoes_correlation_ids():
    bridge = object.__new__(bridge_v3.V3Bridge)
    reply = bridge._error_reply({"type": "draft.update", "update_id": "u7", "intent_id": "i7", "text": "正文"},
                                "UNEXPECTED_FIELD")
    assert reply == {"type": "error", "error": "UNEXPECTED_FIELD", "update_id": "u7", "intent_id": "i7",
                     "request_type": "draft.update"}
    assert bridge._error_reply(["bad"], "INVALID_MESSAGE") == {"type": "error", "error": "INVALID_MESSAGE"}


# ---- O-P1-2：NO_STEPS/CANCELLED 可由用户再点重试；可能已送达的结果永不重放 ----

@pytest.mark.parametrize("result", ["NO_STEPS", "CANCELLED"])
def test_no_steps_intent_can_be_retried_by_explicit_click(result):
    ledger = IntentLedger()
    assert ledger.begin("i") == "accept"
    ledger.finish("i", result)
    assert ledger.begin("i") == "accept"


@pytest.mark.parametrize("result", ["UNKNOWN", "PARTIAL", "CONFIRMED", "RUNNING"])
def test_possibly_delivered_intent_is_never_replayed(result):
    ledger = IntentLedger()
    assert ledger.begin("i") == "accept"
    if result != "RUNNING":
        ledger.finish("i", result)
    assert ledger.begin("i") in {"duplicate", "busy"}
    if result != "RUNNING":
        assert ledger.begin("i") == "duplicate"


def test_retry_needs_fresh_nonce_so_network_replay_cannot_reinject():
    auth = AuthService()
    session = auth.complete_pairing(auth.new_pairing_challenge())
    nonce = auth.issue_nonce(session)
    auth.consume_nonce(session, nonce)
    with pytest.raises(ValueError):
        auth.consume_nonce(session, nonce)


# ---- O-P1-3：新配对不能凭旧 device_id 夺权；有效旧会话证明才延续身份 ----

def test_repair_with_valid_previous_session_keeps_device_and_grants():
    auth = AuthService()
    first = auth.complete_pairing(auth.new_pairing_challenge(), allow_insert=True, allow_capture=True)
    again = auth.complete_pairing(auth.new_pairing_challenge(),
                                  previous={"session_id": first.session_id, "token": first.token})
    assert again.device_id == first.device_id and again.session_id != first.session_id
    assert again.allow_insert and again.allow_capture


@pytest.mark.parametrize("previous", [
    {"device_id": "victim"},
    {"session_id": "forged", "token": "x"},
    "not-a-dict",
])
def test_repair_without_valid_proof_gets_new_identity(previous):
    auth = AuthService()
    victim = auth.complete_pairing(auth.new_pairing_challenge(), allow_insert=True)
    if isinstance(previous, dict) and previous.get("device_id"):
        previous = {"device_id": victim.device_id, "session_id": victim.session_id}
    other = auth.complete_pairing(auth.new_pairing_challenge(), previous=previous)
    assert other.device_id != victim.device_id
    assert not other.allow_insert


def test_wrong_token_for_real_session_is_not_proof():
    auth = AuthService()
    victim = auth.complete_pairing(auth.new_pairing_challenge())
    other = auth.complete_pairing(auth.new_pairing_challenge(),
                                  previous={"session_id": victim.session_id, "token": "guess"})
    assert other.device_id != victim.device_id


def test_foreign_asset_refs_are_rejected_and_claim_needs_desktop_approval(tmp_path, monkeypatch):
    app = _app(tmp_path)
    try:
        owner = app.auth.complete_pairing(app.auth.new_pairing_challenge())
        stranger = app.auth.complete_pairing(app.auth.new_pairing_challenge())
        app.db.upsert_asset("asset-a", "sha-a", 10, owner_session_id=owner.session_id)
        with pytest.raises(bridge_v3.AssetOwnerError) as caught:
            app.bridge._validate_ref_owners({"asset_documents": [{"id": "l", "asset_id": "asset-a"}]}, stranger)
        assert caught.value.asset_ids == ["asset-a"]
        app.bridge._validate_ref_owners({"asset_refs": ["asset-a"]}, owner)

        online = {owner.device_id, stranger.device_id}
        monkeypatch.setattr(app.bridge, "online_device_ids", lambda: set(online))
        # 原手机在线：别的手机不能认领它的图。
        assert app.bridge._request_asset_claim(stranger, ["asset-a"]) == []
        online.discard(owner.device_id)
        claims = []
        app.bridge._on_asset_claim = lambda device, ids: claims.append((device, ids))
        assert app.bridge._request_asset_claim(stranger, ["asset-a"]) == ["asset-a"]
        assert claims == [(stranger.device_id, ["asset-a"])]
        assert app.db.asset_by_id("asset-a")["owner_session_id"] == owner.session_id  # 未批准不转移
        sent = []
        async def record(device_id, payload):
            sent.append((device_id, payload)); return 1
        monkeypatch.setattr(app.bridge, "send_to_device", record)
        monkeypatch.setattr(app.bridge, "_device_sockets", lambda device_id: [object()])
        # 批准前原手机回来了：拒绝转移。
        online.add(owner.device_id)
        assert asyncio.run(app.bridge.resolve_asset_claim(stranger.device_id, True)) == []
        assert sent[-1][1]["type"] == "asset.claim_denied"
        online.discard(owner.device_id)
        app.bridge._request_asset_claim(stranger, ["asset-a"])
        assert asyncio.run(app.bridge.resolve_asset_claim(stranger.device_id, False)) == []
        app.bridge._request_asset_claim(stranger, ["asset-a"])
        assert asyncio.run(app.bridge.resolve_asset_claim(stranger.device_id, True)) == ["asset-a"]
        assert sent[-1] == (stranger.device_id, {"type": "asset.claimed", "asset_ids": ["asset-a"]})
        app.bridge._validate_ref_owners({"asset_refs": ["asset-a"]}, stranger)
        with pytest.raises(bridge_v3.AssetOwnerError):
            app.bridge._validate_ref_owners({"asset_refs": ["asset-a"]}, owner)
    finally:
        _close(app)


# ---- O-P1-4：配对错误限流、锁定，合法二维码不因猜错作废 ----

def test_wrong_codes_lock_source_but_keep_qr_valid():
    auth = AuthService()
    long_code = auth.new_pairing_challenge()
    short = auth.current_short_code()
    wrong = "0000" if short != "0000" else "0001"
    for _ in range(PAIR_SOURCE_LIMIT - 1):
        with pytest.raises(PairingError) as caught:
            auth.complete_pairing(wrong, source="10.0.0.9")
        assert caught.value.code in {"PAIRING_MISMATCH", "SHORT_CODE_DISABLED"}
    with pytest.raises(PairingError):
        auth.complete_pairing(wrong, source="10.0.0.9")
    with pytest.raises(PairingError) as locked:
        auth.complete_pairing(long_code, source="10.0.0.9")
    assert locked.value.code == "PAIRING_LOCKED" and locked.value.retry_after > 0
    # 另一台手机扫同一个二维码仍然成功：攻击者猜错不使合法二维码失效。
    assert auth.complete_pairing(long_code, source="10.0.0.20").device_id


def test_refreshing_challenge_does_not_reset_lockout_or_short_budget():
    auth = AuthService()
    for _ in range(PAIR_SOURCE_LIMIT):
        auth.new_pairing_challenge()
        wrong = "0000" if auth.current_short_code() != "0000" else "0001"
        with pytest.raises(PairingError):
            auth.complete_pairing(wrong, source="attacker")
    auth._challenge = None
    code = auth.new_pairing_challenge()
    with pytest.raises(PairingError) as caught:
        auth.complete_pairing(code, source="attacker")
    assert caught.value.code == "PAIRING_LOCKED"


def test_short_code_disables_after_budget_and_rotate_restores():
    auth = AuthService()
    auth.new_pairing_challenge()
    real = auth.current_short_code()
    wrong = [f"{n:04d}" for n in range(10000) if f"{n:04d}" != real]
    for index in range(SHORT_CODE_CHALLENGE_LIMIT):
        with pytest.raises(PairingError):
            auth.complete_pairing(wrong[index], source=f"phone-{index}")
    assert auth.current_short_code() is None and not auth.short_code_available()
    with pytest.raises(PairingError) as caught:
        auth.complete_pairing(real, source="honest")
    assert caught.value.code == "SHORT_CODE_DISABLED"
    long_code = auth.current_pairing_challenge()
    assert auth.complete_pairing(long_code, source="honest")
    auth.rotate_pairing_challenge()
    assert auth.current_short_code() is not None
    assert auth.complete_pairing(auth.current_short_code(), source="honest")


# ---- V3：信任保存事务性 ----

def _failing_store(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(draft_snapshot, "write_json_atomic", fail)


def test_remember_failure_exposes_no_secret_and_can_retry(tmp_path, monkeypatch):
    auth = AuthService(store_path=tmp_path / "trusted.json")
    session = auth.complete_pairing(auth.new_pairing_challenge())
    original = draft_snapshot.write_json_atomic
    _failing_store(monkeypatch)
    with pytest.raises(TrustStoreError):
        auth.remember_device(session)
    assert not session.remembered and not session.device_secret_once and session.device_id not in auth.trusted
    assert auth.public_sessions()[0]["remembered"] is False
    monkeypatch.setattr(draft_snapshot, "write_json_atomic", original)
    secret = auth.remember_device(session)
    assert secret and session.remembered and session.device_id in auth.trusted
    assert AuthService(store_path=tmp_path / "trusted.json").trusted[session.device_id]


def test_grant_and_revoke_failures_roll_back(tmp_path, monkeypatch):
    auth = AuthService(store_path=tmp_path / "trusted.json")
    session = auth.complete_pairing(auth.new_pairing_challenge())
    auth.remember_device(session)
    _failing_store(monkeypatch)
    with pytest.raises(TrustStoreError):
        auth.set_grants(session.session_id, allow_insert=True)
    assert not session.allow_insert and not auth.trusted[session.device_id].allow_insert
    with pytest.raises(TrustStoreError):
        auth.forget_device(session.device_id)
    assert session.session_id in auth.sessions and session.device_id in auth.trusted
    with pytest.raises(TrustStoreError):
        auth.unremember_device(session.device_id)
    assert session.remembered and session.device_id in auth.trusted


def test_unremember_only_affects_that_device(tmp_path):
    auth = AuthService(store_path=tmp_path / "trusted.json")
    a = auth.complete_pairing(auth.new_pairing_challenge())
    b = auth.complete_pairing(auth.new_pairing_challenge())
    auth.remember_device(a); auth.remember_device(b)
    assert auth.unremember_device(a.device_id)
    assert not a.remembered and a.session_id in auth.sessions and a.expires_at <= time.time() + auth.session_ttl_s + 1
    assert b.remembered and b.device_id in auth.trusted
    assert set(AuthService(store_path=tmp_path / "trusted.json").trusted) == {b.device_id}


# ---- P2：资产路径限制 ----

@pytest.mark.parametrize("asset_id", ["..\\review-canary", "../x", "a/b", "", "a" * 129, "名字"])
def test_asset_store_refuses_ids_outside_asset_dir(tmp_path, asset_id):
    (tmp_path / "review-canary.bin").write_bytes(b"owned")
    store = AssetStore(tmp_path / "assets")
    with pytest.raises(FileNotFoundError):
        store.get(asset_id)
    assert store.meta(asset_id) == {}


def test_heartbeat_detects_half_open_sockets():
    assert 0 < bridge_v3.WS_HEARTBEAT_S <= 30


# ---- P2：截图只发当前有效目标；练习框不制造隐藏电脑副本 ----

def _app(tmp_path):
    from doubao_typeless.app import V3App
    return V3App(data_dir=tmp_path / "data", port=0)


def _close(app):
    app._commands.close(1)
    app.db.conn.close()
    lock = getattr(app, "_lock", None)
    if lock:
        lock.release()


def test_capture_target_prefers_online_owner_and_refuses_ambiguity(tmp_path, monkeypatch):
    app = _app(tmp_path)
    try:
        a = app.auth.complete_pairing(app.auth.new_pairing_challenge(), allow_capture=True)
        b = app.auth.complete_pairing(app.auth.new_pairing_challenge(), allow_capture=True)
        online = {a.device_id, b.device_id}
        monkeypatch.setattr(app.bridge, "online_device_ids", lambda: set(online))
        app.draft.authority = "pc"
        assert app.capture_target() == (None, "CAPTURE_TARGET_AMBIGUOUS")
        app.draft.authority = "phone"; app.draft.editor_device_id = b.device_id
        assert app.capture_target()[0].device_id == b.device_id
        app.auth.set_grants(b.session_id, allow_capture=False)
        assert app.capture_target() == (None, "CAPTURE_DENIED")
        online.discard(b.device_id)
        assert app.capture_target()[0].device_id == a.device_id
        a.expires_at = time.time() - 1
        assert app.capture_target() == (None, "CAPTURE_NO_PHONE")
    finally:
        _close(app)


def test_practice_box_cannot_shadow_phone_primary_draft(tmp_path):
    app = _app(tmp_path)
    try:
        app.draft.authority = "phone"; app.draft.text = "手机主稿"
        assert app.update_practice_text("练习") is False and app.draft.text == "手机主稿"
        app.draft.authority = "pc"
        assert app.update_practice_text("练习") is True and app.draft.text == "练习"
    finally:
        _close(app)


# ---- P2：退出等待失败后继续可用 ----

def test_queue_reopen_after_close_timeout_rejects_overlap_then_serves():
    started, release = threading.Event(), threading.Event()
    q = CommandQueue()
    q.submit(lambda: (started.set(), release.wait(5)))
    assert started.wait(2)
    assert q.close(0.05) is False
    assert q.submit(lambda: 1).result(1)["error_code"] == "SHUTTING_DOWN"
    q.reopen()
    assert q.submit(lambda: 1).result(1)["error_code"] == "BUSY"
    release.set()
    for _ in range(100):
        future = q.submit(lambda: "ok")
        value = future.result(2)
        if value == "ok":
            break
        time.sleep(.02)
    assert value == "ok"
    assert q.close(2) is True


def test_failed_stop_restores_hotkeys_and_cancels_abandoned_delivery(tmp_path, monkeypatch):
    app = _app(tmp_path)
    started, release = threading.Event(), threading.Event()
    applied = []
    try:
        monkeypatch.setattr(app, "apply_hotkeys", lambda *args, **kw: applied.append((args, kw)) or [])
        app._hotkey_combos = ("<alt>+i", "<alt>+<shift>+i", "<alt>+<shift>+e", "<alt>+<shift>+s")
        app._commands.submit(lambda: (started.set(), release.wait(5)))
        assert started.wait(2)
        original = app._commands.close
        monkeypatch.setattr(app._commands, "close", lambda timeout=5.0: original(0.05))
        assert asyncio.run(app.stop()) is False
        assert app._stopping is False and app._delivery_cancelled() is True
        assert applied and applied[0][0] == ("<alt>+i", "<alt>+<shift>+i")
        release.set()
        for _ in range(100):
            if app._commands.submit(lambda: None).result(2) is None:
                break
            time.sleep(.02)
        assert app._delivery_cancelled() is False
    finally:
        release.set()
        _close(app)


# ---- P2：升级备份排除更新缓存、失败回滚、清理旧暂存 ----

def test_upgrade_backup_skips_update_cache_and_removes_partial_backup(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "updates" / ("a" * 32)).mkdir(parents=True)
    (data / "updates" / ("a" * 32) / "DoubaoTypeless.exe").write_bytes(b"x" * 1024)
    (data / "config.json").write_text("{}", encoding="utf-8")
    backup = prepare_upgrade(data, version="9.9.9")
    assert backup and (backup / "config.json").is_file() and not (backup / "updates").exists()

    (data / "workspace-version.json").unlink()
    import shutil
    def broken(src, dst, *args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(shutil, "copy2", broken)
    with pytest.raises(OSError):
        prepare_upgrade(data, version="9.9.10")
    backups = sorted((data / "upgrade-backups").iterdir())
    assert backups == [backup]
    assert (data / "config.json").read_text(encoding="utf-8") == "{}"
    assert not (data / "workspace-version.json").exists()


def test_prune_update_staging_keeps_recent_foreign_and_locked(tmp_path):
    root = tmp_path / "updates"
    old, recent, foreign = root / ("b" * 32), root / ("c" * 32), root / "keep-me"
    for item in (old, recent, foreign):
        item.mkdir(parents=True)
        (item / "pkg.exe").write_bytes(b"1")
    now = time.time()
    os.utime(old, (now - 3600, now - 3600)); os.utime(foreign, (now - 3600, now - 3600))
    nested = root / ("d" * 32)
    (nested / "inner").mkdir(parents=True)
    os.utime(nested, (now - 3600, now - 3600))
    assert prune_update_staging(tmp_path, now=now) == 1
    assert not old.exists() and recent.exists() and foreign.exists() and nested.exists()
