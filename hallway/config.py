"""Settings. This is the only module that reads the environment or .env."""
from __future__ import annotations

import math
import os
import re
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import passwd, pricing

DEFAULT_CAP_USD = 5.0
DEFAULT_DAILY_USD = 2.0
MAX_CAP_USD = 1000.0
MIN_TOKEN_LEN = 16


class ConfigError(Exception):
    pass


def parse_cap(raw: object, default: float) -> float:
    """A cap is a finite number in (0, MAX_CAP_USD]. Anything else gives the default."""
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0 or value > MAX_CAP_USD:
        return default
    return value


def _bounded_int(values: Mapping[str, str], key: str, default: int, low: int, high: int) -> int:
    raw = values.get(key)
    if raw is None:
        return default
    try:
        number = int(str(raw).strip())
    except ValueError:
        raise ConfigError(f"{key} must be a whole number from {low} to {high}") from None
    if not low <= number <= high:
        raise ConfigError(f"{key} must be a whole number from {low} to {high}")
    return number


ESTIMATE_CEILING_USD = 10.0
ESTIMATE_FLOOR_DIVISOR = 10  # an estimate may be lowered to a tenth of the built-in value, no further


def estimate_floor_micro(default: int) -> int:
    """The lowest accepted value for an estimate: a tenth of the built-in one. Below that the cap
    would reserve next to nothing and stop protecting the budget."""
    return max(1, default // ESTIMATE_FLOOR_DIVISOR)


def _estimate_micro(values: Mapping[str, str], key: str, default: int) -> int:
    """An estimate in USD from the environment, as micro-dollars. A bad value stops the start:
    silently falling back could hide that the cap is reserving the wrong amount. The value must be
    between the floor (a tenth of the built-in estimate) and 10 USD."""
    raw = values.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        usd = float(str(raw).strip())
    except ValueError:
        raise ConfigError(f"{key} must be a number of US dollars") from None
    floor = estimate_floor_micro(default)
    if not math.isfinite(usd) or not floor <= round(usd * 1_000_000) <= ESTIMATE_CEILING_USD * 1_000_000:
        raise ConfigError(f"{key} must be between {floor / 1_000_000:g} and {ESTIMATE_CEILING_USD:g} US dollars "
                          f"(the floor is a tenth of the built-in estimate)")
    return round(usd * 1_000_000)


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    cap_micro: int
    daily_micro: int
    admin_token: str = field(repr=False)
    password_hash: str = field(repr=False)
    loop_token: str = field(repr=False)
    provider: str
    mock_priced: bool
    ffmpeg: str
    host: str
    port: int
    secret: bytes = field(repr=False)
    gemini_key: str = field(repr=False, default="")
    text_model: str = pricing.DEFAULT_MODELS["text"]
    image_model: str = pricing.DEFAULT_MODELS["image"]
    video_model: str = pricing.DEFAULT_MODELS["video"]
    video_seconds: int = pricing.DEFAULT_VIDEO_SECONDS
    max_figures: int = 2
    gemini_retries: int = 0
    estimates: pricing.Estimates = pricing.DEFAULT_ESTIMATES

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hallway.sqlite3"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @classmethod
    def load(cls, env: Mapping[str, str], dotenv: Path | None = None) -> "Settings":
        merged: dict[str, str] = {}
        if dotenv is not None:
            merged.update(read_dotenv(dotenv))
        merged.update({k: v for k, v in env.items() if v != ""})

        admin = merged.get("ADMIN_TOKEN", "")
        pw_hash = merged.get("ADMIN_PASSWORD_HASH", "")
        if not admin and not pw_hash:
            raise ConfigError("set ADMIN_PASSWORD_HASH (or ADMIN_TOKEN); refusing to start without a credential")
        if admin and len(admin) < MIN_TOKEN_LEN:
            raise ConfigError(f"ADMIN_TOKEN must be {MIN_TOKEN_LEN}+ characters")
        if pw_hash:
            try:
                passwd.parse(pw_hash)
            except ValueError as exc:
                raise ConfigError(f"ADMIN_PASSWORD_HASH is malformed: {exc}") from exc
        loop = merged.get("LOOP_TOKEN", "")
        if loop and len(loop) < MIN_TOKEN_LEN:
            raise ConfigError(f"LOOP_TOKEN must be {MIN_TOKEN_LEN}+ characters")
        if loop and loop == admin:
            raise ConfigError("LOOP_TOKEN must differ from ADMIN_TOKEN")

        data_dir = Path(merged.get("DATA_DIR", "./data")).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)

        ffmpeg = merged.get("FFMPEG_BIN") or shutil.which("ffmpeg") or ""
        provider = merged.get("PROVIDER", "mock")
        if provider not in ("mock", "gemini"):
            raise ConfigError("PROVIDER must be 'mock' or 'gemini'")
        gemini_key = merged.get("GEMINI_API_KEY", "").strip()
        if provider == "gemini" and (len(gemini_key) < 20 or any(ch.isspace() for ch in gemini_key)):
            raise ConfigError("PROVIDER=gemini needs GEMINI_API_KEY (a single token, 20+ characters); it is not shown here")
        models = {}
        for kind in ("text", "image", "video"):
            name = merged.get(f"GEMINI_{kind.upper()}_MODEL", pricing.DEFAULT_MODELS[kind]).strip()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,80}", name):
                raise ConfigError(f"GEMINI_{kind.upper()}_MODEL is not a plausible model name")
            models[kind] = name
        video_seconds = _bounded_int(merged, "VIDEO_SECONDS", pricing.DEFAULT_VIDEO_SECONDS, 3, 15)
        max_figures = _bounded_int(merged, "MAX_FIGURES", 2, 1, 4)
        retries = _bounded_int(merged, "GEMINI_RETRIES", 0, 0, 2)
        base = pricing.DEFAULT_ESTIMATES
        estimates = pricing.Estimates(
            text_micro=_estimate_micro(merged, "ESTIMATE_TEXT_USD", base.text_micro),
            image_micro=_estimate_micro(merged, "ESTIMATE_IMAGE_USD", base.image_micro),
            video_second_micro=_estimate_micro(merged, "ESTIMATE_VIDEO_SECOND_USD", base.video_second_micro))

        return cls(
            data_dir=data_dir,
            cap_micro=int(parse_cap(merged.get("SPEND_CAP_USD"), DEFAULT_CAP_USD) * 1_000_000),
            daily_micro=int(parse_cap(merged.get("SPEND_CAP_DAILY_USD"), DEFAULT_DAILY_USD) * 1_000_000),
            admin_token=admin,
            password_hash=pw_hash,
            loop_token=loop,
            provider=provider,
            mock_priced=merged.get("MOCK_PRICES", "0") == "1",
            ffmpeg=ffmpeg,
            host=merged.get("HOST", "127.0.0.1"),
            port=int(merged.get("PORT", "8080")),
            secret=_session_secret(data_dir),
            gemini_key=gemini_key,
            text_model=models["text"],
            image_model=models["image"],
            video_model=models["video"],
            video_seconds=video_seconds,
            max_figures=max_figures,
            gemini_retries=retries,
            estimates=estimates,
        )


def require_ffmpeg(settings: "Settings") -> None:
    """Fail at startup, with a clear message, when ffmpeg is missing or does not run."""
    hint = "ffmpeg is required. Install it (Debian, Ubuntu, Raspberry Pi OS: sudo apt install ffmpeg; macOS: brew install ffmpeg) or set FFMPEG_BIN in .env"
    path = settings.ffmpeg
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise ConfigError(f"ffmpeg not found. {hint}")
    try:
        out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=15, errors="replace")
        ok = out.returncode == 0 and out.stdout.lstrip().startswith("ffmpeg version")
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        raise ConfigError(f"{path} is not a working ffmpeg (its -version output must start with 'ffmpeg version'). {hint}")


def _session_secret(data_dir: Path) -> bytes:
    path = data_dir / ".session_secret"
    if path.is_file():
        return bytes.fromhex(path.read_text().strip())
    value = secrets.token_bytes(32)
    path.write_text(value.hex())
    path.chmod(0o600)
    return value
