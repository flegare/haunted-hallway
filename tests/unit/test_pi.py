"""The Pi side against a real HTTP server and a simulated player. No Pi, no mpv, no network beyond localhost."""
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from hallway.app import create_app
from tests.conftest import LOOP
from tests.unit.test_media_and_loop import make_clip

ROOT = Path(__file__).resolve().parents[2]
PI = ROOT / "pi"


def load_sync():
    spec = importlib.util.spec_from_file_location("hh_sync", PI / "sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync_mod = load_sync()


class Live:
    def __init__(self, app):
        self.server = make_server("127.0.0.1", 0, app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.thread.join(5)


@pytest.fixture
def live(svc):
    server = Live(create_app(svc))
    yield server
    server.stop()


def run_sync(url, loop_dir, token=LOOP):
    env = {**os.environ, "HH_SERVER": url, "LOOP_TOKEN": token, "HH_LOOP_DIR": str(loop_dir)}
    return subprocess.run(["python3", str(PI / "sync.py")], env=env, capture_output=True, text=True, timeout=60)


def publish(client, svc, admin):
    drawing_id, clip_id, _ = make_clip(client, svc, admin)
    client.post(f"/api/drawings/{drawing_id}/online", headers=admin, json={"ok": True})
    assert client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "public"}).status_code == 200
    return drawing_id, clip_id


def clips_in(path):
    return sorted(p.name for p in path.glob("*.mp4"))


def test_sync_downloads_verifies_and_is_quiet_the_second_time(client, svc, admin, live, tmp_path):
    _, clip_id = publish(client, svc, admin)
    loop = tmp_path / "loop"
    first = run_sync(live.url, loop)
    assert first.returncode == 0, first.stderr
    assert clips_in(loop) == [f"{clip_id}.mp4"]
    body = (loop / f"{clip_id}.mp4").read_bytes()
    assert body[4:8] == b"ftyp"
    assert hashlib.sha256(body).hexdigest() == client.get("/loop/manifest.json", headers={"Authorization": f"Bearer {LOOP}"}).get_json()["clips"][0]["sha256"]
    assert "fetched" in first.stdout
    second = run_sync(live.url, loop)
    assert second.returncode == 0 and second.stdout.strip() == ""
    assert not list(loop.glob(".*.part"))


def test_clip_leaving_the_loop_is_removed(client, svc, admin, live, tmp_path):
    drawing_id, _ = publish(client, svc, admin)
    loop = tmp_path / "loop"
    run_sync(live.url, loop)
    assert len(clips_in(loop)) == 1
    client.post(f"/api/drawings/{drawing_id}/unpublish", headers=admin)
    res = run_sync(live.url, loop)
    assert res.returncode == 0 and clips_in(loop) == []        # a valid, empty manifest does mean "nothing"


def test_unreachable_server_keeps_the_current_loop(client, svc, admin, live, tmp_path):
    publish(client, svc, admin)
    loop = tmp_path / "loop"
    run_sync(live.url, loop)
    before = clips_in(loop)
    live.stop()
    res = run_sync(live.url, loop)
    assert res.returncode == 1 and clips_in(loop) == before and before


def test_wrong_token_keeps_the_current_loop_and_leaks_nothing(client, svc, admin, live, tmp_path):
    publish(client, svc, admin)
    loop = tmp_path / "loop"
    run_sync(live.url, loop)
    before = clips_in(loop)
    res = run_sync(live.url, loop, token="x" * 24)
    assert res.returncode == 1 and clips_in(loop) == before
    assert "x" * 24 not in res.stdout + res.stderr


def test_missing_settings_exit_2(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("HH_SERVER", "LOOP_TOKEN")}
    res = subprocess.run(["python3", str(PI / "sync.py")], env=env, capture_output=True, text=True)
    assert res.returncode == 2


class Fake(BaseHTTPRequestHandler):
    routes = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        status, headers, body = self.routes.get(self.path, (404, {}, b""))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake():
    Fake.routes = {}
    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", Fake.routes
    server.shutdown()


def manifest(*clips):
    return (200, {"Content-Type": "application/json"}, json.dumps({"version": 1, "clips": list(clips)}).encode())


