# Modal Bridge 本地 HTTP API

插件在 ComfyUI 本地服务上注册的机器接口——UI 用它,任何脚本 / agent / MCP 也可以直接调。

- **Base URL**:ComfyUI 本地服务地址(Desktop 默认 `http://127.0.0.1:8000`,OSS 默认 `:8188`;容器内访问宿主机用 `host.docker.internal`)
- **鉴权**:无(同机信任模型,与 ComfyUI 本体一致)。云端调用的鉴权(bridge_api_key)由后端自动附加,调用方不用管
- **密钥**:`/config` 读写永不回吐 `modal_token_secret` / `bridge_api_key` / `comfy_api_key` / `aigc_bypass_secret`,只回 `has_*` 布尔标志
- **prompt 格式**:均为 ComfyUI **API prompt**(`{node_id: {class_type, inputs}}`,即前端 `graphToPrompt().output`),不是画布 JSON

## 核心链路:提交一个任务

```
estimate_vram(可选) → submit → poll(循环) → fetch_result
```

### POST /modal_bridge/submit

提交工作流到云端。后端自动完成:GPU 档位路由(`gpu_tier` 配置)、CPU/GPU worker 判定、
输入图片(LoadImage)打包上传。

```bash
curl -X POST http://127.0.0.1:8000/modal_bridge/submit \
  -H 'Content-Type: application/json' \
  -d '{"prompt": { ...API prompt... }}'
```

返回:
```json
{"ok": true, "job_id": "uuid", "gpu": "L40S", "input_image_count": 1, "worker_timeout_sec": 3600}
```
`worker_timeout_sec` 是云端单任务上限——调用方的等待窗应 ≥ 它 + 3 分钟尾巴(decode/回传)。

### GET /modal_bridge/poll?job_id=…

轮询状态(建议间隔 1–2s)。透传云端 status 对象:
```json
{"status": "running", "progress": {"step": 4, "total": 20, "s_it": 50.6, "n_samples": 3, "elapsed": 210.5}}
```
`status` ∈ `queued / running / completed / failed / cancelled`;`failed` 带 `error` 字符串;
`completed` 的完整对象作为下一步的 `modal_state` 原样传回。
`progress.s_it` 是滑窗中位数(≥3 个采样点才可信),可用于投影是否会撞 `worker_timeout_sec`。

### POST /modal_bridge/fetch_result

`{"job_id": "...", "modal_state": {poll 拿到的 completed 对象}}` → 把产物写进
`ComfyUI/output/<output_subfolder>/<job_id>/`,返回 `{ok, outputs:[{filename, subfolder, type}]}`。
大文件自动走 Volume 直连,小文件 base64,调用方无感。

### POST /modal_bridge/cancel

`{"job_id": "..."}` → 请求云端取消。**必须检查返回的 `ok`**:`ok:false` 表示云端还在跑、还在计费。

## 预检与估算

| 端点 | 方法 | 入参 | 说明 |
|---|---|---|---|
| `/modal_bridge/estimate_vram` | POST | `{prompt}` | 返回 `{est_vram_gb, est_basis, category, total_mb, unknown[]}`。视频类在能从工作流抠出 分辨率×帧数 字面量时走激活公式(`est_basis:"activation"`,实测校准),否则回退权重×系数(`"legacy"`,偏保守) |
| `/modal_bridge/check_required_inputs` | POST | `{prompt}` | 找出缺必填输入的节点(老工作流 × 新节点定义),`{missing:[{node_id, class_type, missing[]}]}` |
| `/modal_bridge/check_models` | POST | `{prompt}` | 对比工作流所需模型 vs 云端 Volume,返回缺失清单 |
| `/modal_bridge/check_nodes` | POST | `{prompt}` | 对比工作流 custom_node vs 云端镜像清单。分流:`add`/`update`(有 git 且已推送 → 进镜像,要重部署)、`local_pack`(自写节点或 commit 未推送 → 走 Volume 打包通道,**不用重部署**)、`missing_no_git`(本地连目录都没有 → 补不了) |

## 同步与部署(耗时操作,内部有互斥锁)

