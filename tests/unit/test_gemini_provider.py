"""The Gemini provider, written and tested WITHOUT a key, against a local fake server that counts requests."""
import base64
import io
import json
import re
from pathlib import Path

import pytest
from PIL import Image

from hallway import db, pricing
from hallway.app import create_app
from hallway.config import ConfigError, Settings
from hallway.providers.base import NotSent, ProviderError
from hallway.providers.gemini import OFFICIAL_BASE_URL, GeminiProvider
from hallway.services import Services
from hallway.spend import BudgetExceeded, Reservation, SpendGuard, _MINT
from tests.conftest import ADMIN, PASSWORD_HASH, drawing_png, ffmpeg_bin, upload
from tests.fake_gemini import FakeGemini, png_bytes

KEY = "AIza" + "Zq3" * 11 + "x"            # 37 characters, shaped like a real key, worthless
MODELS = dict(text_model=pricing.DEFAULT_MODELS["text"], image_model=pricing.DEFAULT_MODELS["image"],
              video_model=pricing.DEFAULT_MODELS["video"])


def res(kind="text", micro=1):
    return Reservation(1, "test", micro, kind, _MINT)


@pytest.fixture
def fake():
    server = FakeGemini(KEY)
    yield server
    server.stop()


@pytest.fixture
def provider(fake):
    sleeps = []
    p = GeminiProvider(KEY, base_url=fake.url, sleep=sleeps.append, **MODELS)
    p.sleeps = sleeps
    return p


@pytest.fixture
def drawing(tmp_path):
    path = tmp_path / "drawing.jpg"
    Image.new("RGB", (600, 400), "white").save(path, "JPEG")
    return path


def decode_input(req):
    """The parts the SDK sent, whatever wrapping it uses."""
    parts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") in ("image", "text") and ("data" in node or "text" in node):
                parts.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(req["body"]["input"])
    return parts


def assert_clean_request(fake):
    """The key travels in one header and nowhere else."""
    for r in fake.requests:
        assert r["headers"].get("x-goog-api-key") == KEY
        assert KEY not in json.dumps(r["body"]) and KEY not in r["path"]


# ---- no reservation, no network ----

@pytest.mark.parametrize("bad", [None, object(), "x", 5])
def test_no_valid_reservation_means_no_request(provider, fake, drawing, bad):
    with pytest.raises(TypeError):
        provider.count_and_describe(drawing, bad)
    with pytest.raises(TypeError):
        provider.extract_character(drawing, {"box": [0, 0, 50, 50]}, bad)
    with pytest.raises(TypeError):
        provider.make_video([png_bytes()], "p", bad)
    assert fake.requests == []


# ---- describe ----

def test_describe_sends_the_image_and_a_schema_and_maps_boxes_to_pixels(provider, fake, drawing):
    items = provider.count_and_describe(drawing, res())
    assert items == [{"description": "a green ghost", "box": [30, 40, 270, 360]},
                     {"description": "a purple monster", "box": [330, 40, 570, 360]}]
    (req,) = fake.reqs("text")
    assert req["path"] in ("/v1/interactions", "/v1beta/interactions")
    assert req["body"]["model"] == pricing.DEFAULT_MODELS["text"]
    rf = req["body"]["response_format"]
    assert rf["type"] == "text" and rf["mime_type"] == "application/json" and "figures" in json.dumps(rf["schema"])
    kinds = [p["type"] for p in decode_input(req)]
    assert kinds == ["image", "text"]
    sent = decode_input(req)[0]
    assert Image.open(io.BytesIO(base64.b64decode(sent["data"]))).size == (600, 400)
    assert_clean_request(fake)
    assert provider.last_measured_micro is None                 # no price list in the code: nothing is computed


