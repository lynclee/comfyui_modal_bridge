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
    「确定没提交」(401、其它 4xx、云端回 {"error"} 拒收)照旧抛普通 RuntimeError。"""

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
) -> dict:
    """POST /run,带鉴权;结果未知的失败(网络错 / 超时 / 5xx / 408 / 429 / 非 JSON)按指数退避重试,
    默认最多 3 次。重试全部失败抛 SubmitUnknown(带 .job_id);确定没提交的(401 / 其它 4xx /
    云端回 {"error"})直接抛 RuntimeError、不重试。
    needs_gpu=False → 后端路由到 CPU worker(纯 API/无模型工作流,省钱)。
    gpu_class='cheap' → 显存放得下的工作流降到便宜卡(L40S);'primary' → 主卡(H100)。"""
    url = _endpoint(cfg["modal_endpoint_base"], "run")
    job_id = str(uuid.uuid4())
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

    headers = {"Content-Type": "application/json"}
    last_err: Optional[Exception] = None
    attempts = 0
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
                    last_err = RuntimeError(f"Modal /run 返回重定向 {r.status},没有跟随(会把 bridge key 带过去)")
                    break
                if r.status == 401:
                    raise RuntimeError("Modal /run 401 — bridge key 不对/缺失。点 [Modal Setup] 重新部署会刷新 key")
                # 4xx = 请求被拒、没有进到 spawn(408 / 429 除外:超时与限流不说明请求被处理了没有)
                if 400 <= r.status < 500 and r.status not in (408, 429):
                    raise RuntimeError(f"Modal /run failed {r.status}: {text[:500]}")
                if r.status >= 400:
                    last_err = RuntimeError(f"Modal /run transient {r.status}: {text[:200]}")
                    print(f"[modal_bridge] /run attempt {attempt+1} got {r.status}, retrying...")
                    continue
                try:
                    data = json.loads(raw)
                except ValueError:
                    last_err = RuntimeError(f"Modal /run non-JSON: {text[:300]}")
                    print(f"[modal_bridge] /run attempt {attempt+1} got non-JSON, retrying...")
                    continue
                if isinstance(data, dict) and "error" in data:
                    raise RuntimeError(f"Modal /run error: {data['error']}")   # 云端校验没过,确定没提交
                if isinstance(data, dict) and data.get("id"):
                    return data
                last_err = RuntimeError(f"Modal /run missing id: {str(data)[:300]}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = e
            print(f"[modal_bridge] /run attempt {attempt+1} network err: {e}, retrying...")
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
    nodes = h.get("custom_nodes", []) if isinstance(h, dict) else []
    manifest = h.get("custom_nodes_manifest") if isinstance(h, dict) else None
    return {"custom_nodes": nodes, "custom_nodes_manifest": manifest or []}