| 端点 | 方法 | 说明 |
|---|---|---|
| `/modal_bridge/sync_models` | POST | 本地模型 → Modal Volume(SDK batch_upload,CAS 去重) |
| `/modal_bridge/sync_nodes` | POST | custom_node 清单同步 + 触发重新部署 |
| `/modal_bridge/sync_local_nodes` | POST | `{folders:[...]}` → 自写节点(无 git remote / commit 未推送)打包传 Volume。**不重建镜像**,worker 启动时解压;内容指纹去重,没改就跳过 |
| `/modal_bridge/list_local_nodes` | GET | Volume 上现存的本地节点包名单 |
| `/modal_bridge/remove_local_node` | POST | `{folder}` → 从 Volume 删掉某个本地节点包 |
| `/modal_bridge/deploy` | POST | 重新部署云端 app(drain 语义:在跑的任务在旧版本上跑完) |
| `/modal_bridge/list_nodes` | GET | 云端镜像当前的 custom_node 清单 |

## 状态与配置

| 端点 | 方法 | 说明 |
|---|---|---|
| `/modal_bridge/health` | GET | 云端 app 健康(`{ok, modal:{...}}`) |
| `/modal_bridge/version` | GET | 版本契约:`{local, deployed, match, reachable}`,不匹配应引导重新部署 |
| `/modal_bridge/platform_status` | GET | Modal 官方状态页聚合态(`operational/degraded/...`),区分平台故障 vs 未部署 |
| `/modal_bridge/config` | GET/POST | 读/写插件配置(密钥字段永不回吐;POST 是浅合并) |
| `/modal_bridge/bridge_key` | GET | 取回本机 bridge_api_key(显式动作用,如导出脚本) |
| `/modal_bridge/job_event` | POST | 前端/调用方上报客户端侧结局(`{job_id, event, detail}`)进后端日志留痕 |

## 无 ComfyUI 直连云端(standalone)

本地 ComfyUI 不是必需品——云端 app 本身就是一组独立 REST endpoint(自建 bridge_key 鉴权),
上面的本地 API 只是它的「全功能前台」。脱离 ComfyUI 的消费/自建方式(0.7.3+):

**云端协议**(`https://<ws>--comfyui-bridge` + `-{label}.modal.run`):

| label | 方法 | 说明 |
|---|---|---|
| `-run` | POST | `{workflow, images?, gpu_class?, needs_gpu?, delivery?, auth_key}` → `{id, status, gpu}` |
| `-status` | GET | `?job_id=&key=` → 状态对象(同上文 poll 的透传源) |
| `-fetch` | GET | `?job_id=&path=<volume_path>&key=&delete=1` → **流式下载大文件产物**(路径囚笼在该 job 目录;这是外部消费者不需要 modal token 的关键) |
| `-cancel` | POST | `{job_id, auth_key}` |
| `-health` | GET | `?key=` → 部署版本/卡型/已装节点 |

**三种消费方式**(都基于 `bridge_client.py`,纯 stdlib 零依赖):

1. **Python 库**:`BridgeClient(endpoint, key)` → `submit / wait / download_outputs`(base64 与 Volume 大文件双路径自动处理,输入图 `pack_input_images` 打包)
2. **CLI**(`bridge_cli.py`):消费者 `configure → submit --wait`;自建者 `deploy`(复用插件的 env 链路,避开裸 `modal deploy` 陷阱)+ `upload-model`(模型上 Volume)
3. **MCP cloud 模式**(`mcp_server.py`):设 `MODAL_BRIDGE_ENDPOINT` + `MODAL_BRIDGE_KEY` 即切换,agent 工具面不变

**能力边界**:standalone 只「消费」部署好的能力——模型要先在 Volume(部署者同步过,或 `upload-model` 手动放)、custom_node 要先在镜像(部署者本机同步过);显存档位 `gpu_class` 手选,没有本地估算路由。

## 给 agent 的注意事项

- **等待窗**:用 submit 返回的 `worker_timeout_sec` + 180s 做轮询 deadline,别硬编码
- **配置生效链路**:`gpu_tier` 改完即生效;`default_gpu`/`cheap_gpu`/`use_sage_attention`/`worker_timeout_sec` 等要 `/deploy` 后生效
- **取消要核验**:`cancel` 返回 `ok:false` 时任务仍在计费
- **显存不足的形态**是静默降速不是报错:`progress.s_it` 显著高于同配置基线即是信号
