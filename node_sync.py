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
import hashlib
import inspect
import json

try:                                   # 插件里是包内相对导入;CLI / 测试里是顶层模块
    from . import health_client
except ImportError:
    import health_client
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

_HERE = Path(__file__).resolve().parent
MODAL_APP_DIR = _HERE / "modal_app"
DATA_FILE = MODAL_APP_DIR / "_custom_nodes_data.py"
# 本地自写节点的依赖清单。与 _custom_nodes_data.py 同性质:部署时生成的本地状态,
# 不入库(.gitignore),缺了 modal_image 自愈成空列表。
LOCAL_REQS_FILE = MODAL_APP_DIR / "_local_nodes_data.py"
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


def resolve_comfyui_tag(version: str, tags: list[str], prev_tag: str = "",
                        pin: str = "") -> tuple[str, str]:
    """纯函数:本机版本 + 可用 tag 列表 (+ 上次部署的 tag) → (选用的 tag, 警告说明)。
    精确命中 → ('vX.Y.Z', '')。无精确 → 取 semver 距离最近的(平手取更老的 ≤ 本机,避免云端比本地新),
    返回说明。

    ⚠ 拉不到 tag 列表(本机没 git / GitHub 一时连不上 / 20s 超时)时,**绝不能退回写死的
    DEFAULT_COMFYUI_TAG**。那是 v0.22.0,不支持 MiniMax H3、新节点全部导入失败 —— 而部署
    rc=0、日志只多一行 ⚠,一次网络抖动就让云端大面积坏掉(2026-09-23 review 抓到)。回落顺序:
      · 本机版本已知 → 直接用 v{本机版本}。它几乎必然是个真 tag;万一不是(开发版),
        镜像 build 时 git clone 会**明确失败**,远好过静默装一个老版本。
      · 本机版本未知 → 沿用上次部署的 tag(它至少是上次跑通过的)。
      · 两者都没有 → 才用默认值,并在说明里写清楚。"""
    # 钉住(config 的 comfyui_tag_pin):云端版本不再跟随本机。用于「本机 Desktop 还没出新版,但云端
    # 要先升」—— 没有它,面板里下一次「推送到云端」会按本机版本把云端退回去。说明里必须写清楚,
    # 否则这种「故意不一致」和「跟随失败」在部署日志里分不出来。
    pinned = (pin or "").strip()
    if pinned:
        return pinned, (f"云端 ComfyUI 钉在 {pinned}(config 的 comfyui_tag_pin),不跟随本机 "
                        f"{version or '未知'};要恢复跟随就清空这个字段")
    lv = _parse_ver(version)
    prev = (prev_tag or "").strip()
    if not lv:
        if prev:
            return prev, f"本机 ComfyUI 版本未知 → 云端沿用上次部署的 {prev}"
        return DEFAULT_COMFYUI_TAG, f"本机 ComfyUI 版本未知、也没有上次部署记录 → 云端用默认 {DEFAULT_COMFYUI_TAG}"
    cand = [(pv, t) for t in tags if (pv := _parse_ver(t))]
    if not cand:
        guess = "v" + ".".join(map(str, lv))
        return guess, (f"拉不到 ComfyUI tag 列表(网络 / git 不可用)→ 按本机版本直接用 {guess};"
                       f"若它不是正式 tag,镜像构建会明确失败")
    exact = [t for pv, t in cand if pv == lv]
    if exact:
        return next((t for t in exact if t.startswith("v")), exact[0]), ""

    def dist(pv):
        a, b = lv + (0,) * (3 - len(lv)), pv + (0,) * (3 - len(pv))
        return abs((a[0] - b[0]) * 10**6 + (a[1] - b[1]) * 10**3 + (a[2] - b[2]))

    cand.sort(key=lambda x: (dist(x[0]), 0 if x[0] <= lv else 1))
    best = cand[0][1]
    return best, f"本机 ComfyUI v{'.'.join(map(str, lv))} 无对应 tag → 云端用最接近的 {best}"


def comfyui_tag_change_note(prev_tag: str | None, new_tag: str | None) -> str:
    """这次部署的 ComfyUI tag 与上次不同 → 返回一句警告;相同 / 首次部署 → 空串。

    为什么需要:部署输出原本只打**当前值**(`本机=0.34.6 → 云端 clone v0.34.6`),
    看不出它**变了**。而云端 ComfyUI tag 是跟随部署者本机的 —— 用户自己升了 ComfyUI
    Desktop,下一次部署就被动把云端也换掉,同 seed 同工作流的产物随之改变,却没有
    任何提示。2026-09-08 我(Claude)正是因此拿 config 里的旧值 v0.34.2 做了错误预测,
    对协作方声明"这次 tag 不变",实际部署成了 v0.34.6。

    「打印当前值」和「打印变化」是两回事:前者在该报警的时刻**看起来完全正常**。
    """
    prev = (prev_tag or "").strip()
    new = (new_tag or "").strip()
    if not prev or not new or prev == new:
        return ""
    return (f"云端 ComfyUI 版本变了:{prev} → {new}(跟随本机升级)。"
            f"同 seed / 同工作流的产物可能与之前不同。")


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
   Comfy Registry(CNR)装的节点另有两个字段 {"cnr_id","version"},url 是
   https://api.comfy.org/nodes/<cnr_id>/versions/<version>,build 时按这个版本下载 Registry 的 zip。

modal_image.py 在 build 时读这个列表生成 git clone / Registry 下载命令。
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


# ============================================================================
# Comfy Registry(CNR)节点
#
# ComfyUI-Manager 从 Registry 装的节点**没有 .git**,只有 .tracking(装了哪些文件)和 pyproject 的
# [project] name / version —— Manager 自己就是这么认 CNR 包的(glob/cnr_utils.read_cnr_info)。
# 以前只能退回 pyproject 里的仓库地址:第一次部署云端克隆的是 GitHub 默认分支 HEAD,不是本机装的版本
# (ComfyUI-GGUF 的 pyproject 自己写着「2.0.0 = GitHub main,1.X.X = Registry」);之后同一个节点
# 又因为没有 commit 被判 no_git,走私有节点通道传 Volume,还触发依赖重建(2026-10-05 深度 review)。
# 现在当公共节点处理,钉到本机的 Registry 版本:镜像构建时按 (id, version) 从 Registry 下载那一版的 zip。
#
# 2026-10-05 联网核实:GET https://api.comfy.org/nodes/<id>/versions/<version> 返回 downloadUrl
# (如 https://cdn.comfy.org/city96/ComfyUI-GGUF/1.1.10/node.zip,文件直接在 zip 根),id 大小写不敏感,
# 版本不存在回 404。下载下来的 1.1.10 与本机 .tracking 列的文件逐个一致。
#
# ⚠ 清单条目的 url 就写成这个版本 API 地址,(id, version) 以 url 为准、cnr_id / version 字段只是冗余:
#   routes 的 /sync_nodes 只保留 name/url/commit,云端 /health 的 manifest 也只回这三个字段 ——
#   把版本放在额外字段里,走一圈就丢了;放在 url 里,任何只认 url 的环节都原样带着它。
# ============================================================================
CNR_API = "https://api.comfy.org"
_CNR_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CNR_VER_RE = re.compile(r"^[0-9][0-9A-Za-z.+_-]{0,63}$")
_CNR_URL_RE = re.compile(r"^https://api\.comfy\.org/nodes/([^/?#\s]+)/versions/([^/?#\s]+)/?$", re.I)


def cnr_url(cnr_id: str, version: str) -> str:
    return f"{CNR_API}/nodes/{cnr_id}/versions/{version}"


