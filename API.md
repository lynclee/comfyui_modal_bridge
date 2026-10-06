# Modal Bridge 本地 HTTP API

插件在 ComfyUI 本地服务上注册的机器接口——UI 用它,任何脚本 / agent / MCP 也可以直接调。

本机直连(peer 与 `Host` 同为 loopback)且通过 Origin 校验的管理请求**不需要** capability。
经局域网、反向代理、`host.docker.internal` 或容器过来的管理请求一律需要。
本机请求仍校验 Origin：跨源 Origin、`Origin: null` 和 `Sec-Fetch-Site: cross-site`
会被拒绝，即使 ComfyUI 开启 CORS——本机直连时这是唯一的守卫。
远程配对不依赖反向代理内部连接的 HTTP/HTTPS 协议一致。

> 0.8.36–0.8.39 曾对本机直连也要求 capability，0.8.40 撤回：每个浏览器都要手工粘一次
> token，而 `/submit`、`/poll` 同在保护范围内，连提交任务都被拦；对同源浏览器页面的
> 安全增量近乎为零——能在 ComfyUI 页面里执行 JS 的攻击者本就能直接排队跑工作流。

- **Base URL**:ComfyUI 本地服务地址(Desktop 默认 `http://127.0.0.1:8000`,OSS 默认 `:8188`;容器内访问宿主机用 `host.docker.internal`)
- **本地管理鉴权**:请求头 `X-Modal-Bridge-Capability` 的值来自服务器插件用户配置 `config.json` 的 `local_api_capability`；首次管理请求缺值时自动生成，日志只显示文件路径，API 不回吐 token。浏览器首次手动配对后存入当前 origin 的 localStorage。反代须保留外部 `Host` 或正确追加转发头，以维持 Origin 防护语义
- **云端鉴权**:云端调用的 `bridge_api_key` 由本地后端自动附加,调用方不用管
- **密钥**:`/config` 读写永不回吐 `modal_token_secret` / `bridge_api_key` / `comfy_api_key` / `aigc_bypass_secret`,只回 `has_*` 布尔标志
- **prompt 格式**:均为 ComfyUI **API prompt**(`{node_id: {class_type, inputs}}`,即前端 `graphToPrompt().output`),不是画布 JSON

| 调用方式 | 0.8.40 起 |
|---|---|
| 浏览器(ComfyUI 本机,`127.0.0.1`/`localhost`) | 不需要配对，直接可用 |
| 浏览器(局域网 / 反代 / 容器) | 首次管理请求提示配对；从服务器本机复制 token，不要发到聊天中。已有有效配对继续使用 |
| 本地 HTTP 脚本 / MCP(直连 loopback) | 不需要请求头 |
| MCP 经 `host.docker.internal` / 局域网 | 每次请求携带上述请求头；用 `MODAL_BRIDGE_LOCAL_CONFIG` 指向 0600 的 config.json，不要提交含真实值的配置 |
| 直连云端的 standalone CLI / MCP cloud | 不受影响，仍使用 `bridge_api_key` |
| 公开只读端点 | GET `/config`(脱敏)、`/health`、`/platform_status`、`/version` 不要求 capability。`/health` 对非本机、又没带有效 capability 的请求只回最小字段(见下文) |

缺失/错误 token 返回 403 和 `X-Modal-Bridge-Auth: capability-required`，在解析请求体、
写配置、上传、创建 Secret 或启动部署之前拒绝。本机跨站拒绝不发起配对。
capability 不是对恶意已安装插件或同源 XSS 的隔离沙箱；不要向不可信用户开放 ComfyUI。

MCP 和独立客户端只接受 HTTP(S)，逐跳拒绝跨源和 HTTPS 降级重定向，避免管理凭据随跳转外泄。
两种 MCP 模式均继承系统代理；内网/localhost 目标由 `no_proxy` / `NO_PROXY` 配置直连。
本地配置保存使用独立随机临时文件，POSIX 创建权限为 0600，写完才原子替换；不会复用旧 `.tmp`。

## 核心链路:提交一个任务

```
estimate_vram(可选) → submit → poll(循环) → fetch_result
```

### POST /modal_bridge/submit