def test_describe_cleans_text_skips_bad_boxes_and_trims(provider, fake, drawing):
    fake.figures = [
        {"description": "<script>alert(1)</script> a ghost http://evil.example/x `rm`", "box": [0, 0, 500, 500]},
        {"description": "tiny", "box": [0, 0, 5, 5]},
        {"description": "no box"},
        {"description": "bad box", "box": ["a", 1, 2, 3]},
        {"description": "flipped", "box": [900, 900, 100, 100]},
        {"description": "x" * 400, "box": [0, 500, 400, 1000]},
    ]
    items = provider.count_and_describe(drawing, res())
    assert len(items) == 2                                   # max_figures defaults to 2
    first = items[0]["description"]
    assert "<" not in first and ">" not in first and "http" not in first and "`" not in first and "ghost" in first
    assert all(len(i["description"]) <= 120 for i in items)
    assert all(0 <= i["box"][0] < i["box"][2] <= 600 and 0 <= i["box"][1] < i["box"][3] <= 400 for i in items)


def test_describe_with_no_figure_falls_back_to_the_whole_drawing(provider, fake, drawing):
    fake.figures = []
    assert provider.count_and_describe(drawing, res()) == [{"description": "the whole drawing", "box": [0, 0, 600, 400]}]


@pytest.mark.parametrize("raw", ["not json", "{}", '{"figures": "x"}', "[]", ""])
def test_describe_with_unusable_json_fails_after_exactly_one_request(provider, fake, drawing, raw):
    fake.text_override = raw
    with pytest.raises(ProviderError):
        provider.count_and_describe(drawing, res())
    assert fake.count["text"] == 1


# ---- extract ----

def test_extract_sends_only_the_crop_and_returns_a_png(provider, fake, drawing):
    png = provider.extract_character(drawing, {"description": "a green ghost", "box": [30, 40, 270, 360]}, res("image"))
    assert Image.open(io.BytesIO(png)).format == "PNG"
    (req,) = fake.reqs("image")
    assert req["body"]["model"] == pricing.DEFAULT_MODELS["image"]
    sent = [p for p in decode_input(req) if p["type"] == "image"][0]
    size = Image.open(io.BytesIO(base64.b64decode(sent["data"]))).size
    assert size[0] < 600 and size[1] < 400                   # a crop, not the whole drawing
    assert "a green ghost" in [p for p in decode_input(req) if p["type"] == "text"][0]["text"]
    assert provider.last_measured_micro is None                 # no price list in the code: nothing is computed
    assert_clean_request(fake)


def test_extract_takes_the_last_image_and_skips_thoughts(provider, fake, drawing):
    fake.thought_step, fake.image_blocks = True, 2
    png = provider.extract_character(drawing, {"description": "x", "box": [0, 0, 200, 200]}, res("image"))
    assert Image.open(io.BytesIO(png)).getpixel((5, 5)) == (255, 255, 255)   # white = the last block, not the black decoy


def test_extract_refuses_unusable_answers_without_retrying(provider, fake, drawing):
    fake.image_data = b"not an image at all"
    with pytest.raises(ProviderError):
        provider.extract_character(drawing, {"description": "x", "box": [0, 0, 200, 200]}, res("image"))
    assert fake.count["image"] == 1


def test_extract_with_a_broken_drawing_sends_nothing(provider, fake, tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"garbage")
    with pytest.raises(NotSent):
        provider.extract_character(bad, {"description": "x", "box": [0, 0, 10, 10]}, res("image"))
    with pytest.raises(NotSent):
        provider.count_and_describe(bad, res())
    with pytest.raises(NotSent):
        provider.count_and_describe(tmp_path / "missing.jpg", res())
    assert fake.requests == []


# ---- video ----

