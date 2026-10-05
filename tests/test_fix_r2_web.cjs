// 2026-10-05 深度 review 第二轮(fix1005-r2)前端修复的回归测试。
// 执行 web/modal_bridge.js 的真实函数:网络 / DOM / 时钟全部桩掉(虚拟时钟,不真等);
// localStorage 用一个可在多个「标签页」沙箱之间共享的内存实现,loadLS / saveLS / clearLS 用源码里的真实实现。
// 运行:node --test tests/test_fix_r2_web.cjs
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../web/modal_bridge.js"), "utf8");
const NOT_FOUND_STREAK = Number(/const NOT_FOUND_STREAK = (\d+);/.exec(source)[1]);
const HB = "modal_bridge.tab_hb.";
const JOBS = "modal_bridge.active_job";
const H = 3600 * 1000;
// 排队阶段兜底线 = 6 小时 + 10 分钟余量。故意写死:6 小时是和云端 _QUEUE_STALE_S 的契约。
const QUEUE_DEADLINE_MS = 6 * H + 10 * 60 * 1000;

function chunk(startMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf("\n// =====================================================================", start);
  assert(start >= 0 && end > start, "marker not found: " + startMarker);
  return source.slice(start, end);
}
function between(a, b) {
  const i = source.indexOf(a);
  const j = source.indexOf(b, i);
  assert(i >= 0 && j > i, `slice not found: ${a} .. ${b}`);
  return source.slice(i, j);
}

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const tick = () => new Promise((r) => setImmediate(r));
const ticks = async (n = 10) => { for (let i = 0; i < n; i++) await tick(); };
function deferred() { let resolve; const p = new Promise((r) => { resolve = r; }); return { p, resolve }; }

function domEl(tag = "div") {
  return {
    tag, style: {}, children: [], textContent: "", title: "", value: "", placeholder: "",
    disabled: false, dataset: {}, options: [], removed: false,
    _kids: {}, _all: {}, _html: "",
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener() {},
    querySelector(sel) { return (this._kids[sel] ||= domEl("q:" + sel)); },
    querySelectorAll(sel) { return this._all[sel] || []; },
    remove() { this.removed = true; },
    getBoundingClientRect() { return { top: 0, right: 0 }; },
  };
}

// 多个标签页共享的 localStorage(字符串存储,和浏览器一样)
function makeStore(init = {}) {
  const data = {};
  for (const [k, v] of Object.entries(init)) data[k] = JSON.stringify(v);
  const writes = [];
  const ls = {
    get length() { return Object.keys(data).length; },
    key(i) { return Object.keys(data)[i] ?? null; },
    getItem(k) { return k in data ? data[k] : null; },
    setItem(k, v) { writes.push(k); data[k] = String(v); },
    removeItem(k) { writes.push(k); delete data[k]; },
  };
  return { data, ls, writes, get: (k) => (k in data ? JSON.parse(data[k]) : null),
           set: (k, v) => { data[k] = JSON.stringify(v); } };
}

// 一个「标签页」:进度卡片 / 持久化 / 轮询 / 取消 / 取回 / 恢复全部是真实源码
function makeTab({ store = makeStore(), clock = { t: 1_000_000_000 }, fetch, settings = {}, realI18n = false } = {}) {
  const observed = { polls: 0, cancels: 0, fetches: 0, submits: 0, alerts: [], notifies: [], events: [] };
  const sandbox = {
    Date: { now: () => clock.t }, Headers, JSON, Math, String, Number, Object, Array, Promise, Error, Set, Map,
    parseInt, encodeURIComponent, setTimeout: () => 0, setInterval: () => 1, clearInterval: () => {},
    localStorage: store.ls,
    getSetting: (k, d) => (k in settings ? settings[k] : d),
    getVramTier: () => "80g",
    sleep: async (ms) => { clock.t += ms; },
    log: () => {}, err: () => {},
    notify: (m, sev, life) => observed.notifies.push({ m, sev, life }),
    alert: (m) => observed.alerts.push(m),
    confirm: () => true,
    reportJobEvent: (id, ev) => observed.events.push(ev),
    t: (key, vars) => (vars ? key + " " + JSON.stringify(vars) : key),
    fmtRate: String, fmtDur: String,
    MODEL3D_EXT_RE: /\.glb$/i,
    displayInGraph: () => 1, activeWorkflowKey: () => null, storePendingResult: () => {},
    document: { createElement: domEl, body: domEl("body"), addEventListener() {} },
    window: { innerWidth: 1000 },
    bridgeFetch: async (url, o) => fetch(url, o, observed, clock),
  };
  vm.createContext(sandbox);
  if (realI18n) {
    sandbox.app = { ui: { settings: { getSettingValue: () => "zh" } } };
    sandbox.navigator = { language: "zh" };
    vm.runInContext(between("function _locale()", "const sleep ="), sandbox);
  }
  vm.runInContext(between("const LS_KEYS =", "// ComfyUI Settings 读取"), sandbox);   // 真实 loadLS / saveLS / clearLS
  vm.runInContext(chunk("const NOT_FOUND_STREAK ="), sandbox);
  vm.runInContext(chunk("function addActiveJob("), sandbox);
  vm.runInContext(chunk("async function recoverPendingJob("), sandbox);
  return { sb: sandbox, observed, clock, store, tabId: vm.runInContext("TAB_ID", sandbox),
           jobs: () => store.get(JOBS) || [] };
}

const done = (extra = {}) => ({ status: "completed", images: [{ filename: "a.png", data_base64: "AA==", node_id: "9" }], ...extra });
const submitOk = (id = "job-1") => json(200, { ok: true, job_id: id, gpu: "H100", worker_timeout_sec: 1200 });
const fetchOk = () => json(200, { ok: true, outputs: [{ filename: "a.png", subfolder: "modal_results" }] });

// =============================================================================
// #1 [P2] 批量里点取消、取消没确认:当前单跟踪到底,后面的不再提交
// =============================================================================
function batchTab({ batchCount = 3, cancelResp }) {
  let cancelAt = null, jobN = 0;
  const tab = makeTab({
    settings: { "ModalBridge.batchCount": batchCount, "ModalBridge.autoCheckNodes": false, "ModalBridge.autoSyncModels": false },
    fetch: (url, o, obs, clock) => {
      if (url.endsWith("/submit")) { obs.submits++; jobN++; return submitOk("job-" + jobN); }
      if (url.includes("/poll?")) {
        obs.polls++;
        if (cancelAt == null || clock.t < cancelAt + 5000) return json(200, { status: "running" });
        return json(200, done());
      }
      if (url.endsWith("/cancel")) { obs.cancels++; cancelAt = clock.t; return cancelResp(); }
      if (url.endsWith("/fetch_result")) { obs.fetches++; return fetchOk(); }
      return json(200, { ok: true });
    },
  });
  const sb = tab.sb;
  Object.assign(sb, {
    activeWorkflowName: () => "wf", workflowHasApiNodes: () => false,
    app: { graphToPrompt: async () => ({ output: { "9": { class_type: "SaveImage", inputs: {} } } }) },
    findOutputNodes: () => ["9"], fetchConfig: async () => ({ has_comfy_api_key: true }), isConfigured: () => true,
    checkVersionOrBlock: async () => true, requiredInputsPreflight: async () => true,
    vramPreflightOrConfirm: async () => true, reseedPrompt: (p) => p, openDeployDialog: () => {},
  });
  vm.runInContext(between("async function queueOnModal(", "\n// ====="), sb);
  let card = null;
  const np = sb.newProgress;
  sb.newProgress = (...a) => (card = np(...a));
  return { tab, card: () => card };
}

