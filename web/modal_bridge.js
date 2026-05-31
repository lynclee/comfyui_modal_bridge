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

function displayInGraph(outputNodeIds, modalOutputs) {
  if (!outputNodeIds.length || !modalOutputs?.length) return;
  const imagesMeta = modalOutputs.map((o) => ({
    filename: o.filename,
    subfolder: o.subfolder,
    type: o.type || "output",
  }));
  app.nodeOutputs = app.nodeOutputs || {};
  for (const nid of outputNodeIds) {
    const node = app.graph.getNodeById(parseInt(nid, 10)) || app.graph.getNodeById(nid);
    if (!node) continue;
    app.nodeOutputs[node.id] = { images: imagesMeta };
    try { node.onExecuted?.({ images: imagesMeta }); } catch (e) {}
    try {
      api.dispatchEvent(new CustomEvent("executed", {
        detail: {
          node: String(nid), display_node: String(nid),
          output: { images: imagesMeta },
          prompt_id: "modal-bridge-" + Date.now(),
        },
      }));
    } catch (e) {}
  }
  try { app.graph.setDirtyCanvas(true, true); } catch (e) {}
}

// =====================================================================
// custom_node 同步
// 后端 /check_nodes 精确解析(本地 NODE_CLASS_MAPPINGS → custom_nodes 文件夹,
// 再和 Modal /list-nodes 权威比对),缺的就走 /add_nodes 一键加进镜像 + 重部署。
// 全程在 ComfyUI 里完成,不用开终端。
// =====================================================================
async function checkNodesOnModal(prompt) {
  const res = await api.fetchApi("/modal_bridge/check_nodes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!res.ok) throw new Error(`check_nodes HTTP ${res.status}`);
  return res.json(); // {missing, missing_no_git, unresolved, ok_builtin, ok_baked, source}
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

// 调 /add_nodes,流式推进 deploying 进度条,返回 deploy 是否成功
async function addMissingNodes(missing) {
  const [a, b] = STATUS_PROGRESS.deploying;
  let tick = 0;
  const rc = await streamPost(
    "/modal_bridge/add_nodes",
    { nodes: missing.map((m) => ({ folder: m.folder, url: m.url, commit: m.commit })) },
    (line) => {
      log("deploy:", line);
      tick++;
      setProgressBar(a + Math.min(b - a - 1, tick * 0.4)); // 缓慢逼近 band 末端
      updateStage("deploying", line.length > 72 ? line.slice(0, 72) + "…" : line);
    },
  );
  return rc === 0;
}

// submit 前调:缺 custom_node 就问用户要不要一键补。返回 true=可继续 / false=用户取消
async function ensureNodesAvailable(prompt) {
  updateStage("nodes", "Scanning workflow custom nodes...");
  let info;
  try {
    info = await checkNodesOnModal(prompt);
  } catch (e) {
    log("check_nodes failed, skip:", e);
    return true; // 检查本身失败不阻塞提交
  }

  const { missing = [], missing_no_git = [], unresolved = [], source } = info;
  if (unresolved.length) log("unresolved class_types (本地也没装):", unresolved);

  if (!missing.length && !missing_no_git.length) {
    updateStage("nodes", `nodes ok (${info.ok_baked} custom + ${info.ok_builtin} builtin)`);
    return true;
  }

  if (missing_no_git.length) {
    const list = missing_no_git
      .map((m) => `  ${m.folder} (${m.class_types.join(", ")})`).join("\n");
    notify(`这些 custom_node 没有 git 信息,无法自动补:\n${list}`, "warn");
  }

  if (!missing.length) {
    return confirm(`部分 custom_node 不在 Modal 且无法自动补,仍然提交?(很可能失败)`);
  }

  const lines = missing.map((m) =>
    `  • ${m.folder}  (${m.class_types.length} 个节点)\n     ${m.url}` +
    (m.commit ? ` @ ${m.commit.slice(0, 8)}` : "")
  ).join("\n");
  const msg =
    `工作流用到以下 custom_node,Modal 镜像(${source})里还没有:\n\n${lines}\n\n` +
    `点「确定」一键加进 Modal 镜像并重新部署(约 1-3 分钟,只这一次,之后秒进)。\n` +
    `点「取消」则不补,直接提交。`;
  if (!confirm(msg)) {
    return confirm("不补节点,直接提交?(大概率失败)");
  }

  updateStage("deploying", "Redeploying Modal image with new nodes...", true);
  setProgressBar(STATUS_PROGRESS.deploying[0]);
  const ok = await addMissingNodes(missing);
  if (!ok) {
    throw new Error("Modal 重部署失败(看进度窗 deploy 日志 / ComfyUI 控制台)");
  }
  updateStage("deploying", "✓ image updated", false);
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
// 进度浮窗
// =====================================================================
let progressEl = null;
let progressTimer = null;
let progressStartMs = 0;
let currentJobId = null;
let currentCancelable = false;
let seedController = null;   // seeding 阶段的下载 AbortController(供取消按钮中断)

const STATUS_PROGRESS = {
  preparing:   [0,  2],    // 序列化 workflow(纯前端,几百 ms)
  nodes:       [2,  5],    // 检查 custom_node Modal 是否齐全
  deploying:   [5, 35],    // (仅需补节点时)重 build 镜像并部署,1-3 分钟
  checking:    [35, 40],   // 检查 Modal Volume 已有哪些模型
  seeding:     [40, 78],   // 下载缺失模型到 Volume
  submitting:  [78, 82],
  queued:      [82, 85],
  running:     [85, 96],
  downloading: [96, 99],   // 出图 base64 回流
  done:        [100, 100],
  failed:      [100, 100],
};

function ensureProgressEl() {
  if (progressEl) return progressEl;
  progressEl = document.createElement("div");
  progressEl.id = "modal-bridge-progress";

  // 应用上次保存的位置
  const savedPos = loadLS(LS_KEYS.progressPos);
  const top = savedPos?.top ?? 60;
  const right = savedPos?.right ?? 20;

  progressEl.style.cssText = `
    position: fixed; top: ${top}px; right: ${right}px;
    z-index: 99999; min-width: 280px; max-width: 360px;
    padding: 10px 14px;
    background: rgba(28, 28, 36, 0.96);
    color: #fff;
    border-radius: 10px;
    font-size: 12px;
    font-family: -apple-system, system-ui, sans-serif;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    display: none;
  `;
  progressEl.innerHTML = `
    <div id="mb-drag-handle" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;cursor:move;user-select:none;">
      <div id="mb-progress-label" style="font-weight:600;display:flex;align-items:center;gap:4px;">☁️ Modal</div>
      <div style="display:flex;align-items:center;gap:8px;">
        <div id="mb-progress-time" style="opacity:0.7;font-variant-numeric:tabular-nums;font-size:11px;">0s</div>
        <button id="mb-cancel-btn" title="Cancel" style="
          all:unset;cursor:pointer;color:#ef4444;font-size:14px;font-weight:bold;
          padding:0 4px;border-radius:3px;display:none;
        ">✕</button>
      </div>
    </div>
    <div style="height:4px;background:rgba(255,255,255,0.1);border-radius:2px;overflow:hidden;">
      <div id="mb-progress-bar" style="
        height:100%;width:0%;
        background:linear-gradient(90deg,#6366f1,#8b5cf6);
        border-radius:2px;
        transition:width 0.4s cubic-bezier(0.4, 0, 0.2, 1);
      "></div>
    </div>
    <div id="mb-progress-detail" style="margin-top:6px;opacity:0.6;font-size:11px;word-break:break-all;"></div>
    <div id="mb-error-toggle" style="display:none;margin-top:4px;font-size:10px;color:#fca5a5;cursor:pointer;text-decoration:underline;">
      ▶ Show error details
    </div>
    <pre id="mb-error-detail" style="display:none;margin:4px 0 0;padding:6px;background:rgba(0,0,0,0.3);border-radius:4px;
      font-size:10px;max-height:160px;overflow:auto;white-space:pre-wrap;"></pre>
  `;
  document.body.appendChild(progressEl);

  // 绑定拖动
  setupDrag(progressEl, progressEl.querySelector("#mb-drag-handle"));

  // 绑定取消
  progressEl.querySelector("#mb-cancel-btn").onclick = async (ev) => {
    ev.stopPropagation();
    // seeding 阶段:中断模型下载
    if (seedController) {
      if (!confirm("取消正在进行的模型下载?(已下好的会保留;正在下的那个 Modal 端可能继续下完,下次直接缓存)")) return;
      seedController.abort();
      return;
    }
    // run 阶段:取消 Modal job
    if (!currentJobId || !currentCancelable) return;
    if (!confirm(`Cancel Modal job ${currentJobId.slice(0, 8)}?`)) return;
    try {
      await api.fetchApi("/modal_bridge/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: currentJobId }),
      });
      finishProgress(false, "✕ Cancelled");
      clearLS(LS_KEYS.activeJob);
    } catch (e) {
      err("cancel failed", e);
    }
  };

  // 错误展开
  const errToggle = progressEl.querySelector("#mb-error-toggle");
  errToggle.onclick = () => {
    const detail = progressEl.querySelector("#mb-error-detail");
    const isOpen = detail.style.display === "block";
    detail.style.display = isOpen ? "none" : "block";
    errToggle.textContent = isOpen ? "▶ Show error details" : "▼ Hide error details";
  };

  return progressEl;
}

function setupDrag(el, handle) {
  let startX, startY, startTop, startRight;
  let dragging = false;
  handle.addEventListener("mousedown", (e) => {
    if (e.target.id === "mb-cancel-btn") return;
    dragging = true;
    startX = e.clientX; startY = e.clientY;
    const rect = el.getBoundingClientRect();
    startTop = rect.top;
    startRight = window.innerWidth - rect.right;
    e.preventDefault();
  });
  document.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX, dy = e.clientY - startY;
    const top = Math.max(0, startTop + dy);
    const right = Math.max(0, startRight - dx);
    el.style.top = `${top}px`;
    el.style.right = `${right}px`;
    el.style.left = "auto";
  });
  document.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    const top = parseInt(el.style.top, 10);
    const right = parseInt(el.style.right, 10);
    saveLS(LS_KEYS.progressPos, { top, right });
  });
}

