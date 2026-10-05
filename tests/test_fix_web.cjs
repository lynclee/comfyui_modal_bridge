// 2026-10-05 深度 review(fix1005)前端修复的回归测试。
// 执行 web/modal_bridge.js 的真实函数:网络 / DOM / 时钟 / 存储全部桩掉(虚拟时钟,不真等)。
// 运行:node --test tests/test_fix_web.cjs
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../web/modal_bridge.js"), "utf8");
const NOT_FOUND_STREAK = Number(/const NOT_FOUND_STREAK = (\d+);/.exec(source)[1]);

// 从 startMarker 截到下一个分节线(源码按 `// =====` 分节)
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

// 假 DOM:够 newProgress / 部署面板用。querySelector 按选择器缓存同一个假元素。
function domEl(tag = "div") {
  const el = {
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
  return el;
}

// ── 轮询 / 取消 / 取回 / 恢复 的沙箱 ─────────────────────────────────────────
function makeRunSandbox(opts = {}) {
  const clock = { t: 1_000_000_000 };
  let saved = opts.saved ? JSON.parse(JSON.stringify(opts.saved)) : [];
  const observed = { polls: 0, cancels: 0, fetches: 0, alerts: [], notifies: [], events: [],
                     fetchBodies: [], cancelBodies: [], submitted: 0 };
  const sandbox = {
    Date: { now: () => clock.t }, Headers, JSON, Math, String, Number, Object, Array, Promise, Error,
    parseInt, encodeURIComponent, Map, setTimeout: () => 0,
    setInterval: () => 1, clearInterval: () => {},
    LS_KEYS: { activeJob: "jobs", progressPos: "pos" },
    loadLS: (k, d = null) => (k === "jobs" ? JSON.parse(JSON.stringify(saved)) : d),
    saveLS: (k, v) => { if (k === "jobs") saved = JSON.parse(JSON.stringify(v)); },
    getSetting: (k, d) => (opts.settings && k in opts.settings ? opts.settings[k] : d),
    getVramTier: () => "80g",
    sleep: async (ms) => { clock.t += ms; },
    log: () => {}, err: () => {},
    notify: (m) => observed.notifies.push(m),
    alert: (m) => observed.alerts.push(m),
    confirm: () => true,
    reportJobEvent: (id, ev) => observed.events.push(ev),
    t: (key, vars) => (vars ? key + " " + JSON.stringify(vars) : key),
    fmtRate: String, fmtDur: String,
    MODEL3D_EXT_RE: /\.glb$/i,
    displayInGraph: () => 1, activeWorkflowKey: () => null, storePendingResult: () => {},
    document: { createElement: domEl, body: domEl("body"), addEventListener() {} },
    window: { innerWidth: 1000 },
    bridgeFetch: async (url, o) => opts.fetch(url, o, observed, clock),
  };
  vm.createContext(sandbox);
  vm.runInContext(chunk("const NOT_FOUND_STREAK ="), sandbox);   // STAGE_LABELS / newProgress
  vm.runInContext(chunk("function addActiveJob("), sandbox);      // 持久化 / 截止线 / 取消 / 轮询 / 取回
  vm.runInContext(chunk("async function recoverPendingJob("), sandbox);
  return { sb: sandbox, observed, clock, saved: () => saved, setSaved: (v) => { saved = v; } };
}

// 模拟云端:提交后排队 queueS 秒才变 running(started_at=那一刻),跑 runS 秒完成。
// worker 上限按 Modal 语义从 run() 开始计(排队 / @enter 不计)。
function cloud({ queueS, runS, workerS, stepS = 38, steps = 30, onPoll, cancelResp }) {
  let t0 = null, cancelledAt = null;
  return (url, o, obs, clock) => {
    if (url.endsWith("/submit")) {
      obs.submitted++;
      t0 = clock.t;
      return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: workerS });
    }
    if (t0 == null) t0 = clock.t;   // 恢复路径:没有 submit
    const el = (clock.t - t0) / 1000;
    if (url.includes("/poll?")) {
      obs.polls++;
      if (onPoll) { const r = onPoll(el, obs); if (r) return r; }
      if (cancelledAt != null) return json(200, { status: "cancelled" });
      if (el < queueS) return json(200, { status: "queued" });
      const runEl = el - queueS;
      if (runEl >= runS) return json(200, { status: "completed", images: [{ filename: "a.mp4", data_base64: "AA==", node_id: "9" }] });
      if (runEl > workerS + 120) return json(200, { status: "failed", error: "worker 超过部署时的超时上限" });
      const step = Math.min(steps, Math.max(1, Math.floor(runEl / stepS)));
      return json(200, { status: "running", started_at: (t0 / 1000) + queueS, timeout_s: workerS,
                         progress: { step, total: steps, s_it: stepS, n_samples: 5 } });
    }
    if (url.endsWith("/cancel")) {
      obs.cancels++;
      obs.cancelAtS = el;
      if (cancelResp) return cancelResp(obs);
      cancelledAt = el;
      return json(200, { ok: true, still_billing: false, id: "job-1", status: "cancelled", was_running: true });
    }
    if (url.endsWith("/fetch_result")) {
      obs.fetches++;
      obs.fetchBodies.push(o && o.body);
      return json(200, { ok: true, outputs: [{ filename: "a.mp4" }] });
    }
    if (url.includes("/fetch_progress")) return json(200, { ok: false });
    if (url.endsWith("/job_event")) return json(200, { ok: true });
    throw new Error("unexpected " + url);
  };
}

function captureStages(ctx) {
  const labels = [];
  const orig = ctx.stage;
  ctx.stage = (k, d, c) => { labels.push(String(d)); return orig(k, d, c); };
  return labels;
}

// =============================================================================
// F-P1-1 / C8:截止线从「第一次看到 running」起算,排队不计
// =============================================================================
test("F-P1-1 排队+冷启动 240s、运行 1150s(< worker 1200s)的正常任务:不被前端取消,产物取回", async () => {
  let recAtFetch = null;
  const base = cloud({ queueS: 240, runS: 1150, workerS: 1200 });
  const h = makeRunSandbox({ fetch: (url, o, obs, clock) => {
    if (url.endsWith("/fetch_result")) recAtFetch = h.saved().find((j) => j.jobId === "job-1");
    return base(url, o, obs, clock);
  } });
  const ctx = h.sb.newProgress("submitting", "wf");
  const r = await h.sb.runOnceOnModal({}, ["9"], ctx, null);
  assert.equal(h.observed.cancels, 0, "前端在 worker 上限之前取消了正常任务");
  assert.equal(h.observed.fetches, 1);
  assert.equal(r.jobId, "job-1");
  // runSeenAt 写进了恢复记录(刷新恢复靠它),口径是本地时刻、晚于排队结束
  assert(recAtFetch && recAtFetch.runSeenAt >= 1_000_000_000 + 240_000, JSON.stringify(recAtFetch));
  assert(recAtFetch.runSeenAt <= 1_000_000_000 + 240_000 + 1300);
  assert.equal(recAtFetch.runTimeoutSec, 1200);
});

test("F-P1-1 排队阶段不由前端截止:排队 2 小时(设置 1200s)后正常跑完", async () => {
  const h = makeRunSandbox({ fetch: cloud({ queueS: 7200, runS: 60, workerS: 1200 }) });
  const ctx = h.sb.newProgress("submitting", "wf");
  await h.sb.runOnceOnModal({}, ["9"], ctx, null);
  assert.equal(h.observed.cancels, 0);
  assert.equal(h.observed.fetches, 1);
});

test("F-P1-1 运行期截止线仍在:跑过 runSeenAt + worker 上限 + 3 分钟仍不结束 → 请求取消", async () => {
  // 云端一直报 running(判死也没写回),前端兜底在 runSeenAt + 1200 + 180 取消
  const h = makeRunSandbox({ fetch: cloud({ queueS: 100, runS: 1e9, workerS: 1200, onPoll: (el) =>
    (el >= 100 ? json(200, { status: "running", started_at: 1, timeout_s: 1200 }) : null) }) });
  const ctx = h.sb.newProgress("submitting", "wf");
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], ctx, null), /Polling timed out/);
  assert.equal(h.observed.cancels, 1);
  assert(h.observed.cancelAtS >= 100 + 1200 + 180, `取消太早:${h.observed.cancelAtS}`);
  assert(h.observed.cancelAtS <= 100 + 1200 + 180 + 5, `取消太晚:${h.observed.cancelAtS}`);
});