提交工作流到云端。后端自动完成:GPU 档位路由(`gpu_tier` 配置)、CPU/GPU worker 判定、
输入图片(LoadImage)打包上传。

```bash
curl -X POST http://127.0.0.1:8000/modal_bridge/submit \
  -H "X-Modal-Bridge-Capability: ${MODAL_BRIDGE_LOCAL_CAPABILITY}" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": { ...API prompt... }, "job_id": "可选,自带的幂等 id"}'
```

`job_id` 可选(规则见下文「job_id 规则」,不合法返回 400):自带 id 的调用方在自己超时、连接断开时,仍然知道该去 poll 哪个任务。
MCP 本地模式就是这么做的。

返回:
```json
{"ok": true, "job_id": "uuid", "gpu": "L40S", "input_image_count": 1, "worker_timeout_sec": 3600}
```
`worker_timeout_sec` 是云端单任务**执行**上限,从 worker 开始执行算起,不含排队和冷启动。
调用方的截止线应从**第一次看到 `running`** 起算:那一刻 + `worker_timeout_sec` + 3 分钟尾巴(decode/回传)。
从提交时刻起算的话,排队加冷启动一长,就会在任务完成前误判超时(2026-10-05 修过的前端 bug)。

**提交结果未知**:HTTP 502 且正文带 `"outcome": "unknown"` 和 `job_id` 时,任务**可能已经在云端跑了**
(网关超时、响应丢失)。不要重新提交,用这个 `job_id` 去 poll:查到就照常跟进;连续 not_found **并且持续分钟级**(前端用 2 分钟,冷启动慢时任务可能晚落地)才说明多半没落地,重交前先到 Modal 控制台核实。

**分清答复来自谁**:插件 `/submit` 的每个答复(成功和各种错误)都带响应头 `X-Modal-Bridge-Origin: plugin-submit`。
调用方与 ComfyUI 之间隔着反向代理时,不带这个头的答复是代理 / 网关回的,不能按正文格式当成插件的结论:
不带头的 5xx / 408 / 429 / 3xx 按「提交结果未知」处理(用自带的 `job_id` 去 poll);不带头的其它 4xx 说明请求没到插件,
任务没有提交。带 `X-Modal-Bridge-Auth` 头的 403 也是插件的(没重启 ComfyUI 的旧版插件不带来源头,但配对 403 照样带它)。
插件的前端与 MCP 本地模式都按这个分,而且都自带 `job_id`。
不是插件判的「结果未知」(代理掐断、连接中断)时,插件多半还在 `/submit` 里重试(最长约 420s),「没落地」的确认窗口要拉到 9 分钟(前端就是这么做的),不能按 2 分钟算。

### GET /modal_bridge/poll?job_id=…

轮询状态(建议间隔 1–2s)。透传云端 status 对象:
```json
{"status": "running", "progress": {"step": 4, "total": 20, "s_it": 50.6, "n_samples": 3, "elapsed": 210.5}}
```
`status` 取值:

| status | 含义 |
|---|---|
| `queued` / `running` / `delivering` | 进行中(`delivering` = 产物正在写回 / 交付) |
| `completed` / `failed` / `cancelled` | 终态;`failed` 带 `error` 字符串 |
| `not_found` | 云端查无此任务(已被回收,或 id 不对)。单次可能是跨容器读的延迟,**连续多次**(建议 5 次、间隔 ≥2s)才作数 |
| `auth_failed` | 本机路由附加:bridge key 与云端不一致(云端 401)。终态,重新部署会刷新 key |
| `unknown` | 本机路由附加:云端回了其它 HTTP 错误,带 `http_status`。按瞬态处理,继续 poll |

`completed` 的完整对象作为下一步的 `modal_state` 原样传回。
`completed` 可能带 `warnings: [str]`:工作流里有输出分支被 ComfyUI 校验剔除(比如没接线的 PreviewImage),
合法分支照常跑完、少了那一份产物,每条写明节点与原因。**模型 / 参数在云端找不到**的分支不会这样放行:
会先撤回、等 Volume 同步后重试,仍找不到就整单 `failed`。aigc-r2 交付时,job-complete 回调里也带同样的 `warnings`。
`gpu` 是部署时的**候选卡型链**(如 `H100→A100-80GB`),实际跑在哪张卡看 `gpu_actual`(如 `NVIDIA H100 80GB HBM3`)。
`progress.s_it` 是滑窗中位数(≥3 个采样点才可信),可用于投影是否会撞 `worker_timeout_sec`。
终态记录在云端只保留 1 小时(超过 200 条时更早裁剪),之后 poll 会得到 `not_found`。