def test_video_request_shape(provider, fake):
    clip = provider.make_video([png_bytes("red"), png_bytes("blue")], "A friendly ghost waves.", res("video"))
    assert clip[4:8] == b"ftyp"
    (req,) = fake.reqs("video")
    assert req["body"]["model"] == pricing.DEFAULT_MODELS["video"]
    assert req["body"]["response_format"] == {"type": "video", "aspect_ratio": "9:16", "duration": "8s",
                                              "resolution": "720p", "delivery": "uri"}
    parts = decode_input(req)
    assert [p["type"] for p in parts] == ["image", "image", "text"] and parts[-1]["text"] == "A friendly ghost waves."
    assert provider.last_measured_micro is None                 # no price list in the code: nothing is computed
    assert_clean_request(fake)


def test_video_waits_for_the_file_then_downloads_once(provider, fake):
    fake.file_states = ["PROCESSING", "PROCESSING", "ACTIVE"]
    provider.make_video([png_bytes()], "p", res("video"))
    assert fake.count["video"] == 1 and fake.count["file_get"] == 3 and fake.count["download"] == 1
    assert provider.sleeps == [5, 5]


def test_video_that_failed_is_never_downloaded(provider, fake):
    fake.file_states = ["PROCESSING", "FAILED"]
    with pytest.raises(ProviderError, match="failed"):
        provider.make_video([png_bytes()], "p", res("video"))
    assert fake.count["download"] == 0 and fake.count["video"] == 1


def test_video_that_never_becomes_ready_stops_waiting(provider, fake):
    fake.file_states = ["PROCESSING"]
    with pytest.raises(ProviderError, match="in time"):
        provider.make_video([png_bytes()], "p", res("video"))
    assert fake.count["download"] == 0 and sum(provider.sleeps) == 300 and fake.count["video"] == 1


def test_inline_video_data_is_accepted_too(provider, fake):
    fake.delivery = "inline"
    assert provider.make_video([png_bytes()], "p", res("video"))[4:8] == b"ftyp"
    assert fake.count["file_get"] == 0 and fake.count["download"] == 0


def test_a_video_that_is_not_an_mp4_is_refused(provider, fake):
    fake.video_bytes = b"<html>not a video</html>" * 10
    with pytest.raises(ProviderError, match="MP4"):
        provider.make_video([png_bytes()], "p", res("video"))


@pytest.mark.parametrize("refs", [[], [png_bytes()] * 3, [b"junk"], [png_bytes(), b"junk"]])
def test_bad_reference_images_are_refused_before_any_request(provider, fake, refs):
    with pytest.raises(NotSent):
        provider.make_video(refs, "p", res("video"))
    assert fake.requests == []


# ---- exactly one request per paid call ----

CALLS = {
    "text": lambda p, d: p.count_and_describe(d, res()),
    "image": lambda p, d: p.extract_character(d, {"description": "x", "box": [0, 0, 200, 200]}, res("image")),
    "video": lambda p, d: p.make_video([png_bytes()], "p", res("video")),
}


@pytest.mark.parametrize("status", [500, 503, 429, 400, 403])
@pytest.mark.parametrize("kind", ["text", "image", "video"])
def test_an_error_answer_is_exactly_one_request(provider, fake, drawing, kind, status):
    """The SDK alone would send 5 requests on a 429 or a 5xx; each could be a paid call."""
    fake.errors[kind] = [status] * 10
    with pytest.raises(ProviderError) as info:
        CALLS[kind](provider, drawing)
    assert fake.count[kind] == 1
    assert f"HTTP {status}" in str(info.value)
    assert provider.sleeps == []


def test_a_timeout_is_one_request_and_an_error(fake, drawing):
    fake.stall["text"] = 3
    p = GeminiProvider(KEY, base_url=fake.url, timeout_s=1, **MODELS)
    with pytest.raises(ProviderError):
        p.count_and_describe(drawing, res())
    assert fake.count["text"] == 1


