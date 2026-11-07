#!/usr/bin/env bash
# Play the synced loop with mpv, forever. Env (set by the systemd unit):
#   HH_LOOP_DIR     clips folder (default /var/lib/haunted-hallway/loop)
#   ORIENTATION     0, 90, 180 or 270: rotate the picture (portrait projector)
#   MIRROR          1 for rear projection (mirror left-right)
#   HH_PLAYER_CMD   player command, default mpv; HH_ONCE=1 plays one pass (tests, dry runs)
set -u
LOOP_DIR="${HH_LOOP_DIR:-/var/lib/haunted-hallway/loop}"
STATE="$LOOP_DIR/.last"
PLAYER="${HH_PLAYER_CMD:-mpv}"
case "${ORIENTATION:-0}" in 0|90|180|270) ;; *) echo "player: ORIENTATION must be 0, 90, 180 or 270" >&2; exit 2 ;; esac
case "${MIRROR:-0}" in 0|1) ;; *) echo "player: MIRROR must be 0 or 1" >&2; exit 2 ;; esac

opts=(--really-quiet --fs --no-terminal --vo=drm --video-rotate="${ORIENTATION:-0}" --image-display-duration=inf)
[ "${MIRROR:-0}" = 1 ] && opts+=(--vf=hflip)

while :; do
  mapfile -t clips < <(find "$LOOP_DIR" -maxdepth 1 -type f -name '*.mp4' ! -name '.*' | shuf)
  if [ "${#clips[@]}" -eq 0 ]; then
    # Nothing published yet: a plain dark screen, no text, no code, no name.
    "$PLAYER" "${opts[@]}" --length=30 av://lavfi:color=c=black:s=1280x720 || sleep 5
  else
    # Never start a pass with the clip that ended the previous one.
    last="$(cat "$STATE" 2>/dev/null || true)"
    if [ "${#clips[@]}" -gt 1 ] && [ "${clips[0]}" = "$last" ]; then
      swap="${clips[0]}"; clips[0]="${clips[1]}"; clips[1]="$swap"
    fi
    printf '%s\n' "${clips[-1]}" > "$STATE"
    "$PLAYER" "${opts[@]}" -- "${clips[@]}" || sleep 5
  fi
  [ "${HH_ONCE:-0}" = 1 ] && exit 0
done
