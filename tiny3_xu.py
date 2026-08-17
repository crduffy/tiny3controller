#!/usr/bin/env python3
"""
tiny3_xu.py — OBSBOT Tiny 3 vendor-feature control over the UVC Extension Unit.

The Tiny 3's OBSBOT-specific features (AI tracking, FOV, HDR, face-AE, …) are
not V4L2 controls; they live on a vendor UVC Extension Unit (unit id 2) and are
reached with the UVCIOC_CTRL_QUERY ioctl. Protocol reverse-engineered from the
Tiny 2 (github.com/cgevans/tiny2, github.com/mitchelloharawild/obsbot-tiny-2-control,
github.com/samliddicott/meet4k) and verified on real Tiny 3 hardware 2026-07-02:

  * GET_LEN on selectors 2 and 6 returns 60 — every payload is a 60-byte buffer.
  * Selector 6 is a register RPC, no checksum: SET_CUR [reg, nbytes, values...].
  * GET_CUR on selector 6 returns a 60-byte status block; on the Tiny 3:
    byte 0x04 = FOV (0x00 wide / 0x06 medium / 0x0f narrow), 0x06 = HDR,
    0x07 = face-AE, 0x18 = AI mode major, 0x1C = AI mode minor. All verified.
    (0x18 briefly reads a transient value while a mode change settles.)
  * Selector 2 carries framed "AA" V3 command packets with CRC16 checksums.
    Format cracked by github.com/jcdoll/obsBotRemote (validated on a Tiny 3):
      [0]=0xAA [1]=flag [2:4]=sequence LE [4]=0x0C [5]=0 [6:8]=CRC16(bytes 0..11)
      [8]=0x0A [9]=route [10]=cmdset|((cmdid&3)<<6) [11]=(cmdid>>2)&0xFF
      [12:14]=payload len LE [14:16]=CRC16(bytes 12..15+len, CRC field zeroed)
      [16:]=payload, zero-padded to 60. CRC16: poly 0xA001 reflected, init
      0xFFFF, final complement. Responses arrive on GET_CUR selector 2 with the
      same framing and echo the request sequence.

Verified working on the Tiny 3 (status read-back and/or visible effect):
    FOV      reg 0x04: 0=wide(86°) 1=medium(78°) 2=narrow(65°)  ← SDK mapping
    AI mode  reg 0x16: [0x16, 2, major, minor]; status byte 0x18 reflects the
             major mode and 0x1C the sub-mode. The sub-modes are numbered from
             1 (see AI_MODES) and were confirmed against the camera's actual
             framing on 2026-08-16 -- the firmware echoes any value it is
             given, so a status read-back cannot validate this mapping.
    HDR      reg 0x01: [0x01, 1, 0|1]; status byte 0x06 follows.
    Face-AE  reg 0x03: [0x03, 1, 0|1]; status byte 0x07 follows (default on).
    Voice ctrl reg 0x15: [0x15, 2, cmd, 0|1] per voice command (Hi Tiny, Sleep
             Tiny, Track Me, Unlock Me, Zoom In, Zoom Out, Preset). Extracted
             from the official SDK's cameraSetAudioCtrlStateU in libdev.so.
             Status byte 0x15 is the enabled-commands bitfield, per the
             vendor's REMO_VOICE_*_BIT defines: bit0 HiTiny, bit1 Preset,
             bit2 ZoomIn, bit3 ZoomOut, bit4 Track, bit5 Unlock, bit6 Sleep
             (byte 0x14 = language, 0=zh 1=en).
    Gesture control: framed selector-2 SDK commands (see GESTURE_* below);
             set 0x03 id 0x007C writes a boolean gesture parameter
             (0=master, 1=target-select, 2=zoom, 3=dynamic-zoom, 4=record,
             5=snapshot, 6=rolling, 7=mirror), id 0x007D reads one back.

The selector-6 status block is the SDK's CameraStatus "tiny" struct (dev.hpp,
pack(1)): 6=hdr 7=face_ae 0x11=fov-enum 0x14=voice-lang 0x15=voice-bitfield
0x16:2=voice-zoom 0x18=ai_mode 0x1C=ai_sub_mode 0x26:2=meet-style gesture bits
(NOT the Tiny hand-gesture state — reads 0 even while gestures work).

Usage as CLI:
    python3 tiny3_xu.py status                 # decode + hex-dump selector 6
    python3 tiny3_xu.py ai off|normal|upper|closeup|headshot|lower|group
    python3 tiny3_xu.py fov wide|medium|narrow
    python3 tiny3_xu.py hdr on|off
    python3 tiny3_xu.py face-ae on|off
    python3 tiny3_xu.py gesture on|off         # all hand-gesture controls
    python3 tiny3_xu.py gesture-status         # read gesture params back
    python3 tiny3_xu.py voice on|off           # all "Hi Tiny" voice commands
    python3 tiny3_xu.py raw 16 02 02 00        # SET_CUR selector 6, hex bytes

All of these are soft settings — idempotent, and recoverable from the OBSBOT
app (or a USB re-plug) if something is off.
"""

