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
import ast
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
MODAL_APP_DIR = _HERE / "modal_app"
DATA_FILE = MODAL_APP_DIR / "_custom_nodes_data.py"
PYPROJECT = _HERE / "pyproject.toml"


def plugin_version() -> str:
    """读 pyproject.toml 的 version(版本契约的真源)。读不到返回 '0.0.0'。"""
    try:
        m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']',
                      PYPROJECT.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.0.0"

# ============================================================================
# ComfyUI 版本跟随:云端镜像 clone 的 ComfyUI tag 跟本机版本走,
# 让"本地能跑的节点云端也能跑"。本机版本无对应 git tag 时取最接近的 tag(只警告,不中止)。
# ============================================================================
DEFAULT_COMFYUI_TAG = "v0.22.0"          # 兜底:本机版本测不到 / tag 拉不到时用
COMFYUI_REPO = "https://github.com/comfyanonymous/ComfyUI"


def detect_local_comfyui_version() -> str:
    """本机 ComfyUI 版本(插件跑在 ComfyUI 进程里,直接 import 官方版本模块)。读不到返回 ''。"""
    try:
        import comfyui_version  # type: ignore
        return (getattr(comfyui_version, "__version__", "") or "").strip()
    except Exception:
        return ""


def _parse_ver(s: str):
    """'v0.22.3' / '0.22.3' → (0,22,3);解析不了返回 None。"""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", (s or "").strip().lstrip("v"))
    return tuple(int(x) for x in m.groups()) if m else None


def list_comfyui_tags(repo: str = COMFYUI_REPO, timeout: int = 20) -> list[str]:
    """git ls-remote 拿 ComfyUI 仓库的 vX.Y.Z tag 列表(去掉 ^{} 解引用行)。失败返回 []。"""
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", repo],
                             capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return []
        tags = []
        for line in out.stdout.splitlines():
            if line.rstrip().endswith("^{}"):
                continue
            m = re.search(r"refs/tags/(v?\d+\.\d+\.\d+)$", line)
            if m:
                tags.append(m.group(1))
        return sorted(set(tags))
    except Exception:
        return []


def resolve_comfyui_tag(version: str, tags: list[str]) -> tuple[str, str]:
    """纯函数:本机版本 + 可用 tag 列表 → (选用的 tag, 警告说明)。
    精确命中 → ('vX.Y.Z', '')。无精确 → 取 semver 距离最近的(平手取更老的 ≤ 本机,避免云端比本地新),
    返回说明。版本测不到 / tag 列表空 → 默认 tag + 说明。"""
    lv = _parse_ver(version)
    if not lv:
        return DEFAULT_COMFYUI_TAG, f"本机 ComfyUI 版本未知 → 云端用默认 {DEFAULT_COMFYUI_TAG}"
    cand = [(pv, t) for t in tags if (pv := _parse_ver(t))]
    if not cand:
        return DEFAULT_COMFYUI_TAG, f"拉不到 ComfyUI tag 列表 → 云端用默认 {DEFAULT_COMFYUI_TAG}"
    exact = [t for pv, t in cand if pv == lv]
    if exact:
        return next((t for t in exact if t.startswith("v")), exact[0]), ""

    def dist(pv):
        a, b = lv + (0,) * (3 - len(lv)), pv + (0,) * (3 - len(pv))
        return abs((a[0] - b[0]) * 10**6 + (a[1] - b[1]) * 10**3 + (a[2] - b[2]))

    cand.sort(key=lambda x: (dist(x[0]), 0 if x[0] <= lv else 1))
    best = cand[0][1]
    return best, f"本机 ComfyUI v{'.'.join(map(str, lv))} 无对应 tag → 云端用最接近的 {best}"


# ============================================================================
# 云端模型目录映射:云端 extra_model_paths.yaml 跟随本机注册的模型目录类型,
# 这样自定义类别(geometry_estimation / optical_flow / liveportrait / ...)里的模型
# 也能被云端 ComfyUI 看到(否则 LoadMoGeModel 这类节点下拉为空 → 'not in []')。
# 部署时生成,是本地状态(.gitignore + 缺则自愈,和 _custom_nodes_data.py 一样)。
# ============================================================================
EXTRA_MODEL_PATHS_YAML = MODAL_APP_DIR / "extra_model_paths.yaml"

