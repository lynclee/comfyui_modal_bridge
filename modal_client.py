"""
modal_client.py — 调用 Modal endpoint(私有 endpoint,自建鉴权:GET 走 X-Bridge-Key 头,POST 走 body auth_key)
"""
import asyncio
import json
import uuid
from typing import Optional

import aiohttp

try:                                   # 插件里是包内相对导入;CLI / 测试里是顶层模块
    from . import health_client
except ImportError:
    import health_client


class SubmitUnknown(RuntimeError):
    """/run 的重试全部失败,而**至少有一次请求可能已经到达云端** —— 任务也许已在排队 / 运行、在计费。

    .job_id 是这次提交用的幂等键:调用方拿它去 status / cancel 核实,**绝不能换新 id 重新提交**
    (双跑双计费)。以前重试用尽只抛 last_err,job_id 跟着丢了,云端那个任务没人看得见
    (2026-10-05 深度 review,契约 C3)。本机 /submit 捕获它回 502 {error, job_id, outcome: "unknown"}。
    「确定没提交」(401、其它 4xx、云端回 {"error"} 拒收、每次都失败在连接阶段)照旧抛普通 RuntimeError ——
    但前面有过结果不确定的尝试时,之后的拒收也抛它(分类规则见 submit_job)。"""

    def __init__(self, msg: str, job_id: str):
        super().__init__(msg)
        self.job_id = job_id


def _endpoint(base: str, label: str) -> str:
    """https://<ws>--comfyui-bridge + '-run' → https://<ws>--comfyui-bridge-run.modal.run"""
    return f"{base.rstrip('/')}-{label}.modal.run"


# ⚠ 所有带 key 的请求都传 allow_redirects=False:aiohttp 跨域跳转时只剥 Authorization,自定义的
#   X-Bridge-Key 照带,307/308 还会把 body 里的 auth_key 原样重发给跳转目标(2026-09-26 review)。
#   我们的 endpoint 超时都 ≤60s,碰不到 Modal 对 150s 以上请求才发的 303,合法流程里不会有重定向。


def _key(cfg: dict) -> str:
    """自建鉴权 key(私有 endpoint 用)。GET 走 X-Bridge-Key 头(不进 query,避免落进
    反代 / CDN 日志),POST 走 body auth_key。云端 ≥0.8.3 认这个头。"""
    return cfg.get("bridge_api_key", "")


