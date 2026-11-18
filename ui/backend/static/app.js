/* ==========================================================================
   Home Proxy UI v3
   信息架构按「用户任务」组织，而非后端模块：
     home     能不能上网 / 不能就告诉我哪坏了
     traffic  这网站为啥走代理 / 加条规则
     nodes    换机场、看节点延迟
     doctor   深度诊断
     settings 内核参数
   ========================================================================== */
"use strict";

/* ------------------------------------------------------------ 基础 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const fmtBytes = (n) => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n), i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return i === 0 ? `${Math.round(v)} B` : `${v.toFixed(1)} ${u[i]}`;
};
const fmtRate = (n) => `${fmtBytes(n)}/s`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let toastTimer;
function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

const latClass = (d) => (d == null ? "n" : d < 200 ? "g" : d < 600 ? "y" : "r");

/* ------------------------------------------------------------ API */
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401) { toast("登录已过期，请刷新页面", true); throw new Error("401"); }
  const txt = await res.text();
  let data = null;
  if (txt) { try { data = JSON.parse(txt); } catch { data = txt; } }
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || `HTTP ${res.status}`);
  return data;
}
const get = (p) => api(p);
const send = (m, p, b) => api(p, { method: m, body: b ? JSON.stringify(b) : undefined });

/* ------------------------------------------------------------ 状态 */
const state = {
  view: "home",
  readOnly: false,
  traffic: { up: 0, down: 0, t: 0 },
  hist: { up: [], down: [] },
  timer: null,
  rules: [],
  conns: [],
  groups: [],
  nodes: [],
};

const HIST = 60;

/* ------------------------------------------------------------ 视图 */
const VIEW_ORDER = ["home", "traffic", "nodes", "doctor", "settings"];