test("#1 批量 3 单:第 1 单点取消、云端回 still_billing(没确认)→ 第 1 单照常取回,第 2、3 单不再提交", async () => {
  const { tab, card } = batchTab({ cancelResp: () => json(200, { ok: false, still_billing: true, id: "job-1",
    status: "queued", error: "任务正在提交中,还拿不到句柄 —— 稍等一两秒再点取消" }) });
  let k = 0;
  tab.sb.sleep = async (ms) => { tab.clock.t += ms; if (++k === 2) await card().onCancel(); };
  await tab.sb.queueOnModal();
  assert.equal(tab.observed.cancels, 1);
  assert.equal(tab.observed.submits, 1, "点过取消后批量还在提交后面的单");
  assert.equal(tab.observed.fetches, 1, "当前这一单要跟踪到底、照常取回");
  assert.match(card().els.label.textContent, /1\/3 done · batch stopped/);
  const stop = tab.observed.notifies.find((n) => n.m.startsWith("run.batch_stopped"));
  assert(stop && stop.sev === "warn", JSON.stringify(tab.observed.notifies));
  assert(stop.m.includes('"left":2'), stop.m);
  assert(!tab.observed.notifies.some((n) => n.m.startsWith("toast.done")), "不能再报「3 张完成」");
});

test("#1 对照:没点取消的批量照常提交全部 3 单;取消确认成功则停在第 1 单(行为不变)", async () => {
  const plain = batchTab({ cancelResp: () => json(200, {}) });
  plain.tab.sb.sleep = async (ms) => { plain.tab.clock.t += ms; };
  // 没人点取消:poll 第一拍就完成
  plain.tab.sb.bridgeFetch = async (url, o) => {
    if (url.endsWith("/submit")) { plain.tab.observed.submits++; return submitOk("j" + plain.tab.observed.submits); }
    if (url.includes("/poll?")) return json(200, done());
    if (url.endsWith("/fetch_result")) return fetchOk();
    return json(200, { ok: true });
  };
  await plain.tab.sb.queueOnModal();
  assert.equal(plain.tab.observed.submits, 3);
  assert.match(plain.card().els.label.textContent, /3 done/);

  const ok = batchTab({ cancelResp: () => json(200, { ok: true, still_billing: false, id: "job-1",
    status: "cancelled", was_running: true }) });
  let k = 0;
  ok.tab.sb.sleep = async (ms) => { ok.tab.clock.t += ms; if (++k === 2) await ok.card().onCancel(); };
  await ok.tab.sb.queueOnModal();
  assert.equal(ok.tab.observed.submits, 1);
  assert.match(ok.card().els.label.textContent, /✕ Cancelled/);
});

// =============================================================================
// #2 [P3] 排队阶段兜底截止、连续瞬态提示、恢复记录过期
// =============================================================================
test("#2 提交后 /poll 一直回 unknown(云端 /status 404):主轮询在提交 + 6h10m 结束并请求取消,不再无限跑", async () => {
  const tab = makeTab({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return submitOk();
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "unknown", http_status: 404, error: "app not found" }); }
    if (url.endsWith("/cancel")) { obs.cancels++; return json(200, { ok: true, still_billing: false, status: "cancelled" }); }
    return json(200, { ok: true });
  } });
  const t0 = tab.clock.t;
  const ctx = tab.sb.newProgress("submitting", "wf");
  await assert.rejects(tab.sb.runOnceOnModal({}, ["9"], ctx, null),
    (e) => /Polling timed out/.test(e.message) && e.message.includes("run.queue_timeout"));
  const el = tab.clock.t - t0;
  assert(el >= QUEUE_DEADLINE_MS && el <= QUEUE_DEADLINE_MS + 5000, `结束于 ${(el / H).toFixed(3)}h`);
  assert.equal(tab.observed.cancels, 1, "兜底截止时要请求取消止损");
  const warns = tab.observed.notifies.filter((n) => n.m.startsWith("run.poll_unreachable"));
  assert.equal(warns.length, 1, "连续瞬态只提示一次");
  assert(warns[0].m.includes("app not found") && warns[0].sev === "warn", warns[0].m);
  assert.equal(tab.observed.events.filter((e) => e === "poll_unreachable").length, 1);
});

test("#2 正常情况:云端在排队 6 小时整判 failed,前端先看到它,不走兜底取消", async () => {
  const tab = makeTab({ fetch: (url, o, obs, clock) => {
    if (url.endsWith("/submit")) { obs.t0 = clock.t; return submitOk(); }
    if (url.includes("/poll?")) {
      return clock.t - obs.t0 < 6 * H ? json(200, { status: "queued" })
        : json(200, { status: "failed", error: "排队超过 6 小时仍未开始执行" });
    }
    if (url.endsWith("/cancel")) { obs.cancels++; return json(200, { ok: true, still_billing: false, status: "cancelled" }); }
    return json(200, { ok: true });
  } });
  await assert.rejects(tab.sb.runOnceOnModal({}, ["9"], tab.sb.newProgress("submitting", "wf"), null), /排队超过 6 小时/);
  assert.equal(tab.observed.cancels, 0);
  assert.equal(tab.jobs().length, 0);
});

test("#2 恢复列表:没见过 running 的记录按提交 + 6h10m 过期;之内的照常接手", async () => {
  const now = 1_000_000_000;
  const store = makeStore({ [JOBS]: [
    { jobId: "ancient", startedAt: now - 30 * 24 * H, workerTimeoutSec: 1200 },   // 30 天前提交、从没见过 running
    { jobId: "edge", startedAt: now - QUEUE_DEADLINE_MS - 1 },
    { jobId: "queued5h", startedAt: now - 5 * H },
  ] });
  const tab = makeTab({ store, clock: { t: now }, fetch: () => json(200, {}) });
  const adopted = [];
  tab.sb.recoverOne = (j) => adopted.push(j.jobId);
  await tab.sb.recoverPendingJob();
  assert.deepEqual(adopted, ["queued5h"]);
  assert.deepEqual(tab.jobs().map((j) => j.jobId), ["queued5h"], "过期的记录要丢掉");
});