# 标准模型类型(folder_paths 取不到时的兜底,也始终并入)。
STANDARD_MODEL_TYPES = [
    "checkpoints", "diffusion_models", "unet", "vae", "clip", "text_encoders",
    "clip_vision", "style_models", "loras", "controlnet", "upscale_models",
    "embeddings", "hypernetworks", "photomaker", "gligen", "diffusers",
    "vae_approx", "pulid", "inpaint", "insightface", "onnx", "sams", "ultralytics",
]
# 永不映射到 Volume(映射过去云端启动 os.listdir 会崩 / 无意义)
_MODEL_TYPE_DENY = {"custom_nodes", "configs"}


def local_model_folder_types() -> list[str]:
    """本机 ComfyUI 注册的所有模型目录类型(folder_paths)∪ 标准基线,去黑名单、排序去重。
    取不到 folder_paths 时退回标准基线。"""
    types = set(STANDARD_MODEL_TYPES)
    try:
        import folder_paths  # type: ignore
        types |= set(folder_paths.folder_names_and_paths.keys())
    except Exception:
        pass
    return sorted(t for t in types if t and t not in _MODEL_TYPE_DENY)


def render_extra_model_paths_yaml(types: list[str]) -> str:
    """生成云端 extra_model_paths.yaml 内容(纯函数)。每个 type → models/<type>/,
    与 bridge 上传路径(modal_volume 用 models/<type>/)严格一致。"""
    lines = [
        "# ComfyUI 模型搜索路径 — Modal worker 用(部署时由 node_sync 按本机模型目录类型生成)",
        "# base_path 指向挂载的 Volume:/comfy-volume;每个 type → /comfy-volume/models/<type>/",
        "",
        "comfyui-bridge:",
        "    base_path: /comfy-volume/",
        "    is_default: true",
        "",
    ]
    lines += [f"    {t}: models/{t}/" for t in types]
    lines.append("")
    lines.append("    # ⚠ 不映射 custom_nodes 到 Volume —— Volume 无此目录,云端启动 os.listdir 会崩。")
    return "\n".join(lines) + "\n"


def write_extra_model_paths(types: list[str] | None = None) -> list[str]:
    """部署前调:把生成的 yaml 写到 baked 文件。返回实际写入的 type 列表。"""
    types = types if types is not None else local_model_folder_types()
    EXTRA_MODEL_PATHS_YAML.write_text(render_extra_model_paths_yaml(types), encoding="utf-8")
    return types


def ensure_extra_model_paths_file() -> None:
    """缺则写标准基线(供 modal_image 打包兜底,和 ensure_baked_file 同理)。"""
    if not EXTRA_MODEL_PATHS_YAML.exists():
        EXTRA_MODEL_PATHS_YAML.write_text(
            render_extra_model_paths_yaml(STANDARD_MODEL_TYPES), encoding="utf-8")


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
    """读 _custom_nodes_data.py 里的 CUSTOM_NODES。
    用 ast 解析 + literal_eval(不 exec):该文件是机器维护的纯数据(只有一个 CUSTOM_NODES
    列表字面量),literal_eval 足够且更安全 —— 也满足 Registry 安全检查(exec 将被禁)。"""
    if not DATA_FILE.exists():
        return []
    try:
        tree = ast.parse(DATA_FILE.read_text(encoding="utf-8"), str(DATA_FILE))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CUSTOM_NODES" for t in node.targets
            ):
                return list(ast.literal_eval(node.value))
    except Exception as e:
        print(f"[modal_bridge] read baked nodes failed: {e}")
    return []


def baked_node_names() -> set[str]:
    return {n.get("name", "") for n in read_baked_nodes() if n.get("name")}


def ensure_baked_file() -> None:
    """确保 _custom_nodes_data.py 存在(它是 .gitignore 的本地状态,可能缺失)。
    缺则写空清单 —— 部署前调用,保证 modal_image 的 import / 打包不因文件缺失失败。"""
    if not DATA_FILE.exists():
        write_baked_nodes([])


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


