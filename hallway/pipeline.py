"""Drawing -> characters -> clip. One thread, one job at a time.

A job is marked 'running' BEFORE any paid call. A job stuck in 'running' is marked
'error' by reap_stale() and is never retried automatically.
"""
from __future__ import annotations

import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sqlite3
from .providers.base import NotSent
from .spend import BudgetExceeded, LedgerCorrupt

STALE_MINUTES = 20
DEFAULT_PROMPT = "The figures from the drawing drift slowly down a dark hallway."


def _ts(delta_minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def enqueue(svc, drawing_id: str, prompt: str = "") -> int | None:
    """Queue a job. Returns None when this drawing already has one queued or running:
    the database itself refuses a second one (unique index), so a double click cannot slip through."""
    from .services import now
    conn = svc.connect()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (drawing_id, state, prompt, created_at, updated_at) VALUES (?, 'queued', ?, ?, ?)",
            (drawing_id, prompt or DEFAULT_PROMPT, now(), now()),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def reap_stale(svc) -> int:
    conn = svc.connect()
    try:
        cur = conn.execute(
            "UPDATE jobs SET state='error', error='stale: running too long, not retried', updated_at=? "
            "WHERE state='running' AND updated_at < ?",
            (_ts(), _ts(-STALE_MINUTES)),
        )
        return cur.rowcount
    finally:
        conn.close()


def finish_clip(ffmpeg: str, raw: bytes, timeout: int = 120) -> bytes:
    """Normalise to 720x1280, 24 fps, H.264 yuv420p."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "in.mp4", Path(tmp) / "out.mp4"
        src.write_bytes(raw)
        cmd = [
            ffmpeg, "-y", "-loglevel", "error", "-i", str(src),
            "-vf", "scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2",
            "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(dst),
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
        return dst.read_bytes()


def _paid(svc, res, call, *args):
    """Run one paid provider call that already holds its reservation.
    Released only when the call provably never left the machine (NotSent); otherwise it stays booked."""
    svc.provider.last_measured_micro = None
    try:
        out = call(*args, res)
    except NotSent:
        svc.guard.release(res)
        raise
    svc.guard.settle(res, svc.provider.last_measured_micro)
    return out


def process_next(svc) -> bool:
    from .services import new_id, now
    conn = svc.connect()
    try:
        row = conn.execute("SELECT id, drawing_id, prompt FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return False
        claimed = conn.execute(
            "UPDATE jobs SET state='running', updated_at=? WHERE id=? AND state='queued'", (now(), row["id"])
        ).rowcount
        if claimed != 1:
            return True
        job_id, drawing_id = row["id"], row["drawing_id"]
        try:
            _run(svc, conn, job_id, drawing_id, row["prompt"] or DEFAULT_PROMPT)
            conn.execute("UPDATE jobs SET state='done', updated_at=? WHERE id=?", (now(), job_id))
        except BudgetExceeded as exc:
            conn.execute("UPDATE jobs SET state='error', error=?, updated_at=? WHERE id=?",
                         (f"budget: {exc}", now(), job_id))
        except LedgerCorrupt as exc:
            conn.execute("UPDATE jobs SET state='error', error=?, updated_at=? WHERE id=?",
                         (f"ledger: {exc}", now(), job_id))
        except Exception as exc:  # noqa: BLE001 - recorded on the job, never retried here
            conn.execute("UPDATE jobs SET state='error', error=?, updated_at=? WHERE id=?",
                         (f"{type(exc).__name__}: {exc}"[:500], now(), job_id))
        return True
    finally:
        conn.close()


def _run(svc, conn, job_id: int, drawing_id: str, prompt: str) -> None:
    from .services import new_id, now
    guard, provider = svc.guard, svc.provider
    drawing = conn.execute("SELECT original_file FROM drawings WHERE id=?", (drawing_id,)).fetchone()
    if drawing is None:
        raise LookupError("drawing not found")
    original = svc.media_path("originals", drawing["original_file"], "jpg")

    ref = f"job{job_id}"
    res = guard.reserve(f"{ref}:describe", svc.price("text"), "text")
    items = _paid(svc, res, provider.count_and_describe, original)[: svc.settings.max_figures]

    refs: list[bytes] = []
    spent = res.micro
    char_version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM characters WHERE drawing_id=?", (drawing_id,)).fetchone()[0]
    for idx, item in enumerate(items):
        res = guard.reserve(f"{ref}:image{idx}", svc.price("image"), "image")
        png = _paid(svc, res, provider.extract_character, original, item)
        spent += res.micro
        file_id = new_id()
        svc.media_path("characters", file_id, "png").write_bytes(png)
        conn.execute(
            "INSERT INTO characters (id, drawing_id, idx, description, file_id, version, created_at) VALUES (?,?,?,?,?,?,?)",
            (new_id(), drawing_id, idx, item["description"], file_id, char_version, now()),
        )
        refs.append(png)

    res = guard.reserve(f"{ref}:video", svc.price("video"), "video")
    raw = _paid(svc, res, provider.make_video, refs, prompt)
    spent += res.micro
    clip = finish_clip(svc.settings.ffmpeg, raw)
    file_id = new_id()
    svc.media_path("clips", file_id, "mp4").write_bytes(clip)
    clip_version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM clips WHERE drawing_id=?", (drawing_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO clips (id, drawing_id, file_id, cost_micro, version, prompt, created_at) VALUES (?,?,?,?,?,?,?)",
        (new_id(), drawing_id, file_id, spent, clip_version, prompt, now()),
    )