def test_bounded_retries_only_for_429_and_5xx_and_without_a_new_reservation(fake, drawing):
    sleeps = []
    p = GeminiProvider(KEY, base_url=fake.url, retries=1, sleep=sleeps.append, **MODELS)
    fake.errors["text"] = [429]
    assert p.count_and_describe(drawing, res())
    assert fake.count["text"] == 2 and sleeps == [2]
    fake.count["text"], fake.errors["text"] = 0, [400] * 5
    with pytest.raises(ProviderError):
        p.count_and_describe(drawing, res())
    assert fake.count["text"] == 1                           # a 400 is never retried
    fake.count["text"], fake.errors["text"] = 0, [500] * 5
    with pytest.raises(ProviderError):
        p.count_and_describe(drawing, res())
    assert fake.count["text"] == 2                           # 1 try + 1 retry, then it stops


def test_two_retries_is_at_most_three_requests(fake, drawing):
    p = GeminiProvider(KEY, base_url=fake.url, retries=2, sleep=lambda s: None, **MODELS)
    fake.errors["text"] = [503] * 10
    with pytest.raises(ProviderError):
        p.count_and_describe(drawing, res())
    assert fake.count["text"] == 3


# ---- the key ----

def test_the_key_never_appears_in_errors_or_representations(fake, drawing):
    fake.echo_key = True
    fake.errors["text"] = [500] * 5
    p = GeminiProvider(KEY, base_url=fake.url, **MODELS)
    with pytest.raises(ProviderError) as info:
        p.count_and_describe(drawing, res())
    text = str(info.value) + repr(info.value)
    assert KEY not in text and "***" in text                 # the server repeated it, we masked it
    assert KEY not in repr(p) and KEY not in str(p)
    assert info.value.__cause__ is None                      # no chained SDK exception carrying request headers
    settings = Settings.load({"ADMIN_PASSWORD_HASH": PASSWORD_HASH, "PROVIDER": "gemini", "GEMINI_API_KEY": KEY,
                              "DATA_DIR": str(drawing.parent / "d"), "FFMPEG_BIN": ffmpeg_bin()})
    assert KEY not in repr(settings) and PASSWORD_HASH not in repr(settings)


def test_the_official_address_and_service_are_fixed_in_code(monkeypatch):
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "http://evil.invalid")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "env-key-that-must-not-be-used-0000")
    monkeypatch.setenv("GEMINI_API_KEY", "other-env-key-that-must-not-win-00")
    p = GeminiProvider(KEY, **MODELS)
    api = p._client._api_client
    assert api._http_options.base_url == OFFICIAL_BASE_URL
    assert not api.vertexai and api.api_key == KEY


def test_a_short_or_missing_key_is_refused_at_construction():
    for bad in ("", "short", None):
        with pytest.raises(ValueError):
            GeminiProvider(bad, **MODELS)


# ---- the provider inside the pipeline: reservation first, release only when nothing was sent ----

def gemini_services(tmp_path, fake, **env):
    cfg = {"ADMIN_TOKEN": ADMIN, "ADMIN_PASSWORD_HASH": PASSWORD_HASH, "DATA_DIR": str(tmp_path / "data"),
           "FFMPEG_BIN": ffmpeg_bin(), "PROVIDER": "gemini", "GEMINI_API_KEY": KEY, **env}
    settings = Settings.load(cfg)
    db.init(settings.db_path)
    guard = SpendGuard(settings.db_path, settings.cap_micro, settings.daily_micro)
    provider = GeminiProvider(KEY, base_url=fake.url, sleep=lambda s: None, video_seconds=settings.video_seconds,
                              retries=settings.gemini_retries, max_figures=settings.max_figures, **MODELS)
    svc = Services(settings, provider, guard)
    return svc, create_app(svc).test_client(), {"Authorization": f"Bearer {ADMIN}"}


def run_job(client, svc, admin):
    drawing_id = upload(client, admin).get_json()["id"]
    assert client.post(f"/api/drawings/{drawing_id}/generate", headers=admin).status_code == 202
    svc.process_all()
    return client.get("/api/drawings", headers=admin).get_json()["drawings"][0]


def ledger(svc):
    return [dict(r) for r in svc.connect().execute("SELECT kind, micro, state, measured_micro FROM spend ORDER BY id")]


