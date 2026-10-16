/* 灰港 · NPC Agent 调试台（零外部依赖，纯原生 JS） */

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const api = async (path, opts) => {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
};
const pct = (v) => `${Math.max(0, Math.min(100, Number(v) || 0))}%`;

let NPC_LIST = [];
let CURRENT = "";
let LAST = null;
let HISTORY = [];

/* ------------------------------------------------------------------ */
/* 启动                                                               */
/* ------------------------------------------------------------------ */
async function boot() {
  await Promise.all([loadRuntime(), loadNpcs()]);
  await Promise.all([loadWorld(), loadTools(), loadCards(), loadTraces()]);
  loadQuickPrompts();
  bindUI();
}

function bindUI() {
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    send($("#input").value);
  });
  $("#btnReset").addEventListener("click", () => {
    HISTORY = [];
    LAST = null;
    renderMessages();
    setPane("plan", '<p class="empty">发送一条消息后这里会显示分层计划。</p>');
  });
  $("#btnExport").addEventListener("click", exportMarkdown);
  $("#tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
    $(`#pane-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "trace") loadTraces();
    if (btn.dataset.tab === "eval") loadEval();
  });
}

/* ------------------------------------------------------------------ */
/* 加载                                                               */
/* ------------------------------------------------------------------ */
async function loadRuntime() {
  const rt = await api("/api/runtime");
  const badge = $("#engineBadge");
  badge.textContent = rt.degraded
    ? `离线演示脑${rt.llm_configured ? "" : "（未配置 API Key）"}`
    : `大模型 ${rt.brain}`;
  badge.className = `badge ${rt.degraded ? "demo" : "llm"}`;
  $("#chatHint").textContent = rt.degraded
    ? "当前为离线演示脑：功能链路完整，台词来自预设话术，数值仍由权威世界状态给出。"
    : "已接入大模型：台词由模型生成，数值与状态仍由服务端权威确认。";
}

async function loadNpcs() {
  const data = await api("/api/npcs");
  NPC_LIST = data.npcs || [];
  const bar = $("#npcBar");
  bar.innerHTML = "";
  NPC_LIST.forEach((npc) => {
    const card = el("button", "npc-card");
    card.innerHTML = `<div class="n">${npc.name}<span class="r"> · ${npc.role_type}</span></div>
      <div class="r">${npc.id}</div><div class="i">${npc.identity}</div>`;
    card.addEventListener("click", () => selectNpc(npc.id));
    card.dataset.id = npc.id;
    bar.appendChild(card);
  });
  if (NPC_LIST.length) selectNpc(CURRENT || NPC_LIST[0].id);
}

function selectNpc(id) {
  CURRENT = id;
  document.querySelectorAll(".npc-card").forEach((c) =>
    c.classList.toggle("active", c.dataset.id === id));
  const npc = NPC_LIST.find((n) => n.id === id);
  $("#chatTitle").textContent = npc ? `与「${npc.name}」对话` : "对话";
  HISTORY = [];
  LAST = null;
  renderMessages();
  loadQuickPrompts();
  loadTools();
}

async function loadQuickPrompts() {
  const box = $("#quick");
  const npc = NPC_LIST.find((n) => n.id === CURRENT);
  box.innerHTML = "";
  const base = ["你好", "你知道什么传闻？", "现在几点？天气怎么样？"];
  const extra = {
    npc_herbalist_ailin: ["我想接这个委托 q_102", "学徒小陶有什么线索吗", "告诉我城门布防细节"],
    npc_guard_calvin: ["北门什么时候宵禁？", "开城门，放我过去", "说说你的布防安排"],
    npc_peddler_john: ["银叶草多少钱一份？", "我要买两份银叶草", "你的货源是哪里？"],
  }[CURRENT] || [];
  [...extra, ...base].forEach((t) => {
    const b = el("button", null, t);
    b.addEventListener("click", () => send(t));
    box.appendChild(b);
  });
}

async function loadWorld() {
  const w = await api("/api/world");
  const pane = $("#pane-state");
  pane.innerHTML = "";
  if (!LAST) {
    pane.appendChild(el("h3", "seg", "世界"));
    pane.appendChild(worldBlock(w));
    pane.appendChild(el("p", "empty", "与 NPC 交谈后，这里会显示关系、任务与背包。"));
  }
}

function worldBlock(w) {
  const frag = document.createDocumentFragment();
  const kv = el("dl", "kv");
  Object.values(w.environment || {}).forEach((env) => {
    kv.appendChild(el("dt", null, "世界"));
    kv.appendChild(el("dd", null, `${env.name} · ${env.time} · ${env.weather}`));
  });
  Object.values(w.gates || {}).forEach((gates) => {
    Object.entries(gates).forEach(([gid, g]) => {
      kv.appendChild(el("dt", null, "门禁"));
      kv.appendChild(el("dd", null, `${g.name || gid}：${g.open ? "开启" : "闭闸"}（${g.note || ""}）`));
    });
  });
  frag.appendChild(kv);
  return frag;
}

async function loadTools() {
  const data = await api(`/api/tools?npc_id=${encodeURIComponent(CURRENT)}`);
  const pane = $("#pane-tools");
  pane.innerHTML = "";
  pane.appendChild(el("h3", "seg", `工具白名单（${CURRENT || "全部"}）`));
  const t = el("table", "mini");
  t.innerHTML = `<thead><tr><th>工具</th><th>类别</th><th>读/写</th><th>可用</th></tr></thead><tbody></tbody>`;
  const tb = t.querySelector("tbody");
  (data.tools || []).forEach((tool) => {
    const tr = el("tr");
    tr.innerHTML = `<td>${tool.name}</td><td>${tool.category}</td>
      <td>${tool.read_only ? "只读" : (tool.requires_confirmation ? "写·需确认" : "写")}</td>
      <td>${tool.allowed === false ? '<span class="chip bad">无权限</span>'
        : (tool.allowed === undefined ? "—" : '<span class="chip ok">允许</span>')}</td>`;
    tr.title = tool.description || "";
    tb.appendChild(tr);
  });
  pane.appendChild(t);
  if (data.stats && Object.keys(data.stats.by_tool || {}).length) {
    pane.appendChild(el("h3", "seg", "调用统计"));
    const s = el("table", "mini");
    s.innerHTML = `<thead><tr><th>工具</th><th>次数</th></tr></thead><tbody></tbody>`;
    const sb = s.querySelector("tbody");
    Object.entries(data.stats.by_tool).forEach(([k, v]) => {
      const tr = el("tr");
      tr.innerHTML = `<td>${k}</td><td class="num">${v}</td>`;
      sb.appendChild(tr);
    });
    pane.appendChild(s);
    pane.appendChild(el("p", "muted", `总调用 ${data.stats.total} 次，失败 ${data.stats.failures} 次`));
  }
}

async function loadCards() {
  const pane = $("#pane-cards");
  pane.innerHTML = "";
  NPC_LIST.forEach((npc) => {
    const box = el("div");
    box.appendChild(el("h3", "seg", `${npc.name} · ${npc.role_type}（v${npc.version}）`));
    const kv = el("dl", "kv");
    const rows = [
      ["身份", npc.identity], ["性格", npc.personality.join("、")],
      ["价值观", npc.values.join("、")],
      ["长期目标", (npc.goals.long_term || []).join("；")],
      ["当前目标", (npc.goals.current || []).join("；")],
      ["可知范围", npc.knowledge_scope.join("、")],
      ["禁忌", npc.taboos.join("；")], ["语气", npc.voice.tone],
    ];
    rows.forEach(([k, v]) => {
      if (!v) return;
      kv.appendChild(el("dt", null, k));
      kv.appendChild(el("dd", null, v));
    });
    box.appendChild(kv);
    const tags = el("div");
    (npc.tool_permissions || []).forEach((p) => tags.appendChild(el("span", "tag", p)));
    box.appendChild(tags);
    pane.appendChild(box);
  });
}

async function loadTraces() {
  const data = await api("/api/traces?limit=15");
  const pane = $("#pane-trace");
  pane.innerHTML = "";
  pane.appendChild(el("h3", "seg", `最近轨迹（共 ${data.total} 条）`));
  if (!data.traces.length) {
    pane.appendChild(el("p", "empty", "暂无轨迹。"));
    return;
  }
  data.traces.forEach((t) => {
    const box = el("div", "detail");
    const head = el("div");
    head.innerHTML = `<span class="chip">${t.intent || "-"}</span>
      <span class="chip">${t.engine || "-"}</span>
      <span class="chip">${Math.round(t.latency_ms)}ms</span>
      ${t.degraded ? '<span class="chip warn">降级</span>' : ""}
      ${t.errors && t.errors.length ? `<span class="chip bad">${t.errors.length} 错误</span>` : ""}`;
    box.appendChild(head);
    box.appendChild(el("div", null, `「${t.user_message}」`));
    if (t.tool_calls && t.tool_calls.length) {
      const p = el("pre", null,
        t.tool_calls.map((c) =>
          `${c.tool}(${JSON.stringify(c.args || {})}) → ${c.success ? "ok" : c.reason_code}\n` +
          `  ${c.error || ""}${c.state_delta && Object.keys(c.state_delta).length ? "\n  Δ " + JSON.stringify(c.state_delta) : ""}`
        ).join("\n"));
      box.appendChild(p);
    }
    pane.appendChild(box);
  });
}

async function loadEval() {
  const pane = $("#pane-eval");
  pane.innerHTML = "";
  pane.appendChild(el("h3", "seg", "MVP 验收场景"));
  const listBox = el("div");
  pane.appendChild(listBox);

  const btn = el("button", "primary", "运行回放评测");
  btn.style.marginTop = "10px";
  pane.appendChild(btn);

  const out = el("div");
  out.style.marginTop = "12px";
  pane.appendChild(out);

  const scenarios = await api("/api/evaluations/scenarios");
  (scenarios.scenarios || []).forEach((s) => {
    const d = el("div", "detail");
    d.style.padding = "6px 0";
    d.innerHTML = `<strong>${s.title}</strong><br>
      <span class="chip">${s.dimension}</span><span class="chip">${s.npc_id}</span>
      <span class="muted">${s.acceptance}</span>`;
    listBox.appendChild(d);
  });

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "评测中…";
    out.innerHTML = "";
    try {
      const r = await api("/api/evaluations/replay", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      const head = el("div");
      head.innerHTML = `<span class="chip ${r.passed_checks === r.checks ? "ok" : "bad"}">
        断言 ${r.passed_checks}/${r.checks}</span>
        <span class="chip">场景 ${r.passed_scenarios}/${r.scenarios}</span>
        <span class="chip">p95 ${r.latency_ms.p95}ms</span>`;
      out.appendChild(head);
      (r.results || []).forEach((res) => {
        const d = el("div", "detail");
        d.style.padding = "6px 0";
        const failed = [];
        (res.turns || []).forEach((t) => (t.checks || []).forEach((c) => {
          if (!c.ok) failed.push(`${t.message} → ${c.name}（${c.detail}）`);
        }));
        d.innerHTML = `<span class="chip ${res.ok ? "ok" : "bad"}">${res.ok ? "PASS" : "FAIL"}</span>
          <strong>${res.title}</strong>
          ${failed.length ? `<pre>${failed.join("\n")}</pre>` : ""}`;
        out.appendChild(d);
      });
      toast("评测完成");
    } catch (e) {
      out.innerHTML = `<p class="chip bad">${e.message}</p>`;
    } finally {
      btn.disabled = false;
      btn.textContent = "运行回放评测";
    }
  });
}

/* ------------------------------------------------------------------ */
/* 对话                                                               */
/* ------------------------------------------------------------------ */
async function send(text) {
  const msg = (text || "").trim();
  if (!msg || !CURRENT) return;
  $("#input").value = "";
  HISTORY.push({ role: "me", text: msg });
  renderMessages();

  const btn = $("#composer button");
  btn.disabled = true;
  try {
    const res = await api(`/api/npcs/${CURRENT}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ npc_id: CURRENT, player_id: "p_001",
                             instance_id: "world_01", message: msg }),
    });
    LAST = res;
    HISTORY.push({ role: "npc", text: res.reply, res });
  } catch (e) {
    HISTORY.push({ role: "npc", text: `（请求失败：${e.message}）` });
  } finally {
    btn.disabled = false;
    renderMessages();
    renderStatePane(res1() || {});
    await loadWorldPane();
  }
}