def cnr_ref(entry) -> tuple[str, str] | None:
    """清单条目 / 云端 manifest / folder_git_info 的结果 → (cnr_id 小写, version);不是 CNR 返回 None。
    url 优先(见上方说明),url 不是 Registry 地址时才看 cnr_id / version 字段。"""
    if not isinstance(entry, dict):
        return None
    m = _CNR_URL_RE.match((entry.get("url") or "").strip())
    if m:
        cid, ver = m.group(1), m.group(2)
    else:
        cid, ver = str(entry.get("cnr_id") or "").strip(), str(entry.get("version") or "").strip()
    if _CNR_ID_RE.match(cid) and _CNR_VER_RE.match(ver):
        return cid.lower(), ver
    return None


def _read_cnr_info(path: Path) -> tuple[str, str] | None:
    """本机节点目录是不是 Registry 装的:有 .tracking + pyproject 的 [project] name / version。
    判据与 ComfyUI-Manager 的 read_cnr_info 一致(id 取 name 的小写;TOML 解析失败 = 不是)。"""
    if not (path / ".tracking").is_file():
        return None
    try:
        text = (path / "pyproject.toml").read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        import tomllib
    except ImportError:          # Python 3.10 及以下:只认最常见的写法
        sect = re.search(r"(?ms)^\[project\]\s*$(.*?)(?=^\[|\Z)", text)
        body = sect.group(1) if sect else ""
        mn = re.search(r'(?m)^\s*name\s*=\s*["\']([^"\']+)["\']', body)
        mv = re.search(r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']', body)
        name, ver = (mn.group(1) if mn else ""), (mv.group(1) if mv else "")
    else:
        try:
            proj = tomllib.loads(text).get("project") or {}
        except Exception:
            return None
        name, ver = str(proj.get("name") or ""), str(proj.get("version") or "")
    name, ver = name.strip(), ver.strip()
    if _CNR_ID_RE.match(name) and _CNR_VER_RE.match(ver):
        return name.lower(), ver
    return None


def _baked_entry(name: str, src: dict) -> dict:
    """任一来源(云端 manifest / 本机清单 / folder_git_info)→ 规整的清单条目。
    CNR 的统一成 {url: 版本 API 地址, commit: "", cnr_id, version};其它只留 name/url/commit。"""
    ref = cnr_ref(src)
    if ref:
        return {"name": name, "url": cnr_url(*ref), "commit": "", "cnr_id": ref[0], "version": ref[1]}
    return {"name": name, "url": (src.get("url") or ""), "commit": (src.get("commit") or "")}


def _has_url_creds(url: str) -> bool:
    """http(s) 地址里带不带 userinfo(https://tok@host/… / https://u:p@host/…)。"""
    return bool(re.match(r"^https?://[^/@\s]+@[^/\s]", (url or "").strip(), re.I))


def complete_baked_entries(names: list[str], local_by_name: dict,
                           manifest: list[dict] | None) -> list[dict]:
    """云端 /health 只保证给出节点**名字**,url/commit 从哪来:**云端 manifest 优先**,本机清单兜底。

    ⚠ 以前只用本机清单补,本机没有的节点被填成 {url: ""},随后 write_baked_nodes 在出口把空 url
      条目丢掉 —— 下一次部署就把它从镜像里删了。机器 A 加的节点被机器 B 一次同步删掉;
      插件被 Manager 重装、本机清单丢了,一次部署清空云端全部节点。设计本意是「多机取并集、
      永不互删」(见 plan_node_sync),实现却因为云端只报名字把自己的承诺打破了(2026-09-23 review)。
    云端优先的理由:manifest 就是镜像实际装的那份,比本机「上次从这台机器部署的」更权威。
    补不出来源的条目 url 为空(调用方据此中止);url 带凭据被脱敏的规则见 complete_baked_entries_ex。"""
    return complete_baked_entries_ex(names, local_by_name, manifest)[0]


def complete_baked_entries_ex(names: list[str], local_by_name: dict,
                              manifest: list[dict] | None) -> tuple[list[dict], list[str]]:
    """同 complete_baked_entries,另回一组差异说明行(给 reconcile 的 drift)。

    url 被 /health 脱敏(url_redacted)的云端条目:脱敏后的地址克隆不了私有仓库,只能借本机的地址。
    ⚠ 以前直接退回本机清单 / 本机 git 的**整条**(地址 + commit):本机落后时云端被静默换成本机的旧
      commit,既不算 drift、自动部署也不拦;本机地址指向别的仓库时连仓库都换了(2026-10-05 深度 review)。
    现在只有「本机地址与云端是同一个仓库、且本机地址确实带凭据」才借用,而且只借地址、commit 用云端的;
    本机 commit 与云端不同时出一条差异说明。其它情况一律当补不出来源(url 留空,调用方中止)。"""
    by_name = {n["name"]: n for n in (manifest or []) if isinstance(n, dict) and n.get("name")}
    out, drift = [], []
    for name in names:
        c = by_name.get(name)
        if c and c.get("url_redacted"):
            e, line = _complete_redacted(name, c, local_by_name.get(name))
            out.append(e)
            if line:
                drift.append(line)
            continue
        if c and (c.get("url") or "").strip():
            out.append(_baked_entry(name, c))
            continue
        e = local_by_name.get(name) or _local_git_entry(name) or {"url": "", "commit": ""}
        out.append(_baked_entry(name, e))
    return out, drift


def _complete_redacted(name: str, cloud: dict, listed: dict | None) -> tuple[dict, str]:
    """脱敏的云端条目 → (条目, 差异说明行)。见 complete_baked_entries_ex。"""
    cc = (cloud.get("commit") or "").strip()

    def candidates():
        if listed:
            yield "清单里", listed
        g = _local_git_entry(name)     # 跑 git,用到才算
        if g:
            yield "实际装的", g

    for where, src in candidates():
        u = (src.get("url") or "").strip()
        if not (_same_repo(u, cloud.get("url")) and _has_url_creds(u)):
            continue
        lc = (src.get("commit") or "").strip()
        line = ""
        if lc != cc:
            line = (f"{name}: 云端独有的私有节点,按云端 {_show_commit(cc)} 并回;"
                    f"本机{where}是 {_show_commit(lc)}(与云端不同,这次不会改动云端)")
        return {"name": name, "url": u, "commit": cc}, line
    return {"name": name, "url": "", "commit": ""}, ""


def _local_git_entry(name: str) -> dict | None:
    """本机 custom_nodes/<name> 装着的话,用它的 git / Registry 信息补。

    ⚠ 这是云端报不出来源时的兜底:云端 < 0.8.48 没有 manifest(Registry 上的 latest 还是 0.7.9,
      所有从 Registry 升级的用户第一次部署都走这里),或 url 带凭据被 /health 脱敏了
      (url_redacted —— 照抄脱敏后的地址会让私有仓库克隆失败)。"""
    try:
        g = folder_git_info(name)
    except Exception:
        return None
    if g.get("has_git") and (g.get("url") or "").strip():
        return _baked_entry(name, g)
    return None


def fetch_cloud_nodes(cfg: dict, timeout: int = 20) -> tuple[list, list | None]:
    """取云端镜像装的节点:(文件夹名列表, 带 url/commit 的 manifest)。
    名字所有版本都报;manifest 0.8.48 起才有(老云端为 None)。
    拿不到时抛 health_client.HealthUnavailable,kind 说明原因(未部署 / key 不对 / 网络……)。
    同步实现,部署路径(含 CLI)共用;async 路由里请用 asyncio.to_thread 调。"""
    info = health_client.fetch(cfg, timeout)
    names = info.get("custom_nodes")
    if not isinstance(names, list):
        raise health_client.HealthUnavailable(
            "http", f"/health 没有报节点清单: {info.get('custom_nodes_error', '字段缺失')}")
    manifest = info.get("custom_nodes_manifest")
    return names, (manifest if isinstance(manifest, list) else None)