async def submit_job(
    session: aiohttp.ClientSession,
    cfg: dict,
    workflow: dict,
    input_images: Optional[list] = None,
    tier: str = "40g",
    needs_gpu: bool = True,
    gpu_class: str = "primary",
    local_nodes: Optional[dict] = None,
    max_retries: int = 3,
    job_id: Optional[str] = None,
) -> dict:
    """POST /run,带鉴权;结果未知的失败按指数退避重试,默认最多 3 次(最坏耗时 4×60s + 1.5+3+6s
    = 250.5s,MCP 本地模式的 /submit 超时按它定)。
    needs_gpu=False → 后端路由到 CPU worker(纯 API/无模型工作流,省钱)。
    gpu_class='cheap' → 显存放得下的工作流降到便宜卡(L40S);'primary' → 主卡(H100)。
    job_id:调用方自带的幂等键(契约 D2,本机 /submit 已按 C1 校验过);不给就生成一个。

    每次尝试的结局分三类(2026-10-05 深度 review 第二轮):
      · 没发出去:连接阶段就失败(aiohttp.ClientConnectorError 一族 —— DNS / 连接被拒 / TLS 握手 /
        连不上代理;以及代理拒绝 CONNECT 的 ClientHttpProxyError)。请求没到云端,重试安全;
        **全部**尝试都是这一类 → 抛普通 RuntimeError(确定没提交)。
      · 结果不确定:超时、读响应时断连、5xx / 408 / 429、非 JSON、缺 id、拒绝跟随的重定向 —— 请求可能
        已经到了云端、spawn 了。重试(同一个 job_id,云端回 duplicate,不会双跑)。
      · 确定拒收:401 / 其它 4xx / 云端回 {"error"}。不重试,抛 RuntimeError。
    重试全部失败抛 SubmitUnknown(带 .job_id)。**之前有过结果不确定的尝试时,之后的确定拒收也抛
    SubmitUnknown**(附拒收原文):前一次可能已经 spawn,后一次的 401 / 404 说明不了它的结局 ——
    以前直接抛「确定没提交」,job_id 跟着丢了。"""
    url = _endpoint(cfg["modal_endpoint_base"], "run")
    job_id = job_id or str(uuid.uuid4())
    payload = {
        "workflow": workflow,
        "user_id": cfg.get("user_id", "local-dev"),
        "tier": tier,
        "needs_gpu": bool(needs_gpu),
        "gpu_class": gpu_class,
        # 本地提交固定 desktop 交付(结果回本地);缺省也是 desktop,显式写上便于排查。
        # 旧 incognito 字段已删:服务端从未消费,语义由 delivery.mode 取代。
        "delivery": {"mode": "desktop"},
        "auth_key": _key(cfg),
        # 幂等键:客户端定 job_id,下面的重试循环用同一个。
        # 不带的话服务端每次 uuid4 新建,而 502/504/超时的那一次 spawn 可能其实已经成功
        # (只是响应丢在网关)—— 重试就等于再开一个同样的 GPU 任务,双跑双计费,
        # 前端只拿得到第二个 id,第一个在后台烧到跑完谁也不知道。
        "job_id": job_id,
    }
    if input_images:
        payload["images"] = input_images
    if local_nodes:
        # {folder: digest} —— 自写节点的期望版本。暖容器若装着旧版会据此自我纠偏
        # (解压发生在容器启动时,而暖容器不会重新启动)。
        payload["local_nodes"] = local_nodes

    def rejected(msg: str):
        """确定拒收。之前有结果不确定的尝试 → SubmitUnknown(附原文);没有 → 确定没提交。"""
        if not uncertain:
            return RuntimeError(msg)
        return SubmitUnknown(
            f"提交结果未知 job_id={job_id}:前面 {uncertain} 次尝试结果不确定(请求可能已到云端并开跑),"
            f"之后这次被拒收({msg})—— 拒收说明不了前一次的结局。先用 status/cancel 核实这个 job_id,"
            f"别重新提交(会双跑双计费)", job_id)

    headers = {"Content-Type": "application/json"}
    last_err: Optional[Exception] = None
    attempts = 0
    uncertain = 0            # 结果不确定的尝试次数(可能已经到了云端)
    for attempt in range(max_retries + 1):
        if attempt:
            # 指数退避:网关 504 多半是 run_endpoint 冷启动慢,紧接着重试大概率撞同一个冷容器
            await asyncio.sleep(1.5 * 2 ** (attempt - 1))
        attempts += 1
        try:
            async with session.post(url, json=payload, headers=headers, allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=60)) as r:
                raw = await r.read()
                text = raw.decode("utf-8", "replace")
                if 300 <= r.status < 400:
                    # 不跟随(会把 bridge key 带过去),也不重试(同一个地址还会回同一个跳转)。
                    # 跳转不说明请求有没有被处理,按「结果未知」交还 job_id。
                    uncertain += 1
                    last_err = RuntimeError(f"Modal /run 返回重定向 {r.status},没有跟随(会把 bridge key 带过去)")
                    break
                if r.status == 401:
                    raise rejected("Modal /run 401 — bridge key 不对/缺失。点 [Modal Setup] 重新部署会刷新 key")
                # 4xx = 请求被拒、没有进到 spawn(408 / 429 除外:超时与限流不说明请求被处理了没有)
                if 400 <= r.status < 500 and r.status not in (408, 429):
                    raise rejected(f"Modal /run failed {r.status}: {text[:500]}")
                if r.status >= 400:
                    uncertain += 1
                    last_err = RuntimeError(f"Modal /run transient {r.status}: {text[:200]}")
                    print(f"[modal_bridge] /run attempt {attempt+1} got {r.status}, retrying...")
                    continue
                try:
                    data = json.loads(raw)
                except ValueError:
                    uncertain += 1
                    last_err = RuntimeError(f"Modal /run non-JSON: {text[:300]}")
                    print(f"[modal_bridge] /run attempt {attempt+1} got non-JSON, retrying...")
                    continue
                if isinstance(data, dict) and "error" in data:
                    raise rejected(f"Modal /run error: {data['error']}")   # 云端校验没过,确定没提交
                if isinstance(data, dict) and data.get("id"):
                    return data
                uncertain += 1
                last_err = RuntimeError(f"Modal /run missing id: {str(data)[:300]}")
        except (aiohttp.ClientConnectorError, aiohttp.ClientHttpProxyError) as e:
            # 连接阶段就失败了(见 docstring),请求没发出去:重试安全,不计入「结果不确定」
            last_err = e
            print(f"[modal_bridge] /run attempt {attempt+1} connect err: {e}, retrying...")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            uncertain += 1
            last_err = e
            print(f"[modal_bridge] /run attempt {attempt+1} network err: {e}, retrying...")
    if not uncertain:
        raise RuntimeError(
            f"Modal /run 连不上,请求没有发出去({attempts} 次尝试都失败在连接阶段;最后一次:{last_err})。"
            f"任务没有提交,网络恢复后直接重新提交即可")
    raise SubmitUnknown(
        f"提交结果未知 job_id={job_id}:{attempts} 次尝试都没拿到确定答复(最后一次:{last_err})。"
        f"任务可能已在云端排队 / 运行 —— 先用 status/cancel 核实这个 job_id,别重新提交(会双跑双计费)",
        job_id)


