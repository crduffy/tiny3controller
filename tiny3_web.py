#!/usr/bin/env python3
"""
tiny3_web.py — zero-dependency local web controller for the OBSBOT Tiny 3.

Talks to the camera over standard V4L2 controls (pan/tilt/zoom/focus/exposure/
white balance/image) via raw ioctls — no SDK, no pyusb, no v4l-utils needed.

Run:   python3 tiny3_web.py            # auto-detect the OBSBOT, serve on :8080
       python3 tiny3_web.py --device /dev/video4 --port 8080 --host 0.0.0.0

Then open http://localhost:8080  (or http://<this-pc-ip>:8080 from your phone).
"""

import argparse
import errno
import fcntl
import glob
import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tiny3_xu import Tiny3XU, AI_MODES, FOV_MODES

# ---------------------------------------------------------------------------
# V4L2 ioctl plumbing
# ---------------------------------------------------------------------------

# _IOC(dir, type, nr, size) on Linux: (dir<<30)|(size<<16)|(type<<8)|nr
_IOC_WRITE, _IOC_READ = 1, 2

def _iowr(nr, size):
    return ((_IOC_READ | _IOC_WRITE) << 30) | (size << 16) | (ord('V') << 8) | nr

VIDIOC_QUERYCTRL = _iowr(36, 68)   # struct v4l2_queryctrl  (68 bytes)
VIDIOC_QUERYMENU = _iowr(37, 44)   # struct v4l2_querymenu  (44 bytes, packed)
VIDIOC_G_CTRL    = _iowr(27, 8)    # struct v4l2_control    (8 bytes)
VIDIOC_S_CTRL    = _iowr(28, 8)

# struct v4l2_control { __u32 id; __s32 value; }
_CTRL_FMT  = "=Ii"
# struct v4l2_queryctrl { u32 id; u32 type; char name[32]; s32 min,max,step,def;
#                         u32 flags; u32 reserved[2]; }
_QUERY_FMT = "=II32siiiII8x"
# struct v4l2_querymenu { u32 id; u32 index; char name[32]; u32 reserved; } (packed)
_QMENU_FMT = "=II32sI"

# Controls we expose, in display order. (v4l2 id, key, label, group)
CONTROLS = [
    (0x009A0908, "pan",        "Pan",            "ptz"),
    (0x009A0909, "tilt",       "Tilt",           "ptz"),
    (0x009A090D, "zoom",       "Zoom",           "ptz"),
    # pan_speed (0x009A0920) and tilt_speed (0x009A0921) are deliberately NOT
    # exposed. Writing them physically moves the gimbal while the firmware
    # leaves the reported pan/tilt untouched, so the coordinate frame silently
    # desyncs and only a USB reset recovers it. Leaving them out of CONTROLS
    # keeps them unreachable through /api/set as well. Use absolute targets.
    (0x009A090A, "focus",      "Focus",          "focus"),
    (0x009A090C, "focus_auto", "Auto focus",     "focus"),
    (0x009A0901, "auto_exposure", "Exposure mode", "exposure"),
    (0x009A0902, "exposure",   "Exposure",       "exposure"),
    (0x0098090C, "wb_auto",    "Auto white bal", "color"),
    (0x0098091A, "wb_temp",    "WB temp",        "color"),
    (0x00980900, "brightness", "Brightness",     "image"),
    (0x00980901, "contrast",   "Contrast",       "image"),
    (0x00980902, "saturation", "Saturation",     "image"),
    (0x00980903, "hue",        "Hue",            "image"),
    (0x00980913, "gain",       "Gain",           "image"),
    (0x0098091B, "sharpness",  "Sharpness",      "image"),
    (0x0098091C, "backlight",  "Backlight comp", "image"),
]
ID_BY_KEY = {key: cid for cid, key, _, _ in CONTROLS}

# Which controls a software preset captures + restores.
PRESET_KEYS = ["pan", "tilt", "zoom", "focus", "focus_auto"]
PRESETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tiny3_presets.json")


# AI tracking modes exposed in the web UI (verified on hardware; the
# hand/whiteboard/desk modes stay CLI-only until confirmed on a Tiny 3).
UI_AI_MODES = ["off", "normal", "upper", "closeup", "headless", "lower", "group"]


class InactiveControl(Exception):
    """A control whose auto mode currently owns it refused the write."""


class PresetStoreError(Exception):
    """The presets file could not be written."""


class Camera:
    def __init__(self, device):
        self.device = device
        self.fd = os.open(device, os.O_RDWR)
        # The fd is shared across request-handler threads; serialize all ioctls.
        self._lock = threading.Lock()
        self.xu = Tiny3XU(self.fd)
        # Gesture state lives behind a request/response RPC that occasionally
        # doesn't answer. Kept only as the last reading for debugging -- it is
        # never served to clients in place of a fresh one (see xu_status).
        self._gesture = None

    def xu_status(self):
        """Vendor-feature state, or None if the XU doesn't answer."""
        try:
            with self._lock:
                st = self.xu.decode_status()
                gesture = self.xu.get_gesture_param("master", retries=5)
        except OSError:
            return None
        # Report the gesture bit we actually read back. Selector 2 occasionally
        # does not answer, and a stale cached value is indistinguishable from a
        # real "off" -- so an unconfirmed read is reported as null and the UI
        # shows it as unknown rather than inventing a state.
        self._gesture = gesture
        return {"ai": st["ai_mode"], "fov": st["fov"],
                "hdr": st["hdr"], "face_ae": st["face_ae"],
                "voice": st["voice"], "gesture": gesture,
                "ai_modes": UI_AI_MODES, "fov_modes": list(FOV_MODES)}

    def xu_set(self, feature, value):
        with self._lock:
            if feature == "ai":
                if value not in UI_AI_MODES:
                    raise ValueError(f"unknown AI mode {value!r}")
                self.xu.set_ai_mode(value)
            elif feature == "fov":
                if value not in FOV_MODES:
                    raise ValueError(f"unknown FOV {value!r}")
                self.xu.set_fov(value)
            elif feature == "hdr":
                self.xu.set_hdr(bool(value))
            elif feature == "face_ae":
                self.xu.set_face_ae(bool(value))
            elif feature == "gesture":
                self.xu.set_gesture(bool(value))
                self._gesture = bool(value)
            elif feature == "voice":
                self.xu.set_voice(bool(value))
            else:
                raise ValueError(f"unknown feature {feature!r}")

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def query(self, cid):
        """Return dict(min,max,step,default,type,name,flags) or None if absent."""
        buf = bytearray(struct.pack(_QUERY_FMT, cid, 0, b"", 0, 0, 0, 0, 0))
        try:
            with self._lock:
                fcntl.ioctl(self.fd, VIDIOC_QUERYCTRL, buf, True)
        except OSError:
            return None
        _id, ctype, name, mn, mx, step, dflt, flags = struct.unpack(_QUERY_FMT, bytes(buf))
        if flags & 0x0001:  # V4L2_CTRL_FLAG_DISABLED
            return None
        return {
            "type": ctype, "name": name.split(b"\x00")[0].decode(errors="replace"),
            "min": mn, "max": mx, "step": step, "default": dflt, "flags": flags,
            # INACTIVE: control is overridden by an auto mode (e.g. focus while
            # auto-focus is on) and will reject writes with EACCES.
            "inactive": bool(flags & 0x0010),
        }

    def query_menu(self, cid, mn, mx):
        """For a menu control, return {value: label} for each valid entry."""
        items = {}
        for idx in range(mn, mx + 1):
            buf = bytearray(struct.pack(_QMENU_FMT, cid, idx, b"", 0))
            try:
                with self._lock:
                    fcntl.ioctl(self.fd, VIDIOC_QUERYMENU, buf, True)
            except OSError:
                continue  # gap in the menu (e.g. exposure_auto only has 1 and 3)
            _id, _idx, name, _res = struct.unpack(_QMENU_FMT, bytes(buf))
            items[idx] = name.split(b"\x00")[0].decode(errors="replace")
        return items

    def get(self, cid):
        buf = bytearray(struct.pack(_CTRL_FMT, cid, 0))
        try:
            with self._lock:
                fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf, True)
        except OSError:
            return None
        _id, val = struct.unpack(_CTRL_FMT, bytes(buf))
        return val

    def set(self, cid, value):
        # Clamp to the control's advertised range before writing. The driver
        # clamps most controls itself, but not all of them treat the value as
        # signed: zoom_absolute reads a negative as a large unsigned number and
        # saturates at *maximum*, so an out-of-range write lands at the opposite
        # end from what the caller meant. Clamping here makes every control
        # behave the same way. Step alignment is left to the driver, which
        # rounds correctly.
        # Reject non-numbers explicitly. bool is a subclass of int, so `true`
        # would otherwise be accepted silently as 1.
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("value must be a number")
        value = int(value)
        q = self.query(cid)
        if q:
            value = max(q["min"], min(q["max"], value))
        buf = bytearray(struct.pack(_CTRL_FMT, cid, value))
        try:
            with self._lock:
                fcntl.ioctl(self.fd, VIDIOC_S_CTRL, buf, True)
        except OSError as e:
            if e.errno == errno.EACCES:
                raise InactiveControl(cid) from e
            raise
        return self.get(cid)

    def snapshot(self):
        """Full state: every exposed control with range + current value."""
        out = {}
        for cid, key, label, group in CONTROLS:
            q = self.query(cid)
            if not q:
                continue
            c = {
                "id": cid, "label": label, "group": group,
                "min": q["min"], "max": q["max"], "step": q["step"],
                "default": q["default"], "type": q["type"], "value": self.get(cid),
                "inactive": q["inactive"],
            }
            if q["type"] == 3:  # V4L2_CTRL_TYPE_MENU
                c["menu"] = self.query_menu(cid, q["min"], q["max"])
            out[key] = c
        return out


