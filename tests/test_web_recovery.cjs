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
                 cancelGone = false, pollSeq = null, cancelSeq = null } = {}) {
  let saved = [];
  const observed = { stages: [], timers: [], cleared: [], fetches: 0, polls: 0, alerts: 0,
                     cancels: 0, sleeps: [], notifies: [], finishes: [] };
  const context = () => ({
    wfName: "test", stage: (...args) => observed.stages.push(args),
    finish: (ok, label) => observed.finishes.push(label), setCancel: () => {},
  });
  const sandbox = {
    Date, Headers, LS_KEYS: { activeJob: "jobs" },
    loadLS: () => JSON.parse(JSON.stringify(saved)),
    saveLS: (_key, value) => { saved = JSON.parse(JSON.stringify(value)); },
    getSetting: (_key, value) => value, getVramTier: () => "80g",
    sleep: async (ms) => { observed.sleeps.push(ms); }, log: () => {}, err: () => {},
    notify: (m) => { observed.notifies.push(m); },
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
        // pollSeq:按次给响应(用完后停在最后一个),用来模拟 502 {error}、中途变状态等真实形态
        if (pollSeq) return pollSeq[Math.min(observed.polls - 1, pollSeq.length - 1)];
        return {ok: true, json: async () => ({status, images: []})};
      }
      if (url.endsWith("/cancel")) {
        observed.cancels++;
        if (cancelSeq) return cancelSeq[Math.min(observed.cancels - 1, cancelSeq.length - 1)];
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
  const t = setup({cancelGone: true, status: "not_found"});   // 复查也查不到 = 确认没了
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  const r = await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(r, false);
  assert.equal(t.observed.alerts, 0,
    "不存在的任务不该弹'云端可能仍在运行并继续计费'——那是假警报");
  assert.equal(t.saved().length, 0, "记录该清掉");
});

test("取消回 not_found 但复查时任务还在:不清记录、不承诺'不再计费'", async () => {
  // ⚠ 这条路误判的代价比轮询那条路高一个量级:删掉恢复记录 → 再没人去取结果,任务继续
  //   跑到底、继续计费、产物烂在 Volume 上;而且会告诉用户「不会继续计费」,那句可能是假的。
  //   (2026-09-20 codex 复查抓到:轮询那条路论证了一次不算数,cancel 这条却一次就下结论。)
  const t = setup({cancelGone: true, status: "running"});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  const r = await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(r, false);
  assert.equal(t.saved().length, 1, "没确认之前不能删恢复记录");
  assert.equal(t.observed.alerts, 1, "两次结果矛盾,必须弹到用户面前,而不是安抚");
  assert(t.observed.polls >= 1, "必须真的去复查过状态");
  assert.equal(t.observed.cancels, 2, "复查看到它活着,应当重发一次取消(且只重发一次)");
});

test("取消回 not_found、复查查不动:按'没确认'处理(fail-closed)", async () => {
  const t = setup({cancelGone: true});
  const inner = t.sandbox.bridgeFetch;
  t.sandbox.bridgeFetch = async (url) => {
    if (url.includes("/poll?")) throw new Error("network down");
    return inner(url);
  };
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  const r = await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(r, false);
  assert.equal(t.saved().length, 1, "查不动就不敢下结论,记录留着");
  assert.equal(t.observed.alerts, 1);
});

// ── 2026-09-23 review:0.8.46 的复核号称 fail-closed,在最常见的故障形态下是 fail-open ──
// bridgeFetch 对非 2xx 不抛异常,本地 /poll 连不上 Modal 时回 502 {error}、没有 status。
// 0.8.46 只 catch 了 throw,测试也只 mock 了 throw —— 所以测试全绿给的是假保证。
// 下面这几条全部用**真实会出现的响应形态**,不再只模拟抛异常。

const GONE = {ok: true, json: async () => ({id: "job", status: "not_found", error: "job not found"})};
const r502 = {ok: false, status: 502, json: async () => ({error: "upstream timeout"})};

test("复核遇到本地 /poll 502 {error}:按'没确认'处理,绝不说'不再计费'", async () => {
  const t = setup({cancelSeq: [GONE], pollSeq: [r502]});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.saved().length, 1, "Modal 不可达时删记录 = 任务可能在跑却没人管");
  assert.equal(t.observed.alerts, 1);
  assert(!t.observed.notifies.includes("cancel.gone_msg"),
    "不能对用户说'没有任务在跑,也不会继续计费'——这正是 0.8.46 的 fail-open");
});

