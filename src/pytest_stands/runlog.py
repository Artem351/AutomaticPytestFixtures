from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4


class RunLog:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "run.log"
        self._lock = Lock()

    @classmethod
    def create(cls, base: str | Path) -> RunLog:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        directory = Path(base) / f"{stamp}-{uuid4().hex[:8]}"
        directory.mkdir(parents=True)
        return cls(directory)

    def event(self, event_name: str, /, **fields) -> None:
        with self._lock:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            record = {"time": timestamp, "event": event_name, **fields}
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