test("#2 恢复轮询:没见过 running、/poll 一直 unknown → 到兜底线结束并请求取消", async () => {
  const now = 1_000_000_000;
  const job = { jobId: "job-1", wfName: "wf", startedAt: now - 6 * H };
  const tab = makeTab({ store: makeStore({ [JOBS]: [job] }), clock: { t: now }, fetch: (url, o, obs) => {
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "unknown", http_status: 404 }); }
    if (url.endsWith("/cancel")) { obs.cancels++; return json(200, { ok: true, still_billing: false, status: "cancelled" }); }
    return json(200, { ok: true });
  } });
  await tab.sb.recoverOne({ ...job }, 1200);
  assert(tab.clock.t >= job.startedAt + QUEUE_DEADLINE_MS && tab.clock.t <= job.startedAt + QUEUE_DEADLINE_MS + 5000);
  assert.equal(tab.observed.cancels, 1);
  assert(tab.observed.notifies.some((n) => n.m.startsWith("run.poll_unreachable")), "恢复路径也要提示连续查不到");
});

test("#2 连续瞬态 5 分钟才提示;看清一次就撤掉卡片上那条,不误撤取消失败的警告", async () => {
  const tab = makeTab({ fetch: () => json(200, {}) });
  const ctx = tab.sb.newProgress("queued", "wf");
  const w = tab.sb.transientWatch("job-1", ctx);
  const p502 = { transient: true, data: { error: "upstream timeout" } };
  w.transient(p502);
  tab.clock.t += 4 * 60 * 1000;
  w.transient(p502);
  assert.equal(tab.observed.notifies.length, 0, "不到 5 分钟不提示");
  tab.clock.t += 61 * 1000;
  w.transient(p502);
  w.transient(p502);
  assert.equal(tab.observed.notifies.length, 1);
  assert(ctx.els.warn.textContent.startsWith("run.poll_unreachable"));
  assert.equal(ctx.els.warn.style.display, "block");
  w.seen();
  assert.equal(ctx.els.warn.style.display, "none", "看清了要撤掉");
  // 取消失败的警告不能被「状态恢复」撤掉
  ctx.setWarn("cancel.failed_retry", "cancel");
  const w2 = tab.sb.transientWatch("job-1", ctx);
  w2.transient(p502); tab.clock.t += 6 * 60 * 1000; w2.transient(p502);
  ctx.setWarn("cancel.failed_retry", "cancel");   // 取消在这期间又失败了一次
  w2.seen();
  assert.equal(ctx.els.warn.textContent, "cancel.failed_retry");
  // 计时以「连续」为准:中间看清一次就重新计
  const w3 = tab.sb.transientWatch("job-1", null);
  const before = tab.observed.notifies.length;
  w3.transient(p502); tab.clock.t += 4 * 60 * 1000; w3.seen();
  w3.transient(p502); tab.clock.t += 4 * 60 * 1000; w3.transient(p502);
  assert.equal(tab.observed.notifies.length, before);
});

// =============================================================================
// #3 [P3] 多标签页心跳归属
// =============================================================================
test("#3a A 取回失败、主循环结束、记录保留:A 不再给它续约,B 标签页启动时接手", async () => {
  const store = makeStore();
  const clock = { t: 1_000_000_000 };
  const A = makeTab({ store, clock, fetch: (url) => {
    if (url.endsWith("/submit")) return submitOk();
    if (url.includes("/poll?")) return json(200, done());
    if (url.endsWith("/fetch_result")) return json(502, { error: "write result failed: disk full" });
    return json(200, { ok: true });
  } });
  await assert.rejects(A.sb.runOnceOnModal({}, ["9"], A.sb.newProgress("submitting", "wf"), null), /disk full/);
  assert.equal(A.jobs().length, 1, "记录保留");
  assert.equal(A.jobs()[0].tabId, A.tabId);
  assert.equal(store.get(HB + A.tabId), null, "主循环结束后本页不再跟踪,心跳键里不该再有它");
  // A 继续开着,定时器照跑:不跟踪任何 job 时不写心跳
  for (let i = 0; i < 12; i++) { clock.t += 5000; if (vm.runInContext("_trackedJobs.size", A.sb)) A.sb.writeTabHeartbeat(); }
  assert.equal(store.get(HB + A.tabId), null);
  const B = makeTab({ store, clock, fetch: () => json(200, {}) });
  const adopted = [];
  B.sb.recoverOne = (j) => adopted.push(j.jobId);
  await B.sb.recoverPendingJob();
  assert.deepEqual(adopted, ["job-1"], "A 已不再跟踪,B 必须能接手");
  assert.equal(B.jobs()[0].tabId, B.tabId);
});

test("#3a A 同时还在跟踪另一单:心跳键还在,但只护住它列出的那单,结束了的那条照样能被接手", async () => {
  const store = makeStore();
  const clock = { t: 1_000_000_000 };
  const A = makeTab({ store, clock, fetch: (url) => {
    if (url.endsWith("/submit")) return submitOk("job-1");
    if (url.includes("/poll?")) return json(200, done());
    if (url.endsWith("/fetch_result")) return json(502, { error: "disk full" });
    return json(200, { ok: true });
  } });
  // job-2 在 A 里还在跑(比如另一个工作流的卡片)
  A.sb.addActiveJob({ jobId: "job-2", startedAt: clock.t });
  A.sb.trackJob("job-2");
  await assert.rejects(A.sb.runOnceOnModal({}, ["9"], A.sb.newProgress("submitting", "wf"), null), /disk full/);
  assert.deepEqual(store.get(HB + A.tabId).jobs, ["job-2"]);
  const B = makeTab({ store, clock, fetch: () => json(200, {}) });
  const adopted = [];
  B.sb.recoverOne = (j) => adopted.push(j.jobId);
  await B.sb.recoverPendingJob();
  assert.deepEqual(adopted, ["job-1"], "心跳键在 ≠ 每条记录都有人跟");
});

test("#3a A 正在轮询时:心跳键列着这个 job,B 不接手;A 结束后撤销", async () => {
  const store = makeStore();
  const clock = { t: 1_000_000_000 };
  const gate = deferred();
  let polled = false;
  const A = makeTab({ store, clock, fetch: (url) => {
    if (url.endsWith("/submit")) return submitOk();
    if (url.includes("/poll?")) { polled = true; return gate.p.then(() => json(200, done())); }
    if (url.endsWith("/fetch_result")) return fetchOk();
    return json(200, { ok: true });
  } });
  const run = A.sb.runOnceOnModal({}, ["9"], A.sb.newProgress("submitting", "wf"), null);
  while (!polled) await tick();
  assert.deepEqual(store.get(HB + A.tabId).jobs, ["job-1"]);
  const B = makeTab({ store, clock, fetch: () => json(200, {}) });
  const adopted = [];
  B.sb.recoverOne = (j) => adopted.push(j.jobId);
  await B.sb.recoverPendingJob();
  assert.deepEqual(adopted, [], "A 正在跟踪,B 不能重复接手");
  assert.equal(B.jobs().length, 1, "也不能删掉 A 的记录");
  gate.resolve();
  await run;
  assert.equal(store.get(HB + A.tabId), null);
  assert.equal(A.jobs().length, 0);
});