test("F-P1-1 云端 started_at 变了(抢占后重跑):runSeenAt 跟着重置,截止线往后挪", () => {
  const h = makeRunSandbox({ fetch: () => json(200, {}) });
  const rec = { jobId: "j", startedAt: h.clock.t, runSeenAt: null, runStartedAt: null, runTimeoutSec: null };
  h.setSaved([{ jobId: "j" }]);
  h.sb.noteRunning(rec, { status: "running", started_at: 100, timeout_s: 1200 });
  const first = rec.runSeenAt;
  assert.equal(first, h.clock.t);
  h.clock.t += 500_000;
  h.sb.noteRunning(rec, { status: "running", started_at: 100, timeout_s: 1200 });
  assert.equal(rec.runSeenAt, first, "同一次执行不重置");
  h.clock.t += 500_000;
  h.sb.noteRunning(rec, { status: "running", started_at: 1100, timeout_s: 1200 });
  assert.equal(rec.runSeenAt, h.clock.t, "换了一次执行要重置");
  assert.equal(h.saved()[0].runSeenAt, h.clock.t, "并写进恢复记录");
  assert.equal(h.sb.jobDeadline({ startedAt: 0 }, 1200), Infinity, "没见过 running = 排队阶段,不截止");
});

test("F-P1-1 刷新恢复同口径:提交 1385s 前、running 1145s 前的任务不被当过期丢掉,也不被提前取消", async () => {
  const now = 1_000_000_000;
  const job = { jobId: "job-1", wfName: "wf", startedAt: now - 1_385_000, runSeenAt: now - 1_145_000,
                runTimeoutSec: 1200, workerTimeoutSec: 1200 };
  // 旧口径:startedAt + max(1200, 1200+180) = now - 5s → 已过期,直接丢掉、没人去取
  const h = makeRunSandbox({ saved: [job], fetch: cloud({ queueS: 0, runS: 1145 + 200, workerS: 1200 }) });
  h.clock.t = now;
  const recovered = [];
  const realRecoverOne = h.sb.recoverOne;
  h.sb.recoverOne = (j, s) => { recovered.push(j.jobId); return realRecoverOne(j, s); };
  await h.sb.recoverPendingJob();
  assert.deepEqual(recovered, ["job-1"]);
  // 恢复轮询:云端还要 200s 才完成(1145+200 > 1200 是故意的:晚于「提交 + 1380」,早于 runSeen + 1380)
  const h2 = makeRunSandbox({ saved: [job], fetch: (u, o, obs, clock) => {
    if (u.includes("/poll?")) {
      obs.polls++;
      return clock.t >= now + 200_000
        ? json(200, { status: "completed", images: [{ filename: "a.png", data_base64: "AA==" }] })
        : json(200, { status: "running", started_at: 5, timeout_s: 1200 });
    }
    return cloud({ queueS: 0, runS: 1, workerS: 1200 })(u, o, obs, clock);
  } });
  h2.clock.t = now;
  await h2.sb.recoverOne({ ...job }, 1200);
  assert.equal(h2.observed.cancels, 0, "恢复路径在 worker 上限之前取消了正常任务");
  assert.equal(h2.observed.fetches, 1);
});

test("F-P1-1 刷新恢复:没见过 running 的老记录(排队阶段)不过期;恢复时看到 running 补记 runSeenAt", async () => {
  const now = 1_000_000_000;
  const job = { jobId: "job-1", startedAt: now - 7_200_000, workerTimeoutSec: 1200 };
  const h = makeRunSandbox({ saved: [job], fetch: () => json(200, {}) });
  h.clock.t = now;
  assert.equal(h.sb.recoveryDeadline(job, 1200), Infinity);
  let n = 0;
  h.sb.bridgeFetch = async (u) => {
    if (u.includes("/poll?")) {
      n++;
      if (n === 1) return json(200, { status: "running", started_at: 9, timeout_s: 1800 });
      return json(200, { status: "failed", error: "x" });
    }
    return json(200, { ok: true });
  };
  await h.sb.recoverOne({ ...job }, 1200);
  // failed 会删记录;在删之前 noteRunning 已经写过 —— 用一个不终结的场景单独验写入
  const h2 = makeRunSandbox({ saved: [job], fetch: () => json(200, {}) });
  h2.clock.t = now;
  let m = 0;
  h2.sb.bridgeFetch = async (u) => {
    if (u.includes("/poll?")) {
      m++;
      if (m === 1) return json(200, { status: "running", started_at: 9, timeout_s: 1800 });
      throw new Error("stop");   // 之后全是瞬态
    }
    return json(200, { ok: true });
  };
  // 之后全是瞬态:虚拟时钟一路走到 runSeenAt + 1800 + 180 的截止线,走超时取消收尾
  await h2.sb.recoverOne({ ...job }, 1200);
  const r = h2.saved()[0];
  assert.equal(r.runSeenAt, now, "恢复时第一次看到 running 要补记 runSeenAt");
  assert.equal(r.runTimeoutSec, 1800, "用云端按任务记下的 timeout_s");
  assert(h2.clock.t >= now + (1800 + 180) * 1000, "截止线按 runSeenAt + timeout_s + 尾巴,不按提交时刻");
});

// =============================================================================
// F-P2-3:投影式预警按 runSeenAt + worker 上限算
// =============================================================================
test("F-P2-3 前端设置 3600 > worker 1200:注定被强杀的任务会被预警", async () => {
  const h = makeRunSandbox({ settings: { "ModalBridge.timeoutSec": 3600 },
                             fetch: cloud({ queueS: 30, runS: 1600, workerS: 1200, stepS: 40, steps: 40 }) });
  const ctx = h.sb.newProgress("submitting", "wf");
  const labels = captureStages(ctx);
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], ctx, null), /超时上限/);
  const warned = labels.filter((s) => s.startsWith("run.eta_overrun"));
  assert(warned.length > 0, "40 步 × 40s = 1600s > worker 1200s,必须预警");
  assert(warned.every((s) => s.includes('"limit":20')), warned[0]);
});

test("F-P2-3 默认设置:文案里的 worker 上限是 20 分钟(不是前端等待窗 23 分钟)", async () => {
  const h = makeRunSandbox({ fetch: cloud({ queueS: 10, runS: 1600, workerS: 1200, stepS: 40, steps: 40 }) });
  const ctx = h.sb.newProgress("submitting", "wf");
  const labels = captureStages(ctx);
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], ctx, null));
  const w = labels.find((s) => s.startsWith("run.eta_overrun"));
  assert(w && w.includes('"limit":20'), w);
});

test("F-P2-3 健康任务(跑得完)不预警", async () => {
  const h = makeRunSandbox({ fetch: cloud({ queueS: 300, runS: 1000, workerS: 1200, stepS: 33, steps: 30 }) });
  const ctx = h.sb.newProgress("submitting", "wf");
  const labels = captureStages(ctx);
  await h.sb.runOnceOnModal({}, ["9"], ctx, null);
  assert.equal(labels.filter((s) => s.startsWith("run.eta_overrun")).length, 0,
    "排队 300s 不该算进 worker 的配额里");
});

// =============================================================================
// F-P1-2:看到过 alive 之后的 gone 一律按矛盾处理
// =============================================================================
test("F-P1-2 alive → 重发取消 not_found → 复核全 not_found:弹窗、保留记录,不说『不会继续计费』", async () => {
  let polls = 0;
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.includes("/poll?")) {
      obs.polls++; polls++;
      return polls === 1 ? json(200, { status: "running" }) : json(200, { status: "not_found", error: "job not found" });
    }
    if (url.endsWith("/cancel")) {
      obs.cancels++;
      return json(200, { ok: true, still_billing: false, id: "job-1", status: "not_found", error: "job not found" });
    }
    if (url.endsWith("/job_event")) return json(200, { ok: true });
    throw new Error("unexpected " + url);
  } });
  h.sb.addActiveJob({ jobId: "job-1", startedAt: h.clock.t });
  const ctx = h.sb.newProgress("running", "wf");
  await h.sb.requestCancel("job-1", ctx, "wf");
  assert.equal(h.observed.cancels, 2, "看到活着要重发一次(且只一次)");
  assert(!h.observed.notifies.some((m) => m.startsWith("cancel.gone_msg")), "不能说『没有任务在跑,也不会继续计费』");
  assert.equal(h.saved().length, 1, "恢复记录必须保留");
  assert.equal(h.observed.alerts.length, 1, "必须弹窗");
  assert(h.observed.alerts[0].includes("cancel.state_inconsistent"), h.observed.alerts[0]);
  assert(/status\\?":\\?"running/.test(h.observed.alerts[0]), "文案带上之前看到的状态:" + h.observed.alerts[0]);
});

