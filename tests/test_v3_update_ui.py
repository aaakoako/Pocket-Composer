"""Production Qt update button; transport/installer are explicit controlled substitutes."""
import os
import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox
from tests.test_v3_unified_ui import pair
from doubao_typeless.services import v3_update


def until(check):
    for _ in range(100):
        if check():return
        QTest.qWait(20)
    raise AssertionError('Qt update callback timed out')


@pytest.mark.parametrize('available', [False, True])
def test_auto_update_opens_independent_window_only_for_new_version(pair, monkeypatch, available):
    from PySide6.QtCore import Qt
    app, window, _ = pair
    monkeypatch.setattr(v3_update, 'check_preview_update', lambda **kw: {
        'message': '发现正式版 0.6.0' if available else '无法查询 GitHub',
        'latest': '0.6.0', 'update_available': available})
    window.check_update(automatic=True)
    until(lambda: window.update_button.isEnabled())
    assert window.update_dialog.isVisible() == available
    assert not window.widget.isVisible()
    assert window.update_dialog.isWindow()
    if available:
        assert window.update_dialog.testAttribute(Qt.WA_ShowWithoutActivating)
        folder = os.environ.get('DT_UI_EVIDENCE_DIR')
        if folder:
            from pathlib import Path
            path = Path(folder)
            path.mkdir(parents=True, exist_ok=True)
            QTest.qWait(30)
            window.update_dialog.grab().save(str(path / 'independent-update-window.png'))
        window.update_dialog.hide()
        window.check_update(automatic=True)
        until(lambda: window.update_button.isEnabled())
        assert not window.update_dialog.isVisible()


def test_manual_check_displays_failure_outside_settings(pair, monkeypatch):
    app, window, _ = pair
    monkeypatch.setattr(v3_update, 'check_preview_update', lambda **kw: {'message': '无法查询 GitHub'})
    window.update_button.click()
    until(lambda: window.update_button.isEnabled())
    assert window.update_dialog.isVisible()
    assert window.update_status.isVisible()
    assert window.update_status.text() == '无法查询 GitHub'
    assert not window.update_timer.isActive()  # 源码测试不自动联网


def test_candidate_does_not_automatically_prompt_for_older_stable(pair, monkeypatch):
    app, window, _ = pair
    monkeypatch.setattr(v3_update, 'check_preview_update', lambda **kw: {
        'message': '切换至正式版', 'latest': '0.5.8', 'update_available': True,
        'automatic_update_available': False})
    window.check_update(automatic=True)
    until(lambda: window.update_button.isEnabled())
    assert not window.update_dialog.isVisible()


def test_release_startup_schedules_automatic_check(pair, monkeypatch):
    from doubao_typeless import build_info
    from doubao_typeless.ui.desktop import ClientWindow
    app, _, _ = pair
    calls = []
    monkeypatch.setattr(build_info, 'release_layout', lambda: True)
    monkeypatch.setattr(v3_update, 'check_preview_update', lambda **kw: calls.append(True) or
                       {'message': '已经最新', 'latest': '0.5.9', 'update_available': False})
    window = ClientWindow(app)
    try:
        assert window.update_timer.isActive()
        assert window.update_timer.interval() == 6 * 60 * 60 * 1000
        QTest.qWait(3200)
        until(lambda: bool(calls) and window.update_button.isEnabled())
        assert len(calls) == 1 and not window.update_dialog.isVisible()
    finally:
        window.widget.deleteLater()
        QTest.qWait(10)


def test_network_choice_changes_display_qr_and_copy(pair, monkeypatch):
    from doubao_typeless import runtime
    app, window, _ = pair
    monkeypatch.setattr(runtime, 'connection_addresses', lambda: [('192.168.1.20', 'Wi-Fi'), ('100.101.2.3', 'Tailscale')])
    window._refresh_addresses()
    window._select_address(2)
    window.show_window()
    window.copy_url()
    assert window.url_label.text().startswith('http://100.101.2.3:')
    assert window._clipboard.text().startswith('http://100.101.2.3:')
    assert window._pairing_url.startswith('http://100.101.2.3:')


def test_typing_refresh_reuses_adapter_result_until_next_network_poll(pair, monkeypatch):
    from doubao_typeless.ui import desktop
    app, window, _ = pair
    calls = []
    monkeypatch.setattr(desktop, 'lan_ip', lambda: calls.append(True) or '192.168.1.20')
    window._address_checked_at = 0
    for _ in range(50):
        assert '192.168.1.20' in window.phone_url()
    assert len(calls) == 1

@pytest.mark.parametrize('fails',[False,True])
def test_explicit_update_button_quits_only_after_valid_handoff(pair,tmp_path,monkeypatch,fails):
    app,window,_=pair
    accepted=tmp_path/'handoff.accept';seen=[]
    window._update_package={'version':'0.5.2'}
    window.on_update_ready=lambda:seen.append(accepted.read_text())
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.Yes)
    def download(*args,**kwargs):
        if fails:raise ValueError('校验失败，当前版本未改变')
        return tmp_path/'package.exe'
    monkeypatch.setattr(v3_update,'download_upgrade',download)
    monkeypatch.setattr(v3_update,'start_upgrade',lambda *a,**k:accepted)
    window.update_install.click()
    if fails:
        until(lambda:'校验失败' in window.update_status.text())
        assert window.update_install.isEnabled() and window.update_button.isEnabled()
        assert not seen and not accepted.exists()
    else:
        until(lambda:bool(seen))
        assert seen==[str(os.getpid())]
        assert '新版将自动打开' in window.update_status.text()
