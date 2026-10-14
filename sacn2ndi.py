#!/usr/bin/env python3
"""
sacn2ndi - receive sACN (E1.31) from a lighting desk and publish a pixel
matrix as an NDI video source (e.g. for Depence, Resolume, OBS ...).

No third-party Python packages needed. Requires the NDI runtime
(libndi.dylib on macOS - installed by NDI Tools; Processing.NDI.Lib.x64.dll
on Windows; libndi.so on Linux).

Running it opens a settings page in your browser (http://localhost:8090) with
the sACN input, matrix layout, NDI output and a live preview. Settings are
saved to settings.json next to this script.

Default: 10 x 10 RGB pixels, universe 1, address 1 -> NDI source "sACN Matrix",
each LED pixel drawn as a 32 x 32 block (320 x 320 video).

    python3 sacn2ndi.py
    python3 sacn2ndi.py --universe 2 --address 101 --snake   # override saved settings
    python3 sacn2ndi.py --help
"""

import argparse
import ctypes
import ctypes.util
import glob
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "settings.json")  # --settings changes it
UI_FILE = os.path.join(HERE, "sacn2ndi_ui.html")

SACN_PORT = 5568
ACN_ID = b"ASC-E1.17\x00\x00\x00"
VECTOR_ROOT_E131_DATA = 0x00000004
VECTOR_E131_DATA_PACKET = 0x00000002
SOURCE_TIMEOUT = 2.5  # seconds, per E1.31

COLOR_MODES = {
    # name: (channels per pixel, function bytes->(r, g, b))
    "RGB": (3, lambda c: (c[0], c[1], c[2])),
    "RBG": (3, lambda c: (c[0], c[2], c[1])),
    "GRB": (3, lambda c: (c[1], c[0], c[2])),
    "BGR": (3, lambda c: (c[2], c[1], c[0])),
    "RGBW": (4, lambda c: tuple(min(255, c[i] + c[3]) for i in range(3))),
    "W": (1, lambda c: (c[0], c[0], c[0])),
}
CORNERS = ["top-left", "top-right", "bottom-left", "bottom-right"]

DEFAULTS = {
    "name": "sACN Matrix",
    "width": 10,
    "height": 10,
    "universe": 1,
    "address": 1,
    "mode": "RGB",
    "pixels_per_universe": 0,
    "order": "rows",
    "start": "top-left",
    "snake": False,
    "pixel_size": 32,
    "gap": 0,
    "fps": 50.0,
    "master": 1.0,
    "interface": "0.0.0.0",
    "multicast": True,
    "blackout_on_loss": False,
    "machine_name": "",  # NDI machine name; blank = this Mac's network name
}


# --------------------------------------------------------------------------
# sACN receiver
# --------------------------------------------------------------------------

