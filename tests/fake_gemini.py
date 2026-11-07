"""A local stand-in for the Gemini API. It counts every request, so a test can prove how many
calls a code path made, and it can fail, stall or echo the API key on demand."""
import base64
import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from hallway import pricing

import functools
import subprocess
import tempfile
from pathlib import Path


@functools.lru_cache(maxsize=1)
def real_mp4() -> bytes:
    """A real, decodable 2-second H.264 file made with ffmpeg, so the whole pipeline can finish it."""
    from tests.conftest import ffmpeg_bin
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "fake.mp4"
        subprocess.run([ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x302040:s=576x1024:d=2:r=24",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                       check=True, capture_output=True, timeout=60)
        return out.read_bytes()


def png_bytes(color="white", size=(160, 160)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


class FakeGemini:
    def __init__(self, key: str):
        self.key = key
        self.requests: list[dict] = []
        self.count = {"text": 0, "image": 0, "video": 0, "file_get": 0, "download": 0}
        self.errors: dict[str, list[int]] = {}        # kind -> statuses to answer before succeeding
        self.stall: dict[str, float] = {}             # kind -> seconds to wait before answering
        self.echo_key = False                         # put the API key in error messages
        self.figures = [{"description": "a green ghost", "box": [100, 50, 900, 450]},
                        {"description": "a purple monster", "box": [100, 550, 900, 950]}]
        self.text_override: str | None = None         # raw text instead of the JSON above
        self.image_blocks = 1                         # how many image blocks the image answer holds
        self.image_data: bytes | None = None
        self.thought_step = False
        self.delivery = "uri"                         # "uri" or "inline"
        self.file_states = ["ACTIVE"]
        self.video_bytes = real_mp4()
        self.usage = {"text": (900, 120), "image": (800, 1290), "video": (3000, 46336)}
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw) if raw else None
                kind = fake.classify(body)
                with fake.lock:
                    fake.requests.append({"method": "POST", "path": self.path, "kind": kind,
                                          "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                    fake.count[kind] += 1
                    queued = fake.errors.get(kind) or []
                    status = queued.pop(0) if queued else None
                if kind in fake.stall:
                    time.sleep(fake.stall[kind])
                if status:
                    message = "request refused" + (f" for key {fake.key}" if fake.echo_key else "")
                    return self._send(status, json.dumps({"error": {"code": status, "message": message, "status": "ERROR"}}).encode())
                self._send(200, json.dumps(fake.answer(kind)).encode())

            def do_GET(self):
                with fake.lock:
                    is_download = "alt=media" in self.path
                    fake.requests.append({"method": "GET", "path": self.path, "kind": "download" if is_download else "file_get",
                                          "headers": {k.lower(): v for k, v in self.headers.items()}, "body": None})
                    fake.count["download" if is_download else "file_get"] += 1
                    state = fake.file_states.pop(0) if len(fake.file_states) > 1 else fake.file_states[0]
                if is_download:
                    return self._send(200, fake.video_bytes, "video/mp4")
                m = re.search(r"/files/([A-Za-z0-9_-]+)", self.path)
                if not m:
                    return self._send(404, b"{}")
                self._send(200, json.dumps({"name": f"files/{m.group(1)}", "state": state, "mimeType": "video/mp4",
                                            "uri": f"{fake.url}/v1beta/files/{m.group(1)}"}).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ---- what the fake answers ----
    @staticmethod
    def classify(body) -> str:
        model = (body or {}).get("model", "")
        if "omni" in model:
            return "video"
        if "banana" in model or "image" in model:
            return "image"
        return "text"

    def answer(self, kind: str) -> dict:
        tin, tout = self.usage[kind]
        usage = {"total_input_tokens": tin, "total_output_tokens": tout, "total_tokens": tin + tout}
        steps = []
        if self.thought_step:
            steps.append({"type": "thought", "signature": "c2ln", "summary": [{"type": "text", "text": "thinking"}]})
        if kind == "text":
            text = self.text_override if self.text_override is not None else json.dumps({"figures": self.figures})
            content = [{"type": "text", "text": text}]
        elif kind == "image":
            data = self.image_data if self.image_data is not None else png_bytes("white")
            content = [{"type": "image", "mime_type": "image/png", "data": base64.b64encode(data).decode()}] * self.image_blocks
            if self.image_blocks > 1:      # the first image is a decoy, the LAST one is the answer
                content[0] = {"type": "image", "mime_type": "image/png", "data": base64.b64encode(png_bytes("black")).decode()}
        else:
            if self.delivery == "inline":
                content = [{"type": "video", "mime_type": "video/mp4", "data": base64.b64encode(self.video_bytes).decode()}]
            else:
                content = [{"type": "video", "mime_type": "video/mp4", "uri": f"{self.url}/v1beta/files/abc123"}]
        steps.append({"type": "model_output", "content": content})
        return {"id": f"i-{kind}", "status": "completed", "usage": usage, "steps": steps}

    def reqs(self, kind=None, method=None):
        return [r for r in self.requests if (kind is None or r["kind"] == kind) and (method is None or r["method"] == method)]
