# Family lounge: live stream

The family lounge (the PIN-protected page behind Tailscale Funnel) used to fetch one
snapshot at a time, capped at 2 per second. Every picture was a separate round trip
through Funnel, so phones saw roughly one picture a second.

These files replace that with one long-running stream:

- **lounge.py**: one ffmpeg reads µStreamer's MJPEG stream, crops it and hands out
  cropped JPEGs at the configured rate (default 10, max 12 per second). It runs only
  while someone is watching and stops 15 seconds after the last viewer leaves.
  `/perch.mjpg` serves the stream. It ends on pause, crop change, camera loss or
  sign-out (the session is re-checked every 20 s), and after 10 minutes the viewer
  reconnects. At most 8 streams run at once.
- **pin_lounge.py**: puts `/perch.mjpg` behind the same PIN session as the pictures.
- **lounge.js**: reads the stream itself, so a stalled stream is noticed and
  replaced. It shows only the newest picture, so slow phones don't fall behind.
  Browsers that can't read streams fall back to quick snapshots.
- **pin_setup.py**: the Caddy gateway passes the stream through unbuffered, and the
  write timeout is raised from 15 s to 11 min so streams aren't cut off. The
  installer now reads frames through the gateway and rolls back if they don't arrive.

## Install on mina

```
catcam-update
bash ~/catcam-repo/lounge/apply.sh
```

`apply.sh` builds a new release from the installed one plus these files and runs the
lounge's own installer, which restores the previous release if any check fails. The
invitation link and PIN stay the same. The rate is raised to 10 per second only after
the install passes.

To change the rate later, edit `"fps"` in `~/.config/catcam-lounge/settings.json`
(0.5–12). The lounge picks it up within a second.