class SacnReceiver:
    """Listens on the given universes. Merges sources: highest priority wins,
    equal priorities are HTP-merged. Stream-terminated sources drop out
    immediately; silent sources drop out after 2.5 s."""

    def __init__(self, universes, interface_ip="0.0.0.0", multicast=True):
        self.universes = set(universes)
        self.lock = threading.Lock()
        # universe -> {cid: (priority, last_seen, data bytes, source name)}
        self.sources = {u: {} for u in universes}
        self.packets = 0
        self.last_merged = {u: bytes(512) for u in universes}
        self.warnings = []

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        self.sock.bind(("", SACN_PORT))
        if multicast:
            # "all interfaces" (0.0.0.0) would only join on the default network
            # port, so join on each interface, localhost included
            if interface_ip == "0.0.0.0":
                ips = [ip for _label, ip in list_interfaces() if ip != "0.0.0.0"] or ["0.0.0.0"]
            else:
                ips = [interface_ip]
            for u in sorted(universes):
                group = f"239.255.{(u >> 8) & 0xFF}.{u & 0xFF}"
                errors = []
                for ip in ips:
                    mreq = socket.inet_aton(group) + socket.inet_aton(ip)
                    try:
                        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                    except OSError as e:
                        errors.append(f"{ip}: {e.strerror or e}")
                if len(errors) == len(ips):
                    self.warnings.append(f"could not join multicast {group} ({'; '.join(errors)}); "
                                         "unicast to this machine will still work")
        self.sock.settimeout(0.5)
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while self.running:
            try:
                pkt, _addr = self.sock.recvfrom(1144)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(pkt)

    def _handle(self, pkt):
        if len(pkt) < 126 or pkt[4:16] != ACN_ID:
            return
        if struct.unpack_from(">I", pkt, 18)[0] != VECTOR_ROOT_E131_DATA:
            return  # e.g. universe discovery
        if struct.unpack_from(">I", pkt, 40)[0] != VECTOR_E131_DATA_PACKET:
            return  # e.g. universe sync
        universe = struct.unpack_from(">H", pkt, 113)[0]
        if universe not in self.universes:
            return
        options = pkt[112]
        if options & 0x80:  # preview data - not for live output
            return
        if pkt[125] != 0x00:  # only null start code (dimmer data)
            return
        cid = bytes(pkt[22:38])
        priority = pkt[108]
        count = struct.unpack_from(">H", pkt, 123)[0] - 1  # minus start code
        data = bytes(pkt[126:126 + max(0, count)]).ljust(512, b"\x00")
        name = pkt[44:108].split(b"\x00", 1)[0].decode("utf-8", "replace")
        with self.lock:
            self.packets += 1
            srcs = self.sources[universe]
            if options & 0x40:  # stream terminated: drop now, black if it was the last
                srcs.pop(cid, None)
                if not srcs:
                    self.last_merged[universe] = bytes(512)
            else:
                srcs[cid] = (priority, time.monotonic(), data, name)

    def snapshot(self, hold_on_loss=True):
        """Return {universe: 512 bytes} merged view, plus active source names."""
        now = time.monotonic()
        out, names = {}, set()
        with self.lock:
            for u, srcs in self.sources.items():
                for cid in [c for c, s in srcs.items() if now - s[1] > SOURCE_TIMEOUT]:
                    del srcs[cid]
                if not srcs:
                    out[u] = self.last_merged[u] if hold_on_loss else bytes(512)
                    continue
                top = max(s[0] for s in srcs.values())
                winners = [s for s in srcs.values() if s[0] == top]
                names.update(s[3] for s in winners)
                if len(winners) == 1:
                    merged = winners[0][2]
                else:
                    merged = bytes(max(vals) for vals in zip(*(w[2] for w in winners)))
                self.last_merged[u] = merged
                out[u] = merged
        return out, sorted(names)

    def close(self):
        self.running = False
        self.sock.close()
        self.thread.join(timeout=1)


# --------------------------------------------------------------------------
# NDI (ctypes binding to the NDI runtime)
# --------------------------------------------------------------------------

class NDIlib_send_create_t(ctypes.Structure):
    _fields_ = [
        ("p_ndi_name", ctypes.c_char_p),
        ("p_groups", ctypes.c_char_p),
        ("clock_video", ctypes.c_bool),
        ("clock_audio", ctypes.c_bool),
    ]


class NDIlib_video_frame_v2_t(ctypes.Structure):
    _fields_ = [
        ("xres", ctypes.c_int),
        ("yres", ctypes.c_int),
        ("FourCC", ctypes.c_uint32),
        ("frame_rate_N", ctypes.c_int),
        ("frame_rate_D", ctypes.c_int),
        ("picture_aspect_ratio", ctypes.c_float),
        ("frame_format_type", ctypes.c_int),
        ("timecode", ctypes.c_int64),
        ("p_data", ctypes.c_void_p),
        ("line_stride_in_bytes", ctypes.c_int),
        ("p_metadata", ctypes.c_char_p),
        ("timestamp", ctypes.c_int64),
    ]


