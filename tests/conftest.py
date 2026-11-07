import io
import os
import shutil

import pytest
from PIL import Image, ImageDraw

from hallway import db, providers
from hallway.app import create_app
from hallway.config import Settings
from hallway.services import Services
from hallway.spend import SpendGuard

from hallway import passwd

ADMIN = "a" * 24
LOOP = "l" * 24
PASSWORD = "correct horse battery staple"
PASSWORD_HASH = passwd.hash_password(PASSWORD, n=2 ** 10)  # low cost: tests only


def ffmpeg_bin() -> str:
    path = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
    if not path:
        pytest.fail("ffmpeg not found: install it or set FFMPEG_BIN")
    return path


def make_settings(tmp_path, **extra):
    env = {"ADMIN_TOKEN": ADMIN, "ADMIN_PASSWORD_HASH": PASSWORD_HASH, "LOOP_TOKEN": LOOP, "DATA_DIR": str(tmp_path / "data"),
           "FFMPEG_BIN": ffmpeg_bin(), "PROVIDER": "mock"}
    env.update(extra)
    return Settings.load(env)


def make_services(settings):
    db.init(settings.db_path)
    guard = SpendGuard(settings.db_path, settings.cap_micro, settings.daily_micro)
    return Services(settings, providers.build(settings), guard)


@pytest.fixture
def svc(tmp_path):
    # MOCK_PRICES=1 makes the mock reserve real estimates, so the cap is exercised.
    # Cap of 0.90 USD: room for exactly one 2-figure clip (0.651032 USD estimated), not two (1.302).
    return make_services(make_settings(tmp_path, SPEND_CAP_USD="0.9", MOCK_PRICES="1"))


@pytest.fixture
def client(svc):
    return create_app(svc).test_client()


@pytest.fixture
def admin():
    return {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture
def loop():
    return {"Authorization": f"Bearer {LOOP}"}


def drawing_png(figures=2) -> bytes:
    """A white page with N separated black blobs."""
    img = Image.new("RGB", (600, 400), "white")
    d = ImageDraw.Draw(img)
    for i in range(figures):
        x = 40 + i * 260
        d.ellipse((x, 100, x + 150, 300), fill="black")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def upload(client, admin, data=None, consent="1"):
    return client.post("/api/drawings", headers=admin, content_type="multipart/form-data",
                       data={"file": (io.BytesIO(data or drawing_png()), "d.png"), "consent": consent})


def login(client) -> str:
    """Sign in with the password; returns the CSRF token. The cookie stays in the client."""
    res = client.post("/api/login", json={"password": PASSWORD})
    assert res.status_code == 200, res.get_json()
    return res.get_json()["csrf"]