function setProgressBar(pct) {
  const bar = document.getElementById("mb-progress-bar");
  if (bar) bar.style.width = `${pct}%`;
}

function showProgress(stage, detail = "", cancelable = false) {
  ensureProgressEl();
  progressEl.style.display = "block";
  document.getElementById("mb-progress-label").innerHTML = `☁️ Modal · ${stage}`;
  document.getElementById("mb-progress-detail").textContent = detail;
  document.getElementById("mb-error-toggle").style.display = "none";
  document.getElementById("mb-error-detail").style.display = "none";
  currentCancelable = cancelable;
  document.getElementById("mb-cancel-btn").style.display = cancelable ? "inline-block" : "none";

  if (!progressTimer) {
    progressStartMs = Date.now();
    progressTimer = setInterval(() => {
      const sec = (Date.now() - progressStartMs) / 1000;
      document.getElementById("mb-progress-time").textContent = `${sec.toFixed(1)}s`;
    }, 100);
  }
}

function updateStage(stageKey, detail = null, cancelable = null) {
  if (!progressEl) ensureProgressEl();
  const labels = {
    preparing: "Preparing", nodes: "Checking nodes", deploying: "Deploying image",
    checking: "Checking models", seeding: "Downloading models", submitting: "Submitting",
    queued: "Queued", running: "Running",
    downloading: "Downloading result", done: "Done", failed: "Failed",
  };
  document.getElementById("mb-progress-label").innerHTML = `☁️ Modal · ${labels[stageKey] || stageKey}`;
  if (detail !== null) document.getElementById("mb-progress-detail").textContent = detail;
  if (cancelable !== null) {
    currentCancelable = cancelable;
    document.getElementById("mb-cancel-btn").style.display = cancelable ? "inline-block" : "none";
  }

  // 把进度条平滑滑到该阶段区间的中点
  const range = STATUS_PROGRESS[stageKey];
  if (range) setProgressBar((range[0] + range[1]) / 2);

  // running 阶段:在区间内随时间渐进
  if (stageKey === "running") {
    const [a, b] = range;
    const stageStartMs = Date.now();
    if (progressTimer) clearInterval(progressTimer);
    progressTimer = setInterval(() => {
      const totalSec = (Date.now() - progressStartMs) / 1000;
      document.getElementById("mb-progress-time").textContent = `${totalSec.toFixed(1)}s`;
      const stageSec = (Date.now() - stageStartMs) / 1000;
      const pct = a + Math.min(b - a, (stageSec / 60) * (b - a));  // 60s 占满 running 区间
      setProgressBar(pct);
    }, 200);
  }
}

