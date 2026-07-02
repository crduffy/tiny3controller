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
import fcntl
import glob
import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    (0x009A0920, "pan_speed",  "Pan speed",      "ptz"),
    (0x009A0921, "tilt_speed", "Tilt speed",     "ptz"),
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


class Camera:
    def __init__(self, device):
        self.device = device
        self.fd = os.open(device, os.O_RDWR)
        # The fd is shared across request-handler threads; serialize all ioctls.
        self._lock = threading.Lock()

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
        buf = bytearray(struct.pack(_CTRL_FMT, cid, int(value)))
        with self._lock:
            fcntl.ioctl(self.fd, VIDIOC_S_CTRL, buf, True)
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
    with open(PRESETS_FILE, "w") as f:
        json.dump(p, f, indent=2)


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
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json({"controls": self.cam.snapshot(),
                        "presets": load_presets(),
                        "device": self.cam.device})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        data = self._read_body()
        try:
            if self.path == "/api/set":
                key = data.get("key")
                cid = ID_BY_KEY.get(key)
                if cid is None:
                    return self._json({"error": f"unknown control {key!r}"}, 400)
                val = self.cam.set(cid, data["value"])
                return self._json({"key": key, "value": val})

            if self.path == "/api/move":
                # continuous pan/tilt via speed; 0 stops
                ps, ts = int(data.get("pan_speed", 0)), int(data.get("tilt_speed", 0))
                if "pan_speed" in ID_BY_KEY:
                    self.cam.set(ID_BY_KEY["pan_speed"], ps)
                if "tilt_speed" in ID_BY_KEY:
                    self.cam.set(ID_BY_KEY["tilt_speed"], ts)
                return self._json({"pan_speed": ps, "tilt_speed": ts})

            if self.path == "/api/center":
                for k in ("pan", "tilt"):
                    self.cam.set(ID_BY_KEY[k], 0)
                return self._json({"ok": True})

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
                        self.cam.set(ID_BY_KEY[k], v)
                        applied[k] = v
                    except OSError as e:
                        errors[k] = str(e)
                return self._json({"slot": slot, "applied": applied, "errors": errors})

            return self._json({"error": "not found"}, 404)
        except (OSError, KeyError, ValueError) as e:
            return self._json({"error": str(e)}, 500)


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>OBSBOT Tiny 3 · Control Deck</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Sora:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#080a0e;
    --fg:#eaf0f6; --mut:#7e8a9c; --mut2:#5d6878;
    --acc:#2fe0c0; --acc-deep:#13b89c; --acc-soft:rgba(47,224,192,.14);
    --live:#ff5b6e; --warn:#ffb454;
    --line:rgba(255,255,255,.07); --line2:rgba(255,255,255,.12);
    --surface:linear-gradient(168deg,#161b23 0%,#10141b 60%,#0d1118 100%);
    --shadow:0 18px 40px -22px rgba(0,0,0,.85);
    --mono:"Chakra Petch",ui-monospace,monospace;
    --disp:"Chakra Petch",system-ui,sans-serif;
    --body:"Sora",system-ui,-apple-system,sans-serif;
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
  button:hover{border-color:rgba(47,224,192,.4);background:rgba(47,224,192,.06)}
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
  .pad button[data-dir]:active,.pad button[data-dir].held{background:var(--acc);color:#04130f;
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
          <button data-dir="ul" class="diag">◤</button>
          <button data-dir="up">▲</button>
          <button data-dir="ur" class="diag">◥</button>
          <button data-dir="left">◄</button>
          <button id="center" title="Re-center pan/tilt">⌂</button>
          <button data-dir="right">►</button>
          <button data-dir="dl" class="diag">◣</button>
          <button data-dir="down">▼</button>
          <button data-dir="dr" class="diag">◢</button>
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
        <button data-zoom="-1" title="Hold to zoom out">−</button>
        <input type="range" id="zoom"><span class="val" id="zoom_v">–</span>
        <button data-zoom="1" title="Hold to zoom in">+</button>
      </div>
      <div class="kbd">
        <b>←↑↓→</b> move · <b>+</b><b>−</b> zoom · <b>C</b> center · <b>1</b>–<b>4</b> preset · <b>⇧1</b>–<b>4</b> save
      </div>
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
let ST={}, PRE={};
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
  const stepsPerTick=speedFrac===1?7:speedFrac===.6?3:1;
  [['pan',ptDir[0]],['tilt',ptDir[1]]].forEach(([k,d])=>{
    if(!d)return; const c=ST[k]; if(!c)return;
    if(TARGET[k]==null) TARGET[k]=c.value??0;
    const v=Math.max(c.min,Math.min(c.max,TARGET[k]+d*stepsPerTick*(c.step||3600)));
    if(v!==TARGET[k]){ TARGET[k]=v; c.value=v; setKey(k,v); updateHUD(); }
  });
}
function move(px,ty){ // px,ty in -1..1; (0,0) stops
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
});
$('#center').onclick=()=>api('/api/center',{}).then(refresh).catch(oops);

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
  zi.value=v; $('#zoom_v').textContent=v; $('#t_zoom').textContent=v+'%'; fill(zi); setKey('zoom',v); }
document.querySelectorAll('[data-zoom]').forEach(b=>{
  const dir=+b.dataset.zoom; let rep=null;
  const start=e=>{e.preventDefault(); zoomStep(dir); rep=setInterval(()=>zoomStep(dir),160);};
  const stop =()=>{clearInterval(rep); rep=null;};
  b.addEventListener('pointerdown',start);
  b.addEventListener('pointerup',stop);
  b.addEventListener('pointerleave',stop);
  b.addEventListener('pointercancel',stop);
});
$('#zoom').addEventListener('pointerdown',()=>dragging=true);
$('#zoom').onchange=()=>{ $('#zoom_v').textContent=$('#zoom').value; setKey('zoom',+$('#zoom').value); };
$('#zoom').oninput =()=>{ $('#zoom_v').textContent=$('#zoom').value;
  $('#t_zoom').textContent=$('#zoom').value+'%'; fill($('#zoom')); };

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
    const name=document.createElement('button'); name.className='pname';
    name.textContent=p?'rename':'empty';
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
function savePreset(i){ api('/api/preset/save',{slot:i})
  .then(()=>{toast('Preset '+i+' saved','okc');refresh();}).catch(oops); }
function gotoPreset(i){ if(!PRE[String(i)]) return toast('Preset '+i+' is empty — shift+'+i+' to save','err');
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
  else if(e.key.toLowerCase()==='c'){ $('#center').click(); }
  else if(/^[1-4]$/.test(e.key)){ gotoPreset(+e.key); }
  else if(e.shiftKey&&/^[!@#$]$/.test(e.key)){ savePreset('!@#$'.indexOf(e.key)+1); }
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
    const snap=JSON.stringify([s.controls,s.presets]);
    ST=s.controls; PRE=s.presets||{};
    if(!ptTimer){ TARGET.pan=ST.pan?.value; TARGET.tilt=ST.tilt?.value; }
    updateHUD();
    if(snap===lastSnap) return;         // nothing changed → don't rebuild the DOM
    lastSnap=snap;
    if(ST.zoom){ const z=ST.zoom; const zi=$('#zoom');
      zi.min=z.min; zi.max=z.max; zi.step=z.step||1; zi.value=z.value; $('#zoom_v').textContent=z.value; fill(zi); }
    renderGroup('focusexp',GROUPS.focusexp);
    renderGroup('image',GROUPS.image);
    renderPresets();
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