def test_full_chain_with_the_gemini_provider_one_clip_four_paid_calls(tmp_path, fake):
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1")
    d = run_job(client, svc, admin)
    assert d["job"]["state"] == "done", d["job"]
    assert (fake.count["text"], fake.count["image"], fake.count["video"]) == (1, 2, 1)     # 4 paid calls for 1 clip
    assert len(d["characters"]) == 2 and len(d["clips"]) == 1
    rows = ledger(svc)
    assert [r["kind"] for r in rows] == ["text", "image", "image", "video"]
    assert all(r["state"] == "settled" for r in rows)
    assert [r["micro"] for r in rows] == [5_000, 40_000, 40_000, 566_032]                  # reserved estimates
    assert [r["measured_micro"] for r in rows] == [None] * 4                               # nothing computed from a price list
    assert sum(r["micro"] for r in rows) == 651_032 <= 1_000_000                           # default estimates, under a 1 USD cap
    clip = client.get(f"/media/clip/{d['clips'][0]['id']}", headers=admin)
    assert clip.status_code == 200 and clip.data[4:8] == b"ftyp"
    assert_clean_request(fake)


def test_a_cap_that_cannot_pay_for_the_next_call_sends_nothing(tmp_path, fake):
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="0.003")           # below even the first call (0.005)
    d = run_job(client, svc, admin)
    assert d["job"]["state"] == "error" and d["job"]["error"].startswith("budget:")
    assert fake.requests == []                                                           # NOTHING left the machine


def test_the_video_is_never_requested_when_its_reservation_is_refused(tmp_path, fake):
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="0.2")             # pays text + 2 images, not the video
    d = run_job(client, svc, admin)
    assert d["job"]["error"].startswith("budget:") and d["clips"] == []
    assert (fake.count["text"], fake.count["image"], fake.count["video"]) == (1, 2, 0)


def test_a_1_usd_cap_cannot_pay_for_a_15_second_clip(tmp_path, fake):
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1", VIDEO_SECONDS="15")
    d = run_job(client, svc, admin)
    assert d["job"]["error"].startswith("budget:") and fake.count["video"] == 0


def test_a_failed_video_stays_booked_is_not_retried_and_leaks_nothing(tmp_path, fake):
    fake.errors["video"] = [500] * 5
    fake.echo_key = True
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1")
    d = run_job(client, svc, admin)
    assert d["job"]["state"] == "error" and "HTTP 500" in d["job"]["error"] and KEY not in d["job"]["error"]
    assert fake.count["video"] == 1                                                      # no retry
    video_row = ledger(svc)[-1]
    assert video_row["kind"] == "video" and video_row["state"] == "reserved" and video_row["micro"] == 566_032   # still booked
    assert svc.guard.status()["spent_micro"] == 5_000 + 80_000 + 566_032
    # and the drawing can be retried by a person, who pays a new reservation
    assert client.post(f"/api/drawings/{d['id']}/generate", headers=admin).status_code == 202


def test_a_drawing_that_cannot_be_read_is_released_and_sends_nothing(tmp_path, fake):
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1")
    drawing_id = upload(client, admin).get_json()["id"]
    original = next((svc.settings.media_dir / "originals").glob("*.jpg"))
    original.write_bytes(b"corrupted on disk")
    client.post(f"/api/drawings/{drawing_id}/generate", headers=admin)
    svc.process_all()
    d = client.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert d["job"]["state"] == "error" and "nothing was sent" in d["job"]["error"]
    assert fake.requests == []
    assert [r["state"] for r in ledger(svc)] == ["released"]
    assert svc.guard.status()["spent_micro"] == 0


def test_the_estimate_endpoint_reports_the_real_prices(tmp_path, fake):
    svc, client, admin = gemini_services(tmp_path, fake)
    est = client.get("/api/estimate", headers=admin).get_json()
    assert est["free"] is False and est["provider"] == "gemini" and est["max_figures"] == 2
    assert est["max_micro"] == 651_032 and est["video_micro"] == 566_032