class DeployBlocked(Exception):
    """部署前检查发现「继续部署会删掉云端节点」。调用方**必须中止**,把消息原样给用户。
    用异常而不是返回值:忘了处理的调用方会直接失败,而不是静默继续部署 —— 失败方向是安全的。"""


class Reconciled(NamedTuple):
    added: list          # 并回本机清单的节点名(云端有、本机缺)
    drift: list          # 同名节点与云端不同、且判断不了谁对的说明行 —— 这次部署会把云端换成本机清单这份
    corrected: list = []  # 清单陈旧(本机实际装的 = 云端)、已按实际安装更正的说明行;不会改动云端
    unchecked: str = ""  # 非空 = 没能读到云端清单,上面几项都没查(原因)。别让「没查」长得像「查过、没差异」


def _norm_repo(u: str) -> str:
    """同一仓库的不同写法归一:去掉 URL 里的凭据(https://tok@host/...)、结尾的 / 和 .git、大小写。
    不去凭据的话,一台机器带 token 克隆、另一台不带,就被当成「来源仓库不同」,永远更正不了。"""
    # 按**最后一个** @ 切(同 urlsplit / 云端 _redact_url):密码里有未转义的 @ 时,只切到第一个会留下半截凭据
    u = re.sub(r"^([a-z][a-z0-9+.-]*://)[^/]*@", r"\1", (u or "").strip(), flags=re.I)
    return u.rstrip("/").removesuffix(".git").lower()


def _show_commit(c: str) -> str:
    return c[:12] if c else "未钉 commit(构建时取最新)"


def _show_src(e: dict) -> str:
    """说明行里的版本:Registry 节点显示版本号,git 节点显示 commit。"""
    ref = cnr_ref(e)
    return f"Registry {ref[1]}" if ref else _show_commit((e.get("commit") or "").strip())


def _same_repo(a: str, b: str) -> bool:
    """去掉凭据后比 scheme / host / path。任一边为空 = 来源未知,不算同一仓库。

    ⚠ /health 的脱敏只去掉 userinfo,host / path 都还在,照样能比。以前见 url_redacted 就跳过仓库比较,
      自动更正会拼出「清单里的旧仓库地址 + 云端新仓库的 commit」,commit 不在旧仓库时构建失败
      (2026-09-28 review)。脱敏时解析失败会回空串,落到「未知」,不会被当成相同。"""
    na, nb = _norm_repo(a), _norm_repo(b)
    return bool(na) and na == nb


def _same_source(entry: dict, cloud: dict) -> bool:
    """同一仓库 + 同一 commit;Registry 节点比 (cnr_id, version)。一边 Registry 一边 git 不算相同。"""
    a, b = cnr_ref(entry), cnr_ref(cloud)
    if a or b:
        return a == b
    return _same_repo(entry.get("url"), cloud.get("url")) and \
        (entry.get("commit") or "").strip() == (cloud.get("commit") or "").strip()


def _drift_line(n: dict, c: dict) -> str:
    line = f"{n.get('name')}: 云端 {_show_src(c)} → 本次部署 {_show_src(n)}"
    if bool(cnr_ref(n)) != bool(cnr_ref(c)):
        line += "(安装来源也不同:一边是 Registry 版本、一边是 git 仓库)"
    elif cnr_ref(n):
        pass
    elif not (c.get("url") or "").strip():
        line += "(云端没报来源,确认不了是不是同一仓库)"
    elif not _same_repo(n.get("url"), c.get("url")):
        line += "(来源仓库也不同)"
    return line


def commit_drift(local: list, manifest: list | None) -> list[tuple[dict, dict]]:
    """同名节点里,本机清单与云端镜像的 commit / 来源不同的:[(本机清单条目, 云端条目)]。

    部署以本机清单为准,每一条都会在这次部署里生效。原因可能是有意的(本机升级 / 回滚了节点,
    或上次同步写了清单但部署失败),也可能是本机清单陈旧(另一台机器部署过更新的版本)——
    只看这两边分不出方向,要再对照本机实际装的版本,见 resolve_drift(2026-09-26/27 review)。"""
    if not manifest:
        return []
    cloud = {e.get("name"): e for e in manifest if isinstance(e, dict)}
    return [(n, cloud[n.get("name")]) for n in local
            if n.get("name") in cloud and not _same_source(n, cloud[n.get("name")])]


def resolve_drift(local: list, manifest: list | None, names: list | None = None) -> tuple[list[str], list[str]]:
    """对照本机 custom_nodes 实际装的版本,把 commit_drift 的每一条分成两类:(drift, corrected)。

    · 本机实际装的 == 云端:本机清单只是陈旧(云端已是本机在用的版本)。**就地更正 local 里的条目**,
      这次部署不会改动云端 —— 最常见的情况,零摩擦。
    · 其它(本机装的就是清单里那个、没装、没 git、读不到、云端来源未知):判断不了谁对,原样留作 drift。
      显式部署照推(「推送到云端」= 把本机状态推上去)并逐条列出;自动部署必须停下(见 routes)。
    · names(云端实际装的节点名)里有、manifest 里却没有它的版本:同样比不了,列为 drift。
      manifest 整个缺失的情况由调用方标 unchecked(reconcile_baked_with_cloud)。
    ⚠ 说明行不打印 url:私有仓库的 url 常带凭据。"""
    drift, corrected = [], []
    if manifest is not None and names:
        reported = {e.get("name") for e in manifest if isinstance(e, dict)}
        drift += [f"{n.get('name')}: 云端装着它,但没报它的版本,比对不了" for n in local
                  if n.get("name") in names and n.get("name") not in reported]
    for n, c in commit_drift(local, manifest):
        try:
            g = folder_git_info(n.get("name"))
        except Exception:
            g = {}
        inst = {"url": g.get("url") or "", "commit": (g.get("commit") or "").strip(),
                "cnr_id": g.get("cnr_id"), "version": g.get("version")}
        inst_cnr = cnr_ref(inst) if g.get("has_git") else None
        # 要连地址一起换(仓库搬过家),而云端那条是带凭据的私有仓库、本机地址却没带凭据(ssh / 凭据助手克隆):
        # 换过去的地址构建时克隆不下来,整个镜像构建失败(2026-09-28 review)。这种不自动更正,留作差异。
        moved_private = c.get("url_redacted") and not _same_repo(n.get("url"), c.get("url")) \
            and "@" not in inst["url"].split("://", 1)[-1].split("/", 1)[0]
        if g.get("has_git") and (inst["commit"] or inst_cnr) and _same_source(inst, c) and not moved_private:
            old = _show_src(n)
            if inst_cnr:
                # Registry 节点:(id, version) 就是全部来源,整条换成规整的 CNR 条目
                n.update(_baked_entry(n.get("name"), inst))
            else:
                n.pop("cnr_id", None)           # 清单里原来若是 Registry 条目,字段会让 cnr_ref 仍判成 CNR
                n.pop("version", None)
                n["commit"] = inst["commit"]
                if not _same_source(n, c):      # 仓库也变了(清单里是旧地址)→ 连地址一起更正
                    n["url"] = inst["url"]
            corrected.append(f"{n.get('name')}: 清单里是 {old},本机实际装的 "
                             f"{_show_src(inst)} = 云端")
        else:
            if inst_cnr:
                installed = f"Registry {inst_cnr[1]}"
            elif g.get("has_git") and inst["commit"]:
                installed = _show_commit(inst["commit"])
            elif g.get("has_git"):
                installed = "已装,但没有 git 记录的 commit(Registry / 压缩包安装)"
            elif g.get("url_problem"):
                installed = f"已装,但来源地址云端克隆不了({g['url_problem']})"
            else:
                installed = "没装,或读不到来源"
            if moved_private and g.get("has_git") and _same_source(inst, c):
                installed += "(与云端一致,但云端是带凭据的私有仓库、本机地址不带凭据,没有自动改地址)"
            drift.append(f"{_drift_line(n, c)};本机实际装的:{installed}")
    return drift, corrected


