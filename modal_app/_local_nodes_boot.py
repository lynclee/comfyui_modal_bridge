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
import subprocess
import sys
import zipfile
from pathlib import Path

VOL_DIR = Path("/comfy-volume/_local_nodes")
DEST_DIR = Path("/comfyui/custom_nodes")


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
                    import shutil
                    shutil.rmtree(target, ignore_errors=True)
                target.mkdir(parents=True, exist_ok=True)
                for n in ok:
                    z.extract(n, target)
            _install_requirements(target)
            installed.append(folder)
            print(f"[bridge] local-node ✓ {folder} ({len(ok)} files)")
        except Exception as e:
            print(f"[bridge] ⚠ 本地节点包 {folder} 解压失败(跳过): {e}")
    if installed:
        print(f"[bridge] 本地节点装载完成: {', '.join(installed)}")
    return installed
