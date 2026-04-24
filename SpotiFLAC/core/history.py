import json
import time
from pathlib import Path
from dataclasses import asdict
from typing import List
from .models import TrackMetadata

class HistoryManager:
    """Gestisce la cronologia delle ricerche (recent-fetches)."""

    def __init__(self):
        self.path = Path.home() / ".cache" / "spotiflac" / "recent-fetches.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, metadata: TrackMetadata):
        history = self.get_all()
        # Rimuovi se già presente (deduplicazione)
        history = [h for h in history if h['id'] != metadata.id]

        entry = asdict(metadata)
        entry['fetched_at'] = int(time.time())
        history.insert(0, entry)

        # Mantieni ultimi 50
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(history[:50], f, indent=2)

    def get_all(self) -> List[dict]:
        if not self.path.exists(): return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except: return []