import threading

import pytest

from hallway import __main__ as entry
from hallway.app import create_app
from hallway.config import ConfigError, Settings, require_ffmpeg
from tests.conftest import ADMIN, PASSWORD_HASH, ffmpeg_bin, make_services, make_settings, upload


def finish_job(client, svc, admin, drawing_id, prompt=None):
    body = {} if prompt is None else {"prompt": prompt}
    res = client.post(f"/api/drawings/{drawing_id}/generate", headers=admin, json=body)
    assert res.status_code == 202, res.get_json()
    svc.process_all()
    return res.get_json()["job_id"]


# ---- 4: one job at a time per drawing ----

def test_six_simultaneous_generate_requests_make_one_job(svc, admin):
    client = create_app(svc).test_client()
    drawing_id = upload(client, admin).get_json()["id"]
    codes, barrier = [], threading.Barrier(6)

    def click():
        c = create_app(svc).test_client()
        barrier.wait()
        codes.append(c.post(f"/api/drawings/{drawing_id}/generate", headers=admin).status_code)

    threads = [threading.Thread(target=click) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [202, 409, 409, 409, 409, 409], codes
    jobs = svc.connect().execute("SELECT COUNT(*) FROM jobs WHERE drawing_id=?", (drawing_id,)).fetchone()[0]
    assert jobs == 1
    svc.process_all()
    assert svc.provider.calls["video"] == 1                    # one clip, not six


def test_a_new_job_is_accepted_once_the_previous_one_ended(client, svc, admin):
    drawing_id = upload(client, admin).get_json()["id"]
    finish_job(client, svc, admin, drawing_id)
    assert client.post(f"/api/drawings/{drawing_id}/generate", headers=admin).status_code == 202
    assert client.post(f"/api/drawings/{drawing_id}/generate", headers=admin).status_code == 409


def test_a_failed_job_does_not_block_a_retry(tmp_path, admin):
    svc = make_services(make_settings(tmp_path, SPEND_CAP_USD="0.5", MOCK_PRICES="1"))
    client = create_app(svc).test_client()
    drawing_id = upload(client, admin).get_json()["id"]
    client.post(f"/api/drawings/{drawing_id}/generate", headers=admin)
    svc.process_all()
    assert client.get("/api/drawings", headers=admin).get_json()["drawings"][0]["job"]["state"] == "error"
    assert client.post(f"/api/drawings/{drawing_id}/generate", headers=admin).status_code == 202


# ---- 5: versions and prompt ----

def test_each_generation_adds_a_version_of_characters_and_clip(client, svc, admin):
    drawing_id = upload(client, admin).get_json()["id"]
    finish_job(client, svc, admin, drawing_id)
    finish_job(client, svc, admin, drawing_id)
    d = client.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert [c["version"] for c in d["clips"]] == [1, 2] or len(d["clips"]) == 1     # cap may stop the second (priced mock)
    free = make_services(make_settings(svc.settings.data_dir.parent / "free"))
    c2 = create_app(free).test_client()
    did = upload(c2, admin).get_json()["id"]
    finish_job(c2, free, admin, did)
    finish_job(c2, free, admin, did)
    d = c2.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert [c["version"] for c in d["clips"]] == [1, 2]
    assert sorted({c["version"] for c in d["characters"]}) == [1, 2]
    assert len([c for c in d["characters"] if c["version"] == 1]) == 2


def test_publishing_one_version_replaces_the_other_and_withdraw_works(tmp_path, admin):
    free = make_services(make_settings(tmp_path))
    c = create_app(free).test_client()
    did = upload(c, admin).get_json()["id"]
    finish_job(c, free, admin, did)
    finish_job(c, free, admin, did)
    clips = c.get("/api/drawings", headers=admin).get_json()["drawings"][0]["clips"]
    v1, v2 = clips[0]["id"], clips[1]["id"]
    assert c.post(f"/api/drawings/{did}/publish", headers=admin, json={"channel": "preview", "clip_id": v1}).status_code == 200
    assert c.post(f"/api/drawings/{did}/publish", headers=admin, json={"channel": "preview", "clip_id": v2}).status_code == 200
    d = c.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert d["publication"] == {"clip_id": v2, "channel": "preview"}
    c.post(f"/api/drawings/{did}/unpublish", headers=admin)
    assert c.get("/api/drawings", headers=admin).get_json()["drawings"][0]["publication"] is None


def test_the_edited_prompt_reaches_the_provider_and_is_kept(tmp_path, admin):
    free = make_services(make_settings(tmp_path))
    seen = []
    original = free.provider.make_video
    free.provider.make_video = lambda refs, prompt, res: (seen.append(prompt), original(refs, prompt, res))[1]
    c = create_app(free).test_client()
    did = upload(c, admin).get_json()["id"]
    finish_job(c, free, admin, did, prompt="  A friendly ghost waves from the end of the hall.  ")
    assert seen == ["A friendly ghost waves from the end of the hall."]
    assert c.get("/api/drawings", headers=admin).get_json()["drawings"][0]["clips"][0]["prompt"] == seen[0]
    finish_job(c, free, admin, did)                            # no prompt: the default one
    assert "hallway" in seen[1]


@pytest.mark.parametrize("prompt", ["x" * 601, 5, ["a"], "bad\x00byte", "bell\x07"])
def test_bad_prompts_are_refused_and_nothing_is_queued(client, svc, admin, prompt):
    did = upload(client, admin).get_json()["id"]
    assert client.post(f"/api/drawings/{did}/generate", headers=admin, json={"prompt": prompt}).status_code == 400
    assert svc.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# ---- 3: the fake provider does not use the real cap ----

def test_the_free_mock_never_uses_up_the_cap(tmp_path, admin):
    free = make_services(make_settings(tmp_path, SPEND_CAP_USD="0.9"))           # same cap as the priced tests
    c = create_app(free).test_client()
    did = upload(c, admin).get_json()["id"]
    for _ in range(5):                                                           # six priced clips would break 5 USD
        finish_job(c, free, admin, did)
    d = c.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert len(d["clips"]) == 5 and all(x["cost_micro"] == 0 for x in d["clips"])
    assert c.get("/api/spend", headers=admin).get_json()["spent_micro"] == 0
    est = c.get("/api/estimate", headers=admin).get_json()
    assert est["free"] is True and est["max_micro"] == 0 and est["provider"] == "mock"


def test_priced_mock_still_spends_and_says_so(svc, admin):
    c = create_app(svc).test_client()
    est = c.get("/api/estimate", headers=admin).get_json()
    assert est["free"] is False and est["max_micro"] > 0


# ---- 7: ffmpeg is required ----

def test_missing_ffmpeg_stops_startup_with_a_clear_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("FFMPEG_BIN", str(tmp_path / "no-such-ffmpeg"))
    assert entry.main() == 2
    err = capsys.readouterr().err
    assert "ffmpeg" in err and "install" in err.lower() and "FFMPEG_BIN" in err


def test_no_ffmpeg_anywhere_stops_startup(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))                  # nothing on the path
    assert entry.main() == 2 and "ffmpeg" in capsys.readouterr().err


