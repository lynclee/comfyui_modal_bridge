#!/usr/bin/env python3
"""
bridge_cli.py — Modal Bridge 独立 CLI:完全脱离 ComfyUI 使用云端 GPU。

两类用户:
  **消费者**(拿到部署者给的 endpoint + bridge_key):submit / status / fetch / cancel / health
  **自建者**(自己的 Modal 账号,从 clone 的仓库直接起云端):deploy / upload-model

配置优先级:命令行 flag > env(MODAL_BRIDGE_ENDPOINT / MODAL_BRIDGE_KEY)> ~/.modal_bridge/cli.json
(`configure` 子命令写入;`deploy` 成功后自动写入 endpoint + key)。

自建者前置(一次性):
    pip install modal && modal token new     # Modal 账号鉴权
    python bridge_cli.py deploy --comfyui-tag v0.37.2   # 第一次必须指定;之后沿用上次的
    python bridge_cli.py upload-model /path/to/model.safetensors diffusion_models
限制(与完整插件的差异):无 custom_node 自动同步(镜像只有内置节点,除非部署者本机同步过)、
无模型自动上传(跑前用 upload-model 手动补)、无显存估算路由(--gpu-class 手选)。

消费者用法:
    python bridge_cli.py configure --endpoint https://<ws>--comfyui-bridge --key bk-xxx
    python bridge_cli.py submit workflow_api.json --wait --out ./outputs
workflow_api.json 是 ComfyUI 的 API prompt(UI 里「导出(API)」得到的格式)。

cancel 的退出码(脚本据此判断结局):
    0  已取消,或任务早已结束(完成 / 失败 / 被判死)—— 不再计费
    3  云端查无此任务(id 不对,或早已结束并被清理)—— 没有在跑
    4  取消失败,任务可能仍在跑、仍在计费 —— 去 Modal 控制台确认
    5  请求没成功(网络 / HTTP 错误 / key 不对),结果未知 —— 稍后重试 cancel 或 status 核实
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from bridge_client import BridgeClient, BridgeError  # noqa: E402
from private_json import atomic_write_json  # noqa: E402

CLI_CFG = Path.home() / ".modal_bridge" / "cli.json"


def _load_cli_cfg() -> dict:
    """读 ~/.modal_bridge/cli.json。不存在 → {};存在但读不了 / 不是 JSON 对象 → **中止**。

    ⚠ 以前任何异常都当成空配置(2026-10-05 深度 review 第二轮):手改多打一个逗号,`deploy` 就以为
      从没部署过 —— 换一把新 bridge key 写进 Secret(旧 key 的调用方全部 401)、按全新部署对账;
      `configure` 则拿空配置整份覆盖,按 app 存的 keys 一起丢了。坏了就停,让用户修好或删掉。"""
    try:
        text = CLI_CFG.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        sys.exit(f"✗ 读不了 {CLI_CFG}({type(e).__name__}: {e}),已中止。修好权限 / 编码后重试,"
                 f"或确认不要后删掉它(删掉 = 忘掉记下的 endpoint 和 bridge key)")
    try:
        data = json.loads(text)
    except ValueError as e:
        sys.exit(f"✗ {CLI_CFG} 不是合法 JSON({e}),已中止 —— 照常继续会把它当成空配置:deploy 会换一把新 "
                 f"bridge key、configure 会覆盖掉里面按 app 记的 key。修好或确认不要后删掉再重试")
    if not isinstance(data, dict):
        sys.exit(f"✗ {CLI_CFG} 顶层不是 JSON 对象(是 {type(data).__name__}),已中止。修好或删掉再重试")
    return data


def _save_cli_cfg(d: dict) -> None:
    """含 bridge_key 的 CLI 配置，与插件配置共用私有原子写入。"""
    atomic_write_json(CLI_CFG, d)


def _client(args) -> BridgeClient:
    endpoint = getattr(args, "endpoint", None) or os.environ.get("MODAL_BRIDGE_ENDPOINT") or ""
    key = getattr(args, "key", None) or os.environ.get("MODAL_BRIDGE_KEY") or ""
    if not (endpoint and key):
        # flag / env 已经给全了就不读 cli.json —— 它坏了也不该挡住不用它的调用
        saved = _load_cli_cfg()
        endpoint = endpoint or saved.get("endpoint") or ""
        key = key or saved.get("key") or ""
    if not endpoint:
        sys.exit("缺 endpoint:flag --endpoint / env MODAL_BRIDGE_ENDPOINT / `configure` 三选一")
    return BridgeClient(endpoint, key)


def _print_progress(s: dict) -> None:
    st = s.get("status")
    p = s.get("progress") or {}
    if p.get("total"):
        print(f"  [{st}] {p.get('step')}/{p.get('total')} 步 · {p.get('s_it')}s/步 "
              f"· 已 {p.get('elapsed')}s", flush=True)
    else:
        print(f"  [{st}]", flush=True)


# ── 消费者命令 ──
def cmd_health(args):
    c = _client(args)
    h = c.health()
    print(json.dumps(h, indent=2, ensure_ascii=False))


def cmd_submit(args):
    c = _client(args)
    workflow = json.loads(Path(args.workflow).read_text())
    input_dirs = args.input_dir or [str(Path(args.workflow).parent), "."]
    imgs = BridgeClient.pack_input_images(workflow, input_dirs)
    if imgs:
        print(f"打包 {len(imgs)} 张输入图(来自 {input_dirs})")
    d = c.submit(workflow, input_images=imgs or None, gpu_class=args.gpu_class)
    print(f"job {d['id']}  gpu={d.get('gpu')}")
    if not args.wait:
        print(f"轮询:python {Path(__file__).name} status {d['id']}")
        return
    final = c.wait(d["id"], timeout_s=args.timeout, on_update=_print_progress)
    if final.get("status") != "completed":
        sys.exit(f"任务未成功: {final.get('status')} — {final.get('error', '')[:400]}")
    outs = c.download_outputs(final, os.path.join(args.out, d["id"]))
    for o in outs:
        print(f"✓ {o['path']}  ({o['size_bytes'] / 1e6:.1f} MB)")


def _b64_len(v) -> int:
    """base64 串解码后的字节数(不真解码)。"""
    if not isinstance(v, str):
        return 0
    v = v.strip()
    return max(0, len(v) * 3 // 4 - v[-2:].count("="))


def _printable_state(s: dict) -> dict:
    """打印用的状态副本:剥掉 data_base64,换成文件数和大小。
    ⚠ 以前原样打印,一个视频任务的 status / cancel 输出实测 4 MB base64,终端和日志都被淹掉
    (2026-10-05 深度 review)。产物要用 fetch 取,打印里留个摘要就够了。"""
    if not isinstance(s, dict):
        return s
    out = dict(s)
    files, total = 0, 0
    if isinstance(out.get("images"), list):
        imgs = []
        for img in out["images"]:
            if isinstance(img, dict) and "data_base64" in img:
                img = dict(img)
                n = img.pop("data_base64")
                files += 1
                total += img.get("size_bytes") if isinstance(img.get("size_bytes"), int) else _b64_len(n)
            elif isinstance(img, dict) and img.get("volume_path"):
                files += 1
                total += img.get("size_bytes") if isinstance(img.get("size_bytes"), int) else 0
            imgs.append(img)
        out["images"] = imgs
    if "data_base64" in out:
        n = out.pop("data_base64")
        files += 1
        total += _b64_len(n)
    if files:
        out["outputs_summary"] = {"files": files, "bytes": total,
                                  "note": "产物内容已省略,用 fetch 取回"}
    return out


def cmd_status(args):
    print(json.dumps(_printable_state(_client(args).status(args.job_id)), indent=2, ensure_ascii=False))


def cmd_fetch(args):
    c = _client(args)
    s = c.status(args.job_id)
    st = s.get("status")
    if st != "completed":
        # 失败 / 查无此任务要说清原因,只打一个状态词用户不知道下一步(2026-10-05 深度 review)
        if st == "failed":
            sys.exit(f"✗ 任务失败,没有产物可取:{(s.get('error') or '云端没给原因')[:400]}")
        if st == "not_found":
            sys.exit("✗ 云端查无此任务:job_id 不对,或已超过保留期被清理(产物也一并清掉了)")
        if st == "cancelled":
            sys.exit("✗ 任务已取消,没有产物")
        sys.exit(f"任务还没完成(状态 {st}),稍后再 fetch,或用 submit --wait 等它跑完")
    for o in c.download_outputs(s, os.path.join(args.out, args.job_id)):
        print(f"✓ {o['path']}  ({o['size_bytes'] / 1e6:.1f} MB)")


# cancel 的退出码(--help 里有说明):调用方只看 0 / 非 0 的话分不出「没在跑」和「仍在计费」
CANCEL_OK, CANCEL_NOT_FOUND, CANCEL_STILL_BILLING, CANCEL_UNKNOWN = 0, 3, 4, 5


def cmd_cancel(args):
    try:
        r = _client(args).cancel(args.job_id)
    except Exception as e:      # BridgeError(HTTP / 401)、网络错:请求没成功,不知道取消了没有
        print(f"✗ 取消请求没成功,结果未知:{e}\n  稍后重试 cancel,或用 status 核实任务是否还在跑",
              file=sys.stderr)
        sys.exit(CANCEL_UNKNOWN)
    print(json.dumps(_printable_state(r), ensure_ascii=False))
    if r.get("status") == "not_found":
        print("云端查无此任务 —— id 不对,或早已结束并被回收;没有在跑", file=sys.stderr)
        sys.exit(CANCEL_NOT_FOUND)
    # cancel_noop:任务已先一步结束(完成 / 失败 / 被判死),error 是**任务的**失败原因,不是取消失败 ——
    # 按「取消失败、仍在计费」报正好说反(同前端 settleCancel)。
    if r.get("error") and not r.get("cancel_noop"):
        print("⚠ 取消失败 — 云端可能仍在跑、仍在计费,去 Modal 控制台确认", file=sys.stderr)
        sys.exit(CANCEL_STILL_BILLING)
    if r.get("cancel_noop"):
        print(f"任务已先一步结束({r.get('status')}),不再计费")
    # 正常返回 = 退出码 CANCEL_OK(0)


def cmd_configure(args):
    d = _load_cli_cfg()
    if args.endpoint:
        d["endpoint"] = args.endpoint
    if args.key:
        d["key"] = args.key
    _save_cli_cfg(d)
    print(f"已写 {CLI_CFG}(endpoint={'✓' if d.get('endpoint') else '✗'}, key={'✓' if d.get('key') else '✗'})")


# ── 自建者命令(需要 modal CLI + `modal token new` 已完成)──
def _modal_cmd(*tail: str) -> list[str]:
    return [sys.executable, "-m", "modal", *tail]


_PLACEHOLDER_ENDPOINT = "YOUR_WORKSPACE"   # config.DEFAULT_CONFIG 里的占位 endpoint


class PluginConfigUnreadable(RuntimeError):
    """插件的 config.json 在,但读不了 / 不是合法 JSON 对象。"""


def _plugin_cfg_readonly() -> dict:
    """读插件的 config.json(只读)。插件不在旁边(import 不到 config 模块)或文件不存在 → {}。

    ⚠ 不能用 config.load_config():它先 ensure_config,在 ComfyUI 之外运行时会在插件目录上两级
      凭空建一个默认 config(2026-10-05 深度 review);而且合并进来的默认值(占位 endpoint 等)
      会被当成「插件的配置」用。这里只要文件里真写着的。
    ⚠ 只有上面两种算「没有插件配置」。文件在却读不了 / 坏了必须抛 PluginConfigUnreadable:以前一律
      `except Exception: {}`,于是生成一把新 key 写进 Secret,插件里那把随即失效、所有调用方 401
      (2026-10-05 routes 一路协调:损坏的配置绝不能静默当成默认值)。"""
    try:
        import config as _plugin_cfg
    except ImportError:
        return {}
    path = _plugin_cfg._config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise PluginConfigUnreadable(f"读不了插件 config {path}:{e}") from e
    try:
        data = json.loads(text)
    except ValueError as e:
        raise PluginConfigUnreadable(f"插件 config {path} 不是合法 JSON:{e}") from e
    if not isinstance(data, dict):
        raise PluginConfigUnreadable(f"插件 config {path} 不是 JSON 对象")
    return data


def _known_endpoint(u) -> str:
    """真实的 endpoint;空的 / 占位符(从没部署过)当未知,返回空串。"""
    u = (u or "").strip() if isinstance(u, str) else ""
    return "" if not u or _PLACEHOLDER_ENDPOINT in u else u


# modal_app.py 里 /health 那个 web 函数的名字(label = <app>-health)。endpoint 未知时拿它问 Modal API。
_HEALTH_FUNCTION = "health_endpoint"


class EndpointLookupFailed(RuntimeError):
    """问不了 Modal「这个 app 部署过没有」(没装 modal / 没登录 / 网络 / 地址形态认不出)。"""


def _lookup_deployed_endpoint(app_name: str, modal_mod=None) -> str:
    """本机没记下 endpoint 时,问 Modal API 这个 app 部署过没有。
    部署过 → endpoint base(https://<ws>--<app>,多环境时带环境后缀,以 Modal 给的为准);确认没部署 → "";
    问不了 → 抛 EndpointLookupFailed(调用方中止,不能按全新部署处理)。

    走 Modal 控制面:Function.from_name(...).get_web_url() 是一次 FunctionGet RPC(SDK 1.4.3 / 1.5.4 的
    _functions.py 核对过:查无此 app / 函数抛 modal.exception.NotFoundError,web 函数的地址来自
    handle_metadata.web_url),**不访问 *.modal.run、不唤醒容器**。

    ⚠ 为什么要问(2026-10-05 深度 review 第二轮):以前 endpoint 未知就直接按全新部署 —— 第二台机器
      (没装插件、没有 cli.json)对一个已部署的 app 跑 deploy,云端节点一个都不核对,镜像按本机那份
      空清单重建,云端装的节点全被删掉。"""
    if modal_mod is None:
        try:
            import modal as modal_mod
        except ImportError as e:
            raise EndpointLookupFailed(f"没装 modal SDK({e})") from e
    try:
        url = modal_mod.Function.from_name(app_name, _HEALTH_FUNCTION).get_web_url()
    except modal_mod.exception.NotFoundError:
        return ""
    except Exception as e:      # 鉴权 / 网络 / 服务端错:不知道,就不能当成「没部署」
        raise EndpointLookupFailed(f"{type(e).__name__}: {e}") from e
    m = re.match(r"^(https://[^/\s]+?)-health\.modal\.run/?$", (url or "").strip())
    if not m:
        raise EndpointLookupFailed(f"app {app_name} 的 {_HEALTH_FUNCTION} 地址认不出来:{url!r}")
    return m.group(1)


def cmd_deploy(args):
    """无 ComfyUI 的部署:复用插件的 deploy_env / secret 链路,避开「裸 modal deploy」陷阱
    (裸跑会丢 MODAL_BRIDGE_* env → 云端 ComfyUI 落到老兜底 tag、GPU/超时全回默认)。"""
    import local_nodes
    import modal_volume
    import node_sync
    from config import DEFAULT_CONFIG

    saved = _load_cli_cfg()
    # cli.json 记的是哪个 app:老文件 / configure 写的没有 app_name,按默认 app 算
    saved_same = (saved.get("app_name") or "comfyui-bridge") == args.app_name
    # 同一台机器上若装着插件,它的 config.json 才是这个 app 的权威凭据来源。
    # ⚠ 以前只看 ~/.modal_bridge/cli.json:用 GUI 部署过、从没跑过 CLI 的机器上 cli.json 不存在,
    #   于是新生成一把 BRIDGE_API_KEY 并 --force 覆盖 Secret —— 插件 config 里那把随即失效,
    #   **所有请求 401**;COMFY_API_KEY / AIGC_* / HF 也因为这里的 cfg 是空默认值而被一起抹掉。
    #   (2026-09-23 review 抓到)
    try:
        plugin = _plugin_cfg_readonly()
    except PluginConfigUnreadable as e:
        sys.exit(f"✗ {e}\n  已中止:照常部署会生成一把新 bridge key 写进 Secret,插件里那把随即失效、"
                 f"所有调用方 401。修好(或确认不要后删掉)这个文件再部署。")
    same_app = (plugin.get("modal_app_name") or "comfyui-bridge") == args.app_name
    if not same_app:
        plugin = {}   # 部署的是另一个 app,插件那套凭据不属于它,别串
    # AIGC 地址(取自插件 config)会原样写进 Secret:只收 https://(规则见 contract.aigc_url_problem,
    # 2026-10-05 深度 review 第二轮)。在生成 key、对账、写 Secret 之前挡。
    import contract
    _aigc_err = contract.aigc_url_problem(plugin.get("aigc_studio_base_url") or "")
    if _aigc_err:
        sys.exit(f"✗ 插件设置里的 {_aigc_err}。改好(或清空 = 停用)后再部署")
    # ⚠ 同一个 app 时**插件的 key 优先**,cli.json 的只在插件没有 key 时用。反过来的话,一把过时的
    #   cli.json key 会覆盖 Secret,插件照样全部 401(2026-09-24 review:第一版顺序写反了,
    #   和上面注释自相矛盾)。
    keys = saved.get("keys") if isinstance(saved.get("keys"), dict) else {}
    bridge_key = (plugin.get("bridge_api_key") or keys.get(args.app_name)
                  or (saved.get("key") if saved_same else "") or "")
    if not bridge_key:
        # 新 key **先落盘**再写 Secret(契约 C16):部署中途失败时,Secret 里那把本机至少还有,
        # 不会变成谁都没有的 key、之后全部 401。存进按 app 分的 keys,不动当前生效的 endpoint/key。
        bridge_key = node_sync.gen_bridge_key()
        _save_cli_cfg({**saved, "keys": {**keys, args.app_name: bridge_key}})
        saved = _load_cli_cfg()

    # ⚠ 部署参数同理:CLI 现在会部署到插件的 app 上,就不能拿自己的默认值去覆盖插件的配置 ——
    #   否则一次 `bridge_cli deploy` 会把云端 ComfyUI 静默退回老版本、换掉 Volume、改 GPU、关 sage,
    #   正是 resolve_comfyui_tag 刚修掉的那类静默降级。显式传了参数才用参数,否则插件 → CLI 默认。
    def pick(arg, key, default):
        if arg is not None:
            return arg
        v = plugin.get(key)
        return default if v is None or v == "" else v

    # 云端 ComfyUI 版本:--comfyui-tag > 插件的 comfyui_tag_pin > 插件上次部署的 comfyui_tag > 本 CLI 上次部署的。
    # ⚠ 以前插件 config 的 comfyui_tag 为空(装了插件但没用 GUI 部署过)时,这里拿到空串,deploy_env 再落到
    #   v0.22.0 —— 而 help 写的是 v0.30.2;也不认 comfyui_tag_pin,钉在 v0.37.2 的云端被静默退回
    #   (2026-10-05 深度 review)。都没有就拒绝,不替用户挑一个会过时的默认值。
    pin = (plugin.get("comfyui_tag_pin") or "").strip()
    tag = (args.comfyui_tag or "").strip() or pin or (plugin.get("comfyui_tag") or "").strip() \
        or ((saved.get("comfyui_tag") or "").strip() if saved_same else "")
    if not tag:
        sys.exit("✗ 不知道云端该装哪个 ComfyUI 版本:第一次部署请用 --comfyui-tag vX.Y.Z 指定"
                 "(之后会沿用;装了插件的机器也可在插件 config 里设 comfyui_tag_pin)")
    if args.comfyui_tag and pin and args.comfyui_tag.strip() != pin:
        print(f"⚠ 显式指定 {args.comfyui_tag.strip()},与插件 config 的 comfyui_tag_pin={pin} 不同;"
              f"下一次 GUI 部署会按 pin 改回去")

    # reconcile 要读云端 /health:endpoint 用 cli.json 里这个 app 部署出来的,其次插件 config 的。
    # ⚠ 以前用的是插件 config 合并出来的默认值 https://YOUR_WORKSPACE--comfyui-bridge(占位):
    #   全新自建者查 /health 必然「不可达」,本机清单又是空的 → DeployBlocked,永远部署不了
    #   (2026-10-05 深度 review)。占位符 / 空 = 不知道 endpoint = 按没部署过处理。
    endpoint = (_known_endpoint(saved.get("endpoint")) if saved_same else "") \
        or _known_endpoint(plugin.get("modal_endpoint_base"))
    if not endpoint:
        # 本机没记录 ≠ 云端没部署:问一下 Modal(见 _lookup_deployed_endpoint)。查到就照常对账,
        # 确认没有才按全新部署;问不了就停 —— 宁可让用户重试,也别把一个已部署的 app 当空的覆盖。
        try:
            endpoint = _lookup_deployed_endpoint(args.app_name)
        except EndpointLookupFailed as e:
            sys.exit(f"✗ 本机没记下 app {args.app_name} 的 endpoint,也没法向 Modal 确认它是否已部署({e}),"
                     f"已中止 —— 按全新部署处理的话云端已装的节点一个都不会核对,可能被这次部署删掉。\n"
                     f"  检查 `modal token` / 网络后重试,或用 `configure --endpoint https://<ws>--{args.app_name}` 指定")
        if endpoint:
            print(f"      本机没有记录,但 Modal 上 app {args.app_name} 已部署过:{endpoint} —— 按已部署处理,核对云端节点")
    cfg = {**DEFAULT_CONFIG,
           **{k: plugin[k] for k in ("comfy_api_key", "hf_token", "civitai_token",
                                     "aigc_studio_base_url", "aigc_bypass_secret",
                                     "modal_volume_name", "scaledown_window",
                                     "volume_threshold_mb", "inline_total_mb",
                                     "disable_dynamic_vram", "enable_snapshot")
              if plugin.get(k) not in (None, "")},
           "modal_app_name": args.app_name,
           "modal_endpoint_base": endpoint,
           "comfyui_tag": tag,
           "default_gpu": pick(args.gpu, "default_gpu", "H100"),
           "cheap_gpu": pick(args.cheap_gpu, "cheap_gpu", "L40S"),
           "top_gpu": pick(args.top_gpu, "top_gpu", "B200"),
           "worker_timeout_sec": pick(args.timeout_s, "worker_timeout_sec", 3600),
           "use_sage_attention": pick(args.sage, "use_sage_attention", False),
           "bridge_api_key": bridge_key}
    env = node_sync.deploy_env(cfg)

    # 镜像节点清单取自本机那份被 gitignore 的文件;先把云端有、本机缺的并回来(只加不删),
    # 否则在清单丢失 / 别的机器加过节点时,这次部署会把它们从镜像里删掉。
    print(f"[1/3] 比对云端节点(app {args.app_name},{'endpoint ' + endpoint if endpoint else '没有已知 endpoint,按全新部署'})…")
    node_sync.ensure_baked_file()
    try:
        rec = node_sync.reconcile_baked_with_cloud(cfg)
    except node_sync.DeployBlocked as e:
        sys.exit(f"✗ {e}")
    if rec.added:
        print(f"      节点清单:并回云端独有的 {len(rec.added)} 个 —— {', '.join(rec.added)}")
    print(node_sync.drift_message(rec), end="")
    # 私有节点依赖按 Volume 上每个包的 manifest 刷新(同 GUI);读不到就中止,别拿过时的文件去构建
    try:
        reqs = local_nodes.volume_node_requirements(cfg, node_sync.read_local_node_reqs())
    except local_nodes.VolumeUnavailable as e:
        sys.exit(f"✗ 读不到 Volume 上的私有节点依赖({e}),已中止 —— 照旧部署可能把私有节点的依赖从镜像里漏掉")
    node_sync.write_local_node_reqs(reqs)

    # Secret 放在所有可能中止的检查之后、modal deploy 之前(契约 C16);合并更新,只写这次有值的键
    print(f"[2/3] 更新 Secret({args.app_name}-secrets,合并写入,其余键不动)…")
    r = subprocess.run(node_sync.secret_upsert_cmd(
                           cfg, cfg.get("hf_token", ""), cfg.get("civitai_token", ""), bridge_key,
                           cfg.get("comfy_api_key", ""), cfg.get("aigc_studio_base_url", ""),
                           cfg.get("aigc_bypass_secret", "")),
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"secret 写入失败:{((r.stdout or '') + (r.stderr or ''))[-500:]}\n"
                 f"(先 `pip install modal && modal token new`)")

    print(f"[3/3] modal deploy(ComfyUI tag {cfg['comfyui_tag']},首次要构建镜像,10 分钟级)…")
    proc = subprocess.Popen(node_sync.deploy_command(), cwd=str(_HERE / "modal_app"), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail_lines: list[str] = []
    for line in proc.stdout:
        print("  " + line.rstrip(), flush=True)
        tail_lines.append(line)
    if proc.wait() != 0:
        sys.exit("deploy 失败(日志见上)")
    # 插件的依赖预检读 <app>-meta 里这份(任何机器部署都写);不记的话它拿上次 GUI 部署的旧值比
    modal_volume.record_deployed_reqs(cfg, reqs)

    # 从输出解析 endpoint base:https://<ws>--<app>-run.modal.run → https://<ws>--<app>
    m = re.search(rf"https://[\w\-]+--{re.escape(args.app_name)}-\w+\.modal\.run",
                  "".join(tail_lines))
    saved = _load_cli_cfg()
    keys = saved.get("keys") if isinstance(saved.get("keys"), dict) else {}
    done = {**saved, "key": bridge_key, "app_name": args.app_name, "volume": cfg["modal_volume_name"],
            "comfyui_tag": tag, "keys": {**keys, args.app_name: bridge_key}}
    if m:
        base = re.sub(r"-(run|status|cancel|health|fetch)\.modal\.run$", "", m.group(0))
        _save_cli_cfg({**done, "endpoint": base})
        print(f"\n✓ 部署完成。endpoint + key 已写 {CLI_CFG}")
        print("  下一步:upload-model 把工作流要的模型放上 Volume,然后 submit。")
    else:
        done["endpoint"] = endpoint      # 不知道就留空,别留着别的 app 的 endpoint 配这把 key
        _save_cli_cfg(done)
        print("\n✓ 部署完成,但没从输出解析到 endpoint —— 手动 `configure --endpoint …`"
              f"(key 已写 {CLI_CFG})")


def cmd_upload_model(args):
    """本地模型 → Volume 的 models/<type>/。type 用 ComfyUI 目录名:
    diffusion_models / text_encoders / vae / loras / checkpoints / clip_vision …"""
    saved = _load_cli_cfg()
    volume = args.volume or saved.get("volume") or "comfyui-bridge-models"
    src = Path(args.file)
    if not src.exists():
        sys.exit(f"文件不存在: {src}")
    remote = f"/models/{args.type}/{src.name}"
    print(f"{src.name} → {volume}:{remote}(modal volume put,大文件走上行带宽)")
    r = subprocess.run(_modal_cmd("volume", "put", volume, str(src), remote))
    sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(p):
        p.add_argument("--endpoint", help="https://<ws>--comfyui-bridge")
        p.add_argument("--key", help="bridge_api_key")

    p = sub.add_parser("health", help="云端健康检查")
    _common(p)
    p.set_defaults(f=cmd_health)

    p = sub.add_parser("submit", help="提交工作流(API prompt JSON 文件)")
    _common(p)
    p.add_argument("workflow")
    p.add_argument("--gpu-class", default="primary", choices=["primary", "cheap", "top"])
    p.add_argument("--wait", action="store_true", help="阻塞到完成并自动取产物")
    p.add_argument("--out", default="./modal_bridge_outputs")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--input-dir", action="append", help="输入图搜索目录(可多个)")
    p.set_defaults(f=cmd_submit)

    p = sub.add_parser("status", help="查任务状态")
    _common(p)
    p.add_argument("job_id")
    p.set_defaults(f=cmd_status)

    p = sub.add_parser("fetch", help="取回已完成任务的产物")
    _common(p)
    p.add_argument("job_id")
    p.add_argument("--out", default="./modal_bridge_outputs")
    p.set_defaults(f=cmd_fetch)

    p = sub.add_parser("cancel", help="取消任务(退出码区分结局,见 cancel --help)",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       description="取消任务。退出码:\n"
                                   "  0  已取消,或任务早已结束(完成 / 失败 / 被判死)—— 不再计费\n"
                                   "  3  云端查无此任务(id 不对,或早已结束并被清理)—— 没有在跑\n"
                                   "  4  取消失败,任务可能仍在跑、仍在计费 —— 去 Modal 控制台确认\n"
                                   "  5  请求没成功(网络 / HTTP 错误 / key 不对),结果未知 —— 稍后重试或 status 核实")
    _common(p)
    p.add_argument("job_id")
    p.set_defaults(f=cmd_cancel)

    p = sub.add_parser("configure", help="保存 endpoint/key 到 ~/.modal_bridge/cli.json")
    _common(p)
    p.set_defaults(f=cmd_configure)

    p = sub.add_parser("deploy", help="[自建者] 无 ComfyUI 部署云端 app(需 modal token)")
    p.add_argument("--app-name", default="comfyui-bridge")
    p.add_argument("--comfyui-tag", default=None,
                   help="云端 ComfyUI 版本 tag。不传:插件 config 的 comfyui_tag_pin > 插件上次部署的 comfyui_tag"
                        " > 本 CLI 上次部署的;都没有则拒绝部署(第一次请显式指定)")
    p.add_argument("--gpu", default=None, help="不传:沿用插件配置,没有则 H100")
    p.add_argument("--cheap-gpu", default=None)
    p.add_argument("--top-gpu", default=None)
    p.add_argument("--timeout-s", type=int, default=None)
    p.add_argument("--sage", action="store_true", default=None, help="开 SageAttention(H100/L40S 生效,自行看片验证)")
    p.set_defaults(f=cmd_deploy)

    p = sub.add_parser("upload-model", help="[自建者] 本地模型上 Volume")
    p.add_argument("file")
    p.add_argument("type", help="ComfyUI 模型目录名,如 diffusion_models")
    p.add_argument("--volume", help="Volume 名(默认部署时的)")
    p.set_defaults(f=cmd_upload_model)

    args = ap.parse_args()
    try:
        args.f(args)
    except BridgeError as e:
        sys.exit(f"✗ {e}")
    except KeyboardInterrupt:
        sys.exit("\n中断(云端任务不受影响,可 status/cancel)")
    except Exception as e:
        # 下载 / 解析途中的 URLError、IncompleteRead、ValueError、OSError 等不是 BridgeError,
        # 以前直接甩一屏 traceback(2026-10-05 深度 review)。说清是哪一步、什么错就够了;
        # 已落盘的 .part 会被清掉,远端产物没 ack 不会删,可以重跑 fetch。
        sys.exit(f"✗ {args.cmd} 失败:{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