def _fourcc(s):
    return s[0] | (s[1] << 8) | (s[2] << 16) | (s[3] << 24)


FOURCC_BGRA = _fourcc(b"BGRA")
FRAME_FORMAT_PROGRESSIVE = 1
TIMECODE_SYNTHESIZE = 0x7FFFFFFFFFFFFFFF


def default_machine_name():
    """The name other NDI apps on this Mac use (e.g. OLIVERS-MACBOOK-PRO.LOCAL).
    The NDI runtime otherwise uses gethostname(), which on a Mac without a
    HostName set is whatever the router hands out (e.g. MAC.LAN)."""
    if sys.platform == "darwin":
        try:
            local = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                                   text=True, timeout=2).stdout.strip()
            if local:
                return f"{local}.local".upper()
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname().upper()


def configure_ndi_machine_name(machine_name):
    """Point the NDI runtime at a config file (in this folder) that sets the
    machine name, keeping any settings from the user's own NDI config. Must run
    before the runtime is initialised."""
    cfg = {}
    user_dir = os.environ.get("NDI_CONFIG_DIR") or os.path.expanduser("~/.ndi")
    try:
        with open(os.path.join(user_dir, "ndi-config.v1.json")) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.setdefault("ndi", {})["machinename"] = machine_name
    cfg_dir = os.path.join(HERE, ".ndi")
    try:
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "ndi-config.v1.json"), "w") as f:
            json.dump(cfg, f, indent=2)
        os.environ["NDI_CONFIG_DIR"] = cfg_dir
    except OSError as e:
        print(f"warning: could not write NDI config ({e}); source will use the system hostname",
              file=sys.stderr)


def load_ndi():
    candidates = []
    if os.environ.get("NDI_LIB"):
        candidates.append(os.environ["NDI_LIB"])
    if sys.platform == "darwin":
        candidates += [
            "/usr/local/lib/libndi.dylib",
            "/Library/NDI SDK for Apple/lib/macOS/libndi.dylib",
            "/Library/NDI Advanced SDK for Apple/lib/macOS/libndi_advanced.dylib",
            "/opt/homebrew/lib/libndi.dylib",
        ]
        candidates += sorted(glob.glob("/Applications/NDI*.app/Contents/Frameworks/libndi*.dylib"))
    elif sys.platform == "win32":
        for var in ("NDI_RUNTIME_DIR_V6", "NDI_RUNTIME_DIR_V5", "NDI_RUNTIME_DIR_V4"):
            if os.environ.get(var):
                candidates.append(os.path.join(os.environ[var], "Processing.NDI.Lib.x64.dll"))
        candidates.append("Processing.NDI.Lib.x64.dll")
    else:
        candidates += ["libndi.so.6", "libndi.so.5", "libndi.so"]
    found = ctypes.util.find_library("ndi")
    if found:
        candidates.append(found)
    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
        except OSError:
            continue
        lib.NDIlib_initialize.restype = ctypes.c_bool
        lib.NDIlib_version.restype = ctypes.c_char_p
        lib.NDIlib_send_create.restype = ctypes.c_void_p
        lib.NDIlib_send_create.argtypes = [ctypes.POINTER(NDIlib_send_create_t)]
        lib.NDIlib_send_send_video_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(NDIlib_video_frame_v2_t)]
        lib.NDIlib_send_get_no_connections.restype = ctypes.c_int
        lib.NDIlib_send_get_no_connections.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.NDIlib_send_destroy.argtypes = [ctypes.c_void_p]
        if not lib.NDIlib_initialize():
            sys.exit("error: NDIlib_initialize failed (unsupported CPU?)")
        return lib, path
    sys.exit("error: NDI runtime not found. Install NDI Tools (https://ndi.video/tools/) "
             "or point NDI_LIB at libndi.dylib / Processing.NDI.Lib.x64.dll")


