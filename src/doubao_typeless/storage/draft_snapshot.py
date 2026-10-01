"""原子草稿快照。保留未完成/缺失附件，绝不在重启后静默少图。"""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import time
import uuid
from doubao_typeless.core.bundle import Draft
from doubao_typeless.services.assets import resolve_asset_refs


def write_json_atomic(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def draft_path(data_dir: Path) -> Path:
    return Path(data_dir) / "draft.json"


def _payload(draft: Draft) -> dict:
    return {"draft_id": draft.draft_id, "epoch": draft.epoch,
            "revision": draft.revision, "editor_device_id": draft.editor_device_id,
            "text": draft.text, "authority": draft.authority, "generation": draft.generation,
            "asset_ids": [a.get("asset_id") for a in draft.assets if a.get("asset_id")],
            "asset_documents": [{k: v for k, v in a.items() if k not in {"bytes_data", "path"}}
                                for a in draft.assets]}


def save_draft(data_dir: Path, draft: Draft) -> None:
    write_json_atomic(draft_path(data_dir), _payload(draft))


def save_recovery(data_dir: Path, draft: Draft) -> Path:
    folder = Path(data_dir) / "recovery"
    path = folder / f"{time.time_ns()}-{uuid.uuid4().hex[:8]}.json"
    write_json_atomic(path, _payload(draft))
    # 保留本地编辑恢复副本；清理策略须显式实现，不能假称已按24小时删除。
    return path


def load_draft(data_dir: Path, store) -> tuple[Draft | None, list[str]]:
    path = draft_path(data_dir)
    if not path.is_file():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assets, missing = [], []
        docs = payload.get("asset_documents")
        if not isinstance(docs, list):
            docs = [{"asset_id": ref} for ref in payload.get("asset_ids", [])]
        for doc in docs:
            ref = str(doc.get("asset_id") or "")
            try:
                item = resolve_asset_refs(store, [ref])[0] if ref else {}
            except (OSError, ValueError):
                missing.append(ref)
                # 旧快照只有ID时保留原兼容读取接口，调用方获得missing列表。
                if "asset_documents" not in payload:
                    continue
                item = {"asset_id": ref, "status": "failed"}
            for key in ("local_id", "caption", "render_revision", "status"):
                if key in doc and not (key == "status" and ref in missing):
                    item[key] = doc[key]
            if not ref:
                item["status"] = "failed"
            assets.append(item)
        return Draft(draft_id=str(payload["draft_id"]), epoch=str(payload["epoch"]),
                     revision=int(payload.get("revision", 0)),
                     editor_device_id=str(payload.get("editor_device_id") or "pc"),
                     text=str(payload.get("text") or ""), assets=assets,
                     authority=str(payload.get("authority") or "legacy"),
                     generation=int(payload.get("generation", 0))), missing
    except (ValueError, KeyError, TypeError):
        return None, []