test("F-P1-2 没见过 alive 时,连续 not_found 仍按『已没了』收尾(行为不变)", async () => {
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "not_found" }); }
    if (url.endsWith("/cancel")) return json(200, { ok: true, still_billing: false, status: "not_found", error: "job not found" });
    return json(200, { ok: true });
  } });
  h.sb.addActiveJob({ jobId: "job-1", startedAt: h.clock.t });
  await h.sb.requestCancel("job-1", h.sb.newProgress("running", "wf"), "wf");
  assert(h.observed.notifies.some((m) => m.startsWith("cancel.gone_msg")));
  assert.equal(h.saved().length, 0);
  assert.equal(h.observed.alerts.length, 0);
});

// =============================================================================
// F-P2-1:取消与在途 poll 竞态
// =============================================================================
test("F-P2-1 取消时 /poll 在途、回来是 running:卡片最终停在 Cancelled,不回到 Running", async () => {
  const pending = deferred();
  const cancelResp = deferred();
  let n = 0;
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: 1200 });
    if (url.includes("/poll?")) {
      obs.polls++; n++;
      const running = json(200, { status: "running", progress: { step: 3, total: 30, s_it: 5, n_samples: 3 } });
      if (n === 2) return pending.p.then(() => running);
      return running;
    }
    if (url.endsWith("/cancel")) { obs.cancels++; return cancelResp.p; }
    if (url.endsWith("/job_event")) return json(200, { ok: true });
    throw new Error("unexpected " + url);
  } });
  const ctx = h.sb.newProgress("submitting", "wf");
  const run = h.sb.runOnceOnModal({}, ["9"], ctx, null);
  while (n < 2) await tick();
  const cancelDone = ctx.onCancel();
  await tick();
  pending.resolve();
  await ticks(5);
  assert.match(ctx.els.label.textContent, /Cancelled/, "在途 poll 不能把卡片改回 Running");
  assert.equal(ctx.runTimer, null, "也不能重启进度条动画");
  cancelResp.resolve(json(200, { ok: true, still_billing: false, id: "job-1", status: "cancelled", was_running: true }));
  await cancelDone;
  const r = await run;
  assert.equal(r.cancelled, true);
  assert.match(ctx.els.label.textContent, /✕ Cancelled/);
  assert.equal(ctx.finished, true);
  assert.equal(h.saved().length, 0);
});

test("F-P2-1 在途 poll 回 completed、取消回 cancel_noop:同一 job 只 /fetch_result 一次", async () => {
  let n = 0, release, finishFirst;
  const done = { status: "completed", completed_at: 1, images: [{ filename: "a.png", data_base64: "AA==", node_id: "9" }] };
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: 1200 });
    if (url.includes("/poll?")) {
      n++;
      if (n === 2) return new Promise((r) => { release = () => r(json(200, done)); });
      return n > 2 ? json(200, done) : json(200, { status: "running" });
    }
    // 云端 cancel_noop 的响应是字段子集,不带 images(见下一条测试)
    if (url.endsWith("/cancel")) return json(200, { ok: true, still_billing: false, id: "job-1", status: "completed",
                                                   completed_at: 1, cancel_noop: true, was_running: true });
    if (url.endsWith("/fetch_result")) {
      obs.fetchBodies.push(o.body);
      if (obs.fetchBodies.length === 1) return new Promise((r) => { finishFirst = () => r(json(200, { ok: true, outputs: [{ filename: "a.png" }] })); });
      return json(409, { error: "job is being fetched with different parameters" });
    }
    return json(200, { ok: false });
  } });
  const ctx = h.sb.newProgress("submitting", "wf");
  const run = h.sb.runOnceOnModal({}, ["9"], ctx, null).then((r) => ({ r }), (e) => ({ e: e.message }));
  while (n < 2) await tick();
  const c = ctx.onCancel();
  await tick();
  release();
  await ticks(10);
  finishFirst();
  const [rr] = await Promise.all([run, c]);
  assert.equal(h.observed.fetchBodies.length, 1, "同一个 job 被取回了两次");
  assert.equal(rr.e, undefined, "不该报失败:" + rr.e);
  assert.match(ctx.els.label.textContent, /cancel too late/);
  assert(!h.observed.notifies.some((m) => m.startsWith("toast.fail")));
});

test("cancel_noop 响应不带 images(云端只回字段子集):先 /poll 拿完整终态再取回,产物正确落盘", async () => {
  const full = { id: "job-1", status: "completed", completed_at: 7,
                 images: [{ filename: "a.png", data_base64: "AA==", node_id: "9" }] };
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/cancel")) return json(200, { ok: true, still_billing: false, id: "job-1", status: "completed",
                                                   completed_at: 7, gpu: "H100", cancel_noop: true, was_running: true });
    if (url.includes("/poll?")) { obs.polls++; return json(200, full); }
    if (url.endsWith("/fetch_result")) {
      obs.fetches++;
      const st = JSON.parse(o.body).modal_state;
      // 与 routes._fetch_result 一致:modal_state 里没有产物就 502
      if (!Array.isArray(st.images) || !st.images.length) return json(502, { error: "no image in modal_state" });
      return json(200, { ok: true, outputs: [{ filename: "a.png", subfolder: "modal_results" }] });
    }
    return json(200, { ok: true });
  } });
  h.sb.addActiveJob({ jobId: "job-1", startedAt: h.clock.t });
  const ctx = h.sb.newProgress("running", "wf");
  ctx.finish(false, "✕ Cancelled");   // 点击时的乐观收尾
  await h.sb.requestCancel("job-1", ctx, "wf");
  assert.equal(h.observed.polls, 1, "要先 /poll 一次拿完整状态");
  assert.equal(h.observed.fetches, 1);
  assert.match(ctx.els.label.textContent, /cancel too late/);
  assert(h.observed.notifies.some((m) => m.startsWith("toast.recovered")));
  assert.equal(h.saved().length, 0, "取回成功才清记录");
});

test("cancel_noop 后补查完整状态没看清:按取回失败收尾,保留恢复记录,不拿残缺状态去取", async () => {
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/cancel")) return json(200, { ok: true, still_billing: false, id: "job-1", status: "completed",
                                                   cancel_noop: true, was_running: true });
    if (url.includes("/poll?")) { obs.polls++; return json(502, { error: "upstream timeout" }); }
    if (url.endsWith("/fetch_result")) { obs.fetches++; return json(502, { error: "no image in modal_state" }); }
    return json(200, { ok: true });
  } });
  h.sb.addActiveJob({ jobId: "job-1", startedAt: h.clock.t });
  const ctx = h.sb.newProgress("running", "wf");
  assert.equal(await h.sb.requestCancel("job-1", ctx, "wf"), false);
  assert.equal(h.observed.fetches, 0);
  assert.equal(h.saved().length, 1, "刷新后还要靠它接着取");
  assert.match(ctx.els.label.textContent, /reload to retry/);
  assert.equal(h.observed.alerts.length, 0, "任务已结束,不是『可能仍在计费』");
});

test("F-P2-1 fetchJobResult 去重:同一 job 并发两次只发一次请求,两边拿到同一结果", async () => {
  const gate = deferred();
  let calls = 0;
  const h = makeRunSandbox({ fetch: (url) => {
    if (url.endsWith("/fetch_result")) { calls++; return gate.p.then(() => json(200, { ok: true, outputs: [{ filename: "x.png" }] })); }
    return json(200, { ok: false });
  } });
  h.sb.addActiveJob({ jobId: "j", startedAt: h.clock.t });
  const a = h.sb.fetchJobResult("j", { status: "completed", images: [] }, null);
  const b = h.sb.fetchJobResult("j", { status: "completed", cancel_noop: true, images: [] }, null);
  gate.resolve();
  const [ra, rb] = await Promise.all([a, b]);
  assert.equal(calls, 1);
  assert.deepEqual(ra, rb);
  // 完成后再取一次,是新的请求(去重只针对在途)
  await h.sb.fetchJobResult("j", { status: "completed", images: [] }, null);
  assert.equal(calls, 2);
});

test("F-P2-1 结束后的卡片不接受 stage 更新;reopen 后才恢复", () => {
  const h = makeRunSandbox({ fetch: () => json(200, {}) });
  const ctx = h.sb.newProgress("running", "wf");
  ctx.finish(false, "✕ Cancelled");
  ctx.stage("running", "inference", true);
  assert.match(ctx.els.label.textContent, /✕ Cancelled/);
  assert.equal(ctx.cancelable, false);
  ctx.reopen("warn!");
  assert.equal(ctx.finished, false);
  assert.equal(ctx.els.warn.textContent, "warn!");
  assert.equal(ctx.els.warn.style.display, "block");
  ctx.stage("running", "inference", true);
  assert.match(ctx.els.label.textContent, /Running/);
  assert.equal(ctx.cancelable, true);
  assert.equal(ctx.els.cancel.textContent, "✕");
  ctx.finish(true);
  assert.equal(ctx.els.warn.style.display, "none", "finish 清掉警告行");
});