# ---- only this module touches the network ----

def test_only_the_gemini_module_can_reach_the_network():
    root = Path(__file__).resolve().parents[2] / "hallway"
    pattern = re.compile(r"^\s*(import|from)\s+(google|urllib\.request|http\.client|requests|httpx|socket|aiohttp|ssl)\b", re.M)
    offenders = [str(p.relative_to(root)) for p in root.rglob("*.py") if p.name != "gemini.py" and pattern.search(p.read_text())]
    assert offenders == []
    assert pattern.search((root / "providers" / "gemini.py").read_text())


# ---- configuration ----

BASE = {"ADMIN_PASSWORD_HASH": PASSWORD_HASH}


def load(tmp_path, **env):
    return Settings.load({**BASE, "DATA_DIR": str(tmp_path / "d"), **env})


def test_gemini_needs_a_plausible_key_and_never_echoes_it(tmp_path):
    for bad in ("", "short", "has a space in it 0123456789"):
        with pytest.raises(ConfigError) as info:
            load(tmp_path, PROVIDER="gemini", GEMINI_API_KEY=bad)
        assert bad not in str(info.value) or bad == ""
    assert load(tmp_path, PROVIDER="gemini", GEMINI_API_KEY=KEY).gemini_key == KEY
    assert load(tmp_path).provider == "mock"                  # the mock needs no key


def test_models_and_numbers_are_checked(tmp_path):
    for env in ({"GEMINI_VIDEO_MODEL": "bad name!"}, {"GEMINI_TEXT_MODEL": "x"}, {"VIDEO_SECONDS": "2"}, {"VIDEO_SECONDS": "16"},
                {"VIDEO_SECONDS": "abc"}, {"MAX_FIGURES": "0"}, {"MAX_FIGURES": "5"}, {"GEMINI_RETRIES": "3"}, {"GEMINI_RETRIES": "-1"}):
        with pytest.raises(ConfigError):
            load(tmp_path, **env)
    s = load(tmp_path)
    assert (s.video_seconds, s.max_figures, s.gemini_retries) == (8, 2, 0)
    assert (s.text_model, s.image_model, s.video_model) == tuple(pricing.DEFAULT_MODELS[k] for k in ("text", "image", "video"))


def test_providers_build_makes_the_gemini_provider_from_settings(tmp_path):
    from hallway import providers
    p = providers.build(load(tmp_path, PROVIDER="gemini", GEMINI_API_KEY=KEY, VIDEO_SECONDS="6", GEMINI_RETRIES="1"))
    assert isinstance(p, GeminiProvider) and p.video_seconds == 6 and p.retries == 1 and p.max_figures == 2