# ---------------------------------------------------------------------------
# Software presets (Option-1: store/restore values; no XU needed)
# ---------------------------------------------------------------------------

def load_presets():
    """Presets normalized to {slot: {"name": str, "values": {key: val}}}."""
    try:
        with open(PRESETS_FILE) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for slot, p in raw.items():
        if not isinstance(p, dict):
            continue
        if "values" in p:
            out[slot] = {"name": p.get("name") or "", "values": p["values"]}
        else:  # legacy flat format
            out[slot] = {"name": "", "values": p}
    return out

def save_presets(p):
    # Wrap disk failures in their own type. A read-only presets file raises
    # PermissionError, which is an OSError with errno EACCES -- exactly what the
    # driver raises for an inactive control -- so without this the handler would
    # answer a disk problem with "control is inactive".
    try:
        with open(PRESETS_FILE, "w") as f:
            json.dump(p, f, indent=2)
    except OSError as e:
        raise PresetStoreError(e.strerror or str(e)) from e


# ---------------------------------------------------------------------------
# Device auto-detection
# ---------------------------------------------------------------------------

def find_obsbot():
    """Return the first /dev/videoN whose sysfs name mentions OBSBOT and has PTZ."""
    nodes = sorted(glob.glob("/dev/video*"),
                   key=lambda p: int("".join(filter(str.isdigit, p)) or 0))
    for node in nodes:
        n = os.path.basename(node)
        try:
            with open(f"/sys/class/video4linux/{n}/name") as f:
                name = f.read().strip()
        except OSError:
            continue
        if "obsbot" in name.lower():
            try:
                cam = Camera(node)
                ok = cam.query(0x009A0908) is not None  # pan_absolute present?
                cam.close()
                if ok:
                    return node
            except OSError:
                continue
    return None


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    cam = None  # set in main()

    def log_message(self, *a):
        pass  # quiet

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except ValueError:
            # Say the body was unparseable rather than letting it fall through
            # as an empty dict, which used to surface as "unknown control None".
            raise ValueError("request body is not valid JSON")
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        return body

    def do_GET(self):
        # Wrapped like do_POST: a short/garbled XU status buffer surfaces as an
        # IndexError out of decode_status, and an unhandled one here drops the
        # connection so the UI just reports "cannot reach server".
        if self.path == "/" or self.path.startswith("/index"):
            # Served outside the try: once the headers are out, a failed write
            # means the client vanished, and answering again would be a second
            # response on a committed socket.
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/api/state":
            return self._json({"error": "not found"}, 404)
        # Only the state payload can raise: a short or garbled XU status buffer
        # surfaces as an IndexError out of decode_status, and an unhandled one
        # drops the connection so the UI just reports "cannot reach server".
        try:
            payload = {"controls": self.cam.snapshot(),
                       "presets": load_presets(),
                       "xu": self.cam.xu_status(),
                       "device": self.cam.device}
        except OSError as e:
            return self._json(
                {"error": f"camera I/O error: {e.strerror or e}"}, 500)
        except Exception as e:
            return self._json(
                {"error": f"internal error: {type(e).__name__}"}, 500)
        self._json(payload)

    def do_POST(self):
        try:
            # inside the try: an unparseable body must come back as a 400, not
            # escape the handler and drop the connection with no response
            data = self._read_body()
            if self.path == "/api/set":
                key = data.get("key")
                cid = ID_BY_KEY.get(key)
                if cid is None:
                    return self._json({"error": f"unknown control {key!r}"}, 400)
                val = self.cam.set(cid, data["value"])
                return self._json({"key": key, "value": val})

            # NOTE: there is deliberately no speed-based move endpoint. Writing
            # pan_speed/tilt_speed physically moves the gimbal but the firmware
            # never updates the reported pan/tilt, so the coordinate frame
            # silently desyncs and only a USB reset recovers it. The UI steps
            # absolute pan/tilt targets instead (see ptzTick in the page script).

            if self.path == "/api/center":
                for k in ("pan", "tilt"):
                    self.cam.set(ID_BY_KEY[k], 0)
                return self._json({"ok": True})

            if self.path == "/api/xu":
                self.cam.xu_set(data.get("feature"), data.get("value"))
                return self._json({"ok": True, "xu": self.cam.xu_status()})

            if self.path == "/api/preset/save":
                slot = str(data.get("slot"))
                snap = {k: self.cam.get(ID_BY_KEY[k]) for k in PRESET_KEYS if k in ID_BY_KEY}
                presets = load_presets()
                old_name = presets.get(slot, {}).get("name", "")
                presets[slot] = {"name": data.get("name") or old_name, "values": snap}
                save_presets(presets)
                return self._json({"slot": slot, "saved": presets[slot]})

            if self.path == "/api/preset/rename":
                slot = str(data.get("slot"))
                presets = load_presets()
                if slot not in presets:
                    return self._json({"error": "empty slot"}, 404)
                presets[slot]["name"] = str(data.get("name", ""))[:24]
                save_presets(presets)
                return self._json({"slot": slot, "name": presets[slot]["name"]})

            if self.path == "/api/preset/go":
                slot = str(data.get("slot"))
                presets = load_presets()
                if slot not in presets:
                    return self._json({"error": "empty slot"}, 404)
                saved = presets[slot]["values"]
                # Apply auto/master toggles first; skip manual controls that the
                # restored auto mode makes inactive; never let one failed control
                # abort the rest of the restore.
                order = sorted(saved, key=lambda k: 0 if k.endswith("_auto") else 1)
                focus_auto_on = saved.get("focus_auto")
                applied, errors = {}, {}
                for k in order:
                    v = saved[k]
                    if k not in ID_BY_KEY or v is None:
                        continue
                    if k == "focus" and focus_auto_on:
                        continue
                    try:
                        # report what the camera actually took, not what was
                        # asked for -- the driver clamps and step-rounds
                        applied[k] = self.cam.set(ID_BY_KEY[k], v)
                    except InactiveControl:
                        errors[k] = "inactive (its auto mode is on)"
                    except (OSError, ValueError, TypeError,
                            struct.error) as e:
                        # a hand-edited or legacy presets file can hold junk;
                        # record it and keep restoring the rest, as promised
                        errors[k] = str(e)
                return self._json({"slot": slot, "applied": applied, "errors": errors})

            return self._json({"error": "not found"}, 404)
        except InactiveControl:
            # the driver refuses writes to a control its auto mode owns. Raised
            # only from Camera.set, so a disk EACCES can never land here.
            return self._json({"error": "control is inactive "
                                        "(its auto mode is on)"}, 400)
        except KeyError as e:
            # a required field was missing from the request body
            field = e.args[0] if e.args else "?"
            return self._json({"error": f"missing field {field!r}"}, 400)
        except (ValueError, TypeError, OverflowError, RecursionError,
                struct.error) as e:
            # bad value from the client: unknown mode, non-numeric value, a JSON
            # null/array where a number belongs, an int too large to pack,
            # pathologically nested JSON, ...
            msg = str(e)
            if not isinstance(e, ValueError) or "invalid literal" in msg \
                    or "could not convert" in msg:
                msg = "value must be a number"
            if isinstance(e, RecursionError):
                msg = "request body is nested too deeply"
            return self._json({"error": msg}, 400)
        except PresetStoreError as e:
            # writing tiny3_presets.json failed -- a disk problem, not a camera
            # one, and emphatically not an inactive control
            return self._json({"error": f"could not save presets: {e}"}, 500)
        except OSError as e:
            return self._json(
                {"error": f"camera I/O error: {e.strerror or e}"}, 500)
        except Exception as e:
            # last resort: a malformed status buffer surfaces as IndexError, and
            # anything unforeseen must still get a response rather than dropping
            # the connection with a traceback.
            return self._json(
                {"error": f"internal error: {type(e).__name__}"}, 500)


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>OBSBOT Tiny 3 · Control Deck</title>
<!-- No webfont link on purpose: this panel runs on localhost next to the
     camera and must not stall waiting on a font CDN when the machine is
     offline. The stacks below fall back through the platform's own faces. -->
