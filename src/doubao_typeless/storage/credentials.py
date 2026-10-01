"""Pairing and action grants. Network messages never carry key scripts."""
from __future__ import annotations

import functools
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

REMEMBER_TTL_S = 30 * 24 * 3600
# 四位备用短码只有一万种组合：失败按来源锁定，并对所有挑战累计预算。
# 二维码长码不因别人猜错而作废；只有用户点「重新配对」才恢复短码预算。
PAIR_FAILURE_WINDOW_S = 600.0
PAIR_SOURCE_LIMIT = 5
PAIR_LOCK_S = 30.0
PAIR_LOCK_MAX_S = 900.0
PAIR_SOURCE_TRACKED = 256
SHORT_CODE_CHALLENGE_LIMIT = 5
SHORT_CODE_GLOBAL_LIMIT = 20


class PairingError(ValueError):
    def __init__(self, message: str, *, code: str, retry_after: float = 0.0):
        super().__init__(message)
        self.code = code
        self.retry_after = max(0.0, float(retry_after))


class TrustStoreError(RuntimeError):
    """电脑未能持久保存设备信任；内存状态已回滚，可稍后重试。"""


@dataclass
class Session:
    device_id: str
    session_id: str
    token_hash: str
    expires_at: float
    allow_sync: bool = True
    allow_capture: bool = False
    allow_insert: bool = False
    used_nonces: dict[str, float] = field(default_factory=dict)
    token: str = ""
    remembered: bool = False
    device_secret_once: str = ""


@dataclass
class TrustedDevice:
    device_id: str
    secret_hash: str
    expires_at: float
    allow_insert: bool = False
    allow_capture: bool = False


@dataclass
class PairingChallenge:
    long_code: str
    short_code: str
    expires_at: float
    short_failures: int = 0
    short_disabled: bool = False


@dataclass
class _SourceFailures:
    times: deque = field(default_factory=deque)
    locked_until: float = 0.0
    lockouts: int = 0


def device_label(device_id: str, nicknames: dict | None, index: int) -> str:
    nick = str((nicknames or {}).get(device_id) or "").strip()
    return nick or f"手机 {index}"


