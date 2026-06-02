// comfyui_modal_bridge — P1+P2 增强版
//
// 包含:
//   - actionBarButtons API 顶部按钮
//   - 异步轮询(submit → poll → fetch_result)
//   - 进度浮窗(可拖动 + 取消 + 错误展开 + history 持久化)
//   - custom_node 预警
//   - 批量提交(Run × N,自动改 seed)
//   - 注册到 ComfyUI Settings 面板
//   - 结果回填 SaveImage / PreviewImage 节点

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const log = (...a) => console.log("[modal_bridge]", ...a);
const err = (...a) => console.error("[modal_bridge]", ...a);

// =====================================================================
// 工具
// =====================================================================
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const LS_KEYS = {
  progressPos: "modal_bridge.progress_pos",
  activeJob: "modal_bridge.active_job",   // 持久化未完成 job
};

function loadLS(k, def = null) {
  try {
    const v = localStorage.getItem(k);
    return v ? JSON.parse(v) : def;
  } catch (e) { return def; }
}
function saveLS(k, v) {
  try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {}
}
function clearLS(k) { try { localStorage.removeItem(k); } catch (e) {} }

// ComfyUI Settings 读取(优先,失败回退到 config.json)
function getSetting(id, def) {
  try {
    const v = app.ui?.settings?.getSettingValue?.(id);
    return v !== undefined && v !== null ? v : def;
  } catch (e) { return def; }
}

// =====================================================================
// 通知封装(右上角 toast)
// =====================================================================
function notify(message, severity = "info") {
  try {
    app.extensionManager?.toast?.add?.({
      severity,
      summary: "Modal Bridge",
      detail: message,
      life: severity === "error" ? 8000 : 4000,
    });
    return;
  } catch (e) {}
  log(`[${severity}]`, message);
}

// =====================================================================
// 输出节点 & 回填
// =====================================================================
function findOutputNodes(prompt) {
  const types = new Set([
    "SaveImage", "PreviewImage", "SaveImageWebsocket",
    "SaveImageAdvanced", "Image Save", "VHS_VideoCombine",
  ]);
  const ids = [];
  for (const [id, n] of Object.entries(prompt || {})) {
    if (types.has(n?.class_type)) ids.push(id);
  }
  return ids;
}

// 当前活动工作流的标识(切 tab 会变)。用于判断结果该不该回填到当前画板。
function activeWorkflowKey() {
  try {
    const w = app.extensionManager?.workflow?.activeWorkflow
           ?? app.workflowManager?.activeWorkflow;
    if (!w) return null;
    return w.path ?? w.key ?? w.filename ?? w.id ?? (typeof w === "string" ? w : null);
  } catch (e) { return null; }
}

// 当前工作流的可读短名(用于进度卡片标题)。取文件名去掉 .json,拿不到则 null。
function activeWorkflowName() {
  try {
    const w = app.extensionManager?.workflow?.activeWorkflow
           ?? app.workflowManager?.activeWorkflow;
    if (!w) return null;
    let n = w.filename ?? w.name ?? (typeof w.path === "string" ? w.path : null)
         ?? w.key ?? (typeof w === "string" ? w : null);
    if (!n) return null;
    n = String(n).split(/[\\/]/).pop().replace(/\.json$/i, "");
    return n || null;
  } catch (e) { return null; }
}

// 结果回填:仅当仍停在提交时那个工作流(tab)才按 id 回填,切了就不填(避免填到别的工作流)。
// guard = {graph, wfKey}(提交时抓)。返回回填节点数(0 = 没填,调用方提示存盘路径)。
// 回填到「当前前台」工作流的节点。ComfyUI 单 graph,只能渲染当前前台;调用方负责
// 确保这是该结果对应的工作流。走原生 executed 事件(ComfyUI 自己写 nodeOutputStore/
// nodeOutputs + 调 onExecuted,跨版本可靠)。返回成功回填的节点数。
function displayInGraph(outputNodeIds, modalOutputs) {
  if (!outputNodeIds?.length || !modalOutputs?.length) return 0;
  const imagesMeta = modalOutputs.map((o) => ({
    filename: o.filename,
    subfolder: o.subfolder,
    type: o.type || "output",
  }));
  let placed = 0;
  for (const nid of outputNodeIds) {
    const node = app.graph.getNodeById(parseInt(nid, 10)) || app.graph.getNodeById(nid);
    if (!node) continue;
    try {
      api.dispatchEvent(new CustomEvent("executed", {
        detail: { node: String(nid), display_node: String(nid), output: { images: imagesMeta } },
      }));
    } catch (e) {}
    try { node.onExecuted?.({ images: imagesMeta }); } catch (e) {}
    placed++;
  }
  try { app.graph.setDirtyCanvas(true, true); } catch (e) {}
  log(`displayInGraph: ids=[${outputNodeIds.join(",")}] placed=${placed}`);
  return placed;
}

// 后台工作流的出图结果暂存,等用户切回那个 tab 再渲染(ComfyUI 单 graph,后台渲染不了)。
const _pendingResults = new Map();  // wfKey -> {outputNodeIds, outputs}
let _pendingWatcher = null;
function storePendingResult(wfKey, outputNodeIds, outputs) {
  _pendingResults.set(wfKey, { outputNodeIds, outputs });
  if (_pendingWatcher) return;
  _pendingWatcher = setInterval(() => {
    if (!_pendingResults.size) { clearInterval(_pendingWatcher); _pendingWatcher = null; return; }
    const k = activeWorkflowKey();
    if (k != null && _pendingResults.has(k)) {
      const r = _pendingResults.get(k);
      _pendingResults.delete(k);
      log("切到该工作流,回填暂存结果:", k);
      displayInGraph(r.outputNodeIds, r.outputs);
    }
  }, 700);
}