// =============================================================================
// F-P2-2:取消失败后继续跟踪,允许重试
// =============================================================================
test("F-P2-2 取消失败:继续轮询,随后完成的任务照常取回;卡片恢复成可取消并带警告", async () => {
  let cancelAt = null;
  let snapshot = null;
  const h = makeRunSandbox({ fetch: (url, o, obs, clock) => {
    if (url.endsWith("/submit")) return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: 1200 });
    if (url.includes("/poll?")) {
      obs.polls++;
      if (cancelAt == null || clock.t < cancelAt + 5000) return json(200, { status: "running" });
      return json(200, { status: "completed", images: [{ filename: "a.png", data_base64: "AA==" }] });
    }
    if (url.endsWith("/cancel")) {
      obs.cancels++; cancelAt = clock.t;
      return json(200, { ok: false, still_billing: true, id: "job-1", status: "running",
                         error: "任务正在提交中,还拿不到句柄 —— 稍等一两秒再点取消" });
    }
    if (url.endsWith("/fetch_result")) { obs.fetches++; return json(200, { ok: true, outputs: [{ filename: "a.png" }] }); }
    return json(200, { ok: true });
  } });
  const ctx = h.sb.newProgress("submitting", "wf");
  let k = 0;
  h.sb.sleep = async (ms) => {
    h.clock.t += ms;
    if (++k === 2) {
      await ctx.onCancel();
      snapshot = { finished: ctx.finished, cancelable: ctx.cancelable, warn: ctx.els.warn.textContent,
                   label: ctx.els.label.textContent, cancelBtn: ctx.els.cancel.textContent };
    }
  };
  const r = await h.sb.runOnceOnModal({}, ["9"], ctx, null);
  assert.equal(r.cancelled, undefined, "取消没成功,不能当成已取消退出");
  assert.equal(h.observed.fetches, 1, "随后完成的任务必须取回");
  assert.equal(h.observed.alerts.length, 1);
  assert(h.observed.alerts[0].includes("cancel.retry_hint"), "弹窗要告诉用户可以重试");
  assert.equal(snapshot.finished, false, "✕ 不能变成『关闭』");
  assert.equal(snapshot.cancelable, true);
  assert.equal(snapshot.cancelBtn, "✕");
  assert.equal(snapshot.warn, "cancel.failed_retry");
  assert.match(snapshot.label, /Running/);
  assert.equal(h.saved().length, 0);
});

test("F-P2-2 取消失败后再点一次,第二次成功 → 退出并收尾", async () => {
  let cancels = 0;
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: 1200 });
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "running" }); }
    if (url.endsWith("/cancel")) {
      cancels++;
      return cancels === 1
        ? json(502, { ok: false, still_billing: true, error: "upstream timeout" })
        : json(200, { ok: true, still_billing: false, id: "job-1", status: "cancelled", was_running: true });
    }
    return json(200, { ok: true });
  } });
  const ctx = h.sb.newProgress("submitting", "wf");
  let k = 0;
  h.sb.sleep = async (ms) => { h.clock.t += ms; k++; if (k === 2 || k === 4) await ctx.onCancel(); };
  const r = await h.sb.runOnceOnModal({}, ["9"], ctx, null);
  assert.equal(cancels, 2);
  assert.equal(r.cancelled, true);
  assert.match(ctx.els.label.textContent, /✕ Cancelled/);
  assert.equal(ctx.els.warn.style.display, "none");
  assert.equal(h.saved().length, 0);
});

test("F-P2-2 恢复路径同理:取消失败后继续轮询并取回", async () => {
  let cancelled = false;
  const h = makeRunSandbox({ fetch: (url, o, obs, clock) => {
    if (url.includes("/poll?")) {
      obs.polls++;
      return obs.polls < 6 ? json(200, { status: "running" })
        : json(200, { status: "completed", images: [{ filename: "a.png", data_base64: "AA==" }] });
    }
    if (url.endsWith("/cancel")) { obs.cancels++; return json(200, { ok: false, still_billing: true, error: "boom" }); }
    if (url.endsWith("/fetch_result")) { obs.fetches++; return json(200, { ok: true, outputs: [{ filename: "a.png" }] }); }
    return json(200, { ok: true });
  } });
  const job = { jobId: "job-1", startedAt: h.clock.t, wfName: "wf" };
  h.sb.addActiveJob(job);
  // 抓到恢复卡片:newProgress 返回的 ctx
  let rctx = null;
  const realNP = h.sb.newProgress;
  h.sb.newProgress = (...a) => (rctx = realNP(...a));
  let k = 0;
  h.sb.sleep = async (ms) => { h.clock.t += ms; if (++k === 2 && !cancelled) { cancelled = true; await rctx.onCancel(); } };
  await h.sb.recoverOne(job, 1200);
  assert.equal(h.observed.cancels, 1);
  assert.equal(h.observed.fetches, 1, "取消失败后恢复路径也要接着跟到完成");
  assert.match(rctx.els.label.textContent, /Recovered/);
});

// =============================================================================
// C2:/cancel 的 ok / still_billing
// =============================================================================
test("C2 still_billing=true 是取消失败(弹窗、保留记录);ok=true 且 cancelled 是成功", async () => {
  const fail = makeRunSandbox({ fetch: (url) => url.endsWith("/cancel")
    ? json(200, { ok: false, still_billing: true, id: "j", status: "running", error: "cancel failed: x", was_running: true })
    : json(200, { ok: true }) });
  fail.sb.addActiveJob({ jobId: "j", startedAt: 0 });
  assert.equal(await fail.sb.requestCancel("j", null, null), false);
  assert.equal(fail.observed.alerts.length, 1);
  assert.equal(fail.saved().length, 1);

  const ok = makeRunSandbox({ fetch: (url) => url.endsWith("/cancel")
    ? json(200, { ok: true, still_billing: false, id: "j", status: "cancelled", was_running: true })
    : json(200, { ok: true }) });
  ok.sb.addActiveJob({ jobId: "j", startedAt: 0 });
  const ctx = ok.sb.newProgress("running", "wf");
  const d = await ok.sb.requestCancel("j", ctx, null);
  assert.equal(d.status, "cancelled");
  assert.equal(ok.observed.alerts.length, 0);
  assert.equal(ok.saved().length, 0);
  assert.match(ctx.els.label.textContent, /✕ Cancelled/);

  // 新后端:still_billing=false 时即使带了 error 也不是「可能仍在计费」
  const benign = makeRunSandbox({ fetch: (url) => url.endsWith("/cancel")
    ? json(200, { ok: true, still_billing: false, id: "j", status: "cancelled", error: "note" })
    : json(200, { ok: true }) });
  benign.sb.addActiveJob({ jobId: "j", startedAt: 0 });
  await benign.sb.requestCancel("j", null, null);
  assert.equal(benign.observed.alerts.length, 0);
});

// =============================================================================
// C3:/submit 结果未知 → 按 job_id 轮询
// =============================================================================
test("C3 /submit 回 outcome=unknown + job_id:不当失败,按这个 id 轮询并取回", async () => {
  const polled = [];
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(502, { error: "提交结果未知 job_id=job-u", job_id: "job-u", outcome: "unknown" });
    if (url.includes("/poll?")) {
      obs.polls++; polled.push(url);
      return obs.polls < 3 ? json(200, { status: "queued" })
        : json(200, { status: "completed", images: [{ filename: "a.png", data_base64: "AA==" }] });
    }
    if (url.endsWith("/fetch_result")) { obs.fetches++; return json(200, { ok: true, outputs: [{ filename: "a.png" }] }); }
    return json(200, { ok: true });
  } });
  const ctx = h.sb.newProgress("submitting", "wf");
  const labels = captureStages(ctx);
  const r = await h.sb.runOnceOnModal({}, ["9"], ctx, null);
  assert.equal(r.jobId, "job-u");
  assert(polled.every((u) => u.includes("job_id=job-u")));
  assert.equal(h.observed.fetches, 1);
  assert(h.observed.notifies.some((m) => m.startsWith("run.submit_unknown")), "要提示『提交结果不确定,正在核实』");
  assert(labels.some((s) => s.startsWith("run.submit_unknown_stage")));
});

