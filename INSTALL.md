# 安装 & 使用 / Install & Usage

> 中文 | [English](#english)

## 1. 安装

- **方式一(推荐)**:ComfyUI Manager → Custom Nodes Manager → 搜 `Modal Bridge` → 安装 → 重启。
- **方式二**:把本目录拷到 `ComfyUI/custom_nodes/`(或 `git clone`),重启 ComfyUI。

插件会自动注册前端按钮(`☁️ Modal` / `⚙️ Modal Setup`)、本地路由(`/modal_bridge/*`)、并生成默认 config。
依赖:`aiohttp`、`pyyaml`(ComfyUI 一般已自带)。

## 2. 部署 Modal endpoint

点右上角 **[⚙️ Modal Setup]**:填 Workspace + Token ID/Secret → 部署(零终端,自动建 Secret + `modal deploy` + 写 config)。
命令行等价:`python deploy.py --workspace <ws> --token-id ak-xxx --token-secret as-xxx`。详见 [SETUP.md](./SETUP.md)。

## 3. 使用

1. 打开/搭一个工作流(模型先在本地 ComfyUI 下好)
2. 点 **[☁️ Modal]**
3. 自动:custom_node 同步 → 模型同步(本地→Volume)→ 提交 Modal(H100)→ 出图回填

## 4. 设置(齿轮面板)

`Batch count` / `Poll interval` / `Timeout` / `Incognito` / `Auto-sync models` / `Auto-sync custom nodes`。

## 5. 卸载

删除 `custom_nodes/comfyui_modal_bridge/` 即可。

---

<a name="english"></a>

# Install & Usage (English)

> [中文](#安装--使用--install--usage) | English

## 1. Install

- **Option 1 (recommended)**: ComfyUI Manager → Custom Nodes Manager → search `Modal Bridge` → Install → restart.
- **Option 2**: copy this folder into `ComfyUI/custom_nodes/` (or `git clone`), restart ComfyUI.

The plugin auto-registers the frontend buttons (`☁️ Modal` / `⚙️ Modal Setup`), local routes (`/modal_bridge/*`), and a default config.
Deps: `aiohttp`, `pyyaml` (usually already bundled with ComfyUI).

## 2. Deploy the Modal endpoint

Click **[⚙️ Modal Setup]**: enter Workspace + Token ID/Secret → Deploy (no terminal; auto-creates Secret, runs `modal deploy`, writes config).
CLI equivalent: `python deploy.py --workspace <ws> --token-id ak-xxx --token-secret as-xxx`. See [SETUP.md](./SETUP.md).

## 3. Usage

1. Open/build a workflow (download models locally first)
2. Click **[☁️ Modal]**
3. Auto: node sync → model sync (local→Volume) → submit Modal (H100) → result back to canvas

## 4. Settings (gear panel)

`Batch count` / `Poll interval` / `Timeout` / `Incognito` / `Auto-sync models` / `Auto-sync custom nodes`.

## 5. Uninstall

Delete `custom_nodes/comfyui_modal_bridge/`.