### POST /modal_bridge/fetch_result

`{"job_id": "...", "modal_state": {poll 拿到的 completed 对象}}` → 把产物写进
`ComfyUI/output/<output_subfolder>/<job_id>/`,返回 `{ok, outputs:[{filename, subfolder, type}]}`。
大文件自动走 Volume 直连,小文件 base64,调用方无感。

| 情况 | 行为 |
|---|---|
| 同一 job 同参数并发取回 | 复用同一后台任务；HTTP 断开不会取消下载 |
| 同一 job 取回中改变参数 | 返回 409；同时最多 32 个不同 job，超限返回 429 |
| 某个文件下载失败 | 本次取回不删除任何云端副本；重试复用有完成回执且未被修改的本地文件 |
| 全部文件完成 | 才清理云端副本；清理失败不影响本地成功结果 |
| 成功响应丢失 | 重试通过本地回执复用文件，不再读取已删除的云端副本 |

回执保存在插件 `config.json` 同级的 `download_receipts/`，不含凭据和图像内容。
修改、移动或删除本地文件会使对应回执失效；若云端副本已删除，无法据此恢复文件。
前端仅在取回成功后删除恢复记录；首次取回后保留至少 1 小时的恢复窗口，刷新不续期，
实际恢复仍受云端任务状态保留期限制。下载停滞时刷新会接管仍在运行的下载，并不会中止底层传输。

`GET /modal_bridge/fetch_progress?job_id=…` 返回采样进度；无活跃采样时返回 `{ok:false}`。

### POST /modal_bridge/cancel

`{"job_id": "..."}` → 请求云端取消。看 `still_billing`(`ok` 恒等于 `!still_billing`):

| 返回 | 含义 |
|---|---|
| `still_billing: false`,`status: "cancelled"` | 取消成功 |
| `still_billing: false`,`cancel_noop: true` | 任务早已结束(`status` 是真实结局:completed / failed / cancelled);completed 的产物照常取回(先 poll 拿完整状态) |
| `still_billing: false`,`status: "not_found"` | 云端查无此任务,没有在跑 |
| `still_billing: true` | **取消失败或结果未知,云端可能还在跑、还在计费**;`error` 说明原因,稍后重试取消 |

## 预检与估算

| 端点 | 方法 | 入参 | 说明 |
|---|---|---|---|
| `/modal_bridge/estimate_vram` | POST | `{prompt}` | 返回 `{est_vram_gb, est_basis, category, total_mb, unknown[]}`。视频类在能从工作流抠出 分辨率×帧数 字面量时走激活公式(`est_basis:"activation"`,实测校准),否则回退权重×系数(`"legacy"`,偏保守) |
| `/modal_bridge/check_required_inputs` | POST | `{prompt}` | 找出缺必填输入的节点(老工作流 × 新节点定义),`{missing:[{node_id, class_type, missing[]}]}` |
| `/modal_bridge/check_models` | POST | `{prompt}` | 对比工作流所需模型 vs 云端 Volume,返回缺失清单 |
| `/modal_bridge/check_nodes` | POST | `{prompt}` | 对比工作流 custom_node vs 云端镜像清单。分流:`add`/`update`(有 git 且已推送,或从 Comfy Registry 装的 → 进镜像,要重部署;Registry 节点带 `version` / `old_version`)、`local_pack`(自写节点、commit 未推送、本机改过代码的 Registry 节点(与该版本原包内容不同;取不到原包时按改过算)、或云端克隆不了的地址 → 代码走 Volume;仅依赖变化时自动重部署;`reason: "unclonable"` 时带 `detail`)、`missing_no_git`(本地连目录都没有 → 补不了)。读不到云端、退回本机清单时带 `cloud_unchecked`(此时不要据此同步);读不到 Volume 上的私有节点名单时带 `volume_unchecked` |

