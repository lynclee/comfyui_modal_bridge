"""
node_sync.py — custom_node 同步:把本地工作流用到、但 Modal 镜像没装的 custom_node
找出来,并支持「一键加进镜像 + 重部署」。

核心思路(全部本地、瞬时,不依赖外部 registry):
  本地 ComfyUI 已经装了这些 custom_node(否则你的工作流根本打不开),所以本地能精确知道
  每个 class_type 来自哪个 custom_nodes/<folder>(读节点类的源码文件路径),再去这个文件夹
  读它的 git remote / commit。和 Modal 镜像 baked 的清单一比,就知道缺哪些、怎么补。

写入 modal_app/_custom_nodes_data.py 即更新「Modal 要装的 custom_node 清单」,
重新 modal deploy 就会把新节点 clone 进镜像。
"""
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
MODAL_APP_DIR = _HERE / "modal_app"
DATA_FILE = MODAL_APP_DIR / "_custom_nodes_data.py"

# ComfyUI 自带节点所在(相对 ComfyUI 根)的目录前缀 — 这些永远不算 custom_node
_BUILTIN_DIRS = {"comfy_extras", "comfy", "comfy_api_nodes", "app"}

_DATA_HEADER = '''"""
_custom_nodes_data.py — Modal 镜像里要装的 custom_nodes 清单(纯数据)

⚠ 这个文件由 ComfyUI 里的「一键添加缺失节点」按钮自动维护(routes.py / node_sync.py)。
   手动加也行,格式保持每条一个 dict:{"name","url","commit"}。
   - name:  custom_nodes 下的文件夹名(必须和 git clone 出来的目录名一致)
   - url:   git 仓库地址
   - commit: pin 的 commit sha(防止 master HEAD 漂移;留空字符串则跟随默认分支 HEAD)

modal_image.py 在 build 时读这个列表生成 git clone 命令。
改这里 → 重新 `modal deploy` → 只重 build 节点那两层(clone + 装依赖),不影响其它层。
"""
'''


# ============================================================================
# 读 / 写 baked 清单
# ============================================================================
def read_baked_nodes() -> list[dict]:
    """读 _custom_nodes_data.py 里的 CUSTOM_NODES(exec 自家可信文件)。"""
    if not DATA_FILE.exists():
        return []
    ns: dict = {}
    try:
        exec(compile(DATA_FILE.read_text(encoding="utf-8"), str(DATA_FILE), "exec"), ns)
    except Exception as e:
        print(f"[modal_bridge] read baked nodes failed: {e}")
        return []
    return list(ns.get("CUSTOM_NODES", []))


def baked_node_names() -> set[str]:
    return {n.get("name", "") for n in read_baked_nodes() if n.get("name")}


def write_baked_nodes(nodes: list[dict]) -> None:
    """用固定模板重写 _custom_nodes_data.py(保证格式稳定,可被反复机改)。"""
    lines = [_DATA_HEADER, "CUSTOM_NODES = ["]
    for n in nodes:
        lines.append("    {")
        lines.append(f'        "name": {json.dumps(n.get("name", ""), ensure_ascii=False)},')
        lines.append(f'        "url": {json.dumps(n.get("url", ""), ensure_ascii=False)},')
        lines.append(f'        "commit": {json.dumps(n.get("commit", ""), ensure_ascii=False)},')
        lines.append("    },")
    lines.append("]")
    DATA_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_baked_nodes(new_entries: list[dict]) -> dict:
    """把 new_entries 合并进 baked 清单(按 name 去重),返回 {added, skipped}。"""
    current = read_baked_nodes()
    have = {n.get("name") for n in current}
    added, skipped = [], []
    for e in new_entries:
        name = e.get("name")
        if not name or not e.get("url"):
            skipped.append({"name": name, "reason": "missing name/url"})
            continue
        if name in have:
            skipped.append({"name": name, "reason": "already baked"})
            continue
        current.append({"name": name, "url": e["url"], "commit": e.get("commit", "")})
        have.add(name)
        added.append(name)
    if added:
        write_baked_nodes(current)
    return {"added": added, "skipped": skipped}


