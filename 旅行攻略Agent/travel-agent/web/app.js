/* 旅行攻略 Agent 助手 —— 前端逻辑（零依赖） */
'use strict';

const $ = (id) => document.getElementById(id);
const state = { trip: null, tripId: null, overview: '', trace: [], status: null, tab: 'days' };

/* ---------------- 基础请求 ---------------- */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let data = null;
  try { data = await res.json(); } catch (_) { data = null; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

/* ---------------- 状态栏 ---------------- */
function renderStatus(st) {
  state.status = st;
  const chips = [];
  if (st.engine === 'llm') {
    const demo = st.demo_brain;
    chips.push(chip(demo ? '离线演示脑（合成数据）' : `大模型：${st.llm_model}`, demo ? 'warn' : 'ok'));
  } else {
    chips.push(chip('规则引擎（未接入大模型）', 'warn'));
  }
  chips.push(chip(`坐标源：${st.geo_source}`, st.geo_source === 'amap' ? 'ok' : ''));
  chips.push(chip(`工具 ${st.tools.length} 个`, ''));
  chips.push(chip(`提示词工具模式：${st.llm_tool_mode}`, ''));
  $('statusChips').innerHTML = chips.join('');

  const banner = $('banner');
  if (!st.llm_configured && !st.demo_brain) {
    banner.classList.remove('hidden');
    banner.innerHTML = '尚未配置大模型。请在项目根目录 <code>.env</code> 中填写 ' +
      '<code>TRAVEL_LLM_BASE_URL</code> / <code>TRAVEL_LLM_MODEL</code> / <code>TRAVEL_LLM_API_KEY</code>，' +
      '然后点右上角「重载配置」。当前状态下只能解析需求，无法生成地点。';
  } else if (st.demo_brain) {
    banner.classList.remove('hidden');
    banner.innerHTML = '当前是<b>离线演示脑</b>：地点为合成数据、坐标为未知，仅用于看流程。' +
      '填好 <code>.env</code> 后点「重载配置」即可切换为真实大模型。';
  } else {
    banner.classList.add('hidden');
  }
  $('engineHint').textContent = st.engine === 'llm'
    ? (st.demo_brain ? '演示脑模式' : '大模型驱动：理解需求 + 目的地知识')
    : '规则引擎：仅能解析需求与计算，无地点数据';
}

function chip(text, kind) {
  const cls = kind === 'ok' ? 'chip chip-ok' : kind === 'warn' ? 'chip chip-warn' : 'chip';
  return `<span class="${cls}">${esc(text)}</span>`;
}

/* ---------------- 对话 ---------------- */
function pushMsg(role, text, kind) {
  const log = $('chatLog');
  const wrap = document.createElement('div');
  if (kind === 'note') {
    wrap.className = `msg-note${role === 'warn' ? ' warn' : ''}`;
    wrap.textContent = text;
  } else {
    wrap.className = `msg msg-${role}`;
    const body = document.createElement('div');
    body.className = 'msg-body';
    body.textContent = text;
    wrap.appendChild(body);
  }
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return wrap;
}

function pushLoading() {
  const log = $('chatLog');
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-bot';
  wrap.innerHTML = '<div class="msg-body"><span class="loading"><i></i><i></i><i></i></span></div>';
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
  return wrap;
}

async function sendMessage(text) {
  const message = (text || $('chatInput').value).trim();
  if (!message) return;
  pushMsg('user', message);
  $('chatInput').value = '';
  const loading = pushLoading();
  setBusy(true);
  try {
    const data = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify({ message, trip_id: state.tripId }),
    });
    loading.remove();
    pushMsg('bot', data.reply || '（无回复）');
    if (data.warning) pushMsg('warn', `降级提示：${data.warning}`, 'note');
    if (data.slots && data.slots.destinations && data.slots.destinations.length && !data.trip) {
      pushMsg('note', `已识别：${JSON.stringify(data.slots)}`, 'note');
    }
    if (data.suggestions && data.suggestions.length && !data.trip) {
      renderExamples(data.suggestions);
    }
    if (data.trip) {
      state.trip = data.trip;
      state.tripId = data.trip.trip_id;
      state.trace = data.trace || state.trace;
      if (data.intent === 'plan') state.overview = data.reply || '';
      renderPlan();
    }
  } catch (err) {
    loading.remove();
    pushMsg('warn', `出错了：${err.message}`, 'note');
  } finally {
    setBusy(false);
  }
}

function setBusy(busy) {
  $('btnSend').disabled = busy;
  $('btnPlan').disabled = busy;
  $('btnSend').textContent = busy ? '生成中…' : '发送';
}

