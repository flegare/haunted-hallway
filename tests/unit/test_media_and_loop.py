import os

import pytest

from tests.conftest import drawing_png, upload


def make_clip(client, svc, admin):
    drawing_id = upload(client, admin).get_json()["id"]
    client.post(f"/api/drawings/{drawing_id}/generate", headers=admin)
    svc.process_all()
    d = client.get("/api/drawings", headers=admin).get_json()["drawings"][0]
    assert d["clips"], d["job"]
    return drawing_id, d["clips"][0]["id"], d["characters"][0]["id"]


@pytest.mark.parametrize("kind", ["original", "character", "clip"])
@pytest.mark.parametrize("ident", ["../x", "%2e%2e/x", "..%2f..%2fetc%2fpasswd", "....//....//etc/passwd",
                                   "%252e%252e%252f", "a" * 32, "", "x%00y", "..\\..\\x"])
def test_traversal_ids_never_serve_anything(client, admin, kind, ident):
    res = client.get(f"/media/{kind}/{ident}", headers=admin)
    assert res.status_code in (404, 308, 400), (kind, ident, res.status_code)
    assert res.status_code != 200


def test_ids_are_looked_up_per_kind_and_case_sensitively(client, svc, admin):
    drawing_id, clip_id, char_id = make_clip(client, svc, admin)
    assert client.get(f"/media/clip/{clip_id}", headers=admin).status_code == 200
    assert client.get(f"/media/clip/{clip_id.upper()}", headers=admin).status_code == 404
    assert client.get(f"/media/clip/{drawing_id}", headers=admin).status_code == 404       # a drawing id is not a clip id
    assert client.get(f"/media/character/{clip_id}", headers=admin).status_code == 404
    assert client.get(f"/media/original/{char_id}", headers=admin).status_code == 404


def test_symlinked_media_is_never_followed(client, svc, admin, loop, tmp_path):
    drawing_id, clip_id, _ = make_clip(client, svc, admin)
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("TOP-SECRET")
    row = svc.connect().execute("SELECT file_id FROM clips WHERE id=?", (clip_id,)).fetchone()
    target = svc.media_path("clips", row["file_id"], "mp4")
    target.unlink()
    os.symlink(secret, target)
    client.post(f"/api/drawings/{drawing_id}/online", headers=admin, json={"ok": True})
    # Publishing needs the DB row only; the file guard must still stop the leak.
    client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "public"})
    for url, headers in ((f"/media/clip/{clip_id}", admin), (f"/loop/clip/{clip_id}", loop)):
        res = client.get(url, headers=headers)
        assert res.status_code == 404 and b"TOP-SECRET" not in res.data, url
    manifest = client.get("/loop/manifest.json", headers=loop)
    assert manifest.status_code == 200 and b"TOP-SECRET" not in manifest.data
    assert manifest.get_json()["clips"] == []


def test_manifest_hides_preview_and_originals(client, svc, admin, loop):
    drawing_id, clip_id, char_id = make_clip(client, svc, admin)
    client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "preview"})
    assert client.get("/loop/manifest.json", headers=loop).get_json()["clips"] == []
    assert client.get(f"/loop/clip/{clip_id}", headers=loop).status_code == 404            # preview is not servable to the loop
    for url in (f"/loop/original/{drawing_id}", f"/loop/character/{char_id}", f"/media/original/{drawing_id}"):
        assert client.get(url, headers=loop).status_code in (401, 404), url
    body = client.get("/loop/manifest.json", headers=loop).data
    assert drawing_id.encode() not in body and char_id.encode() not in body


def test_public_publish_is_refused_by_the_server_without_the_second_box(client, svc, admin, loop):
    drawing_id, clip_id, _ = make_clip(client, svc, admin)
    res = client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "public", "clip_id": clip_id})
    assert res.status_code == 409
    assert client.get("/loop/manifest.json", headers=loop).get_json()["clips"] == []


def test_withdrawing_ok_online_pulls_the_clip_from_the_public_loop(client, svc, admin, loop):
    drawing_id, clip_id, _ = make_clip(client, svc, admin)
    client.post(f"/api/drawings/{drawing_id}/online", headers=admin, json={"ok": True})
    client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "public"})
    assert len(client.get("/loop/manifest.json", headers=loop).get_json()["clips"]) == 1
    client.post(f"/api/drawings/{drawing_id}/online", headers=admin, json={"ok": False})
    assert client.get("/loop/manifest.json", headers=loop).get_json()["clips"] == []


def test_cannot_publish_a_clip_of_another_drawing(client, svc, admin):
    drawing_id, clip_id, _ = make_clip(client, svc, admin)
    other = upload(client, admin).get_json()["id"]
    res = client.post(f"/api/drawings/{other}/publish", headers=admin, json={"channel": "preview", "clip_id": clip_id})
    assert res.status_code == 409


def test_delete_removes_rows_and_files_but_not_the_ledger(client, svc, admin):
    drawing_id, clip_id, _ = make_clip(client, svc, admin)
    spent = client.get("/api/spend", headers=admin).get_json()["spent_micro"]
    assert any(svc.settings.media_dir.rglob("*.mp4"))
    assert client.delete(f"/api/drawings/{drawing_id}", headers=admin).status_code == 200
    assert not [p for p in svc.settings.media_dir.rglob("*") if p.is_file()]
    assert client.get("/api/drawings", headers=admin).get_json()["drawings"] == []
    assert client.get("/api/spend", headers=admin).get_json()["spent_micro"] == spent


def test_delete_is_refused_while_a_job_is_queued(client, svc, admin):
    drawing_id = upload(client, admin).get_json()["id"]
    client.post(f"/api/drawings/{drawing_id}/generate", headers=admin)
    assert client.delete(f"/api/drawings/{drawing_id}", headers=admin).status_code == 409