test("#3a 恢复路径同理:恢复轮询期间登记跟踪,结束(auth_failed 保留记录)后撤销", async () => {
  const store = makeStore({ [JOBS]: [{ jobId: "job-1", startedAt: 1_000_000_000 }] });
  let seen = null;
  const tab = makeTab({ store, fetch: (url) => {
    if (url.includes("/poll?")) { seen = store.get(HB + tab.tabId); return json(200, { status: "auth_failed", error: "401" }); }
    return json(200, { ok: true });
  } });
  await tab.sb.recoverOne({ jobId: "job-1", startedAt: 1_000_000_000 }, 1200);
  assert.deepEqual(seen && seen.jobs, ["job-1"], "轮询期间心跳键里要有它");
  assert.equal(tab.jobs().length, 1, "auth_failed 保留记录");
  assert.equal(store.get(HB + tab.tabId), null, "恢复结束后撤销");
});

test("#3b 后台标签页的定时器每分钟才醒一次:60 秒没续约不算死;超过 120 秒才接手", async () => {
  const now = 1_000_000_000;
  const mk = (age) => makeStore({
    [JOBS]: [{ jobId: "job-1", startedAt: now, tabId: "hidden-tab" }],
    [HB + "hidden-tab"]: { t: now - age, jobs: ["job-1"] },
  });
  for (const [age, expect] of [[20_000, []], [65_000, []], [119_000, []], [121_000, ["job-1"]]]) {
    const tab = makeTab({ store: mk(age), clock: { t: now }, fetch: () => json(200, {}) });
    const adopted = [];
    tab.sb.recoverOne = (j) => adopted.push(j.jobId);
    await tab.sb.recoverPendingJob();
    assert.deepEqual(adopted, expect, `心跳 ${age / 1000}s 前`);
  }
});

test("#3c 心跳只写本页自己的键,不读改写共享的恢复记录数组", async () => {
  const store = makeStore();
  const clock = { t: 1_000_000_000 };
  const A = makeTab({ store, clock, fetch: () => json(200, {}) });
  A.sb.trackJob("job-a");
  // B 在 A 两次心跳之间加了一条记录
  store.set(JOBS, [{ jobId: "job-b", startedAt: clock.t, tabId: "tab-b" }]);
  const before = store.data[JOBS];
  store.writes.length = 0;
  for (let i = 0; i < 5; i++) { clock.t += 5000; A.sb.writeTabHeartbeat(); }
  assert.deepEqual([...new Set(store.writes)], [HB + A.tabId], "心跳碰了别的键:" + store.writes);
  assert.equal(store.data[JOBS], before, "共享数组被改写了");
  assert.deepEqual(store.get(HB + A.tabId), { t: clock.t, jobs: ["job-a"] });
});

test("#3 pagehide 释放:删掉本页心跳键,刷新后的页面立即接手;过期的心跳键启动时清理", async () => {
  const now = 1_000_000_000;
  const store = makeStore({
    [JOBS]: [{ jobId: "job-1", startedAt: now }],
    [HB + "crashed-tab"]: { t: now - 10 * 60_000, jobs: ["x"] },
    [HB + "live-tab"]: { t: now - 1000, jobs: ["y"] },
  });
  const A = makeTab({ store, clock: { t: now }, fetch: () => json(200, {}) });
  A.sb.trackJob("job-1");
  assert(store.get(HB + A.tabId));
  A.sb.releaseTabHeartbeat();
  assert.equal(store.get(HB + A.tabId), null);
  const B = makeTab({ store, clock: { t: now }, fetch: () => json(200, {}) });
  const adopted = [];
  B.sb.recoverOne = (j) => adopted.push(j.jobId);
  await B.sb.recoverPendingJob();
  assert.deepEqual(adopted, ["job-1"]);
  assert.equal(store.get(HB + "crashed-tab"), null, "过期心跳键要清掉");
  assert(store.get(HB + "live-tab"), "活着的标签页的心跳键不能删");
});

test("#3 setup 接线:定时器只在有跟踪中的 job 时写心跳;pagehide 同时释放心跳并 flush 防抖设置", () => {
  const setup = between("  async setup() {", "\n});");
  assert(/if \(_trackedJobs\.size\) writeTabHeartbeat\(\);/.test(setup), setup);
  assert(/adoptOrphanJobs\(\)/.test(setup) && /ACTIVE_JOB_HB_MS\)/.test(setup), "定时器里要定期接手无主记录(第三轮)");
  const ph = /addEventListener\("pagehide", \(\) => \{([\s\S]*?)\}\);/.exec(setup);
  assert(ph && ph[1].includes("releaseTabHeartbeat()") && ph[1].includes("flushTextSettings()"), ph && ph[1]);
  assert(!source.includes("heartbeatActiveJobs"), "旧的读改写心跳还在");
});

// =============================================================================
// #4 [P3] 防抖在页面卸载时 flush
// =============================================================================
function settingsSandbox() {
  const timers = new Map();
  let seq = 0;
  const posts = [];
  const sb = {
    JSON, String, Object, Promise, Error,
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    t: (k) => k, log: () => {}, err: () => {}, notify: () => {},
    httpError: async (r) => new Error("HTTP " + r.status),
    bridgeFetch: async (url, o) => { posts.push({ url, o, body: JSON.parse(o.body) }); return json(200, {}); },
  };
  vm.createContext(sb);
  vm.runInContext(chunk("let _snapReady = false;"), sb);
  vm.runInContext("_advReady = true;", sb);
  return { sb, timers, posts };
}

test("#4 输完 800ms 内关页面:pagehide 用 keepalive 立即发出最后的值,定时器清掉不重发", async () => {
  const { sb, timers, posts } = settingsSandbox();
  for (const v of ["h", "ht", "https://a.b"]) sb.syncAigcFieldToConfig("aigc_studio_base_url", v);
  assert.equal(timers.size, 1);
  sb.flushTextSettings();
  await ticks(3);
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { aigc_studio_base_url: "https://a.b" });
  assert.equal(posts[0].o.keepalive, true, "页面卸载后请求要能发完");
  assert.equal(posts[0].o.background, true, "卸载时不弹配对框");
  assert.equal(timers.size, 0, "定时器要清掉,否则(bfcache 回来时)会再写一次");
  sb.flushTextSettings();
  await ticks(3);
  assert.equal(posts.length, 1, "没有待写的值时 flush 不发请求");
  // 正常的防抖写入不带 keepalive
  sb.syncAigcFieldToConfig("aigc_studio_base_url", "https://c.d");
  [...timers.values()][0].fn();
  await ticks(3);
  assert.equal(posts.length, 2);
  assert.equal(posts[1].o.keepalive, undefined);
  assert.deepEqual(posts[1].body, { aigc_studio_base_url: "https://c.d" });
});

