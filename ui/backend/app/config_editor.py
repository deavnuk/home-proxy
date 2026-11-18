"""config.yaml 结构化读写 —— 只改需要改的段落，其余逐字节保留。

为什么不用 yaml.dump：
  用户配置文件里有大量注释和精心排版的顺序，dump 会全部丢掉。
  所以这里采用「定位段落 → 文本级替换」的方式。

支持的操作：
  · 读取 proxy-providers 段
  · 新增/修改/删除单个订阅
  · 通用：替换任意顶层键的值

设计约束：
  · 只处理顶层键（缩进 0 的 `key:`），不处理嵌套的复杂结构
  · 段落边界 = 从 `key:` 行开始，到下一个缩进 0 的非注释行为止
  · 所有写操作返回新文本，由调用方决定是否落盘
"""
from __future__ import annotations

import re
from typing import Any

import yaml


class ConfigError(Exception):
    """配置结构不合法。"""


# ---------------------------------------------------------------- 段落定位
def find_block(text: str, key: str) -> tuple[int, int] | None:
    """返回顶层键 `key:` 段落的 [start, end) 字符偏移。

    段落从 `key:` 所在行**及其紧邻的前置注释/空行**开始，
    到下一个「缩进 0 且非注释」的行首结束。

    为什么要把前置注释包进来：
      否则每次重写段落都会在原注释后面又插入一份新 header，
      反复操作后注释会不断堆积（实测踩过）。
    """
    lines = text.splitlines(keepends=True)

    def is_top_line(line: str) -> bool:
        return bool(line) and line[0] not in (" ", "\t", "\n")

    key_idx = None
    for i, line in enumerate(lines):
        if is_top_line(line) and re.match(rf"^{re.escape(key)}\s*:", line):
            key_idx = i
            break
    if key_idx is None:
        return None

    # 向上吞并紧邻的注释行与连续注释块（但不越过上一个顶层键）
    start_idx = key_idx
    j = key_idx - 1
    while j >= 0:
        line = lines[j]
        if line.startswith("#"):
            start_idx = j
            j -= 1
            continue
        if line.strip() == "":
            # 空行：只有上面还是注释才继续吞，避免吃掉别处的空行
            if start_idx < key_idx and lines[start_idx].startswith("#"):
                start_idx = j
                j -= 1
                continue
        break

    # 向下找段落结束：下一个顶层非注释行
    end_idx = len(lines)
    for i in range(key_idx + 1, len(lines)):
        if is_top_line(lines[i]) and not lines[i].startswith("#"):
            end_idx = i
            break

    start = sum(len(x) for x in lines[:start_idx])
    end = sum(len(x) for x in lines[:end_idx])
    return start, end


def get_block_text(text: str, key: str) -> str:
    r = find_block(text, key)
    return text[r[0]:r[1]] if r else ""


def replace_block(text: str, key: str, new_block: str) -> str:
    """用 new_block 替换 `key:` 段落。段落不存在则追加到末尾。"""
    r = find_block(text, key)
    if not new_block.endswith("\n"):
        new_block += "\n"
    if r:
        return text[: r[0]] + new_block + text[r[1]:]
    # 追加
    return text.rstrip("\n") + "\n\n" + new_block


# ---------------------------------------------------------------- 订阅读写
SUB_DEFAULTS: dict[str, Any] = {
    "type": "http",
    "interval": 21600,
    "proxy": "PROXY",
    "enabled": True,
}


def parse_subscriptions(text: str) -> list[dict[str, Any]]:
    """解析 proxy-providers 段为列表。解析失败返回空列表（不抛异常）。"""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return []
    provs = doc.get("proxy-providers") or {}
    if not isinstance(provs, dict):
        return []
    out = []
    for name, v in provs.items():
        if not isinstance(v, dict):
            continue
        hc = v.get("health-check") or {}
        out.append({
            "name": name,
            "url": v.get("url", ""),
            "type": v.get("type", "http"),
            "interval": v.get("interval", 21600),
            "enabled": v.get("enabled", True),
            "path": v.get("path", f"./providers/{name}.yaml"),
            "proxy": v.get("proxy", ""),
            "filter": v.get("filter", ""),
            "exclude_filter": v.get("exclude-filter", ""),
            "health_check": {
                "enable": hc.get("enable", True),
                "interval": hc.get("interval", 300),
                "url": hc.get("url", "https://www.gstatic.com/generate_204"),
            },
            "size_limit": v.get("size-limit", 0),
        })
    return out


