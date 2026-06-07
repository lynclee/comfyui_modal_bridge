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

## 出图(点 [RunModal] 之后)

1. 序列化当前工作流
2. **版本 + 显卡校验**:插件版本与云端不一致、或所选显卡与云端实际在跑的卡不一致 → 拦住并引导重新部署
3. **custom_node 同步**:工作流用到、云端没有的节点 → 自动加 + 重部署(只这一次,之后秒进)
4. **显存预检**:模型总显存 ×1.15 超所选卡 → 弹警告(可仍要跑 / 去换卡)
5. **模型同步**:云端 Volume 缺、本地有 → 上传(CAS 去重);云端和本地都没有 → 提示先在本地下好
6. 提交 Modal → 轮询 → base64 回流 → 写 `output/modal_results/<job_id>/` → 回填画板 SaveImage

## GPU(可选 + 改卡强制重部署)

Modal Setup 里选显卡:**L40S 48G / A100-80G / H100 80G(默认) / H200 141G**,每档带 Modal 原生 fallback。
**Modal 的卡是部署时固定的** —— 选完点「部署」才生效。改了卡**不重新部署**,点 RunModal 会被**拦住、强制去重部署**(云端 health 上报真实在跑的卡,与所选不一致即判定),杜绝"以为换了卡其实还跑在旧卡上"。
点 RunModal 前还会用「模型总显存 ×1.15」对比所选卡,超了弹警告(可"仍要跑"/"去换显卡")。

## 导出 API(给别人 / 接后端用)

点 **[Export API]** 把当前工作流导成一个自包含 `<名字>_modal.py`:内嵌工作流(base64)+ 提交/轮询/存图 + `--prompt`/`--seed` 覆盖 + 依赖模型清单 + 网络重试。别人:

```bash
pip install requests
python <名字>_modal.py --prompt "..." --seed 123 --out out.png
```

不需要 ComfyUI / 本机 GPU / 你开机(直连已部署的 `-run`/`-status`)。
- **前提**:该工作流的模型 + 自定义节点已用 RunModal 跑通过一次(已同步到云端),否则 worker 找不到。
- **KEY**:默认占位符(接收方填你私下给的 key);导出时可选「嵌入 KEY」—— key = 你的 Modal 账单,只发可信的人,别公开,泄露需轮换 key。

## 模型策略(本地 → Volume)

- 模型在本地下好(放 `models/<类型>/`)。`unet/` 与 `diffusion_models/`、`clip/` 与 `text_encoders/` 互为别名,放哪个都认。
- 提交时本地用 modal SDK 查 Volume、传缺的。不经任何 endpoint、不从 HF 下载、不依赖 registry。
- 一次性整体推送:`python sync_models.py [--dry-run] [--type loras]`。
- 为什么快:Modal Volume 块级去重(CAS),通用大模型很多人传过 → 你这边秒过,只有自训练/私有模型才真占上行带宽。

## custom_node(多机)

点 [RunModal] 自动加工作流需要的节点。多台电脑各装一部分时:**只增不删,镜像 = 各机并集**,互不干扰。
想清理:Setup →「管理云端节点」→ 勾选要移除的 → 移除并重部署(带"别的机器用到会失败"二次确认)。

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
└── modal_app/
    ├── modal_app.py          Modal app(4 endpoint + H100 worker)
    ├── modal_image.py        镜像 DSL(读 _custom_nodes_data)
    ├── _custom_nodes_data.py 镜像要装的 custom_nodes 清单
    ├── _comfy_ws.py          容器内跑 ComfyUI + 取图
    └── extra_model_paths.yaml Volume 模型路径
```

## Settings(齿轮面板)

`Batch count` / `Poll interval` / `Timeout` / `Incognito` / `Auto-sync models` / `Auto-sync custom nodes`。

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

## Generating (after clicking [RunModal])

1. Serialize the current workflow
2. **Version + GPU check**: if the plugin version differs from the cloud, or the selected GPU differs from the one actually running → block and guide a redeploy
3. **Node sync**: nodes the workflow uses but the cloud lacks → auto-add + redeploy (one time)
4. **VRAM preflight**: model VRAM ×1.15 over the selected GPU → warn (run anyway / switch GPU)
5. **Model sync**: missing on Volume but present locally → upload (CAS dedup); missing both places → prompt to download locally first
6. Submit Modal → poll → base64 back → write `output/modal_results/<job_id>/` → display on canvas SaveImage

## GPU (selectable + redeploy-enforced switch)

Pick a GPU in Modal Setup: **L40S 48G / A100-80G / H100 80G (default) / H200 141G**, each with Modal native fallback.
**Modal's GPU is fixed at deploy time** — pick it, then Deploy to apply. Change the GPU **without redeploying** and RunModal will **block and force a redeploy** (the cloud's health reports the GPU it actually runs on; a mismatch with your selection is caught), so you never silently run on the old GPU.
Before running, model VRAM ×1.15 is also checked against the selected GPU; over → warn (run anyway / switch GPU).

## Export API (for others / your backend)

Click **[Export API]** to export the current workflow as a self-contained `<name>_modal.py`: embedded workflow (base64) + submit/poll/save + `--prompt`/`--seed` overrides + model prereq list + network retry. Others run:

```bash
pip install requests
python <name>_modal.py --prompt "..." --seed 123 --out out.png
```

No ComfyUI / no local GPU / your machine off (it talks directly to the deployed `-run`/`-status`).
- **Prereq**: that workflow's models + custom nodes were synced once via RunModal, otherwise the worker can't find them.
- **KEY**: placeholder by default (the recipient fills in the key you give them privately); optionally **embed the KEY** at export — the key = your Modal billing, so only share with trusted people, never publicly; a leak means rotating the key.

## Model strategy (local → Volume)

- Download models locally (`models/<type>/`). `unet/`↔`diffusion_models/` and `clip/`↔`text_encoders/` are aliases — either works.
- On submit, the local modal SDK lists the Volume and uploads what's missing. No endpoint, no HF download, no registry.
- Bulk push: `python sync_models.py [--dry-run] [--type loras]`.
- Why fast: Modal Volume block-level dedup (CAS) — common big models others have uploaded are instant for you; only custom/private models actually use your upstream bandwidth.

## Custom nodes (multi-machine)

Clicking [RunModal] auto-adds nodes the workflow needs. Across machines that each install a subset: **add-only, image = union**, no cross-deletion.
To clean up: Setup → "Manage cloud nodes" → check the ones to remove → remove & redeploy (with a "other machines using it will fail" confirmation).

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
    ├── modal_app.py          Modal app (4 endpoints + H100 worker)
    ├── modal_image.py        image DSL (reads _custom_nodes_data)
    ├── _custom_nodes_data.py custom_nodes baked into the image
    ├── _comfy_ws.py          run ComfyUI in-container + fetch images
    └── extra_model_paths.yaml Volume model paths
```

## Settings (gear panel)

`Batch count` / `Poll interval` / `Timeout` / `Incognito` / `Auto-sync models` / `Auto-sync custom nodes`.
