"""测试全局护栏:测试进程里的 modal SDK 永远连不到真实的 Modal。

2026-10-05 修复时踩过:给 bridge_cli 新加的「查 app 是否已部署」还没在测试里桩掉,全量一跑,两条现有测试就用
容器里的真实凭据向 Modal 控制面发了查询(只读,但读到了用户的生产 app)。新增代码漏桩是迟早的事,
所以在这里兜底:把控制面地址指向一个必然拒绝连接的本地端口,并换成假凭据 —— 漏桩的调用会立刻失败,
而不是悄悄出网。离线构建 Image 对象、dump Dockerfile 这类不联网的用法不受影响。
用 setdefault:CI 或个人想显式指向别处时可以覆盖。
"""
import os

os.environ.setdefault("MODAL_SERVER_URL", "http://127.0.0.1:9")
os.environ.setdefault("MODAL_TOKEN_ID", "ak-test-no-network")
os.environ.setdefault("MODAL_TOKEN_SECRET", "as-test-no-network")
os.environ.setdefault("MODAL_CONFIG_PATH", os.devnull)