def _yaml_str(v: Any) -> str:
    """输出一个安全的 YAML 标量。"""
    s = str(v)
    if s and not re.search(r'[:#\[\]{}&*!|>\'"%@`,\n]', s) and s[0] not in "-? " and s.lower() not in (
        "true", "false", "null", "yes", "no", "on", "off",
    ):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_subscription(sub: dict[str, Any]) -> list[str]:
    """把一个订阅渲染成 YAML 行（缩进 2 空格，属于 proxy-providers 段内）。"""
    name = sub["name"]
    url = sub["url"]
    lines = [
        f"  {name}:",
        f"    type: {sub.get('type', 'http')}",
        f"    url: {_yaml_str(url)}",
        f"    interval: {int(sub.get('interval', 21600))}",
        f"    path: {_yaml_str(sub.get('path') or f'./providers/{name}.yaml')}",
    ]
    if sub.get("enabled", True) is False:
        lines.append("    enabled: false")
    if sub.get("proxy"):
        lines.append(f"    proxy: {_yaml_str(sub['proxy'])}")
    if sub.get("size_limit"):
        lines.append(f"    size-limit: {int(sub['size_limit'])}")
    if sub.get("filter"):
        lines.append(f"    filter: {_yaml_str(sub['filter'])}")
    if sub.get("exclude_filter"):
        lines.append(f"    exclude-filter: {_yaml_str(sub['exclude_filter'])}")

    hc = sub.get("health_check") or {}
    if hc.get("enable", True):
        lines += [
            "    health-check:",
            "      enable: true",
            f"      interval: {int(hc.get('interval', 300))}",
            f"      url: {_yaml_str(hc.get('url') or 'https://www.gstatic.com/generate_204')}",
            "      lazy: false",
        ]
    return lines


def render_providers_block(subs: list[dict[str, Any]]) -> str:
    """把订阅列表渲染成完整的 proxy-providers 段。"""
    if not subs:
        return "proxy-providers: {}\n"
    header = [
        "# ===========================================================================",
        "#  节点订阅（由 Web UI 管理，也可手工编辑）",
        "#    每个订阅会自动把节点注入到引用它的策略组（use: [<name>]）",
        "# ===========================================================================",
        "proxy-providers:",
    ]
    lines = list(header)
    for s in subs:
        lines.append("")
        lines.extend(render_subscription(s))
    return "\n".join(lines) + "\n"


def upsert_subscription(text: str, sub: dict[str, Any]) -> str:
    """新增或更新一个订阅。name 相同视为更新。"""
    subs = parse_subscriptions(text)
    by_name = {s["name"]: s for s in subs}
    by_name[sub["name"]] = sub
    ordered = [by_name[n] for n in by_name]  # 保持原有顺序，新的追加在末尾
    return replace_block(text, "proxy-providers", render_providers_block(ordered))


def remove_subscription(text: str, name: str) -> tuple[str, bool]:
    """删除订阅。返回 (新文本, 是否删除成功)。"""
    subs = parse_subscriptions(text)
    kept = [s for s in subs if s["name"] != name]
    if len(kept) == len(subs):
        return text, False
    return replace_block(text, "proxy-providers", render_providers_block(kept)), True


def sync_group_use(text: str, group_names: list[str] | None = None) -> str:
    """让指定策略组的 `use:` 跟随当前订阅列表。

    订阅增删后，引用它的策略组需要同步更新 use 列表，否则节点不会出现。
    这里做保守实现：只处理 use 列表已存在的组，逐行替换其 use: 行。
    """
    subs = [s["name"] for s in parse_subscriptions(text) if s.get("enabled", True)]
    if not subs:
        return text
    use_line = "    use: [" + ", ".join(subs) + "]"

    lines = text.splitlines(keepends=True)
    out = []
    for line in lines:
        m = re.match(r"^(\s+)use:\s*\[", line)
        if m:
            indent = m.group(1)
            out.append(f"{indent}use: [{', '.join(subs)}]\n")
        else:
            out.append(line)
    return "".join(out)


