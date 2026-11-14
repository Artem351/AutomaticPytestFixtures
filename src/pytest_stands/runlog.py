"""Лог прогона. Метки времени ставит управляющее устройство: ISO 8601, UTC, миллисекунды."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RunLog:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "run.log"

    @classmethod
    def create(cls, base: str | Path) -> "RunLog":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return cls(Path(base) / stamp)

    def event(self, event_name: str, /, **fields) -> None:
        extra = " ".join(f"{k}={v}" for k, v in fields.items())
        line = f"{_now()} {event_name}" + (f" {extra}" if extra else "")
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