_GIT_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "gitee.com")


def _sanitize_repo_url(url: str) -> str:
    """截到 owner/repo 这一层,去掉 /tree/... /blob/... 等子路径和 #frag / ?query。
    结尾的 .git / 斜杠交给 git 自己处理。"""
    url = (url or "").strip()
    m = re.match(r"(https?://[^/]+/[^/#?]+/[^/#?]+)", url)
    return m.group(1) if m else url


def _pyproject_repo_url(path: Path) -> str | None:
    """没有 .git 的节点(ComfyUI-Manager 的 CNR / Registry / 压缩包安装)兜底:
    从 pyproject.toml 读仓库地址。取指向已知 git 托管站的第一个 URL
    (优先 Repository / Source / Code / Git / Homepage 这些 key)。
    这样无论节点是 git clone 还是 CNR 装的,都能解析出可克隆地址,换台机器也一致。"""
    pp = path / "pyproject.toml"
    try:
        text = pp.read_text(encoding="utf-8")
    except Exception:
        return None
    best = None  # (priority, url):priority 越小越优先
    for m in re.finditer(r'(?im)^\s*([A-Za-z][\w .-]*?)\s*=\s*["\']([^"\']+)["\']', text):
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if any(h in val for h in _GIT_HOSTS):
            pri = 0 if key in ("repository", "source", "code", "git", "homepage") else 1
            if best is None or pri < best[0]:
                best = (pri, val)
    return _sanitize_repo_url(best[1]) if best else None


def commit_on_remote(path: Path, commit: str) -> bool:
    """本地 HEAD 是否已推到远端 —— 云端只会 `git clone <url> && git checkout <commit>`,
    没推的 commit 在云端根本不存在,checkout 失败会让**整个镜像 build 崩掉**(连带其它节点)。
    判定用 `git branch -r --contains`:任一远端分支包含该 commit 即算可达。
    ⚠ 读的是本地缓存的 remote-tracking 引用,不联网 —— 刚 push 但没 fetch 的情况会误判为
    未推送(方向安全:宁可多问一句,不可让部署炸)。取不到结论时返回 True(不误伤)。"""
    out = _git(["branch", "-r", "--contains", commit], path)
    if out is None:
        return True  # 没有远端跟踪信息 / git 报错 → 不做判断
    return bool(out.strip())


def worktree_dirty(path: Path) -> bool:
    """工作树有未提交改动(含未跟踪文件)。
    这是自写 / 调试节点**最常见**的状态:改一行试一下,谁会先 commit 再 push?
    而云端只按 commit clone —— HEAD 没变但文件变了,镜像里跑的还是旧代码,
    且改前改后结果一模一样、毫无线索。所以 dirty 必须当成「本地版本 ≠ 云端版本」。"""
    out = _git(["status", "--porcelain"], path)
    return bool(out and out.strip())


def folder_git_info(folder: str) -> dict:
    """读本地 custom_nodes/<folder> 的可克隆地址 + commit。
    主路径读 .git(remote.origin.url + HEAD commit);CNR / Registry / 压缩包装的节点
    没有 .git,则兜底读 pyproject.toml 的仓库地址(commit 留空 = 跟随默认分支 HEAD)。
    has_git 在此表示「解析得到可克隆 url」(未必真有本地 .git)。
    额外回 pushed:False 表示本地 commit 没推到远端(云端 checkout 必失败)。"""
    path = _comfyui_root() / "custom_nodes" / folder
    if not path.is_dir():
        # 注意用 is_dir:单文件节点(custom_nodes/foo.py)不是可 clone 的仓库,
        # 走 exists() 会让后面的 git 调用在非目录 cwd 上报错,白跑一圈。
        return {"folder": folder, "has_git": False, "url": None, "commit": None, "pushed": True}
    url = _git(["config", "--get", "remote.origin.url"], path)
    commit = _git(["rev-parse", "HEAD"], path)
    if url and commit:
        return {"folder": folder, "has_git": True,
                "url": _normalize_git_url(url), "commit": commit,
                "pushed": commit_on_remote(path, commit),
                "dirty": worktree_dirty(path)}
    repo = _pyproject_repo_url(path)
    if repo:
        return {"folder": folder, "has_git": True,
                "url": _normalize_git_url(repo), "commit": "", "pushed": True,
                "dirty": worktree_dirty(path)}
    return {"folder": folder, "has_git": False, "url": None, "commit": None,
            "pushed": True, "dirty": False}


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


