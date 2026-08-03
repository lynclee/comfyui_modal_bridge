"""
workflow_check.py — 提交前的工作流静态预检(纯函数,无 ComfyUI 依赖,可单测)。

当前实现:检测各节点「缺必填输入」。典型场景 —— 老工作流里的节点在新版
ComfyUI 里新增了必填 widget(如内置 API 节点 TencentImageToModelNode 的
`generate_type`),老图序列化出的 prompt 没带这个字段,提交到云端才因
`execute() missing 1 required positional argument` 崩。这里按「当前本地节点
定义」提前把它拦下,在点 RunModal 时就提示,而不是等云端报错。

纯逻辑与「怎么拿节点定义」解耦:调用方传入 required_getter(见 routes.py 用
ComfyUI 的 nodes.NODE_CLASS_MAPPINGS 实现),便于单测注入假数据。
"""
from __future__ import annotations

from typing import Callable, Optional, Set


def _has_autogrow_expansion(name: str, inputs: dict) -> bool:
    """prompt 里是否存在该必填项的 V3 Autogrow 展开输入(`<name>.<后缀>`)。

    V3 schema 的动态输入组(`io.Autogrow.Input("values", …)`)在 INPUT_TYPES() 里
    以**模板名**(values)出现在 required,但序列化进 prompt 的是**展开名**
    (values.a / values.b …),模板名本身永远不会作为 key 出现 —— 裸比对必然误报。
    内置 ComfyMathExpression / GLSL / post_processing 等一批节点都用这个机制。
    """
    prefix = name + "."
    return any(isinstance(k, str) and k.startswith(prefix) for k in inputs)


def find_missing_required_inputs(
    prompt: dict,
    required_getter: Callable[[str], Optional[Set[str]]],
) -> list[dict]:
    """找出 prompt 里「缺必填输入」的节点。

    prompt: ComfyUI prompt,形如 {node_id: {"class_type": str, "inputs": {...}}}。
    required_getter(class_type) -> 该节点类的必填输入名集合;返回 None 表示该类
        未知 / 拿不到定义 → 跳过该节点(宁可漏报也不误报)。

    返回:[{"node_id", "class_type", "missing": [必填但 prompt 未提供的输入名]}],
    按 node_id 排序,只含确有缺失的节点。inputs 里已存在的键(无论是 widget 值还是
    [来源节点, 槽位] 的连线)都算「已提供」,只有完全不存在的必填键才算缺。
    V3 Autogrow 动态输入组按展开名匹配(见 _has_autogrow_expansion);整组一项都没
    展开时仍按缺失报出(模板 min≥1 时确实缺)。
    """
    out: list[dict] = []
    if not isinstance(prompt, dict):
        return out
    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        if not class_type:
            continue
        req = required_getter(class_type)
        if not req:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            inputs = {}
        missing = [k for k in req
                   if k not in inputs and not _has_autogrow_expansion(k, inputs)]
        if missing:
            out.append({
                "node_id": str(node_id),
                "class_type": class_type,
                "missing": sorted(missing),
            })
    out.sort(key=lambda r: r["node_id"])
    return out