/* ---------------- 结构化生成 ---------------- */
async function planDirect() {
  const dests = $('fDest').value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean);
  if (!dests.length) { alert('请至少填一个目的地'); return; }
  const payload = {
    origin: $('fOrigin').value.trim(),
    destinations: dests,
    start_date: $('fStart').value,
    end_date: $('fEnd').value,
    budget: $('fBudget').value ? Number($('fBudget').value) : null,
    preferences: $('fPrefs').value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean),
    pace: $('fPace').value,
    travelers: Number($('fTravelers').value || 1),
    rooms: Number($('fRooms').value || 1),
    mobility: $('fMobility').value,
  };
  if (!payload.start_date || !payload.end_date) { alert('请填开始与结束日期'); return; }
  const loading = pushLoading();
  setBusy(true);
  try {
    const data = await api('/api/plan', { method: 'POST', body: JSON.stringify(payload) });
    loading.remove();
    state.trip = data.trip;
    state.tripId = data.trip.trip_id;
    state.trace = data.trace || [];
    state.overview = data.overview || '';
    pushMsg('bot', data.overview || '行程已生成。');
    renderPlan();
  } catch (err) {
    loading.remove();
    pushMsg('warn', `生成失败：${err.message}`, 'note');
  } finally {
    setBusy(false);
  }
}

/* ---------------- 行程渲染 ---------------- */
function renderPlan() {
  const trip = state.trip;
  if (!trip) return;
  const req = trip.request;
  $('planTitle').textContent =
    `${req.destinations.join('、')} · ${trip.days.length} 天 · ${req.travelers} 人`;
  $('btnExport').disabled = false;
  renderTab();
}

function renderTab() {
  const body = $('planBody');
  const trip = state.trip;
  if (!trip) return;
  if (state.tab === 'days') body.innerHTML = viewDays(trip);
  else if (state.tab === 'budget') body.innerHTML = viewBudget(trip);
  else if (state.tab === 'evidence') body.innerHTML = viewEvidence(trip);
  else body.innerHTML = viewTrace();
  body.scrollTop = 0;
}

function viewDays(trip) {
  const req = trip.request;
  const head = `
    <div class="trip-summary">
      <h3>${esc(req.destinations.join('、'))} · ${trip.days.length} 天行程</h3>
      <div class="trip-meta">
        ${chip(`出发地 ${req.origin || '未填'}`, '')}
        ${chip(`${req.start_date} ~ ${req.end_date}`, '')}
        ${chip(`${req.travelers} 人 / ${req.rooms} 间`, '')}
        ${chip(`节奏 ${paceLabel(req.pace)}`, '')}
        ${chip(`引擎 ${trip.engine}`, trip.engine === 'llm' ? 'ok' : 'warn')}
        ${req.preferences.length ? chip(`偏好 ${req.preferences.join('、')}`, '') : ''}
      </div>
      ${state.overview ? `<div class="trip-overview">${esc(state.overview)}</div>` : ''}
    </div>`;

  const intercity = (trip.intercity || []).length
    ? `<div class="day-card"><div class="day-head"><span class="day-date">城际交通</span></div>
        <div class="items">${trip.intercity.map((leg) => `
          <div class="item"><div class="item-time"><b>${esc(leg.label)}</b>${Math.floor(leg.minutes / 60)}h${leg.minutes % 60}m</div>
          <div class="item-main">
            <div class="item-name">${esc(leg.note || '')}</div>
            <div class="item-sub">约 ${leg.distance_km} km
              <span class="sep">·</span>¥${fmt(leg.cost_low)}~${fmt(leg.cost_high)}
              <span class="sep">·</span>来源 ${esc(leg.source)}</div>
          </div></div>`).join('')}</div></div>`
    : '';

  const days = trip.days.map((day) => {
    const items = day.items.length ? day.items.map(itemRow).join('')
      : '<div class="item"><div class="item-main item-sub">当日无可用安排（候选点不足或天气受限）</div></div>';
    const notes = (day.notes || []).map((n) => esc(n)).join('；');
    const risk = (day.risk_notes || []).length
      ? `<div class="notes warn">风险：${esc(day.risk_notes.join('；'))}</div>` : '';
    return `
      <div class="day-card">
        <div class="day-head">
          <span class="day-date">${esc(day.date)} ${esc(day.weekday)}</span>
          <span class="day-city">${esc(day.city)}</span>
          ${weatherBadge(day.weather)}
          <span class="day-stat">步行 ${day.walking_km} km
            <span class="sep">·</span>在途 ${Math.round(day.active_minutes / 60)} h
            <span class="sep">·</span>${day.items.length} 个安排</span>
        </div>
        <div class="items">${items}</div>
        ${notes ? `<div class="notes">${notes}</div>` : ''}
        ${risk}
      </div>`;
  }).join('');

  return head + intercity + days;
}

