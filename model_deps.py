"""
model_deps.py — 从工作流 prompt 解析需要的模型文件(纯逻辑,无文件系统,可单测)。

两条互补的路:
  1) LOADER_MAP:已知 loader 节点 → 精确知道模型 type(ComfyUI 的 models/<type>/ 目录)。
  2) 通用兜底:扫所有节点的所有 string input,命中模型扩展名的文件名 —— 覆盖 LOADER_MAP
     之外的 loader(新自定义节点很常见)。扩展名集合与前端 Export API
     (web/modal_bridge.js 的 /\.(safetensors|ckpt|pt|pth|gguf|bin|sft)$/i)保持一致,
     避免前后端漂移导致"Export 列了模型、后端却不上传"。

通用兜底拿到的只是文件名(不知道 type)。type 需要按本地命中位置反推,涉及文件系统,
放在 routes.py(用 folder_paths);这里只做纯解析。
"""
from pathlib import Path

# 与前端 Export API 的扩展名集合严格对齐(见模块 docstring)。注意:不含图片扩展名,
# 所以 LoadImage 的 .png/.jpg 天然不会被通用兜底误中。
MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin", ".sft"}


# class_type → (模型 type, [输入字段名...])
LOADER_MAP = {
    "CheckpointLoaderSimple":  ("checkpoints",     ["ckpt_name"]),
    "CheckpointLoader":        ("checkpoints",     ["ckpt_name"]),
    "UNETLoader":              ("diffusion_models", ["unet_name"]),
    "DiffusionModelLoader":    ("diffusion_models", ["model_name"]),
    "VAELoader":               ("vae",             ["vae_name"]),
    "CLIPLoader":              ("text_encoders",   ["clip_name"]),
    "DualCLIPLoader":          ("text_encoders",   ["clip_name1", "clip_name2"]),
    "TripleCLIPLoader":        ("text_encoders",   ["clip_name1", "clip_name2", "clip_name3"]),
    "CLIPVisionLoader":        ("clip_vision",     ["clip_name"]),
    "StyleModelLoader":        ("style_models",    ["style_model_name"]),
    "LoraLoader":              ("loras",           ["lora_name"]),
    "LoraLoaderModelOnly":     ("loras",           ["lora_name"]),
    "ControlNetLoader":        ("controlnet",      ["control_net_name"]),
    "UpscaleModelLoader":      ("upscale_models",  ["model_name"]),
    "GLIGENLoader":            ("gligen",          ["gligen_name"]),
    "PulidFluxModelLoader":    ("pulid",           ["pulid_file"]),
}


def extract_loader_models(prompt: dict) -> list[dict]:
    """LOADER_MAP 命中的模型(已知 type)。返回 [{type, filename}, ...] 去重。"""
    deps: list[dict] = []
    seen: set[tuple] = set()
    for node in (prompt or {}).values():
        if not isinstance(node, dict):
            continue
        spec = LOADER_MAP.get(node.get("class_type", ""))
        if not spec:
            continue
        type_, fields = spec
        ins = node.get("inputs") or {}
        for f in fields:
            name = ins.get(f)
            if isinstance(name, str) and name.strip():
                key = (type_, name)
                if key in seen:
                    continue
                seen.add(key)
                deps.append({"type": type_, "filename": name})
    return deps


def extract_generic_filenames(prompt: dict) -> set:
    """通用兜底:扫所有节点所有 string input,命中模型扩展名的文件名(取 basename,去重)。
    覆盖 LOADER_MAP 之外的 loader。返回 set[str]。"""
    found: set = set()
    for node in (prompt or {}).values():
        if not isinstance(node, dict):
            continue
        ins = node.get("inputs") or {}
        if not isinstance(ins, dict):
            continue
        for v in ins.values():
            if isinstance(v, str) and Path(v).suffix.lower() in MODEL_EXTS:
                found.add(Path(v).name)
    return found