# ---------------------------------------------------------------- 校验
def validate(text: str) -> str | None:
    """返回错误描述，合法则返回 None。"""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return f"YAML 语法错误：{e}"
    if not isinstance(doc, dict):
        return "根节点必须是映射"
    for need in ("proxies", "proxy-groups", "rules"):
        if need not in doc:
            return f"缺少 `{need}` 段"
        if not isinstance(doc.get(need), list):
            return f"`{need}` 必须是列表"
    for name, p in (doc.get("proxy-providers") or {}).items():
        if not isinstance(p, dict):
            return f"订阅 `{name}` 必须是映射"
        url = p.get("url")
        if not url or not isinstance(url, str):
            return f"订阅 `{name}` 缺少 url"
        if not re.match(r"^https?://", url):
            return f"订阅 `{name}` 的 url 必须以 http:// 或 https:// 开头"
    return None


def mask_url(url: str) -> str:
    """把订阅 URL 里的 token/password 参数打码，用于接口返回。"""
    if not url:
        return ""
    def repl(m: re.Match) -> str:
        return f"{m.group(1)}=***"
    masked = re.sub(r"(?i)(token|key|password|passwd|secret|auth)=([^&]+)", repl, url)
    # 路径里的长 hash 也遮一下
    masked = re.sub(r"(/[A-Za-z0-9_-]{24,})(?=$|[?&])", "/***", masked)
    return masked


# ============================================================================
#  「我的规则」读写
#  用户视角是「某网站直连/走代理/拦截」，落到配置就是 rules 里的
#  DOMAIN / DOMAIN-SUFFIX 条目。这里只增删这类简单条目，
#  不动 RULE-SET 等结构性规则，避免把配置搞坏。
# ============================================================================
_SIMPLE_RULE_TYPES = ("DOMAIN", "DOMAIN-SUFFIX", "DOMAIN-KEYWORD")

# 「我的规则」统一放在 rules 段的这个标记之后，便于识别与读写
MY_RULES_START = "  # --- 我的规则（由 Web UI 管理）---"
MY_RULES_END = "  # --- 我的规则结束 ---"


def parse_my_rules(text: str) -> list[dict[str, str]]:
    """解析 rules 段里标记范围内的简单域名规则。"""
    block = get_block_text(text, "rules")
    if MY_RULES_START not in block:
        return []
    seg = block.split(MY_RULES_START, 1)[1].split(MY_RULES_END, 1)[0]
    out = []
    for line in seg.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = [p.strip() for p in line[2:].split(",")]
        if len(parts) >= 2 and parts[0] in _SIMPLE_RULE_TYPES:
            out.append({
                "type": parts[0],
                "domain": parts[1],
                "action": parts[2] if len(parts) > 2 else "PROXY",
            })
    return out


def _render_my_rules(rules: list[dict[str, str]]) -> str:
    lines = [MY_RULES_START]
    for r in rules:
        lines.append(f"  - {r['type']},{r['domain']},{r['action']}")
    lines.append(MY_RULES_END)
    return "\n".join(lines)


def write_my_rules(text: str, rules: list[dict[str, str]]) -> str:
    """把「我的规则」写回 rules 段。

    放在 rules: 段的最前面（优先级最高），这样用户加的规则一定生效。
    若标记已存在则原位替换。
    """
    block = get_block_text(text, "rules")
    rendered = _render_my_rules(rules)

    if MY_RULES_START in block:
        head, rest = block.split(MY_RULES_START, 1)
        _, tail = rest.split(MY_RULES_END, 1)
        new_block = head + rendered + tail
    else:
        # 插到 `rules:` 这一行之后（最前，优先级最高）
        lines = block.splitlines(keepends=True)
        idx = next((i for i, l in enumerate(lines)
                    if re.match(r"^\s*rules\s*:", l)), None)
        if idx is None:
            return text
        new_block = "".join(lines[: idx + 1]) + rendered + "\n" + "".join(lines[idx + 1:])

    return replace_block(text, "rules", new_block)


