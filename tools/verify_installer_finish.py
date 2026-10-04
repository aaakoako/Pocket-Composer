"""CI 桌面上的真实安装向导：默认启动勾选、完成按钮、快捷方式与 EXE 就绪。

仅在 GitHub runner 执行；使用测试注册表、独立数据和 IPC，不操作日用安装。
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid
from urllib.request import urlopen
from urllib.parse import urlparse

import win32con
import win32gui
import win32process
import win32com.client


def wait_for(check, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.1)
    raise TimeoutError('Installer UI or application readiness timed out')


def verify(directory, report):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Native installer UI verification is restricted to the disposable CI desktop')
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtNetwork import QLocalSocket
    qt = QCoreApplication.instance() or QCoreApplication([])
    installers = list(directory.glob('*-installer-test_Setup.exe'))
    assert len(installers) == 1
    shell = win32com.client.Dispatch('WScript.Shell')
    links = [Path(shell.SpecialFolders(name)) / 'DoubaoTypeless Installer Test.lnk'
             for name in ('Desktop', 'Programs')]
    assert not any(link.exists() for link in links), 'Existing test shortcuts must not be overwritten'
    report.parent.mkdir(parents=True, exist_ok=True)
    result = {'passed': False, 'mode': 'native NSIS wizard on disposable CI desktop'}
    with tempfile.TemporaryDirectory(prefix='finish-', dir=report.parent.resolve()) as temporary:
        root = Path(temporary)
        install = root / 'application'
        data = root / 'data'
        data.mkdir()
        (data / 'settings.json').write_text(json.dumps({key: '<smoke-disabled>' for key in
            ('hotkey_insert', 'hotkey_recall', 'hotkey_expand', 'hotkey_capture')}))
        pipe = 'InstallerFinish-' + uuid.uuid4().hex
        receipt = root / 'ready.txt'
        env = {**os.environ, 'DT_V3_DATA_DIR': str(data), 'DT_V3_PIPE': pipe,
               'DT_UPDATE_READY_FILE': str(receipt)}
        env.pop('QT_QPA_PLATFORM', None)
        child = subprocess.Popen([str(installers[0].resolve()), f'/D={install}'], env=env)

        def window():
            found = []
            win32gui.EnumWindows(lambda hwnd, _: found.append(hwnd) if
                win32process.GetWindowThreadProcessId(hwnd)[1] == child.pid and
                win32gui.IsWindowVisible(hwnd) else None, None)
            return found[0] if found else None

        def controls(hwnd):
            found = []
            win32gui.EnumChildWindows(hwnd, lambda h, _: found.append(h), None)
            return found

        def quit_app():
            sock = QLocalSocket()
            sock.connectToServer(pipe)
            if not sock.waitForConnected(1000):
                return False
            sock.write(b'quit\n')
            sock.waitForBytesWritten(1000)
            sock.waitForReadyRead(2000)
            answer = bytes(sock.readAll())
            sock.close()
            return json.loads(answer).get('accepted') == 'quit'

        try:
            hwnd = wait_for(window)
            next_button = win32gui.GetDlgItem(hwnd, 1)
            # Welcome -> directory -> install. Wait for the native page controls.
            win32gui.SendMessage(next_button, win32con.BM_CLICK, 0, 0)
            wait_for(lambda: any(win32gui.GetClassName(h) == 'Edit' and
                win32gui.IsWindowVisible(h) for h in controls(hwnd)))
            win32gui.SendMessage(next_button, win32con.BM_CLICK, 0, 0)

            def run_checkbox():
                return next((h for h in controls(hwnd) if win32gui.IsWindowVisible(h) and
                             win32gui.GetWindowText(h) == '打开 Pocket Composer'), None)

            checkbox = wait_for(run_checkbox)
            assert win32gui.SendMessage(checkbox, win32con.BM_GETCHECK, 0, 0) == win32con.BST_CHECKED
            result['finish_launch_checked_by_default'] = True
            from PIL import ImageGrab
            ImageGrab.grab(window=hwnd).save(report.with_suffix('.png'))
            assert all(link.is_file() for link in links)
            targets = [shell.CreateShortcut(str(link)).TargetPath for link in links]
            assert len(set(targets)) == 1
            exe = Path(targets[0])
            assert exe.is_file() and install in exe.parents
            assert all(Path(shell.CreateShortcut(str(link)).WorkingDirectory) == exe.parent for link in links)
            result['desktop_and_start_menu_target'] = str(exe.relative_to(install))
            win32gui.SendMessage(next_button, win32con.BM_CLICK, 0, 0)
            assert child.wait(timeout=20) == 0
            wait_for(receipt.is_file)
            info = json.loads((exe.parent / '_internal/build-info.json').read_text())
            assert receipt.read_text().strip() == info['source_sha']
            port = urlparse((data / 'pair.txt').read_text().splitlines()[0]).port
            with urlopen(f'http://127.0.0.1:{port}/', timeout=5) as response:
                assert response.status == 200
            result['finish_started_current_build'] = True
            assert quit_app()
            def exited():
                log = data / 'logs/runtime.log'
                if not log.exists():
                    return False
                for line in log.read_text(encoding='utf-8').splitlines():
                    try:
                        event = json.loads(line[line.index('{'):])
                    except (ValueError, TypeError):
                        continue
                    if event.get('event') == 'process_exit' and event.get('exit_code') == 0:
                        return True
                return False
            wait_for(exited, 20)
            subprocess.run([str(install / 'Uninstall.exe'), '/S', f'_?={install}'], check=True, timeout=60)
            assert not any(link.exists() for link in links)
            result.update(passed=True, http_ready=True, ipc_quit=True, shortcuts_removed_on_uninstall=True)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            quit_app()
            if (install / 'Uninstall.exe').exists() and (install / 'versions').exists():
                subprocess.run([str(install / 'Uninstall.exe'), '/S', f'_?={install}'], timeout=60)
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    print(verify(args.directory, args.report))
