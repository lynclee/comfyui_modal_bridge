"""
deploy.py — comfyui_modal_bridge 命令行部署(GUI 上点 [⚙️ Modal Setup] 是等价的零终端版)

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
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODAL_APP_DIR = HERE / "modal_app"
CONFIG_DST = HERE.parents[1] / "user" / "default" / "modal_bridge" / "config.json"

sys.path.insert(0, str(HERE))
import node_sync  # noqa: E402  (复用 deploy_env / secret_create_cmd / gen_bridge_key)

APP_NAME = "comfyui-bridge"


def run(cmd, **kw):
    print(f"$ {' '.join(cmd)}")
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

    # 组 config(复用已有的,补齐这次的)
    cfg = {}
    if CONFIG_DST.exists():
        cfg = json.loads(CONFIG_DST.read_text(encoding="utf-8"))
    ws = args.workspace
    cfg["modal_endpoint_base"] = f"https://{ws}--{APP_NAME}"
    cfg["modal_workspace"] = ws
    cfg["modal_app_name"] = APP_NAME
    cfg.setdefault("modal_volume_name", "comfyui-bridge-models")
    cfg.setdefault("scaledown_window", 40)
    if args.token_id:
        cfg["modal_token_id"] = args.token_id
    if args.token_secret:
        cfg["modal_token_secret"] = args.token_secret
    cfg["bridge_api_key"] = cfg.get("bridge_api_key") or node_sync.gen_bridge_key()

    env = node_sync.deploy_env(cfg)

    print("\n== 建/更新 Secret ==")
    rc = run(node_sync.secret_create_cmd(cfg, args.hf_token, "", cfg["bridge_api_key"]),
             cwd=str(MODAL_APP_DIR), env=env)
    if rc != 0:
        print("✗ secret 创建失败(token 可能无效)"); sys.exit(rc)

    print("\n== 部署(首次拉镜像约 3-5 分钟)==")
    rc = run(node_sync.deploy_command(), cwd=str(MODAL_APP_DIR), env=env)
    if rc != 0:
        print("✗ deploy 失败"); sys.exit(rc)

    CONFIG_DST.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_DST.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n✓ 写入 {CONFIG_DST}")
    print(f"  endpoint base: {cfg['modal_endpoint_base']}")
    print("\n完成!回 ComfyUI 点 ☁️ Modal 跑图。模型用 `python sync_models.py` 整体推上去(或提交时自动同步)。")


if __name__ == "__main__":
    main()