def reconcile_baked_with_cloud(cfg: dict) -> Reconciled:
    """部署前把「云端镜像里有、本机清单里没有」的节点并回本机清单。
    返回 Reconciled(added=并回的节点名, drift / corrected = 同名节点的版本差异,见 resolve_drift)。

    只加不删 —— 删除只能走「管理云端节点」面板的显式 prune(那条路径不调这里)。
    来源优先级:云端 manifest(未脱敏的)→ 本机 custom_nodes 的 git 信息。

    会抛 DeployBlocked(调用方必须中止部署),两种情况:
      · 有节点补不出来源(云端太老报不出来源 / 来源带凭据被脱敏 / 本机也没装)——
        部署下去就把它们从镜像删了。第一版在云端没有 manifest 时直接什么都不做,而 Registry 的
        latest 还是 0.7.9,所有从 Registry 升级的用户第一次部署都是这种云端(2026-09-24 review)。
      · 读不到云端装了什么(key 不对 / 网络 / 服务出错),**而本机清单又是空的** —— 这是最危险的组合:
        多半是插件重装丢了清单,贸然部署就清空云端。以前把 401、超时、未部署统统当「拿不到」,
        保护静默跳过(2026-09-24 review #14)。
    云端 404(app 还没部署 / 已删)不算读不到:那是全新部署,没有可保护的东西。
    本机清单非空时读不到云端就尽力而为、照常部署 —— 那不是丢清单的形态。"""
    try:
        names, manifest = fetch_cloud_nodes(cfg)
    except health_client.HealthUnavailable as e:
        if e.kind == "not_deployed":
            return Reconciled([], [])
        if not read_baked_nodes():
            raise DeployBlocked(
                f"读不到云端装了哪些自定义节点({e}),而本机节点清单是空的 —— 多半是插件重装丢了"
                f"清单,继续部署可能清空云端全部自定义节点,已中止。\n"
                f"处理:检查网络 / bridge key 后重试。若确认云端不需要任何自定义节点,可在 Modal 控制台"
                f"删掉这个 app 再部署(会按全新部署处理)。") from None
        return Reconciled([], [], unchecked=f"读不到云端节点清单:{e};云端独有的节点也没法并回")
    local = read_baked_nodes()
    # 云端报了名字却没报版本(云端早于 0.8.48,或它读清单失败):同名节点一个都比不了。
    # 以前当成「没差异」,自动部署照推本机的旧 commit(2026-09-28 review)。
    unchecked = ""
    if manifest is None and any(n.get("name") in names for n in local):
        unchecked = "云端没报节点版本(云端早于 0.8.48,或它读取清单失败),同名节点比对不了"
    drift, corrected = resolve_drift(local, manifest, names)   # 更正会就地改 local 的条目,下面一并写回
    # 本机清单有、云端镜像里没有的:可能是别的机器在「管理云端节点」里有意删掉的,也可能是上次写了清单但
    # 部署失败。这次部署会把它装回云端 —— 以前只比两边同名的节点,这种既不拦也不列(2026-10-05 深度 review)。
    # 显式部署照推并列出;自动部署据此停下(auto_deploy_blocker)。
    # not_deployed / 读不到云端的情况在上面已经返回,走不到这里,不会把全新部署误判成「云端缺节点」。
    cloud_names = set(names)
    drift += [f"{n.get('name')}: 本机清单有、云端镜像里没有(可能在别处被有意移除,或上次部署没成功),"
              f"这次部署会把它装回云端" for n in local if n.get("name") and n.get("name") not in cloud_names]
    have = {n.get("name") for n in local}
    missing = [n for n in names if n not in have]
    back = []
    if missing:
        entries, back_drift = complete_baked_entries_ex(missing, {}, manifest)
        back = [e for e in entries if (e.get("url") or "").strip()]
        lost = [e["name"] for e in entries if not (e.get("url") or "").strip()]
        if lost:
            raise DeployBlocked(unresolved_nodes_message(lost))   # 中止时什么都不写
        drift += back_drift
    if back or corrected:
        write_baked_nodes(local + back)
    return Reconciled([e["name"] for e in back], drift, corrected, unchecked)


def drift_message(rec: Reconciled) -> str:
    """reconcile 的结果 → 部署日志里的一段(多行,已含缩进和结尾换行);没差异也没跳过返回空串。"""
    if rec.unchecked:
        return (f"   ⚠ 没能比对云端节点({rec.unchecked})。本次部署以本机清单为准,"
                f"同名节点可能被换成清单里的版本\n")
    out = ""
    if rec.corrected:
        out += ("   ✓ 本机节点清单陈旧,已按本机实际装的版本更正(与云端一致,这次不会改动它们):\n"
                + "".join(f"      {d}\n" for d in rec.corrected))
    if rec.drift:
        out += ("   ⚠ 以下节点与云端不一致,本次部署以本机清单为准(若清单是旧的,这就是一次降级):\n"
                + "".join(f"      {d}\n" for d in rec.drift))
    return out


def auto_deploy_blocker(rec: Reconciled) -> str:
    """自动部署(上传私有节点时依赖变了触发的)能不能继续。返回中止说明;能继续返回空串。

    自动部署是为私有节点依赖触发的,用户没要求动公共节点。有判断不了谁对的版本差异、或根本没读到云端,
    就不能替用户决定 —— 停下,交给显式的「推送到云端」(2026-09-27 review:上传私有节点不等于同意回退
    公共节点)。清单只是陈旧的那种已被 resolve_drift 更正,不会走到这里。"""
    if not (rec.drift or rec.unchecked):
        return ""
    # ⚠ 必须是**单行**:前端弹窗只取最后一行带 ✗ 的内容,明细在上面几行里用户看不到,所以点名写进来
    if rec.unchecked:
        why = f"没能比对云端节点({' '.join(rec.unchecked.split())}),确认不了这次会不会改动公共节点"
    else:
        names = [d.split(":", 1)[0] for d in rec.drift]
        shown = "、".join(names[:5]) + (f" 等 {len(names)} 个" if len(names) > 5 else "")
        # 差异不只「同名版本不同」,还有「本机清单有、云端没有」「云端独有的私有节点本机版本不同」,
        # 措辞要盖得住这几种(2026-10-05 深度 review)
        why = f"节点 {shown} 与云端不一致,确认不了这次部署该以哪边为准"
    return (f"自动部署已中止:{why}。它是为私有节点依赖触发的,不替你改公共节点。"
            f"在面板点「推送到云端」确认部署后再提交(明细见 ComfyUI 控制台)")


def unresolved_nodes_message(unresolved: list[str]) -> str:
    return (f"云端镜像装着 {', '.join(unresolved)},但本机清单里没有,也拿不到它们的来源"
            f"(云端版本太旧报不出来源 / 来源带凭据被脱敏 / 本机也没装)。"
            f"继续部署会把它们从镜像里删掉,已中止。\n"
            f"处理:在本机装上这些节点后再部署;若确实不要它们,到「管理云端节点」里移除。")