test("C3 结果未知且连续 not_found:报『提交没有落地』并清记录", async () => {
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(502, { error: "x", job_id: "job-u", outcome: "unknown" });
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "not_found", error: "job not found" }); }
    return json(200, { ok: true });
  } });
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], h.sb.newProgress("submitting", "wf"), null),
                       /run\.submit_not_landed/);
  assert.equal(h.observed.polls, NOT_FOUND_STREAK);
  assert.equal(h.saved().length, 0);
});

test("C3 普通的提交失败(没有 outcome=unknown)仍然直接报错,不轮询", async () => {
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(502, { error: "Modal /run 401 — bridge key 不对" });
    if (url.includes("/poll?")) obs.polls++;
    return json(200, { ok: true });
  } });
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], h.sb.newProgress("submitting", "wf"), null), /401/);
  assert.equal(h.observed.polls, 0);
  assert.equal(h.saved().length, 0);
});

// =============================================================================
// C4:/poll 的 auth_failed 终态、unknown 瞬态
// =============================================================================
test("C4 auth_failed 立即结束并提示 key 不匹配,恢复记录保留", async () => {
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: 1200 });
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "auth_failed", error: "Modal /status 401" }); }
    return json(200, { ok: true });
  } });
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], h.sb.newProgress("submitting", "wf"), null), /run\.auth_failed/);
  assert.equal(h.observed.polls, 1, "auth_failed 是终态,不能接着轮询");
  assert.equal(h.saved().length, 1, "修好 key 后还要靠它恢复");
});

test("C4 unknown 按瞬态:不计入也不打断 not_found 连续计数", async () => {
  const nf = json(200, { status: "not_found", error: "job not found" });
  const unk = json(200, { status: "unknown", error: "Modal /status 503", http_status: 503 });
  const seq = [nf, unk, nf, nf, unk, nf, nf];
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: 1200 });
    if (url.includes("/poll?")) { obs.polls++; return seq[Math.min(obs.polls - 1, seq.length - 1)]; }
    return json(200, { ok: true });
  } });
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], h.sb.newProgress("submitting", "wf"), null), /run\.job_gone/);
  assert.equal(h.observed.polls, 7, "unknown 被当成了有效状态,把连续计数清零了");
});

test("C4 恢复路径:auth_failed 结束卡片、保留记录;取消复核遇到 auth_failed 判『没看清』", async () => {
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.includes("/poll?")) { obs.polls++; return json(200, { status: "auth_failed", error: "401" }); }
    return json(200, { ok: true });
  } });
  const job = { jobId: "job-1", startedAt: h.clock.t };
  h.sb.addActiveJob(job);
  await h.sb.recoverOne(job, 1200);
  assert.equal(h.observed.polls, 1);
  assert.equal(h.saved().length, 1);
  const v = await h.sb.confirmJobGone("job-1");
  assert.equal(v.verdict, "unknown");
});

// =============================================================================
// F-P3-10:取回失败文案 / 配对冷却 / 多标签页归属
// =============================================================================
test("F-P3-10 主流程取回失败:文案带『刷新页面可恢复』,原因也在", async () => {
  const h = makeRunSandbox({ fetch: (url, o, obs) => {
    if (url.endsWith("/submit")) return json(200, { ok: true, job_id: "job-1", gpu: "H100", worker_timeout_sec: 1200 });
    if (url.includes("/poll?")) return json(200, { status: "completed", images: [{ filename: "a.png", data_base64: "AA==" }] });
    if (url.endsWith("/fetch_result")) return json(502, { error: "write result failed: disk full" });
    return json(200, { ok: true });
  } });
  await assert.rejects(h.sb.runOnceOnModal({}, ["9"], h.sb.newProgress("submitting", "wf"), null),
    (e) => e.message.startsWith("run.fetch_failed") && e.message.includes("disk full"));
  assert.equal(h.saved().length, 1, "记录保留,刷新才真能恢复");
});

test("F-P3-10 恢复记录带 tabId / 心跳;别的标签页心跳新鲜的不接手,过期的和老记录接手", async () => {
  const now = 1_000_000_000;
  const h = makeRunSandbox({ fetch: () => json(200, {}) });
  h.clock.t = now;
  h.sb.addActiveJob({ jobId: "mine", startedAt: now });
  const mine = h.saved()[0];
  assert(mine.tabId && mine.hb === now, JSON.stringify(mine));
  h.setSaved([
    { jobId: "busy", startedAt: now, tabId: "other-tab", hb: now - 3000 },        // 另一标签页正在跟
    { jobId: "orphan", startedAt: now, tabId: "dead-tab", hb: now - 60_000 },     // 那个标签页早关了
    { jobId: "legacy", startedAt: now },                                           // 升级前的老记录
    { jobId: "released", startedAt: now, tabId: "refreshed-tab", hb: 0 },         // pagehide 清零
  ]);
  const recovered = [];
  h.sb.recoverOne = (j) => recovered.push(j.jobId);
  await h.sb.recoverPendingJob();
  assert.deepEqual(recovered.sort(), ["legacy", "orphan", "released"]);
  const after = Object.fromEntries(h.saved().map((j) => [j.jobId, j]));
  assert.equal(Object.keys(after).length, 4, "别的标签页的记录不能删");
  assert.equal(after.busy.tabId, "other-tab");
  assert.equal(after.orphan.tabId, mine.tabId, "接手后归到本标签页");
  // 心跳只续本标签页的;release 清零
  h.clock.t = now + 5000;
  h.sb.heartbeatActiveJobs();
  const hb = Object.fromEntries(h.saved().map((j) => [j.jobId, j.hb]));
  assert.equal(hb.orphan, now + 5000);
  assert.equal(hb.busy, now - 3000);
  h.sb.heartbeatActiveJobs(true);
  assert.equal(h.saved().find((j) => j.jobId === "orphan").hb, 0);
});

// 配对:只实现 askCapability 用到的 DOM;overlay 一挂上就「点取消」
function pairingSandbox() {
  const clock = { t: 5_000_000 };
  const seen = { prompts: 0, calls: [] };
  let saved = "";
  const mk = (tag) => ({
    tag, style: {}, children: [], on: {}, value: "", textContent: "", removed: false,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.on[ev] ||= []).push(fn); },
    remove() { this.removed = true; }, focus() {},
  });
  const find = (el, pred) => (pred(el) ? el : el.children.map((c) => find(c, pred)).find(Boolean));
  const sandbox = {
    Headers, Date: { now: () => clock.t }, t: (k) => k,
    localStorage: { getItem: () => saved, setItem: (_k, v) => { saved = v; }, removeItem: () => { saved = ""; } },
    window: { document: { createElement: mk, body: { appendChild(overlay) {
      seen.prompts++;
      queueMicrotask(() => {
        const cancel = find(overlay, (e) => e.tag === "button" && e.textContent === "auth.cancel");
        for (const fn of cancel.on.click || []) fn({ preventDefault() {} });
      });
    } } } },
    api: { fetchApi: async (url) => {
      seen.calls.push(url);
      return { status: 403, headers: new Headers({ "X-Modal-Bridge-Auth": "capability-required" }) };
    } },
  };
  vm.createContext(sandbox);
  vm.runInContext(between("const LOCAL_CAP_KEY =", "// 上报 job"), sandbox);
  return { sb: sandbox, seen, clock };
}

test("F-P3-10 取消配对后 60 秒内,后台轮询不再弹配对框;用户主动操作照常弹;过了冷却期恢复", async () => {
  const p = pairingSandbox();
  assert.equal((await p.sb.bridgeFetch("/deploy", { method: "POST" })).status, 403);
  assert.equal(p.seen.prompts, 1);
  for (let i = 0; i < 5; i++) {
    p.clock.t += 1200;
    assert.equal((await p.sb.bridgeFetch("/modal_bridge/poll?job_id=x", { background: true })).status, 403);
  }
  assert.equal(p.seen.prompts, 1, "轮询每一拍都在重新弹配对框");
  await p.sb.bridgeFetch("/version");
  assert.equal(p.seen.prompts, 2, "用户主动点的操作仍要能配对");
  p.clock.t += 61_000;
  await p.sb.bridgeFetch("/modal_bridge/poll?job_id=x", { background: true });
  assert.equal(p.seen.prompts, 3, "冷却期过了应当再问");
});

test("F-P3-10 background 标记不传给 fetchApi", async () => {
  let got = null;
  const sb = { Headers, Date, t: (k) => k, localStorage: { getItem: () => "", setItem() {}, removeItem() {} },
               window: {}, api: { fetchApi: async (u, o) => { got = o; return { status: 200, headers: new Headers() }; } } };
  vm.createContext(sb);
  vm.runInContext(between("const LOCAL_CAP_KEY =", "// 上报 job"), sb);
  await sb.bridgeFetch("/x", { method: "POST", body: "{}", background: true });
  assert.equal("background" in got, false);
  assert.equal(got.body, "{}");
});

