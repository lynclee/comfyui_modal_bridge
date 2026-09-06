// 执行真实前端函数，网络/画布/存储隔离；不依赖浏览器或第三方测试包。
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../web/modal_bridge.js"), "utf8");

function setup({ fetchFails = false, status = "completed", cancelError = false } = {}) {
  let saved = [];
  const observed = { stages: [], timers: [], cleared: [], fetches: 0 };
  const context = () => ({
    wfName: "test", stage: (...args) => observed.stages.push(args),
    finish: () => {}, setCancel: () => {},
  });
  const sandbox = {
    Date, Headers, LS_KEYS: { activeJob: "jobs" },
    loadLS: () => JSON.parse(JSON.stringify(saved)),
    saveLS: (_key, value) => { saved = JSON.parse(JSON.stringify(value)); },
    getSetting: (_key, value) => value, getVramTier: () => "80g",
    sleep: async () => {}, log: () => {}, err: () => {}, notify: () => {}, alert: () => {},
    reportJobEvent: () => {}, confirm: () => true, t: (key) => key,
    fmtRate: String, fmtDur: String, newProgress: context,
    MODEL3D_EXT_RE: /\.glb$/, displayInGraph: () => true,
    setInterval: (fn) => { observed.timers.push(fn); return observed.timers.length; },
    clearInterval: (id) => observed.cleared.push(id),
    bridgeFetch: async (url) => {
      if (url.endsWith("/submit")) return {ok: true, json: async () => ({ok: true, job_id: "job", gpu: "test"})};
      if (url.includes("/poll?")) return {ok: true, json: async () => ({status, images: []})};
      if (url.endsWith("/cancel")) return {ok: !cancelError, json: async () => cancelError
        ? {error: "injected failure"} : {cancel_noop: true, status: "completed", images: []}};
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