<style>
  :root{
    --bg:#080a0e;
    --fg:#eaf0f6; --mut:#7e8a9c; --mut2:#5d6878;
    --acc:#2fe0c0; --acc-deep:#13b89c; --acc-soft:rgba(47,224,192,.14);
    --live:#ff5b6e; --warn:#ffb454;
    --line:rgba(255,255,255,.07); --line2:rgba(255,255,255,.12);
    --surface:linear-gradient(168deg,#161b23 0%,#10141b 60%,#0d1118 100%);
    --shadow:0 18px 40px -22px rgba(0,0,0,.85);
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
    --disp:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    --body:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  }
  *{box-sizing:border-box}
  html{-webkit-text-size-adjust:100%}
  body{margin:0;font-family:var(--body);font-size:15px;line-height:1.45;color:var(--fg);
    background:var(--bg);-webkit-user-select:none;user-select:none;min-height:100vh;
    background-image:
      radial-gradient(900px 500px at 12% -8%, rgba(47,224,192,.10), transparent 60%),
      radial-gradient(700px 600px at 110% 0%, rgba(70,120,255,.07), transparent 55%),
      linear-gradient(180deg,#090b10,#070809);
    background-attachment:fixed;}
  /* faint instrument grid overlay */
  body::before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.5;z-index:0;
    background-image:
      linear-gradient(rgba(255,255,255,.018) 1px,transparent 1px),
      linear-gradient(90deg,rgba(255,255,255,.018) 1px,transparent 1px);
    background-size:44px 44px;mask-image:radial-gradient(circle at 50% 30%,#000 30%,transparent 85%);}

  /* ---------- header ---------- */
  header{position:sticky;top:0;z-index:20;display:flex;justify-content:space-between;align-items:center;
    gap:16px;padding:14px clamp(16px,3vw,30px);
    background:rgba(9,11,16,.72);backdrop-filter:blur(14px);
    border-bottom:1px solid var(--line)}
  .brand{display:flex;align-items:center;gap:12px;min-width:0}
  .logo{width:30px;height:30px;border-radius:9px;flex:none;position:relative;
    background:radial-gradient(circle at 50% 40%,var(--acc),var(--acc-deep) 70%);
    box-shadow:0 0 0 1px rgba(47,224,192,.4),0 0 20px -4px var(--acc)}
  .logo::after{content:"";position:absolute;inset:8px;border-radius:50%;background:#070809;
    box-shadow:inset 0 0 0 2px rgba(47,224,192,.7)}
  .brand b{font-family:var(--disp);font-weight:700;font-size:15px;letter-spacing:.14em;text-transform:uppercase}
  .brand small{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.32em;
    color:var(--acc);text-transform:uppercase;margin-top:1px}
  .status{display:flex;align-items:center;gap:9px;font-family:var(--mono);font-size:11px;
    letter-spacing:.1em;text-transform:uppercase;color:var(--mut);
    padding:7px 13px;border:1px solid var(--line);border-radius:999px;background:rgba(255,255,255,.02);
    max-width:46vw;overflow:hidden}
  .status #dev{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--live);box-shadow:0 0 0 0 rgba(255,91,110,.6)}
  .dot.ok{background:var(--acc);animation:pulse 2.4s infinite}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,224,192,.5)}70%{box-shadow:0 0 0 7px rgba(47,224,192,0)}100%{box-shadow:0 0 0 0 rgba(47,224,192,0)}}

  /* ---------- layout ---------- */
  .deck{position:relative;z-index:1;max-width:1200px;margin:0 auto;padding:clamp(16px,2.4vw,28px);
    display:grid;gap:clamp(14px,1.8vw,20px);grid-template-columns:1fr}
  .col-right{display:grid;gap:clamp(14px,1.8vw,20px);align-content:start}
  @media(min-width:1024px){
    .deck{grid-template-columns:minmax(460px,1.12fr) minmax(380px,1fr);align-items:start}
    .col-left{position:sticky;top:80px}
  }

  /* ---------- cards ---------- */
  .card{position:relative;background:var(--surface);border:1px solid var(--line);
    border-radius:18px;padding:clamp(15px,2vw,20px);box-shadow:var(--shadow);
    opacity:0;transform:translateY(10px);animation:rise .5s cubic-bezier(.2,.7,.3,1) forwards}
  .card::before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
    background:linear-gradient(180deg,rgba(255,255,255,.05),transparent 28%)}
  .col-left .card{animation-delay:.02s}
  .col-right .card:nth-child(1){animation-delay:.08s}
  .col-right .card:nth-child(2){animation-delay:.14s}
  .col-right .card:nth-child(3){animation-delay:.2s}
  .col-right .card:nth-child(4){animation-delay:.26s}
  .col-right .card:nth-child(5){animation-delay:.32s}
  @keyframes rise{to{opacity:1;transform:none}}
  @media(prefers-reduced-motion:reduce){.card{animation:none;opacity:1;transform:none}}
  .card h2{margin:0 0 15px;font-family:var(--mono);font-size:11px;font-weight:600;
    letter-spacing:.22em;text-transform:uppercase;color:var(--mut);display:flex;align-items:center;gap:9px}
  .card h2::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--acc);
    box-shadow:0 0 8px var(--acc)}
  .card h2 .spacer{flex:1}
  .hbtn{font-family:var(--mono);font-size:10px;letter-spacing:.18em;text-transform:uppercase;
    color:var(--mut2);background:none;border:1px solid transparent;border-radius:8px;
    padding:4px 9px;cursor:pointer;transition:color .15s,border-color .15s}
  .hbtn:hover{color:var(--acc);border-color:rgba(47,224,192,.3)}

  /* ---------- preview ---------- */
  .preview-wrap{position:relative;background:#000;border-radius:13px;overflow:hidden;aspect-ratio:16/9;
    border:1px solid var(--line2);box-shadow:inset 0 0 0 1px rgba(0,0,0,.5),inset 0 0 60px rgba(0,0,0,.6)}
  .preview-wrap video{width:100%;height:100%;object-fit:contain;display:block;background:#000}
  /* corner viewfinder ticks */
  .preview-wrap::after{content:"";position:absolute;inset:11px;border-radius:7px;pointer-events:none;
    background:
      linear-gradient(var(--acc),var(--acc)) 0 0/14px 2px no-repeat,
      linear-gradient(var(--acc),var(--acc)) 0 0/2px 14px no-repeat,
      linear-gradient(var(--acc),var(--acc)) 100% 0/14px 2px no-repeat,
      linear-gradient(var(--acc),var(--acc)) 100% 0/2px 14px no-repeat,
      linear-gradient(var(--acc),var(--acc)) 0 100%/14px 2px no-repeat,
      linear-gradient(var(--acc),var(--acc)) 0 100%/2px 14px no-repeat,
      linear-gradient(var(--acc),var(--acc)) 100% 100%/14px 2px no-repeat,
      linear-gradient(var(--acc),var(--acc)) 100% 100%/2px 14px no-repeat;
    opacity:.55}
  .preview-msg{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    text-align:center;padding:24px;color:var(--mut);font-size:13px;white-space:pre-line;
    background:radial-gradient(circle at 50% 50%,rgba(10,12,16,.4),rgba(8,10,14,.85))}
  .live{position:absolute;top:12px;left:12px;display:none;align-items:center;gap:7px;
    font-family:var(--mono);font-size:10px;letter-spacing:.2em;font-weight:600;
    padding:5px 10px;border-radius:6px;background:rgba(0,0,0,.5);backdrop-filter:blur(6px);
    border:1px solid rgba(255,91,110,.5);color:#ffd7dc}
  .live::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--live);
    box-shadow:0 0 8px var(--live);animation:pulse 1.6s infinite}
  .live.on{display:flex}
  /* viewfinder HUD: live pan/tilt/zoom telemetry over the video */
  .hud{position:absolute;left:12px;right:12px;bottom:12px;display:flex;gap:8px;pointer-events:none}
  .hud span{display:flex;align-items:baseline;gap:6px;font-family:var(--mono);
    padding:5px 10px;border-radius:6px;background:rgba(0,0,0,.55);backdrop-filter:blur(6px);
    border:1px solid rgba(255,255,255,.1)}
  .hud em{font-style:normal;font-size:9px;letter-spacing:.24em;color:var(--mut)}
  .hud b{font-weight:600;font-size:12px;color:var(--acc);font-variant-numeric:tabular-nums}
  .pvbar{display:flex;gap:9px;align-items:center;margin-top:13px}
  select,.btn{font-family:var(--body);color:var(--fg);background:rgba(255,255,255,.04);
    border:1px solid var(--line2);border-radius:10px;padding:10px 12px;font-size:13px;cursor:pointer}
  select{flex:1;min-width:0;text-overflow:ellipsis;
    background-image:linear-gradient(45deg,transparent 50%,var(--mut) 50%),linear-gradient(135deg,var(--mut) 50%,transparent 50%);
    background-position:calc(100% - 16px) center,calc(100% - 11px) center;
    background-size:5px 5px,5px 5px;background-repeat:no-repeat;-webkit-appearance:none;appearance:none;padding-right:30px}
  select:focus,.btn:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-soft)}

  /* ---------- generic buttons ---------- */
  button{font-family:var(--body);background:rgba(255,255,255,.045);color:var(--fg);
    border:1px solid var(--line2);border-radius:12px;padding:12px;font-size:14px;cursor:pointer;
    touch-action:manipulation;transition:transform .08s,background .15s,border-color .15s,box-shadow .15s}
  button:hover:not(:disabled){border-color:rgba(47,224,192,.4);background:rgba(47,224,192,.06)}
  button:disabled{cursor:not-allowed}
  button:active{transform:scale(.95)}
  .pvtoggle{font-family:var(--mono);letter-spacing:.12em;text-transform:uppercase;font-size:12px;
    padding:10px 16px;color:var(--acc);border-color:rgba(47,224,192,.35);background:var(--acc-soft)}

  /* ---------- PTZ pad ---------- */
  .padwrap{display:flex;justify-content:center;margin:4px 0 14px}
  .pad{display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);
    gap:9px;width:min(240px,74vw);aspect-ratio:1;padding:16px;border-radius:50%;
    background:radial-gradient(circle at 50% 38%,rgba(47,224,192,.07),rgba(255,255,255,.015) 55%,transparent 72%);
    border:1px solid var(--line);box-shadow:inset 0 1px 0 rgba(255,255,255,.05),var(--shadow)}
  .pad button{display:flex;align-items:center;justify-content:center;font-size:19px;border-radius:16px;
    color:var(--acc);background:rgba(255,255,255,.04)}
  .pad button.diag{font-size:13px;color:var(--mut);border-radius:14px}
  .pad button[data-dir]:active:not(:disabled),
  .pad button[data-dir].held:not(:disabled){background:var(--acc);color:#04130f;
    border-color:var(--acc);box-shadow:0 0 22px -4px var(--acc)}
  #center{border-radius:50%;font-size:18px;color:var(--mut);font-family:var(--mono)}
  #center:hover{color:var(--acc)}

  /* ---------- segmented control ---------- */
  .seg{display:flex;gap:0;border:1px solid var(--line2);border-radius:10px;overflow:hidden;
    background:rgba(255,255,255,.03)}
  .seg button{flex:1;border:none;border-radius:0;padding:8px 6px;font-family:var(--mono);
    font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--mut)}
  .seg button+button{border-left:1px solid var(--line)}
  .seg button.on{background:var(--acc-deep);color:#04130f;font-weight:600}
  .speedrow{display:grid;grid-template-columns:auto 1fr;gap:11px;align-items:center;margin-bottom:14px}
  .speedrow .zlab{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--mut)}

  /* ---------- zoom + control rows ---------- */
  .zoomrow{display:grid;grid-template-columns:auto 42px 1fr 44px 42px;gap:11px;align-items:center}
  .zoomrow .zlab{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--mut)}
  .zoomrow button{padding:8px 0;font-size:18px;border-radius:10px}
  .row{display:grid;grid-template-columns:118px 1fr 54px;align-items:center;gap:14px;margin:13px 0}
  .row:first-child{margin-top:4px}
  .row label{font-size:13px;color:var(--mut);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer}
  .row .val{font-family:var(--mono);font-size:13px;text-align:right;color:var(--acc);
    font-variant-numeric:tabular-nums;cursor:text;border-radius:6px;padding:2px 4px}
  .row .val:hover{background:rgba(47,224,192,.08)}
  .row .val input{width:100%;font:inherit;color:inherit;background:rgba(0,0,0,.4);
    border:1px solid var(--acc-deep);border-radius:6px;text-align:right;padding:1px 3px;outline:none}
  .row.menu-row{grid-template-columns:118px 1fr}
  input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:5px;border-radius:99px;
    background:linear-gradient(90deg,var(--acc-deep),var(--acc)) no-repeat,rgba(255,255,255,.1);
    background-size:var(--fill,50%) 100%;cursor:pointer}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:17px;height:17px;border-radius:50%;
    background:#eafff9;border:3px solid var(--acc-deep);box-shadow:0 2px 6px rgba(0,0,0,.5);margin-top:0}
  input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:#eafff9;border:3px solid var(--acc-deep)}
  input[type=range]:disabled{opacity:.4;cursor:not-allowed}
  .row.off label,.row.off .val{opacity:.45}
  .autotag{font-family:var(--mono);font-size:9px;letter-spacing:.14em;color:var(--mut2);
    text-transform:uppercase;margin-left:6px}
  /* toggle rows: switch on the right */
  .toggle-row{grid-template-columns:1fr auto}
  .toggle-row label{color:var(--fg)}
  .toggle input[type=checkbox]{-webkit-appearance:none;appearance:none;width:44px;height:25px;border-radius:99px;
    background:rgba(255,255,255,.12);border:1px solid var(--line2);position:relative;cursor:pointer;transition:background .2s}
  .toggle input[type=checkbox]::after{content:"";position:absolute;top:2px;left:2px;width:19px;height:19px;
    border-radius:50%;background:#cdd6e2;transition:transform .2s,background .2s}
  .toggle input[type=checkbox]:checked{background:var(--acc-deep);border-color:var(--acc)}
  .toggle input[type=checkbox]:checked::after{transform:translateX(19px);background:#04130f}

  /* ---------- AI & lens ---------- */
  .modegrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(84px,1fr));gap:8px;margin-bottom:15px}
  .modegrid button{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
    padding:11px 4px;border-radius:10px;color:var(--mut)}
  .modegrid button.on{color:#04130f;background:var(--acc);border-color:var(--acc);font-weight:600;
    box-shadow:0 0 18px -6px var(--acc)}
  .ainote{display:none;margin-top:13px;padding:9px 12px;border-radius:9px;text-align:center;
    font-family:var(--mono);font-size:10px;letter-spacing:.12em;text-transform:uppercase;
    color:var(--acc);background:var(--acc-soft);border:1px dashed rgba(47,224,192,.35)}
  .ai-live .ainote{display:block}
  .ai-live .padwrap,.ai-live .speedrow{opacity:.35;pointer-events:none}
  /* ⌂ keeps taking clicks while tracking so it can explain the lockout rather
     than sitting there inert; the direction buttons stay fully blocked. */
  .ai-live .padwrap #center{pointer-events:auto;cursor:pointer}
  /* the buttons also carry a real disabled attribute while tracking steers */
  .pad button:disabled,#speedseg button:disabled{cursor:not-allowed}
  /* an XU toggle whose readback was not confirmed shows as unknown, not off */
  .toggle-row.unknown label::after{content:' · unknown';color:var(--warn);
    font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase}
  .toggle-row.unknown .toggle{opacity:.55}

  /* ---------- presets ---------- */
  .presets{display:grid;grid-template-columns:repeat(4,1fr);gap:11px}
  .presets .slot{display:grid;gap:7px;text-align:center}
  .presets .go{font-family:var(--mono);font-size:13px;letter-spacing:.04em;padding:15px 4px;font-weight:600;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .presets .go.filled{color:var(--acc);border-color:rgba(47,224,192,.4);background:var(--acc-soft)}
  .presets .save{font-family:var(--mono);font-size:10px;letter-spacing:.16em;text-transform:uppercase;
    padding:7px 0;color:var(--mut)}
  .presets .pname{font-family:var(--mono);font-size:9px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--mut2);border:none;background:none;padding:2px 0;cursor:text}
  .presets .pname:hover{color:var(--acc)}
  /* an empty slot's label is a span, not a button — no hover affordance */
  .presets .pname-empty{cursor:default;display:block;text-align:center}
  .presets .pname-empty:hover{color:var(--mut2)}
  .presets input.pname{color:var(--fg);background:rgba(0,0,0,.4);border:1px solid var(--acc-deep);
    border-radius:6px;text-align:center;outline:none;text-transform:none;letter-spacing:.05em;font-size:11px;width:100%}
  @media(max-width:380px){.presets{grid-template-columns:repeat(2,1fr)}}

  /* ---------- keyboard hint ---------- */
  .kbd{margin-top:13px;text-align:center;font-family:var(--mono);font-size:10px;
    letter-spacing:.08em;color:var(--mut2);line-height:2}
  .kbd b{font-weight:500;color:var(--mut);background:rgba(255,255,255,.05);
    border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:1px 6px;margin:0 1px}
  @media(hover:none){.kbd{display:none}}

  /* ---------- toast ---------- */
  #toast{position:fixed;left:50%;bottom:22px;transform:translate(-50%,80px);z-index:50;
    font-family:var(--mono);font-size:12px;letter-spacing:.06em;max-width:88vw;
    padding:10px 18px;border-radius:10px;background:rgba(14,17,23,.95);border:1px solid var(--line2);
    color:var(--fg);box-shadow:var(--shadow);opacity:0;transition:transform .25s,opacity .25s;pointer-events:none}
  #toast.show{transform:translate(-50%,0);opacity:1}
  #toast.err{border-color:rgba(255,91,110,.5);color:#ffd7dc}
  #toast.okc{border-color:rgba(47,224,192,.45);color:#d8fff6}
