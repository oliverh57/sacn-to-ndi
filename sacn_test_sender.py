#!/usr/bin/env python3
"""
Stand-in for a lighting desk: sends a moving rainbow (or a pixel-order test)
over sACN so sacn2ndi.py can be tested without a console.

    python3 sacn_test_sender.py                 # rainbow, unicast to this machine
    python3 sacn_test_sender.py --pattern chase # lights pixels 1,2,3... in patch order
    python3 sacn_test_sender.py --multicast     # send like a real desk
    python3 sacn_test_sender.py --multicast --interface 127.0.0.1   # multicast on localhost only
"""

import argparse
import colorsys
import math
import socket
import struct
import time
import uuid


def packet(cid, name, universe, seq, data, priority=100, terminate=False):
    data = bytes(data)[:512]
    n = len(data)
    root_len = 38 + 77 + 11 + n - 16  # everything after the preamble/ACN id
    pkt = bytearray()
    pkt += struct.pack(">HH", 0x0010, 0x0000) + b"ASC-E1.17\x00\x00\x00"
    pkt += struct.pack(">HI", 0x7000 | (root_len), 0x00000004) + cid
    pkt += struct.pack(">HI", 0x7000 | (77 + 11 + n), 0x00000002)
    pkt += name.encode()[:63].ljust(64, b"\x00")
    pkt += struct.pack(">BHBBH", priority, 0, seq & 0xFF, 0x40 if terminate else 0, universe)
    pkt += struct.pack(">HBBHHH", 0x7000 | (11 + n), 0x02, 0xA1, 0, 1, n + 1)
    pkt += b"\x00" + data
    return bytes(pkt)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--universe", type=int, default=1)
    p.add_argument("--pixels", type=int, default=100)
    p.add_argument("--width", type=int, default=10)
    p.add_argument("--pattern", choices=["rainbow", "chase"], default="rainbow")
    p.add_argument("--target", default="127.0.0.1", help="unicast destination IP")
    p.add_argument("--multicast", action="store_true")
    p.add_argument("--interface", help="IP of the interface to multicast from (e.g. 127.0.0.1)")
    p.add_argument("--fps", type=float, default=40)
    p.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = forever)")
    args = p.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    if args.interface:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(args.interface))
    dest = (f"239.255.{args.universe >> 8}.{args.universe & 0xFF}" if args.multicast else args.target, 5568)
    cid = uuid.uuid4().bytes
    seq = 0
    t0 = time.monotonic()
    via = f" via {args.interface}" if args.interface else ""
    print(f"sending {args.pattern} to {dest[0]}{via} universe {args.universe}, Ctrl+C to stop")
    try:
        while True:
            t = time.monotonic() - t0
            if args.seconds and t > args.seconds:
                break
            dmx = bytearray(512)
            for i in range(args.pixels):
                if args.pattern == "rainbow":
                    x, y = i % args.width, i // args.width
                    h = (x + y) / (2 * args.width) + t * 0.25
                    r, g, b = colorsys.hsv_to_rgb(h % 1.0, 1.0, 1.0)
                else:
                    on = i == int(t * 10) % args.pixels
                    r = g = b = 1.0 if on else 0.0
                dmx[i * 3:i * 3 + 3] = bytes((int(r * 255), int(g * 255), int(b * 255)))
            sock.sendto(packet(cid, "Test Sender", args.universe, seq, dmx), dest)
            seq += 1
            time.sleep(1 / args.fps)
    except KeyboardInterrupt:
        pass
    for _ in range(3):  # tell receivers we're done (E1.31 stream terminated)
        sock.sendto(packet(cid, "Test Sender", args.universe, seq, bytes(512), terminate=True), dest)
        seq += 1


if __name__ == "__main__":
    main()
