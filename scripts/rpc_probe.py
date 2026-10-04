#!/usr/bin/env python3
"""Application-level health probe for a ggml-rpc server.

Speaks the minimal RPC HELLO handshake (cmd=14, 24-byte caps request,
28-byte version response). A wedged server accepts TCP but never answers
the handshake — which a plain TCP connect cannot detect.

Usage: rpc_probe.py <ip> [port]     exit 0 = healthy, 1 = wedged/unreachable
Prints: '<ip> HELLO <major>.<minor>.<patch>' on success.
"""
import socket
import struct
import sys

RPC_CMD_HELLO = 14
CONN_CAPS_SIZE = 24
RSP_SIZE = 28  # major, minor, patch, padding + 24 caps bytes


def hello(ip, port, timeout):
    s = socket.create_connection((ip, port), timeout=timeout)
    s.settimeout(timeout)
    try:
        s.sendall(struct.pack("<B", RPC_CMD_HELLO)
                  + struct.pack("<Q", CONN_CAPS_SIZE)
                  + b"\x00" * CONN_CAPS_SIZE)
        hdr = b""
        while len(hdr) < 8:
            c = s.recv(8 - len(hdr))
            if not c:
                return None
            hdr += c
        (sz,) = struct.unpack("<Q", hdr)
        if sz != RSP_SIZE:
            return None
        resp = b""
        while len(resp) < sz:
            c = s.recv(sz - len(resp))
            if not c:
                return None
            resp += c
        return (resp[0], resp[1], resp[2])
    finally:
        s.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(2)
    ip = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 50052
    try:
        v = hello(ip, port, timeout=5)
    except Exception:
        v = None
    if v:
        print(f"{ip} HELLO {v[0]}.{v[1]}.{v[2]}")
        sys.exit(0)
    print(f"{ip} WEDGE (no hello response)")
    sys.exit(1)