function res1() { return LAST; }

function renderMessages() {
  const box = $("#messages");
  box.innerHTML = "";
  if (!HISTORY.length) {
    const npc = NPC_LIST.find((n) => n.id === CURRENT);
    const tip = el("div", "detail");
    tip.innerHTML = `<strong>提示</strong><br>选好角色后直接说话即可。试试：接委托、问价、买货、请求开城门。<br>
      敏感操作会让你二次确认；越界提问会被角色拒绝（这是设计，不是故障）。`;
    box.appendChild(tip);
    if (npc && npc.greeting) {
      const g = el("div", "msg");
      const b = el("div", "bubble", npc.greeting);
      g.appendChild(b);
      g.appendChild(el("div", "meta")).appendChild(el("span", "chip", "开场白 · 角色卡配置"));
      box.appendChild(g);
    }
    return;
  }
  HISTORY.forEach((m) => box.appendChild(renderBubble(m)));
  box.scrollTop = box.scrollHeight;
}

function renderBubble(m) {
  const node = el("div", `msg ${m.role === "me" ? "me" : ""}`);
  node.appendChild(el("div", "bubble", m.text));
  const meta = el("div", "meta");
  node.appendChild(meta);

  if (m.role !== "npc" || !m.res) return node;
  const r = m.res;

  meta.appendChild(el("span", "chip", `意图 ${r.intent}`));
  meta.appendChild(el("span", "chip", `情绪 ${r.presentation.emotion}`));
  meta.appendChild(el("span", "chip", `动作 ${r.presentation.action}`));
  meta.appendChild(el("span", "chip", `表情 ${r.presentation.expression}`));
  if (r.tool_result) {
    const ok = r.tool_result.success;
    meta.appendChild(el("span", `chip ${ok ? "ok" : "bad"}`,
      `${r.tool_result.tool} ${ok ? "成功" : (r.tool_result.reason_code || "失败")}`));
  }
  if (r.state_changed) meta.appendChild(el("span", "chip warn", "已改变世界状态"));
  if (r.degraded) meta.appendChild(el("span", "chip warn", "离线降级"));

  const details = el("details", "detail");
  details.appendChild(el("summary", null, "查看计划 / 权威结果 / 数据来源 / 记忆"));

  const blocks = [];
  if (r.plan) {
    blocks.push(`【分层计划】
战略：${r.plan.strategic || "-"}
战术：${r.plan.tactical || "-"}
执行：${(r.plan.executive || []).join(" → ") || "-"}${r.plan.reactive ? `\n反应：${r.plan.reactive}` : ""}`);
  }
  if (r.tool_result) {
    blocks.push(`【工具调用】${r.tool_result.tool}
success=${r.tool_result.success}  read_only=${r.tool_result.read_only}
message=${r.tool_result.message || "-"}
error=${r.tool_result.error || "-"}
state_delta=${JSON.stringify(r.tool_result.state_delta || {})}
latency=${r.tool_result.latency_ms}ms`);
  }
  if (r.relation_changes && r.relation_changes.length) {
    blocks.push("【关系变更（规则服务确认）】\n" + r.relation_changes
      .map((c) => `${c.field}: ${c.before} → ${c.after}（${c.delta >= 0 ? "+" : ""}${c.delta}）因为 ${c.reason}`)
      .join("\n"));
  }
  if (r.memory_writes && r.memory_writes.length) {
    blocks.push("【写入记忆】\n" + r.memory_writes
      .map((w) => `${w.subject}（可信度 ${w.confidence}）：${w.text}`).join("\n"));
  }
  if (r.sources && r.sources.length) {
    blocks.push("【数据来源】\n" + r.sources
      .map((s) => `- [${s.kind}] ${s.detail}（可信度 ${s.confidence}）`).join("\n"));
  }
  if (r.degradations && r.degradations.length) {
    blocks.push("【降级说明】\n" + r.degradations.map((d) => `- ${d}`).join("\n"));
  }
  const pre = el("pre", null, blocks.join("\n\n"));
  details.appendChild(pre);
  node.appendChild(details);
  return node;
}

