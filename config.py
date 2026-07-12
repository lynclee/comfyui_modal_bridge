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
    # 云端 ComfyUI 版本跟随本机:部署时自动检测本机 ComfyUI 版本并解析成 git tag(无对应取最接近)。
    "comfyui_version": "",      # 部署时检测到的本机 ComfyUI 版本(契约:本机升级后提示重部署)
    "comfyui_tag": "",          # 解析出的云端 clone tag(如 v0.22.0);空 = 镜像兜底默认
    "default_gpu": "H100",      # 主卡(大工作流 / 默认)。换值需重新部署
    # 省钱档:估算显存放得下便宜卡(默认 L40S 48G)且非视频的工作流,自动降到这张卡跑
    # (如 Z-Image-Turbo → L40S,FLUX.2-dev → 仍 H100)。换值需重新部署。
    "cheap_gpu": "L40S",
    # 顶配档:估算显存超过主卡容量(如 >80G)的工作流自动升到这张卡(默认 B200 183G),防 OOM。
    # B200 是 Blackwell 最强档(显存最大、速度最快),大图自动上这张。
    # 升档是正确性兜底,不受 auto_downgrade 控制;设成与 default_gpu 相同则不启用。换值需重新部署。
    "top_gpu": "B200",
    "auto_downgrade": True,     # 开:按 estimate_vram 自动在 default_gpu / cheap_gpu 间选档(本地路由决策,改它不必重部署)
    "enable_snapshot": True,   # 内存快照(实验):冷启 ~30s→~5s。默认开;不支持的 GPU 档自动退化为普通冷启(不更差)。换值需重新部署
    "user_id": "local-dev",
    "poll_interval_sec": 1.5,
    # worker(Modal)单任务超时上限(秒)。覆盖最慢类别(视频)——见 categories.max_worker_timeout_s()。
    # 是上限不是每任务时长:高上限不拖慢快任务(按实际运行计费)。换值需重新部署生效。
    "worker_timeout_sec": 1800,
    "output_subfolder": "modal_results",
    # 产物大于此(MB)走 Volume 直连取回(避开 base64/modal.Dict 上限);小的仍 base64。换值需重新部署。
    "volume_threshold_mb": 8,

    # ── 模型自动同步(本地 → Modal Volume,SDK batch_upload,CAS 去重)──
    # 提交前检查 Volume,工作流要、Volume 没、但本地有的模型自动上传上去。
    # 不再从 HF/civitai 下载——模型都在本地 ComfyUI Desktop 下好。
    "auto_sync_models": True,
    "model_sync_timeout_sec": 3600,  # 上传整批模型的超时(大模型走上行带宽)

    # ── AIGC Studio 交付(可选,网站 aigc-r2 模式;本地 desktop 用户不用管)──
    # 网站(Vercel)地址:部署时写进 Modal Secret(AIGC_STUDIO_BASE_URL),worker 交付
    # 结果时调它的 asset-intake / job-complete。留空 = 不启用(desktop 交付完全不受影响)。
    "aigc_studio_base_url": "",
    # Vercel Protection 旁路密钥(可选,仅生产域名被保护时需要)。存本地 + Modal Secret,
    # /config 永不回吐(同 bridge_api_key),页面只显示「已保存」。
    "aigc_bypass_secret": "",

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
