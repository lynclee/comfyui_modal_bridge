# 部署指南 / Setup Guide

> 中文 | [English](#english)

把当前 ComfyUI 工作流一键推到 Modal Serverless GPU(H100)上跑,出图回流本地。
**模型在本地 ComfyUI Desktop 下好**,提交时自动把云端缺的同步上去(本地 → Modal Volume,块级去重,通用大模型秒过)。custom_node 自动同步。

## TL;DR

```
# 1. 装插件(二选一)
#    A) ComfyUI Manager 搜 "Modal Bridge" 安装
#    B) git clone https://github.com/lynclee/comfyui_modal_bridge 到 custom_nodes/
# 2. 重启 ComfyUI → 右上角点 [⚙️ Modal Setup] 填 token 部署(零终端)
```

## 准备:Modal 账号

1. 注册 Modal(送 $30/月免费额度,需绑卡):https://modal.com
2. 拿 **Token ID**(`ak-…`)+ **Token Secret**(`as-…`):https://modal.com/settings/tokens
3. 记下 **workspace 名**(modal.com 个人主页 URL 那段,如 `your-workspace`)。

## 方式 A:GUI 部署(推荐,零终端)

点右上角 **[⚙️ Modal Setup]** → 填 Workspace / Token ID / Token Secret → **部署**。
背后:后端自动 `pip install modal` → 建 Secret(随机生成私有鉴权 key `BRIDGE_API_KEY`)→ `modal deploy` → 写 config → 验证 health。首次拉镜像约 3-5 分钟。

- **测试连接**:Setup 里有「测试连接」按钮,ping 一次 health,确认 app 是否真活着(光看本地有没有 token 查不出 app 是否被删)。
- **Token Secret 已保存后**:重新部署时可留空,自动沿用已存的(`/config` 不会把 secret 回显到前端)。

## 方式 B:命令行部署

```bash
cd ~/Documents/ComfyUI/custom_nodes/comfyui_modal_bridge
pip install modal
python deploy.py --workspace <你的workspace> --token-id ak-xxx --token-secret as-xxx
# token 也可走环境变量 MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
```

## 出图 / 视频 / 3D(点 [RunModal] 之后)

1. 序列化当前工作流
2. **版本 + 显卡校验**:插件版本与云端不一致、或所选显卡与云端实际在跑的卡不一致 → 拦住并引导重新部署
3. **API 节点检测**:工作流含 ComfyUI API 节点但没配 comfy.org key → 提交前提示(否则云端会 401)
4. **custom_node 同步**:工作流用到、云端没有的节点 → 自动加 + 重部署(只这一次,之后秒进)
5. **显存预检**:模型总显存(按类别估,视频含多帧激活开销)超所选卡 → 弹警告(可仍要跑 / 去换卡)
6. **必填输入预检**:按当前本地节点定义检查各节点是否缺必填输入(典型:老工作流里的节点在新版新增了必填 widget,如内置 API 节点的 `generate_type`,老图没带上)→ 弹提示(可仍要提交 / 去修),避免等云端 `execute() missing required argument` 才报错。拿不到定义的节点跳过,不误报
7. **模型同步**:云端 Volume 缺、本地有 → 上传(CAS 去重);云端和本地都没有 → 提示先在本地下好
8. **CPU / GPU 路由**:工作流无本地模型(纯 API)→ CPU 容器(账单≈0);要 sample → GPU 容器
9. 提交 Modal → 轮询 → 小文件 base64 / 大文件走 Volume 直连 → 写 `output/modal_results/<job_id>/` → 按来源节点回填:SaveImage 出图、SaveVideo 出视频、**SaveGLB / Preview3D 出 3D 转盘**

## GPU 模式(Auto 省钱 / H100 固定 / B200 固定)

Modal Setup 里选三种模式之一:

- **Auto(更省钱,默认)**:提交时按工作流估算显存,自动选**最省又够用**的卡:
  - 小图(如 **Z-Image-Turbo**,est ~18–24G)→ **L40S 48G**(最便宜)
  - 常规(如 **FLUX.2-dev**,est ~71–80G)→ **H100/A100-80G**
  - 真超 80G(大视频 / fp16 大模型叠大 stack)→ **B200 183G**(防 OOM,Blackwell 最强档)
- **H100(固定)**:一律 H100,不降也不升。`>80G` 的工作流会在 RunModal 前显存预警,提示切 Auto 或 B200。
- **B200(固定 · 最快最强)**:一律 B200,显存最大(183G)、速度最快。适合大图 / 视频 / 赶时间、或想要全程顶配,代价是**最贵**那档。

四档 worker(CPU / L40S / H100 / B200)**一次部署全部建好**,空闲各自 scale-to-zero —— 没被路由到的档 **0 容器 = 0 成本**,所以多档不额外花钱。切换模式后点「部署」生效(首次升级到本版本也需部署一次)。

> 显存估算 = 工作流引用的模型文件总大小 × 类别系数(图像 ×1.15;视频 ×1.3 + 多帧激活开销);本地查不到大小的模型按"稳妥"留在主卡,不乱降也不乱升。

## 图 / 视频 / 3D 输出 + 画板预览

bridge 扫工作流**所有输出节点**取产物并回填画板:
- **SaveImage / SaveVideo / SaveWEBM**:图、视频,画板预览;
- **SaveGLB**(`3d` 键)/ **Preview3D**(`result` 键):3D 网格,**画板内渲染可转动的转盘**;
- 产物按**来源节点**回填,多个输出节点各归各、不串台。
- **大文件**(视频 / 网格 >8MB)走 **Volume 直连取回**(worker 写进 Volume → 本地 SDK 直接下),绕开 base64/modal.Dict 体积上限;小文件仍 base64。阈值 `config.volume_threshold_mb`(默认 8,换值需重新部署)。

## CPU / GPU 自动路由(省钱)

提交时后端按"工作流有没有引用本地模型"判要不要 GPU:
- **无本地模型**(纯 API / 轻节点)→ **CPU 容器**,GPU 账单≈0;
- **有模型要 sample** → GPU 容器。

进度卡会显示 `CPU` 或显卡名。CPU worker 用同一镜像、`--cpu` 跑(ComfyUI CPU 模式)。

## ComfyUI API 节点(Kling / Luma / Tripo / OpenAI…)

工作流含 API 节点时需要 comfy.org 的 API key(platform.comfy.org 生成):Setup 里填「**comfy.org API Key**」→ 部署时进云端 Secret,worker 跑 API 节点时从 `extra_data` 注入鉴权。
- ⚠ 账单走**你的 comfy.org 额度**;
- 前端检测到工作流有 API 节点但没配 key,会在提交前提示。

## 模型策略(本地 → Volume)

- 模型在本地下好(放 `models/<类型>/`)。`unet/` 与 `diffusion_models/`、`clip/` 与 `text_encoders/` 互为别名,放哪个都认。
- 提交时本地用 modal SDK 查 Volume、传缺的。不经任何 endpoint、不从 HF 下载、不依赖 registry。
- 一次性整体推送:`python sync_models.py [--dry-run] [--type loras]`。
- 为什么快:Modal Volume 块级去重(CAS),通用大模型很多人传过 → 你这边秒过,只有自训练/私有模型才真占上行带宽。

## custom_node(多机)

点 [RunModal] 自动加工作流需要的节点。多台电脑各装一部分时:**只增不删,镜像 = 各机并集**,互不干扰。
想清理:Setup →「管理云端节点」→ 勾选要移除的 → 移除并重部署(带"别的机器用到会失败"二次确认)。

## ComfyUI 版本跟随 + 节点兼容自检

**版本跟随**:部署时自动 `import comfyui_version` 读本机 ComfyUI 版本,云端镜像 clone **同一个 tag**,让"本地能跑的节点云端也能跑"。
- 本机版本无对应 git tag(Desktop 偶尔跑在两个 tag 之间)→ **取最接近的 tag(平手取更老,不让云端比本地新)+ 部署日志警告**,不中止。
- 本机升级 ComfyUI 后 → 点 RunModal 会**警告提示重新部署**让云端跟上(非硬拦,本次照常出图)。
- health 回报 `deployed_comfyui_tag`;改 tag 会重 build clone 层及之后。兜底默认 `v0.22.0`。

**节点兼容自检**:每次部署成功后,自动在云端**同一镜像**(便宜 GPU)里 boot 一次 ComfyUI,解析 ComfyUI 自己打印的 `(IMPORT FAILED)` 标记,逐个报告自定义节点导入成功 / 失败。
- 失败 = 与当前 ComfyUI 版本不兼容 / 缺依赖 / commit 坏。
- **只警告不阻断**:结果串进部署日志,坏节点不影响其它工作流;修好(本地换版本 / commit)后重新部署即可。
- 也可手动跑:`cd modal_app && python -m modal run node_compat_check.py`。

## Modal 端 endpoint(4 个,私有,自建 key 鉴权)

```
https://<ws>--comfyui-bridge-run.modal.run     (POST 跑 workflow)
https://<ws>--comfyui-bridge-status.modal.run  (GET 查状态)
https://<ws>--comfyui-bridge-cancel.modal.run  (POST 取消)
https://<ws>--comfyui-bridge-health.modal.run  (GET 健康 + 已装 custom_nodes)
```

模型查/传全走本地 modal SDK,所以不需要 list/check/seed 这些 endpoint。

## 文件结构

```
comfyui_modal_bridge/
├── __init__.py            注册 web + routes
├── config.py             config.json 读写
├── routes.py             本地 /modal_bridge/* 路由
├── modal_client.py       调 Modal 4 endpoint(私有鉴权)
├── modal_volume.py       本地 SDK 操作 Volume(查 + 传模型)
├── node_sync.py          custom_node 同步规划 + 部署命令
├── sync_models.py        命令行:本地模型整体同步到 Volume
├── deploy.py             命令行部署(= GUI [Modal Setup])
├── web/modal_bridge.js   前端按钮 + 进度卡片 + 同步流程
├── tests/test_core.py    核心纯函数单测
├── categories.py        工作流类别画像(图/视频:显存估算 + 超时)
└── modal_app/
    ├── modal_app.py          Modal app(4 endpoint + GPU worker + CPU worker + 路由)
    ├── modal_image.py        镜像 DSL(读 _custom_nodes_data,缺失自愈)
    ├── _custom_nodes_data.py 镜像要装的 custom_nodes 清单(本地状态,.gitignore)
    ├── _comfy_ws.py          容器内跑 ComfyUI + 取图/视频/3D(大文件写 Volume)
    ├── snapshot_bench.py     内存快照隔离 bench(按 GPU 档验证用)
    └── extra_model_paths.yaml Volume 模型路径
```

## Settings(齿轮面板)

`Batch count` / `Poll interval` / `Timeout` / `Incognito` / `Auto-sync models` / `Auto-sync custom nodes` / `Memory snapshot`(内存快照,加速冷启 ~30s→~5s,实验;改后需在 Setup 重新部署生效)。

---

<a name="english"></a>

# Setup Guide (English)

> [中文](#部署指南--setup-guide) | English

Push the current ComfyUI workflow to a Modal Serverless GPU (H100) with one click; results flow back locally.
**Download models locally in ComfyUI Desktop**; on submit, anything missing in the cloud is auto-synced (local → Modal Volume, block dedup, common big models are instant). Custom nodes auto-sync too.

## TL;DR

```
# 1. Install (either)
#    A) ComfyUI Manager → search "Modal Bridge" → Install
#    B) git clone https://github.com/lynclee/comfyui_modal_bridge into custom_nodes/
# 2. Restart ComfyUI → click [⚙️ Modal Setup], enter token, deploy (no terminal)
```

## Prereq: Modal account

1. Sign up (free $30/mo, card required): https://modal.com
2. Get **Token ID** (`ak-…`) + **Token Secret** (`as-…`): https://modal.com/settings/tokens
3. Note your **workspace name** (the segment in your modal.com profile URL, e.g. `your-workspace`).

## Option A: GUI deploy (recommended, no terminal)

Click **[⚙️ Modal Setup]** → fill Workspace / Token ID / Token Secret → **Deploy**.
Behind the scenes: backend auto `pip install modal` → create Secret (random `BRIDGE_API_KEY`) → `modal deploy` → write config → verify health. First image pull ~3-5 min.

- **Test connection**: the "Test connection" button pings health to confirm the app is actually alive (having a token locally doesn't mean the app still exists).
- **After secret is saved**: leave it blank on redeploy to reuse the stored one (`/config` never returns the secret to the frontend).

## Option B: CLI deploy

```bash
cd ~/Documents/ComfyUI/custom_nodes/comfyui_modal_bridge
pip install modal
python deploy.py --workspace <your-workspace> --token-id ak-xxx --token-secret as-xxx
# token may also come from env MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
```

## Generating images / video / 3D (after clicking [RunModal])

1. Serialize the current workflow
2. **Version + GPU check**: if the plugin version differs from the cloud, or the selected GPU differs from the one actually running → block and guide a redeploy
3. **API node check**: workflow has ComfyUI API nodes but no comfy.org key configured → warn before submit (otherwise the cloud 401s)
4. **Node sync**: nodes the workflow uses but the cloud lacks → auto-add + redeploy (one time)
5. **VRAM preflight**: model VRAM (estimated per category; video includes multi-frame activations) over the selected GPU → warn (run anyway / switch GPU)
6. **Required-input preflight**: check each node against its current local definition for missing required inputs (typically an old workflow whose node gained a new required widget in a newer version, e.g. a built-in API node's `generate_type`, that the old graph didn't carry) → warn (submit anyway / go fix), instead of failing on the cloud with `execute() missing required argument`. Nodes whose definition can't be read are skipped (no false positives)
7. **Model sync**: missing on Volume but present locally → upload (CAS dedup); missing both places → prompt to download locally first
8. **CPU / GPU routing**: no local model (pure API) → CPU container (bill ≈ 0); needs sampling → GPU container
9. Submit Modal → poll → small files base64 / large files via direct Volume → write `output/modal_results/<job_id>/` → fill back per source node: SaveImage for images, SaveVideo for video, **SaveGLB / Preview3D for a 3D turntable**

## GPU mode (Auto = cheaper / H100 = fixed / B200 = fastest)

Pick one of three modes in Modal Setup:

- **Auto (cheaper, default)**: on submit, estimate the workflow's VRAM and pick the **cheapest GPU that fits**:
  - small images (e.g. **Z-Image-Turbo**, est ~18–24G) → **L40S 48G** (cheapest)
  - normal (e.g. **FLUX.2-dev**, est ~71–80G) → **H100/A100-80G**
  - truly over 80G (big video / fp16 model + heavy stack) → **B200 183G** (avoids OOM, top Blackwell tier)
- **H100 (fixed)**: always H100, no downgrade/escalation. Workflows `>80G` get a VRAM warning before RunModal suggesting you switch to Auto or B200.
- **B200 (fixed · fastest)**: always B200 — biggest VRAM (183G) and fastest. For large images / video / rush jobs, or when you want top-tier throughout; the trade-off is it's the **most expensive** tier.

All four workers (CPU / L40S / H100 / B200) are **deployed at once** and each scales to zero when idle — an un-routed tier is **0 containers = $0**, so extra tiers cost nothing. Click Deploy to apply a mode change (and once when upgrading to this version).

> VRAM estimate = total size of the workflow's referenced model files × a category factor (image ×1.15; video ×1.3 + multi-frame activation overhead). Models whose size can't be found locally stay on the primary card ("safe") — no wrong downgrade or escalation.

## Images / video / 3D output + canvas preview

The bridge collects outputs from **every output node** and fills them back on the canvas:
- **SaveImage / SaveVideo / SaveWEBM**: images, video, previewed on the canvas;
- **SaveGLB** (`3d` key) / **Preview3D** (`result` key): 3D meshes, **rendered as a rotatable turntable on the canvas**;
- outputs are filled back **per source node** — multiple output nodes each get their own, no cross-bleed.
- **Large files** (video / meshes >8MB) come back via **direct Volume download** (worker writes to the Volume → local SDK pulls it), bypassing the base64/modal.Dict size ceiling; small files stay base64. Threshold: `config.volume_threshold_mb` (default 8, redeploy to change).

## CPU / GPU auto-routing (cost saving)

On submit the backend decides whether a GPU is needed by "does the workflow reference a local model":
- **No local model** (pure API / lightweight) → **CPU container**, GPU bill ≈ 0;
- **Has a model to sample** → GPU container.

The progress card shows `CPU` or the GPU name. The CPU worker uses the same image launched with `--cpu` (ComfyUI CPU mode).

## ComfyUI API nodes (Kling / Luma / Tripo / OpenAI…)

Workflows with API nodes need a comfy.org API key (generated at platform.comfy.org): enter **"comfy.org API Key"** in Setup → it goes into the cloud Secret on deploy, and the worker injects it via `extra_data` when running API nodes.
- ⚠ Billed to **your comfy.org credits**;
- the frontend warns before submit if the workflow has API nodes but no key is configured.

## Model strategy (local → Volume)

- Download models locally (`models/<type>/`). `unet/`↔`diffusion_models/` and `clip/`↔`text_encoders/` are aliases — either works.
- On submit, the local modal SDK lists the Volume and uploads what's missing. No endpoint, no HF download, no registry.
- Bulk push: `python sync_models.py [--dry-run] [--type loras]`.
- Why fast: Modal Volume block-level dedup (CAS) — common big models others have uploaded are instant for you; only custom/private models actually use your upstream bandwidth.

## Custom nodes (multi-machine)

Clicking [RunModal] auto-adds nodes the workflow needs. Across machines that each install a subset: **add-only, image = union**, no cross-deletion.
To clean up: Setup → "Manage cloud nodes" → check the ones to remove → remove & redeploy (with a "other machines using it will fail" confirmation).

## ComfyUI version follow + node compatibility self-check

**Version follow**: at deploy time it reads your local ComfyUI version (`import comfyui_version`) and clones the **same tag** into the cloud image, so nodes that work locally work in the cloud.
- No exact git tag for your local version (Desktop sometimes runs between tags) → **use the nearest tag (ties pick the older one, never newer than local) + a deploy-log warning**, not aborted.
- After you upgrade local ComfyUI → RunModal **warns and suggests a redeploy** to let the cloud catch up (non-blocking; the current run still proceeds).
- health reports `deployed_comfyui_tag`; changing the tag rebuilds the clone layer onward. Default fallback `v0.22.0`.

**Node compatibility self-check**: after every successful deploy it boots ComfyUI once in the **same cloud image** (on a cheap GPU), parses ComfyUI's own `(IMPORT FAILED)` markers, and reports each custom node's import OK / FAILED.
- Failed = incompatible with the current ComfyUI version / missing deps / bad commit.
- **Warn-only, never blocks**: results stream into the deploy log; a broken node doesn't affect other workflows; fix it (bump version / commit locally) and redeploy.
- Run manually too: `cd modal_app && python -m modal run node_compat_check.py`.

## Modal endpoints (4, private, self-issued key auth)

```
https://<ws>--comfyui-bridge-run.modal.run     (POST run workflow)
https://<ws>--comfyui-bridge-status.modal.run  (GET status)
https://<ws>--comfyui-bridge-cancel.modal.run  (POST cancel)
https://<ws>--comfyui-bridge-health.modal.run  (GET health + installed custom_nodes)
```

Model list/upload all go through the local modal SDK, so no list/check/seed endpoints are needed.

## File layout

```
comfyui_modal_bridge/
├── __init__.py            register web + routes
├── config.py             config.json read/write
├── routes.py             local /modal_bridge/* routes
├── modal_client.py       call the 4 Modal endpoints (private auth)
├── modal_volume.py       local SDK over the Volume (list + upload models)
├── node_sync.py          custom_node sync planning + deploy commands
├── sync_models.py        CLI: bulk-sync local models to the Volume
├── deploy.py             CLI deploy (= GUI [Modal Setup])
├── web/modal_bridge.js   frontend buttons + progress cards + sync flow
├── tests/test_core.py    unit tests for core pure functions
└── modal_app/
    ├── modal_app.py          Modal app (4 endpoints + GPU worker + CPU worker + routing)
    ├── modal_image.py        image DSL (reads _custom_nodes_data, self-heals if absent)
    ├── _custom_nodes_data.py custom_nodes baked into the image (local state, .gitignore)
    ├── _comfy_ws.py          run ComfyUI in-container + fetch images/video/3D (large files to Volume)
    ├── snapshot_bench.py     isolated memory-snapshot bench (verify per GPU tier)
    └── extra_model_paths.yaml Volume model paths
```

## Settings (gear panel)

`Batch count` / `Poll interval` / `Timeout` / `Incognito` / `Auto-sync models` / `Auto-sync custom nodes` / `Memory snapshot` (cuts cold start ~30s→~5s, experimental; redeploy in Setup to apply).
