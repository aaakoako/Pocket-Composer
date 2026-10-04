"""Windows graphical client. Same V3App/bridge as the phone; not a static page."""
from __future__ import annotations

import io
import sys
import traceback
from pathlib import Path
from typing import Callable

from doubao_typeless.runtime import lan_ip
from doubao_typeless.storage.credentials import pairing_page_url
from doubao_typeless.storage.settings_store import load_settings, save_settings
from doubao_typeless.storage.vocab_store import load_vocab, save_vocab

PROVIDER_PRESETS = [
    ("自定义", "", ""),
    ("DeepSeek", "https://api.deepseek.com/v1", "deepseek-chat"),
    ("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    ("OpenRouter", "https://openrouter.ai/api/v1", ""),
    ("Vercel AI Gateway", "https://ai-gateway.vercel.sh/v1", ""),
]
from doubao_typeless.ui.filelog import FileLogger
from doubao_typeless.ui.single_instance import listen_for_commands, request_quit, request_show
from doubao_typeless.ui.v3_startup import apply_v3_autostart

from doubao_typeless.ui.theme import QSS as STYLESHEET, style_root
from doubao_typeless.ui.theme_generated import COLORS
TOKENS = {"accent":COLORS["accent"], "surface":COLORS["bg"], "card":COLORS["surface"],
          "ink":COLORS["ink"], "muted":COLORS["muted"], "danger":COLORS["danger"]}

RESULT_LABELS = {
    "CONFIRMED": "已插入",
    "UNKNOWN": "上次结果未知",
    "PARTIAL": "只完成一部分",
    "NO_STEPS": "没有贴出",
    "CANCELLED": "已取消",
    "BUSY": "正忙",
}


def _result_label(code: object) -> str:
    if not code:
        return "未记录"
    return RESULT_LABELS.get(str(code), str(code))



def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[3]


def qr_pixmap(url: str, size: int = 168):
    from PySide6.QtGui import QImage, QPixmap

    try:
        import qrcode
    except ImportError:
        return QPixmap()
    image = qrcode.make(url)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    qimg = QImage.fromData(buf.getvalue())
    return QPixmap.fromImage(qimg).scaled(size, size)


def apply_ui_font(qt=None) -> str:
    from PySide6.QtGui import QFont, QFontDatabase
    from PySide6.QtWidgets import QApplication

    qt = qt or QApplication.instance()
    if qt is None:
        return ""
    families = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "Segoe UI"):
        if name in families:
            qt.setFont(QFont(name, 10))
            return name
    return ""


def app_icon():
    from PySide6.QtGui import QColor, QIcon, QPixmap

    candidates = [
        _repo_root() / "assets" / "icon.ico",
        Path(sys.executable).parent / "assets" / "icon.ico",
    ]
    for ico in candidates:
        if ico.is_file():
            return QIcon(str(ico))
    pm = QPixmap(32, 32)
    pm.fill(QColor(TOKENS["accent"]))
    return QIcon(pm)


class RecoveryDialog:
    def __init__(self, parent=None, *, confirm_image: bool = False, progress: dict | None = None):
        from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

        self.choice = "cancel"
        dlg = QDialog(parent)
        dlg.setWindowTitle("上次结果未知")
        dlg.setModal(True)
        style_root(dlg)
        dlg.resize(420, 180)
        layout = QVBoxLayout(dlg)
        message = QLabel("程序不能确认刚才的图片是否已加入。请查看目标输入框，再决定继续或重贴。"
                         if confirm_image else "上次插入结果不确定。不自动重贴，也不全选删除。")
        if progress and progress.get("message"):
            message.setText(progress["message"] + "。\n确认图片已出现后继续，不会重贴该图。")
        message.setWordWrap(True)
        layout.addWidget(message)
        if confirm_image:
            caption = "图片已出现，继续文字" if progress and progress.get("images_attempted")==progress.get("images_total") and progress.get("text_state")=="not_attempted" else "我已看到刚才的图片，继续剩余内容"
            confirmed = QPushButton(caption)
            confirmed.setObjectName("primary")
            confirmed.clicked.connect(lambda: self._pick(dlg, "confirm_continue"))
            layout.addWidget(confirmed)
        row = QHBoxLayout()
        for label, mode, name in (
            ("完整重贴", "full", "ghost"),
            ("只贴文字", "text_only", "ghost"),
            ("取消", "cancel", "ghost"),
        ):
            btn = QPushButton(label)
            btn.setObjectName(name)
            btn.clicked.connect(lambda _=False, m=mode: self._pick(dlg, m))
            row.addWidget(btn)
        layout.addLayout(row)
        self._dlg = dlg

    def _pick(self, dlg, mode: str) -> None:
        self.choice = mode
        dlg.accept() if mode != "cancel" else dlg.reject()

    def exec(self) -> str:
        self._dlg.exec()
        return self.choice


class ComposerPicker:
    """多候选只列明确的输入框；选中后聚焦但不自动发送或投递。"""
    def __init__(self, parent, candidates):
        from PySide6.QtWidgets import QDialog,QLabel,QListWidget,QPushButton,QVBoxLayout,QHBoxLayout
        self.candidate=None
        dlg=QDialog(parent);dlg.setWindowTitle("选择对话输入框");dlg.resize(420,280);style_root(dlg)
        layout=QVBoxLayout(dlg);layout.setContentsMargins(16,16,16,16);layout.setSpacing(12)
        note=QLabel("当前窗口有多个对话输入框。选中后只定位，不会自动插入或发送。");note.setWordWrap(True);layout.addWidget(note)
        choices=QListWidget()
        for n,item in enumerate(candidates,1):choices.addItem(f"{n}. {item.get('label','对话输入框')} · {item.get('title','')[:55]}")
        layout.addWidget(choices);row=QHBoxLayout();cancel=QPushButton("取消");cancel.clicked.connect(dlg.reject)
        use=QPushButton("使用此输入框");use.setObjectName("primary");use.setEnabled(False)
        choices.currentRowChanged.connect(lambda n:use.setEnabled(0<=n<len(candidates)))
        def choose():
            index=choices.currentRow()
            if 0<=index<len(candidates):self.candidate=candidates[index];dlg.accept()
        use.clicked.connect(choose);row.addWidget(cancel);row.addWidget(use);layout.addLayout(row);self.widget=dlg
    def exec(self):self.widget.exec();return self.candidate