class NdiSender:
    def __init__(self, lib, name, width, height, fps):
        self.lib = lib
        self._name = name.encode()
        create = NDIlib_send_create_t(self._name, None, True, False)
        self.instance = lib.NDIlib_send_create(ctypes.byref(create))
        if not self.instance:
            raise RuntimeError(f"could not create NDI source '{name}'. Is sacn2ndi already "
                               "running? Stop the other copy (Ctrl+C in its window, or "
                               "`pkill -f sacn2ndi.py`), or pick another name")

        self.width, self.height = width, height
        self.frame = NDIlib_video_frame_v2_t()
        self.frame.xres, self.frame.yres = width, height
        self.frame.FourCC = FOURCC_BGRA
        # express fps as a rational, e.g. 29.97 -> 30000/1001
        if abs(fps - round(fps)) < 1e-6:
            self.frame.frame_rate_N, self.frame.frame_rate_D = int(round(fps)), 1
        else:
            self.frame.frame_rate_N, self.frame.frame_rate_D = int(round(fps * 1001)), 1001
        self.frame.picture_aspect_ratio = width / height
        self.frame.frame_format_type = FRAME_FORMAT_PROGRESSIVE
        self.frame.timecode = TIMECODE_SYNTHESIZE
        self.frame.line_stride_in_bytes = width * 4
        self.buf = ctypes.create_string_buffer(width * height * 4)
        self.frame.p_data = ctypes.cast(self.buf, ctypes.c_void_p)

    def send(self, bgra_bytes):
        ctypes.memmove(self.buf, bgra_bytes, len(bgra_bytes))
        # clock_video=True: this call paces itself to the frame rate
        self.lib.NDIlib_send_send_video_v2(self.instance, ctypes.byref(self.frame))

    def connections(self):
        return self.lib.NDIlib_send_get_no_connections(self.instance, 0)

    def close(self):
        self.lib.NDIlib_send_destroy(self.instance)


# --------------------------------------------------------------------------
# Pixel mapping
# --------------------------------------------------------------------------

