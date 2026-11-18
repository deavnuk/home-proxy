#!/usr/bin/env python3
"""
dns-stub.py — 测试用最小 DNS 服务器

沙箱/隔离 netns 里没有可达的上游 DNS，这里造一个：
  · 任意 A 查询 -> 固定返回一批 "公网" IP（非 198.18 段）
这样就能验证 mihomo 的 fake-ip 白名单：命中 gfwlist 的域名拿到 198.18.x，
未命中的域名拿到这里的 "真实" IP。

用法: python3 dns-stub.py [port]     默认 5300
"""
import socket
import struct
import sys
import threading

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5300
# 看起来像真实公网地址的固定返回值
FIXED = ["203.0.113.10", "203.0.113.11", "203.0.113.12"]


def skip_name(data: bytes, i: int) -> int:
    while True:
        if i >= len(data):
            return i
        ln = data[i]
        if ln == 0:
            return i + 1
        if ln & 0xC0 == 0xC0:
            return i + 2
        i += 1 + ln


def parse_qname(data: bytes, i: int) -> str:
    parts = []
    while data[i] != 0:
        ln = data[i]
        parts.append(data[i + 1: i + 1 + ln].decode("ascii", "replace"))
        i += 1 + ln
    return ".".join(parts)


def handle(data: bytes, addr):
    tid = data[0:2]
    if len(data) < 12:
        return
    qtype, qclass = struct.unpack(">HH", data[len(data) - 4:])
    qname = parse_qname(data, 12)
    qend = skip_name(data, 12) + 4
    question = data[12:qend]

    answers = b""
    ancount = 0
    if qtype == 1:  # A
        for ip in FIXED:
            answers += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + socket.inet_aton(ip)
            ancount += 1
    elif qtype == 28:  # AAAA -> 空答（避免 IPv6 干扰）
        ancount = 0

    flags = 0x8180  # QR=1 RD=1 RA=1
    resp = (tid + struct.pack(">HHHHH", flags, 1, ancount, 0, 0)
            + question + answers)
    return resp


def serve():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", PORT))
    print(f"[dns-stub] listening 127.0.0.1:{PORT}", flush=True)
    while True:
        data, addr = s.recvfrom(4096)
        try:
            r = handle(data, addr)
            if r:
                s.sendto(r, addr)
        except Exception as e:  # noqa: BLE001
            print("[dns-stub] err", repr(e), flush=True)


if __name__ == "__main__":
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    # 同时支持 TCP（mihomo 会同时开 UDP/TCP）
    ts = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ts.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ts.bind(("127.0.0.1", PORT))
    ts.listen(16)
    while True:
        c, _ = ts.accept()
        try:
            ln = struct.unpack(">H", c.recv(2))[0]
            data = c.recv(ln)
            r = handle(data, None)
            if r:
                c.sendall(struct.pack(">H", len(r)) + r)
        except Exception:
            pass
        finally:
            c.close()