def test_a_file_that_is_not_ffmpeg_is_refused(tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    settings = make_settings(tmp_path, FFMPEG_BIN=str(fake))
    with pytest.raises(ConfigError, match="not a working ffmpeg"):
        require_ffmpeg(settings)
    require_ffmpeg(make_settings(tmp_path))                    # the real one is accepted
    assert ffmpeg_bin()


def test_a_program_that_succeeds_but_is_not_ffmpeg_is_refused(tmp_path):
    import shutil
    for real in (shutil.which("true"), "/bin/true"):
        assert real, "no 'true' program on this machine"
        with pytest.raises(ConfigError, match="not a working ffmpeg"):
            require_ffmpeg(make_settings(tmp_path, FFMPEG_BIN=real))
    liar = tmp_path / "liar"
    liar.write_text("#!/bin/sh\necho 'something else 1.0'\nexit 0\n")
    liar.chmod(0o755)
    with pytest.raises(ConfigError, match="not a working ffmpeg"):
        require_ffmpeg(make_settings(tmp_path, FFMPEG_BIN=str(liar)))


def test_main_refuses_to_start_with_true_as_ffmpeg(tmp_path, monkeypatch, capsys):
    import shutil
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("FFMPEG_BIN", shutil.which("true"))
    assert entry.main() == 2
    assert "not a working ffmpeg" in capsys.readouterr().err


# ---- 2: the example config matches the code ----

def test_env_example_is_consistent_with_config():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    example = (root / ".env.example").read_text()
    source = (root / "hallway" / "config.py").read_text()
    keys = [l.split("=")[0] for l in example.splitlines() if l and not l.startswith("#") and "=" in l]
    assert "ADMIN_PASSWORD_HASH" in keys
    built_by_fstring = {f"GEMINI_{k}_MODEL" for k in ("TEXT", "IMAGE", "VIDEO")}   # config.py builds these names
    assert 'f"GEMINI_{kind.upper()}_MODEL"' in source
    for key in keys:
        if key in built_by_fstring:
            continue
        assert f'"{key}"' in source, f"{key} is in .env.example but config.py never reads it"
    assert built_by_fstring <= set(keys)
    assert "Required: ADMIN_TOKEN" not in example
    assert example.index("ADMIN_PASSWORD_HASH") < example.index("ADMIN_TOKEN=")
    # a copy of the example with a real hash must load
    env = {k: v for k, v in (l.split("=", 1) for l in example.splitlines() if l and not l.startswith("#") and "=" in l)}
    env["ADMIN_PASSWORD_HASH"] = PASSWORD_HASH
    env["DATA_DIR"] = "/tmp/hh-example-check"
    env["LOOP_TOKEN"] = "l" * 24
    env = {k: v for k, v in env.items() if v != ""}
    Settings.load(env)


def test_readme_names_ffmpeg_before_the_quick_start():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "README.md").read_text()
    assert text.index("ffmpeg") < text.index("## Quick start")
    assert "required" in text[: text.index("## Quick start")].lower()
    assert "costs nothing" in text and "MOCK_PRICES" in text