import argparse
import ctypes
import fcntl
import glob
import os
import time

UVCIOC_CTRL_QUERY = 0xC0107521          # _IOWR('u', 0x21, 16-byte struct)
UVC_SET_CUR, UVC_GET_CUR, UVC_GET_LEN = 0x01, 0x81, 0x85
XU_UNIT = 2                             # vendor XU on the Tiny 3 (same as Tiny 2)
SEL_CONFIG = 6                          # register-RPC selector
SEL_COMMAND = 2                         # framed V3 command selector

# Register 0x16 takes (major, minor). The minor is the vendor's
# RemoAITrackMode_e, which starts at DISABLE=0 and so numbers the portrait
# sub-modes from 1 -- NOT from 0:
#     0 DISABLE  1 ROUTINE  2 UPPER_PART_BODY
#     3 SHOT (close-up)     4 STRIPPER_HEAD (head shot) 5 LOWER_PART_BODY
# We previously numbered them from 0, which shifted every sub-mode down one:
# picking "close-up" gave upper-body framing, "head shot" gave close-up, and so
# on. The firmware accepts and echoes ANY minor byte without validating it, so
# a status read-back confirms whatever was sent and cannot catch the error --
# it only shows up in how the camera actually frames you.
AI_MODES = {                            # -> (major, minor) for register 0x16
    "off":       (0, 0),
    "normal":    (2, 1),   # ROUTINE
    "upper":     (2, 2),   # UPPER_PART_BODY
    "closeup":   (2, 3),   # SHOT
    # STRIPPER_HEAD reads either way in English, and the Tiny 2 projects took
    # it as "strip off the head" -> "headless". On hardware it frames a head
    # shot, i.e. strip down TO the head, so it is named for what it does.
    "headshot":  (2, 4),   # STRIPPER_HEAD
    "lower":     (2, 5),   # LOWER_PART_BODY
    "group":     (1, 0),
    # Unverified on the Tiny 3. Note the vendor's RemoAIMode_e reads
    # DESKTOP=3, WHITEBOARD=4, HANDSTRACK=5, which would make "hand" and
    # "desk" below the wrong way round -- but that same enum calls 2
    # ANIMAL_TRACK while 2 is demonstrably portrait tracking here, so the
    # major byte does not follow it and these are left as found.
    "hand":      (3, 0),
    "whiteboard":(4, 0),
    "desk":      (5, 0),
}
FOV_MODES = {"wide": 0, "medium": 1, "narrow": 2}

# Voice-command ids for register 0x15 (SDK AudioCtrlCmdType) and where each
# lands in the status-byte-0x15 bitfield.
#
# The bit positions are the vendor's REMO_VOICE_*_BIT defines:
#     0 HI_TINY  1 PRESET  2 ZOOM_IN  3 ZOOM_OUT
#     4 TRACE    5 UNTRACE 6 SLEEP
# "sleep" and "preset" were previously mapped to each other's bits. Only the
# per-command read-back was affected -- the UI enables or disables the whole
# set (0x7f), so the swap never showed there, and the camera echoes the byte
# it was given either way. The cmd ids in the first slot are a separate SDK
# enum that this header does not define, so they remain unverified.
VOICE_CMDS = {  # name -> (cmd id, status bit)
    "hi_tiny":  (0, 0),
    "sleep":    (1, 6),
    "track":    (2, 4),
    "unlock":   (3, 5),
    "zoom_in":  (4, 2),
    "zoom_out": (5, 3),
    "preset":   (6, 1),
}

