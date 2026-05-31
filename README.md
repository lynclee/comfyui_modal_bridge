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
        {workflow, user_id, gpu, images, incognito=true}
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

详细安装见 [INSTALL.md](./INSTALL.md)。
