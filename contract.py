"""
contract.py — routes.py 的纯计算(无副作用,可单测)。

routes.py 本身 import 不进测试(相对导入 + 依赖 ComfyUI 的 folder_paths),所以它那些
「决定行为对错」的判断都抽到这里:版本/GPU 契约(compute_contract,直接决定前端会不会
拦截 RunModal)、外部输入的合法性(is_safe_job_id)。
"""
import re

# job_id 会拼进本地落盘路径(output/<subfolder>/<job_id>/)。云端产生的 id 是 uuid4
# 或 AIGC Studio 的任务 UUID,都在这个字符集内;别的一律拒。
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def is_safe_job_id(job_id) -> bool:
    """job_id 能不能安全地拼进文件路径。

    ⚠ /fetch_result 的 job_id 直接来自 HTTP body,而本地 API 无鉴权(设计如此,同机任意
    进程都能打)。filename 一直有 basename 防逃逸,job_id 以前没有 —— {"job_id": "../../x"}
    就能把 base64 内容写到 output 目录之外。插件对 folders / path / blend_path 都做了囚笼,
    这里是漏的那个。"""
    return (isinstance(job_id, str)
            and bool(_SAFE_JOB_ID.match(job_id))
            and ".." not in job_id)


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
