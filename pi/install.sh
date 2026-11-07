#!/usr/bin/env bash
# Install the Pi player and sync timer. Run on the Raspberry Pi from a copy of this repository:
#   sudo pi/install.sh [--dry-run]
# It asks for the server address and the read-only loop token (typed, never on the command line).
# Safe to run again: it never overwrites an existing /etc/haunted-hallway.env (server + token, root only)
# or /etc/haunted-hallway-player.env (picture settings, no secret).
# Test mode: HH_PREFIX=/some/dir writes under that folder and skips apt, useradd and systemctl.
set -eu
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
PREFIX="${HH_PREFIX:-}"
SRC="$(cd "$(dirname "$0")" && pwd)"
APP="$PREFIX/opt/haunted-hallway"
STATE="$PREFIX/var/lib/haunted-hallway"
ENVF="$PREFIX/etc/haunted-hallway.env"
PENV="$PREFIX/etc/haunted-hallway-player.env"
UNITS="$PREFIX/etc/systemd/system"

say() { printf '==> %s\n' "$*"; }
run() { say "$*"; [ "$DRY" = 1 ] || "$@"; }

if [ -z "$PREFIX" ] && [ "$(id -u)" -ne 0 ] && [ "$DRY" = 0 ]; then echo "run with sudo" >&2; exit 1; fi

if [ -z "$PREFIX" ]; then
  run apt-get install -y mpv python3
  getent passwd hallway >/dev/null || run useradd --system --home-dir "$STATE" --shell /usr/sbin/nologin --groups video,render hallway
fi
run mkdir -p "$APP/pi" "$STATE/loop" "$UNITS"
for f in sync.py player.sh; do run install -m 0755 "$SRC/$f" "$APP/pi/$f"; done
for f in hh-player.service hh-sync.service hh-sync.timer; do run install -m 0644 "$SRC/$f" "$UNITS/$f"; done
[ -z "$PREFIX" ] && run chown -R hallway:hallway "$STATE"

if [ -e "$ENVF" ]; then
  say "keeping existing $ENVF"
else
  server="${HH_SERVER:-}"; token="${LOOP_TOKEN:-}"
  if [ "$DRY" = 0 ]; then
    [ -n "$server" ] || read -r -p "Server address (for example http://192.0.2.10:8080): " server
    [ -n "$token" ] || { read -r -s -p "Loop token (input hidden): " token; echo; }
    case "$server" in http://*|https://*) ;; *) echo "server must start with http:// or https://" >&2; exit 1 ;; esac
    [ "${#token}" -ge 16 ] || { echo "token must be 16+ characters" >&2; exit 1; }
    umask 077
    printf 'HH_SERVER=%s\nLOOP_TOKEN=%s\n' "$server" "$token" > "$ENVF"
    chmod 600 "$ENVF"
  fi
  say "wrote $ENVF (mode 600; token not shown)"
fi

# Picture settings live apart from the token: the player service loads only this file.
if [ -e "$PENV" ]; then
  say "keeping existing $PENV"
elif [ "$DRY" = 0 ]; then
  printf 'ORIENTATION=0\nMIRROR=0\n' > "$PENV"
  chmod 644 "$PENV"
  say "wrote $PENV (picture settings, no secret)"
fi

if [ -z "$PREFIX" ]; then
  run systemctl daemon-reload
  run systemctl enable --now hh-sync.timer hh-player.service
fi
say "done. Check: systemctl status hh-player hh-sync.timer ; journalctl -u hh-sync -n 20"
