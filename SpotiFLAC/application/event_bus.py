from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any


class EventBus:
    """Simple application-level event bus for UI and service notifications."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = defaultdict(list)

    def subscribe(self, event_name: str, callback: Callable[[dict[str, Any]], None]) -> None:
        self._listeners[event_name].append(callback)

    async def publish(self, event_name: str, payload: dict[str, Any]) -> None:
        for callback in list(self._listeners.get(event_name, [])):
            callback(payload)