function switchView(view) {
  state.view = view;
  $$(".rail-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== `page-${view}`));
  loadView(view);
}

function loadView(v) {
  if (v === "home") loadHome();
  if (v === "traffic") loadTraffic();
  if (v === "nodes") loadNodes();
  if (v === "doctor") { runCheck(); loadConns(); connectLogs(); }
  if (v === "settings") loadSettings();
}

/* ------------------------------------------------------------ 首页 */
function drawChart(canvas, down, up) {
  const dpr = devicePixelRatio || 1;
  const w = canvas.clientWidth || 600, h = canvas.clientHeight || 140;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const max = Math.max(...down, ...up, 1024);
  const step = w / (HIST - 1);

  // 网格
  ctx.strokeStyle = "rgba(130,155,195,.08)";
  ctx.lineWidth = 1;
  for (let i = 1; i <= 3; i++) {
    const y = (h / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }

  const line = (data, color, fill) => {
    if (data.length < 2) return;
    const x0 = w - (data.length - 1) * step;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = x0 + i * step, y = h - 4 - (v / max) * (h - 12);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.lineWidth = 1.8; ctx.strokeStyle = color;
    ctx.lineJoin = "round"; ctx.stroke();
    if (fill) {
      ctx.lineTo(x0 + (data.length - 1) * step, h);
      ctx.lineTo(x0, h); ctx.closePath();
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, fill); g.addColorStop(1, "transparent");
      ctx.fillStyle = g; ctx.fill();
    }
  };
  line(down, "rgb(77,212,240)", "rgba(77,212,240,.22)");
  line(up, "rgb(139,124,248)", "rgba(139,124,248,.16)");
}

function renderTraffic(ov) {
  const now = performance.now();
  const dt = (now - state.traffic.t) / 1000;
  let dR = 0, uR = 0;
  if (state.traffic.t && dt > 0.05) {
    dR = Math.max(0, (ov.traffic.down - state.traffic.down) / dt);
    uR = Math.max(0, (ov.traffic.up - state.traffic.up) / dt);
  }
  state.traffic = { up: ov.traffic.up, down: ov.traffic.down, t: now };

  state.hist.down.push(dR); state.hist.up.push(uR);
  if (state.hist.down.length > HIST) state.hist.down.shift();
  if (state.hist.up.length > HIST) state.hist.up.shift();

  $("#traffic-now").textContent = `↓ ${fmtRate(dR)} · ↑ ${fmtRate(uR)}`;
  $("#traffic-total").textContent = `累计 ↓${ov.traffic.down_h} · ↑${ov.traffic.up_h}`;
  $("#foot-down").textContent = fmtBytes(dR);
  $("#foot-up").textContent = fmtBytes(uR);
  $("#s-conn").textContent = ov.connections;
  $("#s-mem").textContent = ov.memory.human;
  $("#s-ver").textContent = ov.version;
  drawChart($("#chart"), state.hist.down, state.hist.up);
}

function renderVerdict(d) {
  const box = $("#verdict");
  box.className = "verdict " + (d.status || "ok");

  const icon = d.status === "ok"
    ? `<svg viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></svg>`
    : `<svg viewBox="0 0 24 24"><path d="M12 8v5M12 16h.01"/><circle cx="12" cy="12" r="9"/></svg>`;
  $("#verdict-icon").innerHTML = icon;

  const titles = {
    ok: "一切正常",
    warn: "基本正常",
    error: "需要处理",
  };
  $("#verdict-title").textContent = titles[d.status] || "未知";
  $("#verdict-sub").textContent = d.verdict || "";

  const bad = (d.checks || []).filter((c) => !c.ok && c.level !== "warn");
  const warn = (d.checks || []).filter((c) => !c.ok && c.level === "warn");
  const list = bad.length ? bad : warn;

  $("#verdict-actions").innerHTML = list.length
    ? `<button class="btn btn-sm btn-primary" id="v-goto">查看 ${list.length} 项问题</button>`
    : `<button class="btn btn-sm btn-ghost" id="v-goto">查看体检详情</button>`;
  $("#v-goto").onclick = () => switchView("doctor");
}

function renderMode(mode) {
  $$("#seg-mode button").forEach((b) => b.classList.toggle("active", b.dataset.v === mode));
  $("#mode-hint").textContent = {
    rule: "按 gfwlist 自动分流",
    global: "全部流量走代理",
    direct: "全部直连，不走代理",
  }[mode] || "";
}

async function loadHome() {
  markRefresh(".panel", true);
  try {
    const [ov, chk] = await Promise.all([
      get("/api/overview"),
      get("/api/health/check").catch(() => null),
    ]);
    renderTraffic(ov);
    renderMode(ov.mode);
    if (chk) renderVerdict(chk);

    // 选节点策略（PROXY 组当前选的是哪套规则）
    const px = await get("/api/proxies").catch(() => null);
    if (px) {
      const main = px.groups.find((g) => g.name === "PROXY") || px.groups[0];
      const cur = main?.now || "balance";
      state.strategy = cur;
      $$("#seg-strategy button").forEach((b) =>
        b.classList.toggle("active", b.dataset.v === cur));
      const HINT = {
        balance: "多节点分摊流量，某个节点挂了立刻换下一个；全部挂了自动退回直连",
        fallback: "按顺序用第一个能用的节点，主节点挂掉才切下一个（最长等 60 秒）",
        auto: "每 5 分钟测速，永远用延迟最低的节点",
        DIRECT: "不走代理，全部直接连接（排查问题或临时全直连时用）",
      };
      $("#strategy-hint").textContent = HINT[cur] || cur;
    }
  } catch (e) {
    toast(`加载失败：${e.message}`, true);
  } finally {
    markRefresh(".panel", false);
  }
}

function markRefresh(sel, on) {
  $$(sel).forEach((el) => el.classList.toggle("refreshing", on));
}

/* ------------------------------------------------------------ 分流页 */
async function loadTraffic() {
  try {
    const [prov, rl] = await Promise.all([
      get("/api/rule-providers"),
      get("/api/rules"),
    ]);
    const RSET_LABEL = {
      gfw: "常见被墙网站（gfwlist）",
      "gfw-extra": "被墙网站补充清单",
      cncidr: "国内 IP 段（直连）",
      direct: "已知可直连的网站",
      private: "内网地址",
      reject: "广告与追踪",
    };
    $("#rulesets").innerHTML = prov.providers.map((p) => `
      <div class="rset">
        <div class="rset-main">
          <b>${esc(RSET_LABEL[p.name] || p.name)}</b>
          <span>${p.count.toLocaleString()} 条 · ${esc((p.updated_at || "").replace("T", " ").slice(5, 16))}</span>
        </div>
        <div class="rset-act">
          <button class="btn btn-sm btn-ghost" data-upd="${esc(p.name)}">更新</button>
        </div>
      </div>`).join("");

    $$("[data-upd]").forEach((b) => b.addEventListener("click", async () => {
      if (state.readOnly) return toast("只读模式", true);
      b.disabled = true; b.textContent = "…";
      try {
        await send("PUT", `/api/rule-providers/${encodeURIComponent(b.dataset.upd)}`);
        toast("已刷新"); loadTraffic();
      } catch (e) { toast(`刷新失败：${e.message}`, true); b.disabled = false; b.textContent = "刷新"; }
    }));

    state.rules = rl.rules;
    $("#rules-count").textContent = `共 ${rl.total} 条`;
    renderRules();
    loadMyRules();
  } catch (e) {
    toast(`加载失败：${e.message}`, true);
  }
}

function renderRules() {
  const kw = ($("#rule-filter")?.value || "").trim().toLowerCase();
  const rows = state.rules.filter((r) =>
    !kw || String(r.payload).toLowerCase().includes(kw) || String(r.proxy).toLowerCase().includes(kw));
  $("#rules-table tbody").innerHTML = rows.slice(0, 400).map((r) => `
    <tr>
      <td class="dim">${r.index}</td>
      <td><span class="tag">${esc(r.type)}</span></td>
      <td class="host" title="${esc(r.payload)}">${esc(r.payload)}</td>
      <td><span class="tag ${r.proxy === "DIRECT" ? "dir" : r.proxy === "REJECT" ? "rej" : "pxy"}">${esc(r.proxy)}</span></td>
      <td class="mono dim">${r.hits || 0}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="empty">无匹配</td></tr>`;
}

/* 「我的规则」：用户自己指定的网站 */
async function loadMyRules() {
  try {
    const d = await get("/api/my-rules");
    const box = $("#myrules");
    $("#myrules-count").textContent = d.rules.length ? `${d.rules.length} 条` : "";
    if (!d.rules.length) {
      box.innerHTML = `<div class="myrule-empty">
        还没有指定任何网站。<br>
        自动分流判断不准时，点右上角「+ 指定一个」覆盖它。
      </div>`;
      return;
    }
    const label = { DIRECT: "直连", PROXY: "走代理", REJECT: "拦截" };
    const cls = { DIRECT: "dir", PROXY: "pxy", REJECT: "rej" };
    box.innerHTML = d.rules.map((r) => `
      <div class="myrule">
        <b>${esc(r.domain)}</b>
        <div class="row-gap">
          <span class="tag ${cls[r.action] || ""}">${label[r.action] || r.action}</span>
          <button class="btn btn-sm btn-ghost" data-delrule="${esc(r.domain)}">删除</button>
        </div>
      </div>`).join("");
    $$("[data-delrule]").forEach((b) => b.onclick = () => delMyRule(b.dataset.delrule));
  } catch (e) {
    toast(`加载我的规则失败：${e.message}`, true);
  }
}

async function delMyRule(domain) {
  if (!(await confirmDialog("删除规则", `不再为 <b>${esc(domain)}</b> 单独指定去向，恢复自动判断。`, false))) return;
  try {
    await send("DELETE", `/api/my-rules/${encodeURIComponent(domain)}`);
    toast("已删除");
    loadMyRules(); loadTraffic();
  } catch (e) { toast(`删除失败：${e.message}`, true); }
}

let editingRule = false;
function openRuleModal() {
  $("#rule-domain").value = "";
  $("#rule-action").value = "PROXY";
  setFieldErr("#rule-domain", "");
  $("#rule-modal").classList.remove("hidden");
  setTimeout(() => $("#rule-domain").focus(), 60);
}

async function saveMyRule() {
  const domain = $("#rule-domain").value.trim();
  if (!domain) return setFieldErr("#rule-domain", "请填写域名");
  setFieldErr("#rule-domain", "");
  const btn = $("#rule-save");
  btn.disabled = true;
  try {
    await send("POST", "/api/my-rules", { domain, action: $("#rule-action").value });
    toast("已保存，立即生效");
    $("#rule-modal").classList.add("hidden");
    loadMyRules(); loadTraffic();
  } catch (e) {
    setFieldErr("#rule-domain", e.message);
  } finally { btn.disabled = false; }
}

async function probeDomain() {
  const raw = $("#probe-input").value.trim();
  if (!raw) return toast("请输入网站域名", true);

  const box = $("#probe-result");
  box.innerHTML = `<div class="probe-hit"><span class="probe-why">查询中…</span></div>`;
  try {
    const r = await get(`/api/probe?domain=${encodeURIComponent(raw)}`);
    const map = {
      PROXY:  { cls: "pxy", text: "会走代理" },
      DIRECT: { cls: "dir", text: "直接连接" },
      REJECT: { cls: "rej", text: "会被拦截" },
    };
    const v = map[r.verdict] || { cls: "dir", text: r.verdict };
    box.innerHTML = `
      <div class="probe-hit ${r.verdict === "PROXY" ? "is-proxy" : "is-direct"}">
        <span class="probe-dom">${esc(r.domain)}</span>
        <span class="probe-arrow">→</span>
        <span class="probe-verdict ${v.cls}">${v.text}</span>
        <span class="probe-why">${esc(r.reason)}</span>
      </div>`;
  } catch (e) {
    box.innerHTML = `<div class="probe-hit" style="border-color:rgba(255,107,129,.3)">
      <span class="probe-why" style="color:var(--err)">${esc(e.message)}</span></div>`;
  }
}

/* ------------------------------------------------------------ 节点页 */
async function loadNodes() {
  markRefresh(".panel", true);
  try {
    const [subs, px] = await Promise.all([
      get("/api/subscriptions").catch(() => ({ subscriptions: [], read_only: false })),
      get("/api/proxies"),
    ]);
    state.readOnly = subs.read_only;

    $("#subs-count").textContent = `${subs.subscriptions.length} 个`;
    $("#subs").innerHTML = subs.subscriptions.length
      ? subs.subscriptions.map((s) => {
          const info = s.info || {};
          const up = s.updated_at && !String(s.updated_at).startsWith("0001")
            ? new Date(s.updated_at).toLocaleString() : "尚未拉取";
          const badge = !s.enabled
            ? '<span class="tag dir">已停用</span>'
            : s.alive > 0
              ? `<span class="tag pxy">${s.alive}/${s.count} 可用</span>`
              : s.loaded ? '<span class="tag rej">无可用节点</span>' : '<span class="tag">未加载</span>';
          return `
            <div class="rset">
              <div class="rset-main">
                <b>${esc(s.name)} ${badge}</b>
                <span>${esc(up)} · 每 ${Math.round(s.interval / 3600)}h 更新</span>
              </div>
              <div class="rset-act">
                <button class="btn btn-sm btn-ghost" data-subedit="${esc(s.name)}">编辑</button>
                <button class="btn btn-sm btn-ghost" data-subreload="${esc(s.name)}">更新</button>
                <button class="btn btn-sm btn-danger" data-subdel="${esc(s.name)}">删除</button>
              </div>
            </div>`;
        }).join("")
      : `<div class="empty">还没有订阅。点「+ 添加订阅」填入机场或自建面板给的链接。</div>`;

    const map = Object.fromEntries(subs.subscriptions.map((s) => [s.name, s]));
    $$("[data-subedit]").forEach((b) => b.onclick = () => openSubModal(map[b.dataset.subedit]));
    $$("[data-subdel]").forEach((b) => b.onclick = () => delSub(b.dataset.subdel));
    $$("[data-subreload]").forEach((b) => b.onclick = async () => {
      b.disabled = true;
      try {
        await send("POST", `/api/subscriptions/${encodeURIComponent(b.dataset.subreload)}/reload`);
        toast("已重新拉取"); setTimeout(loadNodes, 1200);
      } catch (e) { toast(`失败：${e.message}`, true); b.disabled = false; }
    });

    // 策略组：用中文解释每个组的含义
    const GTYPE = {
      Selector: "手动选择",
      URLTest: "自动选最快",
      Fallback: "按顺序容灾",
      LoadBalance: "轮流使用",
    };
    // 用「怎么选」来描述，而不是用组名
    const GNAME = {
      PROXY: "总开关：决定用哪套选节点规则",
      fallback: "固定顺序：主节点能用就一直用，挂了才换下一个",
      auto: "选最快的：每隔几分钟测速，始终用延迟最低的",
      GLOBAL: "全局",
    };
    const shown = px.groups.filter((g) => g.name !== "GLOBAL");
    const GDESC = {
      fallback:
        "按下面的顺序用，第一个能用就一直用；它挂了才换下一个。" +
        "检测靠定时心跳（默认每题 60 秒一次），所以节点刚挂到被切换最多可能等 60 秒。" +
        "适合「有明确偏好，比如专线优先」。",
      auto:
        "每隔几分钟给下面所有节点测速，永远用延迟最低的那个。" +
        "主节点变慢会自动换到快的，变快了也会换回来。" +
        "适合「只想网速快，不在乎用哪个」。",
      PROXY:
        "这是一道总开关：选定上面哪一套规则，所有走代理的流量就按它执行。",
    };
    // 每组附一行「关键参数」，让用户知道切换速度
    const GMETA = {
      fallback: "检测间隔 60 秒 → 故障后最长 60 秒切换",
      auto: "测速间隔 300 秒 → 5 分钟重新选一次最快",
    };
    $("#groups").innerHTML = shown.map((g) => `
      <div class="group">
        <div class="group-head">
          <div>
            <b>${esc(GNAME[g.name] || g.name)}</b>
            <span class="group-type">${esc(GTYPE[g.type] || g.type)}</span>
          </div>
        </div>
        ${GDESC[g.name] ? `<p class="group-desc">${esc(GDESC[g.name])}</p>` : ""}
        ${GMETA[g.name] ? `<p class="group-meta">${esc(GMETA[g.name])}</p>` : ""}
        <div class="opts">
          ${g.all.map((n) => `<button class="opt ${n === g.now ? "active" : ""}"
            title="${esc(n)}"
            data-group="${esc(g.name)}" data-node="${esc(n)}">${esc(n)}</button>`).join("")}
        </div>
        ${g.name !== "PROXY" ? `<p class="group-current">当前：<b>${esc(g.now || "—")}</b></p>` : ""}
      </div>`).join("");

    $$(".opt").forEach((b) => b.onclick = async () => {
      if (state.readOnly) return toast("只读模式", true);
      try {
        await send("PUT", `/api/proxies/${encodeURIComponent(b.dataset.group)}`, { name: b.dataset.node });
        toast(`已切到 ${b.dataset.node}`); loadNodes();
      } catch (e) { toast(`切换失败：${e.message}`, true); }
    });

    // 节点
    state.nodes = px.nodes.filter((n) =>
      !["DIRECT", "REJECT", "COMPATIBLE", "PASS", "PASS-RULE", "REJECT-DROP"].includes(n.name));
    renderNodes();
  } catch (e) {
    toast(`加载失败：${e.message}`, true);
  } finally {
    markRefresh(".panel", false);
  }
}

function renderNodes() {
  const list = state.nodes || [];
  const kw = ($("#node-search")?.value || "").trim().toLowerCase();
  const sort = $("#node-sort")?.value || "default";

  let rows = list.filter((n) => !kw || n.name.toLowerCase().includes(kw));
  if (sort === "delay") {
    rows = [...rows].sort((a, b) => (a.delay ?? 1e9) - (b.delay ?? 1e9));
  } else if (sort === "name") {
    rows = [...rows].sort((a, b) => a.name.localeCompare(b.name, "zh"));
  }

  $("#nodes-count").textContent = `${list.length} 个`;
  $("#nodes").innerHTML = rows.map((n) => `
    <div class="node">
      <span class="node-nm" title="${esc(n.name)}">${esc(n.name)}</span>
      <span class="node-rt">
        <span class="lat ${latClass(n.delay)}">${
          n.delay != null ? n.delay + "ms" : (n.alive ? "—" : "不可用")}</span>
      </span>
    </div>`).join("") || `<div class="empty">${kw ? "没有匹配的节点" : "暂无节点，先添加订阅"}</div>`;
}

async function testAllNodes() {
  const btn = $("#btn-test-all") || $("#btn-quick-test");
  if (btn) { btn.disabled = true; }
  const groups = [...new Set($$(".opt").map((o) => o.dataset.group))];
  if (!groups.length) {
    const px = await get("/api/proxies").catch(() => null);
    px?.groups.forEach((g) => groups.push(g.name));
  }
  let best = null;
  for (const g of groups) {
    try {
      const r = await get(`/api/delay/group/${encodeURIComponent(g)}`);
      Object.entries(r.delays || {}).forEach(([n, d]) => {
        const el = $(`.lat[data-node="${CSS.escape(n)}"]`);
        if (el) { el.className = `lat ${latClass(d)}`; el.textContent = d + "ms"; }
        if (!best || d < best.d) best = { n, d };
      });
    } catch { /* 单组失败不影响其他 */ }
  }
  if (btn) { btn.disabled = false; }
  toast(best ? `最快：${best.n}（${best.d}ms）` : "测速完成");
  const tr = $("#test-result");
  if (tr) tr.textContent = best ? `${best.d}ms` : "完成";
}

/* ------------------------------------------------------------ 订阅 CRUD */
let editingSub = null;

function openSubModal(sub) {
  editingSub = sub ? sub.name : null;
  $("#sub-modal-title").textContent = sub ? `编辑 ${sub.name}` : "添加订阅";
  $("#sub-name").value = sub ? sub.name : "";
  $("#sub-name").disabled = !!sub;
  $("#sub-url").value = "";
  $("#sub-url").placeholder = sub ? "留空表示不修改" : "https://...";
  $("#sub-interval").value = sub ? Math.round(sub.interval / 3600) : 6;
  $("#sub-filter").value = sub ? (sub.filter || "") : "";
  $("#sub-exclude").value = sub ? (sub.exclude_filter || "") : "";
  $("#sub-modal").classList.remove("hidden");
  setTimeout(() => (sub ? $("#sub-url") : $("#sub-name")).focus(), 60);
}
function closeSubModal() { $("#sub-modal").classList.add("hidden"); editingSub = null; }

async function saveSub() {
  const name = editingSub || $("#sub-name").value.trim();
  const url = $("#sub-url").value.trim();
  setFieldErr("#sub-name", "");
  setFieldErr("#sub-url", "");
  let bad = false;
  if (!name) { setFieldErr("#sub-name", "名称不能为空"); bad = true; }
  else if (!editingSub && !/^[A-Za-z0-9_-]+$/.test(name)) {
    setFieldErr("#sub-name", "只能用字母数字下划线短横线"); bad = true;
  }
  if (!url && !editingSub) { setFieldErr("#sub-url", "链接不能为空"); bad = true; }
  else if (url && !/^https?:\/\//i.test(url)) { setFieldErr("#sub-url", "必须以 http 开头"); bad = true; }
  if (bad) return;

  const body = {
    interval: (parseInt($("#sub-interval").value, 10) || 6) * 3600,
    filter: $("#sub-filter").value.trim(),
    exclude_filter: $("#sub-exclude").value.trim(),
  };
  if (url) body.url = url;

  const btn = $("#sub-save");
  btn.disabled = true;
  try {
    if (editingSub) await send("PUT", `/api/subscriptions/${encodeURIComponent(editingSub)}`, body);
    else await send("POST", "/api/subscriptions", { name, ...body });
    toast(editingSub ? "已更新" : "已添加");
    closeSubModal(); loadNodes();
  } catch (e) {
    toast(`保存失败：${e.message}`, true);
  } finally { btn.disabled = false; }
}

async function delSub(name) {
  if (!(await confirmDialog("删除订阅", `将从配置移除 <b>${esc(name)}</b>，其节点立即失效。`))) return;
  try { await send("DELETE", `/api/subscriptions/${encodeURIComponent(name)}`); toast("已删除"); loadNodes(); }
  catch (e) { toast(`失败：${e.message}`, true); }
}

/* ------------------------------------------------------------ 诊断页 */
function renderChecks(d) {
  $("#doctor-summary").textContent = `${d.passed}/${d.total}`;
  $("#checks").innerHTML = d.checks.map((c) => {
    const cls = c.ok ? "ok" : (c.level === "warn" ? "warn" : "bad");
    const ico = c.ok ? "✓" : (c.level === "warn" ? "!" : "✕");
    return `<div class="check ${cls}">
      <div class="check-ico">${ico}</div>
      <div class="check-body">
        <div class="check-nm">${esc(c.name)}</div>
        <div class="check-dt">${esc(c.detail)}</div>
        ${c.fix ? `<pre class="check-fix" data-copy="${esc(c.fix)}">${esc(c.fix)}</pre>` : ""}
      </div>
    </div>`;
  }).join("");
  $$(".check-fix").forEach((el) => el.onclick = async () => {
    try { await navigator.clipboard.writeText(el.dataset.copy); toast("已复制"); }
    catch { toast("复制失败", true); }
  });
}

async function runCheck() {
  markRefresh(".panel", true);
  try {
    const d = await get("/api/health/check");
    renderChecks(d);
    renderVerdict(d);
  } catch (e) {
    toast(`体检失败：${e.message}`, true);
  } finally { markRefresh(".panel", false); }
}

async function loadConns() {
  try {
    const d = await get("/api/connections");
    state.conns = d.connections;
    $("#conn-count").textContent = `${d.total} 条 · ↓${d.down_h} ↑${d.up_h}`;
    renderConns();
  } catch { /* 忽略 */ }
}

function renderConns() {
  const kw = ($("#conn-filter")?.value || "").trim().toLowerCase();
  const rows = state.conns.filter((c) =>
    !kw || String(c.host).toLowerCase().includes(kw) || String(c.rule).toLowerCase().includes(kw));
  $("#conn-table tbody").innerHTML = rows.slice(0, 200).map((c) => `
    <tr>
      <td class="host" title="${esc(c.host)}">${esc(c.host)}</td>
      <td><span class="tag">${esc(c.rule)}</span></td>
      <td class="mono dim">${esc((c.chains || []).slice(-1)[0] || "-")}</td>
      <td class="mono">↓${c.down_h} / ↑${c.up_h}</td>
      <td class="mono dim">${esc(c.duration)}</td>
      <td><button class="btn btn-sm btn-ghost" data-close="${esc(c.id)}">断开</button></td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty">当前没有活动连接</td></tr>`;

  $$("[data-close]").forEach((b) => b.onclick = async () => {
    if (state.readOnly) return toast("只读模式", true);
    try { await send("DELETE", `/api/connections/${b.dataset.close}`); loadConns(); }
    catch (e) { toast(`失败：${e.message}`, true); }
  });
}

/* 日志 */
const logBuf = [];
let logWs = null, logPaused = false;

function appendLog(o) {
  if (logPaused) return;
  const box = $("#logbox");
  if (!box) return;
  const p = o.payload || "";
  const lv = (o.type || "info").toLowerCase();
  const kw = ($("#logs-filter")?.value || "").trim().toLowerCase();
  if (kw && !p.toLowerCase().includes(kw)) return;

  let cls = "logline " + lv;
  if (/match RuleSet\(gfw\).*using/.test(p)) cls += " hit-proxy";
  else if (/match RuleSet\(direct\).*using/.test(p)) cls += " hit-direct";

  const div = document.createElement("div");
  div.className = cls;
  div.innerHTML = `<span class="ts">${new Date().toLocaleTimeString("zh-CN", { hour12: false })}</span>` +
                  `<span class="lv">${esc(lv)}</span><span>${esc(p)}</span>`;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 70;
  box.appendChild(div);
  logBuf.push(div);
  while (logBuf.length > 400) logBuf.shift().remove();
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function connectLogs() {
  const level = $("#logs-level")?.value || "info";
  if (logWs) { try { logWs.close(); } catch {} }
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  logWs = new WebSocket(`${proto}//${location.host}/ws/logs?level=${level}`);
  logWs.onmessage = (ev) => {
    try {
      const o = JSON.parse(ev.data);
      if (o.type !== "error") appendLog(o);
    } catch { appendLog({ type: "info", payload: ev.data }); }
  };
  logWs.onclose = () => { setTimeout(() => { if (state.view === "doctor") connectLogs(); }, 3000); };
}

/* ------------------------------------------------------------ 设置页 */
async function loadAdvanced() {
  try {
    const d = await get("/api/advanced");
    const box = $("#adv-grid");
    if (!box) return;
    box.innerHTML = d.fields.map((f) => {
      const v = d.values[f.key];
      let input;
      if (f.type === "bool") {
        input = `<label class="switch"><input type="checkbox" data-adv="${esc(f.key)}" data-type="bool"
          ${v ? "checked" : ""}><span></span></label>`;
      } else if (f.type === "enum") {
        input = `<select class="input" data-adv="${esc(f.key)}" data-type="enum">
          ${(f.options || []).map((o) => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}
        </select>`;
      } else if (f.type === "int") {
        input = `<input class="input" type="number" data-adv="${esc(f.key)}" data-type="int"
          value="${esc(v ?? "")}">`;
      } else {
        input = `<input class="input" data-adv="${esc(f.key)}" data-type="str" value="${esc(v ?? "")}">`;
      }
      return `<div class="adv-field">
        <label>${esc(f.label)}</label>
        ${input}
        <span class="adv-help">${esc(f.help)}</span>
      </div>`;
    }).join("");
  } catch (e) {
    toast(`加载高级配置失败：${e.message}`, true);
  }
}

async function saveAdvanced() {
  const body = {};
  $$("[data-adv]").forEach((el) => {
    const k = el.dataset.adv;
    const t = el.dataset.type;
    body[k] = t === "bool" ? el.checked : (t === "int" ? Number(el.value) : el.value);
  });
  const btn = $("#btn-adv-save");
  btn.disabled = true;
  try {
    const r = await send("PUT", "/api/advanced", body);
    toast(r.ok ? "已保存并生效" : `已保存，重载失败：${r.reload}`, !r.ok);
  } catch (e) {
    toast(`保存失败：${e.message}`, true);
  } finally { btn.disabled = false; }
}

async function loadSettings() {
  try {
    const s = await get("/api/settings");
    $("#sw-ipv6").checked = !!s.ipv6;
    $("#sw-tcpconc").checked = !!s.tcp_concurrent;
    $("#sw-lan").checked = !!s.allow_lan;
    $("#sel-log").value = s.log_level || "info";
    const modeSel = $("#sel-mode-inline");
    if (modeSel) modeSel.value = s.mode || "rule";
    $("#ports-info").innerHTML = [
      ["混合 (HTTP+SOCKS)", s.ports.mixed],
      ["TPROXY (局域网)", s.ports.tproxy],
      ["REDIRECT (本机)", s.ports.redir],
    ].map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v || "-")}</dd>`).join("");

    await loadAdvanced();

    const cf = await get("/api/config/file");
    $("#config-editor").value = cf.content;
    $("#config-editor").readOnly = !cf.writable;
    $("#btn-config-save").disabled = !cf.writable;
  } catch (e) {
    toast(`加载失败：${e.message}`, true);
  }
}

async function patchSettings(payload) {
  if (state.readOnly) return toast("只读模式", true);
  try { await send("PATCH", "/api/settings", payload); toast("已应用"); loadSettings(); }
  catch (e) { toast(`失败：${e.message}`, true); loadSettings(); }
}

/* ------------------------------------------------------------ 弹层工具 */
function confirmDialog(title, msg, danger = true) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.className = "modal";
    back.innerHTML = `<div class="modal-box" style="width:min(430px,92vw)">
      <div class="modal-head"><h3>${esc(title)}</h3></div>
      <div class="form">
        <p style="margin:0;font-size:13px;color:var(--txt-2);line-height:1.7">${msg}</p>
        <div class="form-actions">
          <button class="btn" data-no>取消</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-yes>确定</button>
        </div>
      </div></div>`;
    document.body.appendChild(back);
    const done = (v) => { back.remove(); resolve(v); };
    back.querySelector("[data-no]").onclick = () => done(false);
    back.querySelector("[data-yes]").onclick = () => done(true);
    back.onclick = (e) => { if (e.target === back) done(false); };
    back.querySelector("[data-yes]").focus();
  });
}

function setFieldErr(sel, msg) {
  const label = $(sel)?.closest("label");
  if (!label) return;
  label.classList.toggle("invalid", !!msg);
  const e = label.querySelector(".field-err");
  if (e) e.textContent = msg || "";
}

/* ------------------------------------------------------------ 主题 / 键盘 */
function toggleTheme() {
  const next = (document.documentElement.dataset.theme || "dark") === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
  drawChart($("#chart"), state.hist.down, state.hist.up);
}

function bindKeys() {
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.key === "Escape") {
      document.querySelectorAll(".modal:not(.hidden)").forEach((m) => m.classList.add("hidden"));
      return;
    }
    const n = parseInt(e.key, 10);
    if (n >= 1 && n <= VIEW_ORDER.length) { switchView(VIEW_ORDER[n - 1]); return; }
    if (e.key.toLowerCase() === "t") toggleTheme();
    if (e.key.toLowerCase() === "r") { if (state.view === "doctor") runCheck(); else loadHome(); }
    if (e.key.toLowerCase() === "f") { if (state.view === "nodes") testAllNodes(); }
    if (e.key === "?") $("#kbd-modal").classList.remove("hidden");
  });
}

/* ------------------------------------------------------------ 绑定 */
function bind() {
  $$(".rail-item").forEach((b) => b.onclick = () => switchView(b.dataset.view));
  $("#btn-theme").onclick = toggleTheme;

  // 模式切换
  $$("#seg-mode button").forEach((b) => b.onclick = () => {
    patchSettings({ mode: b.dataset.v }).then(() => renderMode(b.dataset.v));
  });

  $$("#seg-strategy button").forEach((b) => b.onclick = async () => {
    if (state.readOnly) return toast("只读模式", true);
    try {
      await send("PUT", "/api/proxies/PROXY", { name: b.dataset.v });
      toast(`已切换到「${b.textContent.trim()}」`);
      loadHome();
    } catch (e) { toast(`切换失败：${e.message}`, true); }
  });
  $("#btn-quick-test").onclick = testAllNodes;
  $("#btn-test-all").onclick = testAllNodes;
  $("#btn-recheck").onclick = runCheck;
  $("#btn-refresh-rules").onclick = async () => {
    const btn = $("#btn-refresh-rules");
    btn.disabled = true; btn.textContent = "更新中…";
    try {
      const prov = await get("/api/rule-providers");
      for (const p of prov.providers) {
        await send("PUT", `/api/rule-providers/${encodeURIComponent(p.name)}`).catch(() => {});
      }
      toast("清单已更新");
      loadTraffic();
    } catch (e) { toast(`更新失败：${e.message}`, true); }
    finally { btn.disabled = false; btn.textContent = "更新"; }
  };
  $("#btn-toggle-raw").onclick = (e) => {
    const p = $("#raw-rules-panel");
    p.classList.toggle("hidden");
    e.target.textContent = p.classList.contains("hidden") ? "显示完整规则表" : "收起完整规则表";
  };
  $("#btn-add-rule").onclick = openRuleModal;
  $("#rule-modal-close").onclick = () => $("#rule-modal").classList.add("hidden");
  $("#rule-cancel").onclick = () => $("#rule-modal").classList.add("hidden");
  $("#rule-save").onclick = saveMyRule;
  $("#btn-probe").onclick = probeDomain;
  $("#probe-input").addEventListener("keydown", (e) => { if (e.key === "Enter") probeDomain(); });
  $("#rule-filter").addEventListener("input", renderRules);
  $("#conn-filter").addEventListener("input", renderConns);
  $("#node-search")?.addEventListener("input", renderNodes);
  $("#node-sort")?.addEventListener("change", renderNodes);

  $("#btn-add-sub").onclick = () => openSubModal(null);
  $("#sub-modal-close").onclick = closeSubModal;
  $("#sub-cancel").onclick = closeSubModal;
  $("#sub-save").onclick = saveSub;

  $("#btn-close-all").onclick = async () => {
    if (state.readOnly) return toast("只读模式", true);
    if (!(await confirmDialog("断开全部连接", "正在进行的下载会中断。"))) return;
    try { await send("DELETE", "/api/connections"); toast("已断开"); loadConns(); }
    catch (e) { toast(`失败：${e.message}`, true); }
  };

  $("#sw-ipv6").onchange = (e) => patchSettings({ ipv6: e.target.checked });
  $("#sw-tcpconc").onchange = (e) => patchSettings({ "tcp-concurrent": e.target.checked });
  $("#sw-lan").onchange = (e) => patchSettings({ "allow-lan": e.target.checked });
  $("#sel-log").onchange = (e) => patchSettings({ "log-level": e.target.value });

  $("#sel-mode-inline")?.addEventListener("change", (e) => {
    patchSettings({ mode: e.target.value }).then(() => renderMode(e.target.value));
  });
  $("#btn-adv-save")?.addEventListener("click", saveAdvanced);
  $("#btn-toggle-raw-config")?.addEventListener("click", (e) => {
    const box = $("#raw-config-box");
    box.classList.toggle("hidden");
    e.target.textContent = box.classList.contains("hidden") ? "显示源文件" : "隐藏源文件";
    if (!box.classList.contains("hidden")) loadConfigFile();
  });
  $("#btn-config-save").onclick = async () => {
    const btn = $("#btn-config-save");
    btn.disabled = true;
    try {
      const r = await send("PUT", "/api/config/file", { content: $("#config-editor").value });
      toast(r.ok ? "已保存并重载" : `已保存，重载失败：${r.reload}`, !r.ok);
    } catch (e) { toast(`保存失败：${e.message}`, true); }
    finally { btn.disabled = false; }
  };
  $("#btn-config-reload").onclick = async () => {
    try { await send("POST", "/api/config/reload"); toast("已重载"); }
    catch (e) { toast(`失败：${e.message}`, true); }
  };

  $("#btn-logs-toggle").onclick = (e) => {
    logPaused = !logPaused;
    e.target.textContent = logPaused ? "继续" : "暂停";
  };
  $("#logs-level").onchange = connectLogs;
  $("#logs-filter").addEventListener("input", () => {
    const kw = $("#logs-filter").value.trim().toLowerCase();
    logBuf.forEach((el) => { el.style.display = !kw || el.textContent.toLowerCase().includes(kw) ? "" : "none"; });
  });

  $("#kbd-close").onclick = () => $("#kbd-modal").classList.add("hidden");
  $("#config-editor")?.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const ta = e.target, s = ta.selectionStart;
      ta.value = ta.value.slice(0, s) + "  " + ta.value.slice(ta.selectionEnd);
      ta.selectionStart = ta.selectionEnd = s + 2;
    }
  });

  window.addEventListener("resize", () => drawChart($("#chart"), state.hist.down, state.hist.up));
  document.addEventListener("visibilitychange", () => { if (!document.hidden) loadView(state.view); });
}

/* ------------------------------------------------------------ 启动 */
async function boot() {
  bind();
  bindKeys();
  switchView("home");
  state.timer = setInterval(() => {
    if (document.hidden) return;
    if (state.view === "home") loadHome();
    if (state.view === "doctor") loadConns();
  }, 1200);
}

boot();