# Gesture-control wire commands for selector 2 (SDK cmd -> V3 wire cmd, from
# jcdoll/obsBotRemote). "Parameters" are the persisted enables; "controls" are
# the per-gesture switches; hand-track covers gimbal-follow of a raised hand.
GESTURE_PARAMS = {  # aiSetGestureParaR -> wire set 0x04 id 0x00D1 (get 0x00D2)
    "master": 0, "target": 1, "zoom": 2, "dynamic_zoom": 3,
    "record": 4, "snapshot": 5, "rolling": 6, "mirror": 7,
}
GESTURE_CONTROLS = {  # per-gesture SDK writes, already wire (set, id)
    "target":         (0x04, 0x00C3),
    "zoom":           (0x04, 0x00C5),
    "record":         (0x04, 0x00C7),
    "dynamic_zoom":   (0x04, 0x00CD),
    "zoom_direction": (0x04, 0x00CF),
}
WIRE_GESTURE_PARAM_SET  = (0x04, 0x00D1)   # SDK 0x03/0x007C
WIRE_GESTURE_PARAM_GET  = (0x04, 0x00D2)   # SDK 0x03/0x007D
WIRE_HAND_TRACK_GIMBAL  = (0x04, 0x009B)   # SDK 0x03/0x0056
WIRE_HAND_TRACK_PARAM   = (0x04, 0x0081)   # SDK 0x03/0x007A (6=pan, 7=pitch)


def crc16(data):
    """CRC16 used by the framed selector-2 packets (0xA001 reflected, ~final)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def make_v3_packet(flag, route, cmd_set, cmd_id, payload, seq):
    """Build a 60-byte framed command packet for selector 2."""
    pkt = bytearray(60)
    pkt[0] = 0xAA
    pkt[1] = flag
    pkt[2] = seq & 0xFF
    pkt[3] = (seq >> 8) & 0xFF
    pkt[4] = 0x0C
    pkt[8] = 0x0A
    pkt[9] = route
    pkt[10] = (cmd_set & 0x3F) | ((cmd_id & 0x03) << 6)
    pkt[11] = (cmd_id >> 2) & 0xFF
    pkt[12] = len(payload) & 0xFF
    pkt[13] = (len(payload) >> 8) & 0xFF
    pkt[16:16 + len(payload)] = bytes(payload)
    hdr = crc16(pkt[0:12])
    pkt[6] = hdr & 0xFF
    pkt[7] = (hdr >> 8) & 0xFF
    body = crc16(pkt[12:12 + len(payload) + 4])   # covers len + zeroed CRC field
    pkt[14] = body & 0xFF
    pkt[15] = (body >> 8) & 0xFF
    return bytes(pkt)


def parse_v3_response(pkt, seq, cmd_set, cmd_id):
    """Return the payload of a framed response, or None if it doesn't match."""
    if len(pkt) < 16 or pkt[0] != 0xAA or (pkt[1] & 0x03) != 0x01:
        return None
    if (pkt[2] | (pkt[3] << 8)) != seq:
        return None
    if (pkt[10] & 0x3F) != cmd_set or ((pkt[10] >> 6) | (pkt[11] << 2)) != cmd_id:
        return None
    n = pkt[12] | (pkt[13] << 8)
    if len(pkt) < 16 + n:
        return None
    return bytes(pkt[16:16 + n])


class _Query(ctypes.Structure):
    # struct uvc_xu_control_query { u8 unit, selector, query; u16 size; u8 *data; }
    _fields_ = [("unit", ctypes.c_uint8), ("selector", ctypes.c_uint8),
                ("query", ctypes.c_uint8), ("size", ctypes.c_uint16),
                ("data", ctypes.c_void_p)]


