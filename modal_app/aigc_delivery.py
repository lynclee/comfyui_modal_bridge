"""
aigc_delivery.py — AIGC Studio(网站)交付:契约校验 + R2 直传(见 PLUGIN_MODAL_BRIDGE_CHANGE_PLAN.md)。

两种交付模式(/run 的 payload.delivery 决定,缺省 desktop,向后兼容):
  - desktop : 现状不变,结果回本地 ComfyUI(base64 / Volume)。
  - aigc-r2 : 结果流式直传 Cloudflare R2(用 Vercel 签发的短期预签名 PUT 地址),
              再回调通知 AIGC Studio。R2 长期密钥永不进入 Modal。

安全不变量:
  - delivery.token 是单任务临时许可 —— 只在内存里用,不写 job_state、不进日志。
  - Bridge 只拿几分钟有效的预签名 PUT 地址,不接触 R2 长期密钥。

本文件顶层只 import 标准库(requests 延迟到函数内),纯契约部分可在 CI 无依赖单测。
"""
from __future__ import annotations


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
