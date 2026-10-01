"""Assemble Pocket Composer v3. Isolated data dir; idle HUD hidden."""
from __future__ import annotations

import asyncio
import copy
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from doubao_typeless.core.attempt import Attempt
from doubao_typeless.core.bundle import Draft, archive_if_match, freeze_bundle, source_snapshot
from doubao_typeless.core.intent import IntentLedger
from doubao_typeless.core.policy import classify_focus, is_own_window
from doubao_typeless.platform.desktop import same_target
from doubao_typeless.runtime import lan_ip, pick_port, v3_data_dir
from doubao_typeless.services.bridge_v3 import V3Bridge
from doubao_typeless.services.byok import ByokService
from doubao_typeless.services.capture import CaptureService
from doubao_typeless.services.delivery import DeliveryService
from doubao_typeless.services.history import HistoryService
from doubao_typeless.storage.asset_store import AssetStore
from doubao_typeless.storage.credentials import AuthService
from doubao_typeless.adapters.observable_target import from_env as observer_from_env
from doubao_typeless.ui.hud import HudController


def _httpx_json_post(url: str, body: dict, headers: dict, timeout: float = 8.0) -> dict:
    import httpx

    response = httpx.post(url, json=body, headers=headers, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("model response is not an object")
    return data


_log_impl = print


def _log(message: str) -> None:
    _log_impl(str(message))


def set_log(fn) -> None:
    global _log_impl
    _log_impl = fn


def _optional_float(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class V3App:
    def __init__(self, *, data_dir: Path | None = None, port: int = 8766, instance_lock=None):
        self.data_dir = Path(data_dir or v3_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.port = pick_port(port) if port else 0
        self._state_lock = threading.RLock()
        self._lock = instance_lock
        self._acquire_instance_lock()
        from doubao_typeless.build_info import build_info, release_layout
        if release_layout():
            from doubao_typeless.storage.upgrade import prepare_upgrade
            try:
                prepare_upgrade(self.data_dir, version=build_info()['version'])
            except Exception:
                self._lock.release()
                raise
        from doubao_typeless.services.command_queue import CommandQueue
        self._commands = CommandQueue(on_error=self._report_command_error)
        self._last_delivery_status: dict = {}
        self.auth = AuthService(store_path=self.data_dir / "trusted_devices.json")
        self.store = AssetStore(self.data_dir / "assets")
        from doubao_typeless.storage.db import V3DB
        from doubao_typeless.services.assets import CHUNK, UploadService

        self.db = V3DB(self.data_dir / "v3.sqlite")
        raw_chunk = os.environ.get("DT_V3_CHUNK_SIZE", "").strip()
        chunk = int(raw_chunk) if raw_chunk.isdigit() and int(raw_chunk) >= 1024 else CHUNK
        self.uploads = UploadService(self.store, self.db, chunk_size=chunk)
        self.ledger = IntentLedger()
        self.draft = Draft(
            draft_id=str(uuid.uuid4()),
            epoch=str(uuid.uuid4()),
            revision=0,
            editor_device_id="pc",
            text="",
        )
        self.history = HistoryService(self.data_dir / "history.json", persist=True, db=self.db)
        from doubao_typeless.storage.settings_store import load_settings, save_settings

        stored = load_settings(self.data_dir)
        from doubao_typeless.services.input_check import InputCheck
        self.input_check = InputCheck(stored)
        self.byok = ByokService(
            endpoint=stored.get("byok_endpoint") or "",
            api_key=stored.get("byok_api_key") or "",
            model=stored.get("byok_model") or "",
            extra_prompt=str(stored.get("byok_prompt") or ""),
            temperature=_optional_float(stored.get("byok_temperature")),
            timeout=_optional_float(stored.get("byok_timeout")) or 8.0,
            post=_httpx_json_post,
        )
        self.byok.current = lambda: {
            "draft_id": self.draft.draft_id,
            "revision": self.draft.revision,
        }
        from doubao_typeless.services.phone_send import PhoneSendService
        from doubao_typeless.platform.desktop import send_submit
        self.phone_send = PhoneSendService(data_dir=self.data_dir,
            options=lambda: load_settings(self.data_dir), read_focus=lambda: self._read_focus(),
            wait_modifiers=lambda: self._wait_modifiers(), emit=send_submit,
            authorize=self.auth.authorize, current_draft=lambda: self.draft,
            is_locked=lambda: self._session_locked(), is_elevated=lambda: self._target_elevated())
        self._last_suggestion = None
        self.hud = HudController(
            motion_enabled=bool(stored.get('ui_motion',True)),
            on_toggle_note=self.toggle_input_note,
            on_insert=self.request_insert,
            on_copy=self.copy_text,
            on_recover=lambda: self._notify_ui("recovery_ask"),
            position=stored.get("hud_position"),
            on_position=lambda pos: save_settings(self.data_dir, {"hud_position": pos}),
            on_expand=lambda: self._notify_ui("expand"),
            on_reference=lambda: self._notify_ui("expand_reference"),
        )
        self._observer = observer_from_env()
        self._last_attempt: Attempt | None = None
        self._recovery_needed = False
        self._saved_target: tuple[str, str] | None = None
        self._copied_text = ""
        self._last_target_fp: tuple[str, str, int] | None = None
        self.review_editing = False
        self._stopping = False
        self.phone_pending = None
        self.ui_hook = None
        from doubao_typeless.storage.draft_snapshot import load_draft, save_draft

        restored, missing = load_draft(self.data_dir, self.store)
        if restored is not None:
            self.draft = restored
            if missing:
                _log(f"[v3.draft] 快照缺图 {len(missing)}，不造假像素")
        self._save_draft = lambda: save_draft(self.data_dir, self.draft)
        self._desktop_edit = None
        try:
            import json
            raw_edit = json.loads((self.data_dir / "desktop-edit.json").read_text(encoding="utf-8"))
            if isinstance(raw_edit, dict) and isinstance(raw_edit.get("text"), str):
                self._desktop_edit = raw_edit
        except (OSError, ValueError):
            pass
        self.bridge = V3Bridge(
            port=self.port,
            auth=self.auth,
            store=self.store,
            draft=self.draft,
            on_activity=self._on_activity,
            on_intent=self.submit_delivery,
            on_capture=self._on_capture,
            on_recall=self.request_recall,
            on_phone_draft=self.apply_phone_update,
            is_pc_editing=lambda: self.review_editing,
            history_list=self._history_public,
            uploads=self.uploads,
            logger=_log,
            byok=self.byok,
            data_dir=self.data_dir,
            phone_send=self.phone_send,
            on_send=lambda session, body: self._commands.submit(self.phone_send.commit, session, body),
        )
        self.capture = CaptureService(grab=self._grab, hide_surfaces=self.hud.hide)
        self.delivery = DeliveryService(
            paste=self._paste,
            set_clipboard_image=self._set_image,
            set_clipboard_text=self._set_text,
            read_focus=self._read_focus,
            observe_image=self._observe_image,
            observe_text=self._observe_text,
            wait_modifiers=self._wait_modifiers,
            is_locked=self._session_locked,
            is_elevated=self._target_elevated,
            read_clipboard_text=self._read_clipboard_text,
            prepare_image=self._prepare_image_observation,
            is_cancelled=lambda: self._stopping,
            resume_input=self._resume_after_image,
            progress=lambda progress:self._notify_ui("delivery_progress", **progress),
        )

    def _report_command_error(self, exc: BaseException) -> None:
        # 不记录异常消息：系统/模型异常可能包含原文、密钥或私人路径。
        from doubao_typeless.runtime_diagnostics import record_runtime_exception
        record_runtime_exception("delivery", exc, self.data_dir)
        _log(f"[v3.delivery] error_type={type(exc).__name__}")
        self._notify_ui("delivery_failed", error_code="COMMAND_FAILED",
                        detail_code=getattr(exc, "error_code", ""))

    def submit_delivery(self, intent: dict, bundle: dict):
        """正式网络与桌面入口共用，入队前已冻结当前版本。"""
        return self._commands.submit(self.deliver_and_finish, dict(intent), self._with_input_note(copy.deepcopy(bundle)))

    def request_insert(self):
        """UI/热键非阻塞入口；总是返回Future，校验失败同样有明确结果。"""
        from concurrent.futures import Future
        if self.draft.authority == "phone":
            return self._local_command(self._insert_primary, bool(self._desktop_edit and self._desktop_edit["base"] == source_snapshot(self.draft)))
        with self._state_lock:
            if not (self.draft.text or self.draft.assets):
                self._notify_ui("delivery_failed", error_code="EMPTY_DRAFT")
                done = Future(); done.set_result({"result":"NO_STEPS", "error_code":"EMPTY_DRAFT"})
                return done
            try:
                bundle = freeze_bundle(self.draft, bundle_id=str(uuid.uuid4()))
            except ValueError as exc:
                code = str(exc)
                self._notify_ui("delivery_failed", error_code=code)
                done = Future(); done.set_result({"result":"NO_STEPS", "error_code":code})
                return done
            intent = self._session_intent("insert_current")
        return self._local_command(self.deliver_and_finish, dict(intent), self._with_input_note(copy.deepcopy(bundle)))

    def _local_command(self, callback, *args):
        future = self._commands.submit(callback, *args)
        # Rejected commands never enter the worker; hotkeys have no caller that
        # consumes their Future. Show feedback without queueing a later paste.
        if future.done():
            result = future.result() or {}
            if result.get('result') == 'BUSY':
                code = result.get('error_code', 'BUSY')
                _log(f'[v3.command] rejected={code}')
                self._notify_ui('command_rejected', error_code=code)
        return future

    def request_locate_composer(self):
        """用户明确点定位；只定位不投递，不改变草稿或之前的投递结果。"""
        return self._local_command(self._locate_composer, True)

    def _locate_composer(self, show_choices: bool = False) -> dict:
        from doubao_typeless.platform.desktop import locate_current
        from doubao_typeless.platform.desktop import FocusSnapshot, restore_target
        self._notify_ui("composer_locating")
        _log('[v3.locator] stage=start')
        try:
            before = self._read_focus()
            result = locate_current(self._saved_target)
            if result.get("status") != "found":
                if result.get("status") == "ambiguous":
                    self._composer_candidates = result.get("candidates") or []
                    if show_choices:
                        self._notify_ui("composer_pick", candidates=self._composer_candidates)
                code = "COMPOSER_AMBIGUOUS" if result.get("status") == "ambiguous" else "COMPOSER_NOT_FOUND"
                self._notify_ui("delivery_failed", error_code=code, operation="locate")
                return result
            item = result["candidate"]
            target = FocusSnapshot(item["class_name"], item["title"], item["hwnd"], item["pid"],
                                   item["control_hwnd"], tuple(item["runtime_id"]), item["kind"])
            # 查找期间用户切到其他应用时，不抢回来。自身控件点击仍允许恢复已保存窗口。
            current = self._read_focus()
            if (not is_own_window(*before[:2]) and not same_target(before, current)):
                self._notify_ui("delivery_failed", error_code="TARGET_CHANGED", operation="locate")
                return {"status": "changed"}
            if (not is_own_window(*current[:2]) and len(current) >= 4
                    and (current[2], current[3]) != (target.hwnd, target.pid)):
                self._notify_ui("delivery_failed", error_code="TARGET_CHANGED", operation="locate")
                return {"status": "changed"}
            if not restore_target(target):
                _log('[v3.locator] stage=restore_refused')
                self._notify_ui("delivery_failed", error_code="NEEDS_TARGET", operation="locate")
                return {"status": "focus_failed"}
            self._saved_target = target
            _log('[v3.locator] stage=restored')
            self._notify_ui("composer_located")
            return {"status": "located", "reason": result["reason"]}
        except Exception as exc:
            self._report_command_error(exc)
            self._notify_ui("delivery_failed", error_code="COMPOSER_NOT_FOUND")
            return {"status": "failed"}

    def request_choose_composer(self, candidate: dict):
        return self._commands.submit(self._choose_composer, dict(candidate))

    def _choose_composer(self, candidate: dict) -> dict:
        # 候选只来自上次受限扫描；选择不触发插入、不清当前稿。
        if candidate not in getattr(self, "_composer_candidates", []):
            return {"status":"stale"}
        from doubao_typeless.platform.desktop import FocusSnapshot,restore_target
        target=FocusSnapshot(candidate["class_name"],candidate["title"],candidate["hwnd"],candidate["pid"],
                             candidate["control_hwnd"],tuple(candidate["runtime_id"]),candidate["kind"])
        before=self._read_focus()
        if not is_own_window(*before[:2]) and (len(before)<4 or tuple(before[2:4])!=tuple(target[2:4])):
            return {"status":"changed"}
        if not restore_target(target) or not same_target(target,self._read_focus()):
            self._notify_ui("delivery_failed",error_code="NEEDS_TARGET")
            return {"status":"focus_failed"}
        self._saved_target=target;self._composer_candidates=[]
        self._notify_ui("composer_located")
        return {"status":"located"}

    def request_recall(self):
        return self._commands.submit(self.recall_last)

    def request_recovery(self, mode: str):
        future = self._commands.submit(self.confirm_recovery, mode)
        def completed(done):
            try:
                result = done.result() or {}
                code = result.get("error_code")
                _log(f"[v3.recovery] result={result.get('result', 'none')} error={code or 'none'}")
                if code:
                    # Queue rejection and early target validation must not vanish
                    # when the modal closes. Never enqueue an automatic retry.
                    self._notify_ui("delivery_failed", **result)
            except Exception as exc:
                self._report_command_error(exc)
        future.add_done_callback(completed)
        return future

    def update_pc_text(self, text: str) -> None:
        with self._state_lock:
            if self.draft.authority == "phone":
                # 电脑改字是明确的编辑副本，不抢手机稿的 revision。
                from doubao_typeless.storage.draft_snapshot import write_json_atomic
                if self._desktop_edit is None:
                    self._desktop_edit = {"base": source_snapshot(self.draft), "text": text}
                else:
                    self._desktop_edit["text"] = text
                write_json_atomic(self.data_dir / "desktop-edit.json", self._desktop_edit)
                return
            if self.draft.text != text:
                self.draft.text = text
                self.draft.revision += 1
                self._save_draft()

    def review_text(self) -> str:
        if self._desktop_edit and (self.review_editing or self._desktop_edit["base"] == source_snapshot(self.draft)):
            return self._desktop_edit["text"]
        return self.draft.text

    def request_review_insert(self):
        if self.draft.authority == "phone":
            return self._local_command(self._insert_primary, True)
        return self.request_insert()

    def _copy_fallback(self, bundle: dict, payload: dict) -> dict:
        """Explicit local Insert-and-copy still leaves a manual paste when targeting fails."""
        if payload.get("steps") or payload.get("error_code") not in {
            "NEEDS_TARGET", "OWN_WINDOW", "COMPOSER_NOT_FOUND", "COMPOSER_AMBIGUOUS",
            "TARGET_CHANGED", "TARGET_ELEVATED", "TARGET_PERMISSION_UNKNOWN",
            "TARGET_INSPECTION_TIMEOUT", "TARGET_INSPECTION_FAILED", "DELIVERY_FAILED"}:
            return payload
        try:
            bundle = self._with_input_note(bundle)
            self.history.record(bundle, attempt_result="NO_STEPS")
            text = str(bundle.get("text") or "")
            assets = bundle.get("assets") or []
            if text:
                self._set_text(text)
                copied = "文字"
            elif len(assets) == 1 and assets[0].get("asset_id"):
                self._set_image(self.store.get(assets[0]["asset_id"]))
                copied = "图片"
            else:
                return payload
            payload = {**payload, "copied": copied}
            return payload
        except Exception as exc:
            self._report_command_error(exc)
            return payload

    def _insert_primary(self, use_desktop_edit: bool = False) -> dict:
        """本地点插入冻结电脑当前可见稿；手机离线不影响已收到的内容。"""
        bundle = None
        try:
            with self._state_lock:
                bound = source_snapshot(self.draft)
                current = copy.deepcopy(self.draft)
                if use_desktop_edit and self._desktop_edit is not None:
                    if self._desktop_edit["base"] != bound:
                        raise ValueError("PHONE_CHANGED_REVIEW")
                    current.text = self._desktop_edit["text"]
                if not (current.text or current.assets):
                    raise ValueError("EMPTY_DRAFT")
                bundle = freeze_bundle(current, bundle_id=str(uuid.uuid4()))
                bundle['checked_text'] = current.text
                bundle["source_text"] = self.draft.text
                bundle["source_snapshot"] = bound
                from doubao_typeless.core.bundle import canonical_manifest_hash
                bundle["manifest_hash"] = canonical_manifest_hash(bundle)
            # Freeze before any focus scan; edits arriving during a scan are not
            # substituted into the user's action. Receipt binds the captured version.
            bundle = self._with_input_note(bundle)
            self._restore_external_target()
            focus = self._read_focus()
            kind = getattr(focus, "kind", focus[6] if len(focus)>6 else "")
            if (kind in {"unknown", "edit"} and bool(bundle.get("assets"))) or is_own_window(*focus[:2]):
                located = self._locate_composer()
                if located.get("status") != "located": raise ValueError("COMPOSER_NOT_FOUND")
                focus = self._read_focus()
            intent = self._session_intent("insert_current")
            intent["expected_focus"] = tuple(focus)
            return self.deliver_and_finish(intent, bundle)
        except ValueError as exc:
            payload = {"result":"NO_STEPS", "error_code":str(exc), "steps":[]}
        except Exception as exc:
            self._report_command_error(exc)
            payload = {"result":"NO_STEPS", "error_code":"DELIVERY_FAILED",
                       "detail_code":getattr(exc,"error_code",""), "steps":[]}
        payload = self._copy_fallback(bundle, payload) if bundle else payload
        self._notify_ui("delivery_failed", **payload)
        return payload

    def remember_connected(self) -> int:
        devices = set()
        loop = getattr(self, "_loop", None)
        for session in list(self.auth.sessions.values()):
            if time.time() > session.expires_at:
                continue
            secret = self.auth.remember_device(session)
            devices.add(session.device_id)
            if secret and loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self.bridge.send_to_session(
                        session.session_id,
                        {
                            "type": "device.remembered",
                            "device_id": session.device_id,
                            "device_secret": secret,
                        },
                    ),
                    loop,
                )
        return len(devices)

    def forget_connected(self) -> int:
        ids = {s.device_id for s in self.auth.sessions.values()}
        ids.update(self.auth.trusted)
        count = 0
        for device_id in ids:
            if self.auth.forget_device(device_id):
                count += 1
        return count

    def _notify_ui(self, event: str, **kwargs) -> None:
        if event in {'delivery_failed', 'delivery_complete', 'recovery_available', 'composer_located'}:
            if self._commands.when_ready(lambda: self._notify_ui(event, **kwargs)):
                return
        if event == 'composer_located':_log('[v3.locator] stage=ready_notification')
        # 状态首先进入真正的HUD，托盘提示只是补充；未知结果不能伪装成成功。
        if event in {"sync_wait", "delivery_start", "delivery_failed", "delivery_complete", "composer_locating", "composer_located", "delivery_progress", "recovery_available", "command_rejected"}:
            self._last_delivery_status = {"event": event, **kwargs}
            hud = getattr(self, "hud", None)
            if hud is not None:
                hud.operation_event(event, **kwargs)
        if event == "delivery_progress":
            device = getattr(self, "_active_delivery_device", None)
            loop = getattr(self, "_loop", None)
            if device and loop and loop.is_running() and getattr(self, "_active_delivery_primary", False):
                progress = {"type":"delivery.progress", "stage":kwargs.get("stage"),
                            "intent_id": getattr(self, "_active_delivery_intent", None),
                            "index":kwargs.get("index"), "total":kwargs.get("total")}
                for session in list(self.auth.sessions.values()):
                    if session.device_id == device:
                        asyncio.run_coroutine_threadsafe(self.bridge.send_to_session(session.session_id, progress), loop)
        hook = getattr(self, "ui_hook", None)
        if hook:
            hook(event, **kwargs)

    def _remember_external_target(self) -> None:
        try:
            focus = self._read_focus()
        except Exception:
            return
        if focus and not is_own_window(*focus[:2]):
            self._saved_target = focus

    def _restore_external_target(self) -> tuple[str, str]:
        # 检查超时/辅助进程退出必须停止本次操作，不能用空字符串降级放行。
        current = self._read_focus()
        if is_own_window(*current[:2]):
            if not self._saved_target:
                return ("DT-V3-HUD", "无法确认原输入框")
            from doubao_typeless.platform.desktop import restore_target
            if not restore_target(self._saved_target):
                return ("DT-V3-HUD", "无法恢复原输入框")
            current = self._read_focus()
        return current[0], current[1]

    def apply_phone_update(self, data: dict, *, allow_server_assets: bool = False) -> dict:
        with self._state_lock:
            return self._apply_phone_update(data, allow_server_assets=allow_server_assets)

    def _apply_phone_update(self, data: dict, *, allow_server_assets: bool = False) -> dict:
        if data.get("authority") == "phone":
            from doubao_typeless.services.phone_primary import apply_phone_snapshot
            from doubao_typeless.storage.draft_snapshot import save_draft, save_recovery
            ack = apply_phone_snapshot(self.draft, data, self.store,
                lambda draft: save_draft(self.data_dir, draft),
                lambda draft: save_recovery(self.data_dir, draft))
            if ack["changed"]:
                if self._desktop_edit and self._desktop_edit["base"] != source_snapshot(self.draft):
                    self.phone_pending = {"text": self.draft.text, "phone_primary": True}
                    self._notify_ui("phone_pending")
                self._on_activity(self.draft.text, len(self.draft.assets))
            return ack
        if self.draft.authority == "phone":
            raise ValueError("PHONE_PRIMARY_REQUIRED")
        if self.review_editing:
            self.phone_pending = dict(data)
            self._notify_ui("phone_pending")
            return {
                "revision": self.draft.revision,
                "parked": True,
                "durable": False,
                "hash": self.draft.acked_hash,
            }
        if data.get("assets") is not None and not allow_server_assets:
            raise ValueError("client assets rejected")
        if data.get("epoch") and str(data["epoch"]) != self.draft.epoch:
            return {
                "revision": self.draft.revision,
                "parked": False,
                "durable": False,
                "hash": self.draft.acked_hash,
                "error": "stale epoch",
            }
        if data.get("draft_id") and str(data["draft_id"]) != self.draft.draft_id:
            return {
                "revision": self.draft.revision,
                "parked": False,
                "durable": False,
                "hash": self.draft.acked_hash,
                "error": "stale draft",
            }
        from doubao_typeless.core.bundle import apply_draft_update
        from doubao_typeless.services.assets import resolve_asset_refs

        refs = data.get("asset_refs")
        from doubao_typeless.services.draft_assets import resolve_draft_assets
        if allow_server_assets and data.get("assets") is not None:
            assets = copy.deepcopy(data["assets"])
        else:
            assets = resolve_draft_assets(self.store, data, self.draft.assets)
        try:
            revision = int(data["revision"])
        except (KeyError, TypeError, ValueError):
            if allow_server_assets:
                revision = self.draft.revision + 1
            else:
                raise ValueError("invalid revision") from None
        before = (self.draft.text, self.draft.revision)
        update = {
            "text": data.get("text", self.draft.text),
            "revision": revision,
            "asset_refs": refs if refs is not None else [a.get("asset_id") for a in self.draft.assets],
        }
        if assets is not None:
            update["assets"] = assets
        apply_draft_update(self.draft, update)
        if data.get("_device_id"):
            self.draft.editor_device_id = str(data["_device_id"])
        changed = (self.draft.text, self.draft.revision) != before
        if changed:
            self._save_draft()
            self._on_activity(self.draft.text, len(self.draft.assets))
        return {
            "revision": self.draft.revision,
            "parked": False,
            "durable": changed,
            "hash": self.draft.acked_hash,
        }

    def accept_phone_pending(self) -> None:
        if self.draft.authority == "phone":
            # 手机版一直是镜像主稿，采用时只释放独立电脑编辑副本。
            self._desktop_edit = None
            self.phone_pending = None
            self.review_editing = False
            from doubao_typeless.storage.draft_snapshot import write_json_atomic
            write_json_atomic(self.data_dir / "desktop-edit.json", {})
            return
        # 用户明确选择采用冲突稿时才生成当前身份下的新修订；重连绝不自动这么做。
        with self._state_lock:
            pending = copy.deepcopy(self.phone_pending)
            if not pending:
                return
            self._preserve_current_draft()
            pending.update(draft_id=self.draft.draft_id, epoch=self.draft.epoch,
                           revision=self.draft.revision + 1)
            self.review_editing = False
            self._apply_phone_update(pending, allow_server_assets=True)
            self.phone_pending = None

    def keep_pc_edit(self) -> None:
        if self.draft.authority == "phone" and self._desktop_edit is not None:
            # 用户明确保留电脑文字：只改变本次编辑副本的基线，手机内容仍不被覆盖。
            from doubao_typeless.storage.draft_snapshot import write_json_atomic
            self._desktop_edit["base"] = source_snapshot(self.draft)
            write_json_atomic(self.data_dir / "desktop-edit.json", self._desktop_edit)
            self.phone_pending = None
        self.review_editing = True
        self._notify_ui("pc_kept")

    def _on_activity(self, text: str, image_count: int) -> None:
        # 同步线程只更新稿件与GUI消息；输入框检查只在明确操作时进行。
        if self.review_editing and self.draft.authority != "phone":
            self.phone_pending = {"text": text, "image_count": image_count}
            self._notify_ui("phone_pending")
            return
        if text or image_count:
            previews = []
            for asset in self.draft.assets:
                item = {"id": asset.get("local_id") or asset.get("asset_id"),
                        "status": asset.get("status", "ready"),
                        "render_revision": asset.get("render_revision", 1)}
                if item["status"] == "ready" and asset.get("asset_id"):
                    # 路径只由服务器资源存储构造；不接受手机提供的磁盘路径。
                    item["path"] = str(self.store.root / (asset["asset_id"] + ".bin"))
                previews.append(item)
            self.hud.show_receiving(text, image_count, assets=previews,
                revision=self.draft.revision, phone_primary=self.draft.authority == "phone",
                draft_key=(self.draft.draft_id, self.draft.epoch))
        else:
            self.hud.hide()
        self._notify_ui("activity")

    def _history_public(self) -> list:
        return [
            {
                "bundle_id": item["bundle"]["bundle_id"],
                "revision": item["bundle"].get("revision"),
                "asset_count": len(item["bundle"].get("assets") or []),
                "text_chars": len(item["bundle"].get("text") or ""),
                "attempt_result": item.get("attempt_result"),
            }
            for item in self.history.items
        ]

    def _grab(self, scope: str) -> bytes:
        from doubao_typeless.platform.desktop import grab_primary

        return grab_primary(scope, hide=self.hud.hide)

    def _on_capture(self, scope: str, request_id: str = "", session=None) -> dict:
        if session is None:
            raise ValueError("no session")
        self.hud.hide()
        blob = self.capture.capture(session, scope, request_id=request_id or str(uuid.uuid4()))
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(blob))
        meta = self.store.put_png(blob, width=image.width, height=image.height, role="screenshot")
        self.db.upsert_asset(
            meta["asset_id"],
            meta["sha256"],
            meta["bytes"],
            referenced=True,
            owner_session_id="device:" + session.device_id,
        )
        meta = dict(meta)
        meta["status"] = "editing"
        meta["role"] = "source"
        # 截图只生成源资源，不擅自提升手机稿的版本或把原图混入可投递附件。
        # capture.result 到手机后，由统一 draft.update 显式声明 editing 附件。
        return meta

    def _wait_modifiers(self) -> bool:
        from doubao_typeless.platform.desktop import wait_modifiers_up

        return wait_modifiers_up()

    def _session_locked(self) -> bool:
        from doubao_typeless.platform.desktop import session_locked

        return session_locked()

    def _target_elevated(self) -> bool:
        from doubao_typeless.platform.desktop import target_above_ours
        return target_above_ours()

    def _read_clipboard_text(self) -> str | None:
        try:
            from doubao_typeless.platform.desktop import read_clipboard_text

            return read_clipboard_text()
        except Exception:
            return None

    def _read_focus(self):
        from doubao_typeless.platform.desktop import read_target

        return read_target()

    def _paste(self) -> None:
        from doubao_typeless.platform.desktop import send_paste

        send_paste()
        # Diagnostic only: LastInputInfo also changes for our own injected keys.
        self._injected_input_stamp = self._input_stamp()
        # Pace separate clipboard pastes so the target can dispatch Ctrl+V.
        # This is not an upload wait or proof that the target accepted content.
        time.sleep(0.15)

    @staticmethod
    def _input_stamp():
        if sys.platform != "win32":return None
        try:
            import win32api
            return win32api.GetLastInputInfo()
        except Exception:return None

    def _resume_after_image(self, expected):
        from doubao_typeless.platform.windows.automation_host import host
        from doubao_typeless.platform.desktop import FocusSnapshot
        # Only refocus, never replay a paste. Attachment callbacks can move focus
        # again while UIA is returning, so recheck the same scope at most 3 times.
        for retry in range(3):
            current = self._read_focus()
            if same_target(current, expected):return current
            # Read before the barrier: input during this read must invalidate
            # recovery instead of becoming the helper's accepted timestamp.
            stamp = self._input_stamp()
            activity = getattr(self, '_delivery_input_activity', None)
            baseline = getattr(self, '_delivery_input_baseline', None)
            if activity is None or baseline is None or activity.snapshot() != baseline:
                _log('[v3.focus_resume] refused=input_changed_or_unavailable')
                return None
            anchor=getattr(self, "_image_baseline", None)
            if not anchor or not anchor.get("scope"):
                _log('[v3.focus_resume] refused=missing_scope')
                return None
            if stamp is None:return None
            value=host().call("resume_composer", {"anchor":anchor,"expected":list(expected),
                                                   "input_stamp":stamp})
            if activity.snapshot() != baseline:
                _log('[v3.focus_resume] refused=external_input_during_probe')
                return None
            if value:
                actual=FocusSnapshot(*value[:5],tuple(value[5]),value[6])
                if same_target(actual,expected) and same_target(actual,self._read_focus()):
                    return actual
            _log(f'[v3.focus_resume] retry={retry+1} reason={"postcheck_changed" if value else "helper_refused"}')
        return None

    def _set_image(self, data: bytes) -> None:
        from doubao_typeless.platform.desktop import set_clipboard_png

        if not data:
            return
        set_clipboard_png(data)

    def _set_text(self, text: str) -> None:
        from doubao_typeless.platform.desktop import set_clipboard_text

        set_clipboard_text(text)

    def _prepare_image_observation(self) -> None:
        if sys.platform != "win32":
            self._image_baseline = None
            return
        if self._observer is not None:
            return
        from doubao_typeless.adapters.cursor_windows import capture_image_baseline
        self._image_baseline = None
        try:
            self._image_baseline = capture_image_baseline()
        except Exception as exc:
            from doubao_typeless.runtime_diagnostics import record_runtime_exception
            record_runtime_exception("attachment_observation", exc, self.data_dir)

    def _observe_image(self) -> str:
        if self._observer is not None:
            return self._observer.observe_image()
        if sys.platform != "win32":
            return "unknown"
        from doubao_typeless.adapters.cursor_windows import observe_image

        try:
            return observe_image(getattr(self, "_image_baseline", None), cancelled=lambda:self._stopping)
        except Exception as exc:
            from doubao_typeless.runtime_diagnostics import record_runtime_exception
            record_runtime_exception("attachment_observation", exc, self.data_dir)
            return "unknown"

    def _observe_text(self) -> str:
        if self._observer is not None:
            return self._observer.observe_text()
        from doubao_typeless.adapters.generic_text import observe_text

        return observe_text()

    def _hydrate_bundle(self, bundle: dict) -> dict:
        hydrated = dict(bundle)
        assets = []
        for asset in hydrated.get("assets") or []:
            item = dict(asset)
            if "bytes_data" not in item:
                item["bytes_data"] = self.store.get(item["asset_id"])
            if item.get("sha256"):
                import hashlib
                if hashlib.sha256(item["bytes_data"]).hexdigest() != item["sha256"]:
                    raise ValueError("ASSET_CHANGED")
            assets.append(item)
        hydrated["assets"] = assets
        return hydrated

    def _session_intent(self, trigger: str) -> dict:
        # Local actions do not depend on a phone credential or its expiry.
        return {"intent_id": str(uuid.uuid4()), "trigger": trigger}

    def _keep_inserted_copy(self, bundle: dict, payload: dict | None = None) -> None:
        if payload and payload.get("error_code") == "CLIPBOARD_INTERFERENCE":
            return
        text = str(bundle.get("text") or "")
        self._copied_text = text
        if not text:
            return
        try:
            self._set_text(text)
        except Exception:
            _log("[v3.copy] 插入后保留剪贴板失败，稿未丢")
            self._notify_ui("copy_failed")

    def _delivery_blocked(self, bundle: dict | None = None) -> str:
        assets = (bundle or {}).get("assets") if bundle else self.draft.assets
        for asset in assets or []:
            status = str(asset.get("status") or "ready")
            if status in {"queued", "editing", "failed", "dirty"}:
                return "IMAGE_EDITING"
            if asset.get("role") == "source" and status != "ready":
                return "SOURCE_NOT_FINISHED"
        return ""

    def _maybe_start_next_draft(self, bundle: dict, payload: dict) -> bool:
        if payload.get("duplicate") or payload.get("error_code"):
            return False
        result = payload.get("result")
        steps = payload.get("steps") or []
        text_done = any(s.get("kind") == "text" and s.get("state") in {"injected", "observed", "unknown"}
                        for s in steps if isinstance(s, dict))
        planned = {a.get("asset_id") for a in bundle.get("assets") or []}
        pasted = {st.get("asset_id") for st in steps if st.get("kind")=="image"
                  and st.get("state") in {"injected", "observed"}
                  and st.get("evidence") in {"os_input_count", "target_attachment", "user_confirmed"}}
        # A complete explicit paste sequence may start the next draft; external
        # receipt stays UNKNOWN when only successful OS input is available.
        text_injected = any(st.get("kind")=="text" and st.get("state") in {"injected","observed"}
                            and st.get("evidence") in {"os_input_count","target_text"} for st in steps)
        complete_attempt = (bool(planned) and planned <= pasted
                            and (not bundle.get("text") or text_injected) and not payload.get("text_only"))
        may_rotate = result == "CONFIRMED" or (result in {"UNKNOWN","PARTIAL"} and
                       ((not planned and text_done) or complete_attempt))
        if not may_rotate:
            return False
        with self._state_lock:
            bound = bundle.get("source_snapshot")
            if bound is None or source_snapshot(self.draft) != bound:
                return False
            if self.draft.authority == "phone":
                # 仅通知已消耗这个确定快照。手机保存上次图文后自行创建下一段。
                self._notify_ui("new_draft")
                return True
            # 已在投递前保留快照。原子保存失败则恢复内存，不能回执清手机。
            before = copy.deepcopy(self.draft.__dict__)
            self.draft.text = ""
            self.draft.assets = []
            self.draft.revision += 1
            self.draft.epoch = str(uuid.uuid4())
            try:
                self._save_draft()
            except Exception:
                self.draft.__dict__.update(before)
                raise
        self._notify_ui("new_draft")
        return True

    def _phone_rotate_event(self, bundle: dict, rotated: bool, result: str = "") -> dict:
        source = bundle.get("source_snapshot") or {}
        archived = {
            "draft_id": bundle.get("draft_id"), "epoch": bundle.get("epoch"),
            "revision": bundle.get("revision"), "hash": bundle.get("manifest_hash"),
            "text": bundle.get("source_text", bundle.get("text") or ""),
            "source_text": bundle.get("source_text", bundle.get("text") or ""),
            "assets": source.get("assets"),
            "asset_refs": [a.get("asset_id") for a in (bundle.get("assets") or [])],
            "result": result,
        }
        return {
            "type": "draft.rotated", "event_id": str(uuid.uuid4()),
            "rotated": rotated, "result": result,
            "phone_primary": bundle.get("authority") == "phone",
            "generation": bundle.get("generation", 0),
            "owner_device_id": bundle.get("device_id") if bundle.get("authority") == "phone" else None,
            "archived": archived if rotated else None,
            "draft_id": self.draft.draft_id, "epoch": self.draft.epoch,
            "revision": self.draft.revision, "text": self.draft.text,
            "asset_refs": [a.get("asset_id") for a in self.draft.assets],
        }

    def _publish_phone_event(self, event: dict) -> None:
        from doubao_typeless.storage.draft_snapshot import write_json_atomic
        write_json_atomic(self.data_dir / "phone-event.json", event)
        self.bridge.last_phone_event = event
        loop = getattr(self, "_loop", None)
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(self.bridge.publish_phone_event(event), loop)

    def _after_insert(self, bundle: dict, payload: dict, *, publish: bool = True) -> dict:
        if payload.get("duplicate"):
            self._notify_ui("delivery_failed", error_code="DUPLICATE_INTENT")
            return payload
        if payload.get("result") == "BUSY":
            return payload
        if publish and payload.get("error_code") and not payload.get("steps"):
            payload = self._copy_fallback(bundle, payload)
        # 已发图片步骤不改剪贴板；尚未完成的图文仍留在恢复副本中。
        if any(step.get("kind") == "text" for step in payload.get("steps") or []) and not payload.get("error_code"):
            self._keep_inserted_copy(bundle, payload)
        from doubao_typeless.services.delivery_progress import summarize_delivery
        payload = {**payload, "progress": summarize_delivery(bundle, payload)}
        rotated = self._maybe_start_next_draft(bundle, payload)
        event = self._phone_rotate_event(bundle, rotated, str(payload.get("result") or ""))
        event["intent_id"] = payload.get("intent_id")
        event["progress"] = payload["progress"]
        self.phone_send.record_delivery(bundle, payload, self._last_target_fp)
        if payload.get("error_code"): event["error_code"] = payload["error_code"]
        self.bridge.last_phone_event = event
        if publish:
            self._publish_phone_event(event)
        payload = {**payload, "phone_event": event}
        self._notify_ui("hide_after_insert")
        if payload.get("error_code"):
            self._notify_ui("delivery_failed", **payload)
        else:
            self._notify_ui("delivery_complete", result=payload.get("result"), rotated=rotated,
                            text_sent=any(s.get("kind") == "text" for s in payload.get("steps") or []),
                            image_count=len(bundle.get("assets") or []), progress=payload["progress"])
        if (not rotated and not payload.get("error_code") and bundle.get("assets")
                and payload.get("result") in {"UNKNOWN", "PARTIAL"}):
            # 主流程暂停而不是自动重贴。用户的明确确认才允许继续下一张。
            self._notify_ui("recovery_available", progress=payload["progress"])
        return payload

    def _with_input_note(self, bundle: dict) -> dict:
        # Decorate the frozen delivery only. Source text/snapshot continue to
        # identify the exact phone draft for rotation and restore.
        if not bundle.get('input_note_frozen'):
            identity = (bundle.get('draft_id'), bundle.get('epoch'), bundle.get('revision'),
                        bundle.get('checked_text', bundle.get('source_text', bundle.get('text', ''))))
            decorated = str(bundle.get('text') or '')
            if identity == self.input_check_identity():
                decorated = self.input_check.decorate(identity, decorated)
            bundle = copy.deepcopy(bundle)
            from doubao_typeless.core.bundle import canonical_manifest_hash, TEXT_UTF8_LIMIT
            if len(decorated.encode('utf-8')) <= TEXT_UTF8_LIMIT:
                bundle['text'] = decorated
            bundle['input_note_frozen'] = True
            bundle['manifest_hash'] = canonical_manifest_hash(bundle)
        return bundle

    def deliver_and_finish(self, intent: dict, bundle: dict) -> dict:
        if not intent.get('recovery_mode'):
            bundle = self._with_input_note(bundle)
        # 仅收起可编辑详情以归还目标焦点，非激活HUD保持实际进度。
        self._notify_ui("delivery_start")
        self._notify_ui("hide_after_insert")
        try:
            payload = self._on_intent(intent, bundle)
            payload["intent_id"] = str(intent.get("intent_id") or "")
            result = self._after_insert(bundle, payload, publish=intent.get("_source") != "remote")
            return result
        except Exception as exc:
            self._report_command_error(exc)
            self._notify_ui("delivery_failed", error_code="FINALIZE_FAILED")
            return {"result": "UNKNOWN", "error_code": "FINALIZE_FAILED",
                    "intent_id": str(intent.get("intent_id") or ""),
                    "steps": payload.get("steps", []) if "payload" in locals() else []}

    def _on_intent(self, intent: dict, bundle: dict) -> dict:
        intent_id = str(intent.get("intent_id") or "")
        decision = self.ledger.begin(intent_id)
        if decision == "duplicate":
            return {"result": self.ledger.status(intent_id) or "UNKNOWN", "duplicate": True}
        if decision == "busy":
            return {"result": "BUSY", "error_code": "BUSY"}
        attempt = Attempt(attempt_id=str(uuid.uuid4()), intent_id=intent_id,
                          bundle_id=bundle.get("bundle_id", ""), adapter_id="generic_text")
        phase = "prepare"
        self._active_delivery_device = bundle.get("device_id")
        self._active_delivery_intent = intent_id
        self._active_delivery_primary = bundle.get("authority") == "phone"
        try:
            # 保存完整副本后才允许触碰剪贴板。未准备成功不执行任何按键。
            self.history.record(bundle, attempt_result="RUNNING")
            self.bridge.last_bundle = copy.deepcopy(bundle)
            hydrated = self._hydrate_bundle(bundle)
            phase = "focus"
            remote = intent.get("_source") == "remote"
            if remote:
                focus_now = self._read_focus()
                class_name, control = focus_now[0], focus_now[1]
            else:
                class_name, control = self._restore_external_target()
            focus = self._read_focus()
            self._last_target_fp = tuple(focus)
            if not is_own_window(*focus[:2]):
                self._saved_target = focus
            if intent.get("expected_focus") and not same_target(focus, intent["expected_focus"]):
                attempt.result, attempt.error_code = "NO_STEPS", "TARGET_CHANGED"
                return attempt.to_dict()
            if is_own_window(class_name, control):
                attempt.result, attempt.error_code = "NO_STEPS", "OWN_WINDOW"
            else:
                remote = intent.get("_source") == "remote"
                kind = classify_focus(class_name, control)
                from doubao_typeless.adapters.cursor_windows import identify
                attempt.adapter_id = "s2_paste_target" if kind == "paste" else identify(class_name, control)
                phase = "delivery"
                from doubao_typeless.core.attempt import Step
                valid_ids = {a["asset_id"] for a in bundle.get("assets") or []}
                for old in intent.get("verified_steps") or []:
                    if old.get("asset_id") in valid_ids and old.get("state") == "observed":
                        attempt.steps.append(Step(len(attempt.steps), "image", old["asset_id"], "observed", old["evidence"]))
                activity = None
                if hydrated.get('assets'):
                    from doubao_typeless.platform.desktop import start_input_activity
                    activity = start_input_activity()
                self._delivery_input_activity = activity
                self._delivery_input_baseline = None
                try:
                    # A synchronization barrier can block. Take it before
                    # DeliveryService's final target checks, never inside paste.
                    if activity is not None:self._delivery_input_baseline = activity.snapshot()
                    attempt = self.delivery.run(attempt, hydrated,
                        mode=str(intent.get("recovery_mode") or "full"),
                        skip_asset_ids=set(intent.get("skip_asset_ids") or []), remote=remote,
                        expected_focus=self._last_target_fp)
                finally:
                    self._delivery_input_activity = None
                    self._delivery_input_baseline = None
                    if activity is not None:activity.close()
            if intent.get("recovery_mode") == "text_only" and bundle.get("assets") and attempt.steps:
                # 用户选择只贴文字，不等于全部图片都已收到；保留待恢复图文。
                attempt.result = "PARTIAL"
            phase = "journal"
            self.history.record(bundle, attempt_result=attempt.result)
            self.db.record_attempt(attempt.attempt_id, bundle["bundle_id"], attempt.result,
                                   attempt.to_dict()["steps"])
        except Exception as exc:
            # 完整命令边界包括读图、焦点、平台、观察和持久化。
            attempt.result = "UNKNOWN"
            attempt.error_code = "ASSET_MISSING" if isinstance(exc, FileNotFoundError) else "DELIVERY_FAILED"
            attempt.detail_code = getattr(exc, "error_code", "")
            self._report_command_error(exc)
            try:
                self.history.record(bundle, attempt_result="UNKNOWN")
            except Exception:
                pass  # 投递前成功写下的 RUNNING 副本仍然用于恢复。
        finally:
            self.ledger.finish(intent_id, attempt.result)
            self._active_delivery_device = None
            self._active_delivery_intent = None
            self._active_delivery_primary = False
        self._last_attempt = attempt
        self._last_attempt_bundle_id = bundle.get("bundle_id")
        _log(f"[v3.delivery] result={attempt.result} phase={phase} steps={len(attempt.steps)}")
        result = attempt.to_dict()
        if intent.get("recovery_mode") == "text_only": result["text_only"] = True
        return result

    def insert_current(self) -> dict | None:
        if self.draft.authority == "phone":
            return self._insert_primary(False)
        if not (self.draft.text or self.draft.assets):
            return None
        blocked = self._delivery_blocked()
        if blocked:
            return {"result": "NO_STEPS", "error_code": blocked}
        try:
            bundle = freeze_bundle(self.draft, bundle_id=str(uuid.uuid4()))
        except ValueError as exc:
            self._notify_ui("delivery_failed", error_code=str(exc))
            return {"result": "NO_STEPS", "error_code": str(exc)}
        self.bridge.last_bundle = bundle
        intent = self._session_intent("insert_current")
        return self.deliver_and_finish(intent, bundle)

    def draft_image_previews(self) -> list[dict]:
        out: list[dict] = []
        for index, asset in enumerate(self.draft.assets):
            asset_id = str(asset.get("asset_id") or "")
            data = b""
            present = False
            if asset_id:
                try:
                    data = self.store.get(asset_id)
                    present = True
                except FileNotFoundError:
                    present = False
            out.append(
                {
                    "order": index + 1,
                    "asset_id": asset_id,
                    "present": present,
                    "bytes": len(data),
                    "data": data,
                    "width": asset.get("width"),
                    "height": asset.get("height"),
                }
            )
        return out

    def suggest_text(self, text: str) -> dict:
        bound = {
            "original": text,
            "draft_id": self.draft.draft_id,
            "epoch": self.draft.epoch,
            "revision": self.draft.revision,
        }
        out = self.byok.polish(
            text,
            draft_id=bound["draft_id"],
            revision=bound["revision"],
            current_draft_id=bound["draft_id"],
            current_revision=bound["revision"],
        )
        live_match = (
            self.draft.draft_id == bound["draft_id"]
            and self.draft.epoch == bound["epoch"]
            and self.draft.revision == bound["revision"]
            and self.review_text() == bound["original"]
        )
        if not live_match:
            out = {**out, "status": "stale", "reason": "draft moved after response", "text": None}
        suggested = out.get("text") if out.get("status") == "ok" else None
        self._last_suggestion = {**bound, "suggested": suggested, **out}
        return self._last_suggestion

    def apply_suggestion(self) -> bool:
        last = self._last_suggestion or {}
        if not last.get("suggested"):
            return False
        if self.draft.authority == "phone":
            if (last.get("draft_id"), last.get("epoch"), last.get("revision"), last.get("original")) != (
                    self.draft.draft_id, self.draft.epoch, self.draft.revision, self.review_text()):
                return False
            last["before_apply"] = self.review_text()
            self.update_pc_text(str(last["suggested"]))
            last["primary_applied"] = self.review_text()
            return True
        if (
            last.get("draft_id") != self.draft.draft_id
            or last.get("epoch") != self.draft.epoch
            or last.get("revision") != self.draft.revision
            or last.get("original") != self.draft.text
        ):
            return False
        last["before_apply"] = self.draft.text
        self.draft.text = str(last["suggested"])
        self.draft.revision += 1
        last["after_revision"] = self.draft.revision
        self._save_draft()
        return True

    def reject_suggestion(self) -> None:
        last = self._last_suggestion or {}
        if self.draft.authority == "phone":
            if (last.get("draft_id"), last.get("epoch"), last.get("revision"), last.get("primary_applied")) == (
                    self.draft.draft_id, self.draft.epoch, self.draft.revision, self.review_text()):
                self.update_pc_text(str(last["before_apply"]))
            self._last_suggestion = None
            return
        if (
            last.get("before_apply") is not None
            and last.get("draft_id") == self.draft.draft_id
            and last.get("epoch") == self.draft.epoch
            and last.get("after_revision") == self.draft.revision
            and self.draft.text == last.get("suggested")
        ):
            self.draft.text = str(last["before_apply"])
            self.draft.revision += 1
            self._save_draft()
        self._last_suggestion = None

    def input_check_identity(self):
        with self._state_lock:
            return (self.draft.draft_id, self.draft.epoch, self.draft.revision, self.review_text())

    def input_check_tick(self):
        identity = self.input_check_identity()
        self.input_check.observe(identity, identity[-1])
        return self.input_check.view(identity)

    def toggle_input_note(self):
        self.input_check.toggle_note(self.input_check_identity())

    def copy_text(self, text: str | None = None) -> str:
        text = self.review_text() if text is None else text or ""
        if text == self.review_text():
            text = self.input_check.decorate(self.input_check_identity(), text)
        self._copied_text = text
        try:
            self._set_text(text)
        except Exception:
            _log("[v3.copy] 只复制失败，稿未清")
            self._notify_ui("delivery_failed", error_code="COPY_FAILED")
        return text

    def _preserve_current_draft(self) -> None:
        if not (self.draft.text or self.draft.assets):
            return
        from doubao_typeless.storage.draft_snapshot import save_recovery
        save_recovery(self.data_dir, self.draft)

    def start_new_draft(self) -> None:
        if self.draft.authority == "phone":
            self._offer_phone_restore({"text":"", "assets":[]})
            return
        self._preserve_current_draft()
        self.draft.text = ""
        self.draft.assets = []
        self.draft.revision += 1
        self.draft.epoch = str(uuid.uuid4())
        self._save_draft()
        self.hud.hide()
        self._notify_ui("new_draft")

    def _offer_phone_restore(self, bundle: dict) -> str:
        loop = getattr(self, "_loop", None)
        sessions = [s for s in self.auth.sessions.values() if s.device_id == self.draft.editor_device_id]
        if loop is None or not loop.is_running() or not sessions:
            self._notify_ui("delivery_failed", error_code="PHONE_OFFLINE")
            return "phone_offline"
        from doubao_typeless.core.bundle import source_assets
        event = {"type":"draft.restore_proposal", "text":bundle.get("source_text", bundle.get("text", "")),
                 "assets":source_assets(bundle.get("assets") or [])}
        for session in sessions:
            asyncio.run_coroutine_threadsafe(self.bridge.send_to_session(session.session_id,event),loop)
        self._notify_ui("restore_on_phone")
        return "sent_to_phone"

    def restore_history(self, bundle: dict, *, replace: bool = False) -> str:
        if self.draft.authority == "phone":
            # 历史仍能复制/重投。恢复成手机新稿必须由手机保全当前稿并确认。
            return self._offer_phone_restore(bundle)
        if (self.draft.text or self.draft.assets) and not replace:
            return "ask"
        self._preserve_current_draft()
        copied = self.history.copy_to_new_draft(bundle)
        self.draft.text = copied.get("text") or ""
        self.draft.assets = list(copied.get("assets") or [])
        self.draft.revision += 1
        self.draft.epoch = str(uuid.uuid4())
        self._save_draft()
        self._notify_ui("activity")
        return "restored"

    def insert_last(self, user_mode: str | None = None) -> dict | None:
        from doubao_typeless.ui.recovery import plan_retry

        bundle = self.bridge.last_bundle
        if bundle is None:
            return None
        intent = self._session_intent("recall_retry")
        if self._last_attempt is not None and self._last_attempt.bundle_id == bundle.get("bundle_id"):
            images_total = len(bundle.get("assets") or [])
            images_obs = sum(1 for s in self._last_attempt.steps if s.kind == "image" and s.state == "observed")
            text_sent = any(s.kind == "text" for s in self._last_attempt.steps)
            try:
                current_fp = tuple(self._read_focus())
            except Exception:
                current_fp = ("", "", 0)
            same = bool(self._last_target_fp and same_target(current_fp, self._last_target_fp))
            plan = plan_retry(
                previous_result=self._last_attempt.result,
                same_target=same,
                images_observed=images_obs,
                images_total=images_total,
                text_sent=text_sent,
                user_mode=user_mode,
            )
            if plan["mode"] == "ask":
                self._recovery_needed = True
                self.hud.show_receiving("上次结果未知，请选择恢复方式", len(bundle.get("assets") or []))
                _log("[v3.recovery] ask; 不自动重放、不Ctrl+A")
                self._notify_ui("recovery_ask")
                return None
            if plan["mode"] == "cancel":
                return None
            if plan["mode"] in {"text_only", "remaining_verified", "full"}:
                intent["recovery_mode"] = plan["mode"]
                if plan["mode"] == "remaining_verified":
                    intent["skip_asset_ids"] = [
                        s.asset_id
                        for s in self._last_attempt.steps
                        if s.kind == "image" and s.state == "observed" and s.asset_id
                    ]
        elif user_mode is None:
            self._recovery_needed = True
            self._notify_ui("recovery_ask")
            return None
        else:
            intent["recovery_mode"] = user_mode
        self._recovery_needed = False
        return self.deliver_and_finish(intent, bundle)

    def can_confirm_image(self) -> bool:
        attempt = self._last_attempt
        bundle = self.bridge.last_bundle
        return bool(attempt and bundle and attempt.bundle_id == bundle.get("bundle_id")
                    and attempt.steps and attempt.steps[-1].kind == "image"
                    and attempt.steps[-1].state == "unknown" and not attempt.error_code)

    def confirm_recovery(self, mode: str) -> dict | None:
        if mode != "confirm_continue":
            return self.insert_last(user_mode=mode)
        if not self.can_confirm_image():
            return {"result": "NO_STEPS", "error_code": "NO_IMAGE_TO_CONFIRM"}
        self._restore_external_target()
        focus = self._read_focus()
        if not same_target(focus, self._last_target_fp):
            self._notify_ui("delivery_failed", error_code="TARGET_CHANGED")
            return {"result": "NO_STEPS", "error_code": "TARGET_CHANGED"}
        bundle = copy.deepcopy(self.bridge.last_bundle)
        verified = []
        for step in self._last_attempt.steps:
            if step.kind == "image" and step.state in {"observed", "unknown"}:
                verified.append({"asset_id": step.asset_id, "state": "observed",
                                 "evidence": step.evidence if step.state == "observed" else "user_confirmed"})
        intent = self._session_intent("recall_retry")
        intent.update(recovery_mode="remaining_verified", expected_focus=tuple(focus),
                      verified_steps=verified, skip_asset_ids=[v["asset_id"] for v in verified])
        return self.deliver_and_finish(intent, bundle)

    def recall_last(self) -> None:
        from doubao_typeless.ui.recovery import plan_retry

        last = self.history.last_bundle()
        if last is None:
            return
        kept_text = self.draft.text
        kept_assets = [a.get("asset_id") for a in self.draft.assets]
        plan = plan_retry(
            previous_result=(self._last_attempt.result if self._last_attempt else "UNKNOWN"),
            same_target=False,
            images_observed=0,
            images_total=len(last.get("assets") or []),
            text_sent=False,
        )
        _log(
            f"[v3.recall] mode={plan['mode']} auto_replay={plan['auto_replay']} "
            f"ctrl_a_delete={plan['ctrl_a_delete']} current_kept={self.draft.text == kept_text}"
        )
        self.hud.show_receiving(last.get("text") or "上次图文", len(last.get("assets") or []))
        self.bridge.last_bundle = last
        if self.draft.text != kept_text or [a.get("asset_id") for a in self.draft.assets] != kept_assets:
            raise RuntimeError("recall must not swallow current draft")
        self._save_draft()
        previous = self._last_attempt.result if self._last_attempt else ""
        if previous in {"NO_STEPS", ""}:
            return self.insert_last(user_mode="full")
        if previous in {"UNKNOWN", "PARTIAL"} or plan["mode"] == "ask":
            self._recovery_needed = True
            self._notify_ui("recovery_ask")
            return

    def capture_region(self) -> None:
        from doubao_typeless.ui.region import select_region

        self.hud.hide()
        box = select_region()
        if box is None:
            _log("[v3.capture] region cancelled; no new image")
            return
        x, y, w, h = box
        granted = [item for item in self.auth.sessions.values() if item.allow_capture]
        if not granted:
            _log("[v3.capture] region needs capture grant")
            return
        session = granted[-1]
        meta = self._on_capture(f"region:{x},{y},{w},{h}", str(uuid.uuid4()), session)
        if isinstance(meta, dict):
            meta["status"] = "editing"
            meta["role"] = "source"
            loop = getattr(self, "_loop", None)
            if loop is not None:
                asyncio.run_coroutine_threadsafe(self.bridge.send_to_session(session.session_id,
                    {"type": "capture.result", "asset": meta}), loop)
        _log(f"[v3.capture] region {meta.get('width')}x{meta.get('height')}")

    def _acquire_instance_lock(self) -> None:
        from doubao_typeless.runtime_lock import InstanceLock

        lock = getattr(self, "_lock", None)
        if lock is not None and getattr(lock, "owned", False):
            return
        self._lock = InstanceLock(self.data_dir / "instance.lock")
        if not self._lock.acquire():
            _log("[v3] 另一个预览实例已在运行，不强杀、不抢锁")
            raise RuntimeError("instance lock held")

    async def start(self) -> None:
        self._acquire_instance_lock()
        await self.bridge.start()
        self.port = self.bridge.port
        code = self.auth.new_pairing_challenge()
        url = f"http://{lan_ip()}:{self.port}/"
        pair_note = self.data_dir / "pair.txt"
        pair_note.write_text(f"{url}\n{code}\n", encoding="utf-8")
        _log(f"[v3] 手机打开 {url}")
        _log("[v3] 配对信息已在连接窗口就绪（2分钟内）")
        _log(f"[v3] 数据目录 {self.data_dir}")
        from doubao_typeless.platform.windows.hotkeys import probe_hotkey_conflicts

        probe = probe_hotkey_conflicts()
        _log(f"[v3] 热键探测 {probe}；冲突时改键，语法合法不等于注册成功")
        _log("[v3] 空闲无浮窗；Alt+I 插入并复制，Alt+Shift+I 召回；不发送 Enter")

    def start_background(self, *, start_hud: bool = False):
        if start_hud:
            self.hud.start()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        asyncio.run_coroutine_threadsafe(self.start(), loop).result(15)
        self._loop = loop
        return loop

    def apply_hotkeys(
        self,
        insert: str,
        recall: str,
        *,
        expand: str = "<alt>+<shift>+e",
        capture: str = "<alt>+<shift>+s",
    ) -> list[str]:
        self._stop_hotkeys()
        from doubao_typeless.platform.desktop import start_hotkeys

        start = start_hotkeys(
            on_insert=self.request_insert,
            on_recall=self.request_recall,
            on_expand=lambda: self._notify_ui("expand"),
            on_region=lambda: self._notify_ui("capture_region"),
            insert_combo=insert or "<alt>+i",
            recall_combo=recall or "<alt>+<shift>+i",
            expand_combo=expand or "<alt>+<shift>+e",
            capture_combo=capture or "<alt>+<shift>+s",
        )
        self._hotkeys = start
        return list(start.get("failures") or [])

    def _stop_hotkeys(self) -> None:
        hotkeys = getattr(self, "_hotkeys", None) or {}
        for key in ("listener", "release"):
            obj = hotkeys.get(key)
            if obj is None:
                continue
            try:
                obj.stop()
            except Exception:
                pass
        self._hotkeys = None

    async def stop(self) -> bool:
        """停止下一步发键，等当前操作收尾；超时不强杀、不提前关闭数据库。"""
        self._stopping = True
        self._stop_hotkeys()
        _log('[v3.shutdown] stage=queue_begin')
        stopped = await asyncio.to_thread(self._commands.close, 5.0)
        if not stopped:
            _log("[v3] 当前目标仍未返回，未强制结束；可稍后再退出")
            return False
        _log('[v3.shutdown] stage=queue_done')
        self.input_check.close()
        _log('[v3.shutdown] stage=bridge_begin')
        await self.bridge.stop()
        _log('[v3.shutdown] stage=bridge_done')
        from doubao_typeless.platform.windows.automation_host import close_host
        await asyncio.to_thread(close_host)
        _log('[v3.shutdown] stage=helper_done')
        try:
            self.db.conn.close()
        except Exception:
            pass
        lock = getattr(self, "_lock", None)
        if lock:
            lock.release()
        _log('[v3.shutdown] stage=complete')
        return True


def main() -> None:
    from doubao_typeless.ui.desktop import run_desktop

    raise SystemExit(run_desktop())


if __name__ == "__main__":
    main()
