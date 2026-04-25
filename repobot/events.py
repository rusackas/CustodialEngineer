"""In-process Server-Sent Events hub.

Mutation paths emit small events via `broadcast(event_type, data)`;
subscribers (SSE clients via the `/events` endpoint) pull them off
their own thread-safe queue. The FastAPI handler then streams each
event as `text/event-stream` so the browser's `EventSource` picks
it up and surgically refreshes only the affected UI surface.

Why this replaces the every-3s polling: with SSE, the client hears
about state changes the moment they happen (no 3s latency, no
polling-vs-typing collisions on quiet ticks) and the server only
sends data when there's actually something to send. The 30s
fallback poll on each container catches the rare case where the
SSE connection drops without a clean reconnect.

Concurrency model:

- Each subscriber owns a `queue.Queue` (stdlib, thread-safe).
  Bounded at `_QUEUE_MAX` so a wedged client can't grow memory
  forever.
- `broadcast()` is callable from any thread — runs synchronously,
  uses `put_nowait`. Slow subscribers are best-effort: if their
  queue is full we drop the event rather than block writers. UI
  refresh is recoverable (the next mutation broadcasts again, so
  worst case the user waits one extra event for a stale view to
  catch up; the 30s fallback poll backstops it regardless).
- The SSE handler bridges the sync queue to async via
  `asyncio.to_thread` so the server side can hold thousands of
  concurrent connections without a thread per subscriber.
"""
from __future__ import annotations

import queue as _q
import threading
from typing import Any

_QUEUE_MAX = 200

_subscribers: list[_q.Queue] = []
_lock = threading.Lock()


def broadcast(event_type: str, data: dict[str, Any] | None = None) -> None:
    """Fan out one event to every subscriber. Thread-safe; non-blocking."""
    payload = {"event": event_type, "data": data or {}}
    with _lock:
        subs = list(_subscribers)
    for sub_q in subs:
        try:
            sub_q.put_nowait(payload)
        except _q.Full:
            # Drop the event for this slow subscriber rather than
            # block the writer. The next event triggers another
            # refetch anyway; the 30s fallback poll backstops.
            pass


def subscribe() -> _q.Queue:
    """Register a subscriber and return its private queue. Caller
    MUST call `unsubscribe(q)` when done — typically in a `finally`
    block in the streaming endpoint."""
    sub_q: _q.Queue = _q.Queue(maxsize=_QUEUE_MAX)
    with _lock:
        _subscribers.append(sub_q)
    return sub_q


def unsubscribe(sub_q: _q.Queue) -> None:
    """Drop a subscriber on disconnect."""
    with _lock:
        try:
            _subscribers.remove(sub_q)
        except ValueError:
            pass


def subscriber_count() -> int:
    """For diagnostics — how many SSE clients are currently
    connected. Useful in a future header-readout pill."""
    with _lock:
        return len(_subscribers)


def _blocking_get(sub_q: _q.Queue, timeout: float):
    """Helper for `asyncio.to_thread` — returns the next event or
    `None` on timeout (so the SSE handler can emit a keepalive)."""
    try:
        return sub_q.get(timeout=timeout)
    except _q.Empty:
        return None
