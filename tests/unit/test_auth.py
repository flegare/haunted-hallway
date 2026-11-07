import base64
import json
import time

import pytest

from hallway import passwd
from hallway.app import create_app
from hallway.config import ConfigError, Settings
from tests.conftest import ADMIN, LOOP, PASSWORD, PASSWORD_HASH, login, make_services, make_settings, upload

PUBLIC_ROUTES = {"/", "/api/login", "/static/<path:filename>"}


def admin_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.rule in PUBLIC_ROUTES or rule.rule.startswith("/loop/"):
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            yield rule.rule, method


def fill(rule: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "x", rule)


def test_every_admin_route_refuses_anonymous(svc):
    app = create_app(svc)
    client = app.test_client()
    seen = list(admin_rules(app))
    assert len(seen) >= 12, seen  # the enumeration really found the API
    for rule, method in seen:
        res = client.open(fill(rule), method=method)
        assert res.status_code in (401, 403), (method, rule, res.status_code)


def test_loop_token_cannot_open_admin_routes(svc):
    app = create_app(svc)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {LOOP}"}
    for rule, method in admin_rules(app):
        res = client.open(fill(rule), method=method, headers=headers)
        assert res.status_code in (401, 403), (method, rule, res.status_code)


def test_admin_token_cannot_open_loop_routes(client, admin):
    assert client.get("/loop/manifest.json", headers=admin).status_code == 401
    assert client.get("/loop/manifest.json").status_code == 401
    assert client.get("/loop/clip/x").status_code == 401


def test_post_without_csrf_is_refused_and_with_csrf_works(client):
    csrf = login(client)
    assert client.get("/api/spend").status_code == 200           # cookie alone is enough to read
    assert client.post("/api/drawings/x/online", json={"ok": True}).status_code == 403
    assert client.post("/api/drawings/x/online", json={"ok": True},
                       headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/drawings/x/online", json={"ok": True},
                       headers={"X-CSRF-Token": csrf}).status_code == 404   # passed auth, no such drawing


def test_csrf_token_of_another_session_is_refused(svc):
    a, b = create_app(svc).test_client(), create_app(svc).test_client()
    login(a)
    csrf_b = login(b)
    assert a.post("/api/drawings/x/online", json={"ok": True}, headers={"X-CSRF-Token": csrf_b}).status_code == 403


def test_forged_and_expired_cookies_are_refused(svc):
    client = create_app(svc).test_client()
    client.set_cookie("hh_session", "e30.deadbeef")
    assert client.get("/api/spend").status_code == 401
    from hallway.app import _sign
    payload = base64.urlsafe_b64encode(json.dumps({"sid": "s", "exp": int(time.time()) - 5}).encode())
    client.set_cookie("hh_session", payload.decode() + "." + _sign(svc.settings.secret, payload))
    assert client.get("/api/spend").status_code == 401
    wrong_key = payload.decode() + "." + _sign(b"k" * 32, payload)
    client.set_cookie("hh_session", wrong_key)
    assert client.get("/api/spend").status_code == 401