// =====================================================================
// custom_node 双向同步
// 后端 /check_nodes 精确解析(本地 NODE_CLASS_MAPPINGS → custom_nodes 文件夹),
// 和 Modal /health 的 custom_nodes 权威比对,算出 加/改/删 plan;确认后走 /sync_nodes
// 写回清单 + 重部署。本地始终是真源。全程在 ComfyUI 里完成,不用开终端。
// =====================================================================
async function checkNodesOnModal(prompt) {
  const res = await api.fetchApi("/modal_bridge/check_nodes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!res.ok) throw new Error(`check_nodes HTTP ${res.status}`);
  return res.json(); // {add, update, prune, missing_no_git, unresolved, new_baked, needs_deploy, ok_*, source}
}

// 流式 POST(/deploy、/add_nodes 共用):逐行回调 onLine,返回 __DEPLOY_DONE__ 的 rc(无则 null)
async function streamPost(path, body, onLine) {
  const res = await api.fetchApi(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`${path} HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "", rc = null;
  const feed = (line) => {
    if (!line.length) return;
    const m = line.match(/__DEPLOY_DONE__ rc=(\d+)/);
    if (m) { rc = parseInt(m[1], 10); return; }
    onLine(line);
  };
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop(); // 末尾不完整的留到下一轮
    for (const l of lines) feed(l);
  }
  if (buf) feed(buf);
  return rc;
}

// 调 /sync_nodes,把新清单写回并重部署,流式推进 deploying 进度条,返回是否成功
async function syncNodes(plan, ctx) {
  const [a, b] = STATUS_PROGRESS.deploying;
  let tick = 0;
  const rc = await streamPost(
    "/modal_bridge/sync_nodes",
    {
      new_baked: plan.new_baked,
      summary: { add: plan.add.length, update: plan.update.length, prune: plan.prune.length },
    },
    (line) => {
      log("deploy:", line);
      tick++;
      ctx.bar(a + Math.min(b - a - 1, tick * 0.4));
      ctx.stage("deploying", line.length > 72 ? line.slice(0, 72) + "…" : line);
    },
  );
  return rc === 0;
}

// submit 前调:custom_node 与本地双向同步(加/改/删)。返回 true=可继续 / false=用户取消
async function ensureNodesAvailable(prompt, ctx) {
  ctx.stage("nodes", "Scanning workflow custom nodes...");
  let plan;
  try {
    plan = await checkNodesOnModal(prompt);
  } catch (e) {
    log("check_nodes failed, skip:", e);
    return true; // 检查本身失败不阻塞提交
  }

  const { add = [], update = [], prune = [], missing_no_git = [], unresolved = [], source } = plan;
  if (unresolved.length) log("unresolved class_types (本地也没装):", unresolved);

  if (missing_no_git.length) {
    const list = missing_no_git.map((m) => `  ${m.folder} (${m.class_types.join(", ")})`).join("\n");
    notify(`这些 custom_node 没有 git 信息,无法自动补:\n${list}`, "warn");
  }

  if (!plan.needs_deploy) {
    ctx.stage("nodes", `nodes ok (${plan.ok_baked} custom + ${plan.ok_builtin} builtin)`);
    if (missing_no_git.length) {
      return confirm(`部分 custom_node 无法自动补,仍然提交?(很可能失败)`);
    }
    return true;
  }

  // 组装确认文案(只加 / 改,不删 —— 多机并集,删走 Setup 的「管理云端节点」手动做)
  const parts = [];
  if (add.length) {
    parts.push("➕ 新增(Modal 还没有):\n" + add.map((m) =>
      `   • ${m.folder} (${m.class_types.length} 节点)` + (m.commit ? ` @ ${m.commit.slice(0, 8)}` : "")
    ).join("\n"));
  }
  if (update.length) {
    parts.push("🔄 更新(本地 commit 变了):\n" + update.map((m) =>
      `   • ${m.folder}  ${(m.old_commit || "—").slice(0, 8)} → ${m.commit.slice(0, 8)}`
    ).join("\n"));
  }
  const msg =
    `工作流需要的 custom_node 要同步到 Modal(镜像来源:${source}):\n\n${parts.join("\n\n")}\n\n` +
    `点「确定」更新 Modal 镜像并重新部署(约 1-3 分钟,只这一次,之后秒进)。\n` +
    `点「取消」则跳过同步,直接提交。`;
  if (!confirm(msg)) {
    return confirm("不同步节点,直接提交?(可能失败)");
  }

  ctx.stage("deploying", "Redeploying Modal image...", false);
  ctx.bar(STATUS_PROGRESS.deploying[0]);
  const ok = await syncNodes(plan, ctx);
  if (!ok) {
    throw new Error("Modal 重部署失败(看进度窗 deploy 日志 / ComfyUI 控制台)");
  }
  ctx.stage("deploying", "✓ image updated", false);
  return true;
}

// =====================================================================
// 批量提交:扫 prompt 找 KSampler/RandomNoise/RandomNoiseAdv 的 seed 字段
// =====================================================================
function reseedPrompt(prompt, newSeed) {
  // 深拷贝
  const p = JSON.parse(JSON.stringify(prompt));
  for (const n of Object.values(p)) {
    const cls = n?.class_type || "";
    const ins = n?.inputs || {};
    if (
      cls === "KSampler" || cls === "KSamplerAdvanced" ||
      cls === "RandomNoise" || cls === "Seed" ||
      cls === "SamplerCustomAdvanced"
    ) {
      if ("seed" in ins && typeof ins.seed === "number") ins.seed = newSeed;
      if ("noise_seed" in ins && typeof ins.noise_seed === "number") ins.noise_seed = newSeed;
    }
  }
  return p;
}

// =====================================================================
// 进度浮窗 — 每个 job 一张独立卡片(多 workflow 并发互不覆盖)
// =====================================================================
const STAGE_LABELS = {
  preparing: "Preparing", nodes: "Checking nodes", deploying: "Deploying image",
  checking: "Checking models", uploading: "Uploading models", submitting: "Submitting",
  queued: "Queued", running: "Running",
  downloading: "Downloading result", done: "Done", failed: "Failed",
};

const STATUS_PROGRESS = {
  preparing:   [0,  2],    // 序列化 workflow(纯前端,几百 ms)
  nodes:       [2,  5],    // 检查 custom_node Modal 是否齐全
  deploying:   [5, 35],    // (仅需补节点时)重 build 镜像并部署,1-3 分钟
  checking:    [35, 40],   // 检查 Modal Volume 已有哪些模型
  uploading:   [40, 78],   // 上传本地缺失模型到 Volume
  submitting:  [78, 82],
  queued:      [82, 85],
  running:     [85, 96],
  downloading: [96, 99],   // 出图 base64 回流
  done:        [100, 100],
  failed:      [100, 100],
};

// 卡片容器:右上角纵向堆叠,每个并发 job 一张卡。拖任意卡的标题 = 移动整个堆。
let progressStack = null;
let _stackDrag = null;

function _initStackDragListeners() {
  if (_initStackDragListeners._done) return;
  _initStackDragListeners._done = true;
  document.addEventListener("mousemove", (e) => {
    if (!_stackDrag || !progressStack) return;
    const dx = e.clientX - _stackDrag.x, dy = e.clientY - _stackDrag.y;
    const top = Math.max(0, _stackDrag.top + dy);
    const right = Math.max(0, _stackDrag.right - dx);
    progressStack.style.top = `${top}px`;
    progressStack.style.right = `${right}px`;
    progressStack.style.left = "auto";
  });
  document.addEventListener("mouseup", () => {
    if (!_stackDrag || !progressStack) { _stackDrag = null; return; }
    const top = parseInt(progressStack.style.top, 10);
    const right = parseInt(progressStack.style.right, 10);
    saveLS(LS_KEYS.progressPos, { top, right });
    _stackDrag = null;
  });
}

function startStackDrag(e) {
  if (e.target.closest(".mb-cancel")) return; // 别和取消按钮抢点击
  const rect = progressStack.getBoundingClientRect();
  _stackDrag = {
    x: e.clientX, y: e.clientY,
    top: rect.top,
    right: window.innerWidth - rect.right,
  };
  e.preventDefault();
}

function ensureStack() {
  if (progressStack) return progressStack;
  progressStack = document.createElement("div");
  progressStack.id = "modal-bridge-progress-stack";
  const pos = loadLS(LS_KEYS.progressPos) || {};
  const top = pos.top ?? 60;
  const right = pos.right ?? 20;
  progressStack.style.cssText =
    `position:fixed;top:${top}px;right:${right}px;z-index:99999;display:flex;flex-direction:column;gap:8px;align-items:flex-end;pointer-events:none;`;
  document.body.appendChild(progressStack);
  _initStackDragListeners();
  return progressStack;
}

let progressSeq = 0;

// 创建一个 job 的进度卡片,返回带方法的 ctx(stage / bar / setCancel / finish)。
// 每张卡自带计时器和状态,互不干扰 —— 这是多 workflow 并发不互相覆盖的关键。
function newProgress(initialStage = "preparing", wfName = null) {
  ensureStack();
  const card = document.createElement("div");
  card.style.cssText =
    "pointer-events:auto;min-width:280px;max-width:360px;padding:10px 14px;" +
    "background:rgba(28,28,36,0.96);color:#fff;border-radius:10px;font-size:12px;" +
    "font-family:-apple-system,system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,0.5);" +
    "backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.08);";
  card.innerHTML = `
    <div class="mb-drag" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;cursor:move;user-select:none;">
      <div class="mb-label" style="font-weight:600;flex:1;min-width:0;margin-right:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">☁️ Modal</div>
      <div style="display:flex;align-items:center;gap:8px;">
        <div class="mb-time" style="opacity:0.7;font-variant-numeric:tabular-nums;font-size:11px;">0s</div>
        <button class="mb-cancel" title="Cancel" style="all:unset;cursor:pointer;color:#ef4444;font-size:14px;font-weight:bold;padding:0 4px;border-radius:3px;display:none;">✕</button>
      </div>
    </div>
    <div style="height:4px;background:rgba(255,255,255,0.1);border-radius:2px;overflow:hidden;">
      <div class="mb-bar" style="height:100%;width:0%;background:linear-gradient(90deg,#6366f1,#8b5cf6);border-radius:2px;transition:width 0.4s cubic-bezier(0.4,0,0.2,1);"></div>
    </div>
    <div class="mb-detail" style="margin-top:6px;opacity:0.6;font-size:11px;word-break:break-all;"></div>
    <div class="mb-errtoggle" style="display:none;margin-top:4px;font-size:10px;color:#fca5a5;cursor:pointer;text-decoration:underline;">▶ Show error details</div>
    <pre class="mb-errdetail" style="display:none;margin:4px 0 0;padding:6px;background:rgba(0,0,0,0.3);border-radius:4px;font-size:10px;max-height:160px;overflow:auto;white-space:pre-wrap;"></pre>
  `;
  progressStack.appendChild(card);
  card.querySelector(".mb-drag").addEventListener("mousedown", startStackDrag);

  const els = {
    card,
    label: card.querySelector(".mb-label"),
    time: card.querySelector(".mb-time"),
    bar: card.querySelector(".mb-bar"),
    detail: card.querySelector(".mb-detail"),
    cancel: card.querySelector(".mb-cancel"),
    errToggle: card.querySelector(".mb-errtoggle"),
    errDetail: card.querySelector(".mb-errdetail"),
  };

  const ctx = {
    id: ++progressSeq,
    jobId: null,
    wfName: wfName,
    cancelable: false,
    finished: false,
    onCancel: null,
    startMs: Date.now(),
    timer: null,
    runTimer: null,
    els,
  };

  ctx.timer = setInterval(() => {
    els.time.textContent = `${((Date.now() - ctx.startMs) / 1000).toFixed(1)}s`;
  }, 100);

  els.cancel.onclick = async (ev) => {
    ev.stopPropagation();
    if (ctx.finished) { ctx.closeCard(); return; }  // 结束后:✕ = 关闭卡片
    if (!ctx.cancelable || typeof ctx.onCancel !== "function") return;
    await ctx.onCancel();
  };
  els.errToggle.onclick = () => {
    const open = els.errDetail.style.display === "block";
    els.errDetail.style.display = open ? "none" : "block";
    els.errToggle.textContent = open ? "▶ Show error details" : "▼ Hide error details";
  };

  ctx.bar = (pct) => { els.bar.style.width = `${pct}%`; };

  ctx.closeCard = () => {
    if (ctx.timer) { clearInterval(ctx.timer); ctx.timer = null; }
    if (ctx.runTimer) { clearInterval(ctx.runTimer); ctx.runTimer = null; }
    els.card.remove();
  };

  ctx.stage = (stageKey, detail = null, cancelable = null) => {
    els.label.textContent = `☁️ ${ctx.wfName || "Modal"} · ${STAGE_LABELS[stageKey] || stageKey}`;
    if (detail !== null) els.detail.textContent = detail;
    if (cancelable !== null) {
      ctx.cancelable = cancelable;
      els.cancel.style.display = cancelable ? "inline-block" : "none";
    }
    const range = STATUS_PROGRESS[stageKey];
    if (range) ctx.bar((range[0] + range[1]) / 2);
    if (ctx.runTimer) { clearInterval(ctx.runTimer); ctx.runTimer = null; }
    if (stageKey === "running" && range) {
      const [a, b] = range;
      const stageStart = Date.now();
      ctx.runTimer = setInterval(() => {
        const s = (Date.now() - stageStart) / 1000;
        ctx.bar(a + Math.min(b - a, (s / 60) * (b - a)));  // 60s 占满 running 区间
      }, 200);
    }
  };

  ctx.setCancel = (jobId, fn) => { ctx.jobId = jobId; ctx.onCancel = fn; };

  ctx.finish = (success, finalLabel = "✓ Done", errDetail = null) => {
    ctx.finished = true;
    if (ctx.timer) { clearInterval(ctx.timer); ctx.timer = null; }
    if (ctx.runTimer) { clearInterval(ctx.runTimer); ctx.runTimer = null; }
    ctx.bar(100);
    els.bar.style.background = success
      ? "linear-gradient(90deg,#10b981,#22c55e)"
      : "linear-gradient(90deg,#ef4444,#f97316)";
    els.label.textContent = `☁️ ${ctx.wfName || "Modal"} · ${finalLabel}`;
    els.time.textContent = `${((Date.now() - ctx.startMs) / 1000).toFixed(1)}s`;
    // 结束后 ✕ 变成「关闭」按钮(失败/取消的卡留着不自动消失,得能手动关)
    ctx.cancelable = false;
    els.cancel.textContent = "×";
    els.cancel.title = "关闭";
    els.cancel.style.color = "#9aa";
    els.cancel.style.display = "inline-block";
    if (errDetail) {
      els.errToggle.style.display = "block";
      els.errDetail.textContent = errDetail;
    }
    // 成功 4s 后自动移除;失败/取消留着让用户看,点 × 关
    if (success) setTimeout(() => els.card.remove(), 4000);
  };

  ctx.stage(initialStage);
  return ctx;
}

// =====================================================================
// 单次跑(submit + poll + fetch_result)
// =====================================================================
// GPU:统一全部走 H100 → A100-80GB(原生 fallback)。
// 不再按显存分档 —— 之前把小模型(klein 等)丢到 L40S 反而比大模型在 H100 上慢。
// tier 入参保留(后端兼容),但恒为 "80g" 都指向同一个 H100 worker。
// =====================================================================

// 所有工作流统一 H100;tier 恒为 "80g"(后端两档都指向同一个 H100 worker)。
function getVramTier(_prompt) {
  return "80g";
}

// 未完成 job 持久化(支持多个并发):LS 存数组,刷新后逐个尝试恢复
function addActiveJob(j) {
  let a = loadLS(LS_KEYS.activeJob);
  a = Array.isArray(a) ? a : (a ? [a] : []);
  a.push(j);
  saveLS(LS_KEYS.activeJob, a);
}
function removeActiveJob(jobId) {
  let a = loadLS(LS_KEYS.activeJob);
  a = Array.isArray(a) ? a : (a ? [a] : []);
  saveLS(LS_KEYS.activeJob, a.filter((x) => x.jobId !== jobId));
}

async function runOnceOnModal(workflowPrompt, outputNodeIds, ctx, submitGuard, batchInfo = null) {
  // batchInfo: {current, total}
  const batchSuffix = batchInfo ? `[${batchInfo.current}/${batchInfo.total}] ` : "";

  ctx.stage("submitting", batchSuffix + "POST /submit", false);

  const subRes = await api.fetchApi("/modal_bridge/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt: workflowPrompt, tier: getVramTier(workflowPrompt) }),
  });
  const sub = await subRes.json();
  if (!subRes.ok || !sub.ok) {
    throw new Error(sub.error || `HTTP ${subRes.status}`);
  }

  const jobId = sub.job_id;
  const gpu = sub.gpu;
  addActiveJob({ jobId, gpu, wfName: ctx.wfName, startedAt: Date.now() });
  // 这张卡的取消只取消这个 job(各 job 互不影响)
  ctx.setCancel(jobId, async () => {
    if (!confirm(`Cancel Modal job ${jobId.slice(0, 8)}?`)) return;
    try {
      await api.fetchApi("/modal_bridge/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId }),
      });
    } catch (e) { err("cancel failed", e); }
  });
  log("submitted", jobId, "gpu=" + gpu);

  ctx.stage("queued", `${batchSuffix}job=${jobId.slice(0, 8)} gpu=${gpu}`, true);

  const interval = getSetting("ModalBridge.pollIntervalSec", 1.2) * 1000;
  const timeoutMs = getSetting("ModalBridge.timeoutSec", 1200) * 1000;
  const deadline = Date.now() + timeoutMs;

  let final = null;
  let lastStatus = "queued";
  try {
    while (Date.now() < deadline) {
      await sleep(interval);
      let pData;
      try {
        const pRes = await api.fetchApi(`/modal_bridge/poll?job_id=${encodeURIComponent(jobId)}`);
        pData = await pRes.json();
      } catch (e) {
        log("poll error (will retry)", e);
        continue;
      }
      if (pData.error) {
        log("poll resp error:", pData);
        continue;
      }
      if (pData.status !== lastStatus) {
        lastStatus = pData.status;
        log("status →", pData.status);
        if (pData.status === "running") {
          ctx.stage("running", `${batchSuffix}inference (gpu=${gpu})`, true);
        } else if (pData.status === "queued") {
          ctx.stage("queued", `${batchSuffix}Waiting for worker...`, true);
        }
      }
      if (pData.status === "completed" || pData.status === "failed" || pData.status === "cancelled") {
        final = pData;
        break;
      }
    }
  } finally {
    removeActiveJob(jobId);
  }
  if (!final) throw new Error("Polling timed out");
  if (final.status === "cancelled") throw new Error("Job cancelled");
  if (final.status === "failed") {
    throw new Error(final.error || "Modal worker failed");
  }

  ctx.stage("downloading", `${batchSuffix}Decoding base64...`, false);
  const fetchRes = await api.fetchApi("/modal_bridge/fetch_result", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId, modal_state: final }),
  });
  const fetched = await fetchRes.json();
  if (!fetchRes.ok || !fetched.ok) {
    throw new Error(fetched.error || `fetch HTTP ${fetchRes.status}`);
  }
  // ComfyUI 单 graph:提交时的工作流当前在前台才能直接回填;在后台 tab 的先暂存,
  // 等用户切回该 tab 再渲染(图始终也在 output 里,一张不丢)。
  const sf = fetched.outputs?.[0]?.subfolder || "modal_results";
  const wfKey = submitGuard?.wfKey;
  const onFront = wfKey == null || activeWorkflowKey() === wfKey;
  if (onFront) {
    const placed = displayInGraph(outputNodeIds, fetched.outputs);
    if (!placed && fetched.outputs?.length) {
      notify(`图已存到 output/${sf}/(没找到对应输出节点)`, "warn");
    }
  } else if (fetched.outputs?.length) {
    storePendingResult(wfKey, outputNodeIds, fetched.outputs);
    notify(`✓ 后台工作流出图完成,切到该 tab 即显示(也已存 output/${sf}/)`, "info");
  }
  return { jobId, gpu, outputs: fetched.outputs };
}

// =====================================================================
// 模型自动同步(在 submit 前):本地 → Modal Volume
// 模型都在本地 ComfyUI Desktop 下好。提交前查 Volume 缺哪些:本地有的直接上传(块级
// 去重,通用大模型秒过);Volume 和本地都没有的 → 提示先在本地下好,允许强行提交。
// =====================================================================
async function ensureModelsAvailable(prompt, ctx) {
  ctx.stage("checking", "Scanning workflow + Modal Volume...");

  const checkRes = await api.fetchApi("/modal_bridge/check_models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  const check = await checkRes.json();
  if (check.error) throw new Error(check.error);

  const { required = [], present = [], missing_local = [], downloading = [], missing_no_source = [] } = check;
  ctx.stage("checking", `${present.length}/${required.length} 已在 Volume`);
  log(`models: required=${required.length} present=${present.length} missing_local=${missing_local.length} ` +
      `downloading=${downloading.length} missing_no_source=${missing_no_source.length}`);

  // 本地还在下载中的模型 → 不能传(会传成残缺),提示等下完
  if (downloading.length) {
    const list = downloading.map((u) => `  ${u.type}/${u.filename}`).join("\n");
    const msg = `这些模型本地还在下载中,现在传会传成残缺文件:\n\n${list}\n\n` +
                `建议:等本地下完再点 [☁️ Modal]。\n\n` +
                `仍然继续提交?(会缺这些模型,大概率失败)`;
    if (!confirm(msg)) throw new Error("Cancelled — 本地模型还在下载中");
  }

  // Volume 和本地都没有 → 没法自动补
  if (missing_no_source.length) {
    const list = missing_no_source.map((u) => `  ${u.type}/${u.filename}`).join("\n");
    const msg = `下面这些模型 Modal Volume 没有,本地也找不到,无法自动同步:\n\n${list}\n\n` +
                `解决:先在本地 ComfyUI 里把这些模型下到对应 models/<类型>/ 目录,再跑。\n\n` +
                `仍然继续提交?(大概率失败)`;
    if (!confirm(msg)) throw new Error("Cancelled — 缺本地模型");
  }

  if (!missing_local.length) {
    ctx.stage("checking", `模型齐全(${present.length}/${required.length})✓`);
    return;
  }

  // 本地有、Volume 没 → 上传
  const totalMb = missing_local.reduce((s, m) => s + (m.size_mb || 0), 0);
  const list = missing_local.map((m) => `  • ${m.type}/${m.filename} (${m.size_mb} MB)`).join("\n");
  const msg = `这些模型本地有、Modal Volume 还没有,需要上传一次(共 ~${totalMb} MB):\n\n${list}\n\n` +
              `点「确定」上传到 Volume(块级去重:网上通用大模型秒过,只有新内容真正占上行带宽;只这一次,之后秒进)。\n` +
              `点「取消」则不传,直接提交。`;
  if (!confirm(msg)) {
    return; // 用户选择不传,直接提交(可能失败,交给 Modal 报错)
  }

  ctx.stage("uploading", `上传 ${missing_local.length} 个模型到 Volume...`, false);
  ctx.bar(STATUS_PROGRESS.uploading[0]);
  const [a, b] = STATUS_PROGRESS.uploading;
  let tick = 0;
  const rc = await streamPost(
    "/modal_bridge/sync_models",
    { items: missing_local.map((m) => ({ type: m.type, filename: m.filename, local_path: m.local_path, size_mb: m.size_mb })) },
    (line) => {
      log("upload:", line);
      tick++;
      ctx.bar(a + Math.min(b - a - 1, tick * 0.6));
      ctx.stage("uploading", line.length > 72 ? line.slice(0, 72) + "…" : line);
    },
  );
  if (rc !== 0) throw new Error("模型上传失败(看进度窗日志 / ComfyUI 控制台)");
  ctx.stage("uploading", `${missing_local.length} 个模型已同步 ✓`, false);
}

// =====================================================================
// 主入口:批量包装
// =====================================================================
async function queueOnModal() {
  // 没部署/没配置就直接引导去 Setup,别走后面链路再失败
  const cfg0 = await fetchConfig();
  if (!isConfigured(cfg0)) {
    notify("还没部署到 Modal。先点右上角 [⚙️ Modal Setup] 填 token 一键部署(不用开终端)", "warn");
    try { openDeployDialog(); } catch (e) {}
    return;
  }
  // 每次点击 = 一个独立的 job 卡片(多 workflow 并发互不覆盖),标题带工作流名
  const ctx = newProgress("preparing", activeWorkflowName());
  ctx.stage("preparing", "Serializing graph...");
  try {
    const p = await app.graphToPrompt();
    const outputNodeIds = findOutputNodes(p.output);
    // 提交时记下当前工作流(tab)身份;结果回来时据此判断该直接回填还是暂存(等切回该 tab)
    const submitGuard = { wfKey: activeWorkflowKey() };
    log("output nodes:", outputNodeIds, "wfKey:", submitGuard.wfKey);

    // ⭐ custom_node 自动同步(默认开启,Settings 可关)
    const autoCheckNodes = getSetting("ModalBridge.autoCheckNodes", true);
    if (autoCheckNodes) {
      const proceed = await ensureNodesAvailable(p.output, ctx);
      if (!proceed) {
        ctx.finish(false, "✕ Cancelled");
        return;
      }
    }

    // ⭐ 模型自动同步:本地 → Volume(默认开启,Settings 可关)
    const autoSync = getSetting("ModalBridge.autoSyncModels", true);
    if (autoSync) {
      await ensureModelsAvailable(p.output, ctx);
    }

    const batchCount = Math.max(1, parseInt(getSetting("ModalBridge.batchCount", 1), 10));
    log("batch count:", batchCount);

    const allOutputs = [];
    for (let i = 0; i < batchCount; i++) {
      const seed = Date.now() % 2147483647 + i * 7919;
      const promptWithSeed = batchCount > 1 ? reseedPrompt(p.output, seed) : p.output;
      const result = await runOnceOnModal(
        promptWithSeed, outputNodeIds, ctx, submitGuard,
        batchCount > 1 ? { current: i + 1, total: batchCount } : null,
      );
      allOutputs.push(result);
    }

    ctx.finish(true, batchCount > 1 ? `✓ ${batchCount} done` : "✓ Done");
    notify(
      batchCount > 1
        ? `✓ ${batchCount} Modal jobs done`
        : `✓ Modal job ${allOutputs[0].jobId.slice(0, 8)} done`,
      "success",
    );
  } catch (e) {
    err(e);
    ctx.finish(false, "✗ " + (e.message || "Error").slice(0, 40), e.stack || e.toString());
    notify(`Failed: ${e.message}`, "error");
  }
}

// =====================================================================
// 健康检查 + 启动时检查未完成 job(history 持久化)
// =====================================================================
async function doHealthCheck() {
  try {
    const r = await api.fetchApi("/modal_bridge/health");
    const h = await r.json();
    log("health check", h);
  } catch (e) {
    err("health check failed", e);
  }
}

async function recoverPendingJob() {
  let pending = loadLS(LS_KEYS.activeJob);
  pending = Array.isArray(pending) ? pending : (pending ? [pending] : []);
  // 丢弃过期的(>20min),其余各自恢复(并行,每个一张卡)
  const fresh = pending.filter((j) => j?.jobId && (Date.now() - j.startedAt) / 1000 <= 1200);
  saveLS(LS_KEYS.activeJob, fresh);
  for (const j of fresh) recoverOne(j);
}

async function recoverOne(pending) {
  log("recovering pending job:", pending.jobId);
  const ctx = newProgress("queued", pending.wfName || null);
  ctx.stage("queued", `recover job=${pending.jobId.slice(0, 8)}`);
  try {
    const pRes = await api.fetchApi(`/modal_bridge/poll?job_id=${encodeURIComponent(pending.jobId)}`);
    const pData = await pRes.json();
    if (pData.status === "completed") {
      ctx.stage("downloading", "Fetching result of recovered job...");
      const fr = await api.fetchApi("/modal_bridge/fetch_result", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: pending.jobId, modal_state: pData }),
      });
      const fd = await fr.json();
      // 恢复时画板多半已不是当时的工作流 → 不强行回填,提示存盘路径
      if (fd.ok) {
        const sf = fd.outputs?.[0]?.subfolder || "modal_results";
        ctx.finish(true, "✓ Recovered");
        notify(`✓ Recovered job ${pending.jobId.slice(0, 8)} → output/${sf}/`, "success");
      } else {
        ctx.finish(false, "✗ Recover fetch failed", JSON.stringify(fd));
      }
    } else if (pData.status === "running" || pData.status === "queued") {
      ctx.stage(pData.status, `still ${pData.status};重新点 [☁️ Modal] 监控`);
      notify(`Job ${pending.jobId.slice(0, 8)} 仍在 ${pData.status};重新点 [☁️ Modal] 监控`, "warn");
      ctx.finish(true, `· ${pData.status}`);
    } else {
      ctx.finish(false, `✗ ${pData.status}`);
    }
  } catch (e) {
    err("recover failed", e);
    ctx.finish(false, "✗ recover error");
  } finally {
    removeActiveJob(pending.jobId);
  }
}

// =====================================================================
// 注册 ComfyUI Settings
// =====================================================================
const SETTINGS = [
  // 注:GPU 已统一为 H100→A100-80GB(原生 fallback),不再有显存档选项。
  {
    id: "ModalBridge.batchCount",
    name: "Modal Bridge: Batch count",
    type: "number",
    defaultValue: 1,
    attrs: { min: 1, max: 20, step: 1 },
    tooltip: "一次点击跑几次(自动改 seed)",
  },
  {
    id: "ModalBridge.pollIntervalSec",
    name: "Modal Bridge: Poll interval (sec)",
    type: "number",
    defaultValue: 1.2,
    attrs: { min: 0.5, max: 10, step: 0.1 },
    tooltip: "查询状态频率",
  },
  {
    id: "ModalBridge.timeoutSec",
    name: "Modal Bridge: Timeout (sec)",
    type: "number",
    defaultValue: 1200,
    attrs: { min: 60, max: 7200, step: 60 },
    tooltip: "单 job 最长等待",
  },
  {
    id: "ModalBridge.incognito",
    name: "Modal Bridge: Incognito (return base64, skip R2)",
    type: "boolean",
    defaultValue: true,
    tooltip: "关闭后图会上传到 R2(需要 modal_app R2 凭据)",
  },
  {
    id: "ModalBridge.autoSyncModels",
    name: "Modal Bridge: Auto-sync models (本地 → Volume)",
    type: "boolean",
    defaultValue: true,
    tooltip: "提交前检查 Modal Volume,工作流要、Volume 没、但本地有的模型自动上传上去(块级去重,通用大模型秒过)",
  },
  {
    id: "ModalBridge.autoCheckNodes",
    name: "Modal Bridge: Auto-sync custom nodes",
    type: "boolean",
    defaultValue: true,
    tooltip: "提交前把工作流用到的 custom_node 与本地双向同步到 Modal:缺的加、本地 commit 变的更新、本地已卸载的移除,再重部署",
  },
];

// =====================================================================
// GUI 一键部署对话框(零终端:后端自动 pip 装 modal → 建 secret → deploy → 写 config)
// =====================================================================
async function fetchConfig() {
  try {
    const r = await api.fetchApi("/modal_bridge/config");
    return await r.json();
  } catch (e) { return {}; }
}

function isConfigured(cfg) {
  return (
    typeof cfg?.modal_token_id === "string" && cfg.modal_token_id.startsWith("ak-") &&
    typeof cfg?.modal_endpoint_base === "string" && !cfg.modal_endpoint_base.includes("YOUR_WORKSPACE")
  );
}

let deployDialogEl = null;

async function openDeployDialog() {
  if (deployDialogEl) { deployDialogEl.style.display = "flex"; return; }
  const cfg = await fetchConfig();

  const overlay = document.createElement("div");
  deployDialogEl = overlay;
  Object.assign(overlay.style, {
    position: "fixed", inset: "0", zIndex: "10001",
    background: "rgba(0,0,0,0.5)", display: "flex",
    alignItems: "center", justifyContent: "center",
  });

  const panel = document.createElement("div");
  Object.assign(panel.style, {
    background: "#1e1e1e", color: "#eee", width: "560px", maxWidth: "92vw",
    maxHeight: "88vh", overflow: "auto", borderRadius: "10px", padding: "20px",
    boxShadow: "0 10px 40px rgba(0,0,0,0.5)", font: "13px/1.5 system-ui,sans-serif",
  });
  const inputCss =
    "width:100%;box-sizing:border-box;margin:4px 0 10px;padding:7px 9px;" +
    "background:#2a2a2a;border:1px solid #444;border-radius:6px;color:#eee;font:13px monospace;";
  panel.innerHTML = `
    <div style="font-size:16px;font-weight:600;margin-bottom:4px;">☁️ 部署到 Modal</div>
    <div style="color:#9aa;margin-bottom:14px;">
      全程在 ComfyUI 里完成,不用开终端。需要 Modal token(免费注册,送 $30):
      <a href="https://modal.com/settings/tokens" target="_blank" style="color:#6cf;">modal.com/settings/tokens</a>
    </div>
    <label>Workspace <span style="color:#9aa;">(modal.com 个人主页 URL 那段,如 lync5134)</span></label>
    <input id="mb-dep-ws" type="text" style="${inputCss}" value="${cfg.modal_workspace || ""}" placeholder="your-workspace">
    <label>Token ID <span style="color:#9aa;">(ak-...)</span></label>
    <input id="mb-dep-id" type="text" style="${inputCss}" value="${cfg.modal_token_id || ""}" placeholder="ak-xxxxxxxx">
    <label>Token Secret <span style="color:#9aa;">(as-...${cfg.has_token_secret ? ";已保存,留空=沿用" : ""})</span></label>
    <input id="mb-dep-secret" type="password" style="${inputCss}" value="" placeholder="${cfg.has_token_secret ? "••••••••(已保存,留空沿用)" : "as-xxxxxxxx"}">
    <div style="margin:4px 0 10px;color:#9aa;">GPU:H100 →(排不到)A100-80GB,所有工作流统一,无需选择。</div>
    <div style="margin:10px 0;">
      <button id="mb-dep-go" style="padding:8px 18px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">部署</button>
      <button id="mb-dep-test" style="padding:8px 14px;margin-left:8px;background:#374151;color:#ddd;border:none;border-radius:6px;cursor:pointer;">测试连接</button>
      <button id="mb-dep-close" style="padding:8px 14px;margin-left:8px;background:#333;color:#ccc;border:none;border-radius:6px;cursor:pointer;">关闭</button>
      <span id="mb-dep-status" style="margin-left:12px;color:#9aa;"></span>
    </div>
    <pre id="mb-dep-log" style="display:none;background:#111;border:1px solid #333;border-radius:6px;padding:10px;max-height:280px;overflow:auto;white-space:pre-wrap;font:11px/1.4 monospace;color:#bdbdbd;"></pre>

    <div style="margin-top:16px;border-top:1px solid #333;padding-top:12px;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <span style="font-weight:600;">管理云端节点</span>
        <button id="mb-nodes-load" style="padding:5px 12px;background:#374151;color:#ddd;border:none;border-radius:6px;cursor:pointer;font-size:12px;">加载镜像节点</button>
      </div>
      <div style="color:#9aa;margin-top:4px;font-size:12px;">
        勾选 = 从云端镜像<b>移除</b>该节点 + 重部署。⚠ 别的电脑若用到会失败、需重新加(多机各装一部分时慎删)。
      </div>
      <div id="mb-nodes-list" style="margin-top:8px;max-height:200px;overflow:auto;"></div>
      <div style="margin-top:8px;">
        <button id="mb-nodes-prune" style="display:none;padding:7px 14px;background:#7f1d1d;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">移除勾选项并重部署</button>
        <span id="mb-nodes-status" style="margin-left:10px;color:#9aa;font-size:12px;"></span>
      </div>
      <pre id="mb-nodes-log" style="display:none;background:#111;border:1px solid #333;border-radius:6px;padding:10px;margin-top:8px;max-height:200px;overflow:auto;white-space:pre-wrap;font:11px/1.4 monospace;color:#bdbdbd;"></pre>
    </div>
  `;
  overlay.appendChild(panel);
  document.body.appendChild(overlay);

  const close = () => { overlay.style.display = "none"; };
  panel.querySelector("#mb-dep-close").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  const goBtn = panel.querySelector("#mb-dep-go");
  const testBtn = panel.querySelector("#mb-dep-test");
  const statusEl = panel.querySelector("#mb-dep-status");
  const logEl = panel.querySelector("#mb-dep-log");

  // 测试连接:真打一次 Modal /health,查出"app 被删 / endpoint 不通 / key 不对"——
  // 这些光看本地 config 有没有 token 是查不出的(config 字段在不代表云端 app 还活着)。
  testBtn.onclick = async () => {
    testBtn.disabled = true;
    statusEl.textContent = "测试中(冷启动时可能要等几秒)...";
    statusEl.style.color = "#9aa";
    logEl.style.display = "block";
    logEl.textContent = "GET /modal_bridge/health …\n";
    try {
      const r = await api.fetchApi("/modal_bridge/health");
      const data = await r.json();
      // 只打 modal/error,不打 data.config(里面含 token)
      logEl.textContent += JSON.stringify(data.modal ?? { error: data.error ?? "unknown" }, null, 2) + "\n";
      logEl.scrollTop = logEl.scrollHeight;
      if (r.ok && data.ok && data.modal?.healthy) {
        const m = data.modal;
        const nodes = Array.isArray(m.custom_nodes) ? m.custom_nodes.length : "?";
        statusEl.textContent = `✓ 连接正常(warm=${m.warm_containers ?? 0}, 已装节点=${nodes})`;
        statusEl.style.color = "#34d399";
      } else {
        const why = data.error || "endpoint 不可达";
        statusEl.textContent = `✗ 连不上:${String(why).slice(0, 80)} — app 可能没部署/被删,请点「部署」`;
        statusEl.style.color = "#f87171";
      }
    } catch (e) {
      statusEl.textContent = "✗ 测试失败:" + (e.message || e);
      statusEl.style.color = "#f87171";
    } finally {
      testBtn.disabled = false;
    }
  };

  // ---- 管理云端节点(手动清理 / prune)----
  const nodesLoadBtn = panel.querySelector("#mb-nodes-load");
  const nodesListEl = panel.querySelector("#mb-nodes-list");
  const nodesPruneBtn = panel.querySelector("#mb-nodes-prune");
  const nodesStatusEl = panel.querySelector("#mb-nodes-status");
  const nodesLogEl = panel.querySelector("#mb-nodes-log");
  let loadedNodes = [];  // [{name,url,commit}]

  nodesLoadBtn.onclick = async () => {
    nodesLoadBtn.disabled = true;
    nodesStatusEl.textContent = "加载中...";
    nodesStatusEl.style.color = "#9aa";
    try {
      const r = await api.fetchApi("/modal_bridge/list_nodes");
      const d = await r.json();
      loadedNodes = d.nodes || [];
      if (!loadedNodes.length) {
        nodesListEl.innerHTML = `<div style="color:#9aa;font-size:12px;">镜像里没有 custom_node</div>`;
        nodesPruneBtn.style.display = "none";
      } else {
        nodesListEl.innerHTML = loadedNodes.map((n, i) =>
          `<label style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px;cursor:pointer;">
             <input type="checkbox" class="mb-node-cb" data-i="${i}">
             <span>${n.name}</span>
             ${n.in_local_baked ? "" : `<span style="color:#fbbf24;font-size:10px;">(本地无 git 信息)</span>`}
           </label>`).join("");
        nodesPruneBtn.style.display = "inline-block";
      }
      nodesStatusEl.textContent = `镜像实装 ${loadedNodes.length} 个(来源:${d.source})`;
      nodesStatusEl.style.color = "#9aa";
    } catch (e) {
      nodesStatusEl.textContent = "✗ 加载失败:" + (e.message || e);
      nodesStatusEl.style.color = "#f87171";
    } finally {
      nodesLoadBtn.disabled = false;
    }
  };

  nodesPruneBtn.onclick = async () => {
    const checked = [...panel.querySelectorAll(".mb-node-cb:checked")]
      .map((cb) => loadedNodes[parseInt(cb.dataset.i, 10)]);
    if (!checked.length) { nodesStatusEl.textContent = "没勾选任何节点"; return; }
    const removeNames = new Set(checked.map((n) => n.name));
    const keep = loadedNodes.filter((n) => !removeNames.has(n.name));
    const list = checked.map((n) => "  • " + n.name).join("\n");
    if (!confirm(`确定从云端镜像移除这 ${checked.length} 个节点并重部署?\n\n${list}\n\n` +
                 `⚠ 别的电脑若用到这些节点会失败,需要时重新加。`)) return;

    nodesPruneBtn.disabled = true;
    nodesStatusEl.textContent = "重部署中(约 1-3 分钟,别关窗口)...";
    nodesStatusEl.style.color = "#9aa";
    nodesLogEl.style.display = "block";
    nodesLogEl.textContent = "";
    try {
      const rc = await streamPost("/modal_bridge/sync_nodes", {
        new_baked: keep.map((n) => ({ name: n.name, url: n.url, commit: n.commit })),
        summary: { add: 0, update: 0, prune: checked.length },
      }, (line) => { nodesLogEl.textContent += line + "\n"; nodesLogEl.scrollTop = nodesLogEl.scrollHeight; });
      if (rc === 0) {
        nodesStatusEl.textContent = `✓ 已移除 ${checked.length} 个,镜像现 ${keep.length} 个`;
        nodesStatusEl.style.color = "#34d399";
        notify(`✓ 已从云端移除 ${checked.length} 个 custom_node`, "success");
        nodesLoadBtn.onclick();  // 刷新列表
      } else {
        nodesStatusEl.textContent = `✗ 重部署失败 rc=${rc}(看日志)`;
        nodesStatusEl.style.color = "#f87171";
      }
    } catch (e) {
      nodesStatusEl.textContent = "✗ " + (e.message || e);
      nodesStatusEl.style.color = "#f87171";
    } finally {
      nodesPruneBtn.disabled = false;
    }
  };

  goBtn.onclick = async () => {
    const payload = {
      workspace: panel.querySelector("#mb-dep-ws").value.trim(),
      token_id: panel.querySelector("#mb-dep-id").value.trim(),
      token_secret: panel.querySelector("#mb-dep-secret").value.trim(),
    };
    // token_secret 留空 = 沿用已存的(/config 不再回显它);只有填了才校验格式
    const secretOk = payload.token_secret === "" ? cfg.has_token_secret : payload.token_secret.startsWith("as-");
    if (!payload.token_id.startsWith("ak-") || !secretOk || !payload.workspace) {
      statusEl.textContent = cfg.has_token_secret
        ? "请填对 workspace + ak- token(secret 可留空沿用)"
        : "请填对 workspace + ak-/as- token";
      statusEl.style.color = "#f87171";
      return;
    }
    goBtn.disabled = true;
    statusEl.textContent = "部署中(首次拉镜像约 3-5 分钟,别关窗口)...";
    statusEl.style.color = "#9aa";
    logEl.style.display = "block";
    logEl.textContent = "";
    try {
      const rc = await streamPost("/modal_bridge/deploy", payload, (line) => {
        logEl.textContent += line + "\n";
        logEl.scrollTop = logEl.scrollHeight;
      });
      if (rc === 0) {
        statusEl.textContent = "✓ 部署成功!可以关掉这个窗口去出图了";
        statusEl.style.color = "#34d399";
        notify("✓ Modal 部署成功", "success");
        doHealthCheck();
      } else {
        statusEl.textContent = `✗ 部署失败 rc=${rc}(看上面日志)`;
        statusEl.style.color = "#f87171";
        notify(`Modal 部署失败 rc=${rc}`, "error");
      }
    } catch (e) {
      statusEl.textContent = "✗ " + (e.message || e);
      statusEl.style.color = "#f87171";
    } finally {
      goBtn.disabled = false;
    }
  };
}

// =====================================================================
// actionBarButtons 注册
// =====================================================================
const BUTTON_TOOLTIP = "Queue on Modal (默认 H100,见 Settings)";
const SETUP_TOOLTIP = "Modal Bridge 部署 / 设置(首次用先点这个)";

app.registerExtension({
  name: "ModalBridge.QueueButton",

  settings: SETTINGS,

  actionBarButtons: [
    {
      icon: "pi pi-cloud-upload",
      tooltip: BUTTON_TOOLTIP,
      label: "Modal",
      onClick: queueOnModal,
    },
    {
      icon: "pi pi-cog",
      tooltip: SETUP_TOOLTIP,
      label: "Modal Setup",
      onClick: openDeployDialog,
    },
  ],

  async setup() {
    log("setup() running");
    doHealthCheck();

    // 没配置过 → 提示去点 Setup 部署(零终端)
    fetchConfig().then((cfg) => {
      if (!isConfigured(cfg)) {
        notify("还没部署到 Modal。点右上角 [Modal Setup] 填 token 一键部署(不用开终端)", "warn");
      }
    });

    // 5 秒后检查 actionBarButtons 是否成功渲染
    setTimeout(() => {
      const found = document.querySelector(
        `button[aria-label="${BUTTON_TOOLTIP}"], button[title="${BUTTON_TOOLTIP}"]`
      );
      if (found) {
        log("✓ actionBarButtons rendered");
      } else {
        log("✗ actionBarButtons not rendered, no fallback");
      }
    }, 5000);

    // history 持久化:启动时检查未完成 job
    recoverPendingJob();
  },
});
