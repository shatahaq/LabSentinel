"""In-process publish/subscribe used to push live updates to the UI.

Publishers run on the WebSocket asyncio thread; subscribers (the Eel bridge)
live on the gevent UI thread. Subscribers must therefore be cheap and
non-blocking — typically they just enqueue onto a thread-safe queue. A failing
subscriber is logged and never breaks the publisher.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger("labsentinel.dashboard.events")

Subscriber = Callable[[str, dict[str, Any]], None]


class Topic:
    AGENT_UPDATED = "agent.updated"
    VIOLATION_NEW = "violation.new"
    COMMAND_UPDATED = "command.updated"
    SESSION_UPDATED = "session.updated"
    RULES_UPDATED = "rules.updated"
    FRAME = "frame"


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Subscriber) -> Callable[[], None]:
        with self._lock:
            self._subs.append(fn)

        def _unsubscribe() -> None:
            with self._lock:
                if fn in self._subs:
                    self._subs.remove(fn)

        return _unsubscribe

    def publish(self, topic: str, data: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(topic, data)
            except Exception:
                log.warning("event subscriber failed for %s", topic, exc_info=True)