def folder_exists_locally(folder: str) -> bool:
    return (_comfyui_root() / "custom_nodes" / folder).is_dir()


def _cloud_stale_reason(git: dict, local_commit: str, baked_commit: str) -> str | None:
    """已烤进镜像的节点:云端那份跟本地比是不是旧的?返回原因,None = 一致无需动作。
      "dirty"    工作树有未提交改动 —— 云端按 commit clone,拿不到这些改动
      "unpushed" commit 变了但没推 —— 云端 clone 不到这个 commit
      "no_git"   拿不到 git 依据(.git 丢了/不是仓库)—— 无从判断,只能按「可能不一致」处理
      "commit"   干净且已推、commit 与镜像不同 —— 走 git 路线更新即可
    纯函数,单测覆盖四条分支。"""
    if git.get("dirty"):
        return "dirty"
    if not git.get("has_git") or not local_commit:
        # 没有 git 可比:镜像里那份是历史某次同步进去的,现在无从校验 → 保守当作可能不一致。
        # (老实说这条大多命中"用户把 .git 删了"的自写节点,正是本地打包通道的目标场景。)
        return "no_git"
    if local_commit == baked_commit:
        return None
    return "commit" if git.get("pushed", True) else "unpushed"


def plan_node_sync(prompt: dict, baked: list[dict] | None = None,
                   allow_prune: bool = False) -> dict:
    """
    节点同步规划:让 Modal 镜像装上工作流需要的 custom_node。
      - add:   工作流用到、本地有 git(且已推送)、baked 还没有的 → 加进镜像
      - update: baked 有、但本地 commit 跟 baked 不一致的 → 按本地 commit 更新
      - local_pack: 无 git remote(自写节点)或本地 commit 未推送 → 走 Volume 打包通道,
                    **不需要重新部署**(worker 启动时解压,见 local_nodes.py)
      - prune: baked 有、但本地 custom_nodes 没有的 → 候选移除

    ⚠ 多机场景:不同电脑各装一部分节点,"本地没有"≠"全局不需要"。所以默认
    allow_prune=False —— 自动同步(出图时)只增不删,镜像 = 各机贡献的并集,永不互删。
    prune 只在「管理云端节点」面板里手动勾选执行(allow_prune=True 时才会从 new_baked 移除)。
    任一非空即 needs_deploy=True;new_baked 是写回 _custom_nodes_data.py 的完整新清单。

    baked 不传则读本地 _custom_nodes_data.py。
    返回:
      {
        "add": [{folder, class_types, url, commit}],
        "update": [{folder, url, old_commit, commit}],
        "prune": [{name}],
        "missing_no_git": [{folder, class_types}],  # 工作流要、baked 没、本地也没 git → 补不了
        "unresolved": [class_type...],              # 本地都没装(打字错/没装)
        "ok_builtin": int, "ok_baked": int,
        "new_baked": [{name, url, commit}],
        "needs_deploy": bool,
      }
    """
    if baked is None:
        baked = read_baked_nodes()
    baked_by_name = {n.get("name"): dict(n) for n in baked if n.get("name")}
    info = analyze_workflow(prompt)

    add, update, local_pack, missing_no_git = [], [], [], []
    ok_baked = 0
    # 1) 工作流用到的 custom_node:加 / 更新 / 走本地打包通道
    for folder, class_types in info["by_folder"].items():
        git = folder_git_info(folder)
        if folder in baked_by_name:
            ok_baked += 1
            local_commit = (git.get("commit") or "").strip()
            baked_commit = (baked_by_name[folder].get("commit") or "").strip()
            reason = _cloud_stale_reason(git, local_commit, baked_commit)
            if reason is None:
                pass                                   # 云端那份就是本地这份,什么都不用做
            elif reason == "commit":
                # 干净、已推、commit 变了 → 走 git 路线更新镜像
                update.append({"folder": folder, "url": git["url"],
                               "old_commit": baked_commit, "commit": local_commit})
                baked_by_name[folder] = {"name": folder, "url": git["url"], "commit": local_commit}
            elif folder_exists_locally(folder):
                # dirty / 未推送 / 没有 git 可依据 —— 云端 clone 不到这一版,
                # 走本地打包通道盖掉镜像里那份。
                # ⚠ 绝不能默默跳过:那样云端**静默跑旧代码**,比部署报错难查得多
                #   (用户改完节点点运行,结果和改之前一模一样,毫无线索)。
                local_pack.append({"folder": folder, "class_types": sorted(class_types),
                                   "reason": reason})
            continue
        if git["has_git"] and git.get("pushed", True) and not git.get("dirty"):
            add.append({"folder": folder, "class_types": sorted(class_types),
                        "url": git["url"], "commit": git.get("commit") or ""})
            baked_by_name[folder] = {"name": folder, "url": git["url"],
                                     "commit": git.get("commit") or ""}
        elif folder_exists_locally(folder):
            # 自写节点(无 git remote)、或有 remote 但本轮改动没推 —— 走本地打包通道:
            # 打包传 Volume,worker 启动时解压。不需要重 build 镜像(见 local_nodes.py)。
            local_pack.append({
                "folder": folder, "class_types": sorted(class_types),
                "reason": "unpushed" if git["has_git"] else "no_git",
            })
        else:
            # 目录都不在(单文件节点 custom_nodes/foo.py,或路径解析异常)→ 真的补不了
            missing_no_git.append({"folder": folder, "class_types": sorted(class_types),
                                   "reason": "not_a_directory"})

    # 2) baked 里、本地没有的 → prune 候选。默认不真删(多机并集,见 docstring),
    #    只在 allow_prune 时才从 new_baked 移除(手动清理面板用)。
    prune = []
    for name in list(baked_by_name.keys()):
        if not folder_exists_locally(name):
            prune.append({"name": name})
            if allow_prune:
                del baked_by_name[name]

    # new_baked 保持原顺序(已存在的)+ 新增的追加在后,排除被 prune 的
    new_baked = []
    seen = set()
    for n in baked:
        nm = n.get("name")
        if nm in baked_by_name and nm not in seen:
            new_baked.append(baked_by_name[nm])
            seen.add(nm)
    for nm, entry in baked_by_name.items():
        if nm not in seen:
            new_baked.append(entry)
            seen.add(nm)

    # 自动同步只看 add/update(prune 默认不执行 → 不该触发部署);allow_prune 时 prune 也算。
    # ⚠ local_pack 刻意**不**触发部署:本地节点走 Volume 运行时挂载,重传 zip 即生效,
    #   这正是这条通道相对 git 路线的核心优势(省掉 3-5 分钟重 build)。
    needs_deploy = bool(add or update or (prune and allow_prune))
    return {
        "add": add,
        "update": update,
        "prune": prune,
        "local_pack": local_pack,
        "needs_local_upload": bool(local_pack),
        "missing_no_git": missing_no_git,
        "unresolved": info["unresolved"],
        "ok_builtin": len(info["builtin"]),
        "ok_baked": ok_baked,
        "new_baked": new_baked,
        "needs_deploy": needs_deploy,
    }