def add_my_rule(text: str, rule_type: str, domain: str, action: str) -> tuple[str, str | None]:
    """添加一条规则。返回 (新文本, 错误信息)。"""
    domain = domain.strip().lower()
    if not domain:
        return text, "域名不能为空"
    if rule_type not in _SIMPLE_RULE_TYPES:
        return text, f"不支持的类型 {rule_type}"
    if action not in ("DIRECT", "PROXY", "REJECT"):
        return text, f"不支持的动作 {action}"

    rules = parse_my_rules(text)
    if any(r["type"] == rule_type and r["domain"] == domain for r in rules):
        return text, f"规则已存在：{domain}"
    rules.insert(0, {"type": rule_type, "domain": domain, "action": action})
    return write_my_rules(text, rules), None


def remove_my_rule(text: str, domain: str) -> tuple[str, bool]:
    rules = parse_my_rules(text)
    kept = [r for r in rules if r["domain"] != domain.strip().lower()]
    if len(kept) == len(rules):
        return text, False
    return write_my_rules(text, kept), True


def normalize_domain(raw: str) -> tuple[str, str, str | None]:
    """把用户输入归一化成 (域名, 规则类型, 错误信息)。

    支持：example.com / www.example.com / +.example.com / *.example.com
    一律映射为 DOMAIN-SUFFIX（子域匹配），符合「填 example.com 管所有子域」的直觉。
    """
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split(":")[0].strip(".")
    if not d:
        return "", "", "域名不能为空"
    d = re.sub(r"^[+*]\.", "", d)      # 去掉 +. 或 *. 前缀
    if not re.match(r"^[a-z0-9]([a-z0-9\-_.]*[a-z0-9])?$", d):
        return "", "", "域名格式不正确"
    if "." not in d:
        return "", "", "请填写完整域名，如 example.com"
    return d, "DOMAIN-SUFFIX", None


# ============================================================================
#  高级配置表单：把最常改的几个字段读出来 / 写回去
#  目标：让用户不用碰 YAML 也能改核心参数。
# ============================================================================
ADV_FIELDS = [
    {
        "key": "mixed-port", "type": "int", "default": 7890,
        "label": "混合代理端口",
        "help": "HTTP 和 SOCKS5 共用这个端口，给需要手动设代理的设备用。",
    },
    {
        "key": "tproxy-port", "type": "int", "default": 7895,
        "label": "透明代理端口",
        "help": "局域网透明代理用这个端口，由 nft 规则转发过来。改了要同步改 nft 规则。",
    },
    {
        "key": "redir-port", "type": "int", "default": 7892,
        "label": "本机重定向端口",
        "help": "本机程序走透明代理时用。",
    },
    {
        "key": "log-level", "type": "enum", "default": "info",
        "options": ["silent", "error", "warning", "info", "debug"],
        "label": "日志级别",
        "help": "排查问题时调成 debug，平时用 info。",
    },
    {
        "key": "mode", "type": "enum", "default": "rule",
        "options": ["rule", "global", "direct"],
        "label": "运行模式",
        "help": "rule=按规则分流（推荐）；global=全部走代理；direct=全部直连。",
    },
    {
        "key": "find-process-mode", "type": "enum", "default": "off",
        "options": ["off", "strict", "always"],
        "label": "进程匹配",
        "help": "按进程名分流时用。透明代理场景用不到，保持 off 省 CPU。",
    },
    {
        "key": "ipv6", "type": "bool", "default": False,
        "label": "启用 IPv6",
        "help": "国内 IPv6 普及但 fake-ip 支持不好，容易出问题，建议关闭。",
    },
    {
        "key": "tcp-concurrent", "type": "bool", "default": True,
        "label": "TCP 并发",
        "help": "同时连接多个 IP 取最快的，能加快网页打开，建议开启。",
    },
    {
        "key": "allow-lan", "type": "bool", "default": True,
        "label": "允许局域网连接",
        "help": "允许家里其他设备直接连代理端口。只用透明代理时不必须。",
    },
    {
        "key": "external-controller", "type": "str", "default": "127.0.0.1:9090",
        "label": "控制接口地址",
        "help": "Web 面板和 mihomo 通信的地址。用 unix socket 时这里不影响。",
    },
]

