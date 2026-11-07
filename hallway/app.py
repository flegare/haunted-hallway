"""HTTP API: admin console routes, media by database id, and the read-only loop feed."""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import io
import json
import secrets
import sqlite3
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, make_response, request, send_file, send_from_directory
from PIL import Image, UnidentifiedImageError

from . import passwd, pipeline
from .services import Services, new_id, now

MAX_UPLOAD = 10 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_SIDE = 1600
SESSION_SECONDS = 8 * 3600
LOGIN_WINDOW = 60
LOGIN_MAX_FAILS = 5
ID_LEN = 32
MAX_PROMPT = 600


def _sign(secret: bytes, payload: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def create_app(svc: Services) -> Flask:
    app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"), static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
    secret = svc.settings.secret
    fails: dict[str, list[float]] = {}
    salt = secrets.token_bytes(16)

    def client_key() -> str:
        return hashlib.sha256(salt + (request.remote_addr or "").encode()).hexdigest()

    def same(a: str, b: str) -> bool:
        """Constant-time comparison that accepts any text. hmac.compare_digest raises TypeError on
        non-ASCII str, which used to turn a hostile header into an unauthenticated HTTP 500."""
        return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))

    def json_body() -> dict:
        """The JSON object sent by the client; anything else (a string, a list, bad JSON) is an empty one."""
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    def bearer() -> str:
        header = request.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def make_cookie() -> tuple[str, str]:
        sid = secrets.token_hex(16)
        payload = base64.urlsafe_b64encode(json.dumps({"sid": sid, "exp": int(time.time()) + SESSION_SECONDS}).encode())
        return payload.decode() + "." + _sign(secret, payload), _sign(secret, b"csrf" + sid.encode())

    def read_cookie() -> str | None:
        raw = request.cookies.get("hh_session", "")
        payload, _, sig = raw.partition(".")
        if not payload or not same(sig, _sign(secret, payload.encode())):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode()))
        except ValueError:
            return None
        return data["sid"] if data.get("exp", 0) > time.time() else None

    def admin_required(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            token = bearer()
            if token and svc.settings.admin_token and same(token, svc.settings.admin_token):
                return fn(*args, **kwargs)  # header token: not browser-borne, so no CSRF needed
            sid = read_cookie()
            if sid is None:
                return jsonify(error="authentication required"), 401
            if request.method not in ("GET", "HEAD"):
                sent = request.headers.get("X-CSRF-Token", "")
                if not same(sent, _sign(secret, b"csrf" + sid.encode())):
                    return jsonify(error="bad CSRF token"), 403
            return fn(*args, **kwargs)
        return wrapper

    def loop_required(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            expected = svc.settings.loop_token
            if not expected or not same(bearer(), expected):
                return jsonify(error="loop token required"), 401
            return fn(*args, **kwargs)
        return wrapper

    @app.after_request
    def headers(resp: Response):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        resp.headers["Referrer-Policy"] = "no-referrer"
        return resp

    @app.post("/api/login")
    def login():
        key, t = client_key(), time.time()
        recent = [x for x in fails.get(key, []) if t - x < LOGIN_WINDOW]
        if len(recent) >= LOGIN_MAX_FAILS:
            return jsonify(error="too many attempts"), 429
        body = json_body()
        password, token = str(body.get("password", "")), str(body.get("token", ""))
        ok = False
        if svc.settings.password_hash and password:
            ok = passwd.verify_password(password, svc.settings.password_hash)
        if not ok and svc.settings.admin_token and token:
            ok = same(token, svc.settings.admin_token)
        if not ok:
            recent.append(t)
            fails[key] = recent
            return jsonify(error="invalid credentials"), 401
        cookie, csrf = make_cookie()
        resp = make_response(jsonify(csrf=csrf))
        resp.set_cookie("hh_session", cookie, httponly=True, samesite="Strict", max_age=SESSION_SECONDS)
        return resp

    @app.get("/api/session")
    def session_info():
        """The CSRF token of the current session, for a page that was reloaded. Cookie only:
        no cookie, a bad one or an expired one gets 401 (a header token does not count here)."""
        sid = read_cookie()
        if sid is None:
            return jsonify(error="authentication required"), 401
        return jsonify(csrf=_sign(secret, b"csrf" + sid.encode()))

    @app.get("/api/spend")
    @admin_required
    def spend_status():
        return jsonify(svc.guard.status())

    @app.post("/api/drawings")
    @admin_required
    def add_drawing():
        if request.form.get("consent") != "1":
            return jsonify(error="parental consent is required"), 400
        upload = request.files.get("file")
        if upload is None:
            return jsonify(error="file is required"), 400
        try:
            img = Image.open(upload.stream)
            if img.format not in ("JPEG", "PNG"):
                return jsonify(error="only JPEG and PNG are accepted"), 400
            if img.width * img.height > MAX_PIXELS:
                return jsonify(error="image too large"), 400
            img.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            return jsonify(error="not a readable image"), 400
        clean = img.convert("RGB")  # new pixel buffer: EXIF and other metadata are dropped
        clean.thumbnail((MAX_SIDE, MAX_SIDE))
        file_id, drawing_id = new_id(), new_id()
        out = io.BytesIO()
        clean.save(out, "JPEG", quality=90)
        svc.media_path("originals", file_id, "jpg").write_bytes(out.getvalue())
        conn = svc.connect()
        try:
            conn.execute("INSERT INTO drawings (id, created_at, consent, original_file) VALUES (?,?,1,?)",
                         (drawing_id, now(), file_id))
        finally:
            conn.close()
        return jsonify(id=drawing_id), 201

    def public_count(conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM publications WHERE channel='public'").fetchone()[0]

    def note_loop_change(conn, before: int) -> None:
        """Remember that the ADMINISTRATOR emptied the public loop (it is what makes an empty
        manifest mean "play nothing"). Any public clip again, and the memory is dropped."""
        if public_count(conn) > 0:
            conn.execute("DELETE FROM settings WHERE key='loop_cleared'")
        elif before > 0:
            conn.execute("INSERT INTO settings (key, value) VALUES ('loop_cleared', ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now(),))

    def drawing_or_404(conn, drawing_id: str):
        row = conn.execute("SELECT * FROM drawings WHERE id=?", (drawing_id,)).fetchone()
        if row is None:
            abort(404)
        return row

    @app.get("/api/drawings")
    @admin_required
    def list_drawings():
        conn = svc.connect()
        try:
            out = []
            for d in conn.execute("SELECT * FROM drawings ORDER BY created_at, id"):
                chars = [dict(id=c["id"], idx=c["idx"], version=c["version"], description=c["description"], used=bool(c["used"]))
                         for c in conn.execute("SELECT * FROM characters WHERE drawing_id=? ORDER BY version, idx", (d["id"],))]
                clips = [dict(id=c["id"], version=c["version"], cost_micro=c["cost_micro"], prompt=c["prompt"])
                         for c in conn.execute("SELECT * FROM clips WHERE drawing_id=? ORDER BY version", (d["id"],))]
                pub = conn.execute("SELECT clip_id, channel FROM publications WHERE drawing_id=?", (d["id"],)).fetchone()
                job = conn.execute("SELECT id, state, error FROM jobs WHERE drawing_id=? ORDER BY id DESC LIMIT 1",
                                   (d["id"],)).fetchone()
                out.append(dict(id=d["id"], ok_online=bool(d["ok_online"]), characters=chars, clips=clips,
                                publication=dict(pub) if pub else None, job=dict(job) if job else None))
            return jsonify(drawings=out)
        finally:
            conn.close()

    @app.post("/api/drawings/<drawing_id>/generate")
    @admin_required
    def generate(drawing_id: str):
        conn = svc.connect()
        try:
            drawing_or_404(conn, drawing_id)
        finally:
            conn.close()
        body = json_body()
        prompt = body.get("prompt", "")
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT or any(
                ord(ch) < 32 and ch not in "\n\t" for ch in prompt):
            return jsonify(error=f"prompt must be text of at most {MAX_PROMPT} characters"), 400
        job_id = pipeline.enqueue(svc, drawing_id, prompt.strip())
        if job_id is None:
            return jsonify(error="a clip is already being made for this drawing"), 409
        return jsonify(job_id=job_id), 202

    @app.post("/api/drawings/<drawing_id>/online")
    @admin_required
    def set_online(drawing_id: str):
        ok = bool((json_body()).get("ok"))
        conn = svc.connect()
        try:
            drawing_or_404(conn, drawing_id)
            before = public_count(conn)
            conn.execute("UPDATE drawings SET ok_online=? WHERE id=?", (1 if ok else 0, drawing_id))
            if not ok:  # withdrawing the OK also pulls it from the public loop
                conn.execute("DELETE FROM publications WHERE drawing_id=? AND channel='public'", (drawing_id,))
            note_loop_change(conn, before)
        finally:
            conn.close()
        return jsonify(ok_online=ok)

    @app.post("/api/drawings/<drawing_id>/publish")
    @admin_required
    def publish(drawing_id: str):
        body = json_body()
        channel = body.get("channel")
        if channel not in ("preview", "public"):
            return jsonify(error="channel must be preview or public"), 400
        conn = svc.connect()
        try:
            drawing = drawing_or_404(conn, drawing_id)
            if channel == "public" and not drawing["ok_online"]:
                return jsonify(error="the 'OK online' box is not checked for this drawing"), 409
            clip_id = body.get("clip_id")
            if clip_id:
                clip = conn.execute("SELECT id FROM clips WHERE id=? AND drawing_id=?", (clip_id, drawing_id)).fetchone()
            else:
                clip = conn.execute("SELECT id FROM clips WHERE drawing_id=? ORDER BY version DESC LIMIT 1",
                                    (drawing_id,)).fetchone()
            if clip is None:
                return jsonify(error="no such clip for this drawing"), 409
            before = public_count(conn)
            conn.execute(
                "INSERT INTO publications (drawing_id, clip_id, channel, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(drawing_id) DO UPDATE SET clip_id=excluded.clip_id, channel=excluded.channel, "
                "updated_at=excluded.updated_at",
                (drawing_id, clip["id"], channel, now()),
            )
            note_loop_change(conn, before)
        finally:
            conn.close()
        return jsonify(clip_id=clip["id"], channel=channel)

    @app.post("/api/drawings/<drawing_id>/unpublish")
    @admin_required
    def unpublish(drawing_id: str):
        conn = svc.connect()
        try:
            drawing_or_404(conn, drawing_id)
            before = public_count(conn)
            conn.execute("DELETE FROM publications WHERE drawing_id=?", (drawing_id,))
            note_loop_change(conn, before)
        finally:
            conn.close()
        return jsonify(ok=True)

    def safe_media(kind: str, file_id: str, ext: str):
        """The file named by the database, never a symlink, never outside its own folder."""
        path = svc.media_path(kind, file_id, ext)
        base = (svc.settings.media_dir / kind).resolve()
        if path.is_symlink() or not path.is_file() or path.resolve().parent != base:
            return None
        return path

    def serve(table: str, column: str, kind: str, ext: str, mime: str, item_id: str):
        # The id is only ever used as a bound SQL parameter; the file name comes from the database.
        conn = svc.connect()
        try:
            row = conn.execute(f"SELECT {column} AS f FROM {table} WHERE id=?", (item_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            abort(404)
        path = safe_media(kind, row["f"], ext)
        if path is None:
            abort(404)
        return send_file(path, mimetype=mime)

    @app.get("/api/estimate")
    @admin_required
    def estimate():
        text, image, video = svc.price("text"), svc.price("image"), svc.price("video")
        remaining = svc.guard.status()["remaining_micro"]
        n = svc.settings.max_figures
        return jsonify(text_micro=text, image_micro=image, video_micro=video, max_figures=n,
                       max_micro=text + n * image + video, remaining_micro=remaining, estimated=True,
                       free=(text + image + video == 0), provider=svc.settings.provider,
                       default_prompt=pipeline.DEFAULT_PROMPT, max_prompt=MAX_PROMPT)

    @app.post("/api/characters/<char_id>/used")
    @admin_required
    def set_used(char_id: str):
        used = bool((json_body()).get("used"))
        conn = svc.connect()
        try:
            if conn.execute("UPDATE characters SET used=? WHERE id=?", (1 if used else 0, char_id)).rowcount != 1:
                abort(404)
        finally:
            conn.close()
        return jsonify(used=used)

    @app.delete("/api/drawings/<drawing_id>")
    @admin_required
    def delete_drawing(drawing_id: str):
        """Purge a drawing: rows and files. The spend ledger is never touched."""
        conn = svc.connect()
        try:
            drawing = drawing_or_404(conn, drawing_id)
            busy = conn.execute("SELECT 1 FROM jobs WHERE drawing_id=? AND state IN ('queued','running')",
                                (drawing_id,)).fetchone()
            if busy:
                return jsonify(error="a job is still queued or running for this drawing"), 409
            files = [("originals", drawing["original_file"], "jpg")]
            files += [("characters", r["file_id"], "png")
                      for r in conn.execute("SELECT file_id FROM characters WHERE drawing_id=?", (drawing_id,))]
            files += [("clips", r["file_id"], "mp4")
                      for r in conn.execute("SELECT file_id FROM clips WHERE drawing_id=?", (drawing_id,))]
            before = public_count(conn)
            conn.execute("DELETE FROM drawings WHERE id=?", (drawing_id,))
            note_loop_change(conn, before)
        finally:
            conn.close()
        for kind, file_id, ext in files:
            svc.media_path(kind, file_id, ext).unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/media/original/<item_id>")
    @admin_required
    def media_original(item_id: str):
        return serve("drawings", "original_file", "originals", "jpg", "image/jpeg", item_id)

    @app.get("/media/character/<item_id>")
    @admin_required
    def media_character(item_id: str):
        return serve("characters", "file_id", "characters", "png", "image/png", item_id)

    @app.get("/media/clip/<item_id>")
    @admin_required
    def media_clip(item_id: str):
        return serve("clips", "file_id", "clips", "mp4", "video/mp4", item_id)

    @app.get("/loop/manifest.json")
    @loop_required
    def manifest():
        conn = svc.connect()
        try:
            rows = conn.execute(
                "SELECT c.id AS id, c.file_id AS f FROM publications p JOIN clips c ON c.id = p.clip_id "
                "WHERE p.channel='public' ORDER BY p.updated_at, c.id"
            ).fetchall()
            cleared = bool(not rows and conn.execute(
                "SELECT 1 FROM settings WHERE key='loop_cleared'").fetchone())
        except sqlite3.Error:
            return jsonify(error="database error"), 503          # never an empty manifest, never "cleared"
        finally:
            conn.close()
        clips = []
        for r in rows:
            path = safe_media("clips", r["f"], "mp4")
            if path is None:
                continue
            data = path.read_bytes()
            clips.append(dict(id=r["id"], url=f"/loop/clip/{r['id']}", sha256=hashlib.sha256(data).hexdigest(),
                              bytes=len(data)))
        body = dict(version=1, clips=clips)
        if cleared and not clips:
            body["cleared"] = True      # the administrator emptied the loop on purpose
        return jsonify(body)

    @app.get("/loop/clip/<item_id>")
    @loop_required
    def loop_clip(item_id: str):
        conn = svc.connect()
        try:
            row = conn.execute(
                "SELECT c.file_id AS f FROM publications p JOIN clips c ON c.id = p.clip_id "
                "WHERE p.channel='public' AND c.id=?", (item_id,)
            ).fetchone()
        finally:
            conn.close()
        path = safe_media("clips", row["f"], "mp4") if row else None
        if path is None:
            abort(404)
        return send_file(path, mimetype="video/mp4")

    return app
