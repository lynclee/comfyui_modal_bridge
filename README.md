# comfyui_modal_bridge

ComfyUI Desktop 插件:把当前工作流推到 Modal Serverless(GPU),结果回流本地 `output/modal_results/`。

## 工作原理

```
ComfyUI Desktop(本地)
   ↓ 点 [☁️ Queue on Modal] 按钮
前端 JS:
   graphToPrompt() → API workflow JSON
   ↓
POST /modal_bridge/queue(本地路由,加在 ComfyUI 服务器上)
   ↓
Python 路由:
   1. 解析 prompt 找 LoadImage,读 input/ 里的图 → base64
   2. POST 你的 Modal /run endpoint:
        {workflow, tier, images, incognito=true, auth_key}
      (tier=显存档 80g/40g,按工作流模型自动选,每档带 GPU 原生 fallback)
   3. 拿 job_id
   4. 轮询 /status?job_id=xxx
   5. 完成 → 拿 data_base64,写 output/modal_results/<job_id>/output.png
   ↓
返回前端:{ok, job_id, gpu, elapsed_sec, outputs}
   ↓
前端弹通知,引导去 output 文件夹看
```

## API 路由

| Method | Path | 用途 |
|---|---|---|
| `POST` | `/modal_bridge/queue` | 提交当前 workflow |
| `GET`  | `/modal_bridge/config` | 读配置 |
| `POST` | `/modal_bridge/config` | 改配置 |
| `GET`  | `/modal_bridge/health` | 健康检查(代理 Modal /health) |

## 模型 & custom_node 同步

- **模型**:都在本地 ComfyUI Desktop 下好。提交时本地用 modal SDK 查 Volume 缺哪些 →
  本地有的直接 `batch_upload` 上去(CAS 块级去重,网上通用大模型秒过)。不从 HF 下载。
  整体推送:`python sync_models.py`。
- **custom_node**:与本地双向同步(缺的加 / 本地 commit 变的更新 / 本地已卸载的移除)→ 重部署。

Modal 端只有 4 个私有 endpoint(run/status/cancel/health),模型查/传全走本地 SDK。

详细安装见 [INSTALL.md](./INSTALL.md),部署见 [SETUP.md](./SETUP.md)。