_DNS_KEYS = ["fake-ip-range", "enhanced-mode", "listen"]


def parse_advanced(text: str) -> dict[str, Any]:
    """从配置里读出高级表单需要的字段。"""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return {}
    out: dict[str, Any] = {}
    for f in ADV_FIELDS:
        out[f["key"]] = doc.get(f["key"], f["default"])
    dns = doc.get("dns") or {}
    for k in _DNS_KEYS:
        out[f"dns.{k}"] = dns.get(k, "")
    return out


def _render_scalar(key: str, val: Any, ftype: str) -> str:
    if ftype == "bool":
        return f"{key}: {'true' if val else 'false'}"
    if ftype == "int":
        return f"{key}: {int(val)}"
    if ftype == "enum":
        return f"{key}: {val}"
    return f"{key}: {_yaml_str(val)}"


def write_advanced(text: str, values: dict[str, Any]) -> tuple[str, str | None]:
    """把高级表单的值写回配置。只改表单覆盖的字段，其余原样保留。"""
    lines = text.splitlines()

    for f in ADV_FIELDS:
        key = f["key"]
        if key not in values:
            continue
        raw = values[key]

        # 类型转换与校验
        if f["type"] == "int":
            try:
                raw = int(raw)
                if raw <= 0 or raw > 65535:
                    return text, f"{f['label']} 必须是 1-65535 的端口号"
            except (TypeError, ValueError):
                return text, f"{f['label']} 必须是数字"
        elif f["type"] == "bool":
            raw = bool(raw)
        elif f["type"] == "enum":
            if raw not in f.get("options", []):
                return text, f"{f['label']} 只能是 {'/'.join(f.get('options', []))}"
        else:
            raw = str(raw)

        new_line = _render_scalar(key, raw, f["type"])
        replaced = False
        for i, line in enumerate(lines):
            if re.match(rf"^{re.escape(key)}\s*:", line):
                # 保留行尾注释
                comment = ""
                if "#" in line:
                    comment = "  " + line[line.index("#"):]
                lines[i] = new_line + comment
                replaced = True
                break
        if not replaced:
            # 插入到第一个顶层键之前
            for i, line in enumerate(lines):
                if line and not line[0].isspace() and not line.startswith("#"):
                    lines.insert(i, new_line)
                    break
            else:
                lines.append(new_line)

    new_text = "\n".join(lines) + "\n"

    # dns.* 字段：只处理 listen / fake-ip-range / enhanced-mode
    for k in _DNS_KEYS:
        full = f"dns.{k}"
        if full not in values:
            continue
        val = values[full]
        new_text = _set_dns_key(new_text, k, val)

    return new_text, None


def _set_dns_key(text: str, key: str, val: str) -> str:
    """在 dns: 段内修改一个二级键。"""
    lines = text.splitlines()
    in_dns = False
    dns_indent = None
    for i, line in enumerate(lines):
        if re.match(r"^dns\s*:", line):
            in_dns = True
            continue
        if in_dns:
            if line and not line[0].isspace():
                break                      # dns 段结束
            if dns_indent is None and line.strip():
                dns_indent = len(line) - len(line.lstrip())
            if re.match(rf"^\s+{re.escape(key)}\s*:", line):
                indent = len(line) - len(line.lstrip())
                lines[i] = " " * indent + f"{key}: {_yaml_str(val)}"
                return "\n".join(lines) + "\n"
    return text


def advanced_fields() -> list[dict[str, Any]]:
    """给前端用的字段定义（去掉内部字段）。"""
    return [
        {k: v for k, v in f.items() if k in ("key", "type", "default", "options", "label", "help")}
        for f in ADV_FIELDS
    ]
