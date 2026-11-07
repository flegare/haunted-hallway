"""Free fake provider: real pixels and a real H.264 MP4, no network, no cost."""
from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from ..spend import Reservation, require
from .base import Provider

MAX_FIGURES = 4


class MockProvider(Provider):
    def __init__(self, ffmpeg: str, timeout: int = 120):
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found; install it or set FFMPEG_BIN")
        self.ffmpeg = ffmpeg
        self.timeout = timeout
        self.calls = {"describe": 0, "extract": 0, "video": 0}

    def count_and_describe(self, image_path: Path, res: Reservation) -> list[dict]:
        require(res)
        self.calls["describe"] += 1
        with Image.open(image_path) as img:
            gray = img.convert("L")
            width, height = gray.size
            dark = gray.point(lambda p: 255 if p < 128 else 0)
            occupied = []
            for x in range(width):
                col = dark.crop((x, 0, x + 1, height))
                occupied.append(col.getbbox() is not None)
        gap = max(2, width // 20)
        runs, start, blank = [], None, 0
        for x, on in enumerate(occupied + [False] * (gap + 1)):
            if on:
                if start is None:
                    start = x
                blank = 0
            elif start is not None:
                blank += 1
                if blank >= gap:
                    runs.append((start, x - blank + 1))
                    start, blank = None, 0
        runs = [r for r in runs if r[1] - r[0] >= 3][:MAX_FIGURES] or [(0, width)]
        return [
            {"description": f"figure {i + 1} of {len(runs)}", "box": [a, 0, b, height]}
            for i, (a, b) in enumerate(runs)
        ]

    def extract_character(self, image_path: Path, item: dict, res: Reservation) -> bytes:
        require(res)
        self.calls["extract"] += 1
        with Image.open(image_path) as img:
            crop = img.convert("RGBA").crop(tuple(item["box"]))
        out = io.BytesIO()
        crop.save(out, "PNG")
        return out.getvalue()

    def make_video(self, refs: list[bytes], prompt: str, res: Reservation) -> bytes:
        require(res)
        if not 1 <= len(refs) <= MAX_FIGURES:
            raise ValueError("1 to 4 reference images")
        self.calls["video"] += 1
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "mock.mp4"
            cmd = [
                self.ffmpeg, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=c=0x201020:s=576x1024:d=2:r=24",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out),
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=self.timeout)
            return out.read_bytes()