def ensure_baked_file() -> None:
    """确保 _custom_nodes_data.py 存在(它是 .gitignore 的本地状态,可能缺失)。
    缺则写空清单 —— 部署前调用,保证 modal_image 的 import / 打包不因文件缺失失败。"""
    if not DATA_FILE.exists():
        write_baked_nodes([])


def write_baked_nodes(nodes: list[dict]) -> None:
    """用固定模板重写 _custom_nodes_data.py(保证格式稳定,可被反复机改)。"""
    lines = [_DATA_HEADER, "CUSTOM_NODES = ["]
    for n in nodes:
        # 出口校验:空 url / 空 name 会在镜像 build 时生成 `git clone '' ...`,
        # 整个 RUN 崩掉而报错和「哪个节点」对不上号。入口(folder_git_info)只在
        # url 非空时才判 has_git=True,正常流程产生不了空值 —— 但这个文件是
        # 机器可改的本地状态(可能被手工编辑、也可能是历史遗留),坏条目宁可丢掉
        # 也不能进镜像。丢弃要出声,静默跳过等于让节点"莫名其妙没装上"。
        name = (n.get("name") or "").strip()
        if not (n.get("url") or "").strip() or not name:
            print(f"[modal_bridge] ⚠ 跳过无效 baked 条目(url/name 为空): {n!r}")
            continue
        # CNR 条目规整成 {url: 版本 API 地址, cnr_id, version}:routes / 云端 manifest 只回传
        # name/url/commit,字段在这里由 url 重新补齐(见 cnr_ref 上方说明)
        e = _baked_entry(name, n)
        if not cnr_ref(e):
            # ssh 写法能换成 https 的就地换掉;换不了的(自建 ssh、本地路径、file://)云端构建克隆不到,
            # 进镜像只会让**整个** RUN 失败、连带其它节点(2026-10-05 深度 review)。同上:丢弃并出声。
            e["url"] = _normalize_git_url(e["url"])
            problem = clone_url_problem(e["url"])
            if problem:
                print(f"[modal_bridge] ⚠ 跳过 baked 条目 {name}:{problem}")
                continue
        lines.append("    {")
        for k in ("name", "url", "commit", "cnr_id", "version"):
            if k in e:
                lines.append(f'        "{k}": {json.dumps(e[k], ensure_ascii=False)},')
        lines.append("    },")
    lines.append("]")
    DATA_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


_LOCAL_REQS_HEADER = '''"""
_local_nodes_data.py — 本地自写节点(走 Volume 通道那批)的 pip 依赖清单(纯数据)

⚠ 部署时由 node_sync.write_local_node_reqs() 自动重写,别手改。

**为什么依赖装在 build 期、而代码走 Volume**:代码要能改一行就重传、不重 build
(那是 Volume 通道存在的全部意义);但依赖不能在 worker 启动时 pip install ——
ComfyUI Registry 明令禁止「Runtime package installation through subprocess calls」,
而且那样每个冷容器都要重付一次安装时间。依赖变更频率远低于代码,放 build 期正合适。

改本地节点代码 → 重传 zip 即可,不用部署。
新增/改依赖   → 同步链自动重新部署一次(这个文件会跟着变,只触发依赖层及之后)。
"""
'''


def write_local_node_reqs(reqs: list[str]) -> None:
    """重写 _local_nodes_data.py(格式固定,可反复机改)。"""
    lines = [_LOCAL_REQS_HEADER, "LOCAL_NODE_REQS = ["]
    for r in reqs:
        r = (r or "").strip()
        if not r:
            continue
        lines.append(f"    {json.dumps(r, ensure_ascii=False)},")
    lines.append("]")
    LOCAL_REQS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_local_node_reqs() -> list[str]:
    """读 _local_nodes_data.py 的 LOCAL_NODE_REQS。缺文件/解析不了 → 空列表。

    用 ast.literal_eval 而不是 import:这个文件是机器维护的纯数据,不该被执行。
    """
    try:
        src = LOCAL_REQS_FILE.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", "") == "LOCAL_NODE_REQS" for t in node.targets)):
                return [str(x) for x in ast.literal_eval(node.value)]
    except Exception:
        pass
    return []