# ============================================================================
# class_type → 本地 custom_node 文件夹 解析
# ============================================================================
def _comfyui_root() -> Path:
    # custom_nodes/comfyui_modal_bridge/ → 上两级是 ComfyUI 根
    return _HERE.parents[1]


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd),
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _normalize_git_url(url: str) -> str:
    """ssh 形式转 https,方便 Modal 容器里无凭据 clone。"""
    url = (url or "").strip()
    if url.startswith("git@github.com:"):
        return "https://github.com/" + url[len("git@github.com:"):]
    return url


def folder_git_info(folder: str) -> dict:
    """读本地 custom_nodes/<folder> 的 git url + commit。"""
    path = _comfyui_root() / "custom_nodes" / folder
    if not path.exists():
        return {"folder": folder, "has_git": False, "url": None, "commit": None}
    url = _git(["config", "--get", "remote.origin.url"], path)
    commit = _git(["rev-parse", "HEAD"], path)
    return {
        "folder": folder,
        "has_git": bool(url and commit),
        "url": _normalize_git_url(url) if url else None,
        "commit": commit,
    }


def _class_source_folder(class_type: str) -> str | None | bool:
    """
    返回:
      None       — class_type 本地不存在(NODE_CLASS_MAPPINGS 里没有)
      True       — 是 ComfyUI 自带节点(不在 custom_nodes 下)
      "<folder>" — 来自 custom_nodes/<folder>
    """
    try:
        import nodes  # ComfyUI 全局
    except Exception:
        return None
    cls = nodes.NODE_CLASS_MAPPINGS.get(class_type)
    if cls is None:
        return None
    try:
        src = Path(inspect.getfile(cls)).resolve()
    except Exception:
        return True  # 拿不到源码路径,保守当作自带
    parts = src.parts
    if "custom_nodes" in parts:
        idx = parts.index("custom_nodes")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return True  # 不在 custom_nodes 下 → 自带


def analyze_workflow(prompt: dict) -> dict:
    """
    扫工作流,按 custom_node 文件夹归类。
    返回:
      {
        "builtin": [class_type...],        # 自带,Modal 一定有
        "by_folder": {folder: [class_type...]},  # 来自某个 custom_node
        "unresolved": [class_type...],     # 本地都没有(打字错/没装),无法自动补
      }
    """
    builtin, by_folder, unresolved = [], {}, []
    seen_cls = set()
    for node in (prompt or {}).values():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not ct or ct in seen_cls:
            continue
        seen_cls.add(ct)
        res = _class_source_folder(ct)
        if res is None:
            unresolved.append(ct)
        elif res is True:
            builtin.append(ct)
        else:
            by_folder.setdefault(res, []).append(ct)
    return {"builtin": builtin, "by_folder": by_folder, "unresolved": unresolved}


def find_missing_nodes(prompt: dict, baked_names: set[str] | None = None) -> dict:
    """
    对比工作流用到的 custom_node 文件夹 vs Modal 已 baked 的清单,找出缺的。
    baked_names 不传则用本地 _custom_nodes_data.py(传 Modal /list-nodes 结果更权威)。
    返回:
      {
        "missing": [{folder, class_types, has_git, url, commit}],  # 缺、且能自动补(has_git=True)
        "missing_no_git": [{folder, class_types, ...}],            # 缺、但本地没 git 信息,补不了
        "unresolved": [class_type...],
        "ok_builtin": int, "ok_baked": int,
      }
    """
    if baked_names is None:
        baked_names = baked_node_names()
    info = analyze_workflow(prompt)
    missing, missing_no_git = [], []
    ok_baked = 0
    for folder, class_types in info["by_folder"].items():
        if folder in baked_names:
            ok_baked += 1
            continue
        git = folder_git_info(folder)
        entry = {"folder": folder, "class_types": sorted(class_types), **git}
        if git["has_git"]:
            missing.append(entry)
        else:
            missing_no_git.append(entry)
    return {
        "missing": missing,
        "missing_no_git": missing_no_git,
        "unresolved": info["unresolved"],
        "ok_builtin": len(info["builtin"]),
        "ok_baked": ok_baked,
    }