class Tiny3XU:
    """XU access on an open V4L2 fd. Safe to use while the camera streams."""

    def __init__(self, fd):
        self.fd = fd
        self._seq = int.from_bytes(os.urandom(2), "little") | 1

    def _io(self, selector, query, buf):
        q = _Query(XU_UNIT, selector, query, len(buf),
                   ctypes.cast(buf, ctypes.c_void_p))
        fcntl.ioctl(self.fd, UVCIOC_CTRL_QUERY, q)

    def get_len(self, selector=SEL_CONFIG):
        b = (ctypes.c_uint8 * 2)()
        self._io(selector, UVC_GET_LEN, b)
        return b[0] | (b[1] << 8)

    def get_status(self, selector=SEL_CONFIG):
        n = self.get_len(selector)
        b = (ctypes.c_uint8 * n)()
        self._io(selector, UVC_GET_CUR, b)
        return bytes(b)

    def send(self, payload, selector=SEL_CONFIG):
        """SET_CUR with payload zero-padded to the selector's GET_LEN size."""
        n = self.get_len(selector)
        b = (ctypes.c_uint8 * n)()
        for i, x in enumerate(payload):
            b[i] = x
        self._io(selector, UVC_SET_CUR, b)

    # -- verified features ---------------------------------------------------
    def set_ai_mode(self, name):
        major, minor = AI_MODES[name]
        self.send([0x16, 0x02, major, minor])

    def set_fov(self, name):
        self.send([0x04, 0x01, FOV_MODES[name]])

    def set_hdr(self, on):
        self.send([0x01, 0x01, 1 if on else 0])

    def set_face_ae(self, on):
        self.send([0x03, 0x01, 1 if on else 0])

    def set_voice_cmd(self, name, on):
        self.send([0x15, 0x02, VOICE_CMDS[name][0], 1 if on else 0])

    def set_voice(self, on):
        """Enable/disable every 'Hi Tiny' voice command."""
        for name in VOICE_CMDS:
            self.set_voice_cmd(name, on)

    # -- framed selector-2 commands (gesture control) ------------------------
    def _next_seq(self):
        self._seq = 1 if self._seq >= 0xFFFF else self._seq + 1
        return self._seq

    def _command(self, cmd_set, cmd_id, payload):
        seq = self._next_seq()
        pkt = make_v3_packet(0x21 if payload else 0x01, 0x04,
                             cmd_set, cmd_id, payload, seq)
        self.send(pkt, selector=SEL_COMMAND)
        return seq

    def set_gesture_param(self, name, on):
        pid = GESTURE_PARAMS[name]
        self._command(*WIRE_GESTURE_PARAM_SET,
                      [pid & 0xFF, (pid >> 8) & 0xFF, 0, 0, 1 if on else 0])

    def get_gesture_param(self, name, retries=50):
        """Read one boolean gesture parameter back, or None on no answer."""
        pid = GESTURE_PARAMS[name]
        seq = self._command(*WIRE_GESTURE_PARAM_GET,
                            [pid & 0xFF, (pid >> 8) & 0xFF, 0, 0])
        req = bytes([pid & 0xFF, (pid >> 8) & 0xFF, 0, 0])
        for attempt in range(retries):
            if attempt:
                time.sleep(0.02)
            try:
                resp = self.get_status(SEL_COMMAND)
            except OSError:
                continue
            payload = parse_v3_response(resp, seq, *WIRE_GESTURE_PARAM_GET)
            # the camera briefly echoes the request before answering
            if payload is None or payload == req:
                continue
            if payload and payload[0] in (0, 1):
                return bool(payload[0])
        return None

    def set_gesture(self, on):
        """Toggle all hand-gesture recognition (mirrors the official app).

        Order matters (from jcdoll/obsBotRemote): per-gesture controls and
        parameters first, the master parameter last, so the master switch
        lands on a consistent config.
        """
        for wire in GESTURE_CONTROLS.values():
            self._command(*wire, [1 if on else 0])
        self._command(*WIRE_HAND_TRACK_GIMBAL, [1 if on else 0])
        for axis in (6, 7):                       # hand-track pan / pitch
            self._command(*WIRE_HAND_TRACK_PARAM,
                          [axis, 0, 0, 0, 1 if on else 0])
        for name, pid in GESTURE_PARAMS.items():
            if pid != 0:
                self.set_gesture_param(name, on)
        self.set_gesture_param("master", on)

    def decode_status(self):
        s = self.get_status()
        ai = {v: k for k, v in AI_MODES.items()}
        fov = {0x00: "wide", 0x06: "medium", 0x0F: "narrow"}
        major, minor = s[0x18], s[0x1C]
        return {
            "fov_raw": s[0x04],
            # Do not fall back to "wide": byte 0x04 passes through intermediate
            # values while the lens is changing (0x0a has been observed between
            # settled states), and defaulting would report a confident, wrong
            # answer. An unrecognised value is reported as unknown so the UI
            # highlights nothing rather than the wrong button.
            "fov": fov.get(s[0x04], f"unknown(0x{s[0x04]:02x})"),
            "hdr": bool(s[0x06]),
            "face_ae": bool(s[0x07]),
            "ai_major": major,
            "ai_mode": ai.get((major, minor), f"unknown({major},{minor})"),
            "voice_raw": s[0x15],
            "voice": bool(s[0x15]),
            "voice_cmds": {name: bool(s[0x15] >> bit & 1)
                           for name, (_, bit) in VOICE_CMDS.items()},
            "voice_lang": {0: "zh", 1: "en"}.get(s[0x14], str(s[0x14])),
            "raw": s,
        }


