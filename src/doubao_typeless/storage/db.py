"""SQLite persistence for V3. Image bytes stay in AssetStore, not in this file."""
from __future__ import annotations

import sqlite3
import json
import threading
import time
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    referenced INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    owner_session_id TEXT
);
CREATE TABLE IF NOT EXISTS bundles (
    bundle_id TEXT PRIMARY KEY,
    draft_id TEXT,
    epoch TEXT,
    revision INTEGER,
    manifest_hash TEXT,
    text TEXT,
    recorded_at REAL NOT NULL,
    attempt_result TEXT
);
CREATE TABLE IF NOT EXISTS bundle_assets (
    bundle_id TEXT,
    asset_id TEXT,
    ordinal INTEGER,
    PRIMARY KEY (bundle_id, ordinal)
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    bundle_id TEXT,
    result TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt_steps (
    attempt_id TEXT,
    idx INTEGER,
    kind TEXT,
    asset_id TEXT,
    state TEXT,
    evidence TEXT,
    PRIMARY KEY (attempt_id, idx)
);
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    session_id TEXT,
    last_seen REAL
);
"""


class V3DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute("INSERT OR IGNORE INTO migrations(id, name) VALUES (1, 'v3-init')")
            cols = {row[1] for row in self.conn.execute("PRAGMA table_info(assets)")}
            if "owner_session_id" not in cols:
                self.conn.execute("ALTER TABLE assets ADD COLUMN owner_session_id TEXT")
            bundle_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(bundles)")}
            if "payload_json" not in bundle_cols:
                self.conn.execute("ALTER TABLE bundles ADD COLUMN payload_json TEXT")
            self.conn.commit()

    def upsert_asset(self, asset_id: str, sha256: str, nbytes: int, *,
                     referenced: bool = False, created_at: float | None = None,
                     owner_session_id: str = "") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO assets(asset_id, sha256, bytes, referenced, created_at, owner_session_id) VALUES (?,?,?,?,?,?)",
                (asset_id, sha256, nbytes, int(referenced), time.time() if created_at is None else created_at, owner_session_id or None))

    def asset(self, sha256: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM assets WHERE sha256=?", (sha256,)).fetchone()
            return dict(row) if row else None

    def asset_by_id(self, asset_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
            return dict(row) if row else None

    def transfer_asset_owner(self, asset_ids: list[str], *, from_owner: str, to_owner: str) -> list[str]:
        """只转移仍属于 from_owner 的素材；返回实际转移的素材号。"""
        moved: list[str] = []
        with self._lock, self.conn:
            for asset_id in asset_ids:
                cur = self.conn.execute("UPDATE assets SET owner_session_id=? WHERE asset_id=? AND owner_session_id=?",
                                        (to_owner, asset_id, from_owner))
                if cur.rowcount:
                    moved.append(asset_id)
        return moved

    def mark_referenced(self, asset_id: str, referenced: bool = True) -> None:
        with self._lock:
            self.conn.execute("UPDATE assets SET referenced=? WHERE asset_id=?", (int(referenced), asset_id))
            self.conn.commit()

    def gc_unreferenced(self, *, now: float, ttl_s: float, protected_ids: set[str]) -> list[str]:
        with self._lock:
            rows = self.conn.execute("SELECT asset_id, referenced, created_at FROM assets").fetchall()
            removed: list[str] = []
            for row in rows:
                asset_id = row["asset_id"]
                if asset_id in protected_ids or row["referenced"]:
                    continue
                if now - float(row["created_at"]) <= ttl_s:
                    continue
                self.conn.execute("DELETE FROM assets WHERE asset_id=?", (asset_id,))
                removed.append(asset_id)
            self.conn.commit()
            return removed

    def record_bundle(self, bundle: dict[str, Any], *, attempt_result: str) -> None:
        safe = {**bundle, "assets": [{k: v for k, v in a.items() if k != "bytes_data"}
                                     for a in bundle.get("assets", [])]}
        encoded = json.dumps(safe, ensure_ascii=False)
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO bundles(bundle_id,draft_id,epoch,revision,manifest_hash,text,recorded_at,attempt_result,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (bundle["bundle_id"],bundle.get("draft_id"),bundle.get("epoch"),bundle.get("revision"),
                 bundle.get("manifest_hash"),bundle.get("text", ""),time.time(),attempt_result,encoded))
            self.conn.execute("DELETE FROM bundle_assets WHERE bundle_id=?", (bundle["bundle_id"],))
            for index, asset in enumerate(safe["assets"]):
                aid = asset.get("asset_id")
                self.conn.execute("INSERT INTO bundle_assets(bundle_id,asset_id,ordinal) VALUES (?,?,?)",
                                  (bundle["bundle_id"],aid,index))
                if aid:
                    self.conn.execute("UPDATE assets SET referenced=1 WHERE asset_id=?",(aid,))

    def retain_history(self, keep_ids: set[str]) -> None:
        """只删除历史引用，不删图片像素；当前草稿由资产GC另行保护。"""
        with self._lock, self.conn:
            old = [r[0] for r in self.conn.execute("SELECT bundle_id FROM bundles") if r[0] not in keep_ids]
            for bid in old:
                self.conn.execute("DELETE FROM attempt_steps WHERE attempt_id IN (SELECT attempt_id FROM attempts WHERE bundle_id=?)",(bid,))
                self.conn.execute("DELETE FROM attempts WHERE bundle_id=?",(bid,))
                self.conn.execute("DELETE FROM bundle_assets WHERE bundle_id=?",(bid,))
                self.conn.execute("DELETE FROM bundles WHERE bundle_id=?",(bid,))
            self.conn.execute("UPDATE assets SET referenced=EXISTS(SELECT 1 FROM bundle_assets WHERE bundle_assets.asset_id=assets.asset_id)")

    def last_bundle(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM bundles ORDER BY recorded_at DESC LIMIT 1").fetchone()
            if not row:
                return None
            if row["payload_json"]:
                return json.loads(row["payload_json"])
            assets = [
                {"asset_id": r["asset_id"]}
                for r in self.conn.execute(
                    "SELECT asset_id FROM bundle_assets WHERE bundle_id=? ORDER BY ordinal",
                    (row["bundle_id"],),
                )
            ]
            return {
                "bundle_id": row["bundle_id"],
                "draft_id": row["draft_id"],
                "epoch": row["epoch"],
                "revision": row["revision"],
                "manifest_hash": row["manifest_hash"],
                "text": row["text"],
                "assets": assets,
            }

    def list_history(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM bundles ORDER BY recorded_at").fetchall()
            items = []
            for row in rows:
                if row["payload_json"]:
                    items.append({"recorded_at": row["recorded_at"], "attempt_result": row["attempt_result"],
                                  "bundle": json.loads(row["payload_json"])})
                    continue
                items.append(
                    {
                        "recorded_at": row["recorded_at"],
                        "attempt_result": row["attempt_result"],
                        "bundle": {
                            "bundle_id": row["bundle_id"],
                            "draft_id": row["draft_id"],
                            "epoch": row["epoch"],
                            "revision": row["revision"],
                            "manifest_hash": row["manifest_hash"],
                            "text": row["text"],
                            "assets": [
                                {"asset_id": r["asset_id"]}
                                for r in self.conn.execute(
                                    "SELECT asset_id FROM bundle_assets WHERE bundle_id=? ORDER BY ordinal",
                                    (row["bundle_id"],),
                                )
                            ],
                        },
                    }
                )
            return items

    INTENT_KEEP = 2048

    def record_intent(self, intent_id: str, result: str) -> None:
        """插入意图的结果跨重启保留：重启后同一意图仍按原结果判重，不重贴。"""
        with self._lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO intents(intent_id, result, updated_at) VALUES (?,?,?)",
                              (intent_id, result, time.time()))
            self.conn.execute("DELETE FROM intents WHERE intent_id NOT IN "
                              "(SELECT intent_id FROM intents ORDER BY updated_at DESC LIMIT ?)", (self.INTENT_KEEP,))

    def recent_intents(self) -> list[tuple[str, str]]:
        with self._lock:
            rows = self.conn.execute("SELECT intent_id, result FROM intents ORDER BY updated_at ASC LIMIT ?",
                                     (self.INTENT_KEEP,)).fetchall()
        return [(row["intent_id"], row["result"]) for row in rows]

    def record_attempt(self, attempt_id: str, bundle_id: str, result: str, steps: list[dict]) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO attempts(attempt_id, bundle_id, result, created_at) VALUES (?,?,?,?)",
                (attempt_id, bundle_id, result, time.time()),
            )
            self.conn.execute("DELETE FROM attempt_steps WHERE attempt_id=?", (attempt_id,))
            for step in steps:
                self.conn.execute(
                    "INSERT INTO attempt_steps(attempt_id, idx, kind, asset_id, state, evidence) VALUES (?,?,?,?,?,?)",
                    (
                        attempt_id,
                        step.get("index", 0),
                        step.get("kind"),
                        step.get("asset_id"),
                        step.get("state"),
                        step.get("evidence"),
                    ),
                )
            self.conn.commit()
