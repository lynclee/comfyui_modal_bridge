"""
config.py — 配置文件管理
路径:ComfyUI/user/default/modal_bridge/config.json
"""
import json
import secrets
import sys
import threading
from pathlib import Path

if __package__:
    from .private_json import atomic_write_json
else:  # deploy.py / mcp_server.py 和独立测试以顶层模块导入
    from private_json import atomic_write_json

# 默认配置(用户问答确认)
DEFAULT_CONFIG = {
    # ── Endpoint(deploy.py 自动写)──
    "modal_endpoint_base": "https://YOUR_WORKSPACE--comfyui-bridge",
    "modal_app_name": "comfyui-bridge",
    "modal_workspace": "",                       # 用于拼 endpoint
    "modal_volume_name": "comfyui-bridge-models",  # 重部署(加 custom_node)时要
    "scaledown_window": 12,                      # 空闲多久回收容器(s);重部署时要。
    # 12s:典型用法是「跑一次、看结果、再调参」,间隔远大于窗口,留久了纯空转计费。
    # 留 12s 是为了「失败立刻重试」还能复用容器(冷启约 20+s,比窗口贵得多)。

    # ── 鉴权(私有 endpoint,deploy.py / GUI 部署自动写)──
    "modal_token_id": "",      # ak-xxx(account token,仅本机 deploy 用)
    "modal_token_secret": "",  # as-xxx
    "bridge_api_key": "",      # 部署时随机生成,调私有 endpoint 用(自建鉴权)
    # 本地 HTTP 管理面的 capability。127.0.0.1/localhost 同源访问免填；经局域网、
    # 反向代理、host.docker.internal 或 MCP 访问时,所有读写本机 / 花费云账单的端点
    # 都必须带它。首次需要时自动生成并原子写入 config；不会经 /config 回吐给浏览器。
    "local_api_capability": "",
    # 最近一次成功 deploy 时镜像包含的本地私有节点依赖指纹；内部状态,不由前端修改。
    "local_node_reqs_deployed_hash": "",
    "comfy_api_key": "",       # 可选:comfy.org API key,供工作流里的 ComfyUI API 节点鉴权(账单走你的 comfy.org)

    # ── 运行选项 ──
    # 云端 ComfyUI 版本跟随本机:部署时自动检测本机 ComfyUI 版本并解析成 git tag(无对应取最接近)。
    "comfyui_version": "",      # 部署时检测到的本机 ComfyUI 版本(契约:本机升级后提示重部署)
    "comfyui_tag": "",          # 解析出的云端 clone tag(如 v0.22.0);空 = 镜像兜底默认
    "default_gpu": "H100",      # 主卡(大工作流 / 默认)。换值需重新部署
    # 省钱档:估算显存放得下便宜卡(默认 L40S 48G)且非视频的工作流,自动降到这张卡跑
    # (如 Z-Image-Turbo → L40S,FLUX.2-dev → 仍 H100)。换值需重新部署。
    "cheap_gpu": "L40S",
    # 顶配档:估算显存超过主卡容量(如 >80G)的工作流自动升到这张卡(默认 B200 180G),防 OOM。
    # B200 是 Blackwell 最强档(显存最大、速度最快),大图自动上这张。
    # 升档是正确性兜底,不受 auto_downgrade 控制;设成与 default_gpu 相同则不启用。换值需重新部署。
    "top_gpu": "B200",
    # GPU 档位:auto | cheap | primary | top。四档 worker(CPU/cheap/primary/top)一次部署
    # 全建好、空闲各自 scale-to-zero,这里只决定「这次路由到哪一档」——
    # ⭐ 改它立即生效,不必重新部署(与 default_gpu/cheap_gpu/top_gpu 换卡型不同,那才要重部署)。
    #   auto    : 按 estimate_vram 自动选(小图降 cheap、超主卡显存升 top)
    #   cheap   : 固定走 cheap_gpu    primary: 固定走 default_gpu    top: 固定走 top_gpu
    # 空值 = 旧配置,回落到 auto_downgrade 的语义(见 routes._pick_gpu_class)。
    "gpu_tier": "auto",
    "auto_downgrade": True,     # 旧开关(gpu_tier 为空时才用):开=自动选档,关=固定 primary。新配置请用 gpu_tier
    # 关掉云端 ComfyUI 的动态 VRAM(--disable-dynamic-vram),改用估算式模型加载。
    # 动态 VRAM 开着时权重只常驻一小部分、其余按需从 CPU 搬(日志 "N MB Staged"),
    # 显存装得下也照搬 —— 云上按秒计费,这段 PCIe 搬运是白付的钱。关掉通常更快,
    # 代价:显存不够会直接 OOM,没有降速兜底。默认不关,按工作流自行取舍。换值需重新部署。
    "disable_dynamic_vram": False,
    # 用 SageAttention 替换 ComfyUI 默认的 PyTorch SDPA(--use-sage-attention)。
    # attention 在长序列视频模型上占绝大部分算力(H3 单步约七成 FLOPs),量化后理论翻倍。
    # 代价是 QK 走 INT8 属有损:论文在 CogVideoX 上端到端 ~0.2%(视频那组甚至略优),
    # 但 H3 是音视频联合生成、权重本身已剪枝+INT8,误差叠加没有先例数据 —— 默认关,
    # 自己同 seed 跑 A/B 看过片子(重点看音画同步和高频细节)再决定常开。换值需重新部署。
    "use_sage_attention": False,
    # auto 档下「工作流里扫不到本地模型」时,是否路由到 CPU worker(GPU 账单≈0)。
    # ⚠ 这是**负向推断**,不可靠:节点内部下载权重、无模型文件的 CUDA/Triton 图像处理与
    # 3D/光流节点、模型参数不以文件名字符串出现的节点 —— 都扫不到,却真的要 GPU。
    # 误判代价不对称:该给 GPU 却给了 CPU = 跑不动、耗到 worker 超时、白烧钱零产出;
    # 反过来只是多花一点(GPU 档 scale-to-zero,纯 API 工作流跑几秒就结束)。
    # 关掉 = auto 档一律走 GPU 梯子。保持 True 是为了不改变既有用户的账单;吃过误判亏就关掉。
    "cpu_tier_when_no_model": True,
    # 内存快照(实验):实测对 GPU worker 基本无效 —— ComfyUI 是子进程,Modal 快照盖不住
    # (2026-08-05 实测 7 启动 7 重建 0 复用),开着反添 ~5s/次创建开销,故默认关。
    # CPU worker 的 CPU 快照不受影响。换值需重新部署。
    "enable_snapshot": False,
    "user_id": "local-dev",
    "poll_interval_sec": 1.5,
    # worker(Modal)单任务超时上限(秒)。覆盖最慢类别(视频)——见 categories.max_worker_timeout_s()。
    # 是上限不是每任务时长:高上限不拖慢快任务(按实际运行计费)。换值需重新部署生效。
    "worker_timeout_sec": 1200,
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
    "comfyui_tag_pin": "",  # 非空 = 云端 ComfyUI 钉在这个 tag,不跟随本机 Desktop(见 node_sync.resolve_comfyui_tag)
    "hf_token": "",        # 持久化:Secret 是 --force 整份重建的,不存就会在下次部署时被抹掉
    "civitai_token": "",

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


class ConfigCorrupt(RuntimeError):
    """config.json 存在,但读不出一个 JSON 对象(语法错 / 顶层不是对象 / 读不了)。

    ⚠ 以前解析失败就静默退回默认值,而之后任何一次写配置(非 loopback 请求触发的 capability
    生成、设置页切一次 GPU 档位、部署收尾)都会拿这份默认值**整份覆盖**原文件 —— 用户手改
    config.json 多打一个尾逗号,bridge_api_key / modal_token_secret / hf_token / comfy_api_key
    就全没了,而且没有任何报错指向真实原因(2026-10-05 深度 review)。
    现在读失败一律抛它:读的一侧报清楚,写的一侧拒绝覆盖。只有文件**不存在**才用默认值。"""

    def __init__(self, path: Path, detail: str, lineno: int | None = None):
        self.path = path
        self.lineno = lineno
        super().__init__(f"config.json {detail},请修好或删除该文件"
                         f"(删除 = 回到默认配置,要重新填 token 并部署):{path}")


def _read_config_file(p: Path) -> dict | None:
    """读 config.json:不存在返回 None;存在但读不出 JSON 对象抛 ConfigCorrupt。

    ⚠ 错误信息只带行号和解析器的说明,不带文件内容 —— 里面是凭据。"""
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:   # ValueError:UnicodeDecodeError(不是 UTF-8)
        raise ConfigCorrupt(p, f"读取失败({type(e).__name__})") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigCorrupt(p, f"第 {e.lineno} 行解析失败({e.msg})", lineno=e.lineno) from None
    if not isinstance(data, dict):
        raise ConfigCorrupt(p, f"顶层不是 JSON 对象(是 {type(data).__name__})")
    return data


def ensure_config() -> Path:
    """首次启动时自动生成默认 config.json,后续不覆盖。"""
    p = _config_path()
    if p.exists():
        return p
    # 走原子写 + 创建即 0600:以前是 write_text,写一半崩会留下半个 JSON,
    # 而现在半个 JSON 会被当成损坏、拒绝一切写入(2026-10-05 深度 review)。
    atomic_write_json(p, DEFAULT_CONFIG)
    print(f"[modal_bridge] generated default config: {p}")
    return p


def load_config() -> dict:
    """读取 config,缺字段用默认值兜底。文件存在但损坏时抛 ConfigCorrupt,**不**退回默认值。"""
    p = ensure_config()
    data = _read_config_file(p)
    return {**DEFAULT_CONFIG, **(data or {})}


def save_config(new_data: dict) -> None:
    """覆盖写完整配置：独立随机临时文件、创建即私有、原子替换，失败抛出。

    原文件损坏时拒绝写入(抛 ConfigCorrupt),不替用户「修复」:调用方手里的 dict 可能来自
    损坏之前读到的旧值,也可能是默认值,覆盖下去就是把用户正在手改的那份连同凭据一起抹掉。"""
    p = _config_path()
    _read_config_file(p)
    atomic_write_json(p, new_data)


_CAPABILITY_LOCK = threading.Lock()


def read_local_capability_file(path: str) -> str:
    """从插件 config.json 读 local_api_capability;文件不存在 / 不是 JSON / 没这个键 → 返回空串。

    给 MCP 本地模式用(mcp_server.py):本机 127.0.0.1 / localhost 直连免 capability(0.8.40 起),
    但 MCP 经 host.docker.internal / 局域网 / 反向代理访问 ComfyUI 时仍然要带。MCP 进程和
    ComfyUI 在同一台机器(或共享这份文件)时,让它**直接读 0600 的 config.json**比把 token
    写进 .mcp.json / 环境变量 / 聊天记录都干净 —— 值不经过任何中间人。
    只读这一个键,不回吐其它字段。

    只读、从不写,所以文件损坏时返回空串不会覆盖任何东西;但要往 stderr 说一声 —— 否则
    MCP 只会收到一个「缺 capability」的 403,看不出真实原因是 config.json 坏了
    (2026-10-05 深度 review)。stderr 不占 MCP 的 stdio 协议通道。
    """
    p = Path(path)
    try:
        data = _read_config_file(p)
    except ConfigCorrupt as e:
        print(f"[modal_bridge] ⚠ 读不到 local_api_capability:{e}", file=sys.stderr)
        return ""
    if data is None:
        return ""
    v = data.get("local_api_capability")
    return v.strip() if isinstance(v, str) else ""


def ensure_local_api_capability() -> str:
    """返回持久化的本地管理 capability；缺失时生成一次。

    不在模块 import 时生成，避免只读安装/打包流程无故改配置。第一次管理请求
    才需要它；生成后放在 0600 config 中，调用方从服务器本机读取并在浏览器配对。
    config.json 损坏时 load_config 直接抛 ConfigCorrupt —— 既不生成也不写:以前这里是
    「默认值 + 新 capability」整份覆盖原文件的入口之一,任何一个非 loopback 请求都能触发。
    """
    cfg = load_config()
    value = str(cfg.get("local_api_capability") or "").strip()
    if value:
        return value
    with _CAPABILITY_LOCK:
        cfg = load_config()
        value = str(cfg.get("local_api_capability") or "").strip()
        if not value:
            value = "lc-" + secrets.token_urlsafe(32)
            cfg["local_api_capability"] = value
            save_config(cfg)
            print(f"[modal_bridge] generated local API capability: {_config_path()}")
        return value