// =============================================================================
// 节点同步 / 模型同步 / streamPost(真实 i18n)
// =============================================================================
function streamRes(text) {
  const bytes = new TextEncoder().encode(text);
  let done = false;
  return { ok: true, status: 200, body: { getReader: () => ({
    read: async () => (done ? { done: true } : (done = true, { done: false, value: bytes })) }) } };
}
function nodeSandbox(fetchImpl, { confirm = () => true } = {}) {
  const obs = { confirms: [], notifies: [], bodies: {} };
  const sb = {
    app: { ui: { settings: { getSettingValue: () => "zh" } } }, navigator: { language: "zh" },
    TextDecoder, JSON, String, Object, Array, Promise, Error, parseInt, Math,
    log: () => {}, err: () => {},
    notify: (m, sev) => obs.notifies.push([m, sev]),
    confirm: (m) => { obs.confirms.push(m); return confirm(m); },
    STATUS_PROGRESS: { deploying: [5, 35], uploading: [40, 78] },
    bridgeFetch: async (url, o) => { if (o && o.body) obs.bodies[url] = JSON.parse(o.body); return fetchImpl(url, o); },
  };
  vm.createContext(sb);
  vm.runInContext(between("function _locale()", "const sleep ="), sb);   // 真实 t() + I18N(zh)
  vm.runInContext(chunk("async function httpError("), sb);               // streamPost / ensureNodesAvailable …
  vm.runInContext(chunk("async function ensureModelsAvailable("), sb);
  return { sb, obs };
}
const nctx = () => ({ stages: [], stage(k, d) { this.stages.push(String(d)); }, bar() {} });

test("F-P2-4 streamPost 在流开始前被拒(409 JSON):把后端的 error 原样带进异常", async () => {
  const msg = "云端镜像装着 foo,但本机清单里没有,也拿不到它们的来源 —— 已中止。\n处理:在本机装上这些节点后再部署";
  const { sb } = nodeSandbox(async () => ({ ok: false, status: 409, body: {}, json: async () => ({ error: msg }) }));
  await assert.rejects(sb.streamPost("/modal_bridge/sync_nodes", {}, () => {}), (e) => e.message === msg && e.httpStatus === 409);
  const { sb: sb2 } = nodeSandbox(async () => ({ ok: false, status: 500, json: async () => { throw new Error("not json"); } }));
  await assert.rejects(sb2.streamPost("/x", {}, () => {}), /\/x HTTP 500/);
  const { sb: sb3 } = nodeSandbox(async () => ({ ok: false, status: 400, json: async () => ({ error: "prompt required" }) }));
  await assert.rejects(sb3.checkNodesOnModal({}), /prompt required/);
});

test("F-P3-1 / C5 私有节点同步遇到 DeployBlocked(一行『部署已中止』):专门文案,不落到 requirements", async () => {
  const body = "== 打包上传 1 个本地节点到 Volume ==\n  ✓ mynode (2 KB, 3 files)\n" +
               "== 私有节点依赖已变化(2 条),自动重新部署 ==\n" +
               "== ✗ 部署已中止:读不到云端装了哪些自定义节点(timeout),而本机节点清单是空的 —— 继续部署可能清空云端全部自定义节点 处理:检查网络 / bridge key 后重试。 ==\n" +
               "\n__DEPLOY_DONE__ rc=1\n";
  const { sb } = nodeSandbox(async (url) => {
    if (url.endsWith("/check_nodes")) return json(200, { local_pack: [{ folder: "mynode" }], ok_baked: 0, ok_builtin: 0 });
    if (url.endsWith("/local_nodes_diff")) return json(200, { upload: ["mynode"], reqs_redeploy_pending: false });
    if (url.endsWith("/sync_local_nodes")) return streamRes(body);
    throw new Error(url);
  });
  await assert.rejects(sb.ensureNodesAvailable({}, nctx()), (e) => {
    assert(e.message.includes("不是依赖装不上"), e.message);
    assert(e.message.includes("处理:检查网络"), "原因那一行要带上");
    assert(!e.message.includes("requirements"), e.message);
    return true;
  });
});

test("F-P3-1 / C5 RunModal 自动同步节点遇到保护性中止:同样用专门文案", async () => {
  const body = "== 同步 custom_nodes ==\n== ✗ 部署已中止:云端有 foo,本机清单没有且补不出来源 ==\n__DEPLOY_DONE__ rc=1\n";
  const { sb } = nodeSandbox(async (url) => {
    if (url.endsWith("/check_nodes")) return json(200, { needs_deploy: true, add: [{ folder: "a", class_types: ["A"] }],
      update: [], prune: [], new_baked: [{ name: "a", url: "u", commit: "c" }], source: "modal", ok_baked: 1, ok_builtin: 0 });
    if (url.endsWith("/sync_nodes")) return streamRes(body);
    throw new Error(url);
  });
  await assert.rejects(sb.ensureNodesAvailable({}, nctx()), (e) => e.message.includes("不是依赖装不上") && e.message.includes("补不出来源"));
});

test("C6 /check_nodes 带 cloud_unchecked 且要部署:不自动同步、中止提交,也不先推私有节点", async () => {
  const hits = [];
  const { sb } = nodeSandbox(async (url) => {
    hits.push(url);
    if (url.endsWith("/check_nodes")) return json(200, { needs_deploy: true, cloud_unchecked: "timeout",
      add: [{ folder: "a", class_types: ["A"] }], update: [], prune: [], local_pack: [{ folder: "mine" }],
      new_baked: [], source: "local", ok_baked: 0, ok_builtin: 0 });
    throw new Error("不该再请求 " + url);
  });
  await assert.rejects(sb.ensureNodesAvailable({}, nctx()), (e) => e.message.includes("读不到云端节点清单(timeout)"));
  assert.deepEqual(hits, ["/modal_bridge/check_nodes"]);
});

test("C6 cloud_unchecked 但不需要部署:照常放行;volume_unchecked 只提示不拦", async () => {
  const { sb, obs } = nodeSandbox(async (url) => {
    if (url.endsWith("/check_nodes")) return json(200, { needs_deploy: false, cloud_unchecked: "timeout",
      volume_unchecked: "Volume 超时", add: [], update: [], prune: [], local_remove: [], expect_baked: ["x"],
      source: "local", ok_baked: 1, ok_builtin: 0 });
    throw new Error(url);
  });
  const ctx = nctx();
  assert.equal(await sb.ensureNodesAvailable({}, ctx), true);
  assert(obs.notifies.some(([m, s]) => m.includes("Volume 超时") && s === "warn"), JSON.stringify(obs.notifies));
});

test("C6 RunModal 自动同步路径从不传 prune", async () => {
  const { sb, obs } = nodeSandbox(async (url) => {
    if (url.endsWith("/check_nodes")) return json(200, { needs_deploy: true, add: [{ folder: "a", class_types: ["A"] }],
      update: [], prune: [{ folder: "old" }], new_baked: [{ name: "a", url: "u", commit: "c" }], source: "modal",
      local_remove: [], expect_baked: [], ok_baked: 1, ok_builtin: 0 });
    if (url.endsWith("/sync_nodes")) return streamRes("ok\n__DEPLOY_DONE__ rc=0\n");
    throw new Error(url);
  });
  assert.equal(await sb.ensureNodesAvailable({}, nctx()), true);
  const body = obs.bodies["/modal_bridge/sync_nodes"];
  assert(body && Array.isArray(body.new_baked));
  assert.equal("prune" in body, false);
});

