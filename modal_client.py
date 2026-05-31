"""
modal_client.py — 调用 Modal endpoint(私有 endpoint,自建鉴权:GET 走 ?key=,POST 走 body auth_key)
"""
import asyncio
import time
from typing import Optional

import aiohttp


def _endpoint(base: str, label: str) -> str:
    """https://lync5134--comfyui-bridge + '-run' → https://lync5134--comfyui-bridge-run.modal.run"""
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
    max_retries: int = 1,
) -> dict:
    """POST /run,带鉴权,自动重试 1 次。tier=显存档(80g/40g),后端按档选带原生 fallback 的 worker"""
    url = _endpoint(cfg["modal_endpoint_base"], "run")
    payload = {
        "workflow": workflow,
        "user_id": cfg.get("user_id", "local-dev"),
        "tier": tier,
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


async def poll_status(session, cfg, job_id, on_progress=None) -> dict:
    """GET /status,轮询直到 completed / failed / cancelled"""
    url = _endpoint(cfg["modal_endpoint_base"], "status")
    interval = float(cfg.get("poll_interval_sec", 1.5))
    timeout = float(cfg.get("timeout_sec", 1200))
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        async with session.get(
            url, params={"job_id": job_id, "key": _key(cfg)},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            text = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"Modal /status {r.status}: {text[:500]}")
            try:
                data = await r.json(content_type=None)
            except Exception:
                raise RuntimeError(f"Modal /status non-JSON: {text[:500]}")
        if "error" in data:
            raise RuntimeError(f"Modal /status error: {data['error']}")
        status = data.get("status")
        if status != last_status:
            last_status = status
            if on_progress:
                on_progress(status, data)
        if status in ("completed", "failed", "cancelled"):
            return data
        await asyncio.sleep(interval)
    raise TimeoutError(f"Modal job {job_id} timed out after {timeout}s")


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
# 模型管理
# ============================================================================

async def list_models(session, cfg, type_: Optional[str] = None) -> dict:
    url = _endpoint(cfg["modal_endpoint_base"], "list-models")
    params = {"key": _key(cfg)}
    if type_:
        params["type"] = type_
    async with session.get(url, params=params,
                           timeout=aiohttp.ClientTimeout(total=30)) as r:
        return await r.json(content_type=None)


async def check_models(session, cfg, required: list) -> dict:
    """批量检查模型存在性。带重试(check 类 endpoint 易遇瞬时连接抖动/代理波动)。"""
    url = _endpoint(cfg["modal_endpoint_base"], "check-models")
    body = {"required": required, "auth_key": _key(cfg)}
    last = None
    for attempt in range(3):
        try:
            async with session.post(
                url, json=body, headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                return await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = e
            print(f"[modal_bridge] check_models attempt {attempt+1} 连接失败: {e},重试...")
            await asyncio.sleep(1.5)
    raise last or RuntimeError("check_models failed after retries")


async def seed_model(session, cfg, item: dict) -> dict:
    """
    触发 Modal 端下载一个模型。同步等待(可能很久)。
    item: {type, filename, source, repo?, hf_filename?, url?, requires_token?}
    """
    url = _endpoint(cfg["modal_endpoint_base"], "seed-model")
    timeout = aiohttp.ClientTimeout(total=cfg.get("seed_timeout_sec", 1800))
    async with session.post(
        url, json={**item, "auth_key": _key(cfg)},
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    ) as r:
        return await r.json(content_type=None)


async def list_nodes(session, cfg) -> dict:
    """镜像已装的 custom_nodes(权威源)。并入 /health 返回(Starter plan 限 8 endpoint)。"""
    h = await health(session, cfg)
    nodes = h.get("custom_nodes", []) if isinstance(h, dict) else []
    return {"custom_nodes": nodes}


async def seed_status(session, cfg, type_: str, filename: str) -> dict:
    """查模型下载状态(用于前端 polling 显示进度)"""
    url = _endpoint(cfg["modal_endpoint_base"], "seed-status")
    async with session.get(
        url, params={"type": type_, "filename": filename, "key": _key(cfg)},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as r:
        return await r.json(content_type=None)