function finishProgress(success, finalLabel = "✓ Done", errDetail = null) {
  if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
  if (!progressEl) ensureProgressEl();
  setProgressBar(100);
  const bar = document.getElementById("mb-progress-bar");
  bar.style.background = success
    ? "linear-gradient(90deg,#10b981,#22c55e)"
    : "linear-gradient(90deg,#ef4444,#f97316)";
  document.getElementById("mb-progress-label").innerHTML = `☁️ Modal · ${finalLabel}`;
  const sec = (Date.now() - progressStartMs) / 1000;
  document.getElementById("mb-progress-time").textContent = `${sec.toFixed(1)}s`;
  document.getElementById("mb-cancel-btn").style.display = "none";

  if (errDetail) {
    document.getElementById("mb-error-toggle").style.display = "block";
    document.getElementById("mb-error-detail").textContent = errDetail;
  }

  currentJobId = null;
  currentCancelable = false;
  clearLS(LS_KEYS.activeJob);

  // 4 秒后淡出(成功才自动隐藏;失败留着让用户看)
  if (success) {
    setTimeout(() => { if (progressEl) progressEl.style.display = "none"; }, 4000);
  }
}

// =====================================================================
// 单次跑(submit + poll + fetch_result)
// =====================================================================
// 显存档 → GPU 优先级(后端各档 worker 用 @app.cls(gpu=[...]) 原生 fallback)
//   80g: H100 → A100-80GB     40g: L40S → H100
// 判断:估算工作流里所有模型权重总大小,>~26GB 走 80g 档,否则 40g。
// 估算来自文件名特征(前端拿不到字节数);命中大模型/大权重特征 = 高显存。