## 同步与部署(耗时操作,内部有互斥锁)

| 端点 | 方法 | 说明 |
|---|---|---|
| `/modal_bridge/sync_models` | POST | 本地模型 → Modal Volume(SDK batch_upload,CAS 去重)。同路径大小不同会覆盖;最后一行汇总已同步 / 已存在跳过 / 被拒,有被拒的项 rc≠0 |
| `/modal_bridge/sync_nodes` | POST | `{new_baked, summary?, prune?}` → custom_node 清单同步 + 重新部署。**删除必须显式**:只有 `prune` 里点名的节点会从镜像移除;云端有、`new_baked` 里没写、也不在 `prune` 里的节点会被自动并回。补不出来源、读不到云端且这次会少掉节点、或读不到云端而本机清单为空时,返回 409 `{error, cloud_unchecked?, vanish?}` |
| `/modal_bridge/sync_local_nodes` | POST | `{folders:[...]}` → 自写节点打包传 Volume；代码变化只重传,`requirements.txt` 变化会自动重建依赖层。每个包携带 manifest,支持多机恢复 |
| `/modal_bridge/list_local_nodes` | GET | Volume 上现存的本地节点包名单;读不到 Volume 时 `{ok:false, error}`,不会冒充「没有」 |
| `/modal_bridge/remove_local_node` | POST | `{folder}` → 从 Volume 删掉某个本地节点包 |
| `/modal_bridge/deploy` | POST | 重新部署云端 app(drain 语义:在跑的任务在旧版本上跑完)。workspace / app 名不合规、或 AIGC 地址不是 `https://` 时返回 rc=2;部署后 `/health` 仍 404 判失败 |
| `/modal_bridge/list_nodes` | GET | 云端镜像当前的 custom_node 清单 |

## 状态与配置

| 端点 | 方法 | 说明 |
|---|---|---|
| `/modal_bridge/health` | GET | 云端 app 健康(`{ok, modal:{...}}`)。非本机、又没带有效 capability 的请求只回 `{ok, healthy, checked_at, limited:true, detail}`:`healthy` 是最近一次完整检查的结论,超过 10 分钟或 endpoint 变了就回 `null`;不会为此唤醒云端容器,也不回节点清单 |
| `/modal_bridge/version` | GET | 版本契约:`{local, deployed, match, reachable, err_kind}`。`err_kind` ∈ `not_deployed / unauthorized / http_error / timeout / unreachable / local_busy`;只有 `not_deployed` 和 `unauthorized` 应拦截提交。非本机匿名请求同样只回受限视图(`local` 加缓存的 `healthy / checked_at`,`limited:true`),不请求云端 |
| `/modal_bridge/platform_status` | GET | Modal 官方状态页聚合态(`operational/degraded/...`),区分平台故障 vs 未部署 |
| `/modal_bridge/config` | GET/POST | GET 返回脱敏配置;POST 只接受 GPU/高级设置 allowlist,不能改凭据或管理鉴权字段。`aigc_studio_base_url` 只收 `https://`(填跳转后的最终地址;云端只跟随同主机内的 307/308),否则 400。config.json 损坏(解析失败)时各路由返回 500 `{error, code:"config_corrupt"}`,并**拒绝写入** —— 修好或删除该文件后再用,插件不会用默认值覆盖它 |
| `/modal_bridge/job_event` | POST | 前端/调用方上报客户端侧结局(`{job_id, event, detail}`)进后端日志留痕 |

## 无 ComfyUI 直连云端(standalone)

本地 ComfyUI 不是必需品——云端 app 本身就是一组独立 REST endpoint(自建 bridge_key 鉴权),
上面的本地 API 只是它的「全功能前台」。脱离 ComfyUI 的消费/自建方式(0.7.3+):

**云端协议**(`https://<ws>--comfyui-bridge` + `-{label}.modal.run`):

