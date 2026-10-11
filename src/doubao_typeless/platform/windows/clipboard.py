"""Windows focus/clipboard helpers. Do not treat process name as Composer."""
from __future__ import annotations

import ctypes
import io
import sys
import time
from contextlib import contextmanager
from typing import Callable

if sys.platform == "win32":
    import pywintypes
    import win32clipboard
    import win32con
    import win32gui
    from PIL import Image

VK_CONTROL = 0x11
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002


def read_focus() -> tuple[str, str]:
    class_name, title, _hwnd = read_focus_fp()
    return class_name, title


def read_focus_fp() -> tuple[str, str, int]:
    if sys.platform != "win32":
        return ("", "", 0)
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    class_name = win32gui.GetClassName(hwnd)
    return class_name, title, int(hwnd or 0)


def send_paste() -> int:
    from doubao_typeless.platform.windows.native_input import send_paste as checked_paste

    return checked_paste()


def restore_focus(class_name: str, title: str, hwnd: int = 0) -> bool:
    if sys.platform != "win32":
        return False
    if hwnd:
        try:
            win32gui.SetForegroundWindow(int(hwnd))
            return True
        except Exception:
            pass
    matches: list[int] = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        got_class = win32gui.GetClassName(hwnd) or ""
        got_title = win32gui.GetWindowText(hwnd) or ""
        if got_class == class_name and got_title == title:
            matches.append(hwnd)
        return True

    win32gui.EnumWindows(_enum, None)
    if not matches:
        return False
    hwnd = matches[0]
    try:
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


@contextmanager
def opened_clipboard():
    deadline = time.monotonic() + 0.25
    while True:
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        win32clipboard.CloseClipboard()


def clipboard_sequence() -> int:
    if sys.platform != "win32":
        return 0
    api = ctypes.windll.user32.GetClipboardSequenceNumber
    api.restype = ctypes.c_uint32
    return int(api())


def read_clipboard_text() -> str | None:
    if sys.platform != "win32":
        return None
    # CloseClipboard 后剪贴板监听器可能立刻读取/转换格式；OpenClipboard
    # 成功也不保证随后的 GetClipboardData 不会暂时报 1418。重试整个读取事务，
    # 只处理访问错误：读到别的文字仍立即交给投递保护判断，绝不重写剪贴板。
    deadline = time.monotonic() + 0.25
    while True:
        try:
            with opened_clipboard():
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
                return None
        except (OSError, pywintypes.error) as exc:
            if time.monotonic() >= deadline:
                raise ClipboardReadError() from exc
            time.sleep(0.01)


class ClipboardReadError(OSError):
    error_code = "CLIPBOARD_UNAVAILABLE"


def set_clipboard_text(text: str) -> None:
    if sys.platform != "win32":
        return
    with opened_clipboard():
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)


def set_clipboard_png(data: bytes) -> None:
    if sys.platform != "win32":
        return
    image = Image.open(io.BytesIO(data)).convert("RGB")
    with io.BytesIO() as buf:
        image.save(buf, "BMP")
        dib = buf.getvalue()[14:]
    with opened_clipboard():
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
