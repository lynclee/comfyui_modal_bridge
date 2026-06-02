# comfyui_modal_bridge — 部署指南

把当前 ComfyUI 工作流一键推到 Modal Serverless GPU 上跑,出图回流本地。
**模型都在本地 ComfyUI Desktop 下好**,提交时自动把缺的同步到云端(本地 → Modal Volume,
块级去重,通用大模型秒过)。custom_node 与本地双向同步(加 / 改 / 删)。

---

## TL;DR

```
# 1. 拷插件到 ComfyUI
cp -r comfyui_modal_bridge ~/Documents/ComfyUI/custom_nodes/

# 2. 重启 ComfyUI → 右上角点 [⚙️ Modal Setup] 填 token 部署(零终端)
```

详细:
1. 注册 Modal(送 $30/月),拿 **Token ID**(`ak-...`)+ **Token Secret**(`as-...`)+ 记下 **workspace 名**
   (modal.com 个人主页 URL 那段,如 `lync5134`)。地址:https://modal.com/settings/tokens
2. 拷插件目录到 `ComfyUI/custom_nodes/`,重启 ComfyUI Desktop
3. 点右上角 **[⚙️ Modal Setup]** → 填 Workspace / Token ID / Token Secret(HF token 可选)→ 选默认 GPU → **部署**
4. 等部署完(首次 3-5 分钟拉镜像)→ 回画板点 **[☁️ Modal]** 出图

---

## 方式 A:GUI 部署(推荐,零终端)

[⚙️ Modal Setup] 按钮背后:后端自动 `pip install modal` → 建 Secret(随机生成私有鉴权 key
`BRIDGE_API_KEY`)→ `modal deploy` → 写 config → 验证 health。全程在 ComfyUI 进程里,不用开终端。

## 方式 B:命令行部署

```bash
cd ~/Documents/ComfyUI/custom_nodes/comfyui_modal_bridge
pip install modal
python deploy.py --workspace <你的workspace> --token-id ak-xxx --token-secret as-xxx
# token 也可走环境变量 MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
```

---

## 出图流程(点 [☁️ Modal] 之后)

1. 前端 `graphToPrompt()` 序列化工作流
2. **custom_node 双向同步**:对比工作流用到的节点与 Modal 镜像 → 缺的加 / 本地 commit 变的更新 /
   本地已卸载的移除 → 有变化就重部署(只这一次,之后秒进)
3. **模型同步**:查 Modal Volume 缺哪些 → 本地有的用 `modal.Volume.batch_upload` 传上去
   (CAS 块级去重,网上通用大模型秒过);Volume 和本地都没有的 → 提示先在本地下好
4. POST `/modal_bridge/submit` → 提交到 Modal,按工作流总显存自动选 GPU 档,轮询状态
5. 完成 → base64 回流 → 写 `output/modal_results/<job_id>/` → 回填画板 SaveImage 节点

---

## 模型策略(本地 → Volume,不再从 HF 下载)

- 模型都在本地 ComfyUI Desktop 下好(放在 `models/<类型>/`)。
- 提交时本地直接用 modal SDK 查 Volume、传缺的文件。不经任何 Modal endpoint,不依赖 registry。
- 想一次性把本地模型库整体推上去(省得每个工作流第一次跑时等上传):
  ```bash
  python sync_models.py            # 同步所有缺的
  python sync_models.py --dry-run  # 只看差异
  python sync_models.py --type loras
  ```
- 为什么快:Modal Volume 做块级去重(CAS),相同内容只存一份;网上通用大模型很多人传过,
  你这边等于秒过,只有自训练 / 私有模型才真正占你的上行带宽。

---

## GPU 档(按工作流显存自动选)

- **80g**:H100 → A100-80GB(大模型,如 flux2 dev)
- **40g**:L40S → H100(z-image / flux2-klein 等)

每档用 `@app.cls(gpu=[...])` 自带 Modal 原生 fallback(主卡排不到自动降级,不干等)。
前端按工作流模型权重总大小估算档位;也可在 Settings → "Modal Bridge: 显存档 / GPU" 强制指定。

---

## custom_node

在 ComfyUI 里点 [☁️ Modal],会自动和本地双向同步:
- 工作流用到、Modal 没有的 → 加进镜像
- 本地 commit 变了的 → 按本地 commit 更新
- 本地已卸载的 → 从镜像清单移除

清单存在 `modal_app/_custom_nodes_data.py`(由按钮自动维护,也可手改)。

> 注意:同步以**本地为真源**。单机 Desktop 场景适用;若 baked 清单里有某个节点本地没装,
> 提交时会被当作"本地已卸载"而移除。

---

## Modal 端 endpoint(4 个,私有,自建 key 鉴权)

```
https://<ws>--comfyui-bridge-run.modal.run     (POST,跑 workflow)
https://<ws>--comfyui-bridge-status.modal.run  (GET,查状态)
https://<ws>--comfyui-bridge-cancel.modal.run  (POST,取消)
https://<ws>--comfyui-bridge-health.modal.run  (GET,健康 + 已装 custom_nodes)
```

模型的查 / 传全走本地 modal SDK,所以这里不需要 list/check/seed 这些 endpoint。

---

## 文件结构

```
comfyui_modal_bridge/
├── __init__.py            注册 web + routes
├── config.py             config.json 读写
├── routes.py             本地 /modal_bridge/* 路由
├── modal_client.py       调 Modal 4 endpoint(私有鉴权)
├── modal_volume.py       本地 SDK 操作 Volume(查 + 传模型)
├── node_sync.py          custom_node 双向同步规划 + 部署命令
├── sync_models.py        命令行:本地模型整体同步到 Volume
├── deploy.py             命令行部署(GUI [Modal Setup] 的等价版)
├── web/modal_bridge.js   前端按钮 + 进度浮窗 + 同步流程
└── modal_app/
    ├── modal_app.py          Modal app(4 endpoint + 两档 GPU worker)
    ├── modal_image.py        镜像 DSL(读 _custom_nodes_data)
    ├── _custom_nodes_data.py 镜像要装的 custom_nodes 清单
    ├── _comfy_ws.py          容器内跑 ComfyUI + 取图
    └── extra_model_paths.yaml Volume 模型路径
```

---

## 架构

```
┌──────────────────┐     ┌──────────────┐     ┌──────────────────┐
│  ComfyUI Desktop │ ──> │  本地 routes │ ──> │  Modal Serverless │
│  (前端 JS)       │     │  (aiohttp)   │     │  (GPU worker)     │
└──────────────────┘     └──────┬───────┘     └──────────────────┘
      点按钮                     │  modal SDK         跑 ComfyUI 出图
                          解析/轮询/回填   ────────>   Volume 存模型
                          节点同步         直传 Volume
```