</style></head>
<body>
<header>
  <div class="brand">
    <div class="logo"></div>
    <div><b>OBSBOT&nbsp;Tiny&nbsp;3</b><small>Control Deck</small></div>
  </div>
  <span class="status"><span class="dot" id="dot"></span><span id="dev">connecting…</span></span>
</header>
<main class="deck">
  <section class="col-left">
    <div class="card">
      <h2>Live Preview</h2>
      <div class="preview-wrap">
        <video id="preview" autoplay playsinline muted></video>
        <div class="live" id="live">LIVE</div>
        <div class="hud">
          <span><em>PAN</em><b id="t_pan">–</b></span>
          <span><em>TILT</em><b id="t_tilt">–</b></span>
          <span><em>ZOOM</em><b id="t_zoom">–</b></span>
        </div>
        <div class="preview-msg" id="pvmsg">Starting preview…</div>
      </div>
      <div class="pvbar">
        <select id="camsel"></select>
        <button id="pvtoggle" class="pvtoggle">Stop</button>
      </div>
    </div>
  </section>

  <section class="col-right">
    <div class="card" id="ptzcard">
      <h2>Pan · Tilt · Zoom</h2>
      <div class="padwrap">
        <div class="pad">
          <button data-dir="ul" class="diag" aria-label="Pan left and tilt up">◤</button>
          <button data-dir="up" aria-label="Tilt up">▲</button>
          <button data-dir="ur" class="diag" aria-label="Pan right and tilt up">◥</button>
          <button data-dir="left" aria-label="Pan left">◄</button>
          <button id="center" title="Re-center pan/tilt" aria-label="Re-center pan and tilt">⌂</button>
          <button data-dir="right" aria-label="Pan right">►</button>
          <button data-dir="dl" class="diag" aria-label="Pan left and tilt down">◣</button>
          <button data-dir="down" aria-label="Tilt down">▼</button>
          <button data-dir="dr" class="diag" aria-label="Pan right and tilt down">◢</button>
        </div>
      </div>
      <div class="speedrow">
        <span class="zlab">Speed</span>
        <div class="seg" id="speedseg">
          <button data-spd=".25">Slow</button>
          <button data-spd=".6" class="on">Normal</button>
          <button data-spd="1">Fast</button>
        </div>
      </div>
      <div class="zoomrow">
        <span class="zlab">Zoom</span>
        <button data-zoom="-1" title="Hold to zoom out" aria-label="Zoom out">−</button>
        <input type="range" id="zoom"><span class="val" id="zoom_v">–</span>
        <button data-zoom="1" title="Hold to zoom in" aria-label="Zoom in">+</button>
      </div>
      <div class="kbd">
        <b>←↑↓→</b> move · <b>+</b><b>−</b> zoom · <b>C</b> center · <b>1</b>–<b>4</b> preset · <b>⇧1</b>–<b>4</b> save
      </div>
      <div class="ainote">AI tracking is steering — set it to OFF for manual moves</div>
    </div>

    <div class="card" id="xucard" style="display:none">
      <h2>AI Tracking &amp; Lens</h2>
      <div class="modegrid" id="aimodes"></div>
      <div class="row menu-row"><label>Field of view</label><div class="seg" id="fovseg"></div></div>
      <div id="xutoggles"></div>
    </div>

    <div class="card">
      <h2>Presets</h2>
      <div class="presets" id="presets"></div>
    </div>

    <div class="card">
      <h2>Focus &amp; Exposure <span class="spacer"></span>
        <button class="hbtn" data-reset="focusexp" title="Reset this section to camera defaults">reset</button></h2>
      <div id="focusexp"></div>
    </div>

    <div class="card">
      <h2>Color &amp; Image <span class="spacer"></span>
        <button class="hbtn" data-reset="image" title="Reset this section to camera defaults">reset</button></h2>
      <div id="image"></div>
    </div>
  </section>
