// 只执行真实配对函数(含弹层接线),凭据/网络/存储完全隔离;沙箱里故意没有 window.prompt。
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

// 假 DOM:只实现 askCapability 用到的那几个接口。overlay 一挂到 body,就"扮演用户":
// 下一拍把答案填进输入框、点确定(或按 Enter);无效值窗口不关,继续按答案队列答下一个;
// 队列耗尽仍没关就点取消。这样测的是真实的弹层接线,而不是一个被桩掉的 prompt。
function fakeDocument(seen, next, via) {
  const mk = (tag) => ({
    tag, style: {}, children: [], on: {}, value: "", textContent: "", removed: false,
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { (this.on[ev] ||= []).push(fn); },
    fire(ev, e = {}) { for (const fn of this.on[ev] || []) fn({ preventDefault() {}, ...e }); },
    remove() { this.removed = true; }, focus() { seen.focused++; },
  });
  const find = (el, pred) => (pred(el) ? el : el.children.map((c) => find(c, pred)).find(Boolean));
  return {
    createElement: mk,
    body: { appendChild(overlay) {
      seen.prompts++;
      queueMicrotask(() => {
        const input = find(overlay, (e) => e.tag === "input");
        const ok = find(overlay, (e) => e.tag === "button" && e.textContent === "auth.ok");
        const cancel = find(overlay, (e) => e.tag === "button" && e.textContent === "auth.cancel");
        const err = find(overlay, (e) => e.className === "mb-pair-error");
        let guard = 0;
        while (!overlay.removed && guard++ < 8) {
          const a = next();
          if (a === null) { cancel.fire("click"); break; }
          input.value = a;
          if (via === "enter") input.fire("keydown", { key: "Enter" });
          else if (via === "escape") input.fire("keydown", { key: "Escape" });
          else ok.fire("click");
          if (!overlay.removed) seen.errors.push(err.textContent);
        }
      });
    } },
  };
}

// answer:字符串 = 每次都答它;数组 = 按序答,答完停在最后一个;null = 点取消。
function setup({ saved = "", answer = "test-cap", fetch, via } = {}) {
  const seen = { prompts: 0, calls: [], errors: [], focused: 0 };
  let queue = Array.isArray(answer) ? [...answer] : null;
  let single = Array.isArray(answer) ? null : answer;
  const next = () => (queue ? (queue.length > 1 ? queue.shift() : queue[0]) : single);
  const sandbox = {
    Headers, t: (key) => key,
    localStorage: {
      getItem: () => saved, setItem: (_key, value) => { saved = value; },
      removeItem: () => { saved = ""; },
    },
    window: { document: fakeDocument(seen, next, via) },   // 故意没有 prompt:Desktop/Electron 就是这样
    api: { fetchApi: async (url, options) => {
      seen.calls.push({url, options});
      if (fetch) return fetch(url, options, seen.calls.length);
      return options.headers.get("X-Modal-Bridge-Capability") === "test-cap" ? ok() : denied();
    } },
  };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(start, end), sandbox);
  return { call: sandbox.bridgeFetch, seen, saved: () => saved,
    answer: (value) => { queue = Array.isArray(value) ? [...value] : null; single = Array.isArray(value) ? null : value; } };
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

test("误粘中文不能写入存储:行内提示、窗口不关,改对后同一次配对成功", async () => {
  const t = setup({answer: ["错误-token", "test-cap"]});
  assert.equal((await t.call("/a")).status, 200);
  assert.equal(t.seen.prompts, 1);                  // 没有二次弹窗,是同一个窗内改的
  assert.deepEqual(t.seen.errors, ["auth.invalid"]);
  assert.equal(t.saved(), "test-cap");
  const legacy = setup({saved: "旧的错误值"});
  assert.equal((await legacy.call("/a")).status, 200);
});

test("Enter 提交、Esc 放弃,与点按钮等价", async () => {
  const enter = setup({via: "enter"});
  assert.equal((await enter.call("/a")).status, 200);
  assert.equal(enter.saved(), "test-cap");
  const esc = setup({via: "escape"});
  assert.equal((await esc.call("/a")).status, 403);
  assert.equal(esc.saved(), "");
  assert.equal(esc.seen.calls.length, 1);
});

test("点取消等价于放弃,不写存储", async () => {
  const t = setup({answer: null});
  assert.equal((await t.call("/a")).status, 403);
  assert.equal(t.saved(), "");
  assert.equal(t.seen.prompts, 1);
});

test("源码不再调用 prompt():Electron(ComfyUI Desktop)不支持,调用即抛错", () => {
  // 去掉行注释再查,注释里允许提到它;正则排除 queuePrompt( / graphToPrompt( 这类 ComfyUI API。
  const code = source.split("\n").map((l) => l.replace(/\/\/.*$/, "")).join("\n");
  assert(!/window\.prompt\s*\(/.test(code), "window.prompt( 回来了");
  assert(!/(^|[^A-Za-z0-9_.$])prompt\s*\(/.test(code), "裸 prompt( 回来了");
  assert(source.includes("function askCapability("));
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
