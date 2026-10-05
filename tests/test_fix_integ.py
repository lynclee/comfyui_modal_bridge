"""fix1005 合并时主会话补的跨模块对接(2026-10-05 深度 review)。"""
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import modal_client  # noqa: E402
import node_sync  # noqa: E402


def test_deploy_writes_the_secret_with_merge_semantics_and_clears_aigc_explicitly():
    """C16:/deploy 改用合并语义;用户清空 AIGC 集成时要显式清掉,否则旧地址 / 密钥一直留在云端。"""
    src = (ROOT / "routes.py").read_text(encoding="utf-8")
    assert "node_sync.secret_create_cmd(" not in src, "/deploy 还在整份重建 Secret"
    i = src.index("node_sync.secret_upsert_cmd(")
    seg = src[src.rindex("_clear = ", 0, i):i + 300]
    assert '"aigc_base_url", aigc_base_url' in seg and '"aigc_bypass_secret", aigc_bypass' in seg
    assert "clear=_clear" in seg
    cmd = node_sync.secret_upsert_cmd({"modal_app_name": "a"}, "", "", "bk-1", "", "", "",
                                      clear=("aigc_base_url", "aigc_bypass_secret"))
    assert "--clear=AIGC_STUDIO_BASE_URL" in cmd and "--clear=AIGC_STUDIO_BYPASS_SECRET" in cmd
    assert not any(c.startswith("--clear=HF_TOKEN") for c in cmd), "没点名清的凭据必须保持不动"


def test_list_nodes_refuses_a_health_reply_without_the_node_list():
    """缺 custom_nodes 时以前回 [],被当成「云端一个节点都没有」,对账与 cloud_unchecked 全被跳过。"""
    async def fake_health(session, cfg):
        return {"healthy": True, "custom_nodes_error": "boom"}
    orig = modal_client.health
    modal_client.health = fake_health
    try:
        try:
            asyncio.run(modal_client.list_nodes(None, {}))
            assert False, "缺节点清单必须抛"
        except RuntimeError as e:
            assert "boom" in str(e)
    finally:
        modal_client.health = orig


def test_frontend_shows_registry_versions_instead_of_blank_commits():
    """Registry 装的节点 commit 为空,以前确认框里显示成空白。"""
    js = (ROOT / "web" / "modal_bridge.js").read_text(encoding="utf-8")
    assert not re.search(r"m\.commit\.slice\(", js), "还有直接切 commit 的地方"
    assert "function nodeRevNew(" in js and "m.old_version" in js
    assert '"node.unclonable"' in js and 'p.reason === "unclonable"' in js