/* ------------------------------------------------------------------ */
/* 侧栏：状态 / 计划                                                  */
/* ------------------------------------------------------------------ */
function renderStatePane(r) {
  const pane = $("#pane-state");
  pane.innerHTML = "";
  if (!r || !r.tool_result) { /* 仍渲染世界 */ }

  const npc = NPC_LIST.find((n) => n.id === CURRENT);
  pane.appendChild(el("h3", "seg", "角色"));
  const kv = el("dl", "kv");
  kv.appendChild(el("dt", null, "角色"));
  kv.appendChild(el("dd", null, npc ? `${npc.name} · ${npc.role_type}` : CURRENT));
  if (r && r.presentation) {
    kv.appendChild(el("dt", null, "情绪"));
    kv.appendChild(el("dd", null, `${r.presentation.emotion}（${r.presentation.intensity}）`));
    kv.appendChild(el("dt", null, "表现"));
    kv.appendChild(el("dd", null, `${r.presentation.expression} / ${r.presentation.action}`));
  }
  pane.appendChild(kv);

  if (r && r.relation_changes && r.relation_changes.length) {
    pane.appendChild(el("h3", "seg", "本次关系变化"));
    const ul = el("ul", "list");
    r.relation_changes.forEach((c) => ul.appendChild(el("li", null,
      `${c.field} ${c.before} → ${c.after}（${c.reason}）`)));
    pane.appendChild(ul);
  }
  pane.appendChild(el("h3", "seg", "世界"));
  pane.appendChild(el("p", "muted", "完整世界状态见下方「世界」区块（点击刷新）。"));
  const btn = el("button", "ghost small", "刷新世界状态");
  btn.addEventListener("click", loadWorldPane);
  pane.appendChild(btn);
  const wbox = el("div");
  wbox.id = "worldBox";
  pane.appendChild(wbox);
  loadWorldPane();
}

