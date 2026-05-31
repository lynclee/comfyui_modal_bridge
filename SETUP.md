# comfyui_modal_bridge — 30 分钟从零部署

> 用 Modal Serverless 给 ComfyUI Desktop 加云端 GPU,自动同步模型,带进度条
> 完全独立部署,**不影响你已有的 Modal app**

---

## 你会得到什么

- ComfyUI Desktop 顶部多个 ☁️ Modal 按钮
- 点了 → 当前工作流推到 **你自己的** Modal H100 跑 → 结果回到本地 SaveImage
- workflow 用到的模型如果 Modal 没有,**自动从 HuggingFace 下载到 Volume**(数据中心带宽 GB/s,不消耗本地上行)
- 进度浮窗显示:检查模型 → 下载模型 → 排队 → 推理 → 出图
- **endpoint 私有**,只有你的 Modal token 能调用,不会被外人烧 credits

---

## 🚀 推荐:ComfyUI 里一键部署(零终端)

只会用 ComfyUI Desktop、不想碰命令行的,走这条:

1. **拿 token**(见下面「准备阶段」,只需注册 Modal + 复制 Token ID/Secret + 记下 workspace 名;HF token 可选)
2. **把插件装进 ComfyUI**:
   ```bash
   git clone https://github.com/<owner>/comfyui_modal_bridge.git \
     ~/Documents/ComfyUI/custom_nodes/comfyui_modal_bridge
   ```
   (路径换成你的 ComfyUI 数据目录;Windows / 其它系统同理拷到 `custom_nodes/` 下)
3. **重启 ComfyUI Desktop**
4. 点右上角 **[⚙️ Modal Setup]** 按钮 → 填 Workspace / Token ID / Token Secret(HF token 可选)→ 选 GPU → 点 **部署**
5. 弹窗里看进度:
   ```
   == 未检测到 modal 包,正在装到 ComfyUI 内嵌 Python ==
   == 创建 Modal Secret ==
   == modal deploy(首次拉镜像约 3-5 分钟)==
   == ✓ config 已写入 ==
   == ✓ /health: {...} ==
   ```
   全程**不用开终端**(后端自动 `pip install modal`、建 secret、部署、写配置)。
6. 看到 `✓ 部署成功` 就能关窗口,点 **[☁️ Modal]** 出图了。

> 之后日常使用(出图、自动下模型、自动加 custom_node)全在 ComfyUI 里,零终端。
> 想换 GPU / 改 token / 重新部署,随时再点 [⚙️ Modal Setup]。

下面的「安装 / 配置 / 部署阶段」是**终端方式(方式 B,高级用户)**,效果等价,二选一即可。

---

## 准备阶段(10 分钟)

### 1️⃣ 注册 Modal 账号
- https://modal.com/signup(GitHub OAuth)
- 新账号送 **$30 free credits**(够你跑几百张 H100 图)
- 不需要绑卡

### 2️⃣ 创建 Modal API Token
- 打开 https://modal.com/settings/tokens
- 点 **New Token** → 取个名字(比如 "comfyui-bridge")→ Create
- 弹窗里**两段都复制**:
  - **Token ID**:`ak-xxxxxxxxxxxxxxxxxxxx`
  - **Token Secret**:`as-xxxxxxxxxxxxxxxxxxxx`
- ⚠️ Secret 只显示一次,关了弹窗就再也看不到

### 3️⃣(可选)注册 HuggingFace + Token
- https://huggingface.co/join
- https://huggingface.co/settings/tokens → New token → Read 权限 → 创建
- 复制 `hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`
- 用途:下载需要授权的模型(FLUX.2 dev / FLUX.1 Redux 等)
- 不填:只能下公开模型(Z-Image-Turbo / 大部分 LoRA 等)

### 4️⃣ 找你的 Modal Workspace 名
- 登录 Modal 后,左上角你的头像旁边那个名字
- 例如显示 `lync5134` 或者 `your-username`,记下来

---

## (方式 B · 终端)安装阶段(5 分钟)

> 以下到「部署阶段」是终端方式,高级用户用。只想点按钮的看上面的「🚀 一键部署」即可。

### 5️⃣ Clone 仓库
```bash
git clone https://github.com/<owner>/comfyui_modal_bridge.git
cd comfyui_modal_bridge
```

