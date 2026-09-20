// 执行真实前端函数，网络/画布/存储隔离；不依赖浏览器或第三方测试包。
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../web/modal_bridge.js"), "utf8");

// 从源码里现读,别在测试里写死 —— 写死的话源码改了阈值测试还绿。
const NOT_FOUND_STREAK = Number(
  /const NOT_FOUND_STREAK = (\d+);/.exec(source)[1]);

function setup({ fetchFails = false, status = "completed", cancelError = false,
                 cancelGone = false } = {}) {
  let saved = [];
  const observed = { stages: [], timers: [], cleared: [], fetches: 0, polls: 0, alerts: 0 };
  const context = () => ({
    wfName: "test", stage: (...args) => observed.stages.push(args),
    finish: () => {}, setCancel: () => {},
  });
  const sandbox = {
    Date, Headers, LS_KEYS: { activeJob: "jobs" },
    loadLS: () => JSON.parse(JSON.stringify(saved)),
    saveLS: (_key, value) => { saved = JSON.parse(JSON.stringify(value)); },
    getSetting: (_key, value) => value, getVramTier: () => "80g",
    sleep: async () => {}, log: () => {}, err: () => {}, notify: () => {},
    alert: () => { observed.alerts++; },
    reportJobEvent: () => {}, confirm: () => true, t: (key) => key,
    NOT_FOUND_STREAK,
    fmtRate: String, fmtDur: String, newProgress: context,
    MODEL3D_EXT_RE: /\.glb$/, displayInGraph: () => true,
    setInterval: (fn) => { observed.timers.push(fn); return observed.timers.length; },
    clearInterval: (id) => observed.cleared.push(id),
    bridgeFetch: async (url) => {
      if (url.endsWith("/submit")) return {ok: true, json: async () => ({ok: true, job_id: "job", gpu: "test"})};
      if (url.includes("/poll?")) {
        observed.polls++;
        return {ok: true, json: async () => ({status, images: []})};
      }
      if (url.endsWith("/cancel")) {
        if (cancelGone) return {ok: true, json: async () =>
          ({id: "job", status: "not_found", error: "job not found"})};
        return {ok: !cancelError, json: async () => cancelError
          ? {error: "injected failure"} : {cancel_noop: true, status: "completed", images: []}};
      }
      if (url.endsWith("/fetch_result")) {
        observed.fetches++;
        observed.savedAtFetch = saved.some((j) => j.jobId === "job");
        if (fetchFails) throw new Error("injected fetch failure");
        return {ok: true, json: async () => ({ok: true, outputs: [{filename: "test.png"}]})};
      }
      throw new Error("Unexpected URL: " + url);
    },
  };
  vm.createContext(sandbox);
  for (const name of ["function addActiveJob(", "async function recoverPendingJob("]) {
    const start = source.indexOf(name);
    const end = source.indexOf("\n// =====================================================================", start);
    assert(start >= 0 && end > start);
    vm.runInContext(source.slice(start, end), sandbox);
  }
  return { sandbox, observed, context, saved: () => saved };
}

test("取回失败时，主流程保留恢复记录和取回开始时间", async () => {
  const t = setup({fetchFails: true});
  await assert.rejects(t.sandbox.runOnceOnModal({}, [], t.context(), null), /injected fetch failure/);
  assert.equal(t.observed.savedAtFetch, true);
  assert.equal(t.saved().length, 1);
  assert(t.saved()[0].fetchStartedAt > 0);
});

test("成功取回后才清除恢复记录", async () => {
  const t = setup();
  await t.sandbox.runOnceOnModal({}, [], t.context(), null);
  assert.equal(t.observed.savedAtFetch, true);
  assert.equal(t.saved().length, 0);
});

test("worker 失败时清记录且不取回", async () => {
  const t = setup({status: "failed"});
  await assert.rejects(t.sandbox.runOnceOnModal({}, [], t.context(), null), /Modal worker failed/);
  assert.equal(t.saved().length, 0);
  assert.equal(t.observed.fetches, 0);
});

test("刷新恢复取回失败仍保留记录", async () => {
  const t = setup({fetchFails: true});
  const job = {jobId: "job", startedAt: Date.now()};
  t.sandbox.addActiveJob(job);
  await t.sandbox.recoverOne(job, 1200);
  assert.equal(t.saved().length, 1);
  assert.equal(t.observed.fetches, 1);
});

