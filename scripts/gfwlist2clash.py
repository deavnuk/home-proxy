#!/usr/bin/env python3
"""
gfwlist2clash.py — 把官方 gfwlist (AutoProxy/base64) 转成 mihomo rule-provider YAML

为什么要这个：Loyalsoldier/clash-rules 的 gfw.txt 只是 gfwlist 的一个转换版本，
上游更新有延迟。这个脚本直接吃官方 gfwlist，生成 ./config/providers/gfwlist-extra.yaml，
在 config.yaml 里以 gfw-extra 规则集参与分流。

用法：
    ./scripts/gfwlist2clash.py                       # 默认走直连
    https_proxy=http://127.0.0.1:7890 ./scripts/gfwlist2clash.py   # 走本机代理拉
    ./scripts/gfwlist2clash.py --out /etc/mihomo/providers/gfwlist-extra.yaml
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import re
import sys
import urllib.request
from pathlib import Path

SOURCES = [
    "https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt",
    "https://pagure.io/gfwlist/raw/master/f/gfwlist.txt",
]

# 只收「域名型」规则；IP/CIDR 与正则太脏，交给 cncidr + gfw 规则集
DOMAIN_RULE = re.compile(r"^(?:\|\|)?([A-Za-z0-9._*-]+)$")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def load_list() -> str:
    last = None
    for url in SOURCES:
        try:
            raw = fetch(url)
            # 官方文件是 base64；也兼容已是明文的镜像
            try:
                return base64.b64decode(raw).decode("utf-8", "replace")
            except Exception:
                return raw
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[warn] {url} 失败: {e}", file=sys.stderr)
    raise SystemExit(f"所有源都失败: {last}")


def parse(text: str) -> list[str]:
    domains: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("!", "[", "#")):
            continue
        # 去掉 AutoProxy 前缀/后缀
        if line.startswith("@@"):
            continue  # 白名单条目，跳过
        m = DOMAIN_RULE.match(line)
        if not m:
            continue
        host = m.group(1).lower().strip(".")
        if not host or "*" in host:
            # 通配符规则（如 ads*.example.com）转成后缀规则有误，丢弃
            continue
        if "." not in host:
            continue
        if host.startswith("."):
            host = host[1:]
        # 排除误收的 IP
        try:
            ipaddress.ip_address(host)
            continue
        except ValueError:
            pass
        # 裸域名 -> 转成 +.domain 以覆盖所有子域
        domains.add(f"+.{host}")
        # 同时保留精确匹配（gfwlist 里的 ||x.com 本意是含子域，+. 已覆盖）
    return sorted(domains)


def write_yaml(domains: list[str], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write("payload:\n")
        for d in domains:
            f.write(f"  - '{d}'\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(
        Path(__file__).resolve().parent.parent / "config/providers/gfwlist-extra.yaml"))
    ap.add_argument("--min", type=int, default=500,
                    help="少于这么多条就视为拉取失败（防污染/防截断）")
    args = ap.parse_args()

    text = load_list()
    domains = parse(text)
    if len(domains) < args.min:
        raise SystemExit(f"解析结果只有 {len(domains)} 条，疑似被污染/截断，放弃写入")

    out = Path(args.out)
    write_yaml(domains, out)
    print(f"写入 {out}，共 {len(domains)} 条域名规则")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