### 6️⃣ 装本地依赖
```bash
pip install modal toml pyyaml
```

(modal CLI 用于部署,toml 读配置,pyyaml 读 model_registry)

---

## 配置阶段(2 分钟)

### 7️⃣ 复制配置模板
```bash
cp secrets.example.toml secrets.toml
```

### 8️⃣ 编辑 `secrets.toml`,填上 4 个字段
```toml
[modal]
token_id     = "ak-粘贴你的 ID"
token_secret = "as-粘贴你的 Secret"
workspace    = "你的 workspace 名"   # 比如 lync5134

[huggingface]
token = "hf_粘贴你的"                # 没有就留空 ""

[app]
name             = "comfyui-bridge"  # 默认即可
volume_name      = "comfyui-bridge-models"
default_gpu      = "H100"            # 想便宜用 "L40S"
scaledown_window = 120
```

---

## 部署阶段(自动,10 分钟)

### 9️⃣ 跑部署脚本
```bash
python deploy.py
```

脚本会自动做:
```
▶ Step 1: 校验 secrets.toml             (1s)
▶ Step 2: 配置 modal CLI                (3s)
▶ Step 3: 创建 Modal Secret (HF token)  (5s)
▶ Step 4: 部署 Modal app                (★ 首次 3-5 分钟拉镜像)
▶ Step 5: 写本地 ComfyUI config.json    (1s)
▶ Step 6: 验证 /health endpoint         (5s)
```

完成后输出:
```
✓ 部署完成!
endpoint base = https://你的workspace--comfyui-bridge
```

---

## 装到 ComfyUI(1 分钟)

### 🔟 拷到 ComfyUI 的 custom_nodes/

**Mac**:
```bash
cp -r . ~/Documents/ComfyUI/custom_nodes/comfyui_modal_bridge/
```

**Windows**:
```cmd
xcopy /E /I /Y . "%USERPROFILE%\Documents\ComfyUI\custom_nodes\comfyui_modal_bridge\"
```

**Linux**:
```bash
cp -r . ~/ComfyUI/custom_nodes/comfyui_modal_bridge/
```

### 1️⃣1️⃣ 重启 ComfyUI Desktop

完全退出再重开。启动日志应该看到:
```
[modal_bridge] ✓ loaded — endpoint: https://你的ws--comfyui-bridge
```

---

## 首次使用(2 分钟)

### 1️⃣2️⃣ 加载工作流(用 Z-Image-Turbo 试最简的)
ComfyUI 模板里随便一个文生图工作流,或者用我们 demo:
- `examples/z_image_turbo_t2i.json`(无需 HF token,模型公开)

### 1️⃣3️⃣ 点右上角 [☁️ Modal] 按钮
你会看到右上角浮窗:
```
☁️ Modal · Checking models     1.2s
████░░░░░░░░░░░░░░░ 8%
0/4 cached on Modal
```

第一次跑会自动下载缺失模型(Z-Image 主模型 ~6GB,首次 30-60 秒)。
```
☁️ Modal · Downloading models  45.3s
████████████░░░░░░░ 40%
[2/4] z_image_turbo_bf16.safetensors — downloading on Modal...
```

之后:
```
☁️ Modal · Submitting → Queued → Running → Downloading result → ✓ Done
```

最终结果出现在 SaveImage 节点框里。

---

## ⚙️ 后续使用

### 改 GPU / 默认设置
ComfyUI 设置面板(齿轮)→ 搜 "Modal Bridge":
- **Default GPU**:H100 / A100-80GB / A100 / L40S / A10G
- **Batch count**:一次点击跑 N 次(自动改 seed)
- **Auto-seed models**:开/关 自动下载
- **Poll interval / Timeout**

### 加新模型到 registry
编辑 `model_registry.yaml`,加一条:
```yaml
my_lora.safetensors:
  type: loras
  source: huggingface
  repo: someone/my-lora
  hf_filename: my_lora.safetensors
```

下次工作流引用了 `my_lora.safetensors`,会自动下载。

### 查 Modal 上的模型
```bash
modal volume ls comfyui-bridge-models /models/diffusion_models
```

### 查实时日志
```bash
modal app logs comfyui-bridge -f
```

### 卸载/重置
```bash
# 停 app
modal app stop comfyui-bridge

# 删 Volume(注意会删所有下载的模型)
modal volume delete comfyui-bridge-models
```