test("cancel_noop 取回失败不能丢记录", async () => {
  const t = setup({fetchFails: true});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  assert.equal(await t.sandbox.requestCancel("job", t.context()), false);
  assert.equal(t.saved().length, 1);
});

test("取消失败不能丢记录", async () => {
  const t = setup({cancelError: true});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  assert.equal(await t.sandbox.requestCancel("job", t.context()), false);
  assert.equal(t.saved().length, 1);
});

test("取回窗口从首次取回计算，重试不续期", () => {
  const t = setup();
  const now = Date.now();
  const job = {jobId: "job", startedAt: now - 7200000, fetchStartedAt: now - 10000};
  t.sandbox.addActiveJob(job);
  t.sandbox.markJobFetching("job");
  assert.equal(t.saved()[0].fetchStartedAt, job.fetchStartedAt);
  assert.equal(t.sandbox.recoveryDeadline(job, 1200), job.fetchStartedAt + 3600000);
});

test("Volume 取回失败会结束进度轮询，仍保留恢复记录", async () => {
  const t = setup({fetchFails: true});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await assert.rejects(t.sandbox.fetchJobResult("job", {images: [{volume_path: "_outputs/job/a"}]}, t.context()));
  assert.equal(t.observed.timers.length, 1);
  assert.deepEqual(t.observed.cleared, [1]);
  assert.equal(t.saved().length, 1);
});

// ── 云端查无此 job(已过 JOB_TTL_S 被 GC / id 不对)────────────────────────────
// 云端 0.8.42 起显式回 {status:"not_found", error:"job not found"}。前端此前不认这个状态:
// 两条轮询都只认 completed/failed/cancelled,于是一直转到超时,然后去"取消"一个不存在的
// 任务,而 cancel 的响应带 error 字段 → 掉进"取消失败"分支,弹出"云端可能仍在运行并继续
// 计费,请到 Modal 控制台确认"。任务根本不存在,却让用户去控制台找。
// (2026-09-20 codex review 抓到;修好契约反而让旧调用方更难发现问题,我们自己就是那个旧调用方。)

test("主轮询:连续 not_found 达阈值就结束,不拖到超时", async () => {
  const t = setup({status: "not_found"});
  await assert.rejects(t.sandbox.runOnceOnModal({}, [], t.context(), null), /job_gone/);
  assert.equal(t.observed.polls, NOT_FOUND_STREAK,
    `应当只轮询 ${NOT_FOUND_STREAK} 次,实际 ${t.observed.polls}`);
  assert.equal(t.observed.fetches, 0, "不存在的 job 不该去取回");
});

test("主轮询:一次 not_found 不判死(modal.Dict 跨容器最终一致)", async () => {
  const t = setup();
  let n = 0;
  const inner = t.sandbox.bridgeFetch;
  t.sandbox.bridgeFetch = async (url) => {
    if (url.includes("/poll?") && ++n === 1) {
      return {ok: true, json: async () => ({status: "not_found", error: "job not found"})};
    }
    return inner(url);
  };
  await t.sandbox.runOnceOnModal({}, [], t.context(), null);
  assert.equal(t.saved().length, 0, "陈旧读之后照常完成并清记录");
  assert.equal(t.observed.fetches, 1);
});

test("刷新恢复:not_found 达阈值即清记录收工", async () => {
  const t = setup({status: "not_found"});
  const job = {jobId: "job", startedAt: Date.now()};
  t.sandbox.addActiveJob(job);
  await t.sandbox.recoverOne(job, 1200);
  assert.equal(t.saved().length, 0, "云端已无此任务,恢复记录不该永久残留");
  assert.equal(t.observed.fetches, 0);
});

test("取消不存在的任务:不报'取消失败',不弹计费警告", async () => {
  const t = setup({cancelGone: true});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  const r = await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(r, false);
  assert.equal(t.observed.alerts, 0,
    "不存在的任务不该弹'云端可能仍在运行并继续计费'——那是假警报");
  assert.equal(t.saved().length, 0, "记录该清掉");
});
