"""Cost ESTIMATES in micro-dollars (a millionth of a dollar).

These numbers are not Google's prices. They are rough placeholders that the spend cap reserves before
each paid call, so a call can be refused if it might pass the cap. Check Google's current pricing
and set ESTIMATE_TEXT_USD, ESTIMATE_IMAGE_USD and ESTIMATE_VIDEO_SECOND_USD in .env to match what
you pay. Nothing here knows what a call really costs: your Google bill is the only authority.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_VIDEO_SECONDS = 8

DEFAULT_MODELS = {
    "text": "gemini-3.8-flash",
    "image": "gemini-nano-banana-2.1",
    "video": "gemini-omni-1.2-flash",
}
VIDEO_MARGIN_MICRO = 10_000          # added to the video estimate for the input side


@dataclass(frozen=True)
class Estimates:
    """What to reserve per call, in micro-dollars. Editable through .env."""
    text_micro: int = 5_000
    image_micro: int = 40_000
    video_second_micro: int = 69_504


DEFAULT_ESTIMATES = Estimates()


def estimate(kind: str, video_seconds: int = DEFAULT_VIDEO_SECONDS, est: Estimates = DEFAULT_ESTIMATES) -> int:
    """Micro-dollars to reserve before one call, rounded up."""
    if kind == "text":
        return est.text_micro
    if kind == "image":
        return est.image_micro
    if kind == "video":
        return math.ceil(est.video_second_micro * video_seconds) + VIDEO_MARGIN_MICRO
    raise KeyError(kind)
