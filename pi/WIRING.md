# Raspberry Pi and projector wiring

What this covers: a Raspberry Pi that plays the public loop full screen on a projector in a
hallway. The Pi only pulls clips from your server. It never holds your admin password or
your Gemini key, only a read-only loop token.

**Status:** the scripts are tested against a simulated player. They have not been run on a
real Pi yet, and the dark gap between two clips has not been measured. See the notes at the end.

## You need

- A Raspberry Pi 4 or 5 with Raspberry Pi OS (64-bit, Lite is fine) and network access to
  the machine that runs the server.
- A projector with an HDMI input. A short HDMI cable (micro-HDMI to HDMI on a Pi 4 or 5).
- Power for both. A power strip you can switch off in one go is the simplest way to stop the show.

## Wiring

1. Pi HDMI port 0 (the one next to the USB-C power port) to the projector's HDMI input.
2. Power the projector, then the Pi. Choose the projector's HDMI input once.
3. Aim and focus using a still picture before you install the player: any image works.

## Portrait projector or rear projection

Edit `/etc/haunted-hallway-player.env` (as root; it holds no secret), then `sudo systemctl restart hh-player`:

| Setting | Values | Use |
| --- | --- | --- |
| `ORIENTATION` | `0`, `90`, `180`, `270` | Turn the picture, for example a projector standing on its side. Clips are portrait (720x1280). |
| `MIRROR` | `0`, `1` | `1` flips left and right, for rear projection (the projector sits behind a thin screen). |

Nothing else about the projection is configurable on purpose.

The player service loads only this file. The server address and loop token stay in
`/etc/haunted-hallway.env`, which only the sync service loads. If you installed an earlier version,
move any `ORIENTATION` or `MIRROR` line from the old file to the new one.

## Install

On the Pi, from a copy of this repository:

```sh
sudo pi/install.sh --dry-run    # prints what it would do, changes nothing
sudo pi/install.sh              # asks for the server address and the loop token
```

The server address looks like `http://192.0.2.10:8080` (use your server's address on your
network). The loop token is the `LOOP_TOKEN` from the server's `.env`; typing it is hidden and
it is stored only in `/etc/haunted-hallway.env`, readable by root alone. Running the installer
again is safe and keeps that file.

It installs `mpv`, creates a `hallway` user without a login, copies the scripts to
`/opt/haunted-hallway`, and starts two things:

- `hh-sync.timer`: every minute, asks the server for the public loop, downloads missing
  clips, checks each one against its checksum, and deletes clips that left the loop.
- `hh-player.service`: plays the clips in random order, never starting a pass with the clip
  that ended the previous one, and starts again when the pass ends.

If the server is unreachable the Pi keeps playing what it already has. If no clip is
published, it shows a plain dark screen.

## Check it works

```sh
systemctl status hh-player hh-sync.timer
journalctl -u hh-sync -n 20        # "fetched ..." lines after you publish a clip
ls /var/lib/haunted-hallway/loop   # one .mp4 per public clip
```

Publish a clip in the console, wait up to a minute, and it appears on the wall.

## Stopping and removing

```sh
sudo systemctl disable --now hh-player.service hh-sync.timer
```

## Not measured yet

- Behaviour on a real Pi: the DRM video output, permissions of the `video` and `render`
  groups, and CPU use have not been observed on hardware.
- The player starts mpv once per pass, so there may be a short dark gap when a pass ends.
  If it is visible on your wall, say so: the fallback is to join clips into one reel.