// =============================================================================
// #5 [P3] F-P1-2 变体:看到过活着、取消都回 not_found 之后,轮询的 not_found 只报矛盾不删记录
// =============================================================================
test("#5 running → 取消 not_found → 复核 alive → 重发 not_found → 复核瞬态 → 继续轮询 → not_found×5:记录保留,报矛盾", async () => {
  let phase = "run", cancels = 0;
  const tab = makeTab({ fetch: (url) => {
    if (url.endsWith("/submit")) return submitOk();
    if (url.includes("/poll?")) {
      if (phase === "run") return json(200, { status: "running" });
      if (phase === "c1") { phase = "wait2"; return json(200, { status: "running" }); }   // 第一轮复核:alive
      if (phase === "c2") { phase = "after"; return json(502, { error: "upstream timeout" }); }  // 第二轮复核:瞬态
      return json(200, { status: "not_found", error: "job not found" });
    }
    if (url.endsWith("/cancel")) {
      cancels++;
      phase = cancels === 1 ? "c1" : "c2";
      return json(200, { ok: true, still_billing: false, id: "job-1", status: "not_found", error: "job not found" });
    }
    return json(200, { ok: true });
  } });
  const ctx = tab.sb.newProgress("submitting", "wf");
  let k = 0;
  tab.sb.sleep = async (ms) => { tab.clock.t += ms; if (++k === 3) await ctx.onCancel(); };
  await assert.rejects(tab.sb.runOnceOnModal({}, ["9"], ctx, null),
    (e) => e.message.includes("run.gone_contradicted") && !e.message.includes("run.job_gone"));
  assert.equal(cancels, 2);
  assert.equal(tab.jobs().length, 1, "记录被轮询的 not_found 判死删掉了");
  assert.equal(tab.jobs()[0].cancelContradicted, "running", "矛盾要记在恢复记录上");
  assert.equal(tab.observed.alerts.length, 1);
  assert(tab.observed.alerts[0].includes("cancel.retry_hint"), "瞬态时仍继续跟踪");
  assert(!tab.observed.notifies.some((n) => n.m.startsWith("cancel.gone_msg")));
});

test("#5 刷新恢复 / 别的标签页接手带矛盾标记的记录:连续 not_found 只报矛盾、保留记录", async () => {
  const job = { jobId: "job-1", wfName: "wf", startedAt: 1_000_000_000, cancelContradicted: "running" };
  const tab = makeTab({ store: makeStore({ [JOBS]: [job] }), fetch: (url, o, obs) => {
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "not_found" }); }
    return json(200, { ok: true });
  } });
  let rctx = null;
  const np = tab.sb.newProgress;
  tab.sb.newProgress = (...a) => (rctx = np(...a));
  await tab.sb.recoverOne({ ...job }, 1200);
  assert.equal(tab.observed.polls, NOT_FOUND_STREAK);
  assert.equal(tab.jobs().length, 1);
  assert.match(rctx.els.label.textContent, /recover\.contradicted/);
  assert(tab.observed.notifies.some((n) => n.m.startsWith("run.gone_contradicted") && n.sev === "error"));
  // 对照:没有标记的照旧按「已过保留期」收工
  const plain = makeTab({ store: makeStore({ [JOBS]: [{ jobId: "job-1", startedAt: 1_000_000_000 }] }),
    fetch: (url) => (url.includes("/poll?") ? json(200, { status: "not_found" }) : json(200, { ok: true })) });
  await plain.sb.recoverOne({ jobId: "job-1", startedAt: 1_000_000_000 }, 1200);
  assert.equal(plain.jobs().length, 0);
});

test("#5 在带矛盾标记的记录上再点取消:复核全 not_found 也不判『没了』,不删记录、不说不再计费", async () => {
  const tab = makeTab({ store: makeStore({ [JOBS]: [{ jobId: "job-1", startedAt: 1, cancelContradicted: "running" }] }),
    fetch: (url, o, obs) => {
      if (url.endsWith("/cancel")) { obs.cancels++; return json(200, { ok: true, still_billing: false, status: "not_found", error: "job not found" }); }
      if (url.includes("/poll?")) return json(200, { status: "not_found" });
      return json(200, { ok: true });
    } });
  await tab.sb.requestCancel("job-1", tab.sb.newProgress("running", "wf"), "wf");
  assert.equal(tab.jobs().length, 1);
  assert(!tab.observed.notifies.some((n) => n.m.startsWith("cancel.gone_msg")));
  assert.equal(tab.observed.alerts.length, 1);
  assert(/cancel\.state_inconsistent.*running/.test(tab.observed.alerts[0]), tab.observed.alerts[0]);
});

// =============================================================================
// #6 [P3] 取消在途时用户关掉卡片:弹窗不再说「在卡片上再点 ✕」
// =============================================================================
test("#6 取消在途时点 × 关掉卡片,随后取消失败:弹窗提示刷新页面后在恢复的卡片上操作", async () => {
  let release;
  const tab = makeTab({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return submitOk();
    if (url.includes("/poll?")) { obs.polls++; return json(200, obs.polls > 6 ? done() : { status: "running" }); }
    if (url.endsWith("/cancel")) return new Promise((r) => { release = () => r(json(502, { ok: false, still_billing: true, error: "upstream timeout" })); });
    if (url.endsWith("/fetch_result")) return fetchOk();
    return json(200, { ok: true });
  } });
  const ctx = tab.sb.newProgress("submitting", "wf");
  let k = 0, cancelP = null;
  tab.sb.sleep = async (ms) => { tab.clock.t += ms; if (++k === 2) cancelP = ctx.onCancel(); };
  const run = tab.sb.runOnceOnModal({}, ["9"], ctx, null);
  for (let i = 0; i < 50 && !release; i++) await tick();
  await ctx.els.cancel.onclick({ stopPropagation() {} });   // ✕ 此刻是「关闭」
  assert.equal(ctx.closed, true);
  release();
  await cancelP;
  const r = await run;
  assert.equal(tab.observed.alerts.length, 1);
  assert(tab.observed.alerts[0].includes("cancel.retry_hint_closed"), tab.observed.alerts[0]);
  assert(!/cancel\.retry_hint(?!_closed)/.test(tab.observed.alerts[0]), "卡片已经没了,不能再指向它的 ✕");
  assert.equal(r.jobId, "job-1", "后台照常跟踪到结束");
});

test("#6 复核没看清(unknown)时卡片已关:同样用『刷新后在恢复的卡片上操作』", async () => {
  const tab = makeTab({ fetch: (url) => {
    if (url.endsWith("/cancel")) return json(200, { ok: true, still_billing: false, status: "not_found", error: "job not found" });
    if (url.includes("/poll?")) return json(502, { error: "upstream timeout" });
    return json(200, { ok: true });
  } });
  tab.sb.addActiveJob({ jobId: "job-1", startedAt: 1 });
  const ctx = tab.sb.newProgress("running", "wf");
  ctx.finish(false, "✕ Cancelled");
  ctx.closeCard();
  await tab.sb.requestCancel("job-1", ctx, "wf", null, { onUnconfirmed: () => {} });
  assert(tab.observed.alerts[0].includes("cancel.retry_hint_closed"), tab.observed.alerts[0]);
  // 卡片还在:原来的提示
  const tab2 = makeTab({ fetch: (url) => (url.endsWith("/cancel")
    ? json(502, { ok: false, still_billing: true, error: "x" }) : json(200, { ok: true })) });
  await tab2.sb.requestCancel("job-1", tab2.sb.newProgress("running", "wf"), "wf", null, { onUnconfirmed: () => {} });
  assert(/cancel\.retry_hint$/.test(tab2.observed.alerts[0]), tab2.observed.alerts[0]);
});

