"""
aigc_delivery.py — AIGC Studio(网站)交付:契约校验 + R2 直传(见 PLUGIN_MODAL_BRIDGE_CHANGE_PLAN.md)。

两种交付模式(/run 的 payload.delivery 决定,缺省 desktop,向后兼容):
  - desktop : 现状不变,结果回本地 ComfyUI(base64 / Volume)。
  - aigc-r2 : 结果流式直传 Cloudflare R2(用 Vercel 签发的短期预签名 PUT 地址),
              再回调通知 AIGC Studio。R2 长期密钥永不进入 Modal。

安全不变量:
  - delivery.token 是单任务临时许可 —— 只在内存里用,不写 job_state、不进日志。
  - Bridge 只拿几分钟有效的预签名 PUT 地址,不接触 R2 长期密钥。

交付流程(deliver_outputs,每个产物一轮):
  ComfyUI /view 流式读 → 写 /tmp(边写边算 size + SHA-256)→ POST asset-intake 换预签名
  PUT 地址 → requests.put 直传 R2(记 ETag)→ 删临时文件;全部传完 POST job-complete。
  sha256 只作 metadata 存档(Vercel 不下载文件,核验只信 R2 实际 size + ETag)。

重试与幂等(与计划 §6 一致):
  - intake ≤3 / PUT ≤3(预签名过期则重新 intake 换地址)/ complete ≤5,退避 1/2/4/8s
  - 4xx(token 失效 / 类型不允许 / size 超限)不重试;5xx / 网络超时重试
  - 同 (job_id, asset_type, position) 重复 intake 恒返同一 r2_key(Vercel 唯一约束)

本文件顶层只 import 标准库(requests 延迟到函数内),纯契约部分可在 CI 无依赖单测。
HTTP 动作(poster/putter/streamer)全部可注入 —— 单测喂假实现,不碰网络。
"""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import tempfile
import time


VALID_MODES = ("desktop", "aigc-r2")
DEFAULT_DELIVERY = {"mode": "desktop"}


def normalize_delivery(payload: dict) -> tuple[dict, str | None]:
    """从 /run payload 取出并校验 delivery。返回 (delivery, error)。

    - 没传 delivery → 默认 {"mode": "desktop"}(老客户端向后兼容)。
    - mode ∉ {desktop, aigc-r2} → error "unsupported delivery mode"。
    - aigc-r2 必须带非空 job_id(AIGC Studio 任务 UUID)和 token(单任务临时许可)。
    error 非 None 时 delivery 无意义,调用方应直接拒绝请求。
    """
    raw = payload.get("delivery")
    if raw is None:
        return dict(DEFAULT_DELIVERY), None
    if not isinstance(raw, dict):
        return {}, "invalid delivery: must be an object"
    mode = raw.get("mode")
    if mode not in VALID_MODES:
        return {}, "unsupported delivery mode"
    if mode == "aigc-r2":
        if not raw.get("job_id") or not isinstance(raw.get("job_id"), str):
            return {}, "aigc-r2 delivery requires 'job_id'"
        if not raw.get("token") or not isinstance(raw.get("token"), str):
            return {}, "aigc-r2 delivery requires 'token'"
    return raw, None


def public_delivery(delivery: dict | None) -> dict:
    """delivery 的可外泄形态:只留 mode / job_id,剥掉 token 等敏感字段。
    job_state、日志、/status 响应一律只用这个,绝不放原始 delivery。"""
    d = delivery or DEFAULT_DELIVERY
    out = {"mode": d.get("mode", "desktop")}
    if d.get("job_id"):
        out["job_id"] = d["job_id"]
    return out


# ============================================================================
# R2 直传引擎(aigc-r2 模式)
# ============================================================================
COMFY_HOST = "127.0.0.1:8188"  # worker 容器内的 ComfyUI(与 _comfy_ws 一致)
UPLOAD_TIMEOUT = int(os.environ.get("AIGC_UPLOAD_TIMEOUT", "600"))  # 单文件 PUT 超时(s)

INTAKE_TRIES = 3
PUT_TRIES = 3
COMPLETE_TRIES = 5
_BACKOFF_S = [1, 2, 4, 8]  # 第 n 次失败后等 _BACKOFF_S[min(n-1, len-1)] 秒

# mimetypes 缺的补上(3D 容器格式)
_CONTENT_TYPE_OVERRIDES = {
    ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
    ".obj": "model/obj", ".fbx": "application/octet-stream",
    ".stl": "model/stl", ".ply": "application/octet-stream",
    ".splat": "application/octet-stream", ".spz": "application/octet-stream",
    ".ksplat": "application/octet-stream", ".webp": "image/webp",
}