def test_wrong_password_and_lockout(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_login_needs_a_credential(client):
    assert client.post("/api/login", json={}).status_code == 401
    assert client.post("/api/login", json={"password": ""}).status_code == 401
    assert client.post("/api/login", data="not json").status_code == 401


def test_hash_only_config_does_not_accept_empty_bearer(tmp_path):
    svc = make_services(make_settings(tmp_path, ADMIN_TOKEN=""))
    client = create_app(svc).test_client()
    for value in ("Bearer ", "Bearer", f"Bearer {PASSWORD_HASH}", f"Bearer {PASSWORD}"):
        assert client.get("/api/spend", headers={"Authorization": value}).status_code == 401, value
    assert client.post("/api/login", json={"token": ""}).status_code == 401


def test_cookie_flags(client):
    res = client.post("/api/login", json={"password": PASSWORD})
    cookie = res.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_start_refuses_without_credential(tmp_path):
    env = {"DATA_DIR": str(tmp_path / "d")}
    with pytest.raises(ConfigError):
        Settings.load(env)
    with pytest.raises(ConfigError):
        Settings.load({**env, "ADMIN_TOKEN": "short"})
    with pytest.raises(ConfigError):
        Settings.load({**env, "ADMIN_PASSWORD_HASH": "scrypt$1$1$1$00$00"})
    with pytest.raises(ConfigError):
        Settings.load({**env, "ADMIN_PASSWORD_HASH": "plain-text-password"})
    with pytest.raises(ConfigError):
        Settings.load({**env, "ADMIN_TOKEN": ADMIN, "LOOP_TOKEN": ADMIN})
    Settings.load({**env, "ADMIN_PASSWORD_HASH": PASSWORD_HASH})  # and this one is fine


def test_main_exits_nonzero_without_credential(tmp_path, monkeypatch, capsys):
    from hallway import __main__ as entry
    monkeypatch.chdir(tmp_path)
    for name in ("ADMIN_TOKEN", "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    assert entry.main() == 2
    assert "refusing to start" in capsys.readouterr().err


def test_scrypt_roundtrip_and_salt():
    h1, h2 = passwd.hash_password("abc", n=2 ** 10), passwd.hash_password("abc", n=2 ** 10)
    assert h1 != h2
    assert passwd.verify_password("abc", h1) and not passwd.verify_password("abd", h1)
    assert not passwd.verify_password("abc", "scrypt$1024$8$1$zz$zz")


def test_security_headers(client, admin):
    res = client.get("/api/spend", headers=admin)
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Cache-Control"] == "no-store"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]


# ---- GET /api/session: the CSRF token for a reloaded page, cookie only ----

def test_session_without_cookie_is_401_even_with_a_header_token(client, admin, loop):
    assert client.get("/api/session").status_code == 401
    assert client.get("/api/session", headers=admin).status_code == 401      # a header token is not a session
    assert client.get("/api/session", headers=loop).status_code == 401
    res = client.get("/api/session")
    assert b"csrf" not in res.data


def test_session_with_expired_forged_or_wrong_key_cookie_is_401(svc):
    from hallway.app import _sign
    client = create_app(svc).test_client()
    payload = base64.urlsafe_b64encode(json.dumps({"sid": "s", "exp": int(time.time()) - 5}).encode())
    client.set_cookie("hh_session", payload.decode() + "." + _sign(svc.settings.secret, payload))
    assert client.get("/api/session").status_code == 401                       # expired
    client.set_cookie("hh_session", "e30.deadbeef")
    assert client.get("/api/session").status_code == 401                       # forged
    fresh = base64.urlsafe_b64encode(json.dumps({"sid": "s", "exp": int(time.time()) + 999}).encode())
    client.set_cookie("hh_session", fresh.decode() + "." + _sign(b"k" * 32, fresh))
    assert client.get("/api/session").status_code == 401                       # signed with another key


def test_session_gives_the_same_token_as_login_and_it_works(svc):
    a = create_app(svc).test_client()
    csrf_login = login(a)
    res = a.get("/api/session")
    assert res.status_code == 200 and res.get_json() == {"csrf": csrf_login}
    assert res.headers["Cache-Control"] == "no-store"
    assert a.post("/api/drawings/x/online", json={"ok": True},
                  headers={"X-CSRF-Token": res.get_json()["csrf"]}).status_code == 404   # past auth, no such drawing
    other = create_app(svc).test_client()
    csrf_other = login(other)
    assert csrf_other != csrf_login                                            # one token per session
    assert a.post("/api/drawings/x/online", json={"ok": True}, headers={"X-CSRF-Token": csrf_other}).status_code == 403


def test_session_answers_carry_no_permissive_cors_headers(client):
    login(client)
    origin = {"Origin": "https://evil.example"}
    for res in (client.get("/api/session", headers=origin),
                client.options("/api/session", headers={**origin, "Access-Control-Request-Method": "GET"}),
                client.get("/api/session")):
        lowered = {k.lower() for k in res.headers.keys()}
        assert not any(k.startswith("access-control-") for k in lowered), dict(res.headers)


def test_post_still_needs_the_token_after_a_session_call(client):
    login(client)
    assert client.get("/api/session").status_code == 200
    assert client.post("/api/drawings/x/online", json={"ok": True}).status_code == 403      # cookie alone is not enough
    assert client.delete("/api/drawings/x").status_code == 403