</main>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);
async function api(p,b){
  const r=await fetch(p,{method:b?'POST':'GET',
    headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined});
  const j=await r.json();
  if(!r.ok||j.error) throw new Error(j.error||('HTTP '+r.status));
  return j;
}
let ST={}, PRE={}, XU=null;
const aiActive=()=>XU&&XU.ai!=='off';
const GROUPS={focusexp:['focus_auto','focus','auto_exposure','exposure'],
              image:['wb_auto','wb_temp','brightness','contrast','saturation','hue','gain','sharpness','backlight']};

/* ---- toast feedback ---- */
let toastT=null;
function toast(msg,cls){ const t=$('#toast'); t.textContent=msg;
  t.className='show '+(cls||''); clearTimeout(toastT);
  toastT=setTimeout(()=>t.className='',2200); }
const oops=e=>toast(e.message||String(e),'err');

/* ---- live preview via the browser's own camera access (getUserMedia) ---- */
let stream=null;
const vid=$('#preview'), pvmsg=$('#pvmsg'), camsel=$('#camsel'), pvtoggle=$('#pvtoggle'), live=$('#live');
function pvShow(msg){ pvmsg.textContent=msg||''; pvmsg.style.display=msg?'flex':'none';
  if(msg) live.classList.remove('on'); }

