/**
 * WordQuest 自检脚本
 * 用法： node tools/check-wordgame.js
 *
 * 覆盖：
 *   1. 内联脚本能在最小 DOM 桩里真实执行（语法 + 运行时错误）
 *   2. data/*.js 词库按需加载链路可用，词数与 data/meta.js 一致
 *   3. 词条合法性（单词形态、释义非空且不过长、同难度内单词唯一）
 *   4. 出题不变量（选项 4 个、含正确答案、来自 4 个不同单词）
 *   5. 拼写池可解性与体积上限
 *   6. 释义重复统计（信息性 —— 真实考纲词库必然有近义词）
 *   7. 三种玩法的端到端冒烟
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const GAME = path.join(__dirname, "..");        // wordgame/
const HTML = path.join(GAME, "index.html");

let pass = 0, fail = 0;
function ok(msg)  { pass++; console.log("  \u2713 " + msg); }
function bad(msg) { fail++; console.log("  \u2717 " + msg); }
function check(cond, msg) { cond ? ok(msg) : bad(msg); }

/* ---------- 1. 提取并执行内联脚本 ---------- */
const html = fs.readFileSync(HTML, "utf8");
const m = html.match(/<script>([\s\S]*?)<\/script>/);
if (!m) {
  console.log("  \u2717 未在 index.html 中找到 <script> 块");
  process.exit(1);
}
const code = m[1];

function makeEl() {
  return new Proxy({}, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === "classList") return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
      if (k === "dataset") return {};
      if (k === "style") return {};
      if (k === "querySelectorAll" || k === "querySelector") return () => [];
      if (k === "addEventListener") return () => {};
      return () => {};
    },
    set(t, k, v) { t[k] = v; return true; }
  });
}

const storeStub = (() => {
  const mem = {};
  return {
    getItem: (k) => (k in mem ? mem[k] : null),
    setItem: (k, v) => { mem[k] = String(v); },
    removeItem: (k) => { delete mem[k]; }
  };
})();

