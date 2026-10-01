"""信任表写时复制在 Qt 线程与 HTTP 线程交错时仍保持一致：A 停在写盘中途，B 发起另一种变更。"""
from __future__ import annotations

import threading

import pytest

from doubao_typeless.storage.credentials import AuthService, TrustStoreError


def _auth(tmp_path):
    return AuthService(store_path=tmp_path / "trusted.json")


def _pair(auth):
    return auth.complete_pairing(auth.new_pairing_challenge())


class _Gate:
    """让名为 writer-a 的线程停在 _save_trusted 内，直到放行；可选择让它写盘失败。"""

    def __init__(self, auth, *, fail_a=False):
        self.reached, self.release = threading.Event(), threading.Event()
        original = auth._save_trusted

        def save(candidate=None):
            if threading.current_thread().name == "writer-a":
                self.reached.set()
                assert self.release.wait(3)
                if fail_a:
                    raise TrustStoreError("TRUST_STORE_WRITE_FAILED")
            return original(candidate)

        auth._save_trusted = save


def _race(gate, first, second):
    """first 在 writer-a 中停在写盘；second 在 writer-b 中开始，必须等 A 完成后才能完成。"""
    results, errors, done_b = {}, {}, threading.Event()

    def run(name, fn):
        try:
            results[name] = fn()
        except Exception as exc:  # noqa: BLE001 - 记录给断言
            errors[name] = exc
        finally:
            if name == "b":
                done_b.set()

    ta = threading.Thread(target=run, args=("a", first), name="writer-a")
    tb = threading.Thread(target=run, args=("b", second), name="writer-b")
    ta.start()
    assert gate.reached.wait(2)
    tb.start()
    blocked = not done_b.wait(.25)
    gate.release.set()
    ta.join(3); tb.join(3)
    assert not ta.is_alive() and not tb.is_alive()
    return results, errors, blocked


def _disk(tmp_path):
    return AuthService(store_path=tmp_path / "trusted.json").trusted


def test_remember_and_grant_change_on_other_device_both_persist(tmp_path):
    auth = _auth(tmp_path)
    a, b = _pair(auth), _pair(auth)
    auth.remember_device(b)
    gate = _Gate(auth)
    results, errors, blocked = _race(gate, lambda: auth.remember_device(a),
                                     lambda: auth.set_grants(b.session_id, allow_insert=True, allow_capture=True))
    assert not errors and blocked
    disk = _disk(tmp_path)
    assert set(disk) == {a.device_id, b.device_id}
    assert disk[b.device_id].allow_insert and disk[b.device_id].allow_capture
    assert a.remembered and b.allow_insert and b.allow_capture
    assert set(auth.trusted) == set(disk)
    assert _auth(tmp_path).resume_trusted(a.device_id, results["a"]).device_id == a.device_id


def test_remember_and_forget_other_device_keep_disk_and_sessions_coherent(tmp_path):
    auth = _auth(tmp_path)
    a, b = _pair(auth), _pair(auth)
    auth.remember_device(b)
    gate = _Gate(auth)
    results, errors, blocked = _race(gate, lambda: auth.remember_device(a), lambda: auth.forget_device(b.device_id))
    assert not errors and blocked and results["b"] is True
    assert set(_disk(tmp_path)) == {a.device_id} == set(auth.trusted)
    assert b.session_id not in auth.sessions and a.session_id in auth.sessions


def test_unremember_during_remember_of_other_device_does_not_resurrect_or_drop(tmp_path):
    auth = _auth(tmp_path)
    a, b = _pair(auth), _pair(auth)
    auth.remember_device(b)
    gate = _Gate(auth)
    _results, errors, blocked = _race(gate, lambda: auth.remember_device(a),
                                      lambda: auth.unremember_device(b.device_id))
    assert not errors and blocked
    assert set(_disk(tmp_path)) == {a.device_id} == set(auth.trusted)
    assert a.remembered and not b.remembered and not b.device_secret_once
    assert b.session_id in auth.sessions  # 取消记住不断开当前连接


def test_failed_write_rolls_back_only_its_own_change_while_other_writer_waits(tmp_path):
    auth = _auth(tmp_path)
    a, b = _pair(auth), _pair(auth)
    gate = _Gate(auth, fail_a=True)
    results, errors, blocked = _race(gate, lambda: auth.remember_device(a), lambda: auth.remember_device(b))
    assert blocked and isinstance(errors.get("a"), TrustStoreError) and "b" not in errors
    assert not a.remembered and not a.device_secret_once
    assert b.remembered and results["b"] == b.device_secret_once
    assert set(_disk(tmp_path)) == {b.device_id} == set(auth.trusted)


def test_resume_and_ack_wait_for_inflight_write_and_see_its_result(tmp_path):
    auth = _auth(tmp_path)
    a, b = _pair(auth), _pair(auth)
    secret_b = auth.remember_device(b)
    gate = _Gate(auth)
    results, errors, blocked = _race(gate, lambda: auth.set_grants(b.session_id, allow_insert=True),
                                     lambda: auth.resume_trusted(b.device_id, secret_b))
    assert not errors and blocked
    resumed = results["b"]
    assert resumed.allow_insert and resumed.session_id in auth.sessions
    assert not any(s.device_secret_once for s in auth.sessions.values() if s.device_id == b.device_id)
    assert a.session_id in auth.sessions


def test_lock_free_session_readers_survive_concurrent_pairing_and_forget(tmp_path):
    auth = _auth(tmp_path)
    stop, failures = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                list(s.device_id for s in auth.sessions.values())
            except RuntimeError as exc:
                failures.append(exc)
                return

    def churn():
        for _ in range(300):
            session = _pair(auth)
            auth.forget_device(session.device_id)

    readers = [threading.Thread(target=reader) for _ in range(2)]
    for t in readers:
        t.start()
    try:
        churn()
    finally:
        stop.set()
        for t in readers:
            t.join(3)
    assert not failures
    assert auth.sessions == {}


@pytest.mark.parametrize("method", ["remember_device", "set_grants", "forget_device", "unremember_device"])
def test_mutators_are_reentrant_from_the_same_thread(tmp_path, method):
    auth = _auth(tmp_path)
    a = _pair(auth)
    with auth._lock:
        if method == "remember_device":
            auth.remember_device(a)
        elif method == "set_grants":
            auth.set_grants(a.session_id, allow_insert=True)
        elif method == "forget_device":
            auth.revoke(a.session_id)
        else:
            auth.unremember_device(a.device_id)
