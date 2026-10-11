"""剪贴板读取事务失败的回归；不修改测试机的系统剪贴板。"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from doubao_typeless.platform.windows import clipboard
from doubao_typeless.ui.insert_status import error_message
from tests.test_v3_assistant_delivery import app, platform, prepare


@pytest.fixture
def native(monkeypatch):
    # 保留跨平台运行能力，pywintypes.error 并不是 OSError 子类。
    class NativeError(Exception):
        pass
    monkeypatch.setattr(clipboard.sys, 'platform', 'win32')
    monkeypatch.setattr(clipboard, 'pywintypes', SimpleNamespace(error=NativeError), raising=False)
    monkeypatch.setattr(clipboard, 'win32con', SimpleNamespace(CF_UNICODETEXT=13), raising=False)
    values = iter([NativeError(1418), '完整文字'])
    transactions = []
    @contextmanager
    def opened():
        transactions.append('open')
        try: yield
        finally: transactions.append('close')
    def read(_):
        value = next(values)
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(clipboard, 'opened_clipboard', opened)
    monkeypatch.setattr(clipboard, 'win32clipboard', SimpleNamespace(
        IsClipboardFormatAvailable=lambda _: True, GetClipboardData=read), raising=False)
    return NativeError, transactions


def test_transient_get_error_reopens_whole_transaction(native):
    _, transactions = native
    assert clipboard.read_clipboard_text() == '完整文字'
    assert transactions == ['open', 'close', 'open', 'close']


def test_real_content_change_is_returned_without_waiting_or_rewriting(native, monkeypatch):
    _, transactions = native
    monkeypatch.setattr(clipboard.win32clipboard, 'GetClipboardData', lambda _: '用户刚复制的新文字')
    assert clipboard.read_clipboard_text() == '用户刚复制的新文字'
    assert transactions == ['open', 'close']


def test_persistent_failure_is_unavailable_not_content_change(native, monkeypatch):
    NativeError, transactions = native
    monkeypatch.setattr(clipboard.win32clipboard, 'GetClipboardData', lambda _: (_ for _ in ()).throw(NativeError(1418)))
    clock = iter([0, 0.1, 0.3])
    monkeypatch.setattr(clipboard.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(clipboard.time, 'sleep', lambda _: None)
    with pytest.raises(clipboard.ClipboardReadError) as error:
        clipboard.read_clipboard_text()
    assert error.value.error_code == 'CLIPBOARD_UNAVAILABLE'
    assert transactions == ['open', 'close', 'open', 'close']
    assert '暂时读不出' in error_message({'detail_code': error.value.error_code})


def test_app_preserves_read_failure_code(monkeypatch, app):
    from doubao_typeless.platform import desktop
    monkeypatch.setattr(desktop, 'read_clipboard_text', lambda: (_ for _ in ()).throw(clipboard.ClipboardReadError()))
    with pytest.raises(clipboard.ClipboardReadError): app._read_clipboard_text()


def test_unavailable_clipboard_keeps_draft_and_does_not_paste(app):
    calls = []
    platform(app, paste=lambda: calls.append('paste'))
    app._set_text = lambda _: calls.append('fallback rewrite')
    prepare(app, '读失败时保留原文')
    app.delivery._read_clipboard_text = lambda: (_ for _ in ()).throw(clipboard.ClipboardReadError())
    result = app.request_insert().result(3)
    assert result['detail_code'] == 'CLIPBOARD_UNAVAILABLE'
    assert calls == [] and app.draft.text == '读失败时保留原文'
    assert not result['phone_event']['rotated']
