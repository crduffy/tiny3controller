# OBSBOT Tiny 3 — local web controller

A zero-dependency control panel for the OBSBOT Tiny 3 on Linux. A small Python
daemon drives the camera over **standard V4L2 controls** (raw ioctls — no SDK,
no `pyusb`, no `v4l-utils` required) and serves a browser UI you can open on the
machine itself or from your phone/tablet on the same network.

This covers the controls the camera exposes natively:
pan / tilt / zoom, focus (auto + manual), exposure (auto + manual),
white balance, and image tuning (brightness, contrast, saturation, hue, gain,
sharpness, backlight). **Presets** are implemented in software (save & restore
pan/tilt/zoom/focus to `tiny3_presets.json`).

> AI tracking, gestures, FOV and the other OBSBOT-specific features live on the
> camera's vendor UVC Extension Unit and are *not* covered here — that needs the
> separate reverse-engineering effort ("Option 3").

## Run

```bash
python3 tiny3_web.py                       # auto-detect OBSBOT, serve on 127.0.0.1:8080
python3 tiny3_web.py --host 0.0.0.0        # also reachable from your phone/LAN
python3 tiny3_web.py --device /dev/video4 --port 8099
```

Then open <http://localhost:8080>.

## Permissions

The camera node (`/dev/video4`) is owned by `root:video`. Either add yourself to
the `video` group:

```bash
sudo usermod -aG video "$USER"   # log out / back in afterwards
```

…or install a udev rule so the OBSBOT is always accessible:

```
# /etc/udev/rules.d/99-obsbot.rules
SUBSYSTEM=="video4linux", ATTRS{idVendor}=="3564", ATTRS{idProduct}=="ff02", MODE="0660", GROUP="video"
```

## How it works

- `Camera` wraps `VIDIOC_QUERYCTRL` / `VIDIOC_G_CTRL` / `VIDIOC_S_CTRL` via
  `fcntl.ioctl` on the V4L2 character device.
- Control ranges are queried at runtime, so the UI sliders auto-scale and
  controls made *inactive* by an auto mode (e.g. manual focus while auto-focus
  is on) are greyed out and skipped on preset restore.
- Pan/tilt uses the camera's continuous-speed controls (hold a direction to
  move, release to stop); zoom/focus/exposure/image are absolute.