const sandbox = {
  console,
  // setTimeout 同步执行：让「加载 → 答题 → 下一题」的异步链条在测试里即时走完
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  Math, JSON, Date, Object, Array, String, Number, Boolean, RegExp, Set, Map, Error, Promise,
  localStorage: storeStub,
  scrollTo() {}
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

// document 桩：getElementById 返回 noop；createElement + head.appendChild 用于真实加载 data/*.js
sandbox.document = {
  getElementById: makeEl,
  addEventListener() {},
  querySelectorAll: () => [],
  createElement: () => ({}),
  head: {
    appendChild(el) {
      const src = String(el.src || "");
      const file = path.join(GAME, src);
      let loaded = false;
      try {
        if (src && fs.existsSync(file)) {
          new vm.Script(fs.readFileSync(file, "utf8"), { filename: src }).runInContext(sandbox);
          loaded = true;
        }
      } catch (e) { loaded = false; }
      if (loaded) { if (el.onload) el.onload(); }
      else if (el.onerror) el.onerror(new Error("无法加载 " + src));
    }
  }
};

vm.createContext(sandbox);

try {
  new vm.Script(code, { filename: "index.html<script>" }).runInContext(sandbox, { timeout: 8000 });
  ok("内联脚本执行无语法/运行时错误");
} catch (err) {
  bad("脚本执行失败：" + err.message);
  console.log("\n\u2717 自检中止");
  process.exit(1);
}

const WQ = sandbox.window.__WQ__;
if (!WQ) { bad("未导出 window.__WQ__，无法继续校验"); process.exit(1); }

/* ---------- 主流程（需 await 词库加载） ---------- */
(async () => {
  const {
    DIFFS, MODES, PER_ROUND, QUEUE_SIZE, MATCH_PAIRS,
    loadWords, loadMeta, pickDistractors, _test
  } = WQ;

  /* ---------- 2. 词库加载与元信息 ---------- */
  console.log("\n[词库]");
  check(DIFFS.length === 3, "难度共 3 级");
  check(MODES.length === 3, "玩法共 3 种（quiz / spell / match）");

  const meta = await loadMeta();
  check(!!meta, "data/meta.js 可加载");
  DIFFS.forEach(d => check(typeof meta[d.id] === "number" && meta[d.id] > 0,
    d.id + " 元信息词量 = " + (meta[d.id] || 0).toLocaleString("en-US")));

  const sumMeta = DIFFS.reduce((a, d) => a + (meta[d.id] || 0), 0);
  check(sumMeta === meta.total, "三级词量之和 = meta.total（" + sumMeta + "）");

  const pools = {};
  let loadErr = null;
  for (const d of DIFFS) {
    try { pools[d.id] = await loadWords(d.id); } catch (e) { loadErr = e; break; }
  }
  check(!loadErr, "三个 data/*.js 均可加载并解析" + (loadErr ? "：" + loadErr.message : ""));

  DIFFS.forEach(d => {
    const n = (pools[d.id] || []).length;
    check(n === meta[d.id], d.id + " 实际词数 " + n + " 与 meta 一致");
  });
  console.log("  · 词库总量 " + sumMeta.toLocaleString("en-US") + " 词（源表行数 " +
    DIFFS.map(d => meta.src[d.id]).join(" / ") + "，去重后见上）");

  /* ---------- 3. 词条合法性 ---------- */
  console.log("\n[词条合法性]");
  let badWord = 0, badCn = 0, tooLong = 0, dupInLevel = 0, longest = "", longestLen = 0;
  DIFFS.forEach(d => {
    const seen = new Set();
    (pools[d.id] || []).forEach(x => {
      if (!/^[a-z]{2,20}$/.test(x.w)) badWord++;
      if (!x.cn || !x.cn.trim()) badCn++;
      if (x.cn.length > 26) tooLong++;
      if (seen.has(x.w)) dupInLevel++;
      seen.add(x.w);
      if (x.w.length > longestLen) { longestLen = x.w.length; longest = x.w; }
    });
  });
  check(badWord === 0, "单词均为 2-20 位纯小写字母（异常 " + badWord + "）");
  check(badCn === 0, "释义均非空（异常 " + badCn + "）");
  check(tooLong === 0, "释义长度均 ≤ 26 字符（超长 " + tooLong + "）");
  check(dupInLevel === 0, "同难度内单词唯一（重复 " + dupInLevel + "）");
  console.log("  · 最长单词 " + longest + "（" + longestLen + " 字母）");

  /* ---------- 4. 出题不变量 ---------- */
  console.log("\n[出题逻辑]");
  let optErr = 0, lackAns = 0, sameWord = 0, relaxed = 0, trials = 0;
  DIFFS.forEach(d => {
    const pool = pools[d.id] || [];
    if (!pool.length) return;
    for (let i = 0; i < 300; i++) {
      const ans = pool[(Math.random() * pool.length) | 0];
      const dis = pickDistractors(pool, ans, 3);
      trials++;
      if (dis.length !== 3) optErr++;
      const opts = [ans.cn, ...dis.map(x => x.cn)];
      if (opts.length !== 4) optErr++;
      if (opts.indexOf(ans.cn) === -1) lackAns++;
      if (new Set([ans.w, ...dis.map(x => x.w)]).size !== 4) sameWord++;
      if (new Set(opts).size !== opts.length) relaxed++;
    }
  });
  check(optErr === 0, trials + " 次抽样：干扰项均补足 3 个（异常 " + optErr + "）");
  check(lackAns === 0, "正确答案始终在选项内（缺失 " + lackAns + "）");
  check(sameWord === 0, "4 个选项来自 4 个不同单词（异常 " + sameWord + "）");
  const relaxedRate = trials ? (relaxed / trials) * 100 : 0;
  console.log("  · " + relaxed + " 次（" + relaxedRate.toFixed(1) + "%）因释义撞车放宽为「单词不同即可」");
  check(relaxedRate < 5, "释义撞车导致降级的比例 < 5%（实际 " + relaxedRate.toFixed(1) + "%）");

  /* ---------- 5. 拼写池 ---------- */
  console.log("\n[拼写池]");
  let unreachable = 0, oversized = 0, checked = 0, poolMax = 0;
  DIFFS.forEach(d => {
    (pools[d.id] || []).forEach(x => {
      checked++;
      const letters = x.w.split("");
      const need = letters.length >= 10 ? 2 : Math.max(3, Math.min(6, Math.ceil(letters.length * 0.5)));
      if (26 - new Set(letters).size < need) unreachable++;
      const size = letters.length + need;
      if (size > poolMax) poolMax = size;
      if (size > 22) oversized++;
    });
  });
  check(unreachable === 0, checked.toLocaleString("en-US") + " 词均可生成足够干扰字母（异常 " + unreachable + "）");
  check(oversized === 0, "字母池最大 " + poolMax + " 个按钮，未超 22 个上限（超限 " + oversized + "）");

  /* ---------- 6. 释义重复（信息性，不计入通过/失败） ---------- */
  console.log("\n[释义重复 · 仅供参考]");
  DIFFS.forEach(d => {
    const list = pools[d.id] || [];
    const byCn = new Map();
    list.forEach(x => {
      if (!byCn.has(x.cn)) byCn.set(x.cn, []);
      byCn.get(x.cn).push(x.w);
    });
    const groups = [...byCn.entries()].filter(([, v]) => v.length > 1)
      .sort((a, b) => b[1].length - a[1].length);
    const covered = groups.reduce((a, [, v]) => a + v.length, 0);
    console.log("  · " + d.id + "：" + byCn.size.toLocaleString("en-US") + " 种释义 / " +
      list.length.toLocaleString("en-US") + " 词，" + groups.length + " 组重复，涉及 " +
      covered + " 词（" + ((covered / list.length) * 100).toFixed(1) + "%）");
    groups.slice(0, 2).forEach(([cn, ws]) =>
      console.log("      「" + cn + "」×" + ws.length + " → " + ws.slice(0, 6).join(", ")));
  });

  /* ---------- 7. 三种玩法端到端冒烟 ---------- */
  console.log("\n[玩法冒烟]");
  const { G, startGame, answerQuiz, clickPool, clickCard } = _test;
  const fakeBtn = (w) => ({ dataset: { w }, disabled: false, classList: { add() {}, remove() {} } });

  // --- 四选一 ---
  try {
    G.mode = "quiz"; G.diff = "cet4";
    await startGame();
    check(G.pool.length > 1000 && !!G.cur, "quiz 开局：词池 " + G.pool.length + " 词 + 首题就绪");
    check(G.queue.length === Math.min(QUEUE_SIZE, G.pool.length),
      "题序按 QUEUE_SIZE 批量抽取（" + G.queue.length + " 题）");
    check(new Set(G.queue.map(x => x.w)).size === G.queue.length, "同批题序内单词不重复");
    let n = 0;
    while (n < PER_ROUND && G.cur) { answerQuiz(fakeBtn(G.cur.w)); n++; }
    check(G.idx >= PER_ROUND, "quiz 答满 " + PER_ROUND + " 题（idx=" + G.idx + "）");
    check(G.right === PER_ROUND, "quiz 全对计数 = " + G.right);
    check(G.score >= PER_ROUND * 10, "quiz 得分累计 = " + G.score + "（含连击加成）");
  } catch (e) { bad("quiz 冒烟异常：" + e.message); }

  // --- 拼写 ---
  try {
    G.mode = "spell"; G.diff = "kaoyan";
    await startGame();
    const sp = G.spell;
    check(!!sp && sp.slots.length === G.cur.w.length,
      "spell 开局：槽位数 = " + G.cur.w.length + "（单词 " + G.cur.w + "）");
    check(sp.slots[0] === -1, "spell 首字母为提示位（不可点击）");
    let miss = null;
    for (let i = 1; i < sp.answer.length; i++) {
      const item = sp.pool.find(p => !p.used && p.ch === sp.answer[i]);
      if (!item) { miss = sp.answer[i]; break; }
      clickPool(item.id);
    }
    check(!miss, "spell 字母池可完整拼出答案" + (miss ? "（缺字母 " + miss + "）" : ""));
    check(G.right === 1, "spell 拼对即判定通过，正确计数 = " + G.right);
    check(G.idx === 2, "spell 判定后自动进入下一题（idx=" + G.idx + "）");
  } catch (e) { bad("spell 冒烟异常：" + e.message); }

  // --- 翻牌 ---
  try {
    G.mode = "match"; G.diff = "cet6";
    await startGame();
    const mt = G.match;
    check(!!mt && mt.cards.length === MATCH_PAIRS * 2,
      "match 开局：" + mt.cards.length + " 张卡 = " + MATCH_PAIRS + " 对");
    const pids = Array.from(new Set(mt.cards.map(c => c.pid)));
    check(pids.length === MATCH_PAIRS, "match 配对 id 唯一且完整");
    pids.forEach(pid => {
      const pair = mt.cards.filter(c => c.pid === pid);
      clickCard(pair[0].idx);
      clickCard(pair[1].idx);
    });
    check(mt.remain === 0, "match 全部配对完成（剩余 " + mt.remain + " 对）");
    check(G.right === MATCH_PAIRS, "match 配成计数 = " + G.right);
  } catch (e) { bad("match 冒烟异常：" + e.message); }

  /* ---------- 汇总 ---------- */
  console.log("\n" + "-".repeat(46));
  console.log("通过 " + pass + " 项 / 失败 " + fail + " 项");
  process.exit(fail === 0 ? 0 : 1);
})().catch(err => {
  console.log("\n\u2717 自检脚本异常中止：" + (err && err.stack || err));
  process.exit(1);
});
