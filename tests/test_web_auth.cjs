// 只执行真实配对函数，凭据/网络/存储完全隔离。
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const source = fs.readFileSync(path.join(__dirname, "../web/modal_bridge.js"), "utf8");
const start = source.indexOf("const LOCAL_CAP_KEY =");
const end = source.indexOf("// 上报 job", start);
assert(start >= 0 && end > start);
const denied = (pair = true) => ({ status: 403, headers: new Headers(
  pair ? {"X-Modal-Bridge-Auth": "capability-required"} : {}) });
const ok = () => ({ status: 200, headers: new Headers() });

function setup({ saved = "", answer = "test-cap", fetch } = {}) {
  const seen = { prompts: 0, calls: [], alerts: [] };
  const sandbox = {
    Headers, t: (key) => key,
    alert: (message) => seen.alerts.push(message),
    localStorage: {
      getItem: () => saved, setItem: (_key, value) => { saved = value; },
      removeItem: () => { saved = ""; },
    },
    window: { prompt: () => { seen.prompts++; return answer; } },
    api: { fetchApi: async (url, options) => {
      seen.calls.push({url, options});
      if (fetch) return fetch(url, options, seen.calls.length);
      return options.headers.get("X-Modal-Bridge-Capability") === "test-cap" ? ok() : denied();
    } },
  };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(start, end), sandbox);
  return { call: sandbox.bridgeFetch, seen, saved: () => saved,
    answer: (value) => { answer = value; } };
}

test("本机首次配对后重放原始 JSON 请求，已有 token 不再提示", async () => {
  const t = setup();
  const options = {method: "POST", body: '{"test":true}', headers: {"Content-Type": "application/json"}};
  assert.equal((await t.call("/modal_bridge/deploy", options)).status, 200);
  assert.equal(t.seen.prompts, 1);
  assert.equal(t.seen.calls.length, 2);
  assert.equal(t.seen.calls[1].options.body, options.body);
  assert.equal(t.seen.calls[1].options.headers.get("Content-Type"), "application/json");
  await t.call("/modal_bridge/poll");
  assert.equal(t.seen.prompts, 1);
});

test("并发首次请求只配对一次", async () => {
  const t = setup();
  const results = await Promise.all([t.call("/a"), t.call("/b"), t.call("/c")]);
  assert(results.every((r) => r.status === 200));
  assert.equal(t.seen.prompts, 1);
});

test("取消配对不连环弹窗，下次操作仍可配对", async () => {
  const t = setup({answer: ""});
  const results = await Promise.all([t.call("/a"), t.call("/b")]);
  assert(results.every((r) => r.status === 403));
  assert.equal(t.seen.prompts, 1);
  assert.equal(t.seen.calls.length, 2);
  t.answer("test-cap");
  assert.equal((await t.call("/c")).status, 200);
  assert.equal(t.seen.prompts, 2);
});

test("输入错误 token 最多重试一次，不无限弹窗", async () => {
  const t = setup({answer: "wrong-cap"});
  assert.equal((await t.call("/a")).status, 403);
  assert.equal(t.seen.prompts, 1);
  assert.equal(t.seen.calls.length, 2);
  t.answer("test-cap");
  assert.equal((await t.call("/b")).status, 200);
});

test("普通跨站 403 不发起配对、不删除已存 token", async () => {
  const t = setup({saved: "test-cap", fetch: async () => denied(false)});
  assert.equal((await t.call("/a")).status, 403);
  assert.equal(t.seen.prompts, 0);
  assert.equal(t.saved(), "test-cap");
});

test("误粘中文不能写入存储或卡死后续配对", async () => {
  const t = setup({answer: "错误-token"});
  assert.equal((await t.call("/a")).status, 403);
  assert.equal(t.saved(), "");
  assert.deepEqual(t.seen.alerts, ["auth.invalid"]);
  t.answer("test-cap");
  assert.equal((await t.call("/b")).status, 200);
  const legacy = setup({saved: "旧的错误值"});
  assert.equal((await legacy.call("/a")).status, 200);
});

test("迟到的旧 token 403 复用新 token，不清空、不重新询问", async () => {
  let release;
  const t = setup({saved: "old-cap", fetch: async (url, options) => {
    if (options.headers.get("X-Modal-Bridge-Capability") === "test-cap") return ok();
    if (url === "/late") return new Promise((resolve) => { release = resolve; });
    return denied();
  }});
  const late = t.call("/late");
  assert.equal((await t.call("/first")).status, 200);
  release(denied());
  assert.equal((await late).status, 200);
  assert.equal(t.seen.prompts, 1);
  assert.equal(t.saved(), "test-cap");
});

test("长下载不阻止取消配对后的新操作再次配对", async () => {
  let release;
  const t = setup({answer: "", fetch: async (url, options) => {
    if (url === "/download") return new Promise((resolve) => { release = resolve; });
    return options.headers.get("X-Modal-Bridge-Capability") === "test-cap" ? ok() : denied();
  }});
  const downloading = t.call("/download");
  assert.equal((await t.call("/a")).status, 403);
  t.answer("test-cap");
  assert.equal((await t.call("/b")).status, 200);
  assert.equal(t.seen.prompts, 2);
  release(ok());
  await downloading;
});
