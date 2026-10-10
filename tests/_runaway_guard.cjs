// 测试沙箱的虚拟时钟保险(2026-10-10)。每个用到 vm 沙箱的 .cjs 测试文件开头 require 它。
//
// 沙箱里的 sleep 桩是立即 resolve 的 async 函数(只推进虚拟时钟),fetch 桩也同步返回。被测代码的轮询循环一旦
// 失去终止条件(变异测试、或者真的改坏了),每一圈只 await 已经 resolve 的 promise:微任务队列永远清不空,
// 事件循环拿不到控制权,--test-timeout 的定时器不会触发,父进程被杀后子进程变成孤儿接着空转。
// 2026-10-05 的前端变异测试就这样留下 4 个进程,各占满一个核跑了三天多。
//
// 做法:包住 vm.createContext,沙箱上有 sleep 函数就换成访问器 —— 读到的是带保险的包装,
// 用例自己赋值(h.sb.sleep = …)只替换里面那层,保险照旧。
//   · 每 YIELD_EVERY 次让出一次事件循环(setImmediate),定时器与信号有机会处理;
//   · 同一个沙箱累计超过 MAX_SLEEPS 次判为失控:抛错让用例失败;错误被被测代码吞掉、还在接着调 sleep 的,
//     直接结束进程(退出码 1),绝不留下空转的进程。
// MAX_SLEEPS 按实测定:现有用例单个沙箱最多约 18 500 次(排队 6 小时兜底线那条,1.2s 一拍),留足余量。
"use strict";
const vm = require("node:vm");

const MAX_SLEEPS = 200_000;
const YIELD_EVERY = 1_000;
const stats = { maxSleeps: 0 };

function guard(sandbox) {
  const d = Object.getOwnPropertyDescriptor(sandbox, "sleep");
  if (!d || !("value" in d) || typeof d.value !== "function") return;
  let inner = d.value;
  let n = 0;
  let tripped = false;
  const guarded = async (ms) => {
    n++;
    if (n > stats.maxSleeps) stats.maxSleeps = n;
    if (n % YIELD_EVERY === 0) await new Promise((r) => setImmediate(r));
    if (n > MAX_SLEEPS) {
      const msg = `虚拟时钟失控:同一个沙箱调了 ${n} 次 sleep(上限 ${MAX_SLEEPS}),被测循环多半没有终止条件`;
      if (tripped) {
        console.error(msg + ";抛出的错误被吞掉了,直接结束进程");
        process.exit(1);
      }
      tripped = true;
      throw new Error(msg);
    }
    return inner(ms);
  };
  Object.defineProperty(sandbox, "sleep", {
    get: () => guarded,
    set: (fn) => { inner = fn; },
    enumerable: true,
    configurable: true,
  });
}

const origCreateContext = vm.createContext;
vm.createContext = function createContextWithRunawayGuard(sandbox, ...rest) {
  if (sandbox && typeof sandbox === "object") guard(sandbox);
  return origCreateContext.call(this, sandbox, ...rest);
};

module.exports = { MAX_SLEEPS, YIELD_EVERY, stats };
