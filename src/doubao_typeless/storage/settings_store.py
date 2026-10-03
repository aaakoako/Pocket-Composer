"""Isolated V3 settings. Never writes daily-use config.json. API keys leave this file."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from doubao_typeless.storage.secret_store import get_secret, put_secret


def endpoint_authority(url: str) -> tuple[str, str, int | None]:
    from doubao_typeless.services.endpoints import endpoint_origin
    return endpoint_origin(url)


def settings_path(data_dir: Path) -> Path:
    return Path(data_dir) / "settings.json"


def load_settings(data_dir: Path) -> dict[str, Any]:
    defaults = {
        "byok_endpoint": "",
        "byok_api_key": "",
        "byok_model": "",
        "hotkey_insert": "<alt>+i",
        "hotkey_recall": "<alt>+<shift>+i",
        "hotkey_expand": "<alt>+<shift>+e",
        "hotkey_capture": "<alt>+<shift>+s",
        "hud_position": None,
        "autostart": False,
        "start_minimized": False,
        "phone_send_enabled": False,
        "phone_send_mode": "enter",
        "tray_explained": False,
        "device_nicknames": {},
        "byok_temperature": "",
        "byok_timeout": "",
        "byok_prompt": "",
        "jev_enabled": False,
        "jev_api_key": "",
        "jev_vercel_key": "",
        "jev_provider": "typesafe",
        "jev_openrouter_key": "",
        "jev_custom_key": "",
        "jev_custom_endpoint": "",
        "jev_custom_model": "jev-latest",
        "ui_motion": True,
        "jev_emotion": True,
        "jev_voice_note": False,
    }
    path = settings_path(data_dir)
    if not path.is_file():
        out = dict(defaults)
        out["byok_api_key"] = get_secret(data_dir, "byok_api_key")
        out["jev_api_key"] = get_secret(data_dir, "jev_api_key")
        out["jev_vercel_key"] = get_secret(data_dir, "jev_vercel_key")
        out["jev_openrouter_key"] = get_secret(data_dir,"jev_openrouter_key")
        out["jev_custom_key"] = get_secret(data_dir,"jev_custom_key")
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    out = dict(defaults)
    for key in defaults:
        if key in data:
            out[key] = data[key]
    out["byok_endpoint"] = str(out.get("byok_endpoint") or "")
    out["byok_model"] = str(out.get("byok_model") or "")
    out["hotkey_insert"] = str(out.get("hotkey_insert") or "<alt>+i")
    out["hotkey_recall"] = str(out.get("hotkey_recall") or "<alt>+<shift>+i")
    out["hotkey_expand"] = str(out.get("hotkey_expand") or "<alt>+<shift>+e")
    out["hotkey_capture"] = str(out.get("hotkey_capture") or "<alt>+<shift>+s")
    out["autostart"] = bool(out.get("autostart"))
    out["start_minimized"] = bool(out.get("start_minimized"))
    out["tray_explained"] = bool(out.get("tray_explained"))
    names = out.get("device_nicknames")
    out["device_nicknames"] = {
        str(key): str(value).strip()
        for key, value in (names.items() if isinstance(names, dict) else [])
        if str(value).strip()
    }
    out["byok_temperature"] = str(out.get("byok_temperature") or "")
    out["byok_timeout"] = str(out.get("byok_timeout") or "")
    out["byok_prompt"] = str(out.get("byok_prompt") or "")
    file_key = str(data.get("byok_api_key") or "")
    stored_key = get_secret(data_dir, "byok_api_key")
    if file_key and not stored_key:
        put_secret(data_dir, "byok_api_key", file_key)
        stored_key = file_key
        _rewrite_without_secrets(path, data)
    out["byok_api_key"] = stored_key
    for name in ('jev_api_key','jev_vercel_key','jev_openrouter_key','jev_custom_key'):
        out[name] = get_secret(data_dir,name)
        if data.get(name):
            if not out[name]:
                put_secret(data_dir,name,str(data[name]));out[name]=str(data[name])
            _rewrite_without_secrets(path,data)
    return out


def _rewrite_without_secrets(path: Path, data: dict[str, Any]) -> None:
    cleaned = dict(data)
    cleaned["byok_api_key"] = ""
    cleaned["jev_api_key"] = ""
    cleaned["jev_vercel_key"] = ""
    cleaned["jev_openrouter_key"] = ""
    cleaned["jev_custom_key"] = ""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


ALLOWED = {
    "jev_enabled", "jev_api_key", "jev_vercel_key", "jev_provider", "jev_emotion", "jev_voice_note",
    "jev_openrouter_key", "jev_custom_key", "jev_custom_endpoint", "jev_custom_model", "ui_motion",
    "hud_position",
    "byok_endpoint",
    "byok_api_key",
    "byok_model",
    "hotkey_insert",
    "hotkey_recall",
    "hotkey_expand",
    "hotkey_capture",
    "autostart",
    "start_minimized",
    "phone_send_enabled",
    "phone_send_mode",
    "tray_explained",
    "device_nicknames",
    "byok_temperature",
    "byok_timeout",
    "byok_prompt",
}


def save_settings(data_dir: Path, payload: dict[str, Any]) -> list[str]:
    path = settings_path(data_dir)
    current = load_settings(data_dir)
    previous_endpoint = str(current.get("byok_endpoint") or "")
    previous_jev_endpoint = str(current.get('jev_custom_endpoint') or '')
    previous_jev_key = str(current.get('jev_custom_key') or '')
    previous_key = str(current.get("byok_api_key") or "")
    current.update({k: payload[k] for k in payload if k in ALLOWED})
    new_endpoint = str(current.get("byok_endpoint") or "")
    new_key = str(current.get("byok_api_key") or "")
    if endpoint_authority(previous_endpoint) != endpoint_authority(new_endpoint) and new_key == previous_key and not payload.get('byok_key_reentered'):
        current["byok_api_key"] = ""
    if (endpoint_authority(previous_jev_endpoint)!=endpoint_authority(current.get('jev_custom_endpoint',''))
            and current.get('jev_custom_key')==previous_jev_key and not payload.get('jev_custom_key_reentered')):
        current['jev_custom_key']=''
    from doubao_typeless.storage.secret_store import CLEANUP_WARNING
    warnings = []
    for name in ("byok_api_key", "jev_api_key", "jev_vercel_key", "jev_openrouter_key", "jev_custom_key"):
        if put_secret(data_dir, name, str(current.get(name) or "")) == "os_cleanup_pending":
            if CLEANUP_WARNING not in warnings:
                warnings.append(CLEANUP_WARNING)
    on_disk = dict(current)
    on_disk["byok_api_key"] = ""
    on_disk["jev_api_key"] = ""
    on_disk["jev_vercel_key"] = ""
    on_disk["jev_openrouter_key"] = ""
    on_disk["jev_custom_key"] = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(on_disk, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return warnings
