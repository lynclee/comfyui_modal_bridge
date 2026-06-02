# comfyui_modal_bridge

> 中文 | [English](#english)

ComfyUI Desktop 插件:一键把当前工作流推到 **Modal Serverless GPU(H100)** 上跑,出图回流到本地画板。
模型在本地下好、自动同步到云端;custom_node 自动同步;全程在 ComfyUI 里完成,**不用开终端**。

> Registry: `comfyui_modal_bridge`(publisher `lynclee`)· 在 ComfyUI Manager 搜 **Modal Bridge** 即可安装。

## 它解决什么

本地没有大显存 GPU(Mac / 轻薄本 / 4090 显存不够跑 flux2),又不想搭一套云端 ComfyUI。
装上这个插件,点一下 **☁️ Modal**,当前工作流就在你自己的 Modal 账号的 H100 上跑完、图回到本地 SaveImage。**按秒计费、空闲自动归零**,不出图不花钱。

## 工作流程(点 [☁️ Modal] 之后)

```
ComfyUI Desktop(本地)
  │ graphToPrompt() 序列化当前工作流
  ▼
custom_node 同步   工作流用到、云端镜像没有的节点 → 自动加 + 重部署(只这一次)
  ▼
模型同步          工作流要的模型,云端 Volume 没、本地有 → 用 modal SDK 直传 Volume
  │              (CAS 块级去重:网上通用大模型秒过)
  ▼
提交 Modal /run → 轮询 → base64 回流 → 写 output/modal_results/<job_id>/
  ▼
回填到画板的 SaveImage 节点
```

## 关键特性

- **零终端部署**:点 `⚙️ Modal Setup` 填 Modal token → 后端自动 `pip install modal`、建 Secret、`modal deploy`、写配置。
- **统一 H100**:所有工作流跑 H100,排不到自动降级 A100-80GB(Modal 原生 fallback)。
- **模型本地 → Volume**:模型在本地 ComfyUI 下好,提交时自动把云端缺的传上去;块级去重让通用大模型秒过。不从 HF 下载、不依赖手维护的 registry。
- **custom_node 多机友好**:自动加工作流需要的节点;多台电脑各装一部分时取**并集、互不删**;清理走 Setup 里的「管理云端节点」手动勾选。
- **私有鉴权**:endpoint 用自建 `BRIDGE_API_KEY` 校验,只有你的 key 能调用。
- **并发 & 进度**:多工作流并发各有独立进度卡片(可拖动/取消/关闭);上传带速率 + ETA;job 状态自动清理。

## 安装

- **方式一(推荐)**:ComfyUI Manager → Custom Nodes Manager → 搜 `Modal Bridge` → 安装 → 重启。
- **方式二**:`git clone https://github.com/lynclee/comfyui_modal_bridge` 到 `ComfyUI/custom_nodes/`,重启。

装好后点右上角 `⚙️ Modal Setup` 部署。详见 [SETUP.md](./SETUP.md)。

## 安全

- `config.json`(含 token)和 `secrets.toml` 在 `.gitignore` 里,**绝不进仓库**。
- Modal endpoint 私有,自建 key 鉴权,无 key 一律 401。

## License

MIT

---

<a name="english"></a>

# comfyui_modal_bridge (English)

> [中文](#comfyui_modal_bridge) | English

A ComfyUI Desktop plugin: push the current workflow to **Modal Serverless GPU (H100)** with one click; the result flows back to your local canvas. Models are downloaded locally and auto-synced to the cloud; custom nodes auto-sync too. Everything happens inside ComfyUI — **no terminal needed**.

> Registry: `comfyui_modal_bridge` (publisher `lynclee`) · Search **Modal Bridge** in ComfyUI Manager to install.

## What it solves

You don't have a big-VRAM GPU locally (Mac / thin laptop / a 4090 that can't fit flux2), and you don't want to stand up a full cloud ComfyUI. Install this, click **☁️ Modal**, and the current workflow runs on an H100 in *your own* Modal account, with the image returned to your local SaveImage. **Per-second billing, scales to zero when idle** — you pay nothing when not generating.

## Flow (after clicking [☁️ Modal])

```
ComfyUI Desktop (local)
  │ graphToPrompt() serializes the current workflow
  ▼
node sync      Nodes the workflow uses but the cloud image lacks → auto-add + redeploy (one time)
  ▼
model sync     Models the workflow needs, missing on the Volume but present locally
  │            → uploaded directly via modal SDK (CAS block dedup: common big models are instant)
  ▼
submit Modal /run → poll → base64 back → write output/modal_results/<job_id>/
  ▼
display on the canvas SaveImage node
```

## Highlights

- **Zero-terminal deploy**: click `⚙️ Modal Setup`, enter your Modal token → the backend auto `pip install modal`, creates the Secret, runs `modal deploy`, writes config.
- **Unified H100**: every workflow runs H100, falling back to A100-80GB automatically (Modal native fallback).
- **Local → Volume models**: download models locally, missing ones get uploaded on submit; block-level dedup makes common big models instant. No HF download, no hand-maintained registry.
- **Multi-machine friendly nodes**: auto-add nodes the workflow needs; across machines the cloud image is the **union, never cross-deleted**; cleanup is manual via "Manage cloud nodes" in Setup.
- **Private auth**: endpoints verify a self-issued `BRIDGE_API_KEY`; only your key can call them.
- **Concurrency & progress**: each concurrent workflow gets its own progress card (draggable / cancelable / closable); uploads show rate + ETA; job state is auto-cleaned.

## Install

- **Option 1 (recommended)**: ComfyUI Manager → Custom Nodes Manager → search `Modal Bridge` → Install → restart.
- **Option 2**: `git clone https://github.com/lynclee/comfyui_modal_bridge` into `ComfyUI/custom_nodes/`, then restart.

After install, click `⚙️ Modal Setup` to deploy. See [SETUP.md](./SETUP.md).

## Security

- `config.json` (contains tokens) and `secrets.toml` are in `.gitignore` — **never committed**.
- Modal endpoints are private with self-issued key auth; missing key always returns 401.

## License

MIT
