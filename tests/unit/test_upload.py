import io
import os

from PIL import Image

from tests.conftest import drawing_png, upload


def stored_files(svc):
    return [p for p in (svc.settings.media_dir).rglob("*") if p.is_file()]


def test_consent_and_file_required(client, admin, svc):
    assert upload(client, admin, consent="0").status_code == 400
    assert client.post("/api/drawings", headers=admin, data={"consent": "1"}).status_code == 400
    assert stored_files(svc) == []


def test_svg_is_refused(client, admin, svc):
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    assert upload(client, admin, svg).status_code == 400
    assert stored_files(svc) == []


def test_html_renamed_as_image_is_refused(client, admin, svc):
    assert upload(client, admin, b"<html><script>alert(1)</script></html>").status_code == 400
    assert stored_files(svc) == []


def test_heic_is_refused_with_a_clear_message(client, admin, svc):
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
    res = upload(client, admin, heic)
    assert res.status_code == 400
    assert "JPEG" in res.get_json()["error"] or "readable" in res.get_json()["error"]
    assert stored_files(svc) == []


def test_polyglot_jpeg_html_is_reencoded(client, admin, svc):
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), "red").save(buf, "JPEG")
    polyglot = buf.getvalue() + b"<script>alert('x')</script>"
    res = upload(client, admin, polyglot)
    assert res.status_code == 201
    drawing_id = res.get_json()["id"]
    served = client.get(f"/media/original/{drawing_id}", headers=admin)
    assert served.mimetype == "image/jpeg"
    assert b"<script" not in served.data
    assert all(b"<script" not in p.read_bytes() for p in stored_files(svc))


def test_decompression_bomb_is_refused_before_decoding(client, admin, svc):
    buf = io.BytesIO()
    Image.new("1", (20000, 20000), 1).save(buf, "PNG")     # 400 Mpx, tiny on disk
    assert len(buf.getvalue()) < 10 * 1024 * 1024
    res = upload(client, admin, buf.getvalue())
    assert res.status_code == 400
    assert stored_files(svc) == []


def test_upload_limit_is_enforced_one_byte_over(client, admin, svc):
    over = os.urandom(10 * 1024 * 1024 + 1)
    assert upload(client, admin, over).status_code == 413
    assert stored_files(svc) == []


def test_exif_gps_and_author_are_removed(client, admin, svc):
    img = Image.new("RGB", (80, 80), "blue")
    exif = Image.Exif()
    exif[0x013B] = "SECRET-ARTIST-NAME"                     # Artist
    gps = exif.get_ifd(0x8825)
    gps[1], gps[2], gps[3], gps[4] = "N", (45.0, 30.0, 12.0), "W", (73.0, 35.0, 6.0)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    sent = buf.getvalue()
    assert b"SECRET-ARTIST-NAME" in sent                    # the input really carries the metadata
    assert Image.open(io.BytesIO(sent)).getexif().get_ifd(0x8825)
    res = upload(client, admin, sent)
    assert res.status_code == 201
    out = client.get(f"/media/original/{res.get_json()['id']}", headers=admin).data
    assert b"SECRET-ARTIST-NAME" not in out
    reread = Image.open(io.BytesIO(out))
    assert len(reread.getexif()) == 0 and not reread.getexif().get_ifd(0x8825)


def test_large_image_is_scaled_down(client, admin):
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), "green").save(buf, "PNG")
    res = upload(client, admin, buf.getvalue())
    out = client.get(f"/media/original/{res.get_json()['id']}", headers=admin).data
    assert max(Image.open(io.BytesIO(out)).size) <= 1600