---

## 🚨 常见问题

### 启动 ComfyUI 没看到 [☁️ Modal] 按钮
1. 看启动日志有没有 `[modal_bridge] ✓ loaded`
2. 没有 → 检查 `custom_nodes/comfyui_modal_bridge/` 文件齐全
3. 有但按钮不见 → F12 Console 看 `[modal_bridge]` 日志,硬刷新 Cmd+Shift+R

### 点 Modal 报 401 Unauthorized
- `config.json` 里的 `modal_token_id` / `modal_token_secret` 写错了
- 最简单:点 **[⚙️ Modal Setup]** 重填 token 再部署一次(会写到正确路径)
- 或终端重跑 `python deploy.py` 覆写

### 模型下载到一半失败
- 看 ComfyUI 日志找 `[modal_bridge] seed_model` 那一行
- 常见原因:HF token 没填(下私有模型)→ 编辑 `secrets.toml` 加 token → `python deploy.py` 重跑

### 工作流用到的 custom_node,Modal 镜像没装(比如本地新加了 KJNodes / Florence2)
**不用开终端**,点 [☁️ Modal] 时会自动处理:

1. 点 [☁️ Modal] → 自动扫工作流用到的 custom_node
2. 发现 Modal 镜像缺哪个(精准:本地解析每个节点来自哪个 custom_nodes 文件夹,再和 Modal 真实已装清单比对)
3. 弹窗:「工作流用到 XXX,Modal 还没有,一键加进去并重部署?(约 1-3 分钟,只这一次)」
4. 点确定 → 进度窗显示 `Deploying image` + 实时 `modal deploy` 日志 → 完成后自动继续出图
5. 之后再用这个工作流,节点已常驻镜像,秒进

原理:把该 custom_node 的 git 地址 + 当前 commit 写进 `modal_app/_custom_nodes_data.py`,
在本机调 `modal deploy` 重 build(只重 clone + 装依赖那两层,其它层走缓存)。
要求:本机装过 `modal`(跑过一次 `deploy.py`)。

**手动方式**(可选):编辑 `modal_app/_custom_nodes_data.py` 的 `CUSTOM_NODES` 加一条
`{"name","url","commit"}` → 重跑 `python deploy.py`。

**关掉自动检查**:设置面板 → Modal Bridge: Auto-check custom nodes 关闭。

**补不了的情况**:某 custom_node 本地不是 git 仓库(没有 remote)→ 无法自动拿地址,
弹窗会提示,你手动在 `_custom_nodes_data.py` 填它的 git url 即可。

### 想停止/省钱
- ComfyUI 设置改 `Default GPU = L40S`(便宜 3 倍)
- 编辑 `secrets.toml` 改 `scaledown_window = 60`(60s 空闲就回收)
- 长时间不用就 `modal app stop comfyui-bridge`

---

## 📊 成本参考(Modal 官方价)

| GPU | $/小时 | $30 能跑(H100 满速,单图 ~30s 含冷启动) |
|---|---|---|
| H100 80GB | $3.95 | ~900 张 |
| A100 80GB | $3.95 | ~700 张 |
| A100 40GB | $2.78 | ~700 张 |
| L40S 48GB | $1.40 | ~1500 张 |
| A10G 24GB | $1.10 | 不够跑 FLUX.2 |

---

## 架构图

```
ComfyUI Desktop (Mac/Win/Linux)
    │ 点 [☁️ Modal]
    ↓
custom_nodes/comfyui_modal_bridge/  (本地)
    │ 解析 workflow,扫 model loaders
    ↓ POST /check_models
Modal app: comfyui-bridge (你自己的)
    │ 检查 Volume → 列缺失
    ↓
缺失:逐个 POST /seed_model
    │ Modal worker 内 hf_hub_download (HF) / aria2c (URL)
    ↓ 写 Volume + commit
所有就绪:POST /run
    │ ComfyWorker 跑 ComfyUI
    ↓ WebSocket 监听完成
返回 base64 → 本地写 output/modal_results/<job_id>/output.png
    │ JS displayInGraph
    ↓
SaveImage 节点显示结果图 ✓
```

---

## 详细架构 / 二开

见 [ARCHITECTURE.md](./ARCHITECTURE.md)
