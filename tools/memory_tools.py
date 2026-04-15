"""function-calling 工具：read_memory_fragment 和 search_memory_fragments。"""

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..memory.store import MemoryStore


def create_memory_tools(store: MemoryStore):
    """创建记忆检索工具。

    Args:
        store: MemoryStore 实例

    Returns:
        FunctionTool 列表
    """
    from astrbot.core.agent.tool import FunctionTool

    async def read_memory_fragment(event: AstrMessageEvent, fragment_id: str) -> str:
        """读取指定记忆片段的完整内容。当需要回忆用户之前某段对话的细节时使用。

        Args:
            fragment_id(string): 片段ID（如 "2024-01-15_103000"）
        """
        umo = event.unified_msg_origin
        if not umo:
            return "错误：无法获取当前用户标识。"

        # 需要在所有主题中搜索该片段
        try:
            index = await store.load_topics_index(umo)
            for topic in index.get("topics", []):
                fragment = await store.load_fragment(umo, topic["id"], fragment_id)
                if fragment:
                    # 格式化输出
                    lines = [f"片段 ID: {fragment['id']}"]
                    lines.append(f"主题: {fragment.get('topic', '')}")
                    lines.append(f"摘要: {fragment.get('summary', '')}")
                    lines.append(f"创建时间: {fragment.get('created_at', '')}")
                    lines.append(f"更新时间: {fragment.get('updated_at', '')}")
                    lines.append(f"关键词: {', '.join(fragment.get('keywords', []))}")
                    lines.append("\n对话详情:")
                    for i, rnd in enumerate(fragment.get("rounds", [])):
                        lines.append(f"\n--- 轮次 {i + 1} ---")
                        lines.append(f"[用户] {rnd.get('user_message', '')}")
                        lines.append(f"[助手] {rnd.get('assistant_response', '')}")
                    return "\n".join(lines)

            return f"未找到 ID 为 {fragment_id} 的记忆片段。"
        except Exception as e:
            logger.error(f"[MemoryTool] read_memory_fragment 失败: {e}")
            return f"读取记忆片段失败: {e}"

    async def search_memory_fragments(event: AstrMessageEvent, keyword: str) -> str:
        """按关键词搜索当前主题下的记忆片段。当 core.md 索引中没有找到需要的记忆时使用。

        Args:
            keyword(string): 搜索关键词
        """
        umo = event.unified_msg_origin
        if not umo:
            return "错误：无法获取当前用户标识。"

        try:
            index = await store.load_topics_index(umo)
            all_results = []

            for topic in index.get("topics", []):
                results = await store.search_fragments_by_keyword(
                    umo, topic["id"], keyword
                )
                for frag in results:
                    all_results.append(
                        {
                            "topic": topic["name"],
                            "topic_id": topic["id"],
                            "fragment_id": frag["id"],
                            "summary": frag.get("summary", ""),
                            "rounds_count": len(frag.get("rounds", [])),
                            "created_at": frag.get("created_at", ""),
                        }
                    )

            if not all_results:
                return f"未找到与 '{keyword}' 相关的记忆片段。"

            lines = [f"找到 {len(all_results)} 个相关片段:\n"]
            for r in all_results:
                lines.append(
                    f"- [{r['topic']}] {r['summary']} "
                    f"(ID: {r['fragment_id']}, {r['rounds_count']}轮, {r['created_at'][:10]})"
                )

            lines.append("\n使用 read_memory_fragment 工具读取具体片段的完整内容。")
            return "\n".join(lines)

        except Exception as e:
            logger.error(f"[MemoryTool] search_memory_fragments 失败: {e}")
            return f"搜索记忆片段失败: {e}"

    tool_read = FunctionTool(
        name="read_memory_fragment",
        description="读取指定记忆片段的完整内容。当需要回忆用户之前某段对话的细节时使用。",
        parameters={
            "type": "object",
            "properties": {
                "fragment_id": {
                    "type": "string",
                    "description": "片段ID（如 2024-01-15_103000）",
                }
            },
            "required": ["fragment_id"],
        },
        handler=read_memory_fragment,
    )

    tool_search = FunctionTool(
        name="search_memory_fragments",
        description="按关键词搜索记忆片段。当 core.md 索引中没有找到需要的记忆时使用。",
        parameters={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词",
                }
            },
            "required": ["keyword"],
        },
        handler=search_memory_fragments,
    )

    return [tool_read, tool_search]
