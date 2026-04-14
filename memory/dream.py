"""Dream 记忆整理器：统一整理 core.md 和 experience.md。"""

import re

from astrbot.api import logger

from .store import MemoryStore


class DreamManager:
    def __init__(self, llm_caller, store: MemoryStore):
        """
        Args:
            llm_caller: 异步函数，签名为 async (system_prompt, prompt, caller_name=) -> str
            store: MemoryStore 实例
        """
        self.llm_caller = llm_caller
        self.store = store

    async def organize_core(
        self,
        umo: str,
        topic_id: str,
        topic_name: str,
        instruction: str = "",
    ) -> None:
        """整理单个主题的 core.md，统一 prompt，可选带用户指令。"""
        core = await self.store.load_core_md(umo, topic_id)
        fragments = await self.store.load_all_fragments(umo, topic_id)
        if not fragments:
            return

        # 清理 core.md 中引用不存在的 fragment ID 的条目
        valid_ids = {f["id"] for f in fragments}
        lines = core.split("\n") if core else []
        cleaned_lines = []
        removed = 0
        for line in lines:
            match = re.search(r"\(ID:\s*(\S+)\)", line)
            if match:
                frag_id = match.group(1).rstrip(")")
                if frag_id not in valid_ids:
                    removed += 1
                    continue
            cleaned_lines.append(line)
        if removed > 0:
            core = "\n".join(cleaned_lines)
            await self.store.save_core_md(umo, topic_id, core)
            logger.info(
                f"[Dream] 主题 {topic_name}: 清理了 {removed} 条无效的 fragment 引用"
            )

        fragments_summary = "\n".join(
            f"- [{f.get('created_at', '')[:10]}] {f.get('summary', '')} (ID: {f.get('id', '')})"
            for f in fragments
        )

        # 去掉「最近记忆」部分，让 LLM 基于 fragment 摘要列表重新整理
        core_without_recent = re.sub(
            r"\n*## 最近记忆\n[\s\S]*", "", core
        ) if core else ""

        user_instruction = f"\n用户额外指令: {instruction}\n" if instruction else ""

        prompt = f"""你是一个记忆整理助手。以下是某个主题的核心记忆（概述与关键信息）和所有记忆片段的摘要列表。
请整理并输出完整的 core.md，各部分要求如下：

- "概述"：用一段话（2-4句）围绕用户和助手的实际聊天历程来总结——用户聊了什么、讨论了什么、目前处于什么状态。不要泛泛地解释主题概念，要聚焦于对话中实际发生的事情。站在全局视角提炼，不要罗列细节。
- "关键信息"：以 "- " 开头的条目列表。从对话中提取具体的、时效性不强的关键信息，如：用户表达的偏好/习惯、达成的原则/共识、重要的决定或里程碑等。只保留对话中实际出现的信息。不要放进来的：具体的对话细节、临时性的讨论、已经解决的中间问题、泛泛的主题解释。宁可精简也不要凑数。
- "最近记忆"：根据下方「所有片段摘要」重新生成，条数不超过10条。排序以时效性为优先（最新的在前），重要性次之。不再重要的记忆片段可以省略。每条记忆末尾必须保留 (ID: xxx) 标识，ID 必须来自下方「所有片段摘要」中对应的 ID，不要编造。
{user_instruction}
主题: {topic_name}

当前 core.md（概述与关键信息）:
{core_without_recent if core_without_recent else "(空)"}

所有片段摘要:
{fragments_summary}

请直接输出整理后的完整 core.md 内容（保持 markdown 格式），不需要其他说明。

注意：如果已有记忆中出现把用户对助手的称呼错误当成了用户名字（如"用户阿乐"），请纠正为"用户"。

重要：所有涉及时间的描述必须使用绝对日期（如"2025年3月15日"），禁止使用"今天"、"昨天"、"最近"等相对时间。如果原文中已有绝对日期，请保留。"""

        try:
            result = await self.llm_caller(
                system_prompt="你是一个记忆整理助手。",
                prompt=prompt,
                caller_name="Dream.organize_core",
            )
            if result and len(result.strip()) > 20:
                await self.store.save_core_md(umo, topic_id, result.strip())
                logger.info(f"[Dream] 已整理主题 {topic_name} 的 core.md")
            else:
                logger.warning(
                    f"[Dream] 主题 {topic_name} 的 core.md 整理结果为空或过短，跳过更新"
                )
        except Exception as e:
            logger.error(f"[Dream] 整理 core.md 失败 {topic_name}: {e}")

    async def organize_experience(
        self, umo: str, topic_id: str, topic_name: str
    ) -> None:
        """Dream 整理：去重合并经验条目。"""
        existing = await self.store.load_experience_md(umo, topic_id)
        if not existing:
            return

        prompt = f"""以下是某个主题的经验教训文件。请整理：
- 合并相似或重复的经验条目
- 删除已经不再适用或过时的经验
- 让每条经验更精炼、更具可操作性
- 你自主决定最终保留多少条经验
- 保持原有的 markdown 格式

重要：所有涉及时间的描述必须使用绝对日期（如"2025年3月15日"），禁止使用"今天"、"昨天"、"最近"等相对时间。如果原文中已有绝对日期，请保留。

经验文件内容：
{existing}

请直接输出整理后的完整经验文件内容（保持 markdown 格式），不需要其他说明。"""

        try:
            result = await self.llm_caller(
                system_prompt="你是一个记忆整理助手，负责精炼和去重经验教训。",
                prompt=prompt,
                caller_name="Dream.organize_experience",
            )
            result = result.strip()
            if result and len(result) > 20:
                await self.store.save_experience_md(umo, topic_id, result)
                logger.info(f"[Dream] 已整理主题 {topic_name} 的经验文件")
        except Exception as e:
            logger.error(f"[Dream] 整理失败 {topic_name}: {e}")