def test_missing_google_package_stops_startup_with_a_clear_message(tmp_path, monkeypatch, capsys):
    import sys
    from hallway import __main__ as entry
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("FFMPEG_BIN", ffmpeg_bin())
    monkeypatch.setenv("PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setitem(sys.modules, "hallway.providers.gemini", None)       # makes "import it" fail
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "google-genai" in err and KEY not in err


def test_readme_is_honest_about_the_real_provider():
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    assert "has not been run against the real service" in text
    assert "estimate" in text and "check google's current pricing" in text.lower()
    assert "leaves your machine" in text and "Google" in text


# ---- prices ----

def test_default_estimates_are_placeholders_that_fit_a_one_dollar_trial():
    two = pricing.estimate("text") + 2 * pricing.estimate("image") + pricing.estimate("video")
    four = pricing.estimate("text") + 4 * pricing.estimate("image") + pricing.estimate("video")
    assert two == 651_032 and four == 731_032 < 1_000_000
    assert pricing.estimate("video", 15) + pricing.estimate("text") > 1_000_000
    assert not hasattr(pricing, "RATES") and not hasattr(pricing, "measured_micro")      # no frozen tariff in the code


def settings_with(tmp_path, **env):
    return Settings.load({"ADMIN_PASSWORD_HASH": PASSWORD_HASH, "DATA_DIR": str(tmp_path / "d"),
                          "FFMPEG_BIN": ffmpeg_bin(), **env})


def test_estimates_are_editable_in_config(tmp_path):
    s = settings_with(tmp_path, ESTIMATE_TEXT_USD="0.01", ESTIMATE_IMAGE_USD="0.1", ESTIMATE_VIDEO_SECOND_USD="0.05",
                      VIDEO_SECONDS="10")
    assert (pricing.estimate("text", 10, s.estimates), pricing.estimate("image", 10, s.estimates)) == (10_000, 100_000)
    assert pricing.estimate("video", 10, s.estimates) == 500_000 + pricing.VIDEO_MARGIN_MICRO
    assert settings_with(tmp_path).estimates == pricing.DEFAULT_ESTIMATES


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "abc", "11", "1e9"])
def test_a_bad_estimate_stops_the_start_instead_of_falling_back(tmp_path, bad):
    with pytest.raises(ConfigError) as info:
        settings_with(tmp_path, ESTIMATE_VIDEO_SECOND_USD=bad)
    assert "ESTIMATE_VIDEO_SECOND_USD" in str(info.value)


def test_the_cap_reserves_the_configured_estimate(tmp_path, fake):
    """Changing the estimate changes what is reserved: at 0.2 USD per second the video no longer fits 1 USD."""
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1", ESTIMATE_VIDEO_SECOND_USD="0.2")
    d = run_job(client, svc, admin)
    assert d["job"]["error"].startswith("budget:") and fake.count["video"] == 0


def test_the_readme_and_env_example_state_no_google_price():
    root = Path(__file__).resolve().parents[2]
    for name in ("README.md", ".env.example", "hallway/pricing.py"):
        text = (root / name).read_text()
        assert not re.search(r"\$\s?\d|\b0\.\d+\s*(USD|dollars?)|per (second|image|clip|million)|price list|promotion", text, re.I), name


# ---- a refused request is not billed: its reservation is given back ----

def test_a_503_on_the_first_call_releases_the_reservation(tmp_path, fake):
    """What the trial run showed: 503 on the text call left 5,000 micro 'reserved' for a call that did nothing."""
    fake.errors["text"] = [503] * 5
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1")
    d = run_job(client, svc, admin)
    assert d["job"]["state"] == "error" and "HTTP 503" in d["job"]["error"]
    assert fake.count["text"] == 1                                    # still no retry
    assert [(r["kind"], r["state"]) for r in ledger(svc)] == [("text", "released")]
    assert svc.guard.status()["spent_micro"] == 0


@pytest.mark.parametrize("status", [500, 502, 504])
def test_an_ambiguous_server_error_stays_booked(tmp_path, fake, status):
    fake.errors["text"] = [status] * 5
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1")
    run_job(client, svc, admin)
    assert [(r["kind"], r["state"]) for r in ledger(svc)] == [("text", "reserved")]
    assert svc.guard.status()["spent_micro"] == 5_000


def test_a_timeout_stays_booked(fake, drawing):
    fake.stall["text"] = 3
    p = GeminiProvider(KEY, base_url=fake.url, timeout_s=1, **MODELS)
    with pytest.raises(ProviderError) as info:
        p.count_and_describe(drawing, res())
    assert not isinstance(info.value, NotSent)                         # the pipeline keeps the booking


