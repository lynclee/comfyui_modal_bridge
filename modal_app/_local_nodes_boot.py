"""
_local_nodes_boot.py — worker 启动时把 Volume 上的「本地自写节点包」解压进 ComfyUI。

对应本地侧 local_nodes.py:那边打包上传到 /comfy-volume/_local_nodes/<folder>.zip,
这边在 ComfyUI 进程起来之前解压到 /comfyui/custom_nodes/<folder>/。

为什么放运行时而不是 build 时:这条通道存在的意义就是「改代码不必重 build 镜像」。
代价是每个冷容器付一次解压(代码包很小,毫秒级)。依赖不在这里装 —— 见 _warn_if_requirements。

⚠ 解压必须防 zip-slip:包虽然是用户自己传的,但解压路径来自 zip 内的字符串,
一个 ../../ 就能写到 /comfyui 之外。这里逐条校验规范化后的目标路径仍在目标目录内。
"""
import os
import shutil
import zipfile
import tempfile
from pathlib import Path

VOL_DIR = Path("/comfy-volume/_local_nodes")
DEST_DIR = Path("/comfyui/custom_nodes")
# 本地包覆盖同名 baked 节点前,把镜像版留在容器临时目录。这样包从 Volume 删除后,
# 已经启动的暖容器也能恢复 baked,不必继续运行内存/磁盘里的旧覆盖版。
# tempfile.gettempdir() 在容器里就是 /tmp,行为不变;写死字面量会吃 Registry 扫描器的
# Bandit B108(MEDIUM)—— 2026-09-05 查实那 4 条 MEDIUM 是我们被 Flagged 唯一能控的杠杆。
BACKUP_DIR = Path(tempfile.gettempdir()) / "modal-bridge-baked-nodes"
BAKED_SENTINEL = "__modal_bridge_baked__"
# 解压了一个「有 zip 无 .digest」的残包时写这个值。不能不写:marker 缺失会被
# current_digests / needs_refresh 读成「这个目录是镜像自带的 baked 版」,于是残留的旧本地
# 代码一直跑下去还没人发现(见 local_nodes.remove_volume_local_node 的同一处注释)。
# 写成一个永远对不上任何真实 digest 的值,expected 是 BAKED_SENTINEL 就触发回退 baked、
# 是具体指纹就触发重装 —— 两条路都能自愈。
UNKNOWN_DIGEST = "__modal_bridge_unknown__"

# 节点目录名校验。名字会拼成 DEST_DIR / name 再拿去 rmtree / copytree。
# 历史:最早只挡 "/"、"\\"、".." 三种子串 —— "." 本身漏过去了:restore_baked(["."]) 的 target 就是
# custom_nodes 整个目录,备份目录存在时会被 rmtree 掉、再拿备份根整个盖回来(2026-10-05 深度 review)。
# 随后改成的字符白名单又比本机 local_nodes.safe_folder 严:Bob's Nodes、🎨nodes、(old) nodes、[dev] tools
# 这类本机能打包上传的名字,云端一律拒,声明了它的任务必然失败(2026-10-05 深度 review 复核 r2)。
# 现在与本机对齐:去掉首尾空白后为空 / "." / ".." 拒;含 "/" "\\" 拒;另外拒 NUL(本机那侧由 resolve 的
# "embedded null byte" 挡住)。其余字符一律放行 —— 越界防护不靠字符集,靠下面「规范化后的父目录必须
# 恰好是 base」那道闸:单段、非 "." / ".." 的名字规范化后不可能离开 base。
_FOLDER_FORBIDDEN = ("/", "\\", "\0")


def node_target(folder, base: Path | None = None) -> Path:
    """校验节点目录名并返回 base(默认 DEST_DIR)下的目标路径;不合法抛 ValueError。

    两道闸:与本机 local_nodes.safe_folder 同一套名字规则 + 规范化后的父目录必须恰好是 base。"""
    base = DEST_DIR if base is None else base
    if (not isinstance(folder, str) or folder.strip() in ("", ".", "..")
            or any(c in folder for c in _FOLDER_FORBIDDEN)):
        raise ValueError(f"非法节点名: {folder!r}")
    root = Path(os.path.normpath(str(base)))
    target = Path(os.path.normpath(str(root / folder)))
    if target.parent != root or target == root:
        raise ValueError(f"非法节点名(越出 {root}): {folder!r}")
    return target


def safe_members(names: list[str], dest: Path) -> tuple[list[str], list[str]]:
    """把 zip 条目分成 (安全的, 危险的)。纯函数,可单测。
    危险 = 绝对路径 / 含 .. / 规范化后跑出 dest。"""
    ok, bad = [], []
    dest_res = Path(os.path.normpath(str(dest)))
    for n in names:
        if n.endswith("/"):
            continue  # 目录条目,解压时自动建
        if n.startswith("/") or n.startswith("\\") or ".." in Path(n).parts:
            bad.append(n)
            continue
        target = Path(os.path.normpath(str(dest_res / n)))
        try:
            target.relative_to(dest_res)
        except ValueError:
            bad.append(n)
            continue
        ok.append(n)
    return ok, bad