class ReviewPanel:
    def __init__(self, app, parent=None):
        from PySide6.QtWidgets import (
            QHBoxLayout,
            QLabel,
            QPlainTextEdit,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )

        self.app = app
        self._editing = False
        w = QWidget(parent)
        w.setWindowTitle("当前图文")
        w.resize(480, 620)
        style_root(w)
        from PySide6.QtWidgets import QScrollArea
        from doubao_typeless.ui.flow_layout import FlowLayout
        outer = QVBoxLayout(w)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)
        content = QWidget()
        scroll = QScrollArea(w)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0,0,8,0)
        layout.setSpacing(8)
        self.banner = QLabel("")
        self.banner.setObjectName("error")
        self.banner.setWordWrap(True)
        self.banner.hide()
        layout.addWidget(self.banner)
        self.images = QLabel("没有图片")
        self.images.setObjectName("muted")
        layout.addWidget(self.images)
        self.image_row = QHBoxLayout()
        layout.addLayout(self.image_row)
        self.editor = QPlainTextEdit()
        from doubao_typeless.ui.motion import InputFeedback
        self.input_feedback=InputFeedback(self.editor)
        self.input_feedback.configure(bool(load_settings(self.app.data_dir).get('ui_motion',True)))
        self.editor.setMinimumHeight(180)
        self.editor.textChanged.connect(self._mark_editing)
        layout.addWidget(self.editor, 1)
        from doubao_typeless.ui.input_check import InputCheckDetails
        self.input_check_details = InputCheckDetails(self.app, w, feedback=self.input_feedback)
        layout.addWidget(self.input_check_details)
        row = FlowLayout()
        use_phone = QPushButton("采用手机版")
        use_phone.setObjectName("ghost")
        use_phone.clicked.connect(self.take_phone)
        keep = QPushButton("保留电脑稿")
        keep.setObjectName("ghost")
        keep.clicked.connect(self.keep_pc)
        terms_btn = QPushButton("检查术语")
        terms_btn.setObjectName("ghost")
        terms_btn.clicked.connect(self.check_terms)
        suggest = QPushButton("建议改写")
        suggest.setObjectName("ghost")
        suggest.clicked.connect(self.suggest_rewrite)
        apply_s = QPushButton("采用建议")
        apply_s.setObjectName("ghost")
        apply_s.clicked.connect(self.apply_rewrite)
        reject_s = QPushButton("不用建议")
        reject_s.setObjectName("ghost")
        reject_s.clicked.connect(self.reject_rewrite)
        copy = QPushButton("复制")
        copy.setObjectName("ghost")
        copy.clicked.connect(self.copy_only)
        insert = QPushButton("插入并复制")
        insert.setObjectName("primary")
        insert.clicked.connect(self.insert)
        context_row = FlowLayout()
        for button in (use_phone, keep, apply_s, reject_s):
            context_row.addWidget(button)
        layout.addLayout(context_row)
        tools_row = FlowLayout()
        tools_row.addWidget(terms_btn)
        tools_row.addWidget(suggest)
        locate = QPushButton("定位输入框")
        locate.setObjectName("ghost")
        locate.clicked.connect(lambda: self.app.request_locate_composer())
        tools_row.addWidget(locate)
        layout.addLayout(tools_row)
        back = QPushButton("返回浮窗")
        back.clicked.connect(self.return_to_hud)
        self.btn_back = back
        from doubao_typeless.ui.icons import icon
        for button, name in ((terms_btn,'inspect'),(suggest,'edit'),(locate,'target'),
                             (copy,'copy'),(back,'back')):
            button.setIcon(icon(name))
        insert.setIcon(icon('insert','#ffffff'))
        row.addWidget(back)
        row.addWidget(copy)
        row.addWidget(insert)
        outer.addLayout(row)
        self.btn_use_phone = use_phone
        self.btn_keep = keep
        self.btn_apply = apply_s
        self.btn_reject = reject_s
        use_phone.hide()
        keep.hide()
        apply_s.hide()
        reject_s.hide()
        self.widget = w
        w.closeEvent = lambda event: (event.ignore(), self.return_to_hud())
        from PySide6.QtCore import QEvent, QObject

        class _HideRelease(QObject):
            def eventFilter(inner, _obj, ev):
                if ev.type() == QEvent.Hide:
                    self.app.review_editing = False
                    self._editing = False
                return False

        self._hide_filter = _HideRelease(w)
        w.installEventFilter(self._hide_filter)
        self.app.hud.bind_foreground_surface(w)
        self.reload()

    def return_to_hud(self) -> None:
        self.app.update_pc_text(self.editor.toPlainText())
        self.widget.hide()
        self.app._on_activity(self.app.review_text(), len(self.app.draft.assets))

    def _mark_editing(self) -> None:
        self.input_feedback.pulse()
        self._editing = True
        self.app.review_editing = True
        self.app.update_pc_text(self.editor.toPlainText())

    def _sync_buttons(self) -> None:
        pending = bool(self.app.phone_pending)
        self.btn_use_phone.setVisible(pending)
        self.btn_keep.setVisible(pending)
        last = getattr(self.app, "_last_suggestion", None) or {}
        has = bool(last.get("suggested"))
        self.btn_apply.setVisible(has)
        self.btn_reject.setVisible(has)

    def reload(self) -> None:
        self._editing = False
        self.app.review_editing = False
        self.banner.hide()
        self._set_text_preserving(self.app.review_text() or "")
        self._refresh_images()
        self._sync_buttons()

    def _set_text_preserving(self, text: str) -> None:
        """手机连续更新时只替换变化的尾部：用户的阅读位置和选区不被顶回开头或末尾。"""
        from PySide6.QtGui import QTextCursor
        from doubao_typeless.ui.live_text import clamp_position, replace_tail
        editor = self.editor
        old = editor.toPlainText()
        if old == text:
            return
        bar = editor.verticalScrollBar()
        cursor = editor.textCursor()
        selecting = cursor.hasSelection()
        following = not selecting and bar.value() >= bar.maximum() - 4
        position, anchor, value = cursor.position(), cursor.anchor(), bar.value()
        editor.blockSignals(True)
        try:
            # 只改变化点之后的文字；变化点之前的选区/光标原样不动。
            replace_tail(editor.document(), old, text)
            if not following:
                # 改动覆盖到阅读位置时 Qt 会收拢选区：按原 UTF-16 位置恢复（夹到文档范围内）。
                restored = editor.textCursor()
                if (restored.anchor(), restored.position()) != (anchor, position):
                    document = editor.document()
                    restored.setPosition(clamp_position(document, anchor))
                    restored.setPosition(clamp_position(document, position), QTextCursor.KeepAnchor)
                    editor.setTextCursor(restored)
        finally:
            editor.blockSignals(False)
        bar.setValue(bar.maximum() if following else min(value, bar.maximum()))

    def note_phone_pending(self) -> None:
        if not self._editing:
            self.reload()
            return
        self.banner.setText("手机有更新；电脑修改仍保留。默认以手机为准，可复制电脑修改或采用手机版继续。")
        self.banner.show()
        self._sync_buttons()

    def take_phone(self) -> None:
        self.app.accept_phone_pending()
        self.reload()

    def keep_pc(self) -> None:
        self.app.keep_pc_edit()
        self.banner.hide()
        self._sync_buttons()

    def copy_only(self) -> None:
        self.app.update_pc_text(self.editor.toPlainText())
        self.app.review_editing = False
        self.app._save_draft()
        self.app.copy_text(self.editor.toPlainText())

    def _clear_image_row(self) -> None:
        while self.image_row.count():
            item = self.image_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _refresh_images(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QLabel, QPushButton

        # 图片没变时不重读/重解码原图；手机每说一句都会触发这里。
        signature = tuple((a.get("asset_id"), a.get("status"), a.get("render_revision"))
                          for a in self.app.draft.assets)
        if signature == getattr(self, "_image_signature", None):
            return
        self._image_signature = signature
        previews = self.app.draft_image_previews()
        if not previews:
            self.images.setText("没有图片")
            self._clear_image_row()
            return
        missing = sum(1 for item in previews if not item["present"])
        self.images.setText(
            f"{len(previews)} 张图，顺序即投递顺序"
            + ("，有图还没传到电脑" if missing else "")
        )
        self._clear_image_row()
        for item in previews:
            thumb = QPushButton(f"{item['order']}")
            thumb.setFixedSize(72, 72)
            if item["present"] and item["data"]:
                pix = QPixmap()
                pix.loadFromData(item["data"])
                from PySide6.QtGui import QIcon
                thumb.setIcon(QIcon(pix))
                thumb.setIconSize(thumb.size() * 0.9)
                thumb.clicked.connect(lambda _=False, payload=item["data"]: self._enlarge(payload))
            else:
                thumb.setText(f"{item['order']}\n缺图")
            self.image_row.addWidget(thumb)
        self.image_row.addStretch(1)

    def _enlarge(self, payload: bytes) -> None:
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout

        dlg = QDialog(self.widget)
        dlg.setWindowTitle("查看图片")
        style_root(dlg)
        box = QVBoxLayout(dlg)
        label = QLabel()
        pix = QPixmap()
        pix.loadFromData(payload)
        from PySide6.QtCore import Qt

        if not pix.isNull():
            label.setPixmap(pix.scaled(480, 480, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            label.setPixmap(pix)
        box.addWidget(label)
        dlg.resize(500, 500)
        dlg.exec()

    def suggest_rewrite(self) -> None:
        import threading

        text = self.editor.toPlainText()
        self.app.update_pc_text(text)
        self.banner.setText("正在请求建议，仍可改字或插入")
        self.banner.show()

        def work() -> None:
            out = self.app.suggest_text(text)
            def apply() -> None:
                suggested = out.get("suggested") or ""
                if suggested and suggested != out.get("original"):
                    self.banner.setText(f"建议：{suggested}")
                else:
                    self.banner.setText(out.get("message") or "没有可用的改写建议")
                self.banner.show()
                self._sync_buttons()
            try:
                from PySide6.QtCore import QTimer

                QTimer.singleShot(0, self.widget, apply)
            except RuntimeError:
                pass  # 窗口已销毁，不能在工作线程操作 Qt 控件。

        threading.Thread(target=work, daemon=True).start()

    def apply_rewrite(self) -> None:
        self.app.update_pc_text(self.editor.toPlainText())
        if not self.app.apply_suggestion():
            self.banner.setText("建议已过期，请再点建议改写")
            self.banner.show()
            return
        self.editor.blockSignals(True)
        self.editor.setPlainText(self.app.review_text())
        self.editor.blockSignals(False)
        self.banner.setText("已采用建议；继续编辑前可撤回这次改写。")
        self.banner.show()
        self._sync_buttons()

    def reject_rewrite(self) -> None:
        self.app.reject_suggestion()
        self.editor.blockSignals(True)
        self.editor.setPlainText(self.app.review_text())
        self.editor.blockSignals(False)
        self.banner.setText("已取消本次建议；较新的编辑不会被覆盖。")
        self.banner.show()
        self._sync_buttons()

    def check_terms(self) -> None:
        from doubao_typeless.services.terms import hints
        from doubao_typeless.storage.vocab_store import load_vocab, parse_mappings

        text = self.editor.toPlainText()
        notes = [item["hint"] for item in hints(text)]
        vocab_hits = [src for src, _dst in parse_mappings(load_vocab(self.app.data_dir)) if src and src in text]
        if vocab_hits:
            notes.append("词库命中：" + "、".join(vocab_hits[:8]))
        self.banner.setText("；".join(notes) if notes else "当前稿没有命中术语或词库")
        self.banner.show()

    def insert(self) -> None:
        self.app.update_pc_text(self.editor.toPlainText())
        self.app.review_editing = False
        self.app._save_draft()
        self.widget.hide()
        self._editing = False
        self.app.request_review_insert()

    def show(self) -> None:
        self.app._remember_external_target()
        if not self._editing:
            self.reload()
        self.widget.show()
        self.widget.raise_()
        self.widget.activateWindow()


class ClientWindow:
    def __init__(self, app, *, on_hide: Callable[[], None] | None = None, on_quit: Callable[[], None] | None = None):
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import (
            QCheckBox,
            QComboBox,
            QFormLayout,
            QFrame,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QPlainTextEdit,
            QPushButton,
            QScrollArea,
            QTabWidget,
            QToolButton,
            QVBoxLayout,
            QWidget,
        )

        self.app = app
        self._on_hide = on_hide
        self._on_quit = on_quit
        self._closing_for_quit = False
        self._session_sig = None
        stored = load_settings(app.data_dir)
        from doubao_typeless.ui.motion import interface_motion
        interface_motion().configure(stored.get('ui_motion',True))

        class ShellWindow(QWidget):
            def closeEvent(inner_self, event):
                self._close_event(event)

        w = ShellWindow()
        from doubao_typeless.services.v3_update import preview_version_label
        w.setWindowTitle(preview_version_label())
        w.resize(560, 600)
        style_root(w)
        root = QVBoxLayout(w)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        tabs = QTabWidget()
        brand = QLabel('Pocket Composer')
        brand.setProperty('role', 'title')
        root.addWidget(brand)
        subtitle = QLabel('手机随手说，电脑接着做')
        subtitle.setObjectName('muted')
        root.addWidget(subtitle)
        root.addWidget(tabs, 1)

        connect = QWidget()
        cl = QVBoxLayout(connect)
        cl.addWidget(QLabel("让手机成为更顺手的输入工具。"))
        from doubao_typeless.services.v3_update import preview_version_label

        version = QLabel(preview_version_label())
        version.setObjectName("muted")
        version.setWordWrap(True)
        cl.addWidget(version)
        self.version_label = version
        self.delivery_status = QLabel("")
        self.delivery_status.setWordWrap(True)
        self.delivery_status.setProperty("role", "status")
        cl.addWidget(self.delivery_status)
        card = QFrame()
        card.setObjectName("card")
        from PySide6.QtWidgets import QSizePolicy
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        card_l = QHBoxLayout(card)
        self.qr = QLabel()
        self.qr.setFixedSize(168, 168)
        card_l.addWidget(self.qr)
        info = QVBoxLayout()
        self.url_label = QLabel("")
        self.url_label.setWordWrap(True)
        info.addWidget(self.url_label)
        self._selected_address = None
        self.address_choice = QComboBox()
        self.address_choice.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.address_choice.setMinimumContentsLength(12)
        self.address_choice.setToolTip('默认优先使用局域网；多个网络时选择手机能访问的网卡')
        info.addWidget(self.address_choice)
        self.address_choice.activated.connect(self._select_address)
        refresh_addresses = QPushButton('刷新网络地址')
        refresh_addresses.clicked.connect(self._refresh_addresses)
        info.addWidget(refresh_addresses)
        self.code_label = QLabel("")
        info.addWidget(self.code_label)
        hint = QLabel("1. 手机与电脑连同一网络，扫码连接。\n2. 在下方允许手机插入或截电脑。\n3. 点一下目标输入框，再从手机或浮窗插入。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        info.addWidget(hint)
        btns = QHBoxLayout()
        copy = QPushButton("复制地址")
        copy.setObjectName("ghost")
        copy.clicked.connect(self.copy_url)
        help_btn = QPushButton("连接帮助")
        help_btn.setObjectName("ghost")
        help_btn.clicked.connect(self.show_help)
        rotate = QPushButton("重新配对")
        rotate.setObjectName("ghost")
        rotate.clicked.connect(self.rotate_code)
        btns.addWidget(copy)
        btns.addWidget(help_btn)
        btns.addWidget(rotate)
        info.addLayout(btns)
        info.addStretch(1)
        card_l.addLayout(info, 1)
        cl.addWidget(card)
        self.device_box = QLabel("还没有手机连上。扫码后在这里批准插入和截图。")
        self.device_box.setWordWrap(True)
        cl.addWidget(self.device_box)
        self.grant_row = QVBoxLayout()
        cl.addLayout(self.grant_row)
        self.device_note = QLabel("")
        self.device_note.setWordWrap(True)
        self.device_note.hide()
        cl.addWidget(self.device_note)
        self.practice_toggle = QToolButton()
        self.practice_toggle.setText("试一段文字（可选）")
        self.practice_toggle.setCheckable(True)
        self.practice_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.practice_toggle.setArrowType(Qt.RightArrow)
        cl.addWidget(self.practice_toggle)
        self.practice = QPlainTextEdit()
        self.practice.setPlaceholderText("可选：输入测试文字，再到「当前图文」预览。实际插入请选中外部输入框。")
        self.practice.textChanged.connect(self._practice_changed)
        self.practice.setFixedHeight(88)
        cl.addWidget(self.practice)
        self.practice.hide()
        self.practice_toggle.toggled.connect(lambda opened: (
            self.practice.setVisible(opened),
            self.practice_toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)))
        cl.addStretch(1)
        foot = QHBoxLayout()
        start = QPushButton("开始使用，收起窗口")
        start.setObjectName("primary")
        start.clicked.connect(self.hide_to_tray)
        diag = QPushButton("帮助与诊断")
        diag.setObjectName("ghost")
        diag.clicked.connect(self.show_help)
        foot.addWidget(start)
        foot.addStretch(1)
        foot.addWidget(diag)
        cl.addLayout(foot)
        connect_scroll = QScrollArea()
        connect_scroll.setWidgetResizable(True)
        connect_scroll.setFrameShape(QFrame.NoFrame)
        connect_scroll.setWidget(connect)
        tabs.addTab(connect_scroll, "连接手机")

        settings = QWidget()
        sl = QFormLayout(settings)
        sl.setRowWrapPolicy(QFormLayout.WrapLongRows)
        sl.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        sl.setVerticalSpacing(12)
        sl.addRow(QLabel("输入与启动"))
        self.hotkey_insert = QLineEdit(str(stored.get("hotkey_insert") or "<alt>+i"))
        self.hotkey_recall = QLineEdit(str(stored.get("hotkey_recall") or "<alt>+<shift>+i"))
        sl.addRow("插入并复制", self.hotkey_insert)
        sl.addRow("召回上次", self.hotkey_recall)
        self.hotkey_expand = QLineEdit(str(stored.get("hotkey_expand") or "<alt>+<shift>+e"))
        self.hotkey_capture = QLineEdit(str(stored.get("hotkey_capture") or "<alt>+<shift>+s"))
        sl.addRow("展开当前图文", self.hotkey_expand)
        sl.addRow("截图给手机", self.hotkey_capture)
        self.autostart = QCheckBox("登录 Windows 时启动（到托盘）")
        self.autostart.setChecked(bool(stored.get("autostart")))
        if sys.platform != 'win32':
            self.autostart.setText('登录启动（此平台暂未提供）')
            self.autostart.setChecked(False)
            self.autostart.setEnabled(False)
        self.start_min = QCheckBox("启动后先到托盘")
        self.start_min.setChecked(bool(stored.get("start_minimized")))
        sl.addRow(self.autostart)
        sl.addRow(self.start_min)
        self.phone_send_enabled = QCheckBox("允许手机确认后发送（插入仍不自动发送）")
        self.phone_send_enabled.setChecked(stored.get("phone_send_enabled") is True)
        self.phone_send_mode = QComboBox()
        self.phone_send_mode.addItem("Enter", "enter")
        self.phone_send_mode.addItem("Ctrl+Enter", "ctrl_enter")
        self.phone_send_mode.setCurrentIndex(1 if stored.get("phone_send_mode") == "ctrl_enter" else 0)
        sl.addRow(self.phone_send_enabled)
        sl.addRow("目标应用发送快捷键", self.phone_send_mode)
        ai_intro = QLabel("纠错与改写 · 按需调用")
        ai_intro.setToolTip('只在主动检查或改写时发送当前文字；不影响普通输入和画图。')
        ai_intro.setWordWrap(True)
        sl.addRow(ai_intro)
        self.byok_provider = QComboBox()
        for name, _url, _model in PROVIDER_PRESETS:
            self.byok_provider.addItem(name)
        self.byok_provider.currentIndexChanged.connect(self._apply_provider)
        sl.addRow("服务商", self.byok_provider)
        self.byok_endpoint = QLineEdit(str(stored.get("byok_endpoint") or ""))
        self.byok_key = QLineEdit(str(stored.get("byok_api_key") or ""))
        self.byok_key.setEchoMode(QLineEdit.Password)
        self.byok_model = QLineEdit(str(stored.get("byok_model") or ""))
        self.byok_endpoint.setPlaceholderText('域名、Base URL 或完整接口地址')
        from doubao_typeless.services.endpoints import endpoint_origin
        self._byok_key_origin=endpoint_origin(self.byok_endpoint.text()) if self.byok_key.text() else None
        self._byok_key_edited=False
        def bind_byok_key():
            origin=endpoint_origin(self.byok_endpoint.text())
            self._byok_key_origin=origin if self.byok_key.text() and origin[1] else None
            self._byok_key_edited=True
        self.byok_key.textChanged.connect(bind_byok_key)
        sl.addRow("接口地址", self.byok_endpoint)
        sl.addRow("API Key", self.byok_key)
        sl.addRow("模型 ID", self.byok_model)
        from doubao_typeless.services.byok import url_join_note

        self.byok_url_note = QLabel('自动补全接口地址')
        self.byok_url_note.setToolTip(url_join_note(self.byok_endpoint.text()))
        self.byok_url_note.setObjectName("muted")
        self.byok_url_note.setWordWrap(True)
        self.byok_endpoint.textChanged.connect(lambda t: self.byok_url_note.setToolTip(url_join_note(t)))
        self.byok_endpoint.editingFinished.connect(self._normalize_byok)
        sl.addRow(self.byok_url_note)
        from PySide6.QtWidgets import QGroupBox

        advanced_toggle = QToolButton()
        advanced_toggle.setText("高级模型参数")
        advanced_toggle.setCheckable(True)
        advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        advanced = QWidget()
        adv = QFormLayout(advanced)
        adv.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.byok_prompt = QPlainTextEdit()
        self.byok_prompt.setPlainText(str(stored.get("byok_prompt") or ""))
        self.byok_prompt.setFixedHeight(64)
        self.byok_temperature = QLineEdit(str(stored.get("byok_temperature") or ""))
        self.byok_timeout = QLineEdit(str(stored.get("byok_timeout") or ""))
        adv.addRow("附加说明", self.byok_prompt)
        adv.addRow("温度", self.byok_temperature)
        adv.addRow("超时秒", self.byok_timeout)
        sl.addRow(advanced_toggle)
        sl.addRow(advanced)
        opened = bool(stored.get("byok_prompt") or stored.get("byok_temperature") or stored.get("byok_timeout"))
        advanced_toggle.setChecked(opened)
        advanced_toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)
        advanced.setVisible(opened)
        advanced_toggle.toggled.connect(lambda yes: (
            advanced.setVisible(yes), advanced_toggle.setArrowType(Qt.DownArrow if yes else Qt.RightArrow)))
        self.byok_advanced = advanced
        self.byok_status = QLabel("")
        self.byok_status.setObjectName("muted")
        self.byok_status.setWordWrap(True)
        sl.addRow(self.byok_status)
        from doubao_typeless.ui.input_check import InputCheckSettings
        self.jev_settings = InputCheckSettings(sl, stored, w)
        sl.addRow(QLabel("词库（一行 错词 -> 正确）"))
        self.vocab = QPlainTextEdit()
        self.vocab.setPlainText(load_vocab(app.data_dir))
        self.vocab.setFixedHeight(120)
        sl.addRow(self.vocab)
        srow = QHBoxLayout()
        probe = QPushButton("测试连接")
        probe.setObjectName("ghost")
        probe.clicked.connect(self.probe_byok)
        export = QPushButton("导出诊断")
        export.setObjectName("ghost")
        export.clicked.connect(self.export_diagnostics)
        import_vocab = QPushButton("导入日用词库")
        import_vocab.setObjectName("ghost")
        import_vocab.clicked.connect(self.import_daily_vocab)
        update = QPushButton("检查更新")
        self.update_button = update
        update.setObjectName("ghost")
        update.clicked.connect(self.check_update)
        data_folder = QPushButton("数据与备份")
        def open_data_folder():
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.app.data_dir.resolve())))
        data_folder.clicked.connect(open_data_folder)
        from doubao_typeless.platform.desktop import platform_hint
        hint = platform_hint()
        if hint:
            platform_note = QLabel(hint)
            platform_note.setWordWrap(True)
            sl.addRow(platform_note)
        self.update_status = QLabel("")
        self.update_status.setWordWrap(True)
        self.update_status.hide()
        self.update_download = QPushButton("打开下载页")
        self.update_download.hide()
        def open_download():
            import webbrowser
            from doubao_typeless.services.v3_update import DOWNLOAD_PAGE
            webbrowser.open(DOWNLOAD_PAGE)
        self.update_download.clicked.connect(open_download)
        self.update_install = QPushButton("下载并重启更新")
        self.update_install.setObjectName("primary")
        self.update_install.hide()
        self.update_install.clicked.connect(self.install_update)
        self.update_cancel = QPushButton("取消下载")
        self.update_cancel.hide()
        self.update_cancel.clicked.connect(lambda: self._update_cancel.set())
        from PySide6.QtWidgets import QDialog
        self.update_dialog = QDialog(w, Qt.Window)
        self.update_dialog.setWindowTitle('Pocket Composer 更新')
        self.update_dialog.resize(430, 220)
        style_root(self.update_dialog)
        update_layout = QVBoxLayout(self.update_dialog)
        for control in (self.update_status, self.update_install, self.update_cancel, self.update_download):
            update_layout.addWidget(control)
        later = QPushButton('关闭')
        later.clicked.connect(self.update_dialog.hide)
        update_layout.addWidget(later)
        self._announced_update = None
        self.app.hud.register_companion(self.update_dialog)
        save = QPushButton("保存设置")
        save.setObjectName("primary")
        save.clicked.connect(self.save_settings)
        model_actions = QHBoxLayout()
        model_actions.addWidget(probe)
        model_actions.addWidget(import_vocab)
        model_actions.addStretch(1)
        sl.addRow(model_actions)
        srow.addWidget(export)
        srow.addWidget(update)
        srow.addWidget(data_folder)
        srow.addStretch(1)
        srow.addWidget(save)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.NoFrame)
        settings_scroll.setWidget(settings)
        settings_page = QWidget()
        settings_layout = QVBoxLayout(settings_page)
        settings_layout.setContentsMargins(0,0,0,0)
        settings_layout.addWidget(settings_scroll, 1)
        settings_layout.addLayout(srow)
        tabs.addTab(settings_page, "常用设置")

        recent = QWidget()
        rl = QVBoxLayout(recent)
        self.recent_list = QListWidget()
        rl.addWidget(self.recent_list, 1)
        rrow = QHBoxLayout()
        restore = QPushButton("恢复为当前新稿")
        restore.setObjectName("ghost")
        restore.clicked.connect(self.restore_as_draft)
        replay = QPushButton("重投上次快照")
        replay.setObjectName("ghost")
        replay.clicked.connect(self.replay_snapshot)
        rrow.addWidget(restore)
        rrow.addWidget(replay)
        rl.addLayout(rrow)
        note = QLabel("恢复为当前新稿会复制内容，不覆盖手机正在写的稿。重投使用已冻结快照。")
        note.setObjectName("muted")
        note.setWordWrap(True)
        rl.addWidget(note)
        tabs.addTab(recent, "最近内容")

        self.tabs = tabs
        self.widget = w
        self.app.hud.register_companion(w)
        self._clipboard = QGuiApplication.clipboard()
        self._hist_sig = None
        self._pairing_url = None
        self.timer = QTimer(w)
        self.timer.timeout.connect(self._tick_countdown)
        self.timer.start(1000)
        self.input_check_timer = QTimer(w)
        self.input_check_timer.setInterval(200)
        self.input_check_timer.timeout.connect(lambda: self.app.hud.set_input_check(self.app.input_check_tick()))
        self.input_check_timer.start()
        self._refresh_addresses()
        from doubao_typeless.build_info import release_layout
        self.update_timer = QTimer(w)
        self.update_timer.setInterval(6 * 60 * 60 * 1000)
        self.update_timer.timeout.connect(lambda: self.check_update(automatic=True))
        if release_layout():
            self.update_timer.start()
            QTimer.singleShot(3000, w, lambda: self.check_update(automatic=True))
        self.refresh()

    def _apply_provider(self, index: int) -> None:
        if index <= 0 or index >= len(PROVIDER_PRESETS):
            return
        _name, url, model = PROVIDER_PRESETS[index]
        if url:
            self.byok_endpoint.setText(url)
            self._normalize_byok()
        self.byok_model.setText(model)

    def _normalize_byok(self):
        from doubao_typeless.services.endpoints import normalize_endpoint,endpoint_origin
        try:
            normalized=normalize_endpoint(self.byok_endpoint.text())
            self.byok_endpoint.setText(normalized)
            if self._byok_key_origin and endpoint_origin(normalized)!=self._byok_key_origin and self.byok_key.text():
                self.byok_key.clear();self.byok_status.setText('地址已变 · 请填写 Key')
            elif self.byok_key.text():self._byok_key_origin=endpoint_origin(normalized)
            self.byok_url_note.setText('地址就绪' if normalized else '自动补全接口地址')
            self.byok_url_note.setToolTip(normalized)
            return True
        except ValueError as exc:
            self.byok_status.setText(str(exc));self.byok_url_note.setText('地址格式有误');return False

    def phone_url(self) -> str:
        return f"http://{self._selected_address or lan_ip()}:{self.app.port}/"

    def _refresh_addresses(self) -> None:
        from doubao_typeless.runtime import connection_addresses
        addresses = connection_addresses()
        self.address_choice.clear()
        self.address_choice.addItem('自动选择局域网地址', None)
        for address, label in addresses:
            self.address_choice.addItem(label, address)
        index = self.address_choice.findData(self._selected_address)
        self.address_choice.setCurrentIndex(max(0, index))
        if index < 0:
            self._selected_address = None
        self.refresh()

    def _select_address(self, index: int) -> None:
        self._selected_address = self.address_choice.itemData(index)
        self.refresh()

    def pairing_url(self) -> str:
        code = self.app.auth.current_pairing_challenge() or self.app.auth.new_pairing_challenge()
        return pairing_page_url(self.phone_url(), code)

    def _shown_pairing_url(self) -> str:
        if self.phone_url().startswith('http://127.0.0.1:'):
            return ''
        # 只有用户正看着连接页才新建配对挑战；后台同步/隐藏窗口不得持续产出可猜的新短码。
        code = self.app.auth.current_pairing_challenge()
        if code is None and self.widget.isVisible():
            code = self.app.auth.new_pairing_challenge()
        return pairing_page_url(self.phone_url(), code) if code else ""

    def copy_url(self) -> None:
        self._clipboard.setText(self.pairing_url())

    def rotate_code(self) -> None:
        self.app.auth.rotate_pairing_challenge()
        self.refresh()

    def show_help(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from doubao_typeless.services.v3_update import preview_version_label

        log = self.app.data_dir / "logs" / "v3.log"
        QMessageBox.information(
            self.widget,
            "帮助与诊断",
            f"{preview_version_label()}\n\n"
            "连接：手机与电脑连同一网络，扫描当前窗口的二维码；多个网卡可在连接页切换。\n"
            "Tailscale：优先使用局域网地址；选择 Tailscale 地址时，手机也需要接入同一虚拟网络。\n"
            "插入：在连接页允许手机插入，点一下目标输入框，再操作手机或浮窗。\n"
            "失败：文字已复制时可手动粘贴；图片结果待确认时点浮窗「恢复」。\n"
            "浮窗：拖动顶部可移动；展开后可返回；关闭设置仍在托盘运行。\n"
            "断线：手机继续保存草稿，电脑已收到的稿仍可使用。\n"
            "版本：手机设置与这里显示的版本号应一致。\n"
            f"日志：{log}",
        )

    def check_update(self, *, automatic: bool = False) -> None:
        import threading
        from PySide6.QtCore import QTimer, Qt
        from doubao_typeless.services.v3_update import check_preview_update
        if not automatic:
            self.update_dialog.setAttribute(Qt.WA_ShowWithoutActivating, False)
            self.update_dialog.show()
            self.update_dialog.raise_()
            self.update_dialog.activateWindow()
        if not self.update_button.isEnabled(): return
        self.update_button.setEnabled(False)
        self.update_status.setText("正在查询正式版本…")
        self.update_status.show(); self.update_download.hide(); self.update_install.hide()
        def work():
            def get_json(url):
                import httpx
                response = httpx.get(url, timeout=8, headers={'Accept':'application/vnd.github+json'})
                response.raise_for_status()
                return response.json()
            info = check_preview_update(get_json=get_json)
            def apply():
                self.update_status.setText(info['message'])
                self._update_package = info.get('package')
                self.update_install.setVisible(bool(self._update_package))
                self.update_download.show()
                self.update_button.setEnabled(True)
                if automatic and info.get('automatic_update_available', info.get('update_available')) and info.get('latest') != self._announced_update:
                    self._announced_update = info.get('latest')
                    self.update_dialog.setAttribute(Qt.WA_ShowWithoutActivating, True)
                    self.update_dialog.show()
            try: QTimer.singleShot(0, self.widget, apply)
            except RuntimeError: pass
        threading.Thread(target=work, daemon=True).start()

    def install_update(self) -> None:
        import threading
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox
        from doubao_typeless.services.v3_update import download_upgrade, start_upgrade
        from doubao_typeless.ui.single_instance import pipe_name
        package = getattr(self, '_update_package', None)
        if not package or getattr(self, '_updating', False):return
        if QMessageBox.question(self.update_dialog, '更新 Pocket Composer',
                f"下载并更新到 {package['version']}？下载完成后程序会正常退出并自动重启。\n"
                "当前工作区会保留；不会自动迁移旧框架的数据。") != QMessageBox.Yes:
            return
        self._updating = True
        self._update_cancel = threading.Event()
        self.update_install.setEnabled(False);self.update_button.setEnabled(False)
        self.update_cancel.show();self.update_status.setText('正在下载完整升级包…')
        def post(fn):
            try:QTimer.singleShot(0, self.widget, fn)
            except RuntimeError:pass
        def progress(done,total):
            post(lambda:self.update_status.setText(f'正在下载升级包… {done*100//total}%'))
        def work():
            try:
                path = download_upgrade(package,self.app.data_dir,progress=progress,cancelled=self._update_cancel.is_set)
                if self._update_cancel.is_set():raise RuntimeError('下载已取消，当前版本未改变')
                if not callable(getattr(self,'on_update_ready',None)):
                    raise RuntimeError('当前窗口不能完成重启，请从完整客户端更新')
                post(lambda:self.update_cancel.hide())
                acceptance = start_upgrade(path,self.app.data_dir,pipe=pipe_name())
                if self._update_cancel.is_set():
                    acceptance.with_suffix('.cancel').write_text('cancel',encoding='ascii')
                    raise RuntimeError('更新已取消，当前版本未改变')
            except Exception as exc:
                message=str(exc)
                def failed():
                    self._updating=False;self.update_cancel.hide()
                    self.update_install.setEnabled(True);self.update_button.setEnabled(True)
                    self.update_status.setText(message or '更新未完成，当前版本继续运行，请重试')
                post(failed)
                return
            def ready():
                import os
                self._pending_upgrade_acceptance = acceptance
                try:
                    acceptance.write_text(str(os.getpid()),encoding='ascii')
                except OSError:
                    self._pending_upgrade_acceptance = None
                    self._updating = False
                    self.update_install.setEnabled(True);self.update_button.setEnabled(True)
                    self.update_status.setText('无法确认升级交接，当前版本继续运行；请稍后重试')
                    return
                self.update_status.setText('升级程序已就绪，正在保存并退出；新版将自动打开…')
                self.on_update_ready()
            post(ready)
        threading.Thread(target=work,daemon=True).start()

    def import_daily_vocab(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from doubao_typeless.storage.vocab_store import daily_vocab_candidates, import_vocab_preview, inspect_vocab_file

        source = next((path for path in daily_vocab_candidates() if path.is_file()), None)
        if source is None:
            picked, _ok = QFileDialog.getOpenFileName(self.widget, "选择只读词库", "", "Text (*.txt);;All (*)")
            if not picked:
                return
            source = Path(picked)
        info = inspect_vocab_file(source)
        reply = QMessageBox.question(
            self.widget,
            "导入日用词库",
            f"从 {info['path']} 复制 {info['mappings']} 条到当前词库。\n不会改源文件，也不会写日用 config.json。",
        )
        if reply != QMessageBox.Yes:
            return
        result = import_vocab_preview(self.app.data_dir, source)
        self.vocab.setPlainText(load_vocab(self.app.data_dir))
        self.byok_status.setText(f"已复制 {result['imported']} 条新词到当前词库")

    def hide_to_tray(self) -> None:
        stored = load_settings(self.app.data_dir)
        if not stored.get("tray_explained") and self._on_hide:
            self._on_hide()
            save_settings(self.app.data_dir, {"tray_explained": True})
        self.widget.hide()
        if getattr(self, "timer", None):
            self.timer.stop()
        if self._on_hide:
            self._on_hide()

    def _close_event(self, event) -> None:
        if self._closing_for_quit:
            event.accept()
            return
        event.ignore()
        self.hide_to_tray()

    def _clear_grant_row(self) -> None:
        from PySide6.QtWidgets import QLayoutItem

        while self.grant_row.count():
            item: QLayoutItem = self.grant_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _pair_caption(self) -> str:
        if not self.app.auth.current_pairing_challenge():
            return "扫码即连。二维码已过期，打开本窗口或点「重新配对」获取新码"
        remain = int(self.app.auth.pairing_remaining_s())
        short = self.app.auth.current_short_code()
        if not short:
            return f"扫码即连。备用短码因多次输错已停用，扫码不受影响；点「重新配对」恢复  （{remain}s）"
        return f"扫码即连。备用短码 {short}  （{remain}s）"

    def _tick_countdown(self) -> None:
        self._refresh_phone_status()
        if not self.widget.isVisible() or self.qr.isHidden():
            return
        self.refresh()

    def _refresh_phone_status(self) -> None:
        # Connection changes must reach the HUD even while settings is in the tray.
        # Only repaint an already visible HUD: reconnecting must not reopen it.
        hud = self.app.hud
        phone_online = self.app.draft.editor_device_id in self.app.bridge.online_device_ids()
        if hud.phone_online != phone_online:
            hud.phone_online = phone_online
            if hud._widget is not None and hud._widget.isVisible():
                hud._apply_show()

    def _note(self, text: str) -> None:
        self.device_note.setText(text)
        self.device_note.setVisible(bool(text))

    def _practice_changed(self) -> None:
        if not self.app.update_practice_text(self.practice.toPlainText()):
            # 已恢复手机主稿时练习文字不进入待插入内容，避免 Alt+I 插入看不见的副本。
            self.practice.hide()
            self.practice_toggle.hide()
            self._note("电脑上已有手机稿，练习框已收起；请到「当前图文」查看或改字。")

    def _toggle_remember(self, device_id: str, value: bool) -> None:
        from doubao_typeless.storage.credentials import TrustStoreError

        try:
            if value:
                ok = self.app.remember_device_id(device_id)
                self._note("正在让这台手机保存配对，保存后 30 天内可自动续接。" if ok else "这台手机已离线或过期，请重新扫码后再记住。")
            else:
                self.app.unremember_device_id(device_id)
                self._note("已取消记住这台手机；当前连接不断开，其它手机不受影响。")
        except TrustStoreError:
            self._note("电脑没能保存设备信任（磁盘不可写），这台手机保持原状；请检查后重试。")
        self._session_sig = None
        self.refresh()

    def _save_nickname(self, device_id: str, editor) -> None:
        name = editor.text().strip()
        nicks = dict(load_settings(self.app.data_dir).get("device_nicknames") or {})
        if (nicks.get(device_id) or "") == name:
            return
        if name:
            nicks[device_id] = name
        else:
            nicks.pop(device_id, None)
        save_settings(self.app.data_dir, {"device_nicknames": nicks})
        self._session_sig = None
        self.refresh()

    def _resolve_claim(self, device_id: str, approve: bool) -> None:
        moved = self.app.resolve_asset_claim(device_id, approve)
        self._note(f"已把 {moved} 张旧图交给这台手机。" if approve and moved else
                   "已拒绝取回旧图；手机上的文字不受影响。" if not approve else "手机已离线，未转交旧图。")
        self._session_sig = None
        self.refresh()

    def refresh(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QListWidgetItem, QPushButton, QWidget, QHBoxLayout, QLineEdit

        url = self._shown_pairing_url()
        if url != getattr(self, "_pairing_url", None):
            self._pairing_url = url
            self.url_label.setText(self.phone_url())
            self.url_label.setToolTip("扫码已包含一次性配对信息，无需抄写长码")
            pix = qr_pixmap(url) if url else None
            if pix is not None and not pix.isNull():
                self.qr.setPixmap(pix)
            elif not url:
                self.qr.clear()
                self.qr.setText("未找到可用网络\n连接 Wi-Fi 或网线后刷新" if self.phone_url().startswith('http://127.0.0.1:')
                                else "二维码已过期\n打开窗口自动更新")
        self.code_label.setText(self._pair_caption())
        sessions = self.app.auth.public_sessions()
        online = self.app.bridge.online_device_ids()
        claims = self.app.pending_asset_claims()
        self._refresh_phone_status()
        phone_draft = self.app.draft.authority == "phone"
        sig = (tuple((s["session_id"], s["device_id"], s["allow_insert"], s["allow_capture"], s["remembered"],
                      s["remember_pending"], s["device_id"] in online, claims.get(s["device_id"], 0)) for s in sessions),
               phone_draft)
        if sig != self._session_sig:
            self._session_sig = sig
            self._clear_grant_row()
            if not sessions:
                self.qr.show()
                self.practice_toggle.setVisible(not phone_draft)
                self.practice.setVisible(self.practice_toggle.isChecked() and not phone_draft)
                self.device_box.setText("还没有手机连上。扫码后在这里批准插入和截图。")
            else:
                self.qr.show()
                self.practice.hide()
                self.practice_toggle.hide()
                from doubao_typeless.storage.credentials import device_label

                nicks = load_settings(self.app.data_dir).get("device_nicknames") or {}
                lines = []
                for index, item in enumerate(sessions, start=1):
                    device = item["device_id"]
                    name = device_label(device, nicks, index)
                    lines.append(
                        f"{name} · {'在线' if device in online else '离线，电脑保留最后收到的稿'}  插入={'开' if item['allow_insert'] else '关'}  "
                        f"截图={'开' if item['allow_capture'] else '关'}"
                        + (" · 等待手机保存配对" if item["remember_pending"] else " · 已记住" if item["remembered"] else "")
                    )
                    sid = item["session_id"]
                    nickname = QLineEdit(str(nicks.get(device) or ""))
                    nickname.setPlaceholderText(f"{name}（可改名）")
                    nickname.setMaximumWidth(140)
                    nickname.editingFinished.connect(lambda d=device, e=nickname: self._save_nickname(d, e))
                    allow_i = QPushButton("允许插入" if not item["allow_insert"] else "关闭插入")
                    allow_i.setObjectName("primary" if not item["allow_insert"] else "ghost")
                    allow_i.clicked.connect(lambda _=False, s=sid, v=not item["allow_insert"]: self.set_insert(s, v))
                    allow_c = QPushButton("允许截图" if not item["allow_capture"] else "关闭截图")
                    allow_c.setObjectName("ghost")
                    allow_c.clicked.connect(lambda _=False, s=sid, v=not item["allow_capture"]: self.set_capture(s, v))
                    remembered = bool(item["remembered"])
                    remember = QPushButton("取消记住" if remembered else "记住 30 天")
                    remember.setObjectName("ghost")
                    remember.setToolTip("只影响这一台手机")
                    remember.clicked.connect(lambda _=False, d=device, v=not remembered: self._toggle_remember(d, v))
                    revoke = QPushButton("撤销设备")
                    revoke.setObjectName("danger")
                    revoke.clicked.connect(lambda _=False, s=sid: self.revoke(s))
                    row_widget = QWidget()
                    row = QHBoxLayout(row_widget)
                    row.setContentsMargins(0,0,0,0)
                    for widget in (nickname, allow_i, allow_c, remember, revoke):
                        row.addWidget(widget)
                    self.grant_row.addWidget(row_widget)
                    if claims.get(device):
                        lines.append(f"{name} 请求取回之前连接上传的 {claims[device]} 张图（同一手机重新扫码后才需要）")
                        approve = QPushButton(f"允许取回 {claims[device]} 张旧图")
                        approve.setObjectName("primary")
                        approve.clicked.connect(lambda _=False, d=device: self._resolve_claim(d, True))
                        deny = QPushButton("拒绝")
                        deny.setObjectName("ghost")
                        deny.clicked.connect(lambda _=False, d=device: self._resolve_claim(d, False))
                        claim_widget = QWidget()
                        claim_row = QHBoxLayout(claim_widget)
                        claim_row.setContentsMargins(0,0,0,0)
                        claim_row.addWidget(approve)
                        claim_row.addWidget(deny)
                        claim_row.addStretch(1)
                        self.grant_row.addWidget(claim_widget)
                self.device_box.setText("\n".join(lines))
        hist_sig = tuple(
            (item["bundle"].get("bundle_id"), item.get("attempt_result"))
            for item in self.app.history.items[-20:]
        )
        if hist_sig != self._hist_sig:
            selected = None
            current = self.recent_list.currentItem()
            if current is not None:
                selected = (current.data(Qt.UserRole) or {}).get("bundle_id")
            self._hist_sig = hist_sig
            self.recent_list.clear()
            restore_row = None
            for item in reversed(self.app.history.items[-20:]):
                bundle = item["bundle"]
                preview = (bundle.get("text") or "").replace("\n", " ")[:48] or "（无文字）"
                result = _result_label(item.get("attempt_result"))
                row = QListWidgetItem(f"{result}  {len(bundle.get('assets') or [])}图  {preview}")
                row.setData(Qt.UserRole, bundle)
                if selected and bundle.get("bundle_id") == selected:
                    restore_row = row
                self.recent_list.addItem(row)
            if restore_row is not None:
                self.recent_list.setCurrentItem(restore_row)

    def _set_grants(self, session_id: str, **grants) -> None:
        from doubao_typeless.storage.credentials import TrustStoreError

        try:
            self.app.auth.set_grants(session_id, **grants)
        except TrustStoreError:
            self._note("电脑没能保存授权变更（磁盘不可写），原授权不变；请检查后重试。")
        except ValueError:
            self._note("这台手机的连接已过期，请重新扫码。")
        self.refresh()

    def set_insert(self, session_id: str, value: bool) -> None:
        self._set_grants(session_id, allow_insert=value)

    def set_capture(self, session_id: str, value: bool) -> None:
        self._set_grants(session_id, allow_capture=value)

    def revoke(self, session_id: str) -> None:
        from doubao_typeless.storage.credentials import TrustStoreError

        loop = getattr(self.app, "_loop", None)
        try:
            if loop is not None:
                import asyncio

                asyncio.run_coroutine_threadsafe(self.app.bridge.revoke_session(session_id), loop).result(3)
            else:
                self.app.auth.revoke(session_id)
        except TrustStoreError:
            self._note("电脑没能写入撤销记录（磁盘不可写），设备保持原状以免重启后自动续接；请检查后重试。")
        self.refresh()

    def save_settings(self) -> None:
        if not self._normalize_byok():return
        if self.jev_settings.provider.currentData()=='custom' and not self.jev_settings.normalize_custom():return
        from doubao_typeless.services.endpoints import endpoint_origin
        nicks = dict(load_settings(self.app.data_dir).get("device_nicknames") or {})
        payload = {
            "hotkey_insert": self.hotkey_insert.text().strip() or "<alt>+i",
            "hotkey_recall": self.hotkey_recall.text().strip() or "<alt>+<shift>+i",
            "hotkey_expand": self.hotkey_expand.text().strip() or "<alt>+<shift>+e",
            "hotkey_capture": self.hotkey_capture.text().strip() or "<alt>+<shift>+s",
            "autostart": self.autostart.isChecked(),
            "start_minimized": self.start_min.isChecked(),
            "phone_send_enabled": self.phone_send_enabled.isChecked(),
            "phone_send_mode": self.phone_send_mode.currentData(),
            "byok_endpoint": self.byok_endpoint.text().strip(),
            "byok_api_key": self.byok_key.text().strip(),
            "byok_key_reentered":bool(self._byok_key_edited and self._byok_key_origin==endpoint_origin(self.byok_endpoint.text())),
            "byok_model": self.byok_model.text().strip(),
            "byok_prompt": self.byok_prompt.toPlainText().strip(),
            "byok_temperature": self.byok_temperature.text().strip(),
            "byok_timeout": self.byok_timeout.text().strip(),
            "device_nicknames": nicks,
            **self.jev_settings.values(),
        }
        secret_warnings = save_settings(self.app.data_dir, payload) or []
        save_vocab(self.app.data_dir, self.vocab.toPlainText())
        stored = load_settings(self.app.data_dir)
        self.app.input_check.configure(stored)
        self.app.hud.set_motion(stored.get('ui_motion',True))
        self.app.byok.endpoint = stored["byok_endpoint"]
        self.app.byok.api_key = stored["byok_api_key"]
        self.app.byok.model = stored["byok_model"]
        self.app.byok.extra_prompt = stored["byok_prompt"]
        from doubao_typeless.app import _optional_float

        self.app.byok.temperature = _optional_float(stored["byok_temperature"])
        self.app.byok.timeout = _optional_float(stored.get("byok_timeout")) or 8.0
        failures = self.app.apply_hotkeys(
            stored["hotkey_insert"],
            stored["hotkey_recall"],
            expand=stored["hotkey_expand"],
            capture=stored["hotkey_capture"],
        )
        ok, err = apply_v3_autostart(bool(stored["autostart"]), data_dir=self.app.data_dir)
        if stored["autostart"] and not ok:
            self.byok_status.setText(f"设置已保存。开机自启未写入：{err}")
            self.byok_status.setObjectName("error")
        elif failures:
            self.byok_status.setText("已保存 · " + str(failures[0]))
            self.byok_status.setObjectName("error")
        else:
            self.byok_status.setText("设置已保存")
            self.byok_status.setObjectName("muted")
        if secret_warnings:
            warning = " ".join(secret_warnings)
            self.byok_status.setText(self.byok_status.text() + "。" + warning)
            self.byok_status.setObjectName("error")
            self.jev_settings.status.setText(warning)

    def probe_byok(self) -> None:
        from doubao_typeless.services.byok import ByokService, ERROR_LABELS

        if not self._normalize_byok():return
        endpoint = self.byok_endpoint.text().strip()
        key = self.byok_key.text().strip()
        model = self.byok_model.text().strip()
        if not endpoint or not key:
            self.byok_status.setText(ERROR_LABELS["no_key"])
            return
        from doubao_typeless.storage.settings_store import endpoint_authority, load_settings

        stored = load_settings(self.app.data_dir)
        if endpoint_authority(stored.get("byok_endpoint") or "") != endpoint_authority(endpoint):
            if key == (stored.get("byok_api_key") or "") and not self._byok_key_edited:
                self.byok_status.setText("换了服务地址，请重新填写并批准密钥后再测试")
                return
        from doubao_typeless.app import _httpx_json_post, _optional_float
        import threading

        timeout = _optional_float(self.byok_timeout.text()) or 8.0
        svc = ByokService(endpoint=endpoint, api_key=key, model=model, timeout=timeout,
                          extra_prompt=self.byok_prompt.toPlainText().strip(),
                          temperature=_optional_float(self.byok_temperature.text()), post=_httpx_json_post)
        self.byok_status.setText("正在测试连接…")
        host = self.widget

        def work() -> None:
            out = svc.polish("ping", draft_id="probe", revision=1, current_draft_id="probe", current_revision=1)
            used = out.get("model") or model
            def apply() -> None:
                if (self.byok_endpoint.text().strip(),self.byok_key.text().strip(),self.byok_model.text().strip())!=(endpoint,key,model):
                    self.byok_status.setText('配置已变 · 待检测');return
                labels={'unauthorized':'Key 无效','forbidden':'暂无权限','not_found':'检查接口地址',
                        'timeout':'连接超时','rate_limited':'稍后重试','format':'响应格式不兼容'}
                self.byok_status.setText('已连接' if out.get('status')=='ok' else labels.get(out.get('reason'),'连接未完成'))
                self.byok_status.setToolTip(f"{out.get('message') or ''}\n模型：{used}")
            try:
                from PySide6.QtCore import QTimer

                QTimer.singleShot(0, host, apply)
            except RuntimeError:
                pass  # 窗口已销毁，不能在工作线程操作 Qt 控件。

        threading.Thread(target=work, daemon=True).start()

    def _selected_bundle(self):
        from PySide6.QtCore import Qt

        item = self.recent_list.currentItem()
        if item is None:
            return None
        return item.data(Qt.UserRole)

    def restore_as_draft(self) -> None:
        bundle = self._selected_bundle()
        if bundle is None:
            return
        status = self.app.restore_history(bundle)
        if status != "ask":
            return
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self.widget)
        box.setWindowTitle("当前稿还在")
        box.setText("恢复上次会替换当前稿。当前稿不会自动丢掉，除非你确认替换。")
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel)
        if box.exec() != QMessageBox.Ok:
            return
        self.app.restore_history(bundle, replace=True)

    def replay_snapshot(self) -> None:
        bundle = self._selected_bundle()
        if bundle is None:
            return
        self.app.bridge.last_bundle = self.app.history.replay_bundle(bundle)
        self.app._commands.submit(self.app.insert_last)

    def export_diagnostics(self) -> None:
        from doubao_typeless.services.v3_diagnostics import write_snapshot

        path = write_snapshot(self.app)
        self.byok_status.setText(f"诊断已写出 {path.name}，不含密钥和正文")

    def show_window(self) -> None:
        self.widget.show()
        self.widget.raise_()
        self.widget.activateWindow()
        if getattr(self, "timer", None):
            self.timer.start(1000)
        self.refresh()


class DesktopShell:
    def __init__(self, app):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

        qt = QApplication.instance()
        qt.setQuitOnLastWindowClosed(False)
        qt.setWindowIcon(app_icon())
        self.app = app
        self.client = ClientWindow(app, on_hide=self._explained_tray)
        self.client.on_update_ready = self.quit
        self.review = ReviewPanel(app)
        self.tray = QSystemTrayIcon(app_icon())
        menu = QMenu()
        menu.addAction("打开客户端", self.client.show_window)
        menu.addAction("当前图文", self.review.show)
        menu.addAction("定位当前窗口输入框（不插入）", app.request_locate_composer)
        menu.addAction("召回上次", app.request_recall)
        menu.addAction("截图给手机", app.capture_region)
        self._pause_action = menu.addAction("暂停连接", self.toggle_pause)
        menu.addSeparator()
        menu.addAction("退出", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.setToolTip("Pocket Composer")
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()
        self._wake = listen_for_commands(self._on_ipc)
        self.app.ui_hook = self._from_service

    def _explained_tray(self) -> None:
        if not load_settings(self.app.data_dir).get("tray_explained"):
            self.tray.showMessage("Pocket Composer", "已在托盘运行。点图标可再打开窗口。")
            save_settings(self.app.data_dir, {"tray_explained": True})

    def _on_ipc(self, command: str) -> None:
        from PySide6.QtCore import QTimer

        host = self.client.widget
        if command == "show":
            QTimer.singleShot(0, host, self.client.show_window)
        elif command == "quit":
            QTimer.singleShot(0, host, self.quit)

    def _tray_activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.Trigger:
            self.client.show_window()

    def toggle_pause(self) -> None:
        self.app.bridge.paused = not self.app.bridge.paused
        self._pause_action.setText("恢复连接" if self.app.bridge.paused else "暂停连接")

    def _from_service(self, event: str, **_kw) -> None:
        from PySide6.QtCore import QTimer

        host = self.client.widget
        if event == "sync_wait":
            pass  # 统一HUD状态机已处理，不能只改一行文字却忘记停空闲计时。
        elif event == "restore_on_phone":
            QTimer.singleShot(0, host, lambda: self.tray.showMessage("恢复图文", "已发到手机，请在手机确认；当前内容没有被覆盖"))
        elif event in {"delivery_failed", "command_rejected"}:
            code = str(_kw.get("error_code") or "")
            from doubao_typeless.ui.insert_status import error_message
            text = error_message(_kw)
            def show_error():
                from doubao_typeless.app import _log
                if _kw.get('operation') == 'locate':
                    # Focus may have moved before the provider rejected its
                    # acknowledgement. Keep feedback visible above the target.
                    self.review.widget.hide()
                    self.app.hud.operation_event(event, **_kw)
                self.client.delivery_status.setText(text)
                if self.review.widget.isVisible():
                    self.review.banner.setText(text)
                    self.review.banner.show()
                _log(f'[v3.feedback] event={event} code={code} review={self.review.widget.isVisible()} '
                     f'hud={self.app.hud._widget.isVisible()}')
            QTimer.singleShot(0, host, show_error)
        elif event == "capture_region":
            QTimer.singleShot(0, host, self.app.capture_region)
        elif event == "capture_unavailable":
            text = {
                "CAPTURE_NO_PHONE": "没有在线的手机，截图未发送。请先在手机打开页面。",
                "CAPTURE_DENIED": "正在编辑的手机还没有截图权限。请在连接页点「允许截图」。",
                "CAPTURE_TARGET_AMBIGUOUS": "有多台手机在线，无法确定发给哪台。请在要用的手机上点「截电脑」。",
                "CAPTURE_TARGET_CHANGED": "框选期间手机已离线或权限变化，截图未发送。",
            }.get(str(_kw.get("error_code") or ""), "截图未发送。")
            QTimer.singleShot(0, host, lambda: self.tray.showMessage("截图给手机", text))
        elif event == "asset_claim":
            def show_claim():
                self.client._session_sig = None
                self.client.refresh()
                self.tray.showMessage("手机请求取回旧图", "同一部手机重新扫码后需要你确认。请在连接页允许或拒绝。")
            QTimer.singleShot(0, host, show_claim)
        elif event == "composer_located":
            def reveal_target():
                from doubao_typeless.app import _log
                self.review.widget.hide()
                self.app.hud.operation_event("composer_located")
                hud=self.app.hud
                _log(f'[v3.locator] stage=ui_ready review={self.review.widget.isVisible()} '
                     f'hud={hud._widget.isVisible()} surfaces={len(hud._foreground_surfaces)} '
                     f'selection={hud._body.textCursor().hasSelection()} '
                     f'slider_down={hud._body.verticalScrollBar().isSliderDown()}')
            QTimer.singleShot(0, host, reveal_target)
        elif event == "composer_pick":
            candidates=list(_kw.get("candidates") or [])
            QTimer.singleShot(0, host, lambda:self._pick_composer(candidates))
        elif event == "recovery_ask":
            QTimer.singleShot(0, host, self._ask_recovery)
        elif event == "phone_pending":
            QTimer.singleShot(0, host, self.review.note_phone_pending)
        elif event == "activity":
            # 手机每句同步都会触发；合并为一次刷新，避免连接页/审阅窗在 GUI 队列里积压。
            if getattr(self, "_activity_pending", False):
                return
            self._activity_pending = True
            def refresh_activity():
                self._activity_pending = False
                # 连接页隐藏时打开会自行刷新；不为每句话读设置文件、重建设备列表。
                if self.client.widget.isVisible():
                    self.client.refresh()
                if self.review.widget.isVisible() and not self.review._editing:
                    self.review.reload()
            QTimer.singleShot(0, host, refresh_activity)
        elif event == "expand_reference":
            def open_reference():
                self.review.input_check_details.expand.setChecked(True)
                self.review.show()
            QTimer.singleShot(0, host, open_reference)
        elif event == "expand":
            QTimer.singleShot(0, host, self.review.show)
        elif event in {"hide_after_insert", "new_draft"}:
            QTimer.singleShot(0, self.review.widget, self.review.widget.hide)

    def _pick_composer(self, candidates) -> None:
        if getattr(self,"_picker_open",False):return
        self._picker_open=True
        try:
            with self.app.hud.modal_pause():
                choice=ComposerPicker(self.client.widget,candidates).exec()
            if choice:self.app.request_choose_composer(choice)
        finally:self._picker_open=False

    def _ask_recovery(self) -> None:
        from doubao_typeless.services.delivery_progress import summarize_delivery
        previous = self.app._last_attempt
        progress = summarize_delivery(self.app.bridge.last_bundle or {}, previous.to_dict()) if previous else None
        if getattr(self, "_recovery_dialog_open", False):
            return
        self._recovery_dialog_open = True
        try:
            # The HUD is always-on-top. Without suspension it can cover a modal
            # button, making a physical click hit the disabled overlay instead.
            with self.app.hud.modal_pause():
                mode = RecoveryDialog(self.client.widget, confirm_image=self.app.can_confirm_image(), progress=progress).exec()
            if mode != "cancel":
                self.app.request_recovery(mode)
        finally:
            self._recovery_dialog_open = False

    def quit(self) -> None:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QTimer
        import asyncio

        if getattr(self, "_quit_pending", False):
            return
        loop = getattr(self.app, "_loop", None)
        if loop is None:
            QApplication.instance().quit()
            return
        self._quit_pending = True
        future = asyncio.run_coroutine_threadsafe(self.app.stop(), loop)
        self.tray.showMessage("Pocket Composer", "正在结束当前操作，草稿已保留")

        def check() -> None:
            if not future.done():
                QTimer.singleShot(75, self.client.widget, check)
                return
            self._quit_pending = False
            try:
                stopped = future.result()
            except Exception:
                stopped = False
            if stopped is False:
                acceptance = getattr(self.client, '_pending_upgrade_acceptance', None)
                if acceptance:
                    acceptance.with_suffix('.cancel').write_text('cancel',encoding='ascii')
                    self.client._pending_upgrade_acceptance = None
                    self.client._updating = False
                    self.client.update_install.setEnabled(True)
                    self.client.update_button.setEnabled(True)
                    self.client.update_status.setText('当前操作尚未结束，本次升级已撤销；稍后可重试')
                self.tray.showMessage("暂未退出", "目标程序尚未返回，未强制终止。当前版本继续可用，热键已恢复；可稍后再点退出。")
                return
            self.client._closing_for_quit = True
            self.tray.hide()
            from doubao_typeless.app import _log
            _log('[v3.shutdown] stage=qt_quit')
            QApplication.instance().quit()
        QTimer.singleShot(0, self.client.widget, check)


def _show_startup_error(exc: BaseException) -> None:
    text = f"{exc}\n\n可在帮助与诊断中查看日志。程序没有在无界面状态下继续运行。"
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        qt = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "无法启动 Pocket Composer", text)
        if qt is not None:
            pass
    except Exception:
        traceback.print_exc()
        print(text, file=sys.stderr)


def run_desktop(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    minimized = "--minimized" in argv
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError as exc:
        print("需要 PySide6 才能打开图形客户端", flush=True)
        raise SystemExit(1) from exc

    qt = QApplication.instance() or QApplication(argv)
    from doubao_typeless.platform.qt_bridge import initialize
    initialize()
    qt.setQuitOnLastWindowClosed(False)
    qt.setStyleSheet(STYLESHEET)
    apply_ui_font(qt)
    if "--quit" in argv:
        return 0 if request_quit() else 1
    from doubao_typeless.ui.single_instance import identify_running
    from doubao_typeless.build_info import build_info
    active = identify_running()
    if active:
        expected = build_info()["source_sha"]
        if active.get("unknown") or active.get("source_sha") != expected:
            QMessageBox.warning(None, "已有另一版本正在运行",
                "为避免重复快捷键和打开错版本，请先从旧版托盘正常退出，再打开这份新版本。\n"
                "程序不会结束旧进程，也不会覆盖其数据。")
            return 1
        if request_show():
            from doubao_typeless.services.v3_update import acknowledge_update_launch
            acknowledge_update_launch()
            return 0

    from doubao_typeless.app import V3App, set_log
    from doubao_typeless.runtime import v3_data_dir
    from doubao_typeless.runtime_lock import InstanceLock

    data_dir = v3_data_dir()
    lock = InstanceLock(data_dir / "instance.lock")
    if not lock.acquire():
        if request_show():
            return 0
        QMessageBox.warning(
            None,
            "已经在运行",
            "另一个实例已在运行，但没能唤起窗口。请从托盘打开，或结束后再试。不会强杀已有进程。",
        )
        return 1

    logger = FileLogger(data_dir / "logs" / "v3.log", also_print=True)
    set_log(logger)
    try:
        app = V3App(data_dir=data_dir, instance_lock=lock)
        app.hud.start()
        # Build the native surfaces/control listener before exposing HTTP readiness.
        # Otherwise a second --quit/show can reach a half-started process and time out
        # while expensive first-use font/icon/widget initialization is still running.
        shell = DesktopShell(app)
        if shell._wake is None or not shell._wake.isListening():
            raise RuntimeError("无法建立本机控制入口，已停止启动")
        logger("[v3.lifecycle] native_shell_ready")
        loop = app.start_background(start_hud=False)
        app._loop = loop
        try:
            stored = load_settings(app.data_dir)
            # 经 apply_hotkeys 记录组合键，退出超时恢复时才能按原设置重新注册。
            failures = app.apply_hotkeys(
                str(stored.get("hotkey_insert") or "<alt>+i"),
                str(stored.get("hotkey_recall") or "<alt>+<shift>+i"),
                expand=str(stored.get("hotkey_expand") or "<alt>+<shift>+e"),
                capture=str(stored.get("hotkey_capture") or "<alt>+<shift>+s"),
            )
            if failures:
                logger(f"[v3] 热键注册失败: {failures}")
        except Exception as exc:
            logger(f"[v3] 热键未启动: {exc}")
        shell.client.refresh()
        logger("[v3.lifecycle] desktop_event_loop_ready")
        from PySide6.QtCore import QTimer
        from doubao_typeless.services.v3_update import acknowledge_update_launch
        QTimer.singleShot(0, shell.client.widget, acknowledge_update_launch)
        stored = load_settings(app.data_dir)
        if minimized or stored.get("start_minimized"):
            shell.tray.show()
        else:
            shell.client.show_window()
        return qt.exec()
    except Exception as exc:
        logger(f"[v3] 启动失败: {exc}")
        _show_startup_error(exc)
        try:
            lock.release()
        except Exception:
            pass
        return 1