def find_obsbot():
    for node in sorted(glob.glob("/dev/video*"),
                       key=lambda p: int("".join(filter(str.isdigit, p)) or 0)):
        try:
            with open(f"/sys/class/video4linux/{os.path.basename(node)}/name") as f:
                if "obsbot" not in f.read().lower():
                    continue
            fd = os.open(node, os.O_RDWR)
        except OSError:
            continue
        try:
            Tiny3XU(fd).get_len()
            os.close(fd)
            return node            # first node whose XU answers GET_LEN
        except OSError:
            os.close(fd)
    return None


def main():
    ap = argparse.ArgumentParser(description="OBSBOT Tiny 3 vendor (XU) features")
    ap.add_argument("--device", help="V4L2 node (default: auto-detect)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("ai").add_argument("mode", choices=AI_MODES)
    sub.add_parser("fov").add_argument("mode", choices=FOV_MODES)
    sub.add_parser("hdr").add_argument("mode", choices=["on", "off"])
    sub.add_parser("face-ae").add_argument("mode", choices=["on", "off"])
    sub.add_parser("gesture").add_argument("mode", choices=["on", "off"])
    sub.add_parser("gesture-status")
    sub.add_parser("voice").add_argument("mode", choices=["on", "off"])
    sub.add_parser("raw").add_argument("bytes", nargs="+",
                                       help="hex bytes for selector 6 SET_CUR")
    args = ap.parse_args()

    device = args.device or find_obsbot()
    if not device:
        raise SystemExit("No OBSBOT camera found. Pass --device /dev/videoN.")
    fd = os.open(device, os.O_RDWR)
    xu = Tiny3XU(fd)
    try:
        if args.cmd == "status":
            st = xu.decode_status()
            print(f"device   : {device}")
            print(f"ai mode  : {st['ai_mode']} (major={st['ai_major']})")
            print(f"fov      : {st['fov']} (raw 0x{st['fov_raw']:02x})")
            print(f"hdr      : {'on' if st['hdr'] else 'off'}")
            print(f"face-ae  : {'on' if st['face_ae'] else 'off'}")
            print(f"voice    : {'on' if st['voice'] else 'off'} "
                  f"(raw 0x{st['voice_raw']:02x}, lang {st['voice_lang']})")
            raw = st["raw"]
            for off in range(0, len(raw), 16):
                print(f"  {off:04x}  " + " ".join(f"{x:02x}" for x in raw[off:off+16]))
        elif args.cmd == "ai":
            xu.set_ai_mode(args.mode)
            print(f"AI tracking -> {args.mode}")
        elif args.cmd == "fov":
            xu.set_fov(args.mode)
            print(f"FOV -> {args.mode}")
        elif args.cmd == "hdr":
            xu.set_hdr(args.mode == "on")
            print(f"HDR -> {args.mode}")
        elif args.cmd == "face-ae":
            xu.set_face_ae(args.mode == "on")
            print(f"Face-AE -> {args.mode}")
        elif args.cmd == "gesture":
            xu.set_gesture(args.mode == "on")
            state = xu.get_gesture_param("master")
            print(f"Gesture control -> {args.mode} "
                  f"(readback: {'?' if state is None else 'on' if state else 'off'})")
        elif args.cmd == "gesture-status":
            for name in GESTURE_PARAMS:
                state = xu.get_gesture_param(name)
                print(f"{name:14s}: "
                      f"{'no answer' if state is None else 'on' if state else 'off'}")
        elif args.cmd == "voice":
            xu.set_voice(args.mode == "on")
            print(f"Voice control -> {args.mode} "
                  f"(status 0x{xu.decode_status()['voice_raw']:02x})")
        elif args.cmd == "raw":
            payload = [int(x, 16) for x in args.bytes]
            xu.send(payload)
            print("sent:", " ".join(f"{x:02x}" for x in payload))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
