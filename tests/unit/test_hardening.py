"""Fixes after the security review: no unauthenticated HTTP 500, a stricter CSP, hardened units."""
import re
import subprocess
from pathlib import Path

import pytest

from hallway.app import create_app
from tests.conftest import ADMIN, LOOP, login, upload

PI = Path(__file__).resolve().parents[2] / "pi"


@pytest.fixture
def app_client(svc):
    return create_app(svc).test_client()


# ---- B1: hostile input must give a clean 4xx, never a 500 ----

HOSTILE_HEADERS = [
    "Bearer \u00e9", "Bearer \u2603\u2603\u2603", "Bearer " + "\u00e9" * 500, "Bearer \x80\xff",
]


@pytest.mark.parametrize("header", HOSTILE_HEADERS)
@pytest.mark.parametrize("path", ["/api/spend", "/api/drawings", "/loop/manifest.json"])
def test_non_ascii_bearer_is_401_not_500(app_client, header, path):
    res = app_client.get(path, headers={"Authorization": header})
    assert res.status_code == 401, (path, res.status_code)


@pytest.mark.parametrize("body", ['"just a string"', "[1, 2]", "42", "null", "true", "{bad json", '"\u00e9"'])
def test_login_with_a_non_object_body_is_a_clean_refusal(app_client, body):
    res = app_client.post("/api/login", data=body, content_type="application/json")
    assert res.status_code in (400, 401), res.status_code


@pytest.mark.parametrize("payload", [{"password": "\u00e9" * 40}, {"token": "\u2603" * 40}, {"token": "\ud800"},
                                     {"password": ["a"]}, {"password": {"a": 1}}, {"token": 5}])
def test_login_with_non_ascii_or_odd_fields_is_401(app_client, payload):
    res = app_client.post("/api/login", json=payload)
    assert res.status_code == 401, (payload, res.status_code)


def test_non_ascii_csrf_header_and_cookie_signature_are_refused_not_500(app_client, admin):
    login(app_client)
    res = app_client.post("/api/drawings/x/online", json={"ok": True}, headers={"X-CSRF-Token": "\u00e9" * 64})
    assert res.status_code == 403
    app_client.set_cookie("hh_session", "e30=.\u00e9\u00e9", domain="localhost")
    assert app_client.get("/api/session").status_code == 401


@pytest.mark.parametrize("body", ['"s"', "[1]", "7", "null"])
def test_authenticated_routes_survive_a_non_object_json_body(app_client, admin, body):
    d = upload(app_client, admin).get_json()["id"]
    for path in (f"/api/drawings/{d}/online", f"/api/drawings/{d}/publish", f"/api/drawings/{d}/generate"):
        res = app_client.post(path, data=body, content_type="application/json", headers=admin)
        assert res.status_code < 500, (path, res.status_code)
    res = app_client.post("/api/characters/nope/used", data=body, content_type="application/json", headers=admin)
    assert res.status_code == 404


# ---- B2: the CSP closes <base> and form hijacking ----

def test_csp_has_base_uri_and_form_action(app_client):
    for path in ("/", "/api/session", "/static/app.js"):
        csp = app_client.get(path).headers["Content-Security-Policy"]
        assert "base-uri 'none'" in csp and "form-action 'self'" in csp and "default-src 'self'" in csp, path


# ---- B3: unit files ----

def unit(name):
    return (PI / name).read_text()


def test_player_never_loads_the_token_file():
    text = unit("hh-player.service")
    assert not re.search(r"^EnvironmentFile=-?/etc/haunted-hallway\.env\b", text, re.M)
    assert "EnvironmentFile=-/etc/haunted-hallway-player.env" in text
    assert "LOOP_TOKEN" not in text.replace("loop token", "")


@pytest.mark.parametrize("name", ["hh-player.service", "hh-sync.service"])
def test_units_are_hardened(name):
    text = unit(name)
    for line in ("NoNewPrivileges=true", "ProtectSystem=strict", "ProtectHome=true", "PrivateTmp=true",
                 "ProtectKernelTunables=true", "ProtectKernelModules=true", "ProtectControlGroups=true",
                 "RestrictSUIDSGID=true", "LockPersonality=true", "CapabilityBoundingSet=",
                 "SystemCallArchitectures=native", "ReadWritePaths=/var/lib/haunted-hallway/loop"):
        assert re.search(rf"^{re.escape(line)}\s*$", text, re.M), (name, line)
    assert "RestrictAddressFamilies=AF_UNIX" in unit("hh-player.service") and "IPAddressDeny=any" in unit("hh-player.service")
    assert "RestrictAddressFamilies=AF_INET AF_INET6" in unit("hh-sync.service")


def test_systemd_accepts_the_units():
    """systemd-analyze reads the real directive names: a typo would be reported here."""
    exe = subprocess.run(["which", "systemd-analyze"], capture_output=True, text=True).stdout.strip()
    if not exe:
        pytest.skip("systemd-analyze not installed")
    res = subprocess.run([exe, "verify", *(str(PI / n) for n in ("hh-player.service", "hh-sync.service", "hh-sync.timer"))],
                         capture_output=True, text=True)
    complaints = [l for l in res.stderr.splitlines() if "not executable" not in l and l.strip()]
    assert not complaints, complaints
