"""上下文注入器：将 core.md 和 experience.md 注入到 LLM 请求中。"""

from astrbot.api import logger

from .store import MemoryStore


class ContextInjector:
    def __init__(self, store: MemoryStore):
        self.store = store

    async def inject(
        self,
        umo: str,
        matched_topics: list[dict],
        system_prompt: str,
    ) -> str:
        """将匹配到的所有主题的核心记忆和经验教训追加到 system_prompt 中。

        不替换原始 system_prompt，而是在其后追加记忆上下文作为补充。

        Args:
            umo: 用户标识
            matched_topics: 匹配到的主题字典列表，每个包含 "id" 和 "name"
            system_prompt: 原始的 system_prompt

        Returns:
            追加记忆后的 system_prompt
        """
        if not matched_topics:
            return system_prompt

        # 收集所有主题的记忆内容
        topic_blocks = []
        for topic in matched_topics:
            topic_id = topic["id"]
            topic_name = topic["name"]

            core_content = await self.store.load_core_md(umo, topic_id)
            experience_content = await self.store.load_experience_md(umo, topic_id)

            if not core_content and not experience_content:
                continue

            block = f"### 主题: {topic_name}\n"
            if core_content:
                block += f"{core_content}\n"
            if experience_content:
                # 提取经验条目（去掉标题行）
                exp_lines = experience_content.strip().split("\n")
                exp_entries = [line for line in exp_lines if line.startswith("- ") or line.startswith("→ ")]
                if exp_entries:
                    block += "#### 经验教训\n"
                    for line in exp_entries[:10]:
                        block += f"{line}\n"

            topic_blocks.append(block)

        if not topic_blocks:
            return system_prompt

        topic_names = [t["name"] for t in matched_topics]
        memory_block = (
            f"\n[记忆上下文]\n"
            f"相关主题: {', '.join(topic_names)}\n\n"
            + "\n".join(topic_blocks)
            + "\n## 记忆检索工具\n"
            "如果需要查阅之前的详细对话，你可以使用以下工具：\n"
            "- read_memory_fragment: 按片段 ID 读取完整记忆内容\n"
            "- search_memory_fragments: 按关键词搜索记忆片段\n"
            "[记忆上下文结束]"
        )

        return system_prompt + "\n" + memory_block
