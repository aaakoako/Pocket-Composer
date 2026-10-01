"""Opus 5.5 审查修复：连接页真实 Qt 控件（offscreen，不注册热键、不发键）。"""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from doubao_typeless.app import V3App
from doubao_typeless.storage import draft_snapshot
from doubao_typeless.storage.settings_store import load_settings
from doubao_typeless.ui.desktop import ClientWindow


@pytest.fixture
def window(tmp_path):
    QApplication.instance() or QApplication([])
    app = V3App(data_dir=tmp_path / "isolated", port=0)
    w = ClientWindow(app)
    yield app, w
    w.widget.hide(); w.widget.deleteLater()
    app._commands.close(2); app.db.conn.close(); app._lock.release(); QTest.qWait(10)


def buttons(w, text):
    QTest.qWait(20)  # 旧设备行用 deleteLater 移除
    return [b for b in w.widget.findChildren(QPushButton) if b.text() == text]


def test_hidden_window_refresh_does_not_mint_pairing_codes(window):
    app, w = window
    app.auth._challenge = None
    w.refresh()
    assert app.auth.current_pairing_challenge() is None
    assert "过期" in w.qr.text()
    w.show_window(); QTest.qWait(20)
    w.refresh()
    assert app.auth.current_pairing_challenge() is not None


def test_remember_and_nickname_affect_only_that_phone(window, monkeypatch):
    app, w = window
    a = app.auth.complete_pairing(app.auth.new_pairing_challenge())
    b = app.auth.complete_pairing(app.auth.new_pairing_challenge())
    w.show_window(); QTest.qWait(20); w.refresh()
    assert len(buttons(w, "记住 30 天")) == 2
    w._toggle_remember(b.device_id, True)
    assert b.remembered and not a.remembered and set(app.auth.trusted) == {b.device_id}
    assert len(buttons(w, "取消记住")) == 1 and len(buttons(w, "记住 30 天")) == 1
    edits = [e for e in w.widget.findChildren(QLineEdit) if "可改名" in e.placeholderText()]
    assert len(edits) == 2 and all(e.text() == "" for e in edits)
    # 只是离开输入框不应把默认名写成昵称
    w._save_nickname(a.device_id, edits[0])
    assert not load_settings(app.data_dir).get("device_nicknames")
    edits[1].setText("客厅平板")
    w._save_nickname(b.device_id, edits[1])
    assert load_settings(app.data_dir)["device_nicknames"] == {b.device_id: "客厅平板"}
    assert "客厅平板" in w.device_box.text() and "手机 1" in w.device_box.text()
    w._toggle_remember(b.device_id, False)
    assert not b.remembered and b.session_id in app.auth.sessions and not app.auth.trusted

    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(draft_snapshot, "write_json_atomic", fail)
    w._toggle_remember(a.device_id, True)
    assert not a.remembered and not a.device_secret_once and "没能保存" in w.device_note.text()


def test_practice_box_is_withdrawn_when_phone_draft_exists(window):
    app, w = window
    app.draft.authority = "phone"; app.draft.text = "手机主稿"
    w.show_window(); QTest.qWait(20)
    w.practice.setPlainText("练习一下")
    assert app.draft.text == "手机主稿"
    assert w.practice.isHidden() and "练习框已收起" in w.device_note.text()


def test_version_line_is_whole_words_and_wraps_instead_of_clipping(window, monkeypatch):
    from PySide6.QtGui import QFont
    from doubao_typeless.services.v3_update import preview_version_label
    monkeypatch.setattr("doubao_typeless.build_info.build_info",
                        lambda: {"channel": "v3-private-trial", "version": "0.5.6", "source_sha": "development"})
    _app, w = window
    label = w.version_label
    label.setText(preview_version_label())
    assert "developm" not in label.text() and label.text().endswith("开发构建")
    assert label.wordWrap()
    big = QFont(label.font()); big.setPointSize(24); label.setFont(big)
    narrow = label.fontMetrics().horizontalAdvance(label.text()) // 2
    one_line = label.fontMetrics().height()
    assert label.heightForWidth(narrow) >= 2 * one_line


def test_version_line_keeps_short_commit_for_real_builds(monkeypatch):
    from doubao_typeless.services.v3_update import preview_version_label
    monkeypatch.setattr("doubao_typeless.build_info.build_info",
                        lambda: {"channel": "release-candidate", "version": "0.5.6", "source_sha": "ab" * 20})
    assert preview_version_label() == "Pocket Composer 0.5.6 · 发布候选 · abababab"


def test_pending_asset_claim_shows_explicit_desktop_approval(window):
    app, w = window
    s = app.auth.complete_pairing(app.auth.new_pairing_challenge())
    app.bridge._asset_claims[s.device_id] = {"asset_ids": ["x"], "at": 0}
    w.show_window(); QTest.qWait(20); w.refresh()
    assert buttons(w, "允许取回 1 张旧图") and buttons(w, "拒绝")
    w._resolve_claim(s.device_id, False)
    assert not app.pending_asset_claims() and not buttons(w, "允许取回 1 张旧图")
