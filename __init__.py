"""
comfyui_modal_bridge — 把当前工作流推送到 Modal Serverless 跑,结果回本地 output。

第一版职责:
  1. 注册 web 资源(放按钮 JS)
  2. 注册本地 HTTP routes(/modal_bridge/queue 等)
  3. 启动时确保配置文件存在

不提供 ComfyUI 节点(纯 bridge,不出现在画板节点列表)。
"""

# 把 web/ 暴露给前端(modal_bridge.js 会被 ComfyUI 自动加载)
WEB_DIRECTORY = "./web"

# 不注册任何节点
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}


# 注册 routes(import 时副作用)
try:
    from . import routes  # noqa: F401
    from . import config as _cfg
    _cfg.ensure_config()
    print("[modal_bridge] ✓ loaded — endpoint:", _cfg.load_config().get("modal_endpoint_base"))
except Exception as e:
    print(f"[modal_bridge] ✗ load failed: {e}")
    import traceback
    traceback.print_exc()


__all__ = ["WEB_DIRECTORY", "NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