function itemRow(item) {
  const catCls = (item.category === '餐饮' || item.category === '小吃') ? 'cat meal'
    : item.indoor ? 'cat indoor' : 'cat';
  const trans = item.transport
    ? `<span class="sep">·</span>${esc(item.transport.label)} ${item.transport.minutes} 分
       <span class="sep">·</span>${item.transport.distance_km} km
       <span class="sep">·</span>来源 ${esc(item.transport.source)}`
    : '';
  const cost = (item.cost_low || item.cost_high)
    ? `<span class="sep">·</span>门票 ¥${fmt(item.cost_low)}~${fmt(item.cost_high)}` : '';
  const ev = (item.evidence || []).length
    ? `<details class="evidence"><summary>数据来源（${item.evidence.length}）</summary>
         <ul>${item.evidence.map((e) => `<li>${esc(e)}</li>`).join('')}</ul></details>` : '';
  return `
    <div class="item">
      <div class="item-time"><b>${esc(item.arrive)}</b>${esc(item.leave)}</div>
      <div class="item-main">
        <div class="item-name">
          ${esc(item.name)}
          <span class="${catCls}">${esc(item.category)}</span>
          ${item.indoor ? '<span class="cat indoor">室内</span>' : ''}
          <span class="cat score">匹配 ${item.score}</span>
        </div>
        <div class="item-sub">停留 ${item.stay_minutes} 分钟${trans}${cost}</div>
        ${item.reason ? `<div class="item-sub">推荐理由：${esc(item.reason)}</div>` : ''}
        ${item.risk ? `<div class="item-risk">风险：${esc(item.risk)}</div>` : ''}
        ${ev}
      </div>
    </div>`;
}

function weatherBadge(wx) {
  if (!wx) return '<span class="weather unknown">天气无数据</span>';
  if (wx.basis === 'unavailable') return '<span class="weather unknown">天气不可用</span>';
  const rainy = wx.rain_prob > 60;
  const basis = wx.basis === 'forecast' ? '' : `（${wx.basis === 'climatology' ? '去年同期实测' : wx.basis}）`;
  const aqi = wx.aqi != null ? ` AQI ${wx.aqi}` : '';
  return `<span class="weather ${rainy ? 'rain' : ''}">${esc(wx.condition)} ${wx.temp_low}~${wx.temp_high}℃ · 雨 ${wx.rain_prob}%${aqi}${basis}</span>`;
}

function viewBudget(trip) {
  if (!trip.budget) return '<div class="empty"><p>没有预算数据。</p></div>';
  const b = trip.budget;
  const rows = b.lines.map((l) => `<tr>
      <td>${esc(l.category)}</td>
      <td>${fmt(l.low)}</td>
      <td>${fmt(l.high)}</td>
      <td class="basis">${esc(l.basis)}</td>
    </tr>`).join('');
  const isOver = b.user_budget && b.total_low > b.user_budget;
  const kind = isOver ? 'bad' : (b.user_budget && b.total_high <= b.user_budget ? 'good' : '');
  return `
    <table class="budget">
      <thead><tr><th>项目</th><th>下限（元）</th><th>上限（元）</th><th>依据</th></tr></thead>
      <tbody>
        ${rows}
        <tr class="total"><td>合计</td><td>${fmt(b.total_low)}</td><td>${fmt(b.total_high)}</td>
          <td class="basis">${esc(b.basis || '')}</td></tr>
      </tbody>
    </table>
    <div class="verdict ${kind}">${esc(b.verdict)}${b.user_budget ? `　你的预算：¥${fmt(b.user_budget)}` : ''}</div>
    ${b.advice.length ? `<ul class="advice">${b.advice.map((a) => `<li>${esc(a)}</li>`).join('')}</ul>` : ''}
    <div class="item-sub" style="margin-top:14px">说明：价格为区间估算，住宿与餐饮依据城市消费水平画像；
      门票与交通取自行程内各项实测/已知区间。出行前请以官方渠道为准。</div>`;
}