async function listCams(selectId){
  if(!navigator.mediaDevices?.enumerateDevices) return;
  const devs=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==='videoinput');
  camsel.innerHTML='';
  devs.forEach((d,i)=>{ const o=document.createElement('option');
    o.value=d.deviceId; o.textContent=d.label||('Camera '+(i+1)); camsel.append(o); });
  // prefer the OBSBOT if we can see labels
  const ob=devs.find(d=>/obsbot/i.test(d.label));
  if(selectId) camsel.value=selectId;
  else if(ob) camsel.value=ob.deviceId;
}

async function startPreview(deviceId){
  if(!window.isSecureContext && location.hostname!=='localhost' && location.hostname!=='127.0.0.1'){
    pvShow('Live preview needs http://localhost or HTTPS.\nControls still work over the network.');
    pvtoggle.textContent='Start'; return;
  }
  if(!navigator.mediaDevices?.getUserMedia){ pvShow('This browser can’t show a camera preview.'); return; }
  pvShow('Starting preview…');
  try{
    if(stream) stream.getTracks().forEach(t=>t.stop());
    stream=await navigator.mediaDevices.getUserMedia({
      video: deviceId?{deviceId:{exact:deviceId}}:{ width:{ideal:1280} }, audio:false });
    vid.srcObject=stream; pvShow(''); pvtoggle.textContent='Stop'; live.classList.add('on');
    await listCams(stream.getVideoTracks()[0]?.getSettings().deviceId);
  }catch(e){
    pvShow('Camera busy or blocked: '+e.name+
      '.\nClose other apps using the camera, or allow camera access, then press Start.');
    pvtoggle.textContent='Start';
  }
}
function stopPreview(){ if(stream){stream.getTracks().forEach(t=>t.stop());stream=null;}
  vid.srcObject=null; pvShow('Preview stopped.'); pvtoggle.textContent='Start'; }
pvtoggle.onclick=()=> (stream?stopPreview():startPreview(camsel.value));
camsel.onchange=()=> startPreview(camsel.value);

/* ---- PTZ pad: hold to move (pointer or arrow keys), speed from segmented.
   The Tiny 3 never reports back positions reached via the pan/tilt SPEED
   controls, which would silently corrupt the HUD and saved presets — so we
   step the ABSOLUTE pan/tilt targets instead; the camera glides to each. ---- */
let speedFrac=.6;
document.querySelectorAll('#speedseg button').forEach(b=>{
  b.onclick=()=>{ speedFrac=+b.dataset.spd;
    document.querySelectorAll('#speedseg button').forEach(x=>x.classList.toggle('on',x===b)); };
});
const DIRS={up:[0,1],down:[0,-1],left:[-1,0],right:[1,0],
            ul:[-1,1],ur:[1,1],dl:[-1,-1],dr:[1,-1]};
let ptTimer=null, ptDir=[0,0];
const TARGET={pan:null,tilt:null};      // last position we commanded
function ptzTick(){
  // Tracking can be switched on from the remote or the OBSBOT app mid-hold; the
  // guard in move() only runs at pointerdown, so re-check every tick or this
  // timer keeps writing absolute targets and fights the tracker forever.
  // Caveat: this can only see state the poll has fetched, and the poll is
  // suppressed while an arrow key is held (held.size), so during a *keyboard*
  // hold the news arrives on key release, when move() re-guards anyway.
  if(aiActive()){ move(0,0); return; }
  const stepsPerTick=speedFrac===1?7:speedFrac===.6?3:1;
  [['pan',ptDir[0]],['tilt',ptDir[1]]].forEach(([k,d])=>{
    if(!d)return; const c=ST[k]; if(!c)return;
    if(TARGET[k]==null) TARGET[k]=c.value??0;
    const v=Math.max(c.min,Math.min(c.max,TARGET[k]+d*stepsPerTick*(c.step||3600)));
    if(v!==TARGET[k]){ TARGET[k]=v; c.value=v; setKey(k,v); updateHUD(); }
  });
}
function move(px,ty){ // px,ty in -1..1; (0,0) stops
  if(aiActive()&&(px||ty)){ toast('AI tracking is steering — set it to Off first','err'); return; }
  ptDir=[px,ty];
  document.querySelectorAll('.pad button[data-dir]').forEach(b=>{
    const [bx,by]=DIRS[b.dataset.dir]; b.classList.toggle('held',!!(px||ty)&&bx===px&&by===ty); });
  if(px||ty){ if(!ptTimer){ ptzTick(); ptTimer=setInterval(ptzTick,150); } }
  else if(ptTimer){ clearInterval(ptTimer); ptTimer=null; }
}
document.querySelectorAll('.pad button[data-dir]').forEach(b=>{
  const [px,ty]=DIRS[b.dataset.dir];
  const start=e=>{e.preventDefault();move(px,ty);};
  const stop =e=>{e.preventDefault();move(0,0);};
  b.addEventListener('pointerdown',start);
  b.addEventListener('pointerup',stop);
  b.addEventListener('pointerleave',stop);
  b.addEventListener('pointercancel',stop);
  // These buttons are named for assistive tech, so they must also work from the
  // keyboard: hold Enter/Space to move, release to stop, mirroring the pointer.
  b.addEventListener('keydown',e=>{
    if(e.key!=='Enter'&&e.key!==' ')return;
    e.preventDefault(); if(!e.repeat) move(px,ty); });
  b.addEventListener('keyup',e=>{
    if(e.key!=='Enter'&&e.key!==' ')return;
    e.preventDefault(); move(0,0); });
  b.addEventListener('blur',()=>{ if(ptTimer) move(0,0); });
});
// A pointerup anywhere ends a hold: releasing off the button (or over a element
// that swallowed the event) would otherwise strand the repeat timer.
document.addEventListener('pointerup',()=>{ if(ptTimer) move(0,0); });
window.addEventListener('blur',()=>{ if(ptTimer) move(0,0); });
// Shared by the ⌂ button and the C key. Routing the key through this function
// rather than $('#center').click() keeps the two paths independent of whether
// the button happens to be disabled, focusable or hit-testable at the time —
// the guard lives in one place and both callers get the same notice.
function recenter(){
  if(aiActive()) return toast('AI tracking is steering — set it to Off first','err');
  api('/api/center',{}).then(refresh).catch(oops); }
$('#center').onclick=recenter;

function setKey(key,value){ return api('/api/set',{key,value}).catch(oops); }

function fill(inp){ const r=(inp.max-inp.min)||1;
  inp.style.setProperty('--fill',(100*(inp.value-inp.min)/r)+'%'); }

/* ---- control rows ---- */
let dragging=false;
document.addEventListener('pointerup',()=>dragging=false);

