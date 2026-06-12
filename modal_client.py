"""
modal_client.py — 调用 Modal endpoint(私有 endpoint,自建鉴权:GET 走 ?key=,POST 走 body auth_key)
"""
import asyncio
from typing import Optional

import aiohttp


def _endpoint(base: str, label: str) -> str:
    """https://<ws>--comfyui-bridge + '-run' → https://<ws>--comfyui-bridge-run.modal.run"""
    return f"{base.rstrip('/')}-{label}.modal.run"


def _key(cfg: dict) -> str:
    """自建鉴权 key(私有 endpoint 用)。GET 走 query ?key=,POST 走 body auth_key。"""
    return cfg.get("bridge_api_key", "")


async def submit_job(
    session: aiohttp.ClientSession,
    cfg: dict,
    workflow: dict,
    input_images: Optional[list] = None,
    tier: str = "40g",
    needs_gpu: bool = True,
    max_retries: int = 1,
) -> dict:
    """POST /run,带鉴权,自动重试 1 次。needs_gpu=False → 后端路由到 CPU worker(纯 API/无模型工作流,省钱)。"""
    url = _endpoint(cfg["modal_endpoint_base"], "run")
    payload = {
        "workflow": workflow,
        "user_id": cfg.get("user_id", "local-dev"),
        "tier": tier,
        "needs_gpu": bool(needs_gpu),
        "incognito": bool(cfg.get("incognito", True)),
        "auth_key": _key(cfg),
    }
    if input_images:
        payload["images"] = input_images

    headers = {"Content-Type": "application/json"}
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=60)) as r:
                text = await r.text()
                if r.status == 401:
                    raise RuntimeError("Modal /run 401 — bridge key 不对/缺失。点 [Modal Setup] 重新部署会刷新 key")
                if r.status in (502, 503, 504):
                    last_err = RuntimeError(f"Modal /run transient {r.status}: {text[:200]}")
                    print(f"[modal_bridge] /run attempt {attempt+1} got {r.status}, retrying...")
                    await asyncio.sleep(1.5)
                    continue
                if r.status >= 400:
                    raise RuntimeError(f"Modal /run failed {r.status}: {text[:500]}")
                try:
                    data = await r.json(content_type=None)
                except Exception:
                    raise RuntimeError(f"Modal /run non-JSON: {text[:500]}")
                if "error" in data:
                    raise RuntimeError(f"Modal /run error: {data['error']}")
                if "id" not in data:
                    raise RuntimeError(f"Modal /run missing id: {data}")
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = e
            print(f"[modal_bridge] /run attempt {attempt+1} network err: {e}, retrying...")
            await asyncio.sleep(1.5)
    raise last_err or RuntimeError("submit_job failed after retries")


async def health(session, cfg) -> dict:
    url = _endpoint(cfg["modal_endpoint_base"], "health")
    last = None
    for attempt in range(3):
        try:
            async with session.get(url, params={"key": _key(cfg)},
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = e
            await asyncio.sleep(1.0)
    raise last or RuntimeError("health failed after retries")


async def cancel(session, cfg, job_id) -> dict:
    url = _endpoint(cfg["modal_endpoint_base"], "cancel")
    async with session.post(
        url, json={"job_id": job_id, "auth_key": _key(cfg)},
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        return await r.json(content_type=None)


# ============================================================================
# custom_nodes(权威源:并入 /health 返回,供本地双向同步对比真实部署的镜像)
# ============================================================================

async def list_nodes(session, cfg) -> dict:
    """镜像已装的 custom_nodes。模型相关全部走本地 SDK(modal_volume.py),这里只剩节点。"""
    h = await health(session, cfg)
    nodes = h.get("custom_nodes", []) if isinstance(h, dict) else []
    return {"custom_nodes": nodes}