def entry(cid, body):
    return {"id": cid, "url": f"/loop/clip/{cid}", "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}


@pytest.mark.parametrize("bad_id", ["../../escape", "A" * 32, "a" * 31, "a" * 32 + "/x", "..", ""])
def test_hostile_manifest_ids_are_refused_and_nothing_is_written(fake, tmp_path, bad_id):
    url, routes = fake
    body = b"x" * 10
    e = entry("a" * 32, body)
    e["id"], e["url"] = bad_id, f"/loop/clip/{bad_id}"
    routes["/loop/manifest.json"] = manifest(e)
    routes[e["url"]] = (200, {}, body)          # the hostile server really serves the bytes
    loop = tmp_path / "loop"
    res = run_sync(url, loop)
    assert res.returncode == 1 and clips_in(loop) == []
    assert not (tmp_path / "escape.mp4").exists() and not list(tmp_path.glob("*.mp4"))


def test_a_clip_that_does_not_match_its_hash_is_never_installed(fake, tmp_path):
    url, routes = fake
    cid, good, evil = "a" * 32, b"good clip", b"evil clip"       # same length on purpose
    assert len(good) == len(evil)
    routes["/loop/manifest.json"] = manifest(entry(cid, good))
    routes[f"/loop/clip/{cid}"] = (200, {}, evil)
    loop = tmp_path / "loop"
    res = run_sync(url, loop)
    assert res.returncode == 1 and clips_in(loop) == [] and not list(loop.glob(".*"))


def test_a_clip_longer_than_announced_is_refused(fake, tmp_path):
    url, routes = fake
    cid = "b" * 32
    routes["/loop/manifest.json"] = manifest(entry(cid, b"short"))
    routes[f"/loop/clip/{cid}"] = (200, {}, b"short" + b"x" * 1000)
    res = run_sync(url, tmp_path / "loop")
    assert res.returncode == 1 and clips_in(tmp_path / "loop") == []


def test_redirects_are_not_followed_even_to_a_valid_server(fake, tmp_path):
    """If the redirect were followed the Pi would obey a different server and install its clip."""
    url, routes = fake
    other = HTTPServer(("127.0.0.1", 0), type("Other", (Fake,), {"routes": {}}))
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        cid, body = "c" * 32, b"clip from elsewhere"
        other.RequestHandlerClass.routes["/loop/manifest.json"] = manifest(entry(cid, body))
        other.RequestHandlerClass.routes[f"/loop/clip/{cid}"] = (200, {}, body)
        routes["/loop/manifest.json"] = (302, {"Location": f"http://127.0.0.1:{other.server_port}/loop/manifest.json"}, b"")
        routes[f"/loop/clip/{cid}"] = (200, {}, body)      # so a followed redirect WOULD end in an install
        loop = tmp_path / "loop"
        loop.mkdir()
        (loop / ("a" * 32 + ".mp4")).write_bytes(b"keep me")
        res = run_sync(url, loop)
        assert res.returncode == 1 and clips_in(loop) == ["a" * 32 + ".mp4"]
    finally:
        other.shutdown()


def test_a_clip_stream_is_cut_off_once_longer_than_announced(tmp_path):
    reads = []

    class Endless:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            reads.append(n)
            if len(reads) > 50:
                raise RuntimeError("kept reading an endless stream")
            return b"x" * n

    original = sync_mod.get
    sync_mod.get = lambda url, token: Endless()
    try:
        with pytest.raises(ValueError):
            sync_mod.download("http://x", "t", {"id": "a" * 32, "url": "/loop/clip/" + "a" * 32, "sha256": "0" * 64, "bytes": 10},
                              tmp_path / ("a" * 32 + ".mp4"))
    finally:
        sync_mod.get = original
    assert len(reads) <= 2 and not list(tmp_path.glob("*"))


def test_one_failed_download_removes_nothing(fake, tmp_path):
    url, routes = fake
    old, new = "d" * 32, "e" * 32
    routes["/loop/manifest.json"] = manifest(entry(new, b"new clip"))
    routes[f"/loop/clip/{new}"] = (500, {}, b"")
    loop = tmp_path / "loop"
    loop.mkdir()
    (loop / f"{old}.mp4").write_bytes(b"old clip")
    res = run_sync(url, loop)
    assert res.returncode == 1 and clips_in(loop) == [f"{old}.mp4"]


def test_garbage_manifest_changes_nothing(fake, tmp_path):
    url, routes = fake
    loop = tmp_path / "loop"
    loop.mkdir()
    (loop / ("f" * 32 + ".mp4")).write_bytes(b"keep")
    for body in (b"not json", b"{}", b'{"version":2,"clips":[]}', b'{"version":1,"clips":"x"}', b"[]"):
        routes["/loop/manifest.json"] = (200, {}, body)
        assert run_sync(url, loop).returncode == 1
        assert clips_in(loop) == ["f" * 32 + ".mp4"]


def seed_loop(tmp_path, n=2):
    loop = tmp_path / "loop"
    loop.mkdir()
    names = []
    for i in range(n):
        name = f"{i + 1:032x}.mp4"
        (loop / name).write_bytes(b"old clip")
        names.append(name)
    return loop, sorted(names)


def raw_manifest(**fields):
    return (200, {"Content-Type": "application/json"}, json.dumps({"version": 1, "clips": [], **fields}).encode())


def test_an_empty_manifest_without_the_flag_keeps_the_loop(fake, tmp_path):
    url, routes = fake
    loop, names = seed_loop(tmp_path)
    routes["/loop/manifest.json"] = raw_manifest()
    res = run_sync(url, loop)
    assert res.returncode == 0 and clips_in(loop) == names
    assert "warning" in res.stderr and "cleared" in res.stderr


@pytest.mark.parametrize("flag", ["true", 1, "yes", None, False, [], {"cleared": True}])
def test_a_flag_that_is_not_exactly_true_does_not_clear_the_loop(fake, tmp_path, flag):
    url, routes = fake
    loop, names = seed_loop(tmp_path)
    routes["/loop/manifest.json"] = raw_manifest(cleared=flag)
    assert run_sync(url, loop).returncode == 0 and clips_in(loop) == names


def test_an_empty_manifest_with_the_flag_empties_the_loop(fake, tmp_path):
    url, routes = fake
    loop, names = seed_loop(tmp_path)
    routes["/loop/manifest.json"] = raw_manifest(cleared=True)
    res = run_sync(url, loop)
    assert res.returncode == 0 and clips_in(loop) == []
    assert res.stderr == ""


def test_the_flag_does_not_stop_a_normal_update(fake, tmp_path):
    url, routes = fake
    loop, names = seed_loop(tmp_path)
    cid, body = "9" * 32, b"fresh clip"
    routes["/loop/manifest.json"] = manifest({**entry(cid, body)})
    routes[f"/loop/clip/{cid}"] = (200, {}, body)
    assert run_sync(url, loop).returncode == 0 and clips_in(loop) == [f"{cid}.mp4"]


def test_real_server_unpublish_empties_the_pi_loop_but_tampering_and_errors_do_not(client, svc, admin, live, tmp_path):
    import sqlite3
    drawing_id, _ = publish(client, svc, admin)
    loop = tmp_path / "loop"
    run_sync(live.url, loop)
    before = clips_in(loop)
    assert len(before) == 1
    # rows vanish behind the server's back: empty manifest, no flag -> the Pi keeps playing
    conn = sqlite3.connect(svc.settings.db_path)
    conn.execute("DELETE FROM publications")
    conn.commit()
    res = run_sync(live.url, loop)
    assert res.returncode == 0 and clips_in(loop) == before and "warning" in res.stderr
    # database broken: 503 -> the Pi keeps playing
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE publications")
    conn.commit()
    conn.close()
    res = run_sync(live.url, loop)
    assert res.returncode == 1 and clips_in(loop) == before


# ---- player (simulated: the "player" is echo) ----

def run_player(loop, extra=None, cmd="echo"):
    env = {**os.environ, "HH_LOOP_DIR": str(loop), "HH_PLAYER_CMD": cmd, "HH_ONCE": "1"}
    env.update(extra or {})
    return subprocess.run(["bash", str(PI / "player.sh")], env=env, capture_output=True, text=True, timeout=30)


def make_loop(tmp_path, n):
    loop = tmp_path / "loop"
    loop.mkdir()
    for i in range(n):
        (loop / f"{i:032x}.mp4").write_bytes(b"x")
    return loop


def test_player_passes_every_clip_and_the_orientation(tmp_path):
    loop = make_loop(tmp_path, 3)
    res = run_player(loop, {"ORIENTATION": "90", "MIRROR": "1"})
    assert res.returncode == 0
    for i in range(3):
        assert f"{i:032x}.mp4" in res.stdout
    assert "--video-rotate=90" in res.stdout and "--vf=hflip" in res.stdout and "--vo=drm" in res.stdout


def test_player_without_mirror_has_no_flip(tmp_path):
    res = run_player(make_loop(tmp_path, 1))
    assert "hflip" not in res.stdout and "--video-rotate=0" in res.stdout


def test_player_never_starts_a_pass_with_the_clip_that_ended_the_last(tmp_path):
    loop = make_loop(tmp_path, 3)
    previous_last = None
    for _ in range(40):
        out = run_player(loop).stdout.split("--")[-1].split()
        clips = [c for c in out if c.endswith(".mp4")]
        assert len(clips) == 3
        if previous_last is not None:
            assert clips[0] != previous_last
        previous_last = clips[-1]


def test_player_with_no_clips_shows_a_plain_dark_screen(tmp_path):
    loop = tmp_path / "loop"
    loop.mkdir()
    res = run_player(loop)
    assert res.returncode == 0 and "color=c=black" in res.stdout and ".mp4" not in res.stdout


def test_player_ignores_hidden_files_and_part_files(tmp_path):
    loop = make_loop(tmp_path, 1)
    (loop / (".x" * 1 + ".mp4")).write_bytes(b"x")
    (loop / ".abc.mp4.part").write_bytes(b"x")
    res = run_player(loop)
    assert res.stdout.count(".mp4") == 1


@pytest.mark.parametrize("env", [{"ORIENTATION": "45"}, {"ORIENTATION": "x"}, {"MIRROR": "2"}])
def test_player_rejects_bad_settings(tmp_path, env):
    assert run_player(make_loop(tmp_path, 1), env).returncode == 2


# ---- installer (test mode: everything under a prefix, no apt, no systemctl) ----

def run_install(prefix, extra=None, args=()):
    env = {**os.environ, "HH_PREFIX": str(prefix)}
    env.pop("HH_SERVER", None)
    env.pop("LOOP_TOKEN", None)
    env.update(extra or {})
    return subprocess.run(["bash", str(PI / "install.sh"), *args], env=env, capture_output=True, text=True,
                          timeout=60, stdin=subprocess.DEVNULL)


def test_installer_lays_out_files_with_private_env(tmp_path):
    token = "t" * 24
    res = run_install(tmp_path, {"HH_SERVER": "http://192.0.2.10:8080", "LOOP_TOKEN": token})
    assert res.returncode == 0, res.stderr
    assert token not in res.stdout + res.stderr
    env = tmp_path / "etc/haunted-hallway.env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert f"LOOP_TOKEN={token}" in env.read_text()
    for rel in ("opt/haunted-hallway/pi/sync.py", "opt/haunted-hallway/pi/player.sh"):
        assert os.access(tmp_path / rel, os.X_OK)
    for unit in ("hh-player.service", "hh-sync.service", "hh-sync.timer"):
        assert (tmp_path / "etc/systemd/system" / unit).is_file()
    assert (tmp_path / "var/lib/haunted-hallway/loop").is_dir()


def test_installer_is_idempotent_and_keeps_the_env(tmp_path):
    cfg = {"HH_SERVER": "http://192.0.2.10:8080", "LOOP_TOKEN": "t" * 24}
    run_install(tmp_path, cfg)
    env = tmp_path / "etc/haunted-hallway.env"
    env.write_text(env.read_text() + "ORIENTATION=90\n")
    again = run_install(tmp_path, {})
    assert again.returncode == 0 and "keeping existing" in again.stdout
    assert "ORIENTATION=90" in env.read_text()


@pytest.mark.parametrize("server,token", [("ftp://x", "t" * 24), ("192.0.2.1:8080", "t" * 24), ("http://192.0.2.1:8080", "short")])
def test_installer_refuses_bad_input(tmp_path, server, token):
    res = run_install(tmp_path, {"HH_SERVER": server, "LOOP_TOKEN": token})
    assert res.returncode != 0 and not (tmp_path / "etc/haunted-hallway.env").exists()


def test_dry_run_changes_nothing(tmp_path):
    res = run_install(tmp_path, {}, args=("--dry-run",))
    assert res.returncode == 0 and "==>" in res.stdout
    assert not any(tmp_path.rglob("*"))


def test_unit_files_say_what_they_should():
    def read(name):
        return (PI / name).read_text()
    player, sync_svc, timer = read("hh-player.service"), read("hh-sync.service"), read("hh-sync.timer")
    assert "ExecStart=/opt/haunted-hallway/pi/player.sh" in player and "Restart=always" in player
    assert "SupplementaryGroups=video render" in player and "User=hallway" in player
    assert "Type=oneshot" in sync_svc and "EnvironmentFile=/etc/haunted-hallway.env" in sync_svc
    assert re.search(r"OnUnitActiveSec=60\b", timer) and "WantedBy=timers.target" in timer
    for text in (player, sync_svc):
        assert "NoNewPrivileges=true" in text and "User=root" not in text


def test_installer_keeps_picture_settings_apart_from_the_token(tmp_path):
    token = "t" * 24
    assert run_install(tmp_path, {"HH_SERVER": "http://192.0.2.10:8080", "LOOP_TOKEN": token}).returncode == 0
    player_env = tmp_path / "etc/haunted-hallway-player.env"
    assert player_env.read_text() == "ORIENTATION=0\nMIRROR=0\n"
    assert token not in player_env.read_text() and "LOOP_TOKEN" not in player_env.read_text()
    assert "ORIENTATION" not in (tmp_path / "etc/haunted-hallway.env").read_text()
    player_env.write_text("ORIENTATION=90\nMIRROR=1\n")
    assert run_install(tmp_path).returncode == 0                       # run again: kept as the person set it
    assert player_env.read_text() == "ORIENTATION=90\nMIRROR=1\n"