def test_character_used_toggle_and_estimate(client, svc, admin):
    _, _, char_id = make_clip(client, svc, admin)
    assert client.post(f"/api/characters/{char_id}/used", headers=admin, json={"used": False}).get_json() == {"used": False}
    assert client.post("/api/characters/nope/used", headers=admin, json={"used": True}).status_code == 404
    est = client.get("/api/estimate", headers=admin).get_json()
    assert est["max_figures"] == 2
    assert est["max_micro"] == est["text_micro"] + est["max_figures"] * est["image_micro"] + est["video_micro"]
    assert est["estimated"] is True


def test_console_page_and_assets_load(client):
    page = client.get("/")
    assert page.status_code == 200 and b"Haunted Hallway" in page.data
    for name in ("app.js", "table-logic.js", "console.css"):
        assert client.get(f"/static/{name}").status_code == 200, name
    assert client.get("/static/../config.py").status_code == 404
    assert client.get("/static/%2e%2e/config.py").status_code == 404


def test_symlink_to_a_file_in_the_same_folder_is_not_followed(client, svc, admin):
    """The folder check alone passes this one; only the symlink check stops it."""
    _, clip_id, _ = make_clip(client, svc, admin)
    conn = svc.connect()
    file_id = conn.execute("SELECT file_id FROM clips WHERE id=?", (clip_id,)).fetchone()["file_id"]
    victim = svc.media_path("clips", file_id, "mp4")
    neighbour = svc.media_path("clips", "f" * 32, "mp4")
    neighbour.write_bytes(b"NEIGHBOUR-CLIP")
    victim.unlink()
    os.symlink(neighbour, victim)
    res = client.get(f"/media/clip/{clip_id}", headers=admin)
    assert res.status_code == 404 and b"NEIGHBOUR-CLIP" not in res.data


# ---- the "cleared" flag: an empty loop only means "play nothing" after the administrator emptied it ----

def loop_json(client, loop):
    res = client.get("/loop/manifest.json", headers=loop)
    return res.status_code, res.get_json()


def publish_public(client, admin, drawing_id):
    client.post(f"/api/drawings/{drawing_id}/online", headers=admin, json={"ok": True})
    assert client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "public"}).status_code == 200


def test_a_server_that_never_published_does_not_say_cleared(client, loop):
    assert loop_json(client, loop) == (200, {"version": 1, "clips": []})


def test_cleared_is_sent_after_each_way_the_admin_empties_the_loop(client, svc, admin, loop):
    drawing_id, clip_id, _ = make_clip(client, svc, admin)
    publish_public(client, admin, drawing_id)
    assert "cleared" not in loop_json(client, loop)[1]
    # 1. unpublish
    client.post(f"/api/drawings/{drawing_id}/unpublish", headers=admin)
    assert loop_json(client, loop)[1] == {"version": 1, "clips": [], "cleared": True}
    # 2. publishing a public clip again drops the flag
    publish_public(client, admin, drawing_id)
    status, body = loop_json(client, loop)
    assert len(body["clips"]) == 1 and "cleared" not in body
    # 3. withdrawing "OK online"
    client.post(f"/api/drawings/{drawing_id}/online", headers=admin, json={"ok": False})
    assert loop_json(client, loop)[1].get("cleared") is True
    # 4. switching the last public clip to preview
    publish_public(client, admin, drawing_id)
    client.post(f"/api/drawings/{drawing_id}/publish", headers=admin, json={"channel": "preview"})
    assert loop_json(client, loop)[1].get("cleared") is True
    # 5. deleting the drawing (a data deletion)
    publish_public(client, admin, drawing_id)
    assert "cleared" not in loop_json(client, loop)[1]
    client.delete(f"/api/drawings/{drawing_id}", headers=admin)
    assert loop_json(client, loop)[1].get("cleared") is True


def test_the_flag_never_appears_next_to_clips(tmp_path, admin, loop):
    from hallway.app import create_app
    from tests.conftest import make_services, make_settings
    free = make_services(make_settings(tmp_path))                  # the free mock: no cap in the way of two clips
    client = create_app(free).test_client()
    first = make_clip_again(client, free, admin)
    publish_public(client, admin, first)
    client.post(f"/api/drawings/{first}/unpublish", headers=admin)
    assert loop_json(client, loop)[1].get("cleared") is True
    second = make_clip_again(client, free, admin)
    publish_public(client, admin, second)
    body = loop_json(client, loop)[1]
    assert len(body["clips"]) == 1 and "cleared" not in body


def make_clip_again(client, svc, admin):
    did = upload(client, admin).get_json()["id"]
    client.post(f"/api/drawings/{did}/generate", headers=admin)
    svc.process_all()
    return did


def test_a_database_error_never_produces_an_empty_or_cleared_manifest(client, svc, admin, loop):
    import sqlite3
    drawing_id, _, _ = make_clip(client, svc, admin)
    publish_public(client, admin, drawing_id)
    conn = sqlite3.connect(svc.settings.db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE publications")
    conn.commit()
    conn.close()
    res = client.get("/loop/manifest.json", headers=loop)
    assert res.status_code == 503
    assert b"cleared" not in res.data and b'"clips"' not in res.data


def test_rows_deleted_behind_the_servers_back_do_not_say_cleared(client, svc, admin, loop):
    import sqlite3
    drawing_id, _, _ = make_clip(client, svc, admin)
    publish_public(client, admin, drawing_id)
    conn = sqlite3.connect(svc.settings.db_path)
    conn.execute("DELETE FROM publications")
    conn.commit()
    conn.close()
    assert loop_json(client, loop) == (200, {"version": 1, "clips": []})        # empty, but NOT cleared: the Pi will ignore it