def _warn_if_requirements(node_dir: Path) -> None:
    """节点带 requirements.txt 时提示一句 —— 依赖由镜像 build 期安装,这里不装。

    这里以前会起子进程装依赖。改掉有两个原因,后者才是硬约束:
      1. 每个冷容器都要重付一次安装时间;
      2. ComfyUI Registry 明令禁止「Runtime package installation through subprocess
         calls」—— 这条会让发布版本被判 Flagged,用户在 Manager 里装不到新版。

    现在依赖 manifest 随节点包进 Volume,同步链汇总后走 _local_nodes_data.py →
    modal_image 的 pip_install 层。**代码仍走 Volume(改一行免重 build)**,
    新增/改依赖时同步链自动重新部署一次。
    """
    if not (node_dir / "requirements.txt").is_file():
        return
    print(f"[bridge] local-node {node_dir.name}: 有 requirements.txt —— 依赖在镜像 build 期已装。"
          f"若该节点导入失败提示缺包,说明依赖是新加的,去 Setup 重新部署一次即可。")


def current_digests() -> dict:
    """容器内**当前已解压**的那批包的指纹({folder: digest})。
    解压时把 Volume 上的 .digest 落一份到目标目录内,重启也不会丢(目录还在)。"""
    out = {}
    if not DEST_DIR.is_dir():
        return out
    for marker in DEST_DIR.glob("*/.mb_local_digest"):
        try:
            out[marker.parent.name] = marker.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return out


def needs_refresh(expected: dict, loaded: dict | None = None) -> list[str]:
    """提交方声明的指纹 vs 容器内实际的 → 哪些节点过期了(纯函数,可单测)。

    loaded:ComfyUI 进程**启动时**加载的那批指纹(modal_app 在每次拉起 ComfyUI 前记下)。
    判断「这一单会不会跑旧代码」必须比它,不能比磁盘 marker:纠偏失败时磁盘已经是新版、
    内存里还是旧代码,比磁盘会得出「不过期」,下一单就静默跑旧节点(2026-10-05 深度 review)。
    None = 比磁盘(ComfyUI 停着、要复核「下次启动会装哪版」时就该比磁盘)。"""
    cur = current_digests() if loaded is None else loaded
    stale = []
    for folder, digest in (expected or {}).items():
        if digest == BAKED_SENTINEL:
            # 声明 baked 时,容器里仍有本地 digest marker 就说明旧覆盖尚未退场。
            if folder in cur:
                stale.append(folder)
        elif digest and cur.get(folder) != digest:
            stale.append(folder)
    return sorted(stale)


def _remember_baked(target: Path, folder: str) -> None:
    """首次用本地包覆盖镜像目录前保存 baked 副本；后续重复解压不覆盖这份基线。"""
    backup = node_target(folder, BACKUP_DIR)
    if backup.exists() or not target.is_dir() or (target / ".mb_local_digest").exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target, backup)


def restore_baked(folders: list[str]) -> list[str]:
    """撤销指定节点的本地覆盖。

    同名 baked 目录存在备份时恢复它；纯本地节点没有备份,则删除已解压目录。
    已经是 baked(无 marker 且无备份)时幂等跳过。
    """
    restored = []
    for folder in folders or []:
        target = node_target(folder)
        backup = node_target(folder, BACKUP_DIR)
        marker = target / ".mb_local_digest"
        if backup.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(backup, target)
            restored.append(folder)
        elif marker.exists():
            shutil.rmtree(target)
            restored.append(folder)
    return restored


def extract_all() -> list[str]:
    """解压 Volume 上所有本地节点包。返回成功装上的 folder 名单。
    整个流程对异常宽容:本地节点是增量能力,坏一个不该阻断 worker 启动。"""
    if not VOL_DIR.is_dir():
        return []
    installed = []
    for zp in sorted(VOL_DIR.glob("*.zip")):
        folder = zp.stem
        # 目录名消毒:folder 来自 Volume 上的文件名,同样不可信(".zip" 的 stem 就是 "")
        try:
            target = node_target(folder)
        except ValueError:
            print(f"[bridge] ⚠ 跳过非法的本地节点包名: {zp.name}")
            continue
        try:
            with zipfile.ZipFile(zp) as z:
                ok, bad = safe_members(z.namelist(), target)
                if bad:
                    print(f"[bridge] ⚠ {folder}: {len(bad)} 个条目路径越界,已跳过: {bad[:3]}")
                if not ok:
                    continue
                # 已存在(镜像里 clone 过同名节点)先清掉,保证跑的就是刚传上来的这份
                if target.exists():
                    print(f"[bridge] local-node {folder}: 覆盖镜像内同名目录")
                    _remember_baked(target, folder)
                    shutil.rmtree(target, ignore_errors=True)
                target.mkdir(parents=True, exist_ok=True)
                for n in ok:
                    z.extract(n, target)
            # 落一份指纹在节点目录里:暖容器据此判断自己装的是不是最新那版(见 needs_refresh)
            dg = zp.with_suffix(".digest")
            try:
                if dg.is_file():
                    (target / ".mb_local_digest").write_text(
                        dg.read_text(encoding="utf-8").strip(), encoding="utf-8")
                else:
                    print(f"[bridge] ⚠ local-node {folder}: 缺 .digest(残包?)"
                          f" → 标记为未知版本,下次提交会强制刷新")
                    (target / ".mb_local_digest").write_text(UNKNOWN_DIGEST, encoding="utf-8")
            except Exception as e:
                print(f"[bridge] ⚠ local-node {folder}: 写指纹失败: {e}")
            _warn_if_requirements(target)
            installed.append(folder)
            print(f"[bridge] local-node ✓ {folder} ({len(ok)} files)")
        except Exception as e:
            print(f"[bridge] ⚠ 本地节点包 {folder} 解压失败(跳过): {e}")
    if installed:
        print(f"[bridge] 本地节点装载完成: {', '.join(installed)}")
    return installed
