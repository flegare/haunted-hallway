"""Thin chain at zero cost: drawing -> characters -> clip -> published -> manifest."""
import hashlib

from tests.conftest import upload


def run_chain(client, svc, admin):
    drawing_id = upload(client, admin).get_json()["id"]
    assert client.post(f"/api/drawings/{drawing_id}/generate", headers=admin).status_code == 202
    assert svc.process_all() == 1
    return drawing_id


def test_full_chain(client, svc, admin, loop):
    drawing_id = run_chain(client, svc, admin)
    drawing = client.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert drawing["job"]["state"] == "done", drawing["job"]
    assert len(drawing["characters"]) == 2
    assert len(drawing["clips"]) == 1
    clip_id = drawing["clips"][0]["id"]

    # Public needs the second box (OK online); preview does not.
    assert client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "public"}).status_code == 409
    assert client.get("/loop/manifest.json", headers=loop).get_json()["clips"] == []
    assert client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "preview"}).status_code == 200
    assert client.get("/loop/manifest.json", headers=loop).get_json()["clips"] == []

    assert client.post(f"/api/drawings/{drawing_id}/online", headers=admin, json={"ok": True}).status_code == 200
    assert client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "public"}).status_code == 200

    manifest = client.get("/loop/manifest.json", headers=loop).get_json()
    assert [c["id"] for c in manifest["clips"]] == [clip_id]
    clip = client.get(manifest["clips"][0]["url"], headers=loop)
    assert clip.status_code == 200
    body = clip.data
    assert body[4:8] == b"ftyp"  # a real MP4 container
    assert hashlib.sha256(body).hexdigest() == manifest["clips"][0]["sha256"]

    # The ledger recorded the reservations: 1 describe + 2 images + 1 video, all estimates.
    spend = client.get("/api/spend", headers=admin).get_json()
    assert spend["spent_micro"] == svc.price("text") + 2 * svc.price("image") + svc.price("video")
    assert spend["spent_micro"] == 651_032       # 5,000 + 2 x 40,000 + 566,032 (8 s at 69,504 per second + 10,000 margin)
    assert spend["estimated"] is True

    # Unpublish removes it from the loop.
    client.post(f"/api/drawings/{drawing_id}/unpublish", headers=admin)
    assert client.get("/loop/manifest.json", headers=loop).get_json()["clips"] == []

    # The cap (0.90 USD, set by the svc fixture) leaves no room for a second clip:
    # the second run must stop on the budget and make nothing. Without the cap it
    # would make a second clip and this test would fail.
    assert client.post(f"/api/drawings/{drawing_id}/generate", headers=admin).status_code == 202
    svc.process_all()
    after = client.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert after["job"]["state"] == "error" and after["job"]["error"].startswith("budget:")
    assert len(after["clips"]) == 1
    assert svc.provider.calls["video"] == 1
    assert client.get("/api/spend", headers=admin).get_json()["spent_micro"] <= 900_000


def test_spend_cap_stops_the_chain(tmp_path, admin):
    """With a cap below one video, the video is never made. Fails if the cap is removed."""
    from tests.conftest import make_services, make_settings
    from hallway.app import create_app
    svc = make_services(make_settings(tmp_path, SPEND_CAP_USD="0.5", MOCK_PRICES="1"))
    client = create_app(svc).test_client()
    run_chain(client, svc, admin)
    drawing = client.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert drawing["job"]["state"] == "error"
    assert drawing["job"]["error"].startswith("budget:")
    assert drawing["clips"] == []
    assert svc.provider.calls["video"] == 0
    assert svc.guard.status()["spent_micro"] <= 500_000