_sleep = time.sleep  # 单测里可替换成 no-op


class DeliveryError(Exception):
    """交付失败。retryable=False 表示 4xx 类(token 失效/类型不允许/size 超限),重试无意义。"""

    def __init__(self, msg: str, status: int | None = None, retryable: bool = False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


def safe_filename(name: str) -> str:
    """只留 basename,剔除路径分隔与控制字符(intake 的 filename 字段用)。"""
    base = os.path.basename(str(name or "")).strip()
    base = re.sub(r"[^\w.\-()\[\] ]", "_", base)
    return base or "output.bin"


def detect_content_type(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _CONTENT_TYPE_OVERRIDES:
        return _CONTENT_TYPE_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or "application/octet-stream"


def website_headers() -> dict:
    """调 Vercel internal 接口的公共头。bypass 密钥来自 Modal Secret,只进请求头、不进日志。"""
    h = {"Content-Type": "application/json"}
    bypass = os.environ.get("AIGC_STUDIO_BYPASS_SECRET")
    if bypass:
        h["x-vercel-protection-bypass"] = bypass
    return h


def _studio_base_url() -> str:
    url = (os.environ.get("AIGC_STUDIO_BASE_URL") or "").rstrip("/")
    if not url:
        raise DeliveryError("AIGC_STUDIO_BASE_URL not configured in Modal Secret")
    return url


def is_retryable_status(status: int | None) -> bool:
    """错误分类:5xx / 网络层(None)可重试;4xx(token 失效、类型不允许、超限)不重试。"""
    return status is None or status >= 500


def _default_poster(url: str, body: dict, headers: dict, timeout: int):
    """POST JSON → (status_code, 解析后的 dict 或原始文本)。网络异常 → (None, 错误串)。"""
    import requests
    try:
        r = requests.post(url, json=body, headers=headers, timeout=timeout)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:500]


def _default_putter(put_url: str, file_path: str, headers: dict, timeout: int):
    """流式 PUT 文件到预签名地址 → (status_code, 响应头 dict)。网络异常 → (None, {})。
    只带 required_headers(Content-Type),不自加签名头;Content-Length 由 requests 按文件算。"""
    import requests
    try:
        with open(file_path, "rb") as src:
            r = requests.put(put_url, data=src, headers=headers, timeout=timeout)
        return r.status_code, dict(r.headers)
    except Exception as e:
        print(f"[bridge] R2 PUT failed: {type(e).__name__}: {e}")
        return None, {}


def stream_output_to_temp(ref: dict) -> tuple[str, int, str]:
    """从 ComfyUI /view 流式读产物 → 写 /tmp,边写边算 size + SHA-256(不整体进内存)。
    返回 (temp_path, size_bytes, sha256_hex)。caller 负责删临时文件。"""
    import urllib.parse

    import requests
    params = urllib.parse.urlencode({
        "filename": ref["filename"], "subfolder": ref.get("subfolder") or "",
        "type": ref.get("type") or "output",
    })
    sha = hashlib.sha256()
    size = 0
    fd, temp_path = tempfile.mkstemp(prefix="aigc_", dir="/tmp")
    try:
        with requests.get(f"http://{COMFY_HOST}/view?{params}", stream=True, timeout=60) as r:
            r.raise_for_status()
            with os.fdopen(fd, "wb") as out:
                for chunk in r.iter_content(1024 * 1024):
                    out.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return temp_path, size, sha.hexdigest()


def post_json_with_retry(url: str, body: dict, headers: dict, max_tries: int,
                         poster=None, timeout: int = 30) -> dict:
    """带退避的 POST。2xx 返回解析结果;4xx 立即 DeliveryError(不重试);
    5xx/网络错误重试到用尽。⚠ body 含 token,这里绝不打印 body。"""
    poster = poster or _default_poster
    last = "no attempt"
    for attempt in range(max_tries):
        status, resp = poster(url, body, headers, timeout)
        if status is not None and 200 <= status < 300:
            # 2xx 一律算成功:job-complete 完全可能回 204/空 body,不能因为体不是
            # JSON 就误判成拒绝。需要具体字段的调用方(intake)自己校验缺字段。
            return resp if isinstance(resp, dict) else {}
        last = f"HTTP {status}: {str(resp)[:300]}"
        if not is_retryable_status(status):
            raise DeliveryError(f"{url.rsplit('/', 1)[-1]} rejected — {last}",
                                status=status, retryable=False)
        if attempt < max_tries - 1:
            _sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
    raise DeliveryError(f"{url.rsplit('/', 1)[-1]} failed after {max_tries} tries — {last}",
                        retryable=True)


def _intake_one(base_url: str, job_id: str, token: str, asset_type: str, position: int,
                filename: str, content_type: str, size: int, poster=None) -> dict:
    """POST asset-intake 换 {r2_key, put_url, required_headers, ...}。幂等:重复调恒返同一 r2_key。"""
    resp = post_json_with_retry(
        f"{base_url}/api/internal/asset-intake",
        {"job_id": job_id, "token": token, "asset_type": asset_type, "position": position,
         "filename": filename, "content_type": content_type, "size_bytes": size},
        website_headers(), INTAKE_TRIES, poster=poster)
    if not resp.get("put_url") or not resp.get("r2_key"):
        raise DeliveryError(f"asset-intake malformed response: {str(resp)[:300]}")
    return resp


def deliver_one(job_id: str, token: str, position: int, ref: dict,
                poster=None, putter=None, streamer=None) -> dict:
    """交付单个产物:流式落 /tmp → intake → PUT 直传 R2 → 返回 asset 记录(交 job-complete)。
    PUT 失败(含预签名过期)→ 重新 intake 换新地址再传,最多 PUT_TRIES 轮。"""
    putter = putter or _default_putter
    streamer = streamer or stream_output_to_temp
    base_url = _studio_base_url()
    filename = safe_filename(ref["filename"])
    content_type = detect_content_type(filename)
    asset_type = ref.get("asset_type") or "image"

    temp_path, size, sha256 = streamer(ref)
    try:
        last = "no attempt"
        for attempt in range(PUT_TRIES):
            # 每轮重新 intake:预签名只有几分钟有效,过期/失败后旧地址不可复用。
            # 幂等由 Vercel 保证(同 job/type/position 恒返同一 r2_key),多调无害。
            intake = _intake_one(base_url, job_id, token, asset_type, position,
                                 filename, content_type, size, poster=poster)
            status, resp_headers = putter(intake["put_url"], temp_path,
                                          intake.get("required_headers") or {}, UPLOAD_TIMEOUT)
            if status is not None and 200 <= status < 300:
                return {
                    "asset_type": intake.get("asset_type") or asset_type,
                    "position": position,
                    "r2_key": intake["r2_key"],
                    "content_type": intake.get("content_type") or content_type,
                    "size_bytes": size,
                    "etag": (resp_headers or {}).get("ETag") or (resp_headers or {}).get("etag"),
                    "checksum_sha256": sha256,
                }
            last = f"HTTP {status}"
            if attempt < PUT_TRIES - 1:
                _sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
        raise DeliveryError(f"R2 PUT failed after {PUT_TRIES} tries — {last}", retryable=True)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def deliver_outputs(job_id: str, output_refs: list[dict], delivery: dict,
                    provider_job_id: str = "",
                    poster=None, putter=None, streamer=None) -> dict:
    """aigc-r2 交付入口:逐个产物直传 R2,全部成功后 POST job-complete。
    返回 {"status": "completed" | "callback_failed", "assets": [...manifest...]}。
    - position 按 asset_type 各自从 0 计(r2_key 形如 <type>-<position>,幂等键也是它)
    - 任一产物传不上去 → raise DeliveryError(job 记 failed)
    - 文件全传成功但 job-complete 回调用尽重试 → 不算失败:返回 callback_failed +
      完整 manifest,caller 存进 job_state,AIGC Studio 轮询 /status 拿 manifest 兜底落库。
    """
    token = delivery["token"]
    if not output_refs:
        raise DeliveryError("no outputs to deliver")
    assets: list[dict] = []
    positions: dict[str, int] = {}
    for ref in output_refs:
        at = ref.get("asset_type") or "image"
        pos = positions.get(at, 0)
        positions[at] = pos + 1
        assets.append(deliver_one(job_id, token, pos, ref,
                                  poster=poster, putter=putter, streamer=streamer))
    print(f"[bridge] aigc-r2: uploaded {len(assets)} asset(s) for job {job_id}")
    try:
        post_json_with_retry(
            f"{_studio_base_url()}/api/internal/job-complete",
            {"job_id": job_id, "token": token, "provider_job_id": provider_job_id,
             "assets": assets},
            website_headers(), COMPLETE_TRIES, poster=poster)
    except DeliveryError as e:
        # 文件已在 R2,只是没通知到 —— 保留 manifest 让 /status 兜底,不丢结果。
        print(f"[bridge] job-complete callback failed (assets safe in R2): {e}")
        return {"status": "callback_failed", "assets": assets}
    return {"status": "completed", "assets": assets}
