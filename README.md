# comfyui_modal_bridge

> 中文 | [English](#english)

**ComfyUI Desktop 插件:一键把当前工作流推到你自己的 Modal Serverless GPU(H100)上跑,出图回流本地画板。** 本地不用好显卡、不用开终端、不用搭云端 ComfyUI —— 装上、填一次 token、点一下,就跑。

> Registry: `comfyui_modal_bridge`(publisher `lynclee`)· 在 ComfyUI Manager 搜 **Modal Bridge** 即可安装。

## ✨ 核心优势

- 🖥️ **不挑机器,本地零显卡要求** — Mac、轻薄本、核显本都行。FLUX.2 这种吃显存的大模型,**算力全在云端 H100**,本地只负责发起工作流、收图。本地再弱也能跑 flux2,不用为了跑图换电脑。
- ⚡ **多任务并发** — 多个工作流可同时提交、同时跑,各有独立进度卡片(可拖动 / 取消 / 关闭),互不阻塞、互不覆盖。
- 🚀 **全自动部署,零终端** — GUI 填一次 Modal token,后端自动 `pip install modal`、建密钥、`modal deploy`、写配置。全程不碰命令行,首次拉镜像约 3-5 分钟,之后秒进。
- 🧩 **custom node 自动同步** — 工作流用到的自定义节点,云端镜像没有就**自动装进镜像并重部署**;多台机器各装一部分时取**并集、互不删**,换机无缝。
- 💰 **按秒计费,空闲归零** — 用你自己的 Modal 账号(注册送 $30/月额度,不绑卡),**不出图不花钱**,闲置自动缩到零。

## 它解决什么

本地没有大显存 GPU(Mac / 轻薄本 / 4090 显存不够跑 flux2),又不想自己搭一套云端 ComfyUI、不想折腾 Docker 和命令行。装上这个插件,点一下 **☁️ Modal**,当前工作流就在你自己 Modal 账号的 H100 上跑完,图回到本地 SaveImage 节点。

## 关键特性(细节)

- **零终端部署**:点 `⚙️ Modal Setup` 填 Modal token → 后端自动 `pip install modal`、建 Secret、`modal deploy`、写配置并验证 health。
- **不挑本地机器**:本地只做序列化 + 上传 + 收图,**不跑推理**,所以对本地显卡/显存无要求;Mac / Windows / Linux 一致(子进程串流部署日志,绕开 Windows 事件循环坑)。
- **统一 H100**:所有工作流跑 H100,排不到自动降级 A100-80GB(Modal 原生 fallback),不按显存分档把小模型丢到弱卡。
- **多任务并发 & 进度**:多工作流并发各有独立进度卡片(可拖动/取消/关闭);上传带速率 + ETA;job 状态自动清理,不会互相覆盖。
- **custom_node 自动同步 + 多机友好**:自动加工作流需要的节点并重部署(只这一次);多机取并集、互不删;清理走 Setup 的「管理云端节点」手动勾选。
- **模型本地 → Volume**:模型在本地 ComfyUI 下好,提交时自动把云端缺的传上去;**块级去重(CAS)让通用大模型秒过**,只有自训练/私有模型才真占上行带宽。不从 HF 下载、不依赖手维护的 registry。
- **私有鉴权**:endpoint 用自建 `BRIDGE_API_KEY` 校验,只有你的 key 能调用,无 key 一律 401。

## 工作流程(点 [☁️ Modal] 之后)

```
ComfyUI Desktop(本地,不挑机器)
  │ graphToPrompt() 序列化当前工作流
  ▼
custom_node 同步   工作流用到、云端镜像没有的节点 → 自动加 + 重部署(只这一次)
  ▼
模型同步          工作流要的模型,云端 Volume 没、本地有 → 用 modal SDK 直传 Volume
  │              (CAS 块级去重:网上通用大模型秒过)
  ▼
提交 Modal /run → 轮询 → base64 回流 → 写 output/modal_results/<job_id>/
  ▼
回填到画板的 SaveImage 节点(支持多 SaveImage / 多输入)
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

**A ComfyUI Desktop plugin: push the current workflow to your own Modal Serverless GPU (H100) with one click; the image flows back to your local canvas.** No good GPU locally, no terminal, no self-hosted cloud ComfyUI — install, enter a token once, click, done.

> Registry: `comfyui_modal_bridge` (publisher `lynclee`) · Search **Modal Bridge** in ComfyUI Manager to install.

## ✨ Why use it

- 🖥️ **Runs on any machine — zero local GPU required.** Mac, thin laptops, iGPU-only — all fine. VRAM-hungry models like FLUX.2 run **entirely on a cloud H100**; your machine only serializes the workflow and receives images. Run flux2 on a potato.
- ⚡ **Multi-task concurrency.** Submit and run multiple workflows at once — each gets its own progress card (draggable / cancelable / closable), no blocking, no clobbering.
- 🚀 **Fully automatic deploy, zero terminal.** Enter your Modal token once in the GUI; the backend auto `pip install modal`, creates the secret, runs `modal deploy`, writes config. Never touch the command line. First image-pull ~3-5 min, instant afterward.
- 🧩 **Custom nodes auto-sync.** Nodes your workflow uses but the cloud lacks are **auto-baked into the image and redeployed**; across machines the image is the **union, never cross-deleted** — switch machines seamlessly.
- 💰 **Per-second billing, scales to zero.** Uses your own Modal account ($30/mo free credit, no card); **you pay nothing when not generating**, idle scales to zero.

## What it solves

You don't have a big-VRAM GPU locally (Mac / thin laptop / a 4090 that can't fit flux2), and you don't want to stand up a full cloud ComfyUI or fight Docker and the CLI. Install this, click **☁️ Modal**, and the current workflow runs on an H100 in *your own* Modal account, with the image returned to your local SaveImage.

## Highlights (details)

- **Zero-terminal deploy**: click `⚙️ Modal Setup`, enter your Modal token → the backend auto `pip install modal`, creates the Secret, runs `modal deploy`, writes config, verifies health.
- **Machine-agnostic local side**: locally it only serializes + uploads + receives — **no inference** — so it has no requirement on your GPU/VRAM; consistent across Mac / Windows / Linux (streams deploy logs via a subprocess to dodge the Windows event-loop pitfall).
- **Unified H100**: every workflow runs H100, falling back to A100-80GB automatically (Modal native fallback) — no VRAM tiering that drops small models onto weak cards.
- **Multi-task concurrency & progress**: each concurrent workflow gets its own progress card (draggable / cancelable / closable); uploads show rate + ETA; job state auto-cleans without clobbering.
- **Custom-node auto-sync & multi-machine**: auto-adds nodes the workflow needs and redeploys (one time); across machines it's the **union, never cross-deleted**; cleanup is manual via "Manage cloud nodes" in Setup.
- **Local → Volume models**: download models locally; missing ones upload on submit; **block-level dedup (CAS) makes common big models instant** — only custom/private models actually use upstream bandwidth. No HF download, no hand-maintained registry.
- **Private auth**: endpoints verify a self-issued `BRIDGE_API_KEY`; only your key can call them, missing key always returns 401.

## Flow (after clicking [☁️ Modal])

```
ComfyUI Desktop (local, any machine)
  │ graphToPrompt() serializes the current workflow
  ▼
node sync      Nodes the workflow uses but the cloud image lacks → auto-add + redeploy (one time)
  ▼
model sync     Models the workflow needs, missing on the Volume but present locally
  │            → uploaded directly via modal SDK (CAS block dedup: common big models are instant)
  ▼
submit Modal /run → poll → base64 back → write output/modal_results/<job_id>/
  ▼
display on the canvas SaveImage node (multi-SaveImage / multi-input supported)
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
