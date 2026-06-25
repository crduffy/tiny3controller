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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# V4L2 ioctl plumbing
# ---------------------------------------------------------------------------

# _IOC(dir, type, nr, size) on Linux: (dir<<30)|(size<<16)|(type<<8)|nr
_IOC_WRITE, _IOC_READ = 1, 2

def _iowr(nr, size):
    return ((_IOC_READ | _IOC_WRITE) << 30) | (size << 16) | (ord('V') << 8) | nr

VIDIOC_QUERYCTRL = _iowr(36, 68)   # struct v4l2_queryctrl  (68 bytes)
VIDIOC_G_CTRL    = _iowr(27, 8)    # struct v4l2_control    (8 bytes)
VIDIOC_S_CTRL    = _iowr(28, 8)

# struct v4l2_control { __u32 id; __s32 value; }
_CTRL_FMT  = "=Ii"
# struct v4l2_queryctrl { u32 id; u32 type; char name[32]; s32 min,max,step,def;
#                         u32 flags; u32 reserved[2]; }
_QUERY_FMT = "=II32siiiII8x"

# Controls we expose, in display order. (v4l2 id, key, label, group)
CONTROLS = [
    (0x009A0908, "pan",        "Pan",            "ptz"),
    (0x009A0909, "tilt",       "Tilt",           "ptz"),
    (0x009A090D, "zoom",       "Zoom",           "ptz"),
    (0x009A0920, "pan_speed",  "Pan speed",      "ptz"),
    (0x009A0921, "tilt_speed", "Tilt speed",     "ptz"),
    (0x009A090A, "focus",      "Focus",          "focus"),
    (0x009A090C, "focus_auto", "Auto focus",     "focus"),
    (0x009A0901, "auto_exposure", "Auto exposure", "exposure"),
    (0x009A0902, "exposure",   "Exposure time",  "exposure"),
    (0x0098090C, "wb_auto",    "Auto white bal", "color"),
    (0x0098091A, "wb_temp",    "WB temperature", "color"),
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

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def query(self, cid):
        """Return dict(min,max,step,default,type,name,flags) or None if absent."""
        buf = bytearray(struct.pack(_QUERY_FMT, cid, 0, b"", 0, 0, 0, 0, 0))
        try:
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

    def get(self, cid):
        buf = bytearray(struct.pack(_CTRL_FMT, cid, 0))
        try:
            fcntl.ioctl(self.fd, VIDIOC_G_CTRL, buf, True)
        except OSError:
            return None
        _id, val = struct.unpack(_CTRL_FMT, bytes(buf))
        return val

    def set(self, cid, value):
        buf = bytearray(struct.pack(_CTRL_FMT, cid, int(value)))
        fcntl.ioctl(self.fd, VIDIOC_S_CTRL, buf, True)
        return self.get(cid)

    def snapshot(self):
        """Full state: every exposed control with range + current value."""
        out = {}
        for cid, key, label, group in CONTROLS:
            q = self.query(cid)
            if not q:
                continue
            out[key] = {
                "id": cid, "label": label, "group": group,
                "min": q["min"], "max": q["max"], "step": q["step"],
                "default": q["default"], "type": q["type"], "value": self.get(cid),
                "inactive": q["inactive"],
            }
        return out


# ---------------------------------------------------------------------------
# Software presets (Option-1: store/restore values; no XU needed)
# ---------------------------------------------------------------------------

def load_presets():
    try:
        with open(PRESETS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

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
                presets[slot] = snap
                save_presets(presets)
                return self._json({"slot": slot, "saved": snap})

            if self.path == "/api/preset/go":
                slot = str(data.get("slot"))
                presets = load_presets()
                if slot not in presets:
                    return self._json({"error": "empty slot"}, 404)
                saved = presets[slot]
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
<title>OBSBOT Tiny 3 Control</title>
<style>
  :root{--bg:#0f1115;--card:#191c23;--line:#2a2f3a;--fg:#e7ebf2;--mut:#8b93a3;--acc:#4f9cff;}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.4 system-ui,sans-serif;background:var(--bg);color:var(--fg);
       -webkit-user-select:none;user-select:none}
  header{padding:14px 18px;border-bottom:1px solid var(--line);display:flex;
         justify-content:space-between;align-items:center}
  header b{font-size:16px} header span{color:var(--mut);font-size:12px}
  main{max-width:760px;margin:0 auto;padding:16px;display:grid;gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
  .card h2{margin:0 0 12px;font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut)}
  .pad{display:grid;grid-template-columns:repeat(3,72px);grid-template-rows:repeat(3,72px);
       gap:8px;justify-content:center;margin:6px 0 14px}
  .pad button{font-size:26px;border-radius:12px}
  button{background:#222733;color:var(--fg);border:1px solid var(--line);border-radius:10px;
         padding:12px;font-size:15px;cursor:pointer;touch-action:manipulation}
  button:active{background:var(--acc);border-color:var(--acc)}
  .row{display:flex;align-items:center;gap:12px;margin:10px 0}
  .row label{width:130px;color:var(--mut);font-size:13px}
  .row input[type=range]{flex:1}
  .row .val{width:58px;text-align:right;font-variant-numeric:tabular-nums;color:var(--fg)}
  .toggle{display:flex;align-items:center;gap:8px}
  .presets{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
  .presets .slot{display:grid;gap:6px}
  .presets .slot small{text-align:center;color:var(--mut)}
  .presets .save{font-size:12px;padding:6px;background:#1c2330}
  .zoomrow{display:flex;gap:8px;align-items:center}
  .zoomrow button{width:54px;font-size:20px}
</style></head>
<body>
<header><b>OBSBOT&nbsp;Tiny&nbsp;3</b><span id="dev">connecting…</span></header>
<main>
  <div class="card" id="ptzcard">
    <h2>Pan / Tilt / Zoom</h2>
    <div class="pad">
      <span></span>
      <button data-dir="up">▲</button>
      <span></span>
      <button data-dir="left">◄</button>
      <button id="center">＋</button>
      <button data-dir="right">►</button>
      <span></span>
      <button data-dir="down">▼</button>
      <span></span>
    </div>
    <div class="zoomrow">
      <label style="width:130px;color:var(--mut);font-size:13px">Zoom</label>
      <button data-zoom="-1">−</button>
      <input type="range" id="zoom"><span class="val" id="zoom_v">–</span>
      <button data-zoom="1">+</button>
    </div>
  </div>

  <div class="card">
    <h2>Focus &amp; Exposure</h2>
    <div id="focusexp"></div>
  </div>

  <div class="card">
    <h2>Color &amp; Image</h2>
    <div id="image"></div>
  </div>

  <div class="card">
    <h2>Presets</h2>
    <div class="presets" id="presets"></div>
  </div>
</main>
<script>
const $=s=>document.querySelector(s), api=(p,b)=>fetch(p,{method:b?'POST':'GET',
  headers:{'Content-Type':'application/json'},body:b?JSON.stringify(b):undefined}).then(r=>r.json());
let ST={}, PRE={};

const SPEED=40;           // pan/tilt hold speed
function move(ps,ts){ api('/api/move',{pan_speed:ps,tilt_speed:ts}); }
function bindHold(btn,ps,ts){
  const start=e=>{e.preventDefault();move(ps,ts);};
  const stop =e=>{e.preventDefault();move(0,0);};
  btn.addEventListener('pointerdown',start);
  btn.addEventListener('pointerup',stop);
  btn.addEventListener('pointerleave',stop);
  btn.addEventListener('pointercancel',stop);
}
const DIRS={up:[0,1],down:[0,-1],left:[-1,0],right:[1,0]};
document.querySelectorAll('.pad button[data-dir]').forEach(b=>{
  const [px,ty]=DIRS[b.dataset.dir]; bindHold(b,px*SPEED,ty*SPEED);
});
$('#center').onclick=()=>api('/api/center',{}).then(refresh);

function setKey(key,value){ return api('/api/set',{key,value}); }

function sliderRow(c){
  const wrap=document.createElement('div'); wrap.className='row';
  const lab=document.createElement('label'); lab.textContent=c.label;
  const inp=document.createElement('input'); inp.type='range';
  inp.min=c.min; inp.max=c.max; inp.step=c.step||1; inp.value=c.value??c.default;
  inp.disabled=!!c.inactive;
  const val=document.createElement('span'); val.className='val'; val.textContent=inp.value;
  inp.oninput=()=>{val.textContent=inp.value;};
  inp.onchange=()=>setKey(c.key,+inp.value);
  if(c.inactive) wrap.style.opacity=.45;
  wrap.append(lab,inp,val); return wrap;
}
function toggleRow(c){
  const wrap=document.createElement('div'); wrap.className='row';
  const lab=document.createElement('label'); lab.textContent=c.label;
  const cb=document.createElement('input'); cb.type='checkbox'; cb.checked=!!c.value;
  cb.onchange=()=>setKey(c.key,cb.checked?1:0).then(refresh);
  const t=document.createElement('div'); t.className='toggle'; t.append(cb);
  wrap.append(lab,t); return wrap;
}
function row(c){ return c.type===2 ? toggleRow(c) : sliderRow(c); } // 2 == V4L2 boolean

function renderGroup(target,keys){
  const el=$(target); el.innerHTML='';
  keys.forEach(k=>{ const c=ST[k]; if(c){ c.key=k; el.append(row(c)); }});
}

// zoom +/- buttons
document.querySelectorAll('[data-zoom]').forEach(b=>{
  b.onclick=()=>{ const c=ST.zoom; if(!c)return;
    let v=Math.max(c.min,Math.min(c.max,(+$('#zoom').value)+ (+b.dataset.zoom)*(c.step*4||4)));
    $('#zoom').value=v; $('#zoom_v').textContent=v; setKey('zoom',v); };
});
$('#zoom').onchange=()=>{ $('#zoom_v').textContent=$('#zoom').value; setKey('zoom',+$('#zoom').value); };
$('#zoom').oninput =()=>{ $('#zoom_v').textContent=$('#zoom').value; };

function renderPresets(){
  const el=$('#presets'); el.innerHTML='';
  for(let i=1;i<=4;i++){
    const slot=document.createElement('div'); slot.className='slot';
    const go=document.createElement('button'); go.textContent='P'+i;
    const filled=PRE[String(i)]; go.style.opacity=filled?1:.45;
    go.onclick=()=>api('/api/preset/go',{slot:i}).then(refresh);
    const save=document.createElement('button'); save.className='save'; save.textContent='save';
    save.onclick=()=>api('/api/preset/save',{slot:i}).then(()=>refresh());
    const lbl=document.createElement('small'); lbl.textContent=filled?'set':'empty';
    slot.append(go,save,lbl); el.append(slot);
  }
}

function refresh(){
  return api('/api/state').then(s=>{
    ST=s.controls; PRE=s.presets||{}; $('#dev').textContent=s.device;
    if(ST.zoom){ const z=ST.zoom; const zi=$('#zoom');
      zi.min=z.min; zi.max=z.max; zi.step=z.step||1; zi.value=z.value; $('#zoom_v').textContent=z.value; }
    renderGroup('#focusexp',['focus_auto','focus','auto_exposure','exposure']);
    renderGroup('#image',['wb_auto','wb_temp','brightness','contrast','saturation','hue','gain','sharpness','backlight']);
    renderPresets();
  });
}
refresh();
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
    if actual_port != args.port and args.port != 0:
        print(f"Port {args.port} was busy — using {actual_port} instead.")
    print(f"OBSBOT control on {device}")
    print(f"  -> http://{args.host}:{actual_port}"
          + ("   (also reachable from your LAN/phone)" if args.host == "0.0.0.0" else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        Handler.cam.close()


if __name__ == "__main__":
    main()
