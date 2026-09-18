"""Fleet events: a bounded ring for replay and live fan-out for SSE clients.

The panel reads ``kind`` from a closed vocabulary (launch, handoff,
a2a_delegation, file_mod, completion, error, other), ``terminal_id``,
``timestamp`` and a metadata-only ``detail`` -- message bodies are never
recorded here.
"""

import queue
import threading
import uuid
from collections import deque
from datetime import datetime, timezone

RING_CAPACITY = 500

_KIND_OF = {
    "post_create_terminal": "launch",
    "post_create_session": "launch",
    "post_kill_terminal": "completion",
    "post_kill_session": "completion",
    "terminal_error": "error",
}


def kind_of(event_type: str, detail: dict) -> str:
    if event_type == "post_send_message":
        return "a2a_delegation" if "a2a" in str(detail.get("orchestration_type", "")).lower() else "handoff"
    return _KIND_OF.get(event_type, "other")


class EventLog:
    def __init__(self) -> None:
        self._ring: deque[dict] = deque(maxlen=RING_CAPACITY)
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def emit(self, event_type: str, terminal_id: str | None, session: str | None, **detail) -> dict:
        detail = {"event_type": event_type, **detail}
        event = {
            "id": uuid.uuid4().hex,
            "kind": kind_of(event_type, detail),
            "terminal_id": terminal_id,
            "session_name": session,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "detail": detail,
        }
        with self._lock:
            self._ring.append(event)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # a stalled client loses live events; history still has them
        return event

    def history(self, limit: int = RING_CAPACITY) -> list[dict]:
        with self._lock:
            items = list(self._ring)
        return items[-limit:] if limit > 0 else []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s is not q]


log = EventLog()
