# 安装 & 使用 — comfyui_modal_bridge

## 1. 安装

```bash
# 拷到 ComfyUI 的 custom_nodes/
cp -r comfyui_modal_bridge /path/to/ComfyUI/custom_nodes/

# 重启 ComfyUI
```

插件会自动:
- 注册前端按钮(actionBarButtons)
- 注册本地路由(`/modal_bridge/*`)
- 首次启动生成默认 config

## 2. 部署 Modal endpoint

在 ComfyUI 里点 **[⚙️ Modal Setup]** 一键部署(推荐,零终端):填 Workspace + Token ID/Secret
→ 部署。会自动建 Secret、`modal deploy`、把 endpoint 和私有鉴权 key 写进
`ComfyUI/user/default/modal_bridge/config.json`。

命令行等价:`python deploy.py --workspace <ws> --token-id ak-xxx --token-secret as-xxx`

## 3. 使用

1. 打开/搭一个工作流(模型先在本地 ComfyUI 下好)
2. 点右上角 **[☁️ Modal]** 按钮
3. 自动:custom_node 双向同步 → 模型同步(本地→Volume)→ 提交 Modal → 出图回填

## 4. 配置项(Settings 面板)

- **显存档 / GPU**:auto / 80g / 40g
- **Batch count**:一次跑几张
- **Poll interval**:轮询间隔
- **Timeout**:单 job 超时
- **Incognito**:base64 回流(默认开)
- **Auto-sync models**:本地 → Volume 自动同步
- **Auto-sync custom nodes**:custom_node 双向同步

## 5. 卸载

删除 `custom_nodes/comfyui_modal_bridge/` 目录即可。