function viewEvidence(trip) {
  return `
    <div class="sec-title">数据来源</div>
    ${(trip.evidence || []).length
      ? `<ul class="evidence-list">${trip.evidence.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>`
      : '<p class="hint">本次未取到任何外部数据。</p>'}
    <div class="sec-title">提示与风险（${(trip.warnings || []).length}）</div>
    ${(trip.warnings || []).length
      ? `<ul class="warn-list">${trip.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>`
      : '<p class="hint">没有额外提示。</p>'}
    <div class="sec-title">变更历史（v${trip.version}）</div>
    ${(trip.history || []).length
      ? `<ul class="evidence-list">${trip.history.map((h) => `<li>v${h.version} ${esc(h.at)}　${esc(h.reason)}${h.changes.length ? '：' + esc(h.changes.join('；')) : ''}</li>`).join('')}</ul>`
      : '<p class="hint">尚无修改记录。</p>'}`;
}

function viewTrace() {
  if (!state.trace || !state.trace.length) return '<div class="empty"><p>还没有工具调用记录。</p></div>';
  const rows = state.trace.map((t) => `<tr>
      <td>${t.step}</td>
      <td><code>${esc(t.tool)}</code></td>
      <td>${t.ok ? '<span class="pill">成功</span>' : '<span class="pill bad">失败</span>'}
        ${t.degraded && t.ok ? '<span class="pill bad">降级</span>' : ''}</td>
      <td>${esc(t.source || '-')}</td>
      <td>${t.confidence}</td>
      <td>${t.latency_ms} ms</td>
      <td>${esc(t.summary || '')}</td>
    </tr>`).join('');
  return `<table class="trace">
      <thead><tr><th>#</th><th>工具</th><th>状态</th><th>来源</th><th>置信度</th><th>耗时</th><th>摘要</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

/* ---------------- 工具清单 ---------------- */
async function showTools() {
  try {
    const data = await api('/api/tools');
    $('drawerTitle').textContent = `工具清单（${data.count} 个）`;
    $('drawerBody').innerHTML = data.tools.map((t) => `
      <div class="tool-card">
        <h4><code>${esc(t.name)}</code></h4>
        <p>${esc(t.description)}</p>
        <pre>${esc(JSON.stringify(t.parameters, null, 1))}</pre>
      </div>`).join('');
    openDrawer();
  } catch (err) {
    alert(`读取工具清单失败：${err.message}`);
  }
}

function openDrawer() { $('drawer').classList.remove('hidden'); }
function closeDrawer() { $('drawer').classList.add('hidden'); }

/* ---------------- 导出 ---------------- */
async function exportMarkdown() {
  if (!state.tripId) return;
  try {
    const data = await api(`/api/trips/${state.tripId}/export`);
    const blob = new Blob([data.markdown], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `travel-plan-${state.tripId}.md`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert(`导出失败：${err.message}`);
  }
}

/* ---------------- 示例 ---------------- */
function renderExamples(list) {
  $('examples').innerHTML = (list || []).slice(0, 6)
    .map((e) => `<span class="example">${esc(e)}</span>`).join('');
  $('examples').querySelectorAll('.example').forEach((el) => {
    el.addEventListener('click', () => {
      $('chatInput').value = el.textContent;
      $('chatInput').focus();
    });
  });
}

const FALLBACK_EXAMPLES = [
  '从北京出发，下周五去成都玩4天，预算6000，喜欢美食和自然风光',
  '去西安玩3天，带父母，节奏轻松一点',
  '我想去大理和丽江7天，喜欢摄影和小众的地方',
  '帮我规划一趟青海湖4日自驾，预算8000',
  '去东京玩5天，喜欢美食和购物，预算12000',
];

/* ---------------- 工具函数 ---------------- */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function fmt(n) { return (n == null ? 0 : Number(n)).toLocaleString('zh-CN', { maximumFractionDigits: 0 }); }
function paceLabel(p) { return { relaxed: '轻松', normal: '适中', intensive: '紧凑' }[p] || p; }

/* ---------------- 初始化 ---------------- */
async function init() {
  try {
    renderStatus(await api('/api/status'));
  } catch (err) {
    $('statusChips').innerHTML = chip(`无法连接后端：${err.message}`, 'warn');
  }
  try {
    const ex = await api('/api/examples');
    renderExamples(ex.examples);
  } catch (_) {
    renderExamples(FALLBACK_EXAMPLES);
  }

  $('chatForm').addEventListener('submit', (e) => { e.preventDefault(); sendMessage(); });
  $('chatInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendMessage(); }
  });
  $('btnPlan').addEventListener('click', planDirect);
  $('btnExport').addEventListener('click', exportMarkdown);
  $('btnTools').addEventListener('click', showTools);
  $('btnExample').addEventListener('click', () => {
    $('chatInput').value = FALLBACK_EXAMPLES[0];
    $('chatInput').focus();
  });
  $('btnReload').addEventListener('click', async () => {
    try {
      const data = await api('/api/reload', { method: 'POST' });
      renderStatus(data.status);
      pushMsg('note', '配置已重载。', 'note');
    } catch (err) {
      pushMsg('warn', `重载失败：${err.message}`, 'note');
    }
  });

  document.querySelectorAll('#tabs .tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('#tabs .tab').forEach((t) => t.classList.remove('active'));
      tab.classList.add('active');
      state.tab = tab.dataset.tab;
      renderTab();
    });
  });

  document.querySelectorAll('[data-close]').forEach((el) => el.addEventListener('click', closeDrawer));

  // 日期默认值
  const today = new Date();
  const start = new Date(today.getTime() + 7 * 86400000);
  const end = new Date(today.getTime() + 10 * 86400000);
  $('fStart').value = iso(start);
  $('fEnd').value = iso(end);
}

function iso(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

document.addEventListener('DOMContentLoaded', init);
