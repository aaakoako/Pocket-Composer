"""一次性投递登记：重放不补发，异常释放 busy，不覆盖其他操作的锁。"""
from __future__ import annotations
from collections import OrderedDict
import threading
from typing import Literal

Decision = Literal["accept", "duplicate", "busy"]

# 这些结果保证本次没有向目标发出任何按键，用户明确再点时可用同一意图重试。
# RUNNING/UNKNOWN/PARTIAL/CONFIRMED 可能已有内容到达目标，永远不自动重贴。
RETRYABLE_RESULTS = frozenset({"NO_STEPS", "CANCELLED"})


class IntentLedger:
    def __init__(self):
        self.busy = False
        self._seen: OrderedDict[str, str] = OrderedDict()
        self._owner: str | None = None
        self._lock = threading.RLock()

    def begin(self, intent_id: str) -> Decision:
        if not intent_id:
            raise ValueError("intent_id required")
        with self._lock:
            previous = self._seen.get(intent_id)
            if previous is not None and previous not in RETRYABLE_RESULTS:
                return "duplicate"
            if self.busy:
                return "busy"
            self.busy = True
            self._owner = intent_id
            self._seen[intent_id] = "RUNNING"
            self._seen.move_to_end(intent_id)
            return "accept"

    def seed(self, entries) -> None:
        """从持久记录恢复。重启时仍为 RUNNING 的意图可能已贴出一部分，按 UNKNOWN 处理。"""
        with self._lock:
            for intent_id, result in entries:
                if intent_id:
                    self._seen[intent_id] = "UNKNOWN" if result == "RUNNING" else result
                    self._seen.move_to_end(intent_id)
            while len(self._seen) > 2048:
                self._seen.popitem(last=False)

    def finish(self, intent_id: str, result: str) -> None:
        with self._lock:
            self._seen[intent_id] = result
            if self._owner == intent_id:
                self._owner = None
                self.busy = False
            while len(self._seen) > 2048:
                self._seen.popitem(last=False)

    def status(self, intent_id: str) -> str | None:
        with self._lock:
            return self._seen.get(intent_id)
