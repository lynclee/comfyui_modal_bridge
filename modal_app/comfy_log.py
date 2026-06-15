"""
comfy_log.py — 纯函数:解析 ComfyUI 启动输出里的「自定义节点导入结果」。

只用标准库,所以既能在 Modal 容器里 import(由 modal_image.add_local_python_source 带入),
也能被 tests 单测(不依赖 ComfyUI / modal / 文件系统)。

ComfyUI 启动时打印形如:

    Import times for custom nodes:
       0.0 seconds: /comfyui/custom_nodes/websocket_image_save.py
       0.1 seconds: /comfyui/custom_nodes/rgthree-comfy
       0.5 seconds (IMPORT FAILED): /comfyui/custom_nodes/ComfyUI-Broken

带 `(IMPORT FAILED)` 的就是导入失败(和当前 ComfyUI 版本不兼容 / 缺依赖 / commit 坏)。
另外 ComfyUI 还会单独打印一行 `Cannot import <path> module for custom nodes: <err>`,
用来给失败节点补一条错误原因。
"""
import os
import re

# 形如:   "   0.5 seconds (IMPORT FAILED): /comfyui/custom_nodes/Foo"
#         "   0.0 seconds: /comfyui/custom_nodes/bar.py"
_TIME_LINE = re.compile(
    r"^\s*[\d.]+\s+seconds(?P<failed>\s*\(IMPORT FAILED\))?\s*:\s*(?P<path>.+?)\s*$"
)
# 形如:"Cannot import /comfyui/custom_nodes/Foo module for custom nodes: No module named 'x'"
_CANNOT_IMPORT = re.compile(
    r"Cannot import\s+(?P<path>.+?)\s+module for custom nodes:\s*(?P<err>.+?)\s*$"
)


def _node_name(path: str) -> str:
    """从导入行的路径取节点名(目录名;单文件节点去掉 .py)。"""
    name = os.path.basename(path.rstrip("/"))
    if name.endswith(".py"):
        name = name[:-3]
    return name


def parse_import_failures(text: str) -> dict:
    """解析 ComfyUI 启动日志,返回 {"ok": [name...], "failed": [{"name","error"}...]}。

    - ok:     成功导入的自定义节点名
    - failed: 导入失败的(name + 可选 error 原因)
    解析 "Import times for custom nodes:" 块里每行的 (IMPORT FAILED) 标记;
    error 从单独的 "Cannot import ... module for custom nodes: ..." 行补齐(没有就 None)。
    """
    # 1) 先收集每个失败节点的错误原因(全局扫,不限块内)
    errors: dict[str, str] = {}
    for line in text.splitlines():
        m = _CANNOT_IMPORT.search(line)
        if m:
            errors[_node_name(m.group("path"))] = m.group("err")

    # 2) 进入 "Import times" 块逐行解析 ok / failed
    ok: list[str] = []
    failed: list[dict] = []
    seen: set[str] = set()
    in_block = False
    for line in text.splitlines():
        if "Import times for custom nodes" in line:
            in_block = True
            continue
        if not in_block:
            continue
        m = _TIME_LINE.match(line)
        if not m:
            # 块结束(遇到第一条不符合"X seconds: path"的行)
            break
        name = _node_name(m.group("path"))
        if name in seen:
            continue
        seen.add(name)
        if m.group("failed"):
            failed.append({"name": name, "error": errors.get(name)})
        else:
            ok.append(name)
    return {"ok": ok, "failed": failed}