// 单个模型≈"重"(需要大显存)的文件名特征(主要是 diffusion/checkpoint/大 text encoder)
const HEAVY_MODEL_PATTERNS = [
  { re: /flux[-_ ]?2[-_ ]?dev/i,        gb: 32 },  // flux2 dev fp8mixed ~32G
  { re: /flux[-_ ]?1[-_ ]?dev/i,        gb: 24 },  // flux1 dev
  { re: /flux[-_ ]?1[-_ ]?krea/i,       gb: 24 },
  { re: /flux\b/i,                      gb: 24 },   // 其它 flux 全尺寸
  { re: /qwen[-_ ]?image/i,             gb: 20 },   // qwen-image 全家
  { re: /mistral.*flux2/i,              gb: 24 },   // flux2 的大 text encoder(bf16 ~34G)
  { re: /t5xxl.*fp16/i,                 gb: 10 },
  { re: /wan/i,                         gb: 28 },   // WAN 视频
  { re: /hunyuan/i,                     gb: 24 },
];
const TIER_80G_THRESHOLD_GB = 26;  // 总权重超这个 → 80G 档

const MODEL_FIELDS = ["unet_name", "ckpt_name", "model_name", "clip_name",
  "clip_name1", "clip_name2", "clip_name3", "vae_name", "lora_name", "style_model_name"];