test("#6 文案:卡片关掉时指向『刷新页面 → 恢复出来的卡片』,不承诺一定有通知", () => {
  const sb = { app: { ui: { settings: { getSettingValue: () => "zh" } } }, navigator: { language: "zh" } };
  vm.createContext(sb);
  vm.runInContext(between("function _locale()", "const sleep =") + "\nthis.I18N = I18N;", sb);
  const T = sb.I18N["cancel.retry_hint_closed"];
  assert(T.zh.includes("刷新页面") && T.zh.includes("恢复出来的卡片") && T.zh.includes("后台仍在跟踪"), T.zh);
  assert(T.en.includes("reload the page") && T.en.includes("recovered card"), T.en);
});

// =============================================================================
// #7 [P3] 提交结果未知:not_found 的确认窗口拉到分钟级
// =============================================================================
test("#7 结果未知、run_endpoint 冷启动 90 秒后才落地:中途的连续 not_found 不判『没落地』,随后照常取回", async () => {
  const tab = makeTab({ fetch: (url, o, obs, clock) => {
    if (url.endsWith("/submit")) { obs.t0 = clock.t; return json(502, { error: "提交结果未知", job_id: "job-u", outcome: "unknown" }); }
    if (url.includes("/poll?")) {
      obs.polls++;
      const el = clock.t - obs.t0;
      if (el < 90_000) return json(200, { status: "not_found", error: "job not found" });
      return el < 100_000 ? json(200, { status: "queued" }) : json(200, done());
    }
    if (url.endsWith("/fetch_result")) { obs.fetches++; return fetchOk(); }
    return json(200, { ok: true });
  } });
  const r = await tab.sb.runOnceOnModal({}, ["9"], tab.sb.newProgress("submitting", "wf"), null);
  assert.equal(r.jobId, "job-u");
  assert(tab.observed.polls > NOT_FOUND_STREAK * 10, "90 秒的 not_found 远超 5 次");
});

test("#7 结果未知但看到过状态之后,not_found 照常按 5 次判(已过保留期)", async () => {
  const tab = makeTab({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(502, { error: "x", job_id: "job-u", outcome: "unknown" });
    if (url.includes("/poll?")) { obs.polls++; return json(200, obs.polls === 1 ? { status: "queued" } : { status: "not_found" }); }
    return json(200, { ok: true });
  } });
  await assert.rejects(tab.sb.runOnceOnModal({}, ["9"], tab.sb.newProgress("submitting", "wf"), null), /run\.job_gone/);
  assert.equal(tab.observed.polls, 1 + NOT_FOUND_STREAK);
});

test("#7 文案:『多半没落地』,先到 Modal 控制台核实再决定是否重交", () => {
  const sb = { app: { ui: { settings: { getSettingValue: () => "zh" } } }, navigator: { language: "zh" } };
  vm.createContext(sb);
  vm.runInContext(between("function _locale()", "const sleep =") + "\nthis.I18N = I18N; this.tt = t;", sb);
  const T = sb.I18N["run.submit_not_landed"];
  assert(T.zh.includes("多半") && T.zh.includes("Modal 控制台") && T.zh.includes("核实"), T.zh);
  assert(!T.zh.includes("可以重新提交"), "不能再直接说『可以重新提交』");
  assert(T.en.includes("most likely") && T.en.includes("Modal dashboard"), T.en);
  assert(sb.tt("run.submit_not_landed", { id: "job-u", min: 2 }).includes("2 分钟"));
});

// =============================================================================
// #8 [P3] /list_nodes 的 cloud_unchecked:面板显示原因,移除确认框加警告
// =============================================================================
function dialogSandbox(routes) {
  const created = [];
  const obs = { notifies: [], confirms: [], streams: [] };
  const sb = {
    JSON, String, Object, Array, Promise, Error, parseInt, Math,
    document: { createElement: (tag) => { const e = domEl(tag); created.push(e); return e; }, body: domEl("body") },
    _locale: () => "zh",
    t: (key, vars) => (vars ? key + " " + JSON.stringify(vars) : key),
    log: () => {}, err: () => {},
    notify: (m, s) => obs.notifies.push([m, s]),
    confirm: (m) => { obs.confirms.push(m); return true; },
    setRunReady: () => {}, doHealthCheck: () => {}, isModalOutage: async () => false,
    bridgeFetch: async (url) => {
      const r = routes[url.split("?")[0]];
      if (!r) throw new Error("unexpected " + url);
      return typeof r === "function" ? r() : r;
    },
    streamPost: async (p, body) => { obs.streams.push({ path: p, body }); return 0; },
    window: { open() {} },
  };
  vm.createContext(sb);
  vm.runInContext(between("function escHtml(", "const LOCAL_NODE_BAKED_SENTINEL"), sb);
  vm.runInContext(between("function failLine(", "async function checkNodesOnModal("), sb);
  vm.runInContext(chunk("async function fetchConfig("), sb);
  return { sb, obs, created };
}
async function openPanel(listNodes) {
  const d = dialogSandbox({
    "/modal_bridge/config": json(200, {}),
    "/modal_bridge/version": json(200, { reachable: true, match: true, local: "1", deployed: "1" }),
    "/modal_bridge/list_nodes": json(200, listNodes),
    "/modal_bridge/list_local_nodes": json(200, { ok: true, nodes: ["loc"] }),
    "/modal_bridge/remove_local_node": json(200, { ok: true }),
  });
  await d.sb.openDeployDialog();
  await ticks(3);
  const panel = d.created[1];
  return { ...d, panel, q: (s) => panel.querySelector(s) };
}

test("#8 /list_nodes 带 cloud_unchecked:状态行说出原因(黄色),移除镜像节点的确认框加『以本机清单为准』警告", async () => {
  const d = await openPanel({ ok: true, source: "local", cloud_unchecked: "Modal /health timeout",
    nodes: [{ name: "a", url: "ua", commit: "ca" }, { name: "b", url: "ub", commit: "cb" }] });
  await d.q("#mb-nodes-load").onclick();
  const st = d.q("#mb-nodes-status");
  assert(st.textContent.includes("mn.cloud_unchecked") && st.textContent.includes("Modal /health timeout"), st.textContent);
  assert.equal(st.style.color, "#fbbf24");
  d.panel._all[".mb-node-cb:checked"] = [{ dataset: { i: "0" } }];
  await d.q("#mb-nodes-prune").onclick();
  assert.equal(d.obs.confirms.length, 1);
  assert(d.obs.confirms[0].includes("mn.confirm_unchecked") && d.obs.confirms[0].includes("Modal /health timeout"),
         d.obs.confirms[0]);
  assert.deepEqual(JSON.parse(JSON.stringify(d.obs.streams[0].body.prune)), ["a"]);   // 跨 vm realm,先拍平
});

