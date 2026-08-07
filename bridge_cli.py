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
    python bridge_cli.py deploy --comfyui-tag v0.30.2
    python bridge_cli.py upload-model /path/to/model.safetensors diffusion_models
限制(与完整插件的差异):无 custom_node 自动同步(镜像只有内置节点,除非部署者本机同步过)、
无模型自动上传(跑前用 upload-model 手动补)、无显存估算路由(--gpu-class 手选)。

消费者用法:
    python bridge_cli.py configure --endpoint https://<ws>--comfyui-bridge --key bk-xxx
    python bridge_cli.py submit workflow_api.json --wait --out ./outputs
workflow_api.json 是 ComfyUI 的 API prompt(UI 里「导出(API)」得到的格式)。
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

CLI_CFG = Path.home() / ".modal_bridge" / "cli.json"


def _load_cli_cfg() -> dict:
    try:
        return json.loads(CLI_CFG.read_text())
    except Exception:
        return {}


def _save_cli_cfg(d: dict) -> None:
    CLI_CFG.parent.mkdir(parents=True, exist_ok=True)
    CLI_CFG.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    try:
        CLI_CFG.chmod(0o600)  # 含 bridge_key
    except Exception:
        pass


def _client(args) -> BridgeClient:
    saved = _load_cli_cfg()
    endpoint = (getattr(args, "endpoint", None) or os.environ.get("MODAL_BRIDGE_ENDPOINT")
                or saved.get("endpoint") or "")
    key = (getattr(args, "key", None) or os.environ.get("MODAL_BRIDGE_KEY")
           or saved.get("key") or "")
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


def cmd_status(args):
    print(json.dumps(_client(args).status(args.job_id), indent=2, ensure_ascii=False))


def cmd_fetch(args):
    c = _client(args)
    s = c.status(args.job_id)
    if s.get("status") != "completed":
        sys.exit(f"未完成: {s.get('status')}")
    for o in c.download_outputs(s, os.path.join(args.out, args.job_id)):
        print(f"✓ {o['path']}  ({o['size_bytes'] / 1e6:.1f} MB)")


def cmd_cancel(args):
    r = _client(args).cancel(args.job_id)
    print(json.dumps(r, ensure_ascii=False))
    if r.get("error"):
        sys.exit("⚠ 取消失败 — 云端仍在跑、仍在计费,去 Modal 控制台确认")


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


def cmd_deploy(args):
    """无 ComfyUI 的部署:复用插件的 deploy_env / secret 链路,避开「裸 modal deploy」陷阱
    (裸跑会丢 MODAL_BRIDGE_* env → 云端 ComfyUI 落到老兜底 tag、GPU/超时全回默认)。"""
    import node_sync
    from config import DEFAULT_CONFIG

    saved = _load_cli_cfg()
    bridge_key = saved.get("key") or node_sync.gen_bridge_key()  # 复用旧 key,重部署不换锁
    cfg = {**DEFAULT_CONFIG,
           "modal_app_name": args.app_name,
           "comfyui_tag": args.comfyui_tag,
           "default_gpu": args.gpu, "cheap_gpu": args.cheap_gpu, "top_gpu": args.top_gpu,
           "worker_timeout_sec": args.timeout_s,
           "use_sage_attention": args.sage,
           "bridge_api_key": bridge_key}
    env = node_sync.deploy_env(cfg)

    print(f"[1/2] 建/更新 Secret({args.app_name}-secrets)…")
    r = subprocess.run(node_sync.secret_create_cmd(cfg, bridge_key=bridge_key),
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"secret 创建失败:{r.stderr[-500:]}\n(先 `pip install modal && modal token new`)")

    print(f"[2/2] modal deploy(ComfyUI tag {args.comfyui_tag},首次要构建镜像,10 分钟级)…")
    proc = subprocess.Popen(node_sync.deploy_command(), cwd=str(_HERE / "modal_app"), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail_lines: list[str] = []
    for line in proc.stdout:
        print("  " + line.rstrip(), flush=True)
        tail_lines.append(line)
    if proc.wait() != 0:
        sys.exit("deploy 失败(日志见上)")

    # 从输出解析 endpoint base:https://<ws>--<app>-run.modal.run → https://<ws>--<app>
    m = re.search(rf"https://[\w\-]+--{re.escape(args.app_name)}-\w+\.modal\.run",
                  "".join(tail_lines))
    if m:
        base = re.sub(rf"-(run|status|cancel|health|fetch)\.modal\.run$", "", m.group(0))
        _save_cli_cfg({**saved, "endpoint": base, "key": bridge_key,
                       "app_name": args.app_name, "volume": cfg["modal_volume_name"]})
        print(f"\n✓ 部署完成。endpoint + key 已写 {CLI_CFG}")
        print(f"  下一步:upload-model 把工作流要的模型放上 Volume,然后 submit。")
    else:
        print("\n✓ 部署完成,但没从输出解析到 endpoint —— 手动 `configure --endpoint …`"
              f"(key: {bridge_key})")


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

    p = sub.add_parser("health", help="云端健康检查");                       _common(p); p.set_defaults(f=cmd_health)
    p = sub.add_parser("submit", help="提交工作流(API prompt JSON 文件)");   _common(p)
    p.add_argument("workflow"); p.add_argument("--gpu-class", default="primary", choices=["primary", "cheap", "top"])
    p.add_argument("--wait", action="store_true", help="阻塞到完成并自动取产物")
    p.add_argument("--out", default="./modal_bridge_outputs"); p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--input-dir", action="append", help="输入图搜索目录(可多个)")
    p.set_defaults(f=cmd_submit)
    p = sub.add_parser("status", help="查任务状态");   _common(p); p.add_argument("job_id"); p.set_defaults(f=cmd_status)
    p = sub.add_parser("fetch", help="取回已完成任务的产物"); _common(p); p.add_argument("job_id")
    p.add_argument("--out", default="./modal_bridge_outputs"); p.set_defaults(f=cmd_fetch)
    p = sub.add_parser("cancel", help="取消任务");     _common(p); p.add_argument("job_id"); p.set_defaults(f=cmd_cancel)
    p = sub.add_parser("configure", help="保存 endpoint/key 到 ~/.modal_bridge/cli.json"); _common(p); p.set_defaults(f=cmd_configure)

    p = sub.add_parser("deploy", help="[自建者] 无 ComfyUI 部署云端 app(需 modal token)")
    p.add_argument("--app-name", default="comfyui-bridge")
    p.add_argument("--comfyui-tag", default="v0.30.2", help="云端 ComfyUI 版本 tag")
    p.add_argument("--gpu", default="H100"); p.add_argument("--cheap-gpu", default="L40S")
    p.add_argument("--top-gpu", default="B200"); p.add_argument("--timeout-s", type=int, default=3600)
    p.add_argument("--sage", action="store_true", help="开 SageAttention(H100/L40S 生效,自行看片验证)")
    p.set_defaults(f=cmd_deploy)

    p = sub.add_parser("upload-model", help="[自建者] 本地模型上 Volume")
    p.add_argument("file"); p.add_argument("type", help="ComfyUI 模型目录名,如 diffusion_models")
    p.add_argument("--volume", help="Volume 名(默认部署时的)")
    p.set_defaults(f=cmd_upload_model)

    args = ap.parse_args()
    try:
        args.f(args)
    except BridgeError as e:
        sys.exit(f"✗ {e}")
    except KeyboardInterrupt:
        sys.exit("\n中断(云端任务不受影响,可 status/cancel)")


if __name__ == "__main__":
    main()
