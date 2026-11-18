# ============================================================================
#  Home Proxy UI — 镜像
#    只提供 Web UI + mihomo 控制面转发，不含任何网络规则。
#    透明代理（nft/TPROXY/策略路由）必须装在宿主机上，见 docs/DEPLOY.md。
#
#  构建上下文 = ui/backend/
#    docker build -t home-proxy-ui ./backend
#    docker compose -f docker-compose.yml --env-file .env up -d --force-recreate
#
#  为什么用国内镜像源：
#    在国内网络下 apt 直连 deb.debian.org 会超时（即使 Docker daemon 配了代理，
#    apt 也不一定走），导致构建卡住。改用阿里云镜像源可直接构建，无需代理。
#    如需改回官方源，删掉下面两处 sed 即可。
# ============================================================================
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# nft / ip 用于「透明代理链路」只读探测；tini 做 PID 1 信号转发
# 注意：Debian 12+ 的源文件是 /etc/apt/sources.list.d/debian.sources（deb822 格式）
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        nftables iproute2 curl tini ca-certificates; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        -r requirements.txt

COPY app ./app
COPY static ./static

RUN useradd -r -u 10001 -s /usr/sbin/nologin appuser \
    && mkdir -p /etc/mihomo \
    && chown -R appuser:appuser /app /etc/mihomo

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${UI_PORT}/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# 监听端口可通过 UI_PORT 覆盖（network_mode: host 时靠它在宿主机上换端口）
ENV UI_PORT=8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${UI_PORT} --proxy-headers"]