def apply_node_plan(plan: dict) -> None:
    """把 plan 的 new_baked 写回 _custom_nodes_data.py(随后 modal deploy 生效)。"""
    write_baked_nodes(plan.get("new_baked", []))


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


def node_compat_check_command() -> list[str]:
    """部署后跑:隔离 app 在同一镜像里 boot 一次 ComfyUI,报告每个自定义节点导入成功/失败。"""
    return [sys.executable, "-m", "modal", "run", "node_compat_check.py"]


def secret_create_cmd(cfg: dict, hf_token: str = "", civitai_token: str = "",
                      bridge_key: str = "", comfy_api_key: str = "",
                      aigc_base_url: str = "", aigc_bypass_secret: str = "") -> list[str]:
    """建/更新 Modal Secret:BRIDGE_API_KEY(私有鉴权)+ HF / Civitai token(下私有模型)
    + COMFY_API_KEY_COMFY_ORG(可选,工作流里的 ComfyUI API 节点鉴权,worker 注入 /prompt extra_data)
    + AIGC_STUDIO_BASE_URL / AIGC_STUDIO_BYPASS_SECRET(可选,aigc-r2 交付回调地址 / Vercel
    Protection 旁路)。⚠ 绝不含 R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY —— R2 长期密钥只在
    Vercel,Modal 只拿短期预签名 PUT 地址(见 PLUGIN_MODAL_BRIDGE_CHANGE_PLAN.md 铁律)。"""
    app_name = cfg.get("modal_app_name", "comfyui-bridge")
    secret_name = f"{app_name}-secrets"
    pairs = []
    if bridge_key:
        pairs.append(f"BRIDGE_API_KEY={bridge_key}")
    if hf_token:
        pairs += [f"HF_TOKEN={hf_token}", f"HUGGING_FACE_HUB_TOKEN={hf_token}"]
    if civitai_token:
        pairs.append(f"CIVITAI_TOKEN={civitai_token}")
    if comfy_api_key:
        pairs.append(f"COMFY_API_KEY_COMFY_ORG={comfy_api_key}")
    if aigc_base_url:
        pairs.append(f"AIGC_STUDIO_BASE_URL={aigc_base_url}")
    if aigc_bypass_secret:
        pairs.append(f"AIGC_STUDIO_BYPASS_SECRET={aigc_bypass_secret}")
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
    env["MODAL_BRIDGE_COMFYUI_TAG"] = cfg.get("comfyui_tag") or DEFAULT_COMFYUI_TAG  # 云端 ComfyUI 版本(跟随本机)
    env["MODAL_BRIDGE_DEFAULT_GPU"] = cfg.get("default_gpu", "H100")
    env["MODAL_BRIDGE_CHEAP_GPU"] = cfg.get("cheap_gpu", "L40S")  # 省钱档 GPU(自动降档目标)
    env["MODAL_BRIDGE_TOP_GPU"] = cfg.get("top_gpu", "B200")      # 顶配档 GPU(>主卡显存时自动升档,防 OOM)
    env["MODAL_BRIDGE_SCALEDOWN"] = str(cfg.get("scaledown_window", 12))
    env["MODAL_BRIDGE_TIMEOUT"] = str(cfg.get("worker_timeout_sec", 1200))  # worker 超时上限(覆盖最慢类别)
    env["MODAL_BRIDGE_SNAPSHOT"] = "1" if cfg.get("enable_snapshot") else "0"  # 内存快照开关(实验)
    # 关掉云端 ComfyUI 的动态 VRAM(改用估算式加载)。开着时权重只常驻极少一部分、其余按需
    # 从 CPU 搬,显存够也照搬 —— 在按秒计费的云上等于持续付 PCIe 搬运的钱。关掉更快但显存
    # 不够会直接 OOM 而非降速兜底,所以默认不关,由用户按工作流自行取舍。
    env["MODAL_BRIDGE_DISABLE_DYNAMIC_VRAM"] = "1" if cfg.get("disable_dynamic_vram") else "0"
    # 用 SageAttention 替换 ComfyUI 默认的 PyTorch SDPA。attention 在长序列视频模型上占大头
    # (H3 单步约七成 FLOPs),量化后理论翻倍;代价是 QK 走 INT8 有数值误差,需自行看片验证。
    # 包在镜像里总是装好,这里只控制启动参数 → 切换不必重编译 kernel。
    env["MODAL_BRIDGE_SAGE_ATTENTION"] = "1" if cfg.get("use_sage_attention") else "0"
    env["MODAL_BRIDGE_VOLUME_THRESHOLD_MB"] = str(cfg.get("volume_threshold_mb", 8))  # 大产物走 Volume 的阈值
    env["MODAL_BRIDGE_VERSION"] = plugin_version()  # 版本契约:烤进 app,health 回传供前端比对
    if cfg.get("modal_token_id"):
        env["MODAL_TOKEN_ID"] = cfg["modal_token_id"]
    if cfg.get("modal_token_secret"):
        env["MODAL_TOKEN_SECRET"] = cfg["modal_token_secret"]
    return env
