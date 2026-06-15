# comfyui_modal_bridge

> 中文 | [English](#english)

**ComfyUI Desktop 插件:一键把当前工作流推到你自己的 Modal Serverless GPU 上跑,图 / 视频 / 3D 回流本地画板。** 本地不用好显卡、不用开终端、不用搭云端 ComfyUI —— 装上、填一次 token、点一下,就跑。

> Registry: `comfyui_modal_bridge`(publisher `lynclee`)· 在 ComfyUI Manager 搜 **Modal Bridge** 即可安装。

## ✨ 核心优势

- 🖥️ **不挑机器,本地零显卡要求** — Mac、轻薄本、核显本都行。FLUX.2 这种吃显存的大模型,**算力全在云端 GPU**(Auto 模式按显存自动选 L40S/H100/H200,省钱),本地只负责发起工作流、收图。本地再弱也能跑 flux2,不用为了跑图换电脑。
- ⚡ **多任务并发** — 多个工作流可同时提交、同时跑,各有独立进度卡片(可拖动 / 取消 / 关闭),互不阻塞、互不覆盖。
- 🚀 **全自动部署,零终端** — GUI 填一次 Modal token,后端自动 `pip install modal`、建密钥、`modal deploy`、写配置。全程不碰命令行,首次拉镜像约 3-5 分钟,之后秒进。
- 🧩 **custom node 自动同步** — 工作流用到的自定义节点,云端镜像没有就**自动装进镜像并重部署**;多台机器各装一部分时取**并集、互不删**,换机无缝。
- 🎨 **图 / 视频 / 3D 全支持** — SaveImage / SaveVideo / SaveGLB / Preview3D 的产物都回流本地,并直接回填画板预览(3D 出可转动的网格);大文件(视频 / 网格)自动走 Volume 直连取回,不受 base64 体积限制。
- 🤖 **API 节点 + 自动省钱** — 工作流含 ComfyUI API 节点(Kling / Luma / Tripo / OpenAI 等)也能跑(Setup 填一次 comfy.org key);**没有本地模型的纯 API 工作流自动路由到 CPU 容器,GPU 账单≈0**。
- 💰 **按秒计费,空闲归零** — 用你自己的 Modal 账号(注册送 $30/月额度,需绑卡),**不出图不花钱**,闲置自动缩到零。可选「内存快照」把冷启从 ~30s 降到 ~5s。

## 它解决什么

本地没有大显存 GPU(Mac / 轻薄本 / 4090 显存不够跑 flux2),又不想自己搭一套云端 ComfyUI、不想折腾 Docker 和命令行。装上这个插件,点一下 **RunModal**,当前工作流就在你自己 Modal 账号的 GPU 上跑完,图回到本地 SaveImage 节点。

## 关键特性(细节)

- **零终端部署**:点 `⚙️ Modal Setup` 填 Modal token → 后端自动 `pip install modal`、建 Secret、`modal deploy`、写配置并验证 health。
- **不挑本地机器**:本地只做序列化 + 上传 + 收图,**不跑推理**,所以对本地显卡/显存无要求;Mac / Windows / Linux 一致(子进程串流部署日志,绕开 Windows 事件循环坑)。
- **GPU 两种模式 + 自动按显存选卡(省钱)**:Modal Setup 里选 **Auto(更省钱,默认)** 或 **H100(固定)**。Auto 按工作流估算显存自动选最省又够用的卡 —— 小图(如 **Z-Image-Turbo**)走 **L40S**、常规(如 **FLUX.2-dev**)走 **H100/A100**、真超 80G 才上 **H200**(防 OOM);H100 模式则一律 H100。点 RunModal 前还会按类别估算显存预警(视频含多帧激活)。四档 worker(CPU/L40S/H100/H200)**一次部署全部建好**,空闲各自 scale-to-zero,**没被路由到的档 0 成本**。
- **图 / 视频 / 3D 输出 + 画板预览**:扫工作流所有输出节点收产物 —— SaveImage/SaveVideo 出图、视频,**SaveGLB / Preview3D 出 3D 网格并在画板内渲染转盘**(按来源节点回填,多输出不串台);大文件(视频 / 网格 >8MB)走 **Volume 直连取回**,绕开 base64/Dict 体积上限,小文件仍 base64。
- **CPU / GPU 自动路由**:提交时判断工作流要不要 GPU(有没有引用本地模型)—— **纯 API / 无模型的轻工作流自动落 CPU 容器(账单≈0)**,要 sample 的才上 GPU。
- **ComfyUI API 节点**:工作流含 API 节点(Kling/Luma/Tripo/OpenAI…)时,Setup 填的 comfy.org API key 经云端 Secret 注入鉴权;前端检测到 API 节点但没配 key 会提前提示(账单走你的 comfy.org 额度)。
- **更快冷启(可选)**:Setting 开「内存快照」后,容器冷启从 ~30s 降到 ~5s(experimental,按 GPU 档需自测;CPU worker 用 GA 的 CPU 快照,稳)。
- **多任务并发 & 进度**:多工作流并发各有独立进度卡片(可拖动/取消/关闭);上传带速率 + ETA;job 状态自动清理,不会互相覆盖。
- **custom_node 自动同步 + 多机友好**:自动加工作流需要的节点并重部署(只这一次);多机取并集、互不删;清理走 Setup 的「管理云端节点」手动勾选。
- **云端 ComfyUI 版本跟随本机**:部署时自动检测本机 ComfyUI 版本,云端镜像 clone **同一个 tag**(本机版本无对应 tag 时取最接近的,只警告不中止);本机升级后点 RunModal 会提示重新部署让云端跟上 —— 本地能跑的节点云端基本就能跑。
- **节点兼容自检(每次部署)**:部署成功后自动在云端**同镜像**里 boot 一次 ComfyUI,逐个报告自定义节点导入成功 / 失败(失败 = 与该 ComfyUI 版本不兼容 / 缺依赖 / commit 坏)。**只警告不阻断**:坏节点不影响其它工作流。
- **模型本地 → Volume**:模型在本地 ComfyUI 下好,提交时自动把云端缺的传上去;**块级去重(CAS)让通用大模型秒过**,只有自训练/私有模型才真占上行带宽。不从 HF 下载、不依赖手维护的 registry。
- **私有鉴权**:endpoint 用自建 `BRIDGE_API_KEY` 校验,只有你的 key 能调用,无 key 一律 401。

## 工作流程(点 [RunModal] 之后)

```
ComfyUI Desktop(本地,不挑机器)
  │ graphToPrompt() 序列化当前工作流
  ▼
custom_node 同步   工作流用到、云端镜像没有的节点 → 自动加 + 重部署(只这一次)
  ▼
模型同步          工作流要的模型,云端 Volume 没、本地有 → 用 modal SDK 直传 Volume
  │              (CAS 块级去重:网上通用大模型秒过)
  ▼
路由             无本地模型(纯 API)→ CPU 容器;要 sample → GPU 容器
  ▼
提交 Modal /run → 轮询 → 小文件 base64 / 大文件走 Volume 直连 → 写 output/modal_results/<job_id>/
  ▼
按来源节点回填画板:SaveImage 出图、SaveVideo 出视频、SaveGLB / Preview3D 出 3D 转盘
```

## 安装

- **方式一(推荐)**:ComfyUI Manager → Custom Nodes Manager → 搜 `Modal Bridge` → 安装 → 重启。
- **方式二**:`git clone https://github.com/lynclee/comfyui_modal_bridge` 到 `ComfyUI/custom_nodes/`,重启。

装好后点右上角 `⚙️ Modal Setup`,填 Workspace / Token 部署。详见 [SETUP.md](./SETUP.md)。

## 安全

- `config.json`(含 token)和 `secrets.toml` 在 `.gitignore` 里,**绝不进仓库**。
- Modal endpoint 私有,自建 key 鉴权,无 key 一律 401。

## License

MIT

---

<a name="english"></a>

# comfyui_modal_bridge (English)

> [中文](#comfyui_modal_bridge) | English

**A ComfyUI Desktop plugin: push the current workflow to your own Modal Serverless GPU with one click; images / video / 3D flow back to your local canvas.** No good GPU locally, no terminal, no self-hosted cloud ComfyUI — install, enter a token once, click, done.

> Registry: `comfyui_modal_bridge` (publisher `lynclee`) · Search **Modal Bridge** in ComfyUI Manager to install.

## ✨ Why use it

- 🖥️ **Runs on any machine — zero local GPU required.** Mac, thin laptops, iGPU-only — all fine. VRAM-hungry models like FLUX.2 run **entirely on a cloud GPU** (Auto mode picks L40S/H100/H200 by VRAM to save cost); your machine only serializes the workflow and receives images. Run flux2 on a potato.
- ⚡ **Multi-task concurrency.** Submit and run multiple workflows at once — each gets its own progress card (draggable / cancelable / closable), no blocking, no clobbering.
- 🚀 **Fully automatic deploy, zero terminal.** Enter your Modal token once in the GUI; the backend auto `pip install modal`, creates the secret, runs `modal deploy`, writes config. Never touch the command line. First image-pull ~3-5 min, instant afterward.
- 🧩 **Custom nodes auto-sync.** Nodes your workflow uses but the cloud lacks are **auto-baked into the image and redeployed**; across machines the image is the **union, never cross-deleted** — switch machines seamlessly.
- 🎨 **Images / video / 3D — all supported.** Outputs from SaveImage / SaveVideo / SaveGLB / Preview3D flow back locally and render right on the canvas (3D shows a rotatable mesh); large files (video / meshes) are pulled back directly via the Volume, free of the base64 size ceiling.
- 🤖 **API nodes + auto cost-saving.** Workflows with ComfyUI API nodes (Kling / Luma / Tripo / OpenAI, etc.) run too (enter a comfy.org key once in Setup); **pure-API workflows with no local model auto-route to a CPU container — GPU bill ≈ 0**.
- 💰 **Per-second billing, scales to zero.** Uses your own Modal account ($30/mo free credit, card required); **you pay nothing when not generating**, idle scales to zero. Optional memory snapshot cuts cold start from ~30s to ~5s.

## What it solves

You don't have a big-VRAM GPU locally (Mac / thin laptop / a 4090 that can't fit flux2), and you don't want to stand up a full cloud ComfyUI or fight Docker and the CLI. Install this, click **RunModal**, and the current workflow runs on a GPU in *your own* Modal account, with the image returned to your local SaveImage.

## Highlights (details)

- **Zero-terminal deploy**: click `⚙️ Modal Setup`, enter your Modal token → the backend auto `pip install modal`, creates the Secret, runs `modal deploy`, writes config, verifies health.
- **Machine-agnostic local side**: locally it only serializes + uploads + receives — **no inference** — so it has no requirement on your GPU/VRAM; consistent across Mac / Windows / Linux (streams deploy logs via a subprocess to dodge the Windows event-loop pitfall).
- **Two GPU modes + auto VRAM-based card pick (cost-saving)**: in Modal Setup choose **Auto (cheaper, default)** or **H100 (fixed)**. Auto estimates each workflow's VRAM and picks the cheapest card that fits — small images (e.g. **Z-Image-Turbo**) → **L40S**, normal (e.g. **FLUX.2-dev**) → **H100/A100**, truly over 80G → **H200** (avoids OOM); H100 mode always uses H100. Before running, VRAM is estimated per category (video includes multi-frame activations) and warned on. All four workers (CPU/L40S/H100/H200) are **deployed at once**, each scales to zero when idle — **an un-routed tier costs $0**.
- **Images / video / 3D output + canvas preview**: collects outputs from every output node — SaveImage/SaveVideo for images/video, **SaveGLB / Preview3D for 3D meshes rendered as a turntable on the canvas** (routed per source node, no cross-bleed across multiple outputs); large files (video / meshes >8MB) come back via **direct Volume download**, bypassing the base64/Dict size ceiling, small ones stay base64.
- **CPU / GPU auto-routing**: on submit, detect whether the workflow needs a GPU (does it reference a local model) — **pure-API / model-less lightweight workflows auto-land on a CPU container (bill ≈ 0)**, only sampling ones go to GPU.
- **ComfyUI API nodes**: for workflows with API nodes (Kling/Luma/Tripo/OpenAI…), the comfy.org API key entered in Setup is injected from the cloud Secret; the frontend warns up front if API nodes are present but no key is configured (billed to your comfy.org credits).
- **Faster cold start (optional)**: enable "Memory snapshot" in Settings to cut container cold start from ~30s to ~5s (experimental, verify per GPU tier; the CPU worker uses the GA CPU snapshot, reliable).
- **Multi-task concurrency & progress**: each concurrent workflow gets its own progress card (draggable / cancelable / closable); uploads show rate + ETA; job state auto-cleans without clobbering.
- **Custom-node auto-sync & multi-machine**: auto-adds nodes the workflow needs and redeploys (one time); across machines it's the **union, never cross-deleted**; cleanup is manual via "Manage cloud nodes" in Setup.
- **Cloud ComfyUI version follows your machine**: at deploy time it detects your local ComfyUI version and clones the **same tag** into the cloud image (no exact tag → nearest one, warn-only); after you upgrade locally, RunModal nudges you to redeploy so the cloud catches up — nodes that work locally work in the cloud.
- **Node compatibility self-check (every deploy)**: after a successful deploy it boots ComfyUI once in the **same cloud image** and reports each custom node's import OK / FAILED (failed = incompatible with that ComfyUI version / missing deps / bad commit). **Warn-only, never blocks** — a broken node doesn't affect other workflows.
- **Local → Volume models**: download models locally; missing ones upload on submit; **block-level dedup (CAS) makes common big models instant** — only custom/private models actually use upstream bandwidth. No HF download, no hand-maintained registry.
- **Private auth**: endpoints verify a self-issued `BRIDGE_API_KEY`; only your key can call them, missing key always returns 401.

## Flow (after clicking [RunModal])

```
ComfyUI Desktop (local, any machine)
  │ graphToPrompt() serializes the current workflow
  ▼
node sync      Nodes the workflow uses but the cloud image lacks → auto-add + redeploy (one time)
  ▼
model sync     Models the workflow needs, missing on the Volume but present locally
  │            → uploaded directly via modal SDK (CAS block dedup: common big models are instant)
  ▼
routing        no local model (pure API) → CPU container; needs sampling → GPU container
  ▼
submit Modal /run → poll → small files base64 / large files via direct Volume → write output/modal_results/<job_id>/
  ▼
fill back per source node: SaveImage for images, SaveVideo for video, SaveGLB / Preview3D for a 3D turntable
```

## Install

- **Option 1 (recommended)**: ComfyUI Manager → Custom Nodes Manager → search `Modal Bridge` → Install → restart.
- **Option 2**: `git clone https://github.com/lynclee/comfyui_modal_bridge` into `ComfyUI/custom_nodes/`, then restart.

After install, click `⚙️ Modal Setup`, enter Workspace / Token to deploy. See [SETUP.md](./SETUP.md).

## Security

- `config.json` (contains tokens) and `secrets.toml` are in `.gitignore` — **never committed**.
- Modal endpoints are private with self-issued key auth; missing key always returns 401.

## License

MIT