test("F-P3-9 /sync_models:rc≠0 时把被拒 / 失败的项列出来;rc=0 但有被拒项也提示,并显示后端汇总", async () => {
  const items = { required: [1, 2, 3], present: [1], missing_local: [
    { type: "checkpoints", filename: "a.safetensors", local_path: "/x/a", size_mb: 10 },
    { type: "loras", filename: "b.safetensors", local_path: "/x/b", size_mb: 1 }] };
  const fail = nodeSandbox(async (url) => {
    if (url.endsWith("/check_models")) return json(200, items);
    return streamRes("  ✗ 本地找不到(或不在模型目录内),跳过:loras/b.safetensors\n== 上传 1 个模型 ==\n" +
                     "== 汇总:已同步 1 · 已存在跳过 0 · 被拒 1 ==\n__DEPLOY_DONE__ rc=1\n");
  });
  await assert.rejects(fail.sb.ensureModelsAvailable({}, nctx()),
    (e) => e.message.includes("loras/b.safetensors") && !e.message.includes("已同步 ✓"));

  const partial = nodeSandbox(async (url) => {
    if (url.endsWith("/check_models")) return json(200, items);
    return streamRes("  ✗ 被拒:loras/b.safetensors\n== 汇总:已同步 1 · 已存在跳过 0 · 被拒 1 ==\n__DEPLOY_DONE__ rc=0\n");
  });
  const ctx = nctx();
  await partial.sb.ensureModelsAvailable({}, ctx);
  assert(partial.obs.notifies.some(([m]) => m.includes("loras/b.safetensors")), "被拒的项要显示出来");
  assert.equal(ctx.stages.at(-1), "汇总:已同步 1 · 已存在跳过 0 · 被拒 1");
  assert(!ctx.stages.some((s) => s.includes("2 个模型已同步")), "不能一律报『N 个模型已同步 ✓』");
});

// =============================================================================
// 设置项防抖(C10)
// =============================================================================
test("F-P2-5 / C10 AIGC URL 连续输入只在停止 800ms 后写一次,写的是最后的值;启动回填不写", async () => {
  const timers = new Map();
  let seq = 0;
  const posts = [];
  const sb = {
    JSON, String, Object, Promise, Error,
    setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => timers.delete(id),
    t: (k) => k, log: () => {}, err: () => {}, notify: () => {},
    httpError: async (r) => new Error("HTTP " + r.status),
    bridgeFetch: async (url, o) => { posts.push(JSON.parse(o.body)); return json(200, {}); },
  };
  vm.createContext(sb);
  vm.runInContext(chunk("let _snapReady = false;"), sb);
  sb.syncAigcFieldToConfig("aigc_studio_base_url", "h");   // _advReady=false:启动回填
  assert.equal(timers.size, 0);
  vm.runInContext("_advReady = true;", sb);
  for (const v of ["h", "ht", "htt", "https://a.b"]) sb.syncAigcFieldToConfig("aigc_studio_base_url", v);
  assert.equal(timers.size, 1, "每键一个定时器没有被清掉");
  const [only] = [...timers.values()];
  assert.equal(only.ms, 800);
  only.fn();
  await ticks(3);
  assert.deepEqual(posts, [{ aigc_studio_base_url: "https://a.b" }]);
});

// =============================================================================
// /version(C7)、版本徽标转义(F-P3-2)、部署面板(F-P3-4/5/6、C5、C6)
// =============================================================================
function dialogSandbox(routes, extra = {}) {
  const created = [];
  const obs = { notifies: [], confirms: [], streams: [] };
  const sb = {
    JSON, String, Object, Array, Promise, Error, parseInt, Math,
    document: { createElement: (tag) => { const e = domEl(tag); created.push(e); return e; },
                body: domEl("body") },
    _locale: () => "zh",
    t: (key, vars) => (vars ? key + " " + JSON.stringify(vars) : key),
    log: () => {}, err: () => {},
    notify: (m, s) => obs.notifies.push([m, s]),
    confirm: (m) => { obs.confirms.push(m); return false; },
    setRunReady: () => {}, doHealthCheck: () => {},
    isModalOutage: async () => false,
    bridgeFetch: async (url, o) => {
      const r = routes[url.split("?")[0]];
      if (!r) throw new Error("unexpected " + url);
      return typeof r === "function" ? r(o) : r;
    },
    streamPost: async (p, body, onLine) => {
      obs.streams.push({ path: p, body });
      const r = routes["stream:" + p];
      return r ? r(onLine, body) : 0;
    },
    window: { open() {} },
    ...extra,
  };
  vm.createContext(sb);
  vm.runInContext(between("function escHtml(", "const LOCAL_NODE_BAKED_SENTINEL"), sb);
  vm.runInContext(between("function failLine(", "async function checkNodesOnModal("), sb);
  vm.runInContext(chunk("async function fetchConfig("), sb);
  return { sb, obs, created };
}

test("F-P2-6 / C7 /version 超时 / 连不上 / 其它 HTTP 错误:放行提交,只给非阻断提示", async () => {
  for (const kind of ["timeout", "unreachable", "http_error", undefined]) {
    const { sb, obs } = dialogSandbox({ "/modal_bridge/version": json(200, { reachable: false, match: false, err_kind: kind }) });
    assert.equal(await sb.checkVersionOrBlock(), true, `err_kind=${kind} 不该拦`);
    await ticks(3);
    assert.equal(obs.confirms.length, 0, "不能弹阻断式确认框");
    assert(obs.notifies.some(([m]) => m.startsWith("ver.unreach_toast")), JSON.stringify(obs.notifies));
  }
  const outage = dialogSandbox({ "/modal_bridge/version": json(200, { reachable: false, err_kind: "timeout" }) },
                               { isModalOutage: async () => true });
  assert.equal(await outage.sb.checkVersionOrBlock(), true);
  await ticks(3);
  assert(outage.obs.notifies.some(([m]) => m === "ver.platform_toast"));
});

test("F-P3-5 / C7 /version 401(unauthorized)拦下并用『key 不一致』文案,不再说 app 不存在;404 仍拦", async () => {
  const u = dialogSandbox({ "/modal_bridge/version": json(200, { reachable: false, err_kind: "unauthorized" }) });
  assert.equal(await u.sb.checkVersionOrBlock(), false);
  assert(u.obs.notifies.some(([m]) => m === "ver.unauthorized_toast"));
  assert.deepEqual(u.obs.confirms, ["ver.unauthorized_msg"]);
  assert(!u.obs.notifies.some(([m]) => m.startsWith("ver.notdeployed")));
  const n = dialogSandbox({ "/modal_bridge/version": json(200, { reachable: false, err_kind: "not_deployed" }) });
  assert.equal(await n.sb.checkVersionOrBlock(), false);
  assert.deepEqual(n.obs.confirms, ["ver.notdeployed_msg"]);
  const b = dialogSandbox({ "/modal_bridge/version": json(200, { reachable: false, err_kind: "local_busy" }) });
  assert.equal(await b.sb.checkVersionOrBlock(), true);
});

test("F-P3-2 版本徽标:云端返回的 deployed_version 转义后才进 innerHTML", async () => {
  const { sb } = dialogSandbox({ "/modal_bridge/version": json(200, {
    reachable: true, match: false, local: "0.8.58", deployed: "<img src=x onerror=alert(1)>" }) });
  const panel = domEl("div");
  await sb.refreshVerBanner(panel);
  const html = panel.querySelector("#mb-dep-ver").innerHTML;
  assert(!html.includes("<img"), html);
  assert(html.includes("&lt;img src=x onerror=alert(1)&gt;"), html);
  // 401 的提示不是「未部署」
  const { sb: sb2 } = dialogSandbox({ "/modal_bridge/version": json(200, { reachable: false, err_kind: "unauthorized", local: "1" }) });
  const p2 = domEl("div");
  await sb2.refreshVerBanner(p2);
  assert(p2.querySelector("#mb-dep-ver").innerHTML.includes("dlg.ver.unauthorized"));
});

async function openDialog(routes) {
  const d = dialogSandbox(routes);
  await d.sb.openDeployDialog();
  const overlay = d.created[0];
  const panel = d.created[1];
  await ticks(3);
  return { ...d, overlay, panel, q: (s) => panel.querySelector(s) };
}

test("F-P3-4 重开部署面板时重新拉 /config:has_token_secret / AIGC 字段跟着变", async () => {
  let cfg = { modal_workspace: "ws", modal_token_id: "ak-1", has_token_secret: false, aigc_studio_base_url: "" };
  const d = await openDialog({
    "/modal_bridge/config": () => json(200, cfg),
    "/modal_bridge/version": json(200, { reachable: true, match: true, local: "1", deployed: "1" }),
  });
  d.q("#mb-dep-ws").value = "ws";
  d.q("#mb-dep-id").value = "ak-1";
  d.q("#mb-dep-close").onclick();
  cfg = { modal_workspace: "ws", modal_token_id: "ak-1", has_token_secret: true, has_comfy_api_key: true,
          aigc_studio_base_url: "https://aigc.example", has_aigc_bypass_secret: true };
  await d.sb.openDeployDialog();
  await ticks(5);
  assert.equal(d.created.length >= 2 && d.created[0], d.overlay, "不重建 DOM(部署日志可能在里面)");
  assert.equal(d.q("#mb-dep-secret").placeholder, "dlg.secret.ph_saved");
  assert(d.q("#mb-dep-secret-hint").textContent.includes("dlg.secret.saved"));
  assert.equal(d.q("#mb-dep-comfy").placeholder, "dlg.comfy.ph_saved");
  assert.equal(d.q("#mb-dep-aigc-wrap").style.display, "block", "设置了 AIGC URL 后要出现旁路密钥框");
});