def test_retry_after_an_unclear_attempt_keeps_the_booking(fake, drawing):
    """500 (may have run) then 503: the first attempt may have been billed, so no release."""
    p = GeminiProvider(KEY, base_url=fake.url, retries=1, sleep=lambda s: None, **MODELS)
    fake.errors["text"] = [500, 503]
    with pytest.raises(ProviderError) as info:
        p.count_and_describe(drawing, res())
    assert not isinstance(info.value, NotSent)
    fake.count["text"], fake.errors["text"] = 0, [503, 503]
    with pytest.raises(NotSent):                                       # two clean refusals: nothing owed
        p.count_and_describe(drawing, res())
    assert fake.count["text"] == 2


def test_with_a_1_usd_cap_the_second_clip_is_refused_at_the_video_call(tmp_path, fake):
    """README: 'with a cap of 1 USD you get one clip; a second attempt may pay for the first calls
    (about 0.09 USD) and then be refused at the video call'."""
    svc, client, admin = gemini_services(tmp_path, fake, SPEND_CAP_USD="1")
    first = run_job(client, svc, admin)
    assert first["job"]["state"] == "done" and fake.count["video"] == 1
    assert client.post(f"/api/drawings/{first['id']}/generate", headers=admin).status_code == 202
    svc.process_all()
    second = client.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert second["job"]["state"] == "error" and second["job"]["error"].startswith("budget:")
    assert fake.count["video"] == 1                                    # the video was never requested again
    assert (fake.count["text"], fake.count["image"]) == (2, 4)         # the early calls were paid
    assert svc.guard.status()["spent_micro"] == 651_032 + 85_000


# --- the estimate floor and the text model setting -----------------------------------------------------

@pytest.mark.parametrize("key,default", [("ESTIMATE_TEXT_USD", pricing.DEFAULT_ESTIMATES.text_micro),
                                         ("ESTIMATE_IMAGE_USD", pricing.DEFAULT_ESTIMATES.image_micro),
                                         ("ESTIMATE_VIDEO_SECOND_USD", pricing.DEFAULT_ESTIMATES.video_second_micro)])
def test_an_estimate_below_a_tenth_of_the_builtin_one_stops_the_start(tmp_path, key, default):
    from hallway.config import estimate_floor_micro
    floor = estimate_floor_micro(default)
    assert floor == default // 10
    ok = settings_with(tmp_path, **{key: f"{floor / 1_000_000:.6f}"})          # exactly the floor: accepted
    assert ok.estimates != pricing.DEFAULT_ESTIMATES
    for too_low in (f"{(floor - 1) / 1_000_000:.6f}", "0.000001", "0.0000001"):  # just under, and near zero
        with pytest.raises(ConfigError) as info:
            settings_with(tmp_path, **{key: too_low})
        assert key in str(info.value) and "between" in str(info.value)
    settings_with(tmp_path, **{key: "10"})                                      # the top end is still accepted
    with pytest.raises(ConfigError):
        settings_with(tmp_path, **{key: "10.01"})


def test_the_text_model_is_a_setting_with_an_unchanged_default(tmp_path):
    assert settings_with(tmp_path).text_model == "gemini-3.8-flash"
    env_example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    assert "GEMINI_TEXT_MODEL=gemini-3.8-flash" in env_example and "gemini-3.5-flash" in env_example


def test_the_text_model_comes_from_the_process_environment_and_wins_over_dotenv(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("GEMINI_TEXT_MODEL=gemini-from-dotenv-1\n")
    base = {"ADMIN_PASSWORD_HASH": PASSWORD_HASH, "DATA_DIR": str(tmp_path / "d"), "FFMPEG_BIN": ffmpeg_bin()}
    assert Settings.load(base, dotenv).text_model == "gemini-from-dotenv-1"
    s = Settings.load({**base, "GEMINI_TEXT_MODEL": "gemini-3.5-flash"}, dotenv)
    assert s.text_model == "gemini-3.5-flash"
    from hallway import providers
    p = providers.build(Settings.load({**base, "PROVIDER": "gemini", "GEMINI_API_KEY": KEY,
                                       "GEMINI_TEXT_MODEL": "gemini-3.5-flash"}))
    assert p.text_model == "gemini-3.5-flash"