test("#8 读得到云端时不加警告;只勾 Volume 本地包(不重部署)也不加", async () => {
  const ok = await openPanel({ ok: true, source: "modal", nodes: [{ name: "a", url: "ua", commit: "ca" }] });
  await ok.q("#mb-nodes-load").onclick();
  assert(!ok.q("#mb-nodes-status").textContent.includes("mn.cloud_unchecked"));
  assert.equal(ok.q("#mb-nodes-status").style.color, "#9aa");
  ok.panel._all[".mb-node-cb:checked"] = [{ dataset: { i: "0" } }];
  await ok.q("#mb-nodes-prune").onclick();
  assert(!ok.obs.confirms[0].includes("mn.confirm_unchecked"));

  const loc = await openPanel({ ok: true, source: "local", cloud_unchecked: "timeout", nodes: [{ name: "a", url: "ua", commit: "ca" }] });
  await loc.q("#mb-nodes-load").onclick();
  loc.panel._all[".mb-node-cb:checked"] = [{ dataset: { i: "1" } }];   // 只勾本地包 loc
  await loc.q("#mb-nodes-prune").onclick();
  assert(!loc.obs.confirms[0].includes("mn.confirm_unchecked"), loc.obs.confirms[0]);
});

test("#8 文案:确认框警告说清『以本机清单为准、别的机器加的节点没法并回』", () => {
  const sb = { app: { ui: { settings: { getSettingValue: () => "zh" } } }, navigator: { language: "zh" } };
  vm.createContext(sb);
  vm.runInContext(between("function _locale()", "const sleep =") + "\nthis.I18N = I18N;", sb);
  const T = sb.I18N["mn.confirm_unchecked"];
  assert(T.zh.includes("以本机清单为准") && T.zh.includes("别的机器加的节点没法并回"), T.zh);
  assert(T.en.includes("this machine's list") && T.en.includes("other machines"), T.en);
});

// =============================================================================
// #9 契约 D1:completed 带 warnings 时给 warn 级提示(主流程、恢复、取消没赶上)
// =============================================================================
const WARNINGS = ["节点 12 (SaveImage):输出分支被剔除 —— 缺输入 images", "节点 15 (PreviewImage):x",
                  "节点 20 (VHS_VideoCombine):y", "节点 21:z", "节点 22:w"];

test("#9 主流程:completed 带 5 条 warnings → 一条 warn 提示,列前 3 条 + 『… +2』,显示时间放长", async () => {
  const tab = makeTab({ fetch: (url) => {
    if (url.endsWith("/submit")) return submitOk();
    if (url.includes("/poll?")) return json(200, done({ warnings: WARNINGS }));
    if (url.endsWith("/fetch_result")) return fetchOk();
    return json(200, { ok: true });
  } });
  await tab.sb.runOnceOnModal({}, ["9"], tab.sb.newProgress("submitting", "wf"), null);
  const w = tab.observed.notifies.filter((n) => n.m.startsWith("run.warnings"));
  assert.equal(w.length, 1);
  assert.equal(w[0].sev, "warn");
  assert(w[0].life >= 10000, "多行清单 4 秒读不完");
  const vars = JSON.parse(w[0].m.slice("run.warnings ".length));
  assert.equal(vars.n, 5);
  assert.equal(vars.wf, "「wf」");
  assert(vars.list.includes("节点 12 (SaveImage)") && vars.list.includes("节点 20") && !vars.list.includes("节点 21"), vars.list);
  assert(vars.list.endsWith("… +2"), vars.list);
});

test("#9 刷新恢复 / 取消没赶上:同样提示,且同一个 job 只提示一次", async () => {
  const rec = makeTab({ store: makeStore({ [JOBS]: [{ jobId: "job-1", wfName: "wf", startedAt: 1_000_000_000 }] }),
    fetch: (url) => {
      if (url.includes("/poll?")) return json(200, done({ warnings: ["节点 7:分支被剔除"] }));
      if (url.endsWith("/fetch_result")) return fetchOk();
      return json(200, { ok: true });
    } });
  await rec.sb.recoverOne({ jobId: "job-1", wfName: "wf", startedAt: 1_000_000_000 }, 1200);
  assert.equal(rec.observed.notifies.filter((n) => n.m.startsWith("run.warnings")).length, 1);

  const noop = makeTab({ fetch: (url) => {
    if (url.endsWith("/cancel")) return json(200, { ok: true, still_billing: false, id: "job-1", status: "completed", cancel_noop: true });
    if (url.includes("/poll?")) return json(200, done({ warnings: ["节点 7:分支被剔除"] }));
    if (url.endsWith("/fetch_result")) return fetchOk();
    return json(200, { ok: true });
  } });
  noop.sb.addActiveJob({ jobId: "job-1", startedAt: 1 });
  await noop.sb.requestCancel("job-1", noop.sb.newProgress("running", "wf"), "wf");
  assert.equal(noop.observed.notifies.filter((n) => n.m.startsWith("run.warnings")).length, 1);
});

test("#9 没有 warnings / 空数组 / 全是空串:不提示;取回失败也不提示", async () => {
  for (const extra of [{}, { warnings: [] }, { warnings: ["", "  "] }, { warnings: "not a list" }]) {
    const tab = makeTab({ fetch: (url) => {
      if (url.endsWith("/submit")) return submitOk();
      if (url.includes("/poll?")) return json(200, done(extra));
      if (url.endsWith("/fetch_result")) return fetchOk();
      return json(200, { ok: true });
    } });
    await tab.sb.runOnceOnModal({}, ["9"], tab.sb.newProgress("submitting", "wf"), null);
    assert(!tab.observed.notifies.some((n) => n.m.startsWith("run.warnings")), JSON.stringify(extra));
  }
  const fail = makeTab({ fetch: (url) => {
    if (url.endsWith("/submit")) return submitOk();
    if (url.includes("/poll?")) return json(200, done({ warnings: ["x"] }));
    if (url.endsWith("/fetch_result")) return json(502, { error: "disk full" });
    return json(200, { ok: true });
  } });
  await assert.rejects(fail.sb.runOnceOnModal({}, ["9"], fail.sb.newProgress("submitting", "wf"), null));
  assert(!fail.observed.notifies.some((n) => n.m.startsWith("run.warnings")), "取回成功后再提示(刷新重取时会提示)");
});

test("#9 真实文案:中文提示里有任务 id、条数和清单", async () => {
  const tab = makeTab({ realI18n: true, fetch: (url) => {
    if (url.endsWith("/submit")) return submitOk("abcdef123456");
    if (url.includes("/poll?")) return json(200, done({ warnings: ["节点 12 (SaveImage):输出分支被剔除"] }));
    if (url.endsWith("/fetch_result")) return fetchOk();
    return json(200, { ok: true });
  } });
  // 真实 t() 已经装进沙箱;notify 仍是桩
  await tab.sb.runOnceOnModal({}, ["9"], tab.sb.newProgress("submitting", "wf"), null);
  const m = tab.observed.notifies.find((n) => n.m.includes("警告")).m;
  assert(m.includes("abcdef12") && m.includes("1 条警告") && m.includes("• 节点 12 (SaveImage):输出分支被剔除"), m);
  assert(m.startsWith("⚠ 「wf」任务"), m);
});

