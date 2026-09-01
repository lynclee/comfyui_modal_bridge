"""
contract.py — routes.py 的纯计算(无副作用,可单测)。

routes.py 本身 import 不进测试(相对导入 + 依赖 ComfyUI 的 folder_paths),所以它那些
「决定行为对错」的判断都抽到这里:版本/GPU 契约(compute_contract,直接决定前端会不会
拦截 RunModal)、外部输入的合法性(is_safe_job_id)。
"""
import posixpath
import re
import ipaddress
from urllib.parse import urlsplit

# job_id 会拼进本地落盘路径(output/<subfolder>/<job_id>/)。云端产生的 id 是 uuid4
# 或 AIGC Studio 的任务 UUID,都在这个字符集内;别的一律拒。
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# 设置页可经通用 /config 写入的字段。**凭据一律不在此列** —— 这个 allowlist 存在的
# 意义就是挡住"先改配置、再取 key"那类两步绕过,往里加密钥等于自己开口子。
# aigc_studio_base_url 是站点地址、不是凭据,可以进;它的旁路密钥走部署面板的
# 专用输入框 → /deploy,只写 0600 的 config.json,不经这里、也不进 comfy.settings.json。
PUBLIC_CONFIG_WRITE_FIELDS = frozenset({
    "gpu_tier", "enable_snapshot", "use_sage_attention", "cpu_tier_when_no_model",
    "aigc_studio_base_url",
})


def is_safe_job_id(job_id) -> bool:
    """job_id 能不能安全地拼进文件路径。

    ⚠ /fetch_result 的 job_id 直接来自 HTTP body；即使管理 API 已有 capability，路径边界
    仍不能依赖鉴权。filename 一直有 basename 防逃逸,job_id 以前没有 —— {"job_id": "../../x"}
    就能把 base64 内容写到 output 目录之外。插件对 folders / path / blend_path 都做了囚笼,
    这里是漏的那个。"""
    return (isinstance(job_id, str)
            and bool(_SAFE_JOB_ID.match(job_id))
            and ".." not in job_id)


def is_direct_loopback_request(remote: str | None, host: str | None,
                               forwarded_for: str | None = None) -> bool:
    """请求是否真的是通过 localhost/127.0.0.1/::1 访问。

    只看 TCP peer 会把本机反向代理后的所有远程访客都认成 127.0.0.1；因此 peer 和
    浏览器实际访问的 Host 必须同时是 loopback。Host 头本身可伪造,但远程攻击者仍需
    先让 TCP peer 变成 loopback；常规浏览器经过反代时 Host 是外部域名,会被拒绝。
    """
    def _loopback(value: str | None, *, host_value: bool = False) -> bool:
        raw = (value or "").strip()
        if not raw:
            return False
        if host_value:
            try:
                raw = urlsplit("//" + raw).hostname or ""
            except ValueError:
                return False
        raw = raw.strip("[]").split("%", 1)[0]
        if raw.lower() == "localhost":
            return True
        try:
            addr = ipaddress.ip_address(raw)
            mapped = getattr(addr, "ipv4_mapped", None)
            return addr.is_loopback or bool(mapped and mapped.is_loopback)
        except ValueError:
            return False

    if not (_loopback(remote) and _loopback(host, host_value=True)):
        return False
    # 常见反代会把 Host 重写成上游 127.0.0.1，但同时带 X-Forwarded-For/X-Real-IP。
    # 链里出现任意非 loopback 就不能享受本机免鉴权。没带转发信息的反代无法从应用层
    # 与真本机区分，部署文档要求它保留外部 Host 或传该头。
    if forwarded_for:
        return all(_loopback(x.strip()) for x in forwarded_for.split(",") if x.strip())
    return True


def merge_public_config(current: dict, body: dict) -> dict:
    """应用设置页可写字段；凭据、部署状态和未知字段一律拒绝。"""
    unknown = sorted(set(body) - PUBLIC_CONFIG_WRITE_FIELDS)
    if unknown:
        raise ValueError(f"这些字段不能经通用 config API 修改: {unknown}")
    if "gpu_tier" in body and body["gpu_tier"] not in ("auto", "cheap", "primary", "top"):
        raise ValueError("gpu_tier 非法")
    for key in ("enable_snapshot", "use_sage_attention", "cpu_tier_when_no_model"):
        if key in body and not isinstance(body[key], bool):
            raise ValueError(f"{key} 必须是 boolean")
    if "aigc_studio_base_url" in body:
        v = body["aigc_studio_base_url"]
        if not isinstance(v, str):
            raise ValueError("aigc_studio_base_url 必须是字符串")
        v = v.strip()
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("aigc_studio_base_url 必须以 http:// 或 https:// 开头")
        body = {**body, "aigc_studio_base_url": v.rstrip("/")}
    out = dict(current)
    out.update({k: body[k] for k in PUBLIC_CONFIG_WRITE_FIELDS if k in body})
    return out


def compute_contract(local, deployed, reachable, local_gpu, deployed_gpu,
                     local_comfyui=None, deploy_comfyui=None):
    """比对本地插件版本 / 所选显卡 / ComfyUI 版本 与 云端已部署的。

      - match:         插件版本一致(且 endpoint 可达)。不可达一律 False(没部署/app 删了)。
      - gpu_match:     显卡一致。不可达、或老镜像不上报 deployed_gpu(None)时不拦。
      - comfyui_match: 本机 ComfyUI 版本 vs 上次部署时检测到的版本。本机升级了 → False → 前端
                       提示重部署让云端 ComfyUI 跟上(非硬拦,只警告)。任一侧未知则不判(True)。

    返回 /version 需要的字段子集(ok/err_kind 由路由补)。
    """
    match = bool(reachable) and deployed == local
    gpu_match = (not reachable) or (deployed_gpu is None) or (deployed_gpu == local_gpu)
    comfyui_match = (not deploy_comfyui) or (not local_comfyui) or (local_comfyui == deploy_comfyui)
    return {
        "local": local,
        "deployed": deployed,
        "match": match,
        "reachable": bool(reachable),
        "local_gpu": local_gpu,
        "deployed_gpu": deployed_gpu,
        "gpu_match": gpu_match,
        "local_comfyui": local_comfyui,
        "deploy_comfyui": deploy_comfyui,
        "comfyui_match": comfyui_match,
    }


def is_safe_output_path(job_id: str, path: str) -> bool:
    """产物的 Volume 路径是否落在本 job 的输出目录内。

    ⚠ 这份规则在云端 modal_app.fetch_endpoint 里另有一份逐字相同的实现(云端不能
    import 本模块,它不在镜像的 add_local_python_source 名单里),由
    test_output_path_jail_identical_local_and_cloud 钉死两边一致。

    为什么本地也要囚:routes 取回大文件时**绕过云端 endpoint、直连 Volume SDK**,
    云端那道校验管不到。而 volume_path 整个来自浏览器提交的 modal_state,伪造成
    `models/checkpoints/x.safetensors` 就能把上传过的模型下载走、**并且删掉**
    (取回后即删是既定行为)。删除不可逆,几十 GB 的模型重传一次代价极高。

    三重判据与云端一致:前缀 + 不含 .. + 规范化后与原串相同(挡 `./` `//` 等变体)。
    """
    if not isinstance(path, str) or not path:
        return False
    prefix = f"_outputs/{job_id}/"
    return (path.startswith(prefix)
            and ".." not in path
            and path == posixpath.normpath(path))
