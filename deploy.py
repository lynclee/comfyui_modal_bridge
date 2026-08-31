"""
deploy.py — comfyui_modal_bridge 命令行部署(简化版)

⚠ **不等价于 GUI 的 [⚙️ Modal Setup]**,后者才是推荐路径。这里少做了几件事:
  - 不解析 ComfyUI 版本 → 云端 clone 的 tag 只能吃 modal_image 的兜底值,
    可能与本机 ComfyUI 不匹配;
  - 不生成 extra_model_paths.yaml(自定义模型目录);
  - 不同步 custom_nodes / 本地自写节点的依赖清单。
适合"只想把 app 部署起来"的场景;要完整链路请用 GUI 部署。

帮你:
  1. 确保本机能 import modal
  2. 建/更新 Modal Secret(BRIDGE_API_KEY 私有鉴权 + 可选 HF_TOKEN)
  3. modal deploy modal_app/modal_app.py
  4. 把 endpoint base + token + bridge key 写进 config.json

用法:
    cd custom_nodes/comfyui_modal_bridge
    python deploy.py --workspace your-workspace --token-id ak-xxx --token-secret as-xxx
    python deploy.py --workspace your-workspace      # token 走环境变量 MODAL_TOKEN_ID/SECRET
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODAL_APP_DIR = HERE / "modal_app"

sys.path.insert(0, str(HERE))
import node_sync  # noqa: E402  (复用 deploy_env / secret_create_cmd / gen_bridge_key)

APP_NAME = "comfyui-bridge"


def run(cmd, **kw):
    # 凭据打码后再回显(secret create 的 argv 里是明文 token,见 node_sync.redact_cmd)
    print(f"$ {node_sync.redact_cmd(cmd)}")
    return subprocess.run(cmd, **kw).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True, help="Modal workspace(modal.com 个人主页那段,如 your-workspace)")
    ap.add_argument("--token-id", default=os.environ.get("MODAL_TOKEN_ID", ""), help="ak-...")
    ap.add_argument("--token-secret", default=os.environ.get("MODAL_TOKEN_SECRET", ""), help="as-...")
    ap.add_argument("--hf-token", default="", help="可选,下私有模型用(本方案模型走本地上传,一般不需要)")
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
    cfg = dict(cfg_mod.load_config())
    ws = args.workspace
    cfg["modal_endpoint_base"] = f"https://{ws}--{APP_NAME}"
    cfg["modal_workspace"] = ws
    cfg["modal_app_name"] = APP_NAME
    cfg.setdefault("modal_volume_name", "comfyui-bridge-models")
    cfg.setdefault("scaledown_window", 12)
    if args.token_id:
        cfg["modal_token_id"] = args.token_id
    if args.token_secret:
        cfg["modal_token_secret"] = args.token_secret
    cfg["bridge_api_key"] = cfg.get("bridge_api_key") or node_sync.gen_bridge_key()

    env = node_sync.deploy_env(cfg)

    print("\n== 建/更新 Secret ==")
    # ⚠ 必须把 config 里已有的集成字段一并传进去。以前只传 hf_token + bridge_key,
    # comfy_api_key / aigc_* 吃函数默认的空串 —— 于是用 CLI 部署一次,工作流里的
    # ComfyUI API 节点鉴权和 aigc-r2 交付就**静默失效**(config 里明明配着)。
    rc = run(node_sync.secret_create_cmd(
                 cfg,
                 args.hf_token,
                 cfg.get("civitai_token", ""),
                 cfg["bridge_api_key"],
                 cfg.get("comfy_api_key", ""),
                 cfg.get("aigc_studio_base_url", ""),
                 cfg.get("aigc_bypass_secret", "")),
             cwd=str(MODAL_APP_DIR), env=env)
    if rc != 0:
        print("✗ secret 创建失败(token 可能无效)")
        sys.exit(rc)

    print("\n== 部署(首次拉镜像约 3-5 分钟)==")
    rc = run(node_sync.deploy_command(), cwd=str(MODAL_APP_DIR), env=env)
    if rc != 0:
        print("✗ deploy 失败")
        sys.exit(rc)

    # 走 config.save_config 而不是直接 write_text:那边是「临时文件 + chmod 600 + os.replace」。
    # 直接写的话既非原子(写一半崩 → 半个 JSON → 加载静默回落默认值,表现成"配置没了"),
    # 权限也是默认的 0644,而这个文件里有 Modal token / bridge key 等四种凭据。
    cfg_mod.save_config(cfg)
    print(f"\n✓ 写入 {cfg_mod._config_path()}")
    print(f"  endpoint base: {cfg['modal_endpoint_base']}")
    print("\n完成!回 ComfyUI 点 ☁️ Modal 跑图。模型用 `python sync_models.py` 整体推上去(或提交时自动同步)。")


if __name__ == "__main__":
    main()