test("notify 的 life 参数透传给 toast;不给时按严重程度取默认", () => {
  const added = [];
  const sb = { app: { extensionManager: { toast: { add: (o) => added.push(o) } } }, log: () => {} };
  vm.createContext(sb);
  vm.runInContext(chunk("function notify("), sb);
  sb.notify("a", "warn", 12000);
  sb.notify("b", "warn");
  sb.notify("c", "error");
  assert.deepEqual(added.map((o) => o.life), [12000, 4000, 8000]);
});


// =============================================================================
// 第三轮(第二轮复核报出)
// =============================================================================
test("R3-1 结果未知后 2 分钟内刷新:恢复流程同样用分钟级窗口,任务 60 秒后落地照常取回", async () => {
  const t0 = 1_000_000_000;
  const job = { jobId: "job-u", wfName: "wf", startedAt: t0, submitUnknown: true };
  const tab = makeTab({ store: makeStore({ [JOBS]: [job] }), clock: { t: t0 + 5_000 }, fetch: (url, o, obs, clock) => {
    if (url.includes("/poll?")) {
      obs.polls++;
      const el = clock.t - t0;
      if (el < 60_000) return json(200, { status: "not_found" });
      return el < 70_000 ? json(200, { status: "queued" }) : json(200, done());
    }
    if (url.endsWith("/fetch_result")) { obs.fetches++; return fetchOk(); }
    return json(200, { ok: true });
  } });
  await tab.sb.recoverOne({ ...job }, 1200);
  assert.equal(tab.observed.fetches, 1, "落地后要照常取回");
  assert(tab.observed.polls > NOT_FOUND_STREAK * 5, "60 秒的 not_found 远超 5 次也不能判没落地");
});

test("R3-1 结果未知、一直 not_found:满 2 分钟才判『多半没落地』,文案用 submit_not_landed", async () => {
  const t0 = 1_000_000_000;
  const job = { jobId: "job-u", wfName: "wf", startedAt: t0, submitUnknown: true };
  const tab = makeTab({ store: makeStore({ [JOBS]: [job] }), clock: { t: t0 + 5_000 }, fetch: (url, o, obs) => {
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "not_found" }); }
    return json(200, { ok: true });
  } });
  await tab.sb.recoverOne({ ...job }, 1200);
  assert(tab.clock.t - t0 >= 120_000, "不到 2 分钟就判了");
  assert.equal(tab.jobs().length, 0);
  assert(tab.observed.notifies.some((n) => n.m.startsWith("run.submit_not_landed")));
});

test("R3-1 主流程:第一次看到状态就把记录里的 submitUnknown 清掉(之后刷新按普通口径)", async () => {
  const tab = makeTab({ fetch: (url, o, obs, clock) => {
    if (url.endsWith("/submit")) return json(502, { error: "未知", job_id: "job-u", outcome: "unknown" });
    if (url.includes("/poll?")) { obs.polls++; return obs.polls < 3 ? json(200, { status: "queued" }) : json(200, { status: "running", started_at: 1, timeout_s: 1200 }); }
    return json(200, { ok: true });
  } });
  const p = tab.sb.runOnceOnModal({}, ["9"], tab.sb.newProgress("submitting", "wf"), null).catch(() => {});
  for (let i = 0; i < 40 && tab.observed.polls < 2; i++) await tick();
  const rec = tab.jobs().find((j) => j.jobId === "job-u");
  assert(rec && rec.submitUnknown === false, JSON.stringify(rec));
  p.then(() => {});
});

test("R3-2 主人标签页崩溃(没 pagehide)、心跳还新鲜:心跳过期后由定期检查接手,本页自己保留的记录不碰", async () => {
  const now = 1_000_000_000;
  const store = makeStore({
    [JOBS]: [{ jobId: "job-c", startedAt: now, tabId: "crashed-tab" }, { jobId: "job-mine", startedAt: now, tabId: "SELF" }],
    [HB + "crashed-tab"]: { t: now, jobs: ["job-c"] },
  });
  const clock = { t: now + 10_000 };
  const B = makeTab({ store, clock, fetch: () => json(200, {}) });
  // 「本页自己保留」的那条:tabId 改成 B 自己的
  store.set(JOBS, store.get(JOBS).map((j) => (j.tabId === "SELF" ? { ...j, tabId: B.tabId } : j)));
  const adopted = [];
  B.sb.recoverOne = (j) => adopted.push(j.jobId);
  B.sb.adoptOrphanJobs();
  assert.deepEqual(adopted, [], "主人心跳还新鲜时不接");
  clock.t = now + 200_000;   // 超过 120 秒阈值
  B.sb.adoptOrphanJobs();
  assert.deepEqual(adopted, ["job-c"], "心跳过期后要接手;本页自己保留的那条不碰");
  assert.equal(B.jobs().find((j) => j.jobId === "job-c").tabId, B.tabId);
  B.sb.adoptOrphanJobs();
  assert.deepEqual(adopted, ["job-c"], "接手过的不会重复接");
});

test("R3-3 取消失败的警告不会被『连续查不到状态』盖掉、也不会被它的恢复撤掉", () => {
  const tab = makeTab({ fetch: () => json(200, {}) });
  const ctx = tab.sb.newProgress("queued", "wf");
  ctx.setWarn("取消失败,可能仍在计费", "cancel");
  const w = tab.sb.transientWatch("job-1", ctx);
  w.transient({ error: "x" });
  tab.clock.t += 10 * 60 * 1000;
  w.transient({ error: "x" });
  assert.equal(ctx.warnKind, "cancel");
  assert.match(ctx.els.warn.textContent, /取消失败/);
  w.seen();
  assert.match(ctx.els.warn.textContent, /取消失败/, "状态恢复时不能把取消失败那条撤掉");
});

test("R3-4 带矛盾标记的记录:30 分钟内反复刷新只弹一次 error", async () => {
  const job = { jobId: "job-1", wfName: "wf", startedAt: 1_000_000_000, cancelContradicted: "queued" };
  const store = makeStore({ [JOBS]: [job] });
  const clock = { t: 1_000_000_000 + 60_000 };
  const mk = () => makeTab({ store, clock, fetch: (url) => (url.includes("/poll?") ? json(200, { status: "not_found" }) : json(200, { ok: true })) });
  const a = mk();
  await a.sb.recoverOne({ ...store.get(JOBS)[0] }, 1200);
  clock.t += 5 * 60 * 1000;
  const b = mk();
  await b.sb.recoverOne({ ...store.get(JOBS)[0] }, 1200);
  const errs = [...a.observed.notifies, ...b.observed.notifies].filter((n) => n.sev === "error");
  assert.equal(errs.length, 1, JSON.stringify(errs));
  clock.t += 40 * 60 * 1000;
  const c = mk();
  await c.sb.recoverOne({ ...store.get(JOBS)[0] }, 1200);
  assert.equal(c.observed.notifies.filter((n) => n.sev === "error").length, 1, "过了 30 分钟再提示一次");
});

