from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db, pipeline, pricing
from .config import Settings
from .providers.base import Provider
from .spend import SpendGuard


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def new_id() -> str:
    return secrets.token_hex(16)


@dataclass
class Services:
    settings: Settings
    provider: Provider
    guard: SpendGuard
    _stop: threading.Event = field(default_factory=threading.Event)

    def price(self, kind: str) -> int:
        """Micro-dollars to reserve for one call. The free fake provider reserves nothing,
        unless MOCK_PRICES=1 (to rehearse the spend cap without spending)."""
        if self.settings.provider == "mock" and not self.settings.mock_priced:
            return 0
        return pricing.estimate(kind, self.settings.video_seconds, self.settings.estimates)

    def connect(self):
        return db.connect(self.settings.db_path)

    def media_path(self, kind: str, file_id: str, ext: str) -> Path:
        folder = self.settings.media_dir / kind
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{file_id}.{ext}"

    def process_all(self) -> int:
        """Run every queued job once. Returns how many were processed."""
        count = 0
        while pipeline.process_next(self):
            count += 1
        return count

    def start_worker(self, interval: float = 1.0) -> threading.Thread:
        def loop():
            while not self._stop.wait(interval):
                pipeline.reap_stale(self)
                pipeline.process_next(self)
        thread = threading.Thread(target=loop, name="hh-worker", daemon=True)
        thread.start()
        return thread

    def stop_worker(self) -> None:
        self._stop.set()
