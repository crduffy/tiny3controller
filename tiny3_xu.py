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
  * Selector 2 carries framed "AA 25" packets WITH a checksum (sleep/wake,
    exposure setup). Those are replay-only — do not hand-craft them.

Verified working on the Tiny 3 (status read-back and/or visible effect):
    FOV      reg 0x04: 0=wide(86°) 1=medium(78°) 2=narrow(65°)  ← SDK mapping
    AI mode  reg 0x16: [0x16, 2, major, minor]; disable + normal tracking tested,
             status byte 0x18 reflects the major mode.
    HDR      reg 0x01: [0x01, 1, 0|1]; status byte 0x06 follows.
    Face-AE  reg 0x03: [0x03, 1, 0|1]; status byte 0x07 follows (default on).

Plausible but NOT yet verified on a Tiny 3 (same grammar on Tiny 2/Meet 4K):
    AI modes hand(3), whiteboard(4), desk(5) as the major byte.

Usage as CLI:
    python3 tiny3_xu.py status                 # decode + hex-dump selector 6
    python3 tiny3_xu.py ai off|normal|upper|closeup|headless|lower|group
    python3 tiny3_xu.py fov wide|medium|narrow
    python3 tiny3_xu.py hdr on|off
    python3 tiny3_xu.py face-ae on|off
    python3 tiny3_xu.py raw 16 02 02 00        # SET_CUR selector 6, hex bytes

All of these are soft settings — idempotent, and recoverable from the OBSBOT
app (or a USB re-plug) if something is off.
"""

import argparse
import ctypes
import fcntl
import glob
import os

UVCIOC_CTRL_QUERY = 0xC0107521          # _IOWR('u', 0x21, 16-byte struct)
UVC_SET_CUR, UVC_GET_CUR, UVC_GET_LEN = 0x01, 0x81, 0x85
XU_UNIT = 2                             # vendor XU on the Tiny 3 (same as Tiny 2)
SEL_CONFIG = 6                          # register-RPC selector

AI_MODES = {                            # -> (major, minor) for register 0x16
    "off":       (0, 0),
    "normal":    (2, 0),
    "upper":     (2, 1),
    "closeup":   (2, 2),
    "headless":  (2, 3),
    "lower":     (2, 4),
    "group":     (1, 0),
    "hand":      (3, 0),   # unverified on Tiny 3
    "whiteboard":(4, 0),   # unverified on Tiny 3
    "desk":      (5, 0),   # unverified on Tiny 3
}
FOV_MODES = {"wide": 0, "medium": 1, "narrow": 2}


class _Query(ctypes.Structure):
    # struct uvc_xu_control_query { u8 unit, selector, query; u16 size; u8 *data; }
    _fields_ = [("unit", ctypes.c_uint8), ("selector", ctypes.c_uint8),
                ("query", ctypes.c_uint8), ("size", ctypes.c_uint16),
                ("data", ctypes.c_void_p)]


class Tiny3XU:
    """XU access on an open V4L2 fd. Safe to use while the camera streams."""

    def __init__(self, fd):
        self.fd = fd

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

    def decode_status(self):
        s = self.get_status()
        ai = {v: k for k, v in AI_MODES.items()}
        fov = {0x00: "wide", 0x06: "medium", 0x0F: "narrow"}
        major, minor = s[0x18], s[0x1C]
        return {
            "fov_raw": s[0x04],
            "fov": fov.get(s[0x04], "wide"),
            "hdr": bool(s[0x06]),
            "face_ae": bool(s[0x07]),
            "ai_major": major,
            "ai_mode": ai.get((major, minor), f"unknown({major},{minor})"),
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
        elif args.cmd == "raw":
            payload = [int(x, 16) for x in args.bytes]
            xu.send(payload)
            print("sent:", " ".join(f"{x:02x}" for x in payload))
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