test("F-P3-4 部署成功后清空三个密码框并刷新 cfg;C5 保护性中止用专门文案", async () => {
  let cfg = { modal_workspace: "ws", modal_token_id: "ak-1", has_token_secret: false, aigc_studio_base_url: "https://a" };
  let rc = 0;
  const d = await openDialog({
    "/modal_bridge/config": () => json(200, cfg),
    "/modal_bridge/version": json(200, { reachable: true, match: true, local: "1", deployed: "1" }),
    "stream:/modal_bridge/deploy": (onLine) => {
      if (rc !== 0) onLine("== ✗ 部署已中止:读不到云端节点清单(timeout) ==");
      cfg = { ...cfg, has_token_secret: true };
      return rc;
    },
  });
  d.q("#mb-dep-ws").value = "ws";
  d.q("#mb-dep-id").value = "ak-1";
  d.q("#mb-dep-secret").value = "as-secret";
  d.q("#mb-dep-comfy").value = "comfy-key";
  d.q("#mb-dep-aigc-bypass").value = "bypass";
  d.q("#mb-dep-aigc-wrap").style.display = "block";
  await d.q("#mb-dep-go").onclick();
  await ticks(5);
  const sent = d.obs.streams.find((s) => s.path === "/modal_bridge/deploy").body;
  assert.equal(sent.token_secret, "as-secret");
  assert.equal(sent.aigc_bypass_secret, "bypass");
  for (const sel of ["#mb-dep-secret", "#mb-dep-comfy", "#mb-dep-aigc-bypass"]) {
    assert.equal(d.q(sel).value, "", `${sel} 没清空`);
  }
  assert.equal(d.q("#mb-dep-secret").placeholder, "dlg.secret.ph_saved", "部署后 cfg 没刷新");

  rc = 1;
  d.q("#mb-dep-secret").value = "as-again";
  await d.q("#mb-dep-go").onclick();
  assert.equal(d.q("#mb-dep-status").textContent, "dep.aborted", "保护性中止落到了构建失败文案");
  assert.equal(d.q("#mb-dep-secret").value, "as-again", "失败时不清空,方便重试");
});

test("F-P3-5 测试连接遇到 401:说 key 不一致,不说 app 没部署", async () => {
  const d = await openDialog({
    "/modal_bridge/config": json(200, { modal_workspace: "ws", modal_token_id: "ak-1" }),
    "/modal_bridge/version": json(200, { reachable: false, err_kind: "unauthorized", local: "1" }),
    "/modal_bridge/health": json(502, { ok: false, error: "Modal /health 401 — bridge key 不对/缺失。点 [Modal Setup] 重新部署会刷新 key" }),
  });
  await d.q("#mb-dep-test").onclick();
  assert(d.q("#mb-dep-status").textContent.startsWith("test.unauthorized"), d.q("#mb-dep-status").textContent);
});

test("F-P3-6 / C6 移除节点:本地包删除与重部署同时失败两个都报;勾选的名字进 prune", async () => {
  const d = await openDialog({
    "/modal_bridge/config": json(200, {}),
    "/modal_bridge/version": json(200, { reachable: true, match: true, local: "1", deployed: "1" }),
    "/modal_bridge/list_nodes": json(200, { ok: true, source: "modal",
      nodes: [{ name: "a", url: "ua", commit: "ca" }, { name: "b", url: "ub", commit: "cb" }] }),
    "/modal_bridge/list_local_nodes": json(200, { ok: true, nodes: ["loc"] }),
    "/modal_bridge/remove_local_node": json(502, { ok: false, error: "Volume 写失败" }),
    "stream:/modal_bridge/sync_nodes": (onLine) => { onLine("✗ modal deploy failed"); return 1; },
  });
  d.sb.confirm = () => true;
  await d.q("#mb-nodes-load").onclick();
  d.panel._all[".mb-node-cb:checked"] = [{ dataset: { i: "0" } }, { dataset: { i: "2" } }];   // a(镜像)+ loc(本地包)
  await d.q("#mb-nodes-prune").onclick();
  const st = d.q("#mb-nodes-status").textContent;
  assert(st.includes("mn.local_rm_fail"), st);
  assert(st.includes("mn.redeploy_fail"), "重部署失败被吞掉了:" + st);
  const body = JSON.parse(JSON.stringify(d.obs.streams.find((s) => s.path === "/modal_bridge/sync_nodes").body));
  assert.deepEqual(body.prune, ["a"]);
  assert.deepEqual(body.new_baked.map((n) => n.name), ["b"]);
});

test("F-P2-4 / F-P3-6 移除节点时 /sync_nodes 在流开始前 409:后端说明显示出来", async () => {
  const d = await openDialog({
    "/modal_bridge/config": json(200, {}),
    "/modal_bridge/version": json(200, { reachable: true, match: true, local: "1", deployed: "1" }),
    "/modal_bridge/list_nodes": json(200, { ok: true, source: "modal", nodes: [{ name: "a", url: "ua", commit: "ca" }] }),
    "/modal_bridge/list_local_nodes": json(200, { ok: false, nodes: [], error: "Volume 读不到" }),
    "stream:/modal_bridge/sync_nodes": () => { const e = new Error("读不到云端节点清单,拒绝删除"); throw e; },
  });
  d.sb.confirm = () => true;
  await d.q("#mb-nodes-load").onclick();
  assert(d.q("#mb-nodes-status").textContent.includes("mn.local_list_fail"), "Volume 读不到不能静默显示成空");
  d.panel._all[".mb-node-cb:checked"] = [{ dataset: { i: "0" } }];
  await d.q("#mb-nodes-prune").onclick();
  assert(d.q("#mb-nodes-status").textContent.includes("拒绝删除"), d.q("#mb-nodes-status").textContent);
});

// =============================================================================
// 弹窗转义、导出脚本删除、文案
// =============================================================================
test("confirmDialog 的标题 / 正文 / 按钮全部转义(正文里有来自工作流的 class_type)", () => {
  const created = [];
  const sb = { document: { createElement: (t) => { const e = domEl(t); created.push(e); return e; }, body: domEl("body") },
               Object, Promise, String };
  vm.createContext(sb);
  vm.runInContext(between("function escHtml(", "const LOCAL_NODE_BAKED_SENTINEL"), sb);
  vm.runInContext(between("function confirmDialog(", "async function queueOnModal("), sb);
  sb.confirmDialog("<b>t</b>", "节点 <img src=x onerror=alert(1)>", "<i>ok</i>", "no");
  const html = created[1].innerHTML;
  assert(!html.includes("<img"), html);
  assert(html.includes("&lt;img") && html.includes("&lt;b&gt;t&lt;/b&gt;") && html.includes("&lt;i&gt;ok"));
});

test("F-P3-3 / C9 导出脚本删干净:函数、tooltip、i18n、/bridge_key 调用都不在了", () => {
  for (const s of ["exportModalApi", "EXPORT_TOOLTIP", "downloadText", "/modal_bridge/bridge_key",
                   '"export.', "requests.post(", "Export API"]) {
    assert(!source.includes(s), `残留:${s}`);
  }
});

function i18n() {
  const sb = { app: { ui: { settings: { getSettingValue: () => "zh" } } }, navigator: { language: "zh" } };
  vm.createContext(sb);
  vm.runInContext(between("function _locale()", "const sleep =") + "\nthis.I18N = I18N;", sb);
  return sb.I18N;
}

test("F-P3-7 set.timeout 的 tooltip 按新语义写:保底、排队不计时,不再说『别设得比 worker 上限小』", () => {
  const T = i18n()["set.timeout"];
  assert(!T.zh.includes("别设得比 worker 上限小") && !T.en.includes("Do not set it below"), T.zh);
  assert(T.zh.includes("排队") && T.zh.includes("保底"), T.zh);
  assert(T.en.includes("queue") && T.en.includes("Minimum"), T.en);
});

test("F-P3-8 dep.fail 如实描述:不再说『云端保持原样』,提到 Secret / 私有节点可能已更新", () => {
  const T = i18n()["dep.fail"];
  assert(!T.zh.includes("云端保持原样") && !T.en.includes("the cloud is unchanged"), T.zh);
  assert(T.zh.includes("Secret") && T.zh.includes("私有节点"), T.zh);
  assert(T.en.includes("Secret") && T.en.includes("private node"), T.en);
});