function sliderRow(c){
  const wrap=document.createElement('div'); wrap.className='row slider-row'+(c.inactive?' off':'');
  const lab=document.createElement('label'); lab.textContent=c.label;
  lab.title='Double-click to reset to default ('+c.default+')';
  if(c.inactive){ const t=document.createElement('span'); t.className='autotag'; t.textContent='auto'; lab.append(t); }
  const inp=document.createElement('input'); inp.type='range';
  inp.min=c.min; inp.max=c.max; inp.step=c.step||1; inp.value=c.value??c.default;
  inp.disabled=!!c.inactive;
  const val=document.createElement('span'); val.className='val'; val.textContent=inp.value;
  val.title='Click to type a value';
  fill(inp);
  inp.addEventListener('pointerdown',()=>dragging=true);
  inp.oninput=()=>{val.textContent=inp.value; fill(inp);};
  inp.onchange=()=>setKey(c.key,+inp.value);
  lab.ondblclick=()=>{ if(c.inactive) return;
    inp.value=c.default; val.textContent=c.default; fill(inp); setKey(c.key,c.default); };
  val.onclick=()=>{ if(c.inactive||val.querySelector('input')) return;
    const box=document.createElement('input'); box.type='number';
    box.min=c.min; box.max=c.max; box.step=c.step||1; box.value=inp.value;
    val.textContent=''; val.append(box); box.focus(); box.select();
    const commit=ok=>{ const v=Math.max(c.min,Math.min(c.max,+box.value||0));
      val.textContent=ok?v:inp.value;
      if(ok&&v!=+inp.value){ inp.value=v; fill(inp); setKey(c.key,v); } };
    box.onkeydown=e=>{ if(e.key==='Enter') box.blur();
      if(e.key==='Escape'){ box.onblur=null; commit(false); } e.stopPropagation(); };
    box.onblur=()=>commit(true); };
  wrap.append(lab,inp,val); return wrap;
}
function toggleRow(c){
  const wrap=document.createElement('div'); wrap.className='row toggle-row';
  const lab=document.createElement('label'); lab.textContent=c.label;
  const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=!!c.value;
  cb.setAttribute('aria-label',c.label);
  cb.onchange=()=>setKey(c.key,cb.checked?1:0).then(refresh);
  const t=document.createElement('div'); t.className='toggle'; t.append(cb);
  wrap.append(lab,t); return wrap;
}
function menuRow(c){
  const wrap=document.createElement('div'); wrap.className='row menu-row';
  const lab=document.createElement('label'); lab.textContent=c.label;
  const seg=document.createElement('div'); seg.className='seg';
  Object.entries(c.menu||{}).forEach(([v,name])=>{
    const b=document.createElement('button');
    b.textContent=name.replace(/\s*Mode$/i,'').replace(/Aperture Priority/i,'Aperture');
    b.title=name;
    if(+v===c.value) b.classList.add('on');
    b.onclick=()=>setKey(c.key,+v).then(refresh);
    seg.append(b);
  });
  wrap.append(lab,seg); return wrap;
}
function row(c){                       // V4L2 types: 2=boolean, 3=menu
  if(c.type===2) return toggleRow(c);
  if(c.type===3&&c.menu&&Object.keys(c.menu).length) return menuRow(c);
  return sliderRow(c);
}

function renderGroup(target,keys){
  const el=$('#'+target); el.innerHTML='';
  keys.forEach(k=>{ const c=ST[k]; if(c){ c.key=k; el.append(row(c)); }});
}
document.querySelectorAll('[data-reset]').forEach(b=>{
  b.onclick=async()=>{ const g=b.dataset.reset;
    for(const k of GROUPS[g]){ const c=ST[k];
      if(c&&!c.inactive&&c.value!==c.default){ try{ await api('/api/set',{key:k,value:c.default}); }catch(e){} } }
    toast('Section reset to defaults','okc'); refresh(); };
});

/* ---- zoom: slider + hold-to-repeat step buttons ---- */
function zoomStep(dir){ const c=ST.zoom; if(!c)return;
  const zi=$('#zoom');
  let v=Math.max(c.min,Math.min(c.max,(+zi.value)+dir*(c.step*4||4)));
  zi.value=v; $('#zoom_v').textContent=v+'%'; $('#t_zoom').textContent=v+'%'; fill(zi); setKey('zoom',v); }
document.querySelectorAll('[data-zoom]').forEach(b=>{
  const dir=+b.dataset.zoom; let rep=null;
  // Guard re-entry the way move() guards ptTimer. Without it a second
  // activation while the first is still held (mouse-down then Enter, or
  // Enter+Space together) overwrites `rep`, orphaning the first interval —
  // which nothing can then clear, and zoom runs away to its limit on its own.
  const start=e=>{e.preventDefault(); if(rep)return;
    zoomStep(dir); rep=setInterval(()=>zoomStep(dir),160);};
  const stop =()=>{if(rep)clearInterval(rep); rep=null;};
  b.addEventListener('pointerdown',start);
  b.addEventListener('pointerup',stop);
  b.addEventListener('pointerleave',stop);
  b.addEventListener('pointercancel',stop);
  // keyboard equivalents, and a global release so a repeat can never strand
  b.addEventListener('keydown',e=>{
    if(e.key!=='Enter'&&e.key!==' ')return;
    e.preventDefault(); if(!e.repeat) start(e); });
  b.addEventListener('keyup',e=>{
    if(e.key!=='Enter'&&e.key!==' ')return;
    e.preventDefault(); stop(); });
  b.addEventListener('blur',stop);
  document.addEventListener('pointerup',stop);
  window.addEventListener('blur',stop);
});
$('#zoom').addEventListener('pointerdown',()=>dragging=true);
$('#zoom').onchange=()=>{ $('#zoom_v').textContent=$('#zoom').value+'%'; setKey('zoom',+$('#zoom').value); };
$('#zoom').oninput =()=>{ $('#zoom_v').textContent=$('#zoom').value+'%';
  $('#t_zoom').textContent=$('#zoom').value+'%'; fill($('#zoom')); };

/* ---- AI tracking & lens (vendor XU) ---- */
const AI_LABELS={off:'Off',normal:'Human',upper:'Upper body',closeup:'Close-up',
                 headless:'Headless',lower:'Lower body',group:'Group'};
const FOV_LABELS={wide:'Wide 86°',medium:'Med 78°',narrow:'Narrow 65°'};
function xuSet(feature,value){
  XU[feature]=value; renderXU();       // optimistic — the poll confirms
  return api('/api/xu',{feature,value}).then(r=>{
    // Show what the camera actually reports. Substituting the requested value
    // over an unconfirmed readback would render a failed write as a success.
    if(r.xu){ if(String(r.xu[feature]).startsWith('unknown')) r.xu[feature]=null; XU=r.xu; }
    renderXU(); setTimeout(refresh,1600);   // re-read once the mode change settles
  }).catch(e=>{oops(e);refresh();});
}
function xuToggleRow(label,feature){
  const wrap=document.createElement('div'); wrap.className='row toggle-row';
  const lab=document.createElement('label'); lab.textContent=label;
  const cb=document.createElement('input'); cb.type='checkbox';
  cb.setAttribute('aria-label',label);
  if(XU[feature]==null){        // readback did not confirm — show indeterminate
    cb.indeterminate=true; cb.checked=false; wrap.classList.add('unknown');
    cb.setAttribute('aria-label',label+' (state unknown)');
  } else cb.checked=!!XU[feature];
  cb.onchange=()=>xuSet(feature,cb.checked);
  const t=document.createElement('div'); t.className='toggle'; t.append(cb);
  wrap.append(lab,t); return wrap;
}
function renderXU(){
  const card=$('#xucard');
  // Compute and apply the steering lockout BEFORE any early return: if the XU
  // stops answering, XU goes null and the pad must be handed back, not left
  // disabled with the dimming and the explanatory note both gone.
  const steering=aiActive();
  $('#ptzcard').classList.toggle('ai-live',!!steering);
  // Stop an in-flight hold: once the buttons go un-hit-testable their pointerup
  // may never arrive, which would strand the repeat timer and the .held class.
  if(steering&&ptTimer) move(0,0);
  // #center stays enabled so a click still reaches recenter() and explains
  // itself; the direction and speed buttons are genuinely unavailable.
  document.querySelectorAll('.pad button[data-dir],#speedseg button')
    .forEach(b=>{ b.disabled=!!steering; });
  if(!XU){ card.style.display='none'; return; }
  card.style.display='';
  const grid=$('#aimodes'); grid.innerHTML='';
  (XU.ai_modes||[]).forEach(m=>{
    const b=document.createElement('button'); b.textContent=AI_LABELS[m]||m;
    b.classList.toggle('on',XU.ai===m);
    b.onclick=()=>xuSet('ai',m);
    grid.append(b);
  });
  const seg=$('#fovseg'); seg.innerHTML='';
  (XU.fov_modes||[]).forEach(m=>{
    const b=document.createElement('button'); b.textContent=FOV_LABELS[m]||m;
    b.classList.toggle('on',XU.fov===m);
    b.onclick=()=>xuSet('fov',m);
    seg.append(b);
  });
  const tg=$('#xutoggles'); tg.innerHTML='';
  tg.append(xuToggleRow('HDR','hdr'),xuToggleRow('Face-priority exposure','face_ae'),
            xuToggleRow('Gesture control','gesture'),xuToggleRow('Voice control','voice'));
}

