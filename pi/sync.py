#!/usr/bin/env python3
"""Pull the public loop from the server. Standard library only.

Env: HH_SERVER (http(s)://host:port), LOOP_TOKEN, HH_LOOP_DIR (default /var/lib/haunted-hallway/loop).

Rules: a server that cannot be reached, answers badly or sends a malformed manifest changes
NOTHING (the last loop keeps playing). An EMPTY manifest is ignored too (warning, loop kept),
unless it carries {"cleared": true}, which the server sends only after the administrator
removed the last public clip on purpose. Every clip is checked (size and sha256) before it
replaces anything, and written by atomic rename.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_CLIP = 200 * 1024 * 1024
MAX_MANIFEST = 1024 * 1024
MAX_CLIPS = 500
TIMEOUT = 30


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a loop server never needs to send the Pi somewhere else


OPENER = urllib.request.build_opener(NoRedirect)


def get(url: str, token: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    return OPENER.open(req, timeout=TIMEOUT)


def read_manifest(server: str, token: str) -> tuple[list[dict], bool]:
    with get(server + "/loop/manifest.json", token) as res:
        raw = res.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError("manifest too large")
    data = json.loads(raw)
    clips = data["clips"]
    if data.get("version") != 1 or not isinstance(clips, list) or len(clips) > MAX_CLIPS:
        raise ValueError("unexpected manifest shape")
    seen = set()
    for c in clips:
        ok = (isinstance(c, dict) and ID_RE.match(str(c.get("id", ""))) and SHA_RE.match(str(c.get("sha256", "")))
              and c.get("url") == f"/loop/clip/{c['id']}" and isinstance(c.get("bytes"), int)
              and 0 < c["bytes"] <= MAX_CLIP and c["id"] not in seen)
        if not ok:
            raise ValueError("malformed manifest entry")
        seen.add(c["id"])
    return clips, (data.get("cleared") is True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(server: str, token: str, clip: dict, dest: Path) -> None:
    part = dest.with_name("." + dest.name + ".part")
    h, size = hashlib.sha256(), 0
    try:
        with get(server + clip["url"], token) as res, part.open("wb") as out:
            while True:
                block = res.read(1 << 20)
                if not block:
                    break
                size += len(block)
                if size > clip["bytes"]:
                    raise ValueError("clip larger than announced")
                h.update(block)
                out.write(block)
        if size != clip["bytes"] or h.hexdigest() != clip["sha256"]:
            raise ValueError("clip does not match its manifest entry")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


def sync(server: str, token: str, loop_dir: Path) -> int:
    loop_dir.mkdir(parents=True, exist_ok=True)
    try:
        clips, cleared = read_manifest(server.rstrip("/"), token)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"sync: keeping the current loop, manifest not usable: {type(exc).__name__}", file=sys.stderr)
        return 1
    if not clips and not cleared:
        print("sync: warning: empty manifest without the 'cleared' flag ignored; keeping the current loop", file=sys.stderr)
        return 0
    failed = 0
    wanted = {c["id"] for c in clips}
    for clip in clips:
        dest = loop_dir / f"{clip['id']}.mp4"
        if dest.is_file() and not dest.is_symlink() and sha256_of(dest) == clip["sha256"]:
            continue
        try:
            download(server.rstrip("/"), token, clip, dest)
            print(f"sync: fetched {clip['id'][:8]}")
        except (OSError, ValueError) as exc:
            failed += 1
            print(f"sync: could not fetch {clip['id'][:8]}: {type(exc).__name__}", file=sys.stderr)
    if failed == 0:       # remove only after everything wanted is in place
        for path in loop_dir.glob("*.mp4"):
            if path.stem not in wanted:
                path.unlink()
                print(f"sync: removed {path.stem[:8]}")
    return 1 if failed else 0


def main() -> int:
    server, token = os.environ.get("HH_SERVER", ""), os.environ.get("LOOP_TOKEN", "")
    if not server.startswith(("http://", "https://")) or not token:
        print("sync: set HH_SERVER (http:// or https://) and LOOP_TOKEN", file=sys.stderr)
        return 2
    return sync(server, token, Path(os.environ.get("HH_LOOP_DIR", "/var/lib/haunted-hallway/loop")))


if __name__ == "__main__":
    raise SystemExit(main())