| label | 方法 | 说明 |
|---|---|---|
| `-run` | POST | `{workflow, job_id?, images?, gpu_class?, needs_gpu?, tier?, local_nodes?, delivery?, auth_key}` → `{id, status, gpu}`。`job_id` 是幂等键:同一 id 已有记录时回 `{id, status, gpu, duplicate:true}`,不会重复开任务 |
| `-status` | GET | `?job_id=`,请求头 `X-Bridge-Key` → 状态对象(同上文 poll 的透传源;云端本身不回 `auth_failed` / `unknown`,那是本机路由加的) |
| `-fetch` | GET | `?job_id=&path=<volume_path>`,请求头 `X-Bridge-Key` → **流式下载大文件产物**(路径囚笼在该 job 目录;这是外部消费者不需要 modal token 的关键)。下载**永不删除**;客户端确认所有文件完整落盘后再带 `ack=1` 请求一次,云端才删副本。旧的 `delete=1` 已停用(无操作) |
| `-cancel` | POST | `{job_id, auth_key}` → 取消成功回 `status:"cancelled"`;任务早已结束回字段子集 `{id, status, error?, gpu, gpu_actual, completed_at, cancel_noop:true, was_running}`(不含产物);取消失败回 `{error}` |
| `-health` | GET | 请求头 `X-Bridge-Key` → 部署版本/卡型/已装节点(节点来源地址里的凭据已脱敏) |

GET 的 `?key=` 仍兼容,但会进反代 / CDN 日志,新客户端请用请求头。

**job_id 规则**:`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`,不得含 `..`;**一次性,不要复用** —— 要重跑就用新 id
(旧的 `rerun=1` 已删除)。记录被回收后复用同一 id 提交,服务端拦不住,而且和回收之间有竞态,不受支持。

**产物**:`images[]` 每项带 `filename / node_id / key / size_bytes`,小文件带 `data_base64`,大文件带 `volume_path`
(走 `-fetch`)。客户端用 `size_bytes` 校验下载完整性;核对不了时不要 ack,交给云端按保留期回收。

**bridge_client 的错误分类**(`submit`):`SubmitUnknown`(带 `.job_id`)= 结果未知,可能已在跑,去 poll,别换 id 重交;
以 `401` 开头(bridge key 不对)或 `/run:` 开头(如 `/run: HTTP {status} …`)的 `BridgeError` = 确定没提交;每次尝试都失败在连接阶段(请求没发出去)也按确定没提交报。
一旦有过一次结果不确定的尝试,之后的拒收也报 `SubmitUnknown`。`cli.json` 损坏时 `bridge_cli` 中止,不会静默当成空配置。

**三种消费方式**(都基于 `bridge_client.py`,纯 stdlib 零依赖):

1. **Python 库**:`BridgeClient(endpoint, key)` → `submit / wait / download_outputs`(base64 与 Volume 大文件双路径自动处理,输入图 `pack_input_images` 打包)
2. **CLI**(`bridge_cli.py`):消费者 `configure → submit --wait`;自建者 `deploy`(复用插件的 env 链路,避开裸 `modal deploy` 陷阱)+ `upload-model`(模型上 Volume)
3. **MCP cloud 模式**(`mcp_server.py`):设 `MODAL_BRIDGE_ENDPOINT` + `MODAL_BRIDGE_KEY` 即切换,agent 工具面不变

**能力边界**:standalone 只「消费」部署好的能力——模型要先在 Volume(部署者同步过,或 `upload-model` 手动放)、custom_node 要先在镜像(部署者本机同步过);显存档位 `gpu_class` 手选,没有本地估算路由。

## 给 agent 的注意事项

- **等待窗**:从第一次看到 `running` 起算 `worker_timeout_sec` + 180s;排队阶段不要自己判超时(云端排队 6 小时才判死)
- **提交结果未知**:拿到 job_id 的 `outcome:"unknown"` / `SubmitUnknown` 时去 poll 这个 id,别换新 id 重提
- **配置生效链路**:`gpu_tier` 改完即生效;`default_gpu`/`cheap_gpu`/`use_sage_attention`/`worker_timeout_sec` 等要 `/deploy` 后生效
- **取消要核验**:`cancel` 返回 `still_billing:true` 时任务可能仍在计费;`cancel_noop` / `not_found` 都不是取消失败
- **显存不足的形态**是静默降速不是报错:`progress.s_it` 显著高于同配置基线即是信号
