# comfyui_modal_bridge — 安装 + 使用指南

## 1. 安装(第一次)

### 步骤 1.1:把整个目录拷到 ComfyUI 的 custom_nodes/

**容器内 ComfyUI(本工作目录)**:
```bash
cp -r /workspace/comfyui_modal_bridge/ /workspace/ComfyUI/custom_nodes/
```

或者更稳的(避免符号链接问题):
```bash
rm -rf /workspace/ComfyUI/custom_nodes/comfyui_modal_bridge
cp -r /workspace/comfyui_modal_bridge/ /workspace/ComfyUI/custom_nodes/
```

### 步骤 1.2:确认目录结构

```bash
ls /workspace/ComfyUI/custom_nodes/comfyui_modal_bridge/
# 期望:
#   __init__.py
#   routes.py
#   modal_client.py
#   config.py
#   web/
#   INSTALL.md
#   README.md
```

### 步骤 1.3:安装依赖

只需要 `aiohttp`,大概率 ComfyUI 已经装了。如果启动报 import 错:

```bash
pip install aiohttp
```

### 步骤 1.4:启动 ComfyUI

正常启动 ComfyUI。看启动日志,应该看到一行:

```
[modal_bridge] ✓ loaded — endpoint: https://lync5134--art-ai
```

如果没看到这行或看到 `✗ load failed: ...`,说明插件没正确加载,把错误贴出来。

---

## 2. 配置(第一次启动后)

启动 ComfyUI 后,会自动生成默认配置文件:

```
ComfyUI/user/default/modal_bridge/config.json
```

内容:

```json
{
  "modal_endpoint_base": "https://lync5134--art-ai",
  "default_gpu": "H100",
  "user_id": "local-dev",
  "incognito": true,
  "poll_interval_sec": 1.5,
  "timeout_sec": 1200,
  "output_subfolder": "modal_results",
  "auto_strip_models": false
}
```

**如果你的 Modal workspace 不是 `lync5134`**:用记事本/VSCode 改 `modal_endpoint_base`,然后重启 ComfyUI。

---

## 3. 使用

1. 打开任意工作流(文生图 / 图生图都行)
2. 浏览器右下角应该出现 **`☁️ Queue on Modal`** 按钮
3. 点击 → 弹出 "Building workflow..."
4. 等 30~60 秒(cold start 30~40s + 推理 8~20s)
5. 完成后弹 `✓ Modal job <id> done in Xs (gpu=H100)`
6. 结果在 `ComfyUI/output/modal_results/<job_id>/output.png`

---

## 4. 故障排查

### 按钮没出现
- 浏览器 F12 → Console,看有没有 `[modal_bridge]` 开头的日志
- 检查 `ComfyUI/custom_nodes/comfyui_modal_bridge/web/modal_bridge.js` 是否存在

### 按钮点了没反应
- 浏览器 F12 → Network,看 `/modal_bridge/queue` 请求和响应
- 看 ComfyUI 后端控制台的 `[modal_bridge]` 日志

### 报 "modal endpoint not reachable"
- 检查 config.json 的 `modal_endpoint_base` 拼接对不对
- 浏览器直接访问 `https://lync5134--art-ai-health.modal.run` 看是否 200
- 容器内代理问题:health 请求走 aiohttp,默认读 `http_proxy` 环境变量

### 报 "Input image not found locally"
- 工作流里的 LoadImage 节点引用了不存在的图
- 把图先放到 `ComfyUI/input/` 再点

### Modal 端 workflow validation 失败
- Modal 镜像里只有 KJNodes / rgthree / essentials
- 你的工作流用了其它 custom node → 要去 Modal 镜像加(改 `modal/art_ai/modal_image.py`)

### 报 "model not found"
- Modal Volume 里没那个模型文件
- 手动 commit 进 Volume(命令在 `modal/art_ai/README.md`)

---

## 5. 配置项说明

| 字段 | 默认 | 说明 |
|---|---|---|
| `modal_endpoint_base` | `https://lync5134--art-ai` | Modal workspace 前缀,后端拼接 `-run/-status/-health/-cancel.modal.run` |
| `default_gpu` | `H100` | 默认 GPU 档位。允许:A10G / L40S / A100 / A100-80GB / H100 |
| `user_id` | `local-dev` | Modal 那边记录用(incognito 模式下无意义) |
| `incognito` | `true` | 跳过 R2 上传,直接返回 base64 |
| `poll_interval_sec` | `1.5` | 轮询频率 |
| `timeout_sec` | `1200` | 单 job 最大等待时间(20 min) |
| `output_subfolder` | `modal_results` | 结果保存在 `output/<this>/<job_id>/` |
| `auto_strip_models` | `false` | 第一版关闭,不解析自动剥离模型 |

---

## 6. 第一版**不**支持

- 进度条 / WebSocket 实时推送(用轮询)
- 取消按钮(轮询超时自动失败)
- 模型自动同步(手动 commit Volume)
- 多输出(只取第一张结果图)
- 把结果"塞回"画板的 PreviewImage 节点(去 output 文件夹手动看)
- 配置 UI(直接编辑 json)
- token / 鉴权(Modal endpoint 已是 public,自己 endpoint 自己负责)
