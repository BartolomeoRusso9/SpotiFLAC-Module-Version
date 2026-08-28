import json
import time
from pathlib import Path

from .atomic_io import write_json_atomic
from .models import TrackMetadata


class HistoryManager:
    """Manages the search history (recent-fetches)."""

    def __init__(self) -> None:
        self.path = Path.home() / ".cache" / "spotiflac" / "recent-fetches.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, metadata: TrackMetadata) -> None:
        history = self.get_all()
        history = [h for h in history if h["id"] != metadata.id]

        entry = metadata.model_dump()
        entry["fetched_at"] = int(time.time())
        history.insert(0, entry)

        write_json_atomic(self.path, history[:50])

    def get_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def clear(self) -> None:
        write_json_atomic(self.path, [])


def get_recent_fetches() -> list[dict]:
    return HistoryManager().get_all()


def clear_recent_fetches() -> None:
    HistoryManager().clear()
