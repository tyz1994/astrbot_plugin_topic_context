"""主题匹配器：通过 LLM 判断用户消息属于哪个已有主题。"""

from astrbot.api import logger

from .store import MemoryStore


class TopicMatcher:
    def __init__(self, llm_caller, store: MemoryStore):
        """
        Args:
            llm_caller: 异步函数，签名为 async (system_prompt, prompt) -> str
            store: MemoryStore 实例
        """
        self.llm_caller = llm_caller
        self.store = store

    async def match(
        self,
        umo: str,
        user_message: str,
        prev_round: str = "",
    ) -> list[dict]:
        """匹配用户消息到已有主题，支持多主题匹配。

        Args:
            umo: 用户标识
            user_message: 用户消息文本
            prev_round: 上一轮对话的文本（user+assistant），帮助 LLM 判断延续性

        Returns:
            匹配到的主题字典列表，如果没有匹配到则返回空列表。
        """
        index = await self.store.load_topics_index(umo)
        topics = index.get("topics", [])

        if not topics:
            return []

        # 为每个主题加载 core.md 中的概述，帮助 LLM 理解主题含义
        topic_entries = []
        for t in topics:
            core_md = await self.store.load_core_md(umo, t["id"])
            # 提取 ## 概述 到下一个 ## 之间的内容
            summary = ""
            if core_md:
                lines = core_md.split("\n")
                in_summary = False
                for line in lines:
                    if line.strip() == "## 概述":
                        in_summary = True
                        continue
                    if in_summary:
                        if line.startswith("## "):
                            break
                        summary += line + "\n"
                summary = summary.strip()

            entry = f'- {t["name"]}'
            if summary:
                entry += f'\n  概述: {summary}'
            topic_entries.append(entry)

        topics_desc = "\n".join(topic_entries)

        # 构建上下文：上一轮对话
        context_section = ""
        if prev_round:
            context_section = f"\n上一轮对话:\n{prev_round}\n"

        prompt = f"""请判断以下用户消息与哪些已有主题相关。{context_section}
用户消息: {user_message}

已有主题列表：
{topics_desc}

判断规则：
- 如果用户消息是对之前话题的延续（如简短确认、追问、补充等），归入同一主题
- 如果用户消息开启了全新的话题，不返回任何主题
- 如果用户消息与多个主题相关，返回所有相关主题
- 只返回真正相关且能为回答提供有用背景的主题

每行返回一个主题名称（一字不差），不要包含其他文字。如果没有相关主题则返回 NEW。"""

        try:
            result = await self.llm_caller(
                system_prompt="你是一个主题分类助手。每行返回一个主题名称，或返回 NEW。",
                prompt=prompt,
                caller_name="TopicMatcher.match",
            )
            result = result.strip()

            if result.upper() == "NEW":
                return []

            # 按主题名称匹配，支持多行输出
            matched = []
            topic_name_set = set(t["name"] for t in topics)
            for line in result.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line in topic_name_set:
                    for t in topics:
                        if t["name"] == line and t not in matched:
                            matched.append(t)
                            break

            if matched:
                names = [t["name"] for t in matched]
                logger.debug(f"[TopicMatcher] 匹配到主题: {names}")

            return matched

        except Exception as e:
            logger.error(f"[TopicMatcher] 匹配失败: {e}")
            return []
