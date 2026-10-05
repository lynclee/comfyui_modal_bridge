"""
deploy.py — comfyui_modal_bridge 命令行部署(简化版)

⚠ **不等价于 GUI 的 [⚙️ Modal Setup]**,后者才是推荐路径。这里少做了几件事:
  - 不生成 extra_model_paths.yaml(自定义模型目录);
  - 不推送本机的私有节点(只按 Volume 上已有的私有节点刷新依赖清单)。
云端 ComfyUI 版本:--comfyui-tag > config 的 comfyui_tag_pin > 本机版本(在 ComfyUI 里跑时)>
上次部署记下的 comfyui_tag;都没有就拒绝部署,不再静默落到一个老版本。
适合"只想把 app 部署起来"的场景;要完整链路请用 GUI 部署。

帮你:
  1. 确保本机能 import modal
  2. 并回云端独有的节点、刷新私有节点依赖清单(读不到就中止,不留半截状态)
  3. 合并更新 Modal Secret(BRIDGE_API_KEY 私有鉴权 + 可选 HF_TOKEN;只写这次有值的键)
  4. modal deploy modal_app/modal_app.py
  5. 把 endpoint base + token + bridge key 写进 config.json

用法:
    cd custom_nodes/comfyui_modal_bridge
    python deploy.py --workspace your-workspace --token-id ak-xxx --token-secret as-xxx
    python deploy.py --workspace your-workspace      # token 走环境变量 MODAL_TOKEN_ID/SECRET
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODAL_APP_DIR = HERE / "modal_app"

sys.path.insert(0, str(HERE))
import local_nodes  # noqa: E402  (Volume 上私有节点的依赖清单)
import modal_volume  # noqa: E402  (部署成功后记下镜像里的私有节点依赖)
import node_sync  # noqa: E402  (复用 deploy_env / secret_upsert_cmd / gen_bridge_key)

DEFAULT_APP_NAME = "comfyui-bridge"


def run(cmd, **kw):
    # 凭据打码后再回显(secret 命令的 argv 里是明文 token,见 node_sync.redact_cmd)
    print(f"$ {node_sync.redact_cmd(cmd)}")
    return subprocess.run(cmd, **kw).returncode


def _persist(cfg_mod, updates: dict) -> None:
    """只把这几个键并进**当前**的 config 再写回(不拿启动时的快照整份覆盖,免得冲掉别处的改动)。"""
    latest = dict(cfg_mod.load_config())
    latest.update(updates)
    cfg_mod.save_config(latest)


def _resolve_tag(args, cfg: dict) -> tuple[str, str]:
    """云端 ComfyUI tag → (tag, 说明)。拿不到可信的值返回 ("", 原因)。

    ⚠ 以前直接吃 config 的 comfyui_tag,空了就落到 node_sync 的兜底 v0.22.0(不支持 MiniMax H3、
      新节点全部导入失败),也不认 comfyui_tag_pin —— 钉在 v0.37.2 的云端被这条命令静默退回去
      (2026-10-05 深度 review)。"""
    pin = (cfg.get("comfyui_tag_pin") or "").strip()
    explicit = (args.comfyui_tag or "").strip()
    if explicit:
        note = f"显式指定 {explicit},与 config 的 comfyui_tag_pin={pin} 不同" if pin and pin != explicit else ""
        return explicit, note
    version = node_sync.detect_local_comfyui_version()
    prev = (cfg.get("comfyui_tag") or "").strip()
    if not (pin or version or prev):
        return "", ("不知道云端该装哪个 ComfyUI 版本(不在 ComfyUI 里运行、config 里也没有 comfyui_tag / "
                    "comfyui_tag_pin)。用 --comfyui-tag vX.Y.Z 指定")
    tags = node_sync.list_comfyui_tags() if (version and not pin) else []
    return node_sync.resolve_comfyui_tag(version, tags, prev_tag=prev, pin=pin)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, help="Modal workspace(modal.com 个人主页那段,如 your-workspace)")
    ap.add_argument("--token-id", default=os.environ.get("MODAL_TOKEN_ID", ""), help="ak-...")
    ap.add_argument("--token-secret", default=os.environ.get("MODAL_TOKEN_SECRET", ""), help="as-...")
    ap.add_argument("--hf-token", default="", help="可选,下私有模型用(本方案模型走本地上传,一般不需要)")
    ap.add_argument("--app-name", default=None,
                    help=f"Modal app 名(不传:沿用 config 里的 modal_app_name,没有则 {DEFAULT_APP_NAME})")
    ap.add_argument("--comfyui-tag", default=None,
                    help="云端 ComfyUI 版本 tag(不传:comfyui_tag_pin > 本机版本 > 上次部署的 comfyui_tag)")
    args = ap.parse_args()

    try:
        import modal  # noqa
        print(f"✓ modal {modal.__version__}")
    except ImportError:
        print("✗ modal 没装。先 pip install modal")
        sys.exit(1)

    # 组 config(复用已有的,补齐这次的)。读写都走 config 模块 —— 以前读用硬编码的
    # 硬编码路径、写用 write_text,路径在两处各写一遍,迟早漂移到读一个文件写另一个。
    import config as cfg_mod
    # ⚠ config.json 在但坏了:load_config 会退回默认值(或在新版里抛错),接下来 _persist 就拿默认值
    #   + 一把新 key 把它覆盖掉 —— 插件里原来那把 key 失效、所有调用方 401(2026-10-05 routes 一路协调)。
    #   先自己验一遍,坏了就停。
    _p = cfg_mod._config_path()
    if _p.exists():
        try:
            if not isinstance(json.loads(_p.read_text(encoding="utf-8")), dict):
                raise ValueError("不是 JSON 对象")
        except (OSError, ValueError) as e:
            sys.exit(f"✗ 插件 config {_p} 读不了 / 已损坏({e}),已中止 —— 照常部署会用默认值和一把新 key"
                     f"覆盖它。修好(或确认不要后删掉)再部署。")
    try:
        cfg = dict(cfg_mod.load_config())
    except Exception as e:      # 新版 config 对损坏文件抛 ConfigCorrupt:同样停下,不吞
        sys.exit(f"✗ 读插件 config 失败({type(e).__name__}: {e}),已中止")
    # AIGC 地址会原样写进 Secret,worker 往它发带旁路密钥 / job token 的回调:只收 https://
    # (2026-10-05 深度 review 第二轮,规则见 contract.aigc_url_problem)。在任何写入之前挡。
    import contract
    _aigc_err = contract.aigc_url_problem(cfg.get("aigc_studio_base_url") or "")
    if _aigc_err:
        sys.exit(f"✗ 插件设置里的 {_aigc_err}。改好(或清空 = 停用)后再部署")
    ws = args.workspace
    # ⚠ 以前写死 comfyui-bridge:用了自定义 app 名的用户跑一次就多出第二个 app,插件 config 也被
    #   改指向它(2026-10-05 深度 review)。
    app_name = (args.app_name or cfg.get("modal_app_name") or DEFAULT_APP_NAME).strip()
    if args.app_name and cfg.get("modal_app_name") and args.app_name != cfg.get("modal_app_name"):
        print(f"⚠ 部署到 app {args.app_name},而插件 config 里是 {cfg.get('modal_app_name')};"
              f"成功后插件会改用 {args.app_name}")

    # ① 先落盘再写 Secret(契约 C16):新生成的 bridge key、这次传的 HF token 若只进了 Secret,
    #    部署中途失败时本机就没有它们 —— key 对不上全部 401;HF token 下次 GUI 部署就被抹掉。
    early = {}
    if not cfg.get("bridge_api_key"):
        early["bridge_api_key"] = node_sync.gen_bridge_key()
    if args.hf_token:
        early["hf_token"] = args.hf_token
    if early:
        _persist(cfg_mod, early)
        cfg.update(early)

    tag, tag_note = _resolve_tag(args, cfg)
    if not tag:
        sys.exit(f"✗ {tag_note}")
    if tag_note:
        print(f"   ⚠ {tag_note}")
    print(f"   云端 ComfyUI:{tag}")
    _prev_tag = cfg.get("comfyui_tag")

    cfg["modal_endpoint_base"] = f"https://{ws}--{app_name}"
    cfg["modal_workspace"] = ws
    cfg["modal_app_name"] = app_name
    cfg["comfyui_tag"] = tag
    cfg.setdefault("modal_volume_name", "comfyui-bridge-models")
    cfg.setdefault("scaledown_window", 12)
    if args.token_id:
        cfg["modal_token_id"] = args.token_id
    if args.token_secret:
        cfg["modal_token_secret"] = args.token_secret
    _change = node_sync.comfyui_tag_change_note(_prev_tag, tag)
    if _change:
        print(f"   ⚠ {_change}")

    env = node_sync.deploy_env(cfg)

    # ② 同 GUI /deploy:本机清单会被当成镜像的全局清单,先并回云端独有的节点(只加不删)。
    print("\n== 比对云端节点 ==")
    node_sync.ensure_baked_file()
    try:
        rec = node_sync.reconcile_baked_with_cloud(cfg)
    except node_sync.DeployBlocked as e:
        sys.exit(f"✗ {e}")
    if rec.added:
        print(f"   节点清单:并回云端独有的 {len(rec.added)} 个 —— {', '.join(rec.added)}")
    print(node_sync.drift_message(rec), end="")

    # ③ 私有节点依赖按 Volume 上每个包的 manifest 刷新。以前这里不刷新、部署后也不记录:
    #    镜像按本机那份可能过时 / 缺失的文件构建,插件的依赖预检还被骗成「不用重建」(2026-10-05 深度 review)。
    try:
        reqs = local_nodes.volume_node_requirements(cfg, node_sync.read_local_node_reqs())
    except local_nodes.VolumeUnavailable as e:
        sys.exit(f"✗ 读不到 Volume 上的私有节点依赖({e}),已中止 —— 照旧部署可能把私有节点的依赖从镜像里漏掉")
    node_sync.write_local_node_reqs(reqs)
    if reqs:
        print(f"   私有节点依赖:{len(reqs)} 条(镜像 build 期安装)")

    # ④ Secret 放在所有可能中止的检查之后、modal deploy 之前(契约 C16);合并更新,只写这次有值的键 ——
    #    以前 --force 整份重建,本机 config 里没有的 HF / comfy.org / AIGC 凭据被一起抹掉。
    print("\n== 更新 Modal Secret ==")
    rc = run(node_sync.secret_upsert_cmd(
                 cfg,
                 cfg.get("hf_token", ""),
                 cfg.get("civitai_token", ""),
                 cfg["bridge_api_key"],
                 cfg.get("comfy_api_key", ""),
                 cfg.get("aigc_studio_base_url", ""),
                 cfg.get("aigc_bypass_secret", "")),
             cwd=str(MODAL_APP_DIR), env=env)
    if rc != 0:
        print("✗ secret 写入失败(token 可能无效)")
        sys.exit(rc)
    # 记下这台机器往 Secret 里写了哪些 AIGC 键:GUI 部署据此判断 config 里的空值是「用户清掉了」
    # 还是「这台机器从没配过」(见 routes 的 _clear,2026-10-05 深度 review 第二轮)。
    # ⚠ 取并集,不覆盖:deploy.py 不清 Secret 里的 AIGC 键,覆盖成「这次有值的」会把 GUI 部署要清的那一项
    #   从记录里抹掉,旧 URL 从此留在 Secret 里(2026-10-05 第二轮复核)。
    _old = [f for f in (cfg.get(contract.AIGC_PUSHED_FIELD) or []) if isinstance(f, str)]
    _now = [f for f, k in (("aigc_base_url", "aigc_studio_base_url"), ("aigc_bypass_secret", "aigc_bypass_secret"))
            if cfg.get(k)]
    _persist(cfg_mod, {contract.AIGC_PUSHED_FIELD: sorted(set(_old) | set(_now))})

    print("\n== 部署(首次拉镜像约 3-5 分钟)==")
    rc = run(node_sync.deploy_command(), cwd=str(MODAL_APP_DIR), env=env)
    if rc != 0:
        print("✗ deploy 失败")
        sys.exit(rc)

    # 走 config.save_config 而不是直接 write_text:那边是「临时文件 + chmod 600 + os.replace」。
    # 直接写的话既非原子(写一半崩 → 半个 JSON → 加载静默回落默认值,表现成"配置没了"),
    # 权限也是默认的 0644,而这个文件里有 Modal token / bridge key 等四种凭据。
    done = {k: cfg[k] for k in ("modal_endpoint_base", "modal_workspace", "modal_app_name",
                                "modal_volume_name", "scaledown_window", "comfyui_tag",
                                "bridge_api_key") if k in cfg}
    for k in ("modal_token_id", "modal_token_secret", "hf_token"):
        if cfg.get(k):
            done[k] = cfg[k]
    done["local_node_reqs_deployed_hash"] = node_sync.local_node_reqs_hash(reqs)
    _persist(cfg_mod, done)
    # 插件的依赖预检读的是 <app>-meta 里这份(任何机器部署都写),不记的话它拿上次 GUI 部署的旧值比
    modal_volume.record_deployed_reqs(cfg, reqs)
    print(f"\n✓ 写入 {cfg_mod._config_path()}")
    print(f"  endpoint base: {cfg['modal_endpoint_base']}")
    print("\n完成!回 ComfyUI 点 ☁️ Modal 跑图。模型用 `python sync_models.py` 整体推上去(或提交时自动同步)。")


if __name__ == "__main__":
    main()
