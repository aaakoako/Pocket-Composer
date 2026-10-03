"""Chunked asset upload. Files go to temp then atomic replace; DB only after complete."""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import stat
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from PIL import Image

from doubao_typeless.storage.asset_store import JPEG_MAGIC, MAX_BYTES, PNG_MAGIC, AssetStore
from doubao_typeless.storage.db import V3DB

CHUNK = 512 * 1024
# 未完成上传在磁盘上的保留边界：重启后最多续用这么多份、这么久，其余残留分块删除。
UPLOAD_TTL_S = 24 * 3600
MAX_RECOVERED_UPLOADS = 16
MANIFEST = "manifest.json"
MAX_PIXELS = 48_000_000
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
UPLOAD_DIR_ID = re.compile(r"^[0-9a-f]{32}$")  # UploadService.init 使用 uuid4().hex
UPLOAD_FILE = re.compile(r"^(?:\d+\.(?:part|tmp)|manifest\.json|manifest\.writing)$")


_RESOLVED_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_RESOLVED_CACHE_LIMIT = 64
_RESOLVED_LOCK = threading.Lock()


def _file_identity(store: AssetStore, asset_id: str) -> tuple | None:
    """素材文件按 UUID 原子写入后不再改写；路径+大小+修改时间不变即同一份已校验的像素。"""
    path_of = getattr(store, "_path", None)
    if path_of is None:
        return None
    try:
        path = path_of(asset_id, ".bin")
        stat = path.stat()
        meta_path = path_of(asset_id, ".meta.json")
        meta_stat = meta_path.stat() if meta_path.is_file() else None
    except (FileNotFoundError, OSError):
        return None
    return (str(path), stat.st_size, stat.st_mtime_ns,
            meta_stat.st_mtime_ns if meta_stat else None)


def _resolve_one(store: AssetStore, asset_id: str) -> dict:
    try:
        payload = store.get(asset_id)
    except FileNotFoundError as exc:
        raise ValueError("unknown asset_ref") from exc
    image = Image.open(io.BytesIO(payload))
    image.load()
    stored = store.meta(asset_id) if hasattr(store, "meta") else {}
    role = str(stored.get("role") or "photo")
    return {
        "asset_id": asset_id,
        "render_revision": 1,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mime": "image/png" if payload.startswith(PNG_MAGIC) else "image/jpeg",
        "bytes": len(payload),
        "width": image.width,
        "height": image.height,
        "role": role,
    }


def resolve_asset_refs(store: AssetStore, refs: object) -> list[dict]:
    """Map client asset_refs to completed store objects. Never trust client assets.

    手机每说一句都会带着同一批图片引用同步；同一文件只完整解码校验一次，
    之后按文件身份复用结果，文件被替换或删除时重新校验/报错。
    """
    if not isinstance(refs, list):
        raise ValueError("invalid asset_refs")
    ids: list[str] = []
    for ref in refs:
        if not isinstance(ref, str) or not SAFE_ID.match(ref):
            raise ValueError("invalid asset_ref")
        ids.append(ref)
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate asset id")
    resolved: list[dict] = []
    for asset_id in ids:
        identity = _file_identity(store, asset_id)
        cached = None
        if identity is not None:
            with _RESOLVED_LOCK:
                cached = _RESOLVED_CACHE.get(identity)
                if cached is not None:
                    _RESOLVED_CACHE.move_to_end(identity)
        if cached is None:
            cached = _resolve_one(store, asset_id)
            if identity is not None:
                with _RESOLVED_LOCK:
                    _RESOLVED_CACHE[identity] = cached
                    while len(_RESOLVED_CACHE) > _RESOLVED_CACHE_LIMIT:
                        _RESOLVED_CACHE.popitem(last=False)
        resolved.append(dict(cached))
    return resolved


