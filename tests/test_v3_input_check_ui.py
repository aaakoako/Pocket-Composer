import os
from pathlib import Path
import pytest
pytest.importorskip('PySide6')
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea
from tests.test_v3_unified_ui import pair
from tests.test_v3_input_check import READY, response
from doubao_typeless.services.input_check import evaluate
from doubao_typeless.services.input_check import VOICE_NOTE
from doubao_typeless.storage.settings_store import load_settings
from doubao_typeless.ui.desktop import apply_ui_font


def test_settings_show_plaintext_cleanup_warning_without_a_modal(pair, monkeypatch):
    from doubao_typeless.storage.secret_store import CLEANUP_WARNING
    from doubao_typeless.ui import desktop
    _, window, _ = pair
    monkeypatch.setattr(desktop, 'save_settings', lambda *args, **kwargs: [CLEANUP_WARNING])
    window.save_settings()
    assert CLEANUP_WARNING in window.byok_status.text()
    assert window.jev_settings.status.text() == CLEANUP_WARNING
    assert window.byok_status.objectName() == 'error'


@pytest.fixture(autouse=True)
def production_font(pair,monkeypatch):
    # Exercise real settings/widgets without registering OS-wide hotkeys.
    monkeypatch.setattr(pair[0],'apply_hotkeys',lambda *args,**kwargs:[])
    # The offscreen Qt plugin on Windows does not enumerate system fonts.
    from PySide6.QtGui import QFontDatabase
    if not QFontDatabase.families() and os.name == 'nt':
        QFontDatabase.addApplicationFont(str(Path(os.environ['WINDIR'])/'Fonts'/'msyh.ttc'))
    apply_ui_font()


def test_settings_save_inspection_and_nonblocking_visible_controls(pair,monkeypatch):
    a,window,review=pair
    monkeypatch.setattr(a.input_check,'_evaluate',lambda text,key,emotion,**kwargs:evaluate(text,key,emotion,post=response,**kwargs))
    a.update_pc_text('请把图片插入扣得克斯。这里还有一些地方要修改。')
    pane=window.jev_settings
    pane.key.setText('fixture-key');pane.enabled.setChecked(True);pane.note.setChecked(True)
    window.save_settings()
    settings=load_settings(a.data_dir)
    assert settings['jev_enabled'] and settings['jev_voice_note']
    a.hud.start();a.hud.show_receiving(a.review_text())
    QTest.qWait(1200)
    try:
        assert '处待留意' in a.hud._check_button.toolTip()
        assert a.hud._tone_badge.label.text()=='急切'
        assert not a.hud._check_button.icon().isNull()
        assert a.hud._insert.isEnabled() and not a.hud._note_button.isHidden()
        review.show();QTest.qWait(300)
        assert '疑似转写错误' in review.input_check_details.details.toPlainText()
        assert VOICE_NOTE in review.input_check_details.note.toolTip()
        assert review.editor.toPlainText()==a.review_text() and VOICE_NOTE not in a.review_text()
        review.input_check_details.note.click();QTest.qWait(300)
        assert not a.input_check.view(a.input_check_identity())['note']
        a.hud.dismiss();QTest.qWait(300)
        a.hud.set_input_check({**READY,'note':True})
        assert not a.hud._widget.isVisible(),'late judgment reopened dismissed HUD'
        pane.enabled.setChecked(False);window.save_settings();QTest.qWait(300)
        assert a.hud._check_row.isHidden() and review.input_check_details.isHidden()
    finally:a.hud._widget.deleteLater()


def test_production_input_check_screenshots(pair,monkeypatch):
    folder=os.environ.get('DT_JEV_UI_EVIDENCE_DIR')
    if not folder:return
    a,window,review=pair;path=Path(folder);path.mkdir(parents=True,exist_ok=True)
    monkeypatch.setattr(a.input_check,'_evaluate',lambda text,key,emotion,**kwargs:evaluate(text,key,emotion,post=response,**kwargs))
    pane=window.jev_settings
    pane.provider.setCurrentIndex(pane.provider.findData('vercel'))
    pane.gateway_key.setText('fixture-key');pane.enabled.setChecked(True);pane.note.setChecked(True)
    window.save_settings();a.update_pc_text('把图片插入扣得克斯，保留原来的文字。')
    a.hud.start();a.hud.show_receiving(a.review_text());QTest.qWait(1200)
    try:
        a.hud._widget.grab().save(str(path/'hud.png'))
        review.show();QTest.qWait(300);review.widget.grab().save(str(path/'review.png'))
        window.show_window();window.tabs.setCurrentIndex(1);QTest.qWait(100)
        scroll=window.tabs.currentWidget().findChild(QScrollArea)
        scroll.ensureWidgetVisible(pane.status,0,0);QTest.qWait(100)
        window.widget.grab().save(str(path/'settings.png'))
    finally:a.hud._widget.deleteLater()


def test_provider_credentials_are_separate_visible_fields(pair):
    _,window,_=pair;pane=window.jev_settings
    pane.key.setText('typesafe-key')
    pane.provider.setCurrentIndex(pane.provider.findData('vercel'))
    assert pane.key.isHidden() and not pane.gateway_key.isHidden() and not pane.gateway_key.text()
    pane.gateway_key.setText('gateway-key')
    pane.status.setText('已连接 · 保存后生效')
    pane.provider.setCurrentIndex(pane.provider.findData('typesafe'))
    assert pane.gateway_key.isHidden() and pane.key.text()=='typesafe-key'
    assert pane.values()['jev_vercel_key']=='gateway-key'
    assert '待检测' in pane.status.text()


def test_custom_endpoint_change_clears_key_after_saving_same_window(pair):
    _,window,_=pair;pane=window.jev_settings
    pane.provider.setCurrentIndex(pane.provider.findData('custom'))
    pane.custom_endpoint.setText('https://first.example/v1');pane.custom_key.setText('custom-key')
    window.save_settings()
    pane.custom_endpoint.setText('https://second.example/v1')
    pane.probe.click()
    assert pane.custom_key.text()=='' and pane.probe.isEnabled()
    pane.custom_key.setText('custom-key');window.save_settings()
    assert load_settings(pair[0].data_dir)['jev_custom_key']=='custom-key'
