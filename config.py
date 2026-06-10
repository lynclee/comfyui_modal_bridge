"""
config.py — 配置文件管理
路径:ComfyUI/user/default/modal_bridge/config.json
"""
import json
from pathlib import Path

# 默认配置(用户问答确认)
DEFAULT_CONFIG = {
    # ── Endpoint(deploy.py 自动写)──
    "modal_endpoint_base": "https://YOUR_WORKSPACE--comfyui-bridge",
    "modal_app_name": "comfyui-bridge",
    "modal_workspace": "",                       # 用于拼 endpoint
    "modal_volume_name": "comfyui-bridge-models",  # 重部署(加 custom_node)时要
    "scaledown_window": 40,                      # 空闲多久回收容器(s);重部署时要

    # ── 鉴权(私有 endpoint,deploy.py / GUI 部署自动写)──
    "modal_token_id": "",      # ak-xxx(account token,仅本机 deploy 用)
    "modal_token_secret": "",  # as-xxx
    "bridge_api_key": "",      # 部署时随机生成,调私有 endpoint 用(自建鉴权)
    "comfy_api_key": "",       # 可选:comfy.org API key,供工作流里的 ComfyUI API 节点鉴权(账单走你的 comfy.org)

    # ── 运行选项 ──
    "default_gpu": "H100",
    "enable_snapshot": True,   # 内存快照(实验):冷启 ~30s→~5s。默认开;不支持的 GPU 档自动退化为普通冷启(不更差)。换值需重新部署
    "user_id": "local-dev",
    "incognito": True,         # base64 回流,不上 R2
    "poll_interval_sec": 1.5,
    # worker(Modal)单任务超时上限(秒)。覆盖最慢类别(视频)——见 categories.max_worker_timeout_s()。
    # 是上限不是每任务时长:高上限不拖慢快任务(按实际运行计费)。换值需重新部署生效。
    "worker_timeout_sec": 1800,
    "output_subfolder": "modal_results",

    # ── 模型自动同步(本地 → Modal Volume,SDK batch_upload,CAS 去重)──
    # 提交前检查 Volume,工作流要、Volume 没、但本地有的模型自动上传上去。
    # 不再从 HF/civitai 下载——模型都在本地 ComfyUI Desktop 下好。
    "auto_sync_models": True,
    "model_sync_timeout_sec": 3600,  # 上传整批模型的超时(大模型走上行带宽)

    # ── custom_node 双向同步 ──
    # 提交前对比工作流用到的 custom_node 与 Modal 镜像:缺的加、本地 commit 变了的更新、
    # 本地已卸载的从镜像清单里删掉,再重部署。本地始终是真源。
    "auto_check_nodes": True,
}


def _config_path() -> Path:
    """ComfyUI/user/default/modal_bridge/config.json"""
    try:
        import folder_paths  # type: ignore
        # folder_paths 是 ComfyUI 自带的全局模块
        user_dir = Path(folder_paths.get_user_directory())
    except Exception:
        # 兜底:相对于 ComfyUI 根
        user_dir = Path(__file__).resolve().parents[2] / "user"
    return user_dir / "default" / "modal_bridge" / "config.json"


def ensure_config() -> Path:
    """首次启动时自动生成默认 config.json,后续不覆盖。"""
    p = _config_path()
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[modal_bridge] generated default config: {p}")
    return p


def load_config() -> dict:
    """读取 config,缺字段用默认值兜底。"""
    p = ensure_config()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    merged = {**DEFAULT_CONFIG, **data}
    return merged


def save_config(new_data: dict) -> None:
    """覆盖写 config(完整对象)。"""
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(new_data, indent=2, ensure_ascii=False), encoding="utf-8")
