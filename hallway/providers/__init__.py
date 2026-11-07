"""Providers turn drawings into characters and clips. Every paid method needs a Reservation."""
from __future__ import annotations

from ..config import Settings
from .base import Provider


def build(settings: Settings) -> Provider:
    if settings.provider == "mock":
        from .mock import MockProvider
        return MockProvider(settings.ffmpeg)
    if settings.provider == "gemini":
        try:
            from .gemini import GeminiProvider
        except ImportError as exc:                      # the optional package is not installed
            raise RuntimeError("PROVIDER=gemini needs the google-genai package: pip install -e '.[gemini]'") from exc
        return GeminiProvider(
            settings.gemini_key, text_model=settings.text_model, image_model=settings.image_model,
            video_model=settings.video_model, video_seconds=settings.video_seconds,
            retries=settings.gemini_retries, max_figures=settings.max_figures)
    raise ValueError(settings.provider)