# ============================================================================
# 部署 / 重部署 — 统一走 ComfyUI 内嵌 Python(sys.executable)
#
# 关键:一切 modal 调用都用 sys.executable -m modal,保证和 ComfyUI 同一个解释器。
#   - 不依赖系统 PATH 上的 modal(GUI 启动的 app PATH 很精简,常找不到)
#   - GUI「部署」按钮先 pip install modal 到这个解释器,后续 deploy / add_nodes 都用它
#   - 不写 ~/.modal.toml,鉴权全靠 env 注入 MODAL_TOKEN_ID/SECRET(更干净、可移植)
# ============================================================================
def python_executable() -> str:
    return sys.executable


def modal_available() -> bool:
    """ComfyUI 内嵌 Python 里能不能 import modal。"""
    try:
        r = subprocess.run(
            [sys.executable, "-c", "import modal; print(modal.__version__)"],
            capture_output=True, text=True, timeout=20,
        )
        return r.returncode == 0
    except Exception:
        return False


def pip_install_modal_cmd() -> list[str]:
    return [sys.executable, "-m", "pip", "install", "-U", "modal"]


def gen_bridge_key() -> str:
    """私有 endpoint 自建鉴权 key(部署时生成,存 Modal Secret + 本地 config)。"""
    import secrets
    return "bk-" + secrets.token_urlsafe(24)


def deploy_command() -> list[str]:
    return [sys.executable, "-m", "modal", "deploy", "modal_app.py"]


def secret_create_cmd(cfg: dict, hf_token: str = "", civitai_token: str = "",
                      bridge_key: str = "") -> list[str]:
    """建/更新 Modal Secret:BRIDGE_API_KEY(私有鉴权)+ HF / Civitai token(下私有模型)。"""
    app_name = cfg.get("modal_app_name", "comfyui-bridge")
    secret_name = f"{app_name}-secrets"
    pairs = []
    if bridge_key:
        pairs.append(f"BRIDGE_API_KEY={bridge_key}")
    if hf_token:
        pairs += [f"HF_TOKEN={hf_token}", f"HUGGING_FACE_HUB_TOKEN={hf_token}"]
    if civitai_token:
        pairs.append(f"CIVITAI_TOKEN={civitai_token}")
    if not pairs:
        pairs.append("EMPTY=1")  # 空 secret 占位,避免 worker from_name 报错
    return [sys.executable, "-m", "modal", "secret", "create", "--force", secret_name, *pairs]


def deploy_env(cfg: dict) -> dict:
    """从 config 拼出 modal deploy / secret 需要的环境变量(MODAL_BRIDGE_* + 鉴权)。"""
    app_name = cfg.get("modal_app_name", "comfyui-bridge")
    env = os.environ.copy()
    env["MODAL_BRIDGE_APP_NAME"] = app_name
    env["MODAL_BRIDGE_VOLUME"] = cfg.get("modal_volume_name", "comfyui-bridge-models")
    env["MODAL_BRIDGE_SECRET"] = f"{app_name}-secrets"
    env["MODAL_BRIDGE_DEFAULT_GPU"] = cfg.get("default_gpu", "H100")
    env["MODAL_BRIDGE_SCALEDOWN"] = str(cfg.get("scaledown_window", 120))
    if cfg.get("modal_token_id"):
        env["MODAL_TOKEN_ID"] = cfg["modal_token_id"]
    if cfg.get("modal_token_secret"):
        env["MODAL_TOKEN_SECRET"] = cfg["modal_token_secret"]
    return env