/* ---- presets: go / save / rename ---- */
function renderPresets(){
  const el=$('#presets'); el.innerHTML='';
  for(let i=1;i<=4;i++){
    const slot=document.createElement('div'); slot.className='slot';
    const p=PRE[String(i)];
    const go=document.createElement('button');
    go.textContent=(p&&p.name)||('P'+i);
    go.className='go'+(p?' filled':'');
    go.title=p?'Recall preset '+i+' (key '+i+')':'Empty — press save below';
    go.onclick=()=>gotoPreset(i);
    const save=document.createElement('button'); save.className='save'; save.textContent='save';
    save.title='Save current pan/tilt/zoom/focus here (shift+'+i+')';
    save.onclick=()=>savePreset(i);
    // An empty slot has nothing to rename, so render a plain label rather than
    // a button that looks clickable and does nothing.
    const name=document.createElement(p?'button':'span'); name.className='pname';
    if(!p) name.classList.add('pname-empty');
    name.textContent=p?'rename':'empty';
    if(p) name.title='Rename preset '+i;
    if(p) name.onclick=()=>{
      const box=document.createElement('input'); box.className='pname'; box.maxLength=24;
      box.value=p.name||''; box.placeholder='P'+i;
      name.replaceWith(box); box.focus(); box.select();
      const done=()=>api('/api/preset/rename',{slot:i,name:box.value.trim()})
        .then(refresh).catch(e=>{oops(e);refresh();});
      box.onkeydown=e=>{ if(e.key==='Enter')box.blur(); if(e.key==='Escape'){box.onblur=null;refresh();} e.stopPropagation(); };
      box.onblur=done; };
    slot.append(go,save,name); el.append(slot);
  }
}
function savePreset(i){
  // While tracking steers, the firmware moves the gimbal without updating the
  // reported pan/tilt, so a snapshot taken now records stale coordinates and
  // the preset would recall to the wrong place. Refuse rather than save junk.
  if(aiActive()) return toast('AI tracking is steering — pan/tilt readings are '
    +'stale, set tracking to Off before saving','err');
  api('/api/preset/save',{slot:i})
  .then(()=>{toast('Preset '+i+' saved','okc');refresh();}).catch(oops); }
function gotoPreset(i){ if(!PRE[String(i)]) return toast('Preset '+i+' is empty — shift+'+i+' to save','err');
  if(aiActive()) return toast('AI tracking is steering — set it to Off first','err');
  api('/api/preset/go',{slot:i}).then(r=>{
    const errs=Object.keys(r.errors||{});
    if(errs.length) toast('Recalled with errors: '+errs.join(', '),'err');
    refresh(); }).catch(oops); }

/* ---- keyboard control ---- */
const held=new Set();
const KEYDIR={ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right'};
function heldMove(){
  let px=0,ty=0;
  if(held.has('left'))px-=1; if(held.has('right'))px+=1;
  if(held.has('up'))ty+=1;   if(held.has('down'))ty-=1;
  move(px,ty);
}
function typing(){ const a=document.activeElement;
  return a&&(a.tagName==='INPUT'&&a.type!=='range'||a.tagName==='SELECT'||a.tagName==='TEXTAREA'); }
document.addEventListener('keydown',e=>{
  if(typing())return;
  const d=KEYDIR[e.key];
  if(d){ e.preventDefault(); if(!held.has(d)){held.add(d);heldMove();} return; }
  if(e.repeat)return;
  if(e.key==='+'||e.key==='='){ e.preventDefault(); zoomStep(1); }
  else if(e.key==='-'||e.key==='_'){ e.preventDefault(); zoomStep(-1); }
  else if(e.key.toLowerCase()==='c'&&!e.ctrlKey&&!e.metaKey){ recenter(); }
  // Match on e.code, not e.key: with shift held the character depends on the
  // keyboard layout (!@#$ is US-only), but Digit1..Digit4 is layout-independent.
  // Numpad1..4 is accepted too — e.key used to cover the keypad for free.
  // Ctrl/Cmd are excluded so browser tab-switching shortcuts pass through.
  else if(/^(Digit|Numpad)[1-4]$/.test(e.code)&&!e.ctrlKey&&!e.metaKey){
    const n=+e.code.replace(/\D/g,'');
    if(e.shiftKey) savePreset(n); else gotoPreset(n); }
});
document.addEventListener('keyup',e=>{
  const d=KEYDIR[e.key]; if(d){ held.delete(d); heldMove(); }});
window.addEventListener('blur',()=>{ if(held.size){held.clear();heldMove();} });

/* ---- state refresh + light polling (picks up changes from other apps) ---- */
const deg=v=>v==null?'–':(v/3600).toFixed(1).replace(/\.0$/,'')+'°';
function updateHUD(){
  $('#t_pan').textContent=deg(ST.pan?.value);
  $('#t_tilt').textContent=deg(ST.tilt?.value);
  $('#t_zoom').textContent=ST.zoom?ST.zoom.value+'%':'–';
}
let lastSnap='';
function refresh(){
  return api('/api/state').then(s=>{
    $('#dev').textContent=s.device; $('#dot').classList.add('ok');
    const snap=JSON.stringify([s.controls,s.presets,s.xu]);
    ST=s.controls; PRE=s.presets||{}; XU=s.xu;
    if(!ptTimer){ TARGET.pan=ST.pan?.value; TARGET.tilt=ST.tilt?.value; }
    updateHUD();
    if(snap===lastSnap) return;         // nothing changed → don't rebuild the DOM
    lastSnap=snap;
    if(ST.zoom){ const z=ST.zoom; const zi=$('#zoom');
      zi.min=z.min; zi.max=z.max; zi.step=z.step||1; zi.value=z.value; $('#zoom_v').textContent=z.value+'%'; fill(zi); }
    renderGroup('focusexp',GROUPS.focusexp);
    renderGroup('image',GROUPS.image);
    renderPresets();
    renderXU();
  }).catch(e=>{
    $('#dot').classList.remove('ok');
    $('#dev').textContent='cannot reach server';
  });
}
setInterval(()=>{ if(!dragging&&!typing()&&!held.size&&!document.hidden) refresh(); },2500);
refresh();
startPreview();   // best-effort live preview on load
</script>
</body></html>
"""


def bind_server(host, port):
    """Bind on the requested port; if busy, try the next few, then any free port.

    --port 0 (or an unavailable port) lets the OS pick an open one.
    Returns the started ThreadingHTTPServer (actual port in .server_address).
    """
    candidates = [port] if port == 0 else list(range(port, port + 20)) + [0]
    last_err = None
    for p in candidates:
        try:
            return ThreadingHTTPServer((host, p), Handler)
        except OSError as e:
            last_err = e
            continue
    raise SystemExit(f"Could not bind any port on {host}: {last_err}")


def main():
    ap = argparse.ArgumentParser(description="Local web controller for the OBSBOT Tiny 3")
    ap.add_argument("--device", help="V4L2 node (default: auto-detect OBSBOT)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (use 0.0.0.0 for phone access)")
    ap.add_argument("--port", type=int, default=8080,
                    help="preferred port; if busy the next free one is used (0 = auto)")
    args = ap.parse_args()

    device = args.device or find_obsbot()
    if not device:
        raise SystemExit("No OBSBOT camera found. Pass --device /dev/videoN explicitly.")

    Handler.cam = Camera(device)
    srv = bind_server(args.host, args.port)
    actual_port = srv.server_address[1]
    url = f"http://{'localhost' if args.host in ('127.0.0.1', '0.0.0.0') else args.host}:{actual_port}"
    bar = "=" * (len(url) + 14)
    print(bar)
    if actual_port != args.port and args.port != 0:
        print(f"  NOTE: port {args.port} was busy — using {actual_port} instead.")
    print(f"  OBSBOT Tiny 3 on {device}")
    print(f"  OPEN -> {url}")
    if args.host == "0.0.0.0":
        print("  (also reachable from your LAN/phone at http://<this-pc-ip>:%d)" % actual_port)
    print(bar)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.cam.close()


if __name__ == "__main__":
    main()
