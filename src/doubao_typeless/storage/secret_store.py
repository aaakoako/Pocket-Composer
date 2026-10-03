"""BYOK密钥使用Windows凭据库；失败仅在内存保留，不默认写明文文件。"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from pathlib import Path

SERVICE = "DoubaoTypelessV3Preview"
_MEMORY: dict[str, str] = {}
CLEANUP_WARNING = "系统凭据已保存，但旧明文密钥文件未能删除；请检查 secrets 目录权限后重新保存。"


def _finish_os_write(data_dir: Path, name: str) -> str:
    # 系统写入已成功：清理失败不能伪装成凭据库失败或内存回退。
    _MEMORY.pop(_target(data_dir, name), None)
    try:
        _file_path(data_dir, name).unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning(CLEANUP_WARNING)
        return "os_cleanup_pending"
    return "os"


def _native_keyring():
    # Select OS stores explicitly: never pick a third-party plaintext backend.
    if sys.platform == 'darwin':
        from keyring.backends.macOS import Keyring
    else:
        from keyring.backends.SecretService import Keyring
    return Keyring()


def _target(data_dir: Path, name: str) -> str:
    digest = hashlib.sha256(str(Path(data_dir).resolve()).encode("utf-8")).hexdigest()[:16]
    return f"{SERVICE}/{digest}/{name}"


def _file_path(data_dir: Path, name: str) -> Path:
    return Path(data_dir) / "secrets" / f"{name}.txt"


def put_secret(data_dir: Path, name: str, value: str) -> str:
    value = (value or "").strip()
    if not value:
        delete_secret(data_dir, name)
        return "cleared"
    if sys.platform == "win32" and os.environ.get("DT_V3_SECRET_FILE") != "1":
        try:
            import win32cred

            # pywin32把str编码为WCHAR；读取必须按UTF-16LE，不能按UTF-8。
            win32cred.CredWrite(
                {
                    "Type": win32cred.CRED_TYPE_GENERIC,
                    "TargetName": _target(data_dir, name),
                    "UserName": SERVICE,
                    "CredentialBlob": value,
                    "Comment": "isolated V3 preview BYOK",
                    "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
                },
                0,
            )
        except Exception:
            _MEMORY[_target(data_dir, name)] = value
            return "memory"
        return _finish_os_write(data_dir, name)
    if os.environ.get("DT_V3_SECRET_FILE") == "1":
        path = _file_path(data_dir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return "file"
    if sys.platform != 'win32':
        try:
            _native_keyring().set_password(SERVICE, _target(data_dir, name), value)
        except Exception:
            pass
        else:
            return _finish_os_write(data_dir, name)
    _MEMORY[_target(data_dir, name)] = value
    return "memory"


def get_secret(data_dir: Path, name: str) -> str:
    key = _target(data_dir, name)
    # 凭据库本次写入失败时，新输入的内存值应优先于旧凭据；空值也是明确清除。
    if key in _MEMORY:
        return _MEMORY[key]
    if sys.platform == "win32" and os.environ.get("DT_V3_SECRET_FILE") != "1":
        try:
            import win32cred

            blob = win32cred.CredRead(key, win32cred.CRED_TYPE_GENERIC)
            raw = blob.get("CredentialBlob") or b""
            if isinstance(raw, bytes):
                return raw.decode("utf-16-le").rstrip("\x00").strip()
            return str(raw).strip()
        except Exception:
            return ""
    if os.environ.get("DT_V3_SECRET_FILE") == "1":
        path = _file_path(data_dir, name)
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    elif sys.platform != 'win32':
        try:
            return _native_keyring().get_password(SERVICE, key) or ''
        except Exception:
            return ''
    return ""


def delete_secret(data_dir: Path, name: str) -> None:
    key = _target(data_dir, name)
    failed = False
    if sys.platform == "win32":
        try:
            import win32cred
            win32cred.CredDelete(key, win32cred.CRED_TYPE_GENERIC)
        except Exception:
            failed = True
    elif os.environ.get('DT_V3_SECRET_FILE') != '1':
        try:
            _native_keyring().delete_password(SERVICE, key)
        except Exception:
            failed = True
    path = _file_path(data_dir, name)
    if path.is_file():
        path.unlink()
    if failed:
        _MEMORY[key] = ""
    else:
        _MEMORY.pop(key, None)