function estimateWorkflowVramGb(prompt) {
  let total = 0;
  const seen = new Set();
  for (const n of Object.values(prompt || {})) {
    const ins = n?.inputs || {};
    for (const f of MODEL_FIELDS) {
      const v = ins[f];
      if (typeof v !== "string" || !v || seen.has(v)) continue;
      seen.add(v);
      const hit = HEAVY_MODEL_PATTERNS.find((p) => p.re.test(v));
      if (hit) total += hit.gb;
    }
  }
  return total;
}

// 返回 "80g" / "40g":① 设置强制档优先;② 否则按估算总显存
function getVramTier(prompt) {
  const forced = getSetting("ModalBridge.vramTier", "auto");
  if (forced === "80g" || forced === "40g") {
    log(`vram tier=${forced} (forced by setting)`);
    return forced;
  }
  const gb = estimateWorkflowVramGb(prompt);
  const tier = gb >= TIER_80G_THRESHOLD_GB ? "80g" : "40g";
  log(`vram estimate ~${gb}GB → tier=${tier} (${tier === "80g" ? "H100→A100-80G" : "L40S→H100"})`);
  return tier;
}

async function runOnceOnModal(workflowPrompt, outputNodeIds, batchInfo = null) {
  // batchInfo: {current, total}
  const batchSuffix = batchInfo ? `[${batchInfo.current}/${batchInfo.total}] ` : "";

  showProgress("Submitting", batchSuffix + "POST /submit", false);
  updateStage("submitting", batchSuffix + "POST /submit", false);

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
  currentJobId = jobId;
  saveLS(LS_KEYS.activeJob, { jobId, gpu, startedAt: Date.now() });
  log("submitted", jobId, "gpu=" + gpu);

  updateStage("queued", `${batchSuffix}job=${jobId.slice(0, 8)} gpu=${gpu}`, true);

  const interval = getSetting("ModalBridge.pollIntervalSec", 1.2) * 1000;
  const timeoutMs = getSetting("ModalBridge.timeoutSec", 1200) * 1000;
  const deadline = Date.now() + timeoutMs;

  let final = null;
  let lastStatus = "queued";
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
        updateStage("running", `${batchSuffix}H100 inference (gpu=${gpu})`, true);
      } else if (pData.status === "queued") {
        updateStage("queued", `${batchSuffix}Waiting for worker...`, true);
      }
    }
    if (pData.status === "completed" || pData.status === "failed" || pData.status === "cancelled") {
      final = pData;
      break;
    }
  }
  if (!final) throw new Error("Polling timed out");
  if (final.status === "cancelled") throw new Error("Job cancelled");
  if (final.status === "failed") {
    throw new Error(final.error || "Modal worker failed");
  }

  updateStage("downloading", `${batchSuffix}Decoding base64...`, false);
  const fetchRes = await api.fetchApi("/modal_bridge/fetch_result", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId, modal_state: final }),
  });
  const fetched = await fetchRes.json();
  if (!fetchRes.ok || !fetched.ok) {
    throw new Error(fetched.error || `fetch HTTP ${fetchRes.status}`);
  }
  displayInGraph(outputNodeIds, fetched.outputs);
  return { jobId, gpu, outputs: fetched.outputs };
}

// =====================================================================
// 从工作流 MarkdownNote 的 "Model links" 自动提取模型来源
// ComfyUI 官方模板约定:note 里写 [文件名](https://huggingface.co/REPO/resolve/REV/PATH)
// 解析出来 → 动态来源,不依赖手维护的 registry
// 返回 {filename: {source, repo?, hf_filename?, url?, requires_token}}
// =====================================================================
function parseModelLinks(workflow) {
  const out = {};
  const nodes = (workflow && workflow.nodes) || [];
  // [name.ext](url) — 文件名带常见模型扩展名
  const reLink = /\[([^\]\/]+\.(?:safetensors|sft|gguf|pth|ckpt|bin|pt))\]\((https?:\/\/[^)]+)\)/gi;
  for (const n of nodes) {
    if (n.type !== "MarkdownNote") continue;
    const wv = n.widgets_values;
    const text = Array.isArray(wv) ? wv.join("\n") : (typeof wv === "string" ? wv : "");
    if (!text) continue;
    let m;
    while ((m = reLink.exec(text)) !== null) {
      const fname = m[1].trim();
      const url = m[2].trim();
      // https://huggingface.co/OWNER/REPO/resolve/REV/PATH → repo + 路径(走 hf_hub_download,自动处理 token)
      const hf = url.match(/huggingface\.co\/([^\/]+\/[^\/]+)\/resolve\/[^\/]+\/(.+)$/);
      if (hf) {
        out[fname] = { source: "huggingface", repo: hf[1], hf_filename: decodeURIComponent(hf[2]), requires_token: true };
      } else {
        out[fname] = { source: "url", url, requires_token: false };
      }
    }
  }
  return out;
}

