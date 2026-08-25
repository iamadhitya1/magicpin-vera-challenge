"""In-memory state for the Vera challenge bot.

Everything here is process-local memory, matching the brief's "storing in
memory is fine, just don't restart between calls" guidance. A single
threading.Lock guards all mutation since uvicorn may service requests
concurrently even in one worker.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class StoredContext:
    version: int
    payload: dict[str, Any]


class ContextStore:
    """Holds category/merchant/customer/trigger contexts, keyed by (scope, context_id)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], StoredContext] = {}

    def push(
        self, scope: str, context_id: str, version: int, payload: dict[str, Any]
    ) -> tuple[bool, Optional[str], Optional[int]]:
        """Returns (accepted, reason_if_rejected, current_version_if_rejected)."""
        key = (scope, context_id)
        with self._lock:
            cur = self._data.get(key)
            if cur is not None and cur.version >= version:
                return False, "stale_version", cur.version
            self._data[key] = StoredContext(version=version, payload=payload)
            return True, None, None

    def get(self, scope: str, context_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            entry = self._data.get((scope, context_id))
            return entry.payload if entry else None

    def counts(self) -> dict[str, int]:
        counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        with self._lock:
            for scope, _ in self._data.keys():
                counts[scope] = counts.get(scope, 0) + 1
        return counts

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


@dataclass
class ConversationState:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    trigger_id: Optional[str]
    kind: Optional[str]
    send_as: str
    sent_bodies: list[str] = field(default_factory=list)
    turns: list[dict[str, Any]] = field(default_factory=list)
    status: str = "active"  # active | ended | waiting
    last_cta: Optional[str] = None
    wait_until: Optional[float] = None
    created_at: float = field(default_factory=time.time)


class ConversationStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, ConversationState] = {}
        # Auto-reply streaks are tracked per-merchant: WhatsApp Business
        # canned auto-replies are a property of the merchant's account, not
        # of a single conversation thread.
        self._auto_reply_streak: dict[str, int] = {}
        self._sent_suppression_keys: set[str] = set()
        self._opted_out_merchants: set[str] = set()

    def create(self, state: ConversationState) -> None:
        with self._lock:
            self._data[state.conversation_id] = state

    def get(self, conversation_id: str) -> Optional[ConversationState]:
        with self._lock:
            return self._data.get(conversation_id)

    def record_sent(self, conversation_id: str, body: str, cta: Optional[str]) -> None:
        with self._lock:
            st = self._data.get(conversation_id)
            if st:
                st.sent_bodies.append(body)
                st.last_cta = cta

    def already_sent(self, conversation_id: str, body: str) -> bool:
        with self._lock:
            st = self._data.get(conversation_id)
            return bool(st and body in st.sent_bodies)

    def mark_status(self, conversation_id: str, status: str, wait_until: Optional[float] = None) -> None:
        with self._lock:
            st = self._data.get(conversation_id)
            if st:
                st.status = status
                st.wait_until = wait_until

    def bump_auto_reply_streak(self, merchant_id: str) -> int:
        with self._lock:
            n = self._auto_reply_streak.get(merchant_id, 0) + 1
            self._auto_reply_streak[merchant_id] = n
            return n

    def reset_auto_reply_streak(self, merchant_id: str) -> None:
        with self._lock:
            self._auto_reply_streak[merchant_id] = 0

    def suppression_already_sent(self, suppression_key: str) -> bool:
        with self._lock:
            return suppression_key in self._sent_suppression_keys

    def mark_suppression_sent(self, suppression_key: str) -> None:
        with self._lock:
            self._sent_suppression_keys.add(suppression_key)

    def opt_out(self, merchant_id: str) -> None:
        with self._lock:
            self._opted_out_merchants.add(merchant_id)

    def is_opted_out(self, merchant_id: str) -> bool:
        with self._lock:
            return merchant_id in self._opted_out_merchants

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._auto_reply_streak.clear()
            self._sent_suppression_keys.clear()
            self._opted_out_merchants.clear()


context_store = ContextStore()
conversation_store = ConversationStore()
