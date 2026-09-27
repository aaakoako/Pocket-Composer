"""Animated categorical reference map. No radial scores or implied success rate."""
import time
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QFont
from PySide6.QtWidgets import QWidget, QSizePolicy
from doubao_typeless.services.input_check import REFERENCE_DIMENSIONS, REFERENCE_STATES
from doubao_typeless.ui.icons import icon


class ReferenceChart(QWidget):
    def __init__(self, parent=None, *, compact=False):
        super().__init__(parent)
        self.states = {}
        self.presentation_enabled = True
        self.motion = True
        self.started = 0.
        self.phase = 1.
        self.setMinimumHeight(132)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.timer = QTimer(self)
        self.timer.setInterval(25)
        self.timer.timeout.connect(self._tick)
        self.setAccessibleName('本段输入参考图')
        self.hide()

    def sizeHint(self):
        return QSize(300, 132)

    def set_motion(self, enabled):
        self.motion = bool(enabled)
        if not self.motion:
            self.timer.stop(); self.phase = 1.; self.update()

    def set_result(self, result):
        rows = {r['id']: r.get('state', 'unknown') for r in result.get('references', [])
                if r.get('id') in REFERENCE_DIMENSIONS}
        self.setVisible(self.presentation_enabled and result.get('status') == 'ready' and bool(rows))
        if rows == self.states:
            return
        self.states = rows
        descriptions = [title + '：' + REFERENCE_STATES.get(rows.get(key), '暂无法判断')
                        for key, (title, _) in REFERENCE_DIMENSIONS.items()]
        detail = '本段参考，未读取上文；不代表成功率。\n' + '\n'.join(descriptions)
        self.setToolTip(detail)
        self.setAccessibleDescription(detail)
        self.started = time.monotonic()
        self.phase = 0. if self.motion else 1.
        if self.motion and self.isVisible():
            self.timer.start()
        self.update()

    def _tick(self):
        self.phase = min(1., (time.monotonic() - self.started) / .55)
        if self.phase >= 1. or not self.isVisible():
            self.timer.stop()
        self.update()

    def showEvent(self, event):
        if self.motion and self.states:
            self.started = time.monotonic(); self.phase = 0.; self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont(self.font()); font.setPixelSize(12); painter.setFont(font)
        center = QPointF(self.width() / 2, 64)
        reach = max(45, min(115, self.width() / 2 - 32))
        nodes = [QPointF(center.x(), 27), QPointF(center.x() + reach, 64),
                 QPointF(center.x(), 101), QPointF(center.x() - reach, 64)]
        names = list(REFERENCE_DIMENSIONS)
        for i, (key, point) in enumerate(zip(names, nodes)):
            state = self.states.get(key, 'unknown')
            unknown = state in {'unknown', 'na', 'tentative_na'}
            provisional = state.startswith('tentative_')
            color = '#8b94aa' if unknown else '#6156dc'
            pen = QPen(QColor('#dce1ec'), 1.5)
            pen.setStyle(Qt.DashLine if unknown or provisional else Qt.SolidLine)
            painter.setPen(pen); painter.drawLine(center, point)
            if not unknown:
                pulse = center + (point - center) * (1 - (1 - self.phase) ** 3)
                painter.setPen(Qt.NoPen); painter.setBrush(QColor(color)); painter.drawEllipse(pulse, 2.5, 2.5)
            painter.setPen(QPen(QColor(color), 1.3, Qt.DashLine if unknown or provisional else Qt.SolidLine))
            painter.setBrush(QColor('#f1f0ff' if not unknown else '#f6f7fb'))
            painter.drawEllipse(point, 12, 12)
            symbol = ('check' if state == 'clear' else 'help' if 'partial' in state else
                      'link' if 'context' in state else 'inspect' if state == 'tentative_clear' else None)
            if symbol:
                icon(symbol, color).paint(painter, int(point.x()-8), int(point.y()-8), 16, 16)
            else:
                painter.setPen(QPen(QColor(color), 1.5))
                if state in {'na','tentative_na'}:
                    painter.drawLine(point + QPointF(-4,0), point + QPointF(4,0))
                # Unknown remains hollow; it is never a zero value.
            painter.setPen(QColor('#515b72'))
            label = REFERENCE_DIMENSIONS[key][0]
            rect = (QRectF(point.x()-30, 0 if i == 0 else 115, 60, 16) if i in {0,2}
                    else QRectF(point.x()-30, 82, 60, 16))
            painter.drawText(rect, Qt.AlignCenter, label)
        painter.setPen(Qt.NoPen); painter.setBrush(QColor('#ffffff'))
        painter.drawRoundedRect(QRectF(center.x()-30, 46, 60, 36), 9, 9)
        painter.setPen(QColor('#687087'))
        painter.drawText(QRectF(center.x()-30, 47, 60, 16), Qt.AlignCenter, '仅本段')
        small = QFont(font); small.setPixelSize(10); painter.setFont(small)
        painter.drawText(QRectF(center.x()-30, 63, 60, 14), Qt.AlignCenter, '上文未读')


