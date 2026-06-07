"""
contract.py — 版本 / GPU 契约的纯计算(无副作用,可单测)。

routes.py 的 /version 路由调用 compute_contract。抽成纯函数是为了能单测、防回归
(契约逻辑直接决定前端会不会拦截 RunModal、逼用户重新部署)。
"""


def compute_contract(local, deployed, reachable, local_gpu, deployed_gpu):
    """比对本地插件版本 / 所选显卡 与 云端已部署的版本 / 显卡。

      - match:      版本一致(且 endpoint 可达)。不可达一律 False(没部署/app 删了)。
      - gpu_match:  显卡一致。不可达、或老镜像不上报 deployed_gpu(None)时不拦
                    —— 交给版本契约先逼出一次重部署,之后云端才会上报真实在跑的卡。

    返回 /version 需要的字段子集(ok/err_kind 由路由补)。
    """
    match = bool(reachable) and deployed == local
    gpu_match = (not reachable) or (deployed_gpu is None) or (deployed_gpu == local_gpu)
    return {
        "local": local,
        "deployed": deployed,
        "match": match,
        "reachable": bool(reachable),
        "local_gpu": local_gpu,
        "deployed_gpu": deployed_gpu,
        "gpu_match": gpu_match,
    }
