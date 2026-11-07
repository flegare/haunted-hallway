"""The real provider: Google's Gemini API through the google-genai package.

THIS IS THE ONLY MODULE THAT TALKS TO THE NETWORK (tests/unit/test_gemini_provider.py checks it).
Every paid method needs a Reservation from SpendGuard and raises TypeError without one.

Facts this code relies on, read from Google's current documentation on 2026-11-05 and
checked against google-genai 3.0.0:
  - one call, client.interactions.create(model=..., input=[parts], response_format={...});
  - video: model gemini-omni-1.2-flash, response_format {"type": "video", "aspect_ratio": "9:16",
    "duration": "8s", "resolution": "720p", "delivery": "uri"}; a large video comes back as a
    file URI that is polled with client.files.get until ACTIVE, then downloaded;
  - image: model gemini-nano-banana-2.1, the image comes back in the steps of the answer;
  - text: model gemini-3.8-flash, JSON through response_format {"type": "text",
    "mime_type": "application/json", "schema": {...}}.
NOT verified against the real service (no key yet): the exact shape of real answers, how a real
file URI is downloaded, and that the text model accepts an image together with a JSON schema.

Safety rules:
  - ONE network request per paid call. The SDK retries 429/5xx up to 5 times by default, which
    would be hidden paid calls, so retry_options attempts=1 is always set. Our own bounded retry
    (GEMINI_RETRIES, default 0) is the only one, and it never makes a new reservation.
  - The key is passed explicitly and the official base URL is fixed in code, so an environment
    variable such as GOOGLE_GEMINI_BASE_URL cannot send the key somewhere else, and
    GOOGLE_GENAI_USE_VERTEXAI cannot switch the service.
  - The key never appears in an error message, a repr or a log line.
  - A failure before anything is sent raises NotSent (the reservation is then released); any
    failure after the request left stays booked, because it may have been billed.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import re
import tempfile
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .. import pricing
from ..spend import Reservation, require
from .base import NotSent, Provider, ProviderError, Refused

OFFICIAL_BASE_URL = "https://generativelanguage.googleapis.com/"
MAX_REFERENCE_IMAGES = 6            # the documented limit per request
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
POLL_SECONDS = 5
POLL_LIMIT_SECONDS = 300
DESCRIPTION_MAX = 120

FIGURES_SCHEMA = {
    "type": "object",
    "properties": {
        "figures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "box": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["description", "box"],
            },
        }
    },
    "required": ["figures"],
}


def _clean_description(text: object) -> str:
    """Short plain text only: no control characters, no links, no markup."""
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(text))
    value = re.sub(r"[<>`]", "", value)
    value = re.sub(r"https?://\S+", "", value)
    return re.sub(r"\s+", " ", value).strip()[:DESCRIPTION_MAX] or "a figure"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# The service answered the request itself with a refusal (bad request, auth, quota, 'unavailable'):
# it did no work, so nothing is billed. 500, 502, 504, timeouts and dropped connections are NOT in
# this set: the work may have started, so those stay booked.
REFUSED_STATUSES = frozenset(range(400, 430)) | {503}


class GeminiProvider(Provider):
    def __init__(self, api_key: str, *, text_model: str, image_model: str, video_model: str,
                 video_seconds: int = pricing.DEFAULT_VIDEO_SECONDS, retries: int = 0, max_figures: int = 2,
                 base_url: str | None = None, timeout_s: int = 300, sleep=time.sleep):
        if not api_key or len(api_key) < 20:
            raise ValueError("an API key is required")
        from google import genai          # imported here: the package is optional
        from google.genai import types
        self._key = api_key
        self.text_model, self.image_model, self.video_model = text_model, image_model, video_model
        self.video_seconds, self.retries, self.max_figures = video_seconds, retries, max_figures
        self._sleep = sleep
        self.requests_made = 0
        self._client = genai.Client(
            vertexai=False,
            api_key=api_key,
            http_options=types.HttpOptions(
                base_url=base_url or OFFICIAL_BASE_URL,
                timeout=timeout_s * 1000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    def __repr__(self) -> str:
        return f"GeminiProvider(models=({self.text_model}, {self.image_model}, {self.video_model}), key=hidden)"

    # ---- plumbing -------------------------------------------------------------------------

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***")[:300]

    def _create(self, **body):
        """One paid request (plus bounded retries on 429/5xx when GEMINI_RETRIES > 0)."""
        unclear = False   # an earlier attempt ended in a way that may have been billed
        for attempt in range(self.retries + 1):
            self.requests_made += 1
            try:
                return self._client.interactions.create(**body)
            except Exception as exc:  # noqa: BLE001 - everything is turned into a scrubbed ProviderError
                status = getattr(exc, "status_code", None)
                retryable = status == 429 or (isinstance(status, int) and 500 <= status <= 599)
                unclear = unclear or status not in REFUSED_STATUSES
                if retryable and attempt < self.retries:
                    self._sleep(min(2 ** attempt * 2, 20))
                    continue
                detail = self._scrub(str(getattr(exc, "message", "") or exc))
                message = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "") + f": {detail}"
                if not unclear:
                    raise Refused(message) from None   # every attempt was refused: no work done, nothing owed
                raise ProviderError(message) from None

    @staticmethod
    def _check_done(interaction) -> None:
        status = str(getattr(interaction, "status", "") or "")
        if status and status.lower() not in ("completed", "complete", "succeeded", "done"):
            raise ProviderError(f"the service ended the request with status '{status[:40]}'")

    @staticmethod
    def _blocks(interaction, kind: str):
        """Content blocks of a given type from the model's own output steps. Thought steps are skipped."""
        for step in getattr(interaction, "steps", None) or []:
            if getattr(step, "type", None) != "model_output":
                continue
            for block in getattr(step, "content", None) or []:
                if getattr(block, "type", None) == kind:
                    yield block

    @staticmethod
    def _open_image(data: bytes, what: str) -> Image.Image:
        if len(data) > MAX_IMAGE_BYTES:
            raise ProviderError(f"{what} is too large")
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ProviderError(f"{what} is not a readable image") from None

    @staticmethod
    def _local_image(data: bytes, what: str) -> Image.Image:
        """An image WE are about to send. A problem here means nothing has left the machine."""
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            return img
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise NotSent(f"{what} is not a readable image; nothing was sent") from None

    # ---- 1. count and describe ------------------------------------------------------------

    def count_and_describe(self, image_path: Path, res: Reservation) -> list[dict]:
        require(res)
        try:
            raw = Path(image_path).read_bytes()
        except OSError:
            raise NotSent("the drawing file cannot be read; nothing was sent") from None
        img = self._local_image(raw, "the drawing")
        width, height = img.size
        jpeg = io.BytesIO()
        img.convert("RGB").save(jpeg, "JPEG", quality=90)
        prompt = (
            f"This is a child's drawing. List each separate figure that is drawn (a monster, ghost, creature or person), "
            f"at most {self.max_figures}. For each one give a short plain description of at most 12 words, without names "
            f"and without copying any text written on the page, and its bounding box as [ymin, xmin, ymax, xmax] with "
            f"integer values from 0 to 1000 relative to the image. If you see no figure, return an empty list."
        )
        interaction = self._create(
            model=self.text_model,
            input=[{"type": "image", "data": _b64(jpeg.getvalue()), "mime_type": "image/jpeg"},
                   {"type": "text", "text": prompt}],
            response_format={"type": "text", "mime_type": "application/json", "schema": FIGURES_SCHEMA},
        )
        self._check_done(interaction)
        text = "".join(getattr(b, "text", "") or "" for b in self._blocks(interaction, "text"))
        try:
            figures = json.loads(text)["figures"]
            if not isinstance(figures, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            raise ProviderError("the text model did not return the expected JSON") from None
        items = []
        for fig in figures:
            try:
                ymin, xmin, ymax, xmax = (int(v) for v in fig["box"])
                desc = _clean_description(fig["description"])
            except (KeyError, TypeError, ValueError):
                continue
            x0, x1 = sorted((max(0, min(1000, xmin)), max(0, min(1000, xmax))))
            y0, y1 = sorted((max(0, min(1000, ymin)), max(0, min(1000, ymax))))
            box = [x0 * width // 1000, y0 * height // 1000, x1 * width // 1000, y1 * height // 1000]
            if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                continue
            items.append({"description": desc, "box": box})
        items = items[: self.max_figures]
        return items or [{"description": "the whole drawing", "box": [0, 0, width, height]}]

    # ---- 2. extract one character ---------------------------------------------------------

    def extract_character(self, image_path: Path, item: dict, res: Reservation) -> bytes:
        require(res)
        try:
            raw = Path(image_path).read_bytes()
            x0, y0, x1, y1 = (int(v) for v in item["box"])
        except (OSError, KeyError, TypeError, ValueError):
            raise NotSent("the drawing or the box is unusable; nothing was sent") from None
        img = self._local_image(raw, "the drawing")
        pad_x, pad_y = (x1 - x0) // 10, (y1 - y0) // 10
        box = (max(0, x0 - pad_x), max(0, y0 - pad_y), min(img.width, x1 + pad_x), min(img.height, y1 + pad_y))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise NotSent("the box is empty; nothing was sent")
        crop = io.BytesIO()
        img.convert("RGB").crop(box).save(crop, "JPEG", quality=92)
        description = _clean_description(item.get("description", "a figure"))
        prompt = (
            f"Redraw only this one figure from the child's drawing ({description}) as a single character on a plain "
            f"white background. Keep the child's drawing style, shapes and colours. No text, no border, nothing else."
        )
        interaction = self._create(
            model=self.image_model,
            input=[{"type": "text", "text": prompt},
                   {"type": "image", "data": _b64(crop.getvalue()), "mime_type": "image/jpeg"}],
        )
        self._check_done(interaction)
        last = None
        for block in self._blocks(interaction, "image"):       # the LAST image of the model's output
            last = block
        if last is None or not getattr(last, "data", None):
            raise ProviderError("the image model returned no image")
        try:
            data = base64.b64decode(last.data, validate=True)
        except (binascii.Error, ValueError):
            raise ProviderError("the image model returned unreadable data") from None
        out = self._open_image(data, "the character image")
        png = io.BytesIO()
        out.convert("RGB").save(png, "PNG")                   # re-encoded: no metadata is kept
        return png.getvalue()

    # ---- 3. make the video ----------------------------------------------------------------

    def make_video(self, refs: list[bytes], prompt: str, res: Reservation) -> bytes:
        require(res)
        if not 1 <= len(refs) <= min(self.max_figures, MAX_REFERENCE_IMAGES):
            raise NotSent(f"between 1 and {min(self.max_figures, MAX_REFERENCE_IMAGES)} reference images are allowed; nothing was sent")
        parts = []
        for number, data in enumerate(refs, start=1):
            img = self._local_image(data, f"reference image {number}")
            png = io.BytesIO()
            img.convert("RGB").save(png, "PNG")
            parts.append({"type": "image", "data": _b64(png.getvalue()), "mime_type": "image/png"})
        parts.append({"type": "text", "text": str(prompt)[:600]})
        interaction = self._create(
            model=self.video_model,
            input=parts,
            response_format={"type": "video", "aspect_ratio": "9:16", "duration": f"{self.video_seconds}s",
                             "resolution": "720p", "delivery": "uri"},
        )
        self._check_done(interaction)
        block = None
        for block in self._blocks(interaction, "video"):
            pass
        if block is None:
            raise ProviderError("the video model returned no video")
        if getattr(block, "data", None):
            try:
                data = base64.b64decode(block.data, validate=True)
            except (binascii.Error, ValueError):
                raise ProviderError("the video model returned unreadable data") from None
        elif getattr(block, "uri", None):
            data = self._fetch_file(str(block.uri))
        else:
            raise ProviderError("the video answer holds neither data nor a file address")
        if len(data) > MAX_VIDEO_BYTES or data[4:8] != b"ftyp":
            raise ProviderError("the video is not a usable MP4 file")
        return data

    def _fetch_file(self, uri: str) -> bytes:
        """Wait until the generated file is ACTIVE, then download it. These calls are not paid."""
        name = uri.rstrip("/").split("/")[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise ProviderError("the video file address looks wrong")
        waited = 0
        while True:
            try:
                info = self._client.files.get(name=f"files/{name}")
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"could not read the video file state: {type(exc).__name__}") from None
            state = str(getattr(getattr(info, "state", None), "name", getattr(info, "state", "")) or "").upper()
            if state == "ACTIVE":
                break
            if state == "FAILED":
                raise ProviderError("the service reports that making the video failed")
            if waited >= POLL_LIMIT_SECONDS:
                raise ProviderError("the video file did not become ready in time")
            self._sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clip.mp4"
            try:
                self._client.files.download(file=uri, destination=str(target))
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(f"could not download the video: {type(exc).__name__}") from None
            if not target.is_file() or target.stat().st_size > MAX_VIDEO_BYTES:
                raise ProviderError("the downloaded video is missing or too large")
            return target.read_bytes()
