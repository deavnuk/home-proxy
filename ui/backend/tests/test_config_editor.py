#!/usr/bin/env python3
"""config_editor 单元测试 —— 重点验证「不破坏用户原有内容」。

运行： python3 -m pytest ui/backend/tests/test_config_editor.py -v
或：   python3 ui/backend/tests/test_config_editor.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config_editor as ce  # noqa: E402

SRC = """\
# ===== 文件头注释（必须保留）=====
mixed-port: 7890
mode: rule

proxies:
  - name: "手工节点"
    type: socks5
    server: 1.2.3.4
    port: 1080
    # 行内注释也要保留

# ---- 订阅段前的手写注释 ----
proxy-providers:
  legacy:
    type: http
    url: "https://old.example.com/sub"
    interval: 3600

proxy-groups:
  - name: PROXY
    type: select
    proxies: [DIRECT]
    use: [legacy]

rules:
  - RULE-SET,gfw,PROXY
  - MATCH,DIRECT
"""


def t(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  {extra}" if extra and not cond else ""))
    return cond


def run() -> int:
    ok = True
    print("== 解析 ==")
    subs = ce.parse_subscriptions(SRC)
    ok &= t("解析出 1 个订阅", len(subs) == 1, str(subs))
    ok &= t("名称正确", subs[0]["name"] == "legacy")
    ok &= t("interval 正确", subs[0]["interval"] == 3600)

    print("== 段落定位 ==")
    blk = ce.get_block_text(SRC, "proxy-providers")
    ok &= t("段落含 legacy", "legacy" in blk)
    ok &= t("段落不含 proxy-groups", "proxy-groups" not in blk)

    print("== 新增订阅 ==")
    out = ce.upsert_subscription(SRC, {
        "name": "newsub", "url": "https://new.example.com/s?token=secret",
        "interval": 7200, "proxy": "PROXY",
        "health_check": {"enable": True, "interval": 300,
                         "url": "https://www.gstatic.com/generate_204"},
    })
    names = [s["name"] for s in ce.parse_subscriptions(out)]
    ok &= t("两个订阅都在", names == ["legacy", "newsub"], str(names))
    ok &= t("原注释保留", "文件头注释（必须保留）" in out)
    ok &= t("行内注释保留", "# 行内注释也要保留" in out)
    ok &= t("rules 段完好", "MATCH,DIRECT" in out)
    ok &= t("proxies 段完好", "手工节点" in out)
    ok &= t("YAML 合法", ce.validate(out) is None, str(ce.validate(out)))

    print("== 幂等性（反复写不应堆积注释）==")
    t2 = out
    for _ in range(5):
        t2 = ce.upsert_subscription(t2, {
            "name": "newsub", "url": "https://new.example.com/s?token=secret",
            "interval": 7200,
            "health_check": {"enable": True, "interval": 300,
                             "url": "https://www.gstatic.com/generate_204"},
        })
    ok &= t("反复写 5 次后 header 只有 1 份",
            t2.count("节点订阅（由 Web UI 管理") == 1,
            f"实际 {t2.count('节点订阅（由 Web UI 管理')} 份")
    ok &= t("反复写后订阅数仍为 2", len(ce.parse_subscriptions(t2)) == 2)

    print("== 删除订阅 ==")
    d, removed = ce.remove_subscription(out, "legacy")
    ok &= t("删除返回成功", removed is True)
    ok &= t("legacy 已消失", "legacy" not in [s["name"] for s in ce.parse_subscriptions(d)])
    ok &= t("newsub 仍在", "newsub" in [s["name"] for s in ce.parse_subscriptions(d)])
    ok &= t("删除后 YAML 合法", ce.validate(d) is None)
    d2, removed2 = ce.remove_subscription(d, "不存在")
    ok &= t("删除不存在的返回 False", removed2 is False)

    print("== use: 同步 ==")
    synced = ce.sync_group_use(d)
    ok &= t("use 行只含 newsub", "use: [newsub]" in synced, 
            [ln.strip() for ln in synced.splitlines() if "use:" in ln])

    print("== URL 打码 ==")
    ok &= t("token 被打码", "token=***" in ce.mask_url("https://a.com/s?token=abc123"))
    ok &= t("长 hash 路径被打码", "/***" in ce.mask_url("https://a.com/abcdefghijklmnopqrstuvwxyz123456"))
    ok &= t("普通 URL 不变", ce.mask_url("https://a.com/sub?flag=clash") == "https://a.com/sub?flag=clash")

    print("== 校验 ==")
    ok &= t("缺 proxies 被拒", ce.validate("proxy-groups: []\nrules: []") is not None)
    ok &= t("语法错误被拒", ce.validate("a: [unclosed") is not None)
    ok &= t("非法订阅 URL 被拒",
            ce.validate(SRC.replace("https://old.example.com/sub", "ftp://x")) is not None)

    print()
    print("=" * 46)
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
