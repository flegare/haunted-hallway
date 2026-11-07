from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..spend import Reservation


class ProviderError(Exception):
    """A paid call failed. The reservation stays booked: the call may have been billed."""


class NotSent(ProviderError):
    """Failed BEFORE any network request left the machine, so the reservation can be released."""


class Refused(NotSent):
    """The service refused the request before doing any work (for example HTTP 503): no bill."""


class Provider(ABC):
    """Three paid operations. Each takes a Reservation and must call spend.require() first."""

    # A cost in micro-dollars a provider may report after a call, or None when unknown (the default,
    # and what the Gemini provider does: it holds no price list). Recorded next to the reservation.
    last_measured_micro: int | None = None

    @abstractmethod
    def count_and_describe(self, image_path: Path, res: Reservation) -> list[dict]:
        """Return one dict per figure found (max 4): {"description": str, "box": [x0, y0, x1, y1]}."""

    @abstractmethod
    def extract_character(self, image_path: Path, item: dict, res: Reservation) -> bytes:
        """Return PNG bytes of one isolated character."""

    @abstractmethod
    def make_video(self, refs: list[bytes], prompt: str, res: Reservation) -> bytes:
        """Return MP4 bytes made from 1 to 4 character images."""
