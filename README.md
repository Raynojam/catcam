# Cat Cam

A 24/7 camera on **mina** that watches the windowsill for Faff, Siya and Thuli, writes a daily
diary about them, and cuts vertical videos ready to post.

## What runs where

| Piece | File | Service |
| --- | --- | --- |
| Frame stage: camera → windowsill crop → low-light fix → `/dev/video10` | `app/frame.py` | `catcam-frame` |
| Live stream (µStreamer) of the frame stage | `app/boot.py --stream` | `catcam-camera` |
| Recorder (Motion), website, cat detector | `app/catcam.py` | `catcam-recorder` |
| Captions, photo identity, diary, Studio, Telegram | `app/insights.py`, `identity.py`, `studio.py` | `catcam-insights` |
| Resets a camera stuck on a white picture | `guard/catcam_guard.py` | `catcam-guard` |
| Pages served by the website | `app/pages/*.html` | |

Data stays on mina: clips in `~/Videos/CatCam`, everything else in `~/.local/share/catcam`
and settings in `~/.config/catcam`.

## Updating mina

The `main` branch holds the tested release; `VERSION` names it. On mina:

```
catcam-update            # install the newest version
catcam-update --check    # what is installed and available
catcam-update --previous # go back to the version installed before
```

`deploy/install.sh` backs up what is installed, installs, checks the site and the camera stream,
and restores the backup by itself if a check fails.

## Developing

```
python -m pytest -q tests
```

Tests run against a throwaway home folder. CI runs them on every push.
The plan and requirements are in the Cat Cam PRD doc.
