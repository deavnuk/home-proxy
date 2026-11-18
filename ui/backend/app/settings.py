"""运行时配置：全部来自环境变量，容器化友好。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- mihomo 控制接口 ---
    # 支持两种：http://host:port  或  unix:///path/to.sock
    # unix socket 的好处：不受宿主防火墙(ufw)影响，容器挂载文件即可访问。
    mihomo_api: str = field(default_factory=lambda: os.getenv("MIHOMO_API", "http://127.0.0.1:9090"))
    mihomo_secret: str = field(default_factory=lambda: os.getenv("MIHOMO_SECRET", ""))
    timeout: float = field(default_factory=lambda: float(os.getenv("MIHOMO_TIMEOUT", "10")))

    # --- 配置文件（用于查看/编辑/重载）---
    mihomo_config: Path = field(default_factory=lambda: Path(os.getenv("MIHOMO_CONFIG", "/etc/mihomo/config.yaml")))

    # --- UI 自身鉴权（留空 = 不鉴权，仅建议在可信内网使用）---
    ui_user: str = field(default_factory=lambda: os.getenv("UI_USER", ""))
    ui_pass: str = field(default_factory=lambda: os.getenv("UI_PASS", ""))

    # --- 是否允许在 UI 里改配置/改代理/断连接等写操作 ---
    read_only: bool = field(default_factory=lambda: _bool("UI_READ_ONLY", False))

    # --- mihomo DNS 端口（分流探针用，必须与 config.yaml 的 dns.listen 一致）---
    dns_port: int = field(default_factory=lambda: int(os.getenv("MIHOMO_DNS_PORT", "1053")))

    # --- 本机 nft 表名（透明代理状态面板用）---
    nft_table: str = field(default_factory=lambda: os.getenv("NFT_TABLE", "inet transp"))
    lan_net: str = field(default_factory=lambda: os.getenv("LAN_NET", ""))

    @property
    def is_unix_socket(self) -> bool:
        return self.mihomo_api.startswith("unix://")

    @property
    def unix_socket_path(self) -> str:
        """unix:///tmp/mihomo.sock -> /tmp/mihomo.sock"""
        return self.mihomo_api[len("unix://"):]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.ui_user and self.ui_pass)


settings = Settings()