// =====================================================================
// 模型自动同步(在 submit 前)
// 来源三级:① 工作流 note 自带 link  ② model_registry.yaml  ③ 都没有→报 unknown
// =====================================================================
async function ensureModelsAvailable(prompt, workflow) {
  updateStage("checking", "Scanning workflow + Modal Volume...");
  const links = parseModelLinks(workflow);
  const nLinks = Object.keys(links).length;
  if (nLinks) log(`workflow note 自带 ${nLinks} 个模型来源`);

  const checkRes = await api.fetchApi("/modal_bridge/check_models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  const check = await checkRes.json();
  if (check.error) throw new Error(check.error);

  const { required, missing, present, unknown } = check;
  updateStage("checking", `${present.length}/${required.length} cached on Modal`);

  // 组装待下载:missing(note 优先于 registry) + unknown 里 note 能补来源的
  const toSeed = [];
  for (const m of missing) {
    const entry = links[m.filename] || m.registry_entry; // note 优先
    toSeed.push({ type: m.type, filename: m.filename, registry_entry: entry, src: links[m.filename] ? "note" : "registry" });
  }
  const stillUnknown = [];
  for (const u of unknown) {
    if (links[u.filename]) {
      toSeed.push({ type: u.type, filename: u.filename, registry_entry: links[u.filename], src: "note" });
    } else {
      stillUnknown.push(u);
    }
  }
  log(`models: required=${required.length} present=${present.length} toSeed=${toSeed.length} unknown=${stillUnknown.length}`);

  // 真正没来源的(note + registry 都没写):必须手动
  if (stillUnknown.length) {
    const list = stillUnknown.map(u => `  ${u.type}/${u.filename}`).join("\n");
    const msg = `下面这些模型 Modal 没有,工作流 note 和 registry 也都没写来源,无法自动下:\n\n${list}\n\n` +
                `解决:\n` +
                `  1. 用带「## Model links」note 的官方模板(会自动识别来源)\n` +
                `  2. 或在 model_registry.yaml 补来源\n\n` +
                `仍然继续提交?(会失败)`;
    if (!confirm(msg)) throw new Error("Cancelled due to unknown models");
  }

  if (!toSeed.length) {
    updateStage("checking", `All ${required.length} models present ✓`);
    return;
  }

  // 串行下载(避免单 endpoint 并发争抢),每个在 seeding 区间内分一段
  const seedRange = STATUS_PROGRESS.seeding;
  const perModelPct = (seedRange[1] - seedRange[0]) / toSeed.length;
  for (let i = 0; i < toSeed.length; i++) {
    const m = toSeed[i];
    // cancelable=true → 显示取消按钮(seeding 阶段可中断)
    updateStage("seeding", `[${i+1}/${toSeed.length}] ${m.filename} — downloading on Modal... (源:${m.src})`, true);
    setProgressBar(seedRange[0] + i * perModelPct);

    seedController = new AbortController();
    let seedRes;
    try {
      seedRes = await api.fetchApi("/modal_bridge/seed_model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: m.type, filename: m.filename, registry_entry: m.registry_entry }),
        signal: seedController.signal,
      });
    } catch (e) {
      if (e.name === "AbortError") throw new Error(`已取消(下载到第 ${i+1}/${toSeed.length} 个 ${m.filename})`);
      throw e;
    } finally {
      seedController = null;
    }
    const seedData = await seedRes.json();
    if (!seedRes.ok || !seedData.ok) {
      const errMsg = seedData.error || `HTTP ${seedRes.status}`;
      throw new Error(`Seed ${m.filename} failed: ${errMsg}`);
    }
    const note = seedData.cached
      ? `cached (${seedData.size_mb}MB)`
      : `done (${seedData.size_mb}MB in ${seedData.elapsed_sec}s)`;
    log(`seed ${m.filename}: ${note}`);
    setProgressBar(seedRange[0] + (i + 1) * perModelPct);
  }

  seedController = null;
  updateStage("seeding", `${toSeed.length} model(s) seeded ✓`, false);
}

