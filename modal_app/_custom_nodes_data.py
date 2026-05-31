"""
_custom_nodes_data.py — Modal 镜像里要装的 custom_nodes 清单(纯数据)

⚠️ 这个文件由 ComfyUI 里的「一键添加缺失节点」按钮自动维护(routes.py / node_sync.py)。
   手动加也行,格式保持每条一个 dict:{"name","url","commit"}。
   - name:  custom_nodes 下的文件夹名(必须和 git clone 出来的目录名一致)
   - url:   git 仓库地址
   - commit: pin 的 commit sha(防止 master HEAD 漂移;留空字符串则跟随默认分支 HEAD)

modal_image.py 在 build 时读这个列表生成 git clone 命令。
改这里 → 重新 `modal deploy` → 只重 build 节点那两层(clone + 装依赖),不影响其它层。
"""

CUSTOM_NODES = [
    {
        "name": "ComfyUI-KJNodes",
        "url": "https://github.com/kijai/ComfyUI-KJNodes.git",
        "commit": "450dc91069e28496bbd67bd657f820ef0cb8d5ba",  # 2026-05-24 main
    },
    {
        "name": "rgthree-comfy",
        "url": "https://github.com/rgthree/rgthree-comfy.git",
        "commit": "738105af5fb14e96fbecaf406dc356e284797e8c",  # 2026-05-09 main
    },
    {
        "name": "ComfyUI_essentials",
        "url": "https://github.com/cubiq/ComfyUI_essentials.git",
        "commit": "9d9f4bedfc9f0321c19faf71855e228c93bd0dc9",
    },
]
