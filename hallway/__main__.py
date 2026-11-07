"""Run the server: python -m hallway"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import db, providers
from .app import create_app
from .config import ConfigError, Settings, require_ffmpeg
from .services import Services
from .spend import SpendGuard


def build_services(settings: Settings) -> Services:
    db.init(settings.db_path)
    guard = SpendGuard(settings.db_path, settings.cap_micro, settings.daily_micro)
    return Services(settings, providers.build(settings), guard)


def main() -> int:
    try:
        settings = Settings.load(os.environ, Path(".env"))
        require_ffmpeg(settings)
        svc = build_services(settings)
    except (ConfigError, db.LedgerMissing, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    svc.start_worker()
    create_app(svc).run(host=settings.host, port=settings.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