async function loadWorldPane() {
  const box = $("#worldBox") || $("#pane-state");
  const w = await api("/api/world");
  const target = $("#worldBox");
  if (!target) return;
  target.innerHTML = "";
  const kv = el("dl", "kv");
  Object.values(w.environment || {}).forEach((env) => {
    kv.appendChild(el("dt", null, "世界"));
    kv.appendChild(el("dd", null, `${env.name} · ${env.time} · ${env.weather}`));
  });
  Object.values(w.gates || {}).forEach((gates) => Object.entries(gates).forEach(([gid, g]) => {
    kv.appendChild(el("dt", null, "门禁"));
    kv.appendChild(el("dd", null, `${g.name || gid}：${g.open ? "开启" : "闭闸"}`));
  }));
  kv.appendChild(el("dt", null, "玩家银币"));
  kv.appendChild(el("dd", null, String((w.players.p_001 || {}).gold)));
  kv.appendChild(el("dt", null, "背包"));
  kv.appendChild(el("dd", null, JSON.stringify((w.players.p_001 || {}).inventory || {})));
  target.appendChild(kv);

  const quests = Object.entries(w.quests || {});
  if (quests.length) {
    target.appendChild(el("h3", "seg", "任务状态（服务端权威）"));
    const t = el("table", "mini");
    t.innerHTML = `<thead><tr><th>任务</th><th>状态</th><th>版本</th></tr></thead><tbody></tbody>`;
    const tb = t.querySelector("tbody");
    quests.forEach(([k, q]) => {
      const tr = el("tr");
      tr.innerHTML = `<td>${k}</td><td>${q.status} / 阶段${q.stage}</td><td class="num">${q.version}</td>`;
      tb.appendChild(tr);
    });
    target.appendChild(t);
  }
}