async def health(session, cfg) -> dict:
    """健康检查(异步,最多 3 轮)。URL、鉴权头、状态码语义与同步版共用 health_client 的一份规则。
    401 / 404 重试无意义,直接抛;其它 4xx/5xx 与网络错重试。"""
    last = None
    for attempt in range(3):
        try:
            async with session.get(health_client.url(cfg), headers=health_client.headers(cfg),
                                   allow_redirects=False, timeout=aiohttp.ClientTimeout(total=10)) as r:
                # errors="replace":正文不是 UTF-8 时别抛 UnicodeDecodeError 漏出去(它不在下面的 except 里)
                return health_client.interpret(r.status, await r.text(errors="replace"))
        except health_client.HealthUnavailable as e:
            if e.kind in ("unauthorized", "not_deployed"):
                raise
            last = e
            await asyncio.sleep(1.0)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = e
            await asyncio.sleep(1.0)
    raise last or RuntimeError("health failed after retries")


async def cancel(session, cfg, job_id) -> dict:
    """POST /cancel,返回云端响应 dict(读法见契约 C2 / bridge_client.cancel_still_billing:
    带 error 且不是 cancel_noop、不是 not_found,才是「取消失败、可能仍在计费」)。

    HTTP 非 2xx、重定向、响应不是 JSON 对象 → 抛 RuntimeError(取消结果未知,调用方按可能仍在计费报)。
    ⚠ 以前原样返回正文:云端回 500 + {"detail": …}(没有 error 字段)时,本机 /cancel 报 ok:true ——
      取消失败被说成成功(2026-10-05 深度 review,同 bridge_client 的 C8)。"""
    url = _endpoint(cfg["modal_endpoint_base"], "cancel")
    async with session.post(
        url, json={"job_id": job_id, "auth_key": _key(cfg)},
        headers={"Content-Type": "application/json"}, allow_redirects=False,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        raw = await r.read()
        text = " ".join(raw.decode("utf-8", "replace").split())
        if 300 <= r.status < 400:
            raise RuntimeError(f"Modal /cancel 返回重定向 {r.status},没有跟随(会把 bridge key 带过去)")
        if r.status == 401:
            raise RuntimeError("Modal /cancel 401 — bridge key 不对/缺失。点 [Modal Setup] 重新部署会刷新 key")
        if r.status >= 400:
            raise RuntimeError(f"Modal /cancel {r.status}: {text[:300] or '(无正文)'}")
        try:
            data = json.loads(raw)
        except ValueError:
            raise RuntimeError(f"Modal /cancel 返回的不是 JSON: {text[:200]}") from None
        if not isinstance(data, dict):
            raise RuntimeError(f"Modal /cancel 返回的不是 JSON 对象: {text[:200]}")
        return data


# ============================================================================
# custom_nodes(权威源:并入 /health 返回,供本地双向同步对比真实部署的镜像)
# ============================================================================

async def list_nodes(session, cfg) -> dict:
    """镜像已装的 custom_nodes。模型相关全部走本地 SDK(modal_volume.py),这里只剩节点。"""
    h = await health(session, cfg)
    nodes = h.get("custom_nodes") if isinstance(h, dict) else None
    if not isinstance(nodes, list):
        # 以前缺字段时回 [],调用方当成「云端一个节点都没有」(source=modal):对账、并回、cloud_unchecked
        # 全被跳过,正是 /sync_nodes 误删云端节点的那条路(2026-10-05 深度 review)。拿不到就抛,
        # 调用方按「读不到云端」处理。
        why = h.get("custom_nodes_error", "字段缺失") if isinstance(h, dict) else "响应不是对象"
        raise RuntimeError(f"/health 没有报节点清单: {why}")
    manifest = h.get("custom_nodes_manifest")
    return {"custom_nodes": nodes, "custom_nodes_manifest": manifest if isinstance(manifest, list) else []}