def _locked(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def pairing_page_url(base_url: str, code: str) -> str:
    base = (base_url or "").rstrip("/") + "/"
    return f"{base}?pair={quote(code or '', safe='')}"


class AuthService:
    def __init__(
        self,
        *,
        pairing_ttl_s: float = 120,
        session_ttl_s: float = 8 * 3600,
        store_path: Path | None = None,
    ):
        self.pairing_ttl_s = pairing_ttl_s
        self.session_ttl_s = session_ttl_s
        self.store_path = Path(store_path) if store_path else None
        self._challenge: PairingChallenge | None = None
        self.sessions: dict[str, Session] = {}
        self.trusted: dict[str, TrustedDevice] = {}
        self._sources: dict[str, _SourceFailures] = {}
        self._short_failures_total = 0
        self._short_locked = False
        # Qt 界面线程与 asyncio/HTTP 线程共用本服务。每个变更从读出当前信任表、写盘到改内存和会话
        # 都在这把锁内完成，否则并发的两次变更各自基于旧表写盘，后写者会抹掉前者已成功的结果。
        # 锁内只有同步操作，调用方不得在持锁时等待事件循环。
        self._lock = threading.RLock()
        self._load_trusted()

    def _load_trusted(self) -> None:
        if self.store_path is None or not self.store_path.is_file():
            return
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = time.time()
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            expires = float(item.get("expires_at") or 0)
            device_id = str(item.get("device_id") or "")
            secret_hash = str(item.get("secret_hash") or "")
            if not device_id or not secret_hash or expires <= now:
                continue
            self.trusted[device_id] = TrustedDevice(
                device_id=device_id,
                secret_hash=secret_hash,
                expires_at=expires,
                allow_insert=bool(item.get("allow_insert")),
                allow_capture=bool(item.get("allow_capture")),
            )

    def _save_trusted(self, trusted: dict[str, TrustedDevice] | None = None) -> None:
        if self.store_path is None:
            return
        items = self.trusted if trusted is None else trusted
        payload = [
            {
                "device_id": item.device_id,
                "secret_hash": item.secret_hash,
                "expires_at": item.expires_at,
                "allow_insert": item.allow_insert,
                "allow_capture": item.allow_capture,
            }
            for item in items.values()
            if time.time() <= item.expires_at
        ]
        from doubao_typeless.storage.draft_snapshot import write_json_atomic

        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.store_path, payload)
        except OSError as exc:
            raise TrustStoreError("TRUST_STORE_WRITE_FAILED") from exc

    def _commit_trusted(self, candidate: dict[str, TrustedDevice]) -> None:
        """先写盘再替换内存；写盘失败时调用方负责恢复会话字段。调用方须持有 _lock 并基于锁内读出的表构造候选。"""
        with self._lock:
            self._save_trusted(candidate)
            self.trusted = candidate

    def _publish_session(self, session: Session) -> None:
        # 会话表按写时复制发布：其它线程不加锁遍历 sessions 时不会遇到“字典大小改变”。
        sessions = dict(self.sessions)
        sessions[session.session_id] = session
        self.sessions = sessions

    def current_pairing_challenge(self) -> str | None:
        challenge = self._live_challenge()
        return None if challenge is None else challenge.long_code

    def current_short_code(self) -> str | None:
        challenge = self._live_challenge()
        if challenge is None or not self.short_code_available():
            return None
        return challenge.short_code

    def short_code_available(self) -> bool:
        challenge = self._live_challenge()
        return bool(challenge and not challenge.short_disabled and not self._short_locked)

    def _live_challenge(self) -> PairingChallenge | None:
        if not self._challenge:
            return None
        if time.monotonic() > self._challenge.expires_at:
            self._challenge = None
            return None
        return self._challenge

    def pairing_remaining_s(self) -> float:
        challenge = self._live_challenge()
        if challenge is None:
            return 0.0
        return max(0.0, challenge.expires_at - time.monotonic())

    @_locked
    def new_pairing_challenge(self) -> str:
        existing = self.current_pairing_challenge()
        if existing:
            return existing
        self._challenge = PairingChallenge(
            long_code=secrets.token_urlsafe(8),
            short_code=f"{secrets.randbelow(10000):04d}",
            expires_at=time.monotonic() + self.pairing_ttl_s,
        )
        return self._challenge.long_code

    @_locked
    def rotate_pairing_challenge(self) -> str:
        # 用户在电脑上主动点「重新配对」：这是唯一恢复短码预算的入口。
        self._challenge = None
        self._short_failures_total = 0
        self._short_locked = False
        return self.new_pairing_challenge()

    def _source_state(self, source: str, now: float) -> _SourceFailures:
        state = self._sources.get(source)
        if state is None:
            if len(self._sources) >= PAIR_SOURCE_TRACKED:
                victim = next((k for k, v in self._sources.items() if v.locked_until <= now), next(iter(self._sources)))
                self._sources.pop(victim, None)
            state = self._sources[source] = _SourceFailures()
        while state.times and now - state.times[0] > PAIR_FAILURE_WINDOW_S:
            state.times.popleft()
        return state

    def pairing_retry_after(self, source: str = "") -> float:
        now = time.monotonic()
        state = self._sources.get(source)
        return 0.0 if state is None else max(0.0, state.locked_until - now)

    def _record_pair_failure(self, source: str, now: float) -> None:
        state = self._source_state(source, now)
        state.times.append(now)
        if len(state.times) >= PAIR_SOURCE_LIMIT:
            state.lockouts += 1
            state.locked_until = now + min(PAIR_LOCK_MAX_S, PAIR_LOCK_S * (2 ** (state.lockouts - 1)))
            state.times.clear()

    def _prior_session(self, previous: dict | None) -> Session | None:
        """仍有效的旧会话令牌才算同一设备的证明；设备号或名字本身不算。"""
        if not isinstance(previous, dict):
            return None
        sid = str(previous.get("session_id") or "")
        token = str(previous.get("token") or "")
        prior = self.sessions.get(sid)
        if prior is None or not token or time.time() > prior.expires_at:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return prior if hmac.compare_digest(prior.token_hash, digest) else None

    @_locked
    def complete_pairing(self, code: str, *, allow_insert: bool = False, allow_capture: bool = False,
                         source: str = "", previous: dict | None = None) -> Session:
        now = time.monotonic()
        state = self._source_state(source, now)
        if state.locked_until > now:
            raise PairingError("pairing locked", code="PAIRING_LOCKED", retry_after=state.locked_until - now)
        if not self._challenge:
            raise PairingError("no pairing challenge", code="PAIRING_EXPIRED")
        expected = self._challenge
        if now > expected.expires_at:
            self._challenge = None
            raise PairingError("pairing expired", code="PAIRING_EXPIRED")
        offered = (code or "").strip()
        long_ok = len(offered) == len(expected.long_code) and hmac.compare_digest(expected.long_code, offered)
        short_attempt = len(offered) == 4 and offered.isdigit()
        short_open = not expected.short_disabled and not self._short_locked
        short_ok = short_attempt and short_open and hmac.compare_digest(expected.short_code, offered)
        if not (long_ok or short_ok):
            self._record_pair_failure(source, now)
            if short_attempt and short_open:
                expected.short_failures += 1
                self._short_failures_total += 1
                if expected.short_failures >= SHORT_CODE_CHALLENGE_LIMIT:
                    expected.short_disabled = True
                if self._short_failures_total >= SHORT_CODE_GLOBAL_LIMIT:
                    self._short_locked = True
            if short_attempt and (expected.short_disabled or self._short_locked):
                raise PairingError("pairing mismatch; short code disabled", code="SHORT_CODE_DISABLED")
            raise PairingError("pairing mismatch", code="PAIRING_MISMATCH")
        self._challenge = None
        self._sources.pop(source, None)
        prior = self._prior_session(previous)
        token = secrets.token_urlsafe(24)
        if prior is not None:
            # 同一浏览器的新标签页再次扫码：沿用设备身份与该设备已有的授权。
            trusted = self.trusted.get(prior.device_id)
            remembered = bool(trusted and time.time() <= trusted.expires_at)
            session = Session(
                device_id=prior.device_id,
                session_id=secrets.token_hex(8),
                token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                expires_at=trusted.expires_at if remembered else time.time() + self.session_ttl_s,
                allow_insert=prior.allow_insert,
                allow_capture=prior.allow_capture,
                token=token,
                remembered=remembered,
            )
        else:
            session = Session(
                device_id=secrets.token_hex(8),
                session_id=secrets.token_hex(8),
                token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
                expires_at=time.time() + self.session_ttl_s,
                allow_insert=allow_insert,
                allow_capture=allow_capture,
                token=token,
            )
        self._publish_session(session)
        return session

    def _device_sessions(self, device_id: str) -> list[Session]:
        return [s for s in self.sessions.values() if s.device_id == device_id]

    @_locked
    def remember_device(self, session: Session) -> str:
        existing = self.trusted.get(session.device_id)
        if existing is not None and time.time() <= existing.expires_at:
            session.remembered = True
            session.expires_at = existing.expires_at
            session.device_secret_once = next((s.device_secret_once for s in self._device_sessions(session.device_id)
                                               if s.device_secret_once), "")
            return session.device_secret_once
        secret = secrets.token_urlsafe(24)
        expires = time.time() + REMEMBER_TTL_S
        candidate = dict(self.trusted)
        candidate[session.device_id] = TrustedDevice(
            device_id=session.device_id,
            secret_hash=hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            expires_at=expires,
            allow_insert=session.allow_insert,
            allow_capture=session.allow_capture,
        )
        # 写盘成功前不暴露凭据，也不把会话标成已记住；失败可再次点击重试。
        self._commit_trusted(candidate)
        session.remembered = True
        session.expires_at = expires
        session.device_secret_once = secret
        return secret

    @_locked
    def unremember_device(self, device_id: str) -> bool:
        """只取消这台设备的自动续接；当前会话继续可用，到普通会话期限为止。"""
        found = device_id in self.trusted
        if found:
            candidate = dict(self.trusted)
            candidate.pop(device_id, None)
            self._commit_trusted(candidate)
        limit = time.time() + self.session_ttl_s
        for session in self._device_sessions(device_id):
            found = found or session.remembered
            session.remembered = False
            session.device_secret_once = ""
            session.expires_at = min(session.expires_at, limit)
        return found

    @_locked
    def take_device_secret(self, session: Session) -> str:
        # HTTP/WS 发送成功不等于手机已保存。允许重取，收到凭据确认才清除。
        return session.device_secret_once

    @_locked
    def acknowledge_device_secret(self, session: Session, secret: str) -> None:
        item = self.trusted.get(session.device_id)
        digest = hashlib.sha256(str(secret or "").encode("utf-8")).hexdigest()
        if item is None or time.time() > item.expires_at or not hmac.compare_digest(item.secret_hash, digest):
            raise ValueError("device mismatch")
        for other in self._device_sessions(session.device_id):
            other.device_secret_once = ""

    @_locked
    def resume_trusted(self, device_id: str, device_secret: str) -> Session:
        item = self.trusted.get(str(device_id or ""))
        if item is None or time.time() > item.expires_at:
            raise ValueError("device expired")
        digest = hashlib.sha256(str(device_secret or "").encode("utf-8")).hexdigest()
        if not hmac.compare_digest(item.secret_hash, digest):
            raise ValueError("device mismatch")
        token = secrets.token_urlsafe(24)
        session = Session(
            device_id=item.device_id,
            session_id=secrets.token_hex(8),
            token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expires_at=item.expires_at,
            allow_insert=item.allow_insert,
            allow_capture=item.allow_capture,
            token=token,
            remembered=True,
        )
        self._publish_session(session)
        self.acknowledge_device_secret(session, device_secret)
        return session

    def authorize(self, session_id: str, token: str, action: str) -> Session:
        session = self.sessions.get(session_id)
        if not session or time.time() > session.expires_at:
            raise ValueError("session expired")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(session.token_hash, digest):
            raise ValueError("bad token")
        allowed = {
            "sync": session.allow_sync,
            "capture": session.allow_capture,
            "insert": session.allow_insert,
        }.get(action, False)
        if not allowed:
            raise ValueError("action not granted")
        return session

    @_locked
    def set_grants(
        self,
        session_id: str,
        *,
        allow_insert: bool | None = None,
        allow_capture: bool | None = None,
    ) -> Session:
        session = self.sessions.get(session_id)
        if session is None or time.time() > session.expires_at:
            raise ValueError("session missing")
        insert = session.allow_insert if allow_insert is None else bool(allow_insert)
        capture = session.allow_capture if allow_capture is None else bool(allow_capture)
        trusted = self.trusted.get(session.device_id)
        if trusted is not None:
            candidate = dict(self.trusted)
            candidate[session.device_id] = replace(trusted, allow_insert=insert, allow_capture=capture)
            # 已记住设备的授权变化先写盘；失败时内存授权保持原值。
            self._commit_trusted(candidate)
        for other in self._device_sessions(session.device_id):
            other.allow_insert = insert
            other.allow_capture = capture
        return session

    @_locked
    def public_sessions(self) -> list[dict]:
        # 一个设备可以有多个标签页/续接会话，授权列表按设备展示。
        devices = {s.device_id: s for s in self.sessions.values() if time.time() <= s.expires_at}
        return [
            {
                "session_id": s.session_id,
                "device_id": s.device_id,
                "allow_insert": s.allow_insert,
                "allow_capture": s.allow_capture,
                "allow_sync": s.allow_sync,
                "remembered": s.remembered,
                "remember_pending": bool(s.device_secret_once),
            }
            for s in devices.values()
        ]

    @_locked
    def revoke(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None:
            return False
        return self.forget_device(session.device_id)

    @_locked
    def forget_device(self, device_id: str) -> bool:
        found = device_id in self.trusted
        if found:
            candidate = dict(self.trusted)
            candidate.pop(device_id, None)
            # 撤销没写进磁盘时不假装成功：重启后仍会续接，所以保持原状并报错。
            self._commit_trusted(candidate)
        remaining = {sid: item for sid, item in self.sessions.items() if item.device_id != device_id}
        if len(remaining) != len(self.sessions):
            self.sessions = remaining
            found = True
        return found

    def issue_nonce(self, session: Session) -> str:
        nonce = secrets.token_urlsafe(24)
        session.used_nonces[nonce] = time.time() + 10
        return nonce

    def consume_nonce(self, session: Session, nonce: str) -> None:
        expiry = session.used_nonces.pop(nonce, None)
        if expiry is None or time.time() > expiry:
            raise ValueError("nonce invalid")


def looks_like_key_script(payload: dict) -> bool:
    """只按协议结构判断。正文、图注和服务器回执里的技术词都是普通内容。"""
    if not isinstance(payload, dict):
        return False
    if any(k in payload for k in ("keys", "vk", "scan_code")):
        return True
    return isinstance(payload.get("x"), (int, float)) and isinstance(payload.get("y"), (int, float))


_DRAFT_FIELDS = frozenset({"text", "revision", "draft_id", "epoch", "asset_refs", "asset_documents",
                           "authority", "generation", "update_id", "takeover", "hash", "assets"})
CLIENT_MESSAGE_FIELDS: dict[str, frozenset] = {
    "ping": frozenset(),
    "session.hello": frozenset({"session_id", "token"}),
    "draft.update": _DRAFT_FIELDS | {"captions", "asset_status"},
    "draft.prepared": _DRAFT_FIELDS | {"request_id", "error"},
    "bundle.commit": _DRAFT_FIELDS | {"captions", "asset_status"},
    "insert.intent": _DRAFT_FIELDS | {"session_id", "token", "nonce", "intent_id", "trigger",
                                      "captions", "asset_status"},
    "editor.activity": frozenset({"kind"}),
    "capture.request": frozenset({"session_id", "token", "scope", "request_id"}),
    "recall.last": frozenset(),
    "byok.request": frozenset({"api_key", "endpoint", "model"}),
    "mirror.request": frozenset({"request_id"}),
    "asset.claim": frozenset({"asset_ids", "request_id"}),
}
_DOCUMENT_FIELDS = frozenset({"id", "asset_id", "status", "render_revision", "caption"})
_TAKEOVER_FIELDS = frozenset({"draft_id", "epoch", "revision", "owner_device_id"})


def validate_client_message(data: object) -> str | None:
    """手机消息按类型限定顶层字段与结构。返回错误码；None 表示结构可接受。"""
    if not isinstance(data, dict):
        return "INVALID_MESSAGE"
    if looks_like_key_script(data):
        return "forbidden payload"
    kind = data.get("type")
    allowed = CLIENT_MESSAGE_FIELDS.get(kind) if isinstance(kind, str) else None
    if allowed is None:
        return "UNSUPPORTED_MESSAGE"
    if set(data) - allowed - {"type", "protocol"}:
        return "UNEXPECTED_FIELD"
    if "text" in data and not isinstance(data["text"], str):
        return "INVALID_MESSAGE"
    for name in ("asset_refs", "captions", "asset_status", "asset_ids"):
        value = data.get(name)
        if value is not None and (not isinstance(value, list) or len(value) > 16
                                  or not all(isinstance(v, str) for v in value)):
            return "INVALID_MESSAGE"
    docs = data.get("asset_documents")
    if docs is not None:
        if not isinstance(docs, list) or len(docs) > 16:
            return "INVALID_MESSAGE"
        for doc in docs:
            if not isinstance(doc, dict) or set(doc) - _DOCUMENT_FIELDS:
                return "INVALID_MESSAGE"
    takeover = data.get("takeover")
    if takeover is not None and (not isinstance(takeover, dict) or set(takeover) - _TAKEOVER_FIELDS):
        return "INVALID_MESSAGE"
    for name in ("update_id", "intent_id", "request_id", "session_id", "token", "nonce", "trigger", "kind", "scope"):
        value = data.get(name)
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            return "INVALID_MESSAGE"
    return None