class UploadService:
    def __init__(self, store: AssetStore, db: V3DB, *, chunk_size: int = CHUNK):
        self.store = store
        self.db = db
        self.chunk_size = chunk_size
        self._uploads: dict[str, dict] = {}
        self._recover()

    def _uploads_root(self) -> Path:
        return self.store.root / "uploads"

    @staticmethod
    def _is_link(path: Path) -> bool:
        """符号链接或 Windows 目录联接/挂载点：不跟随、不删除目标。"""
        try:
            info = path.lstat()
        except OSError:
            return True
        if stat.S_ISLNK(info.st_mode):
            return True
        if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
        return bool(getattr(path, "is_junction", lambda: False)())

    @classmethod
    def _owned_upload_dir(cls, path: Path) -> bool:
        """只认本服务创建的上传目录：uuid hex 名、真实目录、里面只有本服务写的文件。

        其他名字的目录、链接/联接、含子目录或陌生文件的目录都不是我们的残留，原样保留。
        无清单的旧版本残留（同样只含 N.part / N.tmp）也符合这个形状。"""
        if not UPLOAD_DIR_ID.match(path.name) or cls._is_link(path) or not path.is_dir():
            return False
        try:
            for child in path.iterdir():
                if cls._is_link(child) or not child.is_file() or not UPLOAD_FILE.match(child.name):
                    return False
        except OSError:
            return False
        return True

    @classmethod
    def _remove_dir(cls, path: Path) -> None:
        """删除本服务的上传目录；调用方已确认归属。仍逐项复核，不跟随链接、不递归。"""
        try:
            if not cls._owned_upload_dir(path):
                return
            for child in path.iterdir():
                if not cls._is_link(child) and child.is_file() and UPLOAD_FILE.match(child.name):
                    child.unlink(missing_ok=True)
            path.rmdir()
        except OSError:
            pass

    def _manifest(self, session: dict, created: float) -> dict:
        return {"mime": session["mime"], "total_bytes": session["total_bytes"], "sha256": session["sha256"],
                "width": session["width"], "height": session["height"], "role": session["role"],
                "chunk_size": session["chunk_size"], "expected": session["expected"],
                "owner": session["owner"], "created": created}

    def _recover(self) -> None:
        """重启后：近期且清单有效的未完成上传按原 ID 续传；过期、清单无效或超额的本服务残留删除。

        只处理本服务创建的 uuid 目录（见 _owned_upload_dir）；uploads 下用户或其他程序的目录、
        链接及其目标一律不碰。旧版本没有清单的分块目录无法续传，按目录时间过期后删除。"""
        root = self._uploads_root()
        if self._is_link(root) or not root.is_dir():
            return
        now = time.time()
        candidates: list[tuple[float, str, dict]] = []
        for entry in root.iterdir():
            name = entry.name
            if not self._owned_upload_dir(entry):
                continue
            try:
                raw = json.loads((entry / MANIFEST).read_text(encoding="utf-8"))
                session = self._validated_manifest(raw)
                created = float(raw["created"])
            except FileNotFoundError:
                # 旧版本（无清单）或建清单前中断：不能续传；目录本身过期后再删，避免误伤正在初始化的上传。
                try:
                    stale = now - entry.stat().st_mtime > UPLOAD_TTL_S
                except OSError:
                    stale = False
                if stale:
                    self._remove_dir(entry)
                continue
            except (OSError, ValueError, KeyError, TypeError):
                self._remove_dir(entry)
                continue
            if not (now - UPLOAD_TTL_S <= created <= now + 300):
                self._remove_dir(entry)
                continue
            candidates.append((created, name, session))
        candidates.sort(reverse=True)
        for created, name, session in candidates[MAX_RECOVERED_UPLOADS:]:
            self._remove_dir(root / name)
        for created, name, session in candidates[:MAX_RECOVERED_UPLOADS]:
            tmp_dir = root / name
            chunks: dict[int, int] = {}
            for child in tmp_dir.iterdir():
                if child.name == MANIFEST:
                    continue
                stem, dot, suffix = child.name.partition(".")
                if (suffix == "part" and stem.isdigit() and int(stem) < session["expected"]
                        and child.is_file() and not child.is_symlink()):
                    size = child.stat().st_size
                    if 0 < size <= session["chunk_size"]:
                        chunks[int(stem)] = size
                        continue
                # 写到一半的 .tmp、越界或异常文件不能被当成已收到的分块。
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
            session.update(chunks=chunks, tmp_dir=tmp_dir, completed=None)
            self._uploads[name] = session

    @staticmethod
    def _validated_manifest(raw: object) -> dict:
        if not isinstance(raw, dict):
            raise ValueError("manifest")
        ints = {}
        for key in ("total_bytes", "width", "height", "chunk_size", "expected"):
            value = raw.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError("manifest")
            ints[key] = value
        if ints["total_bytes"] > MAX_BYTES or ints["width"] * ints["height"] > MAX_PIXELS:
            raise ValueError("manifest")
        if not 1024 <= ints["chunk_size"] <= 2 * 1024 * 1024 or ints["expected"] != max(1, math.ceil(ints["total_bytes"] / ints["chunk_size"])):
            raise ValueError("manifest")
        mime, sha, role, owner = raw.get("mime"), raw.get("sha256"), raw.get("role"), raw.get("owner")
        if mime not in {"image/png", "image/jpeg"} or not isinstance(sha, str) or len(sha) > 128:
            raise ValueError("manifest")
        if not isinstance(role, str) or len(role) > 64 or not isinstance(owner, str) or len(owner) > 256:
            raise ValueError("manifest")
        return {"mime": mime, "sha256": sha, "role": role or "photo", "owner": owner, **ints}

    def init(
        self,
        *,
        mime: str,
        total_bytes: int,
        sha256: str,
        width: int,
        height: int,
        chunk_size: int | None = None,
        owner_session_id: str = "",
        role: str = "photo",
    ) -> dict:
        if total_bytes <= 0 or total_bytes > MAX_BYTES:
            raise ValueError("asset too large")
        if width * height > MAX_PIXELS:
            raise ValueError("too many pixels")
        if mime not in {"image/png", "image/jpeg"}:
            raise ValueError("bad mime")
        upload_id = uuid.uuid4().hex
        size = int(chunk_size or self.chunk_size)
        expected = max(1, math.ceil(total_bytes / size))
        tmp_dir = self._uploads_root() / upload_id
        tmp_dir.mkdir(parents=True, exist_ok=True)
        session = self._uploads[upload_id] = {
            "mime": mime,
            "total_bytes": total_bytes,
            "sha256": sha256,
            "width": width,
            "height": height,
            "role": role or "photo",
            "chunk_size": size,
            "expected": expected,
            "chunks": {},
            "tmp_dir": tmp_dir,
            "completed": None,
            "owner": owner_session_id,
        }
        # 清单先于任何分块落盘：电脑重启后同一 ID 仍能续传，过期后可安全清理。
        manifest = tmp_dir / MANIFEST
        temp = manifest.with_suffix(".writing")
        temp.write_text(json.dumps(self._manifest(session, time.time())), encoding="utf-8")
        temp.replace(manifest)
        return {"upload_id": upload_id, "chunk_size": size, "expected_chunks": expected}

    def _session(self, upload_id: str, owner_session_id: str = "") -> dict:
        if not SAFE_ID.match(upload_id or "") or upload_id not in self._uploads:
            raise ValueError("upload id invalid")
        session = self._uploads[upload_id]
        owner = session.get("owner") or ""
        if owner and owner_session_id and owner != owner_session_id:
            raise ValueError("upload owner")
        return session

    def put_chunk(self, upload_id: str, index: int, data: bytes, owner_session_id: str = "") -> None:
        session = self._session(upload_id, owner_session_id)
        if index < 0 or index >= session["expected"]:
            raise ValueError("chunk index")
        if len(session["chunks"]) >= 2 and index not in session["chunks"]:
            # 最多并发2个未完成块：已有未写完的超过2则拒绝新的。已落地块不计入。
            inflight = [i for i in session["chunks"] if not (session["tmp_dir"] / f"{i}.part").is_file()]
            if len(inflight) >= 2:
                raise ValueError("too many concurrent chunks")
        part = session["tmp_dir"] / f"{index}.part"
        tmp = part.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(part)
        session["chunks"][index] = len(data)

    def missing_chunks(self, upload_id: str, owner_session_id: str = "") -> list[int]:
        session = self._session(upload_id, owner_session_id)
        return [i for i in range(session["expected"]) if i not in session["chunks"]]

    def complete(self, upload_id: str, owner_session_id: str = "") -> dict:
        session = self._session(upload_id, owner_session_id)
        if session["completed"]:
            return session["completed"]
        missing = self.missing_chunks(upload_id)
        if missing:
            raise ValueError(f"missing chunks {missing}")
        parts = [ (session["tmp_dir"] / f"{i}.part").read_bytes() for i in range(session["expected"]) ]
        payload = b"".join(parts)
        if len(payload) != session["total_bytes"]:
            self._purge(session)
            raise ValueError("size mismatch")
        digest = hashlib.sha256(payload).hexdigest()
        expected = str(session.get("sha256") or "")
        if expected not in {"", "pending"} and digest != expected:
            self._purge(session)
            raise ValueError("hash mismatch")
        if not (payload.startswith(PNG_MAGIC) or payload.startswith(JPEG_MAGIC)):
            self._purge(session)
            raise ValueError("bad magic")
        image = Image.open(io.BytesIO(payload))
        image.load()
        if image.width * image.height > MAX_PIXELS:
            self._purge(session)
            raise ValueError("too many pixels")
        existing = self.db.asset(digest)
        if existing and (
            str(existing.get("owner_session_id") or "") == str(session.get("owner") or owner_session_id or "")
            and self.store.meta(existing["asset_id"]).get("role") == str(session.get("role") or "photo")
            and (self.store.root / (existing["asset_id"] + ".bin")).is_file()
        ):
            session["completed"] = {
                "asset_id": existing["asset_id"],
                "render_revision": 1,
                "sha256": digest,
                "mime": session["mime"],
                "bytes": len(payload),
                "width": image.width,
                "height": image.height,
                "role": str(session.get("role") or "photo"),
            }
            self._purge(session)
            return session["completed"]
        meta = self.store.put_png(
            payload, width=image.width, height=image.height, role=str(session.get("role") or "photo")
        )
        self.db.upsert_asset(
            meta["asset_id"],
            meta["sha256"],
            meta["bytes"],
            referenced=False,
            owner_session_id=str(session.get("owner") or owner_session_id or ""),
        )
        session["completed"] = meta
        self._purge(session)
        return meta

    def abort(self, upload_id: str) -> None:
        if upload_id in self._uploads:
            self._purge(self._uploads[upload_id])
            del self._uploads[upload_id]

    def _purge(self, session: dict) -> None:
        tmp_dir: Path = session["tmp_dir"]
        if tmp_dir.is_dir():
            for path in tmp_dir.glob("*"):
                path.unlink(missing_ok=True)
            tmp_dir.rmdir()