class ReferenceStrip(ReferenceChart):
    """Quiet segmented sidebar: categorical cues, never invented percentages."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.status = 'disabled'
        self.setAccessibleName('输入参考侧栏')
        self._measure()

    def _measure(self):
        from PySide6.QtGui import QFontMetrics
        self.chart_font = QFont(self.font())
        self.chart_font.setPixelSize(min(14, max(11, self.fontMetrics().height() - 3)))
        metrics = QFontMetrics(self.chart_font)
        self.row_height = metrics.height() + 16
        self.setFixedSize(max(100, metrics.horizontalAdvance('上下文或看上文') + 12),
                          self.row_height * 4 + 8)

    def changeEvent(self, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.FontChange:
            self._measure()
        super().changeEvent(event)

    def set_result(self, result):
        self.status = result.get('status', 'disabled')
        if self.status in {'disabled', 'empty'}:
            self.hide()
            return
        # Reserve the rail while enabled, but never display a previous draft's result.
        rows = result.get('references', []) if self.status == 'ready' else []
        super().set_result({'status':'ready', 'references': rows or [
            {'id':key,'state':'unknown'} for key in REFERENCE_DIMENSIONS]})
        self.setToolTip(self.accessibleDescription() + '\n分段仅区分状态，不是评分或执行成功率。')

    def sizeHint(self):
        return self.size()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont(self.chart_font); painter.setFont(font)
        metrics = painter.fontMetrics()
        labels = {'clear':'清楚','partial':'有歧义','context':'看上文','unknown':'未判断','na':'不涉及',
                  'tentative_clear':'大致清楚','tentative_partial':'疑歧义',
                  'tentative_context':'或看上文','tentative_na':'或无关'}
        for i, (key, (title, _)) in enumerate(REFERENCE_DIMENSIONS.items()):
            state = self.states.get(key, 'unknown')
            base = state.removeprefix('tentative_')
            y = 4 + i * self.row_height
            color = '#5b5ce2' if base == 'clear' else '#a47736' if base == 'partial' else '#8b94aa'
            painter.setPen(QColor('#687087'))
            painter.drawText(QRectF(0,y,self.width(),metrics.height()),Qt.AlignLeft, title)
            label = labels.get(state, '未判断')
            if self.status in {'checking','waiting'}:label = '待更新'
            if self.status in {'error','no_key'}:label = '不可用'
            small = QFont(font);small.setPixelSize(max(10,font.pixelSize()-2))
            painter.setFont(small);painter.setPen(QColor(color))
            painter.drawText(QRectF(0,y,self.width(),metrics.height()),Qt.AlignRight|Qt.AlignVCenter,label)
            painter.setFont(font)
            filled = 8 if base == 'clear' else 4 if base == 'partial' else 0
            width = (self.width()-7*3)/8
            for segment in range(8):
                rect = QRectF(segment*(width+3), y+metrics.height()+3, width, 6)
                active = segment < filled
                painter.setPen(QPen(QColor(color if active else '#dce1ec'), .8,
                    Qt.DashLine if state.startswith('tentative_') or base in {'unknown','context','na'} else Qt.SolidLine))
                paint = QColor(color if active else '#f0f2f7')
                if active:paint.setAlphaF(.3+.7*self.phase)
                painter.setBrush(paint);painter.drawRoundedRect(rect,1.5,1.5)