// =====================================================================
// 主入口:批量包装
// =====================================================================
async function queueOnModal() {
  try {
    showProgress("Preparing", "Serializing graph...");
    updateStage("preparing", "Serializing graph...");

    const p = await app.graphToPrompt();
    const outputNodeIds = findOutputNodes(p.output);
    log("output nodes:", outputNodeIds);

    // ⭐ custom_node 自动同步(默认开启,Settings 可关)
    const autoCheckNodes = getSetting("ModalBridge.autoCheckNodes", true);
    if (autoCheckNodes) {
      const proceed = await ensureNodesAvailable(p.output);
      if (!proceed) {
        finishProgress(false, "✕ Cancelled");
        return;
      }
    }

    // ⭐ 模型自动同步(默认开启,Settings 可关)
    const autoSeed = getSetting("ModalBridge.autoSeedModels", true);
    if (autoSeed) {
      await ensureModelsAvailable(p.output, p.workflow);
    }

    const batchCount = Math.max(1, parseInt(getSetting("ModalBridge.batchCount", 1), 10));
    log("batch count:", batchCount);

    const allOutputs = [];
    for (let i = 0; i < batchCount; i++) {
      const seed = Date.now() % 2147483647 + i * 7919;
      const promptWithSeed = batchCount > 1 ? reseedPrompt(p.output, seed) : p.output;
      const result = await runOnceOnModal(
        promptWithSeed, outputNodeIds,
        batchCount > 1 ? { current: i + 1, total: batchCount } : null,
      );
      allOutputs.push(result);
    }

    finishProgress(true, batchCount > 1 ? `✓ ${batchCount} done` : "✓ Done");
    notify(
      batchCount > 1
        ? `✓ ${batchCount} Modal jobs done`
        : `✓ Modal job ${allOutputs[0].jobId.slice(0, 8)} done`,
      "success",
    );
  } catch (e) {
    err(e);
    finishProgress(false, "✗ " + (e.message || "Error").slice(0, 40), e.stack || e.toString());
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
  const pending = loadLS(LS_KEYS.activeJob);
  if (!pending?.jobId) return;
  const age = (Date.now() - pending.startedAt) / 1000;
  if (age > 1200) {
    log("dropping stale pending job:", pending.jobId);
    clearLS(LS_KEYS.activeJob);
    return;
  }
  log("recovering pending job:", pending.jobId);
  showProgress("Recovering", `job=${pending.jobId.slice(0, 8)}`);
  currentJobId = pending.jobId;
  try {
    const pRes = await api.fetchApi(`/modal_bridge/poll?job_id=${encodeURIComponent(pending.jobId)}`);
    const pData = await pRes.json();
    if (pData.status === "completed") {
      updateStage("downloading", "Fetching result of recovered job...");
      const fr = await api.fetchApi("/modal_bridge/fetch_result", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: pending.jobId, modal_state: pData }),
      });
      const fd = await fr.json();
      if (fd.ok) {
        finishProgress(true, "✓ Recovered");
        notify(`✓ Recovered job ${pending.jobId.slice(0, 8)}`, "success");
      } else {
        finishProgress(false, "✗ Recover fetch failed", JSON.stringify(fd));
      }
    } else if (pData.status === "running" || pData.status === "queued") {
      updateStage(pData.status, "Resuming poll...");
      // 简化:不重新进入完整 poll loop,告诉用户去手动检查
      notify(`Job ${pending.jobId.slice(0, 8)} still ${pData.status}; please re-click Modal to monitor`, "warn");
      progressEl.style.display = "none";
      clearLS(LS_KEYS.activeJob);
    } else {
      finishProgress(false, `✗ ${pData.status}`);
    }
  } catch (e) {
    err("recover failed", e);
    clearLS(LS_KEYS.activeJob);
  }
}