function setPane(tab, html) {
  const p = $(`#pane-${tab}`);
  if (p) p.innerHTML = html;
}

/* ------------------------------------------------------------------ */
/* 导出                                                               */
/* ------------------------------------------------------------------ */
async function exportMarkdown() {
  try {
    const r = await api("/api/export/markdown?player_id=p_001");
    const blob = new Blob([r.markdown], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "npc-agent-snapshot.md";
    a.click();
    URL.revokeObjectURL(a.href);
    toast("已导出 Markdown 快照");
  } catch (e) {
    toast(`导出失败：${e.message}`);
  }
}

let toastTimer = null;
function toast(text) {
  let t = document.querySelector(".toast");
  if (!t) { t = el("div", "toast"); document.body.appendChild(t); }
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2200);
}

/* 未使用但保留：柱状进度 */
function bars(title, items) {
  const box = el("div");
  box.appendChild(el("h3", "seg", title));
  items.forEach(([label, value]) => {
    const d = el("div");
    d.appendChild(el("div", "muted", `${label} · ${value}`));
    const bar = el("div", "bar");
    const i = el("i");
    i.style.width = pct(value);
    bar.appendChild(i);
    d.appendChild(bar);
    box.appendChild(d);
  });
  return box;
}

boot().catch((e) => {
  document.body.innerHTML = `<pre style="padding:24px;color:#d4380d">初始化失败：${e.message}</pre>`;
});
