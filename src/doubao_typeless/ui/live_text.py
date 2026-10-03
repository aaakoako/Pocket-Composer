"""手机连续更新时只改动正文变化的尾部：选区、光标和滚动位置在变化点之前时原样保留。

Qt 的文本位置以 UTF-16 码元计数，Python 字符串按码点计数；emoji 等增补平面字符在两者中长度不同，
所有交给 QTextCursor 的位置都必须先换算。"""
from __future__ import annotations

import os


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def change_start(old: str, new: str) -> int:
    """两版正文第一个不同之处的 UTF-16 位置（相同时为全长）。"""
    return utf16_len(os.path.commonprefix([old, new]))


def replace_tail(document, old: str, new: str) -> int:
    """在 document 上把 old 与 new 的公共前缀之后的部分替换为 new 的尾部，返回变化起点（UTF-16）。

    用独立的 QTextCursor 编辑文档，不改编辑框自身的光标；位于变化点之前的选区/光标位置不受影响。
    调用方负责屏蔽编辑框信号（程序同步不能被当作用户编辑）。"""
    from PySide6.QtGui import QTextCursor
    prefix = os.path.commonprefix([old, new])
    start = utf16_len(prefix)
    edit = QTextCursor(document)
    edit.setPosition(start)
    edit.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
    tail = new[len(prefix):]
    if tail:
        edit.insertText(tail)
    elif edit.hasSelection():
        edit.removeSelectedText()
    # 与 setPlainText 一致：程序同步的内容不进撤销栈，用户按撤销不会把手机内容退回旧版。
    document.clearUndoRedoStacks()
    return start


def clamp_position(document, position: int) -> int:
    """把旧位置限制在文档范围内（UTF-16，文档末尾的段落符不可选）。"""
    return max(0, min(position, document.characterCount() - 1))