// =====================================================================
// 注册 ComfyUI Settings
// =====================================================================
const SETTINGS = [
  {
    id: "ModalBridge.vramTier",
    name: "Modal Bridge: 显存档 / GPU",
    type: "combo",
    options: ["auto", "80g", "40g"],
    defaultValue: "auto",
    tooltip: "auto=按工作流模型自动估算显存档(推荐)。" +
      "80g=H100→A100-80GB(大模型如 flux2 dev);40g=L40S→H100(z-image/klein 等)。" +
      "每档自带 Modal 原生 GPU fallback,主卡排不到自动降级,不干等。",
  },
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
    id: "ModalBridge.autoSeedModels",
    name: "Modal Bridge: Auto-seed missing models",
    type: "boolean",
    defaultValue: true,
    tooltip: "提交前检查 Modal Volume,缺的模型自动从 HF/URL 下载(走 registry 白名单)",
  },
  {
    id: "ModalBridge.autoCheckNodes",
    name: "Modal Bridge: Auto-check custom nodes",
    type: "boolean",
    defaultValue: true,
    tooltip: "提交前检查工作流用到的 custom_node,Modal 镜像没有的可一键加进去并重部署",
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

  const gpuOpts = ["H100", "A100-80GB", "A100", "L40S", "A10G"]
    .map((g) => `<option value="${g}"${(cfg.default_gpu || "H100") === g ? " selected" : ""}>${g}</option>`)
    .join("");

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
    <label>Token Secret <span style="color:#9aa;">(as-...)</span></label>
    <input id="mb-dep-secret" type="password" style="${inputCss}" value="${cfg.modal_token_secret || ""}" placeholder="as-xxxxxxxx">
    <label>HuggingFace Token <span style="color:#9aa;">(可选,下 FLUX 等私有模型才需要)</span></label>
    <input id="mb-dep-hf" type="password" style="${inputCss}" placeholder="hf_xxxxxxxx(留空=只能下公开模型)">
    <label>默认 GPU</label>
    <select id="mb-dep-gpu" style="${inputCss}">${gpuOpts}</select>
    <div style="margin:10px 0;">
      <button id="mb-dep-go" style="padding:8px 18px;background:#2563eb;color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600;">部署</button>
      <button id="mb-dep-close" style="padding:8px 14px;margin-left:8px;background:#333;color:#ccc;border:none;border-radius:6px;cursor:pointer;">关闭</button>
      <span id="mb-dep-status" style="margin-left:12px;color:#9aa;"></span>
    </div>
    <pre id="mb-dep-log" style="display:none;background:#111;border:1px solid #333;border-radius:6px;padding:10px;max-height:280px;overflow:auto;white-space:pre-wrap;font:11px/1.4 monospace;color:#bdbdbd;"></pre>
  `;
  overlay.appendChild(panel);
  document.body.appendChild(overlay);

  const close = () => { overlay.style.display = "none"; };
  panel.querySelector("#mb-dep-close").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  const goBtn = panel.querySelector("#mb-dep-go");
  const statusEl = panel.querySelector("#mb-dep-status");
  const logEl = panel.querySelector("#mb-dep-log");

  goBtn.onclick = async () => {
    const payload = {
      workspace: panel.querySelector("#mb-dep-ws").value.trim(),
      token_id: panel.querySelector("#mb-dep-id").value.trim(),
      token_secret: panel.querySelector("#mb-dep-secret").value.trim(),
      hf_token: panel.querySelector("#mb-dep-hf").value.trim(),
      default_gpu: panel.querySelector("#mb-dep-gpu").value,
    };
    if (!payload.token_id.startsWith("ak-") || !payload.token_secret.startsWith("as-") || !payload.workspace) {
      statusEl.textContent = "请填对 workspace + ak-/as- token";
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