def build_patch(cfg):
    """Return list indexed by [y][x] of (universe, address0) for each pixel,
    in on-screen coordinates (y=0 is the top row)."""
    chans, _ = COLOR_MODES[cfg["mode"]]
    per_univ = cfg["pixels_per_universe"] or (512 // chans)
    w, h = cfg["width"], cfg["height"]

    # order in which pixels are wired / patched on the desk
    order = []
    if cfg["order"] == "rows":
        for r in range(h):
            cols = list(range(w))
            if cfg["snake"] and r % 2 == 1:
                cols.reverse()
            order += [(c, r) for c in cols]
    else:  # columns
        for c in range(w):
            rows = list(range(h))
            if cfg["snake"] and c % 2 == 1:
                rows.reverse()
            order += [(c, r) for r in rows]

    patch = [[None] * w for _ in range(h)]
    for i, (c, r) in enumerate(order):
        x = (w - 1 - c) if cfg["start"] in ("top-right", "bottom-right") else c
        y = (h - 1 - r) if cfg["start"] in ("bottom-left", "bottom-right") else r
        univ = cfg["universe"] + i // per_univ
        addr0 = (cfg["address"] - 1) + (i % per_univ) * chans
        if addr0 + chans > 512:
            raise ValueError(f"pixel {i + 1} runs past channel 512 of universe {univ}: "
                             "lower the start address or set pixels per universe")
        if univ > 63999:
            raise ValueError("patch runs past universe 63999")
        patch[y][x] = (univ, addr0)
    return patch


def render(patch, dmx, cfg):
    chans, to_rgb = COLOR_MODES[cfg["mode"]]
    s = cfg["pixel_size"]
    gap = cfg["gap"]
    lit = s - gap
    master = cfg["master"]
    black = b"\x00\x00\x00\xff"
    rows = []
    rgb_grid = []
    for prow in patch:
        line = []
        grid_row = []
        for univ, a in prow:
            r, g, b = to_rgb(dmx[univ][a:a + chans])
            if master != 1.0:
                r, g, b = int(r * master), int(g * master), int(b * master)
            grid_row.append((r, g, b))
            line.append(bytes((b, g, r, 255)) * lit + black * gap)
        rgb_grid.append(grid_row)
        scan = b"".join(line)
        rows.append(scan * lit + (black * (s * len(prow))) * gap)
    return b"".join(rows), rgb_grid


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def validate(raw):
    """Merge raw values over DEFAULTS, coerce types, check ranges.
    Raises ValueError with a human-readable message."""
    cfg = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key not in raw or raw[key] is None:
            continue
        v = raw[key]
        try:
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                v = int(float(v))
            elif isinstance(default, float):
                v = float(v)
            else:
                v = str(v).strip()
        except (TypeError, ValueError):
            raise ValueError(f"{key.replace('_', ' ')}: '{raw[key]}' is not a valid value")
        cfg[key] = v

    def rng(key, lo, hi, label):
        if not lo <= cfg[key] <= hi:
            raise ValueError(f"{label} must be {lo:g} to {hi:g}")

    rng("width", 1, 512, "width")
    rng("height", 1, 512, "height")
    rng("universe", 1, 63999, "universe")
    rng("address", 1, 512, "start address")
    rng("pixels_per_universe", 0, 512, "pixels per universe")
    rng("pixel_size", 1, 256, "pixel size")
    rng("gap", 0, cfg["pixel_size"] - 1, "gap")
    rng("fps", 1, 120, "frame rate")
    rng("master", 0, 1, "master")
    if cfg["mode"] not in COLOR_MODES:
        raise ValueError(f"colour mode must be one of {', '.join(COLOR_MODES)}")
    if cfg["order"] not in ("rows", "columns"):
        raise ValueError("order must be rows or columns")
    if cfg["start"] not in CORNERS:
        raise ValueError(f"start corner must be one of {', '.join(CORNERS)}")
    if not cfg["name"]:
        raise ValueError("NDI source name can't be blank")
    if cfg["width"] * cfg["pixel_size"] > 8192 or cfg["height"] * cfg["pixel_size"] > 8192:
        raise ValueError("video would be over 8192 pixels - lower the pixel size")
    try:
        socket.inet_aton(cfg["interface"])
    except OSError:
        raise ValueError(f"network interface '{cfg['interface']}' is not an IP address")
    return cfg


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(cfg):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        print(f"warning: could not save settings ({e})", file=sys.stderr)


def list_interfaces():
    """[(label, ip)] of this machine's IPv4 interfaces."""
    out = [("All interfaces", "0.0.0.0"), ("Localhost (this computer) - 127.0.0.1", "127.0.0.1")]
    try:
        text = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    iface = None
    for line in text.splitlines():
        m = re.match(r"^(\S+?):? flags=", line)
        if m:
            iface = m.group(1)
            continue
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if m and iface and not m.group(1).startswith("127."):  # loopback is listed above
            out.append((f"{iface} - {m.group(1)}", m.group(1)))
    return out


# --------------------------------------------------------------------------
# Engine: owns the receiver + sender, re-configurable while running
# --------------------------------------------------------------------------

class Engine:
    def __init__(self, cfg):
        configure_ndi_machine_name(cfg["machine_name"] or default_machine_name())
        self.machine_name = cfg["machine_name"] or default_machine_name()
        self.lib, self.lib_path = load_ndi()
        self.version = self.lib.NDIlib_version().decode()
        self.lock = threading.Lock()
        self.waiting = threading.Event()  # another thread wants the lock: loop yields
        self.cfg = None
        self.rx = None
        self.ndi = None
        self.patch = None
        self.grid = []
        self.names = []
        self.pps = 0.0
        self.receivers = 0
        self.apply(cfg)
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def apply(self, raw):
        """Apply new settings live. Raises ValueError if they're invalid or the
        NDI source can't be created (the previous settings stay active)."""
        new = validate({**(self.cfg or {}), **raw})
        patch = build_patch(new)
        universes = {u for row in patch for (u, _a) in row}
        old = self.cfg or {}
        with self._locked():
            if (self.rx is None or self.rx.universes != universes
                    or (new["interface"], new["multicast"]) != (old.get("interface"), old.get("multicast"))):
                if self.rx:
                    self.rx.close()
                self.rx = SacnReceiver(universes, new["interface"], new["multicast"])
            if self.ndi is None or any(new[k] != old.get(k) for k in ("name", "width", "height", "pixel_size", "fps")):
                vw, vh = new["width"] * new["pixel_size"], new["height"] * new["pixel_size"]
                if self.ndi:
                    self.ndi.close()
                    self.ndi = None
                try:
                    self.ndi = NdiSender(self.lib, new["name"], vw, vh, new["fps"])
                except RuntimeError as e:
                    if old:  # put the previous source back
                        ovw, ovh = old["width"] * old["pixel_size"], old["height"] * old["pixel_size"]
                        self.ndi = NdiSender(self.lib, old["name"], ovw, ovh, old["fps"])
                    raise ValueError(str(e))
            self.cfg, self.patch = new, patch
        save_settings(new)
        return new

    def _loop(self):
        last, last_packets = time.monotonic(), 0
        while self.running:
            with self.lock:
                cfg, rx, ndi, patch = self.cfg, self.rx, self.ndi, self.patch
                dmx, names = rx.snapshot(hold_on_loss=not cfg["blackout_on_loss"])
                frame, grid = render(patch, dmx, cfg)
                ndi.send(frame)  # blocks to keep the frame rate
                self.grid, self.names = grid, names
                now = time.monotonic()
                if now - last >= 1.0:
                    self.pps = (rx.packets - last_packets) / (now - last)
                    self.receivers = ndi.connections()
                    last, last_packets = now, rx.packets
            if self.waiting.is_set():  # let apply()/state() in between frames
                time.sleep(0.005)

    def _locked(self):
        engine = self

        class _Ctx:
            def __enter__(self):
                engine.waiting.set()
                engine.lock.acquire()

            def __exit__(self, *exc):
                engine.lock.release()
                engine.waiting.clear()

        return _Ctx()

    def state(self):
        with self._locked():
            cfg, patch = dict(self.cfg), self.patch
            chans = COLOR_MODES[cfg["mode"]][0]
            first = min((u, a) for row in patch for (u, a) in row)
            last = max((u, a) for row in patch for (u, a) in row)
            return {
                "config": cfg,
                "grid": ["#%02x%02x%02x" % px for row in self.grid for px in row],
                "status": {
                    "sources": self.names,
                    "pps": round(self.pps),
                    "receivers": self.receivers,
                    "ndi_name": f"{self.machine_name} ({cfg['name']})",
                    "video": f"{cfg['width'] * cfg['pixel_size']} x {cfg['height'] * cfg['pixel_size']}",
                    "patch_from": f"{first[0]}.{first[1] + 1:03d}",
                    "patch_to": f"{last[0]}.{last[1] + chans:03d}",
                    "warnings": list(self.rx.warnings),
                    "ndi_runtime": self.version,
                },
                "interfaces": list_interfaces(),
                "modes": list(COLOR_MODES),
            }

    def close(self):
        self.running = False
        self.thread.join(timeout=1)
        with self.lock:
            self.rx.close()
            self.ndi.close()
        self.lib.NDIlib_destroy()


# --------------------------------------------------------------------------
# Settings page
# --------------------------------------------------------------------------

def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                with open(UI_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, engine.state())
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/config":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                engine.apply(body)
                self._send(200, engine.state())
            except ValueError as e:
                self._send(400, {"error": str(e)})

    return Handler


# --------------------------------------------------------------------------

def main():
    global SETTINGS_FILE
    S = argparse.SUPPRESS
    p = argparse.ArgumentParser(
        description="sACN (E1.31) pixel matrix -> NDI video source. Options given here "
                    "override the saved settings (settings.json); everything can also be "
                    "changed live in the settings page.")
    p.add_argument("--name", default=S, help="NDI source name (default: sACN Matrix)")
    p.add_argument("--width", type=int, default=S, help="pixels across (default: 10)")
    p.add_argument("--height", type=int, default=S, help="pixels down (default: 10)")
    p.add_argument("--universe", type=int, default=S, help="first sACN universe (default: 1)")
    p.add_argument("--address", type=int, default=S, help="DMX start address of pixel 1 (default: 1)")
    p.add_argument("--mode", choices=COLOR_MODES, default=S, help="channels per pixel (default: RGB)")
    p.add_argument("--pixels-per-universe", type=int, default=S,
                   help="pixels before rolling to the next universe (default: as many as fit)")
    p.add_argument("--order", choices=["rows", "columns"], default=S, help="patch runs along rows or columns")
    p.add_argument("--start", choices=CORNERS, default=S, help="corner where pixel 1 sits")
    p.add_argument("--snake", action=argparse.BooleanOptionalAction, default=S,
                   help="zig-zag wiring: every other row/column reversed")
    p.add_argument("--pixel-size", type=int, default=S, help="output video pixels per LED pixel (default: 32)")
    p.add_argument("--gap", type=int, default=S, help="black gap in video pixels between LEDs (default: 0)")
    p.add_argument("--fps", type=float, default=S, help="NDI frame rate (default: 50)")
    p.add_argument("--master", type=float, default=S, help="output intensity scale 0..1")
    p.add_argument("--interface", default=S,
                   help="IP of the network card facing the desk, for multicast (default: all)")
    p.add_argument("--multicast", action=argparse.BooleanOptionalAction, default=S,
                   help="join sACN multicast groups (default: on)")
    p.add_argument("--blackout-on-loss", action=argparse.BooleanOptionalAction, default=S,
                   help="go black when the desk stops sending (default: hold last look)")
    p.add_argument("--machine-name", default=S,
                   help="machine name shown in NDI (default: this Mac's network name)")
    p.add_argument("--http-port", type=int, default=8090, help="settings page port (default: 8090)")
    p.add_argument("--settings", default=SETTINGS_FILE, help="settings file (default: settings.json here)")
    p.add_argument("--no-browser", action="store_true", help="don't open the settings page on start")
    p.add_argument("--preview", action="store_true", help=S)  # old flag: preview is in the settings page now
    args = vars(p.parse_args())
    http_port, no_browser = args.pop("http_port"), args.pop("no_browser")
    SETTINGS_FILE = os.path.abspath(args.pop("settings"))
    args.pop("preview")

    try:
        cfg = validate({**load_settings(), **args})
        engine = Engine(cfg)
    except (ValueError, RuntimeError) as e:
        sys.exit(f"error: {e}")

    try:
        server = ThreadingHTTPServer(("127.0.0.1", http_port), make_handler(engine))
    except OSError as e:
        engine.close()
        sys.exit(f"error: settings page port {http_port} is busy ({e}). "
                 "Is sacn2ndi already running? Or use --http-port")
    url = f"http://localhost:{http_port}"

    st = engine.state()["status"]
    print(f"NDI runtime {engine.version}  ({engine.lib_path})")
    print(f"NDI source  {st['ndi_name']}  {st['video']} @ {engine.cfg['fps']:g} fps")
    print(f"sACN in     {st['patch_from']} - {st['patch_to']}")
    print(f"Settings    {url}")
    print("Ctrl+C to stop.\n")
    if not no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(1)
            s = engine.state()["status"]
            src = ", ".join(s["sources"]) if s["sources"] else "no sACN source"
            sys.stdout.write("\r" + f"sACN: {src} ({s['pps']} pkt/s)  |  NDI receivers: {s['receivers']}".ljust(100))
            sys.stdout.flush()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        engine.close()


if __name__ == "__main__":
    main()
