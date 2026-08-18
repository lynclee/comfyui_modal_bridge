"""
_local_nodes_boot.py — worker 启动时把 Volume 上的「本地自写节点包」解压进 ComfyUI。

对应本地侧 local_nodes.py:那边打包上传到 /comfy-volume/_local_nodes/<folder>.zip,
这边在 ComfyUI 进程起来之前解压到 /comfyui/custom_nodes/<folder>/。

为什么放运行时而不是 build 时:这条通道存在的意义就是「改代码不必重 build 镜像」。
代价是每个冷容器付一次解压(代码包很小,毫秒级)+ 可能的 pip install(见下)。

⚠ 解压必须防 zip-slip:包虽然是用户自己传的,但解压路径来自 zip 内的字符串,
一个 ../../ 就能写到 /comfyui 之外。这里逐条校验规范化后的目标路径仍在目标目录内。
"""
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

VOL_DIR = Path("/comfy-volume/_local_nodes")
DEST_DIR = Path("/comfyui/custom_nodes")
# 本地包覆盖同名 baked 节点前,把镜像版留在容器临时目录。这样包从 Volume 删除后,
# 已经启动的暖容器也能恢复 baked,不必继续运行内存/磁盘里的旧覆盖版。
BACKUP_DIR = Path("/tmp/modal-bridge-baked-nodes")
BAKED_SENTINEL = "__modal_bridge_baked__"
# 解压了一个「有 zip 无 .digest」的残包时写这个值。不能不写:marker 缺失会被
# current_digests / needs_refresh 读成「这个目录是镜像自带的 baked 版」,于是残留的旧本地
# 代码一直跑下去还没人发现(见 local_nodes.remove_volume_local_node 的同一处注释)。
# 写成一个永远对不上任何真实 digest 的值,expected 是 BAKED_SENTINEL 就触发回退 baked、
# 是具体指纹就触发重装 —— 两条路都能自愈。
UNKNOWN_DIGEST = "__modal_bridge_unknown__"


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


def _install_requirements(node_dir: Path) -> None:
    """节点有 requirements.txt 就装。失败只警告 —— 一个节点的依赖问题不该让整个 worker 起不来。"""
    req = node_dir / "requirements.txt"
    if not req.is_file():
        return
    print(f"[bridge] local-node {node_dir.name}: pip install -r requirements.txt")
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print(f"[bridge] ⚠ {node_dir.name} 依赖安装失败(节点可能导入不了): "
                  f"{(r.stderr or '').strip()[:400]}")
    except Exception as e:
        print(f"[bridge] ⚠ {node_dir.name} 依赖安装异常: {e}")


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


def needs_refresh(expected: dict) -> list[str]:
    """提交方声明的指纹 vs 容器内实际的 → 哪些节点过期了(纯函数,可单测)。"""
    cur = current_digests()
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
    backup = BACKUP_DIR / folder
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
        if not folder or "/" in folder or "\\" in folder or ".." in folder:
            raise ValueError(f"非法节点名: {folder!r}")
        target = DEST_DIR / folder
        backup = BACKUP_DIR / folder
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
        # 目录名消毒:folder 来自 Volume 上的文件名,同样不可信
        if not folder or "/" in folder or "\\" in folder or ".." in folder:
            print(f"[bridge] ⚠ 跳过非法的本地节点包名: {zp.name}")
            continue
        target = DEST_DIR / folder
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
            _install_requirements(target)
            installed.append(folder)
            print(f"[bridge] local-node ✓ {folder} ({len(ok)} files)")
        except Exception as e:
            print(f"[bridge] ⚠ 本地节点包 {folder} 解压失败(跳过): {e}")
    if installed:
        print(f"[bridge] 本地节点装载完成: {', '.join(installed)}")
    return installed
