"""投递回执与意图结果的持久性：先落盘再发网络，重启后同一意图不重贴。平台按键为替身。"""
from __future__ import annotations

import json

import pytest

import doubao_typeless.storage.draft_snapshot as snapshots
from doubao_typeless.app import V3App
from doubao_typeless.core.bundle import freeze_bundle
from tests.test_v3_assistant_delivery import platform
from tests.test_v3_phone_primary import msg


def _open(path):
    return V3App(data_dir=path, port=0)


def _close(a):
    a._commands.close(3)
    a.db.conn.close()
    a._lock.release()


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / "isolated"


def _remote_delivery(a, intent_id="intent-1"):
    written = platform(a)
    a.apply_phone_update(msg("手机稿"))
    bundle = freeze_bundle(a.draft, bundle_id=f"bundle-{intent_id}")
    result = a.deliver_and_finish({"intent_id": intent_id, "_source": "remote"}, bundle)
    return result, bundle, written


def test_remote_receipt_is_on_disk_when_delivery_returns(data_dir):
    a = _open(data_dir)
    try:
        sent = []

        async def must_not_publish(*_a, **_k):
            sent.append(1)

        a.bridge.publish_phone_event = must_not_publish
        result, _bundle, written = _remote_delivery(a)
        assert written == ["手机稿"] and result["receipt_saved"] is True
        saved = json.loads((data_dir / "phone-event.json").read_text(encoding="utf-8"))
        assert saved["intent_id"] == "intent-1" and saved == result["phone_event"]
        assert saved["archived"]["text"] == "手机稿"
        assert not sent  # 回执落盘不依赖网络广播。
    finally:
        _close(a)


def test_restart_keeps_receipt_and_rejects_same_intent_without_typing(data_dir):
    a = _open(data_dir)
    try:
        first, bundle, written = _remote_delivery(a)
        assert written == ["手机稿"]
    finally:
        _close(a)
    b = _open(data_dir)
    try:
        assert b.ledger.status("intent-1") == first["result"]
        again = platform(b)
        replay = b.deliver_and_finish({"intent_id": "intent-1", "_source": "remote"}, bundle)
        assert replay["duplicate"] and replay["result"] == first["result"]
        assert again == []
        saved = json.loads((data_dir / "phone-event.json").read_text(encoding="utf-8"))
        assert saved["intent_id"] == "intent-1"
    finally:
        _close(b)


def test_intent_running_at_crash_is_unknown_after_restart_and_never_replayed(data_dir):
    a = _open(data_dir)
    a.db.record_intent("crashed", "RUNNING")
    _close(a)
    b = _open(data_dir)
    try:
        written = platform(b)
        b.apply_phone_update(msg("中途退出的稿"))
        bundle = freeze_bundle(b.draft, bundle_id="bundle-crashed")
        replay = b.deliver_and_finish({"intent_id": "crashed", "_source": "remote"}, bundle)
        assert replay["duplicate"] and replay["result"] == "UNKNOWN" and written == []
    finally:
        _close(b)


def test_no_steps_intent_stays_retryable_after_restart(data_dir):
    a = _open(data_dir)
    a.db.record_intent("safe-retry", "NO_STEPS")
    _close(a)
    b = _open(data_dir)
    try:
        assert b.ledger.begin("safe-retry") == "accept"
    finally:
        _close(b)


def test_receipt_persist_failure_is_reported_without_turning_into_safe_retry(data_dir, monkeypatch):
    original = snapshots.write_json_atomic

    def failing(path, data, *a, **kw):
        if getattr(path, "name", "") == "phone-event.json":
            raise OSError("disk full")
        return original(path, data, *a, **kw)

    monkeypatch.setattr(snapshots, "write_json_atomic", failing)
    a = _open(data_dir)
    try:
        result, _bundle, written = _remote_delivery(a, "unsaved")
        assert written == ["手机稿"]
        assert result["receipt_saved"] is False
        assert result["result"] not in {"NO_STEPS", "CANCELLED"} and not result.get("error_code")
        assert not (data_dir / "phone-event.json").exists()
        assert a.ledger.begin("unsaved") == "duplicate"
    finally:
        _close(a)
    b = _open(data_dir)
    try:
        assert b.ledger.begin("unsaved") == "duplicate"
    finally:
        _close(b)
