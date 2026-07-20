# OBSBOT Tiny 3 — local web controller

A zero-dependency control panel for the OBSBOT Tiny 3 on Linux. A small Python
daemon drives the camera over **standard V4L2 controls** (raw ioctls — no SDK,
no `pyusb`, no `v4l-utils` required) and serves a browser UI you can open on the
machine itself or from your phone/tablet on the same network.

This covers the controls the camera exposes natively:
pan / tilt / zoom, focus (auto + manual), exposure (auto/manual/aperture),
white balance, and image tuning (brightness, contrast, saturation, hue, gain,
sharpness, backlight). **Presets** are implemented in software (save, recall
and rename; stored in `tiny3_presets.json`). A **live preview** with a
pan/tilt/zoom HUD is shown at the top of the page.

### UI quick reference

- **PTZ pad** — hold a direction (8-way) to move; `⌂` re-centers. Speed
  selector: Slow / Normal / Fast.
- **Keyboard** — arrows move, `+`/`−` zoom, `C` centers, `1`–`4` recall a
  preset, `Shift+1`–`4` saves one.
- **Sliders** — drag, or click the number to type an exact value;
  double-click a label to reset that control to its camera default. Each
  section header has a `reset` button for the whole group.
- **Presets** — `save` stores the current pan/tilt/zoom/focus; click the big
  button to recall; `rename` gives a slot a label.
- **AI Tracking & Lens** — AI tracking modes (Off / Human / Upper body /
  Close-up / Headless / Lower body / Group), field of view (86°/78°/65°), HDR,
  face-priority exposure, gesture control (hand gestures for target
  selection/zoom/etc.) and voice control (the "Hi Tiny" commands), all via the
  vendor XU (see below). While AI tracking is on, the camera steers itself, so
  the manual pad is disabled.
- The page polls the camera every 2.5 s, so changes made by other apps show up.

### Live preview

The preview uses the browser's own camera access (`getUserMedia`), so the video
is hardware-decoded and never passes through the daemon. Because browsers only
allow camera access from a *secure context*, the preview works when you open the
page on **`http://localhost` / `http://127.0.0.1`** (the local machine) or over
HTTPS. Opening it from a **phone over plain `http://<ip>`** will show the
controls but not the preview (the browser blocks camera access on insecure
origins). The preview also needs the camera to be free — if another app (Zoom,
OBS, …) is currently using it, the preview will report the camera as busy.

## AI features / vendor controls (`tiny3_xu.py`)

AI tracking, FOV, HDR and the other OBSBOT-specific features live on the
camera's vendor UVC Extension Unit (unit id 2), reached via the
`UVCIOC_CTRL_QUERY` ioctl. The protocol was reverse-engineered from the Tiny 2
family (`cgevans/tiny2`, `mitchelloharawild/obsbot-tiny-2-control`,
`samliddicott/meet4k`) and **verified on this Tiny 3**:

- Every payload is a 60-byte buffer (`GET_LEN` first — the driver rejects other
  sizes). Selector 6 is a checksum-free register RPC:
  `SET_CUR [reg, nbytes, values…]`; `GET_CUR` returns a status block
  (byte `0x18` = AI mode, byte `0x04` tracks FOV).
- **Verified working:** AI tracking (`reg 0x16`: off / normal / upper-body /
  close-up / headless / lower-body / group; status bytes 0x18/0x1C), FOV
  (`reg 0x04`: 0 wide 86° / 1 medium 78° / 2 narrow 65° — the SDK enum mapping,
  which settles the conflicting mappings floating around the Tiny 2 projects),
  HDR (`reg 0x01`, status byte 0x06), face-AE (`reg 0x03`, status byte 0x07)
  and **voice control** (`reg 0x15`: `[0x15, 2, cmd, 0|1]` per "Hi Tiny"
  command; status byte 0x15 is the enabled-commands bitfield — found by
  disassembling `cameraSetAudioCtrlStateU` in the official SDK's `libdev.so`).
  The status block turned out to be the SDK's `CameraStatus` "tiny" struct
  (`dev.hpp`), which decodes every byte.
- **Gesture control** uses the framed command packets on selector 2. The
  framing (header/body CRC16 included) was published by `jcdoll/obsBotRemote`
  (validated there on a Tiny 3) and is implemented in `tiny3_xu.py`:
  boolean gesture parameters are written with wire command `0x04/0x00D1` and
  read back via `0x04/0x00D2` request/response. Verified here: enable/disable
  all hand gestures with real state readback.
- **Same grammar, not yet verified here:** the hand/whiteboard/desk AI modes
  (CLI-only for now).
- **Capture/replay only** (framed packets whose semantics are still unmapped):
  sleep/wake, camera-side presets, tracking speed.

```bash
python3 tiny3_xu.py status          # decode + dump the vendor status block
python3 tiny3_xu.py ai normal       # AI tracking: off|normal|upper|closeup|…
python3 tiny3_xu.py fov narrow      # wide|medium|narrow
python3 tiny3_xu.py gesture off     # hand-gesture recognition on|off
python3 tiny3_xu.py gesture-status  # per-gesture enable state
python3 tiny3_xu.py voice off       # "Hi Tiny" voice commands on|off
python3 tiny3_xu.py raw 16 02 02 00 # raw selector-6 register write
```

All of these are soft, idempotent settings, recoverable from the OBSBOT app or
a USB re-plug. Note: AI tracking moves the gimbal without updating the reported
pan/tilt, but recalling a preset (absolute write) re-asserts a known position.

## Run

```bash
python3 tiny3_web.py                       # auto-detect OBSBOT, serve on 127.0.0.1:8080
python3 tiny3_web.py --host 0.0.0.0        # also reachable from your phone/LAN
python3 tiny3_web.py --device /dev/video4 --port 8099
python3 tiny3_web.py --port 0              # let the OS pick any open port
```

If the chosen port is busy, the next free port is used automatically (and
printed at startup). The actual URL is always shown in the console.

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
- Pan/tilt movement steps the **absolute** pan/tilt targets while a direction
  is held (the camera glides to each target). The Tiny 3's continuous-speed
  controls are deliberately *not* used: the firmware never reports positions
  reached that way, which silently desyncs its coordinate frame — the HUD,
  presets, and even "center" then point somewhere wrong until the camera is
  power-cycled/USB-reset. Absolute stepping keeps the reported position
  truthful at all times.
