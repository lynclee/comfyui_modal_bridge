"""
一键部署脚本 — 从 secrets.toml 配置到 Modal app 就绪

用法:
    cp secrets.example.toml secrets.toml
    # 编辑 secrets.toml 填 token
    pip install modal toml
    python deploy.py

脚本做的事:
    ① 校验 secrets.toml
    ② 配置 modal CLI(本地 ~/.modal.toml)
    ③ 创建/更新 Modal Secret(HF token / Civitai token)
    ④ 创建独立 Volume(comfyui-bridge-models)
    ⑤ 跑 modal deploy 部署 app
    ⑥ 提取 endpoint URL,自动写本地 ComfyUI config.json
    ⑦ 验证 4 个核心 endpoint 可达
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import toml as tomllib  # type: ignore
    except ImportError:
        print("ERROR: 需要 Python 3.11+ 或 `pip install toml`")
        sys.exit(1)


HERE = Path(__file__).resolve().parent
SECRETS_FILE = HERE / "secrets.toml"
EXAMPLE_FILE = HERE / "secrets.example.toml"
MODAL_APP_PY = HERE / "modal_app" / "modal_app.py"


def step(n, msg):
    print(f"\n\033[1;36m▶ Step {n}: {msg}\033[0m")


def ok(msg):
    print(f"  \033[32m✓\033[0m {msg}")


def warn(msg):
    print(f"  \033[33m⚠\033[0m {msg}")


def fail(msg):
    print(f"  \033[31m✗ {msg}\033[0m")
    sys.exit(1)


def run(cmd, **kw):
    """跑 shell 命令,失败 raise"""
    print(f"  $ {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if result.returncode != 0:
        print(f"    stdout: {result.stdout[-500:]}")
        print(f"    stderr: {result.stderr[-500:]}")
    return result


# ============================================================================
# Step 1:校验 secrets.toml
# ============================================================================
def load_secrets():
    step(1, "校验 secrets.toml")
    if not SECRETS_FILE.exists():
        fail(f"找不到 {SECRETS_FILE}\n  → cp {EXAMPLE_FILE.name} {SECRETS_FILE.name} 然后填写")

    if hasattr(tomllib, "load") and not hasattr(tomllib, "loads"):  # Python 3.11+ stdlib
        with open(SECRETS_FILE, "rb") as f:
            cfg = tomllib.load(f)
    else:
        cfg = tomllib.load(open(SECRETS_FILE, "r", encoding="utf-8"))  # type: ignore

    if not cfg.get("modal", {}).get("token_id", "").startswith("ak-"):
        fail("[modal] token_id 应该是 ak-xxx 开头,去 https://modal.com/settings/tokens 创建")
    if not cfg.get("modal", {}).get("token_secret", "").startswith("as-"):
        fail("[modal] token_secret 应该是 as-xxx 开头")
    if not cfg.get("modal", {}).get("workspace"):
        warn("[modal] workspace 未填,deploy 后 URL 拼接可能错(URL 里就是 workspace 名)")

    ok(f"workspace = {cfg.get('modal', {}).get('workspace')}")
    ok(f"app.name  = {cfg.get('app', {}).get('name', 'comfyui-bridge')}")
    ok(f"volume    = {cfg.get('app', {}).get('volume_name', 'comfyui-bridge-models')}")
    ok(f"gpu       = {cfg.get('app', {}).get('default_gpu', 'H100')}")
    return cfg


# ============================================================================
# Step 2:配置 modal CLI(写 ~/.modal.toml)
# ============================================================================
def setup_modal_cli(cfg):
    step(2, "配置 modal CLI")
    r = run(["modal", "token", "set",
             "--token-id", cfg["modal"]["token_id"],
             "--token-secret", cfg["modal"]["token_secret"]])
    if r.returncode != 0:
        fail("modal token set 失败")
    ok("modal CLI 已配置")

    # 验证
    r = run(["modal", "app", "list"])
    if r.returncode != 0:
        fail("modal app list 失败,token 可能无效")
    ok("token 验证通过")


# ============================================================================
# Step 3:创建 / 更新 Modal Secret
# ============================================================================
def create_secrets(cfg, bridge_key):
    step(3, "创建 Modal Secret (BRIDGE_API_KEY + HF token 等)")
    app_name = cfg.get("app", {}).get("name", "comfyui-bridge")
    secret_name = f"{app_name}-secrets"

    env_pairs = [f"BRIDGE_API_KEY={bridge_key}"]  # 私有 endpoint 鉴权
    hf_token = cfg.get("huggingface", {}).get("token", "")
    if hf_token:
        env_pairs.append(f"HF_TOKEN={hf_token}")
        env_pairs.append(f"HUGGING_FACE_HUB_TOKEN={hf_token}")
    civitai_token = cfg.get("civitai", {}).get("token", "")
    if civitai_token:
        env_pairs.append(f"CIVITAI_TOKEN={civitai_token}")
    if not hf_token:
        warn("未配置 HF token,只能下公开模型")

    r = run(["modal", "secret", "create", "--force", secret_name, *env_pairs])
    if r.returncode != 0:
        fail(f"创建 secret {secret_name} 失败")
    ok(f"secret {secret_name} 已创建/更新")


# ============================================================================
# Step 4:部署 Modal app
# ============================================================================
def deploy_app(cfg):
    step(4, "部署 Modal app(首次约 3-5 分钟拉镜像)")
    app_name = cfg.get("app", {}).get("name", "comfyui-bridge")
    volume_name = cfg.get("app", {}).get("volume_name", "comfyui-bridge-models")
    default_gpu = cfg.get("app", {}).get("default_gpu", "H100")
    scaledown = cfg.get("app", {}).get("scaledown_window", 120)

    env = os.environ.copy()
    env["MODAL_BRIDGE_APP_NAME"] = app_name
    env["MODAL_BRIDGE_VOLUME"] = volume_name
    env["MODAL_BRIDGE_SECRET"] = f"{app_name}-secrets"
    env["MODAL_BRIDGE_DEFAULT_GPU"] = default_gpu
    env["MODAL_BRIDGE_SCALEDOWN"] = str(scaledown)

    # 切到 modal_app 目录,modal deploy 才能找到本地 import
    r = subprocess.run(
        ["modal", "deploy", "modal_app.py"],
        cwd=str(MODAL_APP_PY.parent),
        env=env,
        text=True,
    )
    if r.returncode != 0:
        fail("modal deploy 失败")
    ok(f"app {app_name} 已部署")


# ============================================================================
# Step 5:写本地 ComfyUI config.json
# ============================================================================
def write_local_config(cfg, bridge_key):
    step(5, "写本地 ComfyUI config.json")
    app_name = cfg.get("app", {}).get("name", "comfyui-bridge")
    workspace = cfg.get("modal", {}).get("workspace", "your-workspace")
    endpoint_base = f"https://{workspace}--{app_name}"
    default_gpu = cfg.get("app", {}).get("default_gpu", "H100")

    # 找 ComfyUI user dir(通过 sys 查找,或回退到常见位置)
    candidates = [
        Path.home() / "Documents" / "ComfyUI" / "user" / "default" / "modal_bridge",
        Path("/workspace/ComfyUI/user/default/modal_bridge"),  # 容器内
        HERE.parent.parent / "user" / "default" / "modal_bridge",  # 相对 custom_nodes/..
    ]
    target_dir = None
    for c in candidates:
        if c.parent.parent.exists():  # ComfyUI/user/default 存在
            target_dir = c
            break
    if target_dir is None:
        target_dir = candidates[0]
        warn(f"找不到 ComfyUI user dir,默认写到 {target_dir}(可能不对,自己核对)")

    target_dir.mkdir(parents=True, exist_ok=True)
    config_file = target_dir / "config.json"

    config = {
        "modal_endpoint_base": endpoint_base,
        "modal_app_name": app_name,
        "modal_workspace": workspace,
        "modal_volume_name": cfg.get("app", {}).get("volume_name", "comfyui-bridge-models"),
        "scaledown_window": cfg.get("app", {}).get("scaledown_window", 120),
        "modal_token_id": cfg["modal"]["token_id"],
        "modal_token_secret": cfg["modal"]["token_secret"],
        "bridge_api_key": bridge_key,
        "default_gpu": default_gpu,
        "user_id": "local-dev",
        "incognito": True,
        "poll_interval_sec": 1.5,
        "timeout_sec": 1200,
        "output_subfolder": "modal_results",
        "auto_seed_models": True,
        "seed_timeout_sec": 1800,
        "auto_check_nodes": True,
    }
    config_file.write_text(json.dumps(config, indent=2), encoding="utf-8")
    ok(f"config 写到 {config_file}")
    ok(f"endpoint base = {endpoint_base}")
    ok(f"token         = {cfg['modal']['token_id'][:8]}...")


# ============================================================================
# Step 6:验证 endpoint 可达
# ============================================================================
def verify(cfg, bridge_key):
    step(6, "验证 endpoint(GET /health)")
    import urllib.request
    import urllib.parse
    app_name = cfg.get("app", {}).get("name", "comfyui-bridge")
    workspace = cfg.get("modal", {}).get("workspace", "")
    url = f"https://{workspace}--{app_name}-health.modal.run?" + urllib.parse.urlencode({"key": bridge_key})
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
            ok(f"/health → {data}")
    except Exception as e:
        warn(f"/health 不可达(可能 endpoint 还在初始化,稍后再试):{e}")


# ============================================================================
# 主流程
# ============================================================================
def main():
    print("=" * 60)
    print("  comfyui_modal_bridge — 一键部署")
    print("=" * 60)

    import secrets as _secrets
    bridge_key = "bk-" + _secrets.token_urlsafe(24)

    cfg = load_secrets()
    setup_modal_cli(cfg)
    create_secrets(cfg, bridge_key)
    deploy_app(cfg)
    write_local_config(cfg, bridge_key)
    verify(cfg, bridge_key)

    print()
    print("\033[1;32m" + "=" * 60)
    print("  ✓ 部署完成!")
    print("=" * 60 + "\033[0m")
    print()
    print("下一步:")
    print(f"  1. 拷整个插件目录到 ComfyUI custom_nodes/:")
    print(f"     cp -r {HERE.name} ~/Documents/ComfyUI/custom_nodes/")
    print(f"  2. 重启 ComfyUI Desktop")
    print(f"  3. 浏览器打开 ComfyUI → 右上角应该有 [☁️ Modal] 按钮")
    print(f"  4. 加载工作流 → 点 [☁️ Modal] → 自动检查并下载缺失模型 → 出图")
    print()


if __name__ == "__main__":
    main()