test("复核遇到 200 但缺 status 的 {error}:同样按'没确认'处理", async () => {
  const t = setup({cancelSeq: [GONE],
                   pollSeq: [{ok: true, json: async () => ({error: "weird"})}]});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.saved().length, 1);
  assert(!t.observed.notifies.includes("cancel.gone_msg"));
});

test("复核看到 completed:按'取消没赶上'取回产物,不报假警报", async () => {
  const t = setup({cancelSeq: [GONE], status: "completed"});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.observed.fetches, 1, "已付费的产物必须取回");
  assert.equal(t.observed.alerts, 0, "对已结束的任务报'可能仍在计费'是假警报");
  assert.equal(t.saved().length, 0);
});

test("复核看到 failed:如实收尾,不报'可能仍在计费'", async () => {
  const t = setup({cancelSeq: [GONE], status: "failed"});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.observed.alerts, 0);
  assert.equal(t.observed.fetches, 0);
  assert.equal(t.saved().length, 0);
});

test("复核看到 running:重发取消,真正止损", async () => {
  const cancelled = {ok: true, json: async () => ({id: "job", status: "cancelled"})};
  const t = setup({cancelSeq: [GONE, cancelled], status: "running"});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.observed.cancels, 2, "看到它活着就该对它再发一次取消");
  assert.equal(t.observed.alerts, 0, "重发成功就不该再吓用户");
  assert.equal(t.saved().length, 0);
});

test("复核窗口跟轮询间隔走,不写死(依据是最终一致的时间窗,不是次数)", async () => {
  const t = setup({cancelGone: true, status: "not_found"});
  t.sandbox.getSetting = (key, dflt) => key === "ModalBridge.pollIntervalSec" ? 2 : dflt;
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.observed.sleeps.length, NOT_FOUND_STREAK - 1);
  assert(t.observed.sleeps.every((ms) => ms === 2000),
    `复核间隔必须等于轮询间隔 2000ms,实际 ${t.observed.sleeps}`);
});

test("复核期间卡片显示'确认中',不停在乐观的 Cancelled 上", async () => {
  const t = setup({cancelGone: true, status: "not_found"});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.observed.finishes[0], "cancel.confirming", t.observed.finishes);
  assert.equal(t.observed.finishes.at(-1), "cancel.gone");
});

test("刷新恢复:502 {error} 夹在 not_found 之间既不计数也不清零", async () => {
  // 0.8.46 之前 recoverOne 只防 throw,502 {error} 会落到下面把连续计数清零,永远凑不满。
  const nf = {ok: true, json: async () => ({status: "not_found", error: "job not found"})};
  const seq = [];
  for (let i = 0; i < NOT_FOUND_STREAK; i++) seq.push(nf, r502);
  const t = setup({pollSeq: seq});
  const job = {jobId: "job", startedAt: Date.now()};
  t.sandbox.addActiveJob(job);
  await t.sandbox.recoverOne(job, 1200);
  assert.equal(t.saved().length, 0, "凑满 NOT_FOUND_STREAK 次肯定的 not_found 后应当收工");
});

test("取消没赶上、任务其实失败了:显示失败原因,不弹「可能仍在计费」", async () => {
  // 取消一个已结束(失败 / worker 早已死)的任务时,响应里的 error 是**任务的**失败原因。
  // 以前它掉进「取消失败,云端可能仍在运行并计费」分支 —— 对已结束的任务正好说反(review #12)。
  const failed = {ok: true, json: async () => ({id: "job", status: "failed",
    error: "worker 超过部署时的超时上限 1200s 仍未写回结果 —— 已被 Modal 强杀", cancel_noop: true})};
  const t = setup({cancelSeq: [failed]});
  t.sandbox.addActiveJob({jobId: "job", startedAt: Date.now()});
  await t.sandbox.requestCancel("job", t.context(), null);
  assert.equal(t.observed.alerts, 0, "对已结束的任务弹「可能仍在计费」是假警报");
  assert.equal(t.saved().length, 0, "任务已结束,恢复记录该清");
  assert.equal(t.observed.finishes.at(-1), "✗ Failed", t.observed.finishes);
});