def local_node_reqs_hash(reqs: list[str]) -> str:
    """稳定标识当前镜像应包含的本地节点依赖清单。"""
    payload = json.dumps(list(reqs), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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


_GIT_HOSTS = ("github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "gitee.com")

# ssh / git 协议的几种写法:scp 风格 user@host:path、ssh://[user@]host[:port]/path、git://host[:port]/path
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@([A-Za-z0-9.-]+):(?!//)/?(.+)$")
_SSH_RE = re.compile(r"^ssh://(?:[^@/]+@)?([^/:]+)(?::(\d+))?/(.+)$", re.I)
_GITPROTO_RE = re.compile(r"^git://([^/:]+)(?::(\d+))?/(.+)$", re.I)


def _normalize_git_url(url: str) -> str:
    """ssh / git:// 写法转 https,方便 Modal 容器里无凭据 clone。

    ⚠ 以前只认 `git@github.com:` 一种,`ssh://git@github.com/…`、`git@gitlab.com:…` 原样进镜像,
      构建时克隆失败、整个 RUN 连带其它节点一起挂(2026-10-05 深度 review)。
    只转已知公共托管站(_GIT_HOSTS)且是默认端口的:自建服务器的 ssh 地址换成 https 多半是另一个端口 /
    根本不开 https,而且它通常是私有仓库 —— 云端没有 ssh 凭据,换了也克隆不到。转不了的原样返回,
    由 clone_url_problem 在入口拒绝。"""
    url = (url or "").strip()
    host = path = port = None
    m = _SCP_RE.match(url)
    if m and "://" not in url:
        host, path = m.group(1), m.group(2)
    elif (m := _SSH_RE.match(url)):
        host, port, path = m.group(1), m.group(2), m.group(3)
        if port not in (None, "22"):
            return url
    elif (m := _GITPROTO_RE.match(url)):
        host, port, path = m.group(1), m.group(2), m.group(3)
        if port not in (None, "9418"):
            return url
    if host is None:
        return url
    host = host.lower()
    if host.removeprefix("www.") not in _GIT_HOSTS:
        return url
    return f"https://{host}/{path.lstrip('/')}"


def clone_url_problem(url: str) -> str:
    """云端构建能不能克隆这个地址;能 → 空串,不能 → 一句原因(给用户看)。

    云端构建容器里没有 ssh 密钥、没有本机文件系统,只认 http(s)。调用方应先过 _normalize_git_url。"""
    u = (url or "").strip()
    if not u:
        return "没有仓库地址"
    if re.match(r"^https?://[^/\s]+", u, re.I):
        return ""
    if _SCP_RE.match(u) or _SSH_RE.match(u):
        return (f"来源是 ssh 地址({u.split('@')[-1].split(':')[0].split('/')[0]}),云端构建没有 ssh 凭据;"
                f"只有 {' / '.join(_GIT_HOSTS)} 的 ssh 地址能自动换成 https")
    if _GITPROTO_RE.match(u):
        return "来源是 git:// 地址且不是已知公共托管站,云端构建克隆不到"
    return "来源不是 http(s) 地址(本地路径 / file:// 等),云端构建克隆不到"


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


# 未跟踪文件里**已知的运行时垃圾**:日志、缓存、字节码、编辑器 / 系统残留。只有它们不算改动。
_JUNK_DIRS = frozenset({"__pycache__", ".cache", "cache", "caches", "logs", "log", "tmp", "temp",
                        ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules",
                        ".ipynb_checkpoints"})
_JUNK_SUFFIXES = frozenset({".log", ".tmp", ".pyc", ".pyo", ".swp", ".bak"})
_JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def _untracked_is_junk(rel: str) -> bool:
    parts = [x for x in rel.strip().strip('"').rstrip("/").split("/") if x]
    if not parts:
        return False
    if any(x in _JUNK_DIRS for x in parts):
        return True
    return parts[-1] in _JUNK_NAMES or Path(parts[-1]).suffix.lower() in _JUNK_SUFFIXES


def worktree_dirty(path: Path) -> bool:
    """工作树有未提交改动。
    这是自写 / 调试节点**最常见**的状态:改一行试一下,谁会先 commit 再 push?
    而云端只按 commit clone —— HEAD 没变但文件变了,镜像里跑的还是旧代码,
    且改前改后结果一模一样、毫无线索。所以 dirty 必须当成「本地版本 ≠ 云端版本」。

    ⚠ 未跟踪文件:只豁免**已知的运行时垃圾**(日志 / 缓存 / 字节码,见 _JUNK_*),其余一律算改动。
      以前把所有未跟踪文件都算改动,运行时往自己目录写日志、缓存的公开节点被永久判 dirty
      (2026-09-23 review)。0.8.48 改成「只有源码(.py/.js…)才算」—— **方向反了**:用户往节点里加的
      .json 预设、.txt 通配词、提示词模板、.so/.cu 扩展照样改变云端行为,却被判成干净,云端 clone
      里没有它们,出错或静默出不同的结果、毫无线索;而 .js/.ts 在无头的云端反而无关紧要
      (2026-09-24 review)。误判 dirty 的代价是多传一次包、多弹一次框;误判干净是静默出错 ——
      所以默认算改动,只豁免能确定是垃圾的。运行时生成的配置文件(如 settings.json)仍会判 dirty,
      这类请在节点里 gitignore。已跟踪文件的任何改动照旧一律算 dirty。"""
    out = _git(["status", "--porcelain", "--untracked-files=normal"], path)
    # 已确认是该节点自己的 repo 后,status 仍失败时按 dirty 处理:方向安全,最多多传一次包;
    # 反过来当 clean 会把本地改动静默丢掉。
    if out is None:
        return True
    for line in out.splitlines():
        if not line.strip():
            continue
        if not line.startswith("??"):
            return True                       # 已跟踪文件有改动
        if not _untracked_is_junk(line[3:]):
            return True
    return False


def _is_own_git_repo(path: Path) -> bool:
    """path 本身是不是一个 git worktree 根。

    Git 在子目录执行时会一路向父目录找 `.git`。ComfyUI 通常自身就是 git clone,
    因此不能仅在 custom_nodes/foo 里跑 `git rev-parse HEAD`:一个完全无 git 的自写节点
    会误继承 ComfyUI 的 remote/HEAD,最后把 ComfyUI 仓库当作该节点部署。
    """
    top = _git(["rev-parse", "--show-toplevel"], path)
    if not top:
        return False
    try:
        return Path(top).resolve() == Path(path).resolve()
    except OSError:
        return False


def folder_git_info(folder: str) -> dict:
    """读本地 custom_nodes/<folder> 的可克隆地址 + commit。
    主路径读 .git(remote.origin.url + HEAD commit);Registry(CNR)装的节点回
    {url: Registry 版本地址, commit: "", cnr_id, version}(见 cnr_ref 上方说明);
    压缩包装的节点没有 .git,则兜底读 pyproject.toml 的仓库地址(commit 留空 = 跟随默认分支 HEAD)。
    has_git 在此表示「解析得到云端构建拿得到的来源」(未必真有本地 .git)。
    额外回 pushed:False 表示本地 commit 没推到远端(云端 checkout 必失败)。
    remote 是云端克隆不了的地址(自建 ssh / 本地路径)时 has_git=False,url_problem 写明原因 ——
    这种节点不能进镜像清单,走私有节点通道(Volume)。"""
    path = _comfyui_root() / "custom_nodes" / folder
    if not path.is_dir():
        # 注意用 is_dir:单文件节点(custom_nodes/foo.py)不是可 clone 的仓库,
        # 走 exists() 会让后面的 git 调用在非目录 cwd 上报错,白跑一圈。
        return {"folder": folder, "has_git": False, "url": None, "commit": None, "pushed": True}
    if _is_own_git_repo(path):
        url = _git(["config", "--get", "remote.origin.url"], path)
        commit = _git(["rev-parse", "HEAD"], path)
        if url and commit:
            url = _normalize_git_url(url)
            info = {"folder": folder, "has_git": True,
                    "url": url, "commit": commit,
                    "pushed": commit_on_remote(path, commit),
                    "dirty": worktree_dirty(path)}
            problem = clone_url_problem(url)
            if problem:
                info.update(has_git=False, url_problem=problem)
            return info
    cnr = _read_cnr_info(path)
    if cnr:
        # Manager 不跟踪 Registry 包里的文件改动,这里也没有可比的依据 → dirty 按 False(同 pyproject 兜底)
        return {"folder": folder, "has_git": True, "url": cnr_url(*cnr), "commit": "",
                "cnr_id": cnr[0], "version": cnr[1], "pushed": True, "dirty": False}
    repo = _pyproject_repo_url(path)
    if repo:
        return {"folder": folder, "has_git": True,
                "url": _normalize_git_url(repo), "commit": "", "pushed": True,
                # pyproject 只提供源码地址,这个目录本身并不是 git worktree；不能跑 git status,
                # 否则会再次向上命中 ComfyUI 主仓库。
                "dirty": False}
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


def _cloud_stale_reason(git: dict, local_commit: str, baked_commit: str,
                        baked: dict | None = None) -> str | None:
    """已烤进镜像的节点:云端那份跟本地比是不是旧的?返回原因,None = 一致无需动作。
      "dirty"    工作树有未提交改动 —— 云端按 commit clone,拿不到这些改动
      "unpushed" commit 变了但没推 —— 云端 clone 不到这个 commit
      "no_git"   拿不到 git 依据(.git 丢了/不是仓库)—— 无从判断,只能按「可能不一致」处理
      "commit"   干净且已推、commit 与镜像不同 —— 走 git 路线更新即可
      "cnr"      本机是 Registry 装的、版本与镜像那条不同 —— 按本机的 Registry 版本更新(同 commit 路线)
      "unclonable" remote 是云端克隆不了的地址 —— 只能走私有节点通道
    baked 是镜像清单里的那条(Registry 节点按 (cnr_id, version) 比,commit 两边都是空的)。
    纯函数,单测覆盖各条分支。"""
    if git.get("dirty"):
        return "dirty"
    lref = cnr_ref(git)
    if lref:
        # ⚠ 以前 Registry 节点没有 commit,一律落到 no_git、走 Volume 通道并触发依赖重建
        #   (2026-10-05 深度 review)。现在有 (id, version) 可比。
        return None if baked is not None and cnr_ref(baked) == lref else "cnr"
    if git.get("url_problem"):
        return "unclonable"
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
      - local_pack: 无 git remote(自写节点)、本地 commit 未推送、或 remote 云端克隆不了
                    (reason=unclonable,detail 写原因)→ 走 Volume 打包通道,
                    **不需要重新部署**(worker 启动时解压,见 local_nodes.py)
      - prune: baked 有、但本地 custom_nodes 没有的 → 候选移除

    ⚠ 多机场景:不同电脑各装一部分节点,"本地没有"≠"全局不需要"。所以默认
    allow_prune=False —— 自动同步(出图时)只增不删,镜像 = 各机贡献的并集,永不互删。
    prune 只在「管理云端节点」面板里手动勾选执行(allow_prune=True 时才会从 new_baked 移除)。
    任一非空即 needs_deploy=True;new_baked 是写回 _custom_nodes_data.py 的完整新清单。

    baked 不传则读本地 _custom_nodes_data.py。
    返回:
      {
        "add": [{folder, class_types, url, commit}],          # Registry 节点另带 cnr_id / version
        "update": [{folder, url, old_commit, commit}],        # Registry 节点另带 cnr_id / version / old_version
        "expect_baked": [folder...],                  # 本次任务明确应运行镜像版
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

    add, update, local_pack, missing_no_git, expect_baked = [], [], [], [], []
    ok_baked = 0
    # 1) 工作流用到的 custom_node:加 / 更新 / 走本地打包通道
    for folder, class_types in info["by_folder"].items():
        git = folder_git_info(folder)
        if folder in baked_by_name:
            ok_baked += 1
            local_commit = (git.get("commit") or "").strip()
            baked_commit = (baked_by_name[folder].get("commit") or "").strip()
            reason = _cloud_stale_reason(git, local_commit, baked_commit, baked_by_name[folder])
            if reason is None:
                # 这次应跑镜像版。若历史上曾上传过 dirty/local 覆盖包,它必须退场;
                # expect_baked 还会随任务发给暖容器,让已解压/已 import 的旧覆盖恢复成 baked。
                expect_baked.append(folder)
            elif reason in ("commit", "cnr"):
                # 干净、已推、commit 变了 → 走 git 路线更新镜像;Registry 节点同理,按本机版本更新
                entry = _baked_entry(folder, git)
                upd = {"folder": folder, "url": entry["url"],
                       "old_commit": baked_commit, "commit": entry["commit"]}
                if reason == "cnr":
                    old_ref = cnr_ref(baked_by_name[folder])
                    upd.update(cnr_id=entry["cnr_id"], version=entry["version"],
                               old_version=old_ref[1] if old_ref else "")
                update.append(upd)
                baked_by_name[folder] = entry
                expect_baked.append(folder)
            elif folder_exists_locally(folder):
                # dirty / 未推送 / 没有 git 可依据 —— 云端 clone 不到这一版,
                # 走本地打包通道盖掉镜像里那份。
                # ⚠ 绝不能默默跳过:那样云端**静默跑旧代码**,比部署报错难查得多
                #   (用户改完节点点运行,结果和改之前一模一样,毫无线索)。
                item = {"folder": folder, "class_types": sorted(class_types), "reason": reason}
                if git.get("url_problem"):
                    item["detail"] = git["url_problem"]
                local_pack.append(item)
            continue
        if git["has_git"] and git.get("pushed", True) and not git.get("dirty"):
            entry = _baked_entry(folder, git)
            item = {"folder": folder, "class_types": sorted(class_types),
                    "url": entry["url"], "commit": entry["commit"]}
            if "cnr_id" in entry:
                item.update(cnr_id=entry["cnr_id"], version=entry["version"])
            add.append(item)
            baked_by_name[folder] = entry
            expect_baked.append(folder)
        elif folder_exists_locally(folder):
            # 自写节点(无 git remote)、或有 remote 但本轮改动没推 —— 走本地打包通道:
            # 打包传 Volume,worker 启动时解压。不需要重 build 镜像(见 local_nodes.py)。
            # remote 是云端克隆不了的地址(自建 ssh / 本地路径)也走这里,而不是进镜像清单让构建失败。
            item = {"folder": folder, "class_types": sorted(class_types),
                    "reason": ("unclonable" if git.get("url_problem")
                               else "unpushed" if git["has_git"] else "no_git")}
            if git.get("url_problem"):
                item["detail"] = git["url_problem"]
            local_pack.append(item)
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
        # 本次工作流里明确应运行镜像版的节点。local backend 会据此清理 Volume 旧覆盖包,
        # worker 也会用 sentinel 恢复已启动暖容器里的 baked 目录。
        "expect_baked": sorted(set(expect_baked)),
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
#   - modal 由 Manager/Registry 按项目依赖安装,后续 deploy / add_nodes 都用同一解释器
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


# 镜像解释器版本。**这是 modal_app/modal_image.py 里 add_python= 的副本** ——
# 不 import 那个模块是因为它 `import modal`,而宿主机不保证装了 modal
# (整个 _ensure_modal 流程就是为此存在)。两处走散会让诊断说瞎话,
# test_core 有一条测试盯着它们一致。
IMAGE_PYTHON_VERSION = "3.12"

# pip 装包失败时的形态。现代 pip 会先打 `Collecting <pkg>`,再在该包的构建段落里报错;
# 末尾往往还有一句 `Failed building wheel for <pkg>` / `Failed to build <pkg>`。
# 两头都认,取到就够给一句人话 —— 用户面对的原始信息是几十行 traceback,里面
# `KeyError: '__version__'` 这种东西跟"我该改什么"看不出任何关系。
_PIP_COLLECT_RE = re.compile(r"^\s*Collecting\s+([A-Za-z0-9._-]+)", re.M)
_PIP_FAILED_RE = re.compile(
    r"(?:Failed building wheel for|Failed to build|Could not build wheels for)\s+([A-Za-z0-9._-]+)")
_PIP_FAIL_MARKS = ("subprocess-exited-with-error", "did not run successfully",
                   "error: metadata-generation-failed", "ERROR: Failed building wheel")


def diagnose_build_failure(text: str) -> str:
    """从命令输出里认出常见的构建失败形态,返回一句可操作的中文提示;认不出返回空串。

    纯函数,可单测。目前只认 pip 装包失败这一种 —— 它是私有节点上云最常见的坑
    (2026-08-31 实测:basicsr 1.4.2 停更于 2022,当时镜像是 Python 3.13,
    隔离构建下 setup.py 取版本号抛 KeyError;2026-09-02 镜像已降到 3.12,
    这一类构建失败随之消失,但别的包仍可能因别的原因装不上)。
    认不出就别硬猜,原始日志已经在上面。
    """
    if not text:
        return ""
    pkg = ""
    m = _PIP_FAILED_RE.search(text)
    if m:
        pkg = m.group(1)
    elif any(k in text for k in _PIP_FAIL_MARKS):
        # 没有显式的 "Failed building wheel for X" 时,取最后一个 Collecting 的包
        # —— pip 是顺序处理的,报错紧跟在它后面。
        found = _PIP_COLLECT_RE.findall(text)
        pkg = found[-1] if found else ""
    if not pkg:
        return ""
    return (f"💡 看起来是云端安装 `{pkg}` 失败。常见原因:这个包已停止维护、或不兼容"
            f"镜像里的 Python 版本(当前 {IMAGE_PYTHON_VERSION}),setup.py 在隔离构建下跑不起来。\n"
            f"   处理办法:在用到它的那个私有节点的 requirements.txt 里把 `{pkg}` 去掉"
            f"(先确认代码是否真的 import 了它)、换一个仍在维护的版本,或改用不依赖它的实现;\n"
            f"   改完在 Setup 里点「推送到云端」即可 —— 它会自动比对差异,"
            f"把改动的节点推上去、需要时重建镜像。")


def gen_bridge_key() -> str:
    """私有 endpoint 自建鉴权 key(部署时生成,存 Modal Secret + 本地 config)。"""
    import secrets
    return "bk-" + secrets.token_urlsafe(24)


def deploy_command() -> list[str]:
    return [sys.executable, "-m", "modal", "deploy", "modal_app.py"]


def node_compat_check_command() -> list[str]:
    """部署后跑:隔离 app 在同一镜像里 boot 一次 ComfyUI,报告每个自定义节点导入成功/失败。"""
    return [sys.executable, "-m", "modal", "run", "node_compat_check.py"]


# 命令行里长这样的 KEY=VALUE,VALUE 属于凭据,回显必须打码。
# 只匹配 key 名里的这几个词,别的(如 AIGC_STUDIO_BASE_URL)保持明文 —— 排查问题要看得见。
_SECRETISH = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


def redact_cmd(cmd: list[str]) -> str:
    """把命令行拼成可回显的字符串,凭据打码。

    ⚠ secret_create_cmd 把 BRIDGE_API_KEY / HF_TOKEN / CIVITAI_TOKEN 等**明文写在 argv 里**
    (modal secret create 的调用形式就是这样)。部署面板会把命令行流式回显给浏览器,
    用户复制这段日志求助 = 全套凭据外泄。插件其它地方对密钥都很小心(/config 抹 secret、
    delivery token 不落 job_state),这条是最宽的口子。
    保留 key 名和长度,够长的再露前 4 位(hf_ / bk- 这类前缀对排查有用)—— 够判断
    "是不是贴错了/贴空了",又不足以复用。短值一位都不露:8 位的东西露 4 位等于露一半。
    key 名命中即打码,宁滥勿缺:多打一个的代价是日志少看一个值,漏一个是事故。"""
    parts = []
    for a in cmd:
        m = _KV_RE.match(a)
        if m and any(w in m.group(1).upper() for w in _SECRETISH):
            k, v = m.group(1), m.group(2)
            if not v:
                parts.append(f"{k}=(empty)")
            else:
                parts.append(f"{k}={v[:4] if len(v) >= 12 else ''}***(len={len(v)})")
        else:
            parts.append(a)
    return " ".join(parts)


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


# 逻辑字段 → Secret 里的键(与 secret_create_cmd 一一对应)
_SECRET_KEYS = {
    "bridge_key": ("BRIDGE_API_KEY",),
    "hf_token": ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"),
    "civitai_token": ("CIVITAI_TOKEN",),
    "comfy_api_key": ("COMFY_API_KEY_COMFY_ORG",),
    "aigc_base_url": ("AIGC_STUDIO_BASE_URL",),
    "aigc_bypass_secret": ("AIGC_STUDIO_BYPASS_SECRET",),
}


def secret_upsert_cmd(cfg: dict, hf_token: str = "", civitai_token: str = "",
                      bridge_key: str = "", comfy_api_key: str = "",
                      aigc_base_url: str = "", aigc_bypass_secret: str = "",
                      clear=()) -> list[str]:
    """合并语义地写 Modal Secret 的命令(参数顺序与 secret_create_cmd 相同,可直接替换)。

    非空的值写入(覆盖同名键);空值 = 这次没提供,Secret 里原有的键**保持不动**;
    clear 里点名的逻辑字段(如 ("aigc_base_url", "aigc_bypass_secret"))才清空 —— 只用于用户明确清掉的项。

    ⚠ 为什么不再 `secret create --force` 整份重建:Secret 只按这一台机器的 config 重建,别的机器 /
      deploy.py --hf-token 写进去的 HF / comfy.org / AIGC 凭据就被抹掉(2026-10-05 深度 review)。
    合并靠 modal.Secret.update(SDK ≥ 1.3.5;Mac 上实测 1.4.3 有,语义同 dict.update)。
    公开 API 删不了键(proto 支持,但只能走私有 stub),所以「清空」是写成空串 —— worker 读这些键
    都按真假判断(空串 = 没配),效果等同删除。SDK 太旧时脚本退回旧的整份重建并明说。

    跑的是 `python node_sync.py secret-upsert …`(见文件末尾):值仍以 KEY=VALUE 出现在 argv 里,
    redact_cmd 的打码规则原样适用。"""
    values = {"bridge_key": bridge_key, "hf_token": hf_token, "civitai_token": civitai_token,
              "comfy_api_key": comfy_api_key, "aigc_base_url": aigc_base_url,
              "aigc_bypass_secret": aigc_bypass_secret}
    clear = set(clear or ())
    bad = clear - set(_SECRET_KEYS) | (clear & {"bridge_key"})
    if bad:
        raise ValueError(f"不能清空的字段: {sorted(bad)}")
    app_name = cfg.get("modal_app_name", "comfyui-bridge")
    pairs = [f"{k}={v}" for f, v in values.items() if v for k in _SECRET_KEYS[f]]
    clears = [f"--clear={k}" for f in sorted(clear) if not values.get(f) for k in _SECRET_KEYS[f]]
    return [sys.executable, str(Path(__file__).resolve()), "secret-upsert",
            f"{app_name}-secrets", *pairs, *clears]


def _secret_upsert_main(argv: list[str], modal_mod=None) -> int:
    """secret_upsert_cmd 的执行体:`python node_sync.py secret-upsert <名> KEY=VALUE… [--clear=KEY…]`。"""
    if not argv:
        print("用法: python node_sync.py secret-upsert <secret 名> KEY=VALUE... [--clear=KEY ...]")
        return 2
    name, sets, clears = argv[0], {}, []
    for a in argv[1:]:
        if a.startswith("--clear="):
            clears.append(a[len("--clear="):])
            continue
        k, sep, v = a.partition("=")
        if not sep or not k:
            print(f"✗ 参数不是 KEY=VALUE: {k[:40]}")
            return 2
        sets[k] = v
    if modal_mod is None:
        import modal as modal_mod
    if not hasattr(modal_mod.Secret, "update"):
        print("⚠ 本机 modal SDK 早于 1.3.5,没有 Secret.update —— 退回整份重建(--force):"
              "这次没给的键会被清掉。升级 SDK(pip install -U modal)后恢复合并更新。", flush=True)
        return subprocess.call([sys.executable, "-m", "modal", "secret", "create", "--force", name,
                                *([f"{k}={v}" for k, v in sets.items()] or ["EMPTY=1"])])
    upd = {**{k: "" for k in clears}, **sets}
    if not upd:
        print(f"[modal_bridge] Secret {name}:没有要改的键")
        return 0
    try:
        try:
            modal_mod.Secret.from_name(name).update(upd)
            how = "合并更新"
        except modal_mod.exception.NotFoundError:
            modal_mod.Secret.objects.create(name, dict(sets) or {"EMPTY": "1"})
            how = "新建"
    except Exception as e:
        # 只报类型和消息:Modal 的报错不含 Secret 的值,但别把 upd 打出来
        print(f"✗ 写 Modal Secret {name} 失败:{type(e).__name__}: {e}")
        return 1
    print(f"✓ Secret {name} 已{how}:写入 {', '.join(sorted(sets)) or '无'}"
          + (f";清空 {', '.join(sorted(clears))}" if clears else "") + "(其余键保持不变)")
    return 0


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
    env["MODAL_BRIDGE_INLINE_TOTAL_MB"] = str(cfg.get("inline_total_mb", 24))  # 单任务内联总量上限
    env["MODAL_BRIDGE_VERSION"] = plugin_version()  # 版本契约:烤进 app,health 回传供前端比对
    if cfg.get("modal_token_id"):
        env["MODAL_TOKEN_ID"] = cfg["modal_token_id"]
    if cfg.get("modal_token_secret"):
        env["MODAL_TOKEN_SECRET"] = cfg["modal_token_secret"]
    return env


if __name__ == "__main__":
    # 只给 secret_upsert_cmd 用:部署流程以子进程跑它,与 `modal secret create` 同样的 argv 形态。
    if len(sys.argv) >= 2 and sys.argv[1] == "secret-upsert":
        sys.exit(_secret_upsert_main(sys.argv[2:]))
    sys.exit("用法: python node_sync.py secret-upsert <secret 名> KEY=VALUE... [--clear=KEY ...]")
